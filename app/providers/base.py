# -*- coding: utf-8 -*-
"""元数据源基类。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

MetaResult = Dict[str, Any]

FIELDS = (
    # code：本地 NFO/JSON 里的番号（num/id）。过去没有它，番号会在 normalize
    # 阶段被丢弃，导致「文件名解析不出番号、NFO 里却写着番号」的影片无法自愈。
    "title", "original_title", "code", "plot", "release_date", "runtime", "studio",
    "publisher", "series", "director", "rating", "cover", "fanart",
    "actresses", "genres", "tags", "actress_profiles",
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

    # 收录范围声明：子类可覆写，用于「智能刮削」跳过不可能命中的源。
    # 返回 None/空 表示不限制；返回类型集合表示只处理这些类型。
    #: 只处理这些番号类型（空 = 不限制）
    handles: tuple = ()
    #: 明确不处理这些番号类型（空 = 不排除）
    excludes: tuple = ()

    def can_handle(self, movie: Dict[str, Any]) -> bool:
        """预判本源是否可能收录该影片；False 时调度层直接跳过，不发请求。

        依据番号类型（见 app/code_kind）：FC2 / 素人 / 普通商业片。
        默认不限制（True），各源按自身收录范围覆写 handles / excludes。
        """
        if not self.handles and not self.excludes:
            return True
        from ..code_kind import classify_movie
        kind = classify_movie(movie, self.cfg)
        if self.handles and kind not in self.handles:
            return False
        if self.excludes and kind in self.excludes:
            return False
        return True

    # -------------------------------------------------------------- 工具

    @property
    def timeout(self) -> int:
        return int(self.scfg.get("timeout", 20))

    def http_get(self, url: str, headers: Optional[Dict[str, str]] = None) -> Optional[str]:
        # 用可变容器接收模块级实现的失败原因，落到 last_error 供诊断接口展示。
        # 否则失败时上层只能看到「返回空」，无法区分是 404 / 反爬 / 代理不通 / 超时。
        err: Dict[str, Any] = {}
        text = http_get(url, self.scfg, self.options, headers, err_out=err)
        if err.get("msg"):
            self.last_error = str(err["msg"])
        return text

    @staticmethod
    def normalize(meta: Dict[str, Any], source: str) -> "MetaResult":
        """统一字段类型，剔除空值（委托模块级实现）。"""
        return _normalize_meta(meta, source)


def http_get(url: str, scfg: Dict[str, Any], options: Dict[str, Any],
             headers: Optional[Dict[str, str]] = None,
             err_out: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """模块级 HTTP GET（供 BaseProvider 与女优资料插件等复用）。

    处理代理 / 完整浏览器请求头 / 数据源级 Cookie / 重试，并复用 scfg 里的
    timeout / proxy / user_agent / retries 配置。返回响应文本或 None（含重试耗尽）。

    err_out：可选的可变容器，用于回传失败原因（{"msg": "..."}）。调用方（如
    BaseProvider.http_get）据此把原因写到 last_error，让诊断接口能给出可操作
    的提示，而不是笼统的「返回空」。
    """
    try:
        import requests
    except ImportError:
        return None
    import time
    from urllib.parse import urlparse
    proxy = scfg.get("proxy") or ""
    # 完整浏览器请求头：JavBus 的 driver-verify 会检查 Accept/Referer 等，
    # 只发 User-Agent 会被当成脚本而拦在验证页外。
    origin = ""
    try:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
    except Exception:
        origin = ""
    merged: Dict[str, str] = {
        "User-Agent": scfg.get(
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
    merged.update(options.get("headers") or {})
    merged.update(headers or {})
    # 数据源级 Cookie：用于绕过 av-wiki / JavBus 等站点的反爬验证页。
    # 支持两种写法：直接的 "k=v; k2=v2" 字符串，或已解析好的 dict。
    raw_cookie = options.get("cookie") or options.get("cookies") or ""
    cookie_dict: Optional[Dict[str, str]] = None
    if isinstance(raw_cookie, dict):
        cookie_dict = raw_cookie
    elif isinstance(raw_cookie, str) and raw_cookie.strip():
        cookie_dict = {}
        for part in raw_cookie.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                cookie_dict[k.strip()] = v.strip()
    timeout = int(scfg.get("timeout", 20))
    retries = int(scfg.get("retries", 3)) or 1
    last_exc: Exception = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers=merged,
                cookies=cookie_dict,
                proxies={"http": proxy, "https": proxy} if proxy else None,
                verify=False,
            )
            resp.raise_for_status()
            # 解码优先级：响应头声明的 charset > HTML <meta> 声明 > UTF-8 兜底。
            #
            # 注意 1：resp.charset_encoding 是 urllib3 的属性，requests.Response 上
            #   没有（requests 2.34 实测无此属性）。直接取会抛 AttributeError 并被
            #   下面的 except 吞掉，表现为「所有在线源一律返回空」——而响应本身是
            #   200 正常的，极具迷惑性。这里用 getattr 兜底并优先用 resp.encoding。
            # 注意 2：不要用 resp.apparent_encoding 兜底——它对日文 UTF-8 常误判成
            #   其它编码，导致标题/演员名变乱码（如 av-wiki 的日文片名）。
            declared = getattr(resp, "charset_encoding", "") or resp.encoding or ""
            if not declared or declared.lower() in ("iso-8859-1", "latin-1"):
                # requests 对无声明响应默认给 ISO-8859-1，不可信，改从 HTML 里找
                declared = _html_charset(resp.text) or "utf-8"
            resp.encoding = declared
            return resp.text
        except Exception as exc:  # 代理隧道抖动 / 超时 / 4xx
            last_exc = exc
            # 4xx 是确定性结果（404 番号不存在、403 被拒、401 未授权），
            # 重试只是成倍放大耗时且结果不变，直接退出。
            status = getattr(getattr(exc, "response", None), "status_code", 0) or 0
            if 400 <= int(status) < 500:
                break
            if attempt < retries - 1:
                time.sleep(0.4 * (attempt + 1))
    if err_out is not None and last_exc is not None:
        err_out["msg"] = _human_http_error(last_exc)
    return None


def _html_charset(text: str) -> str:
    """从 HTML 的 <meta charset=...> 提取字符集声明。

    requests 对无 charset 声明的响应默认给 ISO-8859-1（不可信，会把 UTF-8
    日文解成乱码），此时改从 HTML 自身声明取，比 apparent_encoding 猜测更准。
    """
    m = re.search(r"<meta[^>]+charset=[\"']?\s*([\w\-]+)", (text or "")[:4096], re.I)
    return (m.group(1) or "").strip().lower() if m else ""


def _human_http_error(exc: Exception) -> str:
    """把 requests 异常翻译成人话，供诊断接口展示。"""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", 0) or 0
    name = type(exc).__name__
    if status:
        if status == 404:
            return "HTTP 404：该番号在此站点不存在（或详情页 URL 规则不同）"
        if status == 403:
            return "HTTP 403：站点拒绝访问（多半是反爬，需 Cookie 或换代理）"
        if status == 401:
            return "HTTP 401：需要登录 / Cookie"
        if status == 429:
            return "HTTP 429：请求过于频繁，稍后再试"
        if 400 <= status < 500:
            return f"HTTP {status}：请求被拒绝"
        return f"HTTP {status}：服务端错误"
    if "Timeout" in name or "timeout" in str(exc).lower():
        return f"请求超时（{name}）：代理不通或站点响应慢，可调大设置里的超时"
    if "ProxyError" in name or "proxy" in str(exc).lower():
        return f"代理连接失败（{name}）：请检查代理地址与代理软件是否运行"
    if "SSLError" in name:
        return f"SSL 错误（{name}）：代理不支持 HTTPS 或证书校验失败"
    if "ConnectionError" in name or "NewConnectionError" in name:
        return f"连接失败（{name}）：网络不通或代理未启动"
    return f"{name}: {exc}"


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
        elif field == "actress_profiles":
            # 女优资料映射 {女优名: {字段: 值}}，保留 dict 原样
            out[field] = value if isinstance(value, dict) else {}
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
