<script setup>
import { ref, reactive, onMounted, computed } from 'vue'
import { state } from '../../state.js'
import { scrapeFailures, scrapeRetryNeterr, scrapeRetryWithProvider, listProviders, scrapeOne } from '../../api.js'
import { toast } from '../../utils.js'
import { t } from '../../i18n/index.js'

const loading = ref(false)
const items = ref([])
const groups = ref({})
const err = ref('')
const onlyUn = ref(true)
const filter = ref('')          // 当前归类筛选
const selected = ref(new Set()) // 选中影片的 code
const busy = ref(false)

const providers = ref([])
const providerName = ref('')

async function load() {
  loading.value = true; err.value = ''
  try {
    const r = await scrapeFailures({ limit: 300, group: filter.value, only_unskipped: onlyUn.value })
    items.value = r.items || []
    groups.value = r.groups || {}
  } catch (e) { err.value = e.message || t('maint.failLoadFail') } finally { loading.value = false }
}

async function loadProviders() {
  try { const r = await listProviders(); providers.value = (r.providers || []).map(p => p.name) } catch (e) {}
}

const groupDefs = [
  { key: 'blocked', label: t('maint.failGroupBlocked'), cls: 'red' },
  { key: 'neterr', label: t('maint.failGroupNeterr'), cls: 'orange' },
  { key: 'miss', label: t('maint.failGroupMiss'), cls: 'gray' },
  { key: 'parse_err', label: t('maint.failGroupParse'), cls: 'purple' },
  { key: 'code_issue', label: t('maint.failGroupCode'), cls: 'blue' },
  { key: 'mixed', label: t('maint.failGroupMixed'), cls: 'gray' },
]

const totalCount = computed(() => items.value.length)
function isSel(code) { return selected.value.has(code) }
function toggle(code) {
  const s = new Set(selected.value)
  s.has(code) ? s.delete(code) : s.add(code)
  selected.value = s
}

