"""极简 5 段 cron 解析与「下次触发时间」计算，只用标准库。

支持 ``分 时 日 月 周`` 五段，每段可写 ``*`` / ``*/n`` / ``a`` / ``a-b`` /
``a-b/n``，多值用逗号连接（如 ``0,30 8-20/2 * * 1-5``）。
周字段 0 与 7 都表示周日；日与周同时限定取值时按标准 cron 语义取「或」。

不支持秒字段、``L`` / ``W`` / ``#`` / ``@daily`` 这类扩展语法——直接报错，
避免写了不生效还静默按别的时刻跑。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, NamedTuple, Set

MAX_SEARCH_DAYS = 366 * 5


class CronError(ValueError):
    """表达式不合法（消息给用户看，中文）。"""


class CronSpec(NamedTuple):
    minutes: Set[int]
    hours: Set[int]
    days: Set[int]
    months: Set[int]
    weekdays: Set[int]          # 0=周日 … 6=周六
    dom_any: bool               # 日字段是否为 *
    dow_any: bool               # 周字段是否为 *
    expr: str


def _int(text: str, lo: int, hi: int, name: str) -> int:
    try:
        v = int(text)
    except (TypeError, ValueError):
        raise CronError("%s 不是数字：%s" % (name, text))
    if v < lo or v > hi:
        raise CronError("%s 超出范围 %d-%d：%d" % (name, lo, hi, v))
    return v


def _parse_field(text: str, lo: int, hi: int, name: str) -> Set[int]:
    """解析单个字段，返回允许值集合。"""
    out: Set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError("%s 字段有空项" % name)
        step = 1
        has_step = "/" in part
        if has_step:
            base, _, step_s = part.partition("/")
            step = _int(step_s, 1, hi, name)
            part = base.strip() or "*"
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start = _int(a.strip(), lo, hi, name)
            end = _int(b.strip(), lo, hi, name)
            if start > end:
                raise CronError("%s 范围起点大于终点：%s" % (name, part))
        else:
            start = _int(part, lo, hi, name)
            # 5/10 这种写法按「从 5 到上限，每 10」处理，与标准 cron 一致
            end = hi if has_step else start
        out.update(range(start, end + 1, step))
    return out


def parse(expr: str) -> CronSpec:
    """把 5 段表达式解析成 CronSpec；不合法抛 CronError。"""
    raw = " ".join(str(expr or "").split())
    if not raw:
        raise CronError("表达式为空")
    fields = raw.split(" ")
    if len(fields) != 5:
        raise CronError("需要 5 段（分 时 日 月 周），当前 %d 段" % len(fields))
    minutes = _parse_field(fields[0], 0, 59, "分钟")
    hours = _parse_field(fields[1], 0, 23, "小时")
    days = _parse_field(fields[2], 1, 31, "日")
    months = _parse_field(fields[3], 1, 12, "月")
    weekdays = {v % 7 for v in _parse_field(fields[4], 0, 7, "星期")}
    return CronSpec(
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        dom_any=fields[2].strip() == "*",
        dow_any=fields[4].strip() == "*",
        expr=raw,
    )


def _day_ok(spec: CronSpec, d: datetime) -> bool:
    if spec.dom_any and spec.dow_any:
        return True
    if spec.dom_any:
        return d.isoweekday() % 7 in spec.weekdays
    if spec.dow_any:
        return d.day in spec.days
    # 两个都限定时是「或」：每月 1 号 或 每周一，都在允许范围内
    return d.day in spec.days or d.isoweekday() % 7 in spec.weekdays


def next_after(spec: CronSpec, after: datetime) -> datetime:
    """返回严格晚于 ``after`` 的下一个触发时刻（本地时间）。

    按「月 → 日 → 时 → 分」逐层跳，不逐分钟硬扫，5 年内找不到就抛 CronError。
    """
    cur = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + timedelta(days=MAX_SEARCH_DAYS)
    while cur <= limit:
        if cur.month not in spec.months:
            year = cur.year + (1 if cur.month == 12 else 0)
            month = 1 if cur.month == 12 else cur.month + 1
            cur = cur.replace(year=year, month=month, day=1, hour=0, minute=0)
            continue
        if not _day_ok(spec, cur):
            cur = cur.replace(hour=0, minute=0) + timedelta(days=1)
            continue
        if cur.hour not in spec.hours:
            cur = cur.replace(minute=0) + timedelta(hours=1)
            # 加一小时可能跨天，下一轮重新检查月 / 日
            continue
        if cur.minute not in spec.minutes:
            cur += timedelta(minutes=1)
            continue
        return cur
    raise CronError("表达式在 5 年内没有任何触发时间")


def upcoming(expr: str, count: int = 5, after: datetime | None = None) -> List[datetime]:
    """预览用：列出接下来 count 个触发时刻。"""
    spec = parse(expr)
    cur = after or datetime.now()
    out: List[datetime] = []
    for _ in range(max(1, min(50, int(count or 5)))):
        cur = next_after(spec, cur)
        out.append(cur)
    return out
