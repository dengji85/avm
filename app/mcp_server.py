# -*- coding: utf-8 -*-
"""MCP 服务器：把片匣的库能力暴露成标准工具，交给现成的 agent 客户端驱动。

为什么这样做，而不是自己写一个 agent：
* 规划、联网搜索、多轮工具调用这些能力，MCP 客户端（Claude Desktop、Cursor、
  Cherry Studio 等）本来就自带；我们只要把「工具」这层做好，就能被任何客户端使用；
* 工具是纯函数、只读本地 SQLite，不需要把界面、会话、流式输出再做一遍。

启动（需要额外装一个依赖）：

    pip install mcp
    python run.py mcp

然后在客户端的 MCP 配置里加一条 stdio 命令：
    {"command": "python", "args": ["run.py", "mcp"], "cwd": "<项目目录>"}

暴露的工具：
    library_vocab   库字典（真实类型/标签/片商/系列/女优 + 数量）
    search_movies   按条件检索影片
    movie_detail    按番号或 id 取完整资料
    library_stats   口味 / 观看统计 / 库健康 / 最近入库 / 推荐
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import ai_library, store, vocab
from .agent import card as _card, detail as _detail
from .db import db, init_db

INSTRUCTIONS = (
    "这是本机的成人影片库（片匣 / AVM），数据全在本地、不联网，只读。\n"
    "建议流程：先调 library_vocab 看库里真实存在的类型 / 标签 / 女优写法"
    "（可能是日语或繁体），再用 search_movies 检索；统计类问题用 library_stats。\n"
    "结果里带 id 与 code，可用 movie_detail 取完整资料。"
)


def build():
    """构建 FastMCP 实例；未安装 mcp 时抛 ImportError（由 CLI 给出安装提示）。"""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("avm", instructions=INSTRUCTIONS)
    init_db()

    @mcp.tool()
    def library_vocab() -> str:
        """库字典：本库真实存在的类型 / 标签 / 片商 / 系列 / 女优（含数量）。检索前先看它。"""
        with db() as conn:
            return vocab.block(conn)

    @mcp.tool()
    def search_movies(
        q: Optional[List[str]] = None,
        genres: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        actress: str = "",
        studio: str = "",
        series: str = "",
        like: Optional[List[str]] = None,
        actress_age_min: int = 0,
        actress_age_max: int = 0,
        year_min: int = 0,
        year_max: int = 0,
        min_rating: float = 0,
        flags: Optional[List[str]] = None,
        sort: str = "added_desc",
        limit: int = 12,
    ) -> Dict[str, Any]:
        """按条件检索影片。genres / tags / actress 请使用 library_vocab 里出现过的原文。"""
        params: Dict[str, Any] = {
            "page_size": max(1, min(30, int(limit or 12))),
            "sort": sort or "added_desc",
        }
        if q:
            params["q"] = list(q)
        if genres:
            params["genres"] = list(genres)
        if tags:
            params["tags"] = list(tags)
        if like:
            params["like"] = list(like)
        if actress:
            params["actress"] = actress
        if studio:
            params["studio"] = studio
        if series:
            params["series"] = series
        if actress_age_min:
            params["actress_age_min"] = int(actress_age_min)
        if actress_age_max:
            params["actress_age_max"] = int(actress_age_max)
        if year_min:
            params["year_min"] = int(year_min)
        if year_max:
            params["year_max"] = int(year_max)
        if min_rating:
            params["min_rating"] = float(min_rating)
        if flags:
            params["flags"] = list(flags)

        with db() as conn:
            res = ai_library.execute_spec(conn, {"kind": "search", "params": params})
        items = [_card(m) for m in (res.get("items") or [])]
        return {
            "count": len(items),
            "understood_as": res.get("note") or "",
            "items": items,
            "hint": "" if items else "没有命中。可以先调 library_vocab 看库里实际怎么写这个类型/标签。",
        }

    @mcp.tool()
    def movie_detail(code_or_id: str) -> Dict[str, Any]:
        """按番号（如 IPX-123）或库内 id 取一部影片的完整资料。"""
        key = str(code_or_id or "").strip()
        if not key:
            return {"found": False, "error": "code_or_id 为空"}
        with db() as conn:
            mid = int(key) if key.isdigit() else 0
            if not mid:
                res = store.search_movies(conn, {"q": [key], "page_size": 1})
                items = res.get("items") or []
                if not items:
                    return {"found": False, "error": "没有匹配的影片"}
                mid = int(items[0]["id"])
            m = store.movie_detail(conn, mid)
            if not m:
                return {"found": False, "error": "该 id 不存在"}
            return {"found": True, "movie": _detail(m)}

    @mcp.tool()
    def library_stats(kind: str = "health", limit: int = 8, year: int = 0) -> Dict[str, Any]:
        """整库统计：taste（口味画像）/ watch_stats（观看统计）/ health（库健康）/
        recent（最近入库）/ recommend（按口味推荐）。year 仅用于 taste。"""
        k = str(kind or "health").strip()
        if k not in ("taste", "watch_stats", "health", "recent", "recommend"):
            return {"error": "kind 只能是 taste / watch_stats / health / recent / recommend"}
        params: Dict[str, Any] = {}
        if k in ("recent", "recommend"):
            params["limit"] = max(1, min(24, int(limit or 8)))
        if k == "taste" and year:
            params["year"] = int(year)
        with db() as conn:
            res = ai_library.execute_spec(conn, {"kind": k, "params": params})
        return {
            "kind": res.get("kind"),
            "rows": res.get("rows") or [],
            "items": [_card(m) for m in (res.get("items") or [])],
        }

    return mcp


def run() -> int:
    try:
        server = build()
    except ImportError:
        print("MCP 服务器需要额外依赖：pip install mcp")
        return 2
    server.run()
    return 0
