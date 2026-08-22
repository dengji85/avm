<script setup>
import { ref, reactive, computed } from 'vue'
import { state } from '../state.js'
import { t, setLang, LANGUAGES, i18nState } from '../i18n/index.js'
import { fsList, putConfig, setupDone } from '../api.js'
import { toast } from '../utils.js'
import { useTasks } from '../composables/useTasks.js'

const tasks = useTasks()

const step = ref(1)                 // 1=语言  2=媒体库目录
const paths = reactive([])          // 已添加的目录
const newPath = ref('')
const saving = ref(false)
const starting = ref(false)

/* 语言选择：直接切换全局语言，无需等待保存 */
function chooseLang(code) {
  setLang(code)
}

/* 目录浏览器（复用后端 fs/list） */
const browser = reactive({ open: false, path: '', dirs: [], loading: false })
async function browse(p) {
  try {
    browser.loading = true
    const d = await fsList(p || '')
    browser.path = d.path || ''
    browser.dirs = d.dirs || []
    browser.open = true
  } catch (e) { toast(e.message, 'err') }
  finally { browser.loading = false }
}
function pickDir() {
  if (browser.path) newPath.value = browser.path
  browser.open = false
}
function addPath() {
  const v = newPath.value.trim()
  if (!v) return
  if (paths.includes(v)) { toast(t('settings.dirExists'), 'err'); return }
  paths.push(v)
  newPath.value = ''
}
function removePath(i) { paths.splice(i, 1) }

const canNext = computed(() => step.value === 1 ? true : paths.length > 0)

async function finish() {
  if (!paths.length) { toast(t('onboarding.addDirHint'), 'warn'); return }
  saving.value = true
  try {
    await putConfig({ library: { paths: paths.slice() } })
    await setupDone()
    state.config = state.config || {}
    state.config.library = Object.assign({}, state.config.library, { paths: paths.slice() })
    state.config.server = Object.assign({}, state.config.server, { setup_done: true })
    state.onboarded = true
    // 末步：触发首次扫描 + 刮削，让用户一键进入正轨
    starting.value = true
    await tasks.runScan({})
    state.onboarded = false   // 关闭引导遮罩，任务中心会自动弹出
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    saving.value = false
    starting.value = false
  }
}

function skip() {
  // 跳过引导：立即关闭遮罩（本地优先），再尝试把 setup_done 写回后端
  state.config = state.config || {}
  state.config.server = Object.assign({}, state.config.server, { setup_done: true })
  state.onboarded = false
  setupDone().catch(() => { /* 后端不可达时忽略，下次启动会重新引导 */ })
}
</script>

