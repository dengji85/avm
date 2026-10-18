# -*- coding: utf-8 -*-
"""AI 应用层：在这里写「说什么」，不写「怎么和模型说话」。

与 app/ai_core.py 的分工：
  * 本模块 = 业务提示词 + 结果处理（简介 / 剧情 / 标签 / 搜索意图）
  * ai_core  = 统一客户端（配置、重试、超时、缓存、统计、错误归一）

新增 AI 功能时只改这里；需要换模型 / 加重试 / 限流只改 ai_core。
提示词均带版本号，便于排查“同一部片两次结果不同”。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import ai_core, ai_prompts
from .ai_core import AIConfig, AiError, chat, chat_json, is_available, register_prompt

# 提示词版本：改动提示词时递增，方便线上问题回溯（见 GET /api/ai/status）
register_prompt("synopsis", "v1")
register_prompt("plot.subtitle", "v2")
register_prompt("plot.subtitle.chunk", "v1")
register_prompt("plot.dialog", "v1")
register_prompt("plot.dialog.chunk", "v1")
register_prompt("tags", "v2")
register_prompt("search.intent", "v1")


def _split_chunks(text: str, size: int = 6000, limit: int = 8) -> List[str]:
    """按行切块；超过 limit 块时均匀抽样，避免 token 无上限。"""
    lines = [s for s in (text or "").splitlines() if s.strip()]
    chunks: List[str] = []
    buf: List[str] = []
    n = 0
    for line in lines:
        buf.append(line)
        n += len(line)
        if n >= size:
            chunks.append("\n".join(buf))
            buf, n = [], 0
    if buf:
        chunks.append("\n".join(buf))
    if len(chunks) > limit:
        step = len(chunks) / float(limit)
        chunks = [chunks[int(i * step)] for i in range(limit)]
    return chunks


def _meta_block(movie: Dict[str, Any]) -> str:
    actresses = movie.get("actresses") or []
    if isinstance(actresses, list):
        actresses = "、".join(str(a) for a in actresses[:6])
    genres = movie.get("genres") or []
    if isinstance(genres, list):
        genres = "、".join(str(g) for g in genres[:8])
    return ("番号：%s\n标题：%s\n制作商：%s\n系列：%s\n女优：%s\n类型：%s\n"
            % (movie.get("code") or "", movie.get("title") or "",
               movie.get("studio") or "", movie.get("series") or "",
               actresses, genres))


def generate_synopsis(movie: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> str:
    """根据影片元数据生成/润色中文简介（无字幕时的回退方案）。"""
    c = AIConfig.from_config(cfg)
    existing = (movie.get("plot") or "").strip()
    sys = ai_prompts.system("synopsis", cfg)
    user = _meta_block(movie)
    if existing:
        user += "\n已有简介（请在其基础上润色补全，保持事实一致）：\n%s\n" % existing
    else:
        user += "\n请生成一段 2-4 句话的中文简介。"
    return chat(c, [{"role": "system", "content": sys},
                    {"role": "user", "content": user}],
                max_tokens=500, purpose="synopsis").strip()


def summarize_subtitles(text: str, movie: Dict[str, Any],
                        cfg: Optional[Dict[str, Any]] = None) -> str:
    """根据字幕内容生成剧情简介（不编造、不写露骨细节）。

    字幕超过一块时先分块提炼，再合成最终简介，避免超出上下文。
    """
    c = AIConfig.from_config(cfg)
    sys_final = ai_prompts.system("plot.subtitle", cfg)
    chunks = _split_chunks(text)
    if not chunks:
        return ""
    if len(chunks) == 1:
        user = _meta_block(movie) + "\n字幕内容：\n" + chunks[0] + "\n\n请总结这部影片的剧情。"
        return chat(c, [{"role": "system", "content": sys_final},
                        {"role": "user", "content": user}],
                    max_tokens=700, purpose="plot.subtitle").strip()

    sys_part = ai_prompts.system("plot.subtitle.chunk", cfg)
    parts: List[str] = []
    for i, ch in enumerate(chunks, 1):
        try:
            part = chat(c, [
                {"role": "system", "content": sys_part},
                {"role": "user", "content": "第 %d/%d 段字幕：\n%s" % (i, len(chunks), ch)},
            ], max_tokens=300, purpose="plot.subtitle.chunk").strip()
            if part:
                parts.append(part)
        except AiError as e:
            # 单段失败不阻断整体：丢一段比整体失败更可接受
            ai_core.log.warning("字幕分段摘要失败（第 %d/%d 段）：%s",
                                i, len(chunks), e)
    if not parts:
        raise AiError("全部分段摘要都失败了")
    user = (_meta_block(movie) + "\n以下是按时间顺序的分段剧情摘要：\n"
            + "\n".join(parts) + "\n\n请合并成 3–5 句完整剧情简介，不要重复，不要分点。")
    return chat(c, [{"role": "system", "content": sys_final},
                    {"role": "user", "content": user}],
                max_tokens=700, purpose="plot.subtitle").strip()


def summarize_dialog(text: str, movie: Dict[str, Any],
                     cfg: Optional[Dict[str, Any]] = None) -> str:
    """根据语音转写对白生成剧情简介。

    对白是 ASR 产物，可能有错字与无意义片段：提示词要求模型「只提炼能确认的剧情」，
    并且分段摘要后再合并，避免长音频一次性塞进上下文。
    """
    c = AIConfig.from_config(cfg)
    sys_final = ai_prompts.system("plot.dialog", cfg)
    chunks = _split_chunks(text, size=5000, limit=6)
    if not chunks:
        return ""
    if len(chunks) == 1:
        user = _meta_block(movie) + "\n语音对白（含识别误差）：\n" + chunks[0] + \
               "\n\n请总结这部影片的剧情。"
        return chat(c, [{"role": "system", "content": sys_final},
                        {"role": "user", "content": user}],
                    max_tokens=700, purpose="plot.dialog").strip()

    sys_part = ai_prompts.system("plot.subtitle.chunk", cfg)
    parts: List[str] = []
    for i, ch in enumerate(chunks, 1):
        try:
            part = chat(c, [
                {"role": "system", "content": sys_part},
                {"role": "user", "content": "第 %d/%d 段对白：\n%s" % (i, len(chunks), ch)},
            ], max_tokens=300, purpose="plot.dialog.chunk").strip()
            if part:
                parts.append(part)
        except AiError as e:
            ai_core.log.warning("对白分段摘要失败（第 %d/%d 段）：%s", i, len(chunks), e)
    if not parts:
        raise AiError("全部分段摘要都失败了")
    user = (_meta_block(movie) + "\n以下是按时间顺序的对白摘要（来自语音识别，可能有误差）：\n"
            + "\n".join(parts) + "\n\n请合并成 3–5 句完整剧情简介，不要重复，不要分点。")
    return chat(c, [{"role": "system", "content": sys_final},
                    {"role": "user", "content": user}],
                max_tokens=700, purpose="plot.dialog").strip()


def suggest_tags(movie: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    c = AIConfig.from_config(cfg)
    tags = movie.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in tags.split(",") if t]
    sys = ai_prompts.system("tags", cfg)
    user = (_meta_block(movie) + "\n已有标签：%s"
            % ("、".join(tags) if isinstance(tags, list) else tags))
    # 300 而不是 120：部分模型（如 deepseek 系列）会先输出思考过程，
    # 上限太小时正文被截断成空串，看起来像「模型不给标签」
    out = chat(c, [{"role": "system", "content": sys},
                   {"role": "user", "content": user}],
               max_tokens=300, purpose="tags")
    if not out.strip():
        # 部分模型对题材敏感时会返回空串：降温后重试一次，仍为空才报错
        out = chat(c, [{"role": "system", "content": sys},
                       {"role": "user", "content": user}],
                   max_tokens=300, temperature=0.2, purpose="tags")
    if not out.strip():
        raise AiError("模型返回空内容（可能触发了内容过滤）。可到「提示词」里把措辞改中性一些，或换一个模型后重试")
    # 兼容顿号 / 换行 / 编号列表，避免只认逗号导致整段被当成一个标签
    cleaned = (out.replace("，", ",").replace("、", ",")
                  .replace("\n", ",").replace("；", ",").replace(";", ","))
    tags = []
    for raw in cleaned.split(","):
        t = raw.strip().lstrip("-*0123456789. )）").strip()
        if t and t not in tags:
            tags.append(t)
    return tags


def parse_search_intent(query: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把自然语言搜索意图解析成结构化检索条件（供语义搜索使用）。

    返回示例：{"keywords": [...], "genres": [...], "actress": ..., "studio": ...}
    """
    c = AIConfig.from_config(cfg)
    sys = ai_prompts.system("search.intent", cfg)
    try:
        out = chat_json(c, [{"role": "system", "content": sys},
                            {"role": "user", "content": query}],
                        max_tokens=200, purpose="search.intent")
        return out if isinstance(out, dict) else {"keywords": [query]}
    except AiError as e:
        ai_core.log.warning("parse_search_intent 失败：%s", e)
        return {"keywords": [query]}
