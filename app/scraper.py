# -*- coding: utf-8 -*-
"""元数据抓取编排：调用数据源 -> 写入数据库 -> 落地封面。"""
from __future__ import annotations

import os
import tempfile
import threading
from itertools import islice
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from requests.exceptions import (
        Timeout, ConnectionError as ReqConnectionError, ConnectTimeout,
        ChunkedEncodingError, HTTPError,
    )
    _HAS_REQ = True
except Exception:  # requests 不可用（极少见）时退化为宽松判断
    _HAS_REQ = False

from . import images, store
from .config import load_config, avatar_dir
from .db import connect, query_all
from .jobs import SCRAPE
from .providers import build_providers
from .providers.base import NetBlocked

# 抓取结果里可以直接写回影片表的文本字段
_TEXT_FIELDS = ("title", "original_title", "plot", "release_date", "director")


def _is_empty(value: Any) -> bool:
    return value in (None, "", 0, 0.0)


def _classify_error(exc: BaseException) -> str:
    """把一个数据源异常归类为三类之一，决定它是否写进跳过名单。

    - 'net'  : 临时网络/服务端错误（超时、连接失败、5xx）。下次很可能成功，不该跳过。
    - 'miss' : 明确无此片（HTTP 4xx）。数据源稳定没有，跳过可省时间。
    - 'err'  : 其它异常（解析崩溃、字段错误等）。通常稳定失败，计入跳过更省心。
    """
    if not _HAS_REQ:
        return "err"
    if isinstance(exc, NetBlocked):
        # 反爬/人机验证页拦截：临时性访问受阻，下次可能放行，不应跳过。
        return "net"
    if isinstance(exc, (Timeout, ReqConnectionError, ConnectTimeout, ChunkedEncodingError)):
        return "net"
    if isinstance(exc, HTTPError):
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(code, int) and 500 <= code < 600:
            return "net"
        return "miss"  # 4xx：源确实没有这部
    return "err"


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



def _prefetch_images(meta: Optional[Dict[str, Any]], cfg: Dict[str, Any]):
    """把 meta 中的远程图片 URL 预下载为本地临时文件。

    返回 (meta副本, tmp_files)。批量并行时由 worker 调用，主线程据此把临时
    文件路径交给 apply_metadata（走 save_local_file，不再联网）。临时文件在
    主线程写库完成后续删。
    """
    if not meta:
        return meta, []
    m = dict(meta)
    tmp: List[str] = []

    def _dl(field: str) -> None:
        url = m.get(field)
        if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
            data = images.download(url, cfg)
            if data:
                base, ext = os.path.splitext(url.split("?")[0])
                suf = ext.lower() if ext.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"
                fd, path = tempfile.mkstemp(suffix=suf)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                tmp.append(path)
                m[field] = path

    _dl("cover")
    _dl("background")
    _dl("fanart")
    for av in (m.get("actresses") or []):
        if isinstance(av, dict) and isinstance(av.get("image"), str) \
                and av["image"].lower().startswith(("http://", "https://")):
            url = av["image"]
            data = images.download(url, cfg)
            if data:
                base, ext = os.path.splitext(url.split("?")[0])
                suf = ext.lower() if ext.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"
                fd, path = tempfile.mkstemp(suffix=suf)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                tmp.append(path)
                av["image"] = path
    return m, tmp


def scrape_one(conn, movie_id: int, providers: List[Any], cfg: Dict[str, Any],
               overwrite: bool = False) -> Dict[str, Any]:
    """单部刮削（供 API 单部补头像 / 单部抓取）。在主线程同步抓 + 写库。"""
    movie = store.movie_detail(conn, movie_id)
    if not movie:
        return {"ok": False, "reason": "影片不存在"}
    if not providers:
        return {"ok": False, "reason": "没有启用任何元数据源"}
    if SCRAPE.cancelled:
        return {"ok": False, "reason": "cancelled"}

    primary_meta = None
    primary_name = None
    for provider in providers:
        try:
            meta = provider.fetch(movie)
        except Exception as exc:
            return {"ok": False, "reason": f"{provider.name} 出错: {exc}"}
        if not meta:
            continue
        if primary_meta is None:
            # 第一个命中的源作为主源，写入全部元数据
            primary_meta = meta
            primary_name = provider.name
            continue
        # 后续源：若主源缺封面，用后续源补齐封面（不覆盖已写入的文本字段）
        if not primary_meta.get("cover") and meta.get("cover"):
            primary_meta = dict(primary_meta)
            primary_meta["cover"] = meta["cover"]
            primary_meta["source"] = f"{primary_name}+{provider.name}"

    if primary_meta is None:
        return {"ok": False, "reason": "所有数据源均未命中"}
    try:
        result = apply_metadata(conn, movie_id, primary_meta, cfg, overwrite)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return {"ok": True, "provider": primary_name, **result}


