<script setup>
import { ref, onMounted, computed } from 'vue'
import { state } from '../../state.js'
import { useTasks, pct } from '../../composables/useTasks.js'
import { maintenanceSummary, scrapeTasks, rescanLocalCovers, reparseCodes } from '../../api.js'
import { toast } from '../../utils.js'
import { t } from '../../i18n/index.js'
import PageHead from '../../components/PageHead.vue'
import StatGrid from '../../components/StatGrid.vue'

const { anyRunning, activeTasks, runScan, runScrape } = useTasks()

const summary = ref(null)
const tasks = ref([])
const busy = ref(false)

async function loadSummary() {
  try { summary.value = await maintenanceSummary() } catch { summary.value = null }
}
async function loadTasks() {
  try { tasks.value = (await scrapeTasks(8)).items || [] } catch { tasks.value = [] }
}

const todos = computed(() => {
  const s = summary.value || {}
  return [
    { key: 'noscrape', label: 'maint.ovNoScrape', n: s.noscrape || 0, flag: 'noscrape', icon: '⚑' },
    { key: 'missing_cover', label: 'maint.ovNoCover', n: s.missing_cover || 0, flag: 'nocover', icon: '▢' },
    { key: 'unrecognized', label: 'maint.ovNoCode', n: s.unrecognized || 0, flag: 'nocode', icon: '?' },
    { key: 'split_incomplete', label: 'maint.ovSplit', n: s.split_incomplete || 0, flag: null, icon: '⧉' },
    { key: 'duplicates', label: 'maint.ovDup', n: s.duplicates || 0, flag: null, icon: '⧉' },
    { key: 'missing_files', label: 'maint.ovMissingFile', n: s.missing_files || 0, flag: null, icon: '⛔' },
    { key: 'watchlist', label: 'maint.ovWatchlist', n: s.watchlist || 0, flag: 'watchlist', icon: '⌚' },
  ]
})

function goTodo(t) {
  if (t.flag) {
    state.flags = [t.flag]
    state.actress = []; state.genre = []; state.studio = ''; state.series = ''; state.prefix = ''; state.year = null
    state.q = ''; state.page = 1
    state.view = 'gallery'
  } else {
    state.view = 'maintenance'; state.maintTab = 'storage'
  }
}

const ring = computed(() => {
  const at = activeTasks.value[0]
  if (!at) return { p: 0, label: t('maint.idle'), ok: 0, miss: 0, error: 0 }
  return {
    p: pct(at),
    label: at.label === '刮削元数据' ? t('maint.scraping') : t('maint.scanning'),
    ok: at.ok || 0, miss: at.miss || 0, error: (at.fail || 0) + (at.error_count || 0),
  }
})

const statCards = computed(() => {
  const s = summary.value || {}
  return [
    { label: t('maint.ovTotal'), value: s.total || 0 },
    { label: t('maint.ovPending'), value: (s.noscrape || 0) + (s.missing_cover || 0) + (s.unrecognized || 0), tone: 'warn' },
  ]
})

async function doScan() { busy.value = true; try { runScan({}); toast(t('maint.ovScanStarted'), 'ok') } finally { busy.value = false } }
async function doScrape() { busy.value = true; try { runScrape({ missing_only: true }); toast(t('maint.ovScrapeStarted'), 'ok') } finally { busy.value = false } }
async function doLocalCovers() { busy.value = true; try { await rescanLocalCovers(); toast(t('maint.ovCoversStarted'), 'ok') } finally { busy.value = false } }
async function doReparse() { busy.value = true; try { await reparseCodes({}); toast(t('maint.ovReparseStarted'), 'ok') } finally { busy.value = false } }

function onDrop(e) {
  e.preventDefault()
  dropActive.value = false
  const items = [...(e.dataTransfer?.files || [])]
  // 取首个文件/目录路径（浏览器仅暴露名称，真实路径需后端 watch 目录；此处触发全库扫描）
  runScan({})
  toast(t('maint.ovScanStarted'), 'ok')
}
const dropActive = ref(false)

onMounted(() => { loadSummary(); loadTasks() })
</script>

