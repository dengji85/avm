# -*- coding: utf-8 -*-
"""av-wiki.net 素人片元数据源。

该站专门整理 MGS / FANZA 系素人作品，并披露「素人化名 → AV 女优真名」的对应关系，
URL 规律为 https://av-wiki.net/{番号小写}/（例如 mfc-354）。

页面无封面图，因此本源只补齐「真名 / 化名 / 厂牌 / 发行日 / 标题」等元数据；
封面由 scraper 的回退逻辑从其它源（javbus / javdb）补齐。
"""
from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from ..cdp_fetch import cdp_fetch, _is_blocker
from .base import BaseProvider, MetaResult, NetBlocked, detect_blocker


class AvWikiProvider(BaseProvider):
    name = "avwiki"
    label = "AV-Wiki (素人)"
    desc = "av-wiki.net 素人化名→真名映射，补齐素人片元数据（无封面，封面回退其它源）"
    # av-wiki 只收录素人片：非素人番号直接跳过，不再为空转浪费一次 CDP 抓取
    handles = ("amateur",)

    def __init__(self, cfg: Dict[str, Any]) -> None:
        super().__init__(cfg)
        self.base_url = (self.options.get("base_url") or "https://av-wiki.net").rstrip("/")
        # 数据库里已有核心资料的女优（规范化名）：刮削时直接跳过其档案页抓取
        self._skip_actresses: set = set()

    def enabled(self) -> bool:
        return bool(self.options.get("enabled", True))

    def set_skip_actresses(self, names) -> None:
        """注入「数据库已有核心资料」的女优名集合（由调度层刮削前一次性查出）。

        这些女优无需再抓档案页，刮削时直接跳过，显著提速且不影响已有数据。
        """
        self._skip_actresses = {self._norm(n) for n in (names or ()) if n}

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

    # -------------------------------------------------- 女优资料抓取
    _RE_BIRTH = re.compile(r"(\d{4})\s*[年/\-]\s*(\d{1,2})\s*[月/\-]\s*(\d{1,2})\s*日?")
    # 三围：B87(D) W59 H88 —— 罩杯可能带括号（如 (D)），用非捕获组不改变分组编号
    _RE_BWH = re.compile(r"[Bb](\d{2,3})(?:\([^)]*\))?[-\s]*[Ww](\d{2,3})[-\s]*[Hh](\d{2,3})")
    _RE_CUP = re.compile(r"[Bb]\d{2,3}\(([^)]+)\)")
    _RE_H = re.compile(r"[Tt](\d{2,3})")

    # 女优资料缓存（进程内、跨影片复用）：{规范化女优名: profile 或 None}
    # 刮削同一女优的多部片时，档案页只需抓一次；「抓过但没资料」也记 None，
    # 避免同一批任务里反复重试同一个搜索/档案页（CDP 抓取开销大）。
    _PROFILE_CACHE: Dict[str, Any] = {}
    _PROFILE_LOCK = threading.Lock()

    def _actress_slugs(self, html: str):
        """从素人页提取 av-actress 链接 slug，按出现次数排序（主女优通常最多次）。"""
        from collections import Counter
        slugs = Counter(re.findall(r"/av-actress/([a-z0-9-]+)/", html))
        return [s for s, _ in slugs.most_common() if s and s != "unknown"]

    #: 回退到裸 requests 时的超时/重试。av-wiki 对无信任度的请求不直接拒绝，
    #: 而是故意慢速拖到超时；沿用全局 timeout(20s) × retries(3) 会让单次回退
    #: 耗上 20~60s 却仍拿到空壳页。这里压到「5 秒、不重试」快速放弃。
    _FALLBACK_TIMEOUT = 5
    _FALLBACK_RETRIES = 1

    def _quick_http_get(self, url: str) -> Optional[str]:
        """短超时回退：拿不到就快速放弃，避免 av-wiki 慢速拖垮刮削速度。"""
        saved_timeout = self.scfg.get("timeout")
        saved_retries = self.scfg.get("retries")
        try:
            self.scfg["timeout"] = self._FALLBACK_TIMEOUT
            self.scfg["retries"] = self._FALLBACK_RETRIES
            return self.http_get(url)
        finally:
            if saved_timeout is None:
                self.scfg.pop("timeout", None)
            else:
                self.scfg["timeout"] = saved_timeout
            if saved_retries is None:
                self.scfg.pop("retries", None)
            else:
                self.scfg["retries"] = saved_retries

    def _cget(self, url: str) -> Optional[str]:
        """带信任度地抓取：优先走常驻 Chrome（CDP），失败再快速回退普通 requests。

        av-wiki 对裸 requests 基本会拦截/返回空白壳页，女优档案页/搜索页
        必须用养出信任度的 CDP 会话才能拿到真实内容，否则身高三围生日头像全抓空。
        回退走短超时（见 _quick_http_get），避免被慢速拖 20~60s。
        """
        port = int(self.scfg.get("chrome_debug_port", 9222) or 9222)
        try:
            html = cdp_fetch(url, port=port, wait=12, auto_launch=True)
            if html and not _is_blocker(html):
                return html
        except Exception:
            pass
        return self._quick_http_get(url)

    def _fetch_actress_profiles(self, html: str, real_names, depth: int = 2) -> Dict[str, Any]:
        """素人刮削时顺带抓女优资料（身高/三围/生日/头像），返回 {女优名: {...}}。

        用 WordPress 搜索 ``?s={真名}`` 定位女优档案页（不依赖素人页 html 结构，
        兼容 CDP 渲染版），逐个抓档案页校验「AV女優名」包含真名才采用。
        搜索与档案页都走 CDP（带信任度），避免被 av-wiki 拦截导致资料抓空。
        """
        if not real_names:
            return {}
        # 数据库已有核心资料的女优直接跳过，不再重复抓取档案页
        if self._skip_actresses:
            real_names = [n for n in real_names
                          if n and self._norm(n) not in self._skip_actresses]
            if not real_names:
                return {}
        # 规范化名 -> 原名（搜索 ?s= 必须用原名）
        name_map: Dict[str, str] = {}
        for n in real_names:
            if not n:
                continue
            key = self._norm(n)
            if key:
                name_map.setdefault(key, n)
        if not name_map:
            return {}

        out: Dict[str, Any] = {}
        # 先消费缓存：已抓过的女优直接复用，未命中才走网络
        todo: List[str] = []
        for key in name_map:
            with self._PROFILE_LOCK:
                hit = key in self._PROFILE_CACHE
                cached = self._PROFILE_CACHE.get(key)
            if hit:
                if cached:
                    out[key] = cached
                continue
            todo.append(key)
        if not todo:
            return out

        remaining = set(todo)
        for key in todo:
            name = name_map[key]
            try:
                from urllib.parse import quote
                search_html = self._cget(f"{self.base_url}/?s={quote(name)}")
            except Exception:
                self._cache_profile(key, None)
                continue
            if not search_html:
                self._cache_profile(key, None)
                continue
            slugs = self._actress_slugs(search_html)[:depth]
            got = None
            for slug in slugs:
                try:
                    page = self._cget(f"{self.base_url}/av-actress/{slug}/")
                except Exception:
                    continue
                if not page:
                    continue
                fields = self._dl_fields(page)
                page_name = fields.get("AV女優名", "")
                # 档案页「AV女優名」常带注音/英文（如「那賀崎ゆきね（なかさきゆきね）- nakasaki yukine」），
                # 用「包含」而非「相等」校验，命中目标真名即可
                if not page_name:
                    continue
                matched = next((t for t in remaining if self._norm(t) in self._norm(page_name)), None)
                if not matched:
                    continue
                profile = self._parse_actress_page(page, page_name)
                if profile:
                    out[matched] = profile
                    got = profile
                    # 资料归属 matched（可能是本次搜索之外的女优），按其名缓存
                    self._cache_profile(matched, profile)
                    remaining.discard(matched)
                    break
            if got is None:
                # 搜过但没拿到资料：记负缓存，同一批任务内不再重复尝试
                self._cache_profile(key, None)
            if not remaining:
                break
        return out

    @classmethod
    def _cache_profile(cls, key: str, profile: Any) -> None:
        """写入女优资料缓存（线程安全）。profile 为 None 表示「抓过但没资料」。"""
        with cls._PROFILE_LOCK:
            cls._PROFILE_CACHE[key] = profile

    @classmethod
    def clear_profile_cache(cls) -> None:
        """清空女优资料缓存（需要强制重抓时调用）。"""
        with cls._PROFILE_LOCK:
            cls._PROFILE_CACHE.clear()

    @staticmethod
    def _norm(s: str) -> str:
        s = (s or "").strip().lower()
        for ch in (" ", "　", "・", "·", ".", "（", "）", "(", ")", "ー", "-", "、", ","):
            s = s.replace(ch, "")
        return s

    @staticmethod
    def _dl_fields(html: str) -> Dict[str, str]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        out: Dict[str, str] = {}
        for dl in soup.find_all("dl"):
            for dt in dl.find_all("dt"):
                key = dt.get_text(" ", strip=True)
                if not key:
                    continue
                key = re.sub(r"[：:\s]+$", "", key).strip()
                if not key or key in out:
                    continue
                dd = dt.find_next_sibling("dd")
                out[key] = dd.get_text(" ", strip=True) if dd else ""
        return out

    @classmethod
    def _parse_actress_page(cls, html: str, name: str) -> Dict[str, Any]:
        """解析女优档案页，返回资料 + 头像 URL。"""
        from bs4 import BeautifulSoup
        fields = cls._dl_fields(html)
        prof: Dict[str, Any] = {}
        for key in ("生年月日", "誕生日"):
            m = cls._RE_BIRTH.search(fields.get(key, "") or "")
            if m:
                prof["birthday"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                break
        size_txt = fields.get("サイズ", "") or fields.get("尺寸", "")
        mh = cls._RE_H.search(size_txt)
        if mh:
            prof["height"] = str(int(mh.group(1)))
        mb = cls._RE_BWH.search(size_txt)
        if mb:
            prof["bust"] = str(int(mb.group(1)))
            prof["waist"] = str(int(mb.group(2)))
            prof["hip"] = str(int(mb.group(3)))
        mc = cls._RE_CUP.search(size_txt)
        if mc and not prof.get("cup"):
            prof["cup"] = str(mc.group(1))
        # 头像：档案页里非懒加载、alt 近似女优名的实图
        target = cls._norm(name)
        for img in BeautifulSoup(html, "html.parser").find_all("img"):
            src = (img.get("src") or "").strip()
            if not src or src.startswith("data:"):
                continue
            clsname = " ".join(img.get("class") or [])
            if "lazyload" in clsname:
                continue
            alt = (img.get("alt") or "").strip()
            if target and target in cls._norm(alt):
                prof["avatar_url"] = src
                break
            # DMM 女优头像目录（actress / actjpgs），且文件名含女优罗马音或路径不含作品号
            if (".jpg" in src or ".png" in src) and ("/actress/" in src or "/actjpgs/" in src):
                prof["avatar_url"] = src
                break
        return prof

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
            html = cdp_fetch(url, port=port, wait=12, auto_launch=True)
        except Exception as exc:  # CDP 不可用，回退
            self.last_error = f"CDP 抓取失败，回退 requests: {exc}"
            html = None
        if not html:
            html = self._quick_http_get(url)
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
        # 化名（素人名义）：文案形如「素人名義 さなさん 29歳 結婚5年目 表参道」
        # 或「〔 さなさん 29歳 結婚5年目 表参道 〕は誰」。化名只是首个 token，
        # 后面的年龄/婚龄/出身地等描述不能算进去，否则会变成超长脏标签。
        alias = self._grep(
            body_text,
            r"素人名義[：:\s]*([^\s,、]+)",
            r"の〔\s*([^\s〕]+)",
            r"（" + re.escape(slug.upper()) + r"）の〔\s*([^\s〕]+)〕",
        )
        if alias:
            # 兜底：只保留首个词（化名），去掉年龄/婚龄/出身地等噪声
            alias = alias.split()[0]
        # 厂牌 / 配信商
        studio = self._grep(
            body_text,
            r"配信メーカー[：:]\s*([^\s,、/]+)",
            r"メーカー[：:]\s*([^\s,、/]+)",
        )
        # 系列（シリーズ）—— dl 定义列表里键名可能不带冒号，两种写法都兼容
        series = self._grep(
            body_text,
            r"シリーズ[：:]?\s*([^\s,、/]+)",
        )
        # 发行日
        release = self._grep(
            body_text,
            r"配信開始日[：:]?\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
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
        if series:
            meta["series"] = series
        if real_name:
            meta["actresses"] = [real_name]
            # 素人片以 av-wiki 为最全信息源：顺带抓女优档案（身高/三围/生日/头像）
            # 受 cover/media.auto_actress_profile 配置开关控制（默认开启）
            auto_profile = self.options.get("auto_actress_profile", True)
            # 数据库已有核心资料的女优：连素人页回抓都省掉，直接跳过
            if auto_profile and self._norm(real_name) in self._skip_actresses:
                auto_profile = False
            if auto_profile:
                # 女优资料走 WordPress 搜索定位档案页，不依赖素人页 html
                # （原先会额外回抓一次整页用于解析链接，既慢又用不上，已去掉）
                profiles = self._fetch_actress_profiles(html, [real_name])
                if profiles:
                    meta["actress_profiles"] = profiles
        # 素人片统一打「素人」类型（genre），不写入自定义标签，避免自定义标签快速膨胀。
        # 「素人」本就是影片类型，作为 genre 可正常展示并按类型筛选。
        meta["genres"] = ["素人"]
        # 把「化名 → 真名」关系记到 plot，方便人工核对（化名不再作为标签污染自定义标签）
        if alias and real_name:
            meta["plot"] = f"素人名义：{alias} → 真名：{real_name}"

        return self.normalize(meta, f"avwiki:{slug}")
