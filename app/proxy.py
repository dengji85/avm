# -*- coding: utf-8 -*-
"""统一出站代理：全局 ``network.proxy``，按调用方覆盖；本机 / 内网强制直连。

设计取舍：

* 代理是网络层设施，用户环境通常只有一个出口（Clash / V2Ray 的一个端口）。
  早期每个子系统各自读 ``scraper.proxy`` / ``stream.upstream_proxy``，
  结果是「刮削走代理、封面下载直连」这种半通不通的状态，所以收敛到一处；
* 数据源仍可用自己的 ``proxy`` 字段覆盖全局（少数站点需要不同出口）；
* 127.0.0.1 / 局域网 / ``.local`` / 字面量内网 IP 一律直连 —— 给本地
  Emby、DLNA、托管在 127.0.0.1 的 MetaTube 套代理只会绕坏；
* 留空 = 直连，仍跟随 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量（requests 默认行为）。
"""
from __future__ import annotations

import ipaddress
from typing import Dict, Optional
from urllib.parse import urlsplit

from .config import load_config


def global_proxy() -> str:
    """全局代理地址；空串 = 直连。"""
    return str((load_config().get("network") or {}).get("proxy") or "").strip()


def _direct_host(url: str) -> bool:
    """本机 / 内网 / 拿不到主机的地址一律直连。"""
    try:
        p = urlsplit(str(url or ""))
    except ValueError:
        return True
    host = (p.hostname or "").lower()
    if not host:
        return True
    if host in ("localhost", "::1", "0.0.0.0") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False                       # 域名：可能是公网，交给代理
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_unspecified)


def proxies_for(url: str = "", override: str = "") -> Optional[Dict[str, str]]:
    """统一出站代理：``override`` > 全局 ``network.proxy``；内网返回 ``None``。

    返回值可直接作为 ``requests`` 的 ``proxies=`` 参数（None 表示直连）。
    """
    p = str(override or "").strip() or global_proxy()
    if not p or _direct_host(url):
        return None
    return {"http": p, "https": p}
