<script setup>
/**
 * 桌面隐私保护（见 docs/安全与隐私设计.md 的 L4 隐私与防窥）。
 *
 * 设计要点：**锁定状态由服务端持有**，前端只是渲染器。
 *   · 以前遮挡层只存在于前端内存，任何人刷新页面（F5）就能绕过；
 *   · 现在前端触发锁定时会调用 `POST /api/privacy/lock`，服务端随即进入锁定态，
 *     中间件拒绝除「解锁相关接口」以外的所有请求（封面 / 影片 / 流媒体都拿不到），
 *     所以刷新、重开标签页、直接调接口都无法绕过。
 *
 * 三种触发：空闲自动锁定（privacy.idle_lock_minutes）、紧急遮挡键（privacy.panic_key）、
 * 启动即遮挡（privacy.lock_on_start）。
 * 解锁只有两种凭据：pin = 快速解锁码（设了就用它）/ token = 访问令牌（始终可兜底）。
 */
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { getConfig, onLocked, privacyLock, privacyState, privacyUnlock } from '../api.js'
import { useI18n } from '../i18n/index.js'

const { t } = useI18n()

// 浏览器会话级标记：解锁后本次会话不再被「启动即遮挡」重复拦截
const UNLOCKED_KEY = 'avm_privacy_unlocked'

const DEFAULT_CFG = {
  idle_lock_minutes: 10,
  lock_unlock: 'token',
  panic_key: 'Ctrl+Shift+H',
  neutral_title: true,
  pause_when_hidden: true,
  lock_on_start: false,
}

const cfg = ref({ ...DEFAULT_CFG })
const locked = ref(false)          // 服务端锁定的本地镜像
const reason = ref('idle')         // idle / panic / start / server
const unlockMode = ref('token')    // pin / token（按服务端状态计算，未设解锁码时回退 token）
const secret = ref('')
const showSecret = ref(false) // 解锁输入框的明文 / 密文切换
const err = ref('')
const busy = ref(false)

let idleTimer = null
let pollTimer = null
let lastActivity = Date.now()
let hotkey = parseHotkey(DEFAULT_CFG.panic_key)

const ACTIVITY_EVENTS = ['mousemove', 'mousedown', 'keydown', 'wheel', 'touchstart', 'scroll']

/* ---------------------------------------------------------------- 视频工具 */
function videoPlaying() {
  const vids = document.querySelectorAll('video')
  for (const v of vids) {
    if (!v.paused && !v.ended && v.readyState > 2) return true
  }
  return false
}

/* 被锁定暂停的播放记录：解锁后原样恢复，保留播放器与播放进度 */
let pausedByLock = []

/* 全局锁定标志：锁定时媒体流会被服务端 423 拒绝，播放器据此忽略
   "加载失败"事件，避免误触发降级（转封装 / 系统播放器） */
function markLocked(on) {
  try { window.__avmLocked = !!on } catch (e) { /* 忽略 */ }
}

function pauseAll(record = false) {
  document.querySelectorAll('video').forEach((v) => {
    if (v.paused) return
    try { v.pause() } catch (e) { /* 忽略 */ }
    if (record) {
      pausedByLock.push({ el: v, muted: v.muted })
      v.muted = true // 兜底：防止锁定瞬间还有声音漏出
    }
  })
}

/** 解锁后恢复被锁定时暂停的播放（不整页刷新，播放位置与播放器状态都保留） */
function resumePlayback() {
  const list = pausedByLock
  pausedByLock = []
  list.forEach(({ el, muted }) => {
    try {
      el.muted = muted
      el.play().catch(() => { /* 用户可能已手动暂停，忽略 */ })
    } catch (e) { /* 忽略 */ }
  })
}

/* ---------------------------------------------------------------- 状态同步 */
function applyState(st) {
  if (!st) return
  const m = st.unlock_mode || 'token'
  // 选了「快速解锁码」但尚未设置时按「访问令牌」处理，不会变成敞开
  unlockMode.value = (m === 'pin' && st.has_pin) ? 'pin' : 'token'
  if (st.locked && !locked.value) {
    locked.value = true
    reason.value = st.reason || 'idle'
    markLocked(true)
    pauseAll(true)
  } else if (!st.locked) {
    if (locked.value) {
      markLocked(false)
      locked.value = false
      // 其它标签页 / 其它设备解锁时，本页同步解除遮挡：把锁定期间被暂停、静音的视频
      // 恢复回来，并刷新视图（锁定期间内容接口返回 423，视图数据是空的）。
      // 原先只置 locked=false，会出现「遮挡层没了，但播放器一直停着」的状态。
      secret.value = ''
      resumePlayback()
      window.dispatchEvent(new CustomEvent('avm-refresh'))
    } else {
      locked.value = false
    }
  }
}

async function syncState() {
  try { applyState(await privacyState()) } catch (e) { /* 离线忽略 */ }
}

