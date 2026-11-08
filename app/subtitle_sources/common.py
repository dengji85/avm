# -*- coding: utf-8 -*-
"""在线字幕源的公共基础设施：出站请求、番号变体、语言识别、候选与结果构造。

各站点的解析逻辑放在同级模块里，编排（并行 / 超时 / 去重 / 评分 / 下载）
在 app/subsource.py。这里只放与站点无关、可被所有源复用的东西。

安全边界：
- 只把番号字符串发出去，绝不上传文件名、路径、视频或音频；
- 每个源独立限流 / 超时 / 失败降级，任何一个挂了都不影响其它源；
- 下载域名白名单由 subsource.guard_url 统一校验，源模块只负责「解析出 URL」。
"""
from __future__ import annotations

import json
import threading
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Sequence
from urllib.parse import unquote, urlparse

import requests

from ..config import outbound_verify_tls

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
MAX_BYTES = 4_000_000

_LOCK = threading.Lock()
_LAST_CALL: Dict[str, float] = {}


# --------------------------------------------------------------------------
# 出站
# --------------------------------------------------------------------------
def throttle(provider: str, delay_ms: int) -> None:
    """每个源一条时间轴：锁内只排号、锁外等待，避免串行阻塞其它源。"""
    step = max(0, int(delay_ms or 0)) / 1000.0
    with _LOCK:
        now = time.time()
        scheduled = max(now, _LAST_CALL.get(provider, 0.0) + step)
        _LAST_CALL[provider] = scheduled
    wait = scheduled - now
    if wait > 0:
        time.sleep(wait)


