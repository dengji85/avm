# -*- coding: utf-8 -*-
"""
JavBus 元数据源（在线抓取）。

- 详情页：{base_url}/{CODE}
- 字段：标题 / 女优 / 类型 / 厂商 / 发行商 / 系列 / 导演 / 发行日期 / 时长
- 封面：统一规整为 DMM 图床直链（https://pics.dmm.co.jp/...），该图床全球直连、
  无需代理即可下载，因此即便 JavBus 站点本身走代理，封面下载依然稳定快速。

注意：JavBus 主域有「driver-verify / 年龄验证」页（本质是反爬人机验证），
脚本直连会被拦。解决办法（任选其一，在设置面板填写）：
  1. 配置 HTTP 代理（scraper.proxy）；
  2. 用浏览器正常打开站点后，把整段 Cookie 粘贴到本源的 cookie 字段
     （Cookie 名字不固定，整段粘最稳）。
部分网络下即使走代理仍要带 Cookie 才能过验证。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .base import BaseProvider, detect_blocker
from bs4 import BeautifulSoup

DEFAULT_BASE_URL = "https://www.javbus.com"

# 信息块中文/日文表头 -> 标准化字段
INFO_MAP = {
    "識別碼": None, "番号": None, "品番": None,            # 番号（已有，跳过）
    "發行日期": "release_date", "配信開始日": "release_date", "発売日": "release_date",
    "長度": "runtime", "収録時間": "runtime", "収录時間": "runtime", "时长": "runtime", "時間": "runtime",
    "導演": "director", "監督": "director",
    "製作商": "studio", "メーカー": "studio", "制作商": "studio",
    "發行商": "publisher", "レーベル": "publisher", "发行商": "publisher",
    "系列": "series", "シリーズ": "series",
    "演員": "actresses", "女優": "actresses", "出演者": "actresses",
    "類別": "genres", "ジャンル": "genres", "标签": "genres", "ジャンル": "genres",
}


def _best_cover(url: str, base_url: str = "") -> str:
    """挑选真实封面直链。

    优先保留 JavBus 自己的图床（/pics/cover/... 或 pics.javbus.com），这是详情页里
    真正的封面；不要强行套 DMM 图床模板——DMM 很多番号无图，套模板只会得到
    jppl.jpg 占位图（已被 images 层黑名单拦截，导致永远无封面）。

    仅当链接本身就是 DMM 有效大图时才保留 DMM；DMM 占位图（cid 为 jp/jppl）丢弃。
    """
    if not url:
        return ""
    low = url.lower()
    # JavBus 自有图床：保留原样（相对路径补全为绝对）。
    # 注意：站点有两套图床前缀——/pics/cover/ 与 /imgs/cover/，漏掉任何一个
    # 都会让封面 URL 仍是相对路径，下载时变成 https:///imgs/... 而失败。
    if ("/pics/cover/" in low or "pics.javbus.com" in low
            or "/imgs/cover/" in low or "imgs.javbus.com" in low
            or low.startswith("/imgs/") or low.startswith("/pics/")):
        if url.startswith("/"):
            return (base_url.rstrip("/") + url) if base_url else url
        return url
    # 已是绝对 http(s) 且非 DMM 占位图：保留
    if low.startswith("http") and "dmm.co.jp" not in low:
        return url
    # DMM 链接：识别占位图（cid 为 jp / jppl / 单字符）则丢弃
    m = re.findall(r"digital/video/([^/]+)/", low)
    cid = m[-1] if m else ""
    if cid and cid not in ("jp", "jppl") and not cid.endswith("pl"):
        return url
    # 其它 DMM 占位 / 无法识别：丢弃，交由上层回退
    return ""


def _parse_runtime(text: str) -> Optional[int]:
    m = re.findall(r"(\d+)", text)
    return int(m[0]) if m else None


class JavBusProvider(BaseProvider):
    name = "javbus"
    label = "JavBus"
    desc = "JavBus 在线元数据（详情页，封面走 DMM 图床直连）。主域有 driver-verify 验证页，" \
           "需配置代理或粘贴浏览器整段 Cookie 才能抓取。"

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        super().__init__(cfg)
        self.base_url = (self.options.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

    def fetch(self, movie: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        code = (movie.get("code") or "").strip()
        if not code:
            return None
        url = f"{self.base_url}/{code}"
        headers: Dict[str, str] = {}
        cookie = (self.options.get("cookie") or "").strip()
        if cookie:
            headers["Cookie"] = cookie
        html = self.http_get(url, headers=headers)
        if not html:
            # 直接详情 404 / 连接失败：退回搜索兜底（部分素人番号只在搜索结果里出现）
            return self._search(code, headers)
        blocker = detect_blocker(html)
        if blocker:
            self.last_error = blocker
            # 直接详情被拦 / 无结果时，退回搜索
            return self._search(code, headers)
        return self._parse(code, html)

    def _search_cover(self, code: str) -> Optional[str]:
        """从站内搜索结果页取封面图（javbus 搜索结果用 cloudfront 图床，
        部分素人/无码片详情页 #cover 为空但搜索结果有图）。"""
        try:
            html = self.http_get(f"{self.base_url}/search/{code}", headers={})
            if not html or detect_blocker(html):
                return None
            soup = BeautifulSoup(html, "html.parser")
            box = soup.select_one("a.movie-box")
            if not box:
                return None
            img = box.select_one("img")
            src = (img.get("src") if img else None) or (img.get("data-src") if img else None)
            if not src:
                return None
            # cloudfront / 其它 javbus 图床直接保留（补全相对路径）
            if src.startswith("/"):
                return (self.base_url.rstrip("/") + src)
            return src
        except Exception:
            return None

    def _search(self, code: str, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """搜索兜底：直接详情拿不到时，用站内搜索定位真实番号再抓详情。"""
        surl = f"{self.base_url}/search/{code}"
        shtml = self.http_get(surl, headers=headers)
        if not shtml or detect_blocker(shtml):
            return None
        soup = BeautifulSoup(shtml, "html.parser")
        box = soup.select_one("a.movie-box")
        if not box:
            return None
        href = (box.get("href") or "").strip()
        m = re.findall(r"/([A-Za-z0-9][A-Za-z0-9_\-]*)/?$", href)
        real = m[-1] if m else None
        if not real:
            return None
        dhtml = self.http_get(f"{self.base_url}/{real}", headers=headers)
        if not dhtml or detect_blocker(dhtml):
            return None
        return self._parse(real, dhtml)

    def _parse(self, code: str, html: str) -> Optional[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        if not soup.select_one("h3") and not soup.select_one("#cover"):
            return None  # Cloudflare 挑战页 / 不存在

        meta: Dict[str, Any] = {"source": self.name}

        h3 = soup.select_one("h3")
        if h3:
            meta["title"] = h3.get_text(" ", strip=True)

        img = soup.select_one("#cover") or soup.select_one(".screencap img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            cover = _best_cover(src, self.base_url) or src
            if cover:
                meta["cover"] = cover
        # 详情页无封面（素人/无码片常见）时，回退用搜索结果页的封面图
        # （javbus 搜索结果的图床是 cloudfront，详情页 #cover 为空但搜索结果有图）。
        if not meta.get("cover"):
            scover = self._search_cover(code)
            if scover:
                meta["cover"] = scover

        rows = soup.select(".info p")
        for idx, p in enumerate(rows):
            header = p.select_one("span.header")
            raw = p.get_text(" ", strip=True)
            if header:
                label = header.get_text(strip=True).rstrip("：:")
            else:
                # 部分行（女優 / 類別等）表头不是 span.header，
                # 用文本前缀匹配已知表头来识别字段。
                label = ""
                for k in INFO_MAP:
                    if raw.startswith(k):
                        label = k
                        break
            field = INFO_MAP.get(label)
            if not field:
                continue
            # 取值：去掉表头文本
            if label and raw.startswith(label):
                raw = raw[len(label):].strip(" :：")
            # JavBus 部分镜像把「表头」与「数据」分在两行 <p>：
            # 本行只识别到表头、自身无链接也无内容时，去下一行取数据。
            val_p = p
            links = p.select("a")
            if not links and not raw.strip():
                nxt = rows[idx + 1] if idx + 1 < len(rows) else None
                if nxt and not nxt.select_one("span.header"):
                    val_p = nxt
                    links = val_p.select("a")
                    raw = val_p.get_text(" ", strip=True)
                    if label and raw.startswith(label):
                        raw = raw[len(label):].strip(" :：")
            if field in ("actresses", "genres"):
                items = [a.get_text(strip=True) for a in val_p.select("a") if a.get_text(strip=True)]
                if not items:
                    items = [t.strip() for t in raw.split() if t.strip()]
                if items:
                    meta[field] = items
                    if field == "actresses":
                        # 同时抓取女优头像：JavBus 女優行每个 <a> 内嵌 <img src>
                        avatars = {}
                        for a in val_p.select("a"):
                            nm = a.get_text(strip=True)
                            if not nm:
                                continue
                            img = a.select_one("img")
                            src = (img.get("src") if img else None) or a.get("data-original")
                            if src:
                                avatars[nm] = src
                        if avatars:
                            meta["actress_avatars"] = avatars
            elif field == "runtime":
                meta[field] = _parse_runtime(raw)
            else:
                val = raw.strip()
                if val and val not in ("", "-", "----"):
                    meta[field] = val

        if not (meta.get("title") or meta.get("cover")):
            return None
        return self.normalize(meta, self.name)
