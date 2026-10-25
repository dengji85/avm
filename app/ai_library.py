# -*- coding: utf-8 -*-
"""AI「问库」：自然语言问题 → 查询意图 → 本地执行 → 结构化结果。

隐私设计（与 search-intent 同一模式，更进一步）：
* 模型只见到**用户的问题本身**（意图解析），不发送任何库数据——
  统计、筛选、推荐全部在本机 SQLite 上执行；
* 结果以结构化数据（rows / 影片卡片）返回前端渲染，不需要模型复述；
* 意图解析的提示词与返回都可在「调用详情」里查看，轨迹只在内存。
"""
from __future__ import annotations

import shutil
import time
from typing import Any, Dict, List, Optional

from . import ext_components, store
from . import ai_prompts
from .ai_core import AIConfig, chat, chat_json, register_prompt
from .config import DATA_DIR, load_config

register_prompt("library.qa", "v1")
register_prompt("library.summarize", "v1")

_INTENT_SYSTEM = (
    "你是影片库查询解析器。把用户关于自己收藏库的问题转成 JSON 查询规格，只输出 JSON，不要解释。\n"
    '格式：{"kind":"...","params":{...}}\n'
    "kind 取值：\n"
    '- "search"：按条件找片。params 可选：keywords(数组), genres(数组), actress(字符串), '
    "studio(字符串), series(字符串), year(数字), "
    'flags(数组，可选值 subtitle/embedzh/uncensored/leak/hd4k/vr/favorite/watched/watchlist/unwatched), '
    'sort(added_desc/date_desc/rating_desc/play_desc/year_asc/size_desc), limit(数字)\n'
    '- "recent"：最近入库。params: limit(数字)\n'
    '- "taste"：口味画像（最常看的类型/女优/标签、无码倾向、观影时段等）。params: year(数字，0 或缺省=全部)\n'
    '- "watch_stats"：观看统计（看过多少部 / 场次 / 累计时长 / 本月场次）\n'
    '- "health"：库健康（影片总数、没简介/没女优/没封面的数量、库容量、磁盘剩余、依赖组件状态）\n'
    '- "recommend"：按口味推荐影片。params: limit(数字)\n'
    "判断提示：问「最常看 / 口味 / 喜欢 / 偏好什么类型或女优」→ taste；"
    "问「看了多少 / 几部 / 场次 / 时长」→ watch_stats；"
    "问「多少部没简介 / 没封面 / 磁盘 / 组件 / 库健康」→ health；"
    "问「推荐 / 今晚看什么」→ recommend；问「帮我找 / 列出 / 哪些片…」→ search。\n"
    "如果一句话里有多个子问题，只回答第一个（取最前面的那个）。\n"
    "判断不了就按 search 处理，把问题里的关键词放进 keywords。"
)

def _fmt_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f PB" % n


def _scalar(conn, sql: str, args: tuple = ()) -> Any:
    return conn.execute(sql, args).fetchone()[0]


def _cond_note(c: Dict[str, Any]) -> str:
    parts: List[str] = []
    if c.get("keywords"):
        parts.append(" ".join(c["keywords"]))
    if c.get("genres"):
        parts.append(" / ".join(c["genres"]))
    for k in ("actress", "studio", "series"):
        if c.get(k):
            parts.append(str(c[k]))
    if c.get("year"):
        parts.append(str(c["year"]))
    if c.get("flags"):
        parts.append(", ".join(c["flags"]))
    return " · ".join(str(p) for p in parts)


