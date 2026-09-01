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

  // ---- 首次启动引导 ----
  onboarded: false,   // true 表示引导已结束（遮罩关闭）

  // ---- 界面偏好（持久化） ----
  // 默认深色：媒体库主视觉偏暗，且深色主题对浏览器原生控件（如 file picker）
  // 也更友好。仅接受合法的 'light'，其余（空值/脏数据/旧版本遗留）一律回退深色，
  // 避免存档里残留异常值导致主题“静默失效”。
  theme: saved.theme === 'light' ? 'light' : 'dark',
  density: saved.density || 'cozy',
  cardSize: saved.cardSize || 'normal',   // dense | normal | large
  sidebarCollapsed: !!saved.sidebarCollapsed,
  mobileNavOpen: false,
  // 播放器偏好：auto=智能优先网络（失败回退系统） / web=始终网络 / external=始终系统
  playerMode: saved.playerMode || 'auto',
  // 历史 tab 行为偏好
  recentOpenPlaylist: saved.recentOpenPlaylist !== false,  // 进入历史是否自动打开播放列表（迅雷式），默认开
  recentAutoPlay: saved.recentAutoPlay !== false,          // 打开播放列表后是否自动播放，默认开

  // ---- 上次加入的片单（快速加片单/队列用）----
  lastCollection: saved.lastCollection || 0,

  // ---- 全局播放队列（跨视图保留，单一数据源）----
  // open: 是否显示播放器；queue: 队列项数组；idx: 顺序队列中的当前下标
  // order: sequential|shuffle；loop: 是否循环播放；source: 队列来源描述
  // cid: 所属片单 id（片单连播时用于续播记忆，否则为 0）
  playQueue: {
    open: false,
    queue: [],
    idx: 0,
    order: saved.playOrder || 'sequential',
    loop: saved.playLoop !== false,
    source: '',
    cid: 0,
    autoplay: true,   // 打开播放列表后是否自动播放（历史 tab 可关闭）
  },

  // ---- 后台任务 ----
  // task.* 保留「当前/最近一次」任务的实时快照（完成后不清空，供即时查看）
  task: {
    scan: { running: false, done: 0, total: 0, message: '', phase: '', elapsed: 0, cancelled: false, ok: 0, fail: 0, logs: [], counters: {} },
    scrape: { running: false, done: 0, total: 0, message: '', phase: '', elapsed: 0, cancelled: false, ok: 0, fail: 0, logs: [], counters: {} },
    actress_fetch: { running: false, done: 0, total: 0, message: '', phase: '', elapsed: 0, cancelled: false, ok: 0, fail: 0, logs: [], counters: {} },
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
    playOrder: state.playQueue.order, playLoop: state.playQueue.loop,
    playerMode: state.playerMode,
    recentOpenPlaylist: state.recentOpenPlaylist, recentAutoPlay: state.recentAutoPlay,
  }),
  (v) => { try { localStorage.setItem(PREF_KEY, JSON.stringify(v)) } catch (e) {} },
  { deep: true },
)

/* ---------------- 全局播放队列操作 ---------------- */
/** 打开一个队列进行播放：movies 为影片项数组，source 为来源描述，cid 为片单 id（可选） */
export function openPlayQueue(movies, source = '', startIdx = -1, cid = 0, autoplay = true) {
  const list = (movies || []).map((m) => ({
    id: m.id, code: m.code || m.title, title: m.title, cover: m.cover,
    playable: !!m.playable, progress_seconds: m.progress_seconds || 0,
    duration_seconds: m.duration_seconds || 0,
  }))
  if (!list.length) return
  // 默认从第一部可播且未看完的影片开始
  let s = startIdx >= 0 ? Math.min(startIdx, list.length - 1) : -1
  if (s < 0) {
    const f = list.findIndex((m) => m.playable && !(m.progress_finished))
    s = f >= 0 ? f : 0
  }
  state.playQueue.queue = list
  state.playQueue.idx = s
  state.playQueue.source = source || ''
  state.playQueue.cid = cid || 0
  state.playQueue.autoplay = autoplay !== false
  state.playQueue.open = true
}

/** 关闭全局播放器（保留队列与进度，方便再次打开续播） */
export function closePlayQueue() {
  state.playQueue.open = false
}

/**
 * 按播放偏好打开一部影片（网络播放时附带最近观看历史组成队列）：
 * - playerMode='external' → 始终用系统播放器
 * - 否则 → 打开全局网络播放器，队列 = [当前片] + [最近观看历史（去重、排除当前片）]，
 *   从当前片开始；auto 模式下播放失败由 VideoPlayer 内的「用系统播放器打开」按钮回退
 * 返回 'web' 或 'external'（实际采用的方式）
 */
