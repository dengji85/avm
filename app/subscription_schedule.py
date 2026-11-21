"""订阅自动扫描的调度状态：cron / 固定间隔两种模式，落盘到 ``data/``。

为什么要落盘：Windows 关机或休眠时进程不在，没有系统级定时器能补。把
「上次自动扫描时间 / 下次预计时间」写进 ``data/subscription_schedule.json``，
服务重启后才知道是「刚好错过一次」还是「本来就没到时间」，避免每次重启都
无脑扫一遍站点。

约定：
- ``interval`` 模式：从上次扫描起算间隔（+ 抖动），没有记录时启动即扫一次；
- ``cron`` 模式：按 5 段表达式定点，没有记录时等下一个点；
- ``catch_up_on_start`` 关闭时，两种模式都从启动时刻重新计时，不做补扫。
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from . import cron
from .config import DATA_DIR, load_config

STATE_FILE = DATA_DIR / "subscription_schedule.json"
_TIME_FMT = "%Y-%m-%d %H:%M:%S"


# ------------------------------------------------------------------ 配置
def settings() -> Dict[str, Any]:
    """读订阅调度配置并校正（坏值一律回落到安全默认）。"""
    sub = load_config().get("subscription") or {}
    mode = "cron" if str(sub.get("scan_mode") or "").strip().lower() == "cron" else "interval"
    try:
        interval = int(sub.get("scan_interval_minutes", 360) or 360)
    except (TypeError, ValueError):
        interval = 360
    try:
        jitter = int(sub.get("scan_jitter_minutes", 30) or 0)
    except (TypeError, ValueError):
        jitter = 0
    return {
        "enabled": sub.get("enabled", True) is not False,
        "mode": mode,
        "interval_minutes": max(1, interval),
        "jitter_minutes": max(0, jitter),
        "cron": str(sub.get("scan_cron") or "0 8 * * *").strip(),
        "catch_up": sub.get("catch_up_on_start", True) is not False,
    }


def _signature(st: Dict[str, Any]) -> str:
    """配置指纹：改过模式 / 间隔 / 表达式就重算下次时间。"""
    return "%s|%s|%s|%s" % (st["mode"], st["interval_minutes"], st["jitter_minutes"], st["cron"])


# ------------------------------------------------------------------ 状态文件
def _load() -> Dict[str, Any]:
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 文件缺失 / 损坏都按「没有记录」处理
        return {}


def _save(data: Dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:  # noqa: BLE001 落盘失败不能让后台线程退出
        pass


def _dt(value: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value or ""), _TIME_FMT)
    except (TypeError, ValueError):
        return None


def _fmt(value: datetime) -> str:
    return value.strftime(_TIME_FMT)


# ------------------------------------------------------------------ 时间计算
def _interval_gap(st: Dict[str, Any]) -> timedelta:
    return timedelta(minutes=st["interval_minutes"] + random.randint(0, st["jitter_minutes"]))


def _replan(now: datetime, last: Optional[datetime], st: Dict[str, Any]) -> datetime:
    """算出「下次该跑」的时刻（启动或配置变更时用）。"""
    if st["mode"] == "cron":
        # 补跑：从上次扫描时刻往后找第一个触发点；这个点已经过去说明错过了，
        # 立刻补一次（错过多个点也只补一次，mark_ran 会把下次排到未来）
        base = last if (st["catch_up"] and last is not None) else now
        try:
            nxt = cron.next_after(cron.parse(st["cron"]), base)
        except cron.CronError:
            # 表达式坏了：退化成 5 分钟后再看一次，至少有变化时能自愈
            return now + timedelta(minutes=5)
        if st["catch_up"] and last is not None and nxt <= now:
            return now
        return nxt
    if last is None:
        # 首次启动（没有记录）：补跑开着就立刻扫，关着就等一个完整间隔
        return now if st["catch_up"] else now + _interval_gap(st)
    if not st["catch_up"]:
        return now + _interval_gap(st)
    return last + _interval_gap(st)


def _advance(now: datetime, st: Dict[str, Any]) -> datetime:
    """扫描结束后，从当前时刻算下一次。"""
    if st["mode"] == "cron":
        return cron.next_after(cron.parse(st["cron"]), now)
    return now + _interval_gap(st)


# ------------------------------------------------------------------ 对外
def should_run(now: Optional[datetime] = None) -> bool:
    """调度线程每次醒来问一次：现在该扫了吗？"""
    st = settings()
    if not st["enabled"]:
        return False
    if st["mode"] == "cron":
        try:
            cron.parse(st["cron"])
        except cron.CronError:
            return False        # 表达式写错就不跑，避免按错误配置打站点
    now = now or datetime.now()
    raw = _load()
    due = _dt(raw.get("next_due_at"))
    if raw.get("signature") != _signature(st) or due is None:
        due = _replan(now, _dt(raw.get("last_auto_scan_at")), st)
        _save({
            "signature": _signature(st),
            "last_auto_scan_at": raw.get("last_auto_scan_at"),
            "next_due_at": _fmt(due),
        })
    return now >= due


def mark_ran(now: Optional[datetime] = None) -> None:
    """一次自动扫描结束后调用：记下时间并排下一次。"""
    st = settings()
    now = now or datetime.now()
    try:
        nxt = _advance(now, st)
    except cron.CronError:
        nxt = now + timedelta(minutes=5)
    _save({
        "signature": _signature(st),
        "last_auto_scan_at": _fmt(now),
        "next_due_at": _fmt(nxt),
    })


def state() -> Dict[str, Any]:
    """给前端 / 诊断：当前模式、上次与下次时间、表达式是否合法。"""
    st = settings()
    raw = _load()
    out: Dict[str, Any] = {
        "enabled": st["enabled"],
        "mode": st["mode"],
        "cron": st["cron"],
        "interval_minutes": st["interval_minutes"],
        "catch_up_on_start": st["catch_up"],
        "last_auto_scan_at": raw.get("last_auto_scan_at"),
        "next_due_at": raw.get("next_due_at"),
    }
    if st["mode"] == "cron":
        try:
            out["upcoming"] = [_fmt(d) for d in cron.upcoming(st["cron"], 3)]
            out["valid"] = True
        except cron.CronError as e:
            out["upcoming"] = []
            out["valid"] = False
            out["error"] = str(e)
    # 首次启动调度线程还没写过状态时，按当前配置算一个「预计」值给界面看（不落盘）
    if not out["next_due_at"] and out.get("valid", True):
        try:
            out["next_due_at"] = _fmt(
                _replan(datetime.now(), _dt(raw.get("last_auto_scan_at")), st))
        except Exception:  # noqa: BLE001 只影响展示，不影响调度
            pass
    return out


def preview(expr: str, count: int = 5) -> Dict[str, Any]:
    """表达式预览：返回接下来几次触发时间，供设置界面校验。"""
    try:
        runs: List[datetime] = cron.upcoming(expr, count)
        return {"ok": True, "next": [_fmt(d) for d in runs]}
    except cron.CronError as e:
        return {"ok": False, "error": str(e), "next": []}
