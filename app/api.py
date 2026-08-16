# -*- coding: utf-8 -*-
"""HTTP 接口层。"""
from __future__ import annotations

import csv
import copy
import io
import os
import socket
import platform
import string
import subprocess
import threading
import json
import sys
import hashlib
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse

from . import ai as ai_mod
from . import dedupe, images, nfo as nfo_mod, providers, scanner, scraper, scrape_diag, store, subtitles
from .config import COVER_DIR, DATA_DIR, avatar_dir, _deep_merge, load_config, update_config
from .db import connect, db, query_all, query_one
from .jobs import SCAN, SCRAPE

router = APIRouter(prefix="/api")

# 当前程序版本（与发布版本保持一致）。
APP_VERSION = "1.11.0"
# 编译/构建日期，格式 YYYY-MM-DD。由 build.py 在打包时重写为实际构建日期；
# 源码运行（python run.py）默认显示源码最近修改时间对应的日期。
BUILD_DATE = "2026-08-16"


# ------------------------------------------------------------------ 影片列表


@router.get("/movies")
def list_movies(
    q: str = "", actress: str = "", genre: str = "", tag: str = "", studio: str = "",
    series: str = "", prefix: str = "", year: Optional[int] = None, flags: str = "",
    sort: str = "added_desc", op: str = "AND", page: int = 1, page_size: int = 60,
) -> Dict[str, Any]:
    params = {
        "q": q, "actress": actress, "genre": genre, "tag": tag, "studio": studio,
        "series": series, "prefix": prefix, "year": year, "flags": flags,
        "sort": sort, "op": op, "page": page, "page_size": page_size,
    }
    with db() as conn:
        return store.search_movies(conn, params)


@router.get("/movies/{movie_id}")
def get_movie(movie_id: int) -> Dict[str, Any]:
    with db() as conn:
        movie = store.movie_detail(conn, movie_id)
        if not movie:
            raise HTTPException(404, "影片不存在")
        return movie


