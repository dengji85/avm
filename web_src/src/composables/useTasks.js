import { computed } from 'vue'
import { state } from '../state.js'
import { scanStatus, scrapeStatus, startScan, startScrape, cancelScan, cancelScrape, scrapeLogs } from '../api.js'
import { toast } from '../utils.js'

let timer = null
let wasScanning = false
let wasScraping = false

function normalize(raw) {
  const r = raw || {}
  const total = Number(r.total) || 0
  const done = Number(r.done ?? r.current ?? r.processed) || 0
  return {
    running: !!r.running,
    cancelled: !!r.cancelled,
    phase: r.phase || '',
    total,
    done,
    message: r.message || r.current || r.current_file || '',
    current: r.current || '',
    ok: Number(r.ok ?? r.success) || 0,
    fail: Number(r.fail ?? r.failed) || 0,
    elapsed: Number(r.elapsed) || 0,
    error_count: Number(r.error_count) || 0,
    counters: (r.counters && typeof r.counters === 'object') ? r.counters : {},
    logs: Array.isArray(r.logs) ? r.logs : [],
    task_id: r.task_id || '',
  }
}

async function poll() {
  try {
    const [sc, sp] = await Promise.all([
      scanStatus().catch(() => null),
      scrapeStatus().catch(() => null),
    ])
    if (sc) Object.assign(state.task.scan, normalize(sc))
    if (sp) Object.assign(state.task.scrape, normalize(sp))
  } catch (e) { /* 静默 */ }

  const scanning = state.task.scan.running
  const scraping = state.task.scrape.running

  // 任务从「无 → 有」开始时自动展开任务中心（无论谁触发）
  if ((!wasScanning && scanning) || (!wasScraping && scraping)) {
    state.taskPanelOpen = true
  }

  // 任务结束时：存档进历史（失败数据可回看）+ 提示 + 广播刷新
  if (wasScanning && !scanning) {
    archiveTask('scan', '扫描媒体库')
    toast('扫描已完成', 'ok')
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  }
  if (wasScraping && !scraping) {
    archiveTask('scrape', '刮削元数据')
    const { ok, fail } = state.task.scrape
    toast(`刮削完成${ok || fail ? `：成功 ${ok} · 失败 ${fail}` : ''}`, 'ok')
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  }
  wasScanning = scanning
  wasScraping = scraping

  schedule(scanning || scraping ? 900 : 5000)
}

// 将刚结束的任务快照存入历史（最多保留 20 条，最新的在前）
function archiveTask(key, label) {
  const t = state.task[key]
  if (!t) return
  // 复用 buildOne 派生 logCounts / reasons，供历史条目展示失败统计
  const derived = buildOne(key, label, t)
  const snapshot = {
    id: `${key}-${Date.now()}`,
    key,
    label,
    finishedAt: Date.now(),
    cancelled: !!t.cancelled,
    total: t.total || 0,
    done: t.done || 0,
    ok: t.ok || 0,
    fail: t.fail || t.error_count || 0,
    phase: t.phase || 'done',
    elapsed: t.elapsed || 0,
    counters: { ...(t.counters || {}) },
    logs: (t.logs || []).slice(),
    logCounts: derived.logCounts,
    reasons: derived.reasons,
  }
  state.taskHistory.unshift(snapshot)
  if (state.taskHistory.length > 20) state.taskHistory.length = 20
  // 注意：不清除 state.task[key]，保留为「最近一次」供面板即时展示
}

function clearHistory() {
  state.taskHistory = []
}

function schedule(ms) {
  clearTimeout(timer)
  timer = setTimeout(poll, ms)
}