def provider_of(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    if "subtitlecat" in host:
        return "subtitlecat"
    if "xunlei" in host or "geilijiasu" in host:
        return "thunder"
    if "assrt" in host or "makedie" in host:
        return "assrt"
    if "opensubtitles" in host:
        return "opensubtitles"
    return host or "unknown"


def request(url: str, scfg: Dict[str, Any], *, headers: Dict[str, str] | None = None,
            timeout: int | None = None, binary: bool = False, provider: str = "",
            method: str = "GET", json_body: Any = None):
    """统一出站：默认校验证书、复用代理、失败由调用方降级。"""
    throttle(provider or provider_of(url), int(scfg.get("delay_ms") or 0))
    h = {
        "User-Agent": scfg.get("user_agent") or DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if headers:
        h.update({str(k): str(v) for k, v in headers.items()})
    proxy = scfg.get("proxy") or ""
    from ..proxy import proxies_for
    kwargs: Dict[str, Any] = {
        "timeout": int(timeout or scfg.get("timeout") or 15),
        "headers": h,
        "proxies": proxies_for(url, override=proxy),
        "verify": outbound_verify_tls(),
    }
    if method.upper() == "POST":
        resp = requests.post(url, json=json_body, **kwargs)
    else:
        resp = requests.get(url, **kwargs)
    resp.raise_for_status()
    return resp.content if binary else resp.text


def json_request(url: str, scfg: Dict[str, Any], **kwargs) -> Any:
    return json.loads(request(url, scfg, **kwargs))


# --------------------------------------------------------------------------
# 番号变体与匹配校验
# --------------------------------------------------------------------------
def norm_code(s: str) -> str:
    """只留字母数字的大写形式，用于「结果是否真的属于这个番号」的校验。"""
    return "".join(c for c in unicodedata.normalize("NFKC", s or "").upper()
                   if c.isalnum())


def code_variants(code: str, limit: int = 4) -> List[str]:
    """生成按优先级排序的番号查询写法。

    带数字前缀的素人番号（812MMC-010 / 300MAAN-123）在各站的写法极不统一：
    有的带前缀、有的去掉、有的没有连字符、有的不带前导零。只发一个写法，
    冷门番号几乎必然查不到。这里一次生成，由编排层按阶段并行尝试。
    """
    raw = unicodedata.normalize("NFKC", code or "").strip().upper()
    if not raw:
        return []
    out: List[str] = []

    def add(s: str) -> None:
        s = (s or "").strip()
        if s and s not in out:
            out.append(s)

    add(raw)
    add("".join(ch for ch in raw if ch.isalnum()))
    head, sep, tail = raw.partition("-")
    # 只对「数字前缀 + 字母 + 纯数字编号」的素人番号去前缀；
    # FC2-PPV-... / IPX-343 这类常规番号不拆，避免造出无意义的查询写法。
    if (sep and tail.isdigit() and head and head[0].isdigit()
            and any(c.isalpha() for c in head)):
        alpha = "".join(ch for ch in head if ch.isalpha())
        if alpha:
            add(alpha + "-" + tail)   # 812MMC-010 -> MMC-010
            add(alpha + tail)
            short = tail.lstrip("0") or "0"
            if short != tail:        # 去掉前导零：010 -> 10
                add(head + "-" + short)
                add(alpha + "-" + short)
                add(alpha + short)
    return out[:max(1, int(limit or 4))]


def code_matches(text: str, variants: Sequence[str]) -> bool:
    """结果标题必须真的命中任一番号写法；短变体要求足够长，避免误配到别的片。"""
    hay = norm_code(text)
    if not hay:
        return False
    for v in variants:
        n = norm_code(v)
        if len(n) >= 5 and n in hay:
            return True
    return False


# --------------------------------------------------------------------------
# 语言
# --------------------------------------------------------------------------
_LANG_SUFFIX = {
    "zh-cn": "简体中文", "zh-hans": "简体中文", "hans": "简体中文", "chs": "简体中文",
    "zh-tw": "繁体中文", "zh-hant": "繁体中文", "hant": "繁体中文", "cht": "繁体中文",
    "zh-ja": "日中双语", "zh-jp": "日中双语", "zh": "中文", "chi": "中文", "zho": "中文",
    "ja": "日文", "jpn": "日文", "jp": "日文",
    "en": "英文", "eng": "英文", "en-us": "英文", "en-gb": "英文",
    "ko": "韩文", "kor": "韩文", "kr": "韩文",
    "ru": "俄文", "rus": "俄文", "fr": "法文", "fre": "法文", "fra": "法文",
    "de": "德文", "ger": "德文", "deu": "德文", "es": "西班牙文", "spa": "西班牙文",
    "pt": "葡萄牙文", "por": "葡萄牙文", "pt-br": "葡萄牙文",
    "it": "意大利文", "ita": "意大利文", "th": "泰文", "tha": "泰文",
    "vi": "越南文", "vie": "越南文", "id": "印尼文", "ind": "印尼文",
    "ar": "阿拉伯文", "ara": "阿拉伯文", "tr": "土耳其文", "tur": "土耳其文",
}
_LANG_SUFFIX_SORTED = sorted(_LANG_SUFFIX.items(), key=lambda kv: -len(kv[0]))
_LANG_CODES = dict(_LANG_SUFFIX)
_LANG_CODES.update({"zh-cn": "简体中文", "zh-tw": "繁体中文", "zh": "中文"})


def lang_of_name(filename: str) -> str:
    """只看文件名「最后一个」语言后缀：基名里常带原字幕语言，全文搜会误判。"""
    stem = unquote(filename or "").rsplit(".", 1)[0].lower()
    for suf, lang in _LANG_SUFFIX_SORTED:
        if stem == suf or stem.endswith(("-" + suf, "_" + suf, "." + suf, " " + suf)):
            return lang
    return "未知"


def lang_of_code(code: Any) -> str:
    """ISO 语言码 → 展示名；不认识的语言保留原码，绝不过滤掉。"""
    c = str(code or "").strip().lower().replace("_", "-")
    if not c:
        return "未知"
    if c in _LANG_CODES:
        return _LANG_CODES[c]
    if c.split("-", 1)[0] in _LANG_CODES:
        return _LANG_CODES[c.split("-", 1)[0]]
    return c.upper()[:12] or "未知"


def ext_of(name: str, fallback: str = "srt") -> str:
    e = (name or "").rsplit(".", 1)[-1].lower().strip() if "." in (name or "") else ""
    return e if e in ("srt", "ass", "ssa", "vtt", "sub") else fallback


def as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# 结果与候选
# --------------------------------------------------------------------------
def ok(cands: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    r = {"status": "ok", "reason": "", "message": "", "candidates": cands}
    r.update(extra)
    return r


def miss(message: str = "该站没有此番号的字幕") -> Dict[str, Any]:
    return {"status": "miss", "reason": "no_result", "message": message, "candidates": []}


def degraded(reason: str, message: str) -> Dict[str, Any]:
    return {"status": "degraded", "reason": reason, "message": message, "candidates": []}


def off(message: str) -> Dict[str, Any]:
    """源被禁用或缺少凭据：明确告诉用户「没查」而不是「没有字幕」。"""
    return {"status": "off", "reason": "not_configured", "message": message, "candidates": []}


def make_cand(provider: str, name: str, url: str, *, lang: str = "未知",
              duration_ms: Any = 0, size: Any = 0, **extra: Any) -> Dict[str, Any]:
    c: Dict[str, Any] = {
        "provider": provider, "provider_label": provider,
        "name": name or "", "url": url or "", "ext": ext_of(name),
        "lang": lang or "其他语言", "duration_ms": as_int(duration_ms),
        "size": as_int(size), "upload": "",
    }
    c.update(extra)
    c["ext"] = ext_of("x." + str(c.get("ext") or ""), "srt")
    if c.get("lang") in (None, "", "未知"):
        c["lang"] = "其他语言"
    return c


def dig(obj: Any, path: str) -> Any:
    """按点分隔路径取嵌套 JSON 字段，用于自定义源。"""
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def as_list(value: Any) -> Iterable[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []
