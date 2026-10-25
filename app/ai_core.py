# -*- coding: utf-8 -*-
"""AI 能力层：统一的模型客户端。

只回答「怎么和模型说话」，**不含任何业务提示词**（那些在 app/ai.py 应用层）：

  * 配置解析：端点 / Key / 模型 / 温度 / 超时 / 重试次数 / 附加头
  * 统一入口：``chat`` / ``chat_json`` / ``chat_tools``（function calling）
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
import contextvars
import hashlib
import json
import logging
import random
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Iterator, List, Optional

import requests

from .config import load_config

log = logging.getLogger("avm.ai.core")

DEFAULT_TIMEOUT = 60          # 每次请求的读超时（秒）
DEFAULT_RETRIES = 2           # 失败重试次数（不含首次）
CONNECT_TIMEOUT = 8
CACHE_TTL = 600               # 相同请求的结果缓存（秒）
CACHE_MAX = 200


class AiError(RuntimeError):
    """AI 调用失败：端点不可达、鉴权失败、返回结构异常等。

    ``retryable`` 决定 chat() 是否值得重试：网络/5xx/429 可以，
    鉴权失败、模型名不存在、内容策略拒绝这类重试也没用。
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


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
    # 模型池里的身份：name 用于展示与「指定模型」，kind 只影响界面标签（cloud/local）
    name: str = "default"
    kind: str = "cloud"

    @classmethod
    def from_config(cls, cfg: Optional[Dict[str, Any]],
                    model: Optional[str] = None) -> "AIConfig":
        a = (cfg or {}).get("ai", {}) if cfg else {}
        entry = pick_model(a, model)
        base: Dict[str, Any] = {
            "enabled": bool(a.get("enabled", False)),
            "base_url": str(a.get("base_url", "https://api.openai.com/v1")).rstrip("/"),
            "api_key": str(a.get("api_key", "") or ""),
            "model": str(a.get("model", "gpt-4o-mini")),
            "temperature": a.get("temperature", 0.4),
            "timeout": a.get("timeout", DEFAULT_TIMEOUT),
            "max_retries": a.get("max_retries", DEFAULT_RETRIES),
            "extra_headers": dict(a.get("extra_headers", {}) or {}),
        }
        name = str(model or "default")
        kind = "cloud"
        if entry:
            for key in ("base_url", "api_key", "model", "temperature",
                        "timeout", "max_retries"):
                val = entry.get(key)
                if val not in (None, ""):
                    base[key] = val
            if "enabled" in entry:
                base["enabled"] = bool(entry.get("enabled"))
            if entry.get("extra_headers"):
                base["extra_headers"] = dict(entry["extra_headers"])
            name = str(entry.get("name") or name)
            kind = str(entry.get("kind") or kind)
        else:
            base_url = str(base["base_url"])
            if "127.0.0.1" in base_url or "localhost" in base_url:
                kind = "local"
        try:
            timeout = int(base.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        try:
            retries = int(base.get("max_retries", DEFAULT_RETRIES) or 0)
        except (TypeError, ValueError):
            retries = DEFAULT_RETRIES
        return cls(
            enabled=bool(base.get("enabled", False)),
            base_url=str(base.get("base_url", "https://api.openai.com/v1")).rstrip("/"),
            api_key=str(base.get("api_key", "") or ""),
            model=str(base.get("model", "gpt-4o-mini")),
            temperature=float(base.get("temperature", 0.4) or 0.4),
            timeout=max(5, timeout),
            max_retries=max(0, retries),
            extra_headers=dict(base.get("extra_headers", {}) or {}),
            name=name,
            kind=kind,
        )


def model_entries(ai_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """模型池里的有效条目（至少有 name）。"""
    items = (ai_cfg or {}).get("models") or []
    return [m for m in items
            if isinstance(m, dict) and str(m.get("name") or "").strip()]


def pick_model(ai_cfg: Dict[str, Any], name: Optional[str] = None) -> Dict[str, Any]:
    """挑一条模型配置：指定 name → default_model → 第一条；池为空返回 {}。"""
    entries = model_entries(ai_cfg)
    if not entries:
        return {}
    target = str(name or (ai_cfg or {}).get("default_model") or "").strip()
    for m in entries:
        if str(m.get("name") or "") == target:
            return m
    return entries[0]


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

# 当前请求的调用方信息（由 main.py 的中间件写入）：用来区分「用户在页面上点的」
# 与「脚本 / 外部工具直连的」——两者都走同一个 chat() 入口，此前混在一起，
# 用户在「调用记录」里会看到自己没点过的调用，却无从判断来源。
_CLIENT: "contextvars.ContextVar[Dict[str, str]]" = contextvars.ContextVar(
    "avm_ai_client", default={})


def set_client(ip: str = "", ua: str = "", referer: str = "") -> Any:
    """登记当前请求的调用方（中间件调用），返回一个用于还原的令牌。"""
    return _CLIENT.set({"ip": str(ip or ""), "ua": str(ua or ""),
                        "referer": str(referer or "")})


def reset_client(token: Any) -> None:
    try:
        _CLIENT.reset(token)
    except Exception:
        pass


def _origin_of(client: Dict[str, str]) -> str:
    """判断这次调用来自哪里：ui（页面内）/ external（脚本直连）/ internal（后台任务）。"""
    if not client:
        return "internal"
    ua = str(client.get("ua") or "")
    low = ua.lower()
    # 无头浏览器 / 命令行脚本：不是用户手动点出来的
    if "headlesschrome" in low or "python-requests" in low or "python-urllib" in low \
            or "curl/" in low or "wget/" in low or "powershell" in low:
        return "external"
    if str(client.get("referer") or "") or "mozilla" in low:
        return "ui"
    return "external"


def _record_trace(purpose: str, cfg: AIConfig, messages: List[Dict[str, str]], *,
                  reply: str = "", error: str = "", ms: float = 0,
                  cached: bool = False) -> None:
    global _TRACE_SEQ
    client = dict(_CLIENT.get() or {})
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
            "origin": _origin_of(client),
            "client_ip": client.get("ip") or "",
            "client_ua": (client.get("ua") or "")[:120],
            "prompt_chars": sum(len(str(m.get("content") or "")) for m in messages),
            "reply_chars": len(reply or ""),
        })


