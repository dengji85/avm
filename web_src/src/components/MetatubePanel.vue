<template>
  <div class="panel">
    <div class="panel-head">
      {{ $t('settings.mtTitle') }}
      <span class="sub">{{ $t('settings.mtSub') }}</span>
    </div>
    <div class="panel-body">
      <div class="field-row checkbox">
        <label>{{ $t('settings.mtEnable') }}</label>
        <label class="toggle">
          <input type="checkbox" v-model="mt.enabled" /><span class="track"></span>
        </label>
      </div>

      <div class="field-row">
        <label>{{ $t('settings.mtMode') }}</label>
        <select v-model="mt.mode" style="width: 220px">
          <option value="external">{{ $t('settings.mtModeExternal') }}</option>
          <option value="managed">{{ $t('settings.mtModeManaged') }}</option>
        </select>
      </div>

      <div class="field-row">
        <label>{{ $t('settings.mtUrl') }}</label>
        <input v-model="mt.base_url" placeholder="http://127.0.0.1:18080" />
      </div>

      <div class="field-row">
        <label>{{ $t('settings.mtToken') }}</label>
        <input
          v-model="mt.token"
          type="password"
          autocomplete="new-password"
          :placeholder="st.token_set ? $t('settings.mtTokenSet') : $t('settings.mtTokenPh')"
        />
      </div>

      <div class="mt-state">
        <span class="badge" :class="badge.cls">{{ badge.text }}</span>
        <span class="muted">{{ statusText }}</span>
      </div>

      <div v-if="task.running" class="mt-bar">
        <div class="mt-bar-in" :style="{ width: Math.max(2, task.percent || 0) + '%' }"></div>
      </div>
      <p v-if="task.running" class="muted">{{ task.phase }} · {{ task.percent || 0 }}% {{ task.message || '' }}</p>

      <p class="muted">{{ $t('settings.mtHint') }}</p>
      <p v-if="msg" class="hint">{{ msg }}</p>
    </div>

    <div class="panel-foot">
      <button class="btn primary" :disabled="saving" @click="save(true)">{{ $t('common.save') }}</button>
      <button class="btn" :disabled="busy" @click="doTest">{{ $t('settings.testBtn') }}</button>
      <template v-if="mt.mode === 'managed'">
        <button class="btn" :disabled="busy || task.running" @click="doInstall">
          {{ st.installed ? $t('settings.mtReinstall') : $t('settings.mtDownload') }}
        </button>
        <button v-if="!st.running" class="btn" :disabled="busy || !st.installed" @click="doStart">
          {{ $t('settings.mtStart') }}
        </button>
        <button v-else class="btn" @click="doStop">{{ $t('settings.mtStop') }}</button>
      </template>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { getConfig, putConfig, mtStatus, mtTask, mtInstall, mtStart, mtStop, mtTest } from '../api.js'
import { toast } from '../utils.js'
import { t } from '../i18n/index.js'

const mt = reactive({ enabled: false, mode: 'external', base_url: 'http://127.0.0.1:18080', token: '' })
const st = ref({})
const task = ref({})
const msg = ref('')
const busy = ref(false)
const saving = ref(false)
let timer = null

const badge = computed(() => {
  if (!mt.enabled) return { cls: 'sys', text: t('settings.mtStateOff') }
  if (st.value.running) return { cls: 'ok', text: t('settings.mtStateRunning') }
  if (st.value.reachable) return { cls: 'ok', text: t('settings.mtStateReachable') }
  if (st.value.installed) return { cls: 'warn', text: t('settings.mtStateInstalled') }
  return { cls: 'err', text: t('settings.mtStateBad') }
})

const statusText = computed(() => {
  const s = st.value || {}
  const bits = []
  if (s.server_version) bits.push(s.server_version)
  else if (s.installed_version) bits.push(s.installed_version)
  if (s.base_url) bits.push(s.base_url)
  if (s.error) bits.push(s.error)
  return bits.join(' · ')
})

async function load() {
  try {
    const c = await getConfig()
    Object.assign(mt, { ...(c.metatube || {}), token: '' })
  } catch (e) {
    msg.value = String(e.message || e)
  }
  await refresh()
}

async function refresh() {
  try {
    st.value = await mtStatus()
    task.value = await mtTask()
  } catch {
    /* 服务未就绪时静默 */
  }
}

async function save(notify = false) {
  saving.value = true
  try {
    await putConfig({ metatube: { ...mt } })
    // 托管模式：启用后如果二进制已就绪但服务没跑，直接拉起，省掉一次手动点击
    if (mt.enabled && mt.mode === 'managed') {
      try {
        const s = await mtStatus()
        if (!s.running && s.installed) await mtStart()
      } catch {
        /* 启动失败留给状态行显示 */
      }
    }
    if (notify) toast(t('settings.mtSaved'))
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
    toast(String(e.message || e), 'err')
  } finally {
    saving.value = false
  }
}

async function doTest() {
  busy.value = true
  msg.value = ''
  try {
    await save(false)
    const r = await mtTest({ base_url: mt.base_url, token: mt.token })
    msg.value = r.ok
      ? `${t('settings.mtTestOk')}: ${r.app} ${r.version || ''}`
      : `${t('settings.mtTestFail')}: ${r.error || ''}`
    toast(r.ok ? t('settings.mtTestOk') : t('settings.mtTestFail'), r.ok ? 'ok' : 'err')
  } catch (e) {
    msg.value = String(e.message || e)
  } finally {
    busy.value = false
    await refresh()
  }
}

async function doInstall() {
  busy.value = true
  try {
    await mtInstall()
    startPoll()
  } catch (e) {
    toast(String(e.message || e), 'err')
  } finally {
    busy.value = false
  }
}

async function doStart() {
  busy.value = true
  try {
    await mtStart()
    startPoll()
  } catch (e) {
    toast(String(e.message || e), 'err')
  } finally {
    busy.value = false
    await refresh()
  }
}

async function doStop() {
  try {
    await mtStop()
    await refresh()
  } catch (e) {
    toast(String(e.message || e), 'err')
  }
}

function startPoll() {
  stopPoll()
  timer = setInterval(async () => {
    await refresh()
    if (!task.value.running) {
      stopPoll()
      await refresh()
    }
  }, 1500)
}

function stopPoll() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

onMounted(async () => {
  await load()
  timer = setInterval(refresh, 4000)
})
onBeforeUnmount(stopPoll)
</script>

<style scoped>
.mt-state {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 4px 0 8px;
  min-height: 24px;
}
.mt-bar {
  height: 4px;
  border-radius: 999px;
  background: var(--c-surface-2);
  overflow: hidden;
  margin: 6px 0;
}
.mt-bar-in {
  height: 100%;
  background: var(--c-primary);
  transition: width 0.3s ease;
}
</style>