<template>
  <div class="ob-mask">
    <div class="ob-card">
      <!-- 进度指示 -->
      <div class="ob-steps">
        <span :class="['ob-step', step >= 1 ? 'active' : '']">1 · {{ t('onboarding.lang') }}</span>
        <span class="ob-sep">›</span>
        <span :class="['ob-step', step >= 2 ? 'active' : '']">2 · {{ t('onboarding.dir') }}</span>
      </div>

      <h2 class="ob-title">{{ t('onboarding.title') }}</h2>
      <p class="ob-sub">{{ t('onboarding.subtitle') }}</p>

      <!-- 步骤1：语言 -->
      <div v-if="step === 1" class="ob-body">
        <div class="ob-lang-grid">
          <button
            v-for="l in LANGUAGES"
            :key="l.code"
            :class="['ob-lang', i18nState.lang === l.code ? 'sel' : '']"
            @click="chooseLang(l.code)"
          >{{ l.label }}</button>
        </div>
      </div>

      <!-- 步骤2：媒体库目录 -->
      <div v-else class="ob-body">
        <p class="ob-tip">{{ t('onboarding.dirTip') }}</p>
        <ul v-if="paths.length" class="ob-path-list">
          <li v-for="(p, i) in paths" :key="i">
            <span class="pi">📁</span>
            <span class="pp ellipsis" :title="p">{{ p }}</span>
            <button class="btn tiny ghost" @click="removePath(i)">{{ t('settings.remove') }}</button>
          </li>
        </ul>
        <p v-else class="muted">{{ t('onboarding.noDirYet') }}</p>

        <div class="hstack">
          <input v-model="newPath" :placeholder="t('settings.scanDirPh')" @keydown.enter="addPath" />
          <button class="btn" @click="browse('')" :disabled="browser.loading">{{ t('settings.browseDir') }}</button>
          <button class="btn primary" @click="addPath">{{ t('common.add') }}</button>
        </div>
      </div>

      <!-- 底部操作 -->
      <div class="ob-foot">
        <button v-if="step > 1" class="btn ghost" @click="step--">{{ t('common.back') }}</button>
        <span class="grow"></span>
        <button class="btn ghost" @click="skip">{{ t('onboarding.skip') }}</button>
        <button v-if="step < 2" class="btn primary" :disabled="!canNext" @click="step++">{{ t('common.next') }}</button>
        <button v-else class="btn primary" :disabled="!canNext || saving || starting" @click="finish">
          {{ starting ? t('onboarding.scanning') : t('onboarding.startScan') }}
        </button>
      </div>
    </div>

    <!-- 目录浏览器弹层 -->
    <div v-if="browser.open" class="ob-browse-mask" @click.self="browser.open = false">
      <div class="ob-browse">
        <div class="ob-browse-head">
          <span class="ellipsis">{{ browser.path || t('onboarding.rootDir') }}</span>
          <button class="btn tiny ghost" @click="browse(browser.path ? browser.path.replace(/[\\\/][^\\\/]+$/, '') : '')">{{ t('common.up') }}</button>
          <button class="btn tiny ghost" @click="browser.open = false">✕</button>
        </div>
        <div class="ob-browse-body">
          <div v-if="!browser.dirs.length" class="muted pad">{{ t('onboarding.noSubDir') }}</div>
          <div
            v-for="d in browser.dirs"
            :key="d.path"
            class="ob-dir"
            @click="browse(d.path)"
            @dblclick="() => { newPath = d.path; browser.open = false }"
          >
            <span class="pi">📁</span><span class="ellipsis">{{ d.name }}</span>
          </div>
        </div>
        <div class="ob-browse-foot">
          <button class="btn" @click="pickDir" :disabled="!browser.path">{{ t('common.select') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ob-mask {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(0,0,0,.72);
  display: flex; align-items: center; justify-content: center;
  padding: 16px;
}
.ob-card {
  width: min(560px, 100%);
  max-height: 90vh; overflow: auto;
  background: var(--bg-panel, #1b1d22);
  border: 1px solid var(--border, #2c2f36);
  border-radius: 14px;
  padding: 22px 24px 18px;
  box-shadow: 0 20px 60px rgba(0,0,0,.5);
}
.ob-steps { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--muted, #9aa0a6); margin-bottom: 14px; }
.ob-step.active { color: var(--accent, #5b9cff); font-weight: 600; }
.ob-sep { opacity: .5; }
.ob-title { margin: 0 0 4px; font-size: 20px; color: var(--text, #e8eaed); }
.ob-sub { margin: 0 0 18px; font-size: 13px; color: var(--muted, #9aa0a6); }
.ob-body { min-height: 180px; }
.ob-lang-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.ob-lang {
  padding: 14px; border-radius: 10px; cursor: pointer;
  background: var(--bg-input, #14161a); color: var(--text, #e8eaed);
  border: 1px solid var(--border, #2c2f36); font-size: 15px; text-align: center;
}
.ob-lang.sel { border-color: var(--accent, #5b9cff); box-shadow: 0 0 0 2px rgba(91,156,255,.25); color: var(--accent, #5b9cff); }
.ob-tip { font-size: 13px; color: var(--muted, #9aa0a6); margin: 0 0 10px; }
.ob-path-list { list-style: none; margin: 0 0 10px; padding: 0; }
.ob-path-list li { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px dashed var(--border, #2c2f36); }
.ob-path-list .pp { flex: 1; }
.ob-foot { display: flex; align-items: center; gap: 10px; margin-top: 18px; }
.ob-foot .grow { flex: 1; }

.hstack { display: flex; gap: 8px; }
.hstack input {
  flex: 1; padding: 9px 10px; border-radius: 8px;
  background: var(--bg-input, #14161a); color: var(--text, #e8eaed);
  border: 1px solid var(--border, #2c2f36);
}
.btn {
  padding: 9px 14px; border-radius: 8px; cursor: pointer;
  background: var(--bg-input, #14161a); color: var(--text, #e8eaed);
  border: 1px solid var(--border, #2c2f36); white-space: nowrap;
}
.btn.primary { background: var(--accent, #5b9cff); color: #fff; border-color: transparent; }
.btn.ghost { background: transparent; }
.btn.tiny { padding: 4px 8px; font-size: 12px; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.muted { color: var(--muted, #9aa0a6); }
.pad { padding: 16px; }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.ob-browse-mask {
  position: fixed; inset: 0; z-index: 1100;
  background: rgba(0,0,0,.6); display: flex; align-items: center; justify-content: center; padding: 16px;
}
.ob-browse { width: min(480px, 100%); background: var(--bg-panel, #1b1d22); border: 1px solid var(--border, #2c2f36); border-radius: 12px; overflow: hidden; }
.ob-browse-head { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--border, #2c2f36); }
.ob-browse-head .ellipsis { flex: 1; color: var(--text, #e8eaed); }
.ob-browse-body { max-height: 320px; overflow: auto; padding: 6px 0; }
.ob-dir { display: flex; align-items: center; gap: 8px; padding: 9px 14px; cursor: pointer; color: var(--text, #e8eaed); }
.ob-dir:hover { background: var(--bg-input, #14161a); }
.ob-browse-foot { display: flex; justify-content: flex-end; gap: 8px; padding: 10px 12px; border-top: 1px solid var(--border, #2c2f36); }
</style>
