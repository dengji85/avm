# -*- coding: utf-8 -*-
"""订阅（女优 / 系列）新片提醒：扫描、规则求值、资源候选与入库。

分层的理由（见 docs/订阅新片提醒设计.md）：

* **发现与判定分离**：扫描只负责把列表页上的条目落进 ``subscription_findings``，
  规则在读取时求值。改规则立即生效、不需要重扫，也能解释「为什么没提醒」。
* **订阅域隔离**：规则只影响提醒可见性，不写回 ``movies``，不改变媒体库口径。
* **失败降级**：单目标失败只写 ``last_status``，不中断整批；站点连续失败进冷却。

性能约定：列表页增量翻页（连续命中已知番号即停）、条件请求（ETag / Last-Modified）、
单站串行、批量 upsert；列表接口用 keyset 分页，已入库 / 已看用批量派生而非存列。
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin

from . import providers, store
from .config import DATA_DIR, load_config, outbound_verify_tls
from .db import db, query_all, query_one
from .http_pool import session as http_session
from .movie_identity import norm_code
from .proxy import proxies_for
from .providers.base import NetBlocked, NetError, detect_blocker, _html_title

#: P1 支持的规则类型（二级字段规则留到详情补全落地后再开放）
RULE_TYPES = {
    "release_after": "include",
    "release_before": "include",
    "recent_days": "include",
    "exclude_code": "exclude",
    "code_regex": "exclude",
    "keyword": "exclude",
    "exclude_actress": "exclude",
    "exclude_in_library": "exclude",
    "exclude_watched": "exclude",
    "exclude_ignored": "exclude",
    # 共演 / 合集：同一条作品里出现 ≥2 位已订阅女优
    "exclude_co_star": "exclude",
}

_SITES = ("javbus", "javdb", "manual")
_KINDS = ("actress", "series")
_CODE_RE = re.compile(r"([A-Za-z][A-Za-z0-9]{1,9}-\d{2,5})")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
#: 系列名里的装饰符 / 括号 / 空白：别名比对时全部抹掉（「～」「●」写法变了也能对上）
_ALIAS_NOISE_RE = re.compile(r"[●○◯〇◎◇◆■□▲△▼▽★☆※～〜~（）()\s]+")

#: 单站连续失败冷却（内存态，重启即清空）
_COOLDOWN: Dict[str, Dict[str, Any]] = {}
_UNREAD_CACHE: Dict[str, Any] = {"at": 0.0, "value": 0}
_UNREAD_LOCK = threading.Lock()


# ------------------------------------------------------------------ 工具
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date10(value: Any) -> str:
    m = _DATE_RE.search(str(value or ""))
    return m.group(1) if m else ""


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _values(raw: Any) -> List[str]:
    """规则值：支持换行 / 逗号分隔的多值。"""
    text = str(raw or "")
    return [x.strip() for x in re.split(r"[\n,，;；]+", text) if x.strip()]


def _target_public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "kind": row.get("kind") or "actress",
        "name": row.get("name") or "",
        "site": row.get("site") or "",
        "site_id": row.get("site_id") or "",
        "page_url": row.get("page_url") or "",
        "cover": row.get("cover") or "",
        "enabled": bool(row.get("enabled")),
        "baseline_at": row.get("baseline_at") or "",
        "baseline_code": row.get("baseline_code") or "",
        "last_scan_at": row.get("last_scan_at") or "",
        "last_status": row.get("last_status") or "",
        "created_at": row.get("created_at") or "",
    }


def _finding_public(row: Dict[str, Any], target_names: List[str], visible: bool,
                    reasons: List[str], in_library: bool, watched: bool,
                    co_names: Optional[List[str]] = None) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    try:
        meta = json.loads(row.get("meta_json") or "{}")
    except Exception:
        meta = {}
    confirm_count = int(row.get("confirm_count") or 0)
    return {
        "id": int(row["id"]),
        "code": row.get("code") or "",
        "title": row.get("title") or "",
        "release_date": row.get("release_date") or "",
        "cover": row.get("cover") or "",
        "page_url": row.get("page_url") or "",
        "site": row.get("site") or "",
        "status": row.get("status") or "new",
        "first_seen_at": row.get("first_seen_at") or "",
        "targets": target_names,
        "meta": meta,
        "visible": visible,
        "reasons": reasons,
        "in_library": in_library,
        "watched": watched,
        # 见到 2 次以上才算确认：未确认的仍会显示，但不计入未读角标
        "confirm_count": confirm_count,
        "confirmed": confirm_count >= 2,
        # 共演：元信息里 ≥2 位已订阅女优（合集 / 共演作品）
        "co_star": len(co_names or []) >= 2,
        "co_names": co_names or [],
        "probe_site": row.get("probe_site") or "",
        "probe_status": row.get("probe_status") or "",
        "probe_kind": row.get("probe_kind") or "",
        "probe_at": row.get("probe_at") or "",
    }


# ------------------------------------------------------------------ 封面
#: 订阅发现封面缓存：外站图片直链在浏览器里常常被墙 / 防盗链，统一由后端取回再本地提供
_COVER_DIR = DATA_DIR / "sub_covers"
_COVER_LOCKS: Dict[str, threading.Lock] = {}
_COVER_GUARD = threading.Lock()


def _cover_file(code: str, suffix: str = "") -> Path:
    _COVER_DIR.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^a-z0-9_-]", "", str(code or "").lower()) or "unknown"
    return _COVER_DIR / (name + suffix + ".jpg")


def _cover_lock(key: str) -> threading.Lock:
    with _COVER_GUARD:
        lock = _COVER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _COVER_LOCKS[key] = lock
        return lock


def _cookie_dict(raw: Any) -> Optional[Dict[str, str]]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    out: Dict[str, str] = {}
    for part in text.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out or None


def _large_cover_url(url: str) -> str:
    """JavBus 缩略图换官方大图：/pics/thumb/xxx.jpg → /pics/cover/xxx_b.jpg。

    其它站点没有这套规律，返回空串（调用方回退原图）。
    """
    u = str(url or "")
    if "/pics/thumb/" not in u:
        return ""
    head, _, name = u.rpartition("/")
    stem = name[:-4] if name.lower().endswith(".jpg") else name
    return head.replace("/pics/thumb", "/pics/cover") + "/" + stem + "_b.jpg"


def _thumb_cover_url(url: str) -> str:
    """大图回退缩略图：/pics/cover/xxx_b.jpg → /pics/thumb/xxx.jpg（JavBus 规律）。"""
    u = str(url or "")
    if "/pics/cover/" not in u:
        return ""
    head, _, name = u.rpartition("/")
    stem = name[:-4] if name.lower().endswith(".jpg") else name
    if stem.endswith("_b"):
        stem = stem[:-2]
    return head.replace("/pics/cover", "/pics/thumb") + "/" + stem + ".jpg"


def _download_cover(url: str, path: Path, site: str) -> bool:
    """把封面下载到 path；成功返回 True（小图 / 占位页都算失败）。"""
    try:
        import requests

        cfg = load_config()
        scfg = cfg.get("scraper") or {}
        opts = scfg.get(site) or {}
        headers = {
            "User-Agent": scfg.get("user_agent") or
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": url.rsplit("/", 1)[0] + "/",
        }
        resp = requests.get(
            url, timeout=min(20, int(scfg.get("timeout", 20) or 20)),
            headers=headers, cookies=_cookie_dict(opts.get("cookie")),
            proxies=proxies_for(url, override=scfg.get("proxy") or ""),
            verify=outbound_verify_tls(),
        )
        resp.raise_for_status()
        data = resp.content or b""
        if len(data) < 256:
            return False
        path.write_bytes(data)
        return True
    except Exception:
        return False


def ensure_finding_cover(conn, finding_id: int, large: bool = False) -> Optional[Path]:
    """取回单条发现的封面并缓存；失败返回 None（前端回退占位图）。

    large=True 时优先取官方大图（放大看封面用），拿不到再退回列表缩略图，
    保证「图一定能显示」，只是清晰度不同。
    """
    row = query_one(conn, "SELECT * FROM subscription_findings WHERE id = ?", (int(finding_id),))
    if not row:
        raise KeyError("发现记录不存在")
    code = row.get("code_norm") or row.get("code") or str(finding_id)
    url = str(row.get("cover") or "").strip()
    if not url:
        return None
    site = str(row.get("site") or "")
    if large:
        # 1) DMM 官方竖版海报（1000+ 宽、2:3）：大卡片铺满整列也清晰
        poster_url = ""
        try:
            from .images import dmm_urls
            poster_url = (dmm_urls(row.get("code") or "") or {}).get("poster") or ""
        except Exception:
            poster_url = ""
        if poster_url:
            poster = _cover_file(code, "_p")
            if poster.exists() and poster.stat().st_size > 0:
                return poster
            with _cover_lock(str(code) + "_p"):
                if poster.exists() and poster.stat().st_size > 0:
                    return poster
                if _download_cover(poster_url, poster, "dmm"):
                    return poster
        # 2) 站点官方大图（JavBus 是横版整张，只能兜底）
        big_url = _large_cover_url(url)
        if big_url:
            big = _cover_file(code, "_b")
            if big.exists() and big.stat().st_size > 0:
                return big
            with _cover_lock(str(code) + "_b"):
                if big.exists() and big.stat().st_size > 0:
                    return big
                if _download_cover(big_url, big, site):
                    return big
        # 大图不存在 / 下载失败：回退列表图
    path = _cover_file(code)
    if path.exists() and path.stat().st_size > 0:
        return path
    with _cover_lock(str(code)):
        if path.exists() and path.stat().st_size > 0:
            return path
        if _download_cover(url, path, site):
            return path
        # 存的是大图但此刻取不到：退回缩略图，先保证有图可看
        thumb = _thumb_cover_url(url)
        if thumb and _download_cover(thumb, path, site):
            return path
    return None


# ------------------------------------------------------------------ 在线资源探测
#: 只探测「按番号直达作品页」的站点（如 123av / JavBus）；搜索型 URL 返回 200 也说明不了
#: 有没有资源，直接排除。
_PROBE_SKIP_HINTS = ("search", "keyword=", "vl_searchbyid")

#: 认识详情页结构的站点 → 解析器；探测时顺手补类型 / 女优 / 大封面，零额外请求
_DETAIL_PARSERS = {"preset_javbus": "javbus"}


def _drop_cover_cache(code: str) -> None:
    """封面地址变了，删掉大小图缓存，下次请求重新取。"""
    for suffix in ("", "_b", "_p"):
        try:
            path = _cover_file(code, suffix)
            if path.exists():
                path.unlink()
        except Exception:
            pass


def _enrich_from_detail(conn, row: Dict[str, Any], code: str, html: str,
                        site: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    """把详情页里的类型 / 女优 / 时长 / 大封面补进发现记录。

    探测本来就把整页取回来了，这里只是多解析一次；不额外请求。
    """
    if _DETAIL_PARSERS.get(str(site.get("id") or "")) != "javbus":
        return
    from .providers.javbus import JavBusProvider

    parsed = JavBusProvider(cfg).parse_detail(code, html)
    if not parsed:
        return
    try:
        meta = json.loads(row.get("meta_json") or "{}")
    except Exception:
        meta = {}
    changed = False
    for key in ("genres", "actresses", "runtime", "studio", "series", "actress_avatars"):
        if parsed.get(key) and not meta.get(key):
            meta[key] = parsed[key]
            changed = True
    fields: Dict[str, Any] = {}
    cover = str(parsed.get("cover") or "")
    old_cover = str(row.get("cover") or "")
    # 列表页只有缩略图（/pics/thumb/），详情页是官方大图（/pics/cover/*_b.jpg）
    if cover and "/pics/cover/" in cover and cover != old_cover:
        fields["cover"] = cover
    if changed:
        fields["meta_json"] = json.dumps(meta, ensure_ascii=False)
    if not fields:
        return
    conn.execute("UPDATE subscription_findings SET %s WHERE id = ?"
                 % ", ".join("%s = ?" % k for k in fields),
                 list(fields.values()) + [int(row["id"])])
    conn.commit()
    if "cover" in fields:
        _drop_cover_cache(code)


def _site_kind(site: Dict[str, Any]) -> str:
    """站点类别：watch=在线观看，meta=元数据查询（缺省 meta，见 config 迁移说明）。"""
    return "watch" if str(site.get("kind") or "") == "watch" else "meta"


def probe_sites(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """可探测的站点：**只取在线观看站**（123av / Jable 这类）。

    资料站（JavBus / JavDB / AV-Wiki）命中只说明有资料页，对「能不能看」没有
    参考价值，已经不再参与探测；它们仍保留在设置里供详情页查询元数据。
    """
    out = []
    for s in (cfg.get("online_sites") or []):
        url = str(s.get("url") or "")
        if not s.get("enabled", True) or "{code}" not in url:
            continue
        if _site_kind(s) != "watch":
            continue
        low = url.lower()
        if any(k in low for k in _PROBE_SKIP_HINTS):
            continue
        out.append(s)
    return out


def _probe_url(site: Dict[str, Any], code: str) -> str:
    val = str(code or "")
    if site.get("lower"):
        val = val.lower()
    return str(site.get("url") or "").replace("{code}", quote(val))


# ------------------------------------------------------------------ 站点凭证
#: 通过浏览器验证后，把对站点生效的 Cookie / UA 存下来，后续直接走普通请求，
#: 不必每条发现都重跑一次 Chrome 验证（cf_clearance 这类凭证会过期，过期后
#: 普通请求再次被挡，会自动回落到 CDP 刷新一次）。
_SITE_CRED_PATH = DATA_DIR / "site_cookies.json"
_SITE_CRED_LOCK = threading.Lock()


def _site_key(site: Dict[str, Any]) -> str:
    return str(site.get("id") or site.get("name") or site.get("url") or "")


def _load_site_creds() -> Dict[str, Dict[str, str]]:
    try:
        data = json.loads(_SITE_CRED_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_site_cred(key: str, cookie: str, ua: str) -> None:
    if not key or not cookie:
        return
    with _SITE_CRED_LOCK:
        data = _load_site_creds()
        if (data.get(key) or {}).get("cookie") == cookie:
            return
        data[key] = {"cookie": cookie, "ua": ua, "at": _now()}
        try:
            _SITE_CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
            _SITE_CRED_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass


def _site_cred(site: Dict[str, Any]) -> Dict[str, str]:
    """站点的可用凭证：配置里手填的优先，其次是上次通过验证时存下的。"""
    saved = _load_site_creds().get(_site_key(site)) or {}
    return {
        "cookie": str(site.get("cookie") or saved.get("cookie") or ""),
        "ua": str(site.get("ua") or saved.get("ua") or ""),
    }


def _capture_cred(site: Dict[str, Any], url: str, scfg: Dict[str, Any]) -> None:
    """CDP 通道刚验证通过时，把 Chrome 里的 Cookie + UA 存下来供下次复用。"""
    try:
        from .cdp_fetch import cdp_cookies

        # 探测用的是独立浏览器（端口 = 刮削端口 + 1，见 _probe_cdp），
        # 从它取凭证才拿得到刚才通过挑战的 cf_clearance。
        port = int(scfg.get("chrome_debug_port", 9222) or 9222) + 1
        cookie, ua = cdp_cookies(url, port=port, auto_launch=False)
        _save_site_cred(_site_key(site), cookie, ua)
        if cookie:
            # 有了凭证，直连大概率能过，解除这一站的短暂跳过
            with _HTTP_BLOCKED_LOCK:
                _HTTP_BLOCKED.pop(_site_key(site), None)
    except Exception:
        pass


#: 直连被挡（403 / 429 / 503）的站点短暂跳过：同批次后续条目直接走 CDP，
#: 不把时间浪费在必然失败的请求上；拿到凭证或冷却结束自动恢复。
_HTTP_BLOCKED: Dict[str, float] = {}
_HTTP_BLOCKED_LOCK = threading.Lock()
_HTTP_BLOCK_SECONDS = 180


def _http_blocked(key: str) -> bool:
    with _HTTP_BLOCKED_LOCK:
        until = _HTTP_BLOCKED.get(key) or 0
        if until and until > time.time():
            return True
        _HTTP_BLOCKED.pop(key, None)
        return False


def _mark_http_blocked(key: str) -> None:
    with _HTTP_BLOCKED_LOCK:
        _HTTP_BLOCKED[key] = time.time() + _HTTP_BLOCK_SECONDS


#: 被限流（429）的站点：本批次直接跳过。限流是按 IP 的，换 CDP 也一样被限，
#: 硬试只会拖慢整批；冷却结束自动恢复。
_RATE_LIMITED: Dict[str, float] = {}
_RATE_LIMITED_LOCK = threading.Lock()
_RATE_LIMIT_SECONDS = 300


def _rate_limited(key: str) -> bool:
    with _RATE_LIMITED_LOCK:
        until = _RATE_LIMITED.get(key) or 0
        if until and until > time.time():
            return True
        _RATE_LIMITED.pop(key, None)
        return False


def _mark_rate_limited(key: str) -> None:
    with _RATE_LIMITED_LOCK:
        _RATE_LIMITED[key] = time.time() + _RATE_LIMIT_SECONDS


#: CDP 连续拿不到可用页面的站点：一次 CDP 要等十几秒，整批都试等于把
#: 长任务线程占死（Jable 直连 403、CDP 只回错误页/挑战页就是这种情况）。
#: 连续失败达到阈值后本进程内跳过该站的 CDP，下次服务重启或重试成功即恢复。
_CDP_MISS: Dict[str, int] = {}
_CDP_MISS_LOCK = threading.Lock()
_CDP_MISS_LIMIT = 3


def _cdp_dead(key: str) -> bool:
    with _CDP_MISS_LOCK:
        return (_CDP_MISS.get(key) or 0) >= _CDP_MISS_LIMIT


def _mark_cdp_miss(key: str) -> None:
    with _CDP_MISS_LOCK:
        _CDP_MISS[key] = (_CDP_MISS.get(key) or 0) + 1


def _clear_cdp_miss(key: str) -> None:
    with _CDP_MISS_LOCK:
        _CDP_MISS.pop(key, None)


def _probe_http(url: str, scfg: Dict[str, Any],
                cred: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """直连式探测：返回 (状态码, HTML)。状态码是判断 404 的关键，所以先走这一层。"""
    try:
        import requests

        cred = cred or {}
        # cf_clearance 与 UA 绑定，UA 必须和取 Cookie 时的浏览器一致
        ua = cred.get("ua") or scfg.get("user_agent") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        resp = requests.get(
            url, timeout=max(5, int(scfg.get("timeout", 20) or 20)),
            headers={
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            cookies=_cookie_dict(cred.get("cookie")),
            proxies=proxies_for(url, override=scfg.get("proxy") or ""),
            verify=outbound_verify_tls(),
        )
        return int(getattr(resp, "status_code", 0) or 0), resp.text or ""
    except Exception:
        return 0, ""


def _page_settled(html: str, needle: str) -> bool:
    """页面是否已经「落定」：单页应用先出空壳再填数据，早取会拿到没内容的页面。"""
    title = _html_title(html).replace("-", "")
    low = (html or "").lower()
    if needle and needle in title:
        return True
    if any(k in title for k in ("404", "not found", "頁面丟失", "页面丢失", "找不到")):
        return True
    return any(k in low for k in ("<video", ".m3u8", "hlsurl", "video_url",
                                  "videojs", "jwplayer"))


def _probe_cdp(url: str, scfg: Dict[str, Any], code: str = "") -> str:
    """被反爬挡住的兜底：用**独立的探测浏览器**取页面。

    和刮削的常驻 Chrome 分开：刮削用 headless（av-wiki 能过就行），而 Jable
    这类 Cloudflare 站点会识别 headless 一直卡在挑战页、且需要走代理才连得上。
    所以探测这里用「窗口挪出屏幕的真实 Chrome + 同一个出口代理」——
    实测几秒内放行。没有桌面会话（容器 / 纯命令行）时退回 headless。
    """
    try:
        from .cdp_fetch import cdp_fetch, has_desktop, is_error_page

        base_port = int(scfg.get("chrome_debug_port", 9222) or 9222)
        proxy = str(scfg.get("proxy") or "").strip()
        if not proxy:
            from .proxy import global_proxy
            proxy = global_proxy()
        needle = str(code or "").lower().replace("-", "")
        text = cdp_fetch(
            url,
            port=base_port + 1,
            user_data_dir=str(DATA_DIR / "chrome_probe_profile"),
            wait=25,
            auto_launch=True,
            headless=not has_desktop(),
            proxy=proxy,
            require=(lambda h: _page_settled(h, needle)) if needle else None,
        ) or ""
        # Chrome 自身的错误页会把请求 URL 回显在正文里，不能当站点内容
        return "" if is_error_page(text) else text
    except Exception:
        return ""


def probe_finding(conn, finding_id: int, cfg: Optional[Dict[str, Any]] = None,
                  sites: Optional[List[Dict[str, Any]]] = None,
                  delay: float = 0.0, ttl_days: int = 0) -> str:
    """探测一条发现在在线站点上有没有作品页，结果落库。

    返回 ``ok``（有）/ ``none``（站点明确没有）/ ``''``（网络不通，未知）。
    判据保守：正文出现番号、且标题含番号或页面带播放器才算有，避免把 SPA 首页 /
    反爬页 / Chrome 错误页当命中。

    只问在线观看站（123av / Jable）：命中即记 ``probe_kind='watch'`` 并定论；
    资料站不参与（有资料页不等于能看）。

    ``ttl_days`` > 0 时，结论还在有效期内的条目直接复用旧结果（站点上架 / 下架
    是慢变化，没必要每次重问一遍）；手动「检查在线资源」传 0 强制重探。
    """
    row = query_one(conn, "SELECT * FROM subscription_findings WHERE id = ?", (int(finding_id),))
    if not row:
        raise KeyError("发现记录不存在")
    if ttl_days > 0 and str(row.get("probe_status") or "") and row.get("probe_at"):
        try:
            at = datetime.strptime(str(row["probe_at"]), "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - at).days < int(ttl_days):
                return str(row["probe_status"])
        except ValueError:
            pass
    cfg = cfg or load_config()
    scfg = cfg.get("scraper") or {}
    sites = probe_sites(cfg) if sites is None else sites
    code = str(row.get("code") or row.get("code_norm") or "").strip()
    needle = code.lower().replace("-", "")
    result, hit, kind = "", "", ""
    for i, site in enumerate(sites):
        if i and delay:
            time.sleep(delay)
        url = _probe_url(site, code)
        if not url:
            continue
        cred = _site_cred(site)
        key = _site_key(site)
        if _rate_limited(key):
            # 这一站被限流，本批次不再纠缠（换 CDP 也一样是同一个出口 IP）
            continue
        if cred.get("cookie") or not _http_blocked(key):
            status, text = _probe_http(url, scfg, cred)
            if status == 429:
                _mark_rate_limited(key)
                continue
            if status in (403, 503):
                # 这一站直连被挡：本批次后续条目别再试，直接走 CDP
                _mark_http_blocked(key)
        else:
            # 已知直连会被挡且没有可复用凭证 → 直接走 CDP，省掉一次必然失败
            status, text = 0, ""
        name = str(site.get("name") or site.get("id") or "")
        if status in (404, 410):
            # 这一站明确没有；继续问下一站，别急着下「无资源」的结论
            if not result:
                result, hit = "none", name
            continue
        used_cdp = False
        if status != 200 or not text:
            # 被 Cloudflare / Loader 验证挡住或直接失败：复用刮削的常驻 Chrome（CDP）
            # 绕过去；刮削能过的站点，探测也该能过
            if _cdp_dead(key):
                # 这一站的 CDP 连续多次只拿到错误页 / 验证页，本批次别再耗十几秒
                continue
            text = _probe_cdp(url, scfg, code)
            if not text or detect_blocker(text):
                # 站点抽风 / 被墙 / 挑战页没放行 → 保持「未知」，下次还能重试
                _mark_cdp_miss(key)
                continue
            _clear_cdp_miss(key)
            used_cdp = True
        if detect_blocker(text):
            # 仍然是验证页（需要 Cookie 才能过），不当成「有」或「无」
            continue
        low = text.lower()
        flat = low.replace("-", "")
        title_flat = _html_title(text).replace("-", "")
        has_player = any(
            k in low for k in ("<video", ".m3u8", "hlsurl", "video_url", "videojs", "jwplayer")
        )
        # 命中要同时满足：正文出现番号，且标题含番号或页面带播放器。
        # 只看「正文含番号」太松——错误页 / 验证页 / 首页会回显 URL，
        # 会把没有资源的站点误判成有（例如 Jable 的 Chrome 错误页）。
        if not (needle and needle in flat and (needle in title_flat or has_player)):
            continue
        if used_cdp:
            # 刚验证通过：把 Cookie + UA 存下来，下一条直接走普通请求
            _capture_cred(site, url, scfg)
        # 探测只问在线观看站，命中即定论（资料站已不参与，见 probe_sites）
        result, hit, kind = "ok", name, "watch"
        try:
            _enrich_from_detail(conn, row, code, text, site, cfg)
        except Exception:
            pass
        break
    conn.execute(
        "UPDATE subscription_findings SET probe_site = ?, probe_status = ?, "
        "probe_kind = ?, probe_at = ? WHERE id = ?",
        (hit, result, kind if result == "ok" else "",
         _now() if result else "", int(finding_id)))
    conn.commit()
    return result


# ------------------------------------------------------------------ 目标
def list_targets(conn) -> List[Dict[str, Any]]:
    # new_count 与发现列表「未读」口径保持一致：status='new' 且已二次确认，
    # 否则女优墙的红点会比订阅页多出还没确认的脏数据
    rows = query_all(
        conn,
        "SELECT t.*, (SELECT COUNT(*) FROM subscription_finding_targets ft "
        "WHERE ft.target_id = t.id) AS finding_count, "
        "(SELECT COUNT(*) FROM subscription_finding_targets ft "
        "JOIN subscription_findings f ON f.id = ft.finding_id "
        "WHERE ft.target_id = t.id AND f.status = 'new' AND f.confirm_count >= 2) AS new_count, "
        "(SELECT MAX(f.first_seen_at) FROM subscription_finding_targets ft "
        "JOIN subscription_findings f ON f.id = ft.finding_id "
        "WHERE ft.target_id = t.id) AS latest_found_at "
        "FROM subscription_targets t ORDER BY t.enabled DESC, t.id DESC")
    out = []
    for r in rows:
        item = _target_public(r)
        item["finding_count"] = int(r.get("finding_count") or 0)
        item["new_count"] = int(r.get("new_count") or 0)
        item["latest_found_at"] = r.get("latest_found_at") or ""
        out.append(item)
    return out


def get_target(conn, target_id: int) -> Optional[Dict[str, Any]]:
    row = query_one(conn, "SELECT * FROM subscription_targets WHERE id = ?", (int(target_id),))
    return _target_public(row) if row else None


def _seed_default_rules(conn) -> None:
    """首次出现订阅时写入默认偏好：跳过已看过 / 已忽略。

    只在「一条全局规则都没有」时写，之后用户怎么改都不会被覆盖。
    """
    row = query_one(conn, "SELECT COUNT(*) AS n FROM subscription_rules WHERE target_id IS NULL")
    if row and int(row.get("n") or 0) > 0:
        return
    for t in ("exclude_watched", "exclude_ignored"):
        conn.execute(
            "INSERT INTO subscription_rules(target_id, rule_type, op, value, enabled) "
            "VALUES(NULL,?,?,?,1)", (t, RULE_TYPES[t], "1"))


def create_target(conn, payload: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(payload.get("kind") or "actress").strip().lower()
    if kind not in _KINDS:
        raise ValueError("订阅类型只能是 actress / series")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("请填写订阅名称")
    site = str(payload.get("site") or "").strip().lower()
    if site and site not in _SITES:
        raise ValueError("站点只能是 javbus / javdb / manual")
    site_id = str(payload.get("site_id") or "").strip()
    page_url = str(payload.get("page_url") or "").strip()
    if not site_id and not page_url:
        raise ValueError("请填写站内 ID 或页面地址，否则没有可扫描的列表页")
    if not site:
        site = "manual" if page_url else ""
    # 基线：显式传 baseline_at 时按传入值（可为空 = 不按日期截断）；
    # 不传则默认从订阅当天开始只提醒之后发布的作品。
    if "baseline_at" in payload:
        baseline = _date10(payload.get("baseline_at"))
    else:
        baseline = date.today().isoformat()
    cover = str(payload.get("cover") or "").strip()
    try:
        cur = conn.execute(
            "INSERT INTO subscription_targets(kind, name, site, site_id, page_url, cover, "
            "enabled, baseline_at, baseline_code) VALUES(?,?,?,?,?,?,?,?,?)",
            (kind, name, site, site_id, page_url, cover,
             1 if ("enabled" not in payload or _bool(payload.get("enabled"))) else 0,
             baseline, str(payload.get("baseline_code") or "").strip()))
    except Exception as e:  # sqlite3.IntegrityError：同站点同 ID 已订阅
        if "UNIQUE" in str(e).upper():
            raise ValueError("该目标已订阅过") from e
        raise
    _seed_default_rules(conn)
    return get_target(conn, int(cur.lastrowid))


def update_target(conn, target_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_target(conn, target_id)
    if not cur:
        raise KeyError("订阅目标不存在")
    fields: Dict[str, Any] = {}
    for key in ("name", "site", "site_id", "page_url", "cover", "baseline_code"):
        if key in payload:
            fields[key] = str(payload.get(key) or "").strip()
    if "site" in fields and fields["site"] and fields["site"] not in _SITES:
        raise ValueError("站点只能是 javbus / javdb / manual")
    if "enabled" in payload:
        fields["enabled"] = 1 if _bool(payload.get("enabled")) else 0
    if payload.get("reset_baseline"):
        # 重置基线 = 「从现在开始提醒」：过去的作品不再进入提醒
        fields["baseline_at"] = date.today().isoformat()
        fields["etag"] = ""
        fields["last_modified"] = ""
    elif "baseline_at" in payload:
        d = _date10(payload.get("baseline_at"))
        if not d:
            raise ValueError("基线日期格式应为 YYYY-MM-DD")
        fields["baseline_at"] = d
    if not fields:
        return cur
    cols = ", ".join("%s = ?" % k for k in fields)
    conn.execute("UPDATE subscription_targets SET %s WHERE id = ?" % cols,
                 list(fields.values()) + [int(target_id)])
    return get_target(conn, target_id)


def delete_target(conn, target_id: int) -> None:
    # 只删目标与归属关系；发现记录保留（历史可查，也不影响别的目标）
    row = query_one(conn, "SELECT kind, name FROM subscription_targets WHERE id = ?",
                    (int(target_id),))
    conn.execute("DELETE FROM subscription_targets WHERE id = ?", (int(target_id),))
    # 「关注并入订阅」：取消女优订阅即取消关注，避免关注墙里留下已不订阅的人
    if row and (row.get("kind") or "actress") == "actress" and row.get("name"):
        conn.execute("UPDATE actresses SET followed = 0 WHERE name = ?", (row["name"],))


# ------------------------------------------------------------------ 从库中添加
#: 支持列表扫描的站点：自动订阅只挑这些（manual 必须用户自己提供页面地址）
_LIST_SITES = ("javbus", "javdb")


#: 系列名尾部「（厂商 编号）」：JavBus 整串搜索会 403，建订阅时只用主标题
_SERIES_PAREN_RE = re.compile(r"\s*[（(][^（）()]*[）)]\s*$")


def _search_name(kind: str, name: str) -> str:
    s = str(name or "").strip()
    if kind != "series":
        return s
    return _SERIES_PAREN_RE.sub("", s).strip() or s


def _auto_source(cfg: Dict[str, Any], kind: str, name: str) -> Tuple[str, str]:
    """按设置里已启用的列表源挑一个站点，并用搜索页地址作为扫描入口。

    库里只有女优 / 系列的名字，没有站点内部 ID；用站内搜索页代替详情页，
    这样用户不需要手动查 ID，新增订阅也不需要额外维护映射。
    """
    scfg = cfg.get("scraper") or {}
    order = [str(x) for x in (scfg.get("order") or [])]
    sites = [s for s in order if s in _LIST_SITES]
    sites += [s for s in _LIST_SITES if s not in sites]
    preferred = str((cfg.get("subscription") or {}).get("preferred_site") or "").strip().lower()
    if preferred in _LIST_SITES:
        sites = [preferred] + [s for s in sites if s != preferred]
    for site in sites:
        cls = providers.REGISTRY.get(site)
        if not cls:
            continue
        try:
            inst = cls(cfg)
        except Exception:  # noqa: BLE001 单站初始化失败不影响换下一个
            continue
        if not inst.enabled() or not inst.supports_list():
            continue
        base = str(getattr(inst, "base_url", "") or "").rstrip("/")
        if not base:
            continue
        q = quote(_search_name(kind, name), safe="")
        if site == "javbus":
            # JavBus 女优搜索是 /searchstar/，系列没有独立搜索页，退回关键词搜索
            url = ("%s/searchstar/%s" % (base, q)) if kind == "actress" \
                else ("%s/search/%s" % (base, q))
        else:
            url = "%s/search?q=%s&f=%s" % (base, q, "actor" if kind == "actress" else "series")
        return site, url
    raise ValueError("没有可用的列表站点，请先在 设置 → 刮削 里启用 JavBus 或 JavDB")


def library_options(conn, kind: str = "actress", q: str = "", limit: int = 60,
                    followed_only: bool = False,
                    subscribed_only: bool = False) -> List[Dict[str, Any]]:
    """库内可订阅的候选（女优 / 系列），带上「是否已订阅」供前端置灰。"""
    kind = str(kind or "actress").strip().lower()
    if kind not in _KINDS:
        raise ValueError("订阅类型只能是 actress / series")
    limit = max(1, min(_int(limit, 60), 200))
    text = str(q or "").strip()
    subscribed: Dict[Tuple[str, str], int] = {
        (r["kind"], r["name"]): int(r["id"])
        for r in query_all(conn, "SELECT id, kind, name FROM subscription_targets")}
    items: List[Dict[str, Any]] = []
    if subscribed_only:
        # 只列已订阅的：直接从订阅目标出发，避免订阅的人不在「库内 Top N」里就筛不出来
        names = [n for (k, n) in subscribed if k == kind]
        if text:
            low = text.lower()
            names = [n for n in names if low in n.lower()]
        for name in names[:limit]:
            row = (query_one(conn, "SELECT id, avatar, followed FROM actresses WHERE name = ?",
                             (name,)) if kind == "actress"
                   else query_one(conn, "SELECT id FROM series WHERE name = ?", (name,)))
            rid = int((row or {}).get("id") or 0)
            if kind == "actress":
                cnt = query_one(conn, "SELECT COUNT(*) AS c FROM movie_actress WHERE actress_id = ?",
                                (rid,)) if rid else None
                cover = str((row or {}).get("avatar") or "")
                followed = bool((row or {}).get("followed"))
            else:
                cnt = query_one(conn, "SELECT COUNT(*) AS c FROM movies WHERE series_id = ?",
                                (rid,)) if rid else None
                cover = _library_cover(conn, kind, name)
                followed = False
            items.append({
                "kind": kind, "id": rid, "name": name, "cover": cover,
                "count": int((cnt or {}).get("c") or 0), "followed": followed,
                "subscribed": True, "target_id": subscribed[(kind, name)],
            })
        return items
    if kind == "actress":
        rows = store.actress_wall(conn, q=text, sort="followed" if followed_only else "count",
                                  limit=limit, followed_only=followed_only)
        for r in rows:
            name = str(r.get("name") or "")
            if not name:
                continue
            items.append({
                "kind": "actress", "id": int(r["id"]), "name": name,
                "cover": r.get("avatar") or r.get("sample_cover") or "",
                "count": int(r.get("count") or 0),
                "followed": bool(r.get("followed")),
                "subscribed": ("actress", name) in subscribed,
                "target_id": subscribed.get(("actress", name), 0),
            })
    else:
        where, args = ("WHERE se.name LIKE ?", ["%%%s%%" % text]) if text else ("", [])
        rows = query_all(
            conn,
            "SELECT se.id, se.name, COUNT(m.id) AS count, "
            "(SELECT m2.cover FROM movies m2 WHERE m2.series_id = se.id "
            " AND m2.cover <> '' LIMIT 1) AS sample_cover "
            "FROM series se LEFT JOIN movies m ON m.series_id = se.id "
            + where + " GROUP BY se.id ORDER BY count DESC, se.name LIMIT ?",
            [*args, limit])
        for r in rows:
            name = str(r.get("name") or "")
            if not name:
                continue
            items.append({
                "kind": "series", "id": int(r["id"]), "name": name,
                "cover": r.get("sample_cover") or "",
                "count": int(r.get("count") or 0),
                "followed": False,
                "subscribed": ("series", name) in subscribed,
                "target_id": subscribed.get(("series", name), 0),
            })
    return items


def _library_cover(conn, kind: str, name: str) -> str:
    if kind == "actress":
        row = query_one(conn, "SELECT avatar FROM actresses WHERE name = ?", (name,))
        return str((row or {}).get("avatar") or "")
    row = query_one(
        conn,
        "SELECT (SELECT m.cover FROM movies m WHERE m.series_id = se.id "
        " AND m.cover <> '' LIMIT 1) AS cover FROM series se WHERE se.name = ?",
        (name,))
    return str((row or {}).get("cover") or "")


def _library_latest_date(conn, kind: str, name: str) -> str:
    """库内该女优 / 系列最新一部作品的发行日，作为「从库里订阅」的基线。

    基线取库内最新日期而不是订阅当天：订阅后能先补齐库里缺的作品，
    之后再发布的新片继续提醒；库里没有作品时基线留空（首轮最多 3 页 / 100 条兜底）。
    """
    if kind == "actress":
        row = query_one(
            conn,
            "SELECT MAX(m.release_date) AS d FROM movies m "
            "JOIN movie_actress ma ON ma.movie_id = m.id "
            "JOIN actresses a ON a.id = ma.actress_id WHERE a.name = ?",
            (name,))
    else:
        row = query_one(
            conn,
            "SELECT MAX(m.release_date) AS d FROM movies m "
            "JOIN series se ON se.id = m.series_id WHERE se.name = ?",
            (name,))
    return _date10((row or {}).get("d") or "")


def _library_codes(conn, codes: List[str]) -> set:
    """库里已入库的番号集合：订阅只报没入库的作品，避免重复提醒。"""
    uniq = list(dict.fromkeys([c for c in codes if c]))
    if not uniq:
        return set()
    marks = ",".join("?" * len(uniq))
    rows = query_all(conn, "SELECT code_norm FROM movies WHERE code_norm IN (%s)" % marks, uniq)
    return {r["code_norm"] for r in rows}


def create_target_from_library(conn, cfg: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """从库内的女优 / 系列直接建订阅目标：站点与扫描地址自动生成。"""
    kind = str(payload.get("kind") or "actress").strip().lower()
    if kind not in _KINDS:
        raise ValueError("订阅类型只能是 actress / series")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("请选择库中的女优或系列")
    table = "actresses" if kind == "actress" else "series"
    if not query_one(conn, "SELECT id FROM %s WHERE name = ?" % table, (name,)):
        raise ValueError("库里没有「%s」" % name)
    if query_one(conn, "SELECT id FROM subscription_targets WHERE kind = ? AND name = ?",
                 (kind, name)):
        raise ValueError("「%s」已订阅过" % name)
    site, page_url = _auto_source(cfg, kind, name)
    item = create_target(conn, {
        "kind": kind, "name": name, "site": site,
        "page_url": page_url, "cover": _library_cover(conn, kind, name),
        "baseline_at": _library_latest_date(conn, kind, name),
    })
    # 「关注并入订阅」：订阅女优即自动关注，关注墙筛选与订阅保持一致
    if kind == "actress":
        conn.execute("UPDATE actresses SET followed = 1 WHERE name = ?", (name,))
    return item


def create_targets_from_library(conn, cfg: Dict[str, Any],
                                payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """批量订阅：默认把库里所有「已关注」女优一次性加入（已在订阅里的跳过）。"""
    payload = payload or {}
    kind = str(payload.get("kind") or "actress").strip().lower()
    if kind != "actress":
        raise ValueError("批量添加目前只支持女优")
    rows = query_all(conn, "SELECT name FROM actresses WHERE followed = 1 ORDER BY name")
    added, skipped, failed = 0, 0, []
    for r in rows:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        try:
            create_target_from_library(conn, cfg, {"kind": kind, "name": name})
            added += 1
        except ValueError as e:
            if "已订阅" in str(e):
                skipped += 1
            else:
                failed.append({"name": name, "reason": str(e)})
                if len(failed) >= 10:   # 站点不可用时没必要把每个名字都试一遍
                    break
    return {"added": added, "skipped": skipped, "failed": failed}


# ------------------------------------------------------------------ 规则
def list_rules(conn, target_id: Optional[int] = None) -> List[Dict[str, Any]]:
    if target_id is None:
        # 全局范围只返回全局规则；单目标返回「全局 + 该目标」，便于前端算覆盖关系
        rows = query_all(
            conn, "SELECT * FROM subscription_rules WHERE target_id IS NULL ORDER BY id")
    else:
        rows = query_all(
            conn, "SELECT * FROM subscription_rules WHERE target_id = ? OR target_id IS NULL "
                  "ORDER BY target_id IS NULL, id", (int(target_id),))
    return [{
        "id": int(r["id"]),
        "target_id": r.get("target_id"),
        "rule_type": r.get("rule_type") or "",
        "op": r.get("op") or "exclude",
        "value": r.get("value") or "",
        "enabled": bool(r.get("enabled")),
    } for r in rows]


#: 允许空值的规则类型：单目标用空值表达「这项不限制」，用于显式覆盖全局
_EMPTY_OK = ("keyword", "exclude_actress", "exclude_code")


def _validate_rule(payload: Dict[str, Any]) -> Dict[str, Any]:
    rule_type = str(payload.get("rule_type") or "").strip()
    if rule_type not in RULE_TYPES:
        raise ValueError("不支持的规则类型")
    value = str(payload.get("value") or "").strip()
    if not value and rule_type not in _EMPTY_OK:
        raise ValueError("规则值不能为空")
    if rule_type == "code_regex":
        if len(value) > 200:
            raise ValueError("正则长度不能超过 200")
        try:
            re.compile(value)
        except re.error as e:
            raise ValueError("正则表达式无效：%s" % e) from e
    if rule_type == "recent_days" and _int(value, 0) < 0:
        raise ValueError("天数不能为负数")
    for t in ("release_after", "release_before"):
        if rule_type == t and not _date10(value):
            raise ValueError("日期格式应为 YYYY-MM-DD")
    return {"rule_type": rule_type, "value": value}


def create_rule(conn, payload: Dict[str, Any]) -> Dict[str, Any]:
    item = _validate_rule(payload)
    target_id = payload.get("target_id")
    if target_id is not None:
        if not get_target(conn, int(target_id)):
            raise KeyError("订阅目标不存在")
        target_id = int(target_id)
    cur = conn.execute(
        "INSERT INTO subscription_rules(target_id, rule_type, op, value, enabled) "
        "VALUES(?,?,?,?,?)",
        (target_id, item["rule_type"], RULE_TYPES[item["rule_type"]], item["value"],
         0 if "enabled" in payload and not _bool(payload.get("enabled")) else 1))
    row = query_one(conn, "SELECT * FROM subscription_rules WHERE id = ?", (int(cur.lastrowid),))
    return {"id": int(row["id"]), "target_id": row.get("target_id"),
            "rule_type": row["rule_type"], "op": row["op"], "value": row["value"],
            "enabled": bool(row["enabled"])}


def update_rule(conn, rule_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = query_one(conn, "SELECT * FROM subscription_rules WHERE id = ?", (int(rule_id),))
    if not row:
        raise KeyError("规则不存在")
    fields: Dict[str, Any] = {}
    if "value" in payload or "rule_type" in payload:
        item = _validate_rule({"rule_type": payload.get("rule_type") or row["rule_type"],
                               "value": payload.get("value", row["value"])})
        fields["rule_type"] = item["rule_type"]
        fields["op"] = RULE_TYPES[item["rule_type"]]
        fields["value"] = item["value"]
    if "enabled" in payload:
        fields["enabled"] = 1 if _bool(payload.get("enabled")) else 0
    if not fields:
        return dict(row)
    cols = ", ".join("%s = ?" % k for k in fields)
    conn.execute("UPDATE subscription_rules SET %s WHERE id = ?" % cols,
                 list(fields.values()) + [int(rule_id)])
    row = query_one(conn, "SELECT * FROM subscription_rules WHERE id = ?", (int(rule_id),))
    return {"id": int(row["id"]), "target_id": row.get("target_id"),
            "rule_type": row["rule_type"], "op": row["op"], "value": row["value"],
            "enabled": bool(row["enabled"])}


def delete_rule(conn, rule_id: int) -> None:
    conn.execute("DELETE FROM subscription_rules WHERE id = ?", (int(rule_id),))


def _compile_rules(rows: Iterable[Dict[str, Any]]) -> Tuple[Dict, Dict]:
    """返回 (全局规则表, 目标规则表)。结构：{rule_type: [rule, ...]}。

    合并语义：同 rule_type 时目标规则整体覆盖全局规则（不叠加），避免
    「全局允许、目标排除」变成谁也说不清；不同 rule_type 之间是「与」。
    """
    global_map: Dict[str, List[Dict[str, Any]]] = {}
    target_map: Dict[int, Dict[str, List[Dict[str, Any]]]] = {}
    for r in rows:
        if not r.get("enabled"):
            continue
        t = r.get("rule_type") or ""
        if t not in RULE_TYPES:
            continue
        compiled = dict(r)
        if t == "code_regex":
            try:
                compiled["_re"] = re.compile(str(r.get("value") or ""))
            except re.error:
                continue
        if r.get("target_id") is None:
            global_map.setdefault(t, []).append(compiled)
        else:
            target_map.setdefault(int(r["target_id"]), {}).setdefault(t, []).append(compiled)
    return global_map, target_map


def _rules_for(target_id: int, global_map: Dict, target_map: Dict) -> Dict[str, List]:
    merged = dict(global_map)
    merged.update(target_map.get(int(target_id)) or {})
    return merged


# ------------------------------------------------------------------ 判定
def _load_derived(conn, code_norms: Iterable[str]) -> Tuple[set, set]:
    """批量派生「库内已有 / 已看」：一次查询，避免每条 findng 都打库。"""
    codes = [c for c in set(code_norms) if c]
    in_lib: set = set()
    watched: set = set()
    # 分块：SQLite 的参数上限可能是 999，未读计数一次会传上千个番号
    for i in range(0, len(codes), 400):
        chunk = codes[i:i + 400]
        marks = ",".join("?" * len(chunk))
        in_lib.update(r["code_norm"] for r in query_all(
            conn, "SELECT DISTINCT code_norm FROM movies WHERE code_norm IN (%s)" % marks, chunk))
        watched.update(r["code_norm"] for r in query_all(
            conn, "SELECT DISTINCT m.code_norm AS code_norm FROM movies m "
                  "LEFT JOIN watch_progress w ON w.movie_id = m.id "
                  "WHERE m.code_norm IN (%s) AND (m.watched = 1 OR w.finished = 1 "
                  "OR (w.duration > 0 AND w.position * 1.0 / w.duration >= 0.9))" % marks, chunk))
    return in_lib, watched


def _eval_one(finding: Dict[str, Any], target: Dict[str, Any], rules: Dict[str, List],
              in_lib: set, watched: set, unknown_policy: str,
              co_star: bool = False) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    code_norm = finding.get("code_norm") or norm_code(finding.get("code"))
    release = finding.get("release_date") or ""
    title = finding.get("title") or ""
    meta = {}
    try:
        meta = json.loads(finding.get("meta_json") or "{}")
    except Exception:
        meta = {}

    # 1) 基线：早于订阅建立日期的作品不进入提醒（仍可被历史视图看到）
    baseline = _date10(target.get("baseline_at"))
    if release and baseline and release < baseline:
        reasons.append("baseline")

    # 2) 已入库：默认不算提醒（要回看请去影片库；「已过滤」里能看到这条原因）
    if code_norm and code_norm in in_lib:
        reasons.append("exclude_in_library")

    # 3) 日期未知：按配置保留或排除，绝不静默丢弃
    if not release and unknown_policy == "exclude":
        reasons.append("unknown_date")

    # 4) include 白名单：存在任一 include 规则时，必须至少命中一条
    includes = [t for t in rules if RULE_TYPES.get(t) == "include"]
    if includes and not reasons:
        hit = False
        for t in includes:
            for rule in rules[t]:
                if t == "release_after" and release and release >= _date10(rule["value"]):
                    hit = True
                elif t == "release_before" and release and release <= _date10(rule["value"]):
                    hit = True
                elif t == "recent_days":
                    days = _int(rule.get("value"), 0)
                    if days <= 0:
                        # 0 = 不限：显式覆盖全局的「只看最近 N 天」
                        hit = True
                    elif release and release >= (date.today() - timedelta(days=days)).isoformat():
                        hit = True
        if not hit:
            reasons.append("not_included")

    # 5) exclude 规则：命中任一即排除
    for t, rule_list in rules.items():
        if RULE_TYPES.get(t) != "exclude":
            continue
        for rule in rule_list:
            if t == "exclude_code":
                hit_codes = {norm_code(v) for v in _values(rule.get("value"))}
                if code_norm and code_norm in hit_codes:
                    reasons.append(t)
            elif t == "code_regex":
                rx = rule.get("_re")
                if rx is not None and code_norm and rx.search(code_norm):
                    reasons.append(t)
            elif t == "keyword":
                low = title.lower()
                if any(v.lower() in low for v in _values(rule.get("value")) if v):
                    reasons.append(t)
            elif t == "exclude_actress":
                names = {str(x).strip() for x in (meta.get("actresses") or [])}
                want = set(_values(rule.get("value")))
                if names & want:
                    reasons.append(t)
            elif t == "exclude_in_library":
                if _bool(rule.get("value")) and code_norm in in_lib:
                    reasons.append(t)
            elif t == "exclude_watched":
                if _bool(rule.get("value")) and code_norm in watched:
                    reasons.append(t)
            elif t == "exclude_ignored":
                if _bool(rule.get("value")) and finding.get("status") == "ignored":
                    reasons.append(t)
            elif t == "exclude_co_star":
                # 共演 / 合集：一条作品里出现两位以上已订阅女优
                if co_star:
                    reasons.append(t)
    # 去重但保持顺序
    seen = set()
    ordered = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            ordered.append(r)
    return (not ordered), ordered


def _targets_by_finding(conn, finding_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    if not finding_ids:
        return {}
    marks = ",".join("?" * len(finding_ids))
    rows = query_all(
        conn,
        "SELECT ft.finding_id, t.id, t.kind, t.name FROM subscription_finding_targets ft "
        "JOIN subscription_targets t ON t.id = ft.target_id "
        "WHERE ft.finding_id IN (%s)" % marks, finding_ids)
    out: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(int(r["finding_id"]), []).append(
            {"id": int(r["id"]), "kind": r.get("kind"), "name": r.get("name")})
    return out


def _evaluate_rows(conn, rows: List[Dict[str, Any]], cfg: Dict[str, Any],
                   extra_rules: Optional[List[Dict[str, Any]]] = None,
                   only_target_id: Optional[int] = None) -> Dict[int, Dict[str, Any]]:
    """对一批发现做规则求值，返回 {finding_id: {visible, reasons, target_names}}。"""
    if not rows:
        return {}
    scfg = cfg.get("subscription") or {}
    unknown_policy = str(scfg.get("unknown_date_policy") or "keep")
    ids = [int(r["id"]) for r in rows]
    with_targets = _targets_by_finding(conn, ids)
    rule_rows = query_all(conn, "SELECT * FROM subscription_rules WHERE enabled = 1")
    if extra_rules:
        rule_rows = [r for r in rule_rows if r.get("target_id") is None] + extra_rules
    global_map, target_map = _compile_rules(rule_rows)
    in_lib, watched = _load_derived(conn, [r.get("code_norm") or "" for r in rows])
    # 目标一次预载，避免每条发现都回查目标表（列表页 200 条 × N 目标会很痛）
    target_rows = {int(t["id"]): t for t in query_all(
        conn, "SELECT * FROM subscription_targets")}
    # 共演判定：作品元信息里出现 ≥2 位「已订阅且启用」的女优
    sub_actresses = {str(t.get("name") or "") for t in target_rows.values()
                     if t.get("kind") == "actress" and _bool(t.get("enabled", 1))}
    result: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        fid = int(r["id"])
        targets = with_targets.get(fid) or []
        if only_target_id is not None:
            targets = [t for t in targets if t["id"] == int(only_target_id)]
        co_names: List[str] = []
        try:
            meta = json.loads(r.get("meta_json") or "{}")
        except Exception:
            meta = {}
        act = meta.get("actresses") or []
        if isinstance(act, list):
            co_names = sorted({str(a) for a in act if str(a) and str(a) in sub_actresses})
        co_star = len(co_names) >= 2
        if not targets:
            # 目标已删除但发现记录保留：不进入提醒列表
            result[fid] = {"visible": False, "reasons": ["no_target"], "targets": [],
                           "co_star": co_star, "co_names": co_names}
            continue
        visible = False
        reasons: List[str] = []
        for t in targets:
            target_row = target_rows.get(int(t["id"]))
            if not target_row:
                continue
            rules = _rules_for(t["id"], global_map, target_map)
            ok, why = _eval_one(r, target_row, rules, in_lib, watched, unknown_policy,
                                co_star=co_star)
            if ok:
                visible = True
                reasons = []
                break
            if not reasons:
                reasons = why
        result[fid] = {"visible": visible, "reasons": reasons, "targets": targets,
                       "co_star": co_star, "co_names": co_names}
    return result


# ------------------------------------------------------------------ 发现列表
def list_findings(conn, status: str = "", target_id: Optional[int] = None,
                  site: str = "", date_from: str = "", date_to: str = "",
                  q: str = "", actress: str = "", sort: str = "release",
                  cursor: str = "", limit: int = 50,
                  cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """发现列表：keyset 分页 + 规则实时求值。

    status: ''/all（不含已忽略、已归档）/ unread / read / ignored / archived / filtered
    q: 番号 / 标题 / 元信息模糊匹配
    actress: 按元信息里的女优名精确筛选
    sort: release（按发行日）/ found（按发现先后）
    """
    cfg = cfg or load_config()
    limit = max(1, min(200, _int(limit, 50)))
    by_found = str(sort or "").strip().lower() in ("found", "first_seen", "new")
    where = ["1=1"]
    args: List[Any] = []
    if target_id is not None:
        where.append("EXISTS (SELECT 1 FROM subscription_finding_targets ft "
                     "WHERE ft.finding_id = f.id AND ft.target_id = ?)")
        args.append(int(target_id))
    if site:
        where.append("f.site = ?")
        args.append(site)
    if date_from:
        where.append("f.release_date >= ?")
        args.append(_date10(date_from))
    if date_to:
        where.append("f.release_date <= ?")
        args.append(_date10(date_to))
    if status == "unread":
        # 未读 = 新条目里「已确认」的：只见过一次的先不算，避免站点偶发脏数据报红点
        where.append("f.status = 'new' AND f.confirm_count >= 2")
    elif status in ("read", "ignored", "archived"):
        where.append("f.status = ?")
        args.append(status)
    elif status in ("", "all", "filtered"):
        where.append("f.status NOT IN ('ignored', 'archived')")
    kw = str(q or "").strip()
    if kw:
        like = "%" + kw.replace("%", "").replace("_", "") + "%"
        where.append("(f.code LIKE ? OR f.title LIKE ? OR f.meta_json LIKE ?)")
        args.extend([like, like, like])
    name = str(actress or "").strip()
    if name:
        # 女优口径有两条来源：列表页给的元信息女优，以及「订阅的就是这位女优」本身
        # （JavBus 列表页不带演员，只靠 meta 会筛不出任何东西）。
        where.append("(EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(f.meta_json) "
                     "THEN f.meta_json ELSE '{}' END, '$.actresses') je WHERE je.value = ?) "
                     "OR EXISTS (SELECT 1 FROM subscription_finding_targets fta "
                     "JOIN subscription_targets ta ON ta.id = fta.target_id "
                     "WHERE fta.finding_id = f.id AND ta.kind = 'actress' AND ta.name = ?))")
        args.extend([name, name])
    if cursor:
        if by_found:
            try:
                where.append("f.id < ?")
                args.append(int(cursor))
            except (ValueError, TypeError):
                pass
        else:
            try:
                c_date, c_id = cursor.split("|", 1)
                where.append("(f.release_date < ? OR (f.release_date = ? AND f.id < ?))")
                args.extend([c_date, c_date, int(c_id)])
            except (ValueError, TypeError):
                pass
    fetch = min(300, max(limit * 4, limit))
    order = "f.id DESC" if by_found else "f.release_date DESC, f.id DESC"
    sql = ("SELECT f.* FROM subscription_findings f WHERE %s "
           "ORDER BY %s LIMIT ?" % (" AND ".join(where), order))
    rows = query_all(conn, sql, args + [fetch])
    evals = _evaluate_rows(conn, rows, cfg, only_target_id=target_id)
    in_lib, watched = _load_derived(conn, [r.get("code_norm") or "" for r in rows])

    items = []
    last_row = None
    for r in rows:
        fid = int(r["id"])
        ev = evals.get(fid) or {"visible": False, "reasons": [], "targets": []}
        visible = bool(ev["visible"])
        if status == "archived":
            # 归档是用户明确放进来的「老片」桶，不再套用「只看最近 N 天」之类的规则
            visible = True
        if status == "filtered":
            if visible:
                continue
        elif not visible:
            continue
        if status in ("", "all") and r.get("status") == "ignored":
            continue
        names = [t.get("name") or "" for t in (ev.get("targets") or [])]
        cn = r.get("code_norm") or ""
        items.append(_finding_public(r, [n for n in names if n], visible,
                                     ev.get("reasons") or [], cn in in_lib, cn in watched,
                                     ev.get("co_names") or []))
        last_row = r
        if len(items) >= limit:
            break

    next_cursor = ""
    has_more = len(rows) >= fetch
    if has_more or (len(items) >= limit and last_row is not None):
        # 过滤可能让本批一条都不剩：只要后面还有数据，游标就得推进到本批末尾，
        # 否则前端点「加载更多」会一直从同一位置重取。
        anchor = last_row or rows[-1]
        next_cursor = (str(anchor["id"]) if by_found
                       else "%s|%s" % (anchor.get("release_date") or "", anchor["id"]))
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


def list_finding_actresses(conn, target_id: Optional[int] = None,
                           limit: int = 200) -> List[Dict[str, Any]]:
    """发现里的女优聚合（女优筛选下拉用），按出现次数降序。

    来源取并集：订阅目标本身是女优的（主来源，JavBus 列表无演员信息）+ 元信息里的女优。
    """
    extra = ""
    args: List[Any] = []
    if target_id is not None:
        extra = (" AND EXISTS (SELECT 1 FROM subscription_finding_targets fx "
                 "WHERE fx.finding_id = f.id AND fx.target_id = ?)")
        args.append(int(target_id))
    sql = ("SELECT name, COUNT(*) AS n FROM ("
           "SELECT f.id AS fid, t.name AS name FROM subscription_findings f "
           "JOIN subscription_finding_targets ft ON ft.finding_id = f.id "
           "JOIN subscription_targets t ON t.id = ft.target_id "
           "WHERE f.status <> 'ignored' AND t.kind = 'actress' AND t.name <> ''%s "
           "UNION "
           "SELECT f.id AS fid, je.value AS name FROM subscription_findings f, "
           "json_each(CASE WHEN json_valid(f.meta_json) THEN f.meta_json ELSE '{}' END, "
           "'$.actresses') je WHERE f.status <> 'ignored' AND je.value <> ''%s"
           ") GROUP BY name ORDER BY n DESC, name LIMIT ?") % (extra, extra)
    rows = query_all(conn, sql, args + args + [max(1, min(500, _int(limit, 200)))])
    return [{"name": r.get("name") or "", "count": int(r.get("n") or 0)} for r in rows]


def unread_count(conn) -> int:
    """未读角标：规则实时求值 + 只算上次查看新片列表之后进来的，30 秒内存缓存。"""
    now = time.time()
    with _UNREAD_LOCK:
        if now - float(_UNREAD_CACHE.get("at") or 0) < 30:
            return int(_UNREAD_CACHE.get("value") or 0)
    cfg = load_config()
    seen_at = last_seen_at(conn)
    rows = query_all(conn, "SELECT * FROM subscription_findings "
                           "WHERE status = 'new' AND confirm_count >= 2 "
                           "ORDER BY release_date DESC, id DESC LIMIT 1000")
    # 已经进页面看过的最新一批不再计入角标；单条是否已读由用户自己标
    if seen_at:
        rows = [r for r in rows if str(r.get("first_seen_at") or "") > seen_at]
    evals = _evaluate_rows(conn, rows, cfg)
    count = sum(1 for r in rows if (evals.get(int(r["id"])) or {}).get("visible"))
    with _UNREAD_LOCK:
        _UNREAD_CACHE.update({"at": now, "value": count})
    return int(count)


_LAST_SEEN_KEY = "findings_last_seen_at"


def last_seen_at(conn) -> str:
    row = query_one(conn, "SELECT value FROM subscription_state WHERE key = ?",
                    (_LAST_SEEN_KEY,))
    return str((row or {}).get("value") or "")


def mark_seen(conn) -> str:
    """记录「新片列表已查看」的时间点，顶部徽章据此清零。"""
    at = _now()
    conn.execute(
        "INSERT INTO subscription_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (_LAST_SEEN_KEY, at))
    _invalidate_unread()
    return at


def _invalidate_unread() -> None:
    with _UNREAD_LOCK:
        _UNREAD_CACHE["at"] = 0.0


# ------------------------------------------------------------------ 单条操作
def _get_finding(conn, finding_id: int) -> Dict[str, Any]:
    row = query_one(conn, "SELECT * FROM subscription_findings WHERE id = ?", (int(finding_id),))
    if not row:
        raise KeyError("发现记录不存在")
    return row


def mark_read(conn, finding_id: int) -> Dict[str, Any]:
    _get_finding(conn, finding_id)
    conn.execute("UPDATE subscription_findings SET status = 'read', read_at = ? "
                 "WHERE id = ? AND status = 'new'", (_now(), int(finding_id)))
    _invalidate_unread()
    return {"ok": True}


def mark_unread(conn, finding_id: int) -> Dict[str, Any]:
    """手动标回未读（方便把还想再看一遍的条目标出来）。"""
    _get_finding(conn, finding_id)
    conn.execute("UPDATE subscription_findings SET status = 'new', read_at = '' "
                 "WHERE id = ?", (int(finding_id),))
    _invalidate_unread()
    return {"ok": True}


def ignore(conn, finding_id: int) -> Dict[str, Any]:
    _get_finding(conn, finding_id)
    conn.execute("UPDATE subscription_findings SET status = 'ignored', ignored_at = ? "
                 "WHERE id = ?", (_now(), int(finding_id)))
    _invalidate_unread()
    return {"ok": True}


def restore(conn, finding_id: int) -> Dict[str, Any]:
    """取消忽略：恢复为未读。"""
    _get_finding(conn, finding_id)
    conn.execute("UPDATE subscription_findings SET status = 'new', ignored_at = '' "
                 "WHERE id = ?", (int(finding_id),))
    _invalidate_unread()
    return {"ok": True}


def save_finding(conn, finding_id: int) -> Dict[str, Any]:
    """一键入库：复用 ingest.link 建在线记录（页面地址作为来源）。"""
    f = _get_finding(conn, finding_id)
    from . import ingest
    res = ingest.link(conn, {
        "code": f.get("code") or "",
        "title": f.get("title") or "",
        "page_url": f.get("page_url") or "",
        "site": f.get("site") or "",
        "site_id": f.get("site_id") or "",
    })
    if not res.get("ok"):
        return res
    conn.execute("UPDATE subscription_findings SET status = 'read', read_at = ? "
                 "WHERE id = ? AND status = 'new'", (_now(), int(finding_id)))
    _invalidate_unread()
    res["finding_id"] = int(finding_id)
    return res


def bulk_findings(conn, ids: List[Any], action: str) -> Dict[str, Any]:
    """批量改状态：已读 / 未读 / 忽略 / 恢复 / 归档。

    一次最多 500 条：列表本身是分页的，超过这个量说明前端传错了。
    """
    action = str(action or "").strip().lower()
    if action not in ("read", "unread", "ignore", "restore", "archive"):
        raise ValueError("不支持的批量操作")
    uniq = list(dict.fromkeys(int(i) for i in (ids or []) if str(i).strip()))[:500]
    if not uniq:
        return {"ok": True, "updated": 0}
    marks = ",".join("?" * len(uniq))
    now = _now()
    if action == "read":
        cur = conn.execute("UPDATE subscription_findings SET status = 'read', read_at = ? "
                           "WHERE id IN (%s) AND status = 'new'" % marks, [now] + uniq)
    elif action == "unread":
        cur = conn.execute("UPDATE subscription_findings SET status = 'new', read_at = '' "
                           "WHERE id IN (%s) AND status <> 'ignored'" % marks, uniq)
    elif action == "ignore":
        cur = conn.execute("UPDATE subscription_findings SET status = 'ignored', ignored_at = ? "
                           "WHERE id IN (%s)" % marks, [now] + uniq)
    elif action == "restore":
        cur = conn.execute("UPDATE subscription_findings SET status = 'new', ignored_at = '' "
                           "WHERE id IN (%s) AND status IN ('ignored', 'archived')" % marks, uniq)
    else:  # archive
        cur = conn.execute("UPDATE subscription_findings SET status = 'archived' "
                           "WHERE id IN (%s) AND status IN ('new', 'read')" % marks, uniq)
    _invalidate_unread()
    return {"ok": True, "updated": int(cur.rowcount or 0)}


def resources(conn, finding_id: int) -> Dict[str, Any]:
    """资源候选（P1 只做 link：作品页 / 站点搜索页 / 库内来源）。"""
    f = _get_finding(conn, finding_id)
    code = f.get("code") or ""
    page_url = f.get("page_url") or ""
    site = f.get("site") or ""
    out: List[Dict[str, Any]] = []
    if page_url:
        out.append({"source": site or "page", "title": "作品页", "kind": "page",
                    "page_url": page_url, "magnet": "", "seeders": 0})
    if site == "javbus" and code:
        out.append({"source": "javbus", "title": "JavBus 搜索", "kind": "search",
                    "page_url": "https://www.javbus.com/search/%s" % code,
                    "magnet": "", "seeders": 0})
    elif site == "javdb" and code:
        out.append({"source": "javdb", "title": "JavDB 搜索", "kind": "search",
                    "page_url": "https://javdb.com/search?q=%s&f=all" % code,
                    "magnet": "", "seeders": 0})
    if f.get("code_norm"):
        rows = query_all(
            conn,
            "SELECT s.*, m.id AS mid FROM movie_sources s JOIN movies m ON m.id = s.movie_id "
            "WHERE m.code_norm = ? AND s.available = 1 ORDER BY s.is_primary DESC, s.id LIMIT 20",
            (f["code_norm"],))
        for r in rows:
            if not r.get("page_url"):
                continue
            out.append({"source": r.get("site") or "library", "title": r.get("title") or "库内来源",
                        "kind": "library", "page_url": r.get("page_url") or "",
                        "play_url": r.get("play_url") or "", "magnet": "", "seeders": 0,
                        "movie_id": int(r["mid"])})
    return {"code": code, "items": out}


def preview_rules(conn, payload: Dict[str, Any]) -> Dict[str, Any]:
    """规则 dry-run：只对现有发现做本地求值，不触发扫描。"""
    cfg = load_config()
    target_id = payload.get("target_id")
    rules_payload = payload.get("rules")
    extra: Optional[List[Dict[str, Any]]] = None
    if isinstance(rules_payload, list):
        extra = []
        for r in rules_payload:
            try:
                item = _validate_rule(r)
            except ValueError:
                continue
            extra.append({"target_id": target_id, "enabled": 1, **item})
    rows = query_all(conn, "SELECT * FROM subscription_findings "
                           "ORDER BY release_date DESC, id DESC LIMIT 2000")
    evals = _evaluate_rows(conn, rows, cfg, extra_rules=extra,
                           only_target_id=int(target_id) if target_id is not None else None)
    reasons: Dict[str, int] = {}
    visible = filtered = unknown = 0
    blocked: List[Dict[str, Any]] = []
    for r in rows:
        ev = evals.get(int(r["id"])) or {}
        if not r.get("release_date"):
            unknown += 1
        if ev.get("visible"):
            visible += 1
        else:
            filtered += 1
            why = ev.get("reasons") or ["no_target"]
            for item in why:
                reasons[item] = reasons.get(item, 0) + 1
            # 前 N 条被挡明细：让用户看见「哪些片子因为哪条规则没提醒」，而不是只有一个总数
            if len(blocked) < 12:
                blocked.append({
                    "id": int(r["id"]),
                    "code": r.get("code") or "",
                    "title": r.get("title") or "",
                    "release_date": r.get("release_date") or "",
                    "cover": r.get("cover") or "",
                    "reasons": why,
                })
    return {"total": len(rows), "visible": visible, "filtered": filtered,
            "unknown": unknown, "blocked": blocked,
            "reasons": [{"rule": k, "count": v}
                        for k, v in sorted(reasons.items(), key=lambda x: -x[1])]}


# ------------------------------------------------------------------ 扫描
def _target_provider(target: Dict[str, Any], cfg: Dict[str, Any]):
    site = str(target.get("site") or "").lower()
    cls = providers.REGISTRY.get(site)
    if not cls:
        return None
    try:
        inst = cls(cfg)
    except Exception:
        return None
    return inst if inst.supports_list() else None


def _headers(cfg: Dict[str, Any], site: str, url: str) -> Dict[str, str]:
    scfg = cfg.get("scraper") or {}
    options = (scfg.get(site) or {}) if site else {}
    headers = {
        "User-Agent": scfg.get("user_agent") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    headers.update(options.get("headers") or {})
    cookie = options.get("cookie") or options.get("cookies") or ""
    if isinstance(cookie, str) and cookie.strip():
        headers["Cookie"] = cookie.strip()
    return headers


def _fetch_page(url: str, cfg: Dict[str, Any], site: str,
                etag: str = "", last_modified: str = "") -> Dict[str, Any]:
    """条件请求：返回 {not_modified, text, etag, last_modified} 或抛 NetError。"""
    if not url:
        raise NetError("没有可抓取的列表地址")
    scfg = cfg.get("scraper") or {}
    options = (scfg.get(site) or {}) if site else {}
    headers = _headers(cfg, site, url)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    timeout = _int(options.get("timeout"), _int(scfg.get("timeout"), 20))
    proxy = scfg.get("proxy") or ""
    resp = None
    for attempt in range(2):
        try:
            resp = http_session().get(url, headers=headers, timeout=timeout,
                                      proxies=proxies_for(url, override=proxy),
                                      verify=outbound_verify_tls())
        except Exception as e:  # noqa: BLE001
            raise NetError("请求失败：%s" % str(e)[:120]) from e
        # JavBus 短时间内连抓多个目标会偶发 403/429：等几秒重试一次再判定拦截
        if resp.status_code in (403, 429) and attempt == 0:
            time.sleep(4)
            continue
        break
    if resp.status_code == 304:
        return {"not_modified": True, "text": "", "etag": etag,
                "last_modified": last_modified}
    if resp.status_code in (403, 429):
        raise NetBlocked("HTTP %d：站点拒绝或限流（可配置代理 / Cookie）" % resp.status_code)
    if resp.status_code >= 400:
        raise NetError("HTTP %d" % resp.status_code)
    # requests 对 text/html 默认 ISO-8859-1，中文站会乱码；只有响应头显式声明
    # charset 时才信它，否则按 UTF-8 解（JavBus / JavDB 都是 UTF-8）。
    if "charset=" not in (resp.headers.get("Content-Type") or "").lower():
        resp.encoding = "utf-8"
    return {"not_modified": False, "text": resp.text,
            "etag": resp.headers.get("ETag") or etag,
            "last_modified": resp.headers.get("Last-Modified") or last_modified}


def _parse_generic_list(html: str, base_url: str, site: str) -> List[Dict[str, Any]]:
    """手动目标页的通用解析：只认「链接文本 / 地址里的番号」这一种稳定信号。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    seen = set()
    out: List[Dict[str, Any]] = []
    for a in soup.select("a[href]"):
        text = a.get_text(" ", strip=True)
        href = urljoin(base_url, str(a.get("href") or ""))
        m = _CODE_RE.search(text) or _CODE_RE.search(href)
        if not m:
            continue
        code = m.group(1).upper()
        if code in seen:
            continue
        seen.add(code)
        img = a.select_one("img")
        cover = str((img.get("src") or img.get("data-src") or "") if img else "").strip()
        if cover.startswith("/"):
            cover = urljoin(base_url, cover)
        title = str((img.get("title") if img else "") or text or code).strip()
        date = _date10(text) or _date10(title)
        out.append({"code": code, "title": title, "release_date": date, "cover": cover,
                    "page_url": href, "site": site or "manual", "site_id": code})
    return out


