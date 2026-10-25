# -*- coding: utf-8 -*-
"""FastAPI 应用装配。"""
from __future__ import annotations

import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Scope, Receive, Send
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, auth, lockpage
from .api import router
from .api_auth import privacy_router, router as auth_router
from .config import (WEB_DIR, ensure_dirs, DATA_DIR, CONFIG_PATH, TOKEN_FILE,
                     load_config, ensure_access_token, current_access_token, mask_token,
                     session_cookie_name, IN_CONTAINER)
from .db import init_db

# 会话 Cookie 名按实例区分：同机多实例（开发 8770 + 容器 8771）互不覆盖
auth.SESSION_COOKIE = session_cookie_name()
# 受信设备 Cookie 同样按实例区分，否则同机多实例会互相覆盖「记住此设备」
_cookie_name = auth.SESSION_COOKIE
auth.TRUST_COOKIE = (_cookie_name.replace("session", "trust")
                     if "session" in _cookie_name else _cookie_name + "_trust")

# API 文档自托管：不引用任何 CDN（国内 / 离线 / 纯 http 都能打开）。
# docs_url/redoc_url 关掉 FastAPI 自带的 Swagger 页面（它从 cdn.jsdelivr.net 拉脚本，
# 既被本项目的 CSP 拦、国内也常常打不开），改由 /api/docs 返回我们自己的页面。
app = FastAPI(
    title="片匣 · 本地 AV 收藏管理",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

# 跨源白名单：默认空 = 不启用 CORS（前后端同源部署本不需要 CORS）。
# 如需跨源访问，在 server.cors_origins 里显式列出；改动需重启生效。
_cors_origins = [str(o) for o in ((load_config().get("server") or {}).get("cors_origins") or []) if o]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        allow_headers=["Content-Type", "X-Access-Token", "X-Requested-With",
                       "Authorization", "X-API-Key"],
    )

# 公开的静态资源目录（前缀匹配，路径由 Vite 构建产物决定）
# /api/cast/media：DLNA 投屏的媒体出口，电视没有会话，只能靠投屏短时令牌鉴权
_PUBLIC_PREFIXES = ("/assets", "/icons", "/favicon", "/logo", "/api/cast/media")

# 无需登录即可访问的**精确**路径：首页、健康检查、登录 / 登出 / 会话状态查询。
# 必须精确匹配 —— 早先写成前缀匹配时，`/api/auth/session` 会顺带放行
# `/api/auth/sessions`（会话与设备列表、含 IP）和 `/api/auth/logout-all`（一次登出所有设备）。
# 注意：/api/docs 与 /api/openapi.json 不再默认公开（见 server.expose_docs）。
_PUBLIC_PATHS = {
    "/", "/index.html",
    "/api/health",
    "/api/auth/login", "/api/auth/logout", "/api/auth/session",
    # 解锁接口自带凭据校验（解锁码 / 访问令牌）与失败限速：
    # 登出后刷新进入锁屏页时没有会话，若不公开就永远解不开（只能重启服务）。
    "/api/privacy/unlock",
    # 自托管 API 文档的静态脚本（纯前端渲染，不含任何数据）
    "/api-docs.js",
    # 锁屏页 / 登录页脚本（页面本身必须能加载，且不含任何应用数据）
    "/lock.js",
    "/login.js",
}

# 隐私锁定期间仍可访问的路径（否则页面与解锁接口本身会被自己挡住）。
# 注意**不能**图省事整段放行 /api/auth/ —— 否则锁定状态下仍可调用
# /api/auth/set-pin（覆盖式改简单密码，无需旧密码）、/api/auth/sessions（列出设备与 IP）、
# /api/auth/audit（读登录记录），等于绕过了锁定本身。
# 登录 / 登出 / 会话状态查询已在 _PUBLIC_PREFIXES 中，不受锁定影响。
_LOCK_ALLOWED = (
    "/api/privacy/",           # state / unlock / lock（解锁凭据由该接口统一校验）
)

