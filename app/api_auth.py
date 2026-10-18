# -*- coding: utf-8 -*-
"""访问认证接口：令牌换会话、登出、会话与审计查询。

设计详见 docs/安全与隐私设计.md（L1 会话与传输 / L2 认证硬化）。
"""
from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, Response

from . import auth
from . import totp as totp_mod
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

    # 两步验证：启用后仅凭令牌还不够，必须再提交一次验证器验证码（或备用码）。
    # 令牌正确但未带验证码时返回 200 + need_totp（**不签发会话**），
    # 前端据此在同一个弹窗里显示第二步输入框，用户不必重输令牌。
    if bool(server.get("totp_enabled")) and (server.get("totp_secret") or "").strip():
        code = str((payload or {}).get("totp") or "").strip()
        if not code:
            return {"ok": False, "need_totp": True}
        secret = (server.get("totp_secret") or "").strip()
        step = totp_mod.verify(secret, code, after=float(server.get("totp_last_step") or 0))
        if step is not None:
            # 记录已用时间步，同一验证码在有效期内不可重放
            update_config({"server": {"totp_last_step": int(step)}})
            auth.audit("login", ip, ua, "两步验证通过", ok=True)
        else:
            # 备用码兜底：命中即从列表移除（一次性）
            recovery = list(server.get("totp_recovery") or [])
            hit = totp_mod.match_recovery(code, recovery)
            if not hit:
                n = _record_fail(ip)
                auth.audit("login", ip, ua, "两步验证码错误（第 %d 次）" % n, ok=False)
                raise HTTPException(401, "两步验证码不正确")
            rest = [h for h in recovery if h != hit]
            update_config({"server": {"totp_recovery": rest}})
            auth.audit("login", ip, ua, "使用备用码登录（剩余 %d 个）" % len(rest), ok=True)

    auth.clear_fails(ip)
    idle, absd = _idle_abs(server)
    sid, info = auth.new_session(ip, ua, idle, absd)
    auth.audit("login", ip, ua, "登录成功", ok=True)
    _set_session_cookie(response, sid, auth.is_https(request.scope), idle)
    # 「记住此设备 30 天」：会话过期后自动续期，适合家用固定设备。
    # 设备记录同样只存指纹；换令牌 / 关闭两步验证 / 登出全部设备时统一吊销。
    remembered = False
    if bool((payload or {}).get("remember")):
        did = auth.add_trust(ip, ua)
        response.headers.append(
            "Set-Cookie", auth.trust_cookie(did, auth.is_https(request.scope)))
        remembered = True
    return {
        "ok": True,
        "expires_in_days": idle,
        "abs_days": absd,
        "persist": auth.persist_enabled(),
        "created": info.get("created", 0),
        "remembered": remembered,
    }


@router.post("/logout")
def logout(request: Request, response: Response) -> Dict[str, Any]:
    """退出登录：吊销当前会话并清除 Cookie（幂等）。"""
    server = _server_cfg()
    ip = _client(request, server)
    sid = auth.cookie_value(request.scope, auth.SESSION_COOKIE)
    hit = auth.drop_session(sid) if sid else False
    # 主动退出要同时忘掉本机的「记住此设备」，否则刷新会被它自动登回来
    did = auth.cookie_value(request.scope, auth.TRUST_COOKIE)
    auth.drop_trust(did)
    if hit:
        auth.audit("logout", ip, _ua(request), "已退出登录")
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    response.delete_cookie(auth.COOKIE_TOKEN, path="/")
    response.delete_cookie(auth.TRUST_COOKIE, path="/")
    return {"ok": True, "revoked": 1 if hit else 0}


@router.post("/logout-all")
def logout_all(request: Request, response: Response) -> Dict[str, Any]:
    """吊销全部会话（所有设备都被登出）。"""
    server = _server_cfg()
    ip = _client(request, server)
    n = auth.drop_all()
    devices = auth.drop_all_trust()
    auth.audit("logout-all", ip, _ua(request),
               "已登出全部设备（%d 个会话 / %d 台受信设备）" % (n, devices))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    response.delete_cookie(auth.COOKIE_TOKEN, path="/")
    response.delete_cookie(auth.TRUST_COOKIE, path="/")
    return {"ok": True, "revoked": n, "devices": devices}


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
        from .config import current_access_token
        tok = current_access_token()
        hdrs = auth.http_headers(request.scope)
        token_ok = bool(tok) and hdrs.get("x-access-token") == tok

    local = auth.is_local_ip(ip)
    need_auth = bool(server.get("token_required", True))
    # 不再有「本机免令牌」：未登录就是未登录，本机也一样（令牌是唯一凭据）
    via = "session" if info else ("token" if token_ok else "")
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


