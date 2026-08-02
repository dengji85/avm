# -*- coding: utf-8 -*-
"""本地 NFO / JSON 元数据源（离线，无需联网）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .. import nfo
from .base import BaseProvider, MetaResult


class LocalNfoProvider(BaseProvider):
    name = "local_nfo"
    label = "本地 NFO / JSON"
    desc = "读取影片同目录下的 .nfo 或 .json 元数据文件，完全离线"

    def enabled(self) -> bool:
        return bool(self.options.get("enabled", True))

    def fetch(self, movie: Dict[str, Any]) -> Optional[MetaResult]:
        code = movie.get("code") or ""
        for f in movie.get("files") or []:
            path = f.get("path") if isinstance(f, dict) else str(f)
            if not path:
                continue
            sidecar = nfo.find_sidecar(path, code)
            if not sidecar:
                continue
            meta = nfo.parse_nfo(sidecar)
            if not meta:
                continue
            # 把 NFO 里写的相对图片路径还原成绝对路径，便于后续拷贝
            for key in ("cover", "fanart"):
                val = str(meta.get(key) or "")
                if val and not val.lower().startswith(("http://", "https://")):
                    candidate = (sidecar.parent / val).resolve()
                    meta[key] = str(candidate) if candidate.exists() else ""
            result = self.normalize(meta, f"local_nfo:{sidecar.name}")
            if result:
                return result
        return None