def scrape_one_parallel(movie: Dict[str, Any], providers: List[Any], cfg: Dict[str, Any],
                        overwrite: bool = False) -> Dict[str, Any]:
    """批量并行版：worker 线程内执行，不碰数据库。

    职责：抓取元数据 + 预下载图片到本地临时文件。返回结构化结果，由主线程
    （唯一写者）写库。单源失败不影响整体，交后续源 / 回退处理。

    结果多了一个 neterr 标记：当所有源都因临时网络/服务端错误失败时置位，
    主线程据此**不**把该影片写进跳过名单（下次重跑仍可能成功）。
    """
    if SCRAPE.cancelled:
        return {"cancelled": True, "movie_id": movie.get("id"), "code": movie.get("code")}
    primary_meta = None
    primary_name = None
    net_err = False  # 是否存在「临时网络/服务端错误」源
    try:
        for provider in providers:
            try:
                meta = provider.fetch(movie)
            except Exception as exc:
                # 单源异常：分类后继续后续源（异常隔离）
                if _classify_error(exc) == "net":
                    net_err = True
                continue
            if not meta:
                continue
            if primary_meta is None:
                primary_meta = meta
                primary_name = provider.name
                continue
            if not primary_meta.get("cover") and meta.get("cover"):
                primary_meta = dict(primary_meta)
                primary_meta["cover"] = meta["cover"]
                primary_meta["source"] = f"{primary_name}+{provider.name}"

        if primary_meta is None:
            # 没有命中：若是网络/服务端临时错误导致，标记 neterr（不进跳过名单）
            return {"miss": True, "neterr": net_err,
                    "movie_id": movie.get("id"), "code": movie.get("code")}
        m, tmp = _prefetch_images(primary_meta, cfg)
        return {"movie_id": movie.get("id"), "code": movie.get("code"),
                "meta": m, "tmp": tmp, "provider": primary_name}
    except Exception as exc:  # 整体兜底，绝不抛出到 future 外
        return {"error": str(exc)[:300], "movie_id": movie.get("id"), "code": movie.get("code")}


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


def _target_ids(conn, ids: Optional[List[int]], scope: str,
                skip_ids: Optional[set] = None) -> List[int]:
    """返回本次要刮削的影片 id 列表。

    skip_ids：来自 scrape_skip 的「已知稳定失败」影片集合。命中其中的项默认
    被排除（scope='all' 也排除），除非调用方显式传入 force（此时 skip_ids 应为
    None）。排除用 LEFT JOIN，避免大 IN 列表，也更直观。
    """
    if ids:
        return [int(i) for i in ids]
    skip_join = ""
    skip_where = ""
    params: List[Any] = []
    if skip_ids:
        skip_join = "LEFT JOIN scrape_skip ss ON ss.movie_id = movies.id"
        skip_where = " AND ss.movie_id IS NULL"
    if scope == "all":
        sql = f"SELECT movies.id AS mid FROM movies {skip_join} WHERE has_code = 1{skip_where} ORDER BY mid"
    elif scope == "nocover":
        sql = f"SELECT movies.id AS mid FROM movies {skip_join} WHERE has_code = 1 AND cover = ''{skip_where} ORDER BY mid"
    elif scope == "retry":
        # 只重跑被跳过名单里的项（用于「强制重跑失败项」）
        sql = "SELECT movie_id AS mid FROM scrape_skip ORDER BY mid"
        return [r["mid"] for r in query_all(conn, sql)]
    else:  # missing：只处理还没抓过的（且不在跳过名单）
        sql = (f"SELECT movies.id AS mid FROM movies {skip_join} "
               f"WHERE has_code = 1 AND scraped_at = ''{skip_where} ORDER BY mid")
    return [r["mid"] for r in query_all(conn, sql, params)]


