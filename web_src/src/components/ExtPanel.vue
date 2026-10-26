<template>
  <div>
          <div class="aic-bar">
            <span class="hint">{{ $t('aiCenter.extHint') }}</span>
            <div class="aic-bar-ops">
              <button class="btn tiny ghost" :disabled="ext.loading" @click="loadExt">
                {{ $t('aiCenter.extRecheck') }}
              </button>
            </div>
          </div>
          <div v-for="c in ext.components" :key="c.id" class="aic-card">
            <div class="aic-card-head">
              <strong>{{ $t('aiCenter.extName.' + c.id) }}</strong>
              <span class="aic-badge" :class="c.state">{{ extStateText(c) }}</span>
              <span v-if="c.version" class="hint">{{ c.version }}</span>
              <span class="spacer"></span>
              <span v-if="c.size" class="hint">{{ fmtSize(c.size) }}</span>
            </div>
            <p class="hint">{{ $t('aiCenter.extDesc.' + c.id) }}</p>
            <p v-if="c.path && c.state === 'ready'" class="hint ext-path">{{ c.path }}</p>
            <details v-if="c.id === 'ffmpeg'" class="ext-adv">
              <summary class="hint">{{ $t('aiCenter.extAdvanced') }}</summary>
              <div class="field">
                <label>{{ $t('settings.ffmpegPath') }}</label>
                <input v-model="ffPath" :placeholder="$t('aiCenter.extFfmpegPh')" />
                <span class="hint">{{ $t('aiCenter.extFfmpegHint') }}</span>
              </div>
              <button class="btn tiny" :disabled="ffPath === (extFf.custom || '')" @click="saveFfPath">
                {{ $t('common.save') }}
              </button>
            </details>
            <div v-if="task.running && task.cid === c.id" class="ext-progress">
              <div class="ext-bar">
                <div class="ext-bar-in" :style="{ width: taskPercent + '%' }"
                     :class="{ indet: !task.total }"></div>
              </div>
              <span class="hint">
                {{ task.message }}<template v-if="task.total">
                  · {{ fmtSize(task.downloaded) }} / {{ fmtSize(task.total) }}</template>
              </span>
              <button class="btn tiny ghost" @click="cancelExt">{{ $t('aiCenter.extCancel') }}</button>
              <div v-if="task.log && task.log.length" class="ext-log">
                <div v-for="(l, i) in task.log" :key="i">{{ l }}</div>
              </div>
            </div>
            <div v-else class="aic-bar-ops ext-ops">
              <button v-if="c.state !== 'ready'" class="btn tiny" @click="installExt(c.id)">
                {{ $t('aiCenter.extInstall') }}
              </button>
              <button v-if="c.state !== 'ready'" class="btn tiny ghost" @click="startImport(c.id)">
                {{ $t('aiCenter.extImport') }}
              </button>
              <button v-if="c.state === 'ready' && c.source === 'ext'" class="btn tiny ghost"
                      @click="uninstallExt(c.id)">
                {{ $t('aiCenter.extUninstall') }}
              </button>
              <span v-if="c.state === 'ready' && c.source && c.source !== 'ext'" class="hint">
                {{ $t('aiCenter.extSource.' + c.source) }}
              </span>
            </div>
          </div>
          <div v-if="imp.cid" class="aic-card">
            <div class="aic-card-head">
              <strong>{{ $t('aiCenter.extImportTitle', { name: $t('aiCenter.extName.' + imp.cid) }) }}</strong>
            </div>
            <p class="hint">{{ $t('aiCenter.extImportHint') }}</p>
            <div class="aic-bar-ops">
              <input v-model="imp.dir" class="ext-import-input"
                     :placeholder="$t('aiCenter.extImportPh')" />
              <button class="btn tiny" :disabled="imp.busy || !imp.dir" @click="doImport">
                {{ $t('aiCenter.extImportGo') }}
              </button>
              <button class="btn tiny ghost" @click="imp.cid = ''">{{ $t('common.cancel') }}</button>
            </div>
          </div>

          <!-- 语音识别的使用配置：组件安装与参数设置合并成一页 -->
          <div class="aic-card">
            <div class="aic-card-head">
              <strong>{{ $t('aiCenter.extAsrTitle') }}</strong>
              <span class="hint">{{ $t('aiCenter.extAsrHint') }}</span>
            </div>
            <div class="field-row">
              <label>{{ $t('settings.asrSize') }}</label>
              <select v-model="speech.model" class="aic-sel-sm">
                <option value="small">small</option>
                <option value="medium">medium</option>
              </select>
              <label>{{ $t('settings.asrLang') }}</label>
              <select v-model="speech.language" class="aic-sel-sm">
                <option value="ja">日本語</option>
                <option value="zh">中文</option>
                <option value="en">English</option>
                <option value="auto">{{ $t('settings.asrAuto') }}</option>
              </select>
              <label>{{ $t('aiCenter.asrQuality') }}</label>
              <select v-model="speech.quality" class="aic-sel-sm">
                <option value="accurate">{{ $t('aiCenter.asrQualityAccurate') }}</option>
                <option value="fast">{{ $t('aiCenter.asrQualityFast') }}</option>
              </select>
              <span class="hint">{{ $t('aiCenter.asrQualityTip') }}</span>
            </div>
            <div class="field">
              <label>{{ $t('settings.asrTmp') }}</label>
              <input v-model="speech.tmp_dir" :placeholder="$t('settings.asrTmpPh')" />
            </div>
            <!-- 自定义路径平铺（原先藏在「高级」折叠里，用户找不到）：依赖 / 模型都可指定任意盘位置 -->
            <div class="field">
              <label>{{ $t('settings.asrLibs') }}</label>
              <input v-model="speech.libs_dir" :placeholder="$t('aiCenter.extAsrLibsPh')" />
            </div>
            <div class="field">
              <label>{{ $t('settings.asrModelDir') }}</label>
              <input v-model="speech.model_dir" :placeholder="$t('aiCenter.extAsrModelPh')" />
            </div>
            <button class="btn tiny" :disabled="speechSaving" @click="saveSpeech">
              {{ $t('common.save') }}
            </button>
          </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

