# -*- coding: utf-8 -*-
"""
JavDB 元数据源（在线抓取，作为 JavBus 的备选）。

- 搜索页：{base_url}/search?q={CODE}&f=all
- 详情页：{base_url}/v/{id}
- 字段：标题 / 女优 / 类型 / 厂商 / 发行商 / 系列 / 导演 / 发行日期 / 时长
- 封面：JavDB 自身图床（需代理或 Cookie 才能下载）。

JavDB 同样受到 Cloudflare 保护，通常需要配置代理或 Cookie 才能访问。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .base import BaseProvider
from bs4 import BeautifulSoup

DEFAULT_BASE_URL = "https://javdb.com"

INFO_MAP = {
    "番号": None,
    "日期": "release_date",
    "片長": "runtime",
    "導演": "director",
    "製作商": "studio",
    "發行商": "publisher",
    "系列": "series",
}


def _parse_runtime(text: str) -> Optional[int]:
    m = re.findall(r"(\d+)", text)
    return int(m[0]) if m else None


class JavDBProvider(BaseProvider):
    name = "javdb"
    label = "JavDB"
    desc = "JavDB 在线元数据（数据更全、封面更大），需配置代理或 CF Cookie 才能访问。"

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        super().__init__(cfg)
        self.base_url = (self.options.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

    def fetch(self, movie: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        code = (movie.get("code") or "").strip()
        if not code:
            return None
        cookie = (self.options.get("cookie") or "").strip()
        headers: Dict[str, str] = {}
        if cookie:
            headers["Cookie"] = cookie

        # 1) 搜索
        surl = f"{self.base_url}/search?q={code}&f=all"
        shtml = self.http_get(surl, headers=headers)
        if not shtml:
            return None
        ssoup = BeautifulSoup(shtml, "html.parser")
        box = ssoup.select_one("a.box")
        if not box:
            # 可能已被 CF 拦截或无结果
            return None
        detail_path = box.get("href")
        if not detail_path:
            return None
        if detail_path.startswith("/"):
            detail_path = self.base_url + detail_path
        else:
            detail_path = self.base_url + "/" + detail_path.lstrip("/")

        # 2) 详情
        dhtml = self.http_get(detail_path, headers=headers)
        if not dhtml:
            return None
        return self._parse(dhtml)

    def _parse(self, html: str) -> Optional[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        if not soup.select_one(".movie-panel") and not soup.select_one("h2.title"):
            return None
        meta: Dict[str, Any] = {"source": self.name}

        h2 = soup.select_one("h2.title")
        if h2:
            meta["title"] = h2.get_text(" ", strip=True)

        img = soup.select_one(".movie-panel .image img, .video-cover img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src:
                meta["cover"] = src

        # 字段（番号 / 日期 / 片長 / 導演 / 製作商 / 發行商 / 系列）
        for nav in soup.select(".movie-panel .columns .column nav, .panel-block"):
            key = nav.get_text(" ", strip=True).split(":", 1)[0].strip()
            field = INFO_MAP.get(key)
            if not field:
                continue
            val = nav.get_text(" ", strip=True)
            if ":" in val:
                val = val.split(":", 1)[1].strip()
            if field == "runtime":
                meta[field] = _parse_runtime(val)
            elif val:
                meta[field] = val

        # 女优（含头像）
        actor_els = soup.select(".movie-panel .actors a, .panel-block .actors a")
        actors = [a.get_text(strip=True) for a in actor_els if a.get_text(strip=True)]
        if actors:
            meta["actresses"] = actors
            avatars = {}
            for a in actor_els:
                nm = a.get_text(strip=True)
                if not nm:
                    continue
                img = a.select_one("img")
                src = (img.get("src") if img else None) or a.get("data-original")
                if src:
                    avatars[nm] = src
            if avatars:
                meta["actress_avatars"] = avatars
        # 类型
        genres = [a.get_text(strip=True) for a in soup.select(".movie-panel .tags a, .panel-block .tags a")]
        if genres:
            meta["genres"] = genres

        if not (meta.get("title") or meta.get("cover")):
            return None
        return self.normalize(meta, self.name)