def run_scrape(ids: Optional[List[int]] = None, scope: str = "missing",
               overwrite: Optional[bool] = None, task_id: str = "",
               force: bool = False) -> Dict[str, Any]:
    cfg = load_config(refresh=True)
    if overwrite is None:
        overwrite = bool(cfg["scraper"].get("overwrite", False))
    providers = build_providers(cfg)
    delay = max(0, int(cfg["scraper"].get("delay_ms", 0))) / 1000.0
    auto_local = bool(cfg["cover"].get("auto_local", True))

    def _touch_skip(conn, mid: int, code: str, reason: str, kind: str) -> None:
        """稳定失败时把影片加进跳过名单（累计失败次数）；已存在则 +1。"""
        try:
            conn.execute(
                """INSERT INTO scrape_skip (movie_id, code, reason, kind, count, auto, updated_at)
                       VALUES (?,?,?,?,1,1,datetime('now','localtime'))
                   ON CONFLICT(movie_id) DO UPDATE SET
                       reason=excluded.reason, kind=excluded.kind, count=count+1,
                       updated_at=datetime('now','localtime')""",
                (mid, code or "", reason[:300], kind),
            )
        except Exception as exc:
            SCRAPE.error(f"更新跳过名单失败：{exc}")

    def _clear_skip(conn, mid: int) -> None:
        """刮削成功后从跳过名单移除（自愈）。"""
        try:
            conn.execute("DELETE FROM scrape_skip WHERE movie_id = ?", (mid,))
        except Exception:
            pass

    def _log_row(conn, mid, label, code, provider, status, reason, elapsed_ms, ok):
        """把单部结果写入持久化刮削日志（主线程唯一写者负责）。"""
        try:
            fpath = ""
            frows = query_all(conn, "SELECT path FROM movie_files WHERE movie_id = ? LIMIT 1", (mid,))
            if frows:
                fpath = frows[0]["path"]
            conn.execute(
                """INSERT INTO scrape_logs
                       (task_id, file_path, code, provider, status, reason, elapsed_ms, movie_id)
                       VALUES (?,?,?,?,?,?,?,?)""",
                (task_id, fpath, code or "", provider, status, reason[:500], int(elapsed_ms), mid if ok else None),
            )
        except Exception as log_exc:
            SCRAPE.error(f"写入刮削日志失败：{log_exc}")

    conn = connect()
    try:
        # 跳过名单：除非 force（强制全量重跑）或显式指定 ids，否则默认排除已知稳定失败项。
        skip_ids = None
        if not force and not ids and scope != "retry":
            try:
                skip_ids = {r["movie_id"] for r in
                            query_all(conn, "SELECT movie_id FROM scrape_skip")}
            except Exception:
                skip_ids = None
        targets = _target_ids(conn, ids, scope, skip_ids)
        SCRAPE.total = len(targets)
        SCRAPE.phase = "scraping"
        if not providers:
            SCRAPE.message = "没有启用任何元数据源，请先到「设置」中配置"
            return {"ok": False, "reason": "no_provider"}
        if not targets:
            SCRAPE.message = "没有需要刮削的影片"
            return {"ok": True, "success": 0, "miss": 0, "cancelled": False}
        SCRAPE.message = f"待处理 {len(targets)} 部，数据源：{', '.join(p.name for p in providers)}"

        workers = max(1, int(cfg["scraper"].get("workers", 4)))
        per_worker_delay = delay / workers if delay else 0.0

        def _process(mid: int, res: Dict[str, Any], t0: float) -> None:
            """在主线程（唯一写者）消费一个 worker 的结果：写库 + 记日志。"""
            row = conn.execute("SELECT code, title, key FROM movies WHERE id = ?", (mid,)).fetchone()
            label = (row["code"] or row["title"]) if row else str(mid)
            code = row["code"] if row else ""
            elapsed_ms = (time.monotonic() - t0) * 1000
            provider = res.get("source") or res.get("provider") or ""

            try:
                if res.get("cancelled"):
                    status, reason, ok = "miss", "已取消", False
                elif res.get("error"):
                    status, reason, ok = "error", res["error"], False
                elif res.get("miss"):
                    # 网络/服务端临时错误导致的整体未命中：不进跳过名单，下次仍可重试
                    if res.get("neterr"):
                        status, reason, ok = "miss", "数据源临时网络/服务端错误", False
                    else:
                        status, reason, ok = "miss", "所有数据源均未命中", False
                elif "meta" in res:
                    # 命中：主线程写库（单写者，封面已是本地临时文件，走 save_local_file 不联网）
                    meta = res["meta"]
                    ok = False
                    try:
                        ares = apply_metadata(conn, mid, meta, cfg, overwrite)
                        ok = True
                        reason = "成功"
                        status = "ok"
                        if "cover" in (ares.get("changed") or []):
                            SCRAPE.bump("cover")
                        # 数据源没给封面时，主线程兜底找一次本地图
                        if auto_local and not meta.get("cover"):
                            if sniff_local_cover(conn, mid, cfg):
                                SCRAPE.bump("cover_local")
                                ares.setdefault("cover_local", True)
                    except Exception as exc:
                        ok = False
                        status, reason = "error", f"写库异常: {exc}"
                        conn.rollback()
                    finally:
                        # 无论成败都清理 worker 落盘的临时图片文件
                        for tp in (res.get("tmp") or []):
                            try:
                                os.remove(tp)
                            except Exception:
                                pass
                else:
                    status, reason, ok = "miss", res.get("reason") or "未命中", False

                # 维护跳过名单：命中=自愈移除；稳定失败=沉淀进名单（临时网络错误例外）。
                if ok:
                    _clear_skip(conn, mid)
                elif status == "ok":
                    pass
                elif res.get("neterr") and res.get("miss"):
                    pass  # 临时错误不进名单
                elif status in ("miss", "error"):
                    if "取消" in reason:
                        pass  # 取消导致的未命中不沉淀
                    else:
                        kind = "error" if status == "error" else "miss"
                        _touch_skip(conn, mid, code, reason, kind)

                if ok:
                    SCRAPE.bump("success")
                else:
                    SCRAPE.counters.setdefault("reasons", {})
                    SCRAPE.counters["reasons"][reason] = SCRAPE.counters["reasons"].get(reason, 0) + 1
                    if status == "error":
                        SCRAPE.bump("fail")
                        SCRAPE.error(f"{label}：{reason}")
                    else:
                        SCRAPE.bump("miss")
                        SCRAPE.log(f"{label}：{reason}", "warn", code=code)
                _log_row(conn, mid, label, code, provider, status, reason, elapsed_ms, ok)
                conn.commit()
            finally:
                SCRAPE.tick(label)

        done = 0
        cancelled = False
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scrape") as pool:
            # 分批提交，每批前检查取消；取消后不再提交剩余、且果断停止收集在途结果。
            it = iter(targets)
            batch = list(islice(it, workers))
            pending = {}
            while batch and not cancelled:
                for mid in batch:
                    if SCRAPE.cancelled:
                        cancelled = True
                        break
                    t0 = time.monotonic()
                    fut = pool.submit(scrape_one_parallel, store.movie_detail(conn, mid), providers, cfg, overwrite)
                    pending[fut] = (mid, t0)
                    if per_worker_delay:
                        time.sleep(per_worker_delay)
                # 收集本批结果：用 wait(timeout) 周期轮询，保证取消信号在 ~0.5s 内响应，
                # 不被卡在网络阻塞的 worker 长时间挂起（线程无法强制中断，只能等其超时返回）。
                while pending:
                    if SCRAPE.cancelled:
                        cancelled = True
                        break
                    done_set = wait(pending.keys(), timeout=0.5,
                                    return_when=FIRST_COMPLETED).done
                    if not done_set:
                        continue  # 0.5s 内无完成，继续轮询取消
                    for fut in done_set:
                        if fut not in pending:
                            continue
                        mid, t0 = pending.pop(fut)
                        done += 1
                        try:
                            res = fut.result()
                        except Exception as exc:
                            res = {"error": str(exc)[:300]}
                        if SCRAPE.cancelled and not res.get("cancelled"):
                            res = {"cancelled": True}
                        _process(mid, res, t0)
                        if SCRAPE.cancelled:
                            cancelled = True
                            break
                if cancelled:
                    break
                batch = list(islice(it, workers))

            if cancelled:
                # 丢弃未开始/在途的任务，停止线程池（不等待网络阻塞的 worker）
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pool.shutdown(wait=False)
                SCRAPE.message = f"已取消：完成 {done}/{SCRAPE.total}"
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, **SCRAPE.counters, "cancelled": SCRAPE.cancelled}


def start_scrape_async(ids: Optional[List[int]] = None, scope: str = "missing",
                       overwrite: Optional[bool] = None, force: bool = False) -> bool:
    if not SCRAPE.start():
        return False
    task_id = SCRAPE.task_id

    def worker() -> None:
        try:
            res = run_scrape(ids, scope, overwrite, task_id=task_id, force=force)
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
