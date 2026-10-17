// 通用工具：格式化、封面占位、Toast 队列、确认框、防抖。
import { reactive } from 'vue'
import { t } from './i18n/index.js'

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
  if (h > 0) return t('fmt.durationH', { h, m })
  if (m > 0) return t('fmt.durationM', { m, s })
  return t('fmt.durationS', { s })
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

/**
 * 把后端的各种时间写法统一成毫秒时间戳。
 *
 * 后端（会话 / 审计 / API Key 的 last_used）直接给的是 **秒级 epoch**（如 1792231258.15），
 * 而早期实现只按日期字符串解析 —— `new Date("1792231258.15")` 是 Invalid Date，
 * 于是回退到截断字符串，界面上就出现了裸时间戳。
 * 这里统一兼容：秒级 / 毫秒级数字、ISO 字符串、`YYYY-MM-DD HH:mm:ss`。
 */
export function toMs(d) {
  if (d === null || d === undefined || d === '') return 0
  const s = String(d).trim()
  if (/^\d+(\.\d+)?$/.test(s)) {
    const n = Number(s)
    if (!n) return 0
    // 小于 1e11 视为秒级（毫秒级时间戳在 1973 年以前才会低于这个量级）
    return n < 1e11 ? Math.round(n * 1000) : Math.round(n)
  }
  const ts = new Date(s.replace(' ', 'T')).getTime()
  return Number.isFinite(ts) ? ts : 0
}

/** 绝对时间（本地时区）：用于 title 提示，如 2026-10-17 18:49 */
export function fmtDateTime(d) {
  const ts = toMs(d)
  if (!ts) return d ? String(d) : ''
  const dt = new Date(ts)
  const p = (n) => String(n).padStart(2, '0')
  return `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())} ${p(dt.getHours())}:${p(dt.getMinutes())}`
}

/**
 * 把容器里看到的宿主机路径美化：`/d/迅雷下载` → `D:\迅雷下载`。
 * Docker Desktop 的挂载源在 mountinfo 里就是这种形式；Linux 原生路径原样返回。
 */
export function prettyHostPath(root) {
  const s = String(root || '')
  const m = s.match(/^\/([a-zA-Z])\/(.*)$/)
  if (!m) return s
  return m[1].toUpperCase() + ':\\' + m[2].replace(/\//g, '\\')
}

/** 相对时间：3天前（兼容秒级时间戳） */
export function fmtAgo(d) {
  const ts = toMs(d)
  if (!ts) return d ? fmtDate(d) : ''
  const diff = (Date.now() - ts) / 1000
  if (diff < 60) return t('fmt.justNow')
  if (diff < 3600) return t('fmt.minutesAgo', { n: Math.floor(diff / 60) })
  if (diff < 86400) return t('fmt.hoursAgo', { n: Math.floor(diff / 3600) })
  if (diff < 2592000) return t('fmt.daysAgo', { n: Math.floor(diff / 86400) })
  if (diff < 31536000) return t('fmt.monthsAgo', { n: Math.floor(diff / 2592000) })
  return t('fmt.yearsAgo', { n: Math.floor(diff / 31536000) })
}

export function fmtRating(r) {
  r = Number(r) || 0
  return r ? '★'.repeat(Math.round(r)) + '☆'.repeat(5 - Math.round(r)) : t('fmt.noRating')
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

const _recentToasts = new Map()
export function toast(msg, type = '', ms = 3000) {
  const key = String(msg) + '|' + type
  const now = Date.now()
  // 短时间内相同文案去重，避免后端离线时大量并行请求疯狂弹 toast
  if (_recentToasts.has(key) && now - _recentToasts.get(key) < 2500) return -1
  _recentToasts.set(key, now)
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
  open: false, title: '', desc: '', okText: t('common.confirm'), danger: false, _resolve: null,
})

export function confirmDialog(title, desc = '', opts = {}) {
  confirmState.title = title
  confirmState.desc = desc
  confirmState.okText = opts.okText || t('common.confirm')
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

/**
 * 设置浏览器标题为「品牌名 - 自定义后缀」。
 *
 * 后缀来自 privacy.site_subtitle，用于混淆：任务栏 / 标签页被旁人扫到时不起疑，
 * 是「紧急遮挡」之外的第二道隐私保护（见 docs/安全与隐私设计.md 的 L4）。
 */
export function setSiteTitle(subtitle) {
  const brand = t('nav.brand')
  const sub = String(subtitle == null ? '' : subtitle).trim()
  document.title = sub ? `${brand} - ${sub}` : brand
}

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
    toast(t('common.copied'), 'ok')
  } catch (e) {
    toast(t('common.copyFailed'), 'err')
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

/**
 * 把推荐的 reasons 数组转成一句人话（取权重最高的 1~2 条）。
 * t 为 i18n 翻译函数（作为参数传入，避免 utils 与 i18n 循环依赖）。
 */
export function recommendReason(reasons, t, max = 2) {
  const list = (reasons || []).filter((x) => x && x.kind)
  if (!list.length || typeof t !== 'function') return ''
  const one = (r) => {
    if (r.kind === 'genre') return t('swipe.why_genre', { name: r.name })
    if (r.kind === 'actress') return t('swipe.why_actress', { name: r.name })
    if (r.kind === 'studio') return t('swipe.why_studio', { name: r.name })
    if (r.kind === 'series') return t('swipe.why_series', { name: r.name })
    if (r.kind === 'runtime') return t('swipe.why_runtime')
    if (r.kind === 'year') return t('swipe.why_year')
    return ''
  }
  return list.slice(0, max).map(one).filter(Boolean).join(' · ')
}
