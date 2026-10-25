<template>
  <div class="aic">
    <header class="aic-head">
      <div>
        <h2>{{ $t('aiCenter.title') }}</h2>
        <p class="hint">{{ $t('aiCenter.subtitle') }}</p>
      </div>
      <button class="btn tiny ghost" @click="goBack">{{ $t('aiCenter.back') }}</button>
    </header>

    <div class="aic-body">
      <nav class="aic-nav">
        <button v-for="s in SECTIONS" :key="s.id" class="aic-nav-item"
                :class="{ on: section === s.id }" @click="section = s.id">
          <span class="aic-nav-ico" aria-hidden="true">{{ s.icon }}</span>
          <span class="ellipsis">{{ $t(s.label) }}</span>
        </button>
      </nav>

      <section class="aic-main">
        <template v-if="section === 'models'">
          <div class="aic-bar">
            <span class="hint">{{ $t('aiCenter.modelsHint') }}</span>
            <button class="btn tiny" @click="openForm()">{{ $t('aiCenter.addModel') }}</button>
          </div>

          <table v-if="models.length" class="aic-tbl">
            <thead>
              <tr>
                <th>{{ $t('aiCenter.name') }}</th>
                <th>Base URL</th>
                <th>{{ $t('aiCenter.model') }}</th>
                <th>{{ $t('aiCenter.kind') }}</th>
                <th>{{ $t('aiCenter.status') }}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="m in models" :key="m.name">
                <td>
                  {{ m.name }}
                  <span v-if="m.is_default" class="aic-badge">{{ $t('aiCenter.default') }}</span>
                </td>
                <td class="mono">{{ m.base_url }}</td>
                <td class="mono">{{ m.model }}</td>
                <td>{{ m.kind === 'local' ? $t('aiCenter.local') : $t('aiCenter.cloud') }}</td>
                <td class="sm">
                  {{ m.enabled ? $t('aiCenter.on') : $t('aiCenter.off') }}
                  · {{ m.key_hint || $t('aiCenter.noKey') }}
                </td>
                <td class="aic-ops">
                  <button class="btn tiny ghost" :disabled="testing === m.name" @click="test(m.name)">
                    {{ testing === m.name ? '…' : $t('aiCenter.test') }}
                  </button>
                  <button v-if="!m.is_default" class="btn tiny ghost" @click="setDefault(m.name)">
                    {{ $t('aiCenter.setDefault') }}
                  </button>
                  <button class="btn tiny ghost" @click="openForm(m)">{{ $t('common.edit') }}</button>
                  <button class="btn tiny ghost" @click="remove(m.name)">{{ $t('common.delete') }}</button>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-else class="aic-empty">{{ $t('aiCenter.empty') }}</p>
          <p v-if="testResult" class="hint">{{ testResult }}</p>
          <p class="hint">{{ $t('aiCenter.modelsFootnote') }}</p>

          <!-- 问库隐私：聚合摘要是否允许发给云端模型润色（本地模型自动全量） -->
          <div class="aic-card">
            <div class="aic-card-head">
              <strong>{{ $t('aiCenter.privTitle') }}</strong>
            </div>
            <label class="priv-chk">
              <input type="checkbox" v-model="priv.insights" @change="savePriv" />
              <span>{{ $t('aiCenter.privInsights') }}</span>
            </label>
            <label class="priv-chk" :class="{ dim: !priv.insights }">
              <input type="checkbox" v-model="priv.insights_actress" :disabled="!priv.insights" @change="savePriv" />
              <span>{{ $t('aiCenter.privActress') }}</span>
            </label>
            <p class="hint">{{ privNote }}</p>
          </div>
        </template>

        <template v-else-if="section === 'prompts'">
          <div class="aic-bar">
            <span class="hint">{{ $t('aiCenter.promptsHint') }}</span>
            <button class="btn tiny ghost" :disabled="prompts.loading" @click="loadPrompts">
              {{ $t('common.refresh') }}
            </button>
          </div>
          <div v-for="p in prompts.items" :key="p.id" class="aic-card">
            <div class="aic-card-head">
              <strong>{{ p.title || p.id }}</strong>
              <span class="aic-tag">{{ p.id }}</span>
              <span v-if="p.custom" class="aic-badge">{{ $t('aiCenter.customized') }}</span>
              <span class="spacer"></span>
              <button class="btn tiny ghost" @click="editPrompt(p)">{{ $t('aiCenter.edit') }}</button>
              <button v-if="p.custom" class="btn tiny ghost" @click="resetPrompt(p.id)">
                {{ $t('aiCenter.resetPrompt') }}
              </button>
            </div>
            <p class="hint aic-clamp">{{ p.system }}</p>
          </div>
          <p v-if="!prompts.items.length" class="aic-empty">{{ $t('aiCenter.empty') }}</p>
        </template>

        <template v-else-if="section === 'jobs'">
          <div class="aic-card">
            <div class="aic-bar">
              <div class="field-row">
                <label>{{ $t('settings.aiJobsKind') }}</label>
                <select v-model="job.kind" class="aic-sel">
                  <option value="translate">{{ $t('settings.aiJobsTranslate') }}</option>
                  <option value="plot">{{ $t('settings.aiJobsPlot') }}</option>
                </select>
                <label>{{ $t('settings.aiJobsCount') }}</label>
                <input v-model.number="job.count" type="number" min="1" max="200" class="aic-num" />
              </div>
              <div>
                <button class="btn primary" :disabled="job.busy || jobs.running" @click="startJob">
                  {{ $t('settings.aiJobsStart') }}
                </button>
                <button class="btn ghost" :disabled="!jobs.running" @click="cancelJob">
                  {{ $t('settings.aiJobsCancel') }}
                </button>
              </div>
            </div>
            <p class="hint">{{ $t('settings.aiJobsHint', { n: job.count, budget: jobs.budget_chars_per_run || '∞' }) }}</p>
            <div v-if="jobs.items.length" class="aic-list">
              <div v-for="j in jobs.items" :key="j.id" class="aic-row">
                <span class="aic-tag">
                  {{ j.kind === 'translate' ? $t('settings.aiJobsTranslate') : $t('settings.aiJobsPlot') }}
                </span>
                <span class="hint">{{ j.done }}/{{ j.total }} · {{ $t('aiCenter.failedShort', { n: j.fail }) }}</span>
                <span class="hint">{{ j.running ? $t('aiCenter.running') : (j.error || $t('aiCenter.finished')) }}</span>
              </div>
            </div>
          </div>
        </template>

        <template v-else-if="section === 'traces'">
          <div class="aic-bar">
            <span class="hint">{{ $t('aiCenter.tracesHint') }}</span>
            <div class="aic-bar-ops">
              <select v-model="tracePurpose" class="aic-sel" @change="loadTraces">
                <option value="">{{ $t('aiCenter.traceAll') }}</option>
                <option v-for="p in TRACE_GROUPS" :key="p.value" :value="p.value">
                  {{ $t(p.label) }}
                </option>
              </select>
              <button class="btn tiny ghost" @click="loadTraces">{{ $t('common.refresh') }}</button>
              <button class="btn tiny ghost" :disabled="traces.loading" @click="clearTraces">
                {{ $t('aiCenter.traceClear') }}
              </button>
            </div>
          </div>
          <div v-for="tr in traces.items" :key="tr.id" class="aic-card">
            <div class="aic-card-head">
              <span class="aic-tag">{{ tr.purpose }}</span>
              <span class="hint">{{ fmtDateTime(tr.at) }}</span>
              <span class="hint">{{ tr.ms }} ms</span>
              <span v-if="tr.cached" class="aic-badge">{{ $t('ai.traceCached') }}</span>
              <span class="aic-badge" :class="tr.origin || 'internal'">{{ $t('ai.traceOrigin.' + (tr.origin || 'internal')) }}</span>
              <span class="spacer"></span>
              <span class="hint">{{ (tr.reply || '').length }} 字</span>
            </div>
            <p v-if="tr.client_ip" class="hint">{{ tr.client_ip }}</p>
            <p v-if="tr.error" class="aiw-err">{{ tr.error }}</p>
            <details v-else class="aic-detail">
              <summary>{{ $t('aiCenter.showRaw') }}</summary>
              <div class="hint">{{ $t('ai.traceSystem') }}</div>
              <pre class="aic-pre">{{ tr.messages && tr.messages[0] ? tr.messages[0].content : '' }}</pre>
              <div class="hint">{{ $t('ai.traceUser') }}</div>
              <pre class="aic-pre">{{ tr.messages && tr.messages[1] ? tr.messages[1].content : '' }}</pre>
              <div class="hint">{{ $t('ai.traceReply') }}</div>
              <pre class="aic-pre">{{ tr.reply }}</pre>
            </details>
          </div>
          <p v-if="!traces.items.length" class="aic-empty">{{ $t('aiCenter.traceEmpty') }}</p>
        </template>

        <template v-else-if="section === 'glossary'">
          <div class="aic-card">
            <p class="hint">{{ $t('settings.aiGlossaryHint') }}</p>
            <textarea v-model="glossary.text" class="aic-textarea" rows="10"
                      spellcheck="false" :placeholder="$t('settings.aiGlossaryPh')"></textarea>
            <div class="aic-actions">
              <span v-if="glossary.ok" class="hint">{{ $t('settings.aiGlossarySaved') }}</span>
              <button class="btn" :disabled="glossary.busy" @click="saveGlossary">
                {{ $t('settings.aiGlossarySave') }}
              </button>
            </div>
          </div>
        </template>
      </section>
    </div>

    <div v-if="form.open" class="aic-mask" @click.self="form.open = false">
      <div class="aic-dialog">
        <h3>{{ form.isEdit ? $t('aiCenter.editModel') : $t('aiCenter.addModel') }}</h3>
        <div class="field">
          <label>{{ $t('aiCenter.name') }}</label>
          <input v-model="form.name" :disabled="form.isEdit" :placeholder="$t('aiCenter.namePh')" />
        </div>
        <div class="field">
          <label>Base URL</label>
          <input v-model="form.base_url" placeholder="https://api.deepseek.com 或 http://127.0.0.1:11434/v1" />
        </div>
        <div class="field">
          <label>API Key</label>
          <input v-model="form.api_key" type="password" autocomplete="new-password"
                 :placeholder="form.isEdit ? $t('aiCenter.keyKeep') : ''" />
        </div>
        <div class="field">
          <label>{{ $t('aiCenter.model') }}</label>
          <input v-model="form.model" placeholder="deepseek-flash / qwen2.5:7b-instruct" />
        </div>
        <div class="aic-grid">
          <div class="field">
            <label>{{ $t('aiCenter.kind') }}</label>
            <select v-model="form.kind">
              <option value="cloud">{{ $t('aiCenter.cloud') }}</option>
              <option value="local">{{ $t('aiCenter.local') }}</option>
            </select>
          </div>
          <div class="field">
            <label>{{ $t('aiCenter.timeout') }}</label>
            <input v-model.number="form.timeout" type="number" min="5" max="600" />
          </div>
          <div class="field">
            <label>{{ $t('aiCenter.temperature') }}</label>
            <input v-model.number="form.temperature" type="number" step="0.1" min="0" max="2" />
          </div>
        </div>
        <label class="aic-chk"><input type="checkbox" v-model="form.enabled" /> {{ $t('aiCenter.enabled') }}</label>
        <label class="aic-chk"><input type="checkbox" v-model="form.make_default" /> {{ $t('aiCenter.makeDefault') }}</label>
        <div class="aic-actions">
          <button class="btn ghost" @click="form.open = false">{{ $t('common.cancel') }}</button>
          <button class="btn" :disabled="saving" @click="save">{{ $t('common.save') }}</button>
        </div>
      </div>
    </div>

    <div v-if="promptEdit.open" class="aic-mask" @click.self="promptEdit.open = false">
      <div class="aic-dialog">
        <h3>{{ promptEdit.title }} <span class="aic-tag">{{ promptEdit.id }}</span></h3>
        <textarea v-model="promptEdit.text" class="aic-textarea" rows="14"></textarea>
        <div class="aic-actions">
          <button class="btn ghost" @click="promptEdit.open = false">{{ $t('common.cancel') }}</button>
          <button class="btn" :disabled="promptEdit.saving" @click="savePromptEdit">
            {{ $t('common.save') }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'

import {
  aiGlossary, aiGlossarySave, aiJobs, aiJobsCancel, aiJobsCreate, aiModelDelete, aiModels,
  aiModelSave, aiModelSetDefault, aiModelTest, aiPrompts, aiResetPrompt, aiSavePrompt,
  aiTraces, aiTracesClear, getConfig, putConfig,
} from '../api.js'
import { t } from '../i18n/index.js'
import { state } from '../state.js'
import { confirmDialog, fmtDateTime, toast } from '../utils.js'

const SECTIONS = [
  { id: 'models', label: 'aiCenter.nav.models', icon: '🧠' },
  { id: 'prompts', label: 'aiCenter.nav.prompts', icon: '📝' },
  { id: 'jobs', label: 'aiCenter.nav.jobs', icon: '⚙️' },
  { id: 'traces', label: 'aiCenter.nav.traces', icon: '🔍' },
  { id: 'glossary', label: 'aiCenter.nav.glossary', icon: '📖' },
]

// 调用记录按用途分组：后端的 purpose 是「前缀 + 变体」（plot.subtitle.chunk 等），
// 这里用分组名筛选，后端按「精确或同前缀」匹配，筛选项与真实 purpose 对得上。
const TRACE_GROUPS = [
  { value: 'synopsis', label: 'aiCenter.tracePurpose.synopsis' },
  { value: 'plot', label: 'aiCenter.tracePurpose.plot' },
  { value: 'tags', label: 'aiCenter.tracePurpose.tags' },
  { value: 'subtitle', label: 'aiCenter.tracePurpose.subtitle' },
  { value: 'search', label: 'aiCenter.tracePurpose.search' },
  { value: 'models', label: 'aiCenter.tracePurpose.models' },
]

const section = ref('models')
const models = ref([])
const testing = ref('')
const testResult = ref('')
const saving = ref(false)

const form = reactive({
  open: false, isEdit: false, name: '', base_url: '', api_key: '', model: '',
  kind: 'cloud', timeout: 60, temperature: 0.4, enabled: true, make_default: false,
})

const priv = reactive({ insights: false, insights_actress: false })
const localModel = computed(() => (models.value.find((m) => m.is_default) || {}).kind === 'local')
const privNote = computed(() => (localModel.value
  ? t('aiCenter.privLocal')
  : t('aiCenter.privNote')))

async function loadPriv() {
  try {
    const c = await getConfig()
    priv.insights = !!((c.ai || {}).insights)
    priv.insights_actress = !!((c.ai || {}).insights_actress)
  } catch (e) { /* 读取失败保持默认关闭 */ }
}

async function savePriv() {
  try {
    await putConfig({ ai: { insights: priv.insights, insights_actress: priv.insights_actress } })
    toast(t('aiCenter.saved'), 'ok')
  } catch (e) {
    toast(e.message, 'err')
  }
}

async function load() {
  try {
    const r = await aiModels()
    models.value = r.models || []
  } catch (e) {
    toast(e.message, 'err')
  }
  loadPriv()
}

// 生命周期与分区懒加载见文件末尾

function openForm(m) {
  testResult.value = ''
  Object.assign(form, {
    open: true,
    isEdit: !!m,
    name: m ? m.name : '',
    base_url: m ? m.base_url : '',
    api_key: '',
    model: m ? m.model : '',
    kind: m ? m.kind : 'cloud',
    timeout: m ? m.timeout : 60,
    temperature: m ? m.temperature : 0.4,
    enabled: m ? !!m.enabled : true,
    make_default: false,
  })
}

async function save() {
  if (!form.name.trim()) {
    toast(t('aiCenter.nameRequired'), 'err')
    return
  }
  saving.value = true
  try {
    const body = {
      name: form.name.trim(),
      base_url: form.base_url.trim(),
      model: form.model.trim(),
      kind: form.kind,
      timeout: Number(form.timeout) || 60,
      temperature: Number(form.temperature),
      enabled: !!form.enabled,
      make_default: !!form.make_default,
    }
    if (form.api_key) body.api_key = form.api_key
    const r = await aiModelSave(body)
    models.value = r.models || []
    form.open = false
    toast(t('aiCenter.saved'), 'ok')
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    saving.value = false
  }
}

async function test(name) {
  testing.value = name
  testResult.value = ''
  try {
    const r = await aiModelTest(name)
    testResult.value = t('aiCenter.testOk', { name: r.name, model: r.model, ms: r.ms, reply: r.reply || '—' })
  } catch (e) {
    testResult.value = t('aiCenter.testFail', { msg: e.message })
  } finally {
    testing.value = ''
  }
}

async function setDefault(name) {
  try {
    const r = await aiModelSetDefault(name)
    models.value = r.models || []
    toast(t('aiCenter.defaultSet', { name }), 'ok')
  } catch (e) {
    toast(e.message, 'err')
  }
}

async function remove(name) {
  const ok = await confirmDialog(t('aiCenter.deleteTitle'), t('aiCenter.deleteDesc', { name }))
  if (!ok) return
  try {
    const r = await aiModelDelete(name)
    models.value = r.models || []
    toast(t('aiCenter.deleted'), 'ok')
  } catch (e) {
    toast(e.message, 'err')
  }
}

/* ---------- 提示词 ---------- */
const prompts = reactive({ items: [], loading: false })
const promptEdit = reactive({ open: false, id: '', title: '', text: '', saving: false })

async function loadPrompts() {
  prompts.loading = true
  try {
    prompts.items = (await aiPrompts()).items || []
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    prompts.loading = false
  }
}

function editPrompt(p) {
  Object.assign(promptEdit, {
    open: true, id: p.id, title: p.title || p.id, text: p.system || '', saving: false,
  })
}

async function savePromptEdit() {
  promptEdit.saving = true
  try {
    await aiSavePrompt(promptEdit.id, promptEdit.text)
    toast(t('aiCenter.promptSaved'), 'ok')
    promptEdit.open = false
    await loadPrompts()
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    promptEdit.saving = false
  }
}

async function resetPrompt(id) {
  const ok = await confirmDialog(t('aiCenter.resetPromptTitle'), t('aiCenter.resetPromptDesc'))
  if (!ok) return
  try {
    await aiResetPrompt(id)
    toast(t('aiCenter.promptReset'), 'ok')
    await loadPrompts()
  } catch (e) {
    toast(e.message, 'err')
  }
}

/* ---------- 批量任务 ---------- */
const job = reactive({ kind: 'translate', count: 20, busy: false })
const jobs = reactive({ running: false, total: 0, done: 0, fail: 0, items: [], budget_chars_per_run: 0 })
let jobTimer = null

async function refreshJobs() {
  try {
    Object.assign(jobs, { items: [] }, await aiJobs())
  } catch (e) {
    // 轮询失败不打扰用户
  }
}

async function startJob() {
  job.busy = true
  try {
    const r = await aiJobsCreate({
      kind: job.kind,
      latest: Math.max(1, Math.min(200, Number(job.count) || 1)),
    })
    toast(t('settings.aiJobsStarted', { n: r.count }), 'ok')
    await refreshJobs()
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    job.busy = false
  }
}

async function cancelJob() {
  try {
    await aiJobsCancel()
    await refreshJobs()
  } catch (e) {
    toast(e.message, 'err')
  }
}

/* ---------- 调用记录 ---------- */
const traces = reactive({ items: [], loading: false })
const tracePurpose = ref('')

async function loadTraces() {
  traces.loading = true
  try {
    const r = await aiTraces(50, tracePurpose.value || '')
    traces.items = r.items || []
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    traces.loading = false
  }
}

async function clearTraces() {
  const ok = await confirmDialog(t('aiCenter.traceClear'), t('aiCenter.traceClearConfirm'), { danger: true })
  if (!ok) return
  try {
    const r = await aiTracesClear()
    traces.items = []
    toast(t('aiCenter.traceCleared', { n: r.cleared || 0 }), 'ok')
  } catch (e) {
    toast(e.message, 'err')
  }
}

/* ---------- 翻译术语表 ---------- */
const glossary = reactive({ text: '', busy: false, ok: false })

async function loadGlossary() {
  try {
    const g = await aiGlossary()
    glossary.text = (g.items || []).map((i) => `${i.term} = ${i.translation}`).join('\n')
  } catch (e) {
    // 忽略：术语表为空也能用
  }
}

async function saveGlossary() {
  glossary.busy = true
  glossary.ok = false
  try {
    const items = glossary.text.split('\n').map((line) => {
      const i = line.indexOf('=')
      if (i <= 0) return null
      return { term: line.slice(0, i).trim(), translation: line.slice(i + 1).trim() }
    }).filter((x) => x && x.term && x.translation)
    await aiGlossarySave(items)
    glossary.ok = true
    toast(t('settings.aiGlossarySaved'), 'ok')
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    glossary.busy = false
  }
}

watch(section, (v) => {
  if (v === 'prompts' && !prompts.items.length) loadPrompts()
  if (v === 'jobs') refreshJobs()
  if (v === 'traces' && !traces.items.length) loadTraces()
  if (v === 'glossary' && !glossary.text) loadGlossary()
})

onMounted(() => {
  load()
  jobTimer = setInterval(() => {
    if (jobs.running) refreshJobs()
  }, 3000)
})

onBeforeUnmount(() => {
  if (jobTimer) clearInterval(jobTimer)
})

function goBack() {
  // 设置页已经没有 AI tab，直接回设置首页
  state.view = 'settings'
  state.settingsTab = ''
}
</script>

<style scoped>
/* ============================================================
   AI 中心：定宽两栏（侧栏 208 / 内容最多 880），不随内容伸缩。
   配色一律走 --c-* 主题变量，暗色与浅色各自成套。
   ============================================================ */
/* 根元素自带滚动：外层 .app-main 不滚（overflow:hidden），必须在这里开滚动容器，
   否则内容超出一屏时底部被裁掉且没有滚动条 */
.aic {
  flex: 1; min-width: 0; min-height: 0;
  overflow-y: auto; overflow-x: hidden;
  padding: 20px 24px 64px;
  max-width: 1160px; margin: 0 auto;
}
.aic-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 12px; margin-bottom: 16px;
}
.aic-head h2 { margin: 0 0 4px; font-size: 20px; color: var(--c-text); }
.aic-head .hint { margin: 0; color: var(--c-text-3); }

