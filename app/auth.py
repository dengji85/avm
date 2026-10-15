# -*- coding: utf-8 -*-
"""访问认证：真实客户端 IP、会话存储、登录限速与审计。

设计要点（详见 docs/安全与隐私设计.md）：

* **真实客户端 IP**：只有直连来源本身位于 ``trusted_proxies`` 时才解析
  ``X-Forwarded-For``（从右往左取第一个非信任跳），否则**忽略**该头，
  防止伪造 IP 绕过本机豁免与登录限速。
* **会话默认只存内存**：进程 / 容器重启即全部失效（用户明确诉求）。
  可用 ``server.session_persist`` 开启落盘；落盘只保存会话 ID 的
  **SHA-256 指纹**，磁盘上不会出现明文凭据。
* **登录限速与审计**：同 IP 连续失败达到阈值后临时封禁，并记录最近事件。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# 与 app/main.py 保持一致的本机地址集合
LOCAL_ADDRS = {"127.0.0.1", "::1", "localhost", "testclient"}

SESSION_COOKIE = "avm_session"
COOKIE_TOKEN = "avm_token"  # 旧版前端写入的令牌 Cookie（升级后由前端清理）

_LOCK = threading.RLock()
_SESSIONS: Dict[str, Dict[str, Any]] = {}   # 指纹 -> {created, last, ip, ua}
_FAILS: Dict[str, List[float]] = {}         # ip -> 失败时间戳列表
_AUDIT: List[Dict[str, Any]] = []           # 最近事件（环形）
MAX_AUDIT = 200

_PERSIST = False                            # 是否落盘（由配置同步）


# ------------------------------------------------------------------ 请求工具
def http_headers(scope) -> Dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in (scope.get("headers") or [])}


def cookie_value(scope, name: str) -> str:
    from urllib.parse import unquote
    raw = http_headers(scope).get("cookie") or ""
    for part in raw.split(";"):
        key, _, val = part.strip().partition("=")
        if key == name:
            return unquote(val)
    return ""


def is_https(scope) -> bool:
    """请求是否经 HTTPS（兼容反代：X-Forwarded-Proto）。"""
    if scope.get("scheme") == "https":
        return True
    proto = (http_headers(scope).get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return proto == "https"


def client_ip(scope, trusted: Optional[List[str]] = None) -> str:
    """解析真实客户端 IP（见模块文档）。"""
    peer = (scope.get("client") or ("", 0))[0] or ""
    trusted_set = {t.strip() for t in (trusted or []) if t}
    if peer not in trusted_set:
        return peer  # 直连来源不可信 → 忽略 XFF
    xff = http_headers(scope).get("x-forwarded-for") or ""
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    for ip in reversed(parts):
        if ip not in trusted_set:
            return ip
    return parts[0] if parts else peer


def is_local_ip(ip: str) -> bool:
    return (ip or "") in LOCAL_ADDRS


def fingerprint(sid: str) -> str:
    return hashlib.sha256(("avm-session:" + (sid or "")).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 会话
def set_persist(flag: bool) -> None:
    global _PERSIST
    _PERSIST = bool(flag)


def persist_enabled() -> bool:
    return _PERSIST


def _persist_path() -> str:
    from .config import DATA_DIR
    return str(DATA_DIR / "sessions.json")


def load_persisted() -> int:
    """启动时恢复落盘会话；返回恢复条数。仅在开启持久化时调用。"""
    if not _PERSIST:
        return 0
    try:
        with open(_persist_path(), "r", encoding="utf-8") as f:
            data = json.loads(f.read() or "{}")
    except Exception:
        return 0
    n = 0
    with _LOCK:
        for fp, info in (data.get("sessions") or {}).items():
            if isinstance(info, dict) and float(info.get("created") or 0) > 0:
                _SESSIONS[fp] = info
                n += 1
    if n:
        audit("restore", detail="已恢复 %d 个持久会话" % n)
    return n


def _save_persisted() -> None:
    if not _PERSIST:
        return
    try:
        path = _persist_path()
        with _LOCK:
            payload = json.dumps({"sessions": _SESSIONS}, ensure_ascii=False)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)  # 仅属主可读写
        except Exception:
            pass
    except Exception:
        pass


def session_cookie(sid: str, https: bool, idle_days: int) -> str:
    """构造会话 Cookie 的 Set-Cookie 值。

    HttpOnly（JS 读不到，防 XSS 窃取）+ SameSite=Lax（拦跨站 POST 形式的 CSRF）；
    HTTPS 下追加 Secure。登录与「URL 令牌换会话」共用，避免两处写法不一致。
    """
    parts = ["%s=%s" % (SESSION_COOKIE, sid), "Path=/", "HttpOnly", "SameSite=Lax"]
    if idle_days > 0:
        parts.append("Max-Age=%d" % (idle_days * 86400))
    if https:
        parts.append("Secure")
    return "; ".join(parts)


def new_session(ip: str, ua: str, idle_days: int, abs_days: int) -> Tuple[str, Dict[str, Any]]:
    sid = secrets.token_urlsafe(32)
    now = time.time()
    info = {"created": now, "last": now, "ip": ip or "", "ua": (ua or "")[:200]}
    with _LOCK:
        _SESSIONS[fingerprint(sid)] = info
    _save_persisted()
    return sid, dict(info)


def touch_session(sid: str, idle_days: int, abs_days: int) -> Optional[Dict[str, Any]]:
    """校验并续期会话；不存在或已过期返回 None（顺带删除过期项）。"""
    if not sid:
        return None
    fp = fingerprint(sid)
    now = time.time()
    with _LOCK:
        info = _SESSIONS.get(fp)
        if not info:
            return None
        created = float(info.get("created") or 0)
        last = float(info.get("last") or created)
        if abs_days > 0 and now - created > abs_days * 86400:
            _SESSIONS.pop(fp, None)
            _save_persisted()
            return None
        if idle_days > 0 and now - last > idle_days * 86400:
            _SESSIONS.pop(fp, None)
            _save_persisted()
            return None
        info["last"] = now
        return dict(info)


def drop_session(sid: str) -> bool:
    if not sid:
        return False
    fp = fingerprint(sid)
    with _LOCK:
        hit = _SESSIONS.pop(fp, None) is not None
    if hit:
        _save_persisted()
    return hit


def drop_all() -> int:
    with _LOCK:
        n = len(_SESSIONS)
        _SESSIONS.clear()
    if n:
        _save_persisted()
    return n


def prune(idle_days: int, abs_days: int) -> int:
    """清理过期会话，返回清理条数。"""
    now = time.time()
    dead: List[str] = []
    with _LOCK:
        for fp, info in _SESSIONS.items():
            created = float(info.get("created") or 0)
            last = float(info.get("last") or created)
            if abs_days > 0 and now - created > abs_days * 86400:
                dead.append(fp)
            elif idle_days > 0 and now - last > idle_days * 86400:
                dead.append(fp)
        for fp in dead:
            _SESSIONS.pop(fp, None)
    if dead:
        _save_persisted()
    return len(dead)


def list_sessions() -> List[Dict[str, Any]]:
    """活跃会话列表；id 为指纹前 12 位（对外的可操作标识）。"""
    now = time.time()
    out: List[Dict[str, Any]] = []
    with _LOCK:
        for fp, info in _SESSIONS.items():
            last = float(info.get("last") or 0)
            created = float(info.get("created") or 0)
            out.append({
                "id": fp[:12],
                "ip": info.get("ip", ""),
                "ua": info.get("ua", ""),
                "created": created,
                "last": last,
                "age_sec": int(now - created) if created else 0,
                "idle_sec": int(now - last) if last else 0,
            })
    out.sort(key=lambda x: x["last"], reverse=True)
    return out


def revoke_by_public_id(public_id: str) -> bool:
    pid = (public_id or "").strip().lower()
    if len(pid) < 8:
        return False
    with _LOCK:
        for fp in list(_SESSIONS.keys()):
            if fp.startswith(pid):
                _SESSIONS.pop(fp, None)
                _save_persisted()
                return True
    return False


def fingerprint_of(sid: str) -> str:
    """返回会话指纹（供接口标记"当前会话"）。"""
    return fingerprint(sid)


# ------------------------------------------------------------------ 登录限速
def record_fail(ip: str, keep_min: int = 60) -> int:
    now = time.time()
    with _LOCK:
        lst = [t for t in _FAILS.get(ip, []) if now - t < keep_min * 60]
        lst.append(now)
        _FAILS[ip] = lst
        return len(lst)


def fail_count(ip: str, window_min: int) -> int:
    now = time.time()
    with _LOCK:
        lst = [t for t in _FAILS.get(ip, []) if now - t < max(1, window_min) * 60]
        _FAILS[ip] = lst
        return len(lst)


def blocked_until(ip: str, max_fails: int, block_min: int) -> float:
    """返回封禁截止时间戳；未封禁返回 0。"""
    if max_fails <= 0 or block_min <= 0:
        return 0.0
    now = time.time()
    with _LOCK:
        lst = [t for t in _FAILS.get(ip, []) if now - t < max(block_min, 60) * 60]
        _FAILS[ip] = lst
        if len(lst) >= max_fails:
            return lst[-1] + block_min * 60
    return 0.0


def clear_fails(ip: str) -> None:
    with _LOCK:
        _FAILS.pop(ip, None)


# ------------------------------------------------------------------ 审计
def audit(event: str, ip: str = "", ua: str = "", detail: str = "", ok: bool = True) -> None:
    with _LOCK:
        _AUDIT.append({
            "ts": time.time(),
            "event": event,
            "ip": ip or "",
            "ua": (ua or "")[:120],
            "detail": detail,
            "ok": bool(ok),
        })
        del _AUDIT[:-MAX_AUDIT]


def recent_audit(limit: int = 50) -> List[Dict[str, Any]]:
    with _LOCK:
        return list(reversed(_AUDIT[-max(1, min(limit, MAX_AUDIT)):]))


# ------------------------------------------------------------------ 隐私解锁（简单密码）
PIN_ITERATIONS = 120_000


def hash_pin(pin: str) -> str:
    """把隐私解锁「简单密码」派生为 PBKDF2 哈希，格式 ``pbkdf2$<salt_hex>$<hash_hex>``。

    传空字符串返回 ""（表示清除）。磁盘上不保存明文。
    """
    p = (pin or "").strip()
    if not p:
        return ""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", p.encode("utf-8"), salt, PIN_ITERATIONS)
    return "pbkdf2$%s$%s" % (salt.hex(), dk.hex())


def verify_pin(pin: str, stored: str) -> bool:
    """校验简单密码（常量时间比较）。"""
    p = (pin or "").strip()
    s = (stored or "").strip()
    if not p or not s:
        return False
    try:
        algo, salt_hex, hash_hex = s.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", p.encode("utf-8"), bytes.fromhex(salt_hex), PIN_ITERATIONS)
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ------------------------------------------------------------------ 隐私锁定（服务端状态）
# 隐私锁定必须由服务端持有：以前只存在前端内存里，任何人刷新页面（F5）就能绕过。
# 现在锁定期间中间件会拒绝除「解锁相关」以外的所有接口，刷新 / 直接调 API 都拿不到内容。
_PRIVACY: Dict[str, Any] = {"locked": False, "since": 0.0, "reason": ""}


def privacy_lock(reason: str = "manual") -> Dict[str, Any]:
    """置为「已锁定」（幂等）。"""
    with _LOCK:
        if not _PRIVACY.get("locked"):
            _PRIVACY["locked"] = True
            _PRIVACY["since"] = time.time()
        _PRIVACY["reason"] = reason or "manual"
        return dict(_PRIVACY)


def privacy_unlock() -> Dict[str, Any]:
    """解除锁定。"""
    with _LOCK:
        _PRIVACY["locked"] = False
        _PRIVACY["since"] = 0.0
        _PRIVACY["reason"] = ""
        return dict(_PRIVACY)


def privacy_locked() -> bool:
    with _LOCK:
        return bool(_PRIVACY.get("locked"))


def privacy_state() -> Dict[str, Any]:
    with _LOCK:
        return dict(_PRIVACY)
