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

from concurrent.futures import ThreadPoolExecutor

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


def _source_meta(movie_id: int = 0, source_id: int = 0,
                 url: str = "") -> Dict[str, Any]:
    """来源的回源元信息：页面地址 + 播放时实际生效的请求头。

    HLS 每个分片都要构造请求头，这里做 60 秒内存缓存，避免逐片查库。
    ``play_headers`` 由无头浏览器刷新时截获（有的 CDN 校验的是播放器所在
    跳板域，而不是影片页），随来源保存后播放代理也跟着带。

    ``url`` 用于播放代理：同一部片常有 en / cn 两个页面重复上报的两条来源，
    而真正在播的是其中某一条。按播放地址精确定位到那条来源，才不会「头记在
    另一条来源上、播放还在用影片页 Referer」。本来源还没抓到头的，借用同一
    站点兄弟来源的头（同站点的防盗链策略一致）。
    """
    mid, sid, url = int(movie_id or 0), int(source_id or 0), str(url or "")
    key = (mid, sid, url if not sid else "")
    now = time.time()
    with _PAGE_LOCK:
        hit = _PAGE_CACHE.get(key)
        if hit and now - hit[0] < 60:
            return hit[1]
    row = None
    with db() as conn:
        if sid:
            row = query_one(
                conn, "SELECT movie_id, page_url, play_headers FROM movie_sources "
                "WHERE id = ?", (sid,))
            if row:
                mid = mid or int(row["movie_id"] or 0)   # 只给了来源 id 时补齐影片 id
        else:
            if url:
                row = query_one(
                    conn,
                    "SELECT page_url, play_headers FROM movie_sources "
                    "WHERE movie_id = ? AND play_url = ? ORDER BY id DESC LIMIT 1",
                    (mid, url))
            if not row and mid:
                row = query_one(
                    conn,
                    "SELECT page_url, play_headers FROM movie_sources WHERE movie_id = ? "
                    "AND page_url <> '' ORDER BY is_primary DESC, id ASC LIMIT 1",
                    (mid,))
        meta = {"page_url": str((row or {}).get("page_url") or ""),
                "play_headers": _parse_headers((row or {}).get("play_headers"))}
        if mid and not meta["play_headers"]:
            meta["play_headers"] = _sibling_headers(conn, mid, sid, meta["page_url"])
    with _PAGE_LOCK:
        _PAGE_CACHE[key] = (now, meta)
    return meta


def _sibling_headers(conn, movie_id: int, source_id: int, page_url: str) -> Dict[str, str]:
    """借用同一站点兄弟来源的播放头（en / cn 等重复上报的同片来源）。"""
    site = _site_origin(page_url)
    if not site:
        return {}
    rows = conn.execute(
        "SELECT id, page_url, play_headers FROM movie_sources "
        "WHERE movie_id = ? AND id <> ? AND play_headers <> ''",
        (int(movie_id), int(source_id))).fetchall()
    for r in rows:
        if _site_origin(r["page_url"]) == site:
            got = _parse_headers(r["play_headers"])
            if got:
                return got
    return {}


def source_page(movie_id: int = 0, source_id: int = 0) -> str:
    """来源对应的影片页面完整地址（用于构造 Referer / Origin）。"""
    return _source_meta(movie_id, source_id)["page_url"]


def _parse_headers(raw: Any) -> Dict[str, str]:
    """解析来源上保存的播放请求头（只认 Referer / Origin，其余一律丢弃）。"""
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            import json
            data = json.loads(str(raw or "") or "{}")
        except (ValueError, TypeError):
            return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    for name in ("Referer", "Origin"):
        val = str(data.get(name) or "").strip()
        if val.startswith(("http://", "https://")):
            out[name] = val
    return out


def _cache_headers(movie_id: int, source_id: int, headers: Dict[str, str]) -> None:
    """刷新成功后立刻让内存缓存生效，避免播放请求还读到旧 Referer。

    缓存按「影片 + 来源 + 播放地址」多键存放，这里直接丢掉该影片的所有条目，
    下次读取自然回到数据库（此时新头已写入）。
    """
    with _PAGE_LOCK:
        sid = int(source_id or 0)
        for key in [k for k in _PAGE_CACHE
                    if k[0] == int(movie_id or 0) or (sid and k[1] == sid)]:
            _PAGE_CACHE.pop(key, None)