<template>
  <div class="ov">
    <StatGrid :items="statCards" />

    <!-- 拖入文件夹触发扫描 -->
    <div
      class="dropzone"
      :class="{ active: dropActive }"
      @dragover.prevent="dropActive = true"
      @dragleave.prevent="dropActive = false"
      @drop="onDrop"
    >
      <span class="dz-ico">⤓</span>
      <span class="dz-t">{{ $t('maint.ovDrag') }}</span>
      <span class="dz-s">{{ $t('maint.ovDragHint') }}</span>
    </div>

    <!-- 操作台 -->
    <div class="ops">
      <button class="op" :disabled="anyRunning" @click="doScan">
        <span class="op-ico">🔍</span><span class="op-t">{{ $t('maint.ovScanLib') }}</span>
        <span class="op-d">{{ $t('maint.ovScanDesc') }}</span>
      </button>
      <button class="op" :disabled="anyRunning" @click="doScrape">
        <span class="op-ico">✨</span><span class="op-t">{{ $t('maint.ovScrapeMissing') }}</span>
        <span class="op-d">{{ $t('maint.ovScrapeDesc') }}</span>
      </button>
      <button class="op" :disabled="anyRunning" @click="doLocalCovers">
        <span class="op-ico">🖼️</span><span class="op-t">{{ $t('maint.ovLocalCover') }}</span>
        <span class="op-d">{{ $t('maint.ovLocalCoverDesc') }}</span>
      </button>
      <button class="op" :disabled="anyRunning" @click="doReparse">
        <span class="op-ico">🔤</span><span class="op-t">{{ $t('maint.ovReparse') }}</span>
        <span class="op-d">{{ $t('maint.ovReparseDesc') }}</span>
      </button>
    </div>

    <div class="grid2">
      <!-- 进度 -->
      <section class="panel">
        <div class="panel-head">{{ $t('maint.ovScanTitle') }}</div>
        <div class="panel-body prog-body">
          <div class="ring" :class="{ run: anyRunning }" :style="{ background: `conic-gradient(var(--c-primary) ${ring.p * 3.6}deg, var(--c-line) 0)` }">
            <div class="ring-in">
              <span class="rp tabular">{{ ring.p }}%</span>
              <span class="rl">{{ ring.label }}</span>
            </div>
          </div>
          <div class="prog-stats">
            <div class="ps"><b class="tabular">{{ ring.ok }}</b><span>{{ $t('maint.ovOk') }}</span></div>
            <div class="ps"><b class="tabular">{{ ring.miss }}</b><span>{{ $t('maint.ovMiss') }}</span></div>
            <div class="ps"><b class="tabular">{{ ring.error }}</b><span>{{ $t('maint.ovFail') }}</span></div>
            <p v-if="!anyRunning" class="muted sm">{{ $t('maint.ovNoTask') }}</p>
            <p v-else class="muted sm">{{ $t('maint.ovBellHint') }}</p>
          </div>
        </div>
        <div v-if="summary?.last_scan" class="last-scan">
          {{ $t('maint.ovLastScan') }}：<b class="tabular">{{ summary.last_scan.started_at }}</b>
          · {{ $t('maint.ovAdded') }} <b class="tabular ok">{{ summary.last_scan.added }}</b>
          · {{ $t('maint.ovUpdated') }} <b class="tabular">{{ summary.last_scan.updated }}</b>
          · {{ $t('maint.ovRemoved') }} <b class="tabular warn">{{ summary.last_scan.removed }}</b>
        </div>
      </section>

      <!-- 待办 -->
      <section class="panel">
        <div class="panel-head">{{ $t('maint.ovTodoTitle') }}</div>
        <div class="panel-body">
          <div class="todo-grid">
            <button v-for="t in todos" :key="t.key" class="todo" :class="{ zero: !t.n }" @click="goTodo(t)">
              <span class="ti">{{ t.icon }}</span>
              <span class="tn tabular">{{ t.n }}</span>
              <span class="tl">{{ $t(t.label) }}</span>
            </button>
          </div>
        </div>
      </section>
    </div>

    <!-- 最近刮削任务 -->
    <section class="panel">
      <div class="panel-head">
        {{ $t('maint.ovLastScrape') }}
        <div class="spacer"></div>
        <button class="btn ghost tiny" @click="loadTasks">{{ $t('maint.refresh') }}</button>
      </div>
      <div class="panel-body">
        <div v-if="!tasks.length" class="muted pad">{{ $t('maint.ovNoScrapeLog') }}</div>
        <table v-else class="t-tasks">
          <thead><tr><th>{{ $t('maint.ovStart') }}</th><th>{{ $t('maint.ovTotal') }}</th><th>{{ $t('maint.ovOk') }}</th><th>{{ $t('maint.ovMiss') }}</th><th>{{ $t('maint.ovFail') }}</th><th>{{ $t('maint.ovAvgMs') }}</th><th></th></tr></thead>
          <tbody>
            <tr v-for="t in tasks" :key="t.task_id">
              <td class="mono">{{ t.started_at }}</td>
              <td class="tabular">{{ t.total }}</td>
              <td class="tabular ok">{{ t.ok }}</td>
              <td class="tabular warn">{{ t.miss }}</td>
              <td class="tabular err">{{ t.error }}</td>
              <td class="tabular">{{ t.avg_ms }}ms</td>
              <td><button class="btn ghost tiny" @click="state.maintTab='logs'">{{ $t('maint.viewLogs') }}</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.ov { display: flex; flex-direction: column; gap: var(--sp-4); }