# 事务 API Key **不可**访问的凭据管理接口：Key 是机器凭据，一旦泄露若还能
# 自建新 Key（持久化）、绑定 / 解绑 TOTP（把所有者锁在门外）或一次登出所有设备，
# 影响面会被显著放大。这些操作必须由交互式会话完成。
_KEY_FORBIDDEN = (
    "/api/auth/keys",          # API Key 自身的创建 / 列表 / 吊销
    "/api/auth/totp",          # 两步验证的绑定、启用、关闭、二维码
    "/api/auth/logout-all",    # 一次登出全部设备
    "/api/auth/sessions",      # 活跃会话（含 IP / UA 的设备列表）
)


def _cookie_token(raw: str) -> str:
    """从 Cookie 头里取出旧版前端写入的 avm_token（升级后由前端清除）。"""
    from urllib.parse import unquote
    for part in (raw or "").split(";"):
        key, _, val = part.strip().partition("=")
        if key == auth.COOKIE_TOKEN:
            return unquote(val)
    return ""


def _query_param(scope: Scope, name: str) -> str:
    """取出 URL 查询串里的指定参数（不解析 '+' 为空格，避免改写 code）。"""
    from urllib.parse import unquote
    qs = scope.get("query_string", b"").decode("latin-1")
    prefix = name + "="
    for pair in qs.split("&"):
        if pair.startswith(prefix):
            return unquote(pair.split("=", 1)[1])
    return ""


def _query_token(scope: Scope) -> str:
    """取出 URL 查询串里的 token=xxx（兼容脚本、旧客户端与旧二维码链接）。"""
    return _query_param(scope, "token")


def _api_key_from(scope: Scope) -> str:
    """从 ``Authorization: Bearer ***`` 或 ``X-API-Key`` 头里取出事务 API Key。"""
    h = auth.http_headers(scope)
    bearer = (h.get("authorization") or "").strip()
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return (h.get("x-api-key") or "").strip()


def _token_valid(scope: Scope, cfg_token: str) -> bool:
    """校验「访问令牌」通道（兼容脚本、旧客户端与二维码链接）。

    支持 X-Access-Token 头、?token= 查询、旧版 avm_token Cookie。
    令牌为空时不放行——是否关闭访问保护由 server.token_required 决定，
    避免「配置里没有令牌」意外变成完全开放。
    """
    if not cfg_token:
        return False
    hmap = auth.http_headers(scope)
    provided = hmap.get("x-access-token")
    if not provided:
        provided = _query_token(scope)
    if not provided:
        # <img> / <video> / <a download> 等浏览器自主发起的请求无法携带自定义头，
        # 登录后的 HttpOnly 会话 Cookie 已覆盖该场景，这里仅兼容历史 Cookie。
        provided = _cookie_token(hmap.get("cookie") or "")
    return bool(provided) and provided == cfg_token


def _session_limits(server) -> tuple:
    try:
        idle = int(server.get("session_idle_days", 7) or 0)
    except (TypeError, ValueError):
        idle = 7
    try:
        absd = int(server.get("session_abs_days", 30) or 0)
    except (TypeError, ValueError):
        absd = 30
    return max(0, idle), max(0, absd)