import {
  extCancel, extImport, extInstall, extStatus, extTask, extUninstall, getConfig, putConfig,
} from '../api.js'
import { t } from '../i18n/index.js'
import { confirmDialog, toast } from '../utils.js'

const ext = reactive({ components: [], ext_dir: '', container: false, pip: false, loading: false })
const ffPath = ref('')
const extFf = computed(() => ext.components.find((c) => c.id === 'ffmpeg') || {})
const task = reactive({
  running: false, cid: '', state: '', percent: 0, message: '',
  downloaded: 0, total: 0, speed: 0, log: [],
})
const imp = reactive({ cid: '', dir: '', busy: false })
let extTimer = null

function fmtSize(n) {
  const v = Number(n) || 0
  if (v >= 1073741824) return (v / 1073741824).toFixed(1) + ' GB'
  if (v >= 1048576) return (v / 1048576).toFixed(0) + ' MB'
  if (v >= 1024) return (v / 1024).toFixed(0) + ' KB'
  return v + ' B'
}

function extStateText(c) {
  if (task.running && task.cid === c.id) return t('aiCenter.extState.installing')
  if (c.state === 'ready') return t('aiCenter.extState.ready')
  return t('aiCenter.extState.missing')
}

const taskPercent = computed(() => {
  if (!task.total) return task.percent || 0
  return Math.min(100, Math.round((task.downloaded * 100) / task.total))
})

function syncTask(ts) {
  const running = !!(ts && ts.running)
  if (running) {
    Object.assign(task, {
      running: true, cid: ts.cid, state: ts.state, percent: ts.percent || 0,
      message: ts.message || '', downloaded: ts.downloaded || 0, total: ts.total || 0,
      speed: ts.speed || 0, log: ts.log || [],
    })
  } else if (task.running) {
    task.running = false
    loadExt()
    if (ts && ts.state === 'done') toast(t('aiCenter.extDone', { msg: ts.message || '' }), 'ok', 5000)
    else if (ts && ts.state === 'failed') toast(t('aiCenter.extFailed', { msg: ts.message || '' }), 'err', 6000)
  }
  // 非运行状态（done/failed/cancelled 的历史残留）不弹打扰提示：组件状态以 status 为准
}

async function loadExt() {
  ext.loading = true
  try {
    const r = await extStatus()
    ext.components = r.components || []
    ext.ext_dir = r.ext_dir || ''
    ext.container = !!r.container
    ext.pip = !!r.pip_available
    const ff = ext.components.find((c) => c.id === 'ffmpeg')
    if (ff) ffPath.value = ff.custom || ''
    syncTask(r.task)
    if (r.task && r.task.running) startExtPoll()
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    ext.loading = false
  }
}

function startExtPoll() {
  if (extTimer) return
  extTimer = setInterval(async () => {
    try {
      const ts = await extTask()
      syncTask(ts)
      if (!(ts && ts.running)) stopExtPoll()
    } catch (e) { /* 轮询失败不打扰，下轮再试 */ }
  }, 1500)
}

function stopExtPoll() {
  if (extTimer) { clearInterval(extTimer); extTimer = null }
}

async function installExt(cid) {
  try {
    await extInstall(cid, 'auto')
    startExtPoll()
    await loadExt()
  } catch (e) {
    toast(e.message, 'err')
  }
}