def source_referer(movie_id: int = 0, source_id: int = 0) -> str:
    """兼容旧调用：返回影片页面站点的 origin。"""
    return _site_origin(source_page(movie_id, source_id))


def upstream_proxies(url: str = "") -> Optional[Dict[str, str]]:
    """上游请求代理：``stream.upstream_proxy`` 覆盖 > 全局 ``network.proxy``。

    播放列表、分片与来源验证共用同一套解析，保证「验证能过、播不了」不再发生；
    本机 / 内网地址由 ``proxy.proxies_for`` 强制直连。能直连的主机由
    ``egress`` 判出来后会直接返回 None，不再白绕一圈代理。
    """
    from . import egress
    override = str((load_config().get("stream") or {}).get("upstream_proxy") or "").strip()
    return egress.proxies_for(url, override=override)


def upstream_headers(target: str, movie_id: int = 0, source_id: int = 0,
                     extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """服务端代拉上游时的统一请求头：验证、播放列表、分片共用一套。

    尽量贴近浏览器播放时实际会带的头：Referer（默认完整页面地址，可切
    origin）、Origin、UA、Accept、Accept-Language、Sec-Fetch-*，以及设置页
    里用户自填的自定义头。凭据类头（Cookie / Authorization 等）不自动携带，
    也不允许通过自定义头注入 —— 不代抓、不带登录态、不绕过付费或 DRM。

    优先级：用户显式 ``stream.strm_referer`` > 来源上保存的播放头（无头浏览器
    截获，CDN 认的是播放器跳板域）> 影片页面地址 > CDN 自身 origin。
    ``extra`` 用于「刚截获、还没写库」的候选地址先探一次。
    """
    stream = dict(load_config().get("stream") or {})
    meta = _source_meta(movie_id, source_id, target)
    page = meta["page_url"]
    origin = _site_origin(page)
    stored = _parse_headers(extra) if extra else meta["play_headers"]
    explicit = str(stream.get("strm_referer") or "").strip()
    mode = str(stream.get("strm_referer_mode") or "full").lower()
    if explicit:
        referer = explicit
    elif stored.get("Referer"):
        referer = stored["Referer"]
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
    stored_origin = stored.get("Origin") or origin
    if stored_origin:
        h["Origin"] = stored_origin.rstrip("/")
        h["Sec-Fetch-Dest"] = "empty"
        h["Sec-Fetch-Mode"] = "cors"
        h["Sec-Fetch-Site"] = "cross-site"

    extra_cfg = stream.get("strm_extra_headers")
    if isinstance(extra_cfg, dict):
        for k, v in extra_cfg.items():
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
        # 410 Gone 是签名地址「明确过期」：日常体检仍按软错误保留来源
        # （不判死、等重试），但续期流程要靠 gone 标记去重新取地址。
        return {"status": "stale" if soft else "invalid", "note": note, "hls": hls,
                "http": int(res["status"] or 0), "gone": int(res["status"] or 0) == 410}
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
           available: Optional[int] = None,
           play_headers: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """把验证结果写回来源，并让虚拟文件记录跟随当前主源。

    ``play_headers`` 传 ``None`` 表示不动已有头；传 ``{}`` 表示清空（换了
    CDN / 换站时旧的 Referer 会让新地址 403）。只保存 Referer / Origin。
    """
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
    if play_headers is not None:
        import json
        clean = _parse_headers(play_headers)
        conn.execute("UPDATE movie_sources SET play_headers = ? WHERE id = ?",
                     (json.dumps(clean, ensure_ascii=False) if clean else "", int(source_id)))
        _cache_headers(int(row["movie_id"]), int(source_id), clean)
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
    note = res["note"]
    # 403 且本源还没有保存播放头：多半是防盗链 Referer 不对，而不是真的被墙。
    # 别把「Cloudflare 阻断该出口 IP」这种误导文案丢给用户，入库后立刻就排一次
    # 浏览器续期——等用户点播放时地址和头基本已经补齐，不必手动重试。
    repair = bool(res["status"] == "stale" and not _own_headers(source_id)
                  and can_renew(source_id) and _browser_enabled())
    if repair:
        note = "上游要求防盗链 Referer，正在后台重新获取播放地址"
    with db() as conn:
        out = _apply(conn, source_id, res["status"], note,
                     expires_at=res.get("expires_at", ""), hls=res.get("hls"))
    if repair:
        _refresh_future(source_id)
    if res["status"] == "valid":
        _kick_probe(source_id)
        _kick_egress(source_id)
    return {"ok": True, "status": res["status"], "note": note, **(out or {})}


def _kick_egress(source_id: int) -> None:
    """验证通过后顺手试一次直连（同一主机 6 小时内只试一次）。

    放在验证阶段而不是播放中：此时没有正在进行的播放，换出口不会打断画面；
    等真正开播时该主机多半已经有「直连 / 走代理」的结论，不用再试探。
    """
    try:
        from . import egress
        with db() as conn:
            row = query_one(conn, "SELECT play_url, page_url FROM movie_sources WHERE id = ?",
                            (int(source_id),))
        if not row:
            return
        url = str(row.get("play_url") or row.get("page_url") or "")
        if url:
            egress.probe_async(url, upstream_headers(url))
    except Exception:
        pass


def _kick_probe(source_id: int) -> None:
    """验证通过后排一次清晰度 / 字幕探测（只探从没探过的，幂等）。"""
    try:
        from . import media_probe
        media_probe.submit_probe(int(source_id))
    except Exception:
        pass


def source_order() -> str:
    """选源排序：中文字幕 > 清晰度 > 现有 priority / 主源 / id。

    两个开关分别对应设置页的「优先有字幕」「优先高清晰度」。字幕只认
    ``sub_zh = 'zh'``（判定依据见 media_probe）；unknown 不会被当成有字幕。
    """
    stream = load_config().get("stream") or {}
    parts: List[str] = []
    if bool(stream.get("prefer_subtitle", True)):
        parts.append("CASE WHEN sub_zh = 'zh' THEN 0 ELSE 1 END")
    if bool(stream.get("prefer_quality", True)):
        parts.append("media_height DESC")
    parts.extend(["priority DESC", "is_primary DESC", "id ASC"])
    return ", ".join(parts)


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


def _browser_enabled() -> bool:
    """无头浏览器兜底是否可用（开关 + 依赖都在）。"""
    stream = load_config().get("stream") or {}
    if not bool(stream.get("browser_refresh", True)):
        return False
    from . import online_refresh
    return online_refresh.available()


def _browser_candidates(page: str) -> tuple:
    """无头浏览器兜底：返回 (候选地址, 说明, 地址 → 实际请求头, 兜底 Referer)。

    服务端静态抓取被 Cloudflare 人机校验挡住、或播放地址要等页面脚本
    拉起播放器才请求时（jable / 123av），只有本机浏览器内核能拿到新签名
    地址。开关关闭、Playwright 缺失时返回空列表 + 可读原因。

    有的 CDN 校验的是「播放器所在跳板域」的 Referer / Origin（不是影片页），
    所以这里连请求头一起带回来，验证与播放都用同一套，避免「抽得到播不了」。
    ``referers`` 是页面上跨站 iframe 的 origin，用作 Referer 头拿不到时的兜底。
    """
    stream = load_config().get("stream") or {}
    if not bool(stream.get("browser_refresh", True)):
        return [], "", {}, []
    from . import online_refresh
    if not online_refresh.available():
        return [], "无头浏览器组件未安装（pip install playwright），已跳过", {}, []
    got = online_refresh.extract(page)
    return (list(got.get("urls") or []), str(got.get("note") or ""),
            dict(got.get("headers") or {}), list(got.get("referers") or []))


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
        # 410（签名过期）不能当成「地址还能用」：必须继续走下面的页面重新提取
        gone = bool(res.get("gone")) or "HTTP 410" in str(res.get("note") or "")
        if res["status"] in ("valid", "stale") and not gone:
            # 403 等只是「需要防盗链头」，不是死链：保留地址、标待验证，
            # 别把播放器还能用的来源判成失效。
            soft = res["status"] == "stale"
            msg = "上游需要防盗链头，播放时重试" if soft else "地址仍可用，有效期已续"
            stored = _parse_headers(row["play_headers"])   # 只看本源自己保存的头
            # 已经有能用的请求头就没什么可补的；还没头且浏览器可用时，
            # 先让浏览器把「实际生效的 Referer」抓回来，否则播放时必然 403。
            if not soft or stored or not _browser_enabled():
                with db() as conn:
                    out = _apply(conn, source_id, res["status"], msg,
                                 expires_at=res.get("expires_at", ""), hls=res.get("hls"),
                                 refetched=True, available=1)
                return {"ok": True, "refreshed": True, "note": msg, **(out or {})}
        note = res["note"] or ""

    def _try(list_: List[str], base_note: str, success_note: str,
             headers_by_url: Optional[Dict[str, Dict[str, str]]] = None,
             referers: Optional[List[str]] = None) -> tuple:
        """逐条探测候选地址，第一个「真能播」的写库；返回 (结果 or None, 说明)。

        ``headers_by_url`` 是浏览器截获到的候选地址 → 实际请求头（Referer /
        Origin），``referers`` 是兜底跳板域 origin。CDN 认这个头时必须带上，
        否则明明能播的地址会被判成失效（旧版本就是因为只按影片页 Referer 探，
        才出现「抽得到播不了」）。优先取真正可用的地址；全都不行才退到
        「403/410 待验证」——保留旧行为，不把软错误判死。
        """
        cand_sets: List[tuple] = []
        for cand in list_:
            sets: List[Optional[Dict[str, str]]] = []
            cap = (headers_by_url or {}).get(cand)
            if cap:
                sets.append(cap)
            for ref in (referers or [])[:3]:
                sets.append({"Referer": ref.rstrip("/") + "/", "Origin": ref.rstrip("/")})
            sets.append(None)             # 完全按配置 / 已保存头的默认组合
            cand_sets.append((cand, sets))
        soft: Optional[tuple] = None
        for cand, sets in cand_sets:
            for hdrs in sets:
                res = probe(cand, upstream_headers(cand, source_id=source_id, extra=hdrs))
                if res["status"] == "valid":
                    with db() as conn:
                        out = _apply(conn, source_id, "valid", success_note,
                                     play_url=cand, expires_at=res.get("expires_at", ""),
                                     hls=res.get("hls"), refetched=True, available=1,
                                     play_headers=hdrs)
                    _kick_probe(source_id)
                    return {"ok": True, "refreshed": True, "note": success_note,
                            **(out or {})}, base_note
                base_note = res["note"] or base_note
                if res["status"] == "stale" and soft is None:
                    soft = (cand, hdrs)
        if soft:
            cand, hdrs = soft
            with db() as conn:
                out = _apply(conn, source_id, "valid", base_note,
                             play_url=cand, refetched=True, available=1,
                             play_headers=hdrs)
            _kick_probe(source_id)
            return {"ok": True, "refreshed": True, "note": base_note, **(out or {})}, base_note
        return None, base_note

    # 显式新地址换站时旧 Referer 只会帮倒忙，先清空；同站续期保留已保存的头
    if new_url and _site_origin(new_url) != _site_origin(old):
        with db() as conn:
            _apply(conn, source_id, "checking", "已换新地址，等待验证", play_headers={})

    hit, note = _try([new_url] if new_url else _page_candidates(page), note,
                     note or "已从源站页面重新获取播放地址")
    if hit:
        return hit
    # 静态 HTML 里没有可用地址（被 CF 人机校验挡住 / 播放器由脚本拉流）时，
    # 交给本机无头浏览器打开来源页面，从网络层截获 m3u8 与它实际带的 Referer。
    if not new_url:
        cands, browser_note, browser_headers, browser_referers = _browser_candidates(page)
        if browser_note:
            note = browser_note
        if cands:
            hit, note = _try(cands, note, "已用无头浏览器重新获取播放地址",
                             headers_by_url=browser_headers, referers=browser_referers)
            if hit:
                return hit

    msg = note or "未能重新获取播放地址，请手动填写新地址或配置 Referer"
    # 源站页面要浏览器会话 / 被 CF 挡时服务端拿不到新签名地址：给出能走通的路径，
    # 而不是让用户反复点「续期」。浏览器插件重新捕获后点「立即加入片库」会更新来源。
    if not new_url and ("410" in msg or "403" in msg or "Cloudflare" in msg
                        or "人机校验" in msg or "浏览器" in msg or not msg):
        msg += ("；源站页面可能需要浏览器会话，请用浏览器插件重新打开该片播放几秒，"
                "在插件面板点「立即加入片库」更新播放地址，或手动填写新地址")
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
                "ORDER BY " + source_order() + " LIMIT 1", (int(movie_id),))
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


