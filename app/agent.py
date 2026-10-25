# -*- coding: utf-8 -*-
"""内置 agent：多步工具调用，供 AI 助手直接使用（不需要外部客户端）。

与「单步规划」（``ai_library._plan``）的区别：
* 单步：模型看一次库字典，直接给出一份查询规格；错了只能靠一次重规划；
* 本模块：模型可以**连续调工具**——先看库字典，再检索，结果不理想就换词或放宽，
  最后用一句自然话说结论。步数与总调用次数都有硬上限，不会失控。

与 MCP 的关系：同一套工具实现（``search_movies`` / ``movie_detail`` /
``library_stats`` / ``library_vocab``），MCP 对外给第三方客户端，agent 对内给
助手面板用；改工具只改这里。

为什么不用 agent 框架：我们的模型客户端（``ai_core``）已经管着重试、超时、
用量统计、调用轨迹与模型池。框架通常自带一套 HTTP 客户端，接进来要么绕开这些
能力，要么写一层适配器；而 agent 循环本身只有几十行。真正决定效果的是工具集与
提示词，不是编排层。若将来要接框架，把 ``TOOLS`` 原样喂过去即可。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import ai_library, store, vocab, websearch
from .ai_core import AIConfig, AiError, chat_stream, chat_tools
from .db import db

MAX_STEPS = 4
# 单次工具结果回灌给模型时截断：卡片再多也没用，还烧 token
_TOOL_RESULT_CHARS = 3500

_SYSTEM = (
    "你是片匣（本地影片库）的助手。你可以调用工具查这个库，目标是用最少的步骤给出可用结果。\n"
    "\n"
    "工作方式：\n"
    "1. 先判断问题属于哪一类：找片 → search_movies；统计 / 口味 / 健康 / 推荐 → library_stats；"
    "问某一部 → movie_detail。\n"
    "2. 类型 / 标签 / 女优名**必须**用库里真实存在的写法。字典已在用户消息里给出；"
    "如果拿不准某个写法，先调 library_vocab 或直接用 like 模糊匹配。\n"
    "3. 第一次检索 0 条时不要马上放弃：换同义词、放宽条件（去掉太细的筛选）再试一次。\n"
    "4. **一旦某个检索返回了结果，就立刻收尾**：不要再为了「更全」去换别的同义词，"
    "最多换一次词。反复搜索会把步数耗光，用户反而看不到结论。\n"
    "5. 有结果后用一两句中文收尾，说明你查了什么、结论是什么；不要罗列全部字段。\n"
    "6. 不要编造库里没有的影片；工具没返回的内容就说没查到。\n"
    "7. 库里没有的信息（剧情简介、口碑评价、演员资料），或用户要求「查一下 / 搜一下 / 联网」，"
    "用 web_lookup：搜索词带上番号效果最好（如 \"IPX-123 简介\"）。\n"
    "8. 联网结论必须注明来自哪个站点（写域名即可）；只转述抓到的内容，抓不到正文的结果只引用摘要，"
    "绝不要凭记忆补。\n"
    "9. 用户说「这部 / 当前片」但消息里**没有**【当前影片】时，这个指代落空了："
    "**不要**用 search_movies 去「找讲什么的影片」来凑答案，直接说明没有打开影片，"
    "请他先打开一部或直接给出番号。\n"
)

_DETAIL_KEYS = ("id", "code", "title", "original_title", "plot", "release_date", "year",
                "runtime", "rating", "studio", "series", "director", "actresses", "genres",
                "tags", "watched", "play_count", "resolution")

_CARD_KEYS = ("id", "code", "title", "release_date", "year", "runtime", "rating",
              "actresses", "genres", "tags", "watched")


def card(m: Dict[str, Any]) -> Dict[str, Any]:
    """返回给模型 / 前端的紧凑影片卡片（不含封面路径、文件列表）。"""
    return {k: m.get(k) for k in _CARD_KEYS if m.get(k) not in (None, "", [])}


def detail(m: Dict[str, Any]) -> Dict[str, Any]:
    return {k: m.get(k) for k in _DETAIL_KEYS if m.get(k) not in (None, "", [])}


# ------------------------------------------------------------------ 工具实现

def _tool_web(conn, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """联网查资料：搜索 + 抓正文，交给模型汇总（结论必须带来源）。"""
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query 为空"}
    try:
        return websearch.lookup(ctx.get("cfg"), query, limit=int(args.get("limit") or 3))
    except Exception as e:                        # noqa: BLE001 错误信息直接给模型转述给用户
        return {"error": str(e)[:200]}


def _tool_search(conn, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "page_size": max(1, min(20, int(args.get("page_size") or 8))),
        "sort": str(args.get("sort") or "added_desc"),
    }
    for key in ("q", "genres", "tags", "like", "flags"):
        val = args.get(key)
        if val:
            params[key] = val if isinstance(val, (list, tuple)) else [val]
    for key in ("actress", "studio", "series"):
        if args.get(key):
            params[key] = str(args[key])
    for key in ("actress_age_min", "actress_age_max", "year_min", "year_max"):
        if args.get(key):
            params[key] = int(args[key])
    if args.get("min_rating"):
        params["min_rating"] = float(args["min_rating"])

    res = ai_library.execute_spec(conn, {"kind": "search", "params": params})
    items = [card(m) for m in (res.get("items") or [])]
    return {
        "count": len(items),
        "understood_as": res.get("note") or "",
        "items": items,
        "hint": "" if items else "0 条。换同义词或去掉部分条件再试，不要重复同样的查询。",
    }


def _tool_detail(conn, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    key = str(args.get("code_or_id") or "").strip()
    if not key:
        return {"found": False, "error": "code_or_id 为空"}
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
    return {"found": True, "movie": detail(m)}


def _tool_stats(conn, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(args.get("kind") or "health")
    if kind not in ("taste", "watch_stats", "health", "recent", "recommend"):
        return {"error": "kind 只能是 taste/watch_stats/health/recent/recommend"}
    params: Dict[str, Any] = {}
    if kind in ("recent", "recommend"):
        params["limit"] = max(1, min(24, int(args.get("limit") or 8)))
    if kind == "taste" and args.get("year"):
        params["year"] = int(args["year"])
    res = ai_library.execute_spec(conn, {"kind": kind, "params": params})
    return {
        "kind": res.get("kind"),
        "rows": res.get("rows") or [],
        "items": [card(m) for m in (res.get("items") or [])],
    }


def _tool_vocab(conn, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    return {"vocab": vocab.block(conn)}


TOOLS: List[Dict[str, Any]] = [
    {
        "schema": {
            "type": "function",
            "function": {
                "name": "search_movies",
                "description": "在本地影片库按条件检索影片（只读、不联网）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "q": {"type": "array", "items": {"type": "string"},
                              "description": "关键词：番号/标题/简介/导演/女优名"},
                        "genres": {"type": "array", "items": {"type": "string"},
                                   "description": "类型名，用库字典里的原文"},
                        "tags": {"type": "array", "items": {"type": "string"},
                                 "description": "标签名，用库字典里的原文"},
                        "actress": {"type": "string"},
                        "studio": {"type": "string"},
                        "series": {"type": "string"},
                        "like": {"type": "array", "items": {"type": "string"},
                                 "description": "题材词模糊匹配（字典里没有对应写法时用）"},
                        "actress_age_min": {"type": "integer", "description": "女优年龄下限（按发行年）"},
                        "actress_age_max": {"type": "integer", "description": "女优年龄上限"},
                        "year_min": {"type": "integer"},
                        "year_max": {"type": "integer"},
                        "min_rating": {"type": "number"},
                        "flags": {"type": "array", "items": {"type": "string"}},
                        "sort": {"type": "string"},
                        "page_size": {"type": "integer"},
                    },
                },
            },
        },
        "fn": _tool_search,
        "label": "检索",
    },
    {
        "schema": {
            "type": "function",
            "function": {
                "name": "movie_detail",
                "description": "按番号或库内 id 取一部影片的完整资料",
                "parameters": {
                    "type": "object",
                    "properties": {"code_or_id": {"type": "string"}},
                    "required": ["code_or_id"],
                },
            },
        },
        "fn": _tool_detail,
        "label": "影片资料",
    },
    {
        "schema": {
            "type": "function",
            "function": {
                "name": "library_stats",
                "description": "整库统计：口味画像 / 观看统计 / 库健康 / 最近入库 / 推荐",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string",
                                 "enum": ["taste", "watch_stats", "health", "recent", "recommend"]},
                        "limit": {"type": "integer"},
                        "year": {"type": "integer"},
                    },
                    "required": ["kind"],
                },
            },
        },
        "fn": _tool_stats,
        "label": "统计",
    },
    {
        "schema": {
            "type": "function",
            "function": {
                "name": "library_vocab",
                "description": "库字典：真实存在的类型/标签/片商/系列/女优（含数量）",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        "fn": _tool_vocab,
        "label": "库字典",
    },
    {
        "schema": {
            "type": "function",
            "function": {
                "name": "web_lookup",
                "description": "联网搜索并抓取网页正文（需要用户已配置搜索后端）。"
                               "用于：库里没有的元数据、用户要求「查一下/搜一下」、"
                               "需要剧情简介或口碑等库内没有的信息。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "搜索词：番号 + 关键词效果最好，如 IPX-123 简介"},
                        "limit": {"type": "integer", "description": "要抓几个网页，默认 3"},
                    },
                    "required": ["query"],
                },
            },
        },
        "fn": _tool_web,
        "label": "联网查询",
    },
]

_BY_NAME: Dict[str, Dict[str, Any]] = {t["schema"]["function"]["name"]: t for t in TOOLS}


def _exec_tool(conn, call: Dict[str, Any],
               ctx: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """执行一次工具调用，返回 (结果, 步骤摘要)。异常一律转成工具错误，不中断循环。"""
    name = str(call.get("name") or "")
    t0 = time.time()
    try:
        args = json.loads(call.get("arguments") or "{}")
        if not isinstance(args, dict):
            raise ValueError("参数不是对象")
    except (ValueError, TypeError) as e:
        return ({"error": "参数解析失败：%s" % e},
                {"name": name, "ok": False, "ms": 0, "summary": "参数解析失败"})

    tool = _BY_NAME.get(name)
    if not tool:
        return ({"error": "未知工具 %s" % name},
                {"name": name, "ok": False, "ms": 0, "summary": "未知工具"})

    try:
        out = tool["fn"](conn, args, ctx or {})
    except Exception as e:  # noqa: BLE001 工具失败要回灌给模型继续，而不是整轮报错
        return ({"error": "%s: %s" % (type(e).__name__, str(e)[:200])},
                {"name": name, "ok": False, "ms": int((time.time() - t0) * 1000),
                 "summary": "执行失败"})

    summary = ""
    if isinstance(out, dict):
        if "count" in out:
            summary = "%d 条%s" % (out.get("count") or 0,
                                   ("（%s）" % out["understood_as"]) if out.get("understood_as") else "")
        elif out.get("found") is not None:
            summary = "已取到资料" if out.get("found") else str(out.get("error") or "未找到")
        elif out.get("rows"):
            summary = "%d 行统计" % len(out["rows"])
        elif out.get("items"):
            summary = "%d 条" % len(out["items"])
        elif out.get("vocab"):
            summary = "已读取库字典"
    return (out, {"name": name, "label": tool["label"], "ok": True,
                  "ms": int((time.time() - t0) * 1000), "summary": summary})


# ------------------------------------------------------------------ 入口

def _merge_result(best: Optional[Dict[str, Any]], out: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把多轮检索结果**累积**起来，供前端渲染卡片。

    模型常会为一个问题换几次词（「連褲襪」→「成熟的女人」），只展示最后一次会让
    用户以为「丝袜那部没查到」——而模型结论里明明提到了它。按 id 去重后合并。
    """
    if not isinstance(out, dict):
        return best
    if out.get("rows"):                       # 统计类结果直接覆盖
        return {"items": [], "rows": out["rows"], "note": best.get("note", "") if best else ""}

    items = out.get("items") or []
    if not items:
        return best

    prev = best or {}
    merged: List[Dict[str, Any]] = list(prev.get("items") or [])
    seen = {m.get("id") for m in merged}
    for m in items:
        if m.get("id") not in seen:
            merged.append(m)
            seen.add(m.get("id"))

    note = out.get("understood_as") or out.get("note") or ""
    prev_note = prev.get("note") or ""
    if note and note not in prev_note:
        note = (prev_note + " · " + note).strip(" ·") if prev_note else note
    else:
        note = prev_note
    return {"items": merged[:20], "rows": [], "note": note[:200]}


