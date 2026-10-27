# -*- coding: utf-8 -*-
"""封面 / 海报 / 头像与 fanart 相关路由。

从 api.py 拆出（阶段 2 试点）：路由前缀仍是 /api，路径与行为完全不变。
共享的 db / images / scraper 直接从上层包导入，避免循环依赖。
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from .. import images, scraper, store
from ..config import COVER_DIR, load_config
from ..db import db, query_all, query_one

router = APIRouter()


def _proxy_image(url: str) -> Response:
    """代理远程图片，规避前端跨域与防盗链；失败时返回占位 SVG。"""
    try:
        import requests
        cfg = load_config()
        proxy = (cfg.get("scraper", {}).get("proxy") or "") or None
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}, proxies=proxies)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "image/jpeg")
        if not ctype.startswith("image/"):
            ctype = "image/jpeg"
        return Response(resp.content, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=3600"})
    except Exception:
        svg = images.placeholder_svg("ERR", "图片获取失败")
        return Response(svg, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=60"})










# 批量海报的「后台任务」薄壳：真正的执行与进度在 poster_job 里，
# 路由层只做转发（大库批量联网是分钟级，同步调用会被浏览器/反代判超时）。


@router.get("/cover/{movie_id}")
def get_cover(movie_id: int, request: Request, poster: int = 0) -> Response:
    """封面图。poster=1 时优先返回 2:3 竖版海报（海报墙用），没有再回退原图。"""
    with db() as conn:
        row = query_one(conn, "SELECT code, title, cover FROM movies WHERE id = ?", (movie_id,))
    if row is None:
        raise HTTPException(404, "影片不存在")
    path = None
    if poster and row["cover"]:
        path = images.poster_path(row["cover"])
        if path is None:
            # 兜底就地生成（历史数据没跑过批量生成时，第一次访问即补齐）
            images.ensure_poster(row["cover"], load_config(refresh=False))
            path = images.poster_path(row["cover"])
    if path is None:
        path = images.cover_path(row["cover"] or "")
    if path:
        st = path.stat()
        etag = f'"{int(st.st_mtime)}.{st.st_size}"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        return FileResponse(path, headers={"Cache-Control": "no-cache", "ETag": etag})
    svg = images.placeholder_svg(row["code"] or "", row["title"] or "")
    etag = f'"{hashlib.md5(svg.encode("utf-8", "ignore")).hexdigest()}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache", "ETag": etag})


@router.get("/avatar/{name:path}")
def get_avatar(name: str, request: Request) -> Response:
    """返回女优头像。name 为本地相对文件名；若为远程 URL 则代理转发（规避跨域/防盗链）。"""
    if images.is_remote(name):
        return _proxy_image(name)
    path = images.avatar_path(name)
    if path:
        st = path.stat()
        etag = f'"{int(st.st_mtime)}.{st.st_size}"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        return FileResponse(str(path), headers={"Cache-Control": "no-cache", "ETag": etag})
    # 无头像：返回一张中性占位 SVG
    svg = images.placeholder_svg(name, "无头像")
    etag = f'"{hashlib.md5(svg.encode("utf-8", "ignore")).hexdigest()}"'
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache", "ETag": etag})


@router.get("/fanart/{movie_id}")
def get_fanart(movie_id: int, request: Request) -> Response:
    """返回影片背景大图（fanart）。"""
    with db() as conn:
        row = query_one(conn, "SELECT code, title, fanart FROM movies WHERE id = ?", (movie_id,))
    if row is None:
        raise HTTPException(404, "影片不存在")
    if images.is_remote(row["fanart"] or ""):
        return _proxy_image(row["fanart"])
    path = images.fanart_path(row["fanart"] or "")
    if path:
        st = path.stat()
        etag = f'"{int(st.st_mtime)}.{st.st_size}"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        return FileResponse(str(path), headers={"Cache-Control": "no-cache", "ETag": etag})
    svg = images.placeholder_svg(row["code"] or "", "无背景图")
    etag = f'"{hashlib.md5(svg.encode("utf-8", "ignore")).hexdigest()}"'
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache", "ETag": etag})


@router.post("/movies/{movie_id}/cover")
def set_cover(movie_id: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    cfg = load_config()
    location = str(payload.get("url") or payload.get("path") or "").strip()
    with db() as conn:
        movie = store.movie_detail(conn, movie_id)
        if not movie:
            raise HTTPException(404, "影片不存在")
        if not location:  # 不传地址时自动嗅探本地图片
            name = scraper.sniff_local_cover(conn, movie_id, cfg)
        else:
            name = scraper.save_cover(conn, movie, location, cfg, source="manual")
        if not name:
            raise HTTPException(400, "封面获取失败，请检查地址或本地文件是否有效")
        return {"ok": True, "cover": name}


@router.post("/movies/{movie_id}/cover/upload")
async def upload_cover(movie_id: int, file: UploadFile = File(...)) -> Dict[str, Any]:
    data = await file.read()
    with db() as conn:
        movie = store.movie_detail(conn, movie_id)
        if not movie:
            raise HTTPException(404, "影片不存在")
        name = images.save_bytes(movie["key"], data, file.filename or "")
        if not name:
            raise HTTPException(400, "图片无效")
            conn.execute(
            "UPDATE movies SET cover = ?, cover_source = 'upload' WHERE id = ?", (name, movie_id))
    images.ensure_poster(name)
    return {"ok": True, "cover": name}


@router.delete("/movies/{movie_id}/cover")
def clear_cover(movie_id: int) -> Dict[str, Any]:
    with db() as conn:
        row = query_one(conn, "SELECT cover FROM movies WHERE id = ?", (movie_id,))
        if row and row["cover"]:
            p = COVER_DIR / row["cover"]
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
            _poster = COVER_DIR / images.poster_name(row["cover"])
            if _poster.exists():
                try:
                    _poster.unlink()
                except OSError:
                    pass
        conn.execute("UPDATE movies SET cover = '', cover_source = '' WHERE id = ?", (movie_id,))
    return {"ok": True}


@router.post("/covers/posters")
def gen_posters(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """批量为已有封面的影片生成 2:3 竖版海报（不重刮元数据）。

    body: {force?: bool}  force=true 时无视已存在强制重生成。
    """
    cfg = load_config(refresh=True)
    force = bool(payload.get("force"))
    upgrade = bool(payload.get("upgrade"))
    with db() as conn:
        rows = query_all(
            conn, "SELECT id, code, key, cover FROM movies WHERE cover <> '' ORDER BY id")
    done = skipped = failed = upgraded = 0
    for r in rows:
        cover = str(r["cover"] or "")
        if not cover:
            continue
        if not force and not upgrade and images.poster_path(cover) is not None:
            skipped += 1
            continue
        if force:
            old_poster = images.poster_path(cover)
            if old_poster is not None:
                try:
                    old_poster.unlink()
                except OSError:
                    pass
        if upgrade and str(r["code"] or "").strip():
            # 顺带把横版封面换成 DMM 高清大图（2184×1468），并写入官方竖版海报
            up = images.upgrade_from_dmm(str(r["key"] or ""), str(r["code"] or ""), cfg)
            if up.get("cover"):
                cover = up["cover"]
                upgraded += 1
                with db() as c2:
                    c2.execute("UPDATE movies SET cover = ? WHERE id = ?", (cover, r["id"]))
                    c2.commit()
        name = images.ensure_poster(cover, cfg, code=str(r["code"] or ""),
                                     allow_network=upgrade)
        if name:
            done += 1
        else:
            failed += 1
    return {"ok": True, "total": len(rows), "generated": done, "skipped": skipped,
            "failed": failed, "upgrade": upgrade, "covers_upgraded": upgraded}


@router.post("/covers/posters/task")
def poster_task_start(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    from .. import poster_job
    return poster_job.start(force=bool(payload.get("force")),
                            upgrade=bool(payload.get("upgrade")))


@router.get("/covers/posters/task")
def poster_task_state() -> Dict[str, Any]:
    from .. import poster_job
    return poster_job.state()


@router.post("/covers/posters/task/cancel")
def poster_task_cancel() -> Dict[str, Any]:
    from .. import poster_job
    return poster_job.cancel()


@router.post("/covers/local-sniff")
def sniff_all_covers() -> Dict[str, Any]:
    return scraper.batch_local_covers()


@router.post("/movies/{movie_id}/extract-cover")
def extract_cover(movie_id: int) -> Dict[str, Any]:
    """从影片视频中抽一帧作为封面（未刮削到封面的影片可用）。"""
    cfg = load_config()
    from ..ext_components import ffmpeg_exe
    ffmpeg = ffmpeg_exe(cfg)
    with db() as conn:
        mv = store.movie_detail(conn, movie_id)
        if not mv:
            raise HTTPException(404, "影片不存在")
        if mv.get("files") and (mv["files"][0].get("strm_url") or ""):
            raise HTTPException(400, "网络串流无法抽帧，请手动上传封面")
        name = store.extract_cover_from_video(conn, movie_id, ffmpeg)
    if not name:
        raise HTTPException(400, "抽帧失败：ffmpeg 不可用、视频无法读取或无有效帧")
    if name == "skipped":
        raise HTTPException(409, "该影片已有正式封面（刮削/上传），不会用视频抽帧覆盖")
    images.ensure_poster(name)
    return {"ok": True, "cover": name, "cover_source": "video"}


@router.post("/covers/extract-all")
def extract_all_covers() -> Dict[str, Any]:
    """批量：为所有「没有封面」的影片从视频抽帧生成封面。"""
    cfg = load_config()
    from ..ext_components import ffmpeg_exe
    ffmpeg = ffmpeg_exe(cfg)
    with db() as conn:
        ids = [r["id"] for r in query_all(
            conn, "SELECT id FROM movies WHERE cover = '' AND has_code = 1")]
    ok = 0
    failed = 0
    for mid in ids:
        try:
            with db() as conn:
                name = store.extract_cover_from_video(conn, mid, ffmpeg)
            if name:
                ok += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {"ok": True, "extracted": ok, "failed": failed, "total": len(ids)}
