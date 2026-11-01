# -*- coding: utf-8 -*-
"""可插拔元数据源。

本工具不内置任何第三方站点的抓取规则，数据源完全由用户在「设置」中自行配置：

* ``javbus``    在线抓取 JavBus 详情页（封面走 DMM 图床直连，主源）
* ``javdb``     在线抓取 JavDB（数据更全，需代理/ Cookie，备选）
* ``avwiki``    av-wiki.net 素人片源（化名→真名映射，无封面，封面回退其它源）
* ``fc2db``     fc2db.net 的 FC2-PPV 专用元数据（只处理 FC2 番号）
* ``metatube``  外部刮削引擎（官方源 / 无码 / 素人聚合，需配套服务）
* ``local_nfo``  读取影片同目录下的 .nfo / .json 元数据（离线，推荐）
* ``http_json``  调用用户配置的 JSON 接口
* ``http_html``  按用户配置的 CSS 选择器解析用户指定的网页

新增数据源只需继承 :class:`BaseProvider` 并在 ``REGISTRY`` 中注册。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from .avwiki import AvWikiProvider
from .base import BaseProvider, MetaResult
from .fc2db import Fc2dbProvider
from .http_html import HttpHtmlProvider
from .http_json import HttpJsonProvider
from .javbus import JavBusProvider
from .javdb import JavDBProvider
from .local_nfo import LocalNfoProvider
from .metatube import MetatubeProvider

REGISTRY: Dict[str, Type[BaseProvider]] = {
    JavBusProvider.name: JavBusProvider,
    JavDBProvider.name: JavDBProvider,
    Fc2dbProvider.name: Fc2dbProvider,
    MetatubeProvider.name: MetatubeProvider,
    AvWikiProvider.name: AvWikiProvider,
    LocalNfoProvider.name: LocalNfoProvider,
    HttpJsonProvider.name: HttpJsonProvider,
    HttpHtmlProvider.name: HttpHtmlProvider,
}


#: 固定置顶的数据源：无论用户在设置里怎么排序，都强制放到最前执行。
#: 本地 nfo/json 是用户自己整理的权威数据，且完全离线、零延迟；影片没有
#: nfo 时它直接未命中并落到在线源，没有任何额外成本。置顶可保证本地整理
#: 的元数据不被在线源覆盖（早期实现里本地 nfo 排在在线源之后，读到的标题 /
#: 女优 / 类型会被整体丢弃，只在缺封面时补图）。
#: fc2db 是 FC2 专用源，只对 FC2 番号发请求（其余番号 can_handle 直接跳过，
#: 零开销）；排在前面是为了抢在 javdb 搜索卡之前当主源 —— 搜索卡对 FC2 只
#: 给「标题=番号」的占位值，一旦它先命中，后面源的真实标题就补不进来了。
ALWAYS_FIRST = ("local_nfo", "fc2db")

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
    order = list(cfg.get("scraper", {}).get("order") or ["javbus", "javdb", "local_nfo"])
    # 老配置里没有 metatube：一旦用户在设置里打开它，自动插到 avwiki 之前，
    # 不需要用户手动改顺序（写入 config 是另一回事，这里只在内存里补位）。
    if "metatube" not in order and (cfg.get("metatube") or {}).get("enabled"):
        idx = order.index("avwiki") if "avwiki" in order else len(order)
        order.insert(idx, "metatube")
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
    # 兜底：已启用却不在 order 里的源（历史上拖动排序只保存启用中的源，会把
    # 当时关闭的源挤出顺序）追加到末尾，避免「开关开着却永远不跑」的静默失效。
    listed = {p.name for p in providers}
    for name, cls in REGISTRY.items():
        if name in listed or name in ALWAYS_LAST:
            continue
        try:
            inst = cls(cfg)
        except Exception:
            continue
        if inst.enabled():
            providers.append(inst)
    # 重排放在兜底追加之后：新源（如 fc2db）通常是靠兜底才进链的，若先重排，
    # 它在列表里还不存在，ALWAYS_FIRST 就对它不生效（FC2 片的占位标题会抢主源）。
    if len(providers) > 1:
        # 稳定置顶：保持其余源的相对顺序，只把 ALWAYS_FIRST 提到最前
        head = [p for p in providers if p.name in ALWAYS_FIRST]
        if head:
            rest = [p for p in providers if p.name not in ALWAYS_FIRST]
            providers = head + rest
        # 稳定沉底：保持其余源的相对顺序，只把 ALWAYS_LAST 移到末尾
        tail = [p for p in providers if p.name in ALWAYS_LAST]
        if tail:
            providers = [p for p in providers if p.name not in ALWAYS_LAST] + tail
    return providers


def describe() -> List[Dict[str, str]]:
    return [
        {"id": name, "label": cls.label, "desc": cls.desc}
        for name, cls in REGISTRY.items()
    ]


__all__ = ["BaseProvider", "MetaResult", "REGISTRY", "build_providers", "describe"]
