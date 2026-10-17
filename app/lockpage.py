# -*- coding: utf-8 -*-
"""锁定期间返回的独立锁屏页。

为什么不用 SPA：刷新时浏览器要先下载、启动应用，再请求 /api/privacy/state
才知道被锁，中间会闪一下封面 / 标题。这里直接由服务端返回一个不含任何应用
数据的静态页面，样式与 SPA 内的遮挡层（PrivacyLock.vue）保持一致。

文案按 Accept-Language 选（中 / 繁 / 英 / 日），并按锁定原因（idle / panic）
与是否设置了解锁码切换标题、占位符与提示。
"""
from __future__ import annotations

from typing import Dict

_I18N: Dict[str, Dict[str, str]] = {
    "zh": {
        "lang": "zh-CN",
        "locked": "已锁定",
        "lockedHint": "已遮挡全部内容并暂停播放。锁定期间设置页同样打不开（避免有人借设置绕过），解锁后即恢复。",
        "panicTitle": "已遮挡",
        "panicHint": "已紧急遮挡内容并暂停播放",
        "unlockBtn": "解锁",
        "pinPh": "输入快速解锁码",
        "unlockPh": "输入访问令牌解锁",
        "pinHint": "忘记解锁码？直接输入访问令牌也能解锁（两者任一即可）。",
        "unlockHint": "访问令牌见「设置 → 访问」，或在服务器上读取 data/access_token.txt。",
        "show": "显示输入内容",
        "hide": "隐藏输入内容",
        "needCred": "请输入解锁码或访问令牌",
        "expired": "登录已过期：请改用访问令牌解锁（见 data/access_token.txt）",
        "network": "网络错误，请检查服务是否在运行",
        "foot": "锁定在服务端生效，刷新页面不会绕过。",
    },
    "tw": {
        "lang": "zh-TW",
        "locked": "已鎖定",
        "lockedHint": "已遮擋全部內容並暫停播放。鎖定期間設定頁同樣打不開（避免有人藉設定繞過），解鎖後即恢復。",
        "panicTitle": "已遮擋",
        "panicHint": "已緊急遮擋內容並暫停播放",
        "unlockBtn": "解鎖",
        "pinPh": "輸入快速解鎖碼",
        "unlockPh": "輸入存取令牌解鎖",
        "pinHint": "忘記解鎖碼？直接輸入存取令牌也能解鎖（兩者任一即可）。",
        "unlockHint": "存取令牌見「設定 → 存取」，或在伺服器上讀取 data/access_token.txt。",
        "show": "顯示輸入內容",
        "hide": "隱藏輸入內容",
        "needCred": "請輸入解鎖碼或存取令牌",
        "expired": "登入已過期：請改用存取令牌解鎖（見 data/access_token.txt）",
        "network": "網路錯誤，請檢查服務是否在執行",
        "foot": "鎖定在伺服器端生效，重新整理頁面不會繞過。",
    },
    "en": {
        "lang": "en",
        "locked": "Locked",
        "lockedHint": "Everything is veiled and playback is paused. Settings stays blocked while locked, and returns to normal once unlocked.",
        "panicTitle": "Veiled",
        "panicHint": "Veiled and playback paused",
        "unlockBtn": "Unlock",
        "pinPh": "Enter quick unlock code",
        "unlockPh": "Enter access token to unlock",
        "pinHint": "Forgot the code? The access token unlocks it too — either one works.",
        "unlockHint": "The token is under Settings → Access, or in data/access_token.txt on the server.",
        "show": "Show input",
        "hide": "Hide input",
        "needCred": "Enter the unlock code or access token",
        "expired": "Session expired: unlock with the access token (see data/access_token.txt)",
        "network": "Network error — is the service running?",
        "foot": "Locking happens on the server; refreshing does not bypass it.",
    },
    "ja": {
        "lang": "ja",
        "locked": "ロック中",
        "lockedHint": "全内容を遮断し、再生も停止しました。ロック中は設定画面も開けません。解除すると元に戻ります。",
        "panicTitle": "遮断中",
        "panicHint": "緊急遮断しました（再生も停止）",
        "unlockBtn": "解除",
        "pinPh": "解除コードを入力",
        "unlockPh": "アクセストークンを入力して解除",
        "pinHint": "コードを忘れた場合、アクセストークンでも解除できます（どちらでも可）。",
        "unlockHint": "トークンは「設定 → アクセス」、またはサーバー上の data/access_token.txt にあります。",
        "show": "入力内容を表示",
        "hide": "入力内容を隠す",
        "needCred": "解除コードまたはアクセストークンを入力してください",
        "expired": "ログインの有効期限が切れました。アクセストークンで解除してください（data/access_token.txt）",
        "network": "ネットワークエラー。サービスが起動しているか確認してください",
        "foot": "ロックはサーバー側で有効です。再読み込みでは回避できません。",
    },
}


def pick_lang(accept_language: str) -> str:
    al = (accept_language or "").lower()
    if "zh-tw" in al or "zh-hk" in al or "zh-hant" in al:
        return "tw"
    if al.startswith("ja"):
        return "ja"
    if al.startswith("zh"):
        return "zh"
    if al.startswith("en"):
        return "en"
    return "zh"


