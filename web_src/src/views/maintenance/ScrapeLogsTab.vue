<script setup>
import { ref, reactive, onMounted, watch } from 'vue'
import { state } from '../../state.js'
import { scrapeTasks, scrapeLogs, scrapeLogsClear, scrapeSkips, scrapeSkipsClear, startScrape } from '../../api.js'
import { toast } from '../../utils.js'
import { t } from '../../i18n/index.js'
import PageHead from '../../components/PageHead.vue'

const tasks = ref([])
const taskMap = ref({})
const q = reactive({ task_id: '', status: '', code: '', page: 1, size: 50 })
const loading = ref(false)
const logs = ref([])
const total = ref(0)
const err = ref('')

async function loadTasks() {
  try { const r = await scrapeTasks(50); tasks.value = r.items || []; r.items.forEach((t) => { taskMap.value[t.task_id] = t }) } catch (e) { toast(e.message, 'err') }
}
async function loadLogs() {
  loading.value = true; err.value = ''
  try {
    const r = await scrapeLogs(q.task_id, { status: q.status, code: q.code, page: q.page, size: q.size })
    logs.value = r.items; total.value = r.total
  } catch (e) { err.value = e.message || t('maint.loadFail') } finally { loading.value = false }
}
const totalPages = () => Math.max(1, Math.ceil(total.value / q.size))
function go(p) { q.page = Math.min(totalPages(), Math.max(1, p)); loadLogs() }
function viewMovie(id) { if (id) { state.currentId = id; state.view = 'detail' } }

// 跳过名单（已知稳定失败的影片，下次刮削自动跳过）
const skips = ref([])
const skipTotal = ref(0)
async function loadSkips() {
  try { const r = await scrapeSkips(); skips.value = r.items; skipTotal.value = r.total } catch (e) { toast(e.message, 'err') }
}
async function unskip(movie_id, code) {
  if (!confirm(t('maint.confirmUnskip', { code: code || '#' + movie_id }))) return
  try {
    const r = await scrapeSkipsClear(movie_id)
    toast(t('maint.removed', { n: r.removed }), 'ok')
    await loadSkips()
  } catch (e) { toast(e.message || t('maint.removedFail', { n: 0 }), 'err') }
}
async function clearSkips() {
  if (!confirm(t('maint.confirmClearSkips'))) return
  try {
    const r = await scrapeSkipsClear(0)
    toast(t('maint.removed', { n: r.removed }), 'ok')
    await loadSkips()
  } catch (e) { toast(e.message || t('maint.opFail'), 'err') }
}
async function retrySkips() {
  if (!confirm(t('maint.confirmRetrySkips'))) return
  try {
    await startScrape({ scope: 'retry', force: true })
    toast(t('maint.startedRetry'), 'ok')
    await loadSkips()
  } catch (e) { toast(e.message || t('maint.opFail'), 'err') }
}

async function clearLogs() {
  const scope = (q.task_id ? t('maint.scopeTask') : '') + (q.status ? t('maint.scopeStatus') : '') || t('maint.scopeAll')
  const all = !q.task_id && !q.status
  const msg = all
    ? t('maint.confirmClearLogsAll')
    : t('maint.confirmClearLogsScope', { scope })
  if (!confirm(msg)) return
  try {
    const r = await scrapeLogsClear({ task_id: q.task_id, status: q.status })
    toast(t('maint.cleared', { n: r.deleted }), 'ok')
    q.page = 1
    await loadLogs()
  } catch (e) { toast(e.message || t('maint.opFail'), 'err') }
}

onMounted(() => { loadTasks(); loadLogs(); loadSkips() })
watch(() => [q.status, q.code, q.task_id], () => { q.page = 1; loadLogs() })
</script>

