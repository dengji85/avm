# -*- coding: utf-8 -*-
"""维基百科女优资料插件（日文维基优先，回退中文维基）。

日文维基的 AV 女优条目使用专门的 infobox 模板 ``{{AV女優}}``，字段规整：

    |身長=170
    |バスト=93 |ウエスト=59 |ヒップ=88
    |カップ=F
    |生年=2001 |生月=4 |生日=20
    |出身地=東京都

相比通用影视库（TMDB 等），维基百科的 ``{{AV女優}}`` 是 AV 专门模板，对日本 AV 女优
覆盖最全、且官方提供免费 MediaWiki API（无需 Key）。

实现要点：
  - 直取：action=query&prop=revisions&titles={名}（直接用女优名当标题，最稳）；
  - 搜索兜底：action=query&list=search&srsearch={名}，命中后用 title 再取文，
    处理「中文名 vs 日文名」等写法差异；
  - 取文：action=query&prop=revisions&rvslots=main&rvprop=content；
  - 解析：在 ``{{AV女優`` 模板块内正则提取字段；**仅当条目确实含该模板才采用**，
    避免同名普通人条目张冠李戴（这是本插件的关键正确性保障）；
  - 生日由 生年/生月/生日 三段拼成 YYYY-MM-DD；
  - 复用 providers.base.http_get 的代理/UA/重试（维基偶有限流，走配置代理更稳）。
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from .base import ActressPlugin

WIKI_API = "https://{lang}.wikipedia.org/w/api.php"

# 模板块起止：匹配 {{AV女優 到对应的 }}（只取首个，AV 模板通常在条目最前）
_RE_AVBOX = re.compile(r"\{\{\s*AV女優\s*(.*?)\}\}", re.S | re.I)
# 在模板块内提取 |字段=值
_RE_FIELD = re.compile(r"\|\s*([^=|{}]+?)\s*=\s*([^|{}]*)")
# 生日三段
_RE_YEAR = re.compile(r"\|\s*生年\s*=\s*(\d{4})")
_RE_MONTH = re.compile(r"\|\s*生月\s*=\s*(\d{1,2})")
_RE_DAY = re.compile(r"\|\s*生日\s*=\s*(\d{1,2})")
# 通用数值字段（ja）
_RE_HEIGHT = re.compile(r"\|\s*身長\s*=\s*(\d+(?:\.\d+)?)")
_RE_BUST = re.compile(r"\|\s*バスト\s*=\s*(\d+(?:\.\d+)?)")
_RE_WAIST = re.compile(r"\|\s*ウエスト\s*=\s*(\d+(?:\.\d+)?)")
_RE_HIP = re.compile(r"\|\s*ヒップ\s*=\s*(\d+(?:\.\d+)?)")
_RE_CUP = re.compile(r"\|\s*カップ\s*=\s*([A-Za-z]{1,2})")
_RE_BIRTHPLACE = re.compile(r"\|\s*出身地\s*=\s*([^|{}]+)")


def _norm(s: str) -> str:
    """归一化：去空白/分隔符/括号/长音，便于标题比对。"""
    s = (s or "").strip().lower()
    for ch in (" ", "　", "・", "·", ".", "（", "）", "(", ")", "ー", "-"):
        s = s.replace(ch, "")
    return s


def _clean_wiki(s: str) -> str:
    """清洗维基正文里的引用/链接/HTML 标记，得到纯文本。"""
    if not s:
        return ""
    s = re.sub(r"<ref\b.*?</ref>", "", s, flags=re.S | re.I)  # 去掉 <ref>...</ref>
    s = re.sub(r"<ref\b[^>]*/>", "", s, flags=re.I)           # 自闭合 <ref/>
    s = re.sub(r"<[^>]+>", "", s)                              # 去掉其余 HTML 标签
    # [[a|b]] -> b，[[a]] -> a
    s = re.sub(r"\[\[([^\]|]+)\|?([^\]]*)\]\]", lambda m: m.group(2) or m.group(1), s)
    # [http://... 文本] 外部链接 -> 文本；[url] 无文本 -> 删掉。
    # 兼容链接未闭合（值可能被模板字段分隔符截断）的情况。
    s = re.sub(r"\[https?://[^\s\]]+\s+([^\]]*)", lambda m: m.group(1).rstrip("|"), s)
    s = re.sub(r"\[https?://[^\]]*\]", "", s)
    s = re.sub(r"\[https?://[^\]]*", "", s)
    s = s.replace("'''", "").replace("''", "")                 # 去掉加粗/斜体
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _wiki_fetch_text(cfg: Dict[str, Any], lang: str, params: Dict[str, Any]) -> Optional[str]:
    """向指定语言维基发起 MediaWiki API 请求，返回响应文本；失败返回 None。

    注意：本插件特意用 urllib（标准库）而不用 requests。
    实测部分本地代理会对 requests/urllib3 的 TLS 指纹返回 403（Wikimedia Error 页），
    而 urllib 的标准 TLS 指纹可正常通过代理访问维基。
    """
    scfg = cfg.get("scraper", {}) or {}
    proxy = scfg.get("proxy") or ""
    timeout = int(scfg.get("timeout", 25))
    retries = int(scfg.get("retries", 3)) or 1

    params["format"] = "json"
    query = urllib.parse.urlencode(params)
    url = WIKI_API.format(lang=lang) + "?" + query

    proxy_handler = urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        proxy_handler, urllib.request.HTTPSHandler(context=ctx))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 (av-manager actress plugin)")]

    last_err: Exception = None
    for attempt in range(retries):
        try:
            with opener.open(url, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(0.4 * (attempt + 1))
    return None


def _wiki_get(cfg: Dict[str, Any], lang: str, params: Dict[str, Any]) -> Optional[dict]:
    """向指定语言维基发起 MediaWiki API 请求并解析 JSON；失败返回 None。"""
    raw = _wiki_fetch_text(cfg, lang, params)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _fetch_wikitext(cfg: Dict[str, Any], title: str, lang: str) -> Optional[str]:
    """按标题取条目的维基文本；取不到返回 None。"""
    data = _wiki_get(cfg, lang, {
        "action": "query",
        "prop": "revisions",
        "rvslots": "main",
        "rvprop": "content",
        "titles": title,
    })
    if not data:
        return None
    pages = (data.get("query", {}).get("pages")) or {}
    for pid, page in pages.items():
        if pid == "-1":
            continue  # 标题不存在
        revs = page.get("revisions") or []
        if not revs:
            continue
        slot = revs[0].get("slots", {}).get("main", {})
        # MediaWiki 的 slot 正文键名是 "*"（不是 "content"）
        return slot.get("*") or slot.get("content") or revs[0].get("*") or revs[0].get("content") or ""
    return None


def _search_titles(cfg: Dict[str, Any], name: str, lang: str) -> list:
    """在指定语言维基里搜索女优名，返回候选标题列表；失败返回空列表。"""
    data = _wiki_get(cfg, lang, {
        "action": "query",
        "list": "search",
        "srlimit": "10",
        "srsearch": name,
    })
    if not data:
        return []
    return [h.get("title", "") for h in (data.get("query", {}).get("search") or [])]


def _pick_candidates(name: str, titles: list) -> list:
    """从候选标题里挑最匹配的：归一化完全相等优先，其次包含。"""
    target = _norm(name)
    exact = None
    contains = None
    for t in titles:
        if not t:
            continue
        tn = _norm(t)
        if tn == target:
            exact = t
            break
        if target and (target in tn or tn in target) and contains is None:
            contains = t
    if exact:
        return [exact] + [t for t in titles if t != exact]
    if contains:
        return [contains] + [t for t in titles if t != contains]
    return titles


def _parse_av_box(wikitext: str) -> Dict[str, Any]:
    """在 {{AV女優}} 模板块内解析女优字段。"""
    m = _RE_AVBOX.search(wikitext)
    block = m.group(1) if m else wikitext
    out: Dict[str, Any] = {}

    h = _RE_HEIGHT.search(block)
    if h:
        out["height"] = h.group(1)
    b = _RE_BUST.search(block)
    if b:
        out["bust"] = b.group(1)
    w = _RE_WAIST.search(block)
    if w:
        out["waist"] = w.group(1)
    hp = _RE_HIP.search(block)
    if hp:
        out["hip"] = hp.group(1)
    c = _RE_CUP.search(block)
    if c:
        out["cup"] = c.group(1).upper()
    bp = _RE_BIRTHPLACE.search(block)
    if bp:
        cleaned = _clean_wiki(bp.group(1))
        if cleaned:
            out["birthplace"] = cleaned

    # 生日：生年/生月/生日 三段
    y = _RE_YEAR.search(block)
    mo = _RE_MONTH.search(block)
    d = _RE_DAY.search(block)
    if y and mo and d:
        try:
            out["birthday"] = f"{int(y.group(1)):04d}-{int(mo.group(1)):02d}-{int(d.group(1)):02d}"
        except Exception:
            pass

    return out


def _fetch_extract(cfg: Dict[str, Any], title: str, lang: str) -> str:
    """用 MediaWiki ``prop=extracts`` 取条目的整篇纯文本正文（渲染后，无模板/链接残留）。

    这是最稳的取简介方式：官方 API 直接返回可读正文，无需自己解析 wikitext 模板嵌套。
    整篇取回（不设 exintro），保留章节换行，压缩多余空白。
    """
    data = _wiki_get(cfg, lang, {
        "action": "query",
        "prop": "extracts",
        "explaintext": "1",
        "titles": title,
    })
    if not data:
        return ""
    pages = (data.get("query", {}).get("pages")) or {}
    for pid, page in pages.items():
        if pid == "-1":
            continue
        extract = page.get("extract") or ""
        extract = _compact_wiki_text(extract)
        return extract
    return ""


def _compact_wiki_text(text: str) -> str:
    """压缩维基纯文本：按行保留章节换行，去掉多余空行与行尾空白。"""
    if not text:
        return ""
    lines = []
    blank = 0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            blank += 1
            if blank >= 2:  # 只保留一个空行作段落分隔
                continue
        else:
            blank = 0
        lines.append(s)
    return "\n".join(lines).strip()


def _try_extract(wikitext: str) -> Optional[Dict[str, Any]]:
    """仅当条目含 {{AV女優}} 模板才解析；否则返回 None（避免张冠李戴）。"""
    if not wikitext:
        return None
    if "{{AV女優" not in wikitext and "{{AV女优" not in wikitext:
        return None
    parsed = _parse_av_box(wikitext)
    return parsed or None


def _search_and_fetch(cfg: Dict[str, Any], name: str, lang: str) -> Optional[Dict[str, Any]]:
    """在一个语言版维基里抓取女优资料；找不到返回 None。

    流程：直取（titles=名）→ 若失败或不是 AV 模板，再 search 拿候选标题逐个尝试。
    """
    # 1) 直取：直接用女优名当标题（中文名在 ja 维基可能不命中，但日文名/同名直接命中）
    direct = _fetch_wikitext(cfg, name, lang)
    parsed = _try_extract(direct)
    if parsed:
        parsed["profile"] = _fetch_extract(cfg, name, lang)
        return parsed

    # 2) search 兜底：命中别名 / 不同写法（如中文名在日文维基的日文名）
    titles = _search_titles(cfg, name, lang)
    candidates = _pick_candidates(name, titles)
    for title in candidates:
        if not title or _norm(title) == _norm(name):
            continue
        wikitext = _fetch_wikitext(cfg, title, lang)
        parsed = _try_extract(wikitext)
        if parsed:
            parsed["profile"] = _fetch_extract(cfg, title, lang)
            return parsed
    return None


class WikiActressPlugin(ActressPlugin):
    id = "wiki_actress"
    name = "维基百科女优资料"
    description = "从维基百科（日文维基 {{AV女優}} 模板优先）抓取女优身高/三围/罩杯/生日/出身地。AV 专门模板，覆盖最全，免费 API 无需 Key，联网。"
    capabilities = ["height", "measurements", "cup", "birthday", "birthplace"]
    needs_network = True

    def fetch(self, name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
        if not name:
            raise ValueError("名字为空")
        # 语言优先级：日文维基最全，回退中文维基
        langs = (cfg.get("wiki", {}) or {}).get("languages") or ["ja", "zh"]
        last_err = None
        for lang in langs:
            try:
                result = _search_and_fetch(cfg, name, lang)
            except Exception as e:
                last_err = e
                result = None
            if result:
                result.setdefault("name", name)
                return result
        raise ValueError(f"维基百科未找到女优「{name}」的有效条目"
                         + (f"（{last_err}）" if last_err else ""))