export async function openMoviePlayer(movie) {
  if (!movie) return null
  if (state.playerMode === 'external') {
    const { playMovie } = await import('./api.js')
    await playMovie(movie.id)
    return 'external'
  }
  // 拉最近观看历史作为队列补充（优先排在当前片之后，方便连播/切换）
  let queue = [movie]
  try {
    const { getRecentWatched } = await import('./api.js')
    const r = await getRecentWatched({ page: 1, page_size: 20 })
    const hist = ((r && r.items) || []).filter((h) => h.id !== movie.id)
    queue = [movie, ...hist]
  } catch (e) { /* 历史拉取失败不影响当前片播放 */ }
  openPlayQueue(queue, '', 0)
  return 'web'
}

/** 完全清除播放队列 */
export function clearPlayQueue() {
  state.playQueue.open = false
  state.playQueue.queue = []
  state.playQueue.idx = 0
  state.playQueue.source = ''
}

/* ---------------- URL hash 路由（刷新/分享/前进后退 保持当前视图） ----------------
 * 规则：#/<view>                普通视图
 *       #/maintenance/<tab>     维护中心并定位到指定子 tab
 * 仅在合法视图 id 内生效，非法 hash 退回默认 home。
 */
const VALID_VIEWS = ['home', 'gallery', 'stats', 'maintenance', 'actress',
  'collections', 'rankings', 'swipe', 'settings', 'recent']
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
  return !!(state.q || state.actress.length || state.genre.length || state.tag.length ||
            state.studio || state.series || state.prefix || state.year || state.flags.length)
}

/**
 * 从影片卡片 / 详情点击标签快速筛选：
 * - kind: 'genre' | 'tag'
 * - 该标签已在筛选条件中则移除（切换），否则加入
 * - 跳到影片库视图展示筛选结果
 */
export function applyFacet(kind, name) {
  // kind: 'genre' | 'tag' | 'actress'
  let arr
  if (kind === 'tag') arr = state.tag
  else if (kind === 'actress') arr = state.actress
  else arr = state.genre
  const i = arr.indexOf(name)
  if (i >= 0) arr.splice(i, 1)
  else arr.push(name)
  // 以新引用触发响应式（useLibrary 的 watch 依赖 .slice()）
  if (kind === 'tag') state.tag = [...state.tag]
  else if (kind === 'actress') state.actress = [...state.actress]
  else state.genre = [...state.genre]
  state.view = 'gallery'
  state.page = 1
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
  home: 'M12 3l9 8h-2.2v10H15v-6H9v6H5.2V11H3l9-8zm0 2.3L6.2 9.5V19h1.6v-6h8.4v6h1.6V9.5L12 5.3z',
  gallery: 'M5 6a1 1 0 0 0-1 1v4a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7a1 1 0 0 0-1-1H5zm9 0a1 1 0 0 0-1 1v4a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7a1 1 0 0 0-1-1h-5zM5 13a1 1 0 0 0-1 1v4a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1v-4a1 1 0 0 0-1-1H5zm9 0a1 1 0 0 0-1 1v4a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1v-4a1 1 0 0 0-1-1h-5z',
  actress: 'M12 2C9.24 2 7 4.24 7 7s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zm0 8c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3zM5 18c0-2.76 4.5-4 7-4s7 1.24 7 4v2H5v-2z',
  collections: 'M4 7a1 1 0 0 1 1-1h14a1 1 0 1 1 0 2H5a1 1 0 0 1-1-1zm0 5a1 1 0 0 1 1-1h14a1 1 0 1 1 0 2H5a1 1 0 0 1-1-1zm0 5a1 1 0 0 1 1-1h14a1 1 0 1 1 0 2H5a1 1 0 0 1-1-1z',
  rankings: 'M19 5h-2c-.55 0-1 .45-1 1v8c0 .55.45 1 1 1h2c.55 0 1-.45 1-1V6c0-.55-.45-1-1-1zM5 10H3c-.55 0-1 .45-1 1v3c0 .55.45 1 1 1h2c.55 0 1-.45 1-1v-3c0-.55-.45-1-1-1zm8-5h-2c-.55 0-1 .45-1 1v8c0 .55.45 1 1 1h2c.55 0 1-.45 1-1V6c0-.55-.45-1-1-1z',
  swipe: 'M6.59 6.17a1 1 0 0 0-1.42 1.42L7.17 9.59 3.29 13.46a3 3 0 0 0 0 4.25l.17.17a3 3 0 0 0 4.25 0l2.29-2.29V19a1 1 0 1 0 2 0v-3.59a3 3 0 0 0-.88-2.12L9.83 11l1.42-1.41-4.66-3.42zm10.82 0l4.66 3.42-1.42 1.41 2.29 2.29a3 3 0 0 1 .88 2.12V19a1 1 0 1 1-2 0v-3.41l-2.29 2.29a3 3 0 0 1-4.25 0l-.17-.17a3 3 0 0 1 0-4.25l3.88-3.87-1.42-1.42a1 1 0 0 1 1.42-1.42z',
  stats: 'M11 2.05V13h10.95C21.45 6.4 16.6 1.55 11 2.05zm-2 0C3.55 3.32 0 8.5 0 14c0 5.52 4.48 10 10 10 5.5 0 10.68-3.55 11.95-9H9V2.05z',
  storage: 'M12 2L2 7l10 5 10-5-10-5zm0 13.09L4.55 11 3 11.82l9 4.5 9-4.5-1.55-.82L12 15.09zM3 15.18V20l9 4 9-4v-4.82l-9 4.5-9-4.5z',
  scrapelogs: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z',
  maintenance: 'M22.7 19l-9.1-9.1c.9-2.3.4-5-1.5-6.9-2-2-5-2.4-7.4-1.3L9 6 6 9 1.6 4.7C.4 7.1.9 10.1 2.9 12.1c1.9 1.9 4.6 2.4 6.9 1.5l9.1 9.1c.4.4 1 .4 1.4 0l2.9-2.9c.4-.4.4-1 0-1.4z',
  settings: 'M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.488.488 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 0 0-.48-.41h-3.84a.484.484 0 0 0-.48.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.488.488 0 0 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.27.41.48.41h3.84c.24 0 .44-.17.48-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z',
  yearReview: 'M19 4h-1V2h-2v2H8V2H6v2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 16H5V10h14v10zM5 8V6h14v2H5z',
  recent: 'M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zM12.5 7H11v6l5.25 3.15.75-1.23-4.5-2.67z',
  shuffle: 'M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z',
}