@router.get("/trusted")
def trusted_devices() -> Dict[str, Any]:
    """受信设备（「记住此设备 30 天」）列表：只给 IP / UA / 时间。"""
    items = auth.list_trust()
    return {"count": len(items), "items": items}


@router.post("/trusted/revoke-all")
def revoke_trusted_devices(request: Request, response: Response) -> Dict[str, Any]:
    """忘记全部受信设备：所有设备下次都需重新登录。"""
    server = _server_cfg()
    n = auth.drop_all_trust()
    auth.audit("trusted-revoke", _client(request, server), _ua(request),
               "已忘记全部受信设备（%d 台）" % n)
    response.delete_cookie(auth.TRUST_COOKIE, path="/")
    return {"ok": True, "revoked": n}


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


# ------------------------------------------------------------------ 隐私解锁（快速解锁码）
@router.post("/set-pin")
def set_pin(payload: Dict[str, Any]) -> Dict[str, Any]:
    """设置 / 清除「快速解锁码」（需已登录；pin 为空表示清除）。

    磁盘只保存 PBKDF2 哈希，明文永不落盘、也不会下发到前端。
    遮挡层解锁凭据统一由 /api/privacy/unlock 校验，这里只负责写入。
    """
    pin = str((payload or {}).get("pin") or "").strip()
    if pin and len(pin) < 4:
        raise HTTPException(400, "解锁码至少 4 位")
    if len(pin) > 64:
        raise HTTPException(400, "解锁码过长")
    # 设置解锁码即切到「快速解锁码」；清除后回落到「访问令牌」，不会出现空档
    patch = {"pin_hash": auth.hash_pin(pin), "lock_unlock": "pin" if pin else "token"}
    if pin:
        patch["token_unlocks"] = 0     # 已经设了解锁码，不再需要「用令牌解锁」提示
    update_config({"privacy": patch})
    auth.audit("set-pin", detail="已更新快速解锁码" if pin else "已清除快速解锁码")
    return {"ok": True, "has_pin": bool(pin)}


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
    """隐私锁定状态：前端刷新页面后据此决定是否继续显示遮挡层。

    unlock_mode 只有两种：pin（快速解锁码，已设置时）/ token（访问令牌，始终可用）。
    """
    st = auth.privacy_state()
    privacy = _privacy_cfg()
    has_pin = bool((privacy.get("pin_hash") or "").strip())
    mode = str(privacy.get("lock_unlock") or "token")
    if mode != "pin" or not has_pin:
        mode = "token"
    return {
        "locked": bool(st.get("locked")),
        "since": st.get("since", 0),
        "reason": st.get("reason", ""),
        "unlock_mode": mode,
        "has_pin": has_pin,
        # 未设解锁码时，用访问令牌解锁的次数：前端据此提示「设个解锁码吧」
        "token_unlocks": int(privacy.get("token_unlocks") or 0),
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
def privacy_unlock(payload: Dict[str, Any], request: Request, response: Response) -> Dict[str, Any]:
    """解锁遮挡层：凭据可以是**快速解锁码**，也可以直接用**访问令牌**。

    访问令牌始终可兜底 —— 忘记解锁码时不必去改 config.json；反之只有解锁码
    没有令牌也无所谓，两者任一即可。不再有「点击即解锁」模式。
    """
    from .config import current_access_token
    server = _server_cfg()
    privacy = _privacy_cfg()
    ip = _client(request, server)
    ua = _ua(request)
    credential = str((payload or {}).get("credential") or "").strip()
    stored = (privacy.get("pin_hash") or "").strip()

    _throttle(server, ip, ua, "解锁尝试")

    expected = current_access_token()
    by_token = bool(expected) and credential == expected
    if by_token:
        auth.audit("unlock", ip, ua, "已用访问令牌解锁", ok=True)
    elif stored and auth.verify_pin(credential, stored):
        auth.audit("unlock", ip, ua, "已用快速解锁码解锁", ok=True)
    else:
        n = _record_fail(ip)
        auth.audit("unlock", ip, ua, "解锁凭据错误（第 %d 次）" % n, ok=False)
        msg = "解锁码或访问令牌不正确"
        max_fails = int(server.get("login_max_fails") or 0)
        if max_fails and max_fails - n > 0:
            msg += "，还可尝试 %d 次" % (max_fails - n)
        raise HTTPException(401, msg)

    auth.clear_fails(ip)
    auth.privacy_unlock()

    # 未设解锁码、又只能用访问令牌解锁：计数一次，前端据此给出设置引导
    if by_token and not stored:
        update_config({"privacy": {
            "token_unlocks": int(privacy.get("token_unlocks") or 0) + 1}})

    # 用「访问令牌」解锁本身就证明持有凭据：顺手签发会话。
    # 否则未登录状态下（例如登出后又触发锁定）解锁完刷新会被登录页再要一次令牌。
    if by_token:
        sid = auth.cookie_value(request.scope, auth.SESSION_COOKIE)
        idle, absd = _idle_abs(server)
        if not (sid and auth.touch_session(sid, idle, absd)):
            newsid, _info = auth.new_session(ip, ua, idle, absd)
            _set_session_cookie(response, newsid, auth.is_https(request.scope), idle)

    auth.audit("privacy-unlock", ip, ua, "已解锁")
    return {"ok": True, "locked": False}


# ------------------------------------------------------------------ 两步验证（TOTP）
@router.get("/totp")
def totp_status(request: Request) -> Dict[str, Any]:
    """两步验证状态（不下发密钥与备用码）。"""
    server = _server_cfg()
    return {
        "enabled": bool(server.get("totp_enabled")) and bool((server.get("totp_secret") or "").strip()),
        "recovery_left": len(server.get("totp_recovery") or []),
        "pending": auth.totp_pending(_client(request, server)),
        "issuer": totp_mod.ISSUER,
        "digits": totp_mod.DIGITS,
        "period": totp_mod.STEP,
    }


@router.post("/totp/setup")
def totp_setup(request: Request) -> Dict[str, Any]:
    """开始绑定：生成密钥并返回 otpauth 链接（此时**尚未启用**，需再确认一次验证码）。"""
    server = _server_cfg()
    ip = _client(request, server)
    if bool(server.get("totp_enabled")) and (server.get("totp_secret") or "").strip():
        raise HTTPException(400, "两步验证已启用，请先关闭再重新绑定")
    secret = totp_mod.new_secret()
    auth.totp_stash(ip, secret)
    # 账号名固定为 ASCII（不要塞 IP / 邮箱样式）：微软验证器对这类标签兼容性差
    account = totp_mod.DEFAULT_ACCOUNT
    return {
        "ok": True,
        "secret": secret,
        "uri": totp_mod.provisioning_uri(secret, account),
        # 前端显示服务器时间，便于和手机时间对比（时钟偏差是验证码对不上的头号原因）
        "issuer": totp_mod.ISSUER,
        "account": account,
        "server_time": int(time.time()),
        "period": totp_mod.STEP,
        "digits": totp_mod.DIGITS,
    }


@router.get("/totp/qr")
def totp_qr(request: Request) -> Response:
    """把当前待确认的 otpauth 链接渲染成二维码（绑定用）。

    密钥直接从服务端暂存里取，**不经过 URL 或响应正文**，避免密钥出现在
    浏览器历史 / 反向代理日志里；重复请求图片也不会消费掉暂存。
    """
    import io

    import qrcode

    server = _server_cfg()
    ip = _client(request, server)
    secret = auth.totp_peek(ip)
    if not secret:
        raise HTTPException(400, "绑定已过期，请重新生成二维码")
    uri = totp_mod.provisioning_uri(secret, totp_mod.DEFAULT_ACCOUNT)
    # box_size 8 而不是 6：手机上扫屏更容易对上焦（微软验证器的扫码框较小）
    img = qrcode.QRCode(box_size=8, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    img.add_data(uri)
    img.make(fit=True)
    buf = io.BytesIO()
    img.make_image(fill_color="#111", back_color="#fff").save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/totp/enable")
def totp_enable(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """确认绑定：校验一次验证码后启用，并返回备用码（**只展示这一次**）。"""
    server = _server_cfg()
    ip = _client(request, server)
    ua = _ua(request)
    # 与登录共用同一套失败限速：绑定确认同样不能被反复尝试
    max_fails = int(server.get("login_max_fails") or 0)
    block_min = int(server.get("login_block_minutes") or 0)
    until = auth.blocked_until(ip, max_fails, block_min)
    now = time.time()
    if until > now:
        auth.audit("totp", ip, ua, "处于封禁期，拒绝绑定确认", ok=False)
        raise HTTPException(429, "尝试次数过多，请 %d 分钟后再试" % (int((until - now) // 60) + 1))
    code = str((payload or {}).get("code") or "").strip()
    secret, created = auth.totp_take(ip)
    if not secret:
        raise HTTPException(400, "绑定已超时，请重新生成二维码")
    if totp_mod.verify(secret, code) is None:
        # 输错不算致命失败，允许重试（密钥不丢），但要计数，且**保持原 TTL 起点**
        n = _record_fail(ip)
        auth.totp_stash(ip, secret, created)
        auth.audit("totp", ip, ua, "绑定确认失败：验证码不正确（第 %d 次）" % n, ok=False)
        raise HTTPException(401, "验证码不正确；请确认手机时间已自动同步后重试")
    codes = totp_mod.new_recovery_codes()
    auth.clear_fails(ip)          # 与 login / disable 一致：成功后清零失败计数
    update_config({"server": {
        "totp_enabled": True,
        "totp_secret": secret,
        "totp_last_step": int(time.time() // totp_mod.STEP),
        "totp_recovery": [totp_mod.hash_code(c) for c in codes],
    }})
    # 安全等级变了：所有「记住此设备」作废，其他设备必须重新走完整登录
    dropped = auth.drop_all_trust()
    auth.audit("totp", ip, ua, "已启用两步验证（吊销 %d 台受信设备）" % dropped, ok=True)
    return {"ok": True, "recovery_codes": codes}


@router.post("/totp/disable")
def totp_disable(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """关闭两步验证：需提交当前验证码或任一备用码。"""
    server = _server_cfg()
    ip = _client(request, server)
    ua = _ua(request)
    # 与登录共用同一套失败限速：验证码若能无限次尝试，两步验证即形同虚设
    max_fails = int(server.get("login_max_fails") or 0)
    block_min = int(server.get("login_block_minutes") or 0)
    until = auth.blocked_until(ip, max_fails, block_min)
    now = time.time()
    if until > now:
        auth.audit("totp", ip, ua, "处于封禁期，拒绝关闭尝试", ok=False)
        raise HTTPException(429, "尝试次数过多，请 %d 分钟后再试" % (int((until - now) // 60) + 1))
    secret = (server.get("totp_secret") or "").strip()
    if not (bool(server.get("totp_enabled")) and secret):
        return {"ok": True, "enabled": False}
    code = str((payload or {}).get("code") or "").strip()
    ok = totp_mod.verify(secret, code) is not None
    if not ok:
        ok = totp_mod.match_recovery(code, list(server.get("totp_recovery") or [])) is not None
    if not ok:
        n = _record_fail(ip)
        auth.audit("totp", ip, ua, "关闭两步验证失败：验证码不正确（第 %d 次）" % n, ok=False)
        raise HTTPException(401, "验证码不正确")
    auth.clear_fails(ip)
    update_config({"server": {
        "totp_enabled": False, "totp_secret": "", "totp_last_step": 0, "totp_recovery": [],
    }})
    dropped = auth.drop_all_trust()
    auth.audit("totp", ip, ua, "已关闭两步验证（吊销 %d 台受信设备）" % dropped, ok=True)
    return {"ok": True, "enabled": False}


# ------------------------------------------------------------------ 事务 API Key
@router.get("/keys")
def list_keys() -> Dict[str, Any]:
    """API Key 列表（只含名称 / 范围 / 使用情况，不含明文与哈希）。"""
    return {"items": auth.list_api_keys()}


@router.post("/keys")
def create_key(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """新建 API Key：明文只在本次响应里返回一次，请立即保存。"""
    server = _server_cfg()
    ip = _client(request, server)
    name = str((payload or {}).get("name") or "").strip()
    scope = str((payload or {}).get("scope") or "full").strip().lower()
    raw, info = auth.new_api_key(name, scope)
    auth.audit("api-key", ip, _ua(request), "已创建 API Key：%s（%s）" % (info["name"], info["scope"]))
    return {"ok": True, "key": raw, "item": {
        "id": info["id"], "name": info["name"], "scope": info["scope"],
        "created": info["created"], "last_used": 0.0, "uses": 0,
    }}


@router.delete("/keys/{key_id}")
def delete_key(key_id: str, request: Request) -> Dict[str, Any]:
    """吊销 API Key。"""
    server = _server_cfg()
    ip = _client(request, server)
    ok = auth.revoke_api_key(key_id)
    if ok:
        auth.audit("api-key", ip, _ua(request), "已吊销 API Key %s" % key_id)
    return {"ok": ok}
