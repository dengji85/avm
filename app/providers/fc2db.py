# -*- coding: utf-8 -*-
"""FC2DB 数据源：FC2-PPV 番号专用元数据（自研源）。

为什么不靠 MetaTube 的 FC2 源：
* FC2 官网详情页 2025 年起要求登录，匿名请求直接 404 / 登录页；
* fc2ppvdb.com 换域名后长期被 Cloudflare 拦（526 / JS 挑战），fc2hub 判 invalid id；
* MetaTube 把源站规则写死在 Go 二进制里，本地改不了，只能等上游更新。

fc2db.net 是 WordPress 站，用它开放的 REST 接口，两次请求拿全核心字段：
  1. ``/wp-json/wp/v2/search?search=<编号>&subtype=work`` → 定位作品 post id
     （编号要用纯数字，站内标题不带 ``FC2-PPV-`` 前缀）；
  2. ``/wp-json/wp/v2/work/<id>?_embed=1`` → 标题 / 贩卖日 / 封面 / 标签。

请求头必须"少而素"（实测）：带完整浏览器头（Accept/Language/Referer/Sec-Fetch）
会被站点的 Cloudflare Turnstile 当成可疑浏览器，直接返回「年齢確認」挑战页；
只带一个朴素 UA 反而稳定拿到 JSON。所以这里不走 ``BaseProvider.http_get``
（它负责补齐浏览器头），改为自建最小头请求。

本数据源只处理 FC2 番号（``handles=("fc2",)``），其它番号零请求开销。
"""
from __future__ import annotations

import html as _html
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .base import BaseProvider, MetaResult, NetError

_DEFAULT_BASE = "https://fc2db.net"

#: 只放最必要的头：多余浏览器特征会触发 Turnstile 挑战页
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
    "Accept": "application/json",
}

#: 站点有频率限制：密集请求会回人机验证页（约 1 分钟内恢复）。
#: 批量刮削是多线程并行，用进程内最小间隔把请求摊开，避免自己把自己限死。
_MIN_GAP = 1.2
_GAP_LOCK = threading.Lock()
_LAST_AT = 0.0


def _throttle(gap: float = _MIN_GAP) -> None:
    global _LAST_AT
    with _GAP_LOCK:
        wait = gap - (time.monotonic() - _LAST_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_AT = time.monotonic()


class Fc2dbProvider(BaseProvider):
    name = "fc2db"
    label = "FC2DB"
    desc = "FC2-PPV 专用：标题 / 封面 / 贩卖日 / 标签（MetaTube 的 FC2 源失效后的替代）"
    #: 只处理 FC2：普通番号它没有条目，避免每部片都空转一次
    handles = ("fc2",)

    # -------------------------------------------------------------- 工具

    @property
    def base(self) -> str:
        return str(self.options.get("base_url") or _DEFAULT_BASE).rstrip("/")

    @staticmethod
    def num_of(code: str) -> str:
        """从番号取 FC2 商品 ID（6–8 位数字）。

        ``FC2-PPV-4936167`` / ``FC2-4936167`` / ``fc2ppv4936167`` / ``4936167``
        都要能取到 ``4936167``。取最长数字段是因为 ``FC2`` 自己带一个 2。
        """
        runs = re.findall(r"\d+", str(code or ""))
        if not runs:
            return ""
        cand = max(runs, key=len)
        return cand if 6 <= len(cand) <= 8 else ""

    def _get_json(self, path: str) -> Any:
        """GET 一个 REST 路径并解析 JSON。

        连不上 / 5xx / 返回人机验证页都算**临时故障**：抛 ``NetError``，让调度层
        记成 neterr（可重试、不写进跳过名单），而不是当成「这部片不存在」。
        """
        import requests

        from ..config import outbound_verify_tls

        proxy = str(self.scfg.get("proxy") or "").strip()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        _throttle()
        try:
            resp = requests.get(self.base + path, headers=_HEADERS,
                                timeout=self.timeout, proxies=proxies,
                                verify=outbound_verify_tls())
        except Exception as e:  # 连接拒绝 / DNS / 超时 / TLS
            raise NetError("FC2DB 连接失败：%s" % e)
        if resp.status_code >= 500:
            raise NetError("FC2DB 返回 HTTP %d（站点暂时不稳定）" % resp.status_code)
        if resp.status_code == 404:
            return []  # 作品不存在：按未收录处理
        if resp.status_code >= 400:
            raise NetError("FC2DB 返回 HTTP %d" % resp.status_code)
        text = resp.text or ""
        try:
            return json.loads(text)
        except Exception:
            # 剩下几乎都是「年齢確認 / Turnstile」挑战页（HTML）
            raise NetError("FC2DB 触发人机验证（请求过于频繁），稍后重试")

    # -------------------------------------------------------------- 抓取

    def fetch(self, movie: Dict[str, Any]) -> Optional[MetaResult]:
        self.last_error = ""  # 每部片重置，避免上一次的失败原因串到这一次
        num = self.num_of(str(movie.get("code") or movie.get("num") or ""))
        if not num:
            return None
        pid = self._post_id(num)
        if not pid:
            if not self.last_error:
                self.last_error = "FC2DB 未收录 FC2-PPV-%s" % num
            return None
        data = self._get_json("/wp-json/wp/v2/work/%s?_embed=1" % pid)
        if not isinstance(data, dict):
            return None
        meta = self._to_meta(data, num)
        if not meta.get("title"):
            return None
        return self.normalize(meta, self.name)

    def _post_id(self, num: str) -> str:
        """按商品编号定位作品 post id，返回空串表示没收录。"""
        data = self._get_json(
            "/wp-json/wp/v2/search?search=%s&per_page=10&subtype=work" % quote(num)
        )
        if not isinstance(data, list):
            return ""
        for item in data:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").rstrip("/")
            # 只认 /work/<同一个编号>/：搜索会把相邻编号也带回来
            if url.endswith("/work/" + num):
                return str(item.get("id") or "")
        return ""

    # ---------------------------------------------------------- 字段映射

    @staticmethod
    def _text(value: Any) -> str:
        """REST 的 rendered 字段带 HTML 标签与实体，转成纯文本。"""
        raw = value.get("rendered") if isinstance(value, dict) else value
        text = _html.unescape(str(raw or ""))
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _to_meta(self, data: Dict[str, Any], num: str) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "code": "FC2-PPV-%s" % num,
            "title": self._text(data.get("title")),
        }
        date = str(data.get("date") or "")
        if len(date) >= 10:
            meta["release_date"] = date[:10]

        embedded = data.get("_embedded") or {}
        media = (embedded.get("wp:featuredmedia") or [{}])[0]
        cover = str(media.get("source_url") or "").strip()
        if cover:
            # FC2 商品图本身就是竖版，原图兼作 fanart
            meta["cover"] = meta["fanart"] = cover

        genres = self._terms(embedded, "work_tag")
        if genres:
            meta["genres"] = genres
        return meta

    @staticmethod
    def _terms(embedded: Dict[str, Any], taxonomy: str) -> List[str]:
        out: List[str] = []
        for group in embedded.get("wp:term") or []:
            for term in group or []:
                if not isinstance(term, dict):
                    continue
                if str(term.get("taxonomy") or "") != taxonomy:
                    continue
                name = str(term.get("name") or "").strip()
                if name and name not in out:
                    out.append(name)
        return out[:12]
