# -*- coding: utf-8 -*-
"""联网搜索技能：给 agent 一个「上网查资料」的工具。

定位（为什么这样做，而不是内置一个默认搜索引擎）：
* 查询词（番号、女优名）属于敏感信息，默认**不联网**——这和片匣「本地优先」一致；
* 想用就得显式配置：SearXNG 实例地址（推荐自建，`docker run searxng/searxng`）
  或 Tavily API Key。两个都没配时，这个工具等于不存在，agent 不会报错只会说没配。

流程：搜索 → 取前几条 → 逐条抓正文（bs4 抽文本）→ 截断后交给模型汇总。
模型只负责「对齐与转述」，结论必须带来源；抓不到就说抓不到。

礼貌与稳健：
* 单实例内存缓存 30 分钟（同一问题反复问不再打站点）；
* 串行抓取、每次请求之间至少间隔 1 秒、读超时 10 秒；
* 单页正文截断，避免把几十 KB 的网页塞进模型。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from .config import load_config

log = logging.getLogger("avm.ai.websearch")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7"}
_TIMEOUT = 10.0
_SNIPPET_CHARS = 400          # 搜索结果摘要截断
_PAGE_CHARS = 1600            # 单页正文截断（给模型的量）
_CACHE_TTL = 1800             # 搜索结果缓存 30 分钟
_MIN_GAP = 1.0                # 两次搜索之间的最小间隔（对公共实例友好）

_CACHE: Dict[str, Tuple[float, List[Dict[str, str]]]] = {}
_CACHE_LOCK = threading.Lock()
_LAST_SEARCH = 0.0
_GAP_LOCK = threading.Lock()

_BLOCK_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form")


def _ai_cfg(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return (cfg or load_config(refresh=True)).get("ai") or {}


def backend(cfg: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """返回 (后端类型, 配置值)。都没配时返回 ('', 提示语)。"""
    ai = _ai_cfg(cfg)
    url = str(ai.get("websearch_url") or "").strip().rstrip("/")
    key = str(ai.get("websearch_key") or "").strip()
    if url:
        return "searxng", url
    if key:
        return "tavily", key
    return "", "未配置联网搜索：设置 → AI 中心 → 联网搜索，填 SearXNG 地址或 Tavily Key"


def enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    return bool(backend(cfg)[0])


# ------------------------------------------------------------------ 搜索

def _search_searxng(base: str, query: str, limit: int) -> List[Dict[str, str]]:
    url = "%s/search?q=%s&format=json" % (base, quote_plus(query))
    resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    if resp.status_code == 403:
        raise RuntimeError("该 SearXNG 实例未开启 JSON 输出（返回 403）："
                           "在 settings.yml 的 search.formats 里加上 json，或换一个实例")
    if resp.status_code == 429:
        raise RuntimeError("搜索被限流（429）：稍后再试，或自建实例")
    resp.raise_for_status()
    data = resp.json()
    out: List[Dict[str, str]] = []
    for r in (data.get("results") or [])[:limit]:
        title = str(r.get("title") or "").strip()
        link = str(r.get("url") or "").strip()
        if not link:
            continue
        out.append({"title": title or link, "url": link,
                    "snippet": str(r.get("content") or "")[:_SNIPPET_CHARS]})
    return out


def _search_tavily(key: str, query: str, limit: int) -> List[Dict[str, str]]:
    resp = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": key, "query": query, "max_results": limit,
              "search_depth": "basic"},
        timeout=_TIMEOUT,
    )
    if resp.status_code in (401, 403):
        raise RuntimeError("Tavily Key 无效或已过期（HTTP %d）" % resp.status_code)
    if resp.status_code == 429:
        raise RuntimeError("Tavily 免费额度用完或被限流（429）")
    resp.raise_for_status()
    data = resp.json()
    out: List[Dict[str, str]] = []
    for r in (data.get("results") or [])[:limit]:
        link = str(r.get("url") or "").strip()
        if not link:
            continue
        out.append({"title": str(r.get("title") or "").strip() or link, "url": link,
                    "snippet": str(r.get("content") or "")[:_SNIPPET_CHARS]})
    return out


def search(cfg: Optional[Dict[str, Any]], query: str, limit: int = 5) -> List[Dict[str, str]]:
    """搜索一次，带缓存与节流。失败抛 RuntimeError（错误信息可直接给用户看）。"""
    kind, value = backend(cfg)
    if not kind:
        raise RuntimeError(value)
    q = str(query or "").strip()
    if not q:
        return []

    cache_key = "%s|%s|%d" % (kind, q, limit)
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]

    global _LAST_SEARCH
    with _GAP_LOCK:
        wait = _MIN_GAP - (time.time() - _LAST_SEARCH)
        if wait > 0:
            time.sleep(wait)
        _LAST_SEARCH = time.time()

    if kind == "searxng":
        results = _search_searxng(value, q, max(1, min(8, limit)))
    else:
        results = _search_tavily(value, q, max(1, min(8, limit)))

    with _CACHE_LOCK:
        if len(_CACHE) > 200:                     # 简单的容量上限，避免长期驻留
            _CACHE.clear()
        _CACHE[cache_key] = (time.time(), results)
    return results


# ------------------------------------------------------------------ 抓正文

def _extract(html: str) -> Tuple[str, str]:
    """从 HTML 里抽 (标题, 正文)。只保留主文本，脚本/导航/页脚一律丢弃。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_BLOCK_TAGS):
        tag.decompose()
    title = ""
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = str(og["content"]).strip()
    if not title and soup.title and soup.title.string:
        title = str(soup.title.string).strip()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return title, text[:_PAGE_CHARS]


