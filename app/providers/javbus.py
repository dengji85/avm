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
from urllib.parse import quote, urljoin

from .base import BaseProvider, NetBlocked, NetError, detect_blocker
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


def _name_key(s: str) -> str:
    """女優名归一：去空白与 JavBus 在名字后附带的「有碼 / 無碼 / 歐美」标签。"""
    s = re.sub(r"\s+", "", str(s or ""))
    return re.sub(r"(有碼|無碼|歐美|日本|中国|中國|韓國|韩国|台灣|台湾)$", "", s)


_PAREN_TAIL_RE = re.compile(r"\s*[（(][^（）()]*[）)]\s*$")
# JavBus 搜索对装饰符号不友好：带 ●○～ 的整串会直接 404（如系列名）。
# 关键词过长（含假名长串）也会 404，所以带符号的系列名要退化成分段关键词。
_SERIES_NOISE_RE = re.compile(r"[●○◯〇◎◇◆■□▲△▼▽★☆※～〜~]")


def _series_queries(name: str) -> List[str]:
    """系列搜索关键词候选（按可信度排序）。

    例：「人妻痴●電車～さわられた○○～」整串搜会 404，按符号切段后
    「さわられた」这类最具区分度的片段才能命中。
    """
    raw = str(name or "").strip()
    base = _PAREN_TAIL_RE.sub("", raw).strip()
    if not base:
        return [raw] if raw else []
    if not _SERIES_NOISE_RE.search(base):
        return [base]
    parts = [p.strip() for p in _SERIES_NOISE_RE.split(base) if len(p.strip()) >= 2]
    parts.sort(key=len, reverse=True)
    out: List[str] = []
    for p in parts + [_SERIES_NOISE_RE.sub("", base).strip()]:
        if p and p not in out:
            out.append(p)
    return out or [base]


