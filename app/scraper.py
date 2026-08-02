# -*- coding: utf-8 -*-
"""元数据抓取编排：调用数据源 -> 写入数据库 -> 落地封面。"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import images, store
from .config import load_config, avatar_dir
from .db import connect, query_all
from .jobs import SCRAPE
from .providers import build_providers

# 抓取结果里可以直接写回影片表的文本字段
_TEXT_FIELDS = ("title", "original_title", "plot", "release_date", "director")


def _is_empty(value: Any) -> bool:
    return value in (None, "", 0, 0.0)


def apply_metadata(conn, movie_id: int, meta: Dict[str, Any], cfg: Dict[str, Any],
                   overwrite: bool = False) -> Dict[str, Any]:
    """把抓到的元数据写入影片，返回实际改动的字段。"""
    current = store.movie_detail(conn, movie_id)
    if not current:
        return {"changed": []}

    payload: Dict[str, Any] = {}
    for field in _TEXT_FIELDS:
        if field in meta and (overwrite or _is_empty(current.get(field))):
            payload[field] = meta[field]
    # 扫描阶段的标题只是从文件名猜的，遇到真标题应当替换
    if meta.get("title") and current.get("title") in (current.get("code"), "", None):
        payload["title"] = meta["title"]

    for field in ("runtime", "rating"):
        if field in meta and (overwrite or _is_empty(current.get(field))):
            payload[field] = meta[field]

    for field in ("studio", "publisher", "series"):
        if meta.get(field) and (overwrite or _is_empty(current.get(field))):
            payload[field] = meta[field]

    for field in ("actresses", "genres", "tags"):
        if meta.get(field) and (overwrite or not current.get(field)):
            payload[field] = meta[field]

    if payload:
        store.update_movie(conn, movie_id, payload)

    cover_changed = False
    if cfg.get("cover", {}).get("download", True) and meta.get("cover"):
        if overwrite or not current.get("cover"):
            cover_changed = bool(save_cover(conn, current, str(meta["cover"]), cfg,
                                            source=meta.get("source", "scrape")))

    fanart_changed = False
    if cfg.get("media", {}).get("fanart_download", True) and meta.get("fanart"):
        if overwrite or not current.get("fanart"):
            fanart_changed = bool(save_fanart(conn, current, str(meta["fanart"]), cfg,
                                              source=meta.get("source", "scrape")))

    # 女优头像：刮削阶段抓到的头像 URL 直接落盘（不依赖 media.avatar_download 开关，
    # 因为源站已给出确切头像，且用户需要女优头像真正显示）。仅补不覆盖已有本地头像。
    avatar_changed = 0
    av_map = meta.get("actress_avatars") or {}
    if av_map:
        avatar_changed = _save_actress_avatars(conn, av_map, cfg)

    conn.execute(
        "UPDATE movies SET scraped_at = datetime('now','localtime'), scrape_source = ? WHERE id = ?",
        (str(meta.get("source", ""))[:120], movie_id),
    )
    changed = sorted(payload.keys()) + (["cover"] if cover_changed else []) + (["fanart"] if fanart_changed else [])
    return {"changed": changed, "source": meta.get("source", "")}


def save_cover(conn, movie: Dict[str, Any], location: str, cfg: Dict[str, Any],
               source: str = "manual") -> Optional[str]:
    """location 可以是 http(s) URL，也可以是本地图片路径。"""
    key = movie["key"]
    name: Optional[str] = None
    if location.lower().startswith(("http://", "https://")):
        data = images.download(location, cfg)
        if data:
            name = images.save_bytes(key, data, location)
    else:
        p = Path(location)
        if p.exists() and p.is_file():
            name = images.save_local_file(key, p)
    if name:
        conn.execute(
            "UPDATE movies SET cover = ?, cover_source = ? WHERE id = ?",
            (name, source[:60], movie["id"]),
        )
    return name


def save_fanart(conn, movie: Dict[str, Any], location: str, cfg: Dict[str, Any],
                source: str = "manual") -> Optional[str]:
    """保存影片背景大图（fanart）。location 可以是 URL 或本地图片路径。"""
    key = movie["key"]
    name: Optional[str] = None
    if str(location).lower().startswith(("http://", "https://")):
        data = images.download(location, cfg)
        if data:
            name = images.save_fanart(key, location, cfg)
    else:
        p = Path(location)
        if p.exists() and p.is_file():
            name = images.save_fanart(key, str(p), cfg)
    if name:
        conn.execute(
            "UPDATE movies SET fanart = ?, fanart_source = ? WHERE id = ?",
            (name, source[:60], movie["id"]),
        )
    return name


def _save_actress_avatars(conn, av_map: Dict[str, str], cfg: Dict[str, Any]) -> int:
    """把刮削得到的女优头像 URL 落盘为本地文件，并写回 actresses.avatar。

    仅补不覆盖：已有本地文件名（非远程 URL）的女优不会动；远程 URL 会被下载替换
    为本地文件。返回实际落盘数量。
    """
    if not av_map:
        return 0
    rows = query_all(conn, "SELECT id, name, avatar FROM actresses")
    by_name = {r["name"]: r for r in rows}
    changed = 0
    for name, url in av_map.items():
        r = by_name.get(name)
        if not r or not url:
            continue
        # 已有本地文件则跳过（不覆盖）
        if r["avatar"] and not images.is_remote(r["avatar"]):
            continue
        name_on_disk = images.save_avatar(name, url, cfg)
        if name_on_disk:
            conn.execute(
                "UPDATE actresses SET avatar = ? WHERE id = ?",
                (name_on_disk, r["id"]),
            )
            changed += 1
    return changed


def cache_remote_avatars(conn, cfg: Dict[str, Any] | None = None) -> Dict[str, int]:
    """把 actresses 表中仍是远程 URL 的头像，下载并落盘到配置的 avatar_dir。

    用于「把远程头像下载下来作为本地数据一部分」的按需迁移；仅在开启
    media.avatar_download 时执行实际下载，否则只统计可下载数量。
    """
    cfg = cfg or load_config()
    if not cfg.get("media", {}).get("avatar_download", False):
        return {"skipped": 1, "downloaded": 0, "failed": 0}
    rows = query_all(conn, "SELECT id, name, avatar FROM actresses WHERE avatar <> ''")
    downloaded = failed = 0
    for r in rows:
        url = r["avatar"]
        if not images.is_remote(url):
            continue  # 已是本地文件
        name = images.save_avatar(r["name"], url, cfg)
        if name:
            conn.execute("UPDATE actresses SET avatar = ? WHERE id = ?", (name, r["id"]))
            downloaded += 1
        else:
            failed += 1
    return {"skipped": 0, "downloaded": downloaded, "failed": failed}



def scrape_one(conn, movie_id: int, providers: List[Any], cfg: Dict[str, Any],
               overwrite: bool = False) -> Dict[str, Any]:
    movie = store.movie_detail(conn, movie_id)
    if not movie:
        return {"ok": False, "reason": "影片不存在"}
    if not providers:
        return {"ok": False, "reason": "没有启用任何元数据源"}

    for provider in providers:
        try:
            meta = provider.fetch(movie)
        except Exception as exc:
            return {"ok": False, "reason": f"{provider.name} 出错: {exc}"}
        if meta:
            result = apply_metadata(conn, movie_id, meta, cfg, overwrite)
            return {"ok": True, "provider": provider.name, **result}
    return {"ok": False, "reason": "所有数据源均未命中"}


def sniff_local_cover(conn, movie_id: int, cfg: Dict[str, Any]) -> Optional[str]:
    movie = store.movie_detail(conn, movie_id)
    if not movie:
        return None
    for f in movie.get("files") or []:
        hit = images.find_local_cover(f["path"], movie.get("code") or "")
        if hit:
            return save_cover(conn, movie, str(hit), cfg, source="local")
    return None


# ----------------------------------------------------------------- 批量任务


def _target_ids(conn, ids: Optional[List[int]], scope: str) -> List[int]:
    if ids:
        return [int(i) for i in ids]
    if scope == "all":
        sql = "SELECT id FROM movies WHERE has_code = 1 ORDER BY id"
    elif scope == "nocover":
        sql = "SELECT id FROM movies WHERE has_code = 1 AND cover = '' ORDER BY id"
    else:  # missing：只处理还没抓过的
        sql = "SELECT id FROM movies WHERE has_code = 1 AND scraped_at = '' ORDER BY id"
    return [r["id"] for r in query_all(conn, sql)]


def run_scrape(ids: Optional[List[int]] = None, scope: str = "missing",
               overwrite: Optional[bool] = None) -> Dict[str, Any]:
    cfg = load_config(refresh=True)
    if overwrite is None:
        overwrite = bool(cfg["scraper"].get("overwrite", False))
    providers = build_providers(cfg)
    delay = max(0, int(cfg["scraper"].get("delay_ms", 0))) / 1000.0

    conn = connect()
    try:
        targets = _target_ids(conn, ids, scope)
        SCRAPE.total = len(targets)
        SCRAPE.phase = "scraping"
        if not providers:
            SCRAPE.message = "没有启用任何元数据源，请先到「设置」中配置"
            return {"ok": False, "reason": "no_provider"}
        SCRAPE.message = f"待处理 {len(targets)} 部，数据源：{', '.join(p.name for p in providers)}"

        for idx, mid in enumerate(targets, 1):
            if SCRAPE.cancelled:
                break
            row = conn.execute("SELECT code, title FROM movies WHERE id = ?", (mid,)).fetchone()
            label = (row["code"] or row["title"]) if row else str(mid)
            try:
                res = scrape_one(conn, mid, providers, cfg, overwrite)
                if res.get("ok"):
                    SCRAPE.bump("success")
                    if "cover" in (res.get("changed") or []):
                        SCRAPE.bump("cover")
                else:
                    SCRAPE.bump("miss")
                    # 记录失败原因，便于前端展示「为什么没刮到」
                    reason = (res.get("reason") or "未知原因")
                    SCRAPE.counters.setdefault("reasons", {})
                    SCRAPE.counters["reasons"][reason] = SCRAPE.counters["reasons"].get(reason, 0) + 1
                # 数据源没给封面时，兜底找一次本地图
                if cfg["cover"].get("auto_local", True):
                    cur = conn.execute("SELECT cover FROM movies WHERE id = ?", (mid,)).fetchone()
                    if cur and not cur["cover"] and sniff_local_cover(conn, mid, cfg):
                        SCRAPE.bump("cover_local")
            except Exception as exc:
                SCRAPE.bump("failed")
                SCRAPE.error(f"{label}: {exc}")
                SCRAPE.counters.setdefault("reasons", {})
                SCRAPE.counters["reasons"][f"异常: {exc}"] = SCRAPE.counters["reasons"].get(f"异常: {exc}", 0) + 1
            SCRAPE.tick(label)
            if idx % 20 == 0:
                conn.commit()
            if delay:
                time.sleep(delay)
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, **SCRAPE.counters, "cancelled": SCRAPE.cancelled}


def start_scrape_async(ids: Optional[List[int]] = None, scope: str = "missing",
                       overwrite: Optional[bool] = None) -> bool:
    if not SCRAPE.start():
        return False

    def worker() -> None:
        try:
            res = run_scrape(ids, scope, overwrite)
            if not res.get("ok"):
                SCRAPE.finish(SCRAPE.message or "抓取未执行")
            else:
                SCRAPE.finish(
                    "抓取已取消" if res.get("cancelled") else
                    f"完成：成功 {res.get('success', 0)}，未命中 {res.get('miss', 0)}，"
                    f"封面 {res.get('cover', 0) + res.get('cover_local', 0)}"
                )
        except Exception as exc:
            SCRAPE.error(str(exc))
            SCRAPE.finish(f"抓取失败：{exc}")

    threading.Thread(target=worker, name="scrape-worker", daemon=True).start()
    return True


def batch_local_covers() -> Dict[str, int]:
    """只做本地封面嗅探，不联网。"""
    cfg = load_config()
    conn = connect()
    found = 0
    ids: List[int] = []
    try:
        ids = [r["id"] for r in query_all(conn, "SELECT id FROM movies WHERE cover = ''")]
        for mid in ids:
            if sniff_local_cover(conn, mid, cfg):
                found += 1
        conn.commit()
    finally:
        conn.close()
    return {"checked": len(ids), "found": found}