def _known_codes(conn, codes: List[str]) -> set:
    if not codes:
        return set()
    uniq = list(dict.fromkeys(codes))
    marks = ",".join("?" * len(uniq))
    rows = query_all(conn, "SELECT code_norm FROM subscription_findings "
                           "WHERE code_norm IN (%s)" % marks, uniq)
    return {r["code_norm"] for r in rows}


def _touch_target(target_id: int, status: str = "", etag: Optional[str] = None,
                  last_modified: Optional[str] = None, scanned: bool = True) -> None:
    fields = ["last_status = ?"]
    args: List[Any] = [status[:200]]
    if scanned:
        fields.append("last_scan_at = ?")
        args.append(_now())
    if etag is not None:
        fields.append("etag = ?")
        args.append(etag or "")
    if last_modified is not None:
        fields.append("last_modified = ?")
        args.append(last_modified or "")
    args.append(int(target_id))
    with db() as conn:
        conn.execute("UPDATE subscription_targets SET %s WHERE id = ?" % ", ".join(fields), args)


def _series_alias_key(name: str) -> str:
    return _ALIAS_NOISE_RE.sub("", str(name or "")).lower()


def _series_alias_get(conn, site: str, name: str) -> Optional[Dict[str, str]]:
    """查已记住的系列页（同站 + 去装饰符后的名字）。"""
    key = _series_alias_key(name)
    if not key:
        return None
    return query_one(conn, "SELECT site_id, page_url FROM subscription_series_alias "
                           "WHERE site = ? AND name_norm = ?", (site, key))


