# -*- coding: utf-8 -*-
"""MetaTube 数据源：把外部刮削引擎接成普通 provider。

不做番号范围限制（handles 为空）：FC2、无码、素人、普通番号它都有源，
真正决定「用不用它」的是配置里的 enabled 与 scraper.order 里的位置。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .base import BaseProvider, MetaResult, NetError


class MetatubeProvider(BaseProvider):
    name = "metatube"
    label = "MetaTube"
    desc = "开源刮削引擎：官方源 / FC2 / 无码 / 素人站聚合（需本地或外部服务）"

    def enabled(self) -> bool:
        """配置开启 + 服务可用才启用，避免没装服务时每部片都空转一次。"""
        from ..config import metatube_cfg

        cfg = metatube_cfg(self.cfg)
        if not cfg.get("enabled"):
            return False
        try:
            from .. import metatube
        except Exception:
            return False
        if str(cfg.get("mode") or "external") == "managed":
            return metatube.running() or metatube.installed()
        return bool(str(cfg.get("base_url") or "").strip())

    def fetch(self, movie: Dict[str, Any]) -> Optional[MetaResult]:
        code = str(movie.get("code") or movie.get("num") or "").strip()
        if not code:
            return None
        from .. import metatube
        from ..config import metatube_cfg

        # 托管模式但服务没起来：直接给可操作提示。以前是每部片都去撞一次连接
        # 失败（约 2s），批量几百部就是分钟级白等，而且失败还被当成「无此片」。
        cfg = metatube_cfg(self.cfg)
        if str(cfg.get("mode") or "external") == "managed":
            if metatube.installed() and not metatube.running():
                self.last_error = "MetaTube 托管服务未启动：请到「扩展组件」页点击启动"
                raise NetError(self.last_error)

        try:
            hits = metatube.search(code)
            hit = metatube.pick_exact(hits, code)
            if not hit:
                return None
            info = metatube.movie(str(hit.get("provider") or ""), str(hit.get("id") or ""))
            meta = metatube.to_meta(info)
            if not meta.get("title"):
                return None
            return self.normalize(meta, self.name)
        except metatube.MetaTubeError as e:
            self.last_error = str(e)
            if getattr(e, "kind", "") == "miss":
                return None  # 服务端明确 404：这次确实没有，算未命中
            # 连接失败 / 超时 / 5xx / 鉴权失败：临时故障，抛出去让调度层
            # 记成 neterr（不进跳过名单、可重试），而不是静默当成「无此片」。
            raise NetError(str(e)) from e
        except Exception as e:  # 其它异常（字段解析等）：只记录，不影响其它源
            self.last_error = str(e)
            return None