export function useTasks() {
  const anyRunning = computed(() => state.task.scan.running || state.task.scrape.running)

  const activeTasks = computed(() => {
    const out = []
    if (state.task.scan.running) out.push(buildOne('scan', '扫描媒体库', state.task.scan))
    if (state.task.scrape.running) out.push(buildOne('scrape', '刮削元数据', state.task.scrape))
    return out
  })

  const overallPct = computed(() => {
    const list = activeTasks.value
    if (!list.length) return 0
    const t = list.reduce((s, x) => s + (x.total || 0), 0)
    const d = list.reduce((s, x) => s + (x.done || 0), 0)
    return t > 0 ? Math.min(100, Math.round((d / t) * 100)) : 0
  })

  function start() { schedule(0) }
  function stop() { clearTimeout(timer) }

  async function runScan(body = {}) {
    try {
      await startScan(body)
      state.task.scan.running = true
      state.taskPanelOpen = true
      toast('已开始扫描', 'ok')
      schedule(400)
    } catch (e) { toast(e.message, 'err') }
  }

  async function runScrape(body = {}) {
    try {
      await startScrape(body)
      state.task.scrape.running = true
      state.taskPanelOpen = true
      toast('已开始刮削', 'ok')
      schedule(400)
    } catch (e) { toast(e.message, 'err') }
  }

  async function abort(key) {
    // 乐观更新：立即给出反馈，避免「点了没反应」的错觉（后端正在停止在途 worker）
    const t = state.task[key]
    if (t) {
      t.running = false
      t.cancelled = true
      t.message = '正在取消…'
    }
    try {
      await (key === 'scan' ? cancelScan() : cancelScrape())
      toast('已请求取消', 'ok')
      schedule(200)
    } catch (e) { toast(e.message, 'err') }
  }

  // 最近一次完成/结束的任务（供面板即时展示，无需点开历史）
  const lastFinished = computed(() => {
    const list = []
    for (const key of ['scan', 'scrape']) {
      const t = state.task[key]
      if (t && !t.running && (t.total || t.done || t.logs?.length)) {
        list.push(buildOne(key, key === 'scan' ? '扫描媒体库' : '刮削元数据', t))
      }
    }
    return list
  })

  return {
    anyRunning, activeTasks, overallPct, lastFinished, taskHistory: computed(() => state.taskHistory),
    start, stop, runScan, runScrape, abort, clearHistory,
  }
}

// 统一的任务对象构造（进行中 / 最近完成 共用）
function buildOne(key, label, t) {
  const logs = Array.isArray(t.logs) ? t.logs : []
  let err = 0, miss = 0
  for (const l of logs) {
    if (l.level === 'error') err++
    else if (l.level === 'warn') miss++
  }
  const reasons = Object.entries(t.counters?.reasons || {})
    .map(([name, n]) => ({ name, n, cls: String(name).startsWith('异常') ? 'err' : 'warn' }))
    .sort((a, b) => b.n - a.n)
    .slice(0, 4)
  return { key, label, ...t, logCounts: { error: err, miss }, reasons }
}

// 拉取某次刮削任务的逐文件日志（持久化 DB 查询），供历史展开查看
export async function fetchScrapeLogs(taskId, { status = '', code = '', page = 1, size = 50 } = {}) {
  if (!taskId) return { items: [], total: 0, page, size, pages: 0 }
  try {
    return await scrapeLogs({ task_id: taskId, status, code, page, size })
  } catch (e) {
    return { items: [], total: 0, page, size, pages: 0 }
  }
}

export function pct(t) {
  if (!t) return 0
  // 枚举阶段：总数未知且 done 尚未累积，用 -1 表示「不确定进度」（前端渲染流动条纹）
  if (t.phase === 'enumerating') return -1
  return t.total > 0 ? Math.min(100, Math.round((t.done / t.total) * 100)) : 0
}

const PHASE_LABEL = {
  idle: '空闲',
  starting: '启动中',
  enumerating: '枚举中',
  scanning: '扫描中',
  scraping: '刮削中',
  matching: '匹配中',
  fetching: '抓取中',
  done: '已完成',
}

export function phaseLabel(t) {
  if (!t) return ''
  if (!t.running && t.cancelled) return '已取消'
  if (!t.running) return t.phase === 'done' ? '已完成' : '已结束'
  return PHASE_LABEL[t.phase] || '运行中'
}

// 预计剩余时间（秒）：根据已用时间 / 已完成数估算
export function etaSec(t) {
  if (!t || !t.running || !t.done || !t.elapsed) return null
  const rate = t.done / t.elapsed          // 个/秒
  const remain = Math.max(0, (t.total || 0) - t.done)
  if (rate <= 0) return null
  return Math.round(remain / rate)
}

export function fmtDur(s) {
  s = Math.max(0, Math.round(s || 0))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const r = s % 60
  if (m < 60) return r ? `${m}m${r}s` : `${m}m`
  const h = Math.floor(m / 60)
  const rm = m % 60
  return rm ? `${h}h${rm}m` : `${h}h`
}
