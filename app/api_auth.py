# -*- coding: utf-8 -*-
"""访问认证接口：令牌换会话、登出、会话与审计查询。

设计详见 docs/安全与隐私设计.md（L1 会话与传输 / L2 认证硬化）。
"""
from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, Response

from . import auth
from .config import ensure_access_token, load_config, update_config

router = APIRouter(prefix="/api/auth", tags=["auth"])
# 隐私锁定接口单独一组：锁定期间中间件只放行 /api/privacy/ 与 /api/auth/
privacy_router = APIRouter(prefix="/api/privacy", tags=["privacy"])


# ------------------------------------------------------------------ 内部工具
def _server_cfg() -> Dict[str, Any]:
    """读取 server 段配置，并把 session_persist 同步给 auth 模块。"""
    cfg = load_config()
    server = cfg.get("server") or {}
    auth.set_persist(bool(server.get("session_persist")))
    return server


def _privacy_cfg() -> Dict[str, Any]:
    """读取 privacy 段配置（并顺带把会话持久化开关同步给 auth 模块）。"""
    cfg = load_config()
    auth.set_persist(bool((cfg.get("server") or {}).get("session_persist")))
    return cfg.get("privacy") or {}


def _client(request: Request, server: Dict[str, Any]) -> str:
    return auth.client_ip(request.scope, server.get("trusted_proxies") or ["127.0.0.1", "::1"])


def _ua(request: Request) -> str:
    return request.headers.get("user-agent", "") or ""


def _idle_abs(server: Dict[str, Any]) -> tuple:
    try:
        idle = int(server.get("session_idle_days", 7) or 0)
    except (TypeError, ValueError):
        idle = 7
    try:
        absd = int(server.get("session_abs_days", 30) or 0)
    except (TypeError, ValueError):
        absd = 30
    return max(0, idle), max(0, absd)


def _set_session_cookie(response: Response, sid: str, https: bool, idle_days: int) -> None:
    """写入 HttpOnly 会话 Cookie，并顺手清掉旧版前端写入的长期令牌 Cookie。"""
    response.headers.append("Set-Cookie", auth.session_cookie(sid, https, idle_days))
    # avm_token 是旧版本由前端 JS 写入、有效期长达一年的令牌，安全性差，升级后清除
    response.headers.append(
        "Set-Cookie",
        "%s=; Path=/; Max-Age=0; SameSite=Lax" % auth.COOKIE_TOKEN,
    )


