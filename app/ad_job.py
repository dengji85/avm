# -*- coding: utf-8 -*-
"""广告片批量检测任务：后台线程 + 进度轮询 + 可取消。

为什么单独成模块（照 poster_job.py 的范式）：
  * 全库检测是十几分钟级操作（单片 5~15s），同步 HTTP 会被浏览器/反代判超时；
  * 进度语义（总数 / 已完成 / 命中 / 失败）只服务这一个场景。

单任务语义：全局只跑一份，重复点击返回当前进度（幂等）。
增量语义：默认只测「没有检测记录」的片，force=True 才全量重测。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from . import adcheck
from .config import load_config
from .db import db
from .threadpool import submit_long

_JOB: Dict[str, Any] = {
    "running": False,
    "total": 0,
    "done": 0,
    "percent": 0,
    "checked": 0,
    "flagged": 0,
    "failed": 0,
    "skipped": 0,
    "message": "",
    "current": "",
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


def _rows(movie_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """待检测影片：每部取体积最大的有效文件；网络串流（.strm）跳过。"""
    with db() as conn:
        if movie_ids:
            marks = ",".join("?" * len(movie_ids))
            cur = conn.execute(
                "SELECT m.id AS mid, m.code, m.runtime, f.id AS fid, f.path, f.size, "
                "f.strm_url FROM movies m JOIN movie_files f ON f.movie_id = m.id "
                "WHERE f.missing = 0 AND m.id IN (%s) ORDER BY m.id, f.size DESC" % marks,
                tuple(movie_ids))
        else:
            cur = conn.execute(
                "SELECT m.id AS mid, m.code, m.runtime, f.id AS fid, f.path, f.size, "
                "f.strm_url FROM movies m JOIN movie_files f ON f.movie_id = m.id "
                "WHERE f.missing = 0 ORDER BY m.id, f.size DESC")
        seen = set()
        out = []
        for r in cur.fetchall():
            d = dict(r)
            if d["mid"] in seen:
                continue
            seen.add(d["mid"])
            if (d.get("strm_url") or "").strip():
                continue          # 网盘/网络串流抽不了帧
            out.append(d)
        return out


def start(movie_ids: Optional[List[int]] = None, force: bool = False) -> Dict[str, Any]:
    """启动批量检测；已在跑时直接返回当前进度（幂等）。"""
    with _LOCK:
        if _JOB.get("running"):
            return dict(_JOB)
        _JOB.update({
            "running": True, "total": 0, "done": 0, "percent": 0,
            "checked": 0, "flagged": 0, "failed": 0, "skipped": 0,
            "message": "准备中…", "current": "", "cancel": False,
            "started_at": int(time.time()), "finished_at": 0,
        })
    submit_long(_run, list(movie_ids or []), bool(force))
    return state()


def cancel() -> Dict[str, Any]:
    _set(cancel=True, message="正在取消…")
    return state()


def _run(movie_ids: List[int], force: bool) -> None:
    try:
        cfg = load_config(refresh=True)
        rows = _rows(movie_ids or None)
        if not force:
            with db() as conn:
                done_ids = {r[0] for r in conn.execute("SELECT movie_id FROM ad_checks").fetchall()}
            pending = [r for r in rows if r["mid"] not in done_ids]
            skipped = len(rows) - len(pending)
            rows = pending
        else:
            skipped = 0
        total = len(rows)
        _set(total=total, skipped=skipped,
             message="已开始…" if total else "没有需要检测的影片")

        checked = flagged = failed = 0
        for idx, r in enumerate(rows, 1):
            if state().get("cancel"):
                _set(message="已取消", percent=int((idx - 1) * 100 / total) if total else 100)
                break
            code = str(r.get("code") or r.get("mid"))
            _set(current=code, message="检测中：%s" % code)
            try:
                res = adcheck.analyze_file(
                    r["path"], cfg,
                    runtime_sec=float(r.get("runtime") or 0) * 60.0,
                    filename=r["path"], movie_id=int(r["mid"]))
                if res.get("ok"):
                    with db() as conn:
                        adcheck.save_result(conn, res, file_id=int(r["fid"] or 0))
                    checked += 1
                    if res["level"] in ("likely", "suspect"):
                        flagged += 1
                else:
                    failed += 1
            except Exception:                      # 单部失败不影响整批
                failed += 1
            _set(done=idx, checked=checked, flagged=flagged, failed=failed,
                 percent=int(idx * 100 / total) if total else 100)

        _set(running=False, finished_at=int(time.time()), current="",
             message="完成：检测 %d，疑似 %d，跳过 %d，失败 %d"
                     % (checked, flagged, skipped, failed))
    except Exception as e:                        # noqa: BLE001 兜底：别让任务线程静默死掉
        _set(running=False, finished_at=int(time.time()), current="",
             message="任务异常：%s" % str(e)[:160])


def check_one(movie_id: int) -> Dict[str, Any]:
    """单片同步检测（详情页用，5~15s）。返回结果字典。"""
    cfg = load_config(refresh=True)
    with db() as conn:
        row = conn.execute(
            "SELECT m.id AS mid, m.code, m.runtime, f.id AS fid, f.path, f.strm_url "
            "FROM movies m JOIN movie_files f ON f.movie_id = m.id "
            "WHERE m.id = ? AND f.missing = 0 ORDER BY f.size DESC LIMIT 1",
            (movie_id,)).fetchone()
    if not row:
        raise ValueError("影片不存在或没有可用文件")
    d = dict(row)
    if (d.get("strm_url") or "").strip():
        raise ValueError("网络串流无法抽帧检测")
    res = adcheck.analyze_file(d["path"], cfg,
                               runtime_sec=float(d.get("runtime") or 0) * 60.0,
                               filename=d["path"], movie_id=movie_id)
    if res.get("ok"):
        with db() as conn:
            adcheck.save_result(conn, res, file_id=int(d["fid"] or 0))
    return res
