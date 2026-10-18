# -*- coding: utf-8 -*-
"""可审查、可覆盖的 AI 提示词注册表。

设计取舍：
* 只开放 **system 提示词** 给用户改。影片信息 / 字幕 / 问题由程序拼进 user 消息，
  结构固定；让用户改 user 模板很容易把占位符改坏，得不偿失。
* 实际发出的完整消息（system + user）在「调用详情」里原样可见，所以「审查」不缺信息。
* 覆盖值存在 config.ai.prompts：{prompt_id: system_text}，空串 / 缺省 = 用默认。
* 默认文案与代码同源，这里只做搬运，改文案时记得同步版本号（见 ai_core.register_prompt）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import ai_core
from .config import load_config, save_config

# 单条提示词长度上限：够写详细规则，又不至于把配置撑爆
MAX_PROMPT_CHARS = 8000


PROMPTS: Dict[str, Dict[str, Any]] = {
    "synopsis": {
        "title": "剧情简介（基于元数据）",
        "desc": "影片没有可用字幕时的回退方案：把番号、标题、女优、类型整理成简介。",
        "variables": ["meta", "existing"],
        "system": (
            "你是一个影视资料库助手，擅长把番号影片的元数据整理成简洁、客观、"
            "不低俗的中文简介。只输出简介正文，不要解释、不要标题、不要 Markdown。"
        ),
    },
    "plot.subtitle": {
        "title": "剧情简介（基于字幕）",
        "desc": "从字幕总结剧情。字幕很长时会先分块提炼，再用这条提示词合并成最终简介。",
        "variables": ["meta", "subtitles", "chunks"],
        "system": (
            "你是影视剧情分析助手。只根据用户提供的字幕总结剧情，"
            "不添加字幕里没有的人物、情节或背景；语言客观、简洁、不低俗，"
            "不写露骨细节。输出 3–5 句中文：第一句一句话梗概，"
            "后面补充主要人物关系与情节走向。只输出正文，不要标题、不要 Markdown。"
        ),
    },
    "plot.subtitle.chunk": {
        "title": "剧情简介（分段提炼）",
        "desc": "长字幕按段提炼要点，每段单独调用。之后再交给上一条合并。",
        "variables": ["index", "total", "chunk"],
        "system": (
            "只根据这段字幕片段提炼剧情要点，2–3 句中文，客观、不低俗、不写露骨细节；"
            "不要编造片段里没有的信息，不要标题。"
        ),
    },
    "tags": {
        "title": "标签建议",
        "desc": "根据元数据补充分类标签，输出逗号分隔的列表。",
        "variables": ["meta", "current_tags"],
        "system": (
            "你是影视资料库的标签助手。根据影片信息补充 3-8 个中文分类标签"
            "（题材、人物身份、场景等），只输出逗号分隔的标签列表，不要其它文字。"
        ),
    },
    "plot.qa": {
        "title": "剧情问答",
        "desc": "按问题检索相关字幕片段后回答，并要求模型标注时间。",
        "variables": ["title", "question", "cues"],
        "system": (
            "你是影片剧情问答助手。只根据给出的字幕片段回答问题，不要编造没出现的情节；"
            "引用依据时用 [分:秒] 标注；片段里没有答案就直接说「字幕里没有提到」。"
            "中文回答，客观、简洁、不写露骨细节。"
        ),
    },
    "search.intent": {
        "title": "搜索意图解析",
        "desc": "把自然语言搜索词转成结构化条件（关键词 / 类型 / 女优 / 厂商 / 年份）。",
        "variables": ["query"],
        "system": (
            "你是搜索意图解析器。把用户的中文自然语言查询转成 JSON 检索条件，"
            "字段可选：keywords(数组), genres(数组), actress(字符串), studio(字符串), "
            "series(字符串), year(数字)。只输出 JSON，不要解释。"
        ),
    },
    "plot.dialog": {
        "title": "剧情简介（基于语音对白）",
        "desc": "没有字幕、但有对白时用：语音转写文本带噪声与断句误差，"
                "只提炼能确认的剧情，不要脑补缺失情节。",
        "variables": ["meta", "dialog"],
        "system": (
            "你是影视剧情分析助手。用户给的是一部影片的语音转写对白，"
            "可能因识别误差出现错字、断句错误或少量无意义片段。"
            "只根据能读懂的对白提炼剧情，不要添加对白里没有的人物或情节；"
            "遇到明显噪声直接忽略。语言客观、简洁、不低俗，不写露骨细节。"
            "输出 3–5 句中文：第一句一句话梗概，后面补充人物关系与情节走向。"
            "只输出正文，不要标题、不要 Markdown。"
        ),
    },

    "subtitle.translate": {
        "title": "字幕翻译",
        "desc": "把字幕逐条译成目标语言。{target} 会替换成用户在详情页选的语言，"
                "术语表随请求一起发送。批处理与逐条降级共用这一条。",
        "variables": ["target"],
        "system": (
            "你是专业字幕翻译。把用户给出的字幕 JSON 数组逐条译成{target}，"
            "口语自然、贴合语境；术语表里的词必须严格照译；"
            "不要合并、拆分或增删条目。只输出 JSON 数组，如 "
            "[{\"i\":0,\"t\":\"译文\"}]，不要解释。"
        ),
    },
}


def _overrides(cfg: Optional[Dict[str, Any]]) -> Dict[str, str]:
    c = cfg if cfg is not None else load_config()
    raw = ((c or {}).get("ai") or {}).get("prompts") or {}
    return {str(k): str(v) for k, v in raw.items() if str(v or "").strip()}


def system(prompt_id: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    """取当前生效的 system 提示词：用户覆盖优先，否则用默认。"""
    item = PROMPTS.get(prompt_id)
    if not item:
        raise KeyError("未知提示词：%s" % prompt_id)
    custom = _overrides(cfg).get(prompt_id, "").strip()
    return custom or item["system"]


def render(prompt_id: str, cfg: Optional[Dict[str, Any]] = None, **variables: Any) -> str:
    """取提示词并把 {变量} 替换成实际值（例如字幕翻译的目标语言）。"""
    text = system(prompt_id, cfg)
    for key, value in variables.items():
        text = text.replace("{%s}" % key, str(value))
    return text


def list_prompts(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """给前端的提示词清单：默认值、当前生效值、是否被改过、变量说明、版本。"""
    c = cfg if cfg is not None else load_config()
    over = _overrides(c)
    versions = ai_core.prompt_versions()
    items: List[Dict[str, Any]] = []
    for pid, item in PROMPTS.items():
        custom = over.get(pid, "")
        items.append({
            "id": pid,
            "title": item["title"],
            "desc": item["desc"],
            "variables": item["variables"],
            "system": custom or item["system"],
            "default_system": item["system"],
            "overridden": bool(custom.strip()),
            "version": versions.get(pid, ""),
        })
    return items


def save_prompt(prompt_id: str, text: str) -> Dict[str, Any]:
    """保存用户自定义提示词；传入空白等同于恢复默认。"""
    if prompt_id not in PROMPTS:
        raise KeyError("未知提示词：%s" % prompt_id)
    body = (text or "").strip()
    if len(body) > MAX_PROMPT_CHARS:
        raise ValueError("提示词过长（上限 %d 字）" % MAX_PROMPT_CHARS)
    cfg = load_config()
    ai = dict(cfg.get("ai") or {})
    prompts = dict(ai.get("prompts") or {})
    if body:
        prompts[prompt_id] = body
    else:
        prompts.pop(prompt_id, None)
    ai["prompts"] = prompts
    cfg["ai"] = ai
    save_config(cfg)
    return next(x for x in list_prompts(load_config(refresh=True)) if x["id"] == prompt_id)


def reset_prompt(prompt_id: str) -> Dict[str, Any]:
    """恢复默认提示词。"""
    return save_prompt(prompt_id, "")