def _series_alias_save(conn, site: str, name: str, fixed: Dict[str, Any]) -> None:
    """把「系列名 → 站内系列页」记下来，下次直接命中，不再切词搜索。"""
    key = _series_alias_key(name)
    site_id = str((fixed or {}).get("site_id") or "").strip()
    if not key or not site_id:
        return
    conn.execute(
        "INSERT INTO subscription_series_alias(site, name_norm, site_id, page_url, updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(site, name_norm) DO UPDATE SET "
        "site_id = excluded.site_id, page_url = excluded.page_url, "
        "updated_at = excluded.updated_at",
        (site, key, site_id, str((fixed or {}).get("page_url") or ""), _now()))


def scan_target(target: Dict[str, Any], cfg: Dict[str, Any],
                mode: str = "") -> Dict[str, Any]:
    """扫描单个目标：抓列表 → 批量落库 → 建立归属。返回统计。

    ``mode="backfill"`` 是「历史补抓」：翻页与条数上限放宽，且不因为连续命中
    已知番号就提前停（日常增量巡检要停，补老片恰恰要往后翻）。
    """
    scfg = cfg.get("subscription") or {}
    backfill = str(mode or "").strip().lower() == "backfill"
    pages = max(1, _int(scfg.get("backfill_pages" if backfill else "max_pages_per_target"),
                        12 if backfill else 3))
    max_items = max(1, _int(scfg.get("backfill_items" if backfill else "max_items_per_target"),
                         500 if backfill else 100))
    streak_stop = max(1, _int(scfg.get("known_streak_stop"), 10))
    baseline = _date10(target.get("baseline_at"))
    site = str(target.get("site") or "manual").lower()
    inst = _target_provider(target, cfg)
    etag = str(target.get("etag") or "")
    last_modified = str(target.get("last_modified") or "")

    collected: List[Dict[str, Any]] = []
    streak = 0
    stopped_early = False
    streak_stopped = False
    capped = False
    reached_baseline = False
    pages_done = 0
    aliased = False
    for page in range(1, pages + 1):
        if page == 1 and inst is not None and str(target.get("kind") or "") == "series":
            # 系列别名：上次已经纠偏出「/series/<id>」的，直接沿用，不再切词搜索
            with db() as conn:
                alias = _series_alias_get(conn, site, target.get("name"))
            if alias and alias.get("site_id"):
                target = dict(target)
                target.update({"site_id": alias.get("site_id"),
                               "page_url": alias.get("page_url") or ""})
                aliased = True
                with db() as conn:
                    conn.execute(
                        "UPDATE subscription_targets SET site_id = ?, page_url = ? WHERE id = ?",
                        (target.get("site_id") or "", target.get("page_url") or "",
                         int(target["id"])))
        if page == 1 and inst is not None and not aliased:
            # 地址自动纠偏（如系列名带「（厂商）」导致 JavBus 搜索 403）：纠偏后落库
            normalizer = getattr(inst, "normalize_list_target", None)
            fixed = normalizer(target) if normalizer else None
            if fixed:
                target = dict(target)
                target.update({k: v for k, v in fixed.items() if v})
                with db() as conn:
                    conn.execute(
                        "UPDATE subscription_targets SET site_id = ?, page_url = ? WHERE id = ?",
                        (target.get("site_id") or "", target.get("page_url") or "",
                         int(target["id"])))
                    if str(target.get("kind") or "") == "series":
                        _series_alias_save(conn, site, target.get("name"), fixed)
        if inst is not None:
            url = inst.list_url(target, page)
        else:
            if page > 1:
                break
            url = str(target.get("page_url") or "")
        fetched = _fetch_page(url, cfg, site, etag if page == 1 else "",
                              last_modified if page == 1 else "")
        if page == 1 and inst is not None and not aliased:
            # /searchstar/{名字} 只是女優搜索结果，真正作品列表在 /star/{id}。
            # 解析出真正的作品页后落库，后续扫描直接用它（翻页也不再抓搜索页）。
            resolver = getattr(inst, "resolve_list_target", None)
            fixed = resolver(target, fetched.get("text") or "") if resolver else None
            if fixed:
                target = dict(target)
                target.update({k: v for k, v in fixed.items() if v})
                with db() as conn:
                    conn.execute(
                        "UPDATE subscription_targets SET site_id = ?, page_url = ? WHERE id = ?",
                        (target.get("site_id") or "", target.get("page_url") or "",
                         int(target["id"])))
                    if str(target.get("kind") or "") == "series":
                        _series_alias_save(conn, site, target.get("name"), fixed)
                fetched = _fetch_page(inst.list_url(target, page), cfg, site)
        if fetched.get("not_modified"):
            _touch_target(target["id"], "未变化（页面未更新）", etag=etag,
                          last_modified=last_modified)
            return {"fetched": 0, "new": 0, "not_modified": True, "pages": page - 1,
                    "reason": "not_modified"}
        if page == 1:
            etag = fetched.get("etag") or ""
            last_modified = fetched.get("last_modified") or ""
        if inst is not None:
            items = inst.parse_list(fetched["text"], target) or []
        else:
            items = _parse_generic_list(fetched["text"], url, site)
        if not items:
            break

        with db() as conn:
            known = _known_codes(conn, [norm_code(i.get("code")) for i in items])
            lib = _library_codes(conn, [norm_code(i.get("code")) for i in items])
        for it in items:
            code_norm = norm_code(it.get("code"))
            if code_norm and code_norm in known:
                streak += 1
            else:
                streak = 0
            if streak >= streak_stop and not backfill:
                # 日常增量：连续命中已知番号说明已经追到上次的位置
                stopped_early = True
                streak_stopped = True
                break
            if code_norm and code_norm in lib:
                continue   # 已入库的作品不算「新片」，不进提醒
            rel = _date10(it.get("release_date"))
            if rel and baseline and rel < baseline:
                reached_baseline = True
                continue
            collected.append(it)
            if len(collected) >= max_items:
                stopped_early = True
                capped = True
                break
        pages_done = page
        if stopped_early or reached_baseline:
            break
        if page < pages:
            time.sleep(1.0 + (page % 3) * 0.5)   # 单站串行 + 页间抖动，别打站点

    # 批量 upsert + 归属（一个事务）
    new_count = linked = 0
    with db() as conn:
        codes = [norm_code(i.get("code")) for i in collected if norm_code(i.get("code"))]
        existing = _known_codes(conn, codes)
        new_count = len({c for c in codes if c not in existing})
        for it in collected:
            code = str(it.get("code") or "").strip()
            code_norm = norm_code(code)
            if not code_norm:
                continue
            meta = {k: it.get(k) for k in ("actresses", "series", "studio", "tags")
                    if it.get(k)}
            conn.execute(
                "INSERT INTO subscription_findings(code, code_norm, title, release_date, "
                "cover, page_url, site, site_id, meta_json, confirm_count) "
                "VALUES(?,?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(code_norm) DO UPDATE SET "
                "title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE title END, "
                "release_date = CASE WHEN excluded.release_date <> '' "
                "THEN excluded.release_date ELSE release_date END, "
                "cover = CASE WHEN excluded.cover <> '' THEN excluded.cover ELSE cover END, "
                "page_url = CASE WHEN excluded.page_url <> '' THEN excluded.page_url "
                "ELSE page_url END, "
                "meta_json = CASE WHEN excluded.meta_json <> '{}' THEN excluded.meta_json "
                "ELSE meta_json END, "
                # 第二轮还看到同一条 → 确认；封顶 9，避免老片每轮都 +1
                "confirm_count = MIN(confirm_count + 1, 9)",
                (code, code_norm, str(it.get("title") or ""), _date10(it.get("release_date")),
                 str(it.get("cover") or ""), str(it.get("page_url") or ""),
                 str(it.get("site") or site), str(it.get("site_id") or ""),
                 json.dumps(meta, ensure_ascii=False) if meta else "{}"))
            row = query_one(conn, "SELECT id FROM subscription_findings WHERE code_norm = ?",
                            (code_norm,))
            if row:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO subscription_finding_targets(finding_id, target_id) "
                    "VALUES(?,?)", (int(row["id"]), int(target["id"])))
                linked += max(0, cur.rowcount)
        if new_count:
            status_text = "新增 %d 条" % new_count
            if reached_baseline:
                status_text += "，已到起始日期"
            elif streak_stopped:
                status_text += "，已追到旧片"
        elif reached_baseline:
            status_text = "无新片（已到起始日期）"
        elif streak_stopped:
            status_text = "无新片（已追到旧片）"
        else:
            status_text = "无新片"
        if capped:
            status_text += "，达到单轮上限"
        if backfill:
            status_text = "补抓：" + status_text
        conn.execute("UPDATE subscription_targets SET last_scan_at = ?, last_status = ?, "
                     "etag = ?, last_modified = ? WHERE id = ?",
                     (_now(), status_text, etag, last_modified, int(target["id"])))
    _invalidate_unread()
    if streak_stopped:
        reason = "streak"
    elif reached_baseline:
        reason = "baseline"
    elif capped:
        reason = "capped"
    else:
        reason = "exhausted"
    return {"fetched": len(collected), "new": new_count, "linked": linked,
            "not_modified": False, "pages": pages_done, "reason": reason}


