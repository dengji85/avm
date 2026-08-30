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


#: 固定沉底的数据源：无论用户在设置里怎么排序，都强制放到最后执行。
#: av-wiki 只收录素人片，且抓取开销大（走 CDP）。若排在前面，普通番号也要
#: 先去它那空转一次（实测约 20s/部），严重拖慢整体刮削。作为补充源沉底，
#: 只在前面主源没给出女优真名时才补查，普通片完全不受影响。
ALWAYS_LAST = ("avwiki",)


def build_providers(cfg: Dict[str, Any]) -> List[BaseProvider]:
    """按配置里的 order 顺序实例化所有已启用的数据源（去重）。

    注意：``ALWAYS_LAST`` 里的源（avwiki）会被强制移到最后，用户在设置里
    把它拖到前面也不会生效——这是有意为之的性能保护。
    """
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
    # 稳定沉底：保持其余源的相对顺序，只把 ALWAYS_LAST 移到末尾
    if len(providers) > 1:
        tail = [p for p in providers if p.name in ALWAYS_LAST]
        if tail:
            head = [p for p in providers if p.name not in ALWAYS_LAST]
            providers = head + tail
    return providers


def describe() -> List[Dict[str, str]]:
    return [
        {"id": name, "label": cls.label, "desc": cls.desc}
        for name, cls in REGISTRY.items()
    ]


__all__ = ["BaseProvider", "MetaResult", "REGISTRY", "build_providers", "describe"]
