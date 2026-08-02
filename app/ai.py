"""可选的 AI 增强模块：兼容 OpenAI 协议的聊天补全。

支持任意 OpenAI 兼容端点（云端或本地 Ollama）：
  - 云端：https://api.openai.com/v1  + 你的 key
  - 中转/国产：硅基流动、DeepSeek、通义等（均兼容 /v1/chat/completions）
  - 本地 Ollama：http://127.0.0.1:11434/v1  （ollama pull qwen2.5 后免 key）

所有调用读取 config["ai"] 段，不依赖特定厂商。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from .config import load_config

log = logging.getLogger("avm.ai")

TIMEOUT = 60


@dataclass
class AIConfig:
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.4
    extra_headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: Optional[Dict[str, Any]]) -> "AIConfig":
        a = (cfg or {}).get("ai", {}) if cfg else {}
        return cls(
            enabled=bool(a.get("enabled", False)),
            base_url=str(a.get("base_url", "https://api.openai.com/v1")).rstrip("/"),
            api_key=str(a.get("api_key", "") or ""),
            model=str(a.get("model", "gpt-4o-mini")),
            temperature=float(a.get("temperature", 0.4)),
            extra_headers=dict(a.get("extra_headers", {}) or {}),
        )


def is_available(cfg: Optional[Dict[str, Any]] = None) -> bool:
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            return False
    return AIConfig.from_config(cfg).enabled


def _chat(cfg: AIConfig, messages: List[Dict[str, str]], max_tokens: int = 800) -> str:
    if not cfg.enabled:
        raise RuntimeError("AI 未启用：请在设置中开启并填写 Base URL / API Key / 模型")
    if not cfg.base_url:
        raise RuntimeError("AI Base URL 为空")
    url = f"{cfg.base_url}/chat/completions"
    payload: Dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    headers.update(cfg.extra_headers)
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError(f"AI 请求失败：{e}")
    if r.status_code != 200:
        raise RuntimeError(f"AI 接口返回 {r.status_code}：{r.text[:300]}")
    try:
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError) as e:
        raise RuntimeError(f"AI 响应解析失败：{e} | {r.text[:200]}")


def generate_synopsis(movie: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> str:
    """根据影片元数据生成/润色中文简介。"""
    c = AIConfig.from_config(cfg)
    title = movie.get("title") or movie.get("code") or ""
    code = movie.get("code") or ""
    studio = movie.get("studio") or ""
    series = movie.get("series") or ""
    actresses = movie.get("actresses") or []
    genres = movie.get("genres") or []
    existing = (movie.get("plot") or "").strip()
    sys = (
        "你是一个影视资料库助手，擅长把番号影片的元数据整理成简洁、客观、"
        "不低俗的中文简介。只输出简介正文，不要解释、不要标题、不要 Markdown。"
    )
    user = f"番号：{code}\n标题：{title}\n制作商：{studio}\n系列：{series}\n"
    user += f"女优：{', '.join(actresses) if isinstance(actresses, list) else actresses}\n"
    user += f"类型：{', '.join(genres) if isinstance(genres, list) else genres}\n"
    if existing:
        user += f"\n已有简介（请在其基础上润色补全，保持事实一致）：\n{existing}\n"
    else:
        user += "\n请生成一段 2-4 句话的中文简介。"
    return _chat(c, [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ], max_tokens=500)


def suggest_tags(movie: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """根据元数据建议补充标签。"""
    c = AIConfig.from_config(cfg)
    title = movie.get("title") or movie.get("code") or ""
    code = movie.get("code") or ""
    studio = movie.get("studio") or ""
    series = movie.get("series") or ""
    actresses = movie.get("actresses") or []
    genres = movie.get("genres") or []
    tags = movie.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in tags.split(",") if t]
    sys = "你是标签助手。根据影片元数据补充 3-8 个中文标签，只输出逗号分隔的标签列表，不要其它文字。"
    user = (f"番号：{code}\n标题：{title}\n制作商：{studio}\n系列：{series}\n"
            f"女优：{', '.join(actresses) if isinstance(actresses, list) else actresses}\n"
            f"类型：{', '.join(genres) if isinstance(genres, list) else genres}\n"
            f"已有标签：{', '.join(tags)}")
    out = _chat(c, [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ], max_tokens=120)
    return [t.strip() for t in out.replace("，", ",").split(",") if t.strip()]


def parse_search_intent(query: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把自然语言搜索意图解析成结构化检索条件（供语义搜索使用）。

    返回示例：{"keywords": [...], "genres": [...], "actress": ..., "studio": ...}
    """
    c = AIConfig.from_config(cfg)
    sys = (
        "你是搜索意图解析器。把用户的中文自然语言查询转成 JSON 检索条件，"
        "字段可选：keywords(数组), genres(数组), actress(字符串), studio(字符串), "
        "series(字符串), year(数字)。只输出 JSON，不要解释。"
    )
    try:
        out = _chat(c, [
            {"role": "system", "content": sys},
            {"role": "user", "content": query},
        ], max_tokens=200)
        start = out.find("{")
        end = out.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(out[start:end])
        return {"keywords": [query]}
    except Exception as e:
        log.warning("parse_search_intent 失败：%s", e)
        return {"keywords": [query]}