def _norm_series(text: str) -> str:
    """系列名比对用：去掉符号、空格与「有碼/無碼」标签。"""
    s = _PAREN_TAIL_RE.sub("", str(text or "")).strip()
    s = _SERIES_NOISE_RE.sub("", s)
    return _name_key(s)


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
    # JavBus 不收录 FC2 同人与素人片，这两类直接跳过，避免无效请求
    excludes = ("fc2", "amateur")
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
        # 详情页走较短超时：javbus 详情页在国内网络常读超时，若沿用全局
        # timeout(20s) × retries(3) 会让单部片干等 40~60s。这里压到 10s、
        # 不重试，超时就快速降级到搜索兜底 / 下一个数据源。
        html = self._detail_get(url, headers)
        if not html:
            # 直接详情 404 / 连接失败：退回搜索兜底（部分素人番号只在搜索结果里出现）
            return self._search(code, headers)
        blocker = detect_blocker(html)
        if blocker:
            self.last_error = blocker
            # 直接详情被拦 / 无结果时，退回搜索
            return self._search(code, headers)
        return self._parse(code, html)

    #: 详情页超时（秒）与重试次数。javbus 详情页常读超时，重试只会成倍放大耗时，
    #: 这里快速失败，把机会让给搜索兜底和其它数据源。
    _DETAIL_TIMEOUT = 10
    _DETAIL_RETRIES = 1

    def _detail_get(self, url: str, headers: Optional[Dict[str, str]] = None) -> Optional[str]:
        """详情页专用请求：短超时 + 不重试，避免单部片被慢响应拖死。"""
        saved_timeout = self.scfg.get("timeout")
        saved_retries = self.scfg.get("retries")
        try:
            self.scfg["timeout"] = self._DETAIL_TIMEOUT
            self.scfg["retries"] = self._DETAIL_RETRIES
            return self.http_get(url, headers)
        finally:
            if saved_timeout is None:
                self.scfg.pop("timeout", None)
            else:
                self.scfg["timeout"] = saved_timeout
            if saved_retries is None:
                self.scfg.pop("retries", None)
            else:
                self.scfg["retries"] = saved_retries

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
        """搜索兜底：直接详情拿不到时，用站内搜索定位真实番号再抓详情。

        同样走短超时（见 _detail_get）：搜索页 + 详情页若都慢，会成倍放大
        单部片耗时，不如快速放弃、把机会让给后续数据源。
        """
        surl = f"{self.base_url}/search/{code}"
        shtml = self._detail_get(surl, headers)
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
        dhtml = self._detail_get(f"{self.base_url}/{real}", headers)
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

    def parse_detail(self, code: str, html: str) -> Optional[Dict[str, Any]]:
        """从**已有**的详情页 HTML 解析元数据（类型 / 女优 / 时长 / 大封面）。

        订阅「在线资源探测」本来就要把作品页整页取回来，这里复用它，
        不额外发请求；解析失败返回 None。
        """
        try:
            return self._parse(code, html)
        except Exception:
            return None

    # ------------------------------------------------------ 列表页（订阅）
    # 演员页 /series 页的条目结构与搜索页一致，都是 a.movie-box：
    #   <a class="movie-box" href="/ABC-123">
    #     <div class="photo-frame"><img src="..." title="标题"></div>
    #     <div class="photo-info"><span>ABC-123 <date>2026-01-01</date></span></div>
    # 站点结构可能随改版漂移，因此这里只依赖 a.movie-box 与文本里的番号/日期，
    # 不绑定具体 class 层级；解析不出条目时由调用层写 parse_error，不静默成功。
    def list_url(self, target: Dict[str, Any], page: int = 1) -> str:
        sid = str(target.get("site_id") or "").strip()
        if not sid:
            # 从库导入的目标只有搜索页地址（如 /searchstar/{name}），
            # 翻页沿用站点的「地址 + /页码」形式，否则第二页会重复抓首页。
            url = str(target.get("page_url") or "").strip()
            if not url or page <= 1:
                return url
            return "%s/%d" % (url.rstrip("/"), int(page))
        seg = "star" if (target.get("kind") or "actress") == "actress" else "series"
        base = "%s/%s/%s" % (self.base_url, seg, sid)
        return base if page <= 1 else "%s/%d" % (base, int(page))

    def normalize_list_target(self, target: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """系列搜索地址纠偏：库里系列名常带「（厂商）」或 ●○～，整串搜会 403/404。"""
        if (target.get("kind") or "") != "series":
            return None
        name = str(target.get("name") or "")
        cur = str(target.get("page_url") or "")
        if "/search/" not in cur:
            return None
        cands = _series_queries(name)
        if not cands:
            return None
        url = "%s/search/%s" % (self.base_url, quote(cands[0], safe=""))
        if url == cur:
            return None
        return {"page_url": url, "site_id": ""}

    def resolve_list_target(self, target: Dict[str, Any], html: str) -> Optional[Dict[str, str]]:
        """把「女優搜索页」解析成真正的女優作品页。

        /searchstar/{名字} 只返回女優搜索结果（.avatar-box），不是作品列表；
        真正的作品列表在 /star/{id}。按名字精确匹配（忽略空格与「有碼/無碼」标签），
        只有一个候选时直接采用。返回 {"page_url", "site_id"}，无需重定向则返回 None。
        """
        if (target.get("kind") or "actress") == "series":
            return self._resolve_series(target, html)
        if (target.get("kind") or "actress") != "actress":
            return None
        cur = str(target.get("page_url") or "").lower()
        if "searchstar" not in cur:
            return None
        soup = BeautifulSoup(html or "", "html.parser")
        links = soup.select("#waterfall a[href*='/star/']") or soup.select("a[href*='/star/']")
        if not links:
            return None
        want = _name_key(str(target.get("name") or ""))
        href = ""
        for a in links:
            if want and _name_key(a.get_text(" ", strip=True)) == want:
                href = str(a.get("href") or "")
                break
        if not href and len(links) == 1:
            href = str(links[0].get("href") or "")
        if not href:
            return None
        full = urljoin(self.base_url + "/", href)
        m = re.search(r"/star/([^/?#]+)", full)
        return {"page_url": full, "site_id": m.group(1) if m else ""}

    def _resolve_series(self, target: Dict[str, Any], html: str) -> Optional[Dict[str, str]]:
        """系列搜索页 → 真正的系列页（``/series/<id>``）。

        JavBus 的搜索结果最多 10~30 条且没有系列链接，系列页才是完整作品列表
        （还带翻页）。所以拿搜索命中的作品详情页反查系列 id，并用系列名做比对，
        避免把「同名不同系列」的作品算进来。
        """
        if str(target.get("site_id") or "").strip():
            return None
        if "/search" not in str(target.get("page_url") or ""):
            return None
        want = _norm_series(str(target.get("name") or ""))
        if not want:
            return None
        seen: set = set()
        detail_fetches = 0
        for q in _series_queries(str(target.get("name") or ""))[:2]:
            url = "%s/search/%s" % (self.base_url, quote(q, safe=""))
            page = html if url == str(target.get("page_url") or "") else (self.http_get(url) or "")
            if not page:
                continue
            boxes = BeautifulSoup(page, "html.parser").select("a.movie-box")[:3]
            for box in boxes:
                href = str(box.get("href") or "")
                if not href or href in seen or detail_fetches >= 4:
                    continue
                seen.add(href)
                detail_fetches += 1
                detail = self.http_get(urljoin(self.base_url + "/", href)) or ""
                if not detail:
                    continue
                for a in BeautifulSoup(detail, "html.parser").select("a[href*='/series/']"):
                    text = a.get_text(" ", strip=True)
                    if not text or _norm_series(text) != want:
                        continue
                    full = urljoin(self.base_url + "/", str(a.get("href") or ""))
                    m = re.search(r"/series/([^/?#]+)", full)
                    if m:
                        return {"page_url": full, "site_id": m.group(1)}
        return None

    def parse_list(self, html: str, target: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not html:
            raise NetError("列表页为空")
        blocker = detect_blocker(html)
        if blocker:
            raise NetBlocked(blocker)
        soup = BeautifulSoup(html, "html.parser")
        boxes = soup.select("a.movie-box")
        if not boxes:
            # 没有条目既可能是「真的没有作品」，也可能是改版 / 被拦截。
            # 详情页特征（h3 / #cover）都没有时按拦截处理，避免把失败当空。
            if not soup.select_one("h3") and not soup.select_one("#cover"):
                raise NetError("列表页结构无法识别（可能改版或被拦截）")
            return []
        out: List[Dict[str, Any]] = []
        for box in boxes:
            href = str(box.get("href") or "").strip()
            img = box.select_one("img")
            info = box.select_one(".photo-info")
            text = info.get_text(" ", strip=True) if info else box.get_text(" ", strip=True)
            code = _code_from_list_text(text)
            sid = href.rstrip("/").rsplit("/", 1)[-1] if href else code
            if not code:
                continue
            m = re.search(r"\d{4}-\d{2}-\d{2}", text)
            cover = ""
            if img:
                cover = str(img.get("src") or img.get("data-src") or "").strip()
                if cover.startswith("/"):
                    cover = self.base_url + cover
            out.append({
                "code": code,
                "site_id": sid,
                "title": str((img.get("title") if img else "") or "").strip() or code,
                "release_date": m.group(0) if m else "",
                "cover": cover,
                "page_url": href if href.startswith("http") else self.base_url + href,
                "site": self.name,
            })
        return out


def _code_from_list_text(text: str) -> str:
    """从列表条目文本里取番号：取行首的「字母-数字」组合。"""
    m = re.search(r"([A-Za-z][A-Za-z0-9]*-\d{2,})", text or "")
    return m.group(1).upper() if m else ""
