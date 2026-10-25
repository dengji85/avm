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

          <!-- 联网搜索：给 agent 的「查资料」技能配后端。默认关闭，配了才联网 -->
          <div class="aic-card">
            <div class="aic-card-head">
              <strong>{{ $t('aiCenter.webTitle') }}</strong>
              <span class="aic-tag" :class="{ ok: web.ok }">{{ web.statusText }}</span>
            </div>
            <input v-model="web.url" :placeholder="$t('aiCenter.webUrlPh')"
                   style="width: 100%; margin-bottom: 8px" />
            <input v-model="web.key" type="password" autocomplete="new-password"
                   :placeholder="$t('aiCenter.webKeyPh')" style="width: 100%; margin-bottom: 8px" />
            <p class="hint">{{ $t('aiCenter.webHint') }}</p>
            <div class="aic-bar" style="margin-top: 8px">
              <button class="btn tiny" :disabled="web.busy" @click="saveWeb">{{ $t('common.save') }}</button>
              <button class="btn tiny ghost" :disabled="web.busy" @click="checkWeb">
                {{ web.busy ? $t('ai.working') : $t('aiCenter.webTest') }}
              </button>
            </div>
            <p v-if="web.message" class="hint" style="margin-top: 6px">{{ web.message }}</p>
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
          <!-- 新建批次：范围 + 参数 + 预估，先看清命中多少再提交 -->
          <div class="aic-card">
            <div class="aic-card-head">
              <strong>{{ $t('aiCenter.jobNew') }}</strong>
              <span class="spacer"></span>
              <span class="hint">{{ $t('aiCenter.jobHint') }}</span>
            </div>
            <div class="aic-grid">
              <div class="field">
                <label>{{ $t('settings.aiJobsKind') }}</label>
                <select v-model="job.kind" class="aic-sel" @change="onKindChange">
                  <option value="translate">{{ $t('settings.aiJobsTranslate') }}</option>
                  <option value="plot">{{ $t('settings.aiJobsPlot') }}</option>
                </select>
              </div>
              <div class="field">
                <label>{{ $t('aiCenter.jobScope') }}</label>
                <select v-model="job.scope" class="aic-sel" @change="refreshPreview">
                  <option v-for="s in scopeOptions" :key="s.value" :value="s.value">{{ s.label }}</option>
                </select>
              </div>
              <div class="field">
                <label>{{ $t('aiCenter.jobLimit') }}</label>
                <input v-model.number="job.limit" type="number" min="1" max="500" @change="refreshPreview" />
              </div>
              <template v-if="job.kind === 'translate'">
                <div class="field">
                  <label>{{ $t('aiCenter.jobTarget') }}</label>
                  <select v-model="job.target" class="aic-sel" @change="refreshPreview">
                    <option v-for="tg in targets" :key="tg" :value="tg">{{ targetLabel(tg) }}</option>
                  </select>
                </div>
                <div class="field">
                  <label>{{ $t('aiCenter.jobBilingual') }}</label>
                  <label class="aic-chk">
                    <input type="checkbox" v-model="job.bilingual" />
                    <span>{{ $t('aiCenter.jobBilingualHint') }}</span>
                  </label>
                </div>
              </template>
            </div>
            <details class="aic-adv">
              <summary>{{ $t('aiCenter.jobFilters') }}</summary>
              <div class="aic-grid">
                <div class="field">
                  <label>{{ $t('aiCenter.jobGenre') }}</label>
                  <input v-model="job.filters.genre" @change="refreshPreview" />
                </div>
                <div class="field">
                  <label>{{ $t('aiCenter.jobActress') }}</label>
                  <input v-model="job.filters.actress" @change="refreshPreview" />
                </div>
                <div class="field">
                  <label>{{ $t('aiCenter.jobStudio') }}</label>
                  <input v-model="job.filters.studio" @change="refreshPreview" />
                </div>
                <div class="field">
                  <label>{{ $t('aiCenter.jobYear') }}</label>
                  <input v-model.number="job.filters.year" type="number" @change="refreshPreview" />
                </div>
              </div>
            </details>
            <details class="aic-adv">
              <summary>{{ $t('aiCenter.jobAdvanced') }}</summary>
              <div class="aic-grid">
                <div class="field">
                  <label>{{ $t('aiCenter.jobConcurrency', { n: jobs.job_concurrency }) }}</label>
                  <input v-model.number="job.concurrency" type="number" min="1" max="8" :placeholder="String(jobs.job_concurrency)" />
                </div>
                <div class="field">
                  <label>{{ $t('aiCenter.jobBudget', { n: jobs.budget_chars_per_run || '∞' }) }}</label>
                  <input v-model.number="job.budget" type="number" min="0" placeholder="0" />
                </div>
              </div>
            </details>
            <div class="aic-actions">
              <span v-if="preview.count >= 0" class="hint">
                {{ $t('aiCenter.jobEstimate', { n: preview.count, chars: fmtChars(preview.estimate_chars) }) }}
              </span>
              <button class="btn tiny ghost" :disabled="preview.loading" @click="refreshPreview">
                {{ preview.loading ? $t('aiCenter.jobChecking') : $t('aiCenter.jobRecheck') }}
              </button>
              <button class="btn primary" :disabled="job.busy || preview.count <= 0" @click="startJob">
                {{ job.busy ? $t('aiCenter.jobStarting') : $t('settings.aiJobsStart') }}
              </button>
            </div>
          </div>

          <!-- 进行中 -->
          <div v-if="active" class="aic-card">
            <div class="aic-card-head">
              <strong>{{ kindLabel(active.kind) }}</strong>
              <span class="aic-tag">{{ scopeLabel(active.params && active.params.scope) }}</span>
              <span class="spacer"></span>
              <button class="btn tiny ghost" @click="cancelBatch(active)">{{ $t('settings.aiJobsCancel') }}</button>
            </div>
            <div class="aic-prog"><i :style="{ width: (active.percent || 0) + '%' }"></i></div>
            <p class="hint">
              {{ $t('aiCenter.jobProgress', {
                done: active.done + active.failed + active.skipped, total: active.total,
                ok: active.done, fail: active.failed, skip: active.skipped }) }}
            </p>
          </div>

          <!-- 批次历史 -->
          <div class="aic-bar">
            <span class="hint">{{ $t('aiCenter.jobHistoryHint') }}</span>
            <button class="btn tiny ghost" @click="loadJobs">{{ $t('common.refresh') }}</button>
          </div>
          <table v-if="batches.length" class="aic-tbl">
            <thead>
              <tr>
                <th>{{ $t('aiCenter.jobColTime') }}</th>
                <th>{{ $t('aiCenter.jobColKind') }}</th>
                <th>{{ $t('aiCenter.jobColScope') }}</th>
                <th>{{ $t('aiCenter.jobColResult') }}</th>
                <th>{{ $t('aiCenter.jobColStatus') }}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="b in batches" :key="b.id">
                <td class="sm">{{ b.created_at }}</td>
                <td>{{ kindLabel(b.kind) }}</td>
                <td class="sm">{{ scopeLabel(b.params && b.params.scope) }}</td>
                <td class="sm">
                  {{ b.done }} / {{ b.total }}
                  <span v-if="b.failed" class="aic-badge">{{ $t('aiCenter.jobFailBadge', { n: b.failed }) }}</span>
                </td>
                <td><span class="aic-tag">{{ statusLabel(b.status) }}</span></td>
                <td class="aic-ops">
                  <button class="btn tiny ghost" @click="toggleDetail(b)">
                    {{ detailBatch && detailBatch.id === b.id ? $t('aiCenter.jobHide') : $t('aiCenter.jobView') }}
                  </button>
                  <button v-if="b.failed + b.skipped" class="btn tiny ghost" @click="retryBatch(b)">
                    {{ $t('aiCenter.jobRetry') }}
                  </button>
                  <button v-if="b.status === 'running' || b.status === 'queued'" class="btn tiny ghost" @click="cancelBatch(b)">
                    {{ $t('settings.aiJobsCancel') }}
                  </button>
                  <button v-else class="btn tiny ghost" @click="removeBatch(b)">{{ $t('common.delete') }}</button>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-else class="aic-empty">{{ $t('aiCenter.jobNoHistory') }}</p>

          <!-- 批次明细 -->
          <div v-if="detailBatch" class="aic-card">
            <div class="aic-card-head">
              <strong>{{ $t('aiCenter.jobDetail', { id: detailBatch.id }) }}</strong>
              <span class="spacer"></span>
              <div class="aic-bar-ops">
                <button
                  v-for="f in detailFilters" :key="f.value" class="btn tiny"
                  :class="{ ghost: detail.status !== f.value }"
                  @click="setDetailFilter(f.value)"
                >{{ f.label }}</button>
              </div>
            </div>
            <table v-if="detail.items.length" class="aic-tbl">
              <thead>
                <tr>
                  <th>{{ $t('aiCenter.jobColCode') }}</th>
                  <th>{{ $t('aiCenter.jobColTitle') }}</th>
                  <th>{{ $t('aiCenter.jobColStatus') }}</th>
                  <th>{{ $t('aiCenter.jobColDur') }}</th>
                  <th>{{ $t('aiCenter.jobColChars') }}</th>
                  <th>{{ $t('aiCenter.jobColError') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="it in detail.items" :key="it.id">
                  <td class="mono">{{ it.code || ('#' + it.movie_id) }}</td>
                  <td class="sm ellipsis">{{ it.title }}</td>
                  <td><span class="aic-tag">{{ statusLabel(it.status) }}</span></td>
                  <td class="sm">{{ it.duration_ms ? (it.duration_ms / 1000).toFixed(1) + 's' : '—' }}</td>
                  <td class="sm">{{ it.chars || 0 }}</td>
                  <td class="sm">{{ it.error || '—' }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else class="aic-empty">{{ $t('aiCenter.jobDetailEmpty') }}</p>
            <div v-if="detail.pages > 1" class="aic-actions">
              <button class="btn tiny ghost" :disabled="detail.page <= 1" @click="loadDetail(detail.page - 1)">‹</button>
              <span class="hint">{{ detail.page }} / {{ detail.pages }}</span>
              <button class="btn tiny ghost" :disabled="detail.page >= detail.pages" @click="loadDetail(detail.page + 1)">›</button>
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
              <span v-else-if="glossary.total" class="hint">
                {{ $t('settings.aiGlossaryCount', { n: glossary.total }) }}
              </span>
              <button class="btn ghost" :disabled="suggesting" @click="suggestGlossary">
                {{ suggesting ? $t('settings.aiGlossarySuggesting') : $t('settings.aiGlossarySuggest') }}
              </button>
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
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'

import {
  aiGlossary, aiGlossarySave, aiGlossarySuggest,
  aiJobs, aiJobsCancelBatch, aiJobsCreate, aiJobsDelete,
  aiJobsDetail, aiJobsPreview, aiJobsRetry,
  aiModelDelete, aiModels,
  aiModelSave, aiModelSetDefault, aiModelTest, aiPrompts, aiResetPrompt, aiSavePrompt,
  aiTraces, aiTracesClear, aiWebsearchCheck, aiWebsearchStatus, getConfig, putConfig,
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

// 助手面板里的「改提示词」会带分区深链过来（state.aiSection）；消费一次即清空
const section = ref(state.aiSection || 'models')
state.aiSection = ''
const models = ref([])
const testing = ref('')
const testResult = ref('')
const saving = ref(false)

const form = reactive({
  open: false, isEdit: false, name: '', base_url: '', api_key: '', model: '',
  kind: 'cloud', timeout: 60, temperature: 0.4, enabled: true, make_default: false,
})

const priv = reactive({ insights: false, insights_actress: false })
// 联网搜索配置：SearXNG 地址 / Tavily Key，二选一；配了才联网，默认不联网
const web = reactive({ url: '', key: '', busy: false, ok: null, message: '', statusText: '' })
const localModel = computed(() => (models.value.find((m) => m.is_default) || {}).kind === 'local')
const privNote = computed(() => (localModel.value
  ? t('aiCenter.privLocal')
  : t('aiCenter.privNote')))

async function loadPriv() {
  try {
    const c = await getConfig()
    priv.insights = !!((c.ai || {}).insights)
    priv.insights_actress = !!((c.ai || {}).insights_actress)
    web.url = (c.ai || {}).websearch_url || ''
    web.key = (c.ai || {}).websearch_key || ''
    const st = await aiWebsearchStatus()
    web.ok = st.enabled
    web.statusText = st.enabled ? t('aiCenter.webOn') : t('aiCenter.webOff')
  } catch (e) { /* 读取失败保持默认关闭 */ }
}

async function saveWeb() {
  web.busy = true
  try {
    await putConfig({
      ai: { websearch_url: web.url.trim(), websearch_key: web.key.trim() },
    })
    toast(t('aiCenter.saved'), 'ok')
    await checkWeb()
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    web.busy = false
  }
}

/** 连通性自检：把「没开 JSON」「限流」「Key 无效」翻译成人话 */
async function checkWeb() {
  web.busy = true
  web.message = t('ai.working')
  try {
    const r = await aiWebsearchCheck()
    web.ok = !!r.ok
    web.statusText = r.ok ? t('aiCenter.webOn') : t('aiCenter.webOff')
    web.message = r.message || ''
  } catch (e) {
    web.ok = false
    web.message = e.message
  } finally {
    web.busy = false
  }
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

/* ---------- 批量任务（批次） ---------- */
const job = reactive({
  kind: 'translate', scope: 'untranslated', target: 'zh-Hans', bilingual: false,
  limit: 200, concurrency: 0, budget: 0,
  filters: { genre: '', actress: '', studio: '', year: '' },
  busy: false,
})
const preview = reactive({ count: -1, estimate_chars: 0, loading: false })
const jobs = reactive({ budget_chars_per_run: 0, job_concurrency: 2 })
const batches = ref([])
const active = ref(null)
const scopes = ref({ translate: [], plot: [] })
const targets = ref(['zh-Hans', 'zh-Hant', 'en'])
const detailBatch = ref(null)
const detail = reactive({ items: [], page: 1, pages: 1, total: 0, status: '' })
let jobTimer = null

const SCOPE_LABEL = {
  untranslated: 'aiCenter.scopeUntranslated',
  all_subtitled: 'aiCenter.scopeAllSub',
  no_plot: 'aiCenter.scopeNoPlot',
  from_meta: 'aiCenter.scopeFromMeta',
  failed: 'aiCenter.scopeFailed',
}
const STATUS_LABEL = {
  queued: 'aiCenter.statusQueued',
  running: 'aiCenter.statusRunning',
  done: 'aiCenter.statusDone',
  failed: 'aiCenter.statusFailed',
  skipped: 'aiCenter.statusSkipped',
  cancelled: 'aiCenter.statusCancelled',
}
const TARGET_LABEL = {
  'zh-Hans': 'aiCenter.targetZhHans',
  'zh-Hant': 'aiCenter.targetZhHant',
  en: 'aiCenter.targetEn',
}

function kindLabel(kind) {
  return kind === 'plot' ? t('settings.aiJobsPlot') : t('settings.aiJobsTranslate')
}
function scopeLabel(scope) {
  const key = SCOPE_LABEL[scope]
  return key ? t(key) : (scope || '—')
}
function statusLabel(s) {
  const key = STATUS_LABEL[s]
  return key ? t(key) : (s || '—')
}
function targetLabel(tg) {
  const key = TARGET_LABEL[tg]
  return key ? t(key) : tg
}
function fmtChars(n) {
  const v = Number(n) || 0
  if (v >= 1e8) return (v / 1e8).toFixed(1) + ' 亿'
  if (v >= 1e4) return (v / 1e4).toFixed(1) + ' 万'
  return String(v)
}

const scopeOptions = computed(() => (scopes.value[job.kind] || [])
  .filter((s) => s !== 'ids' && s !== 'latest')
  .map((s) => ({ value: s, label: scopeLabel(s) })))

const detailFilters = computed(() => [
  { value: '', label: t('aiCenter.jobAll') },
  { value: 'failed', label: t('aiCenter.jobFailed') },
  { value: 'skipped', label: t('aiCenter.jobSkipped') },
  { value: 'done', label: t('aiCenter.jobDone') },
])

/** 组装请求体：空筛选字段不传，避免把「没填」当成过滤条件 */
function jobBody() {
  const filters = {}
  Object.entries(job.filters || {}).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined) filters[k] = v
  })
  const body = {
    kind: job.kind,
    scope: job.scope,
    limit: Math.max(1, Math.min(500, Number(job.limit) || 1)),
    filters,
    concurrency: Number(job.concurrency) || 0,
    budget: Number(job.budget) || 0,
  }
  if (job.kind === 'translate') {
    body.target = job.target
    body.bilingual = !!job.bilingual
  }
  return body
}

async function refreshPreview() {
  preview.loading = true
  try {
    const r = await aiJobsPreview(jobBody())
    preview.count = Number(r.count) || 0
    preview.estimate_chars = Number(r.estimate_chars) || 0
  } catch (e) {
    preview.count = -1
    preview.estimate_chars = 0
  } finally {
    preview.loading = false
  }
}

function onKindChange() {
  const list = scopes.value[job.kind] || []
  if (!list.includes(job.scope)) job.scope = list[0] || ''
  refreshPreview()
}

async function loadJobs() {
  try {
    const r = await aiJobs({ limit: 20 })
    batches.value = r.batches || []
    active.value = r.active || null
    jobs.job_concurrency = Number(r.job_concurrency) || 2
    jobs.budget_chars_per_run = Number(r.budget_chars_per_run) || 0
    if (r.scopes && typeof r.scopes === 'object') scopes.value = r.scopes
    if (Array.isArray(r.targets) && r.targets.length) targets.value = r.targets
    const list = scopes.value[job.kind] || []
    if (!list.includes(job.scope)) job.scope = list[0] || job.scope
    if (preview.count < 0) refreshPreview()
    if (detailBatch.value) {
      const fresh = batches.value.find((x) => x.id === detailBatch.value.id)
      if (fresh) detailBatch.value = fresh
    }
  } catch (e) {
    // 轮询失败不打扰用户
  }
}
// 兼容旧调用点（watch / 定时器）
const refreshJobs = loadJobs

async function startJob() {
  if (preview.count <= 0) {
    toast(t('aiCenter.jobNothing'), 'err')
    return
  }
  job.busy = true
  try {
    const r = await aiJobsCreate(jobBody())
    if (!r.count) {
      toast(r.message || t('aiCenter.jobNothing'), 'err')
      return
    }
    toast(t('settings.aiJobsStarted', { n: r.count }), 'ok')
    await loadJobs()
    const b = batches.value.find((x) => x.id === r.batch_id)
    if (b) {
      detailBatch.value = b
      detail.status = ''
      loadDetail(1)
    }
    refreshPreview()
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    job.busy = false
  }
}

async function cancelBatch(b) {
  if (!b) return
  try {
    await aiJobsCancelBatch(b.id)
    toast(t('aiCenter.jobCancelled'), 'ok')
    await loadJobs()
  } catch (e) {
    toast(e.message, 'err')
  }
}

async function retryBatch(b) {
  try {
    const r = await aiJobsRetry(b.id)
    if (!r.retried) {
      toast(t('aiCenter.jobNoRetry'), 'ok')
      return
    }
    toast(t('aiCenter.jobRetried', { n: r.retried }), 'ok')
    await loadJobs()
  } catch (e) {
    toast(e.message, 'err')
  }
}

async function removeBatch(b) {
  const ok = await confirmDialog(t('common.delete'), t('aiCenter.jobDeleteConfirm', { id: b.id }), { danger: true })
  if (!ok) return
  try {
    await aiJobsDelete(b.id)
    if (detailBatch.value && detailBatch.value.id === b.id) detailBatch.value = null
    await loadJobs()
  } catch (e) {
    toast(e.message, 'err')
  }
}

function toggleDetail(b) {
  if (detailBatch.value && detailBatch.value.id === b.id) {
    detailBatch.value = null
    return
  }
  detailBatch.value = b
  detail.status = ''
  loadDetail(1)
}

async function loadDetail(page = 1) {
  if (!detailBatch.value) return
  try {
    const r = await aiJobsDetail(detailBatch.value.id, { status: detail.status, page, size: 50 })
    detail.items = r.items || []
    detail.page = r.page || 1
    detail.pages = r.pages || 1
    detail.total = r.total || 0
    if (r.batch) detailBatch.value = r.batch
  } catch (e) {
    // 忽略
  }
}

function setDetailFilter(v) {
  detail.status = v
  loadDetail(1)
}

/* 从库里的女优 / 系列一键生成候选（只追加到编辑框，保存后才生效） */
const suggesting = ref(false)

async function suggestGlossary() {
  suggesting.value = true
  try {
    const r = await aiGlossarySuggest({ limit: 200 })
    const items = r.items || []
    const c = r.counts || {}
    if (!items.length) {
      toast(t('settings.aiGlossarySuggestEmpty'), 'ok')
      return
    }
    const ok = await confirmDialog(
      t('settings.aiGlossarySuggestTitle'),
      t('settings.aiGlossarySuggestDesc', {
        n: c.total || items.length, alias: c.alias || 0, kana: c.kana || 0, series: c.series || 0,
      }),
      { danger: false },
    )
    if (!ok) return
    // 编辑框里已有的 term 不重复追加（可能是用户手写的同名词）
    const existing = new Set()
    for (const line of glossary.text.split('\n')) {
      const it = parseGlossaryLine(line)
      if (it) existing.add(it.term)
    }
    const groups = [
      ['alias', 'settings.aiGlossarySrcAlias'],
      ['kana', 'settings.aiGlossarySrcKana'],
      ['series', 'settings.aiGlossarySrcSeries'],
    ]
    const lines = []
    let added = 0
    for (const [src, labelKey] of groups) {
      const list = items.filter((i) => i.source === src && !existing.has(i.term))
      if (!list.length) continue
      lines.push(`# ${t(labelKey)}`)
      for (const it of list) {
        lines.push(`${it.term} = ${it.translation}`)
        existing.add(it.term)
        added += 1
      }
    }
    if (!added) {
      toast(t('settings.aiGlossarySuggestDup'), 'ok')
      return
    }
    const base = glossary.text.trimEnd()
    glossary.text = (base ? base + '\n' : '') + lines.join('\n')
    toast(t('settings.aiGlossarySuggestDone', { n: added }), 'ok')
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    suggesting.value = false
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
const glossary = reactive({ text: '', busy: false, ok: false, total: 0 })

/** 解析一行术语：支持 [lang] 前缀，分隔符支持 = / => / Tab / 中英文冒号。 */
function parseGlossaryLine(line) {
  let s = String(line || '').trim()
  if (!s || s.startsWith('#')) return null
  let lang = ''
  const tag = s.match(/^\[([A-Za-z][\w-]*)\]\s*/)
  if (tag) {
    lang = tag[1]
    s = s.slice(tag[0].length)
  }
  // 非贪婪左值 + 最先出现的分隔符；右侧允许再出现分隔符（译名里带 = 也不影响）
  const m = s.match(/^(.+?)\s*(?:=>|：|:|\t|=)\s*(.+)$/)
  if (!m) return null
  const term = m[1].trim()
  const translation = m[2].trim()
  if (!term || !translation) return null
  return lang ? { term, translation, lang } : { term, translation }
}

function glossaryLine(item) {
  const prefix = item.lang && item.lang !== 'any' ? `[${item.lang}] ` : ''
  return `${prefix}${item.term} = ${item.translation}`
}

async function loadGlossary() {
  try {
    const g = await aiGlossary()
    const items = g.items || []
    glossary.text = items.map(glossaryLine).join('\n')
    glossary.total = items.length
  } catch (e) {
    // 忽略：术语表为空也能用
  }
}

async function saveGlossary() {
  glossary.busy = true
  glossary.ok = false
  try {
    const items = []
    let bad = 0
    for (const line of glossary.text.split('\n')) {
      if (!line.trim() || line.trim().startsWith('#')) continue
      const it = parseGlossaryLine(line)
      if (it) items.push(it)
      else bad += 1
    }
    const r = await aiGlossarySave(items)
    glossary.ok = true
    glossary.total = Number(r.count) || items.length
    if (bad) toast(t('settings.aiGlossaryInvalid', { n: bad }), 'err')
    else toast(t('settings.aiGlossarySaved'), 'ok')
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
  // 有批次在跑时刷新列表与已展开的明细，跑完自动停
  jobTimer = setInterval(() => {
    if (!active.value) return
    loadJobs()
    if (detailBatch.value) loadDetail(detail.page)
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

/* 批量任务：进度条 / 折叠区 / 表格小字 */
.aic-prog {
  height: 6px; border-radius: 999px; overflow: hidden; margin: 6px 0 8px;
  background: var(--c-surface-3);
}
.aic-prog > i {
  display: block; height: 100%; background: var(--c-primary);
  transition: width .3s ease;
}
.aic-adv { margin-top: 10px; }
.aic-adv > summary {
  cursor: pointer; font-size: 12px; color: var(--c-text-3);
  padding: 4px 0; border-top: 1px dashed var(--c-line);
}
.aic-adv > summary:hover { color: var(--c-text-2); }
.aic-adv .aic-grid { margin-top: 8px; }
.aic-tbl .sm { font-size: 12px; color: var(--c-text-3); }
.aic-tbl td.ellipsis { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

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
