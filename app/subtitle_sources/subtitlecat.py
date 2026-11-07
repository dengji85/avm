# -*- coding: utf-8 -*-
"""SubtitleCat：列表页 → 详情页 → 按语言挑直链 .srt / .ass / .vtt。

这是聚合型字幕库，语种非常多。旧实现会丢掉「语言后缀认不出来」的字幕、
且每个详情页只取前 2 条，冷门番号明明有日文/其它语种字幕也看不见。
现在全部保留（语言只用于评分，绝不过滤），由编排层择优。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import quote, unquote, urljoin

import requests

from .common import (code_matches, degraded, ext_of, lang_of_name,
                     make_cand, miss, ok, request)

SUBCAT_BASE = "https://www.subtitlecat.com/"


def _detail_duration(html: str) -> int:
    """详情页底部形如「1096 01:55:12」（条数 + 时长），能拿到就用于版本贴合。"""
    pos = html.find("Original subtitles")
    tail = html[pos:] if pos >= 0 else html
    m = re.search(r"\b(\d{1,2}):(\d{2}):(\d{2})\b", re.sub(r"<[^>]+>", " ", tail))
    if not m:
        return 0
    sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return sec * 1000 if 60 < sec < 6 * 3600 else 0


def search(variant: str, scfg: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    variants = ctx.get("variants") or [variant]
    per_page = max(1, min(12, int(scfg.get("per_page") or 6)))
    max_pages = max(1, min(8, int(scfg.get("max_pages") or 4)))
    try:
        html = request(SUBCAT_BASE + "index.php?search=" + quote(variant), scfg,
                       provider="subtitlecat")
        hrefs = re.findall(r'href="(subs/\d+/[^"]+?\.html)"', html, re.I)
        pages: List[str] = []
        for href in hrefs:
            title = unquote(href.rsplit("/", 1)[-1])[:-5]
            # 列表页夹着上百条热门推荐，只留标题真的含番号的
            if code_matches(title, variants) and href not in pages:
                pages.append(href)
            if len(pages) >= max_pages:
                break
        if not pages:
            return miss()

        cands: List[Dict[str, Any]] = []
        for href in pages:
            try:
                detail = request(urljoin(SUBCAT_BASE, href), scfg, provider="subtitlecat")
            except (requests.RequestException, ValueError):
                continue            # 单个详情页失败不影响其它结果
            duration = _detail_duration(detail)
            links = re.findall(r'href="(/subs/\d+/[^"]+?\.(?:srt|ass|ssa|vtt))"',
                               detail, re.I)
            seen = set()
            picked = 0
            for link in links:
                if link in seen:
                    continue
                seen.add(link)
                fname = unquote(link.rsplit("/", 1)[-1])
                lang = lang_of_name(fname)
                cands.append(make_cand(
                    "subtitlecat", fname, urljoin(SUBCAT_BASE, link),
                    lang=lang, ext=ext_of(fname), duration_ms=duration,
                ))
                picked += 1
                if picked >= per_page:
                    break
        if not cands:
            return degraded("parse", "SubtitleCat 页面结构变化，未能解析下载链接")
        return ok(cands)
    except (requests.RequestException, ValueError, TypeError) as exc:
        return degraded("network", "SubtitleCat 暂不可用：%s" % type(exc).__name__)
