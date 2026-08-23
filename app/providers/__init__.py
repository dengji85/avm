# -*- coding: utf-8 -*-
"""可插拔元数据源。

本工具不内置任何第三方站点的抓取规则，数据源完全由用户在「设置」中自行配置：

* ``javbus``    在线抓取 JavBus 详情页（封面走 DMM 图床直连，主源）
* ``javdb``     在线抓取 JavDB（数据更全，需代理/ Cookie，备选）
* ``avwiki``    av-wiki.net 素人片源（化名→真名映射，无封面，封面回退其它源）
* ``local_nfo``  读取影片同目录下的 .nfo / .json 元数据（离线，推荐）
* ``http_json``  调用用户配置的 JSON 接口
* ``http_html``  按用户配置的 CSS 选择器解析用户指定的网页

新增数据源只需继承 :class:`BaseProvider` 并在 ``REGISTRY`` 中注册。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from .avwiki import AvWikiProvider
from .base import BaseProvider, MetaResult
from .http_html import HttpHtmlProvider
from .http_json import HttpJsonProvider
from .javbus import JavBusProvider
from .javdb import JavDBProvider
from .local_nfo import LocalNfoProvider

REGISTRY: Dict[str, Type[BaseProvider]] = {
    JavBusProvider.name: JavBusProvider,
    JavDBProvider.name: JavDBProvider,
    AvWikiProvider.name: AvWikiProvider,
    LocalNfoProvider.name: LocalNfoProvider,
    HttpJsonProvider.name: HttpJsonProvider,
    HttpHtmlProvider.name: HttpHtmlProvider,
}


def build_providers(cfg: Dict[str, Any]) -> List[BaseProvider]:
    """按配置里的 order 顺序实例化所有已启用的数据源（去重）。"""
    order = cfg.get("scraper", {}).get("order") or ["javbus", "javdb", "local_nfo"]
    seen = set()
    providers: List[BaseProvider] = []
    for name in order:
        name = str(name).strip()
        if name in seen:
            continue
        seen.add(name)
        cls = REGISTRY.get(name)
        if not cls:
            continue
        try:
            inst = cls(cfg)
        except Exception:
            continue
        if inst.enabled():
            providers.append(inst)
    return providers


def describe() -> List[Dict[str, str]]:
    return [
        {"id": name, "label": cls.label, "desc": cls.desc}
        for name, cls in REGISTRY.items()
    ]


__all__ = ["BaseProvider", "MetaResult", "REGISTRY", "build_providers", "describe"]