<template>
  <div class="sl">
    <div class="filters">
      <select v-model="q.task_id" class="sel">
        <option value="">{{ $t('maint.allTasks') }}</option>
        <option v-for="t in tasks" :key="t.task_id" :value="t.task_id">{{ t.started_at }} · {{ t.total }}{{ $t('maint.items') }}</option>
      </select>
      <select v-model="q.status" class="sel">
        <option value="">{{ $t('maint.allStatus') }}</option>
        <option value="ok">{{ $t('maint.ok') }}</option>
        <option value="miss">{{ $t('maint.miss') }}</option>
        <option value="error">{{ $t('maint.error') }}</option>
      </select>
      <input v-model="q.code" class="inp" :placeholder="$t('maint.searchCode')" @keyup.enter="go(1)" />
      <span class="muted sm">{{ $t('maint.totalCount', { n: total }) }}</span>
      <button class="btn danger sm clear-btn" @click="clearLogs">{{ $t('maint.clearLogs') }}</button>
    </div>

    <div v-if="err" class="empty compact"><div class="icon err">!</div><div class="desc">{{ err }}</div></div>
    <div v-else-if="loading" class="empty compact"><span class="spinner large"></span></div>
    <div v-else-if="!logs.length" class="empty compact"><div class="desc">{{ $t('maint.noLogs') }}</div></div>
    <div v-else class="tbl-wrap">
      <table class="tbl">
        <thead><tr><th class="c-time">{{ $t('maint.thTime') }}</th><th class="c-code">{{ $t('maint.thCode') }}</th><th class="c-src">{{ $t('maint.thSrc') }}</th><th class="c-status">{{ $t('maint.thStatus') }}</th><th class="c-reason">{{ $t('maint.thReason') }}</th><th class="c-ms">{{ $t('maint.thMs') }}</th></tr></thead>
        <tbody>
          <tr v-for="l in logs" :key="l.id" :class="l.status">
            <td class="mono">{{ l.started_at }}</td>
            <td class="mono c">{{ l.code || '—' }}</td>
            <td class="mono">{{ l.provider || '—' }}</td>
            <td><span class="ls-dot" :class="l.status"></span>{{ { ok: $t('maint.ok'), miss: $t('maint.miss'), error: $t('maint.error') }[l.status] || l.status }}</td>
            <td class="reason">{{ l.reason || '—' }}</td>
            <td class="mono c">{{ l.elapsed_ms }}ms</td>
          </tr>
        </tbody>
      </table>
      <div class="pager">
        <button class="btn tiny" :disabled="q.page <= 1" @click="go(q.page - 1)">{{ $t('common.prev') }}</button>
        <span class="tabular">{{ q.page }} / {{ totalPages() }}</span>
        <button class="btn tiny" :disabled="q.page >= totalPages()" @click="go(q.page + 1)">{{ $t('common.next') }}</button>
      </div>
    </div>

    <section class="skip-block">
      <header class="skip-head">
        <h3 class="sub">{{ $t('maint.skipsTitle') }} <span class="muted sm">{{ $t('maint.skipsHint', { n: skipTotal }) }}</span></h3>
        <div class="skip-acts">
          <button class="btn sm" :disabled="!skipTotal" @click="retrySkips">{{ $t('maint.retry') }}</button>
          <button class="btn danger sm" :disabled="!skipTotal" @click="clearSkips">{{ $t('maint.clearAll') }}</button>
        </div>
      </header>
      <div v-if="!skipTotal" class="empty compact"><div class="desc">{{ $t('maint.noSkips') }}</div></div>
      <div v-else class="tbl-wrap">
        <table class="tbl">
          <thead><tr><th class="c-code">{{ $t('maint.thCode') }}</th><th>{{ $t('maint.thMovie2') }}</th><th class="c-kind">{{ $t('maint.thKind2') }}</th><th>{{ $t('maint.thReason2') }}</th><th class="c-cnt">{{ $t('maint.thCount') }}</th><th class="c-act">{{ $t('maint.thAct') }}</th></tr></thead>
          <tbody>
            <tr v-for="s in skips" :key="s.id">
              <td class="mono c cp" @click="viewMovie(s.movie_id)">{{ s.code || '—' }}</td>
              <td class="cp title" @click="viewMovie(s.movie_id)">{{ s.title || $t('maint.unknownMovie') }}</td>
              <td class="c"><span class="ls-dot" :class="s.kind"></span>{{ { miss: $t('maint.miss'), error: $t('maint.error') }[s.kind] || s.kind }}</td>
              <td class="reason">{{ s.reason || '—' }}</td>
              <td class="mono c tabular">{{ s.count }}</td>
              <td class="c"><button class="btn tiny" @click="unskip(s.movie_id, s.code)">{{ $t('maint.unskip') }}</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.sl { display: flex; flex-direction: column; gap: var(--sp-3); }
.filters { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap; }
.filters .clear-btn { margin-left: auto; }
.sel, .inp { height: 32px; background: var(--c-surface-2); border: 1px solid var(--c-line); border-radius: var(--r-sm); color: var(--c-text); padding: 0 var(--sp-2); font-size: var(--fs-sm); }
.inp { min-width: 160px; }
.tbl-wrap { border: 1px solid var(--c-line); border-radius: var(--r-md); overflow: hidden; }
.tbl { width: 100%; border-collapse: collapse; font-size: var(--fs-sm); }
.tbl th, .tbl td { padding: 7px 10px; text-align: left; border-bottom: 1px solid var(--c-line); }
.tbl th { background: var(--c-surface-2); position: sticky; top: 0; color: var(--c-text-3); font-weight: 600; }
.tbl tbody tr:hover { background: var(--c-surface-2); }
.tbl tr.ok .mono.c { color: var(--c-ok); }
.tbl tr.error { background: var(--c-err-soft); }
.tbl tr.error:hover { background: var(--c-err-soft); }
.mono { font-family: var(--font-mono, monospace); color: var(--c-text-3); font-size: var(--fs-xs); }
.c { text-align: center; }
.reason { color: var(--c-text-2); max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ls-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; vertical-align: middle; background: var(--c-text-3); }
.ls-dot.ok { background: var(--c-ok); } .ls-dot.miss { background: var(--c-warn); } .ls-dot.error { background: var(--c-err); }
.pager { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-2) var(--sp-3); border-top: 1px solid var(--c-line); }
.empty.compact { padding: var(--sp-6) var(--sp-4); } .empty .icon.err { background: var(--c-err); color: #fff; }

/* 跳过名单区块 */
.skip-block { border-top: 1px solid var(--c-line); padding-top: var(--sp-3); }
.skip-head { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3); flex-wrap: wrap; margin-bottom: var(--sp-2); }
.skip-head .sub { margin: 0; }
.skip-acts { display: flex; gap: var(--sp-2); }
.cp { cursor: pointer; } .cp:hover { color: var(--c-primary); }
.title { color: var(--c-text); max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.c-kind { width: 72px; } .c-cnt { width: 56px; } .c-act { width: 64px; }
.ls-dot.miss { background: var(--c-warn); } .ls-dot.error { background: var(--c-err); }
</style>
