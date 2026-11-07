# -*- coding: utf-8 -*-
"""在线字幕源注册表：一个站点一个模块，新增源只要在这里登记。

每个模块提供 search(variant, scfg, ctx) -> {status, reason, message, candidates}；
可选提供 resolve(cand, scfg) -> 真实下载 URL（用于下载链接需要临时换取的源）。

编排、并行、超时、去重、评分、下载白名单都在 app/subsource.py，
站点解析不要写进编排层。
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import assrt, custom, opensubtitles, subtitlecat, thunder

LABELS = {
    "thunder": "迅雷",
    "subtitlecat": "SubtitleCat",
    "opensubtitles": "OpenSubtitles",
    "assrt": "伪射手 ASSRT",
    "custom": "自定义字幕源",
}

#: 需要用户填凭据才能启用的源 -> 配置项与提示；没配就明确标 off，不假装查过
CREDENTIALS = {
    "opensubtitles": ("opensubtitles_key", "未配置 OpenSubtitles API Key"),
    "assrt": ("assrt_token", "未配置伪射手 ASSRT Token"),
}

REGISTRY = {
    "thunder": thunder,
    "subtitlecat": subtitlecat,
    "opensubtitles": opensubtitles,
    "assrt": assrt,
    "custom": custom,
}

#: 允许下载的域名（精确匹配）与域名后缀。下载链接常落在 CDN 上，
#: 后缀只给受信任的官方域，避免把后端当成任意 URL 的代理。
ALLOWED_HOSTS = {
    "api-shoulei-ssl.xunlei.com", "subtitle.v.geilijiasu.com",
    "www.subtitlecat.com", "subtitlecat.com",
    "api.assrt.net", "assrt.net", "api.makedie.me",
    "api.opensubtitles.com", "www.opensubtitles.com", "www.opensubtitles.org",
    "dl.opensubtitles.com", "dl.opensubtitles.org",
}
ALLOWED_SUFFIXES = (".assrt.net", ".opensubtitles.com", ".opensubtitles.org",
                    ".subtitlecat.com")


def label_of(provider: str, cand: Dict[str, Any]) -> str:
    """自定义源可以有多个实例，展示名跟着实例走。"""
    if provider.startswith("custom"):
        return str(cand.get("source_name") or LABELS["custom"])
    return LABELS.get(provider, provider)


def enabled_names(scfg: Dict[str, Any]) -> List[str]:
    """本次参与查询的源；自定义源按配置里的实例展开。"""
    names: List[str] = []
    for name in (scfg.get("providers") or []):
        name = str(name or "").strip()
        if name == "custom":
            for i, spec in enumerate(scfg.get("custom_sources") or []):
                if isinstance(spec, dict) and spec.get("enabled", True):
                    names.append("custom:%d" % i)
        elif name in REGISTRY and name not in names:
            names.append(name)
    return names


def module_of(name: str):
    return REGISTRY.get(name.split(":", 1)[0])


def credential_error(name: str, scfg: Dict[str, Any]) -> str:
    """返回需要凭据但没配时的提示；不需要凭据或已配好则返回空串。"""
    base = name.split(":", 1)[0]
    item = CREDENTIALS.get(base)
    if item and not str(scfg.get(item[0]) or "").strip():
        return item[1]
    if base == "custom":
        try:
            idx = int(name.split(":", 1)[1])
            spec = (scfg.get("custom_sources") or [])[idx]
        except (IndexError, ValueError, TypeError):
            return "自定义字幕源配置无效"
        if not (isinstance(spec, dict) and str(spec.get("url") or "").strip()):
            return "自定义字幕源没有填搜索地址"
        if not str(spec.get("hosts") or "").strip():
            return "自定义字幕源没有填允许下载的域名"
    return ""


def label_for_status(name: str, scfg: Dict[str, Any]) -> str:
    if name.startswith("custom:"):
        try:
            spec = (scfg.get("custom_sources") or [])[int(name.split(":", 1)[1])]
            return str((spec or {}).get("name") or name)
        except (IndexError, ValueError, TypeError):
            pass
    return LABELS.get(name, name)
