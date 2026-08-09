# -*- coding: utf-8 -*-
"""元数据源基类。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

MetaResult = Dict[str, Any]

FIELDS = (
    "title", "original_title", "plot", "release_date", "runtime", "studio",
    "publisher", "series", "director", "rating", "cover", "fanart",
    "actresses", "genres", "tags",
)


class NetBlocked(Exception):
    """数据源返回了反爬/人机验证页（如 Cloudflare、av-wiki 的 Loader 验证）。

    这不是「影片不存在」，而是临时性的访问拦截；应当归类为网络错误（不进
    跳过名单、可重试），而不是当作稳定 missing 污染 scrape_skip。
    """


class BaseProvider:
    name: str = "base"
    label: str = "基础数据源"
    desc: str = ""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.scfg: Dict[str, Any] = cfg.get("scraper", {})
        self.options: Dict[str, Any] = self.scfg.get(self.name, {}) or {}
        # 最近一次 fetch 的失败原因（供测试接口诊断用）
        self.last_error: str = ""

    def enabled(self) -> bool:
        return bool(self.options.get("enabled", True))

    def fetch(self, movie: Dict[str, Any]) -> Optional[MetaResult]:
        raise NotImplementedError

    # -------------------------------------------------------------- 工具

    @property
    def timeout(self) -> int:
        return int(self.scfg.get("timeout", 20))

    def http_get(self, url: str, headers: Optional[Dict[str, str]] = None) -> Optional[str]:
        try:
            import requests
        except ImportError:
            return None
        import time
        from urllib.parse import urlparse
        proxy = self.scfg.get("proxy") or ""
        # 完整浏览器请求头：JavBus 的 driver-verify 会检查 Accept/Referer 等，
        # 只发 User-Agent 会被当成脚本而拦在验证页外。
        origin = ""
        try:
            p = urlparse(url)
            origin = f"{p.scheme}://{p.netloc}"
        except Exception:
            origin = ""
        merged: Dict[str, str] = {
            "User-Agent": self.scfg.get(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }
        if origin:
            merged["Referer"] = origin + "/"
        merged.update(self.options.get("headers") or {})
        merged.update(headers or {})
        # 数据源级 Cookie：用于绕过 av-wiki / JavBus 等站点的反爬验证页。
        # 支持两种写法：直接的 "k=v; k2=v2" 字符串，或已解析好的 dict。
        raw_cookie = self.options.get("cookie") or self.options.get("cookies") or ""
        cookie_dict: Optional[Dict[str, str]] = None
        if isinstance(raw_cookie, dict):
            cookie_dict = raw_cookie
        elif isinstance(raw_cookie, str) and raw_cookie.strip():
            cookie_dict = {}
            for part in raw_cookie.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookie_dict[k.strip()] = v.strip()
        retries = int(self.scfg.get("retries", 3)) or 1
        last_exc: Exception = None
        for attempt in range(retries):
            try:
                resp = requests.get(
                    url,
                    timeout=self.timeout,
                    headers=merged,
                    cookies=cookie_dict,
                    proxies={"http": proxy, "https": proxy} if proxy else None,
                    verify=False,
                )
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or resp.encoding
                return resp.text
            except Exception as exc:  # 代理隧道抖动 / 超时 / 4xx，重试
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(0.4 * (attempt + 1))
        self.last_error = f"请求异常: {type(last_exc).__name__}: {last_exc}"
        return None

    @staticmethod
    def normalize(meta: Dict[str, Any], source: str) -> MetaResult:
        """统一字段类型，剔除空值（委托模块级实现）。"""
        return _normalize_meta(meta, source)


def detect_blocker(html: str) -> str:
    """识别返回的页面是被什么拦了，供测试接口给出可操作提示。

    返回空串表示看起来是正常的详情页；否则返回人话描述。
    """
    if not html:
        return ""
    low = html.lower()
    if ("cf-browser-verification" in low or "challenge-platform" in low
            or "just a moment" in low or "enable javascript and cookies" in low):
        return "Cloudflare 人机验证页（需填 cf_clearance Cookie）"
    if "driver-verify" in low or "age verification" in low:
        return "JavBus 自带的 driver-verify / 年龄验证页（需粘贴浏览器整段 Cookie）"
    # av-wiki.net 的 Loader 验证页：标题「请稍候…」+ 正文「Loader 正在验证您的请求」
    if ("loader" in low and "正在验证" in html) or "请稍候" in html or "正在验证您的请求" in html:
        return "av-wiki 反爬验证页（Loader 正在验证您的请求），需等待放行或带 Cookie"
    return ""

def _normalize_meta(meta: Dict[str, Any], source: str) -> MetaResult:
    """统一字段类型，剔除空值。"""
    out: MetaResult = {}
    for field in FIELDS:
        if field not in meta:
            continue
        value = meta[field]
        if field in ("actresses", "genres", "tags"):
            out[field] = _as_list(value)
        elif field == "runtime":
            out[field] = _as_int(value)
        elif field == "rating":
            out[field] = _as_float(value)
        elif field == "release_date":
            out[field] = _as_date(value)
        else:
            out[field] = str(value).strip()
    cleaned = {k: v for k, v in out.items() if v not in (None, "", [])}
    if cleaned:
        cleaned["source"] = source
    return cleaned


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in re.split(r"[,，、;；/|\n]", value) if x.strip()]
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("name") or item.get("title") or ""
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    return [str(value).strip()]


def _as_int(value: Any) -> int:
    m = re.search(r"\d+", str(value or ""))
    if not m:
        return 0
    n = int(m.group(0))
    return n // 60 if n > 1000 else n


def _as_float(value: Any) -> float:
    m = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(m.group(0)) if m else 0.0


def _as_date(value: Any) -> str:
    text = str(value or "")
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})", text)
    return f"{m.group(1)}-01-01" if m else ""