# 播放前的同步续期：与后台短任务池分开，避免排队等待；超时后任务继续跑，
# 不阻塞播放请求（此时前端仍拿旧地址，失败会照常切源 / 提示）。
_RENEW_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="avm-renew")


def refresh_now(source_id: int, wait: float = 6.0) -> bool:
    """播放前有限等待续期完成；返回是否在等待窗口内完成。

    同一来源复用同一个在跑的任务：入库时已经排上的浏览器续期不会因为播放
    请求再开一个浏览器（既慢又容易被站点判定成爬虫）。
    """
    fut = _refresh_future(source_id)
    try:
        fut.result(timeout=max(0.5, float(wait)))
        return True
    except Exception:
        return False


_REFRESH_LOCK = threading.Lock()
#: 来源 → 正在跑的续期任务（幂等关键：同源同时只跑一个浏览器）
_REFRESH_JOBS: Dict[int, Any] = {}


def _refresh_future(source_id: int) -> Any:
    """取该来源正在跑的续期任务；没有就排一个。"""
    sid = int(source_id or 0)
    with _REFRESH_LOCK:
        fut = _REFRESH_JOBS.get(sid)
        if fut is not None and not fut.done():
            return fut
        fut = _RENEW_POOL.submit(refresh_source, sid)
        _REFRESH_JOBS[sid] = fut
        return fut