async function cancelExt() {
  try { await extCancel() } catch (e) { toast(e.message, 'err') }
}

function startImport(cid) {
  imp.cid = cid
  imp.dir = ''
  imp.busy = false
}

async function doImport() {
  if (!imp.dir || imp.busy) return
  imp.busy = true
  try {
    await extImport(imp.cid, imp.dir)
    toast(t('aiCenter.extImported'), 'ok')
    imp.cid = ''
    await loadExt()
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    imp.busy = false
  }
}

async function uninstallExt(cid) {
  const ok = await confirmDialog(
    t('aiCenter.extUninstall'), t('aiCenter.extUninstallConfirm'), { danger: true })
  if (!ok) return
  try {
    await extUninstall(cid)
    toast(t('aiCenter.extRemoved'), 'ok')
    await loadExt()
  } catch (e) {
    toast(e.message, 'err')
  }
}

async function saveFfPath() {
  try {
    await putConfig({ ffmpeg_path: ffPath.value.trim() })
    toast(t('aiCenter.saved'), 'ok')
    await loadExt()
  } catch (e) {
    toast(e.message, 'err')
  }
}

/* ---------- 语音识别参数 ---------- */
const speech = reactive({
  libs_dir: '', model_dir: '', tmp_dir: '', model: 'small', language: 'ja',
  quality: 'accurate',
})
const speechSaving = ref(false)
let speechLoaded = false

async function loadSpeech() {
  if (speechLoaded) return
  try {
    const cfg = await getConfig()
    Object.assign(speech, cfg.asr || {})
    speechLoaded = true
  } catch (e) {
    toast(e.message, 'err')
  }
}

async function saveSpeech() {
  speechSaving.value = true
  try {
    await putConfig({
      asr: {
        libs_dir: (speech.libs_dir || '').trim(),
        model_dir: (speech.model_dir || '').trim(),
        tmp_dir: (speech.tmp_dir || '').trim(),
        model: speech.model || 'small',
        language: speech.language || 'ja',
        quality: speech.quality === 'fast' ? 'fast' : 'accurate',
      },
    })
    toast(t('aiCenter.speechSaved'), 'ok')
    loadExt()          // 模型档位变了 → 上面组件卡的「使用中」标记要跟着刷新
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    speechSaving.value = false
  }
}

onMounted(() => { loadExt(); loadSpeech() })
onBeforeUnmount(() => { stopExtPoll() })
</script>

<style scoped>
/* 卡片与文字基元：全局没有 .hint/.aic-card 这些定义，这里补齐（与 AI 中心同一套视觉） */
.hint { margin: 0; font-size: var(--fs-sm); line-height: 1.6; color: var(--c-text-3); }
.aic-bar {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap; margin-bottom: 12px;
}
.aic-bar .hint { margin: 0; }
.aic-bar-ops { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.aic-card {
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: 12px; padding: 14px 16px; margin-bottom: 12px;
}
.aic-card-head {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px;
}
.aic-card-head strong { color: var(--c-text); }
.aic-badge {
  display: inline-block; margin-left: 6px; padding: 1px 7px; border-radius: 999px;
  font-size: 11px; background: var(--c-primary-soft); color: var(--c-primary-h);
}
.aic-sel-sm { width: 130px; }
.ext-path { font-family: ui-monospace, Consolas, monospace; font-size: 11px; word-break: break-all; }
.ext-progress { margin-top: 8px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ext-bar { flex: 1 1 220px; height: 8px; border-radius: 999px; background: var(--c-bg-sunken); overflow: hidden; }
.ext-bar-in { height: 100%; border-radius: 999px; background: var(--c-primary); transition: width .4s; }
.ext-bar-in.indet { width: 40% !important; animation: extIndet 1.2s ease-in-out infinite; }
@keyframes extIndet {
  0% { margin-left: -40%; } 100% { margin-left: 100%; }
}
.ext-log { flex-basis: 100%; max-height: 110px; overflow: auto; padding: 6px 10px;
  border-radius: 8px; background: var(--c-bg-sunken);
  font-family: ui-monospace, Consolas, monospace; font-size: 11px; color: var(--c-text-3); }
.ext-import-input { flex: 1 1 260px; }
.ext-ops { margin-top: 6px; }
.ext-adv { margin: 8px 0; }
.ext-adv summary { cursor: pointer; user-select: none; }
.ext-adv .field { margin-top: 8px; }
/* 状态徽标（与 AI 中心同一套视觉） */
.aic-badge.ready { background: rgba(46, 160, 67, .18); color: #3fb950; }
.aic-badge.missing { background: var(--c-surface-2); color: var(--c-text-3); }
.aic-badge.installing { background: var(--c-primary-soft-strong); color: var(--c-primary-h); }
</style>
<!-- @@STYLE@@ -->
