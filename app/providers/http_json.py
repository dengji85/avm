# -*- coding: utf-8 -*-
"""用户自定义 JSON 接口数据源。

不内置任何站点地址，URL、请求头与字段映射全部来自配置文件。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional
from urllib.parse import quote

from .base import BaseProvider, MetaResult


def dig(obj: Any, path: str) -> Any:
    """按 ``a.b.0.c`` 形式的路径取值。"""
    if not path:
        return obj
    cur = obj
    for part in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            if not re.fullmatch(r"-?\d+", part):
                return None
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


class HttpJsonProvider(BaseProvider):
    name = "http_json"
    label = "自定义 JSON 接口"
    desc = "调用你自己配置的 JSON 接口，通过字段映射取回元数据"

    def enabled(self) -> bool:
        return bool(self.options.get("enabled")) and bool(self.options.get("url"))

    def fetch(self, movie: Dict[str, Any]) -> Optional[MetaResult]:
        code = str(movie.get("code") or "").strip()
        if not code:
            return None
        url = str(self.options.get("url") or "")
        url = url.replace("{code}", quote(code)).replace("{code_raw}", code)
        text = self.http_get(url)
        if not text:
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None

        root = dig(data, str(self.options.get("root") or ""))
        if isinstance(root, list):
            root = root[0] if root else None
        if not isinstance(root, (dict, list)):
            return None

        fields: Dict[str, str] = self.options.get("fields") or {}
        meta: Dict[str, Any] = {}
        for field, path in fields.items():
            if not path:
                continue
            value = dig(root, path)
            if value not in (None, "", []):
                meta[field] = value
        name = str(self.options.get("name") or self.label)
        return self.normalize(meta, f"http_json:{name}") or None
