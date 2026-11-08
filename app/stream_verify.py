# -*- coding: utf-8 -*-
"""在线来源的可用性验证、过期续期与失效降级（浏览器接入二期）。

三级验证（见 docs/浏览器插件与在线资源设计.md 第 13.5 节）：

1. 入库轻验证：``link`` 成功后异步 ``submit_task`` 探测一次，不阻塞插件上报；
2. 播放前：``stream-info`` 调 ``ensure_fresh()``，过期提交续期、失效自动降级；
3. 播放中：主播放列表 / 分片遇到 403/410/HTML 时标记来源并自动切换一次。

安全边界：只 http/https、解析后拒绝内网地址、不跟随任意重定向、限超时与体积、
默认校验证书。验证失败只标记状态，**不删除来源、不删除影片**。
对外返回的文案只含状态码 / 原因，绝不回显带签名的完整播放地址。
"""
from __future__ import annotations

import html
import ipaddress
import re
import socket
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urljoin, urlsplit

from . import movie_identity as ident
from .config import load_config
from .db import db, query_one
from .threadpool import submit_task

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_MAX_BODY = 512 * 1024
_MAX_REDIRECTS = 2
#: 页面里可重新提取的播放地址（m3u8 / mp4），不加载任何远程脚本
_CAND_RE = re.compile(
    r"(?:https?://|//|/)?[A-Za-z0-9_.~%/+:\-]+?\.(?:m3u8|mp4)"
    r"(?:\?[A-Za-z0-9_=%&.~+/:;,\-]*)?", re.I)

_PENDING_LOCK = threading.Lock()
_PENDING: set[tuple] = set()
_REFRESH_LOCK = threading.Lock()
_LAST_REFRESH: Dict[int, float] = {}


def cfg() -> Dict[str, Any]:
    return dict(load_config().get("ingest") or {})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def expired(value: Any) -> bool:
    """expires_at 兼容 ISO 与 unix 秒；空值表示长期有效，不算过期。"""
    if not value:
        return False
    try:
        return datetime.fromisoformat(str(value)) < datetime.now()
    except (ValueError, TypeError):
        pass
    try:
        return float(value) < time.time()
    except (ValueError, TypeError):
        return False


def _url_problem(url: str) -> tuple:
    """SSRF 边界检查，返回 (问题类别, 中文说明)。

    ``''`` 允许；``blocked`` 内网 / 非法地址（永久拒绝）；``dns`` 域名暂时
    无法解析（按临时故障处理，只标 stale，不把来源判死）。
    """
    try:
        p = urlsplit(str(url or "").strip())
    except ValueError:
        return "blocked", "地址不合法"
    if (p.scheme not in ("http", "https") or not p.hostname
            or p.username or p.password):
        return "blocked", "地址不合法或指向内网"
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
        infos = socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, ValueError, OSError):
        return "dns", "域名暂时无法解析，已跳过验证"
    if not infos:
        return "dns", "域名暂时无法解析，已跳过验证"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return "blocked", "地址不合法"
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return "blocked", "地址指向内网，已拒绝"
    return "", ""


def _safe_url(url: str) -> bool:
    return not _url_problem(url)[0]


_PAGE_LOCK = threading.Lock()
_PAGE_CACHE: Dict[tuple, tuple] = {}
#: 自定义请求头黑名单：不携带站点登录态 / 凭据，也不允许改写底层传输头
_HEADER_BLOCKLIST = {
    "cookie", "set-cookie", "authorization", "proxy-authorization",
    "host", "content-length", "connection", "transfer-encoding",
}
#: 由本模块按来源页面推导的头，不允许自定义头覆盖（改 Referer 请用 strm_referer）
_MANAGED_HEADERS = {
    "referer", "origin", "user-agent", "accept", "accept-language",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
}


def _site_origin(u: str) -> str:
    try:
        p = urlsplit(str(u or ""))
        if p.scheme in ("http", "https") and p.netloc:
            return "%s://%s/" % (p.scheme, p.netloc)
    except ValueError:
        pass
    return ""


