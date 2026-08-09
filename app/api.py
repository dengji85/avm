# -*- coding: utf-8 -*-
"""HTTP 接口层。"""
from __future__ import annotations

import csv
import copy
import io
import os
import platform
import string
import subprocess
import threading
import json
import sys
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse

from . import ai as ai_mod
from . import dedupe, images, nfo as nfo_mod, providers, scanner, scraper, store
from .config import COVER_DIR, DATA_DIR, avatar_dir, _deep_merge, load_config, update_config
from .db import connect, db, query_all, query_one
from .jobs import SCAN, SCRAPE

router = APIRouter(prefix="/api")


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
    file_id = payload.get("file_id")
    reveal = bool(payload.get("reveal"))
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


@router.get("/rankings")
def get_rankings(kind: str = "watched", limit: int = 30) -> Dict[str, Any]:
    with db() as conn:
        return {"items": store.rankings(conn, kind, limit)}


@router.get("/watch-history")
def get_watch_history(page: int = 1, size: int = 50) -> Dict[str, Any]:
    with db() as conn:
        return store.watch_history(conn, page, size)


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
        store.delete_collection(conn, cid)
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