class AccessTokenMiddleware(BaseHTTPMiddleware):
    """访问控制：会话 Cookie → 访问令牌 → 本机豁免（可配）。

    真实客户端 IP 由 server.trusted_proxies 解析，避免「同机反向代理」被
    误判为本机而整体放行；详见 docs/安全与隐私设计.md。
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        server = load_config().get("server") or {}
        ip = auth.client_ip(request.scope, server.get("trusted_proxies") or ["127.0.0.1", "::1"])
        request.scope["avm_client_ip"] = ip
        ua = request.headers.get("user-agent", "")

        # 0) 用带 ?token= 的链接打开首页（扫码 / 分享）：令牌有效时换一次会话，再 302 到干净地址。
        #    令牌留在 URL 里会被浏览器历史、反向代理 access log 与 Referer 记录；
        #    换成 HttpOnly 会话 Cookie 后，后续请求（含 <img>/<video>）自动通过，也不必每次带参数。
        #    必须放在下面的公开白名单**之前** —— 首页本身是公开的，放后面这段永远不会执行。
        if (path in ("/", "/index.html") and _query_token(request.scope)
                and _token_valid(request.scope, current_access_token())):
            idle, _absd = _session_limits(server)
            sid, _info = auth.new_session(ip, ua, idle, _absd)
            auth.audit("login", ip, ua, "通过 URL 令牌换取会话", ok=True)
            resp = RedirectResponse("/", status_code=302)
            resp.headers.append("Set-Cookie", auth.session_cookie(sid, auth.is_https(request.scope), idle))
            resp.headers.append("Set-Cookie",
                                "%s=; Path=/; Max-Age=0; SameSite=Lax" % auth.COOKIE_TOKEN)
            return resp

        # 0b) 用一次性短链 ?c=<code> 打开首页（二维码扫码）：消费即失效，
        #     不落进长期令牌。有效期与上限见 app/auth.py 的 new_login_link。
        link_code = _query_param(request.scope, "c")
        if path in ("/", "/index.html") and link_code:
            if auth.consume_login_link(link_code):
                idle, _absd = _session_limits(server)
                sid, _info = auth.new_session(ip, ua, idle, _absd)
                auth.audit("login", ip, ua, "通过一次性短链换取会话", ok=True)
                resp = RedirectResponse("/", status_code=302)
                resp.headers.append("Set-Cookie", auth.session_cookie(sid, auth.is_https(request.scope), idle))
                resp.headers.append("Set-Cookie",
                                    "%s=; Path=/; Max-Age=0; SameSite=Lax" % auth.COOKIE_TOKEN)
                return resp
            auth.audit("login", ip, ua, "一次性短链无效或已过期", ok=False)

        # 隐私锁定期间，首页直接返回「锁屏页」而不是 SPA：
        # 否则前端要先下载、启动、再请求 /api/privacy/state 才知道被锁，
        # 中间会闪一下封面 / 标题（刷新时尤其明显）。锁屏页不含任何应用数据。
        if path in ("/", "/index.html") and auth.privacy_locked():
            st = auth.privacy_state()
            privacy = load_config().get("privacy") or {}
            return HTMLResponse(
                lockpage.render_lock_page(
                    request.headers.get("accept-language", ""),
                    str(st.get("reason") or ""),
                    bool((privacy.get("pin_hash") or "").strip()),
                    IN_CONTAINER, str(TOKEN_FILE),
                ),
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
            )

        # 未登录（含刚登出）：首页直接返回登录页，而不是先加载 SPA 再弹窗。
        # 否则登出后会闪一下应用外壳、再在角上浮出一个小登录框，与锁屏页完全是
        # 两套观感。登录页与锁屏页共用同一套 .pv-* 卡片，且不含任何应用数据。
        if path in ("/", "/index.html") and server.get("token_required", True):
            sid0 = auth.cookie_value(request.scope, auth.SESSION_COOKIE)
            idle0, absd0 = _session_limits(server)
            signed = bool(sid0) and auth.touch_session(sid0, idle0, absd0)
            if not signed and not _token_valid(request.scope, current_access_token()):
                # 「记住此设备 30 天」：会话过期后自动续一个，不用再找令牌
                did = auth.cookie_value(request.scope, auth.TRUST_COOKIE)
                if did and auth.touch_trust(did):
                    idle0, absd0 = _session_limits(server)
                    nsid, _info = auth.new_session(ip, ua, idle0, absd0)
                    auth.audit("login", ip, ua, "受信设备自动登录", ok=True)
                    resp = RedirectResponse("/", status_code=302)
                    resp.headers.append(
                        "Set-Cookie",
                        auth.session_cookie(nsid, auth.is_https(request.scope), idle0))
                    return resp
                totp_on = (bool(server.get("totp_enabled"))
                           and bool((server.get("totp_secret") or "").strip()))
                return HTMLResponse(
                    lockpage.render_login_page(
                        request.headers.get("accept-language", ""), totp_on,
                        IN_CONTAINER, str(TOKEN_FILE)),
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
                )

        if (path.startswith(_PUBLIC_PREFIXES) or path in _PUBLIC_PATHS
                or (bool(server.get("expose_docs"))
                    and path in ("/api/docs", "/api/openapi.json", "/openapi.json"))):
            return await call_next(request)

        if server.get("token_required", True):
            allowed = False
            # 1) 会话 Cookie（登录后签发；默认仅内存，server.session_persist 可落盘）
            sid = auth.cookie_value(request.scope, auth.SESSION_COOKIE)
            if sid:
                idle, absd = _session_limits(server)
                if auth.touch_session(sid, idle, absd):
                    allowed = True
                    request.scope["avm_session_ok"] = True
                    request.scope["avm_auth_type"] = "session"
            # 2) 访问令牌（兼容脚本 / 旧客户端；环境变量 AVM_ACCESS_TOKEN 优先）
            #    不再有「本机免令牌」：本机、手机、NAS 一视同仁，令牌是唯一凭据。
            if not allowed and _token_valid(request.scope, current_access_token()):
                allowed = True
                request.scope["avm_auth_type"] = "token"
            # 3) 事务 API Key（Authorization: Bearer / X-API-Key），供脚本与自动化。
            #    与登录会话分离：可命名、可单独吊销、scope=read 时只允许读取。
            #    注意 API Key **不受两步验证约束** —— 它是机器凭据，要求交互式验证码
            #    会让自动化不可用；代价是「多了一把不需要两步验证的钥匙」，设置页已提示。
            if not allowed:
                raw_key = _api_key_from(request.scope)
                if raw_key:
                    key_info = auth.verify_api_key(raw_key)
                    if key_info:
                        # 凭据管理接口不接受 API Key（见 _KEY_FORBIDDEN 注释）
                        if path.startswith(_KEY_FORBIDDEN):
                            auth.audit("deny", ip, ua,
                                       "%s（API Key 不可用于凭据管理）" % path, ok=False)
                            return JSONResponse(
                                status_code=403,
                                content={"detail": "该接口需交互式会话，API Key 不可用",
                                         "code": "KEY_FORBIDDEN"},
                            )
                        allowed = True
                        request.scope["avm_api_scope"] = key_info.get("scope") or "full"
                        request.scope["avm_api_name"] = key_info.get("name") or ""
                        request.scope["avm_auth_type"] = "key"

            # 4) 受信设备（「记住此设备 30 天」）：会话过期后自动续一个，
            #    不用把用户再推回登录页找令牌。设备是在完整登录（含两步验证）
            #    之后才登记的，因此不会绕过 2FA。
            if not allowed:
                did = auth.cookie_value(request.scope, auth.TRUST_COOKIE)
                if did and auth.touch_trust(did):
                    idle_d, absd_d = _session_limits(server)
                    nsid, _info = auth.new_session(ip, ua, idle_d, absd_d)
                    request.scope["avm_pending_session"] = (
                        nsid, auth.is_https(request.scope), idle_d)
                    request.scope["avm_auth_type"] = "device"
                    request.scope["avm_session_ok"] = True   # 已续出真实会话，等同登录态
                    allowed = True
                    auth.audit("login", ip, ua, "受信设备自动登录", ok=True)

            if not allowed:
                auth.audit("deny", ip, ua, path, ok=False)
                resp = JSONResponse(
                    status_code=401,
                    content={"detail": "需要访问令牌", "code": "NO_TOKEN"},
                )
                # 顺手清掉已失效的会话 Cookie：否则浏览器会一直带着作废的 sid 重试，
                # 表现为「刷新 / 点开播放又要求输令牌」（重启、换实例之后尤其明显）。
                resp.headers.append(
                    "Set-Cookie",
                    "%s=; Path=/; Max-Age=0; SameSite=Lax" % auth.SESSION_COOKIE,
                )
                return resp

        # 5) 隐私锁定（状态在服务端）：除解锁相关接口外一律拒绝。
        #    锁定状态不存前端，所以刷新页面 / 重开标签页 / 直接调接口都拿不到内容。
        #    这一段刻意放在 token_required 判断**之外**：关掉访问保护做本机调试时，
        #    锁定仍须生效，否则「锁定」在关闭访问控制后就形同虚设。
        if auth.privacy_locked() and not path.startswith(_LOCK_ALLOWED):
            return JSONResponse(
                status_code=423,
                content={"detail": "内容已锁定", "code": "LOCKED"},
            )

        # 6) 只读 API Key 不得执行写操作（脚本误用 / 泄漏后的影响面收敛）
        if request.scope.get("avm_api_scope") == "read" and request.method not in ("GET", "HEAD"):
            return JSONResponse(
                status_code=403,
                content={"detail": "该 API Key 为只读，不能执行写操作", "code": "READ_ONLY"},
            )

        response = await call_next(request)
        # 受信设备自动续期时补发会话 Cookie（放在响应上，客户端下次直接带会话）
        pending = request.scope.get("avm_pending_session")
        if pending:
            nsid, https_flag, idle_d = pending
            response.headers.append(
                "Set-Cookie", auth.session_cookie(nsid, https_flag, idle_d))
        return response


# 前端为本地打包资源，无第三方 CDN：CSP 收紧脚本来源。
# 注意两处放宽不能少：
#   font-src data:   —— video.js 的图标字体是内联 data URI，缺了它播放/暂停等按钮图标会全部消失
#   img-src https:   —— 远程封面 / 头像
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "img-src 'self' data: blob: https:; media-src 'self' blob:; connect-src 'self'; "
    "frame-ancestors 'self'; object-src 'none'; base-uri 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """安全响应头（可用 server.security_headers 关闭）。"""

    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        if not bool((load_config().get("server") or {}).get("security_headers", True)):
            return resp
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        h.setdefault("Content-Security-Policy", _CSP)
        if auth.is_https(request.scope):
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp


class AiClientContextMiddleware:
    """把「这次请求是谁发起的」记进 contextvar，供 AI 调用轨迹标注来源。

    纯 ASGI 中间件（在最外层），因此 contextvar 在整个请求处理期间可见，
    同步接口在线程池里执行时同样能读到（anyio 会复制上下文）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = auth.http_headers(scope)
        ua = headers.get("user-agent") or ""
        referer = headers.get("referer") or headers.get("origin") or ""
        ip = ""
        try:
            server = load_config().get("server") or {}
            ip = auth.client_ip(scope, server.get("trusted_proxies") or ["127.0.0.1", "::1"])
        except Exception:
            ip = ""
        from . import ai_core
        token = ai_core.set_client(ip=ip, ua=ua, referer=referer)
        try:
            await self.app(scope, receive, send)
        finally:
            ai_core.reset_client(token)