def source_page(movie_id: int = 0, source_id: int = 0) -> str:
    """来源对应的影片页面完整地址（用于构造 Referer / Origin）。

    HLS 每个分片都要构造请求头，这里做 60 秒内存缓存，避免逐片查库。
    """
    key = (int(movie_id or 0), int(source_id or 0))
    now = time.time()
    with _PAGE_LOCK:
        hit = _PAGE_CACHE.get(key)
        if hit and now - hit[0] < 60:
            return hit[1]
    with db() as conn:
        if key[1]:
            row = query_one(conn, "SELECT page_url FROM movie_sources WHERE id = ?",
                            (key[1],))
        else:
            row = query_one(
                conn,
                "SELECT page_url FROM movie_sources WHERE movie_id = ? "
                "AND page_url <> '' ORDER BY is_primary DESC, id ASC LIMIT 1",
                (key[0],))
    value = str((row or {}).get("page_url") or "")
    with _PAGE_LOCK:
        _PAGE_CACHE[key] = (now, value)
    return value


def source_referer(movie_id: int = 0, source_id: int = 0) -> str:
    """兼容旧调用：返回影片页面站点的 origin。"""
    return _site_origin(source_page(movie_id, source_id))


def upstream_proxies(url: str = "") -> Optional[Dict[str, str]]:
    """上游请求代理：``stream.upstream_proxy`` 覆盖 > 全局 ``network.proxy``。

    播放列表、分片与来源验证共用同一套解析，保证「验证能过、播不了」不再发生；
    本机 / 内网地址由 ``proxy.proxies_for`` 强制直连。
    """
    from .proxy import proxies_for
    override = str((load_config().get("stream") or {}).get("upstream_proxy") or "").strip()
    return proxies_for(url, override=override)


