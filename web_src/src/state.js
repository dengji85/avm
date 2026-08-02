import { reactive, watch } from 'vue'

const PREF_KEY = 'avm.prefs.v2'

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREF_KEY) || '{}') } catch (e) { return {} }
}
const saved = loadPrefs()

/** 全局响应式状态 */
export const state = reactive({
  // ---- 路由 / 视图 ----
  view: 'gallery',

  // ---- 影片查询条件 ----
  q: '',
  actress: [],
  genre: [],
  studio: '',
  series: '',
  prefix: '',
  year: null,
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

  // ---- 后台任务 ----
  task: {
    scan: { running: false, done: 0, total: 0, message: '', phase: '' },
    scrape: { running: false, done: 0, total: 0, message: '', ok: 0, fail: 0 },
  },
  taskPanelOpen: false,
})

/* 偏好持久化 */
watch(
  () => ({
    theme: state.theme, density: state.density, cardSize: state.cardSize,
    sidebarCollapsed: state.sidebarCollapsed, sort: state.sort,
    page_size: state.page_size, multiOp: state.multiOp,
  }),
  (v) => { try { localStorage.setItem(PREF_KEY, JSON.stringify(v)) } catch (e) {} },
  { deep: true },
)

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
  state.page = 1
}

/** 当前是否有任何激活筛选 */
export function hasActiveFilter() {
  return !!(state.q || state.actress.length || state.genre.length || state.studio ||
            state.series || state.prefix || state.year || state.flags.length)
}

/* ---------------- 常量 ---------------- */

export const FLAGS = [
  ['favorite', '收藏', '♥'], ['watchlist', '想看', '⌚'],
  ['subtitle', '中文字幕', '字'], ['uncensored', '无码', '無'],
  ['hd4k', '4K', '4K'], ['vr', 'VR', 'VR'],
  ['unwatched', '未看', '○'], ['watched', '看过', '●'],
  ['nocover', '缺封面', '▢'], ['noscrape', '未刮削', '⚑'], ['nocode', '无番号', '?'],
]

export const FACET_KINDS = [
  ['actresses', 'actress', true, '女优'],
  ['genres', 'genre', true, '类型'],
  ['studios', 'studio', false, '厂商'],
  ['series', 'series', false, '系列'],
  ['prefixes', 'prefix', false, '番号前缀'],
  ['years', 'year', false, '年份'],
]

export const SORTS = [
  ['added_desc', '最近添加'], ['added_asc', '最早添加'],
  ['release_desc', '发行最新'], ['release_asc', '发行最早'],
  ['rating_desc', '评分最高'], ['title_asc', '标题 A-Z'],
  ['code_asc', '番号顺序'], ['size_desc', '体积最大'],
  ['duration_desc', '时长最长'], ['played_desc', '播放最多'],
  ['random', '随机'],
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
  settings: 'M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.488.488 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 0 0-.48-.41h-3.84a.484.484 0 0 0-.48.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.488.488 0 0 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.27.41.48.41h3.84c.24 0 .44-.17.48-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z',
}

export const NAV_GROUPS = [
  {
    title: '浏览',
    items: [
      { id: 'gallery', label: '影片库' },
      { id: 'actress', label: '女优墙' },
      { id: 'collections', label: '片单' },
    ],
  },
  {
    title: '发现',
    items: [
      { id: 'rankings', label: '排行榜' },
      { id: 'swipe', label: '滑动评分' },
    ],
  },
  {
    title: '管理',
    items: [
      { id: 'stats', label: '统计分析' },
      { id: 'storage', label: '存储体检' },
      { id: 'health', label: '数据健康' },
      { id: 'settings', label: '设置' },
    ],
  },
]