/* 两列都定宽：侧栏固定 208，内容列最多 880（空间不够时收缩，但不拉伸） */
.aic-body {
  display: grid;
  grid-template-columns: 208px minmax(0, 880px);
  gap: 20px;
  align-items: start;
}

/* ---------- 左侧菜单 ---------- */
.aic-nav {
  position: sticky; top: 16px;
  display: flex; flex-direction: column; gap: 2px;
  padding: 6px; border-radius: 12px;
  background: var(--c-surface); border: 1px solid var(--c-line);
}
.aic-nav-item {
  display: flex; align-items: center; gap: 9px;
  padding: 9px 11px; border-radius: 8px;
  border: 1px solid transparent; background: transparent;
  color: var(--c-text-2); font-size: 13px; text-align: left; cursor: pointer;
  transition: background var(--t-fast), color var(--t-fast), border-color var(--t-fast);
}
/* 图标列定宽，文字省略时不会挤动图标 */
.aic-nav-ico { flex: none; width: 18px; text-align: center; font-size: 14px; line-height: 1; }
.aic-nav-item:hover { background: var(--c-surface-2); color: var(--c-text); }
/* 选中态与全局按钮同一档：玫红加深底 + 实色描边，暗色下也看得清 */
.aic-nav-item.on {
  background: var(--c-primary-soft-strong);
  border-color: var(--c-primary);
  color: var(--c-primary-h); font-weight: 600;
}
.aic-main { min-width: 0; }