# ------------------------------------------------------------------ 登录 / 登出
@router.post("/login")
def login(payload: Dict[str, Any], request: Request, response: Response) -> Dict[str, Any]:
    """用访问令牌换取会话（Set-Cookie: avm_session）。"""
    server = _server_cfg()
    ip = _client(request, server)
    ua = _ua(request)
    max_fails = int(server.get("login_max_fails") or 0)
    block_min = int(server.get("login_block_minutes") or 0)

    # 登录失败限速：同 IP 连续失败达到阈值后临时封禁
    until = auth.blocked_until(ip, max_fails, block_min)
    now = time.time()
    if until > now:
        auth.audit("login", ip, ua, "处于封禁期，拒绝尝试", ok=False)
        raise HTTPException(429, "尝试次数过多，请 %d 分钟后再试" % (int((until - now) // 60) + 1))

    token = str((payload or {}).get("token") or "").strip()
    real = ensure_access_token()
    if not token or token != real:
        n = _record_fail(ip)
        auth.audit("login", ip, ua, "令牌错误（第 %d 次）" % n, ok=False)
        msg = "访问令牌不正确"
        if max_fails and max_fails - n > 0:
            msg += "，还可尝试 %d 次" % (max_fails - n)
        elif max_fails:
            msg += "，已达上限，请稍后再试"
        raise HTTPException(401, msg)

    auth.clear_fails(ip)
    idle, absd = _idle_abs(server)
    sid, info = auth.new_session(ip, ua, idle, absd)
    auth.audit("login", ip, ua, "登录成功", ok=True)
    _set_session_cookie(response, sid, auth.is_https(request.scope), idle)
    return {
        "ok": True,
        "expires_in_days": idle,
        "abs_days": absd,
        "persist": auth.persist_enabled(),
        "created": info.get("created", 0),
    }


@router.post("/logout")
def logout(request: Request, response: Response) -> Dict[str, Any]:
    """退出登录：吊销当前会话并清除 Cookie（幂等）。"""
    server = _server_cfg()
    ip = _client(request, server)
    sid = auth.cookie_value(request.scope, auth.SESSION_COOKIE)
    hit = auth.drop_session(sid) if sid else False
    if hit:
        auth.audit("logout", ip, _ua(request), "已退出登录")
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    response.delete_cookie(auth.COOKIE_TOKEN, path="/")
    return {"ok": True, "revoked": 1 if hit else 0}


@router.post("/logout-all")
def logout_all(request: Request, response: Response) -> Dict[str, Any]:
    """吊销全部会话（所有设备都被登出）。"""
    server = _server_cfg()
    ip = _client(request, server)
    n = auth.drop_all()
    auth.audit("logout-all", ip, _ua(request), "已登出全部设备（%d 个会话）" % n)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    response.delete_cookie(auth.COOKIE_TOKEN, path="/")
    return {"ok": True, "revoked": n}


# ------------------------------------------------------------------ 会话状态
@router.get("/session")
def session_info(request: Request) -> Dict[str, Any]:
    """当前访问状态（未登录也返回，供前端决定是否弹登录框）。

    该接口在鉴权白名单内，不返回任何敏感信息。
    """
    server = _server_cfg()
    ip = _client(request, server)
    idle, absd = _idle_abs(server)
    sid = auth.cookie_value(request.scope, auth.SESSION_COOKIE)
    info = auth.touch_session(sid, idle, absd) if sid else None

    token_ok = False
    if not info:
        tok = (server.get("access_token") or "").strip()
        hdrs = auth.http_headers(request.scope)
        token_ok = bool(tok) and hdrs.get("x-access-token") == tok

    local = auth.is_local_ip(ip)
    bypass = bool(server.get("local_bypass", True))
    need_auth = bool(server.get("require_token_remote", True))
    via = "session" if info else ("token" if token_ok else ("local" if (local and bypass) else ""))
    authenticated = bool(via) or not need_auth

    return {
        "authenticated": authenticated,
        "via": via,
        "local": local,
        "ip": ip,
        "idle_days": idle,
        "abs_days": absd,
        "persist": auth.persist_enabled(),
        "sessions": len(auth.list_sessions()),
        "expires_at": (float(info.get("last") or 0) + idle * 86400) if (info and idle) else 0,
        "abs_expires_at": (float(info.get("created") or 0) + absd * 86400) if (info and absd) else 0,
    }


@router.get("/sessions")
def list_sessions(request: Request) -> Dict[str, Any]:
    """活跃会话列表（可据此踢掉陌生设备）。"""
    server = _server_cfg()
    sid = auth.cookie_value(request.scope, auth.SESSION_COOKIE)
    current = auth.fingerprint_of(sid)[:12] if sid else ""
    items = auth.list_sessions()
    for it in items:
        it["current"] = bool(current) and it["id"] == current
    return {"items": items}


@router.delete("/sessions/{sid}")
def revoke_session(sid: str, request: Request) -> Dict[str, Any]:
    """踢出指定会话。"""
    server = _server_cfg()
    ip = _client(request, server)
    ok = auth.revoke_by_public_id(sid)
    if ok:
        auth.audit("revoke", ip, _ua(request), "已踢出会话 %s" % sid[:12])
    return {"ok": ok}


@router.get("/audit")
def audit_log(limit: int = 50) -> Dict[str, Any]:
    """最近登录 / 失败 / 登出记录（最多 200 条）。"""
    return {"items": auth.recent_audit(limit)}


# ------------------------------------------------------------------ 隐私解锁（简单密码）
@router.get("/pin-status")
def pin_status() -> Dict[str, Any]:
    """是否已设置隐私解锁「简单密码」（不下发任何凭据）。"""
    return {"has_pin": bool((_privacy_cfg().get("pin_hash") or "").strip())}


@router.post("/set-pin")
def set_pin(payload: Dict[str, Any]) -> Dict[str, Any]:
    """设置 / 清除隐私解锁简单密码（需已登录；pin 为空表示清除）。

    磁盘只保存 PBKDF2 哈希，明文永不落盘、也不会下发到前端。
    """
    pin = str((payload or {}).get("pin") or "").strip()
    if pin and len(pin) < 4:
        raise HTTPException(400, "简单密码至少 4 位")
    if len(pin) > 64:
        raise HTTPException(400, "简单密码过长")
    update_config({"privacy": {"pin_hash": auth.hash_pin(pin)}})
    auth.audit("set-pin", detail="已更新隐私解锁简单密码" if pin else "已清除隐私解锁简单密码")
    return {"ok": True, "has_pin": bool(pin)}


@router.post("/verify-pin")
def verify_pin(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """校验隐私解锁简单密码（遮挡层解锁用），带失败限速。"""
    server = _server_cfg()
    # 简单密码保存在 privacy 段（server 段只放访问控制与会话配置）
    stored = (_privacy_cfg().get("pin_hash") or "").strip()
    if not stored:
        raise HTTPException(400, "尚未设置简单密码")
    ip = _client(request, server)
    ua = _ua(request)
    max_fails = int(server.get("login_max_fails") or 0)
    block_min = int(server.get("login_block_minutes") or 0)
    until = auth.blocked_until(ip, max_fails, block_min)
    now = time.time()
    if until > now:
        auth.audit("unlock", ip, ua, "处于封禁期，拒绝解锁尝试", ok=False)
        raise HTTPException(429, "尝试次数过多，请 %d 分钟后再试" % (int((until - now) // 60) + 1))
    pin = str((payload or {}).get("pin") or "").strip()
    if not auth.verify_pin(pin, stored):
        n = _record_fail(ip)
        auth.audit("unlock", ip, ua, "简单密码错误（第 %d 次）" % n, ok=False)
        msg = "简单密码不正确"
        if max_fails and max_fails - n > 0:
            msg += "，还可尝试 %d 次" % (max_fails - n)
        raise HTTPException(401, msg)
    auth.clear_fails(ip)
    auth.audit("unlock", ip, ua, "已用简单密码解锁", ok=True)
    return {"ok": True}


# ------------------------------------------------------------------ 隐私锁定（服务端强制）
def _record_fail(ip: str) -> int:
    """记录失败次数；本机不计（避免设备所有者把自己锁在门外）。"""
    if auth.is_local_ip(ip):
        return 0
    return auth.record_fail(ip)


def _throttle(server: Dict[str, Any], ip: str, ua: str, what: str) -> None:
    """隐私解锁失败限速（复用登录失败阈值与封禁时长）。

    本机（设备所有者自己）不参与限速：否则连续输错几次就会把自己锁在门外，
    而锁定时连设置页都打不开（限速的目的是防远程暴力尝试）。
    """
    if auth.is_local_ip(ip):
        return
    max_fails = int(server.get("login_max_fails") or 0)
    block_min = int(server.get("login_block_minutes") or 0)
    until = auth.blocked_until(ip, max_fails, block_min)
    now = time.time()
    if until > now:
        auth.audit("unlock", ip, ua, "处于封禁期，拒绝%s" % what, ok=False)
        raise HTTPException(429, "尝试次数过多，请 %d 分钟后再试" % (int((until - now) // 60) + 1))


@privacy_router.get("/state")
def privacy_state() -> Dict[str, Any]:
    """隐私锁定状态：前端刷新页面后据此决定是否继续显示遮挡层。"""
    st = auth.privacy_state()
    privacy = _privacy_cfg()
    return {
        "locked": bool(st.get("locked")),
        "since": st.get("since", 0),
        "reason": st.get("reason", ""),
        "unlock_mode": privacy.get("lock_unlock", "token"),
        "has_pin": bool((privacy.get("pin_hash") or "").strip()),
    }


@privacy_router.post("/lock")
def privacy_lock(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """锁定：前端在「空闲超时」或「按下紧急遮挡键」时调用。

    状态保存在**服务端**，因此刷新页面、重开标签页都无法绕过；
    锁定期间中间件会拒绝除解锁相关接口外的所有请求（封面、影片、流媒体都拿不到）。
    """
    server = _server_cfg()
    reason = str((payload or {}).get("reason") or "manual")[:40]
    st = auth.privacy_lock(reason)
    auth.audit("privacy-lock", _client(request, server), _ua(request), "已锁定：%s" % reason)
    return {"ok": True, "locked": True, "reason": st.get("reason", "")}


@privacy_router.post("/unlock")
def privacy_unlock(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """解锁：按 privacy.lock_unlock（click / pin / token）校验后解除服务端锁定。"""
    server = _server_cfg()
    privacy = _privacy_cfg()
    mode = str(privacy.get("lock_unlock") or "token")
    ip = _client(request, server)
    ua = _ua(request)
    credential = str((payload or {}).get("credential") or "").strip()

    if mode in ("pin", "token"):
        _throttle(server, ip, ua, "解锁尝试")

    if mode == "pin":
        stored = (privacy.get("pin_hash") or "").strip()
        # 逃生通道：忘记简单密码时，可用「访问令牌」解锁。
        #   令牌本身就是登录凭据（强度高于 4 位简单密码），用它兜底不会降低安全性；
        #   而没有这条通道，用户忘密码后会连设置页都打不开（锁定期间中间件拒绝所有接口），
        #   只能手动去改 data/config.json。解锁后可进设置重置简单密码。
        expected = (server.get("access_token") or "").strip()
        if expected and credential == expected:
            auth.audit("unlock", ip, ua, "已用访问令牌兜底解锁（简单密码模式）", ok=True)
        else:
            if not stored:
                raise HTTPException(400, "尚未设置简单密码")
            if not auth.verify_pin(credential, stored):
                n = _record_fail(ip)
                auth.audit("unlock", ip, ua, "简单密码错误（第 %d 次）" % n, ok=False)
                raise HTTPException(401, "简单密码不正确")
    elif mode == "token":
        expected = (server.get("access_token") or "").strip()
        if not expected or credential != expected:
            n = _record_fail(ip)
            auth.audit("unlock", ip, ua, "访问令牌错误（第 %d 次）" % n, ok=False)
            raise HTTPException(401, "访问令牌不正确")

    auth.clear_fails(ip)
    auth.privacy_unlock()
    auth.audit("privacy-unlock", ip, ua, "已解锁（%s）" % mode)
    return {"ok": True, "locked": False}
