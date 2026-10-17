# -*- coding: utf-8 -*-
"""两步验证（TOTP，RFC 6238）。

不引入第三方依赖：HMAC-SHA1 + 30 秒时间步 + Base32 密钥，兼容
Google Authenticator / Microsoft Authenticator / 1Password / Aegis / 群晖验证器等。

安全要点：
* 校验用**常量时间**比较，允许前后各 2 个时间步（±60 秒，容忍手机时钟漂移）；
* 返回命中的时间步，调用方记录后即可防止同一验证码在有效期内被重放；
* 备用码（恢复码）只保存 SHA-256，命中后立即从列表移除（一次性）。

兼容性要点（微软验证器踩过的坑）：
* otpauth 里的 issuer 与账号名**只用 ASCII**（`AVM:admin`）。微软验证器对含中文 /
  邮箱样式的标签兼容性差，会出现"扫进去了但验证码对不上"；
* 账号名不要带 `@host`，避免被当成邮箱账号处理；
* 用户输入兼容全角数字、空格与短横线（`123 456` / `123-456` 都能识别）。
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
WINDOW = 2       # 允许前后各 2 个时间步（±60s）：手机时间没自动同步时也能过
# 验证器 App 里显示的发行方。**必须是 ASCII**：微软验证器遇到中文 issuer 容易
# 出现"条目加进去了但验证码始终不对"，用短英文名最稳。
ISSUER = "AVM"
DEFAULT_ACCOUNT = "admin"
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


def norm_code(code: str) -> str:
    """把用户输入规整成纯数字：兼容全角数字、空格、短横线等分隔符。

    微软验证器 / 部分输入法会把验证码显示或输入成 `123 456`、`123-456`、`１２３４５６`，
    这里统一处理，避免"看着没错但校验失败"。
    """
    out = []
    for ch in str(code or ""):
        o = ord(ch)
        if 0xFF10 <= o <= 0xFF19:      # 全角数字 ０-９
            o -= 0xFEE0
        if 48 <= o <= 57:
            out.append(chr(o))
    return "".join(out)


def verify(secret: str, code: str, after: float = 0.0) -> Optional[int]:
    """校验验证码：通过返回命中的时间步，否则返回 None。

    ``after`` 传上次成功的时间步即可防重放（只接受更晚的时间步）。
    """
    c = norm_code(code)
    if not secret or len(c) != DIGITS:
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


def provisioning_uri(secret: str, account: str = DEFAULT_ACCOUNT) -> str:
    """生成 otpauth:// 链接（前端据此渲染二维码给验证器扫描）。

    格式严格遵循 Key Uri Format 且**只用 ASCII**：
        otpauth://totp/AVM:admin?secret=...&issuer=AVM&algorithm=SHA1&digits=6&period=30
    这是微软 / Google / 1Password / Aegis 都能稳定识别的写法。
    """
    from urllib.parse import quote, urlencode
    acct = (account or DEFAULT_ACCOUNT).strip() or DEFAULT_ACCOUNT
    params = {
        "secret": secret,
        "issuer": ISSUER,
        "algorithm": "SHA1",
        "digits": DIGITS,
        "period": STEP,
    }
    # 冒号保留字面量（`AVM:admin`），与 pyotp / Google 官方示例一致：
    # 编成 %3A 虽然合法，但个别验证器不做 URL 解码，会把标签显示成乱码。
    label = quote("%s:%s" % (ISSUER, acct), safe=":")
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
    """备用码哈希（大小写、空格、短横线一律不敏感）。

    用户可能抄成 `XXXXXXXX-XXXXXXXX`、`XXXXXXXX XXXXXXXX` 或连写 16 位，
    统一只保留字母数字后再哈希，三种写法都算同一个码。
    """
    c = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    return hashlib.sha256(("avm-recovery:" + c).encode("utf-8")).hexdigest()


def match_recovery(code: str, hashes: List[str]) -> Optional[str]:
    """校验备用码：命中返回对应哈希（调用方负责从列表删除，实现一次性）。"""
    h = hash_code(code)
    for item in (hashes or []):
        if hmac.compare_digest(str(item), h):
            return str(item)
    return None
