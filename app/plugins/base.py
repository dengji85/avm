# -*- coding: utf-8 -*-
"""女优资料插件框架。

插件是一个独立的、可选启用的模块，用于为女优档案补充资料（头像、三围、年龄等）。
任何实现 ActressPlugin 接口的类都可以通过 settings 启用，无需改动主程序。

用法（在 __init__.py 里注册）：
    from .base import register_plugin
    from .gfriends import GfriendsPlugin
    register_plugin(GfriendsPlugin())
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class ActressPlugin:
    """女优资料插件基类。

    子类需实现：
      - id / name / description / capabilities
      - fetch(name) -> dict
    可选实现：
      - needs_config 属性
    """

    #: 唯一标识（用于配置开关）
    id: str = "base"
    #: 展示名称
    name: str = "基类"
    #: 描述（在设置页展示）
    description: str = ""
    #: 能力列表：['avatar', 'measurements', 'birthday', 'height', ...]
    capabilities: List[str] = []
    #: 是否需要联网（仅作信息展示：供设置页提示用户该插件会发起网络请求）
    needs_network: bool = True

    def fetch(self, name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """按女优名字拉取资料。

        返回结构（字段可选，能提供什么返回什么）：
            {
                "name": str,          # 规范化后的名字（可选）
                "avatar_url": str,    # 头像 URL（可选）
                "height": str,        # 身高 cm（可选）
                "bust": str,          # 胸围 cm（可选）
                "waist": str,         # 腰围 cm（可选）
                "hip": str,           # 臀围 cm（可选）
                "cup": str,           # 罩杯（可选）
                "birthday": str,      # 生日 YYYY-MM-DD（可选）
            }
        拉取不到或出错应抛出 Exception，由调用方统一处理。
        """
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        """返回插件的元信息（供设置页 / API 展示）。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "capabilities": self.capabilities,
            "needs_network": self.needs_network,
        }


#: 已注册的插件（按注册顺序）
_REGISTRY: Dict[str, ActressPlugin] = {}


def register_plugin(plugin: ActressPlugin) -> None:
    """注册一个插件到全局注册表。"""
    _REGISTRY[plugin.id] = plugin


def get_plugin(plugin_id: str) -> Optional[ActressPlugin]:
    return _REGISTRY.get(plugin_id)


def list_plugins() -> List[ActressPlugin]:
    return list(_REGISTRY.values())


def enabled_plugins(cfg: Dict[str, Any]) -> List[ActressPlugin]:
    """返回当前配置中已启用的插件列表。"""
    enabled_map = (cfg.get("plugins") or {}).get("enabled") or {}
    out: List[ActressPlugin] = []
    for pid, plugin in _REGISTRY.items():
        # 默认全部启用。原先按 needs_network 默认关闭联网插件，但内置的女优资料
        # 插件都要联网（wiki/avwiki/gfriends 无一例外），结果就是开箱状态下
        # 「女优资料抓取」因没有任何可用插件而直接报错，用户必须先进设置逐个开启。
        default = True
        if enabled_map.get(pid, default):
            out.append(plugin)
    return out