def is_refreshing(source_id: int) -> bool:
    """该来源此刻是否正在续期（前端据此显示「正在获取播放地址」）。"""
    with _REFRESH_LOCK:
        fut = _REFRESH_JOBS.get(int(source_id or 0))
    return bool(fut is not None and not fut.done())


def can_renew(source_id: int) -> bool:
    """来源是否带回源线索（有页面地址才谈得上重新取播放地址）。"""
    with db() as conn:
        row = query_one(
            conn, "SELECT page_url, refresh_from FROM movie_sources WHERE id = ?",
            (int(source_id or 0),))
    if not row:
        return False
    return bool(str(row["page_url"] or row["refresh_from"] or "").strip())


def primary_source_id(movie_id: int) -> int:
    """播放实际使用的来源 id（与 movie_play_source 的取舍保持一致）。"""
    with db() as conn:
        row = query_one(
            conn, "SELECT id FROM movie_sources WHERE movie_id = ? AND available = 1 "
            "AND play_url <> '' ORDER BY is_primary DESC, id ASC LIMIT 1",
            (int(movie_id or 0),))
    return int((row or {}).get("id") or 0)


def _own_headers(source_id: int) -> Dict[str, str]:
    """只读本源自己保存的播放头（不含借用兄弟来源的那部分）。"""
    with db() as conn:
        row = query_one(conn, "SELECT play_headers FROM movie_sources WHERE id = ?",
                        (int(source_id or 0),))
    return _parse_headers((row or {}).get("play_headers"))