_FINAL_PROMPT = "请用一两句中文总结上面的检索结果，不要调用工具，不要罗列字段。"


def _done(answer: str, steps: List[Dict[str, Any]], best: Optional[Dict[str, Any]],
          stopped: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "type": "done",
        "answer": answer,
        "steps": steps,
        "items": (best or {}).get("items", []),
        "rows": (best or {}).get("rows", []),
        "note": (best or {}).get("note", ""),
        "stopped": stopped,
    }
    if extra:
        payload.update(extra)
    return payload


def iter_agent(question: str, cfg: Optional[Dict[str, Any]] = None, *,
               movie: Optional[Dict[str, Any]] = None,
               max_steps: int = MAX_STEPS) -> Iterator[Dict[str, Any]]:
    """把一轮 agent 跑成**事件流**：调一次工具就吐一条 ``step``，结论边生成边吐 ``delta``。

    事件类型：
      * ``step``  某次工具调用结束（工具名 / 摘要 / 耗时）——立即上屏，用户能看到它在干什么；
      * ``delta`` 结论文本增量（流式）；
      * ``answer`` 非流式的一次性结论（模型直接开口、没有调工具时）；
      * ``done``  最终结果，字段与 ``run_agent`` 返回一致。

    收尾固定用「不带工具的一次流式调用」：模型没有工具可调，就不会再拖延，
    用户能马上看到字一个个出来，而不是等十几秒后整段蹦出来。
    """
    q = str(question or "").strip()
    if not q:
        yield _done("", [], None, "empty")
        return

    c = AIConfig.from_config(cfg)
    # 工具上下文：联网查询需要拿到原始配置（SearXNG 地址 / Tavily Key）
    ctx: Dict[str, Any] = {"cfg": cfg}
    web_ok = websearch.enabled(cfg)
    movie_ctx = ""
    if isinstance(movie, dict):
        code = str(movie.get("code") or "").strip()
        title = str(movie.get("title") or "").strip()
        if code or title:
            movie_ctx = "\n\n【当前影片】%s%s（用户正开着这部；问题里的「这部 / 当前片」指它）" % (
                code, ("《%s》" % title) if title else "")
    steps: List[Dict[str, Any]] = []
    best: Optional[Dict[str, Any]] = None
    stopped = ""

    with db() as conn:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "【库字典】\n%s\n\n【用户问题】%s%s"
                                        % (vocab.block(conn), q, movie_ctx)},
        ]
        for _ in range(max(1, int(max_steps))):
            # 一旦查到结果，就把「检索」这个工具收走：模型只剩「看详情 / 收尾」两条路。
            # 只靠提示词约束不住——实测它会为一个问题连换 6 个同义词，把步数烧光还不给结论。
            tools_now = [t["schema"] for t in TOOLS
                         if t["schema"]["function"]["name"] != "web_lookup" or web_ok]
            if best and best.get("items"):
                tools_now = [s for s in tools_now
                             if s["function"]["name"] != "search_movies"]
            try:
                out = chat_tools(c, messages, tools=tools_now, purpose="agent.step")
            except AiError as e:
                if getattr(e, "retryable", True):
                    raise
                # 端点不认 tools（4xx）：退回单步规划，别让用户看到 502
                res = ai_library.ask_library(q, cfg)
                plan = res.get("plan") or {}
                step = {"name": "library_qa", "label": "规划", "ok": True, "ms": 0,
                        "summary": plan.get("why") or ""}
                yield {"type": "step", "step": step}
                yield _done("", [step],
                            {"items": [card(m) for m in (res.get("items") or [])],
                             "rows": res.get("rows") or [],
                             "note": res.get("note") or ""},
                            "", {"fallback": "library_qa"})
                return

            calls = out.get("tool_calls") or []
            if not calls:
                answer = str(out.get("content") or "").strip()
                yield {"type": "answer", "text": answer}
                yield _done(answer, steps, best, "")
                return

            # 回灌时要把 assistant 的 tool_calls 原样带上，否则下一轮接口会报错
            messages.append({
                "role": "assistant",
                "content": str(out.get("content") or ""),
                "tool_calls": [{
                    "id": call.get("id") or "",
                    "type": "function",
                    "function": {"name": call.get("name") or "",
                                 "arguments": call.get("arguments") or "{}"},
                } for call in calls],
            })
            for call in calls:
                result, step = _exec_tool(conn, call, ctx)
                steps.append(step)
                best = _merge_result(best, result)
                payload = json.dumps(result, ensure_ascii=False)
                if len(payload) > _TOOL_RESULT_CHARS:
                    payload = payload[:_TOOL_RESULT_CHARS] + "…（已截断）"
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": payload,
                })
                yield {"type": "step", "step": step}

            if best and best.get("items"):
                break                        # 已经查到东西：不再换词，转去写结论
        else:
            stopped = "max_steps"

        # 收尾：不带工具的一次流式调用。模型没有工具可调，只能把话说完。
        answer = ""
        text = ""
        try:
            for piece in chat_stream(c, messages + [{"role": "user", "content": _FINAL_PROMPT}],
                                     purpose="agent.final"):
                text += piece
                yield {"type": "delta", "text": piece}
            answer = text.strip()
        except AiError:
            answer = text.strip()
        yield _done(answer, steps, best, stopped)


def run_agent(question: str, cfg: Optional[Dict[str, Any]] = None, *,
              movie: Optional[Dict[str, Any]] = None,
              max_steps: int = MAX_STEPS) -> Dict[str, Any]:
    """非流式版本：消费 ``iter_agent``，接口与 CLI 继续按原样使用。

    返回 ``{answer, steps, items, rows, note, stopped}``。
    """
    final: Dict[str, Any] = {"answer": "", "steps": [], "items": [], "rows": [],
                             "note": "", "stopped": ""}
    for ev in iter_agent(question, cfg, movie=movie, max_steps=max_steps):
        if ev.get("type") == "done":
            final = {k: v for k, v in ev.items() if k != "type"}
    return final
