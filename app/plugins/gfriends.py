# -*- coding: utf-8 -*-
"""gfriends 女优头像插件。

gfriends (https://github.com/gfriends/gfriends) 是一个媒体服务器女优头像仓库。
本插件通过 jsDelivr CDN 读取其 Filetree.json 索引，按女优名字（含多语种/别名）
匹配头像并返回 CDN URL。仅提供头像，不提供三围/年龄等文字资料。
"""
from __future__ import annotations

import gzip
import io
import json
import time
import urllib.request
from typing import Any, Dict

from .base import ActressPlugin

#: gfriends 文件树索引（多 CDN 备选，逐个尝试，提升不同网络环境可达性）
FILE_TREE_URLS = [
    "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master/Filetree.json",
    "https://raw.githubusercontent.com/gfriends/gfriends/master/Filetree.json",
    "https://ghproxy.net/https://raw.githubusercontent.com/gfriends/gfriends/master/Filetree.json",
]
#: 头像文件 CDN 前缀（与 FILE_TREE_URLS 一一对应，index 对齐）
CDN_BASES = [
    "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master/",
    "https://raw.githubusercontent.com/gfriends/gfriends/master/",
    "https://ghproxy.net/https://raw.githubusercontent.com/gfriends/gfriends/master/",
]

#: Filetree.json 缓存（进程内，带过期时间，避免每次下载几 MB）
_CACHE: Dict[str, Any] = {}
_CACHE_CDN = ""
_CACHE_AT = 0.0
_CACHE_TTL = 6 * 3600  # 6 小时
#: 下载失败后的短缓存（避免批量操作时每个女优都重试所有 CDN 造成超时堆积）
_FAIL_AT = 0.0
_FAIL_TTL = 90  # 秒


def _build_opener(proxy: str = "") -> urllib.request.OpenerDirector:
    """构造 urllib opener，可选用代理（复用刮削配置的 scraper.proxy）。"""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    handlers.append(urllib.request.HTTPSHandler())
    return urllib.request.build_opener(*handlers)


def _load_file_tree(timeout: float = 8.0, proxy: str = "") -> "tuple[Dict[str, str], str]":
    """下载并缓存 gfriends Filetree.json（多 CDN 逐个尝试）。

    proxy 复用刮削配置的 scraper.proxy。返回 (flat_tree, cdn_base)：
    flat_tree 为 {演员名: Content/公司/文件}，cdn_base 为成功访问的 CDN 前缀。
    全部失败抛 Exception。
    """
    global _CACHE, _CACHE_CDN, _CACHE_AT, _FAIL_AT
    now = time.time()
    if _CACHE and (now - _CACHE_AT) < _CACHE_TTL:
        return _CACHE, _CACHE_CDN
    # 最近失败过 → 快速失败，不再重试全部 CDN
    if _FAIL_AT and (now - _FAIL_AT) < _FAIL_TTL:
        raise RuntimeError("gfriends 仓库暂不可达（短时间缓存失败）")
    opener = _build_opener(proxy)
    last_err = None
    for url, cdn in zip(FILE_TREE_URLS, CDN_BASES):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "AVM/1.0", "Accept-Encoding": "gzip"})
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                # 有的 CDN 返回 gzip 压缩体，需解压后再解析（能显著缩小传输量）
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                data = json.loads(raw.decode("utf-8"))
            flat: Dict[str, str] = {}
            content = data.get("Content") or {}
            for company, actors in content.items():
                if not isinstance(actors, dict):
                    continue
                for actor_name, filename in actors.items():
                    flat[actor_name] = f"Content/{company}/{filename}"
            _CACHE = flat
            _CACHE_CDN = cdn
            _CACHE_AT = now
            return flat, cdn
        except Exception as e:  # noqa: BLE001
            last_err = e
    _FAIL_AT = now
    raise RuntimeError(f"无法访问 gfriends 仓库：{last_err}")


class GfriendsPlugin(ActressPlugin):
    id = "gfriends"
    name = "gfriends 头像"
    description = "从 gfriends 开源头像仓库（jsDelivr CDN）拉取女优头像。仅提供头像，需联网。"
    capabilities = ["avatar"]
    needs_network = True

    def fetch(self, name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
        proxy = (cfg.get("scraper", {}) or {}).get("proxy") or ""
        tree, cdn = _load_file_tree(proxy=proxy)
        if not name:
            raise ValueError("名字为空")
        # 精确匹配
        if name in tree:
            return {"avatar_url": cdn + tree[name]}
        # 去空格后匹配（gfriends 名字可能带空格差异）
        key = name.replace(" ", "")
        for actor_name, rel in tree.items():
            if actor_name.replace(" ", "") == key:
                return {"avatar_url": cdn + rel}
        # 子串匹配（容忍前后缀差异），返回第一个匹配
        for actor_name, rel in tree.items():
            if key and (key in actor_name.replace(" ", "") or actor_name.replace(" ", "") in key):
                return {"avatar_url": cdn + rel}
        raise ValueError(f"未在 gfriends 找到「{name}」的头像")
