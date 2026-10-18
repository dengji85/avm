# -*- coding: utf-8 -*-
"""详情页 AI 能力的补充实现：剧情问答、相似推荐。

与 app/ai.py 分开是为了让「剧情分析 / 标签 / 翻译」这类高频能力和「问答 /
推荐」这类扩展能力各自独立演进；两者共用 app/ai_core.py 的统一客户端。

设计取舍：
* 问答不做向量库：用字符二元组重合度粗排字幕分句，取 top N 交给模型，
  省掉 embeddings 依赖与索引成本，中文场景召回足够用。
* 相似推荐是纯规则（标签/女优/系列/厂商/剧情文本），**不消耗 token**，
  库大时也能秒出。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from . import ai_prompts
from .ai_core import AIConfig, chat, register_prompt

register_prompt("plot.qa", "v1")


def _bigrams(text: str) -> set:
    s = "".join(ch for ch in (text or "").lower() if not ch.isspace())
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def _clock(sec: float) -> str:
    sec = max(0, int(sec or 0))
    return "%d:%02d" % (sec // 60, sec % 60)


def answer_about_plot(question: str, cues: List[Tuple[float, str]],
                      movie: Dict[str, Any], top: int = 10,
                      cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """基于字幕回答剧情问题，并给出字幕时间戳。"""
    c = AIConfig.from_config(cfg)
    q = _bigrams(question)
    scored: List[Tuple[float, float, str]] = []
    for start, text in cues or []:
        g = _bigrams(text)
        if not g:
            continue
        hit = len(q & g)
        if hit:
            # 长句天然更容易命中二元组，按长度做归一，避免一整段台词占满候选
            length_penalty = 1.0 / (1.0 + len(g) / 40.0)
            scored.append((hit / (len(q) ** 0.5 + 1.0) * length_penalty, start, text))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = sorted(scored[:max(3, top)], key=lambda x: x[1])
    if not picked:
        return {"answer": "", "refs": [], "reason": "no-match"}

    blocks = ["[%s] %s" % (_clock(start), text) for _s, start, text in picked]
    sys = ai_prompts.system("plot.qa", cfg)
    user = ("影片：%s\n\n问题：%s\n\n字幕片段：\n%s"
            % (movie.get("title") or movie.get("code") or "", question, "\n".join(blocks)))
    out = chat(c, [{"role": "system", "content": sys}, {"role": "user", "content": user}],
               max_tokens=600, purpose="plot.qa").strip()
    # 依据列表与真正送给模型的片段保持一致：截断会漏掉答案引用的时间戳
    refs = [{"time": _clock(start), "text": txt[:120]} for _s, start, txt in picked]
    return {"answer": out, "refs": refs, "reason": "ok"}


def _to_set(v: Any) -> set:
    if isinstance(v, str):
        return {x for x in v.split("||") if x}
    if isinstance(v, (list, tuple, set)):
        return {str(x) for x in v if str(x)}
    return set()


def similar_movies(movie: Dict[str, Any], others: List[Dict[str, Any]],
                    limit: int = 5) -> List[Dict[str, Any]]:
    """相似推荐（纯规则，不调用模型）。"""
    base_tags = _to_set(movie.get("tags")) | _to_set(movie.get("genres"))
    base_act = _to_set(movie.get("actresses"))
    base_studio = str(movie.get("studio") or "")
    base_series = str(movie.get("series") or "")
    base_plot = _bigrams(str(movie.get("plot") or "")[:1500] + str(movie.get("title") or ""))

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for o in others or []:
        if str(o.get("id") or "") == str(movie.get("id") or ""):
            continue
        tags = _to_set(o.get("tags")) | _to_set(o.get("genres"))
        act = _to_set(o.get("actresses"))
        shared = base_tags & tags
        shared_act = base_act & act
        score = 3.0 * len(shared) / (len(base_tags) + 1) if shared else 0.0
        score += 2.0 * len(shared_act)
        same_studio = bool(base_studio) and str(o.get("studio") or "") == base_studio
        same_series = bool(base_series) and str(o.get("series") or "") == base_series
        if same_studio:
            score += 1.2
        if same_series:
            score += 1.8
        p = _bigrams(str(o.get("plot") or "")[:1500] + str(o.get("title") or ""))
        plot_sim = (len(base_plot & p) / (len(base_plot) ** 0.5 + 1.0)) if (base_plot and p) else 0.0
        score += 4.0 * plot_sim
        # 评分只在已有实质相似度时加分：否则空元数据的片子会被"高分片"硬凑一堆推荐
        if score > 0 and o.get("rating"):
            score += float(o.get("rating") or 0) * 0.2
        if score <= 0:
            continue
        # 相似原因：前端直接展示「因为共同女优 / 同系列 / …」，让推荐可解释
        reasons: List[Dict[str, Any]] = []
        if shared_act:
            reasons.append({"kind": "actress", "names": sorted(shared_act)[:2]})
        if same_series:
            reasons.append({"kind": "series", "names": [base_series]})
        if shared:
            reasons.append({"kind": "tag", "names": sorted(shared)[:3]})
        if same_studio:
            reasons.append({"kind": "studio", "names": [base_studio]})
        if plot_sim >= 0.15:
            reasons.append({"kind": "plot", "names": []})
        scored.append((score, o, reasons))
    scored.sort(key=lambda x: -x[0])

    out: List[Dict[str, Any]] = []
    for s, o, reasons in scored[:limit]:
        out.append({
            "id": o.get("id"), "code": o.get("code"), "title": o.get("title"),
            "cover": o.get("cover"), "rating": o.get("rating"), "score": round(s, 2),
            "reasons": reasons[:3],
        })
    return out