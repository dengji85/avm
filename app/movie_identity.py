# -*- coding: utf-8 -*-
"""在线来源的身份计算（纯函数，不碰数据库）。

设计要点（见 docs/浏览器插件与在线资源设计.md 第 11、13.1 节）：

* **稳定身份**与**易失资源**分离：番号 / 站内 ID / 规范 URL 参与归并，
  带签名的播放地址只作为可失效的派生产物，不参与身份判定。
* URL 规范化只保留白名单的作品标识参数，去掉追踪参数与签名字段，
  否则同一个页面每次带不同签名就会被当成新来源。
* 任何带查询串的地址对外展示前必须脱敏（``mask_url``）。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Dict, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .parser import extract_code

#: 允许保留在规范 URL 里的「作品标识」参数；其余查询参数一律丢弃。
KEEP_PARAMS = ("id", "v", "cid", "code", "video_id", "av", "aid", "pid", "detail")
#: 明确丢弃的追踪 / 签名参数（前缀匹配，小写比较）。
DROP_PREFIXES = ("utm_", "spm", "share", "ref", "from", "timestamp", "_t",
                 "token", "sign", "auth", "key", "session", "x-amz")
DROP_EXACT = {"e", "t", "ts", "_", "nonce", "rand", "callback", "expires"}

#: 虚拟文件记录的伪路径前缀。磁盘上不会存在，缺失检测靠它跳过。
VIRTUAL_PREFIX = "__avm_online__/"


def norm_code(s: Any) -> str:
    """番号归一，与 ``store._smart_code`` 保持一致（此处不导入 store 以免循环）。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).lower()
    return re.sub(r"[\s\-_/．.。·]", "", s)


def site_of(url: str) -> str:
    """站点标识：小写主机名（不含端口与 www.）。"""
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def canonical_url(url: str, keep_params: Iterable[str] = KEEP_PARAMS) -> str:
    """规范化 URL：小写协议与主机、去掉默认端口、只保留白名单查询参数。

    片段（fragment）与签名 / 追踪参数一律丢弃。返回值用于 ``UNIQUE`` 去重，
    因此**不能**包含任何会随时间变化的字段。
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host
    if port and not ((parts.scheme == "http" and port == 80)
                     or (parts.scheme == "https" and port == 443)):
        netloc = "%s:%d" % (host, port)
    path = parts.path or "/"
    allowed = {str(k).lower() for k in keep_params}
    kept = []
    for key, val in parse_qsl(parts.query, keep_blank_values=False):
        low = key.lower()
        if low not in allowed:
            continue
        if any(low.startswith(p) for p in DROP_PREFIXES) or low in DROP_EXACT:
            continue
        kept.append((key, val))
    kept.sort()
    query = urlencode(kept, doseq=True)
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def mask_url(url: str) -> str:
    """脱敏：只保留 scheme://host/path，去掉查询串与片段。

    带签名的播放地址本质是临时凭据，界面 / 日志 / 导出只能显示这个。
    """
    try:
        p = urlsplit(str(url or ""))
        if p.scheme not in ("http", "https") or not p.netloc:
            return ""
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except ValueError:
        return ""


def is_hls_url(url: str) -> bool:
    """判断播放地址是否为 HLS。

    ``api._is_hls`` 只看 ``.m3u8`` 后缀，遇到带查询参数或形如 ``/hls/...`` 的
    地址会误判；这里作为入库时的补充判定，结果写进 ``movie_sources.kind``。
    """
    raw = str(url or "").lower()
    if not raw:
        return False
    try:
        p = urlsplit(raw)
    except ValueError:
        return False
    if p.path.endswith(".m3u8"):
        return True
    if ".m3u8" in p.query:
        return True
    return "/hls/" in p.path or p.path.endswith("/playlist")


def virtual_path(movie_id: int) -> str:
    """纯在线影片的虚拟 ``movie_files.path``：一部影片一条，稳定且不与磁盘冲突。

    按影片 id 而非 URL 生成，切换主源时只需更新 ``strm_url``，不会留下孤儿记录。
    """
    return VIRTUAL_PREFIX + "m%d" % int(movie_id)


def make_alias(kind: str, value: str) -> str:
    """别名表主键。合并后旧键仍指向新影片，避免再次上报又建出一部。"""
    return "%s:%s" % (str(kind or "").strip(), str(value or "").strip())


def build_evidence(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把插件上报的原始字段整理成用于归一的证据字典。

    番号优先级：显式 ``code`` > 标题 > 页面 URL > 播放 URL。
    """
    p = payload or {}
    page_url = str(p.get("page_url") or p.get("url") or "").strip()
    play_url = str(p.get("play_url") or "").strip()
    title = str(p.get("title") or "").strip()
    site = str(p.get("site") or "").strip() or site_of(page_url or play_url)
    site_id = str(p.get("site_id") or "").strip()

    code = str(p.get("code") or "").strip()
    if not code and play_url:
        # 弱信号兜底只在「有播放地址」时进行：列表 / 搜索页的标题与 URL 里
        # 的字母数字组合提出来常是错的（实测 SOON-005 被提成 OO-005），
        # 错番号会建出难以归并的错误影片。宁可先建无番号条目，
        # 等播放页 / 刮削再补番号。
        for text in (title, page_url, play_url):
            got, _, _ = extract_code(str(text or ""))
            if got:
                code = got
                break

    canonical = canonical_url(page_url)
    if not canonical and play_url:
        # 没有页面 URL 时用播放地址的规范化形式兜底，仍不带签名
        canonical = canonical_url(play_url)
    if not canonical and page_url:
        canonical = "urn:avm:" + hashlib.sha1(page_url.encode("utf-8")).hexdigest()[:16]

    kind = str(p.get("kind") or "").strip().lower()
    if kind not in ("page", "hls", "progressive", "cloud", "site_page"):
        kind = "hls" if is_hls_url(play_url) else ("progressive" if play_url else "page")

    return {
        "code": code,
        "code_norm": norm_code(code),
        "code_rule": "ingest" if code else "",
        "site": site,
        "site_id": site_id,
        "page_url": page_url,
        "play_url": play_url,
        "url_canonical": canonical,
        "title": title,
        "kind": kind,
        "confidence": float(p.get("confidence") or 0),
    }
