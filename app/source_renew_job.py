# -*- coding: utf-8 -*-
"""在线来源巡检任务：后台批量重新解析过期 / 410 的播放地址。

为什么单独成模块（与 poster_job.py / translate_job.py 同形态）：
  * 一批来源要逐条回源站（服务端抓取 + 无头浏览器兜底），单条 10~30 秒，
    分钟级操作不能同步跑在请求里；
  * 「立即巡检」需要进度、可取消、重复点击返回同一任务 —— 这套状态契约
    每个后台任务各写一份，路由层只留三行薄壳。

失败降级：单条异常只计失败、不中断整批；源站不可达 / 人机校验过不了时由
``stream_verify`` 写下可读原因，前端可引导用户用浏览器插件回写。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

from .config import load_config
from .threadpool import submit_long

_JOB: Dict[str, Any] = {
    "running": False,
    "total": 0,
    "done": 0,
    "renewed": 0,
    "failed": 0,
    "percent": 0,
    "message": "",
    "cancel": False,
    "started_at": 0,
    "finished_at": 0,
}

_LOCK = threading.Lock()


def state() -> Dict[str, Any]:
    with _LOCK:
        return dict(_JOB)


def _set(**fields: Any) -> None:
    with _LOCK:
        _JOB.update(fields)


def start() -> Dict[str, Any]:
    """启动巡检；已在跑时直接返回当前进度（幂等，防重复点击 / 定时器叠加）。"""
    with _LOCK:
        if _JOB.get("running"):
            return dict(_JOB)
        _JOB.update({
            "running": True, "total": 0, "done": 0, "renewed": 0, "failed": 0,
            "percent": 0, "message": "准备中…", "cancel": False,
            "started_at": int(time.time()), "finished_at": 0,
        })
    submit_long(_run)
    return state()


def cancel() -> Dict[str, Any]:
    _set(cancel=True, message="正在取消…")
    return state()


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _run() -> None:
    """任务主体：逐条续期，进度写进状态；异常不外抛，落到 message。"""
    try:
        from . import stream_verify
        stream = load_config(refresh=True).get("stream") or {}
        cooldown = max(1, _int(stream.get("renew_cooldown"), 30))
        rows = stream_verify.due_sources(limit=20, cooldown_minutes=cooldown)
        total = len(rows)
        _set(total=total, message="已开始…" if total else "没有需要续期的来源")
        if not total:
            _set(running=False, finished_at=int(time.time()),
                 message="没有需要续期的来源")
            return
        renewed = failed = 0
        for idx, row in enumerate(rows, 1):
            if state().get("cancel"):
                _set(percent=int((idx - 1) * 100 / total), message="已取消")
                break
            label = str(row.get("code") or row.get("movie_id") or row["id"])
            _set(message="处理中：%s" % label)
            try:
                ok = bool(stream_verify.refresh_source(int(row["id"])).get("ok"))
            except Exception:            # noqa: BLE001 单条失败不影响整批
                ok = False
            if ok:
                renewed += 1
            else:
                failed += 1
            _set(done=idx, renewed=renewed, failed=failed,
                 percent=int(idx * 100 / total))
        _set(running=False, finished_at=int(time.time()),
             message="完成：续期 %d，失败 %d" % (renewed, failed))
    except Exception as e:               # noqa: BLE001 兜底：任务线程不能静默死掉
        _set(running=False, finished_at=int(time.time()),
             message="任务异常：%s" % str(e)[:160])
