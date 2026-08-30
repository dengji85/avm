# -*- coding: utf-8 -*-
"""av-wiki 女优资料插件（av-wiki.net 素人片站的女优档案页）。

av-wiki 为 MGS / FANZA 系素人片披露「素人化名 → 女优真名」映射，并给每位女优
维护独立档案页 ``https://av-wiki.net/av-actress/{英文 slug}/``，内容含：

    別名義  ：名取沙雪（なとりさゆき）、雨宮日向子
    生年月日：1994年02月18日
    サイズ  ：T160 / B89-W62-H91

实现要点：
  - 定位：用 WordPress 搜索 ``?s={女优名}`` 拿到含 ``av-actress/{slug}/`` 的页面，
    逐个抓档案页并比对「AV女優名」字段，命中名字才采用（避免同名误抓）；
  - 提取：解析 ``<dl>`` 定义列表里的 生年月日 / サイズ / 別名義；
  - 尺寸串解析：``T160 / B89-W62-H91`` → height=160, bust=89, waist=62, hip=91；
  - 复用 providers.base.http_get 的代理/UA/重试。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from bs4 import BeautifulSoup

from .base import ActressPlugin

# 匹配 size 串里的身高与三围：T160 / B89-W62-H91，罩杯可带括号如 B87(D)
_RE_HEIGHT = re.compile(r"[Tt](\d{2,3})")
_RE_BWH = re.compile(r"[Bb](\d{2,3})(?:\([^)]*\))?[-\s]*[Ww](\d{2,3})[-\s]*[Hh](\d{2,3})")
_RE_CUP = re.compile(r"[Bb]\d{2,3}\(([^)]+)\)")
# 生年月日：1994年02月18日 / 1994-02-18
_RE_BIRTH = re.compile(r"(\d{4})\s*[年/\-]\s*(\d{1,2})\s*[月/\-]\s*(\d{1,2})\s*日?")


def _norm(s: str) -> str:
    """归一化名字，便于比对。"""
    s = (s or "").strip().lower()
    for ch in (" ", "　", "・", "·", ".", "（", "）", "(", ")", "ー", "-", "、", ","):
        s = s.replace(ch, "")
    return s


def _http_get(url: str, cfg: Dict[str, Any]) -> str:
    from ..providers.base import http_get
    scfg = cfg.get("scraper", {}) or {}
    html = http_get(url, scfg, scfg.get("avwiki", {}) or {})
    if not html:
        raise ValueError(f"av-wiki 请求失败：{url}")
    return html


def _dl_map(html: str) -> Dict[str, str]:
    """解析档案页的 <dl> 定义列表：dt -> dd 文本（键去除尾随冒号/空白）。"""
    soup = BeautifulSoup(html, "html.parser")
    out: Dict[str, str] = {}
    for dl in soup.find_all("dl"):
        for dt in dl.find_all("dt"):
            key = dt.get_text(" ", strip=True)
            if not key:
                continue
            # 去除键尾部的冒号（全角/半角）与空白，统一键名
            key = re.sub(r"[：:\s]+$", "", key).strip()
            if not key or key in out:
                continue
            dd = dt.find_next_sibling("dd")
            val = dd.get_text(" ", strip=True) if dd else ""
            out[key] = val
    return out


def _find_actress_slugs(html: str) -> List[str]:
    """从搜索页提取所有 av-actress 链接的 slug（去重、去 unknown）。"""
    slugs = set(re.findall(r"/av-actress/([a-z0-9-]+)/", html))
    return [s for s in slugs if s and s != "unknown"]


def _parse_profile(html: str, name: str = "") -> Dict[str, Any]:
    """从女优档案页解析出资料字段（含头像 URL）。"""
    fields = _dl_map(html)
    result: Dict[str, Any] = {}

    # 生日
    for key in ("生年月日", "誕生日"):
        v = fields.get(key, "")
        m = _RE_BIRTH.search(v)
        if m:
            result["birthday"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            break

    # 别名（別名義，逗号/顿号分隔）
    alias = fields.get("別名義", "") or fields.get("別名", "")
    if alias:
        parts = [p.strip() for p in re.split(r"[、,，]", alias) if p.strip()]
        result["alias"] = parts

    # 身高 / 三围（サイズ 或 尺寸）
    size_txt = fields.get("サイズ", "") or fields.get("尺寸", "")
    mh = _RE_HEIGHT.search(size_txt)
    if mh:
        result["height"] = str(int(mh.group(1)))
    mb = _RE_BWH.search(size_txt)
    if mb:
        result["bust"] = str(int(mb.group(1)))
        result["waist"] = str(int(mb.group(2)))
        result["hip"] = str(int(mb.group(3)))
    mc = _RE_CUP.search(size_txt)
    if mc and not result.get("cup"):
        result["cup"] = str(mc.group(1))

    # 女优头像：找档案页里非懒加载、非占位、alt 近似女优名的真实图片（DMM 图床）
    avatar_url = _find_avatar(html, name)
    if avatar_url:
        result["avatar_url"] = avatar_url

    return result


def _find_avatar(html: str, name: str) -> str:
    """从档案页找女优头像图（DMM 图床 actress/{slug}.jpg 或 alt 匹配的实图）。"""
    soup = BeautifulSoup(html, "html.parser")
    target = _norm(name)
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        cls = " ".join(img.get("class") or [])
        if "lazyload" in cls or "animation" in cls:
            continue
        alt = (img.get("alt") or "").strip()
        # 只收女优名相近的图（排除 logo 等站点图）
        if target and target in _norm(alt):
            return src
        # 兜底：DMM 图床 actress/ 目录下的头像
        if "/actress/" in src and ".jpg" in src:
            return src
    return ""


class AvWikiActressPlugin(ActressPlugin):
    id = "avwiki_actress"
    name = "av-wiki 女优资料"
    description = "从 av-wiki.net 女优档案页抓取素人女优的生日 / 身高 / 三围 / 别名。该站专精素人片女优，与素人刮削互补。联网。"
    capabilities = ["height", "measurements", "birthday", "alias"]
    needs_network = True

    def fetch(self, name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
        if not name:
            raise ValueError("名字为空")
        base = (cfg.get("providers", {}) or {}).get("avwiki", {}).get("base_url") \
            or (cfg.get("scraper", {}) or {}).get("avwiki", {}).get("base_url") \
            or "https://av-wiki.net"
        base = base.rstrip("/")
        target = _norm(name)

        # 1) 搜索，找到候选女优档案页
        from urllib.parse import quote
        search_url = f"{base}/?s={quote(name)}"
        html = _http_get(search_url, cfg)
        slugs = _find_actress_slugs(html)
        if not slugs:
            raise ValueError(f"av-wiki 未找到女优「{name}」的档案页")

        last_err: str = ""
        for slug in slugs:
            try:
                page = _http_get(f"{base}/av-actress/{slug}/", cfg)
                fields = _dl_map(page)
                profile = _parse_profile(page, name)
            except Exception as e:
                last_err = str(e)
                continue
            # 用档案页里的「AV女優名」或标题里的名字做校验
            page_name = fields.get("AV女優名", "")
            # 优先：页面明确标注的 AV女優名
            if page_name and _norm(page_name) == target:
                return self._build(name, profile)
            # 其次：标题里包含目标名
            if re.search(r"<title[^>]*>([^<]*)</title>", page, re.I):
                t = re.search(r"<title[^>]*>([^<]*)</title>", page, re.I).group(1)
                if target and target in _norm(t):
                    return self._build(name, profile)
        raise ValueError(f"av-wiki 女优「{name}」档案页校验失败（{last_err}）")

    @staticmethod
    def _build(name: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"name": name}
        for f in ("height", "bust", "waist", "hip", "birthday", "cup"):
            if profile.get(f):
                result[f] = profile[f]
        alias = profile.get("alias")
        if alias:
            # 只取主别名（逗号分隔后的第一个），避免与既有 alias 冲突
            result["alias"] = str(alias[0])
        if profile.get("avatar_url"):
            result["avatar_url"] = profile["avatar_url"]
        return result
