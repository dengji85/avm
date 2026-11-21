import { computed } from 'vue'
import { state } from '../state.js'
import { scanStatus, scrapeStatus, startScan, startScrape, cancelScan, cancelScrape, scrapeLogs, actressFetchStatus, actressFetchCancel, subScanState, subScanCancel, subProbeState, subProbeCancel } from '../api.js'
import { toast } from '../utils.js'
import { t } from '../i18n/index.js'

let timer = null
let wasScanning = false
let wasScraping = false
let wasActressFetching = false
// 订阅扫描 / 探测：独立一档，只做全局可见 + 可取消，不进历史、不参与总进度
let subFinishedAt = 0
let subPolledAt = 0
//: 订阅状态轮询间隔（毫秒）：空闲时 15s 一次就够，运行中跟随主轮询
const SUB_IDLE_POLL = 15000
//: 结束后仍显示多久（毫秒）
const SUB_SHOW_DONE = 5 * 60 * 1000

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
    // 0 结果时后端给的排查线索（/api/scan/status），供起步清单渲染具体下一步
    hint: r.hint || null,
  }
}

async function poll() {
  try {
    const [sc, sp, af] = await Promise.all([
      scanStatus().catch(() => null),
      scrapeStatus().catch(() => null),
      actressFetchStatus().catch(() => null),
    ])
    if (sc) Object.assign(state.task.scan, normalize(sc))
    if (sp) Object.assign(state.task.scrape, normalize(sp))
    if (af) Object.assign(state.task.actress_fetch, normalize(af))
  } catch (e) { /* 静默 */ }

  // 订阅扫描 / 探测：全局后台任务（定时器也会触发），空闲时低频拉取
  if (state.task.sub_update.running || Date.now() - subPolledAt >= SUB_IDLE_POLL) {
    subPolledAt = Date.now()
    try {
      const [ss, ps] = await Promise.all([
        subScanState().catch(() => null),
        subProbeState().catch(() => null),
      ])
      applySubUpdate(ss, ps)
      const su = state.task.sub_update
      // 结束超过 5 分钟就收起；这里主动翻 show，让 lastFinished 的 computed 能刷新
      if (!su.running && subFinishedAt && Date.now() - subFinishedAt >= SUB_SHOW_DONE) {
        su.show = false
      }
    } catch (e) { /* 静默 */ }
  }

  const scanning = state.task.scan.running
  const scraping = state.task.scrape.running
  const actressFetching = state.task.actress_fetch.running

  // 任务从「无 → 有」开始时自动展开任务中心（无论谁触发）
  if ((!wasScanning && scanning) || (!wasScraping && scraping) || (!wasActressFetching && actressFetching)) {
    state.taskPanelOpen = true
  }

  // 任务结束时：存档进历史（失败数据可回看）+ 提示 + 广播刷新
  if (wasScanning && !scanning) {
    archiveTask('scan', '扫描媒体库')
    toast(t('guide.scanDone'), 'ok')
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  }
  if (wasScraping && !scraping) {
    archiveTask('scrape', '刮削元数据')
    const { ok, fail } = state.task.scrape
    toast(`${t('guide.scrapeDone')}${ok || fail ? ` (${ok}/${fail})` : ''}`, 'ok')
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  }
  if (wasActressFetching && !actressFetching) {
    archiveTask('actress_fetch', '女优资料抓取')
    const { ok, fail } = state.task.actress_fetch
    toast(`${t('guide.actressDone')}${ok || fail ? ` (${ok}/${fail})` : ''}`, 'ok')
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  }
  wasScanning = scanning
  wasScraping = scraping
  wasActressFetching = actressFetching

  schedule(scanning || scraping || actressFetching || state.task.sub_update.running ? 900 : 5000)
}

