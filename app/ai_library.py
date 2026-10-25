# -*- coding: utf-8 -*-
"""AI「问库」：自然语言问题 → 查询意图 → 本地执行 → 结构化结果。

隐私设计（与 search-intent 同一模式，更进一步）：
* 模型只见到**用户的问题本身**（意图解析），不发送任何库数据——
  统计、筛选、推荐全部在本机 SQLite 上执行；
* 结果以结构化数据（rows / 影片卡片）返回前端渲染，不需要模型复述；
* 意图解析的提示词与返回都可在「调用详情」里查看，轨迹只在内存。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from typing import Any, Dict, List, Optional

from . import ext_components, store
from . import ai_prompts
from .concepts import age_hint, clean as clean_term
from .ai_core import AIConfig, AiError, chat, chat_json, chat_tools, register_prompt
from .config import DATA_DIR, load_config

register_prompt("library.qa", "v2")
register_prompt("library.summarize", "v1")

_AGENT_RULES = (
    "规则：\n"
    "1. 用户说的题材 / 类型，先在「库字典」里找对应写法（可能是日语、繁体或英文），"
    "找到就直接用**字典原文**；找不到再用 like 模糊匹配。\n"
    "2. 不要把整句口语塞进 q；q 只放真正需要在文本里检索的词。\n"
    "3. 问数量 / 统计 / 口味 / 库健康时要选统计类能力，不要用找片。\n"
    "4. 不确定的条件宁可不加：先给出可能命中的结果，比 0 条有用。\n"
    "5. why 写人话，会直接展示给用户。\n"
)

# 回退路径：端点不支持 tools 时，让模型直接吐 JSON（同 v2 行为）
_AGENT_SYSTEM = (
    "你是这个影片库的查询规划器。把用户的一句口语翻译成一份可执行的检索规格，只输出 JSON，不要解释。\n"
    '{"kind":"search|recent|taste|watch_stats|health|recommend","params":{...},"why":"一句中文说明你的理解"}\n'
    "search 的 params 可用字段：q / genres / tags / actress / studio / series / like / "
    "actress_age_min / actress_age_max / year / year_min / year_max / min_rating / max_rating / "
    "flags / sort / page_size。\n"
    "recent：最近入库（limit）；taste：口味画像（year，0=全部）；"
    "watch_stats：观看统计；health：库健康；recommend：按口味推荐（limit）。\n\n"
    + _AGENT_RULES
)

# 首选路径：真的调用函数，参数由 schema 约束
_TOOL_SYSTEM = (
    "你是这个影片库的查询规划器。把用户的一句口语翻译成**一次工具调用**："
    "找片用 search_movies，统计 / 口味 / 库健康 / 推荐用 library_stats。\n"
    "必须真的调用函数，不要用自然语言回答问题本身。\n\n"
    + _AGENT_RULES
)

_FLAGS = ["subtitle", "embedzh", "uncensored", "leak", "hd4k", "vr",
          "favorite", "watched", "watchlist", "unwatched"]
_SORTS = ["added_desc", "date_desc", "rating_desc", "play_desc", "year_asc", "size_desc"]

_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_movies",
            "description": "在本地影片库里按条件检索影片（不联网、不写库）",
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {"type": "array", "items": {"type": "string"},
                          "description": "关键词：番号 / 标题 / 简介 / 导演 / 女优名 / 文件名"},
                    "genres": {"type": "array", "items": {"type": "string"},
                               "description": "类型名，必须来自库字典原文；多个默认同时满足"},
                    "tags": {"type": "array", "items": {"type": "string"},
                             "description": "标签名，必须来自库字典原文"},
                    "actress": {"type": "string", "description": "女优名（可用字典原文或只写姓）"},
                    "studio": {"type": "string", "description": "片商名"},
                    "series": {"type": "string", "description": "系列名"},
                    "like": {"type": "array", "items": {"type": "string"},
                             "description": "题材词模糊匹配（字典里没有对应写法时用）"},
                    "actress_age_min": {"type": "integer", "description": "女优年龄下限（按发行年）"},
                    "actress_age_max": {"type": "integer", "description": "女优年龄上限（按发行年）"},
                    "year_min": {"type": "integer", "description": "发行年份下限"},
                    "year_max": {"type": "integer", "description": "发行年份上限"},
                    "min_rating": {"type": "number", "description": "最低评分"},
                    "flags": {"type": "array", "items": {"type": "string", "enum": _FLAGS},
                              "description": "结构性筛选"},
                    "sort": {"type": "string", "enum": _SORTS},
                    "page_size": {"type": "integer", "description": "返回条数，默认 8"},
                    "why": {"type": "string", "description": "一句中文，说明你把这句话理解成了什么"},
                },
                "required": ["why"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "library_stats",
            "description": "整库统计：口味画像 / 观看统计 / 库健康 / 最近入库 / 口味推荐",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string",
                             "enum": ["taste", "watch_stats", "health", "recent", "recommend"]},
                    "limit": {"type": "integer"},
                    "year": {"type": "integer", "description": "口味画像的年份，0 或留空=全部"},
                    "why": {"type": "string", "description": "一句中文，说明你的理解"},
                },
                "required": ["kind", "why"],
            },
        },
    },
]


def _spec_from_calls(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把模型返回的 tool_calls 转成内部规格；不认识的函数返回 {}。"""
    for call in calls or []:
        name = str(call.get("name") or "")
        try:
            args = json.loads(call.get("arguments") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(args, dict):
            continue
        why = str(args.pop("why", "") or "")
        if name == "search_movies":
            return {"kind": "search", "params": args, "why": why}
        if name == "library_stats":
            kind = str(args.pop("kind", "") or "")
            if kind in EXECUTORS:
                return {"kind": kind, "params": args, "why": why}
    return {}

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
    if c.get("q"):
        qv = c["q"]
        parts.append(" ".join(qv) if isinstance(qv, (list, tuple)) else str(qv))
    if c.get("genres"):
        parts.append(" / ".join(c["genres"]))
    for k in ("actress", "studio", "series"):
        if c.get(k):
            parts.append(str(c[k]))
    if c.get("year"):
        parts.append(str(c["year"]))
    if c.get("flags"):
        parts.append(", ".join(c["flags"]))
    like = c.get("like")
    if like:
        vals = like if isinstance(like, (list, tuple)) else [like]
        # 展示清洗后的词：「穿丝袜的」显示成「丝袜」，用户才知道它按什么搜的
        parts.append(" / ".join(clean_term(v) or str(v) for v in vals))
    age_min, age_max = c.get("actress_age_min"), c.get("actress_age_max")
    if age_min and age_max:
        parts.append("女优年龄 %s–%s 岁（按发行年，仅计有生日者）" % (age_min, age_max))
    elif age_min:
        parts.append("女优年龄 ≥ %s 岁（按发行年，仅计有生日者）" % age_min)
    elif age_max:
        parts.append("女优年龄 ≤ %s 岁（按发行年，仅计有生日者）" % age_max)
    return " · ".join(str(p) for p in parts)


def _movie_items(conn, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    p = {"page": 1, "page_size": 12, "sort": "added_desc"}
    for k, v in (params or {}).items():
        if v not in (None, "", []):
            p[k] = v
    # keywords 是意图解析给出的关键词数组，检索层只认 q：在这里翻译过去。
    # 漏了这一步的后果是「帮我找护士的片」直接返回整个库——最典型的答非所问。
    kw = p.pop("keywords", None)
    if kw and not p.get("q"):
        p["q"] = kw if isinstance(kw, (list, tuple)) else [kw]
    # limit 是意图解析里的「要几条」，映射到分页大小
    lim = p.pop("limit", None)
    if lim and not (params or {}).get("page_size"):
        p["page_size"] = lim
    try:
        p["page_size"] = min(24, max(1, int(p.get("page_size") or 12)))
    except (TypeError, ValueError):
        p["page_size"] = 12
    res = store.search_movies(conn, p)
    return res.get("items") or []


# ------------------------------------------------------------------ 各类意图的本地执行
def _exec_search(conn, params) -> Dict[str, Any]:
    p = dict(params or {})
    dropped: List[str] = []
    # 库里一个女优生日都没有时，年龄条件必然 0 条：宁可放宽并说明，也不要给用户一个空结果
    if (p.get("actress_age_min") or p.get("actress_age_max")) and not store.has_birthdays(conn):
        p.pop("actress_age_min", None)
        p.pop("actress_age_max", None)
        dropped.append("age")
    items = _movie_items(conn, p)
    note = _cond_note(p)
    if dropped:
        note = (note + " · " if note else "") + "库里还没有女优生日资料，已忽略年龄条件"
    return {"kind": "movies", "note": note, "items": items}


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


# ------------------------------------------------------------------ 参数归一
_SPLIT_RE = re.compile(r"[、，,。;；/|\s]+")
_FILLER_RE = re.compile(
    r"我|想|要|找一下|找找|找点|搜一下|搜搜|来点|有没有|有什么|推荐|一些|几部|"
    r"这部|这片|看看|哪些|一点|大一点|小一点|那种|这种")
# 只剥「纯粹在说年龄」的词；熟女/年轻这类本身也可能是类型名，保留
_AGE_WORD_RE = re.compile(r"年纪大|年龄大|上了年纪|年长|偏大|年纪小|年龄小|偏小|年纪|年龄")
# 番号形态（IPX-123 / FC2-1234567 / abc_001）：绝不能当成句子拆掉
_CODE_RE = re.compile(r"^[A-Za-z]{2,8}[-_ ]?\d{2,7}$")


def _sentence_like(text: str) -> bool:
    """判断一个 q 值是不是「一整句口语」——是的话要拆词改走 like。"""
    s = str(text or "")
    if _CODE_RE.match(s):
        return False
    cleaned = clean_term(s)
    # 带动词/虚词（穿丝袜的 → 丝袜）：这是口语，不是标题或番号
    if cleaned and cleaned != s:
        return True
    return len(s) > 8 or bool(_SPLIT_RE.search(s)) or bool(_FILLER_RE.search(s))


def _terms_from_sentence(text: str) -> List[str]:
    out: List[str] = []
    for piece in _SPLIT_RE.split(str(text or "")):
        t = clean_term(piece) or ""
        t = _FILLER_RE.sub("", t).strip()
        t = _AGE_WORD_RE.sub("", t).strip()
        if t and len(t) <= 8 and t not in out:
            out.append(t)
    return out


def normalize_search_params(params: Dict[str, Any], question: str = "") -> Dict[str, Any]:
    """把「整句当关键词」修正成可执行的检索条件。

    真实踩过的坑：模型没给结构化参数时，兜底会用 ``q=[整句]`` 去 LIKE，必然 0 条，
    用户看到的就是「完全查不出」。这里统一做三件事：

    1. ``q`` / ``keywords`` 里像句子的值 → 拆词、剥缀词后移进 ``like``（类型/标签模糊匹配）；
    2. 短词仍留在 ``q``（番号、标题、女优名靠它）；
    3. 问题里有「年纪大 / 年轻」这类口语年龄 → 补 ``actress_age_min/max``。
    """
    p = dict(params or {})
    moved: List[str] = []
    for key in ("q", "keywords"):
        vals = p.get(key)
        if not vals:
            continue
        vals = list(vals) if isinstance(vals, (list, tuple)) else [vals]
        keep: List[str] = []
        for v in vals:
            s = str(v or "").strip()
            if not s:
                continue
            if _sentence_like(s):
                moved.extend(_terms_from_sentence(s))
            else:
                keep.append(s)
        if keep:
            p[key] = keep
        else:
            p.pop(key, None)

    if moved:
        like = list(p.get("like") or [])
        for t in moved:
            if t and t not in like:
                like.append(t)
        p["like"] = like

    if not (p.get("actress_age_min") or p.get("actress_age_max")):
        hint = age_hint(question) or age_hint(" ".join(str(x) for x in (p.get("like") or [])))
        if hint:
            p["actress_age_min" if hint[0] == "min" else "actress_age_max"] = hint[1]
    return p


# ------------------------------------------------------------------ 入口
def execute_spec(conn, spec: Dict[str, Any]) -> Dict[str, Any]:
    """执行一份查询规格（意图解析与执行分离：执行器可独立测试，不依赖 LLM）。"""
    kind = str((spec or {}).get("kind") or "search")
    raw = (spec or {}).get("params") or {}
    params = normalize_search_params(raw if isinstance(raw, dict) else {},
                                     str((spec or {}).get("question") or ""))
    fn = EXECUTORS.get(kind)
    if not fn:
        fn = _exec_search
        params = {"keywords": [str((spec or {}).get("question") or "")]}
        kind = "search"
    res = fn(conn, params)
    res["intent"] = {"kind": kind, "params": params}
    return res


# 端点是否支持 tools：一次 4xx 就记住，之后直接走 JSON 模式，不再每轮回一次失败请求。
_tools_ok = True


def _plan(c: AIConfig, question: str, vocab_text: str, feedback: str = "") -> Dict[str, Any]:
    """一次规划调用：问题 + 库字典（+ 上一轮失败反馈）→ 查询规格。

    优先 tool calling（参数受 schema 约束，比自由 JSON 稳）；端点不支持时自动
    退回「让模型吐 JSON」，两条路产出的规格完全一致，上层无感知。
    """
    global _tools_ok
    user = "【库字典】\n%s\n\n【用户问题】%s" % (vocab_text, question)
    if feedback:
        user += "\n\n【上一轮结果】\n" + feedback
    purpose = "library.qa.repair" if feedback else "library.qa"

    if _tools_ok:
        try:
            out = chat_tools(c, [{"role": "system", "content": _TOOL_SYSTEM},
                                 {"role": "user", "content": user}],
                             tools=_TOOLS, purpose=purpose)
            spec = _spec_from_calls(out.get("tool_calls") or [])
            if spec:
                return spec
            log_plan_error(AiError("模型没有发起工具调用，改走 JSON 模式"))
        except AiError as e:
            if not getattr(e, "retryable", True):
                # 400/404 这类确定性失败：多半是端点不认 tools
                _tools_ok = False
                try:
                    from .ai_core import log as ai_log
                    ai_log.info("AI 端点不支持 tools，库查询改用 JSON 模式：%s", e)
                except Exception:  # noqa: BLE001 日志不可用不影响主流程
                    pass
            else:
                log_plan_error(e)

    try:
        spec = chat_json(c, [{"role": "system", "content": _AGENT_SYSTEM},
                             {"role": "user", "content": user}],
                         max_tokens=400, purpose=purpose)
    except Exception as e:                               # noqa: BLE001 规划失败降级为关键词
        log_plan_error(e)
        return {}
    return spec if isinstance(spec, dict) else {}


def log_plan_error(e: BaseException) -> None:
    try:
        from .ai_core import log
        log.warning("问库规划失败：%s", e)
    except Exception:  # noqa: BLE001 日志不可用不影响主流程
        pass


def _is_empty_movies(res: Dict[str, Any]) -> bool:
    return res.get("kind") == "movies" and not (res.get("items") or [])


def _plan_meta(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": spec.get("kind") or "search",
        "params": spec.get("params") or {},
        "why": str(spec.get("why") or "")[:200],
    }


def ask_library(question: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """口语问库：规划（带库字典）→ 本地执行 → 0 结果时带着反馈重规划一次。

    与旧版的区别：模型不再「凭常识猜类型名」，而是从库字典里挑真实存在的值；
    一次没命中会看到「你上次这么写、结果 0 条」，再修正一轮——这就是最小可用的
    agent 循环（规划 → 执行 → 观察 → 重规划）。
    """
    c = AIConfig.from_config(cfg)
    q = str(question or "").strip()
    from .db import db
    from . import vocab as vocab_mod

    with db() as conn:
        vocab_text = vocab_mod.block(conn)
        spec = _plan(c, q, vocab_text)
        if not spec:
            spec = {"kind": "search", "params": {"q": [q]}}
        spec["question"] = q
        res = execute_spec(conn, spec)
        res["plan"] = _plan_meta(spec)

        # 0 结果通常不是「库里没有」，而是「词对不上」：把失败现场交给模型再规划一次
        if _is_empty_movies(res):
            feedback = (
                "规格：%s\n命中：0 条。可能类型/标签名与库里写法不一致，或条件太严。\n"
                "请重新生成规格：优先用库字典里的原文，拿不准的条件去掉，必要时改用 like 或 q。"
                % json.dumps({"kind": spec.get("kind"), "params": spec.get("params") or {}},
                             ensure_ascii=False)
            )
            retry = _plan(c, q, vocab_text, feedback)
            if retry:
                retry["question"] = q
                res2 = execute_spec(conn, retry)
                if not _is_empty_movies(res2):
                    res2["plan"] = _plan_meta(retry)
                    res2["repaired"] = True
                    res = res2

        mode = _polish_mode(cfg, c)
        res["privacy"] = mode
        if mode != "off" and res.get("kind") == "rows":
            _polish(q, res, cfg, c, mode)
        return res