/* ---------------------------------------------------------------- 触发锁定 */
async function lockNow(why) {
  if (locked.value) return
  reason.value = why
  locked.value = true      // 先本地遮挡（即时反馈），再落服务端状态
  secret.value = ''
  showSecret.value = false // 每次遮挡都回到密文，避免明文残留在屏幕上
  err.value = ''
  pauseAll(true)   // 记录播放状态，解锁后恢复
  markLocked(true) // 让播放器忽略 423 触发的加载失败，避免误触发降级
  try { await privacyLock(why) } catch (e) { /* 离线时至少已本地遮挡 */ }
}

/* ---------------------------------------------------------------- 解锁 */
async function doUnlock(credential) {
  await privacyUnlock(credential)
  locked.value = false
  secret.value = ''
  // 记住"本次浏览器会话已解锁"：否则开了「启动即遮挡」时，刷新后会立刻再次遮挡
  try { sessionStorage.setItem(UNLOCKED_KEY, '1') } catch (e) { /* 忽略 */ }
  // 不整页刷新：保留播放器、播放进度与播放队列，只恢复播放，
  // 并广播刷新让视图重新拉取数据（锁定期间内容接口返回 423）。
  markLocked(false)
  resumePlayback()
  window.dispatchEvent(new CustomEvent('avm-refresh'))
}

async function tryUnlock() {
  if (busy.value) return
  const isPin = unlockMode.value === 'pin'
  const v = secret.value.trim()
  if (!v) { err.value = isPin ? t('privacy.pinWrong') : t('common.token.failed'); return }
  busy.value = true
  err.value = ''
  try {
    await doUnlock(v)
  } catch (e) {
    err.value = e.message || (isPin ? t('privacy.pinWrong') : t('common.token.failed'))
  } finally {
    busy.value = false
  }
}

/* ---------------------------------------------------------------- 空闲检测 */
function bump() { lastActivity = Date.now() }

function tick() {
  const mins = Number(cfg.value.idle_lock_minutes) || 0
  if (mins <= 0 || locked.value) return
  if (videoPlaying()) { lastActivity = Date.now(); return } // 看片中不算空闲
  if (Date.now() - lastActivity >= mins * 60000) lockNow('idle')
}

/* ---------------------------------------------------------------- 快捷键 */
function parseHotkey(s) {
  const parts = String(s || '').split('+').map((x) => x.trim().toLowerCase()).filter(Boolean)
  const mods = ['ctrl', 'shift', 'alt', 'meta']
  const key = parts.filter((p) => !mods.includes(p)).pop() || ''
  return {
    ctrl: parts.includes('ctrl'),
    shift: parts.includes('shift'),
    alt: parts.includes('alt'),
    meta: parts.includes('meta'),
    key,
  }
}

function isTyping(el) {
  const tag = ((el && el.tagName) || '').toLowerCase()
  return tag === 'input' || tag === 'textarea' || !!(el && el.isContentEditable)
}

function onKeydown(e) {
  const h = hotkey
  if (h.key && (e.key || '').toLowerCase() === h.key
      && h.ctrl === e.ctrlKey && h.shift === e.shiftKey
      && h.alt === e.altKey && h.meta === e.metaKey) {
    e.preventDefault()
    if (!locked.value) lockNow('panic')
    return
  }
  // 锁定时任何按键都不自动解锁：解锁码 / 令牌必须手动输入
}

/* ---------------------------------------------------------------- 生命周期 */
function onVisibility() {
  if (document.hidden && cfg.value.pause_when_hidden) pauseAll()
}

async function loadCfg() {
  try {
    const c = await getConfig()
    cfg.value = Object.assign({ ...DEFAULT_CFG }, (c && c.privacy) || {})
    hotkey = parseHotkey(cfg.value.panic_key)
    // 注意：标题由 App.vue 统一设置（品牌名 + privacy.site_subtitle 后缀），
    // 这里不要覆盖，否则会把用户自定义的混淆后缀冲掉。
  } catch (e) { /* 锁定或离线时沿用默认值（不影响解锁） */ }
}

function onPrivacyChanged() { loadCfg() } // 设置页改动隐私配置后立即生效

onMounted(async () => {
  await loadCfg()
  await syncState()
  window.addEventListener('avm-privacy-changed', onPrivacyChanged)
  // 任何接口收到 423 都意味着服务端已锁定：立即切到遮挡层
  onLocked(() => {
    if (!locked.value) { reason.value = 'server'; pausedSync() }
  })
  // 「启动即遮挡」每个浏览器会话只触发一次：解锁后的刷新若再次遮挡，
  // 会导致用户永远进不去设置页（无法关闭该开关）。
  const unlockedThisSession = (() => {
    try { return sessionStorage.getItem(UNLOCKED_KEY) === '1' } catch (e) { return false }
  })()
  if (cfg.value.lock_on_start && !locked.value && !unlockedThisSession) await lockNow('start')
  ACTIVITY_EVENTS.forEach((ev) => window.addEventListener(ev, bump, { passive: true }))
  window.addEventListener('keydown', onKeydown)
  document.addEventListener('visibilitychange', onVisibility)
  idleTimer = setInterval(tick, 5000)
  pollTimer = setInterval(syncState, 20000) // 多标签页 / 多设备同步
})