/* ---------- 内容区通用 ---------- */
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
.aic-list { margin-top: 8px; }
.aic-row {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 0; border-top: 1px solid var(--c-line);
}
.aic-row:first-child { border-top: 0; }
.aic-ops { white-space: nowrap; }
.aic-ops .btn { margin-left: 4px; }

/* 表格：外套一层卡片感的边框，表头用沉底 */
.aic-tbl {
  width: 100%; border-collapse: collapse; font-size: 13px;
  border: 1px solid var(--c-line); border-radius: 12px; overflow: hidden;
}
.aic-tbl th, .aic-tbl td {
  padding: 9px 12px; border-bottom: 1px solid var(--c-line);
  text-align: left; vertical-align: middle;
}
.aic-tbl thead th { background: var(--c-bg-sunken); color: var(--c-text-3); font-weight: 500; font-size: 12px; }
.aic-tbl tbody tr:last-child td { border-bottom: 0; }
.aic-tbl tbody tr:hover { background: var(--c-surface-2); }
.aic-tbl .mono {
  font-family: ui-monospace, Consolas, monospace; font-size: 12px;
  word-break: break-all; color: var(--c-text-2);
}

/* 徽章 / 标签 */
.aic-badge {
  display: inline-block; margin-left: 6px; padding: 1px 7px; border-radius: 999px;
  font-size: 11px; background: var(--c-primary-soft); color: var(--c-primary-h);
}
/* 调用来源徽标：页面内 = 正常；外部调用 = 脚本直连；后台任务 = 内部队列 */
.aic-badge.ui { background: var(--c-primary-soft); color: var(--c-primary-h); }
.aic-badge.external { background: var(--c-err-soft, rgba(224, 53, 90, .16)); color: var(--c-err); }
.aic-badge.internal { background: var(--c-surface-2); color: var(--c-text-3); }
.aic-badge.ready { background: rgba(46, 160, 67, .18); color: #3fb950; }
.aic-badge.missing { background: var(--c-surface-2); color: var(--c-text-3); }
.aic-badge.installing { background: var(--c-primary-soft-strong); color: var(--c-primary-h); }
.aic-tag {
  font-family: ui-monospace, Consolas, monospace; font-size: 11px;
  padding: 1px 6px; border-radius: 5px;
  background: var(--c-surface-2); border: 1px solid var(--c-line); color: var(--c-text-3);
}
/* 问库隐私开关 */
.priv-chk { display: flex; align-items: center; gap: 8px; margin: 6px 0; cursor: pointer; }
.priv-chk input { width: auto; }
.priv-chk.dim { opacity: .45; }
.aic-clamp {
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  color: var(--c-text-2); font-size: 12.5px; line-height: 1.6;
}

/* 调用详情 / 代码块 */
.aic-detail summary { cursor: pointer; font-size: 12px; color: var(--c-text-3); }
.aic-pre {
  margin: 6px 0; padding: 10px 12px; border-radius: 8px;
  background: var(--c-bg-sunken); border: 1px solid var(--c-line);
  color: var(--c-text-2); font-size: 12px; line-height: 1.6;
  white-space: pre-wrap; word-break: break-word; max-height: 220px; overflow: auto;
}
.aic-textarea {
  width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px;
  border: 1px solid var(--c-line-strong); background: var(--c-surface-2);
  color: var(--c-text); font-family: inherit; font-size: 13px; line-height: 1.6; resize: vertical;
}
.aic-textarea:focus { outline: none; border-color: var(--c-primary); }

.aic-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.aic-chk { display: flex; align-items: center; gap: 8px; margin: 8px 0; font-size: 13px; color: var(--c-text-2); }
.aic-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 16px; }
.aic-actions .hint { margin-right: auto; }

