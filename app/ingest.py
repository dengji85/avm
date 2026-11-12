# -*- coding: utf-8 -*-
"""浏览器接入：在线来源的幂等入库、来源管理与观看事件落库。

职责边界（AGENTS.md 第 3 节）：

* 身份计算（URL 规范化 / 番号提取）在 ``movie_identity.py``，本模块不重复实现；
* HTTP 薄壳在 ``routers/ingest.py``，本模块不处理 Request / Response；
* 观看会话仍复用 ``store.start_session/update_session/end_session`` 与
  ``watch_sessions``，统计页面因此零改动。

纯在线影片会在 ``movie_files`` 写一条 ``kind='online'`` 的虚拟记录：
详情页 files、播放链路能复用既有 SQL，但**文件口径**（文件数、容量、质量评估、
去重、NFO 导出、删除）必须一律排除它，见 store / dedupe / nfo_writer 中的 kind 过滤。
缺失检测由 ``kind`` 跳过（见 store.check_missing_files）。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from . import movie_identity as ident
from . import store
from .config import load_config
from .db import query_all, query_one

SCHEMA_VERSION = 1
ONLINE_KIND = "online"
VALID_KINDS = ("page", "hls", "progressive", "cloud", "site_page")
VALID_EVENTS = ("start", "heartbeat", "pause", "end")


def cfg() -> Dict[str, Any]:
    return dict(load_config().get("ingest") or {})


def enabled() -> bool:
    return bool(cfg().get("enabled", True))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def capabilities() -> Dict[str, Any]:
    """插件能力协商：版本、支持的来源类型与事件、鉴权方式。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": enabled(),
        "kinds": list(VALID_KINDS),
        "events": list(VALID_EVENTS),
        "auth": ["X-Access-Token", "Authorization: Bearer"],
        "allow_create": bool(cfg().get("allow_create", True)),
        "watch_method": str(cfg().get("watch_method") or "extension"),
    }


def _public(ev: Dict[str, Any]) -> Dict[str, Any]:
    """对插件回显的证据：绝不带完整播放地址。"""
    return {
        "code": ev.get("code") or "",
        "code_norm": ev.get("code_norm") or "",
        "site": ev.get("site") or "",
        "site_id": ev.get("site_id") or "",
        "url_canonical": ev.get("url_canonical") or "",
        "page_url": ident.mask_url(ev.get("page_url") or ""),
        "play_url": ident.mask_url(ev.get("play_url") or ""),
        "title": ev.get("title") or "",
        "kind": ev.get("kind") or "page",
    }


# ----------------------------------------------------------------- 来源查询

def list_sources(conn: sqlite3.Connection, movie_id: int) -> List[Dict[str, Any]]:
    from . import stream_verify
    rows = query_all(
        conn,
        "SELECT id, movie_id, kind, site, site_id, is_primary, pinned, priority, available, "
        "status, check_note, title, fetched_at, expires_at, last_checked, created_at, "
        "page_url, play_url, refresh_from, media_width, media_height, media_bandwidth, "
        "media_source, media_probe_note, media_probe_at, sub_zh, sub_evidence, sub_langs "
        "FROM movie_sources WHERE movie_id = ? "
        "ORDER BY is_primary DESC, priority DESC, id ASC",
        (int(movie_id),),
    )
    out = []
    for r in rows:
        d = dict(r)
        # 「打开源站」用规范化页面地址：只保留白名单的作品标识参数，
        # 签名 / 追踪参数一律不带，既能跳到影片页又不会回显临时凭据。
        d["open_url"] = ident.canonical_url(str(d.get("page_url") or ""))
        # 任何带签名 / 查询串的地址都不回显，只给脱敏后的 scheme://host/path
        for key in ("page_url", "play_url", "refresh_from"):
            d[key] = ident.mask_url(str(d.get(key) or ""))
        d["expired"] = stream_verify.expired(d.get("expires_at"))
        d["checking"] = str(d.get("status") or "") == "checking"
        # 派生视图：页面入口是稳定身份，播放地址才是易失的。
        # page_label 用于「站点 + 路径」拼接展示（已脱敏，不含查询串）。
        d["has_page"] = bool(d["open_url"])
        # 「可播」= 已验证可用；stale（待验证）/ invalid 都只保留「可试」，
        # UI 的主入口此时应该回源站，而不是把一个未确认的地址当默认动作。
        d["playable"] = bool(
            str(d.get("status") or "") == "valid"
            and int(d.get("available") or 0) and d["play_url"])
        d["page_label"] = str(d["page_url"]).replace("https://", "").replace("http://", "")
        # 卡片里拼在站点名后面，只取路径，避免域名重复
        d["page_path"] = urlsplit(str(d["page_url"])).path or "/"
        out.append(d)
    return out


def primary_play_url(conn: sqlite3.Connection, movie_id: int) -> str:
    """用户指定的主源播放地址；没有则返回空串（由 store 回退到本地文件）。"""
    row = query_one(
        conn,
        "SELECT play_url FROM movie_sources WHERE movie_id = ? AND is_primary = 1 "
        "AND available = 1 AND play_url <> '' AND status NOT IN ('invalid','unsupported') "
        "ORDER BY priority DESC, id ASC LIMIT 1",
        (int(movie_id),),
    )
    return str((row or {}).get("play_url") or "")


