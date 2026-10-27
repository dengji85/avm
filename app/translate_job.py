# -*- coding: utf-8 -*-
"""单部字幕翻译后台任务：按影片维护进度，可取消。

为什么需要它：
  单部翻译是分钟级操作（长片要分十几批请求），原来是同步 HTTP 请求——浏览器
  和反代（默认 60s）都会超时，用户既看不到进度，也没法取消。改成后台任务后：
  POST 立即返回，前端轮询 `status` 显示百分比，随时可取消。

与 ai_jobs（批量翻译）的分工：
  * ai_jobs：多部片、有字符预算与批次历史，落库、可重试；
  * translate_job：单部片、内存态、即点即走，成果直接进字幕列表。
  两者都走 translate.translate_movie，翻译逻辑只有一份。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from . import translate
from .threadpool import submit_long

_JOBS: Dict[int, Dict[str, Any]] = {}
_LOCK = threading.Lock()


class _Cancelled(Exception):
    """用户取消：从进度回调里抛出，中断 translate_cues。"""


def _blank(movie_id: int, target: str, bilingual: bool) -> Dict[str, Any]:
    return {
        "movie_id": int(movie_id),
        "status": "idle",          # idle / running / done / error / cancelled
        "percent": 0,
        "done": 0,
        "total": 0,
        "message": "",
        "target": target,
        "bilingual": bool(bilingual),
        "cancel": False,
        "error": "",
        "result": None,
        "started_at": 0,
        "finished_at": 0,
    }


def _set(movie_id: int, **fields: Any) -> None:
    with _LOCK:
        job = _JOBS.setdefault(movie_id, _blank(movie_id, "", False))
        job.update(fields)


def state(movie_id: int) -> Dict[str, Any]:
    with _LOCK:
        job = _JOBS.get(int(movie_id))
        return dict(job) if job else _blank(int(movie_id), "", False)


def start(movie_id: int, cfg: Dict[str, Any], target: str,
          bilingual: bool = False) -> Dict[str, Any]:
    """启动（或返回已在跑的那一次）。同一部片同时只跑一个翻译任务。"""
    movie_id = int(movie_id)
    with _LOCK:
        cur = _JOBS.get(movie_id)
        if cur and cur.get("status") == "running":
            return dict(cur)
        job = _blank(movie_id, target, bilingual)
        job.update(status="running", message="准备中…", started_at=int(time.time()))
        _JOBS[movie_id] = job
    submit_long(_run, movie_id, cfg, target, bool(bilingual))
    return state(movie_id)


def cancel(movie_id: int) -> Dict[str, Any]:
    """标记取消：下一批结束时中断（批次之间不会等太久）。"""
    movie_id = int(movie_id)
    with _LOCK:
        job = _JOBS.get(movie_id)
        if job and job.get("status") == "running":
            job["cancel"] = True
            job["message"] = "正在取消…"
    return state(movie_id)


def _run(movie_id: int, cfg: Dict[str, Any], target: str, bilingual: bool) -> None:
    def progress(done: int, total: int) -> None:
        if state(movie_id).get("cancel"):
            raise _Cancelled()
        _set(movie_id,
             done=done, total=total,
             percent=int(done * 100 / max(1, total)),
             message="已处理 %d/%d 批" % (done, total))

    try:
        _set(movie_id, message="正在读取字幕…")
        res = translate.translate_movie(movie_id, cfg, target, bilingual, progress=progress)
        _set(movie_id, status="done", percent=100, message="完成",
             result=res, finished_at=int(time.time()))
    except _Cancelled:
        _set(movie_id, status="cancelled", message="已取消", finished_at=int(time.time()))
    except Exception as e:                      # noqa: BLE001 任务线程不抛
        _set(movie_id, status="error", message=str(e)[:200], error=str(e)[:400],
             finished_at=int(time.time()))