@router.put("/movies/{movie_id}")
def edit_movie(movie_id: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    with db() as conn:
        if not store.movie_detail(conn, movie_id):
            raise HTTPException(404, "影片不存在")
        store.update_movie(conn, movie_id, payload)
        return store.movie_detail(conn, movie_id)


@router.delete("/movies/{movie_id}")
def remove_movie(movie_id: int, delete_file: bool = False) -> Dict[str, Any]:
    with db() as conn:
        movie = store.movie_detail(conn, movie_id)
        if not movie:
            raise HTTPException(404, "影片不存在")
        deleted: List[str] = []
        if delete_file:
            for f in movie["files"]:
                try:
                    os.remove(f["path"])
                    deleted.append(f["path"])
                except OSError:
                    pass
        store.delete_movie(conn, movie_id)
        return {"ok": True, "deleted_files": deleted}


@router.post("/movies/{movie_id}/toggle")
def toggle_flag(movie_id: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    field = str(payload.get("field", "favorite"))
    if field not in {"favorite", "watched", "watchlist"}:
        raise HTTPException(400, "字段不支持切换")
    with db() as conn:
        row = query_one(conn, f"SELECT {field} AS v FROM movies WHERE id = ?", (movie_id,))
        if row is None:
            raise HTTPException(404, "影片不存在")
        value = 0 if row["v"] else 1
        conn.execute(f"UPDATE movies SET {field} = ? WHERE id = ?", (value, movie_id))
        return {"ok": True, "field": field, "value": value}


# ------------------------------------------------------------------ 播放/定位


def _open_path(path: str, reveal: bool = False) -> Optional[int]:
    """调用系统关联程序打开文件 / 目录。返回子进程 PID（Windows 经 os.startfile 无法获取，返回 None）。"""
    system = platform.system()
    if reveal:
        if system == "Windows":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", str(Path(path).parent)])
        return None
    if system == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]
        return None
    elif system == "Darwin":
        return subprocess.Popen(["open", path]).pid
    else:
        return subprocess.Popen(["xdg-open", path]).pid


@router.post("/movies/{movie_id}/play")
def play_movie(movie_id: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    reveal = bool(payload.get("reveal"))
    file_id = payload.get("file_id")
    with db() as conn:
        movie = store.movie_detail(conn, movie_id)
        if not movie or not movie["files"]:
            raise HTTPException(404, "找不到可播放的文件")
        target = movie["files"][0]
        if file_id:
            target = next((f for f in movie["files"] if f["id"] == int(file_id)), target)
        if not os.path.exists(target["path"]):
            raise HTTPException(404, f"文件已不存在：{target['path']}")
        try:
            pid = _open_path(target["path"], reveal)
        except Exception as exc:
            raise HTTPException(500, f"调用系统播放器失败：{exc}")
        if not reveal:
            # 建立观看场次，并启动后台监控线程记录「看了多久 / 哪几段」
            session_id = store.start_session(conn, movie_id, 0.0, "external")
            runtime = (movie.get("runtime") or 0) * 60
            from . import monitor
            monitor.start_external_monitor(
                movie_id, target["path"], session_id, runtime_sec=runtime, pid=pid)
            conn.execute(
                "UPDATE movies SET play_count = play_count + 1, watched = 1, "
                "last_played = datetime('now','localtime') WHERE id = ?",
                (movie_id,),
            )
            return {"ok": True, "path": target["path"], "session_id": session_id}
        return {"ok": True, "path": target["path"]}


@router.post("/movies/{movie_id}/played")
def mark_played(movie_id: int) -> Dict[str, Any]:
    with db() as conn:
        movie = store.movie_detail(conn, movie_id)
        if not movie:
            raise HTTPException(404, "找不到影片")
        store.mark_played(conn, movie_id)
    return {"ok": True}


# ------------------------------------------------------------------ 封面


@router.get("/cover/{movie_id}")
def get_cover(movie_id: int, request: Request) -> Response:
    with db() as conn:
        row = query_one(conn, "SELECT code, title, cover FROM movies WHERE id = ?", (movie_id,))
    if row is None:
        raise HTTPException(404, "影片不存在")
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


# ------------------------------------------------------------------ 女优头像 / 背景大图


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


@router.post("/media/cache-avatars")
def cache_avatars() -> Dict[str, Any]:
    """把仍是远程 URL 的女优头像批量下载落盘到自定义 avatar_dir（需在设置开启头像下载）。"""
    cfg = load_config()
    with db() as conn:
        result = scraper.cache_remote_avatars(conn, cfg)
    return {"ok": True, **result}


@router.post("/actresses/cache-avatars")
def fill_actress_avatars() -> Dict[str, Any]:
    """重新抓取已刮削影片的元数据，补全女优头像（落盘到本地 avatar_dir）。

    走 scrape_one(overwrite=False)，仅填补仍为空的女优头像，不覆盖已有本地头像、
    不改动影片标题/封面等其它字段。需联网并建议配置代理。
    """
    cfg = load_config()
    providers_list = providers.build_providers(cfg)
    if not providers_list:
        raise HTTPException(400, "未启用任何数据源，无法补全女优头像")
    with db() as conn:
        before = query_one(
            conn, "SELECT COUNT(*) c FROM actresses WHERE avatar='' OR avatar IS NULL")["c"]
        # 仅对有番号且已刮削过的影片重新抓，避免无谓请求
        movie_ids = [r["id"] for r in query_all(
            conn, "SELECT id FROM movies WHERE code<>'' AND scraped_at IS NOT NULL")]
        filled = 0
        for mid in movie_ids:
            try:
                scraper.scrape_one(conn, mid, providers_list, cfg, overwrite=False)
            except Exception:
                pass
        after = query_one(
            conn, "SELECT COUNT(*) c FROM actresses WHERE avatar='' OR avatar IS NULL")["c"]
        filled = max(before - after, 0)
    return {"ok": True, "actresses_before_empty": before,
            "actresses_after_empty": after, "filled": filled,
            "movies_scanned": len(movie_ids)}


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
        conn.execute("UPDATE movies SET cover = '', cover_source = '' WHERE id = ?", (movie_id,))
    return {"ok": True}


@router.post("/covers/local-sniff")
def sniff_all_covers() -> Dict[str, Any]:
    return scraper.batch_local_covers()


# ------------------------------------------------------------------ 分类/统计


@router.get("/facets")
def get_facets(limit: int = 300) -> Dict[str, Any]:
    with db() as conn:
        return store.facets(conn, limit)


@router.get("/actresses")
def get_actresses(q: str = "", sort: str = "count", limit: int = 500) -> Dict[str, Any]:
    with db() as conn:
        return {"items": store.actress_wall(conn, q, sort, limit)}


@router.get("/actresses/{actress_id}")
def get_actress_detail(actress_id: str, page: int = 1, page_size: int = 24) -> Dict[str, Any]:
    with db() as conn:
        d = store.actress_detail(conn, actress_id, page, page_size)
    if not d:
        raise HTTPException(status_code=404, detail="女优不存在")
    return d


@router.put("/actresses/{actress_id}")
def edit_actress(actress_id: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    fields = {k: v for k, v in payload.items() if k in {"alias", "avatar", "birthday", "note", "favorite"}}
    if not fields:
        raise HTTPException(400, "没有可更新的字段")
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE actresses SET {sets} WHERE id = ?", [*fields.values(), actress_id])
        return {"ok": True}


@router.post("/actresses/{ident}/favorite")
def toggle_actress_favorite(ident: str) -> Dict[str, Any]:
    """收藏/取消收藏女优（ident 可为 id 或名称）。"""
    with db() as conn:
        a = (query_one(conn, "SELECT * FROM actresses WHERE id = ?", (int(ident),))
             if str(ident).isdigit()
             else query_one(conn, "SELECT * FROM actresses WHERE name = ?", (str(ident),)))
        if not a:
            raise HTTPException(404, "女优不存在")
        new_val = 0 if (a.get("favorite") or 0) else 1
        conn.execute("UPDATE actresses SET favorite = ? WHERE id = ?", (new_val, a["id"]))
        return {"ok": True, "favorite": new_val}


@router.post("/actresses/{ident}/follow")
def toggle_actress_follow(ident: str) -> Dict[str, Any]:
    """关注 / 取消关注女优（ident 可为 id 或名称）。"""
    with db() as conn:
        a = (query_one(conn, "SELECT * FROM actresses WHERE id = ?", (int(ident),))
             if str(ident).isdigit()
             else query_one(conn, "SELECT * FROM actresses WHERE name = ?", (str(ident),)))
        if not a:
            raise HTTPException(404, "女优不存在")
        new_val = store.toggle_follow(conn, a["id"])
    return {"ok": True, "followed": new_val}


@router.get("/stats")
def get_stats() -> Dict[str, Any]:
    with db() as conn:
        data = store.stats(conn)
        data["recent"] = [store._row_to_card(r) for r in data["recent"]]
        data["scan_logs"] = query_all(
            conn, "SELECT * FROM scan_logs ORDER BY id DESC LIMIT 10")
        return data


@router.get("/profile")
def get_profile_route() -> Dict[str, Any]:
    with db() as conn:
        return store.taxonomy(conn)


@router.get("/rankings")
def get_rankings(kind: str = "watched", limit: int = 30) -> Dict[str, Any]:
    with db() as conn:
        return {"items": store.rankings(conn, kind, limit)}


@router.get("/watch-history")
def get_watch_history(
    page: int = 1,
    size: int = 50,
    from_: str = Query(None, alias="from"),
    to: str = Query(None, alias="to"),
    method: str = Query(None),
    q: str = Query(None),
) -> Dict[str, Any]:
    with db() as conn:
        return store.watch_history(conn, page, size, from_, to, method, q)


@router.get("/stats-enhanced")
def get_stats_enhanced() -> Dict[str, Any]:
    with db() as conn:
        return store.stats_enhanced(conn)


# ------------------------------------------------------------------ 扫描


@router.post("/scan")
def start_scan(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    incremental = bool(payload.get("incremental", True))
    workers = payload.get("workers") or None
    hash_files = bool(payload.get("hash_files", False))
    auto_cleanup = payload.get("auto_cleanup", None)
    if not SCAN.start():
        raise HTTPException(409, "已有扫描任务在执行中")
    def _run():
        try:
            result = scanner.run_scan(
                progress_cb=SCAN.update,
                incremental=incremental,
                workers=workers,
                hash_files=hash_files,
                auto_cleanup=auto_cleanup,
            )
            SCAN.finish(json.dumps(result, ensure_ascii=False))
        except Exception as e:
            SCAN.error(str(e))
            SCAN.finish("扫描失败：" + str(e))
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}


@router.get("/scan/status")
def scan_status() -> Dict[str, Any]:
    return SCAN.snapshot()


@router.post("/scan/cancel")
def scan_cancel() -> Dict[str, Any]:
    SCAN.cancel()
    return {"ok": True}


@router.post("/scan/local-covers")
def rescan_local_covers() -> Dict[str, Any]:
    """重新嗅探所有无封面影片的本地图片，命中则落盘写回 cover。

    用于「补上本地已有的封面」：新增本地封面文件后，无需全量重扫即可补图。
    命名规则见 images.find_local_cover（{视频名}-poster/cover 或同名、poster/cover 等）。
    """
    from . import scanner
    cfg = load_config()
    with db() as conn:
        before = query_one(
            conn, "SELECT COUNT(*) c FROM movies WHERE cover='' OR cover IS NULL")["c"]
        rows = query_all(
            conn,
            "SELECT DISTINCT m.id FROM movies m "
            "WHERE (m.cover IS NULL OR m.cover = '') "
            "AND EXISTS (SELECT 1 FROM movie_files f WHERE f.movie_id = m.id AND f.missing = 0)",
        )
        found = 0
        for r in rows:
            try:
                if scraper.sniff_local_cover(conn, r["id"], cfg):
                    found += 1
            except Exception:
                pass
        after = query_one(
            conn, "SELECT COUNT(*) c FROM movies WHERE cover='' OR cover IS NULL")["c"]
    return {"ok": True, "movies_scanned": len(rows),
            "covers_before_empty": before, "covers_after_empty": after,
            "filled": max(before - after, 0)}


@router.post("/parse-preview")
def parse_preview(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    names = payload.get("names") or []
    if isinstance(names, str):
        names = [n for n in names.splitlines() if n.strip()]
    return {"items": scanner.preview_parse(list(names)[:200])}


# ------------------------------------------------------------------ 抓取


@router.get("/providers")
def list_providers() -> Dict[str, Any]:
    cfg = load_config()
    active = [p.name for p in providers.build_providers(cfg)]
    return {"available": providers.describe(), "active": active}


@router.post("/scraper/test")
def test_scrape(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """
    快速验证当前（或临时覆盖的）抓取配置能否真实抓到元数据。

    不写入数据库，仅做探测，用于国内网络下确认代理 / CF Cookie 配置有效，
    再决定是否全量抓取。可携带 ``override`` 传入临时代理 / Cookie 即时测试，
    也可携带 ``code`` 指定测试番号（留空则用库里第一个有番号的影片）。
    """
    base_cfg = load_config(refresh=True)
    override = payload.get("override") or {}
    cfg = _deep_merge(base_cfg, override) if override else copy.deepcopy(base_cfg)

    plist = providers.build_providers(cfg)
    if not plist:
        return {"ok": False, "reason": "没有启用任何元数据源", "code": None,
                "results": [], "cover_ok": False, "cover_url": None}

    code = str(payload.get("code") or "").strip()
    movie_for_test: Optional[Dict[str, Any]] = None
    if not code:
        with db() as conn:
            row = query_one(
                conn,
                "SELECT code, title FROM movies WHERE has_code=1 AND code != '' "
                "ORDER BY id LIMIT 1",
            )
            if row:
                code = row["code"]
                movie_for_test = {"code": row["code"], "title": row["title"], "key": row["code"]}
    if not code:
        return {"ok": False, "reason": "库里没有可用番号，请在「测试番号」框填一个真实番号",
                "code": None, "results": [], "cover_ok": False, "cover_url": None}
    if movie_for_test is None:
        movie_for_test = {"code": code, "title": code, "key": code}

    results: List[Dict[str, Any]] = []
    first_cover: Optional[str] = None
    for p in plist:
        try:
            meta = p.fetch(movie_for_test)
        except Exception as exc:
            results.append({"provider": p.name, "ok": False, "reason": f"请求异常: {exc}"})
            continue
        if meta:
            fields = {k: v for k, v in meta.items() if k != "source"}
            results.append({"provider": p.name, "ok": True, "fields": fields})
            if not first_cover and meta.get("cover"):
                first_cover = str(meta["cover"])
        else:
            reason = getattr(p, "last_error", "") or "返回空（验证页 / 番号不存在 / 代理不通）"
            results.append({"provider": p.name, "ok": False, "reason": reason})

    cover_ok = False
    if first_cover:
        try:
            data = images.download(first_cover, cfg)
            cover_ok = bool(data)
        except Exception:
            cover_ok = False

    success = any(r.get("ok") for r in results)
    return {
        "ok": success,
        "code": code,
        "cover_ok": cover_ok,
        "cover_url": first_cover,
        "results": results,
    }


@router.post("/scrape")
def start_scrape(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    ids = payload.get("ids")
    scope = str(payload.get("scope", "missing"))
    overwrite = payload.get("overwrite")
    force = bool(payload.get("force", False))
    if not scraper.start_scrape_async(ids, scope, overwrite, force=force):
        raise HTTPException(409, "已有抓取任务在执行中")
    return {"ok": True}


@router.get("/scrape/status")
def scrape_status() -> Dict[str, Any]:
    return SCRAPE.snapshot()


@router.post("/scrape/cancel")
def scrape_cancel() -> Dict[str, Any]:
    SCRAPE.cancel()
    return {"ok": True}


@router.get("/scrape/tasks")
def scrape_tasks(limit: int = 50) -> Dict[str, Any]:
    """列出历史刮削任务汇总（用于日志查询页选择任务）。"""
    with db() as conn:
        rows = query_all(
            conn,
            """
            SELECT task_id,
                   MIN(started_at) AS started_at,
                   COUNT(*)        AS total,
                   SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END)    AS ok,
                   SUM(CASE WHEN status='miss' THEN 1 ELSE 0 END)   AS miss,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END)  AS error,
                   ROUND(AVG(elapsed_ms), 1)                        AS avg_ms
            FROM scrape_logs
            GROUP BY task_id
            ORDER BY MAX(id) DESC
            LIMIT ?
            """,
            (limit,),
        )
        return {"items": rows}


@router.get("/maintenance/summary")
def maintenance_summary() -> Dict[str, Any]:
    """维护中心聚合数据：待办概览 + 最近一次扫描信息。"""
    with db() as conn:
        hc = store.health_check(conn)
        counts = hc.get("counts", {})
        noscrape = query_one(conn,
            "SELECT COUNT(*) AS c FROM movies m "
            "WHERE (m.scraped_at IS NULL OR m.scraped_at='') "
            "AND EXISTS(SELECT 1 FROM movie_files f WHERE f.movie_id=m.id AND f.missing=0)")["c"]
        watchlist = query_one(conn, "SELECT COUNT(*) AS c FROM movies WHERE watchlist=1")["c"]
        total = query_one(conn, "SELECT COUNT(*) AS c FROM movies")["c"]
        last_scan = query_one(conn,
            "SELECT started_at, added, updated, removed FROM scan_logs ORDER BY id DESC LIMIT 1")
        return {
            "ok": True,
            "total": total,
            "noscrape": noscrape,
            "watchlist": watchlist,
            "missing_files": counts.get("missing_files", 0),
            "missing_cover": counts.get("missing_cover", 0),
            "placeholder_cover": counts.get("placeholder_cover", 0),
            "unrecognized": counts.get("unrecognized", 0),
            "duplicates": counts.get("duplicates", 0),
            "split_incomplete": counts.get("split_incomplete", 0),
            "last_scan": dict(last_scan) if last_scan else None,
        }


@router.get("/scrape/logs")
def scrape_logs(task_id: str = "", status: str = "", code: str = "",
                page: int = 1, size: int = 50) -> Dict[str, Any]:
    """逐文件刮削日志查询：可按任务、状态、番号过滤，支持分页。

    - task_id：某次刮削任务 id（来自 /scrape/status 的 task_id 或 /scrape/tasks）
    - status ：ok | miss | error（空=全部）
    - code   ：番号模糊搜索
    """
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    where = []
    args: List[Any] = []
    if task_id:
        where.append("task_id = ?")
        args.append(task_id)
    if status:
        where.append("status = ?")
        args.append(status)
    if code:
        where.append("code LIKE ?")
        args.append(f"%{code}%")
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    with db() as conn:
        total = query_one(conn, f"SELECT COUNT(*) AS n FROM scrape_logs {sql_where}", args)["n"]
        rows = query_all(
            conn,
            f"""SELECT id, task_id, started_at, file_path, code, provider,
                       status, reason, elapsed_ms, movie_id
                FROM scrape_logs {sql_where}
                ORDER BY id DESC LIMIT ? OFFSET ?""",
            args + [size, (page - 1) * size],
        )
    return {
        "items": rows,
        "total": total,
        "page": page,
        "size": size,
        "pages": (total + size - 1) // size if total else 0,
    }


@router.delete("/scrape/logs")
def scrape_logs_delete(task_id: str = "", status: str = "") -> Dict[str, Any]:
    """清理刮削日志。

    - task_id：仅删除某次任务日志（空=不限）
    - status ：ok | miss | error（空=全部状态）
    两者皆空时清空整张 scrape_logs 表，请在前端二次确认。
    """
    where = []
    args: List[Any] = []
    if task_id:
        where.append("task_id = ?")
        args.append(task_id)
    if status:
        where.append("status = ?")
        args.append(status)
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    with db() as conn:
        deleted = query_one(
            conn, f"SELECT COUNT(*) AS n FROM scrape_logs {sql_where}", args
        )["n"]
        if deleted:
            conn.execute(f"DELETE FROM scrape_logs {sql_where}", args)
            conn.commit()
    return {"deleted": deleted, "task_id": task_id, "status": status}


@router.get("/scrape/skips")
def scrape_skips() -> Dict[str, Any]:
    """列出刮削跳过名单（已知稳定失败的影片），供维护页查看与解除。"""
    with db() as conn:
        rows = query_all(
            conn,
            """SELECT s.id, s.movie_id, s.code, s.reason, s.kind, s.count, s.auto,
                      s.created_at, s.updated_at, m.title, m.cover
               FROM scrape_skip s
               LEFT JOIN movies m ON m.id = s.movie_id
               ORDER BY s.count DESC, s.updated_at DESC""",
        )
    return {"items": rows, "total": len(rows)}


@router.get("/scrape/failures")
def scrape_failures(
    limit: int = 200,
    group: str = "",      # blocked | neterr | miss | parse_err | code_issue | mixed | 空=全部
    only_unskipped: bool = True,  # 默认不显示已被稳定跳过（自动屏蔽）的项
) -> Dict[str, Any]:
    """刮削失败原因面板数据源。

    返回每个失败影片的逐源诊断（kind/原因/推荐操作），并按归类分组计数。
    与 scrape_skip 自愈名单区分：被稳定跳过的项默认排除（它们已不再反复尝试）。
    """
    with db() as conn:
        logs = query_all(conn,
                         "SELECT * FROM scrape_logs WHERE status<>'ok' "
                         "ORDER BY started_at DESC LIMIT ?", (int(limit),))
        skips = set()
        if only_unskipped:
            for r in query_all(conn, "SELECT code FROM scrape_skip"):
                skips.add((r["code"] or "").upper())
    items = []
    for row in logs:
        code = (row.get("code") or "").upper()
        if only_unskipped and code in skips:
            continue
        diag = scrape_diag.diagnose(row.get("detail"))
        if group and diag["summary_kind"] != group:
            if not any(s["kind"] == group for s in diag["sources"]):
                continue
        items.append({
            "code": row.get("code"),
            "file_path": row.get("file_path"),
            "movie_id": row.get("movie_id"),
            "provider": row.get("provider"),
            "reason": row.get("reason"),
            "ts": row.get("started_at"),
            "diagnosis": diag,
        })
    groups = scrape_diag.group_failures([it["diagnosis"] for it in items])
    return {"ok": True, "items": items, "groups": groups, "total": len(items)}


@router.post("/open-file")
def open_file(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """在文件管理器中定位并选中指定路径（用于刮削失败但未入库的影片）。"""
    path = (payload.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "缺少 path 参数")
    if not os.path.exists(path) and not os.path.exists(os.path.dirname(path)):
        raise HTTPException(404, f"文件不存在：{path}")
    try:
        _open_path(path, reveal=True)
    except Exception as exc:
        raise HTTPException(500, f"打开文件夹失败：{exc}")
    return {"ok": True, "path": path}


@router.post("/scrape/retry-neterr")
def retry_neterr_failures(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """一键重试所有「临时网络错误/反爬拦截」的影片（不碰稳定 miss/解析失败）。"""
    with db() as conn:
        codes = set()
        logs = query_all(conn,
                         "SELECT code, detail FROM scrape_logs WHERE status<>'ok' "
                         "ORDER BY started_at DESC LIMIT 500")
        for row in logs:
            diag = scrape_diag.diagnose(row.get("detail"))
            if diag["summary_kind"] in ("neterr", "blocked") or any(
                    s["kind"] in ("neterr", "blocked") for s in diag["sources"]):
                if row.get("code"):
                    codes.add(row["code"])
        if codes:
            placeholders = ",".join("?" for _ in codes)
            conn.execute(
                f"DELETE FROM scrape_skip WHERE UPPER(code) IN ({placeholders}) "
                f"AND kind IN ('net','neterr','blocked')", tuple(c.upper() for c in codes))
        cfg = load_config(refresh=True)
        plist = providers.build_providers(cfg)
        if not plist:
            raise HTTPException(400, "未启用任何数据源")
        run_scrape(conn, plist, cfg, pages=1, pattern="|".join(sorted(codes)) or None, force=True)
        result = query_one(conn, "SELECT COUNT(*) c FROM movies WHERE code IN "
                                 "(SELECT code FROM scrape_logs WHERE status<>'ok')")
    return {"ok": True, "retried_codes": sorted(codes), "matched": result["c"] if result else 0}


@router.post("/scrape/retry-with-provider")
def retry_with_provider(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """带指定数据源重试：仅对已失败影片用指定 provider 重抓（换源）。

    body: { "provider": "av-wiki", "codes": ["ABC-123", ...] 可选 }
    """
    provider_name = str(payload.get("provider", "")).strip()
    codes = payload.get("codes") or []
    if not provider_name:
        raise HTTPException(400, "provider 不能为空")
    with db() as conn:
        cfg = load_config(refresh=True)
        all_plist = providers.build_providers(cfg)
        plist = [p for p in all_plist if p.name == provider_name]
        if not plist:
            raise HTTPException(400, f"未启用数据源：{provider_name}")
        if codes:
            ph = ",".join("?" for _ in codes)
            conn.execute(f"DELETE FROM scrape_skip WHERE UPPER(code) IN ({ph})",
                         tuple(str(c).upper() for c in codes))
            pattern = "|".join(str(c) for c in codes)
        else:
            pattern = None
        run_scrape(conn, plist, cfg, pages=1, pattern=pattern, force=True)
        matched = query_one(conn, "SELECT COUNT(*) c FROM movies WHERE code IN "
                                  "(SELECT code FROM scrape_logs WHERE status<>'ok')")
    return {"ok": True, "provider": provider_name, "matched": matched["c"] if matched else 0}


@router.delete("/scrape/skips")
def scrape_skips_delete(movie_id: int = 0) -> Dict[str, Any]:
    """解除跳过名单。

    - movie_id>0：仅解除该影片（重新纳入刮削候选）。
    - movie_id=0 ：清空整张 scrape_skip（全部重新尝试），请前端二次确认。
    返回解除条数。
    """
    with db() as conn:
        if movie_id:
            cur = conn.execute("DELETE FROM scrape_skip WHERE movie_id = ?", (movie_id,))
        else:
            cur = conn.execute("DELETE FROM scrape_skip")
        conn.commit()
        removed = cur.rowcount
    return {"removed": removed, "movie_id": movie_id}


@router.post("/reparse-codes")
def reparse_codes(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """存量番号重解析：用最新 parser 重新识别未识别/全部影片的番号。"""
    only_missing = bool(payload.get("only_missing", True))
    with db() as conn:
        result = store.reparse_all_codes(conn, only_missing=only_missing)
    return {"ok": True, **result}


# ------------------------------------------------------------------ AI 增强
@router.get("/ai/status")
def ai_status() -> Dict[str, Any]:
    """返回 AI 是否可用（基于当前配置）。"""
    cfg = load_config(refresh=True)
    return {"enabled": ai_mod.is_available(cfg), "model": (cfg.get("ai") or {}).get("model", "")}


@router.post("/ai/generate-synopsis/{movie_id}")
def ai_generate_synopsis(movie_id: int) -> Dict[str, Any]:
    cfg = load_config(refresh=True)
    with db() as conn:
        m = store.movie_detail(conn, movie_id)
        if not m:
            raise HTTPException(status_code=404, detail="影片不存在")
        try:
            text = ai_mod.generate_synopsis(m, cfg)
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
        # 写回 plot（不覆盖人工编辑的非空值逻辑：用户主动点击即覆盖）
        store.update_movie(conn, movie_id, {"plot": text})
    return {"ok": True, "plot": text}


@router.post("/ai/suggest-tags/{movie_id}")
def ai_suggest_tags(movie_id: int) -> Dict[str, Any]:
    cfg = load_config(refresh=True)
    with db() as conn:
        m = store.movie_detail(conn, movie_id)
        if not m:
            raise HTTPException(status_code=404, detail="影片不存在")
        try:
            tags = ai_mod.suggest_tags(m, cfg)
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "tags": tags}


@router.post("/ai/search-intent")
def ai_search_intent(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    cfg = load_config(refresh=True)
    query = str(payload.get("query", "")).strip()
    if not query:
        raise HTTPException(status_code=400, detail="query 为空")
    try:
        cond = ai_mod.parse_search_intent(query, cfg)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "conditions": cond}


@router.post("/movies/{movie_id}/scrape")
def scrape_single(movie_id: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    cfg = load_config(refresh=True)
    plist = providers.build_providers(cfg)
    # 指定数据源重试：仅保留命中的 provider（换源）
    only = payload.get("provider")
    if only:
        plist = [p for p in plist if p.name == only] or plist
    with db() as conn:
        result = scraper.scrape_one(conn, movie_id, plist, cfg, bool(payload.get("overwrite", True)))
        result["movie"] = store.movie_detail(conn, movie_id)
    return result


# ------------------------------------------------------------------ 导入导出


@router.post("/movies/{movie_id}/nfo")
def export_nfo(movie_id: int) -> Dict[str, Any]:
    with db() as conn:
        movie = store.movie_detail(conn, movie_id)
    if not movie:
        raise HTTPException(404, "影片不存在")
    if not movie["files"]:
        raise HTTPException(400, "该影片没有关联文件")
    target = Path(movie["files"][0]["path"]).with_suffix(".nfo")
    try:
        target.write_text(nfo_mod.build_nfo(movie), encoding="utf-8")
    except Exception as exc:
        raise HTTPException(500, f"写入失败：{exc}")
    return {"ok": True, "path": str(target)}


@router.get("/movies/{movie_id}/previews")
def movie_previews(movie_id: int, generate: bool = False) -> Dict[str, Any]:
    with db() as conn:
        mv = store.movie_detail(conn, movie_id)
        if not mv:
            raise HTTPException(404, "影片不存在")
        paths = store.get_previews(conn, movie_id)
        if generate:
            target = mv["files"][0]["path"] if mv.get("files") else None
            if not target or not os.path.exists(target):
                raise HTTPException(400, "找不到可播放的文件")
            cfg = load_config()
            ffmpeg = (cfg.get("ffmpeg_path") or "ffmpeg").strip() or "ffmpeg"
            out_dir = COVER_DIR / "preview" / str(movie_id)
            try:
                new_paths = store.generate_previews(target, str(out_dir), ffmpeg, 6)
            except Exception as exc:
                return {"available": False, "error": str(exc), "paths": []}
            if not new_paths:
                return {"available": False,
                        "error": "ffmpeg 不可用或抽帧失败（请在设置中配置 ffmpeg 路径）",
                        "paths": []}
            store.set_previews(conn, movie_id, new_paths)
            paths = new_paths
        urls = [f"/covers/preview/{movie_id}/{os.path.basename(p)}" for p in paths]
    return {"available": True, "paths": paths, "urls": urls}


@router.get("/covers/preview/{movie_id}/{fname}")
def preview_file(movie_id: int, fname: str) -> Response:
    p = COVER_DIR / "preview" / str(movie_id) / fname
    if not p.exists():
        raise HTTPException(404, "预览图不存在")
    return FileResponse(str(p), media_type="image/jpeg")


@router.post("/movies/{movie_id}/session/start")
def start_watch_session(movie_id: int, body: dict = Body(default={})):
    with db() as conn:
        sid = store.start_session(conn, movie_id, float(body.get('start_pos') or 0),
                                  body.get('method', 'external'))
    return {"session_id": sid}


@router.post("/movies/{movie_id}/session/{sid}/update")
def update_watch_session(movie_id: int, sid: int, body: dict = Body(default={})):
    with db() as conn:
        store.update_session(conn, sid, float(body.get('watched_sec') or 0), body.get('segments'))
    return {"ok": True}


@router.post("/movies/{movie_id}/session/{sid}/end")
def end_watch_session(movie_id: int, sid: int, body: dict = Body(default={})):
    with db() as conn:
        store.end_session(conn, sid, float(body.get('end_pos') or 0),
                          float(body.get('watched_sec') or 0),
                          int(body.get('finished') or 0), body.get('segments'))
    return {"ok": True}


@router.get("/movies/{movie_id}/sessions")
def movie_watch_sessions(movie_id: int, limit: int = Query(60, ge=1, le=500)):
    with db() as conn:
        return store.movie_sessions(conn, movie_id, limit)


@router.get("/watch-analytics")
def watch_analytics():
    with db() as conn:
        return store.watch_analytics(conn)


@router.get("/stream/{movie_id}")
def stream_movie(movie_id: int, request: Request):
    """流式播放主视频文件（支持 Range 断点续传）；浏览器无法解码的格式（如 MKV）会失败，此时请用系统播放器。"""
    with db() as conn:
        path = store.movie_primary_file(conn, movie_id)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="视频文件不存在或未入库")
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    media = {"mp4": "video/mp4", "webm": "video/webm", "ogg": "video/ogg",
             "mov": "video/quicktime", "mkv": "video/x-matroska", "avi": "video/x-msvideo",
             "m4v": "video/mp4", "ts": "video/mp2t", "flv": "video/x-flv"}.get(
        ext, "application/octet-stream")
    return FileResponse(path, media_type=media, filename=os.path.basename(path))


@router.get("/export/csv")
def export_csv() -> StreamingResponse:
    with db() as conn:
        rows = store.search_movies(conn, {"page": 1, "page_size": 100000})["items"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["番号", "标题", "女优", "类型", "厂商", "系列", "发行日期",
                     "时长", "评分", "字幕", "无码", "大小(GB)", "目录"])
    for r in rows:
        writer.writerow([
            r["display_code"], r["title"], " / ".join(r["actresses"]), " / ".join(r["genres"]),
            r["studio"], r["series"], r["release_date"], r["runtime"], r["rating"],
            "是" if r["subtitle"] else "", "是" if r["uncensored"] else "",
            round((r["size"] or 0) / 1024 ** 3, 2), r["folder"],
        ])
    data = "\ufeff" + buf.getvalue()
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="av-museum.csv"'},
    )


# ------------------------------------------------------------------ 配置/文件浏览


@router.get("/config")
def get_config() -> Dict[str, Any]:
    return load_config(refresh=True)


@router.put("/config")
def put_config(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    return update_config(payload)


def _lan_addresses() -> List[str]:
    """返回本机所有非回环 IPv4 地址（供局域网访问展示）。"""
    addrs: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            if info[0] == socket.AF_INET:
                ip = info[4][0]
                if ip and not ip.startswith("127.") and ip not in addrs:
                    addrs.append(ip)
    except Exception:
        pass
    # 兜底：UDP 探测本机对外 IP
    if not addrs:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                addrs.append(ip)
        except Exception:
            pass
        finally:
            s.close()
    # 排序：把大概率是虚拟化/容器网桥的网段（172.16/12、链路本地 169.254）排到最后，
    # 让真正的局域网地址（如 192.168.x / 10.x / 143.168.x 等）排在前面，二维码默认更可能正确。
    def _rank(ip: str) -> int:
        if ip.startswith("169.254."):
            return 2
        if ip.startswith("172."):
            oct2 = ip.split(".")[1]
            if oct2.isdigit() and 16 <= int(oct2) <= 31:
                return 2
        return 0
    return sorted(addrs, key=_rank)


def _build_date() -> str:
    """返回编译/构建日期。

    - 若 BUILD_DATE 已被打包脚本改写为真实构建日期则直接使用；
    - 否则（源码直接运行）回退到本文件最近修改日期，作为"代码编译日期"。
    """
    import datetime as _dt
    default_placeholder = "2026-08-16"
    if BUILD_DATE and BUILD_DATE != default_placeholder:
        return BUILD_DATE
    try:
        mtime = os.path.getmtime(__file__)
        return _dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    except Exception:
        return BUILD_DATE


@router.get("/server/info")
def server_info() -> Dict[str, Any]:
    """返回访问令牌与监听信息（供前端设置页展示）。"""
    from .config import ensure_access_token
    cfg = load_config(refresh=True)
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = cfg.get("server", {}).get("port", 8770)
    tok = ensure_access_token()
    lan = _lan_addresses()
    local_url = f"http://127.0.0.1:{port}/"
    urls = [local_url]
    for ip in lan:
        urls.append(f"http://{ip}:{port}/?token={tok}")
    return {
        "host": host,
        "port": port,
        "access_token": tok,
        "require_token_remote": cfg.get("server", {}).get("require_token_remote", True),
        "lan_addresses": lan,
        "access_urls": urls,
        "local_url": local_url,
        "app_version": APP_VERSION,
        "build_date": _build_date(),
        "update_feed": cfg.get("server", {}).get("update_feed", ""),
    }


def _parse_version(v: str) -> List[int]:
    """把 '1.9.0' 之类版本号解析为可比大小的整数元组（忽略非数字尾）。"""
    out: List[int] = []
    for part in str(v or "").split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    return out


def _newer(ver: str, base: str) -> bool:
    """ver 是否比 base 更新（按数字段逐个比较）。"""
    a, b = _parse_version(ver), _parse_version(base)
    n = max(len(a), len(b))
    a += [0] * (n - len(a))
    b += [0] * (n - len(b))
    return a > b


@router.get("/server/check-update")
def check_update(channel: str = Query("stable")) -> Dict[str, Any]:
    """检查是否有新版本。

    从配置 server.update_feed 指向的版本清单读取最新版本信息，与当前版本比较。
    两种来源自动识别：
      1) GitHub Releases API（如 https://api.github.com/repos/owner/repo/releases/latest）：
         解析 tag_name(版本，自动去 v 前缀) / html_url(发布页) / body(更新说明) /
         published_at(日期)。发版即在 git 打 tag 并创建 Release 即可生效。
      2) 自建版本清单 JSON：{"version": "1.9.1", "channel": "stable",
         "download_url": "...", "released": "2026-08-15", "notes": "..."}
    若未配置 feed 或拉取失败，则仅返回当前版本（update_available=False）。
    """
    cfg = load_config(refresh=True)
    feed_url = (cfg.get("server", {}).get("update_feed") or "").strip()
    result: Dict[str, Any] = {
        "current": APP_VERSION,
        "channel": channel,
        "update_available": False,
        "latest": APP_VERSION,
        "download_url": "",
        "released": "",
        "notes": "",
        "error": "",
    }
    if not feed_url:
        result["error"] = "未配置更新源 (server.update_feed)"
        return result
    try:
        import requests
        # 复用刮削代理（墙内访问 GitHub API 常需代理）
        proxy = (cfg.get("scraper", {}).get("proxy") or "") or None
        proxies = {"http": proxy, "https": proxy} if proxy else None
        headers = {"User-Agent": "AVM-update-check"}
        # 候选源：GitHub 公共加速镜像优先（墙内更稳），原始 API 兜底
        candidates = []
        if "api.github.com" in feed_url:
            for mirror in ("https://ghfast.top", "https://gh-proxy.com", "https://mirror.ghproxy.com"):
                candidates.append(feed_url.replace("https://api.github.com", f"{mirror}/https://api.github.com"))
        candidates.append(feed_url)

        def _valid(j) -> bool:
            # 403/限流会返回 {"message":..., "documentation_url":...} 而非版本数据
            return isinstance(j, dict) and (j.get("tag_name") or j.get("version"))

        last_err = ""
        feed = None
        for url in candidates:
            try:
                resp = requests.get(url, timeout=12, headers=headers, proxies=proxies)
                resp.raise_for_status()
                data = resp.json()
                if _valid(data):
                    feed = data
                    break
                last_err = f"响应不含版本信息（可能限流/403）：{str(data)[:120]}"
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                continue
        if feed is None:
            result["error"] = f"无法获取更新信息：{last_err}"
            return result

        latest = ""
        download_url = ""
        released = ""
        notes = ""
        feed_channel = channel
        if isinstance(feed, dict) and feed.get("tag_name"):
            # GitHub Releases 结构
            tag = str(feed.get("tag_name") or "")
            latest = tag[1:] if tag.startswith("v") or tag.startswith("V") else tag
            download_url = feed.get("html_url") or ""
            notes = feed.get("body") or ""
            published = feed.get("published_at") or feed.get("created_at") or ""
            if published:
                released = published[:10]  # 取 YYYY-MM-DD
            prerelease = feed.get("prerelease", False)
            draft = feed.get("draft", False)
            if prerelease or draft:
                feed_channel = "beta"  # 预发布/草稿不参与 stable 更新判定
        else:
            # 通用版本清单 JSON
            latest = str(feed.get("version") or APP_VERSION)
            download_url = feed.get("download_url", "")
            released = feed.get("released", "")
            notes = feed.get("notes", "")
            feed_channel = feed.get("channel", channel)

        result["latest"] = latest or APP_VERSION
        result["download_url"] = download_url
        result["released"] = released
        result["notes"] = notes
        if feed_channel == channel and _newer(latest, APP_VERSION):
            result["update_available"] = True
    except Exception as e:  # noqa: BLE001
        result["error"] = f"无法获取更新信息：{e}"
    return result


@router.get("/server/qr")
def server_qr(addr: str = "", size: int = 220) -> Response:
    """生成访问二维码（含 token 的局域网地址），返回 PNG。

    addr 可为空（默认取首个局域网地址）、主机名/IP，或完整 URL。
    """
    from .config import ensure_access_token
    cfg = load_config(refresh=True)
    port = cfg.get("server", {}).get("port", 8770)
    tok = ensure_access_token()
    if addr:
        if addr.startswith("http://") or addr.startswith("https://"):
            text = addr
        else:
            text = f"http://{addr}:{port}/?token={tok}"
    else:
        lan = _lan_addresses()
        text = f"http://{lan[0] if lan else '127.0.0.1'}:{port}/?token={tok}" if lan else f"http://127.0.0.1:{port}/"
    import qrcode
    qr = qrcode.QRCode(box_size=8, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#111", back_color="#fff")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@router.post("/server/reset-token")
def reset_token() -> Dict[str, Any]:
    """重置访问令牌（留空后由 ensure_access_token 生成新值）。"""
    from .config import ensure_access_token
    update_config({"server": {"access_token": ""}})
    tok = ensure_access_token()
    return {"access_token": tok}


@router.get("/fs/list")
def fs_list(path: str = Query("")) -> Dict[str, Any]:
    """给设置页做目录选择用。path 为空时返回盘符 / 根目录。"""
    if not path:
        roots: List[Dict[str, str]] = []
        if platform.system() == "Windows":
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    roots.append({"name": drive, "path": drive})
        else:
            roots.append({"name": "/", "path": "/"})
        home = str(Path.home())
        roots.append({"name": f"用户目录 ({home})", "path": home})
        return {"path": "", "parent": None, "dirs": roots}

    p = Path(path)
    if not p.is_dir():
        raise HTTPException(400, "目录不存在")
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                dirs.append({"name": child.name, "path": str(child)})
    except PermissionError:
        raise HTTPException(403, "无权访问该目录")
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs}


# ------------------------------------------------------------------ 发现与片单


@router.get("/movies/{movie_id}/similar")
def get_similar(movie_id: int, limit: int = 12) -> Dict[str, Any]:
    with db() as conn:
        if not store.movie_detail(conn, movie_id):
            raise HTTPException(404, "影片不存在")
        return store.similar_movies(conn, movie_id, limit)


@router.put("/movies/{movie_id}/progress")
def put_progress(movie_id: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    position = float(payload.get("position", 0) or 0)
    duration = float(payload.get("duration", 0) or 0)
    with db() as conn:
        if not store.movie_detail(conn, movie_id):
            raise HTTPException(404, "影片不存在")
        store.set_watch_progress(conn, movie_id, position, duration)
    return {"ok": True, "position": position, "duration": duration}


@router.get("/continue-watching")
def get_continue_watching(limit: int = 20) -> Dict[str, Any]:
    with db() as conn:
        return store.continue_watching(conn, limit)


@router.delete("/continue-watching")
def clear_continue_watching() -> Dict[str, Any]:
    with db() as conn:
        conn.execute("DELETE FROM watch_progress")
    return {"ok": True}


@router.get("/collections")
def api_list_collections() -> Dict[str, Any]:
    with db() as conn:
        store.ensure_system_collections(conn)
        return {"items": store.list_collections(conn)}


@router.get("/tags")
def api_list_tags() -> Dict[str, Any]:
    with db() as conn:
        return {"items": store.list_tags(conn)}


@router.post("/tags/rename")
def api_rename_tag(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    try:
        with db() as conn:
            return store.rename_tag(conn, payload.get("old"), payload.get("new"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/tags/delete")
def api_delete_tag(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    try:
        with db() as conn:
            return store.delete_tag(conn, payload.get("name"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/collections")
def api_create_collection(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    name = str(payload.get("name", "")).strip()
    kind = str(payload.get("kind", "manual"))
    rule = payload.get("rule", "")
    with db() as conn:
        try:
            cid = store.create_collection(conn, name, kind, rule)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        collection = next((c for c in store.list_collections(conn) if c["id"] == cid), None)
    return {"ok": True, "id": cid, "collection": collection}


@router.put("/collections/{cid}")
def api_rename_collection(cid: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    name = str(payload.get("name", "")).strip()
    with db() as conn:
        try:
            store.rename_collection(conn, cid, name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return {"ok": True}


@router.delete("/collections/{cid}")
def api_delete_collection(cid: int) -> Dict[str, Any]:
    with db() as conn:
        try:
            store.delete_collection(conn, cid)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return {"ok": True}


@router.get("/collections/{cid}")
def api_collection_detail(cid: int, page: int = 1, page_size: int = 60) -> Dict[str, Any]:
    with db() as conn:
        data = store.collection_movies(conn, cid, page, page_size)
        data["collection"] = next((c for c in store.list_collections(conn) if c["id"] == cid), None)
    return data


@router.post("/collections/{cid}/movies")
def api_add_to_collection(cid: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    movie_id = int(payload.get("movie_id"))
    with db() as conn:
        if not store.movie_detail(conn, movie_id):
            raise HTTPException(404, "影片不存在")
        store.add_to_collection(conn, cid, movie_id)
    return {"ok": True}


@router.delete("/collections/{cid}/movies/{movie_id}")
def api_remove_from_collection(cid: int, movie_id: int) -> Dict[str, Any]:
    with db() as conn:
        store.remove_from_collection(conn, cid, movie_id)
    return {"ok": True}


@router.post("/collections/{cid}/order")
def api_reorder_collection(cid: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    ids = payload.get("ids") or []
    with db() as conn:
        try:
            store.reorder_collection(conn, cid, [int(x) for x in ids])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return {"ok": True}


@router.post("/collections/{cid}/playhead")
def api_set_playhead(cid: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    movie_id = int(payload.get("movie_id"))
    with db() as conn:
        store.set_collection_playhead(conn, cid, movie_id)
    return {"ok": True}


# ------------------------------------------------------------------ 去重 / 存储 / 批量


@router.get("/dedup")
def dedup_scan() -> Dict[str, Any]:
    with db() as conn:
        return dedupe.scan(conn)


@router.post("/dedup/resolve")
def dedup_resolve(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    kind = str(payload.get("kind", "exact"))
    keep_file_id = int(payload.get("keep_file_id", 0))
    delete_files = bool(payload.get("delete_files", False))
    if kind not in {"exact", "version"}:
        raise HTTPException(400, "kind 仅支持 exact / version")
    with db() as conn:
        removed = dedupe.resolve_group(conn, kind, keep_file_id, delete_files)
    return {"ok": True, "removed": removed}


@router.get("/quality")
def quality_scan() -> Dict[str, Any]:
    """劣质片智能筛查：广告/推销样片、低码率压片、同番号多版本劣质版、损坏不完整。"""
    with db() as conn:
        return dedupe.scan_quality(conn)


@router.get("/storage")
def storage() -> Dict[str, Any]:
    with db() as conn:
        return store.storage_stats(conn)


@router.get("/integrity")
def integrity() -> Dict[str, Any]:
    with db() as conn:
        return store.integrity_issues(conn)


@router.get("/health-check")
def health_check() -> Dict[str, Any]:
    with db() as conn:
        return store.health_check(conn)


@router.post("/movies/batch")
def batch_update_movies(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    ids = payload.get("ids") or []
    if not isinstance(ids, list):
        raise HTTPException(400, "ids 必须是数组")
    ids = [int(i) for i in ids]
    with db() as conn:
        updated = store.batch_update(conn, ids, payload)
    return {"ok": True, "updated": updated}


@router.get("/health")
def health() -> Dict[str, Any]:
    with db() as conn:
        total = query_one(conn, "SELECT COUNT(*) AS c FROM movies")
    return {
        "ok": True,
        "python": sys.version.split()[0],
        "data_dir": str(DATA_DIR),
        "movies": total["c"] if total else 0,
        "scan_running": SCAN.running,
        "scrape_running": SCRAPE.running,
    }


# ------------------------------------------------------------------ 字幕匹配与对齐


@router.get("/movies/{movie_id}/subtitles")
def list_movie_subtitles(movie_id: int) -> List[Dict[str, Any]]:
    with db() as conn:
        if not store.movie_detail(conn, movie_id):
            raise HTTPException(404, "影片不存在")
        return subtitles.list_subtitles(conn, movie_id)


@router.post("/subtitles/match")
def match_subtitles(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """扫描字幕包目录，按番号匹配库内影片（仅探测，不改磁盘/库）。"""
    directory = str(payload.get("directory", "")).strip()
    if not directory or not os.path.isdir(directory):
        raise HTTPException(400, "请提供有效的字幕目录")
    with db() as conn:
        return subtitles.match_subtitles_to_movies(conn, directory)


@router.post("/subtitles/upload-and-match")
async def upload_and_match_subtitles(files: List[UploadFile] = File(default=[])) -> Dict[str, Any]:
    """前端用 <input type=file multiple> 选择字幕文件，上传后在服务端做匹配。

    文件先落到临时目录，再按番号匹配库内影片；不改写原上传文件，仅探测。
    """
    if not files:
        raise HTTPException(400, "请选择字幕文件")
    tmp_dir = Path(tempfile.gettempdir()) / "avm_subs"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    saved: List[str] = []
    for f in files:
        suffix = os.path.splitext(f.filename or "")[1].lower()
        if suffix not in subtitles.SUBTITLE_EXTS:
            continue
        dest = tmp_dir / f"{uuid.uuid4().hex}{suffix}"
        data = await f.read()
        dest.write_bytes(data)
        saved.append(str(dest))
    if not saved:
        raise HTTPException(400, "未识别到字幕文件（支持 srt/ass/ssa/vtt/sub/smi/txt）")
    with db() as conn:
        res = subtitles.match_subtitle_files(conn, saved)
    res["uploaded"] = saved
    return res


@router.post("/subtitles/align")
def align_subtitles(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """把字幕对齐到影片：重命名/复制到视频同目录，并登记数据库。

    body: { items: [{ subtitle_path, movie_id }], copy?: bool }
    对每个条目执行，并返回每个条目的对齐结果。
    """
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "items 不能为空")
    copy = bool(payload.get("copy", False))
    results = []
    with db() as conn:
        for it in items:
            sp = str(it.get("subtitle_path", "")).strip()
            mid = int(it.get("movie_id", 0))
            if not sp or not mid:
                results.append({"ok": False, "error": "参数缺失", "subtitle_path": sp})
                continue
            try:
                res = subtitles.align_subtitle(conn, sp, mid, copy=copy)
                results.append({"ok": True, **res})
            except Exception as e:  # noqa: BLE001
                results.append({"ok": False, "error": str(e), "subtitle_path": sp})
    return {"ok": True, "results": results}
