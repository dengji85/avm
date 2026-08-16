import { reactive, watch } from 'vue'
import { t } from './i18n'

const PREF_KEY = 'avm.prefs.v2'

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREF_KEY) || '{}') } catch (e) { return {} }
}
const saved = loadPrefs()

/** 全局响应式状态 */
export const state = reactive({
  // ---- 路由 / 视图 ----
  view: 'home',

  // ---- 维护中心子标签 ----
  maintTab: 'overview',

  // ---- 影片查询条件 ----
  q: '',
  actress: [],
  genre: [],
  tag: [],
  studio: '',
  series: '',
  prefix: '',
  year: null,
  minRating: 0,
  flags: [],
  sort: saved.sort || 'added_desc',
  page: 1,
  page_size: saved.page_size || 60,
  multiOp: saved.multiOp || 'OR',

  // ---- 分面 ----
  facets: {},
  facetFilter: {},

  // ---- 选择模式 ----
  selMode: false,
  selected: new Set(),

  // ---- 详情 ----
  currentId: null,
  // 从详情页点击筛选条件跳转时，记录来源影片，便于在影片库一键返回
  returnFromFilter: null,   // { id, title }

  // ---- 女优 ----
  actFollowOnly: false,
  actressCurrent: null,
  actressCurrentId: null,
  actressPage: 1,
  actressReturnView: 'actress',

  // ---- 滑动评分 ----
  swipeList: [],
  swipeIdx: 0,

  // ---- 配置 ----
  config: null,
  browsePath: '',

  // ---- 界面偏好（持久化） ----
  theme: saved.theme || 'dark',
  density: saved.density || 'cozy',
  cardSize: saved.cardSize || 'normal',   // dense | normal | large
  sidebarCollapsed: !!saved.sidebarCollapsed,
  mobileNavOpen: false,

  // ---- 上次加入的片单（快速加片单/队列用）----
  lastCollection: saved.lastCollection || 0,

  // ---- 后台任务 ----
  // task.* 保留「当前/最近一次」任务的实时快照（完成后不清空，供即时查看）
  task: {
    scan: { running: false, done: 0, total: 0, message: '', phase: '', elapsed: 0, cancelled: false, ok: 0, fail: 0, logs: [], counters: {} },
    scrape: { running: false, done: 0, total: 0, message: '', phase: '', elapsed: 0, cancelled: false, ok: 0, fail: 0, logs: [], counters: {} },
  },
  // 任务历史（已完成的任务存档，任务结束后失败数据仍可回看）
  taskHistory: [],
  taskPanelOpen: false,
})

/* 偏好持久化 */
watch(
  () => ({
    theme: state.theme, density: state.density, cardSize: state.cardSize,
    sidebarCollapsed: state.sidebarCollapsed, sort: state.sort,
    page_size: state.page_size, multiOp: state.multiOp, lastCollection: state.lastCollection,
  }),
  (v) => { try { localStorage.setItem(PREF_KEY, JSON.stringify(v)) } catch (e) {} },
  { deep: true },
)

/* ---------------- URL hash 路由（刷新/分享/前进后退 保持当前视图） ----------------
 * 规则：#/<view>                普通视图
 *       #/maintenance/<tab>     维护中心并定位到指定子 tab
 * 仅在合法视图 id 内生效，非法 hash 退回默认 home。
 */
const VALID_VIEWS = ['home', 'gallery', 'stats', 'maintenance', 'actress',
  'collections', 'rankings', 'swipe', 'settings']
const VALID_MAINT = ['overview', 'storage', 'logs']

