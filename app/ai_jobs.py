# -*- coding: utf-8 -*-
"""AI 任务层：批量执行、限并发、可取消、带预算上限。

用法：
    ai_jobs.register_handler("translate", fn)   # fn(movie_id, payload) -> dict
    ids = ai_jobs.submit("translate", [{"movie_id": 1}, {"movie_id": 2}],
                         payload={"target": "zh-Hans"})

设计要点（与 docs/AI能力设计.md 对应）：
  * 每个任务是一行 ai_jobs，状态机 queued → running → done/failed/cancelled/skipped
  * 固定并发（默认 2），可整体取消；运行中的任务跑完再退出
  * 预算按「本次批次送出的 prompt 字符数」计，超过 `ai.budget_chars_per_run` 即停止
  * 任务失败只影响该片，不影响其他任务与库内已有数据
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import ai_core
from .config import load_config
from .db import connect

_HANDLERS: Dict[str, Callable[[int, Dict[str, Any]], Dict[str, Any]]] = {}

_STATE_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {
    "running": False, "cancelled": False,
    "total": 0, "done": 0, "fail": 0, "skipped": 0, "chars": 0,
    "started_at": 0.0, "ended_at": 0.0, "message": "",
}
_WORKERS: List[threading.Thread] = []
_TASKS: "queue.Queue" = queue.Queue()
_CURRENT_BATCH: List[int] = []


def register_handler(kind: str, fn: Callable[[int, Dict[str, Any]], Dict[str, Any]]) -> None:
    _HANDLERS[kind] = fn


def handlers() -> List[str]:
    return sorted(_HANDLERS)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _update(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join("%s = ?" % k for k in fields)
    with connect() as conn:
        conn.execute("UPDATE ai_jobs SET %s, updated_at = ? WHERE id = ?"
                     % sets, (*fields.values(), _now(), job_id))


def submit(kind: str, items: List[Dict[str, Any]],
           payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """提交一批任务并立即启动工作线程（已有批次在跑则并入同一队列）。"""
    if kind not in _HANDLERS:
        raise ValueError("未知任务类型：%s" % kind)
    items = [i for i in items if i and i.get("movie_id")]
    if not items:
        raise ValueError("没有可执行的任务（影片列表为空）")

    ids: List[int] = []
    with connect() as conn:
        for it in items:
            cur = conn.execute(
                "INSERT INTO ai_jobs(movie_id, kind, payload, status, created_at, updated_at) "
                "VALUES(?,?,?,'queued',?,?)",
                (int(it["movie_id"]), kind,
                 json.dumps({"payload": payload or {}, "extra": it}, ensure_ascii=False),
                 _now(), _now()))
            ids.append(int(cur.lastrowid))

    with _STATE_LOCK:
        if not _STATE["running"]:
            _STATE.update(running=True, cancelled=False, total=len(ids), done=0, fail=0,
                          chars=0, started_at=time.time(), ended_at=0.0,
                          message="")
            with _batch_baseline_lock:
                _BASELINE["chars"] = ai_core.stats().get("prompt_chars", 0)
        else:
            _STATE["total"] += len(ids)
        _CURRENT_BATCH.extend(ids)
        need_start = not _WORKERS or not any(t.is_alive() for t in _WORKERS)

    for jid in ids:
        _TASKS.put(jid)
    if need_start:
        _start_workers()
    return {"ok": True, "job_ids": ids, "count": len(ids)}


def _start_workers() -> None:
    cfg = load_config()
    try:
        n = max(1, int((cfg.get("ai") or {}).get("job_concurrency", 2) or 2))
    except (TypeError, ValueError):
        n = 2
    # 设了预算上限时退化为单线程：否则并发会同时通过预算检查、导致超支
    if _budget_exceeded(baseline=_batch_baseline(), strict=True):
        n = 1
    with _STATE_LOCK:
        _WORKERS.clear()
    for i in range(n):
        t = threading.Thread(target=_worker, name="avm-ai-job-%d" % i, daemon=True)
        t.start()
        _WORKERS.append(t)


def _worker() -> None:
    while True:
        try:
            jid = _TASKS.get(timeout=0.5)
        except queue.Empty:
            if _batch_finished():
                break
            continue
        if jid is None:            # 唤醒后退出（批次结束 / 取消）
            break
        _run_one(jid)


def _batch_finished() -> bool:
    with _STATE_LOCK:
        if _STATE["cancelled"]:
            return True
        return _STATE["done"] + _STATE["fail"] >= _STATE["total"]


def _budget_left(baseline: int) -> Optional[int]:
    cfg = load_config()
    try:
        budget = int((cfg.get("ai") or {}).get("budget_chars_per_run", 0) or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0:
        return None
    used = ai_core.stats().get("prompt_chars", 0) - baseline
    return budget - used


def _budget_exceeded(baseline: Optional[int] = None, strict: bool = False) -> bool:
    """本次批次的 prompt 字符预算是否已用尽。

    ``strict=True`` 用于启动时判断：只要配了预算就按单线程跑（保守，避免并发超支）。
    """
    cfg = load_config()
    try:
        budget = int((cfg.get("ai") or {}).get("budget_chars_per_run", 0) or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0:
        return False
    if strict:
        return True
    left = _budget_left(baseline if baseline is not None else _batch_baseline())
    return left is not None and left <= 0


def _run_one(job_id: int) -> None:
    with _STATE_LOCK:
        if _STATE["cancelled"]:
            _update(job_id, status="cancelled")
            _bump("fail")
            return
    with connect() as conn:
        row = conn.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row["status"] != "queued":
        return

    # 预算守卫：跑之前就检查，已用尽则跳过（而不是跑完再停）
    if _budget_exceeded():
        _update(job_id, status="skipped", error="预算已用完，任务已跳过")
        with _STATE_LOCK:
            _STATE["skipped"] += 1
            _STATE["fail"] += 1
        if _batch_finished():
            _finish_batch("完成")
        return

    _update(job_id, status="running")
    info = json.loads(row["payload"] or "{}")
    payload = (info.get("payload") or {})
    kind = row["kind"]
    try:
        fn = _HANDLERS[kind]
        result = fn(int(row["movie_id"]), payload) or {}
        _update(job_id, status="done", result_path=str(result.get("path") or ""),
                chars=int(result.get("chars") or 0),
                model=str(result.get("model") or ""))
        with _STATE_LOCK:
            _STATE["done"] += 1
    except Exception as e:                       # noqa: BLE001 - 任务隔离：任何异常只记为该片失败
        _update(job_id, status="failed", error=str(e)[:500])
        ai_core.log.warning("AI 任务失败（%s #%s）：%s", kind, job_id, e)
        with _STATE_LOCK:
            _STATE["fail"] += 1
    finally:
        # 预算用尽：把剩余排队任务标记为跳过
        if _batch_finished():
            _finish_batch("完成")
        elif _budget_exceeded():
            _skip_rest("预算已用完，剩余任务已跳过")


_batch_baseline_lock = threading.Lock()
_BASELINE = {"chars": 0}


def _batch_baseline() -> int:
    with _batch_baseline_lock:
        return int(_BASELINE["chars"])


def _bump(key: str) -> None:
    with _STATE_LOCK:
        _STATE[key] = _STATE.get(key, 0) + 1


def _skip_rest(reason: str) -> None:
    with connect() as conn:
        rows = conn.execute("SELECT id FROM ai_jobs WHERE status='queued'").fetchall()
        ids = [int(r["id"]) for r in rows]
        for jid in ids:
            conn.execute("UPDATE ai_jobs SET status='skipped', error=?, updated_at=? WHERE id=?",
                         (reason, _now(), jid))
    for _ in ids:
        _TASKS.put(None)          # 唤醒 worker 让它退出
    _finish_batch(reason)


def _finish_batch(message: str) -> None:
    with _STATE_LOCK:
        if not _STATE["running"]:
            return
        _STATE["running"] = False
        _STATE["ended_at"] = time.time()
        _STATE["message"] = message
        _CURRENT_BATCH.clear()
    with _batch_baseline_lock:
        _BASELINE["chars"] = ai_core.stats().get("prompt_chars", 0)


def cancel_all() -> Dict[str, Any]:
    with _STATE_LOCK:
        if not _STATE["running"]:
            return {"ok": True, "cancelled": 0}
        _STATE["cancelled"] = True
    with connect() as conn:
        n = conn.execute("UPDATE ai_jobs SET status='cancelled', updated_at=? "
                         "WHERE status='queued'", (_now(),)).rowcount
    for _ in range(len(_WORKERS) + 1):
        _TASKS.put(None)
    return {"ok": True, "cancelled": int(n)}


def status() -> Dict[str, Any]:
    with _STATE_LOCK:
        st = dict(_STATE)
    st["kind"] = ""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, movie_id, kind, status, chars, error, updated_at, result_path "
            "FROM ai_jobs ORDER BY id DESC LIMIT 40").fetchall()
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM ai_jobs GROUP BY status").fetchall())
    st["counts"] = counts
    st["items"] = [dict(r) for r in rows]
    st["handlers"] = handlers()
    cfg = load_config()
    ai_cfg = cfg.get("ai") or {}
    st["budget_chars_per_run"] = int(ai_cfg.get("budget_chars_per_run", 0) or 0)
    st["job_concurrency"] = int(ai_cfg.get("job_concurrency", 2) or 2)
    st["stats"] = ai_core.stats()
    return st
