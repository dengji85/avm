# -*- coding: utf-8 -*-
"""订阅扫描任务：后台批量抓取订阅目标的列表页。

形态照 ``source_renew_job.py``：状态字典 + submit_long + 可取消。
扫描本身是分钟级操作（多目标 × 多页 × 页间礼貌延迟），绝不能同步跑在请求里。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from .config import load_config
from .threadpool import submit_long

_JOB: Dict[str, Any] = {
    "running": False,
    "total": 0,
    "done": 0,
    "fetched": 0,
    "new": 0,
    "failed": 0,
    "skipped": 0,
    "archived": 0,
    "percent": 0,
    "message": "",
    "cancel": False,
    "started_at": 0,
    "finished_at": 0,
    "target_ids": [],
    "mode": "",
}

_LOCK = threading.Lock()


def state() -> Dict[str, Any]:
    with _LOCK:
        return dict(_JOB)


def _set(**fields: Any) -> None:
    with _LOCK:
        _JOB.update(fields)


def start(target_ids: Optional[List[int]] = None, mode: str = "") -> Dict[str, Any]:
    """启动扫描；已在跑时直接返回当前进度（幂等，防定时器叠加）。

    ``target_ids`` 非空时只扫这些目标（新增订阅后只检查这一个），
    为空时扫全部启用目标。``mode="backfill"`` 走历史补抓。
    """
    ids = [int(i) for i in (target_ids or [])]
    mode = str(mode or "").strip().lower()
    with _LOCK:
        if _JOB.get("running"):
            return dict(_JOB)
        _JOB.update({
            "running": True, "total": 0, "done": 0, "fetched": 0, "new": 0,
            "failed": 0, "skipped": 0, "archived": 0, "percent": 0,
            "message": "准备中…",
            "cancel": False, "started_at": int(time.time()), "finished_at": 0,
            "target_ids": ids, "mode": mode,
        })
    submit_long(_run, ids, mode)
    return state()


def cancel() -> Dict[str, Any]:
    _set(cancel=True, message="正在取消…")
    return state()


def _progress(idx: int, total: int, message: str, stats: Dict[str, Any]) -> None:
    percent = int(idx * 100 / total) if total else 0
    _set(total=total, done=idx, percent=percent, message=message,
         fetched=int(stats.get("fetched") or 0), new=int(stats.get("new") or 0),
         failed=int(stats.get("failed") or 0), skipped=int(stats.get("skipped") or 0),
         archived=int(stats.get("archived") or 0))


def _run(target_ids: Optional[List[int]] = None, mode: str = "") -> None:
    try:
        from . import subscription
        cfg = load_config(refresh=True)
        sub = cfg.get("subscription") or {}
        if not sub.get("enabled", True):
            _set(running=False, finished_at=int(time.time()), message="订阅功能已关闭")
            return
        stats = subscription.scan_all(progress=_progress,
                                      should_cancel=lambda: bool(state().get("cancel")),
                                      target_ids=target_ids, mode=mode)
        archived = int(stats.get("archived") or 0)
        message = "完成：扫描 %d 个目标，新增 %d 条，失败 %d 个" % (
            int(stats.get("targets") or 0), int(stats.get("new") or 0),
            int(stats.get("failed") or 0))
        if archived:
            message += "，归档 %d 条旧记录" % archived
        _set(running=False, finished_at=int(time.time()), archived=archived,
             message=message)
        # 扫完延迟一轮在线资源探测：新片很快就能带上「有在线资源」标记
        try:
            from . import subscription_probe_job
            subscription_probe_job.schedule_auto()
        except Exception:  # noqa: BLE001 探测排不上不影响扫描结果
            pass
    except Exception as e:               # noqa: BLE001 任务线程不能静默死掉
        _set(running=False, finished_at=int(time.time()),
             message="任务异常：%s" % str(e)[:160])