def _movie_items(conn, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    p = {"page": 1, "page_size": 12, "sort": "added_desc"}
    for k, v in (params or {}).items():
        if v not in (None, "", []):
            p[k] = v
    try:
        p["page_size"] = min(24, max(1, int(p.get("page_size") or 12)))
    except (TypeError, ValueError):
        p["page_size"] = 12
    res = store.search_movies(conn, p)
    return res.get("items") or []


# ------------------------------------------------------------------ 各类意图的本地执行
def _exec_search(conn, params) -> Dict[str, Any]:
    items = _movie_items(conn, params)
    return {"kind": "movies", "note": _cond_note(params), "items": items}


def _exec_taste(conn, params) -> Dict[str, Any]:
    year = 0
    try:
        year = int(params.get("year") or 0)
    except (TypeError, ValueError):
        year = 0
    prof = store.taste_profile(conn, year)
    rows = []
    for d in prof.get("dims") or []:
        rows.append({
            "key": d.get("key"), "i18n": "stats.taste_" + str(d.get("key")),
            "value": "%d%%" % round(d.get("value") or 0),
            "top": " / ".join(d.get("top") or []),
        })
    return {"kind": "rows", "note": "taste", "rows": rows}


def _exec_watch_stats(conn, params) -> Dict[str, Any]:
    month = time.strftime("%Y-%m")
    rows = [
        {"key": "watched_movies", "i18n": "ai.ws_watched_movies",
         "value": str(_scalar(conn, "SELECT COUNT(*) FROM movies WHERE watched=1"))},
        {"key": "sessions", "i18n": "ai.ws_sessions",
         "value": str(_scalar(conn, "SELECT COUNT(*) FROM watch_sessions"))},
        {"key": "total_time", "i18n": "ai.ws_total_time",
         "value": "%.1fh" % (_scalar(conn, "SELECT COALESCE(SUM(watched_sec),0) FROM watch_sessions") / 3600.0)},
        {"key": "month_sessions", "i18n": "ai.ws_month_sessions",
         "value": str(_scalar(conn, "SELECT COUNT(*) FROM watch_sessions WHERE substr(started_at,1,7)=?", (month,)))},
    ]
    return {"kind": "rows", "note": "watch_stats", "rows": rows}


def _exec_health(conn, params) -> Dict[str, Any]:
    st = store.stats(conn)
    st_ext = ext_components.status()
    comp = {c["id"]: c["state"] for c in st_ext.get("components") or []}
    rows = [
        {"key": "total_movies", "i18n": "ai.health_total_movies", "value": str(st.get("movies", 0))},
        {"key": "size", "i18n": "ai.health_size", "value": _fmt_size(st.get("size", 0))},
        {"key": "no_plot", "i18n": "ai.health_no_plot",
         "value": str(_scalar(conn, "SELECT COUNT(*) FROM movies WHERE plot=''"))},
        {"key": "no_actress", "i18n": "ai.health_no_actress",
         "value": str(_scalar(conn, "SELECT COUNT(*) FROM movies m WHERE NOT EXISTS "
                                   "(SELECT 1 FROM movie_actress mc WHERE mc.movie_id=m.id)"))},
        {"key": "no_cover", "i18n": "ai.health_no_cover",
         "value": str(_scalar(conn, "SELECT COUNT(*) FROM movies WHERE cover=''"))},
        {"key": "disk_free", "i18n": "ai.health_disk_free",
         "value": _fmt_size(shutil.disk_usage(str(DATA_DIR)).free)},
        {"key": "comp_ffmpeg", "i18n": "ai.health_comp_ffmpeg",
         "value": "ready" if comp.get("ffmpeg") == "ready" else "missing"},
        {"key": "comp_asr", "i18n": "ai.health_comp_asr",
         "value": "ready" if comp.get("asr-libs") == "ready" else "missing"},
        {"key": "comp_model", "i18n": "ai.health_comp_model",
         "value": "ready" if comp.get("asr-model-small") == "ready" else "missing"},
    ]
    return {"kind": "rows", "note": "health", "rows": rows}


def _exec_recommend(conn, params) -> Dict[str, Any]:
    try:
        limit = min(24, max(1, int(params.get("limit") or 8)))
    except (TypeError, ValueError):
        limit = 8
    res = store.recommend_movies(conn, limit, None, True, 1.0)
    return {"kind": "movies", "note": "recommend", "items": res.get("items") or []}


EXECUTORS = {
    "search": _exec_search,
    "recent": lambda conn, p: _exec_search(conn, {"sort": "added_desc", "page_size": p.get("limit") or 12}),
    "taste": _exec_taste,
    "watch_stats": _exec_watch_stats,
    "health": _exec_health,
    "recommend": _exec_recommend,
}

# ------------------------------------------------------------------ 答案润色（可选）
def _polish_mode(cfg: Optional[Dict[str, Any]], c: AIConfig) -> str:
    """off | aggregate | full。

    * 本地模型（kind=local 或 localhost 端点）：数据不出本机，自动 full；
    * 云端模型：默认 off；用户在设置里开启 insights 后为 aggregate（仅聚合数字），
      再开 insights_actress 才允许摘要携带女优名（full）。
    """
    if c.kind == "local":
        return "full"
    a = (cfg or {}).get("ai") or {}
    if not a.get("insights"):
        return "off"
    return "full" if a.get("insights_actress") else "aggregate"


def _rows_summary(rows: List[Dict[str, Any]], mode: str) -> str:
    """把结构化行压成给模型的摘要；aggregate 档不含 top 名单（女优名在内）。"""
    lines: List[str] = []
    for r in rows or []:
        line = "%s: %s" % (r.get("key") or "", r.get("value") or "")
        if mode == "full" and r.get("top"):
            line += "（top: %s）" % r["top"]
        lines.append(line)
    return "\n".join(lines)


def _polish(question: str, res: Dict[str, Any], cfg, c: AIConfig, mode: str) -> None:
    """把本地执行的统计摘要交给模型组织成自然语言；失败不影响结构化结果。"""
    summary = _rows_summary(res.get("rows") or [], mode)
    if not summary:
        return
    try:
        sys = ai_prompts.system("library.summarize", cfg)
        user = "问题：%s\n\n统计摘要：\n%s" % (question, summary)
        res["answer"] = chat(c, [{"role": "system", "content": sys},
                                 {"role": "user", "content": user}],
                             max_tokens=300, purpose="library.summarize").strip()
        res["privacy"] = mode
    except Exception as e:                               # noqa: BLE001 润色失败降级
        res["polish_error"] = str(e)[:200]


# ------------------------------------------------------------------ 入口
def execute_spec(conn, spec: Dict[str, Any]) -> Dict[str, Any]:
    """执行一份查询规格（意图解析与执行分离：执行器可独立测试，不依赖 LLM）。"""
    kind = str((spec or {}).get("kind") or "search")
    params = (spec or {}).get("params") or {}
    fn = EXECUTORS.get(kind)
    if not fn:
        fn = _exec_search
        params = {"keywords": [str((spec or {}).get("question") or "")]}
        kind = "search"
    res = fn(conn, params)
    res["intent"] = {"kind": kind, "params": params}
    return res


def ask_library(question: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """问库入口：意图解析（模型只见到问题）→ 本地执行 → 结构化结果（可选润色）。"""
    c = AIConfig.from_config(cfg)
    spec = chat_json(c, [{"role": "system", "content": _INTENT_SYSTEM},
                         {"role": "user", "content": str(question or "").strip()}],
                     max_tokens=200, purpose="library.qa")
    if not isinstance(spec, dict):
        spec = {"kind": "search", "params": {"keywords": [str(question)]}}
    spec["question"] = str(question or "").strip()
    from .db import db
    with db() as conn:
        res = execute_spec(conn, spec)
        mode = _polish_mode(cfg, c)
        res["privacy"] = mode
        if mode != "off" and res.get("kind") == "rows":
            _polish(str(question or "").strip(), res, cfg, c, mode)
        return res


