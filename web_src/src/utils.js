// 通用工具：格式化、封面占位、Toast 队列、确认框、防抖。
import { reactive } from 'vue'

export function esc(s) {
  return String(s == null ? '' : s)
}

export function fmtSize(bytes) {
  const b = Number(bytes) || 0
  if (b <= 0) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = b
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return v.toFixed(v >= 100 || i < 2 ? 0 : 1) + ' ' + u[i]
}

export function fmtDuration(sec) {
  sec = Number(sec) || 0
  if (sec <= 0) return '—'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = Math.floor(sec % 60)
  if (h > 0) return `${h}小时${m}分`
  if (m > 0) return `${m}分${s ? s + '秒' : ''}`
  return `${s}秒`
}

/** 播放器用 时:分:秒 */
export function fmtClock(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0))
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  const p = (n) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`
}

/** 分钟 → 简写（用于卡片） */
export function fmtMin(min) {
  const m = Number(min) || 0
  if (m <= 0) return ''
  const h = Math.floor(m / 60)
  return h > 0 ? `${h}h${m % 60 ? (m % 60) + 'm' : ''}` : `${m}m`
}

export function fmtDate(d) {
  if (!d) return ''
  return String(d).slice(0, 10)
}

/** 相对时间：3天前 */
export function fmtAgo(d) {
  if (!d) return ''
  const t = new Date(String(d).replace(' ', 'T')).getTime()
  if (!t) return fmtDate(d)
  const diff = (Date.now() - t) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 2592000) return `${Math.floor(diff / 86400)} 天前`
  if (diff < 31536000) return `${Math.floor(diff / 2592000)} 个月前`
  return `${Math.floor(diff / 31536000)} 年前`
}

export function fmtRating(r) {
  r = Number(r) || 0
  return r ? '★'.repeat(Math.round(r)) + '☆'.repeat(5 - Math.round(r)) : '未评分'
}

export function fmtNum(n) {
  const v = Number(n) || 0
  return v >= 10000 ? (v / 10000).toFixed(1) + 'w' : String(v)
}

// 封面加载失败占位（内联 SVG，避免破图）
export const COVER_PLACEHOLDER =
  "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='300' height='450'%3E%3Crect width='100%25' height='100%25' fill='%23181c26'/%3E%3Ctext x='50%25' y='50%25' fill='%23515b70' font-family='sans-serif' font-size='16' text-anchor='middle' dominant-baseline='middle'%3ENO COVER%3C/text%3E%3C/svg%3E"

export const AVATAR_PLACEHOLDER =
  "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Crect width='100%25' height='100%25' fill='%23181c26'/%3E%3Ccircle cx='100' cy='78' r='34' fill='%232a3040'/%3E%3Cpath d='M40 190c0-33 27-60 60-60s60 27 60 60z' fill='%232a3040'/%3E%3C/svg%3E"

export function coverFallback(e) {
  const img = e && e.target ? e.target : e
  if (!img || img.dataset.fb) return
  img.dataset.fb = '1'
  img.src = COVER_PLACEHOLDER
}

export function avatarFallback(e) {
  const img = e && e.target ? e.target : e
  if (!img || img.dataset.fb) return
  img.dataset.fb = '1'
  img.src = AVATAR_PLACEHOLDER
}

/* ---------------- Toast 队列 ---------------- */
export const toasts = reactive([])
let toastSeq = 0

export function toast(msg, type = '', ms = 3000) {
  const id = ++toastSeq
  toasts.push({ id, msg: String(msg), type })
  setTimeout(() => {
    const i = toasts.findIndex((t) => t.id === id)
    if (i >= 0) toasts.splice(i, 1)
  }, ms)
  return id
}

/* ---------------- 确认框 ---------------- */
export const confirmState = reactive({
  open: false, title: '', desc: '', okText: '确定', danger: false, _resolve: null,
})

export function confirmDialog(title, desc = '', opts = {}) {
  confirmState.title = title
  confirmState.desc = desc
  confirmState.okText = opts.okText || '确定'
  confirmState.danger = !!opts.danger
  confirmState.open = true
  return new Promise((resolve) => { confirmState._resolve = resolve })
}

export function resolveConfirm(v) {
  confirmState.open = false
  const r = confirmState._resolve
  confirmState._resolve = null
  if (r) r(v)
}

/* ---------------- 其他 ---------------- */
export function debounce(fn, ms = 260) {
  let t = null
  return (...args) => {
    clearTimeout(t)
    t = setTimeout(() => fn(...args), ms)
  }
}

export function copyText(text) {
  try {
    navigator.clipboard.writeText(String(text))
    toast('已复制', 'ok')
  } catch (e) {
    toast('复制失败', 'err')
  }
}

/** 分辨率 → 画质标签 */
export function qualityTag(resolution) {
  if (!resolution) return ''
  const m = String(resolution).match(/(\d+)\s*[x×]\s*(\d+)/)
  if (!m) return ''
  const h = Math.max(Number(m[1]), Number(m[2])) >= 3800 ? 2160 : Number(m[2])
  if (h >= 2000) return '4K'
  if (h >= 1000) return '1080P'
  if (h >= 700) return '720P'
  return 'SD'
}