def traces(limit: int = 20, purpose: str = "") -> List[Dict[str, Any]]:
    """最近的调用轨迹（新 → 旧）。purpose 非空时按「精确匹配或同前缀分组」筛选。

    前缀分组是必要的：剧情相关的 purpose 有 plot.subtitle / plot.subtitle.chunk /
    plot.dialog / plot.qa 多个变体，按精确匹配筛「剧情」会一条都看不到。
    """
    n = max(1, min(100, int(limit or 20)))
    with _TRACE_LOCK:
        items = [copy.deepcopy(x) for x in _TRACES]
    p = str(purpose or "").strip()
    if p:
        items = [x for x in items
                 if x.get("purpose") == p or str(x.get("purpose") or "").startswith(p + ".")]
    return items[:n]


def clear_traces() -> int:
    """清空调用记录，返回清掉的条数（内存操作，重启本来也会清空）。"""
    global _TRACE_SEQ
    with _TRACE_LOCK:
        n = len(_TRACES)
        _TRACES.clear()
    return n


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
    attempts = 1 + max(0, cfg.max_retries)
    # 推理型模型（返回 reasoning_content）会把 max_tokens 全部用在思考上，
    # 结果 finish_reason=length 而 content 为空。此时把额度翻倍重试一次，
    # 而不是把空串当成功返回——空答案会让上层显示「模型没回答」。
    bumped = False
    for attempt in range(attempts):
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
                raise AiError("HTTP %d：%s%s" % (resp.status_code, resp.text[:200], hint),
                              retryable=False)
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise AiError("接口返回里没有 choices：%s" % str(data)[:200])
            choice = choices[0]
            text = (choice.get("message") or {}).get("content") or ""
            text = str(text).strip()
            if not text:
                finish = str(choice.get("finish_reason") or "")
                if finish == "content_filter":
                    raise AiError("模型按内容策略拒绝返回（finish_reason=content_filter），可换个模型或调整提示词",
                                  retryable=False)
                if finish == "length":
                    if not bumped and attempt < attempts - 1:
                        bumped = True
                        payload["max_tokens"] = min(8000, max(int(payload["max_tokens"]) * 2, 1600))
                        log.info("AI 返回被 token 上限截断（%s），max_tokens 提到 %d 重试",
                                 purpose or "chat", payload["max_tokens"])
                        continue
                    raise AiError("模型返回被 token 上限截断（finish_reason=length）："
                                  "推理型模型会先消耗额度思考，请调大该模型的 max_tokens 或换用非推理模型",
                                  retryable=False)
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
            retryable = bool(getattr(e, "retryable", True))
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            last = "%s: %s" % (type(e).__name__, e)
            retryable = True
        if attempt >= cfg.max_retries or not retryable:
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