async function retryNet() {
  if (busy.value) return
  if (!confirm(t('maint.failRetryNetTip'))) return
  busy.value = true
  try {
    const r = await scrapeRetryNeterr({})
    toast(t('maint.startedRetry') + ` (${r.retried_codes.length})`, 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') } finally { busy.value = false }
}

async function retryWithProvider() {
  if (busy.value) return
  if (!providerName.value) { toast(t('maint.failProviderSel'), 'err'); return }
  const codes = [...selected.value]
  busy.value = true
  try {
    const r = await scrapeRetryWithProvider({ provider: providerName.value, codes })
    toast(t('maint.failRetrySel') + ` · ${providerName.value}`, 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') } finally { busy.value = false }
}

function viewMovie(id) { if (id) { state.currentId = id; state.view = 'detail' } }

// 由 diagnosis.summary_kind 决定整行配色
function rowClass(d) { return 'k-' + (d && d.summary_kind || 'miss') }

onMounted(() => { load(); loadProviders() })
</script>

<template>
  <div class="sf">
    <div class="sf-head">
      <div>
        <h3 class="sub">{{ t('maint.failTitle') }}</h3>
        <p class="muted sm">{{ t('maint.failSub') }}</p>
      </div>
      <div class="sf-acts">
        <label class="chk"><input type="checkbox" v-model="onlyUn" @change="load" /> {{ t('maint.failFilterAll') }}</label>
        <button class="btn sm" :disabled="busy" @click="retryNet">{{ t('maint.failRetryNet') }}</button>
      </div>
    </div>

    <!-- 分组计数卡 -->
    <div class="grp-cards">
      <button class="grp" :class="{ on: filter === '' }" @click="filter = ''; load()">
        <span class="g-num">{{ totalCount }}</span><span class="g-lab">{{ t('maint.failFilterAll') }}</span>
      </button>
      <button v-for="g in groupDefs" :key="g.key" class="grp" :class="[g.cls, { on: filter === g.key }]"
              :disabled="!groups[g.key]" @click="filter = g.key; load()">
        <span class="g-num">{{ groups[g.key] || 0 }}</span><span class="g-lab">{{ g.label }}</span>
      </button>
    </div>

    <div v-if="err" class="empty compact"><div class="icon err">!</div><div class="desc">{{ err }}</div></div>
    <div v-else-if="loading" class="empty compact"><span class="spinner large"></span></div>
    <div v-else-if="!items.length" class="empty compact"><div class="desc">{{ t('maint.failAllOk') }}</div></div>
    <div v-else class="tbl-wrap">
      <table class="tbl">
        <thead>
          <tr>
            <th class="c-sel"></th>
            <th class="c-code">{{ t('maint.thCode') }}</th>
            <th class="c-diag">{{ t('maint.failHeadline') }}</th>
            <th class="c-src">{{ t('maint.failSources') }}</th>
            <th class="c-rec">{{ t('maint.failRecommend') }}</th>
            <th class="c-act">{{ t('maint.failViewMovie') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="it in items" :key="it.code" :class="rowClass(it.diagnosis)">
            <td class="c"><input type="checkbox" :checked="isSel(it.code)" @change="toggle(it.code)" /></td>
            <td class="mono c cp" @click="viewMovie(it.movie_id)">{{ it.code || '—' }}</td>
            <td class="diag">{{ it.diagnosis.headline }}</td>
            <td class="srcs">
              <span v-for="s in it.diagnosis.sources" :key="s.provider" class="src" :class="'k-' + s.kind"
                    :title="s.reason">
                <span class="dot"></span>{{ s.provider }}
              </span>
            </td>
            <td class="rec">{{ it.diagnosis.recommend }}</td>
            <td class="c"><button class="btn tiny" @click="viewMovie(it.movie_id)">{{ t('maint.failViewMovie') }}</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 选中项：换源重抓 -->
    <div class="sf-foot" v-if="selected.size">
      <span class="muted sm">{{ t('maint.failTotal', { n: selected.size }) }}</span>
      <select v-model="providerName" class="sel">
        <option value="">{{ t('maint.failProviderSel') }}</option>
        <option v-for="p in providers" :key="p" :value="p">{{ p }}</option>
      </select>
      <button class="btn sm" :disabled="busy || !providerName" @click="retryWithProvider">{{ t('maint.failRetryProvider') }}</button>
    </div>
  </div>
</template>

<style scoped>
.sf { display: flex; flex-direction: column; gap: var(--sp-3); }
.sf-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--sp-3); flex-wrap: wrap; }
.sf-head .sub { margin: 0; }
.sf-acts { display: flex; gap: var(--sp-2); align-items: center; }
.chk { font-size: var(--fs-sm); color: var(--c-text-2); display: flex; align-items: center; gap: 4px; }

.grp-cards { display: flex; gap: var(--sp-2); flex-wrap: wrap; }
.grp {
  display: flex; flex-direction: column; align-items: center; min-width: 86px;
  padding: var(--sp-2) var(--sp-3); border: 1px solid var(--c-line); border-radius: var(--r-md);
  background: var(--c-surface-2); cursor: pointer; color: var(--c-text-2);
}
.grp:disabled { opacity: .4; cursor: not-allowed; }
.grp.on { border-color: var(--c-primary); color: var(--c-primary); }
.g-num { font-size: var(--fs-lg); font-weight: 700; font-variant-numeric: tabular-nums; }
.g-lab { font-size: var(--fs-xs); }
.grp.red .g-num { color: var(--c-err); } .grp.orange .g-num { color: #e0820c; }
.grp.gray .g-num { color: var(--c-text-3); } .grp.purple .g-num { color: #9b59b6; }
.grp.blue .g-num { color: #2f7de0; }

.tbl-wrap { border: 1px solid var(--c-line); border-radius: var(--r-md); overflow: hidden; }
.tbl { width: 100%; border-collapse: collapse; font-size: var(--fs-sm); }
.tbl th, .tbl td { padding: 7px 10px; text-align: left; border-bottom: 1px solid var(--c-line); vertical-align: top; }
.tbl th { background: var(--c-surface-2); position: sticky; top: 0; color: var(--c-text-3); font-weight: 600; }
.tbl tbody tr:hover { background: var(--c-surface-2); }
.tbl tr.k-blocked { box-shadow: inset 3px 0 0 var(--c-err); }
.tbl tr.k-neterr { box-shadow: inset 3px 0 0 #e0820c; }
.tbl tr.k-parse_err { box-shadow: inset 3px 0 0 #9b59b6; }
.tbl tr.k-code_issue { box-shadow: inset 3px 0 0 #2f7de0; }
.diag { color: var(--c-text); max-width: 220px; }
.srcs { display: flex; flex-direction: column; gap: 2px; }
.src { display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-xs); color: var(--c-text-2); }
.src .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--c-warn); }
.src.k-ok .dot { background: var(--c-ok); } .src.k-miss .dot { background: var(--c-warn); }
.src.k-blocked .dot { background: var(--c-err); } .src.k-neterr .dot { background: #e0820c; }
.src.k-parse_err .dot { background: #9b59b6; } .src.k-code_issue .dot { background: #2f7de0; }
.rec { color: var(--c-text-2); max-width: 280px; }
.mono { font-family: var(--font-mono, monospace); color: var(--c-text-3); font-size: var(--fs-xs); }
.cp { cursor: pointer; } .cp:hover { color: var(--c-primary); }
.c { text-align: center; }
.sf-foot { display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-2) 0; border-top: 1px solid var(--c-line); }
.sel { height: 32px; background: var(--c-surface-2); border: 1px solid var(--c-line); border-radius: var(--r-sm); color: var(--c-text); padding: 0 var(--sp-2); font-size: var(--fs-sm); }
.empty.compact { padding: var(--sp-6) var(--sp-4); } .empty .icon.err { background: var(--c-err); color: #fff; }
</style>
