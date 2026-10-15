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
 * 启动即遮挡（privacy.lock_on_start）。解锁方式见 privacy.lock_unlock：
 * click = 点击即解锁 / pin = 简单密码 / token = 访问令牌。
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
const unlockMode = ref('token')    // click / pin / token（按服务端状态计算，pin 未设置时回退 token）
const secret = ref('')
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

function pauseAll() {
  document.querySelectorAll('video').forEach((v) => {
    try { v.pause() } catch (e) { /* 忽略 */ }
  })
}

/* ---------------------------------------------------------------- 状态同步 */
function applyState(st) {
  if (!st) return
  const m = st.unlock_mode || 'token'
  // 选了「简单密码」但尚未设置时按「访问令牌」处理，不会变成敞开
  unlockMode.value = m === 'click' ? 'click' : (m === 'pin' && st.has_pin ? 'pin' : 'token')
  if (st.locked && !locked.value) {
    locked.value = true
    reason.value = st.reason || 'idle'
    pauseAll()
  } else if (!st.locked) {
    locked.value = false
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
  err.value = ''
  pauseAll()
  try { await privacyLock(why) } catch (e) { /* 离线时至少已本地遮挡 */ }
}

/* ---------------------------------------------------------------- 解锁 */
async function doUnlock(credential) {
  await privacyUnlock(credential)
  locked.value = false
  secret.value = ''
  // 记住"本次浏览器会话已解锁"：否则开了「启动即遮挡」时，
  // 解锁后的整页刷新会立刻再次遮挡，导致永远进不去设置页。
  try { sessionStorage.setItem(UNLOCKED_KEY, '1') } catch (e) { /* 忽略 */ }
  // 锁定期间内容接口都被 423 拒绝，解锁后整页刷新以重新加载数据
  window.location.reload()
}

async function tryUnlock() {
  if (busy.value) return
  if (unlockMode.value === 'click') { await doUnlock(''); return }
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
  if (isTyping(e.target)) return
  // 只有「点击即解锁」模式才允许任意键解锁；pin / token 必须输入，避免被一下子解开
  if (locked.value && unlockMode.value === 'click') tryUnlock()
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
    if (cfg.value.neutral_title) document.title = t('nav.brand')
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
  pauseAll()
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

      <template v-if="unlockMode === 'click'">
        <button class="btn primary" :disabled="busy" @click="tryUnlock">{{ $t('privacy.unlockBtn') }}</button>
        <p class="pv-hint">{{ $t('privacy.clickHint') }}</p>
      </template>

      <template v-else>
        <div class="pv-unlock">
          <input
            v-model="secret"
            type="password"
            :placeholder="unlockMode === 'pin' ? $t('privacy.pinPh') : $t('privacy.unlockPh')"
            @keyup.enter="tryUnlock"
            autofocus
          />
          <button class="btn primary" :disabled="busy" @click="tryUnlock">{{ $t('privacy.unlockBtn') }}</button>
        </div>
        <p v-if="err" class="pv-err">{{ err }}</p>
        <p class="pv-hint">{{ unlockMode === 'pin' ? $t('privacy.pinHint') : $t('privacy.unlockHint') }}</p>
      </template>
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
  width: min(420px, 88vw); text-align: center; padding: 28px 24px;
  background: var(--c-surface, #16181d);
  border: 1px solid var(--c-line, rgba(255,255,255,.12));
  border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,.5);
}
.pv-icon { font-size: 30px; }
.pv-title { font-size: var(--fs-lg, 17px); font-weight: 700; margin: 8px 0 4px; }
.pv-desc { color: var(--c-text-2, #9aa3af); font-size: var(--fs-sm, 13px); margin: 0 0 14px; }
.pv-unlock { display: flex; gap: 8px; }
.pv-unlock input { flex: 1; }
.pv-err { color: var(--c-danger, #e5484d); font-size: var(--fs-sm, 13px); margin: 8px 0 0; }
.pv-hint { color: var(--c-text-3, #6b7280); font-size: 12px; margin: 10px 0 0; }
</style>
