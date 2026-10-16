<script setup>
import { ref, nextTick } from 'vue'
import { login } from '../api.js'
import { useI18n } from '../i18n/index.js'
import { toast } from '../utils.js'

const { t } = useI18n()
const visible = ref(false)
const token = ref('')
const showToken = ref(false)
const busy = ref(false)
const err = ref('')
// 两步验证：服务端启用后，令牌正确还会要求再输入一次验证器验证码。
// 令牌本身留在内存里（不重新写盘），用户不必重输。
const step = ref('token') // token | totp
const totpCode = ref('')
// 第二步输入框由 v-if 动态挂载，原生 autofocus 对动态节点无效，需手动聚焦
const totpInput = ref(null)

function open() { err.value = ''; step.value = 'token'; visible.value = true }
function close() { visible.value = false; totpCode.value = ''; step.value = 'token' }

async function submit() {
  if (busy.value) return
  const isTotp = step.value === 'totp'
  const v = isTotp ? totpCode.value.trim() : token.value.trim()
  if (!v) { err.value = isTotp ? t('common.token.totpFailed') : t('common.token.failed'); return }
  busy.value = true
  err.value = ''
  try {
    // 令牌只交给后端换取会话（HttpOnly Cookie），浏览器不再保存令牌本身
    const r = await login(token.value.trim(), isTotp ? v : '')
    if (r && r.need_totp) {
      // 令牌正确，但服务器启用了两步验证：切到验证码这一步（此时**尚未签发会话**）
      step.value = 'totp'
      totpCode.value = ''
      // 输入框刚被 v-if 挂载，等一帧再聚焦，用户不必手动点一下
      await nextTick()
      if (totpInput.value) totpInput.value.focus()
      return
    }
    token.value = ''
    totpCode.value = ''
    close()
    toast(t('common.token.saved'))
    // 页面里已经加载过的 <img>/<video> 用的是未认证的旧请求，失败后浏览器不会
    // 自动重试；整页刷新一次，确保封面、头像与播放立刻带上会话 Cookie。
    setTimeout(() => window.location.reload(), 300)
  } catch (e) {
    err.value = e.message || (isTotp ? t('common.token.totpFailed') : t('common.token.failed'))
    toast(err.value, 'err')
  } finally {
    busy.value = false
  }
}

defineExpose({ open })
</script>

<template>
  <div v-if="visible" class="modal-mask" @click.self="close">
    <div class="modal card token-gate">
      <div class="tg-head">
        <span class="icon">{{ step === 'totp' ? '🔐' : '🔑' }}</span>
        <h3>{{ step === 'totp' ? t('common.token.totpTitle') : t('common.token.title') }}</h3>
      </div>
      <p class="tg-desc">{{ step === 'totp' ? t('common.token.totpDesc') : t('common.token.desc') }}</p>
      <div v-if="step === 'token'" class="tg-input">
        <input
          :type="showToken ? 'text' : 'password'"
          v-model="token"
          name="avm-code"
          autocomplete="new-password"
          autocapitalize="off"
          spellcheck="false"
          data-lpignore="true"
          data-1p-ignore
          data-bwignore
          :placeholder="t('common.token.placeholder')"
          @keyup.enter="submit"
          autofocus
        />
        <button class="btn ghost tiny" @click="showToken = !showToken">{{ showToken ? '🙈' : '👁' }}</button>
      </div>
      <div v-else class="tg-input">
        <input
          ref="totpInput"
          v-model="totpCode"
          class="mono tg-code"
          name="avm-otp"
          inputmode="numeric"
          autocomplete="one-time-code"
          autocapitalize="off"
          spellcheck="false"
          maxlength="20"
          :placeholder="t('common.token.totpPh')"
          @keyup.enter="submit"
        />
      </div>
      <p v-if="err" class="tg-err">{{ err }}</p>
      <div class="tg-actions">
        <button v-if="step === 'totp'" class="btn ghost" @click="step = 'token'">{{ t('common.back') }}</button>
        <button v-else class="btn ghost" @click="close">{{ t('common.cancel') }}</button>
        <button class="btn primary" :disabled="busy" @click="submit">
          {{ busy ? t('common.token.signingIn') : (step === 'totp' ? t('common.token.totpEnter') : t('common.token.enter')) }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.token-gate { width: min(440px, 92vw); }
.tg-head { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-2); }
.tg-head .icon { font-size: 22px; }
.tg-head h3 { margin: 0; font-size: var(--fs-lg); }
.tg-desc { color: var(--c-text-2); font-size: var(--fs-sm); margin: var(--sp-2) 0 var(--sp-3); line-height: 1.5; }
.tg-input { display: flex; gap: var(--sp-2); }
.tg-input input { flex: 1; }
.tg-err { color: var(--c-danger, #e5484d); font-size: var(--fs-sm); margin: var(--sp-2) 0 0; }
.tg-actions { display: flex; justify-content: flex-end; gap: var(--sp-2); margin-top: var(--sp-4); }
@media (max-width: 480px) {
  .tg-actions { flex-wrap: wrap; }
  .tg-actions .btn { flex: 1 1 auto; }
}
</style>
