# -*- coding: utf-8 -*-
"""广告片检测路由：启动 / 轮询 / 取消 三件套 + 结果查询 + 证据图。

路由层只做参数校验与转发，检测逻辑在 app/adcheck.py，任务状态在 app/ad_job.py。
注意：固定路径（results / counts / status）必须注册在 /{movie_id} 之前，
否则 "results" 会被当成 movie_id 解析。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse, Response

from .. import ad_job, adcheck
from ..db import db

router = APIRouter()


@router.get("/adcheck/status")
def adcheck_status() -> Dict[str, Any]:
    return ad_job.state()


@router.post("/adcheck/start")
def adcheck_start(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    ids = payload.get("movie_ids") or []
    try:
        ids = [int(x) for x in ids][:5000]
    except Exception:
        ids = []
    return ad_job.start(ids, force=bool(payload.get("force")))


@router.post("/adcheck/cancel")
def adcheck_cancel() -> Dict[str, Any]:
    return ad_job.cancel()


@router.get("/adcheck/results")
def adcheck_results(level: str = "", limit: int = 300, offset: int = 0,
                    include_ignored: bool = False) -> Dict[str, Any]:
    if level and level not in ("likely", "suspect", "clean"):
        raise HTTPException(400, "level 只能是 likely / suspect / clean")
    with db() as conn:
        items = adcheck.list_results(conn, level=level, include_ignored=include_ignored,
                                     limit=limit, offset=offset)
        stat = adcheck.counts(conn)
    return {"items": items, "counts": stat}


@router.get("/adcheck/counts")
def adcheck_counts() -> Dict[str, int]:
    with db() as conn:
        return adcheck.counts(conn)


@router.post("/adcheck/clear")
def adcheck_clear() -> Dict[str, Any]:
    with db() as conn:
        n = adcheck.clear(conn)
    return {"ok": True, "removed": n}


@router.get("/adcheck/{movie_id}/sheet")
def adcheck_sheet(movie_id: int) -> Response:
    p = adcheck.sheet_path(movie_id)
    if not p:
        raise HTTPException(404, "该影片没有证据图（尚未检测或已清理）")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})


@router.get("/adcheck/{movie_id}")
def adcheck_result(movie_id: int) -> Dict[str, Any]:
    with db() as conn:
        res = adcheck.get_result(conn, movie_id)
    if not res:
        raise HTTPException(404, "尚未检测")
    return res


@router.post("/adcheck/{movie_id}/run")
def adcheck_run_one(movie_id: int) -> Dict[str, Any]:
    """单片检测（同步，约 5~15 秒）。"""
    try:
        res = ad_job.check_one(movie_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "检测失败")
    return res


@router.post("/adcheck/{movie_id}/ignore")
def adcheck_ignore(movie_id: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    ignored = bool(payload.get("ignored", True))
    with db() as conn:
        if not adcheck.set_ignored(conn, movie_id, ignored):
            raise HTTPException(404, "尚未检测")
    return {"ok": True, "ignored": ignored}


@router.delete("/adcheck/{movie_id}")
def adcheck_delete(movie_id: int) -> Dict[str, Any]:
    with db() as conn:
        n = adcheck.clear(conn, movie_id)
    return {"ok": True, "removed": n}
