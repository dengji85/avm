# -*- coding: utf-8 -*-
"""Emby 联动：目前只做「刮削落盘后通知刷新媒体库」这一件事。

设计约束：
- 完全可选：配置缺失、服务不可达、Key 失效时静默失败，绝不影响刮削与播放；
- 刷新做**防抖合并**：批量刮削会产生上千次落盘，合并成一次刷新，避免把 Emby 打爆；
- API Key 是凭据：不进日志、不回传前端、异常信息里也不出现。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

try:
    import requests
except Exception:  # pragma: no cover - requests 是硬依赖，这里只做兜底
    requests = None  # type: ignore

from .config import emby_cfg, outbound_verify_tls

# 防抖定时器（同一时刻只有一个待触发的刷新）
_TIMER: Optional[threading.Timer] = None
_TIMER_LOCK = threading.Lock()
_LAST: Dict[str, Any] = {}


def _norm_base(cfg: Dict[str, Any]) -> str:
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if base and not base.lower().startswith(("http://", "https://")):
        base = "http://" + base
    return base


def _headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    return {
        "X-Emby-Token": str(cfg.get("api_key") or ""),
        "Accept": "application/json",
    }


def configured(cfg: Dict[str, Any] | None = None) -> bool:
    c = cfg or emby_cfg()
    return bool(c.get("enabled") and _norm_base(c) and str(c.get("api_key") or "").strip())


def test_connection(cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """测试连通性与凭据。返回 {ok, version, server_name, error}。"""
    c = cfg or emby_cfg()
    base = _norm_base(c)
    if not base:
        return {"ok": False, "error": "未填写 Emby 服务器地址"}
    if not str(c.get("api_key") or "").strip():
        return {"ok": False, "error": "未填写 API Key"}
    if requests is None:
        return {"ok": False, "error": "requests 不可用"}
    try:
        r = requests.get(base + "/System/Info", headers=_headers(c),
                         timeout=8, verify=outbound_verify_tls())
    except Exception as exc:
        return {"ok": False, "error": "连接失败：%s" % str(exc)[:200]}
    if r.status_code in (401, 403):
        return {"ok": False, "error": "API Key 无效或无权限（HTTP %d）" % r.status_code}
    if r.status_code >= 400:
        return {"ok": False, "error": "服务器返回 HTTP %d" % r.status_code}
    try:
        info = r.json() or {}
    except Exception:
        info = {}
    return {
        "ok": True,
        "server_name": info.get("ServerName") or "",
        "version": info.get("Version") or "",
    }


def refresh_library(cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """请求 Emby 刷新媒体库（不等待扫描完成，接口是异步的）。"""
    c = cfg or emby_cfg()
    base = _norm_base(c)
    if not base or not str(c.get("api_key") or "").strip():
        return {"ok": False, "error": "Emby 未配置"}
    if requests is None:
        return {"ok": False, "error": "requests 不可用"}
    try:
        r = requests.post(base + "/Library/Refresh", headers=_headers(c),
                          timeout=10, verify=outbound_verify_tls())
    except Exception as exc:
        return {"ok": False, "error": "连接失败：%s" % str(exc)[:200]}
    if r.status_code in (401, 403):
        return {"ok": False, "error": "API Key 无效或无权限（HTTP %d）" % r.status_code}
    if r.status_code >= 400:
        return {"ok": False, "error": "服务器返回 HTTP %d" % r.status_code}
    return {"ok": True}


def note_result(result: Dict[str, Any]) -> None:
    """记录最近一次刷新结果，供设置页/诊断查看（不含密钥）。"""
    _LAST.clear()
    _LAST.update({"ok": bool(result.get("ok")), "error": str(result.get("error") or "")})


def last_result() -> Dict[str, Any]:
    return dict(_LAST)


def _fire(cfg: Dict[str, Any]) -> None:
    global _TIMER
    with _TIMER_LOCK:
        _TIMER = None
    note_result(refresh_library(cfg))


def notify_soon(delay: Optional[float] = None) -> bool:
    """安排一次「稍后刷新」（防抖）。返回是否已安排。

    在批量刮削里被高频调用也安全：后一次调用只会重置计时器。
    """
    global _TIMER
    c = emby_cfg()
    if not configured(c):
        return False
    try:
        sec = float(c.get("refresh_debounce_sec") or 30)
    except (TypeError, ValueError):
        sec = 30.0
    if delay is not None:
        sec = float(delay)
    sec = max(1.0, min(600.0, sec))
    with _TIMER_LOCK:
        if _TIMER is not None:
            _TIMER.cancel()
        t = threading.Timer(sec, _fire, args=(c,))
        t.daemon = True
        _TIMER = t
        t.start()
    return True
