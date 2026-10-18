# -*- coding: utf-8 -*-
"""AI 能力层：统一的模型客户端。

只回答「怎么和模型说话」，**不含任何业务提示词**（那些在 app/ai.py 应用层）：

  * 配置解析：端点 / Key / 模型 / 温度 / 超时 / 重试次数 / 附加头
  * 统一入口：``chat`` 与 ``chat_json``
  * 健壮性：重试退避、超时隔离、错误归一为 ``AiError``
  * 可观测：调用 / 失败 / 缓存命中 / 输出长度统计（见 ``stats()``）
  * 省钱：同一请求（模型 + 消息 + 参数完全一致）10 分钟内复用上一次结果
  * 可追溯：提示词版本登记（``register_prompt`` / ``prompt_versions``）

设计约束（后续超时 / 限流 / 预算也在这一层加，不要散落到各接口）：
  * 应用层只负责「说什么」和「拿到字符串后怎么处理」
  * 任务层（批量、限流、取消）在 ai_jobs 队列，不在本模块
  * 所有失败均抛 ``AiError``，接口层统一转 4xx/5xx，不让异常穿透到前端
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import random
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

import requests

from .config import load_config

log = logging.getLogger("avm.ai.core")

DEFAULT_TIMEOUT = 60          # 每次请求的读超时（秒）
DEFAULT_RETRIES = 2           # 失败重试次数（不含首次）
CONNECT_TIMEOUT = 8
CACHE_TTL = 600               # 相同请求的结果缓存（秒）
CACHE_MAX = 200


class AiError(RuntimeError):
    """AI 调用失败：端点不可达、鉴权失败、返回结构异常等。"""


@dataclass
class AIConfig:
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.4
    timeout: int = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_RETRIES
    extra_headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: Optional[Dict[str, Any]]) -> "AIConfig":
        a = (cfg or {}).get("ai", {}) if cfg else {}
        try:
            timeout = int(a.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        try:
            retries = int(a.get("max_retries", DEFAULT_RETRIES) or 0)
        except (TypeError, ValueError):
            retries = DEFAULT_RETRIES
        return cls(
            enabled=bool(a.get("enabled", False)),
            base_url=str(a.get("base_url", "https://api.openai.com/v1")).rstrip("/"),
            api_key=str(a.get("api_key", "") or ""),
            model=str(a.get("model", "gpt-4o-mini")),
            temperature=float(a.get("temperature", 0.4)),
            timeout=max(5, timeout),
            max_retries=max(0, retries),
            extra_headers=dict(a.get("extra_headers", {}) or {}),
        )


def is_available(cfg: Optional[Dict[str, Any]] = None) -> bool:
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            return False
    return AIConfig.from_config(cfg).enabled


# ------------------------------------------------------------------ 可观测性
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {
    "calls": 0, "failures": 0, "cache_hits": 0,
    "prompt_chars": 0, "reply_chars": 0,
    "last_model": "", "last_error": "", "last_call_at": 0,
}

_PROMPT_REGISTRY: Dict[str, str] = {}


def register_prompt(name: str, version: str) -> None:
    """登记业务提示词版本（应用层在定义提示词处调用）。"""
    _PROMPT_REGISTRY[name] = version


def prompt_versions() -> Dict[str, str]:
    return dict(_PROMPT_REGISTRY)


def stats() -> Dict[str, Any]:
    with _STATS_LOCK:
        return dict(_STATS)


# ------------------------------------------------------------------ 缓存
_CACHE_LOCK = threading.Lock()
_CACHE: "OrderedDict[str, tuple]" = OrderedDict()


def _cache_key(cfg: AIConfig, messages: List[Dict[str, str]],
               max_tokens: int, temperature: float) -> str:
    raw = json.dumps({
        "m": cfg.model, "b": cfg.base_url, "t": temperature,
        "k": max_tokens, "msgs": messages,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Optional[str]:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if not hit:
            return None
        if hit[1] < now:
            _CACHE.pop(key, None)
            return None
        _CACHE.move_to_end(key)
        return hit[0]


def _cache_put(key: str, value: str) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (value, time.time() + CACHE_TTL)
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_MAX:
            _CACHE.popitem(last=False)


# ------------------------------------------------------------------ 调用轨迹
# 最近若干次调用的「提示词 / 原始返回 / 耗时」：详情页的调用详情就是读这里。
# 只在内存里，重启即清空；不落盘是为了避免把字幕正文长期留在磁盘上。
_TRACE_MAX = 80
_TRACES: Deque[Dict[str, Any]] = deque(maxlen=_TRACE_MAX)
_TRACE_LOCK = threading.Lock()
_TRACE_SEQ = 0


def _record_trace(purpose: str, cfg: AIConfig, messages: List[Dict[str, str]], *,
                  reply: str = "", error: str = "", ms: float = 0,
                  cached: bool = False) -> None:
    global _TRACE_SEQ
    with _TRACE_LOCK:
        _TRACE_SEQ += 1
        _TRACES.appendleft({
            "id": _TRACE_SEQ,
            "at": int(time.time()),
            "purpose": purpose or "chat",
            "model": cfg.model,
            "base_url": cfg.base_url,
            "messages": [{"role": str(m.get("role") or ""),
                          "content": str(m.get("content") or "")} for m in messages],
            "reply": reply or "",
            "error": error or "",
            "ms": int(ms),
            "cached": bool(cached),
            "prompt_chars": sum(len(str(m.get("content") or "")) for m in messages),
            "reply_chars": len(reply or ""),
        })


def traces(limit: int = 20, purpose: str = "") -> List[Dict[str, Any]]:
    """最近的调用轨迹（新 → 旧）。purpose 非空时只取该用途。"""
    n = max(1, min(100, int(limit or 20)))
    with _TRACE_LOCK:
        items = [copy.deepcopy(x) for x in _TRACES]
    if purpose:
        items = [x for x in items if x.get("purpose") == purpose]
    return items[:n]


# ------------------------------------------------------------------ 统一入口
def chat(cfg: AIConfig, messages: List[Dict[str, str]], *,
         max_tokens: int = 800, purpose: str = "",
         temperature: Optional[float] = None,
         use_cache: bool = False) -> str:
    """发送一次聊天补全，返回文本。

    失败重试（网络错误 / 5xx / 429）后抛 ``AiError``；
    ``purpose`` 只用于日志与统计，不影响行为。
    """
    if not cfg.enabled:
        raise AiError("AI 未启用：请在 设置 → AI 中开启并填写接口与模型")

    temp = cfg.temperature if temperature is None else float(temperature)
    t0 = time.time()
    key = _cache_key(cfg, messages, max_tokens, temp) if use_cache else ""
    if key:
        hit = _cache_get(key)
        if hit is not None:
            with _STATS_LOCK:
                _STATS["cache_hits"] += 1
            # 命中缓存也留一条轨迹：详情页能看到「这次没花钱，用的是缓存结果」
            _record_trace(purpose, cfg, messages, reply=hit, cached=True)
            return hit

    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = "Bearer %s" % cfg.api_key
    for k, v in (cfg.extra_headers or {}).items():
        headers[str(k)] = str(v)

    payload: Dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": temp,
        "max_tokens": max_tokens,
    }

    # 兼容「填到 /v1」和「把完整端点也贴进来」两种写法：
    # 后者再拼 /chat/completions 会变成 .../chat/completions/chat/completions → 404
    base = (cfg.base_url or "").rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    last = ""
    for attempt in range(1 + max(0, cfg.max_retries)):
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=(CONNECT_TIMEOUT, cfg.timeout))
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < cfg.max_retries:
                raise AiError("HTTP %d" % resp.status_code)
            if resp.status_code >= 400:
                hint = {
                    400: "（常见原因：模型名不存在、余额不足或参数不被支持）",
                    401: "（API Key 无效或已过期）",
                    403: "（Key 没有该模型的权限）",
                    404: "（接口地址一般填到 /v1 为止；也可能是模型名不存在）",
                }.get(resp.status_code, "")
                raise AiError("HTTP %d：%s%s" % (resp.status_code, resp.text[:200], hint))
            data = resp.json()
            text = (data["choices"][0]["message"]["content"] or "")
            text = str(text).strip()
            with _STATS_LOCK:
                _STATS["calls"] += 1
                _STATS["prompt_chars"] += sum(len(str(m.get("content") or "")) for m in messages)
                _STATS["reply_chars"] += len(text)
                _STATS["last_model"] = cfg.model
                _STATS["last_error"] = ""
                _STATS["last_call_at"] = int(time.time())
            if key and text:
                _cache_put(key, text)
            _record_trace(purpose, cfg, messages, reply=text,
                          ms=(time.time() - t0) * 1000)
            return text
        except AiError as e:
            last = str(e)
            retryable = not last.startswith("HTTP 4")
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            last = "%s: %s" % (type(e).__name__, e)
            retryable = True
        if attempt >= cfg.max_retries:
            break
        time.sleep(min(2 ** attempt, 4) * (1 + random.random() * 0.3))
        log.info("AI 调用重试（%s 第 %d 次）：%s", purpose or "chat", attempt + 1, last)

    with _STATS_LOCK:
        _STATS["failures"] += 1
        _STATS["last_error"] = last[:200]
    log.warning("AI 调用失败（%s）：%s", purpose or "chat", last)
    _record_trace(purpose, cfg, messages, error=last,
                  ms=(time.time() - t0) * 1000)
    raise AiError("AI 调用失败：%s" % (last or "未知错误"))


def chat_json(cfg: AIConfig, messages: List[Dict[str, str]], *,
              max_tokens: int = 400, purpose: str = "") -> Any:
    """发送请求并解析 JSON 响应；模型没给合法 JSON 时抛 AiError。"""
    text = chat(cfg, messages, max_tokens=max_tokens, purpose=purpose or "json")
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        raise AiError("模型未返回 JSON：%s" % text[:120])
    try:
        return json.loads(text[start:end])
    except ValueError as e:
        raise AiError("模型返回的 JSON 无法解析：%s" % e)
