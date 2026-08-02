import { computed } from 'vue'
import { state } from '../state.js'
import { scanStatus, scrapeStatus, startScan, startScrape, cancelScan, cancelScrape } from '../api.js'
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
    total,
    done,
    message: r.message || r.current_file || r.phase || '',
    phase: r.phase || '',
    ok: Number(r.ok ?? r.success) || 0,
    fail: Number(r.fail ?? r.failed) || 0,
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

  // 任务结束时提示并广播刷新
  if (wasScanning && !scanning) {
    toast('扫描已完成', 'ok')
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  }
  if (wasScraping && !scraping) {
    const { ok, fail } = state.task.scrape
    toast(`刮削完成${ok || fail ? `：成功 ${ok} · 失败 ${fail}` : ''}`, 'ok')
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  }
  wasScanning = scanning
  wasScraping = scraping

  schedule(scanning || scraping ? 900 : 5000)
}

function schedule(ms) {
  clearTimeout(timer)
  timer = setTimeout(poll, ms)
}

export function useTasks() {
  const anyRunning = computed(() => state.task.scan.running || state.task.scrape.running)

  const activeTasks = computed(() => {
    const out = []
    if (state.task.scan.running) out.push({ key: 'scan', label: '扫描媒体库', ...state.task.scan })
    if (state.task.scrape.running) out.push({ key: 'scrape', label: '刮削元数据', ...state.task.scrape })
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
    try {
      await (key === 'scan' ? cancelScan() : cancelScrape())
      toast('已请求取消', 'ok')
      schedule(300)
    } catch (e) { toast(e.message, 'err') }
  }

  return { anyRunning, activeTasks, overallPct, start, stop, runScan, runScrape, abort }
}

export function pct(t) {
  if (!t) return 0
  // 枚举阶段：总数未知且 done 尚未累积，用 -1 表示「不确定进度」（前端渲染流动条纹）
  if (t.phase === 'enumerating') return -1
  return t.total > 0 ? Math.min(100, Math.round((t.done / t.total) * 100)) : 0
}
