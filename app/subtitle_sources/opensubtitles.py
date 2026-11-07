# -*- coding: utf-8 -*-
"""OpenSubtitles.com 官方 REST API：全球最大的多语言字幕库。

对冷门番号，中文字幕往往不存在，但日文 / 英文 / 其它语种字幕常在这里；
登记后系统已有的 AI 翻译可以把它们译成中文，所以任何语种都保留。

搜索：GET /api/v1/subtitles?query=番号
下载：POST /api/v1/download {"file_id": ...} → 临时 link（每次现取，不缓存）
需要用户到 opensubtitles.com 免费申请 API Key。
"""
from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import quote, urlencode

from .common import (as_list, code_matches, degraded, ext_of, json_request,
                     lang_of_code, make_cand, miss, ok)

API = "https://api.opensubtitles.com/api/v1"
UA = "AVM SubtitleSearch v1.0"


def _headers(scfg: Dict[str, Any]) -> Dict[str, str]:
    return {
        "Api-Key": str(scfg.get("opensubtitles_key") or ""),
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def search(variant: str, scfg: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    import requests

    variants = ctx.get("variants") or [variant]
    limit = max(1, min(40, int(scfg.get("max_per_source") or 20)))
    url = API + "/subtitles?" + urlencode({"query": variant,
                                           "order_by": "download_count"})
    try:
        data = json_request(url, scfg, headers=_headers(scfg),
                            provider="opensubtitles")
        if not isinstance(data, dict):
            return degraded("api_error", "OpenSubtitles 返回异常，可稍后重试")
        rows = as_list(data.get("data"))
        cands: List[Dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            a = item.get("attributes") or {}
            files = [f for f in (a.get("files") or []) if isinstance(f, dict)]
            if not files:
                continue
            feat = a.get("feature_details") or {}
            hay = " ".join(str(x or "") for x in (
                a.get("release"), feat.get("title"), feat.get("original_title"),
                *(f.get("file_name") for f in files),
            ))
            # 该接口是模糊搜索，返回一堆不相干的片子，标题必须真的属于这个番号
            if not code_matches(hay, variants):
                continue
            lang = lang_of_code(a.get("language"))
            for f in files[:2]:
                fid = f.get("file_id")
                if not fid:
                    continue
                name = str(f.get("file_name") or a.get("release") or variant)
                cands.append(make_cand(
                    "opensubtitles", name,
                    API + "/download?file_id=" + quote(str(fid)),
                    lang=lang, ext=ext_of(name),
                    size=a.get("file_size") or 0,
                    file_id=str(fid),
                    votes=a.get("votes") or 0,
                    downloads=a.get("download_count") or 0,
                ))
            if len(cands) >= limit:
                break
        return ok(cands[:limit]) if cands else miss()
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        return degraded("network", "OpenSubtitles 暂不可用：%s" % type(exc).__name__)


def resolve(cand: Dict[str, Any], scfg: Dict[str, Any]) -> str:
    """下载前现换临时链接；链接有时效，绝不写进候选缓存。"""
    fid = str(cand.get("file_id") or "")
    if not fid.isdigit():
        raise ValueError("字幕编号无效")
    data = json_request(API + "/download", scfg, headers=_headers(scfg),
                        method="POST", json_body={"file_id": int(fid)},
                        provider="opensubtitles")
    link = (data or {}).get("link") if isinstance(data, dict) else None
    if not link:
        raise ValueError("OpenSubtitles 未返回下载地址，可能是下载配额用尽")
    return str(link)
