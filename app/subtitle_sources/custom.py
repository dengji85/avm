# -*- coding: utf-8 -*-
"""自定义 JSON 字幕源：用户自己接任意字幕 API，不必改代码。

配置（subtitles.custom_sources[]）：
  name    显示名
  url     搜索地址，{code} 会被替换成 URL 编码后的番号写法
  root    结果数组在 JSON 里的路径，如 "data.items"，留空表示整个响应
  fields  字段名映射：{"name": "title", "url": "download_url",
                       "lang": "language", "ext": "format"}
  hosts   允许下载的域名，逗号分隔（必填，防止把后端当任意 URL 代理）
  headers 额外请求头（可选，用来放 API Key）
  require_code  是否要求结果标题含番号（默认 false，由源自己保证）

安全：搜索地址只能用 http/https，下载域名必须显式列在 hosts 里。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import quote, urlparse

from .common import (as_list, code_matches, degraded, dig, ext_of, json_request,
                     lang_of_code, lang_of_name, make_cand, miss, off, ok)


def _spec(scfg: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    try:
        spec = (scfg.get("custom_sources") or [])[int(ctx.get("custom_index") or 0)]
    except (IndexError, ValueError, TypeError):
        return {}
    return spec if isinstance(spec, dict) else {}


def _host_list(spec: Dict[str, Any]) -> List[str]:
    raw = spec.get("hosts") or ""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = str(raw).replace("，", ",").split(",")
    out = []
    for h in items:
        h = re.sub(r"^https?://", "", h.strip().lower()).split("/")[0].split(":")[0]
        if h and h not in out:
            out.append(h)
    return out


def search(variant: str, scfg: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    import requests

    spec = _spec(scfg, ctx)
    tmpl = str(spec.get("url") or "").strip()
    if not tmpl:
        return off("自定义字幕源没有填搜索地址")
    if not _host_list(spec):
        return off("自定义字幕源没有填允许下载的域名")
    url = tmpl.replace("{code}", quote(variant))
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return off("自定义字幕源的搜索地址不是合法的 http(s) URL")
    variants = ctx.get("variants") or [variant]
    fields = spec.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    headers = spec.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}
    try:
        data = json_request(url, scfg, headers={str(k): str(v) for k, v in headers.items()},
                            provider="custom:%s" % ctx.get("custom_index"))
        rows = dig(data, str(spec.get("root") or "")) if spec.get("root") else data
        cands: List[Dict[str, Any]] = []
        for row in as_list(rows):
            if not isinstance(row, dict):
                continue
            name = str(dig(row, str(fields.get("name") or "name")) or "")
            link = str(dig(row, str(fields.get("url") or "url")) or "")
            if not name or not link or urlparse(link).scheme not in ("http", "https"):
                continue
            if spec.get("require_code") and not code_matches(name, variants):
                continue
            raw_lang = dig(row, str(fields.get("lang") or "lang")) or ""
            lang = lang_of_code(raw_lang) if raw_lang else lang_of_name(name)
            cands.append(make_cand(
                "custom", name, link, lang=lang,
                ext=str(dig(row, str(fields.get("ext") or "ext")) or "") or ext_of(name),
                source_name=str(spec.get("name") or "自定义源"),
                allowed_hosts=_host_list(spec),
            ))
        return ok(cands) if cands else miss()
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        return degraded("network", "自定义字幕源暂不可用：%s" % type(exc).__name__)