function pausedSync() {
  locked.value = true
  markLocked(true)
  pauseAll(true)
}

onBeforeUnmount(() => {
  ACTIVITY_EVENTS.forEach((ev) => window.removeEventListener(ev, bump))
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('avm-privacy-changed', onPrivacyChanged)
  document.removeEventListener('visibilitychange', onVisibility)
  if (idleTimer) clearInterval(idleTimer)
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <div v-if="locked" class="pv-layer">
    <div class="pv-box">
      <div class="pv-icon">🔒</div>
      <div class="pv-title">{{ reason === 'panic' ? $t('privacy.panicTitle') : $t('privacy.locked') }}</div>
      <p class="pv-desc">{{ reason === 'panic' ? $t('privacy.panicHint') : $t('privacy.lockedHint') }}</p>

      <div class="pv-unlock">
        <div class="pv-input">
          <input
            v-model="secret"
            :type="showSecret ? 'text' : 'password'"
            name="avm-code"
            autocomplete="new-password"
            autocapitalize="off"
            spellcheck="false"
            data-lpignore="true"
            data-1p-ignore
            data-bwignore
            :placeholder="unlockMode === 'pin' ? $t('privacy.pinPh') : $t('privacy.unlockPh')"
            @keyup.enter="tryUnlock"
            autofocus
          />
          <button
            type="button"
            class="pv-eye"
            :title="$t(showSecret ? 'privacy.hideSecret' : 'privacy.showSecret')"
            :aria-label="$t(showSecret ? 'privacy.hideSecret' : 'privacy.showSecret')"
            @click="showSecret = !showSecret"
          >{{ showSecret ? '🙈' : '👁' }}</button>
        </div>
        <button class="btn primary large block" :disabled="busy" @click="tryUnlock">{{ $t('privacy.unlockBtn') }}</button>
      </div>
      <p v-if="err" class="pv-err">{{ err }}</p>
      <p class="pv-hint">{{ unlockMode === 'pin' ? $t('privacy.pinHint') : $t('privacy.unlockHint') }}</p>
    </div>
  </div>
</template>

<style scoped>
/* 覆盖全屏（含播放器）：z-index 高于播放器与抽屉 */
.pv-layer {
  position: fixed; inset: 0; z-index: 4000;
  display: flex; align-items: center; justify-content: center;
  background: rgba(10, 12, 16, .74);
  backdrop-filter: blur(30px) saturate(.55);
  -webkit-backdrop-filter: blur(30px) saturate(.55);
  animation: pv-in .18s ease;
}
@keyframes pv-in { from { opacity: 0; } to { opacity: 1; } }
.pv-box {
  width: min(420px, 88vw); text-align: center; padding: 30px 26px 24px;
  background: var(--c-surface, #16181d);
  border: 1px solid var(--c-line, rgba(255,255,255,.12));
  border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,.5);
}
.pv-icon { font-size: 32px; line-height: 1; }
.pv-title { font-size: var(--fs-lg, 17px); font-weight: 700; margin: 10px 0 6px; }
.pv-desc { color: var(--c-text-2, #9aa3af); font-size: var(--fs-sm, 13px); line-height: 1.65; margin: 0 0 18px; }

/* 解锁区：控件一律整行、同高（40px），保证与弹窗中线对齐。
   原先「输入框 + 小眼睛 + 按钮」挤在一行，其中小眼睛按钮只有 26px 高，
   三个元素高度不一，整行看上去是歪的。 */
.pv-unlock { display: flex; flex-direction: column; gap: 10px; }
.pv-input { position: relative; }
.pv-input input { height: 40px; padding-right: 44px; font-size: var(--fs-md, 14px); }
.pv-eye {
  position: absolute; top: 50%; right: 5px; transform: translateY(-50%);
  width: 30px; height: 30px;
  display: inline-flex; align-items: center; justify-content: center;
  padding: 0; border: 0; border-radius: var(--r-sm, 6px);
  background: transparent; color: var(--c-text-3, #6b7280);
  font-size: 15px; line-height: 1; cursor: pointer;
  transition: background var(--t-fast, .15s), color var(--t-fast, .15s);
}
.pv-eye:hover { background: var(--c-surface-2, rgba(255,255,255,.06)); color: var(--c-text, #e6e8ee); }
.pv-err { color: var(--c-danger, #e5484d); font-size: var(--fs-sm, 13px); margin: 10px 0 0; }
.pv-hint { color: var(--c-text-3, #6b7280); font-size: 12px; line-height: 1.7; margin: 12px 0 0; }
</style>