def scan_all(progress=None, should_cancel=None,
             target_ids: Optional[List[int]] = None,
             mode: str = "") -> Dict[str, Any]:
    """扫描订阅目标（默认全部启用目标）。单目标失败只记状态，不中断整批。

    ``target_ids`` 非空时只扫这些目标（不看 enabled）：新增订阅后只检查这一个，
    不用等整批跑完。``mode="backfill"`` 走历史补抓（见 ``scan_target``）。
    """
    cfg = load_config(refresh=True)
    scfg = cfg.get("subscription") or {}
    mode = str(mode or "").strip().lower()
    if mode not in ("", "backfill"):
        raise NetError("不支持的扫描模式")
    if not _bool(scfg.get("enabled", True)):
        raise NetError("订阅功能已关闭")
    ids = [int(i) for i in (target_ids or [])]
    with db() as conn:
        if ids:
            marks = ",".join("?" * len(ids))
            targets = query_all(
                conn,
                "SELECT * FROM subscription_targets WHERE id IN (%s) "
                "ORDER BY last_scan_at ASC, id" % marks,
                ids,
            )
        else:
            targets = query_all(conn, "SELECT * FROM subscription_targets WHERE enabled = 1 "
                                      "ORDER BY last_scan_at ASC, id")
    total = len(targets)
    stats = {"targets": total, "fetched": 0, "new": 0, "failed": 0,
             "skipped": 0, "archived": 0}
    fail_limit = max(1, _int(scfg.get("site_cooldown_fails"), 3))
    cooldown_min = max(1, _int(scfg.get("site_cooldown_minutes"), 120))
    guard = threading.Lock()
    counter = {"done": 0}

    def _bump(key: str, n: int = 1) -> None:
        with guard:
            stats[key] = int(stats.get(key) or 0) + int(n)

    def _report(idx: int, message: str) -> None:
        if not progress:
            return
        with guard:
            snapshot = dict(stats)
        progress(idx, total, message, snapshot)

    def _scan_group(site: str, items: List[Dict[str, Any]]) -> None:
        """同一站点的目标串行（保持礼貌间隔），不同站点之间由线程池并行。"""
        for i, t in enumerate(items):
            if should_cancel and should_cancel():
                return
            name = t.get("name") or t.get("site_id") or ""
            cd = _COOLDOWN.get(site)
            if cd and float(cd.get("until") or 0) > time.time():
                _bump("skipped")
                with guard:
                    counter["done"] += 1
                    done = counter["done"]
                _report(done, "跳过（%s 冷却中）：%s" % (site, name))
                continue
            with guard:
                done = counter["done"]
            # 进度按「已完成数」上报：进入该目标时还没算完成
            _report(done, "扫描：%s" % name)
            try:
                res = scan_target(t, cfg, mode=mode)
                _bump("fetched", int(res.get("fetched") or 0))
                _bump("new", int(res.get("new") or 0))
                _COOLDOWN.pop(site, None)
            except NetBlocked as e:
                _bump("failed")
                _bump_cooldown(site, fail_limit, cooldown_min)
                _touch_target(t["id"], "blocked:%s" % str(e))
            except NetError as e:
                _bump("failed")
                _bump_cooldown(site, fail_limit, cooldown_min)
                _touch_target(t["id"], "error:%s" % str(e))
            except Exception as e:  # noqa: BLE001 单目标失败不影响其他目标
                _bump("failed")
                _touch_target(t["id"], "error:%s" % str(e)[:120])
            with guard:
                counter["done"] += 1
                done = counter["done"]
            _report(done, "完成：%s" % name)
            if i < len(items) - 1:
                time.sleep(2.0)   # 站内串行抓取：间隔太短会触发 403 限流

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in targets:
        groups.setdefault(str(t.get("site") or "manual").lower(), []).append(t)
    if len(groups) <= 1:
        for site, items in groups.items():
            _scan_group(site, items)
    else:
        # 跨站并行最多 3 组：再多出口 IP 和本地线程都吃不消
        with ThreadPoolExecutor(max_workers=min(3, len(groups))) as pool:
            futures = [pool.submit(_scan_group, site, items)
                       for site, items in groups.items()]
            for fut in futures:
                try:
                    fut.result()
                except Exception:  # noqa: BLE001 组内异常已在 _scan_group 处理
                    pass
    _invalidate_unread()
    if not ids:
        # 整理归档：已读超过 N 天的发现不再占新片列表（只做全量扫描时顺手清）
        days = _int(scfg.get("archive_read_days"), 0)
        if days > 0:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            with db() as conn:
                cur = conn.execute(
                    "UPDATE subscription_findings SET status = 'archived' "
                    "WHERE status = 'read' AND read_at <> '' AND read_at < ?", (cutoff,))
                stats["archived"] = int(cur.rowcount or 0)
    return stats


def _bump_cooldown(site: str, fails: int, minutes: int) -> None:
    item = _COOLDOWN.setdefault(site, {"count": 0, "until": 0.0})
    item["count"] = int(item.get("count") or 0) + 1
    if item["count"] >= fails:
        item["until"] = time.time() + minutes * 60
        item["count"] = 0


def cooldowns() -> Dict[str, Any]:
    """当前冷却状态（供诊断 / 目标列表展示）。"""
    now = time.time()
    return {k: {"until": int(v.get("until") or 0), "left_sec": max(0, int((v.get("until") or 0) - now))}
            for k, v in _COOLDOWN.items() if (v.get("until") or 0) > now}


