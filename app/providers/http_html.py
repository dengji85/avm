# -*- coding: utf-8 -*-
"""用户自定义网页规则数据源。

这是一个通用的、由配置驱动的网页解析引擎：地址模板与 CSS 选择器
全部由使用者自行填写，程序本身不预置任何站点。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin

from .base import BaseProvider, MetaResult


class HttpHtmlProvider(BaseProvider):
    name = "http_html"
    label = "自定义网页规则"
    desc = "按你填写的 CSS 选择器解析你指定的网页，规则完全自定义"

    def enabled(self) -> bool:
        if not self.options.get("enabled"):
            return False
        if not (self.options.get("detail_url") or self.options.get("search_url")):
            return False
        try:
            import bs4  # noqa: F401
        except ImportError:
            return False
        return True

    # ------------------------------------------------------------------

    def fetch(self, movie: Dict[str, Any]) -> Optional[MetaResult]:
        from bs4 import BeautifulSoup

        code = str(movie.get("code") or "").strip()
        if not code:
            return None

        page_url, html = self._load_detail(code)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        selectors: Dict[str, Any] = self.options.get("selectors") or {}
        meta: Dict[str, Any] = {}
        for field, rule in selectors.items():
            value = self._extract(soup, rule, page_url)
            if value not in (None, "", []):
                meta[field] = value

        name = str(self.options.get("name") or self.label)
        return self.normalize(meta, f"http_html:{name}") or None

    # ------------------------------------------------------------------

    def _load_detail(self, code: str) -> tuple[str, Optional[str]]:
        detail_tpl = str(self.options.get("detail_url") or "")
        if detail_tpl:
            url = detail_tpl.replace("{code}", quote(code)).replace("{code_raw}", code)
            return url, self.http_get(url)

        search_tpl = str(self.options.get("search_url") or "")
        link_sel = str(self.options.get("result_link_selector") or "")
        if not search_tpl or not link_sel:
            return "", None

        search_url = search_tpl.replace("{code}", quote(code)).replace("{code_raw}", code)
        html = self.http_get(search_url)
        if not html:
            return search_url, None

        from bs4 import BeautifulSoup

        node = BeautifulSoup(html, "html.parser").select_one(link_sel)
        href = node.get("href") if node else None
        if not href:
            return search_url, None
        detail = urljoin(search_url, str(href))
        return detail, self.http_get(detail, headers={"Referer": search_url})

    @staticmethod
    def _extract(soup: Any, rule: Any, base_url: str) -> Any:
        if isinstance(rule, str):
            rule = {"css": rule, "attr": "text"}
        if not isinstance(rule, dict):
            return None
        css = str(rule.get("css") or "").strip()
        if not css:
            return None

        attr = str(rule.get("attr") or "text")
        multi = bool(rule.get("multi"))
        pattern = str(rule.get("regex") or "")

        nodes = soup.select(css)
        if not nodes:
            return None
        if not multi:
            nodes = nodes[:1]

        values: List[str] = []
        for node in nodes:
            if attr in ("text", "", "innertext"):
                val = node.get_text(" ", strip=True)
            else:
                val = node.get(attr) or node.get(f"data-{attr}") or ""
                if attr in ("src", "href", "data-src") and val:
                    val = urljoin(base_url, str(val))
            val = re.sub(r"\s+", " ", str(val)).strip()
            if pattern:
                m = re.search(pattern, val)
                val = (m.group(1) if m.groups() else m.group(0)) if m else ""
            if val:
                values.append(val)

        if not values:
            return None
        return values if multi else values[0]