_TEMPLATE = """<!doctype html>
<html lang="@@LANG@@" data-theme="dark">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>@@TITLE@@</title>
<style>
  :root {
    --c-surface: #12151d; --c-surface-2: #181c26; --c-line: #232936;
    --c-text: #f1f3f8; --c-text-2: #b6bed0; --c-text-3: #6b7280;
    --c-primary: #e0355a; --c-danger: #e5484d;
  }
  html[data-theme="light"] {
    --c-surface: #ffffff; --c-surface-2: #f4f5f9; --c-line: #d4d8e2;
    --c-text: #171922; --c-text-2: #303647; --c-text-3: #6b7280;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; display: flex; align-items: center; justify-content: center;
    background: rgba(10, 12, 16, .74); color: var(--c-text);
    backdrop-filter: blur(30px) saturate(.55);
    -webkit-backdrop-filter: blur(30px) saturate(.55);
    font: 14px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
  }
  .pv-box {
    width: min(420px, 88vw); text-align: center; padding: 30px 26px 24px;
    background: var(--c-surface); border: 1px solid var(--c-line);
    border-radius: 16px; box-shadow: 0 20px 60px rgba(0, 0, 0, .5);
  }
  .pv-icon { font-size: 32px; line-height: 1; }
  .pv-title { font-size: 17px; font-weight: 700; margin: 10px 0 6px; }
  .pv-desc { color: var(--c-text-2); font-size: 13px; line-height: 1.65; margin: 0 0 18px; }
  .pv-unlock { display: flex; flex-direction: column; gap: 10px; }
  .pv-input { position: relative; }
  .pv-input input {
    width: 100%; height: 40px; padding: 0 44px 0 12px; font-size: 14px;
    color: var(--c-text); background: var(--c-surface-2);
    border: 1px solid var(--c-line); border-radius: 8px; outline: none;
  }
  .pv-input input:focus { border-color: var(--c-primary); }
  .pv-eye {
    position: absolute; right: 4px; top: 4px; width: 32px; height: 32px;
    display: flex; align-items: center; justify-content: center;
    color: var(--c-text-3); background: transparent; border: 0; border-radius: 6px; cursor: pointer;
  }
  .pv-eye:hover { background: var(--c-surface-2); color: var(--c-text); }
  .btn-primary {
    width: 100%; height: 42px; font-size: 14px; font-weight: 600; color: #fff;
    background: var(--c-primary); border: 0; border-radius: 8px; cursor: pointer;
  }
  .btn-primary:disabled { opacity: .55; cursor: default; }
  .pv-err { min-height: 18px; margin: 10px 0 0; color: var(--c-danger); font-size: 13px; }
  .pv-hint { color: var(--c-text-3); font-size: 12px; line-height: 1.7; margin: 12px 0 0; }
</style>
</head>
<body>
<div class="pv-box">
  <div class="pv-icon">🔒</div>
  <div class="pv-title">@@TITLE@@</div>
  <p class="pv-desc">@@DESC@@</p>
  <div class="pv-unlock">
    <div class="pv-input">
      <input id="cred" type="password" name="avm-code" autocomplete="new-password"
             autocapitalize="off" spellcheck="false" data-lpignore="true" data-1p-ignore
             data-bwignore placeholder="@@PH@@" autofocus />
      <button type="button" class="pv-eye" id="eye" title="@@SHOW@@" aria-label="@@SHOW@@">👁</button>
    </div>
    <button class="btn-primary" id="go">@@BTN@@</button>
  </div>
  <p class="pv-err" id="err"></p>
  <p class="pv-hint">@@HINT@@</p>
  <p class="pv-hint">@@FOOT@@</p>
</div>
<script src="/lock.js" defer
        data-msg-needed="@@NEED@@"
        data-msg-expired="@@EXPIRED@@"
        data-msg-network="@@NETWORK@@"
        data-msg-show="@@SHOW@@"
        data-msg-hide="@@HIDE@@"></script>
</body>
</html>
"""


def render_lock_page(accept_language: str, reason: str, has_pin: bool) -> str:
    t = _I18N.get(pick_lang(accept_language), _I18N["zh"])
    panic = (reason or "") == "panic"
    title = t["panicTitle"] if panic else t["locked"]
    desc = t["panicHint"] if panic else t["lockedHint"]
    ph = t["pinPh"] if has_pin else t["unlockPh"]
    hint = t["pinHint"] if has_pin else t["unlockHint"]
    html = _TEMPLATE
    for key, val in (
        ("LANG", t["lang"]), ("TITLE", title), ("DESC", desc), ("PH", ph),
        ("SHOW", t["show"]), ("BTN", t["unlockBtn"]), ("HINT", hint),
        ("FOOT", t["foot"]), ("NEED", t["needCred"]), ("EXPIRED", t["expired"]),
        ("NETWORK", t["network"]), ("HIDE", t["hide"]),
    ):
        html = html.replace("@@%s@@" % key, val)
    return html