def due_sources(limit: int = 20, cooldown_minutes: int = 30) -> List[Dict[str, Any]]:
    """巡检候选：已过期 / 明确 410、且带回源页面地址的在线来源。

    冷却判断在 Python 里做：``updated_at`` 可能是 SQLite 的「空格分隔」或
    旧数据里的 ISO「T 分隔」，直接按字符串比较会因格式不同误判。成功/失败
    都会由 ``_apply`` 更新 ``updated_at``，所以冷却对两者同样生效。
    """
    cutoff_dt = datetime.now() - timedelta(minutes=max(1, int(cooldown_minutes)))
    with db() as conn:
        rows = conn.execute(
            "SELECT s.id, s.movie_id, s.site, s.expires_at, s.check_note, s.updated_at, "
            "m.code FROM movie_sources s LEFT JOIN movies m ON m.id = s.movie_id "
            "WHERE s.available = 1 AND s.play_url <> '' AND s.refresh_from <> '' "
            "ORDER BY s.updated_at ASC LIMIT ?",
            (max(1, int(limit)) * 8,)).fetchall()

    def _due(row) -> bool:
        if not (expired(row["expires_at"]) or "410" in str(row["check_note"] or "")):
            return False
        raw = str(row["updated_at"] or "").strip()
        if not raw:
            return True
        try:
            return datetime.fromisoformat(raw.replace(" ", "T")) <= cutoff_dt
        except ValueError:
            return True

    return [dict(r) for r in rows if _due(r)][:max(1, int(limit))]


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
        "ORDER BY " + source_order() + " LIMIT 1", (movie_id, int(failed_id)))
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