app.add_middleware(AccessTokenMiddleware)
# 后添加者位于外层：安全响应头包裹访问控制，401 响应同样带上安全头
app.add_middleware(SecurityHeadersMiddleware)
# 最外层：先记下调用方，再交给访问控制 / 业务处理
app.add_middleware(AiClientContextMiddleware)

app.include_router(router)
app.include_router(auth_router)
app.include_router(privacy_router)


def _backup_database() -> None:
    """启动时为 library.db 留一份每日备份，防止升级/误删丢数据。
    仅保留最近 7 份，避免无限增长。"""
    try:
        from pathlib import Path
        import shutil
        import datetime as _dt
        db = DATA_DIR / "library.db"
        if not db.exists():
            return
        stamp = _dt.date.today().strftime("%Y%m%d")
        bak = DATA_DIR / f"library.db.bak{stamp}"
        if not bak.exists():
            shutil.copy2(db, bak)
        # 清理超过 7 天的旧备份
        for old in DATA_DIR.glob("library.db.bak*"):
            try:
                days = (_dt.date.today() - _dt.date.fromtimestamp(old.stat().st_mtime)).days
                if days > 7:
                    old.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _record_version() -> None:
    """把当前程序版本写入 config.json，便于升级时识别与提示。"""
    try:
        data = {}
        if CONFIG_PATH.exists():
            try:
                data = __import__("json").loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if data.get("app_version") != __version__:
            data["app_version"] = __version__
            CONFIG_PATH.write_text(
                __import__("json").dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:
        pass


@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    init_db()
    _backup_database()
    _record_version()
    tok = ensure_access_token()
    cfg = load_config()
    server = cfg.get("server") or {}
    # 会话持久化：开启时恢复上次落盘的会话（默认关闭 → 容器重启即全部失效）
    auth.set_persist(bool(server.get("session_persist")))
    restored = auth.load_persisted()
    keys = auth.load_api_keys()
    host = server.get("host", "127.0.0.1")
    if host in ("0.0.0.0", ""):
        print("=" * 60)
        if bool(server.get("token_required", True)):
            print("访问保护已开启：所有设备（含本机）都需要访问令牌。")
            print("  令牌掩码：%s（完整值见 %s）" % (mask_token(tok), TOKEN_FILE))
            print("  也可用环境变量 AVM_ACCESS_TOKEN 预设；登录后可在设置页退出登录 / 管理会话。")
        else:
            print("⚠ 访问保护已关闭：任何能连上的设备都可直接访问（设置 → 访问 可重新开启）。")
        if restored:
            print("  已恢复 %d 个持久会话（server.session_persist=true）" % restored)
        if keys:
            print("  已加载 %d 个 API Key（设置 → 访问 可管理）" % keys)
        print("=" * 60)
    _start_auto_scan()


def _start_auto_scan() -> None:
    """后台定时增量扫描：按 library.auto_scan_interval（分钟）轮询媒体库。

    复用 scanner.run_scan(incremental=True) 的增量能力（仅处理新增/变更文件），
    与手动扫描共享 SCAN 锁，避免并发写库冲突。间隔为 0 时不启动。
    """
    import threading

    from . import scanner
    from .api import SCAN

    def _loop() -> None:
        while True:
            interval = int(load_config().get("library", {}).get("auto_scan_interval", 0) or 0)
            if interval <= 0:
                # 未启用：睡眠较长后重试（配置可能在运行时被打开）
                time.sleep(60)
                continue
            # 睡眠间隔（秒），按分钟换算
            time.sleep(interval * 60)
            try:
                if SCAN.running:
                    continue  # 手动扫描进行中，跳过本轮
                if not (load_config().get("library", {}).get("paths") or []):
                    continue  # 还没配置扫描目录
                if not SCAN.start():
                    continue
                def _run():
                    try:
                        scanner.run_scan(
                            progress_cb=SCAN.update,
                            incremental=True,
                            auto_cleanup=True,
                        )
                        SCAN.finish("自动扫描完成")
                    except Exception as e:  # noqa: BLE001
                        SCAN.error(str(e))
                        SCAN.finish("自动扫描失败：" + str(e))
                threading.Thread(target=_run, daemon=True).start()
            except Exception:  # noqa: BLE001
                try:
                    SCAN.cancel()
                except Exception:
                    pass

    t = threading.Thread(target=_loop, name="auto-scan", daemon=True)
    t.start()


@app.exception_handler(Exception)
async def _unhandled(request, exc):  # pragma: no cover
    """未捕获异常：对外只返回 request_id，细节写服务端日志（不回显内部信息）。"""
    rid = "%08x" % int(time.time() * 1000)
    try:
        import sys
        import traceback
        print("[error %s] %s\n%s" % (rid, request.url.path, traceback.format_exc()),
              file=sys.stderr)
    except Exception:
        pass
    return JSONResponse({"detail": "服务内部错误", "request_id": rid}, status_code=500)


@app.get("/")
def index() -> FileResponse:
    # index.html 不缓存，确保前端更新后立即生效（避免用户看到旧版）
    return FileResponse(
        WEB_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ------------------------------------------------------------------ API 文档（自托管）
# 不引用任何 CDN：脚本与样式都在本地，纯 http、国内网络、离线环境都能打开。
# 页面受访问保护约束（未登录 401）；server.expose_docs = true 时允许匿名查看。
@app.get("/api/docs", include_in_schema=False)
def api_docs_page() -> FileResponse:
    p = WEB_DIR / "api-docs.html"
    if not p.exists():
        raise HTTPException(404, "文档页面缺失（web_dist/api-docs.html）")
    return FileResponse(p, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api-docs.js", include_in_schema=False)
def api_docs_script() -> FileResponse:
    """文档页脚本单独成文件：CSP 禁止内联脚本（script-src 'self'）。"""
    p = WEB_DIR / "api-docs.js"
    if not p.exists():
        raise HTTPException(404, "文档脚本缺失（web_dist/api-docs.js）")
    return FileResponse(p, media_type="application/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/lock.js", include_in_schema=False)
def lock_script() -> FileResponse:
    """锁屏页脚本：锁定状态下必须能加载（含解锁逻辑，不含任何应用数据）。"""
    p = WEB_DIR / "lock.js"
    if not p.exists():
        raise HTTPException(404, "锁屏脚本缺失（web_dist/lock.js）")
    return FileResponse(p, media_type="application/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/login.js", include_in_schema=False)
def login_script() -> FileResponse:
    """登录页脚本：未登录时页面必须能加载（含两步验证切换，不含任何应用数据）。"""
    p = WEB_DIR / "login.js"
    if not p.exists():
        raise HTTPException(404, "登录脚本缺失（web_dist/login.js）")
    return FileResponse(p, media_type="application/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/openapi.json", include_in_schema=False)
def openapi_alias():
    """旧链接兼容：规格的真实地址是 /api/openapi.json。"""
    return JSONResponse(app.openapi())


# Vite 构建产物：模块脚本与静态资源位于 /assets 下
# 产物文件名自带 contenthash，可安全长缓存；用自定义 StaticFiles 复用相同策略。
class HashedStaticFiles(StaticFiles):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        # 带 hash 的资源视为不可变，长期缓存；其余不缓存
        if "-" in path.split("/")[-1]:
            resp.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/assets", HashedStaticFiles(directory=str(WEB_DIR / "assets")), name="assets")


# 品牌资源：favicon / logo / icons 位于 web_dist 根目录，浏览器标签页、桌面图标、关
# 于页都要用。后端没有把整个 WEB_DIR 挂在根下（仅 / 与 /assets 暴露），这里白名单
# 放行，路径逃逸校验保证只允许 web_dist 内的文件。
_BRAND_MIME = {
    "favicon.ico": "image/x-icon",
    "favicon.svg": "image/svg+xml",
    "logo.svg":    "image/svg+xml",
}
_BRAND_CACHE = "public, max-age=604800"  # 一周：favicon 变化频率低

@app.get("/favicon.ico")
def _favicon_ico() -> FileResponse:
    p = (WEB_DIR / "favicon.ico").resolve()
    return FileResponse(p, media_type="image/x-icon", headers={"Cache-Control": _BRAND_CACHE})

@app.get("/favicon.svg")
def _favicon_svg() -> FileResponse:
    p = (WEB_DIR / "favicon.svg").resolve()
    return FileResponse(p, media_type="image/svg+xml", headers={"Cache-Control": _BRAND_CACHE})

@app.get("/logo.svg")
def _logo_svg() -> FileResponse:
    p = (WEB_DIR / "logo.svg").resolve()
    return FileResponse(p, media_type="image/svg+xml", headers={"Cache-Control": _BRAND_CACHE})

@app.get("/logo.png")
def _logo_png() -> FileResponse:
    p = (WEB_DIR / "logo.png").resolve()
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": _BRAND_CACHE})

# 其余 PWA 图标（apple-touch-icon / icon-192 / icon-512 等）统一走 /icons 子目录
app.mount(
    "/icons",
    StaticFiles(directory=str(WEB_DIR / "icons"), check_dir=False),
    name="icons",
)
