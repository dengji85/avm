# -*- coding: utf-8 -*-
"""库字典：把库里真实存在的类型 / 标签 / 片商 / 系列 / 女优列出来，喂给模型。

为什么需要：用户说「丝袜」，库里存的是「パンスト・タイツ」；说「人妻」，库里有
「人妻」「已婚」「主婦」三种写法。让模型凭常识猜名字，命中率取决于运气；
把**真实值 + 数量**摆给它，它只需要做「对应」，不需要做「猜测」。

代价控制：
* 每个类目都有字符预算，按出现次数从多到少取，超了就截断——字典再大也不会
  把 prompt 撑爆；
* 同一次会话内按「影片数 + 最大 id」做签名缓存，5 分钟内复用；
* 全部在本地拼装，不发送任何影片内容（只有分类词与计数）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from . import store

DEFAULT_BUDGET = 6000

_cache: Dict[str, Any] = {"at": 0.0, "text": "", "sig": ""}


def _sig(conn) -> str:
    row = conn.execute("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m FROM movies").fetchone()
    n = row["n"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
    m = row["m"] if isinstance(row, dict) or hasattr(row, "keys") else row[1]
    return "%s:%s" % (n, m)


def _join(rows: List[Dict[str, Any]], budget: int) -> str:
    buf: List[str] = []
    used = 0
    for r in rows or []:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        item = "%s(%d)" % (name, int(r.get("count") or 0))
        if used + len(item) + 1 > budget:
            break
        buf.append(item)
        used += len(item) + 1
    return "、".join(buf)


def render(conn, budget: int = DEFAULT_BUDGET) -> str:
    """拼装库字典文本。"""
    facets = store.facets(conn, 500)
    total = 0
    try:
        total = int(conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0])
    except Exception:  # noqa: BLE001 统计失败不影响查询本身
        total = 0

    years = [int(y.get("year")) for y in (facets.get("years") or []) if y.get("year")]
    span = "，年份 %d–%d" % (min(years), max(years)) if years else ""
    parts = ["本库共 %d 部影片%s。" % (total, span)]

    for label, key, cap in (
        ("类型", "genres", 1500),
        ("标签", "tags", 1100),
        ("片商", "studios", 800),
        ("系列", "series", 800),
        ("女优", "actresses", 1100),
        ("番号前缀", "prefixes", 400),
    ):
        text = _join(facets.get(key) or [], cap)
        if text:
            parts.append("【%s】%s" % (label, text))

    out = "\n".join(parts)
    if len(out) > budget:
        # 截断按「、」切，避免把某个名字砍成半个
        out = out[:budget].rsplit("、", 1)[0]
    return out


def block(conn, budget: int = DEFAULT_BUDGET, ttl: int = 300) -> str:
    """带缓存的库字典。库有变化（影片数或最大 id 变了）会立即重建。"""
    sig = _sig(conn)
    now = time.time()
    if _cache["text"] and _cache["sig"] == sig and (now - _cache["at"]) < ttl:
        return _cache["text"]
    text = render(conn, budget)
    _cache.update(at=now, text=text, sig=sig)
    return text


def invalidate() -> None:
    """手动失效（扫描完、批量改标签后调用）。"""
    _cache.update(at=0.0, text="", sig="")
