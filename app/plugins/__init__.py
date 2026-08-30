# -*- coding: utf-8 -*-
"""女优资料插件包。

在此导入并注册插件。新增插件时，在此 import 并调用 register_plugin 即可，
无需改动主程序其他代码。
"""
from .base import register_plugin, list_plugins, get_plugin, enabled_plugins, ActressPlugin

# 注册内置插件
from .gfriends import GfriendsPlugin
register_plugin(GfriendsPlugin())
from .wiki_actress import WikiActressPlugin
register_plugin(WikiActressPlugin())
from .avwiki_actress import AvWikiActressPlugin
register_plugin(AvWikiActressPlugin())

# 未来新增插件在此追加，例如：
# from .javdb import JavdbProfilePlugin
# register_plugin(JavdbProfilePlugin())

__all__ = ["register_plugin", "list_plugins", "get_plugin", "enabled_plugins",
           "ActressPlugin"]
