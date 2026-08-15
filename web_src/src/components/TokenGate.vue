<script setup>
import { ref, onMounted } from 'vue'
import { getToken, setToken } from '../api.js'
import { useI18n } from '../i18n/index.js'
import { toast } from '../utils.js'

const { t } = useI18n()
const visible = ref(false)
const token = ref('')
const showToken = ref(false)

onMounted(() => {
  token.value = getToken()
})

function open() { visible.value = true }
function close() { visible.value = false }
function submit() {
  const v = token.value.trim()
  setToken(v)
  if (v) {
    close()
    toast(t('token.saved'))
    // 触发一次全局刷新，让被拦截的请求重新发起
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  } else {
    close()
    toast(t('token.cleared'))
  }
}

defineExpose({ open })
</script>

<template>
  <div v-if="visible" class="modal-mask" @click.self="close">
    <div class="modal card token-gate">
      <div class="tg-head">
        <span class="icon">🔑</span>
        <h3>{{ t('token.title') }}</h3>
      </div>
      <p class="tg-desc">{{ t('token.desc') }}</p>
      <div class="tg-input">
        <input
          :type="showToken ? 'text' : 'password'"
          v-model="token"
          :placeholder="t('token.placeholder')"
          @keyup.enter="submit"
          autofocus
        />
        <button class="btn ghost tiny" @click="showToken = !showToken">{{ showToken ? '🙈' : '👁' }}</button>
      </div>
      <div class="tg-actions">
        <button class="btn ghost" @click="close">{{ t('common.cancel') }}</button>
        <button class="btn primary" @click="submit">{{ t('token.enter') }}</button>
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
.tg-actions { display: flex; justify-content: flex-end; gap: var(--sp-2); margin-top: var(--sp-4); }
@media (max-width: 480px) {
  .tg-actions { flex-wrap: wrap; }
  .tg-actions .btn { flex: 1 1 auto; }
}
</style>