.ops { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sp-3); }
.op { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; padding: var(--sp-3); border-radius: var(--r-md); cursor: pointer; text-align: left; border: 1px solid var(--c-line-strong); background: var(--c-surface-2); color: var(--c-text); }
.op:hover:not(:disabled) { border-color: var(--c-primary); background: var(--c-primary-soft); }
.op:disabled { opacity: .55; cursor: not-allowed; }
.op-ico { font-size: 20px; } .op-t { font-weight: 700; } .op-d { font-size: var(--fs-xs); color: var(--c-text-3); }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }
.prog-body { display: flex; gap: var(--sp-3); align-items: center; }
.ring { width: 96px; height: 96px; border-radius: 50%; flex: 0 0 auto; display: grid; place-items: center; transition: background .4s linear; }
.ring-in { width: 76px; height: 76px; border-radius: 50%; background: var(--c-surface); display: grid; place-items: center; }
.rp { font-size: var(--fs-lg); font-weight: 800; } .rl { font-size: var(--fs-xs); color: var(--c-text-3); }
.prog-stats { display: flex; gap: var(--sp-3); flex-wrap: wrap; }
.ps { display: flex; flex-direction: column; } .ps b { font-size: var(--fs-lg); } .ps span { font-size: var(--fs-xs); color: var(--c-text-3); }
.last-scan { margin-top: var(--sp-2); font-size: var(--fs-sm); color: var(--c-text-2); border-top: 1px dashed var(--c-line); padding-top: var(--sp-2); }
.todo-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sp-2); }
.todo { display: flex; flex-direction: column; align-items: center; gap: 1px; padding: var(--sp-2); border-radius: var(--r-sm); border: 1px solid var(--c-line); background: var(--c-surface-2); cursor: pointer; color: var(--c-text); }
.todo:hover:not(.zero) { border-color: var(--c-primary); background: var(--c-primary-soft); }
.todo.zero { opacity: .5; cursor: default; }
.ti { font-size: 16px; } .tn { font-size: var(--fs-lg); font-weight: 800; } .tl { font-size: var(--fs-xs); color: var(--c-text-3); }
.t-tasks { width: 100%; border-collapse: collapse; font-size: var(--fs-sm); }
.t-tasks th, .t-tasks td { padding: 6px 8px; text-align: left; border-bottom: 1px solid var(--c-line); }
.t-tasks th { color: var(--c-text-3); font-weight: 600; }
.t-tasks .ok { color: var(--c-ok); } .t-tasks .warn { color: var(--c-warn); } .t-tasks .err { color: var(--c-err); }
.mono { font-family: var(--font-mono, monospace); color: var(--c-text-3); }
.pad { padding: 24px 0; text-align: center; }
@media (max-width: 860px) { .ops { grid-template-columns: repeat(2, 1fr); } .grid2 { grid-template-columns: 1fr; } .todo-grid { grid-template-columns: repeat(3, 1fr); } }

.dropzone { margin-bottom: var(--sp-4); border: 1.5px dashed var(--c-line-strong, #2a3040); border-radius: 14px; padding: 22px; text-align: center; color: var(--c-text-3); transition: all .15s; }
.dropzone.active { border-color: var(--c-primary); background: var(--c-surface-2); color: var(--c-primary); }
.dz-ico { display: block; font-size: 26px; margin-bottom: 6px; }
.dz-t { display: block; font-weight: 600; color: var(--c-text); }
.dz-s { display: block; font-size: var(--fs-xs); margin-top: 2px; }
</style>