/** 订阅扫描 / 探测 → 任务中心里的一条只读任务（无历史、无总进度）。 */
function applySubUpdate(ss, ps) {
  const scanRunning = !!(ss && ss.running)
  const probeRunning = !!(ps && ps.running)
  const task = state.task.sub_update
  const prev = !!task.running
  let kind = ''
  if (scanRunning) kind = 'scan'
  else if (probeRunning) kind = 'probe'
  // 刚结束：沿用之前盯着的那个，避免拿上一次探测的旧 message 顶替刚结束的扫描
  else if (prev && task.kind) kind = task.kind
  else if (ps && ps.finished_at) kind = 'probe'
  else if (ss && ss.finished_at) kind = 'scan'
  if (!kind) return
  const src = (kind === 'scan' ? ss : ps) || {}
  const running = scanRunning || probeRunning
  task.running = running
  // 只有「正在跑」或「本轮亲眼看到它结束」才展示；刷新页面不会翻出上次的旧任务
  if (running || prev) task.show = true
  task.kind = kind
  task.total = Number(src.total) || 0
  task.done = Number(src.done) || 0
  task.percent = Number(src.percent) || (task.total ? Math.round(task.done * 100 / task.total) : 0)
  task.message = src.message || ''
  // 扫描：新增 / 失败；探测：有资源 / 未知（站点不可达）
  task.ok = kind === 'scan' ? (Number(src.new) || 0) : (Number(src.found) || 0)
  task.fail = kind === 'scan' ? (Number(src.failed) || 0) : (Number(src.unknown) || 0)
  task.phase = running ? (kind === 'scan' ? 'scanning' : 'fetching') : 'done'
  if (running) {
    subFinishedAt = 0
  } else if (prev) {
    subFinishedAt = Date.now()
  }
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
  const anyRunning = computed(() => state.task.scan.running || state.task.scrape.running
    || state.task.actress_fetch.running || state.task.sub_update.running)

  const activeTasks = computed(() => {
    const out = []
    if (state.task.scan.running) out.push(buildOne('scan', '扫描媒体库', state.task.scan))
    if (state.task.scrape.running) out.push(buildOne('scrape', '刮削元数据', state.task.scrape))
    if (state.task.actress_fetch.running) out.push(buildOne('actress_fetch', '女优资料抓取', state.task.actress_fetch))
    // 订阅更新：定时器也会触发，所以独立显示在任务中心（不进历史）
    if (state.task.sub_update.running) out.push(buildOne('sub_update', t('tasks.subUpdate'), state.task.sub_update))
    return out
  })

  const overallPct = computed(() => {
    // 订阅更新不参与总进度：只有它一个在跑时，直接用它自己的百分比
    const list = activeTasks.value.filter(x => x.key !== 'sub_update')
    if (!list.length) {
      const s = state.task.sub_update
      return s.running ? Math.min(100, Number(s.percent) || 0) : 0
    }
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
      toast(t('guide.scanStarted'), 'ok')
      schedule(400)
      return true
    } catch (e) { toast(e.message, 'err'); return false }
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
      if (key === 'scan') await cancelScan()
      else if (key === 'scrape') await cancelScrape()
      else if (key === 'actress_fetch') await actressFetchCancel()
      else if (key === 'sub_update') {
        // 扫描 / 探测二选一：按当前类型取消对应任务
        if (state.task.sub_update.kind === 'probe') await subProbeCancel()
        else await subScanCancel()
      }
      toast('已请求取消', 'ok')
      schedule(200)
    } catch (e) { toast(e.message, 'err') }
  }

  // 最近一次完成/结束的任务（供面板即时展示，无需点开历史）
  const lastFinished = computed(() => {
    const list = []
    const labels = { scan: '扫描媒体库', scrape: '刮削元数据', actress_fetch: '女优资料抓取' }
    for (const key of ['scan', 'scrape', 'actress_fetch']) {
      const t = state.task[key]
      if (t && !t.running && (t.total || t.done || t.logs?.length)) {
        list.push(buildOne(key, labels[key], t))
      }
    }
    // 订阅更新结束后只保留 5 分钟：定时扫描每几小时一次，不适合长期占位
    const sub = state.task.sub_update
    if (sub.show) {
      list.push(buildOne('sub_update', t('tasks.subUpdate'), sub))
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