def upstream_headers(target: str, movie_id: int = 0,
                     source_id: int = 0) -> Dict[str, str]:
    """服务端代拉上游时的统一请求头：验证、播放列表、分片共用一套。

    尽量贴近浏览器播放时实际会带的头：Referer（默认完整页面地址，可切
    origin）、Origin、UA、Accept、Accept-Language、Sec-Fetch-*，以及设置页
    里用户自填的自定义头。凭据类头（Cookie / Authorization 等）不自动携带，
    也不允许通过自定义头注入 —— 不代抓、不带登录态、不绕过付费或 DRM。
    """
    stream = dict(load_config().get("stream") or {})
    page = source_page(movie_id, source_id)
    origin = _site_origin(page)
    explicit = str(stream.get("strm_referer") or "").strip()
    mode = str(stream.get("strm_referer_mode") or "full").lower()
    if explicit:
        referer = explicit
    elif mode == "origin":
        referer = origin
    else:
        referer = page or origin
    if not referer:
        referer = _site_origin(target)

    h = {
        "User-Agent": str(stream.get("strm_user_agent") or _UA),
        "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        h["Referer"] = referer
    if origin:
        h["Origin"] = origin.rstrip("/")
        h["Sec-Fetch-Dest"] = "empty"
        h["Sec-Fetch-Mode"] = "cors"
        h["Sec-Fetch-Site"] = "cross-site"

    extra = stream.get("strm_extra_headers")
    if isinstance(extra, dict):
        for k, v in extra.items():
            key = str(k).strip()
            low = key.lower()
            if (not key or v is None or low in _HEADER_BLOCKLIST
                    or low in _MANAGED_HEADERS):
                continue
            h[key] = str(v)
    return h


def _fetch(url: str, *, limit: int = _MAX_BODY, partial: bool = False,
           headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """限量 GET：不跟随任意重定向，每跳重新做 SSRF 校验，只读前 limit 字节。"""
    import requests
    problem, note = _url_problem(url)
    if problem:
        return {"ok": False, "status": 0, "url": url, "text": "", "headers": {},
                "note": note}
    insecure = bool((load_config().get("server") or {}).get("insecure_skip_verify", False))
    req_headers = dict(headers) if headers else upstream_headers(url)
    if partial:
        req_headers["Range"] = "bytes=0-1023"
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        try:
            r = requests.get(current, headers=req_headers, timeout=(8, 20), stream=True,
                             allow_redirects=False, verify=not insecure,
                             proxies=upstream_proxies(current))
        except Exception as exc:
            return {"ok": False, "status": 0, "url": current, "text": "", "headers": {},
                    "note": "上游不可达（%s）" % type(exc).__name__}
        if r.status_code in (301, 302, 303, 307, 308):
            loc = urljoin(current, r.headers.get("Location") or "")
            r.close()
            if not loc or not _safe_url(loc):
                return {"ok": False, "status": 0, "url": current, "text": "", "headers": {},
                        "note": "重定向被拒绝"}
            current = loc
            continue
        status = r.status_code
        hdrs = {str(k).lower(): str(v) for k, v in r.headers.items()}
        buf = b""
        try:
            for chunk in r.iter_content(64 * 1024):
                buf += chunk
                if len(buf) >= limit:
                    break
        except Exception as exc:
            return {"ok": False, "status": status, "url": current, "text": "",
                    "headers": hdrs, "note": "读取响应失败（%s）" % type(exc).__name__}
        finally:
            r.close()
        return {"ok": status < 400, "status": status, "url": current,
                "text": buf.decode("utf-8", "ignore"), "headers": hdrs, "note": ""}
    return {"ok": False, "status": 0, "url": current, "text": "", "headers": {},
            "note": "重定向次数过多"}


def _expiry(url: str, fetched: datetime) -> str:
    """从签名参数推算有效期；算不出就用保守 TTL，让播放前能触发续期。"""
    try:
        q = dict(parse_qsl(urlsplit(url).query, keep_blank_values=False))
    except ValueError:
        q = {}
    ttl = q.get("X-Amz-Expires") or q.get("x-amz-expires")
    if ttl and str(ttl).isdigit():
        return _iso(fetched + timedelta(seconds=int(ttl)))
    for key in ("expires", "expire", "exp", "e"):
        v = q.get(key)
        if v and str(v).isdigit():
            n = int(v)
            if n > 10 ** 12:
                n //= 1000
            if n > fetched.timestamp():
                return _iso(datetime.fromtimestamp(n))
    try:
        hours = max(1.0, float(cfg().get("stale_ttl_hours") or 6))
    except (TypeError, ValueError):
        hours = 6.0
    return _iso(fetched + timedelta(hours=hours))


_BLOCK_MARKERS = ("you have been blocked", "attention required",
                  "cf-challenge", "just a moment", "access denied")


def _block_note(res: Dict[str, Any]) -> str:
    """把 403 细化：Cloudflare 硬阻断 / 人机校验 / 普通拒绝。

    「Sorry, you have been blocked」是 Cloudflare 的硬阻断（1020/1006），
    对浏览器同样拒绝——补请求头没用，只能换出口 IP 或回源站。
    """
    hdrs = res.get("headers") or {}
    body = str(res.get("text") or "")[:2000].lower()
    cf = ("cloudflare" in str(hdrs.get("server") or "").lower()
          or bool(hdrs.get("cf-ray")))
    if cf and "you have been blocked" in body:
        return "Cloudflare 硬阻断（该出口 IP 被拒，换节点或回源站）"
    if cf and any(m in body for m in _BLOCK_MARKERS):
        return "Cloudflare 人机校验/拦截（换节点或回源站）"
    return "上游返回 HTTP %d" % int(res.get("status") or 0)


def probe(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """探测一个播放地址：返回 status / note / hls / expires_at，不碰数据库。

    HLS 校验首行 #EXTM3U、识别 master / media、抽查 1~2 个分片；
    直链用 Range 取前 1KB。403/401/410/429 与返回网页只算「待验证」：
    多为防盗链头缺失或签名过期，播放器带对的头往往还能播，不能判死来源。
    """
    problem, note = _url_problem(url)
    if problem:
        return {"status": "invalid" if problem == "blocked" else "stale",
                "note": note, "hls": False}
    hls = ident.is_hls_url(url)
    res = _fetch(url, limit=_MAX_BODY, partial=not hls, headers=headers)
    if not res["ok"] or res["status"] >= 400:
        note = res["note"] or _block_note(res)
        soft = res["status"] in (0, 401, 403, 410, 429)
        return {"status": "stale" if soft else "invalid", "note": note, "hls": hls}
    ctype = str(res["headers"].get("content-type") or "").lower()
    text = res["text"]
    if "html" in ctype or text.lstrip().lower().startswith(("<!doctype html", "<html")):
        return {"status": "stale", "note": "返回网页而非媒体内容（可能需要防盗链头或登录）",
                "hls": hls}
    if text.lstrip().startswith("#EXTM3U"):
        hls = True
    if hls:
        if not text.lstrip().startswith("#EXTM3U"):
            return {"status": "invalid", "note": "播放列表格式不正确", "hls": True}
        if "SAMPLE-AES" in text.upper():
            return {"status": "unsupported", "note": "加密流（DRM / SAMPLE-AES）不支持", "hls": True}
        uris = [line.strip() for line in text.splitlines()
                if line.strip() and not line.strip().startswith("#")]
        for uri in uris[:2]:
            seg = urljoin(res["url"], uri)
            one = _fetch(seg, limit=2048, partial=True, headers=headers)
            if not one["ok"] or one["status"] >= 400:
                return {"status": "stale",
                        "note": "分片返回 HTTP %d（可能需要防盗链头）" % one["status"],
                        "hls": True}
            if "html" in str(one["headers"].get("content-type") or "").lower():
                return {"status": "stale", "note": "分片返回网页（签名可能已过期）",
                        "hls": True}
    return {"status": "valid", "note": "可用", "hls": hls,
            "expires_at": _expiry(url, datetime.now())}


def _apply(conn, source_id: int, status: str, note: str, *, play_url: str = "",
           expires_at: str = "", hls: Optional[bool] = None, refetched: bool = False,
           available: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """把验证结果写回来源，并让虚拟文件记录跟随当前主源。"""
    row = query_one(conn, "SELECT * FROM movie_sources WHERE id = ?", (int(source_id),))
    if not row:
        return None
    avail = available if available is not None else (1 if status in ("valid", "stale", "checking") else 0)
    url = str(play_url or row["play_url"] or "")
    exp = str(expires_at or row["expires_at"] or "")
    fetched = _now() if refetched else str(row["fetched_at"] or "")
    kind = ("hls" if hls else "progressive") if hls is not None else str(row["kind"] or "page")
    conn.execute(
        "UPDATE movie_sources SET status = ?, available = ?, check_note = ?, last_checked = ?, "
        "play_url = ?, expires_at = ?, fetched_at = ?, kind = ?, "
        "updated_at = datetime('now','localtime') WHERE id = ?",
        (status, int(avail), str(note or ""), _now(), url, exp, fetched, kind, int(source_id)),
    )
    from . import ingest       # 延迟导入，避免 ingest → stream_verify 循环
    ingest.sync_virtual_file(conn, int(row["movie_id"]))
    return {"id": int(source_id), "movie_id": int(row["movie_id"]),
            "status": status, "available": int(avail), "note": str(note or "")}


def check_source(source_id: int) -> Dict[str, Any]:
    """验证单个来源（同步执行；HTTP 端点通过 submit_check 放进短任务池）。"""
    with db() as conn:
        row = query_one(conn, "SELECT play_url FROM movie_sources WHERE id = ?", (int(source_id),))
        if not row:
            return {"ok": False, "message": "来源不存在"}
        url = str(row["play_url"] or "")
    if not url:
        with db() as conn:
            _apply(conn, source_id, "stale", "没有播放地址，请重新获取")
        return {"ok": True, "status": "stale", "note": "没有播放地址，请重新获取"}
    res = probe(url, upstream_headers(url, source_id=source_id))
    with db() as conn:
        out = _apply(conn, source_id, res["status"], res["note"],
                     expires_at=res.get("expires_at", ""), hls=res.get("hls"))
    return {"ok": True, "status": res["status"], "note": res["note"], **(out or {})}


def _page_candidates(page_url: str) -> List[str]:
    """从源站页面里保守地提取播放地址（不执行页面脚本、不拉远程代码）。"""
    if not _safe_url(page_url):
        return []
    res = _fetch(page_url, limit=_MAX_BODY)
    if not res["ok"] or res["status"] >= 400:
        return []
    text = (html.unescape(res["text"]).replace("\\/", "/")
            .replace("\\u002F", "/").replace("\\u002f", "/"))
    found: List[str] = []
    for m in _CAND_RE.finditer(text):
        cand = urljoin(res["url"], m.group(0))
        if not _safe_url(cand) or cand in found:
            continue
        found.append(cand)
        if len(found) >= 4:
            break
    found.sort(key=lambda u: 0 if ".m3u8" in u.lower() else 1)
    return found


def refresh_source(source_id: int, play_url: str = "", page_url: str = "") -> Dict[str, Any]:
    """续期：优先用显式新地址；否则先确认旧地址是否仍有效，再回源站页面重新提取。"""
    with db() as conn:
        row = query_one(conn, "SELECT * FROM movie_sources WHERE id = ?", (int(source_id),))
        if not row:
            return {"ok": False, "message": "来源不存在"}
        old = str(row["play_url"] or "")
        page = str(page_url or row["refresh_from"] or row["page_url"] or "")

    new_url = str(play_url or "").strip()
    if new_url and _url_problem(new_url)[0]:
        # 显式填写的地址被拒时要说清原因，不能悄悄退回旧地址让用户以为续期成功
        reason = _url_problem(new_url)[1]
        with db() as conn:
            _apply(conn, source_id, "invalid", reason)
        return {"ok": False, "message": reason}
    note = "已用新播放地址续期" if new_url else ""

    if not new_url and old:
        res = probe(old, upstream_headers(old, source_id=source_id))
        if res["status"] in ("valid", "stale"):
            # 403 等只是「需要防盗链头」，不是死链：保留地址、标待验证，
            # 别把播放器还能用的来源判成失效。
            soft = res["status"] == "stale"
            msg = "上游需要防盗链头，播放时重试" if soft else "地址仍可用，有效期已续"
            with db() as conn:
                out = _apply(conn, source_id, res["status"], msg,
                             expires_at=res.get("expires_at", ""), hls=res.get("hls"),
                             refetched=True, available=1)
            return {"ok": True, "refreshed": True, "note": msg, **(out or {})}
        note = res["note"] or ""

    candidates = [new_url] if new_url else _page_candidates(page)
    for cand in candidates:
        res = probe(cand, upstream_headers(cand, source_id=source_id))
        if res["status"] not in ("valid", "stale"):
            note = res["note"] or note
            continue
        with db() as conn:
            out = _apply(conn, source_id, "valid", note or "已从源站页面重新获取播放地址",
                         play_url=cand, expires_at=res.get("expires_at", ""),
                         hls=res.get("hls"), refetched=True, available=1)
        return {"ok": True, "refreshed": True, "note": note or "已重新获取播放地址", **(out or {})}

    msg = note or "未能重新获取播放地址，请手动填写新地址或配置 Referer"
    with db() as conn:
        _apply(conn, source_id, "stale", msg)
    return {"ok": False, "message": msg}


def _submit(kind: str, source_id: int, fn) -> Dict[str, Any]:
    """标 checking 后放进短任务池；同一来源的同类任务不重复排队。"""
    key = (kind, int(source_id))
    with _PENDING_LOCK:
        if key in _PENDING:
            return {"ok": True, "status": "checking", "queued": False}
        _PENDING.add(key)
    with db() as conn:
        row = query_one(conn, "SELECT id FROM movie_sources WHERE id = ?", (int(source_id),))
        if not row:
            with _PENDING_LOCK:
                _PENDING.discard(key)
            return {"ok": False, "message": "来源不存在"}
        conn.execute("UPDATE movie_sources SET status = 'checking', check_note = ? WHERE id = ?",
                     ("正在验证" if kind == "check" else "正在重新获取", int(source_id)))

    def run():
        try:
            fn()
        except Exception:
            with db() as conn:
                _apply(conn, source_id, "stale", "任务异常，请重试")
        finally:
            with _PENDING_LOCK:
                _PENDING.discard(key)

    submit_task(run)
    return {"ok": True, "status": "checking", "queued": True}


def submit_check(source_id: int) -> Dict[str, Any]:
    return _submit("check", source_id, lambda: check_source(source_id))


def submit_refresh(source_id: int, play_url: str = "", page_url: str = "") -> Dict[str, Any]:
    return _submit("refresh", source_id,
                   lambda: refresh_source(source_id, play_url, page_url))


def _allow_refresh(source_id: int) -> bool:
    """播放前的自动续期节流：同一来源 60 秒内最多触发一次。"""
    now = time.time()
    with _REFRESH_LOCK:
        if now - _LAST_REFRESH.get(int(source_id), 0) < 60:
            return False
        _LAST_REFRESH[int(source_id)] = now
    return True


def ensure_fresh(conn, movie_id: int) -> Dict[str, Any]:
    """播放前检查（在调用方持有的连接里执行）：恢复固定主源、过期续期、失效降级。"""
    primary = query_one(
        conn,
        "SELECT * FROM movie_sources WHERE movie_id = ? AND is_primary = 1 "
        "AND available = 1 AND play_url <> '' LIMIT 1", (int(movie_id),))
    if not primary:
        # 上次自动验证遇到 403 把主源判死时，别让整部片变成不可播：
        # 恢复成「待验证」，让播放代理用补好的防盗链头重试一次。
        # 之前的失败上报可能把主源标志也清掉了，所以不要求 is_primary=1，
        # 恢复时顺手把它扶正，保证后续验证 / 续期有明确对象。
        stuck = query_one(
            conn,
            "SELECT * FROM movie_sources WHERE movie_id = ? AND play_url <> '' "
            "AND status IN ('invalid','stale','checking') "
            "ORDER BY is_primary DESC, id ASC LIMIT 1",
            (int(movie_id),))
        if stuck:
            conn.execute("UPDATE movie_sources SET is_primary = 0 WHERE movie_id = ?",
                         (int(movie_id),))
            conn.execute(
                "UPDATE movie_sources SET is_primary = 1, available = 1, status = 'stale', "
                "check_note = '上次验证未通过，播放时重试' WHERE id = ?",
                (int(stuck["id"]),))
            primary = dict(stuck, status="stale", available=1, is_primary=1)
        else:
            alt = query_one(
                conn,
                "SELECT * FROM movie_sources WHERE movie_id = ? AND available = 1 "
                "AND play_url <> '' AND status NOT IN ('invalid','unsupported') "
                "ORDER BY priority DESC, id ASC LIMIT 1", (int(movie_id),))
            if alt:
                conn.execute("UPDATE movie_sources SET is_primary = 0 WHERE movie_id = ?",
                             (int(movie_id),))
                conn.execute("UPDATE movie_sources SET is_primary = 1 WHERE id = ?",
                             (int(alt["id"]),))
                primary = alt
    if not primary:
        return {"url": "", "source_id": 0, "stale": False, "status": ""}

    pinned = query_one(
        conn,
        "SELECT * FROM movie_sources WHERE movie_id = ? AND pinned = 1 AND available = 1 "
        "AND status NOT IN ('invalid','unsupported','checking') AND play_url <> '' LIMIT 1",
        (int(movie_id),))
    if pinned and int(pinned["id"]) != int(primary["id"]) and not expired(pinned["expires_at"]):
        conn.execute("UPDATE movie_sources SET is_primary = 0 WHERE movie_id = ?", (int(movie_id),))
        conn.execute("UPDATE movie_sources SET is_primary = 1 WHERE id = ?", (int(pinned["id"]),))
        primary = pinned

    sid = int(primary["id"])
    stale = bool(expired(primary["expires_at"])
                 or str(primary["status"] or "") in ("stale", "invalid", "checking"))
    return {"url": str(primary["play_url"] or ""), "source_id": sid,
            "stale": stale, "status": str(primary["status"] or ""),
            "need_refresh": bool(stale and _allow_refresh(sid))}


def schedule_refresh(source_id: int) -> None:
    """调用方事务提交后再排队续期，避免短任务抢占未提交的写事务。"""
    submit_task(refresh_source, int(source_id), "", "")


def _failover(conn, failed_id: int, reason: str) -> str:
    """把失效来源标为不可用，并把下一个可用来源设为主源；返回新播放地址。"""
    row = query_one(conn, "SELECT * FROM movie_sources WHERE id = ?", (int(failed_id),))
    if not row:
        return ""
    movie_id = int(row["movie_id"])
    note = str(reason or "播放失败")[:200]
    nxt = query_one(
        conn,
        "SELECT id, play_url FROM movie_sources WHERE movie_id = ? AND id <> ? "
        "AND available = 1 AND play_url <> '' AND status NOT IN ('invalid','unsupported') "
        "ORDER BY priority DESC, id ASC LIMIT 1", (movie_id, int(failed_id)))
    if not nxt:
        # 没有备用来源时不能把主源判死、更不能清掉 is_primary：
        # 否则整部片会变成「无主源」，连重试和重新获取都找不到对象。
        conn.execute(
            "UPDATE movie_sources SET status = 'stale', available = 1, "
            "check_note = ?, last_checked = ? WHERE id = ?",
            (note + "（无备用来源，保留待重试）", _now(), int(failed_id)))
        return ""
    conn.execute(
        "UPDATE movie_sources SET is_primary = 0, available = 0, status = 'invalid', "
        "check_note = ?, last_checked = ? WHERE id = ?",
        (note, _now(), int(failed_id)))
    conn.execute("UPDATE movie_sources SET is_primary = 0 WHERE movie_id = ?", (movie_id,))
    conn.execute("UPDATE movie_sources SET is_primary = 1 WHERE id = ?", (int(nxt["id"]),))
    from . import ingest
    ingest.sync_virtual_file(conn, movie_id)
    return str(nxt["play_url"] or "")


def report_failure(movie_id: int, reason: str = "", switch: bool = True) -> Dict[str, Any]:
    """播放中失败上报：标记主源、自动降级；没有备用源时后台尝试续期。

    ``switch=False`` 用于分片级 403（多为签名过期）：只触发续期，不换来源。
    """
    with db() as conn:
        row = query_one(
            conn,
            "SELECT * FROM movie_sources WHERE movie_id = ? AND is_primary = 1 "
            "AND available = 1 AND play_url <> '' LIMIT 1", (int(movie_id),))
        if not row:
            return {"ok": False, "message": "没有可处理的在线来源"}
        sid = int(row["id"])
        new_url = ""
        if switch and bool(cfg().get("auto_failover", True)):
            new_url = _failover(conn, sid, reason)
        else:
            conn.execute(
                "UPDATE movie_sources SET status = 'stale', check_note = ?, last_checked = ? "
                "WHERE id = ?", (str(reason or "播放失败")[:200], _now(), sid))
        if not new_url and _allow_refresh(sid):
            submit_task(refresh_source, sid, "", "")
    return {"ok": True, "switched": bool(new_url), "source_id": sid,
            "message": "已切换到下一可用来源" if new_url else "已标记失效并尝试续期"}


def playback_failover(movie_id: int, target: str, reason: str) -> str:
    """服务端 HLS 代理用：主播放列表失败时降级一次，返回可重试的新地址。"""
    del target                     # 目标地址不进日志
    res = report_failure(movie_id, reason, switch=True)
    if not res.get("switched"):
        return ""
    with db() as conn:
        row = query_one(
            conn, "SELECT play_url FROM movie_sources WHERE movie_id = ? AND is_primary = 1 "
                  "AND available = 1 LIMIT 1", (int(movie_id),))
    return str((row or {}).get("play_url") or "")
