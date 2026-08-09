<script setup>
import { ref, onMounted, computed } from 'vue'
import { state } from '../../state.js'
import { useTasks, pct } from '../../composables/useTasks.js'
import { maintenanceSummary, scrapeTasks, rescanLocalCovers, reparseCodes } from '../../api.js'
import { toast } from '../../utils.js'
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
    { key: 'noscrape', label: '未刮削', n: s.noscrape || 0, flag: 'noscrape', icon: '⚑' },
    { key: 'missing_cover', label: '缺封面', n: s.missing_cover || 0, flag: 'nocover', icon: '▢' },
    { key: 'unrecognized', label: '无番号', n: s.unrecognized || 0, flag: 'nocode', icon: '?' },
    { key: 'split_incomplete', label: '分片不全', n: s.split_incomplete || 0, flag: null, icon: '⧉' },
    { key: 'duplicates', label: '疑似重复', n: s.duplicates || 0, flag: null, icon: '⧉' },
    { key: 'missing_files', label: '失效文件', n: s.missing_files || 0, flag: null, icon: '⛔' },
    { key: 'watchlist', label: '待观看', n: s.watchlist || 0, flag: 'watchlist', icon: '⌚' },
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
  const t = activeTasks.value[0]
  if (!t) return { p: 0, label: '空闲', ok: 0, miss: 0, error: 0 }
  return {
    p: pct(t),
    label: t.label === '刮削元数据' ? '刮削中' : '扫描中',
    ok: t.ok || 0, miss: t.miss || 0, error: (t.fail || 0) + (t.error_count || 0),
  }
})

const statCards = computed(() => {
  const s = summary.value || {}
  return [
    { label: '影片总数', value: s.total || 0 },
    { label: '待处理', value: (s.noscrape || 0) + (s.missing_cover || 0) + (s.unrecognized || 0), tone: 'warn' },
  ]
})

async function doScan() { busy.value = true; try { runScan({}); toast('已开始扫描媒体库', 'ok') } finally { busy.value = false } }
async function doScrape() { busy.value = true; try { runScrape({ missing_only: true }); toast('已开始刮削缺失项', 'ok') } finally { busy.value = false } }
async function doLocalCovers() { busy.value = true; try { await rescanLocalCovers(); toast('已触发本地封面扫描', 'ok') } finally { busy.value = false } }
async function doReparse() { busy.value = true; try { await reparseCodes({}); toast('已触发番号重解析', 'ok') } finally { busy.value = false } }

function onDrop(e) {
  e.preventDefault()
  dropActive.value = false
  const items = [...(e.dataTransfer?.files || [])]
  // 取首个文件/目录路径（浏览器仅暴露名称，真实路径需后端 watch 目录；此处触发全库扫描）
  runScan({})
  toast('已触发扫描媒体库', 'ok')
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
      <span class="dz-t">把影片文件夹拖到这里，松手即扫描入库</span>
      <span class="dz-s">也可直接点上方「扫描入库」按钮</span>
    </div>

    <!-- 操作台 -->
    <div class="ops">
      <button class="op" :disabled="anyRunning" @click="doScan">
        <span class="op-ico">🔍</span><span class="op-t">扫描入库</span>
        <span class="op-d">发现新文件 / 清理失效记录</span>
      </button>
      <button class="op" :disabled="anyRunning" @click="doScrape">
        <span class="op-ico">✨</span><span class="op-t">刮削缺失</span>
        <span class="op-d">为未刮削影片补充元数据</span>
      </button>
      <button class="op" :disabled="anyRunning" @click="doLocalCovers">
        <span class="op-ico">🖼️</span><span class="op-t">本地封面</span>
        <span class="op-d">从文件目录匹配封面图</span>
      </button>
      <button class="op" :disabled="anyRunning" @click="doReparse">
        <span class="op-ico">🔤</span><span class="op-t">重解析番号</span>
        <span class="op-d">按文件名校正番号识别</span>
      </button>
    </div>

    <div class="grid2">
      <!-- 进度 -->
      <section class="panel">
        <div class="panel-head">当前进度</div>
        <div class="panel-body prog-body">
          <div class="ring" :class="{ run: anyRunning }" :style="{ background: `conic-gradient(var(--c-primary) ${ring.p * 3.6}deg, var(--c-line) 0)` }">
            <div class="ring-in">
              <span class="rp tabular">{{ ring.p }}%</span>
              <span class="rl">{{ ring.label }}</span>
            </div>
          </div>
          <div class="prog-stats">
            <div class="ps"><b class="tabular">{{ ring.ok }}</b><span>成功</span></div>
            <div class="ps"><b class="tabular">{{ ring.miss }}</b><span>未命中</span></div>
            <div class="ps"><b class="tabular">{{ ring.error }}</b><span>失败</span></div>
            <p v-if="!anyRunning" class="muted sm">当前没有进行中的任务</p>
            <p v-else class="muted sm">点击顶部铃铛查看实时详情</p>
          </div>
        </div>
        <div v-if="summary?.last_scan" class="last-scan">
          上次扫描：<b class="tabular">{{ summary.last_scan.started_at }}</b>
          · 新增 <b class="tabular ok">{{ summary.last_scan.added }}</b>
          · 更新 <b class="tabular">{{ summary.last_scan.updated }}</b>
          · 清理 <b class="tabular warn">{{ summary.last_scan.removed }}</b>
        </div>
      </section>

      <!-- 待办 -->
      <section class="panel">
        <div class="panel-head">待办概览</div>
        <div class="panel-body">
          <div class="todo-grid">
            <button v-for="t in todos" :key="t.key" class="todo" :class="{ zero: !t.n }" @click="goTodo(t)">
              <span class="ti">{{ t.icon }}</span>
              <span class="tn tabular">{{ t.n }}</span>
              <span class="tl">{{ t.label }}</span>
            </button>
          </div>
        </div>
      </section>
    </div>

    <!-- 最近刮削任务 -->
    <section class="panel">
      <div class="panel-head">
        最近刮削任务
        <div class="spacer"></div>
        <button class="btn ghost tiny" @click="loadTasks">刷新</button>
      </div>
      <div class="panel-body">
        <div v-if="!tasks.length" class="muted pad">暂无刮削任务记录</div>
        <table v-else class="t-tasks">
          <thead><tr><th>开始时间</th><th>总数</th><th>成功</th><th>未命中</th><th>失败</th><th>平均耗时</th><th></th></tr></thead>
          <tbody>
            <tr v-for="t in tasks" :key="t.task_id">
              <td class="mono">{{ t.started_at }}</td>
              <td class="tabular">{{ t.total }}</td>
              <td class="tabular ok">{{ t.ok }}</td>
              <td class="tabular warn">{{ t.miss }}</td>
              <td class="tabular err">{{ t.error }}</td>
              <td class="tabular">{{ t.avg_ms }}ms</td>
              <td><button class="btn ghost tiny" @click="state.maintTab='logs'">查看日志</button></td>
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
