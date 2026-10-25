# -*- coding: utf-8 -*-
"""AI 批量任务层：以「批次」为单位提交、跟踪、重试与取消。

用法：
    ai_jobs.register_handler("translate", fn)      # fn(movie_id, payload) -> dict
    batch_id, ids = ai_jobs.submit("translate", items, payload={"target": "zh-Hans"},
                                   meta={"scope": "untranslated", "budget": 2000000})

设计要点（与 docs/AI能力设计.md 对应）：
  * 一次提交 = 一批（ai_batches 一行），逐片任务在 ai_jobs 里，靠 batch_id 关联
  * 状态机：批 queued → running → done / cancelled；片 queued → running → done/failed/skipped/cancelled
  * 并发、预算可**按批次覆盖**全局配置（ai.job_concurrency / ai.budget_chars_per_run）
  * 取消、重试都是批次粒度：取消只影响本批排队项，重试把失败/跳过项复制成新批次
  * 失败只影响该片，不影响其他任务与库内数据
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import ai_core
from .config import load_config
from .db import connect, db, query_all, query_one

_HANDLERS: Dict[str, Callable[[int, Dict[str, Any]], Dict[str, Any]]] = {}

_STATE_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {"running": False, "message": "", "started_at": 0.0, "ended_at": 0.0}
_WORKERS: List[threading.Thread] = []
_TASKS: "queue.Queue" = queue.Queue()

# 历史批次保留数量：超出后连同明细一起清理，避免 ai_jobs 无限增长
KEEP_BATCHES = 30


def register_handler(kind: str, fn: Callable[[int, Dict[str, Any]], Dict[str, Any]]) -> None:
    _HANDLERS[kind] = fn


def handlers() -> List[str]:
    return sorted(_HANDLERS)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _one(sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
    conn = connect()
    try:
        return query_one(conn, sql, params)
    finally:
        conn.close()


def _all(sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        return query_all(conn, sql, params)
    finally:
        conn.close()


# ------------------------------------------------------------------ 提交


def submit(kind: str, items: List[Dict[str, Any]], payload: Optional[Dict[str, Any]] = None,
           meta: Optional[Dict[str, Any]] = None) -> Tuple[str, List[int]]:
    """创建批次并立刻开始执行。返回 (batch_id, job_ids)。"""
    if kind not in _HANDLERS:
        raise ValueError("未知任务类型：%s" % kind)
    items = [i for i in items if i and i.get("movie_id")]
    if not items:
        raise ValueError("没有可执行的任务（影片列表为空）")

    params = dict(payload or {})
    for key in ("scope", "filters", "limit", "concurrency", "budget"):
        if meta and meta.get(key) is not None:
            params[key] = meta[key]

    batch_id = uuid.uuid4().hex[:12]
    ids: List[int] = []
    with db() as conn:
        conn.execute(
            "INSERT INTO ai_batches(id, kind, params, total, status, created_at, started_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (batch_id, kind, json.dumps(params, ensure_ascii=False), len(items),
             "queued", _now(), _now()))
        for it in items:
            cur = conn.execute(
                "INSERT INTO ai_jobs(movie_id, kind, payload, status, batch_id, "
                "created_at, updated_at) VALUES(?,?,?,'queued',?,?,?)",
                (int(it["movie_id"]), kind,
                 json.dumps({"payload": payload or {}, "extra": it}, ensure_ascii=False),
                 batch_id, _now(), _now()))
            ids.append(int(cur.lastrowid))

    with _STATE_LOCK:
        if not _STATE["running"]:
            _STATE.update(running=True, message="", started_at=time.time(), ended_at=0.0)
    for jid in ids:
        _TASKS.put(jid)
    _start_workers(_concurrency_of(params))
    _cleanup_old_batches()
    return batch_id, ids


def _concurrency_of(params: Dict[str, Any]) -> int:
    try:
        n = int(params.get("concurrency") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        try:
            n = int((load_config().get("ai") or {}).get("job_concurrency", 2) or 2)
        except (TypeError, ValueError):
            n = 2
    return max(1, min(8, n))


def _budget_of(batch: Dict[str, Any]) -> int:
    """批次预算：批次参数优先，其次全局配置；0 = 不限。"""
    try:
        params = json.loads(batch.get("params") or "{}")
    except Exception:
        params = {}
    try:
        b = int(params.get("budget") or 0)
    except (TypeError, ValueError):
        b = 0
    if b > 0:
        return b
    try:
        return int((load_config().get("ai") or {}).get("budget_chars_per_run", 0) or 0)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ 执行


def _start_workers(n: int = 0) -> None:
    alive = [t for t in _WORKERS if t.is_alive()]
    _WORKERS[:] = alive
    if n <= 0:
        try:
            n = int((load_config().get("ai") or {}).get("job_concurrency", 2) or 2)
        except (TypeError, ValueError):
            n = 2
    need = max(0, max(1, min(8, n)) - len(alive))
    for i in range(need):
        t = threading.Thread(target=_worker, name="avm-ai-job-%d" % (len(alive) + i), daemon=True)
        t.start()
        _WORKERS.append(t)


def _worker() -> None:
    while True:
        try:
            jid = _TASKS.get(timeout=0.6)
        except queue.Empty:
            if _queue_idle():
                break
            continue
        if jid is None:
            break
        try:
            _run_one(jid)
        except Exception as exc:  # 兜底：worker 绝不能因单条异常退出
            ai_core.log.warning("AI 任务异常（#%s）：%s", jid, exc)


def _queue_idle() -> bool:
    row = _one("SELECT COUNT(*) AS n FROM ai_jobs WHERE status IN ('queued','running')")
    return int((row or {}).get("n") or 0) == 0


def _run_one(job_id: int) -> None:
    job = _one("SELECT * FROM ai_jobs WHERE id=?", (job_id,))
    if not job or job.get("status") != "queued":
        return
    batch = _one("SELECT * FROM ai_batches WHERE id=?", (job.get("batch_id") or "",))
    if not batch:
        _update_job(job_id, status="cancelled", error="批次已删除")
        return
    if batch.get("status") == "cancelled":
        _update_job(job_id, status="cancelled", error="批次已取消")
        _refresh_batch(batch["id"])
        return

    # 预算守卫：跑之前就检查，用尽则跳过（而不是跑完再停）
    budget = _budget_of(batch)
    if budget > 0 and int(batch.get("chars") or 0) >= budget:
        _update_job(job_id, status="skipped", error="预算已用完，任务已跳过")
        _refresh_batch(batch["id"])
        _skip_rest(batch, "预算已用完，剩余任务已跳过")
        return

    _update_job(job_id, status="running", started_at=_now())
    _refresh_batch(batch["id"])

    info = json.loads(job.get("payload") or "{}")
    payload = info.get("payload") or {}
    t0 = time.time()
    try:
        fn = _HANDLERS[job["kind"]]
        result = fn(int(job["movie_id"]), payload) or {}
        _update_job(job_id, status="done",
                    result_path=str(result.get("path") or ""),
                    chars=int(result.get("chars") or 0),
                    model=str(result.get("model") or ""),
                    error="", ended_at=_now(),
                    duration_ms=int((time.time() - t0) * 1000))
    except Exception as exc:  # noqa: BLE001 - 任务隔离：任何异常只记为该片失败
        _update_job(job_id, status="failed", error=str(exc)[:500], ended_at=_now(),
                    duration_ms=int((time.time() - t0) * 1000))
        ai_core.log.warning("AI 任务失败（%s #%s）：%s", job.get("kind"), job_id, exc)
    finally:
        _refresh_batch(batch["id"])
        fresh = _one("SELECT chars FROM ai_batches WHERE id=?", (batch["id"],)) or {}
        limit = _budget_of(_one("SELECT * FROM ai_batches WHERE id=?", (batch["id"],)) or {})
        if limit > 0 and int(fresh.get("chars") or 0) >= limit:
            _skip_rest(batch, "预算已用完，剩余任务已跳过")


def _update_job(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join("%s = ?" % k for k in fields)
    with db() as conn:
        conn.execute("UPDATE ai_jobs SET %s, updated_at = ? WHERE id = ?" % sets,
                     (*fields.values(), _now(), job_id))


def _skip_rest(batch: Dict[str, Any], reason: str) -> None:
    with db() as conn:
        conn.execute("UPDATE ai_jobs SET status='skipped', error=?, updated_at=? "
                     "WHERE batch_id=? AND status='queued'",
                     (reason, _now(), batch["id"]))
    _refresh_batch(batch["id"])


def _refresh_batch(batch_id: str) -> None:
    """按明细重算批次计数与状态（幂等，可频繁调用）。"""
    if not batch_id:
        return
    with db() as conn:
        rows = query_all(conn,
                         "SELECT status, COUNT(*) AS n, COALESCE(SUM(chars),0) AS chars "
                         "FROM ai_jobs WHERE batch_id=? GROUP BY status", (batch_id,))
        batch = query_one(conn, "SELECT * FROM ai_batches WHERE id=?", (batch_id,))
        if not batch:
            return
        counts = {r["status"]: int(r["n"]) for r in rows}
        chars = sum(int(r["chars"] or 0) for r in rows)
        waiting = counts.get("queued", 0) + counts.get("running", 0)
        status = batch["status"]
        ended = batch.get("ended_at") or ""
        if status != "cancelled":
            status = "running" if waiting else "done"
            if not waiting and not ended:
                ended = _now()
        conn.execute(
            "UPDATE ai_batches SET done=?, failed=?, skipped=?, chars=?, status=?, "
            "ended_at=? WHERE id=?",
            (counts.get("done", 0), counts.get("failed", 0), counts.get("skipped", 0),
             chars, status, ended, batch_id))

    if not waiting:
        pending = _one("SELECT COUNT(*) AS n FROM ai_jobs WHERE status IN ('queued','running')")
        if not int((pending or {}).get("n") or 0):
            with _STATE_LOCK:
                _STATE["running"] = False
                _STATE["ended_at"] = time.time()
                _STATE["message"] = "本批完成：成功 %d，失败 %d，跳过 %d" % (
                    counts.get("done", 0), counts.get("failed", 0), counts.get("skipped", 0))


# ------------------------------------------------------------------ 取消 / 重试 / 删除


def cancel_batch(batch_id: str) -> Dict[str, Any]:
    with db() as conn:
        batch = query_one(conn, "SELECT id FROM ai_batches WHERE id=?", (batch_id,))
        if not batch:
            raise ValueError("批次不存在")
        n = conn.execute(
            "UPDATE ai_jobs SET status='cancelled', error='批次已取消', updated_at=? "
            "WHERE batch_id=? AND status='queued'", (_now(), batch_id)).rowcount
        conn.execute("UPDATE ai_batches SET status='cancelled', ended_at=? WHERE id=?",
                     (_now(), batch_id))
    return {"ok": True, "batch_id": batch_id, "cancelled": int(n)}


def retry_batch(batch_id: str, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把该批的失败/跳过/取消项复制成一个新批次重新执行。"""
    batch = _one("SELECT * FROM ai_batches WHERE id=?", (batch_id,))
    if not batch:
        raise ValueError("批次不存在")
    rows = _all("SELECT movie_id FROM ai_jobs WHERE batch_id=? AND status IN "
                "('failed','skipped','cancelled') ORDER BY id", (batch_id,))
    if not rows:
        return {"ok": True, "retried": 0, "batch_id": ""}

    params = json.loads(batch.get("params") or "{}")
    for k, v in (overrides or {}).items():
        if v not in (None, ""):
            params[k] = v
    payload = {k: params[k] for k in ("target", "bilingual") if k in params}
    new_id, ids = submit(batch["kind"], [{"movie_id": int(r["movie_id"])} for r in rows],
                         payload=payload, meta=params)
    with db() as conn:
        conn.execute("UPDATE ai_batches SET message=? WHERE id=?",
                     ("重试自批次 %s" % batch_id, new_id))
    return {"ok": True, "retried": len(ids), "batch_id": new_id}


