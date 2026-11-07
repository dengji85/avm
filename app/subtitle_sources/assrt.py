# -*- coding: utf-8 -*-
"""伪射手 ASSRT 官方 API：中文 / 双语字幕补充源。

搜索：GET /v1/sub/search?token=&q=番号
下载：GET /v1/sub/detail?token=&id= → filelist[] 里的单文件直链
      （主 url 常是 rar/zip 压缩包，系统只吃文本字幕，所以优先取 onthefly 直链）

需要用户到 assrt.net 免费申请 Token；未配置时源明确显示「未启用」。
配额 20 次/分钟（token 与 IP 共享），所以搜索阶段只发一次请求，
只有用户真正采纳某条字幕时才会再发一次 detail。
"""
from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import quote, urlencode

from .common import (code_matches, degraded, ext_of, json_request,
                     make_cand, miss, off, ok)

API = "https://api.assrt.net/v1"
_TEXT_EXT = ("srt", "ass", "ssa", "vtt", "sub")


def _lang_of(item: Dict[str, Any]) -> str:
    lang = item.get("lang") or {}
    desc = str((lang.get("desc") if isinstance(lang, dict) else lang) or "").lower()
    if "简" in desc:
        return "简体中文"
    if "繁" in desc:
        return "繁体中文"
    if "日" in desc and "中" in desc:
        return "日中双语"
    if "英" in desc:
        return "英文"
    if "日" in desc:
        return "日文"
    if "双" in desc or "中" in desc:
        return "中文"
    if "韩" in desc:
        return "韩文"
    return "其他语言"


def _token(scfg: Dict[str, Any]) -> str:
    return str(scfg.get("assrt_token") or "").strip()


def _auth(scfg: Dict[str, Any]) -> Dict[str, str]:
    # Token 走请求头而不是 URL 查询串，避免出现在代理 / 站点访问日志里
    return {"Authorization": "Bearer " + _token(scfg)}


def search(variant: str, scfg: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    import requests

    if not _token(scfg):
        return off("未配置伪射手 ASSRT Token")
    variants = ctx.get("variants") or [variant]
    url = API + "/sub/search?" + urlencode({"q": variant, "cnt": 15})
    try:
        data = json_request(url, scfg, headers=_auth(scfg), provider="assrt")
        if not isinstance(data, dict):
            return degraded("api_error", "ASSRT 返回异常，可稍后重试")
        if int(data.get("status") or 0) != 0:
            return degraded("api_error", "ASSRT 查询失败，可能是 Token 无效或配额用尽")
        rows = ((data.get("sub") or {}).get("subs") or [])
        cands: List[Dict[str, Any]] = []
        for it in rows:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            name = str(it.get("videoname") or it.get("native_name") or variant)
            hay = name + " " + str(it.get("native_name") or "")
            if not code_matches(hay, variants):
                continue
            sub_id = str(it["id"])
            cands.append(make_cand(
                "assrt", name, API + "/sub/detail?id=" + quote(sub_id),
                lang=_lang_of(it), ext=ext_of(name),
                sub_id=sub_id,
                votes=it.get("vote_score") or 0,
                upload=str(it.get("release_site") or ""),
            ))
        return ok(cands) if cands else miss()
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        return degraded("network", "ASSRT 暂不可用：%s" % type(exc).__name__)


def resolve(cand: Dict[str, Any], scfg: Dict[str, Any]) -> str:
    """换出真正的文本字幕直链；压缩包里的文本文件优先。"""
    if not _token(scfg):
        raise ValueError("未配置伪射手 ASSRT Token")
    sub_id = str(cand.get("sub_id") or "")
    if not sub_id.isdigit():
        raise ValueError("字幕编号无效")
    url = API + "/sub/detail?" + urlencode({"id": sub_id})
    data = json_request(url, scfg, headers=_auth(scfg), provider="assrt")
    rows = ((data or {}).get("sub") or {}).get("subs") or []
    item = rows[0] if rows and isinstance(rows[0], dict) else {}
    for f in (item.get("filelist") or []):
        if not isinstance(f, dict):
            continue
        fname = str(f.get("f") or "")
        if ext_of(fname, "") in _TEXT_EXT and f.get("url"):
            return _https(str(f["url"]))
    main = str(item.get("url") or "")
    if ext_of(main, "") in _TEXT_EXT:
        return _https(main)
    raise ValueError("该字幕是压缩包格式，暂不支持自动解压，请到源站手动下载")


def _https(url: str) -> str:
    """文档示例里的下载地址是明文 http，统一走 TLS。"""
    return "https://" + url[7:] if url.startswith("http://") else url