/* 顶部主导航（Tab）：核心内容用文字，最近观看用图标，其余收进「更多」hover 菜单 */
export const NAV_TABS = [
  { id: 'home', label: 'nav.home', icon: 'home', text: true },
  { id: 'gallery', label: 'nav.gallery', icon: 'gallery', text: true },
  { id: 'collections', label: 'nav.collections', icon: 'collections', text: true },
  // 最近观看：图标（省空间）
  { id: 'recent', label: 'nav.recent', icon: 'recent' },
]

/* 「更多」下拉（hover 显示）：系统 + 探索（含统计/维护） */
export const NAV_MORE = [
  { key: 'system', title: 'navGroup.system', items: [
    { id: 'stats', label: 'nav.stats', icon: 'stats' },
    { id: 'maintenance', label: 'nav.maintenance', icon: 'maintenance' },
    { id: 'settings', label: 'nav.settings', icon: 'settings' },
  ] },
  { key: 'explore', title: 'navGroup.browse', items: [
    { id: 'actress', label: 'nav.actress', icon: 'actress' },
    { id: 'rankings', label: 'nav.rankings', icon: 'rankings' },
    { id: 'yearReview', label: 'nav.yearReview', icon: 'yearReview' },
    { id: 'swipe', label: 'nav.swipe', icon: 'swipe' },
  ] },
]

/* 次级入口（兼容旧引用，功能已由 NAV_TABS/NAV_MORE 覆盖） */
export const NAV_SECONDARY = [
  { id: 'actress', label: 'nav.actress' },
  { id: 'collections', label: 'nav.collections' },
  { id: 'rankings', label: 'nav.rankings' },
  { id: 'yearReview', label: 'nav.yearReview' },
  { id: 'swipe', label: 'nav.swipe' },
  { id: 'stats', label: 'nav.stats' },
  { id: 'maintenance', label: 'nav.maintenance' },
  { id: 'settings', label: 'nav.settings' },
]

/* 侧边栏分组导航（Sidebar.vue 使用，注意字段为 id/label/icon） */
export const NAV_GROUPS = [
  { title: 'navGroup.browse', items: [
    { id: 'home', label: 'nav.home', icon: 'gallery' },
    { id: 'gallery', label: 'nav.gallery', icon: 'gallery' },
    { id: 'actress', label: 'nav.actress', icon: 'actress' },
    { id: 'recent', label: 'nav.recent', icon: 'recent' },
    { id: 'collections', label: 'nav.collections', icon: 'collections' },
    { id: 'rankings', label: 'nav.rankings', icon: 'rankings' },
    { id: 'yearReview', label: 'nav.yearReview', icon: 'yearReview' },
    { id: 'swipe', label: 'nav.swipe', icon: 'swipe' },
  ] },
  { title: 'navGroup.maintenance', items: [
    { id: 'maintenance', label: 'nav.maintenance', icon: 'maintenance' },
    { id: 'settings', label: 'nav.settings', icon: 'settings' },
  ] },
]