def chat_tools(cfg: AIConfig, messages: List[Dict[str, Any]], *,
               tools: List[Dict[str, Any]], tool_choice: str = "auto",
               max_tokens: int = 600, temperature: Optional[float] = None,
               purpose: str = "tools") -> Dict[str, Any]:
    """带 ``tools`` 的一次调用：返回 ``{"content": str, "tool_calls": [...]}``。

    与 ``chat`` 的差别只在返回值——tool_calls 在 content 之外，只要字符串就拿不到，
    所以单独实现；重试、超时、统计、轨迹的口径与 ``chat`` 保持一致。

    **端点不支持 tools 时**（多数自建小模型、部分代理）会抛
    ``retryable=False`` 的 ``AiError``，调用方据此回退到 JSON 模式，不要重试浪费额度。
    """
    if not cfg.enabled:
        raise AiError("AI 未启用：请在 设置 → AI 中开启并填写接口与模型")

    temp = cfg.temperature if temperature is None else float(temperature)
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
        "tools": tools,
        "tool_choice": tool_choice,
    }

    base = (cfg.base_url or "").rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    t0 = time.time()
    last = ""
    last_retryable = True
    attempts = 1 + max(0, cfg.max_retries)
    for attempt in range(attempts):
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=(CONNECT_TIMEOUT, cfg.timeout))
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < cfg.max_retries:
                raise AiError("HTTP %d" % resp.status_code)
            if resp.status_code >= 400:
                raise AiError("HTTP %d：%s（该端点可能不支持 tools）"
                              % (resp.status_code, resp.text[:200]), retryable=False)
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise AiError("接口返回里没有 choices：%s" % str(data)[:200])
            msg = choices[0].get("message") or {}
            content = str(msg.get("content") or "").strip()
            calls: List[Dict[str, Any]] = []
            for c in msg.get("tool_calls") or []:
                fn = c.get("function") or {}
                calls.append({
                    "id": str(c.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "arguments": fn.get("arguments") or "",
                })
            with _STATS_LOCK:
                _STATS["calls"] += 1
                _STATS["prompt_chars"] += sum(len(str(m.get("content") or "")) for m in messages)
                _STATS["reply_chars"] += len(content)
                _STATS["last_model"] = cfg.model
                _STATS["last_error"] = ""
                _STATS["last_call_at"] = int(time.time())
            reply = content or json.dumps([c["name"] for c in calls], ensure_ascii=False)
            _record_trace(purpose, cfg, messages, reply=reply,
                          ms=(time.time() - t0) * 1000)
            return {"content": content, "tool_calls": calls}
        except AiError as e:
            last = str(e)
            retryable = bool(getattr(e, "retryable", True))
            last_retryable = retryable
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            last = "%s: %s" % (type(e).__name__, e)
            retryable = True
            last_retryable = True
        if attempt >= cfg.max_retries or not retryable:
            break
        time.sleep(min(2 ** attempt, 4) * (1 + random.random() * 0.3))
        log.info("AI 工具调用重试（%s 第 %d 次）：%s", purpose or "tools", attempt + 1, last)

    with _STATS_LOCK:
        _STATS["failures"] += 1
        _STATS["last_error"] = last[:200]
    log.warning("AI 工具调用失败（%s）：%s", purpose or "tools", last)
    _record_trace(purpose, cfg, messages, error=last,
                  ms=(time.time() - t0) * 1000)
    # 一定要保留 retryable：上层靠它判断「是不是端点不支持 tools」，决定要不要回退
    raise AiError("AI 调用失败：%s" % (last or "未知错误"), retryable=last_retryable)


def chat_stream(cfg: AIConfig, messages: List[Dict[str, Any]], *,
                max_tokens: int = 800, temperature: Optional[float] = None,
                purpose: str = "stream") -> Iterator[str]:
    """逐块产出回答文本（OpenAI 兼容 SSE），让界面「边生成边显示」。

    与 ``chat`` 的差别：
      * **不重试**——已经吐出半句再重发，界面会出现重复内容；
      * 端点忽略 ``stream`` 参数、直接返回整段 JSON 时，这里同样能产出一块文本；
      * 统计与轨迹在结束时记一次，口径与 ``chat`` 一致。
    """
    if not cfg.enabled:
        raise AiError("AI 未启用：请在 设置 → AI 中开启并填写接口与模型")

    temp = cfg.temperature if temperature is None else float(temperature)
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
        "stream": True,
    }
    base = (cfg.base_url or "").rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"

    t0 = time.time()
    try:
        resp = requests.post(url, headers=headers, json=payload,
                             timeout=(CONNECT_TIMEOUT, cfg.timeout), stream=True)
    except requests.RequestException as e:
        raise AiError("AI 调用失败：%s" % e)

    pieces: List[str] = []
    try:
        if resp.status_code >= 400:
            raise AiError("HTTP %d：%s" % (resp.status_code, resp.text[:200]), retryable=False)

        ctype = str(resp.headers.get("content-type") or "")
        if "event-stream" not in ctype:
            # 有的代理会忽略 stream 参数，直接返回整段 JSON
            data = resp.json()
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if text:
                pieces.append(str(text))
                yield str(text)
        else:
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                choice = (obj.get("choices") or [{}])[0]
                piece = (choice.get("delta") or {}).get("content") or ""
                if piece:
                    pieces.append(str(piece))
                    yield str(piece)

        with _STATS_LOCK:
            _STATS["calls"] += 1
            _STATS["prompt_chars"] += sum(len(str(m.get("content") or "")) for m in messages)
            _STATS["reply_chars"] += sum(len(p) for p in pieces)
            _STATS["last_model"] = cfg.model
            _STATS["last_error"] = ""
            _STATS["last_call_at"] = int(time.time())
        _record_trace(purpose, cfg, messages, reply="".join(pieces),
                      ms=(time.time() - t0) * 1000)
    finally:
        try:
            resp.close()
        except Exception:                                # noqa: BLE001 关闭失败不影响已产出的内容
            pass


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
