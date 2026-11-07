# -*- coding: utf-8 -*-
"""迅雷字幕（未公开接口）：按文件名精确查，中文网友上传为主。

实测同一个番号连打 5 次可能返回 [0, 11, 11, 11, 0]，空结果一律算
degraded 而不是 miss，避免把限流误判成「这部片没有字幕」。
"""
from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import quote

import requests

from .common import code_matches, degraded, json_request, make_cand, ok

THUNDER_API = "https://api-shoulei-ssl.xunlei.com/oracle/subtitle"


def _lang(item: Dict[str, Any]) -> str:
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
    return "其他语言"


def search(variant: str, scfg: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """只查一个番号写法；变体的调度由编排层负责。"""
    variants = ctx.get("variants") or [variant]
    cands: List[Dict[str, Any]] = []
    empties = 0
    try:
        # 该接口按文件名匹配，带扩展名的写法常常比纯番号命中率高
        for name in (variant, variant + ".mp4"):
            data = json_request(THUNDER_API + "?name=" + quote(name), scfg,
                                provider="thunder")
            if not isinstance(data, dict):
                return degraded("api_error", "迅雷字幕接口返回异常，可稍后重试")
            if data.get("code") != 0:
                return degraded("api_error", "迅雷字幕接口返回异常，可稍后重试")
            rows = data.get("data") or []
            if not rows:
                empties += 1
                continue
            for it in rows:
                if not isinstance(it, dict):
                    continue
                url = it.get("url") or ""
                title = it.get("name") or ""
                if not url or not title:
                    continue
                # 库里混着一堆以 GCID 命名的重复上传，标题必须真的属于这个番号
                hay = title + " " + str(it.get("simple_name") or "")
                if not code_matches(hay, variants):
                    continue
                cands.append(make_cand(
                    "thunder", title, url, lang=_lang(it),
                    ext=str(it.get("ext") or "srt").lower(),
                    duration_ms=it.get("duration") or 0,
                    size=it.get("size") or 0,
                    upload="（网友上传）" if "网友上传" in str(it.get("extra_name") or "") else "",
                ))
            if cands:
                return ok(cands)
        if empties >= 2:
            return degraded("empty", "迅雷未返回字幕（该接口偶发返回空，建议稍后重试）")
        return ok([])
    except (requests.RequestException, ValueError, TypeError) as exc:
        return degraded("network", "迅雷字幕源暂不可用：%s" % type(exc).__name__)
