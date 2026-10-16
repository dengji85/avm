# -*- coding: utf-8 -*-
"""两步验证（TOTP，RFC 6238）。

不引入第三方依赖：HMAC-SHA1 + 30 秒时间步 + Base32 密钥，兼容
Google Authenticator / Microsoft Authenticator / 1Password / Aegis / 群晖验证器等。

安全要点：
* 校验用**常量时间**比较，允许 ±1 个时间步（容忍客户端时钟漂移）；
* 返回命中的时间步，调用方记录后即可防止同一验证码在有效期内被重放；
* 备用码（恢复码）只保存 SHA-256，命中后立即从列表移除（一次性）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import List, Optional

STEP = 30        # 时间步长（秒）
DIGITS = 6       # 验证码位数
WINDOW = 1       # 允许前后各 1 个时间步
ISSUER = "片匣"   # 验证器 App 里显示的发行方
RECOVERY_COUNT = 8


def new_secret() -> str:
    """生成 Base32 密钥（160 bit，与 RFC 4226 推荐一致）。"""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    s = (secret or "").strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * ((-len(s)) % 8))


def code_at(secret: str, counter: int) -> str:
    """计算指定时间步的验证码。"""
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def verify(secret: str, code: str, after: float = 0.0) -> Optional[int]:
    """校验验证码：通过返回命中的时间步，否则返回 None。

    ``after`` 传上次成功的时间步即可防重放（只接受更晚的时间步）。
    """
    c = (code or "").strip().replace(" ", "")
    if not secret or not c.isdigit() or len(c) != DIGITS:
        return None
    now = int(time.time() // STEP)
    try:
        for step in range(now - WINDOW, now + WINDOW + 1):
            if step <= int(after or 0):
                continue
            if hmac.compare_digest(code_at(secret, step), c):
                return step
    except Exception:
        return None
    return None


def provisioning_uri(secret: str, account: str = "avm") -> str:
    """生成 otpauth:// 链接（前端据此渲染二维码给验证器扫描）。"""
    from urllib.parse import quote, urlencode
    params = {
        "secret": secret,
        "issuer": ISSUER,
        "algorithm": "SHA1",
        "digits": DIGITS,
        "period": STEP,
    }
    label = quote("%s:%s" % (ISSUER, account or "avm"))
    return "otpauth://totp/%s?%s" % (label, urlencode(params))


# ------------------------------------------------------------------ 备用码
def new_recovery_codes() -> List[str]:
    """生成备用码（明文，只在启用时展示一次）。

    格式 ``XXXXXXXX-XXXXXXXX``：两段各 8 位十进制数字，合计约 **53 bit** 熵
    （早先每段 2 字节十六进制、合计 32 bit 偏低）。用纯数字而非十六进制是为了：
    ① 移动端输入框（inputmode=numeric）能弹出数字键盘；② 用户辨识与抄写更不容易出错。
    """
    digits = "0123456789"
    return [
        "%s-%s" % (
            "".join(secrets.choice(digits) for _ in range(8)),
            "".join(secrets.choice(digits) for _ in range(8)),
        )
        for _ in range(RECOVERY_COUNT)
    ]


def hash_code(code: str) -> str:
    """备用码哈希（大小写与分隔符不敏感）。"""
    c = (code or "").strip().upper().replace(" ", "")
    return hashlib.sha256(("avm-recovery:" + c).encode("utf-8")).hexdigest()


def match_recovery(code: str, hashes: List[str]) -> Optional[str]:
    """校验备用码：命中返回对应哈希（调用方负责从列表删除，实现一次性）。"""
    h = hash_code(code)
    for item in (hashes or []):
        if hmac.compare_digest(str(item), h):
            return str(item)
    return None