# ----------------------------------------------------------------- 身份归并

def resolve(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    """四层证据查找已有影片；多条证据指向不同影片时返回 ambiguous 交人工裁决。"""
    ev = ident.build_evidence(payload or {})
    ids: set[int] = set()

    if ev["code_norm"]:
        for r in query_all(conn, "SELECT id FROM movies WHERE code_norm = ? AND code_norm <> ''",
                           (ev["code_norm"],)):
            ids.add(int(r["id"]))
        a = query_one(conn, "SELECT movie_id FROM movie_aliases WHERE alias = ?",
                      (ident.make_alias("code", ev["code_norm"]),))
        if a:
            ids.add(int(a["movie_id"]))

    if ev["site"] and ev["site_id"]:
        r = query_one(conn, "SELECT movie_id FROM movie_external_ids WHERE site = ? AND site_id = ?",
                      (ev["site"], ev["site_id"]))
        if r:
            ids.add(int(r["movie_id"]))

    if ev["url_canonical"]:
        r = query_one(conn, "SELECT movie_id FROM movie_sources WHERE url_canonical = ?",
                      (ev["url_canonical"],))
        if r:
            ids.add(int(r["movie_id"]))

    if len(ids) > 1:
        return {"status": "ambiguous", "movie_id": None,
                "candidates": sorted(ids), "evidence": _public(ev),
                "message": "多条证据指向不同影片，请人工确认后再加入"}
    mid = next(iter(ids), None)
    return {
        "status": "existing" if mid else "new",
        "movie_id": mid,
        "evidence": _public(ev),
        "sources": list_sources(conn, mid) if mid else [],
    }


def _insert_aliases(conn: sqlite3.Connection, movie_id: int, ev: Dict[str, Any]) -> None:
    pairs = []
    if ev["code_norm"]:
        pairs.append(("code", ev["code_norm"]))
    if ev["site"] and ev["site_id"]:
        pairs.append(("site", ev["site"] + "/" + ev["site_id"]))
    if ev["url_canonical"]:
        pairs.append(("url", ev["url_canonical"]))
    for kind, value in pairs:
        conn.execute(
            "INSERT OR IGNORE INTO movie_aliases(alias, alias_kind, movie_id) VALUES(?,?,?)",
            (ident.make_alias(kind, value), kind, int(movie_id)),
        )


def _ensure_virtual_file(conn: sqlite3.Connection, movie_id: int,
                         ev: Optional[Dict[str, Any]] = None) -> None:
    """给纯在线影片写一条虚拟 movie_files 记录，让现有播放 / 列表 SQL 直接可用。

    一部影片只保留一条虚拟记录，``strm_url`` 始终指向当前主源；
    追加新来源不会互相覆盖，切换主源只需更新这一行。
    """
    ev = ev or {}
    play = primary_play_url(conn, movie_id) or str(ev.get("play_url") or "")
    if not play:
        return
    vpath = ident.virtual_path(movie_id)
    ext = "m3u8" if ident.is_hls_url(play) else "mp4"
    filename = (str(ev.get("site") or "") or "online") + "." + ext
    row = query_one(conn, "SELECT id FROM movie_files WHERE path = ?", (vpath,))
    if row:
        conn.execute(
            "UPDATE movie_files SET movie_id = ?, strm_url = ?, filename = ?, ext = ?, "
            "size = 0, missing = 0, missing_since = '', part = 1, kind = ? WHERE id = ?",
            (int(movie_id), play, filename, ext, ONLINE_KIND, int(row["id"])),
        )
    else:
        conn.execute(
            "INSERT INTO movie_files(movie_id, path, filename, ext, size, mtime, part, "
            "missing, strm_url, kind) VALUES(?,?,?,?,0,0,1,0,?,?)",
            (int(movie_id), vpath, filename, ext, play, ONLINE_KIND),
        )


def sync_virtual_file(conn: sqlite3.Connection, movie_id: int,
                      ev: Optional[Dict[str, Any]] = None) -> None:
    """切换 / 更新主源后同步虚拟记录（供 stream_verify 等外部模块调用）。"""
    _ensure_virtual_file(conn, movie_id, ev)


def link(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    """幂等建片 / 追加来源。同一个规范 URL 或站内 ID 不会挂到两部影片。"""
    if not enabled():
        return {"ok": False, "message": "浏览器接入已关闭"}
    ev = ident.build_evidence(payload or {})
    if not ev["url_canonical"] and not ev["code_norm"]:
        return {"ok": False, "message": "缺少可识别的页面地址或番号"}

    found = resolve(conn, payload or {})
    if found["status"] == "ambiguous":
        return {"ok": False, "status": "ambiguous",
                "candidates": found["candidates"], "message": found["message"]}

    movie_id = found["movie_id"]
    created = False
    status = ""
    if not movie_id:
        if not bool(cfg().get("allow_create", True)):
            return {"ok": False, "message": "已关闭自动建片，请先在库中创建影片"}
        if ev["code_norm"]:
            key = ev["code"].upper()
        else:
            seed = ev["url_canonical"] or ev["title"] or ev["page_url"]
            key = "WEB:" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
        conn.execute(
            "INSERT OR IGNORE INTO movies(key, code, code_norm, has_code, code_rule, title) "
            "VALUES(?,?,?,?,?,?)",
            (key, ev["code"], ev["code_norm"], 1 if ev["code_norm"] else 0,
             ev["code_rule"], ev["title"]),
        )
        row = query_one(conn, "SELECT id FROM movies WHERE key = ?", (key,))
        if not row:
            return {"ok": False, "message": "影片创建失败"}
        movie_id = int(row["id"])
        created = True

    # 后来刮到番号时补齐展示字段，但不改 key（唯一归并键保持稳定）
    if ev["code_norm"] and not created:
        conn.execute(
            "UPDATE movies SET code = CASE WHEN code = '' THEN ? ELSE code END, "
            "code_norm = CASE WHEN code_norm = '' THEN ? ELSE code_norm END, "
            "has_code = CASE WHEN has_code = 0 THEN 1 ELSE has_code END, "
            "title = CASE WHEN title = '' THEN ? ELSE title END WHERE id = ?",
            (ev["code"], ev["code_norm"], ev["title"], int(movie_id)),
        )

    _insert_aliases(conn, movie_id, ev)
    if ev["site"] and ev["site_id"]:
        conn.execute(
            "INSERT OR IGNORE INTO movie_external_ids(movie_id, site, site_id) VALUES(?,?,?)",
            (int(movie_id), ev["site"], ev["site_id"]),
        )

    has_primary = query_one(
        conn, "SELECT id FROM movie_sources WHERE movie_id = ? AND is_primary = 1",
        (int(movie_id),))
    # movie_sources.site_id 是 NOT NULL + UNIQUE(site, site_id)：
    #   空串会让「同一站点、无 site_id 的多部影片」撞唯一键；
    #   NULL 又违反 NOT NULL。用页面地址哈希做唯一占位值，两全。
    sid = ev["site_id"] or ("pg:" + hashlib.sha1(
        str(ev["page_url"] or "").encode("utf-8")).hexdigest()[:12])
    expires_at = str((payload or {}).get("expires_at") or "").strip()
    refresh_from = ev["page_url"] or ev["url_canonical"]

    def _find_source():
        """按「规范 URL → 站内 ID」找已有来源；两步都命中同一条才算重复。"""
        if ev["url_canonical"]:
            row = query_one(conn, "SELECT id, play_url FROM movie_sources "
                                  "WHERE url_canonical = ?", (ev["url_canonical"],))
            if row:
                return row
        if ev["site"] and ev["site_id"]:
            return query_one(conn, "SELECT id, play_url FROM movie_sources "
                                   "WHERE site = ? AND site_id = ?",
                             (ev["site"], ev["site_id"]))
        return None

    play_url_updated = False
    src = _find_source()
    if src:
        source_id = int(src["id"])
        status = "duplicate"
        new_play = str(ev["play_url"] or "")
        if new_play and new_play != str(src["play_url"] or ""):
            # 抓到新播放地址：更新并重新验证，有效期由验证任务回填
            play_url_updated = True
            conn.execute(
                "UPDATE movie_sources SET movie_id = ?, kind = ?, site = ?, site_id = ?, "
                "page_url = ?, play_url = ?, title = CASE WHEN ? <> '' THEN ? ELSE title END, "
                "available = 1, status = 'valid', check_note = '', last_checked = '', "
                "fetched_at = ?, expires_at = ?, refresh_from = ?, "
                "updated_at = datetime('now','localtime') WHERE id = ?",
                (int(movie_id), ev["kind"], ev["site"], sid, ev["page_url"],
                 new_play, ev["title"], ev["title"], _now(), expires_at,
                 refresh_from, source_id),
            )
        else:
            # 地址没变、或这次没抓到地址：只补元数据。
            # 绝不能用空地址覆盖已入库的播放地址，也不能抹掉验证结果与有效期
            # （重复点「加入片库」不能把一部可播的片子变回不可播）。
            conn.execute(
                "UPDATE movie_sources SET movie_id = ?, site = ?, site_id = ?, page_url = ?, "
                "title = CASE WHEN ? <> '' THEN ? ELSE title END, refresh_from = ?, "
                "updated_at = datetime('now','localtime') WHERE id = ?",
                (int(movie_id), ev["site"], sid, ev["page_url"],
                 ev["title"], ev["title"], refresh_from, source_id),
            )
    else:
        # 已有影片、但不是同一条来源：作为新来源挂上去。唯一索引
        # （idx_ms_url / idx_ms_site）仍可能因并发或规范化差异先命中，
        # 这时落回「重复」分支，绝不把 IntegrityError 抛成 500。
        play_url_updated = bool(ev["play_url"])
        status = "source_added"
        try:
            cur = conn.execute(
                "INSERT INTO movie_sources(movie_id, kind, site, site_id, page_url, "
                "url_canonical, play_url, title, is_primary, priority, available, status, "
                "confidence, evidence, fetched_at, expires_at, refresh_from) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,1,'valid',?,?,?,?,?)",
                (int(movie_id), ev["kind"], ev["site"], sid, ev["page_url"],
                 ev["url_canonical"], ev["play_url"], ev["title"],
                 0 if has_primary else 1, 0, ev["confidence"],
                 "ingest", _now(), expires_at, refresh_from),
            )
            source_id = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            dup = _find_source()
            if not dup:
                return {"ok": False, "message": "来源写入冲突，请刷新后重试"}
            source_id = int(dup["id"])
            status = "duplicate"
            play_url_updated = False
            conn.execute(
                "UPDATE movie_sources SET page_url = ?, title = CASE WHEN ? <> '' "
                "THEN ? ELSE title END, refresh_from = ?, "
                "updated_at = datetime('now','localtime') WHERE id = ?",
                (ev["page_url"], ev["title"], ev["title"], refresh_from, source_id),
            )

    _ensure_virtual_file(conn, movie_id, ev)
    store.refresh_aggregate(conn, movie_id)
    if created:
        status = "created"
    sources = list_sources(conn, movie_id)
    return {
        "ok": True,
        "status": status,
        "movie_id": int(movie_id),
        "source_id": source_id,
        "source_count": len(sources),
        "primary": bool(not has_primary),
        "play_url_updated": play_url_updated,
        "evidence": _public(ev),
        "sources": sources,
    }


# ----------------------------------------------------------------- 文本批量导入

# 一行的形态：「片名或番号」+ 分隔符（制表符 / 逗号 / 竖线 / 空格）+ 播放地址。
# 地址用正则单独摘出来，剩下的部分一律当片名，这样片名里带逗号也不会被切碎。
_IMPORT_URL_RE = re.compile(r"https?://[^\s,，|｜]+", re.I)
_IMPORT_SEP_RE = re.compile(r"[\t,，|｜]+")


def _valid_http_url(url: str) -> bool:
    try:
        p = urlsplit(str(url or ""))
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def import_links(conn: sqlite3.Connection, text: str, scrape: bool = False) -> Dict[str, Any]:
    """按「片名/番号 + 播放地址」逐行入库，返回每行结果。

    复用 :func:`link` 的幂等建片 / 加来源；单行失败只记原因，不影响其他行。
    ``.m3u8`` 直接当播放地址入库；普通页面地址写进 ``page_url``，由调用方
    在事务提交后排队 ``refresh`` 回源站提取真实播放地址（refresh_from 那条路）。

    返回值里的 ``follow`` 是提交后才能执行的后置动作（轻验证 / 回源提取 /
    刮削），由路由层负责派发——不能在未提交的事务里起独立连接。
    """
    from . import parser

    lines = str(text or "").splitlines()
    items: List[Dict[str, Any]] = []
    follow: List[tuple] = []
    total = 0
    for no, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        total += 1
        base = {"line": no, "text": s[:200]}
        m = _IMPORT_URL_RE.search(s)
        if not m:
            items.append(dict(base, status="failed",
                              message="缺少播放地址（需以 http:// 或 https:// 开头）"))
            continue
        url = m.group(0).rstrip(".,;:)]}\"'")
        name = _IMPORT_SEP_RE.sub(" ", (s[:m.start()] + " " + s[m.end():])).strip()
        name = re.sub(r"\s+", " ", name)
        if not _valid_http_url(url):
            items.append(dict(base, status="failed", message="播放地址格式不合法"))
            continue
        if not name:
            items.append(dict(base, status="failed", message="缺少片名或番号"))
            continue

        kind = "hls" if ident.is_hls_url(url) else "page"
        code = ""
        try:
            code, _, _ = parser.extract_code(name)
        except Exception:
            code = ""
        payload: Dict[str, Any] = {"code": code or "", "title": name,
                                   "kind": "hls" if kind == "hls" else "page"}
        if kind == "hls":
            payload["play_url"] = url
        else:
            payload["page_url"] = url
        try:
            res = link(conn, payload)
        except Exception as exc:          # 单行异常不该炸整批
            items.append(dict(base, status="failed",
                              message="写入失败：%s" % str(exc)[:120]))
            continue
        if not res.get("ok"):
            items.append(dict(base, status="failed",
                              message=res.get("message") or "写入失败"))
            continue

        status = res.get("status") or ""
        sid = int(res.get("source_id") or 0)
        item = dict(base, status=status, code=code or "",
                    movie_id=int(res.get("movie_id") or 0),
                    source_id=sid,
                    site=urlsplit(url).netloc, kind=kind)
        items.append(item)

        if not sid:
            continue
        if kind == "hls":
            if status in ("created", "source_added") or res.get("play_url_updated"):
                follow.append(("check", sid))
        else:
            row = query_one(conn, "SELECT play_url FROM movie_sources WHERE id = ?", (sid,))
            if not str((row or {}).get("play_url") or ""):
                # 只有页面地址：提交后回源站提取播放地址（refresh_from）
                follow.append(("refresh", sid))
        if status == "created" and scrape:
            follow.append(("scrape", int(res.get("movie_id") or 0)))

    counts = {"created": 0, "source_added": 0, "duplicate": 0, "failed": 0}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    return {"ok": True, "total": total, "items": items,
            "summary": counts, "follow": follow}


# ----------------------------------------------------------------- 观看事件

def sync(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    """浏览器插件自动续期：只更新**已存在**来源的播放地址，绝不新建影片/来源。

    与 link 的区别：link 找不到会建片/建来源（用户主动加入时才合适）；
    sync 找不到来源直接返回 not_found，保证自动流程零副作用。带时间戳签名的
    站点（jable 等）地址几小时就过期，插件在页面里捕获到新签名后调这个端点。
    """
    if not enabled():
        return {"ok": False, "message": "浏览器接入已关闭"}
    ev = ident.build_evidence(payload or {})
    canonical = str(ev["url_canonical"] or "")
    page = str(ev["page_url"] or "")
    new_play = str(ev["play_url"] or "")
    if not canonical and not page:
        return {"ok": False, "message": "缺少页面地址"}
    if not new_play:
        return {"ok": False, "message": "缺少播放地址"}

    # 优先按规范 URL 精确命中；页面地址可能带/不带尾斜杠，逐个变体兜底
    src = None
    if canonical:
        src = query_one(conn, "SELECT id, movie_id, play_url FROM movie_sources "
                        "WHERE url_canonical = ? LIMIT 1", (canonical,))
    if not src:
        keys = []
        for k in (page, canonical, page.rstrip("/"), canonical.rstrip("/")):
            if k and k not in keys:
                keys.append(k)
        for k in keys:
            src = query_one(
                conn, "SELECT id, movie_id, play_url FROM movie_sources "
                      "WHERE page_url = ? OR refresh_from = ? "
                      "ORDER BY is_primary DESC, id ASC LIMIT 1", (k, k))
            if src:
                break
    if not src:
        return {"ok": True, "status": "not_found", "source_id": 0}

    sid = int(src["id"])
    movie_id = int(src["movie_id"])
    if new_play == str(src["play_url"] or ""):
        return {"ok": True, "status": "unchanged", "source_id": sid, "movie_id": movie_id}

    # 自动同步的护栏：当前地址仍有效且未过期时不覆盖。
    # 页面上可能有预览位 / 推荐位在播放，插件捕获到的「不同地址」未必是主片——
    # 自动流程只做续期（旧地址失效才覆盖）；内容对不对由用户在面板里人工确认
    # （手动更新走 link / 带 force 的 sync）。
    row = query_one(conn, "SELECT status, expires_at, available FROM movie_sources WHERE id = ?",
                    (sid,))
    from . import stream_verify   # 延迟导入：避免 ingest → stream_verify 循环
    force = bool((payload or {}).get("force"))
    # 只有「明确失效 / 已过期」才允许自动覆盖；valid 与 checking（正在验证）
    # 都不动 —— 自动流程只做续期，防串台把错误地址写进仍可用的来源。
    bad = str(row["status"] or "") in ("stale", "invalid", "unsupported", "disabled")
    if not force and not bad and not stream_verify.expired(row["expires_at"]):
        return {"ok": True, "status": "skipped", "source_id": sid, "movie_id": movie_id,
                "reason": "当前地址仍有效（未过期），自动同步不覆盖；如内容不对请在插件面板手动更新"}

    expires_at = str((payload or {}).get("expires_at") or "").strip()
    refresh_from = page or canonical
    conn.execute(
        "UPDATE movie_sources SET play_url = ?, kind = ?, page_url = ?, "
        "available = 1, status = 'valid', check_note = '', last_checked = '', "
        "fetched_at = ?, expires_at = ?, refresh_from = ?, "
        "updated_at = datetime('now','localtime') WHERE id = ?",
        (new_play, ev["kind"], page or canonical, _now(), expires_at, refresh_from, sid),
    )
    return {"ok": True, "status": "updated", "source_id": sid,
            "movie_id": movie_id, "play_url_updated": True}


def _reuse_or_start_session(conn, movie_id, position, method,
                            source_ref, device_id, dedupe_key):
    """按 dedupe_key 复用场次；没有才新建，返回 (session_id, reused)。

    离线补报会把同一场次的 start / heartbeat 重放多次，这里必须幂等，
    否则 watch_sessions.dedupe_key 的唯一约束会把上报接口打成 500。
    """
    if dedupe_key:
        old = query_one(conn, "SELECT id FROM watch_sessions WHERE dedupe_key = ?",
                        (dedupe_key,))
        if old:
            return int(old["id"]), True
    sid = int(store.start_session(conn, movie_id, position, method))
    if dedupe_key:
        cur = conn.execute(
            "UPDATE OR IGNORE watch_sessions SET source_ref = ?, device_id = ?, "
            "dedupe_key = ? WHERE id = ?",
            (source_ref, device_id, dedupe_key, sid))
        if cur.rowcount == 0:
            # 并发下另一个请求先写入了同一个 key：复用它，丢掉刚建的空白场次
            other = query_one(conn, "SELECT id FROM watch_sessions WHERE dedupe_key = ?",
                              (dedupe_key,))
            if other and int(other["id"]) != sid:
                conn.execute("DELETE FROM watch_sessions WHERE id = ?", (sid,))
                return int(other["id"]), True
    else:
        conn.execute(
            "UPDATE watch_sessions SET source_ref = ?, device_id = ? WHERE id = ?",
            (source_ref, device_id, sid))
    return sid, False


def handle_event(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    """把插件的观看事件写进既有 watch_sessions / watch_progress。

    永远返回业务结果（不因重复上报抛错）：dedupe_key 相同则复用同一场次。
    """
    if not enabled():
        return {"ok": False, "message": "浏览器接入已关闭"}
    p = payload or {}
    event = str(p.get("event") or "").strip().lower()
    if event not in VALID_EVENTS:
        return {"ok": False, "message": "未知事件类型"}

    movie_id = _i(p.get("movie_id"), 0)
    movie_payload = dict(p.get("movie") or {})
    if not movie_id:
        if not movie_payload:
            return {"ok": False, "message": "缺少 movie_id 或 movie 证据"}
        linked = link(conn, movie_payload)
        if not linked.get("ok"):
            return {"ok": False, "message": linked.get("message") or "影片匹配失败"}
        movie_id = int(linked["movie_id"])
    if not query_one(conn, "SELECT id FROM movies WHERE id = ?", (movie_id,)):
        return {"ok": False, "message": "影片不存在"}

    player = dict(p.get("player") or {})
    position = _f(player.get("position"))
    duration = _f(player.get("duration"))
    watched_sec = _f(player.get("watched_sec"))
    segments = player.get("segments") or []
    method = str(cfg().get("watch_method") or "extension")
    source_ref = str(p.get("source_ref") or movie_payload.get("site") or "").strip()
    device_id = str(p.get("device_id") or "").strip()
    dedupe_key = str(p.get("dedupe_key") or "").strip()

    if event == "start":
        sid, reused = _reuse_or_start_session(
            conn, movie_id, position, method, source_ref, device_id, dedupe_key)
        return {"ok": True, "event": event, "movie_id": movie_id,
                "session_id": sid, "reused": reused}

    # 补报 / 重试时 session_id 可能还是 0（start 那条当时失败了），
    # dedupe_key 才是场次的稳定身份：优先按它定位，避免重复建场次或撞唯一约束。
    sid = 0
    if dedupe_key:
        known = query_one(conn, "SELECT id FROM watch_sessions WHERE dedupe_key = ?",
                          (dedupe_key,))
        if known:
            sid = int(known["id"])
    if not sid:
        sid = _i(p.get("session_id"), 0)
    if not sid:
        # 心跳先于 start 到达（断线补报）时补建场次，保证时长不丢
        sid, _ = _reuse_or_start_session(
            conn, movie_id, position, method, source_ref, device_id, dedupe_key)
    if event in ("heartbeat", "pause"):
        store.update_session(conn, sid, watched_sec, segments)
    else:
        finished = 1 if (duration and position >= duration * 0.95) else 0
        store.end_session(conn, sid, position, watched_sec, finished, segments)
    if duration or position:
        store.set_watch_progress(conn, movie_id, position, duration)
    return {"ok": True, "event": event, "movie_id": movie_id, "session_id": int(sid)}


# ----------------------------------------------------------------- 来源管理

def add_source(conn: sqlite3.Connection, movie_id: int,
               payload: Dict[str, Any]) -> Dict[str, Any]:
    """手动登记来源：只挂到已存在的影片下，不改 ``movies.key`` 与归并键。"""
    if not query_one(conn, "SELECT id FROM movies WHERE id = ?", (int(movie_id),)):
        return {"ok": False, "message": "影片不存在"}
    ev = ident.build_evidence(payload or {})
    if not ev["url_canonical"] and not ev["play_url"]:
        return {"ok": False, "message": "请填写页面地址或播放地址"}
    if ev["url_canonical"]:
        dup = query_one(conn, "SELECT id FROM movie_sources WHERE url_canonical = ?",
                        (ev["url_canonical"],))
        if dup:
            return {"ok": False, "message": "该地址已登记为来源"}
    has_primary = query_one(
        conn, "SELECT id FROM movie_sources WHERE movie_id = ? AND is_primary = 1",
        (int(movie_id),))
    primary_playable = query_one(
        conn, "SELECT id FROM movie_sources WHERE movie_id = ? AND is_primary = 1 "
              "AND available = 1 AND play_url <> '' "
              "AND status NOT IN ('invalid','unsupported')", (int(movie_id),))
    # 现有主源没有可播地址时，把这条新登记的地址直接扶正，
    # 避免「手动补了 m3u8 却仍然播不了」。
    make_primary = (not has_primary) or (bool(ev["play_url"]) and not primary_playable)
    if make_primary:
        conn.execute("UPDATE movie_sources SET is_primary = 0 WHERE movie_id = ?",
                     (int(movie_id),))
    cur = conn.execute(
        "INSERT INTO movie_sources(movie_id, kind, site, site_id, page_url, "
        "url_canonical, play_url, title, is_primary, priority, available, status, "
        "confidence, evidence, fetched_at, expires_at, refresh_from) "
        "VALUES(?,?,?,?,?,?,?,?,?,0,1,'valid',?,'manual',?,?,?)",
        (int(movie_id), ev["kind"], ev["site"], ev["site_id"], ev["page_url"],
         ev["url_canonical"], ev["play_url"], ev["title"],
         1 if make_primary else 0, ev["confidence"], _now(),
         str((payload or {}).get("expires_at") or "").strip(),
         ev["page_url"] or ev["url_canonical"]),
    )
    source_id = int(cur.lastrowid)
    _ensure_virtual_file(conn, movie_id, ev)
    store.refresh_aggregate(conn, movie_id)
    return {"ok": True, "movie_id": int(movie_id), "source_id": source_id,
            "sources": list_sources(conn, movie_id)}


def set_primary(conn: sqlite3.Connection, source_id: int) -> Dict[str, Any]:
    """设为主源并记为「用户固定」，自动降级的临时切换之后会优先切回它。"""
    row = query_one(conn, "SELECT id, movie_id FROM movie_sources WHERE id = ?",
                    (int(source_id),))
    if not row:
        return {"ok": False, "message": "来源不存在"}
    movie_id = int(row["movie_id"])
    conn.execute("UPDATE movie_sources SET is_primary = 0, pinned = 0 WHERE movie_id = ?",
                 (movie_id,))
    conn.execute(
        "UPDATE movie_sources SET is_primary = 1, pinned = 1, available = 1, "
        "status = 'valid', check_note = '' WHERE id = ?", (int(source_id),))
    _ensure_virtual_file(conn, movie_id)
    return {"ok": True, "movie_id": movie_id, "source_id": int(source_id),
            "sources": list_sources(conn, movie_id)}


def set_source_enabled(conn: sqlite3.Connection, source_id: int,
                       enabled: bool) -> Dict[str, Any]:
    """停用 / 启用来源；停用主源时自动把播放切到下一个可用来源。"""
    from . import stream_verify
    row = query_one(conn, "SELECT * FROM movie_sources WHERE id = ?", (int(source_id),))
    if not row:
        return {"ok": False, "message": "来源不存在"}
    movie_id = int(row["movie_id"])
    if enabled:
        conn.execute(
            "UPDATE movie_sources SET available = 1, status = 'valid', check_note = '' "
            "WHERE id = ?", (int(source_id),))
        has_primary = query_one(
            conn, "SELECT id FROM movie_sources WHERE movie_id = ? AND is_primary = 1 "
                  "AND available = 1", (movie_id,))
        if not has_primary:
            conn.execute("UPDATE movie_sources SET is_primary = 0 WHERE movie_id = ?", (movie_id,))
            conn.execute("UPDATE movie_sources SET is_primary = 1 WHERE id = ?", (int(source_id),))
        _ensure_virtual_file(conn, movie_id)
    else:
        conn.execute(
            "UPDATE movie_sources SET available = 0, status = 'disabled', "
            "check_note = '已停用' WHERE id = ?", (int(source_id),))
        if row["is_primary"]:
            conn.execute("UPDATE movie_sources SET is_primary = 0 WHERE movie_id = ?", (movie_id,))
            nxt = query_one(
                conn, "SELECT id FROM movie_sources WHERE movie_id = ? AND available = 1 "
                      "AND play_url <> '' AND status NOT IN ('invalid','unsupported') "
                      "ORDER BY " + stream_verify.source_order() + " LIMIT 1", (movie_id,))
            if nxt:
                conn.execute("UPDATE movie_sources SET is_primary = 1 WHERE id = ?",
                             (int(nxt["id"]),))
                _ensure_virtual_file(conn, movie_id)
            else:
                conn.execute(
                    "UPDATE movie_files SET strm_url = '' WHERE path = ? AND kind = ?",
                    (ident.virtual_path(movie_id), ONLINE_KIND))
    return {"ok": True, "movie_id": movie_id, "source_id": int(source_id),
            "sources": list_sources(conn, movie_id)}


def delete_source(conn: sqlite3.Connection, source_id: int) -> Dict[str, Any]:
    """删除来源（不删影片、不动观看记录）；若删的是主源则自动另选一个。"""
    row = query_one(conn, "SELECT * FROM movie_sources WHERE id = ?", (int(source_id),))
    if not row:
        return {"ok": False, "message": "来源不存在"}
    movie_id = int(row["movie_id"])
    conn.execute("DELETE FROM movie_sources WHERE id = ?", (int(source_id),))
    if row["is_primary"]:
        from . import stream_verify
        nxt = query_one(conn, "SELECT id FROM movie_sources WHERE movie_id = ? "
                        "AND available = 1 AND play_url <> '' "
                        "AND status NOT IN ('invalid','unsupported') "
                        "ORDER BY pinned DESC, " + stream_verify.source_order()
                        + " LIMIT 1", (movie_id,))
        if nxt:
            conn.execute("UPDATE movie_sources SET is_primary = 1 WHERE id = ?",
                         (int(nxt["id"]),))
    rest = query_one(conn, "SELECT id FROM movie_sources WHERE movie_id = ? "
                     "AND available = 1 AND play_url <> '' LIMIT 1", (movie_id,))
    if rest:
        _ensure_virtual_file(conn, movie_id)
    else:
        # 最后一个在线来源被移除：清掉虚拟记录，避免留下永远播不了的「可播」影片
        conn.execute("DELETE FROM movie_files WHERE path = ? AND kind = ?",
                     (ident.virtual_path(movie_id), ONLINE_KIND))
    store.refresh_aggregate(conn, movie_id)
    return {"ok": True, "movie_id": movie_id, "sources": list_sources(conn, movie_id)}


def list_all_sources(conn: sqlite3.Connection, *, status: str = "", q: str = "",
                     page: int = 1, size: int = 50) -> Dict[str, Any]:
    """全库来源列表（维护 → 来源体检）。

    筛选与分页都在 SQL 里完成，保证 total / summary 与列表一致；
    ``needs_site`` / ``expired`` 是派生视图，不落库，避免状态双写不一致。
    排序把「有问题」的排在前面：需回源站 > 失效 > 待验证 > 检测中 > 停用 > 其余。
    """
    from . import stream_verify

    page = max(1, int(page or 1))
    size = min(200, max(1, int(size or 50)))
    where: List[str] = []
    args: List[Any] = []
    st = str(status or "").strip().lower()
    if st == "needs_site":
        where.append("ms.check_note LIKE '%回源站观看%'")
    elif st == "expired":
        where.append("ms.expires_at <> '' AND datetime(ms.expires_at) < "
                     "datetime('now','localtime')")
    elif st in ("valid", "stale", "invalid", "unsupported", "checking", "disabled"):
        where.append("ms.status = ?")
        args.append(st)
    if str(q or "").strip():
        like = "%" + str(q).strip() + "%"
        where.append("(m.code LIKE ? OR m.title LIKE ? OR ms.site LIKE ?)")
        args.extend([like, like, like])
    wsql = (" WHERE " + " AND ".join(where)) if where else ""

    total = int(conn.execute(
        "SELECT COUNT(*) FROM movie_sources ms JOIN movies m ON m.id = ms.movie_id" + wsql,
        args).fetchone()[0])
    rows = query_all(conn, """
        SELECT ms.id, ms.movie_id, ms.kind, ms.site, ms.site_id, ms.status,
               ms.available, ms.is_primary, ms.check_note, ms.last_checked,
               ms.expires_at, ms.page_url, ms.play_url,
               ms.media_width, ms.media_height, ms.media_source,
               ms.media_probe_note, ms.sub_zh, ms.sub_evidence, ms.sub_langs,
               m.code, m.title AS movie_title
        FROM movie_sources ms JOIN movies m ON m.id = ms.movie_id
        {wsql}
        ORDER BY CASE
                   WHEN ms.check_note LIKE '%回源站观看%' THEN 0
                   WHEN ms.status = 'invalid' THEN 1
                   WHEN ms.status = 'stale' THEN 2
                   WHEN ms.status = 'checking' THEN 3
                   WHEN ms.status = 'disabled' THEN 4
                   WHEN ms.status = 'unsupported' THEN 5
                   ELSE 6 END,
                 COALESCE(ms.last_checked, '') DESC, ms.id DESC
        LIMIT ? OFFSET ?
    """.format(wsql=wsql), args + [size, (page - 1) * size])

    items = []
    for r in rows:
        d = dict(r)
        d["needs_site"] = "回源站观看" in str(d.get("check_note") or "")
        d["expired"] = stream_verify.expired(d.get("expires_at"))
        d["open_url"] = ident.canonical_url(str(d.get("page_url") or ""))
        d["page_url"] = ident.mask_url(str(d.get("page_url") or ""))
        d["play_url"] = ident.mask_url(str(d.get("play_url") or ""))
        d["has_page"] = bool(d["open_url"])
        d["playable"] = bool(
            str(d.get("status") or "") == "valid"
            and int(d.get("available") or 0) and d["play_url"])
        d["page_label"] = str(d["page_url"]).replace("https://", "").replace("http://", "")
        d["page_path"] = urlsplit(str(d["page_url"])).path or "/"
        items.append(d)

    srow = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status = 'valid' AND available = 1 THEN 1 ELSE 0 END) AS valid,
               SUM(CASE WHEN status = 'stale' THEN 1 ELSE 0 END) AS stale,
               SUM(CASE WHEN status = 'invalid' THEN 1 ELSE 0 END) AS invalid,
               SUM(CASE WHEN status = 'unsupported' THEN 1 ELSE 0 END) AS unsupported,
               SUM(CASE WHEN status = 'checking' THEN 1 ELSE 0 END) AS checking,
               SUM(CASE WHEN status = 'disabled' THEN 1 ELSE 0 END) AS disabled,
               SUM(CASE WHEN check_note LIKE '%回源站观看%' THEN 1 ELSE 0 END) AS needs_site,
               SUM(CASE WHEN expires_at <> ''
                         AND datetime(expires_at) < datetime('now','localtime')
                        THEN 1 ELSE 0 END) AS expired
        FROM movie_sources
    """).fetchone()
    summary = {k: int(v or 0) for k, v in dict(srow).items()}
    return {"items": items, "total": total, "page": page, "size": size,
            "summary": summary}
