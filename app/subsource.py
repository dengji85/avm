# -*- coding: utf-8 -*-
"""在线字幕源：按番号检索候选字幕（P0 两源：SubtitleCat / 迅雷）。

设计要点（都是实测出来的坑）：
1. 只把「番号字符串」发出去，绝不上传文件名、路径、视频或音频。
2. 迅雷的 oracle/subtitle 会静默返回空 data（同一番号连打 5 次可能是
   [0, 11, 11, 11, 0]），空结果一律算 degraded 而不是 miss，避免把限流
   误判成「这部片没有字幕」。
3. 结果标题归一化后必须真的包含番号，否则丢弃（SubtitleCat 列表页夹着
   上百条热门推荐，直接取会配错片）。
4. 每个源独立限流 / 超时 / 失败降级，任何一个挂了都不影响另一个。

这是未公开接口，无 SLA：所有源都必须可关、可替换，失败不能影响主流程。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List
from urllib.parse import quote, unquote, urljoin, urlparse

from . import subqc
from .config import outbound_verify_tls

THUNDER_API = "https://api-shoulei-ssl.xunlei.com/oracle/subtitle"
SUBCAT_BASE = "https://www.subtitlecat.com/"
ALLOWED_HOSTS = {
    "api-shoulei-ssl.xunlei.com",
    "subtitle.v.geilijiasu.com",
    "www.subtitlecat.com",
    "subtitlecat.com",
}
PROVIDER_LABELS = {"thunder": "迅雷", "subtitlecat": "SubtitleCat"}
MAX_BYTES = 4_000_000

_LOCK = threading.Lock()
_LAST_CALL: Dict[str, float] = {}


def _sub_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    s = dict(cfg.get("subtitles") or {})
    s.setdefault("enabled", True)
    s.setdefault("providers", ["subtitlecat", "thunder"])
    s.setdefault("timeout", 15)
    s.setdefault("delay_ms", 800)
    s.setdefault("max_candidates", 24)
    s.setdefault("proxy", "")
    s.setdefault("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0.0.0 Safari/537.36")
    # 字幕源没有单独配代理时沿用刮削代理，避免用户配了两遍
    if not s["proxy"]:
        s["proxy"] = (cfg.get("scraper") or {}).get("proxy") or ""
    return s


def _throttle(provider: str, delay_ms: int) -> None:
    """每个源一个时间轴，锁内只排号、锁外等待，避免串行阻塞其它源。"""
    step = max(0, int(delay_ms or 0)) / 1000.0
    with _LOCK:
        now = time.time()
        scheduled = max(now, _LAST_CALL.get(provider, 0.0) + step)
        _LAST_CALL[provider] = scheduled
    wait = scheduled - now
    if wait > 0:
        time.sleep(wait)


def _request(url: str, scfg: Dict[str, Any], *, binary: bool = False,
             timeout: int | None = None):
    """统一出站 GET：默认校验证书、复用代理、失败由调用方降级。"""
    import requests

    proxy = scfg.get("proxy") or ""
    _throttle(_provider_of(url), int(scfg.get("delay_ms") or 0))
    resp = requests.get(
        url,
        timeout=int(timeout or scfg.get("timeout") or 15),
        headers={"User-Agent": scfg.get("user_agent") or "Mozilla/5.0",
                 "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                 "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        proxies={"http": proxy, "https": proxy} if proxy else None,
        verify=outbound_verify_tls(),
    )
    resp.raise_for_status()
    return resp.content if binary else resp.text


def _provider_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "subtitlecat" in host:
        return "subtitlecat"
    if "xunlei" in host or "geilijiasu" in host:
        return "thunder"
    return host


def _norm_code(s: str) -> str:
    """番号归一化，仅用于「结果标题是否真的包含该番号」的硬校验。"""
    return "".join(c for c in (s or "").upper() if c.isalnum())


def _degraded(reason: str, message: str) -> Dict[str, Any]:
    return {"status": "degraded", "reason": reason, "message": message, "candidates": []}


def _ok(cands: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"status": "ok", "reason": "", "message": "", "candidates": cands}


def _thunder_lang(item: Dict[str, Any]) -> str:
    langs = " ".join(str(x) for x in (item.get("languages") or []))
    simple = "简体" in langs or "简" in langs
    trad = "繁体" in langs or "繁" in langs
    english = "英" in langs or "English" in langs or "eng" in langs.lower()
    japanese = "日" in langs or "Japanese" in langs
    if simple and (english or japanese):
        return "日中双语" if japanese else "中文"
    if simple:
        return "简体中文"
    if trad:
        return "繁体中文"
    if japanese:
        return "日文"
    if english:
        return "英文"
    return "未知"


def search_thunder(code: str, scfg: Dict[str, Any]) -> Dict[str, Any]:
    """迅雷：按文件名精确查（实测 IPX343 去掉连字符直接返回 0，必须用原样番号）。"""
    import json
    import requests

    norm = _norm_code(code)
    variants = [code, code + ".mp4"]
    cands: List[Dict[str, Any]] = []
    empties = 0
    try:
        for variant in variants:
            data = json.loads(_request(THUNDER_API + "?name=" + quote(variant), scfg))
            if data.get("code") != 0:
                return _degraded("api_error", "迅雷字幕接口返回异常，可稍后重试")
            rows = data.get("data") or []
            if not rows:
                empties += 1
                continue
            for it in rows:
                url = it.get("url") or ""
                name = it.get("name") or ""
                if not url or not name:
                    continue
                # 结果必须真的属于这个番号：库里混着一堆以 GCID 命名的重复上传
                hay = _norm_code(name + " " + (it.get("simple_name") or ""))
                if norm not in hay:
                    continue
                ext = (it.get("ext") or "srt").lower()
                cands.append({
                    "provider": "thunder", "provider_label": PROVIDER_LABELS["thunder"],
                    "name": name, "url": url, "ext": ext,
                    "lang": _thunder_lang(it),
                    "duration_ms": int(it.get("duration") or 0),
                    "size": int(it.get("size") or 0),
                    "upload": "（网友上传）" if "网友上传" in (it.get("extra_name") or "") else "",
                })
            if cands:
                return _ok(cands)
        if empties >= len(variants):
            # 空结果也可能是被限流，绝不判成 miss
            return _degraded("empty", "迅雷未返回字幕（该接口偶发返回空，建议稍后重试）")
        return _ok([])
    except (requests.RequestException, ValueError, TypeError) as exc:
        return _degraded("network", "迅雷字幕源暂不可用：%s" % type(exc).__name__)


# 只看文件名「最后一个」语言后缀：基名里常带 zh-CN（原字幕语言），
# 例如 IPX-343.zh-zh-CN-en.srt 是英文译文，全文搜 zh-CN 会误判成简中。
_LANG_SUFFIX = {
    "zh-cn": "简体中文", "zh-hans": "简体中文", "hans": "简体中文", "chs": "简体中文",
    "zh-tw": "繁体中文", "zh-hant": "繁体中文", "hant": "繁体中文", "cht": "繁体中文",
    "zh-ja": "日中双语", "zh": "中文", "chi": "中文",
    "ja": "日文", "jpn": "日文",
    "en": "英文", "eng": "英文", "en-us": "英文",
}
_LANG_SUFFIX_SORTED = sorted(_LANG_SUFFIX.items(), key=lambda kv: -len(kv[0]))


def _subcat_lang(filename: str) -> str:
    stem = unquote(filename).rsplit(".", 1)[0].lower()
    for suf, lang in _LANG_SUFFIX_SORTED:
        if stem == suf or stem.endswith(("-" + suf, "_" + suf, "." + suf, " " + suf)):
            return lang
    return "未知"


def _detail_duration(html: str) -> int:
    """详情页底部形如「1096 01:55:12」（条数 + 时长），能拿到就用于版本贴合。"""
    pos = html.find("Original subtitles")
    tail = html[pos:] if pos >= 0 else html
    m = re.search(r"\b(\d{1,2}):(\d{2}):(\d{2})\b", re.sub(r"<[^>]+>", " ", tail))
    if not m:
        return 0
    sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return sec * 1000 if 60 < sec < 6 * 3600 else 0


def search_subtitlecat(code: str, scfg: Dict[str, Any]) -> Dict[str, Any]:
    """SubtitleCat：列表页 → 详情页 → 按语言挑直链 .srt。"""
    import requests

    norm = _norm_code(code)
    try:
        html = _request(SUBCAT_BASE + "index.php?search=" + quote(code), scfg)
        hrefs = re.findall(r'href="(subs/\d+/[^"]+?\.html)"', html, re.I)
        pages: List[str] = []
        for href in hrefs:
            title = unquote(href.rsplit("/", 1)[-1])[:-5]
            if norm in _norm_code(title) and href not in pages:
                pages.append(href)
            if len(pages) >= 4:
                break
        if not pages:
            return {"status": "miss", "reason": "no_result",
                    "message": "该站没有此番号的字幕", "candidates": []}
        cands: List[Dict[str, Any]] = []
        for href in pages:
            try:
                detail = _request(urljoin(SUBCAT_BASE, href), scfg)
            except (requests.RequestException, ValueError):
                continue            # 单个详情页失败不影响其它结果
            duration = _detail_duration(detail)
            links = re.findall(r'href="(/subs/\d+/[^"]+?\.(?:srt|ass|vtt))"', detail, re.I)
            picked = 0
            for link in links:
                fname = unquote(link.rsplit("/", 1)[-1])
                lang = _subcat_lang(fname)
                if lang == "未知":
                    continue
                cands.append({
                    "provider": "subtitlecat",
                    "provider_label": PROVIDER_LABELS["subtitlecat"],
                    "name": fname, "url": urljoin(SUBCAT_BASE, link),
                    "ext": fname.rsplit(".", 1)[-1].lower(),
                    "lang": lang, "duration_ms": duration, "size": 0,
                })
                picked += 1
                if picked >= 2:      # 每个结果最多取简中/繁中两条，别把 100 种语言塞进菜单
                    break
        if not cands:
            return _degraded("parse", "SubtitleCat 页面结构变化，未能解析下载链接")
        return _ok(cands)
    except (requests.RequestException, ValueError, TypeError) as exc:
        return _degraded("network", "SubtitleCat 暂不可用：%s" % type(exc).__name__)


PROVIDERS = {"thunder": search_thunder, "subtitlecat": search_subtitlecat}


def search_all(code: str, cfg: Dict[str, Any], video_seconds: float = 0) -> Dict[str, Any]:
    """按番号并行查所有启用的源，合并候选、去重、预估分并排序。"""
    scfg = _sub_cfg(cfg)
    if not scfg.get("enabled", True):
        return {"code": code, "candidates": [], "sources": [], "video_seconds": video_seconds,
                "disabled": True}
    names = [n for n in (scfg.get("providers") or []) if n in PROVIDERS]
    results: Dict[str, Dict[str, Any]] = {}
    if names:
        pool = ThreadPoolExecutor(max_workers=min(2, len(names)))
        try:
            futures = {pool.submit(PROVIDERS[n], code, scfg): n for n in names}
            for fut, name in futures.items():
                try:
                    results[name] = fut.result(timeout=int(scfg.get("timeout") or 15) * 3 + 10)
                except Exception as exc:
                    results[name] = _degraded("network", "%s 查询超时：%s"
                                              % (PROVIDER_LABELS.get(name, name), type(exc).__name__))
        finally:
            # 某个源卡住时不拖住整个请求：线程自行收尾，主线程立刻返回已拿到的结果
            pool.shutdown(wait=False)
    cands: List[Dict[str, Any]] = []
    for name in names:
        cands.extend(results.get(name, {}).get("candidates") or [])
    cands = subqc.dedupe(cands, limit=int(scfg.get("max_candidates") or 24))
    for c in cands:
        c["estimate"] = subqc.estimate_score(c, video_seconds)
        c["id"] = hashlib.sha1((c["provider"] + "|" + c["url"]).encode("utf-8")).hexdigest()[:12]
    cands.sort(key=lambda c: (-c["estimate"], c["provider"], c["name"]))
    sources = [{
        "provider": n, "label": PROVIDER_LABELS.get(n, n),
        "status": results.get(n, {}).get("status", "degraded"),
        "reason": results.get(n, {}).get("reason", ""),
        "message": results.get(n, {}).get("message", ""),
        "count": len(results.get(n, {}).get("candidates") or []),
    } for n in names]
    return {"code": code, "candidates": cands, "sources": sources,
            "video_seconds": video_seconds}


def download(cand: Dict[str, Any], cfg: Dict[str, Any]) -> bytes:
    """下载字幕字节；域名白名单在路由层再校验一次，这里做二次防护。"""
    url = cand.get("url") or ""
    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError("字幕地址不在允许的来源域名内")
    scfg = _sub_cfg(cfg)
    raw = _request(url, scfg, binary=True, timeout=30)
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("字幕内容为空或超过 4 MB")
    head = raw[:256].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html")):
        raise ValueError("下载到的是网页而不是字幕文件")
    return raw