function parseHash() {
  const raw = (location.hash || '').replace(/^#\/?/, '')
  if (!raw) return null
  const [view, sub] = raw.split('/')
  if (!VALID_VIEWS.includes(view)) return null
  return { view, sub }
}

function applyHashToState() {
  const r = parseHash()
  if (!r) return
  state.view = r.view
  if (r.view === 'maintenance' && r.sub && VALID_MAINT.includes(r.sub)) {
    state.maintTab = r.sub
  }
}

function stateToHash() {
  if (state.view === 'maintenance' && state.maintTab && VALID_MAINT.includes(state.maintTab)) {
    return `#/maintenance/${state.maintTab}`
  }
  return `#/${state.view}`
}

// 1) 启动即根据当前 URL 还原视图（在 state 初始化后调用一次）
applyHashToState()

// 2) 视图/子标签变化 → 写回 hash（不触发 hashchange 回环，因值一致时浏览器不派发）
watch(
  () => [state.view, state.maintTab],
  () => { if (VALID_VIEWS.includes(state.view) && location.hash !== stateToHash()) location.hash = stateToHash() },
)

// 3) 浏览器前进/后退 → 同步回 state
window.addEventListener('hashchange', applyHashToState)

/* 主题 / 密度应用到 <html> */
export function applyTheme() {
  const el = document.documentElement
  el.setAttribute('data-theme', state.theme)
  el.setAttribute('data-density', state.density)
  // 关键：让原生表单控件（input/select/textarea/滚动条）按主题配色渲染，
  // 否则暗色下浏览器会用浅色控件配色，覆盖自定义 background/color，导致表单文字看不清
  el.style.colorScheme = state.theme === 'light' ? 'light' : 'dark'
}
watch(() => [state.theme, state.density], applyTheme)

/** 重置全部筛选条件 */
export function resetFilters() {
  state.q = ''
  state.actress = []
  state.genre = []
  state.studio = ''
  state.series = ''
  state.prefix = ''
  state.year = null
  state.flags = []
  state.tag = []
  state.page = 1
  state.returnFromFilter = null
}

/** 当前是否有任何激活筛选 */
export function hasActiveFilter() {
  return !!(state.q || state.actress.length || state.genre.length || state.studio ||
            state.series || state.prefix || state.year || state.flags.length)
}

/* ---------------- 常量 ---------------- */

export const FLAGS = [
  ['favorite', 'flag.favorite', '♥'], ['watchlist', 'flag.watchlist', '⌚'],
  ['subtitle', 'flag.subtitle', 'flag.subtitleShort'], ['uncensored', 'flag.uncensored', 'flag.uncensoredShort'],
  ['hd4k', 'flag.hd4k', '4K'], ['vr', 'flag.vr', 'VR'],
  ['unwatched', 'flag.unwatched', '○'], ['watched', 'flag.watched', '●'],
  ['nocover', 'flag.nocover', '▢'], ['noscrape', 'flag.noscrape', '⚑'], ['nocode', 'flag.nocode', '?'],
]

export const FACET_KINDS = [
  ['actresses', 'actress', true, 'facet.actresses'],
  ['genres', 'genre', true, 'facet.genres'],
  ['tags', 'tag', true, 'facet.tags'],
  ['studios', 'studio', false, 'facet.studios'],
  ['series', 'series', false, 'facet.series'],
  ['prefixes', 'prefix', false, 'facet.prefixes'],
  ['years', 'year', false, 'facet.years'],
]

export const SORTS = [
  ['added_desc', 'sort.added_desc'], ['added_asc', 'sort.added_asc'],
  ['release_desc', 'sort.release_desc'], ['release_asc', 'sort.release_asc'],
  ['rating_desc', 'sort.rating_desc'], ['title_asc', 'sort.title_asc'],
  ['code_asc', 'sort.code_asc'], ['size_desc', 'sort.size_desc'],
  ['duration_desc', 'sort.duration_desc'], ['played_desc', 'sort.played_desc'],
  ['random', 'sort.random'],
]

/* 内联 SVG 图标（24x24 viewBox，fill=currentColor）实心面性图标，小尺寸更醒目 */
export const NAV_ICONS = {
  gallery: 'M5 6a1 1 0 0 0-1 1v4a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7a1 1 0 0 0-1-1H5zm9 0a1 1 0 0 0-1 1v4a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7a1 1 0 0 0-1-1h-5zM5 13a1 1 0 0 0-1 1v4a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1v-4a1 1 0 0 0-1-1H5zm9 0a1 1 0 0 0-1 1v4a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1v-4a1 1 0 0 0-1-1h-5z',
  actress: 'M12 2C9.24 2 7 4.24 7 7s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zm0 8c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3zM5 18c0-2.76 4.5-4 7-4s7 1.24 7 4v2H5v-2z',
  collections: 'M4 7a1 1 0 0 1 1-1h14a1 1 0 1 1 0 2H5a1 1 0 0 1-1-1zm0 5a1 1 0 0 1 1-1h14a1 1 0 1 1 0 2H5a1 1 0 0 1-1-1zm0 5a1 1 0 0 1 1-1h14a1 1 0 1 1 0 2H5a1 1 0 0 1-1-1z',
  rankings: 'M19 5h-2c-.55 0-1 .45-1 1v8c0 .55.45 1 1 1h2c.55 0 1-.45 1-1V6c0-.55-.45-1-1-1zM5 10H3c-.55 0-1 .45-1 1v3c0 .55.45 1 1 1h2c.55 0 1-.45 1-1v-3c0-.55-.45-1-1-1zm8-5h-2c-.55 0-1 .45-1 1v8c0 .55.45 1 1 1h2c.55 0 1-.45 1-1V6c0-.55-.45-1-1-1z',
  swipe: 'M6.59 6.17a1 1 0 0 0-1.42 1.42L7.17 9.59 3.29 13.46a3 3 0 0 0 0 4.25l.17.17a3 3 0 0 0 4.25 0l2.29-2.29V19a1 1 0 1 0 2 0v-3.59a3 3 0 0 0-.88-2.12L9.83 11l1.42-1.41-4.66-3.42zm10.82 0l4.66 3.42-1.42 1.41 2.29 2.29a3 3 0 0 1 .88 2.12V19a1 1 0 1 1-2 0v-3.41l-2.29 2.29a3 3 0 0 1-4.25 0l-.17-.17a3 3 0 0 1 0-4.25l3.88-3.87-1.42-1.42a1 1 0 0 1 1.42-1.42z',
  stats: 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm.75-13h-1.5v6l5.25 3.15.75-1.23-4.5-2.67V7z',
  storage: 'M12 2L2 7l10 5 10-5-10-5zm0 13.09L4.55 11 3 11.82l9 4.5 9-4.5-1.55-.82L12 15.09zM3 15.18V20l9 4 9-4v-4.82l-9 4.5-9-4.5z',
  scrapelogs: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z',
  maintenance: 'M22.7 19l-9.1-9.1c.9-2.3.4-5-1.5-6.9-2-2-5-2.4-7.4-1.3L9 6 6 9 1.6 4.7C.4 7.1.9 10.1 2.9 12.1c1.9 1.9 4.6 2.4 6.9 1.5l9.1 9.1c.4.4 1 .4 1.4 0l2.9-2.9c.4-.4.4-1 0-1.4z',
  settings: 'M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.488.488 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 0 0-.48-.41h-3.84a.484.484 0 0 0-.48.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.488.488 0 0 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.27.41.48.41h3.84c.24 0 .44-.17.48-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z',
}

/* 顶部主导航（Tab）：首页 / 影片库 / 统计 / 维护中心 */
export const NAV_TABS = [
  { id: 'home', label: 'nav.home', icon: 'home' },
  { id: 'gallery', label: 'nav.gallery', icon: 'gallery' },
  { id: 'collections', label: 'nav.collections', icon: 'collections' },
  { id: 'stats', label: 'nav.stats', icon: 'stats' },
  { id: 'maintenance', label: 'nav.maintenance', icon: 'maintenance' },
]

/* 次级入口（仍可通过顶栏菜单/路由直达，不在主 Tab 强暴露） */
export const NAV_SECONDARY = [
  { id: 'actress', label: 'nav.actress' },
  { id: 'collections', label: 'nav.collections' },
  { id: 'rankings', label: 'nav.rankings' },
  { id: 'swipe', label: 'nav.swipe' },
  { id: 'settings', label: 'nav.settings' },
]

/* 侧边栏分组导航（Sidebar.vue 使用，注意字段为 id/label/icon） */
export const NAV_GROUPS = [
  { title: 'navGroup.browse', items: [
    { id: 'home', label: 'nav.home', icon: 'gallery' },
    { id: 'gallery', label: 'nav.gallery', icon: 'gallery' },
    { id: 'actress', label: 'nav.actress', icon: 'actress' },
    { id: 'collections', label: 'nav.collections', icon: 'collections' },
    { id: 'rankings', label: 'nav.rankings', icon: 'rankings' },
    { id: 'swipe', label: 'nav.swipe', icon: 'swipe' },
  ] },
  { title: 'navGroup.maintenance', items: [
    { id: 'maintenance', label: 'nav.maintenance', icon: 'maintenance' },
    { id: 'settings', label: 'nav.settings', icon: 'settings' },
  ] },
]