def delete_batch(batch_id: str) -> Dict[str, Any]:
    with db() as conn:
        conn.execute("DELETE FROM ai_jobs WHERE batch_id=?", (batch_id,))
        n = conn.execute("DELETE FROM ai_batches WHERE id=?", (batch_id,)).rowcount
    return {"ok": True, "deleted": int(n)}


def _cleanup_old_batches(keep: int = KEEP_BATCHES) -> None:
    try:
        with db() as conn:
            rows = query_all(
                conn,
                "SELECT id FROM ai_batches ORDER BY created_at DESC, rowid DESC "
                "LIMIT -1 OFFSET ?", (keep,))
            for r in rows:
                conn.execute("DELETE FROM ai_jobs WHERE batch_id=?", (r["id"],))
                conn.execute("DELETE FROM ai_batches WHERE id=?", (r["id"],))
    except Exception:
        pass


# ------------------------------------------------------------------ 查询


def _parse(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    try:
        out["params"] = json.loads(row.get("params") or "{}")
    except Exception:
        out["params"] = {}
    total = int(out.get("total") or 0)
    done = int(out.get("done") or 0)
    failed = int(out.get("failed") or 0)
    skipped = int(out.get("skipped") or 0)
    out["percent"] = round((done + failed + skipped) / total * 100, 1) if total else 0.0
    return out


def batches(limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    rows = _all("SELECT * FROM ai_batches ORDER BY created_at DESC, rowid DESC "
                "LIMIT ? OFFSET ?", (max(1, min(100, int(limit))), max(0, int(offset))))
    return [_parse(r) for r in rows]


def active_batch() -> Optional[Dict[str, Any]]:
    row = _one("SELECT * FROM ai_batches WHERE status IN ('queued','running') "
               "ORDER BY created_at DESC, rowid DESC LIMIT 1")
    return _parse(row) if row else None


def batch_detail(batch_id: str, status: str = "", page: int = 1, size: int = 50) -> Dict[str, Any]:
    batch = _one("SELECT * FROM ai_batches WHERE id=?", (batch_id,))
    if not batch:
        raise ValueError("批次不存在")
    where = ["j.batch_id = ?"]
    args: List[Any] = [batch_id]
    if status:
        where.append("j.status = ?")
        args.append(status)
    cond = " AND ".join(where)
    total = int((_one("SELECT COUNT(*) AS n FROM ai_jobs j WHERE " + cond,
                      tuple(args)) or {}).get("n") or 0)
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    rows = _all(
        "SELECT j.id, j.movie_id, j.status, j.error, j.chars, j.model, j.result_path, "
        "       j.started_at, j.ended_at, j.duration_ms, "
        "       COALESCE(m.code,'') AS code, COALESCE(m.title,'') AS title "
        "FROM ai_jobs j LEFT JOIN movies m ON m.id = j.movie_id "
        "WHERE " + cond + " ORDER BY j.id LIMIT ? OFFSET ?",
        tuple(args) + (size, (page - 1) * size))
    counts = {r["status"]: int(r["n"]) for r in _all(
        "SELECT status, COUNT(*) AS n FROM ai_jobs WHERE batch_id=? GROUP BY status",
        (batch_id,))}
    return {"batch": _parse(batch), "items": rows, "total": total, "page": page,
            "size": size, "pages": max(1, (total + size - 1) // size), "counts": counts}


def status() -> Dict[str, Any]:
    """兼容旧接口：返回当前/最近一批的进度 + 配置回显。"""
    cfg = load_config()
    ai_cfg = cfg.get("ai") or {}
    current = active_batch()
    latest = current or (batches(limit=1) or [None])[0]
    with _STATE_LOCK:
        running = bool(_STATE["running"])
        message = _STATE["message"]
    return {
        "running": running or bool(current),
        "total": int((latest or {}).get("total") or 0),
        "done": int((latest or {}).get("done") or 0),
        "fail": int((latest or {}).get("failed") or 0),
        "skipped": int((latest or {}).get("skipped") or 0),
        "chars": int((latest or {}).get("chars") or 0),
        "message": (latest or {}).get("message") or message,
        "batch": latest or {},
        "batch_id": (latest or {}).get("id") or "",
        "handlers": handlers(),
        "budget_chars_per_run": int(ai_cfg.get("budget_chars_per_run", 0) or 0),
        "job_concurrency": int(ai_cfg.get("job_concurrency", 2) or 2),
        "stats": ai_core.stats(),
    }