def fetch_text(url: str) -> Dict[str, str]:
    """抓一个页面并抽正文。失败不抛异常——返回 error 字段让模型自己决定要不要提。"""
    try:
        if not re.match(r"^https?://", str(url or "")):
            raise ValueError("不是 http(s) 链接")
        resp = requests.get(str(url), headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        ctype = str(resp.headers.get("content-type") or "")
        if "html" not in ctype and "xml" not in ctype:
            return {"url": url, "error": "不是网页（%s）" % ctype.split(";")[0]}
        title, text = _extract(resp.text)
        if len(text) < 40:
            return {"url": url, "error": "页面正文太短或需要登录/执行脚本"}
        return {"url": url, "title": title, "text": text}
    except Exception as e:                        # noqa: BLE001 单页失败不能拖垮整个查询
        return {"url": url, "error": "%s" % str(e)[:120]}


# ------------------------------------------------------------------ 技能入口

def lookup(cfg: Optional[Dict[str, Any]], query: str, limit: int = 3) -> Dict[str, Any]:
    """搜索 + 抓正文，聚合后交给模型。这就是 agent 的 ``web_lookup`` 工具本体。"""
    kind, _ = backend(cfg)
    if not kind:
        raise RuntimeError(backend(cfg)[1])
    results = search(cfg, query, limit=max(3, limit + 2))[: max(1, min(6, limit + 2))]

    pages: List[Dict[str, str]] = []
    for r in results[:max(1, min(4, limit))]:
        pages.append(fetch_text(r["url"]))
        time.sleep(0.3)                           # 对目标站点温和一点

    return {
        "backend": kind,
        "query": query,
        "results": results,
        "pages": pages,
        "hint": "结论必须标注来自哪条结果（给域名即可）；抓不到正文的结果只引用摘要，不要编造。",
    }


def check(cfg: Optional[Dict[str, Any]], query: str = "test") -> Dict[str, Any]:
    """连通性自检：给设置页的「测试连接」按钮用，错误信息都说人话。"""
    kind, value = backend(cfg)
    if not kind:
        return {"ok": False, "backend": "", "message": value}
    try:
        results = search(cfg, query or "test", limit=3)
        sample = results[0]["title"] if results else ""
        return {"ok": bool(results), "backend": kind,
                "message": "可用，示例结果：%s" % sample if results else "连通，但没有返回结果"}
    except Exception as e:                        # noqa: BLE001 自检的目的就是把错误说清楚
        return {"ok": False, "backend": kind, "message": str(e)[:300]}


def status(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    kind, value = backend(cfg)
    return {"enabled": bool(kind), "backend": kind,
            "configured": bool(value) if kind else False}
