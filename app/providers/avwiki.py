# -*- coding: utf-8 -*-
"""av-wiki.net 素人片元数据源。

该站专门整理 MGS / FANZA 系素人作品，并披露「素人化名 → AV 女优真名」的对应关系，
URL 规律为 https://av-wiki.net/{番号小写}/（例如 mfc-354）。

页面无封面图，因此本源只补齐「真名 / 化名 / 厂牌 / 发行日 / 标题」等元数据；
封面由 scraper 的回退逻辑从其它源（javbus / javdb）补齐。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from ..cdp_fetch import cdp_fetch
from .base import BaseProvider, MetaResult, NetBlocked, detect_blocker


class AvWikiProvider(BaseProvider):
    name = "avwiki"
    label = "AV-Wiki (素人)"
    desc = "av-wiki.net 素人化名→真名映射，补齐素人片元数据（无封面，封面回退其它源）"

    def __init__(self, cfg: Dict[str, Any]) -> None:
        super().__init__(cfg)
        self.base_url = (self.options.get("base_url") or "https://av-wiki.net").rstrip("/")

    def enabled(self) -> bool:
        return bool(self.options.get("enabled", True))

    # ----------------------------------------------------------- 文本提取助手
    @staticmethod
    def _grep(text: str, *patterns: str) -> str:
        for pat in patterns:
            m = re.search(pat, text, re.DOTALL)
            if m:
                val = m.group(1).strip()
                val = re.sub(r"[、。\s]+$", "", val).strip()  # 去掉尾随标点/空白
                if val:
                    return val
        return ""

    def fetch(self, movie: Dict[str, Any]) -> Optional[MetaResult]:
        code = (movie.get("code") or "").strip()
        if not code:
            return None
        slug = code.lower()
        url = f"{self.base_url}/{slug}/"

        # 经自带常驻 Chrome（CDP）抓取，绕过 av-wiki 的「请稍候」验证页。
        # cdp_fetch 会自动拉起一个独立 profile 的 headless Chrome（后台、无窗口），
        # 该 profile 养出信任度后完全自动运行；失败（无 Chrome / 拉起失败）时回退到普通 requests。
        html = None
        port = int(self.scfg.get("chrome_debug_port", 9222) or 9222)
        try:
            html = cdp_fetch(url, port=port, wait=25, auto_launch=True)
        except Exception as exc:  # CDP 不可用，回退
            self.last_error = f"CDP 抓取失败，回退 requests: {exc}"
            html = None
        if not html:
            html = self.http_get(url)
        if not html:
            return None

        # 反爬/人机验证页（如 av-wiki 的 Loader 验证）：这不是影片不存在，而是
        # 临时性的访问拦截，应当抛 NetBlocked 让调度层归为「网络错误」、不进跳过名单、可重试。
        blocker = detect_blocker(html)
        if blocker:
            self.last_error = blocker
            raise NetBlocked(blocker)

        soup = BeautifulSoup(html, "html.parser")
        # 标题：优先 h1，其次 <title>，并去掉站点后缀
        h1 = soup.find(["h1", "h2"])
        title = h1.get_text(" ", strip=True) if h1 else ""
        if not title:
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
        title = re.sub(r"\s*\|?\s*AV女優の名前が知りたい！.*$", "", title).strip()
        title = re.sub(r"\s*\|?\s*av-wiki.*$", "", title, flags=re.IGNORECASE).strip()

        # 取正文纯文本（去掉脚本/样式/评论区噪声）
        for tag in soup(["script", "style", "form", "nav", "footer"]):
            tag.decompose()
        body_text = soup.get_text(" ", strip=True)

        # 真名：两种常见句式
        real_name = self._grep(
            body_text,
            r"AV女優名[：:]\s*([^\s,、]+)",
            r"名前は、\s*([^\s,、]+?)\s*さん",
            r"出演してるAV女優の名前は、\s*([^\s,、]+?)\s*さん",
        )
        # 化名（素人名义）
        alias = self._grep(
            body_text,
            r"素人名義[：:]\s*([^\s,、]+)",
            r"の〔([^〕]+)〕は誰",
            r"（" + re.escape(slug.upper()) + r"）の〔([^〕]+)〕",
        )
        # 厂牌 / 配信商
        studio = self._grep(
            body_text,
            r"配信メーカー[：:]\s*([^\s,、/]+)",
            r"メーカー[：:]\s*([^\s,、/]+)",
        )
        # 发行日
        release = self._grep(
            body_text,
            r"配信開始日[：:]\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
            r"作品配信開始\D*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
        )
        release = release.replace("年", "-").replace("月", "-").replace("日", "").strip("-")

        if not real_name and not studio and not release and not title:
            # 页面存在但没解析出任何有用字段，视为未命中（可能是无关文章）
            return None

        meta: Dict[str, Any] = {
            "title": title,
            "release_date": release,
            "studio": studio,
            "source": f"avwiki:{slug}",
        }
        if real_name:
            meta["actresses"] = [real_name]
        # 化名作为标签，便于画廊按素人筛选 / 关联；并统一打「素人」标签
        tags = []
        if alias:
            tags.append(alias)
        tags.append("素人")
        meta["tags"] = tags
        # 把「化名 → 真名」关系也记到 plot，方便人工核对
        if alias and real_name:
            meta["plot"] = f"素人名义：{alias} → 真名：{real_name}"

        return self.normalize(meta, f"avwiki:{slug}")