/* 模型编辑弹窗 */
.aic-mask {
  position: fixed; inset: 0; z-index: 60;
  background: var(--c-overlay); backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center; padding: 18px;
}
.aic-dialog {
  width: min(560px, 94vw); max-height: 88vh; overflow: auto;
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: 14px; padding: 18px 20px; box-shadow: var(--sh-4);
}
.aic-dialog h3 { margin: 0 0 14px; font-size: 16px; color: var(--c-text); }

.spacer { flex: 1; }
/* 页面里大量使用的说明文字：全局没有 .hint/.sm 定义，这里补上，保证两个主题一致 */
.hint { margin: 0; font-size: var(--fs-sm); line-height: 1.6; color: var(--c-text-3); }
.sm { font-size: var(--fs-sm); color: var(--c-text-2); }
/* 空态：比一行灰字明确，也顺带占住高度，避免切换栏目时页面跳动 */
.aic-empty {
  padding: 28px 16px; text-align: center; font-size: var(--fs-sm); color: var(--c-text-3);
  border: 1px dashed var(--c-line-strong); border-radius: 12px; background: var(--c-surface);
}
/* 提示条：用于「入口在详情页」这类需要先读懂的说明 */
.aic-note {
  display: flex; gap: 8px; margin: 0 0 12px; padding: 10px 12px;
  border-radius: 10px; border: 1px solid var(--c-line);
  border-left: 3px solid var(--c-accent);
  background: var(--c-bg-sunken);
  font-size: var(--fs-sm); line-height: 1.65; color: var(--c-text-2);
}
/* 表单控件定宽：原来写在内联 style 里，收成类便于统一调整 */
.aic-sel { width: 150px; }
.aic-sel-sm { width: 130px; }
.aic-num { width: 90px; }
.aiw-err { color: var(--c-err); font-size: 12px; margin: 6px 0; }

/* 窄屏：侧栏变成顶部横向滚动，内容列占满 */
@media (max-width: 760px) {
  .aic { padding: 14px 12px 48px; }
  .aic-body { grid-template-columns: minmax(0, 1fr); gap: 12px; }
  .aic-nav {
    position: static; flex-direction: row; overflow: auto;
    border-radius: 10px; scrollbar-width: none;
  }
  .aic-nav::-webkit-scrollbar { display: none; }
  .aic-nav-item { white-space: nowrap; }
  .aic-grid { grid-template-columns: 1fr; }
}
</style>
