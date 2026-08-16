<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { state } from '../state.js'
import { getStats, getStatsEnhanced, getRankings, listMovies, getStorage, getWatchHistory, getWatchAnalytics } from '../api.js'
import { fmtMin, fmtSize, fmtDuration, fmtDate } from '../utils.js'
import { t } from '../i18n/index.js'
import { toast } from '../utils.js'
import PageHead from '../components/PageHead.vue'
import StatGrid from '../components/StatGrid.vue'
import EmptyState from '../components/EmptyState.vue'

const loading = ref(true)
const error = ref('')
const activeTab = ref('storage')

/* ===== 观看明细（watchlog） ===== */
const wlQuery = reactive({ from: '', to: '', method: '', q: '' })
const wlItems = ref([])
const wlPage = ref(1)
const wlTotal = ref(0)
const wlLoading = ref(false)
const wlSummary = reactive({ sessions: 0, total_sec: 0, movies: 0, days: 0 })
const wlHasFilter = computed(() => !!(wlQuery.from || wlQuery.to || wlQuery.method || wlQuery.q))

async function loadWatchLog(keepPage = false) {
  if (!keepPage) wlPage.value = 1
  wlLoading.value = true
  try {
    const r = await getWatchHistory({
      page: wlPage.value,
      size: 50,
      from: wlQuery.from || undefined,
      to: wlQuery.to || undefined,
      method: wlQuery.method || undefined,
      q: wlQuery.q || undefined,
    })
    wlItems.value = (r && r.items) || []
    wlTotal.value = (r && r.total) || 0
    if (r && r.summary) Object.assign(wlSummary, r.summary)
  } catch (e) { toast(e.message, 'err'); wlItems.value = [] } finally { wlLoading.value = false }
}
function applyWlFilter() { loadWatchLog(false) }
function resetWlFilter() {
  wlQuery.from = ''; wlQuery.to = ''; wlQuery.method = ''; wlQuery.q = ''
  loadWatchLog(false)
}
function wlChangePage(delta) {
  const next = wlPage.value + delta
  if (next < 1 || (next - 1) * 50 >= wlTotal.value) return
  wlPage.value = next
  loadWatchLog(true)
}
// 进入 watchlog tab 时首次加载
watch(activeTab, (v) => { if (v === 'watchlog' && !wlItems.value.length) loadWatchLog(false) })

/* 概览 */
const overview = reactive({
  totalMovies: 0,
  actresses: 0,
  series: 0,
  runtimeMin: 0,
  watched: 0,
  watchedPct: 0,
  addedThisMonth: 0,
  withCover: 0,
  scraped: 0,
  noCode: 0,
  favorite: 0,
  watchlist: 0,
  subtitle: 0,
  uncensored: 0,
})

/* 趋势 / 标签云 */
const byYear = ref([])
const runtimeByYear = ref([])
const tagCloud = ref([])

/* 最爱榜单维度 */
const favDim = ref('actress')
const favData = reactive({ actress: [], studio: [], series: [], genre: [] })

/* 趣味 / 观看行为榜单 */
const ratingTop = ref([])
const ratingLow = ref([])
const playTop = ref([])
const watchedTop = ref([])

/* 观看多维分析（基于真实观看明细） */
const analytics = ref({})

/* 所有影片一览 */
const allMovies = ref([])
const sortKey = ref('rating')   // rating | runtime | year | watched | favorite
const sortDir = ref('desc')

/* 存储占用 */
const storage = reactive({ total: {}, byDisk: [], byStudio: [], byYear: [] })

function fmtRuntime(min) {
  min = Number(min) || 0
  const h = Math.floor(min / 60)
  const m = min % 60
  return h ? `${h}h ${m}m` : `${m}m`
}

function drillTo(dim, name) {
  if (!name) return
  state.actress = dim === 'actress' ? [name] : []
  state.studio = dim === 'studio' ? name : ''
  state.series = dim === 'series' ? name : ''
  state.genre = dim === 'genre' ? [name] : []
  state.view = 'gallery'
}

function openDetail(id) { state.currentId = id }

/* 顶部全局搜索：统计页展示的是全量指标，搜索不应过滤上方数据。
   这里仅查询匹配影片数量，并引导用户跳到影片库查看结果。 */
const searchCount = ref(null)
const searchBusy = ref(false)
let searchToken = 0
function querySearchCount() {
  const q = (state.q || '').trim()
  if (!q) { searchCount.value = null; return }
  const token = ++searchToken
  searchBusy.value = true
  listMovies({ q, size: 1, page: 1 })
    .then((r) => { if (token === searchToken) searchCount.value = (r && r.total) || 0 })
    .catch(() => { if (token === searchToken) searchCount.value = 0 })
    .finally(() => { if (token === searchToken) searchBusy.value = false })
}
function gotoSearchResults() {
  state.view = 'gallery'   // state.q 已写入，影片库会自动应用该关键词
}
watch(() => state.q, querySearchCount, { immediate: true })

/* 跳转到 gallery 指定筛选态 */
function gotoFilter(flag) {
  state.favorite = false
  state.watchlist = false
  state.watched = null
  if (flag === 'favorite') state.favorite = true
  else if (flag === 'watchlist') state.watchlist = true
  else if (flag === 'watched') state.watched = true
  else if (flag === 'unwatched') state.watched = false
  state.view = 'gallery'
}

function barify(list, key = 'count', labelKey = 'name') {
  const max = Math.max(1, ...list.map((x) => Number(x[key]) || 0))
  return list.slice(0, 10).map((x) => ({
    label: x[labelKey],
    value: Number(x[key]) || 0,
    pct: Math.round(((Number(x[key]) || 0) / max) * 100),
  }))
}

const favBars = computed(() => barify(favData[favDim.value]))

const quality = computed(() => {
  const total = overview.totalMovies || 0
  const pct = (n) => (total ? Math.round((n / total) * 100) : 0)
  return [
    { label: t('stats.hasCover'), value: overview.withCover, pct: pct(overview.withCover), tone: 'ok' },
    { label: t('stats.scraped'), value: overview.scraped, pct: pct(overview.scraped), tone: 'accent' },
    { label: t('stats.noCode'), value: overview.noCode, pct: pct(overview.noCode), tone: overview.noCode ? 'warn' : 'ok' },
  ]
})

const yearTrend = computed(() => {
  const map = {}
  for (const y of byYear.value) map[y.year] = { year: y.year, count: y.count, minutes: 0 }
  for (const y of runtimeByYear.value) {
    if (!map[y.year]) map[y.year] = { year: y.year, count: 0, minutes: y.minutes || 0 }
    else map[y.year].minutes = y.minutes || 0
  }
  const arr = Object.values(map).sort((a, b) => a.year - b.year)
  const maxCount = Math.max(1, ...arr.map((x) => x.count))
  const maxMin = Math.max(1, ...arr.map((x) => x.minutes))
  return arr.map((x) => ({
    ...x,
    countPct: Math.round((x.count / maxCount) * 100),
    minPct: Math.round((x.minutes / maxMin) * 100),
  }))
})

/* 一览表排序 */
const sortedMovies = computed(() => {
  const arr = (allMovies.value || []).slice()
  const k = sortKey.value
  const dir = sortDir.value === 'desc' ? -1 : 1
  const val = (m) => {
    if (k === 'rating') return Number(m.rating) || 0
    if (k === 'runtime') return Number(m.runtime) || 0
    if (k === 'year') return Number(m.year) || 0
    if (k === 'watched') return m.watched ? 1 : 0
    if (k === 'favorite') return m.favorite ? 1 : 0
    return 0
  }
  return arr.sort((a, b) => (val(a) - val(b)) * dir)
})

function setSort(k) {
  if (sortKey.value === k) { sortDir.value = sortDir.value === 'desc' ? 'asc' : 'desc' }
  else { sortKey.value = k; sortDir.value = 'desc' }
}

const favNote = computed(() => (favDim.value === 'actress' ? t('stats.favNote') : ''))

/* 存储：各硬盘占比条形 */
const diskBars = computed(() => {
  const arr = storage.byDisk || []
  const max = Math.max(1, ...arr.map((d) => Number(d.bytes) || 0))
  return arr.map((d) => ({
    label: d.drive,
    value: Number(d.bytes) || 0,
    movies: d.movies,
    files: d.files,
    pct: Math.round(((Number(d.bytes) || 0) / max) * 100),
  }))
})
const diskTotal = computed(() => (storage.total && storage.total.bytes) || 0)
const studioBars = computed(() => {
  const arr = (storage.byStudio || []).slice(0, 8)
  const max = Math.max(1, ...arr.map((d) => Number(d.bytes) || 0))
  return arr.map((d) => ({
    label: d.studio,
    value: Number(d.bytes) || 0,
    pct: Math.round(((Number(d.bytes) || 0) / max) * 100),
  }))
})
const avgSize = computed(() =>
  diskTotal.value && overview.totalMovies ? Math.round(diskTotal.value / overview.totalMovies) : 0
)

/* 顶部存储 Hero 的小指标 */
const storageHero = computed(() => {
  const t = storage.total || {}
  return [
    { label: t('stats.heroFiles'), value: (t.files || 0).toLocaleString() },
    { label: t('stats.heroMovies'), value: (t.movies || 0).toLocaleString() },
    { label: t('stats.heroAvg'), value: fmtSize(avgSize.value) },
  ]
})
const diskCount = computed(() => (storage.byDisk || []).length)

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    const [s, se, rPlay, rRate, ml, st] = await Promise.all([
      getStats(),
      getStatsEnhanced(),
      getRankings('play', 30),
      getRankings('rating', 30),
      listMovies({ limit: 1000 }),
      getStorage(),
    ])

    overview.totalMovies = s.movies || 0
    overview.actresses = s.actresses || 0
    overview.series = s.series || 0
    overview.runtimeMin = Math.round((s.runtime || 0) / 60)
    overview.watched = s.watched || 0
    overview.watchedPct = s.movies ? Math.round((s.watched / s.movies) * 100) : 0
    overview.addedThisMonth = (s.recent || []).length
    overview.withCover = s.with_cover || 0
    overview.scraped = s.scraped || 0
    overview.noCode = s.no_code || 0
    overview.favorite = s.favorite || 0
    overview.watchlist = s.watchlist || 0
    overview.subtitle = s.subtitle || 0
    overview.uncensored = s.uncensored || 0

    favData.actress = s.top_actresses || []
    favData.studio = s.top_studios || []
    favData.series = s.top_series || []
    favData.genre = s.top_genres || []

    byYear.value = (s.by_year || []).map((x) => ({ year: x.year, count: x.count }))
    runtimeByYear.value = se.runtime_by_year || []
    tagCloud.value = se.tag_cloud || []

    // 播放最多：依赖 rankings play 的 play_count
    playTop.value = (rPlay.items || []).filter((m) => m && (m.play_count || 0) > 0).slice(0, 10)

    const list = ml.items || ml.movies || []
    allMovies.value = Array.isArray(list) ? list : []

    storage.total = st.total || {}
    storage.byDisk = st.by_disk || []
    storage.byStudio = st.by_studio || []
    storage.byYear = st.by_year || []
    storage.byExt = st.by_ext || []
    storage.byGenre = st.by_genre || []
    storage.largest = st.largest || []

    // 评分榜单：仅取真实评分（rating > 0）
    const rated = (rRate.items || []).filter((m) => m && m.rating && Number(m.rating) > 0)
    ratingTop.value = rated.slice(0, 10)
    ratingLow.value = rated.slice(-10).reverse()

    // 观看最久：使用真实观看明细汇总（watch_analytics.top_movies 的 total_sec）
    const wa = await getWatchAnalytics()
    analytics.value = wa || {}
    watchedTop.value = (wa && wa.top_movies || [])
      .map((m) => ({ ...m, runtime: (Number(m.total_sec) || 0) / 60 }))
      .slice(0, 10)
  } catch (e) {
    error.value = e.message || t('stats.loadFail')
  } finally {
    loading.value = false
  }
}

onMounted(loadAll)

const ring = computed(() => {
  const pct = overview.watchedPct
  const r = 42, c = 2 * Math.PI * r
  return { dash: (pct / 100) * c, total: c, pct }
})

const favFilterCards = computed(() => [
  { key: 'favorite', label: t('flag.favorite'), value: overview.favorite, icon: '⭐', tone: 'gold' },
  { key: 'watchlist', label: t('flag.watchlist'), value: overview.watchlist, icon: '🔖', tone: 'accent' },
  { key: 'watched', label: t('flag.watched'), value: overview.watched, icon: '✅', tone: 'ok' },
  { key: 'unwatched', label: t('stats.favUnwatched'), value: overview.totalMovies - overview.watched, icon: '⏳', tone: 'muted' },
])

/* 存储分布：按文件类型、最大文件 */
const byExtBars = computed(() => {
  const arr = storage.byExt || []
  const max = Math.max(1, ...arr.map(d => d.bytes || 0))
  return arr.map(d => ({
    label: (d.ext || '未知').toUpperCase(),
    files: d.files || 0,
    value: d.bytes || 0,
    pct: Math.round((d.bytes / max) * 100),
    share: diskTotal.value ? Math.round((d.bytes / diskTotal.value) * 100) : 0,
  }))
})
const largestFiles = computed(() => storage.largest || [])

/* 存储分布：按内容类型（genre）聚合占用 */
const byGenreBars = computed(() => {
  const arr = storage.byGenre || []
  const max = Math.max(1, ...arr.map(d => d.bytes || 0))
  return arr.map(d => ({
    label: d.genre || '未分类',
    movies: d.movies || 0,
    value: d.bytes || 0,
    pct: Math.round((d.bytes / max) * 100),
    share: diskTotal.value ? Math.round((d.bytes / diskTotal.value) * 100) : 0,
  }))
})

/* 观看明细辅助 */
function wlFmtHours(sec) {
  const h = (Number(sec) || 0) / 3600
  return h >= 100 ? `${Math.round(h)} h` : h.toFixed(1) + ' h'
}
function methodLabel(method) {
  return { builtin: t('rankings.methodBuiltin'), external: t('rankings.methodExternal'), system: t('rankings.methodSystem') }[method] || method || '—'
}
// 浏览器错误页标题等异常文本不应当作片名展示
const BAD_TITLES = ['无法访问此网站', '无法访问此网站。', 'ERR_CONNECTION_REFUSED', 'This site can’t be reached']
function displayTitle(h) {
  const t = (h.title || '').trim()
  if (!t || BAD_TITLES.includes(t)) return h.code || h.file_name || ('#' + h.movie_id)
  return t
}
function methodTone(method) {
  return { builtin: 'ok', external: 'accent', system: 'muted' }[method] || 'muted'
}

/* 观看分析：多维度聚合 */
const wa = computed(() => analytics.value || {})
const waTotal = computed(() => ({
  sessions: wa.value.sessions || 0,
  total_sec: wa.value.total_sec || 0,
  movies: wa.value.movies || 0,
  days: wa.value.days || 0,
  avg: wa.value.avg_session_sec || 0,
  first: wa.value.first_at,
  last: wa.value.last_at,
}))
const waLast = computed(() => wa.value.last_at ? fmtDate(wa.value.last_at) : '—')
const byHourBars = computed(() => {
  const arr = wa.value.by_hour || []
  const max = Math.max(1, ...arr.map((x) => Number(x) || 0))
  return arr.map((v, h) => ({ h, label: String(h).padStart(2, '0'), value: Number(v) || 0, pct: Math.round(((Number(v) || 0) / max) * 100) }))
})
const byMethodBars = computed(() => {
  const arr = wa.value.by_method || []
  const max = Math.max(1, ...arr.map((x) => Number(x.sessions) || 0))
  return arr.map((x) => ({ method: x.method, sessions: x.sessions || 0, sec: Number(x.sec) || 0,
    pct: Math.round(((x.sessions || 0) / max) * 100) }))
})
const byMonthBars = computed(() => {
  const arr = (wa.value.by_month || []).slice(-12)
  const max = Math.max(1, ...arr.map((x) => Number(x.sec) || 0))
  return arr.map((x) => ({ month: x.month, sessions: x.sessions || 0, sec: Number(x.sec) || 0,
    pct: Math.round(((Number(x.sec) || 0) / max) * 100) }))
})
const profileDims = [
  { key: 'actresses', label: () => t('stats.dimActress') },
  { key: 'studios', label: () => t('stats.dimStudio') },
  { key: 'genres', label: () => t('stats.dimGenre') },
  { key: 'series', label: () => t('stats.dimSeries') },
  { key: 'directors', label: () => t('stats.dimDirector') },
]
function profileBars(key) {
  const arr = (wa.value.profile && wa.value.profile[key]) || []
  const max = Math.max(1, ...arr.map((x) => Number(x.sec) || 0))
  return arr.slice(0, 10).map((x) => ({
    name: x.name, sec: Number(x.sec) || 0,
    pct: Math.round(((Number(x.sec) || 0) / max) * 100),
  }))
}

/* 观看分析：新增维度（完成度 / 复看 / 时间节律 / 结合分析） */
const completion = computed(() => wa.value.completion || { finish_rate: 0, avg_completion: 0 })
const rewatch = computed(() => wa.value.rewatch || {
  rewatched_movies: 0, rewatch_rate: 0, avg_sessions_per_movie: 0, top: [] })
const byWeekday = computed(() => {
  const arr = wa.value.by_weekday || []
  const max = Math.max(1, ...arr.map((x) => Number(x.sec) || 0))
  // 周日为 0，调整为周一开头的展示顺序
  const order = [1, 2, 3, 4, 5, 6, 0]
  const map = {}
  arr.forEach((x) => { map[x.wd] = x })
  return order.map((wd) => {
    const r = map[wd] || { sessions: 0, sec: 0 }
    return { wd, label: t('stats.wd' + wd), sessions: r.sessions || 0, sec: Number(r.sec) || 0,
      pct: Math.round((Number(r.sec) || 0) / max * 100) }
  })
})
const weekdaySplit = computed(() => wa.value.weekday_split || { workday_sec: 0, weekend_sec: 0, ratio: 0 })
const streak = computed(() => wa.value.streak || { current: 0, max: 0, longest_gap_days: 0 })
const trend = computed(() => wa.value.trend || { recent_sec: 0, older_sec: 0, growth: 0 })
const ratingX = computed(() => {
  const arr = wa.value.rating_x || []
  const labels = { '4-5': '★4-5', '3-4': '★3-4', '1-3': '★1-3', 'unrated': t('stats.unrated') }
  const max = Math.max(1, ...arr.map((x) => Number(x.avg_sec) || 0))
  return arr.map((x) => ({ band: x.band, label: labels[x.band] || x.band,
    sessions: x.sessions || 0, sec: Number(x.sec) || 0, avg_sec: Number(x.avg_sec) || 0,
    pct: Math.round((Number(x.avg_sec) || 0) / max * 100) }))
})
const uncensoredX = computed(() => {
  const arr = wa.value.uncensored_x || []
  const map = {}
  arr.forEach((x) => { map[x.key] = x })
  const unc = map['uncensored'] || { sessions: 0, sec: 0, movies: 0 }
  const cen = map['censored'] || { sessions: 0, sec: 0, movies: 0 }
  return {
    uncensored: unc, censored: cen,
    uncAvg: unc.sessions ? unc.sec / unc.sessions : 0,
    cenAvg: cen.sessions ? cen.sec / cen.sessions : 0,
  }
})
const durationProfile = computed(() => {
  const dp = wa.value.duration_profile || { watched: {}, library: {} }
  const labels = { lt40: t('stats.durLt40'), '40_90': t('stats.dur40_90'), gt90: t('stats.durGt90'), unknown: t('stats.durUnk') }
  const order = ['lt40', '40_90', 'gt90', 'unknown']
  const watched = dp.watched || {}, library = dp.library || {}
  const wMax = Math.max(1, ...order.map((k) => Number(watched[k]) || 0))
  const lMax = Math.max(1, ...order.map((k) => Number(library[k]) || 0))
  return order.map((k) => ({
    key: k, label: labels[k],
    watched: Number(watched[k]) || 0, library: Number(library[k]) || 0,
    wPct: Math.round((Number(watched[k]) || 0) / wMax * 100),
    lPct: Math.round((Number(library[k]) || 0) / lMax * 100),
  }))
})

/* 一句话洞察引擎：基于指标生成结构化洞察（前端按语言渲染文案） */
const insights = computed(() => {
  const out = []
  const c = completion.value, rw = rewatch.value, s = streak.value, tr = trend.value
  const ws = weekdaySplit.value, ux = uncensoredX.value
  if (c.finish_rate >= 0) out.push({
    icon: '✅', tone: c.finish_rate >= 0.6 ? 'ok' : 'warn',
    key: 'finish', p: [Math.round(c.finish_rate * 100), Math.round(c.avg_completion * 100)] })
  if (rw.rewatch_rate >= 0) out.push({
    icon: '🔁', tone: rw.rewatch_rate >= 0.2 ? 'accent' : 'muted',
    key: 'rewatch', p: [rw.rewatched_movies, Math.round(rw.rewatch_rate * 100), rw.avg_sessions_per_movie.toFixed(1)] })
  if (s.max > 0) out.push({
    icon: '🔥', tone: s.current >= 3 ? 'ok' : 'muted',
    key: 'streak', p: [s.current, s.max, s.longest_gap_days] })
  if (tr.recent_sec > 0 || tr.older_sec > 0) out.push({
    icon: tr.growth >= 0 ? '📈' : '📉', tone: tr.growth >= 0 ? 'ok' : 'warn',
    key: 'trend', p: [Math.round(tr.growth * 100)] })
  if (ws.workday_sec || ws.weekend_sec) out.push({
    icon: '🗓', tone: 'muted',
    key: 'week', p: [Math.round(ws.workday_sec / 3600), Math.round(ws.weekend_sec / 3600)] })
  if (ux.uncensored.sessions && ux.censored.sessions) out.push({
    icon: '🔞', tone: 'accent',
    key: 'unc', p: [Math.round(ux.uncAvg / 60), Math.round(ux.cenAvg / 60)] })
  return out
})

// 观看分析子页：overview / rhythm / preference
const waSeg = ref('overview')

// 趋势对比条百分比（近期 vs 早期）
function trendPct(which) {
  const a = Number(trend.value.recent_sec) || 0
  const b = Number(trend.value.older_sec) || 0
  const max = Math.max(1, a, b)
  return which === 'recent' ? Math.round(a / max * 100) : Math.round(b / max * 100)
}

// 洞察文案（按语言渲染，p 为参数数组）
function insightText(it) {
  switch (it.key) {
    case 'finish': return t('stats.insFinish', { rate: it.p[0], comp: it.p[1] })
    case 'rewatch': return t('stats.insRewatch', { n: it.p[0], rate: it.p[1], avg: it.p[2] })
    case 'streak': return t('stats.insStreak', { cur: it.p[0], max: it.p[1], gap: it.p[2] })
    case 'trend': return t('stats.insTrend', { pct: it.p[0] })
    case 'week': return t('stats.insWeek', { work: it.p[0], weekend: it.p[1] })
    case 'unc': return t('stats.insUnc', { unc: it.p[0], cen: it.p[1] })
    default: return ''
  }
}
</script>

<template>
  <section class="view">
    <div class="view-body stats block-scroll">
    <PageHead :title="$t('view.stats')" :sub="$t('stats.sub', { n: overview.totalMovies })">
      <template #actions>
        <button class="btn ghost" @click="loadAll">{{ $t('stats.refresh') }}</button>
      </template>
    </PageHead>

    <div v-if="loading" class="loading-block">
      <div class="spinner big"></div>
      <p class="muted">{{ $t('stats.loading') }}</p>
    </div>

    <EmptyState v-else-if="error" icon="!" :title="error" :action="$t('stats.retry')" @action="loadAll" />

    <template v-else>
      <!-- 全局搜索提示：统计为全量数据，搜索不筛选上方指标，引导到影片库查看结果 -->
      <div v-if="state.q" class="search-hint">
        <span class="sh-ico">🔍</span>
        <span class="sh-text">
          <b>{{ state.q }}</b> · {{ $t('stats.searchIsGlobal') }}
        </span>
        <button class="sh-go" :disabled="searchBusy" @click="gotoSearchResults">
          <span v-if="searchBusy" class="spinner sm"></span>
          <template v-else>{{ $t('stats.viewInGallery', { n: searchCount == null ? '…' : searchCount }) }}</template>
          →
        </button>
      </div>

      <!-- ===== 核心 KPI 行 ===== -->
      <div class="kpi-row">
        <div class="kpi storage" :class="{ empty: !diskTotal }">
          <span class="kpi-ico">💾</span>
          <div class="kpi-body">
            <span class="kpi-label">{{ $t('stats.totalStorage') }}</span>
            <span class="kpi-value tabular">{{ fmtSize(diskTotal) || '—' }}</span>
            <span class="kpi-sub muted">{{ $t('stats.storageSub', { files: (storage.total.files || 0).toLocaleString(), avg: fmtSize(avgSize) }) }}</span>
          </div>
        </div>
        <div class="kpi">
          <span class="kpi-ico">🎞️</span>
          <div class="kpi-body">
            <span class="kpi-label">{{ $t('stats.moviesTotal') }}</span>
            <span class="kpi-value tabular">{{ overview.totalMovies.toLocaleString() }}</span>
            <span class="kpi-sub muted">{{ $t('stats.thisMonth', { n: overview.addedThisMonth }) }}</span>
          </div>
        </div>
        <div class="kpi">
          <span class="kpi-ico">⏱️</span>
          <div class="kpi-body">
            <span class="kpi-label">{{ $t('stats.totalRuntime') }}</span>
            <span class="kpi-value tabular">{{ fmtRuntime(overview.runtimeMin) }}</span>
            <span class="kpi-sub muted">{{ $t('stats.actressSeries', { a: overview.actresses, s: overview.series }) }}</span>
          </div>
        </div>
        <div class="kpi">
          <span class="kpi-ico">👁️</span>
          <div class="kpi-body">
            <span class="kpi-label">{{ $t('stats.watchedPct') }}</span>
            <span class="kpi-value tabular">{{ overview.watchedPct }}%</span>
            <span class="kpi-sub muted">{{ $t('stats.watchedOf', { w: overview.watched, t: overview.totalMovies }) }}</span>
          </div>
          <span class="kpi-ring" :style="{ '--p': ring.pct }"></span>
        </div>
      </div>

      <!-- ===== 收藏速览卡 ===== -->
      <div class="filter-cards">
        <button
          v-for="c in favFilterCards"
          :key="c.key"
          class="fcard"
          :class="c.tone"
          @click="gotoFilter(c.key)"
        >
          <span class="fi">{{ c.icon }}</span>
          <span class="fcard-body">
            <span class="fv tabular">{{ c.value }}</span>
            <span class="fl">{{ c.label }}</span>
          </span>
        </button>
      </div>

      <!-- ===== Tab 分区 ===== -->
      <div class="tabs">
        <button :class="{on: activeTab==='storage'}" @click="activeTab='storage'">{{ $t('stats.storageDist') }}</button>
        <button :class="{on: activeTab==='collect'}" @click="activeTab='collect'">{{ $t('stats.collectHealth') }}</button>
        <button :class="{on: activeTab==='favorite'}" @click="activeTab='favorite'">{{ $t('stats.favTop') }}</button>
        <button :class="{on: activeTab==='trend'}" @click="activeTab='trend'">{{ $t('stats.yearTrend') }}</button>
        <button :class="{on: activeTab==='fun'}" @click="activeTab='fun'">{{ $t('stats.funTop') }}</button>
        <button :class="{on: activeTab==='movies'}" @click="activeTab='movies'">{{ $t('stats.allMovies', { n: allMovies.length }) }}</button>
        <button :class="{on: activeTab==='wanalysis'}" @click="activeTab='wanalysis'">{{ $t('stats.watchAnalysis') }}</button>
        <button :class="{on: activeTab==='watchlog'}" @click="activeTab='watchlog'">{{ $t('stats.watchlog') }}</button>
      </div>

      <!-- 存储分布 -->
      <div v-show="activeTab==='storage'">
        <section class="card panel">
          <div class="panel-head">
            <h2>{{ $t('stats.storageDist') }}</h2>
            <span class="muted small">{{ $t('stats.storageHead', { size: storage.total.bytes ? fmtSize(diskTotal) : '0', n: storage.total.movies || 0 }) }}</span>
          </div>

          <div v-if="!diskBars.length" class="muted pad">{{ $t('stats.noFiles') }}</div>
          <template v-else>
            <div class="bars">
              <div v-for="d in diskBars" :key="d.label" class="bar-row" style="cursor: default">
                <span class="rank disk">{{ d.label }}</span>
                <span class="name ellipsis">{{ fmtSize(d.value) }} · {{ $t('stats.diskInfo', { movies: d.movies, files: d.files }) }}</span>
                <span class="track"><i class="disk" :style="{ width: d.pct + '%' }"></i></span>
                <span class="val tabular">{{ Math.round((d.value / diskTotal) * 100) }}%</span>
              </div>
            </div>

            <h3 class="sub">{{ $t('stats.studioTop') }}</h3>
            <div v-if="!studioBars.length" class="muted pad">{{ $t('stats.noData') }}</div>
            <div v-else class="bars">
              <div v-for="d in studioBars" :key="d.label" class="bar-row" style="cursor: default">
                <span class="rank disk">{{ fmtSize(d.value) }}</span>
                <span class="name ellipsis">{{ d.label }}</span>
                <span class="track"><i class="disk2" :style="{ width: d.pct + '%' }"></i></span>
                <span class="val tabular">{{ Math.round((d.value / diskTotal) * 100) }}%</span>
              </div>
            </div>

            <h3 class="sub">{{ $t('stats.byGenre') }}</h3>
            <div v-if="!byGenreBars.length" class="muted pad">{{ $t('stats.noData') }}</div>
            <div v-else class="bars">
              <div v-for="d in byGenreBars" :key="d.label" class="bar-row" style="cursor: default">
                <span class="rank disk">{{ d.label }}</span>
                <span class="name ellipsis">{{ $t('stats.genreInfo', { movies: d.movies, share: d.share }) }}</span>
                <span class="track"><i class="disk" :style="{ width: d.pct + '%' }"></i></span>
                <span class="val tabular">{{ fmtSize(d.value) }}</span>
              </div>
            </div>

            <h3 class="sub">{{ $t('stats.byExt') }}</h3>
            <div v-if="!byExtBars.length" class="muted pad">{{ $t('stats.noData') }}</div>
            <div v-else class="bars">
              <div v-for="d in byExtBars" :key="d.label" class="bar-row" style="cursor: default">
                <span class="rank disk">{{ d.label }}</span>
                <span class="name ellipsis">{{ $t('stats.fileInfo', { files: d.files, share: d.share }) }}</span>
                <span class="track"><i class="disk2" :style="{ width: d.pct + '%' }"></i></span>
                <span class="val tabular">{{ fmtSize(d.value) }}</span>
              </div>
            </div>

            <h3 class="sub">{{ $t('stats.largestTop') }}</h3>
            <div v-if="!largestFiles.length" class="muted pad">{{ $t('stats.noData') }}</div>
            <div v-else class="bars">
              <div
                v-for="f in largestFiles"
                :key="f.file_id"
                class="bar-row file-row"
                @click="openDetail(f.movie_id)"
                :title="`${f.filename} · 点击查看影片`"
              >
                <span class="rank file-ext">{{ (f.ext || '?').toUpperCase() }}</span>
                <span class="name ellipsis">{{ f.movie_name }}</span>
                <span class="track"><i :style="{ width: (f.bytes / largestFiles[0].bytes * 100) + '%' }"></i></span>
                <span class="val tabular">{{ fmtSize(f.bytes) }}</span>
              </div>
            </div>
          </template>
        </section>
      </div>

      <!-- 收藏健康 -->
      <div v-show="activeTab==='collect'">
        <div class="row">
          <section class="card panel">
            <div class="panel-head"><h2>{{ $t('stats.collectQuality') }}</h2></div>
            <div class="quality">
              <div v-for="q in quality" :key="q.label" class="q-row">
                <div class="q-top">
                  <span class="q-label">{{ q.label }}</span>
                  <span class="q-val tabular">{{ q.value }} / {{ overview.totalMovies }}（{{ q.pct }}%）</span>
                </div>
                <span class="track"><i :class="q.tone" :style="{ width: q.pct + '%' }"></i></span>
              </div>
              <p class="q-hint muted">
                {{ $t('stats.qualityHint', { f: overview.favorite, w: overview.watchlist, s: overview.subtitle, u: overview.uncensored }) }}
              </p>
            </div>
          </section>

          <section class="card panel watch-ring">
            <div class="panel-head"><h2>{{ $t('stats.watchProgress') }}</h2></div>
            <div class="ring-wrap">
              <svg viewBox="0 0 100 100" class="ring">
                <circle cx="50" cy="50" :r="42" class="ring-bg" />
                <circle
                  cx="50" cy="50" :r="42" class="ring-fg"
                  :stroke-dasharray="`${ring.dash} ${ring.total}`"
                  transform="rotate(-90 50 50)"
                />
              </svg>
              <div class="ring-center">
                <b>{{ ring.pct }}%</b>
                <small>{{ $t('stats.watchedOfShort', { w: overview.watched, t: overview.totalMovies }) }}</small>
              </div>
            </div>
          </section>
        </div>
      </div>

      <!-- 最爱榜单 -->
      <div v-show="activeTab==='favorite'">
        <section class="card panel">
          <div class="panel-head">
            <h2>{{ $t('stats.favTop') }}</h2>
            <div class="seg">
              <button :class="{on: favDim==='actress'}" @click="favDim='actress'">{{ $t('stats.favActress') }}</button>
              <button :class="{on: favDim==='studio'}" @click="favDim='studio'">{{ $t('stats.favStudio') }}</button>
              <button :class="{on: favDim==='series'}" @click="favDim='series'">{{ $t('stats.favSeries') }}</button>
              <button :class="{on: favDim==='genre'}" @click="favDim='genre'">{{ $t('stats.favGenre') }}</button>
            </div>
          </div>
          <p v-if="favNote" class="dim-note muted">{{ favNote }}</p>
          <div v-if="!favBars.length" class="muted pad">{{ $t('stats.noData') }}</div>
          <div v-else class="bars">
            <button
              v-for="(b, i) in favBars"
              :key="b.label"
              class="bar-row"
              @click="drillTo(favDim, b.label)"
              :title="$t('stats.drillTip', { name: b.label })"
            >
              <span class="rank">{{ i + 1 }}</span>
              <span class="name ellipsis">{{ b.label }}</span>
              <span class="track"><i :style="{ width: b.pct + '%' }"></i></span>
              <span class="val tabular">{{ b.value }}</span>
            </button>
          </div>
        </section>
      </div>

      <!-- 年份双趋势 -->
      <div v-show="activeTab==='trend'">
        <section class="card panel">
          <div class="panel-head">
            <h2>{{ $t('stats.yearTrend') }}</h2>
            <div class="legend">
              <span class="lg count">{{ $t('stats.legendCount') }}</span>
              <span class="lg mins">{{ $t('stats.legendMins') }}</span>
            </div>
          </div>
          <div v-if="!yearTrend.length" class="muted pad">{{ $t('stats.noData') }}</div>
          <div v-else class="year-trend">
            <div v-for="y in yearTrend" :key="y.year" class="yt-row">
              <span class="yt-year">{{ y.year }}</span>
              <div class="yt-bars">
                <span class="track sm"><i class="count" :style="{ width: y.countPct + '%' }"></i></span>
                <span class="track sm"><i class="mins" :style="{ width: y.minPct + '%' }"></i></span>
              </div>
              <span class="yt-val tabular">{{ $t('stats.yearInfo', { count: y.count, mins: fmtRuntime(Math.round(y.minutes / 60)) }) }}</span>
            </div>
          </div>
        </section>
      </div>

      <!-- 趣味榜单 -->
      <div v-show="activeTab==='fun'">
        <section class="card panel">
          <div class="panel-head"><h2>{{ $t('stats.funTop') }}</h2></div>
          <div class="fun-grid">
            <div class="fun-col">
              <h3>🏆 {{ $t('stats.rateHigh') }}</h3>
              <ol class="fun-list">
                <li v-for="m in ratingTop" :key="m.id" @click="openDetail(m.id)">
                  <span class="t ellipsis">{{ m.title || m.code }}</span><span class="gold">★{{ m.rating }}</span>
                </li>
                <li v-if="!ratingTop.length" class="muted">{{ $t('stats.noRating') }}</li>
              </ol>
            </div>
            <div class="fun-col">
              <h3>💩 {{ $t('stats.rateLow') }}</h3>
              <ol class="fun-list">
                <li v-for="m in ratingLow" :key="m.id" @click="openDetail(m.id)">
                  <span class="t ellipsis">{{ m.title || m.code }}</span><span class="dim">★{{ m.rating }}</span>
                </li>
                <li v-if="!ratingLow.length" class="muted">{{ $t('stats.noRating') }}</li>
              </ol>
            </div>
            <div class="fun-col">
              <h3>🔥 {{ $t('stats.playMost') }}</h3>
              <ol class="fun-list">
                <li v-for="m in playTop" :key="m.id" @click="openDetail(m.id)">
                  <span class="t ellipsis">{{ m.title || m.code }}</span><span class="dim">×{{ m.play_count }}</span>
                </li>
                <li v-if="!playTop.length" class="muted">{{ $t('stats.noPlay') }}</li>
              </ol>
            </div>
            <div class="fun-col">
              <h3>📺 {{ $t('stats.watchLong') }}</h3>
              <ol class="fun-list">
                <li v-for="m in watchedTop" :key="m.id" @click="openDetail(m.id)">
                  <span class="t ellipsis">{{ m.title || m.code }}</span><span class="dim">{{ fmtMin(Math.round(m.runtime || 0)) }}</span>
                </li>
                <li v-if="!watchedTop.length" class="muted">{{ $t('stats.noWatch') }}</li>
              </ol>
            </div>
          </div>

          <div v-if="tagCloud.length" class="tagcloud">
            <span
              v-for="t in tagCloud"
              :key="t.name"
              class="tc"
              :style="{ fontSize: (12 + Math.min(18, t.count)) + 'px', opacity: 0.55 + Math.min(0.45, t.count / 20) }"
            >{{ t.name }}</span>
          </div>
        </section>
      </div>

      <!-- 所有影片一览 -->
      <div v-show="activeTab==='movies'">
        <section class="card panel">
          <div class="panel-head">
            <h2>{{ $t('stats.allMovies', { n: allMovies.length }) }}</h2>
            <div class="sort-tabs">
              <button :class="{on: sortKey==='rating'}" @click="setSort('rating')">{{ $t('stats.sortRating') }}</button>
              <button :class="{on: sortKey==='runtime'}" @click="setSort('runtime')">{{ $t('stats.sortRuntime') }}</button>
              <button :class="{on: sortKey==='year'}" @click="setSort('year')">{{ $t('stats.sortYear') }}</button>
              <button :class="{on: sortKey==='watched'}" @click="setSort('watched')">{{ $t('stats.sortWatched') }}</button>
              <button :class="{on: sortKey==='favorite'}" @click="setSort('favorite')">{{ $t('stats.sortFavorite') }}</button>
            </div>
          </div>
          <div class="tbl">
            <div class="trow th">
              <span class="c-title">{{ $t('stats.colMovie') }}</span>
              <span class="c-rating">{{ $t('stats.colRating') }}</span>
              <span class="c-runtime">{{ $t('stats.colRuntime') }}</span>
              <span class="c-year">{{ $t('stats.colYear') }}</span>
              <span class="c-flags">{{ $t('stats.colStatus') }}</span>
            </div>
            <div
              v-for="m in sortedMovies"
              :key="m.id"
              class="trow"
              @click="openDetail(m.id)"
            >
              <span class="c-title ellipsis">{{ m.title || m.code || ('#' + m.id) }}</span>
              <span class="c-rating">
                <b v-if="m.rating" class="gold">★{{ m.rating }}</b>
                <i v-else class="muted">—</i>
              </span>
              <span class="c-runtime tabular muted">{{ fmtRuntime(Math.round((m.runtime || 0) / 60)) }}</span>
              <span class="c-year tabular muted">{{ m.year || '—' }}</span>
              <span class="c-flags">
                <span v-if="m.favorite" class="pill gold">★</span>
                <span v-if="m.watchlist" class="pill accent">{{ $t('stats.stateWatchlist') }}</span>
                <span v-if="m.watched" class="pill ok">{{ $t('stats.stateWatched') }}</span>
                <span v-if="!m.watched && !m.watchlist && !m.favorite" class="pill muted">{{ $t('stats.dash') }}</span>
              </span>
            </div>
          </div>
        </section>
      </div>

      <!-- 观看分析（多维） -->
      <div v-show="activeTab==='wanalysis'">
        <div class="seg-tabs wa-seg">
          <button :class="{on: waSeg==='overview'}" @click="waSeg='overview'">{{ $t('stats.waSegOverview') }}</button>
          <button :class="{on: waSeg==='rhythm'}" @click="waSeg='rhythm'">{{ $t('stats.waSegRhythm') }}</button>
          <button :class="{on: waSeg==='preference'}" @click="waSeg='preference'">{{ $t('stats.waSegPref') }}</button>
        </div>

        <!-- 子页：概览 -->
        <template v-if="waSeg==='overview'">
          <section class="card panel">
            <div class="panel-head"><h2>{{ $t('stats.waOverview') }}</h2></div>
            <div class="wa-kpis">
              <div class="wa-kpi"><div class="wa-kpi-val tabular">{{ waTotal.sessions }}</div><div class="wa-kpi-label">{{ $t('stats.wlSessions') }}</div></div>
              <div class="wa-kpi"><div class="wa-kpi-val tabular">{{ wlFmtHours(waTotal.total_sec) }}</div><div class="wa-kpi-label">{{ $t('stats.wlDuration') }}</div></div>
              <div class="wa-kpi"><div class="wa-kpi-val tabular">{{ waTotal.movies }}</div><div class="wa-kpi-label">{{ $t('stats.wlMovies') }}</div></div>
              <div class="wa-kpi"><div class="wa-kpi-val tabular">{{ waTotal.days }}</div><div class="wa-kpi-label">{{ $t('stats.wlDays') }}</div></div>
              <div class="wa-kpi"><div class="wa-kpi-val tabular">{{ Math.round(completion.finish_rate*100) }}%</div><div class="wa-kpi-label">{{ $t('stats.waFinishRate') }}</div></div>
              <div class="wa-kpi"><div class="wa-kpi-val tabular">{{ Math.round(rewatch.rewatch_rate*100) }}%</div><div class="wa-kpi-label">{{ $t('stats.waRewatchRate') }}</div></div>
            </div>
          </section>

          <section class="card panel">
            <div class="panel-head"><h2>{{ $t('stats.waInsights') }}</h2><span class="muted small">{{ $t('stats.waInsightsHint') }}</span></div>
            <div v-if="!insights.length" class="muted small pad">{{ $t('stats.waNoData') }}</div>
            <div v-else class="wa-insights">
              <div v-for="(it,i) in insights" :key="i" class="wa-insight" :class="'tone-'+it.tone">
                <span class="wa-insight-icon">{{ it.icon }}</span>
                <span class="wa-insight-text">{{ insightText(it) }}</span>
              </div>
            </div>
          </section>

          <div class="wa-grid">
            <section class="card panel">
              <div class="panel-head"><h2>{{ $t('stats.waByMethod') }}</h2></div>
              <div v-if="!byMethodBars.length" class="muted small pad">{{ $t('stats.waNoData') }}</div>
              <div v-else class="bars">
                <div v-for="b in byMethodBars" :key="b.method" class="bar-row">
                  <span class="bar-label">{{ methodLabel(b.method) }}</span>
                  <span class="bar-track"><span class="bar-fill" :style="{ width: b.pct + '%' }"></span></span>
                  <span class="bar-val tabular">{{ b.sessions }} · {{ wlFmtHours(b.sec) }}</span>
                </div>
              </div>
            </section>

            <section class="card panel">
              <div class="panel-head"><h2>{{ $t('stats.waPrefer') }}</h2><span class="muted small">{{ $t('stats.waPreferHint') }}</span></div>
              <div class="wa-pref">
                <div v-for="d in profileDims" :key="d.key" class="wa-pref-col">
                  <div class="wa-pref-title">{{ d.label() }}</div>
                  <div v-if="!profileBars(d.key).length" class="muted small pad">{{ $t('stats.waNoData') }}</div>
                  <div v-else class="bars compact">
                    <div v-for="b in profileBars(d.key)" :key="b.name" class="bar-row" @click="drillTo(d.key === 'actresses' ? 'actress' : d.key, b.name)">
                      <span class="bar-label ellipsis" :title="b.name">{{ b.name }}</span>
                      <span class="bar-track"><span class="bar-fill" :style="{ width: b.pct + '%' }"></span></span>
                      <span class="bar-val tabular">{{ wlFmtHours(b.sec) }}</span>
                    </div>
                  </div>
                </div>
              </div>
            </section>
          </div>
        </template>

        <!-- 子页：节奏 -->
        <template v-else-if="waSeg==='rhythm'">
          <section class="card panel">
            <div class="panel-head"><h2>{{ $t('stats.waByHour') }}</h2><span class="muted small">{{ $t('stats.waByHourHint') }}</span></div>
            <div class="wa-hours">
              <div v-for="b in byHourBars" :key="b.h" class="wa-hour" :title="b.label + ':00'">
                <div class="wa-hour-bar" :style="{ height: b.pct + '%' }"></div>
                <span class="wa-hour-label">{{ b.h % 6 === 0 ? b.label : '' }}</span>
              </div>
            </div>
          </section>

          <div class="wa-grid">
            <section class="card panel">
              <div class="panel-head"><h2>{{ $t('stats.waByWeekday') }}</h2></div>
              <div class="wa-weekday">
                <div v-for="b in byWeekday" :key="b.wd" class="wa-wd" :title="b.label">
                  <div class="wa-wd-bar" :style="{ height: b.pct + '%' }"></div>
                  <span class="wa-wd-label">{{ b.label }}</span>
                </div>
              </div>
            </section>

            <section class="card panel">
              <div class="panel-head"><h2>{{ $t('stats.waStreak') }}</h2></div>
              <div class="wa-kpis">
                <div class="wa-kpi"><div class="wa-kpi-val tabular">{{ streak.current }}</div><div class="wa-kpi-label">{{ $t('stats.waStreakCur') }}</div></div>
                <div class="wa-kpi"><div class="wa-kpi-val tabular">{{ streak.max }}</div><div class="wa-kpi-label">{{ $t('stats.waStreakMax') }}</div></div>
                <div class="wa-kpi"><div class="wa-kpi-val tabular">{{ streak.longest_gap_days }}</div><div class="wa-kpi-label">{{ $t('stats.waGap') }}</div></div>
              </div>
              <div class="muted small pad">{{ $t('stats.waWeekSplit', { work: Math.round(weekdaySplit.workday_sec/3600), weekend: Math.round(weekdaySplit.weekend_sec/3600) }) }}</div>
            </section>
          </div>

          <section class="card panel">
            <div class="panel-head"><h2>{{ $t('stats.waTrend') }}</h2><span class="muted small">{{ $t('stats.waTrendHint') }}</span></div>
            <div class="wa-trend">
              <div class="wa-trend-side">
                <div class="wa-trend-num tabular" :class="trend.growth>=0?'up':'down'">{{ trend.growth>=0?'+':'' }}{{ Math.round(trend.growth*100) }}%</div>
                <div class="muted small">{{ $t('stats.waTrendGrowth') }}</div>
              </div>
              <div class="wa-trend-detail">
                <div class="bar-row"><span class="bar-label">{{ $t('stats.waRecent') }}</span><span class="bar-track"><span class="bar-fill" :style="{width: trendPct('recent')+'%'}"></span></span><span class="bar-val tabular">{{ wlFmtHours(trend.recent_sec) }}</span></div>
                <div class="bar-row"><span class="bar-label">{{ $t('stats.waOlder') }}</span><span class="bar-track"><span class="bar-fill" :style="{width: trendPct('older')+'%'}"></span></span><span class="bar-val tabular">{{ wlFmtHours(trend.older_sec) }}</span></div>
              </div>
            </div>
          </section>
        </template>

        <!-- 子页：偏好 × 内容 -->
        <template v-else-if="waSeg==='preference'">
          <div class="wa-pref-grid">
            <section class="card panel">
              <div class="panel-head"><h2>{{ $t('stats.waRatingX') }}</h2><span class="muted small">{{ $t('stats.waRatingXHint') }}</span></div>
              <div v-if="!ratingX.length" class="muted small pad">{{ $t('stats.waNoData') }}</div>
              <div v-else class="bars">
                <div v-for="b in ratingX" :key="b.band" class="bar-row">
                  <span class="bar-label">{{ b.label }}</span>
                  <span class="bar-track"><span class="bar-fill" :style="{ width: b.pct + '%' }"></span></span>
                  <span class="bar-val tabular">{{ b.sessions }} · {{ Math.round(b.avg_sec/60) }}m</span>
                </div>
              </div>
            </section>

            <section class="card panel">
              <div class="panel-head"><h2>{{ $t('stats.waUncX') }}</h2></div>
              <div class="wa-kpis">
                <div class="wa-kpi"><div class="wa-kpi-val tabular">{{ Math.round(uncensoredX.uncAvg/60) }}m</div><div class="wa-kpi-label">{{ $t('stats.waUncAvg') }}</div></div>
                <div class="wa-kpi"><div class="wa-kpi-val tabular">{{ Math.round(uncensoredX.cenAvg/60) }}m</div><div class="wa-kpi-label">{{ $t('stats.waCenAvg') }}</div></div>
                <div class="wa-kpi"><div class="wa-kpi-val tabular">{{ uncensoredX.uncensored.movies }}</div><div class="wa-kpi-label">{{ $t('stats.waUncMovies') }}</div></div>
              </div>
            </section>
          </div>

          <section class="card panel">
            <div class="panel-head"><h2>{{ $t('stats.waDurX') }}</h2><span class="muted small">{{ $t('stats.waDurXHint') }}</span></div>
            <div class="wa-dur">
              <div v-for="b in durationProfile" :key="b.key" class="wa-dur-row">
                <span class="wa-dur-label">{{ b.label }}</span>
                <div class="wa-dur-bars">
                  <div class="wa-dur-line"><span class="wa-dur-tag watched">{{ $t('stats.waWatched') }}</span><span class="bar-track"><span class="bar-fill ok" :style="{width: b.wPct+'%'}"></span></span><span class="bar-val tabular">{{ wlFmtHours(b.watched) }}</span></div>
                  <div class="wa-dur-line"><span class="wa-dur-tag library">{{ $t('stats.waLibrary') }}</span><span class="bar-track"><span class="bar-fill muted" :style="{width: b.lPct+'%'}"></span></span><span class="bar-val tabular">{{ b.library }}</span></div>
                </div>
              </div>
            </div>
          </section>
        </template>

        <EmptyState v-if="!waTotal.sessions" icon="👁" :title="$t('stats.wlEmpty')" :desc="$t('stats.wlEmptyDesc')" />
      </div>

      <!-- 观看明细 -->
      <div v-show="activeTab==='watchlog'">
        <div class="wh-filter">
          <div class="wh-fields">
            <label class="wh-field">
              <span>{{ $t('stats.wlFrom') }}</span>
              <input type="date" v-model="wlQuery.from" @change="applyWlFilter" />
            </label>
            <label class="wh-field">
              <span>{{ $t('stats.wlTo') }}</span>
              <input type="date" v-model="wlQuery.to" @change="applyWlFilter" />
            </label>
            <label class="wh-field">
              <span>{{ $t('stats.wlMethod') }}</span>
              <select v-model="wlQuery.method" @change="applyWlFilter">
                <option value="">{{ $t('stats.all') }}</option>
                <option value="builtin">{{ $t('rankings.methodBuiltin') }}</option>
                <option value="external">{{ $t('rankings.methodExternal') }}</option>
                <option value="system">{{ $t('rankings.methodSystem') }}</option>
              </select>
            </label>
            <label class="wh-field grow">
              <span>{{ $t('stats.wlQ') }}</span>
              <input type="text" v-model="wlQuery.q" :placeholder="$t('stats.wlQPh')" @keyup.enter="applyWlFilter" @blur="applyWlFilter" />
            </label>
            <button class="btn tiny ghost" v-if="wlHasFilter" @click="resetWlFilter">{{ $t('stats.reset') }}</button>
          </div>

          <div class="wh-summary" v-if="!wlLoading && wlTotal">
            <div class="wh-card">
              <div class="wh-card-val tabular">{{ wlSummary.sessions }}</div>
              <div class="wh-card-label">{{ $t('stats.wlSessions') }}</div>
            </div>
            <div class="wh-card">
              <div class="wh-card-val tabular">{{ wlFmtHours(wlSummary.total_sec) }}</div>
              <div class="wh-card-label">{{ $t('stats.wlDuration') }}</div>
            </div>
            <div class="wh-card">
              <div class="wh-card-val tabular">{{ wlSummary.movies }}</div>
              <div class="wh-card-label">{{ $t('stats.wlMovies') }}</div>
            </div>
            <div class="wh-card">
              <div class="wh-card-val tabular">{{ wlSummary.days }}</div>
              <div class="wh-card-label">{{ $t('stats.wlDays') }}</div>
            </div>
          </div>
        </div>

        <section class="card panel">
          <div class="panel-head">
            <h2>{{ $t('stats.watchlog') }}</h2>
            <span class="muted small">{{ $t('stats.wlCount', { n: wlTotal }) }}</span>
          </div>

          <div v-if="wlLoading" class="loading-block"><div class="spinner"></div></div>
          <EmptyState v-else-if="!wlItems.length" icon="👁" :title="$t('stats.wlEmpty')" :desc="$t('stats.wlEmptyDesc')" />

          <div v-else>
            <div class="tbl wl-tbl">
              <div class="trow th">
                <span class="c-movie">{{ $t('stats.colMovie') }}</span>
                <span class="c-when">{{ $t('stats.colWhen') }}</span>
                <span class="c-dur">{{ $t('stats.colDur') }}</span>
                <span class="c-method">{{ $t('stats.colMethod') }}</span>
              </div>
              <div
                v-for="h in wlItems"
                :key="h.id"
                class="trow"
                :class="{ 'no-src': !h.playable }"
                @click="openDetail(h.movie_id)"
              >
                <span class="c-movie ellipsis">{{ displayTitle(h) }}</span>
                <span class="c-when tabular muted">{{ fmtDate(h.started_at) }}</span>
                <span class="c-dur tabular muted">{{ fmtDuration(h.watched_sec) }}</span>
                <span class="c-method">
                  <span v-if="!h.playable" class="pill nosrc">{{ $t('stats.wlNoSource') }}</span>
                  <span v-else class="pill" :class="methodTone(h.method)">{{ methodLabel(h.method) }}</span>
                </span>
              </div>
            </div>

            <div class="wh-pager">
              <button class="btn tiny" :disabled="wlPage <= 1" @click="wlChangePage(-1)">‹</button>
              <span class="tabular">{{ wlPage }} / {{ Math.max(1, Math.ceil(wlTotal / 50)) }}</span>
              <button class="btn tiny" :disabled="wlPage * 50 >= wlTotal" @click="wlChangePage(1)">›</button>
            </div>
          </div>
        </section>
      </div>
    </template>
    </div>
  </section>
</template>

<style scoped>
.stats { padding-bottom: 40px; }
.loading-block { display: grid; place-items: center; gap: 12px; padding: 60px 0; }
.pad { padding: 16px; }
.muted { color: var(--c-text-3); }

.card.panel { margin-top: 18px; padding: 18px 20px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; gap: 12px; flex-wrap: wrap; }
.panel-head h2 { font-size: 15px; margin: 0; font-weight: 700; }

.seg { display: flex; gap: 4px; background: var(--c-surface-2); border-radius: 999px; padding: 3px; }
.seg button { border: 0; background: none; color: var(--c-text-3); font: inherit; font-size: 12px; font-weight: 600; padding: 5px 12px; border-radius: 999px; cursor: pointer; }
.seg button.on { background: var(--c-primary); color: #fff; }

.sort-tabs { display: flex; gap: 4px; background: var(--c-surface-2); border-radius: 999px; padding: 3px; }
.sort-tabs button { border: 0; background: none; color: var(--c-text-3); font: inherit; font-size: 12px; font-weight: 600; padding: 5px 10px; border-radius: 999px; cursor: pointer; }
.sort-tabs button.on { background: var(--c-primary); color: #fff; }

.dim-note { font-size: 12px; margin: -4px 0 10px; }

/* ===== 核心 KPI 行 ===== */
/* 全局搜索提示条 */
.search-hint {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 18px;
  padding: 10px 14px;
  background: color-mix(in srgb, var(--c-primary-h, #4f8cff) 10%, var(--c-surface-2, #161a22));
  border: 1px solid color-mix(in srgb, var(--c-primary-h, #4f8cff) 35%, transparent);
  border-radius: var(--r-md, 12px);
}
.search-hint .sh-ico { font-size: 16px; flex: 0 0 auto; }
.search-hint .sh-text { flex: 1; min-width: 0; font-size: var(--fs-sm); color: var(--c-text-2); }
.search-hint .sh-text b { color: var(--c-text); word-break: break-all; }
.search-hint .sh-go {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--c-primary-h, #4f8cff) 50%, transparent);
  background: color-mix(in srgb, var(--c-primary-h, #4f8cff) 16%, transparent);
  color: var(--c-primary-h, #4f8cff);
  font: inherit;
  font-size: var(--fs-sm);
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}
.search-hint .sh-go:hover:not(:disabled) { background: color-mix(in srgb, var(--c-primary-h, #4f8cff) 26%, transparent); }
.search-hint .sh-go:disabled { opacity: .6; cursor: default; }

.kpi-row {
  margin-top: 18px;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}
.kpi {
  position: relative;
  display: flex;
  align-items: center;
  gap: 14px;
  height: 84px;
  padding: 14px 18px;
  border-radius: var(--r-lg);
  background: var(--c-surface);
  border: 1px solid var(--c-line);
  box-shadow: var(--sh-card);
  overflow: hidden;
  transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease;
}
.kpi:hover { transform: translateY(-2px); box-shadow: 0 10px 28px rgba(0,0,0,.22); }
.kpi-ico {
  width: 40px; height: 40px; flex: none;
  display: grid; place-items: center; font-size: 20px;
  border-radius: 12px;
  background: var(--c-surface-3);
  border: 1px solid var(--c-line);
}
.kpi-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.kpi-label { font-size: 12px; color: var(--c-text-3); font-weight: 600; }
.kpi-value { font-size: 26px; font-weight: 800; line-height: 1.05; color: var(--c-text-1); letter-spacing: -.4px; }
.kpi-sub { font-size: 11px; }

/* 存储主角卡高亮 */
.kpi.storage {
  background: linear-gradient(135deg, color-mix(in srgb, var(--c-primary) 14%, var(--c-surface)), var(--c-surface));
  border-color: color-mix(in srgb, var(--c-primary) 45%, var(--c-line));
}
.kpi.storage .kpi-ico {
  background: linear-gradient(135deg, var(--c-primary), color-mix(in srgb, var(--c-primary) 70%, #fff));
  border-color: transparent;
  box-shadow: 0 6px 16px color-mix(in srgb, var(--c-primary) 45%, transparent);
}
.kpi.storage .kpi-value { color: var(--c-primary); }
.kpi.storage.empty { opacity: .65; }

/* KPI 已看占比环形角标 */
.kpi-ring {
  position: absolute; right: 14px; top: 14px;
  width: 30px; height: 30px; border-radius: 50%;
  background:
    conic-gradient(var(--c-primary) calc(var(--p) * 1%), var(--c-surface-3) 0);
  -webkit-mask: radial-gradient(closest-side, transparent 64%, #000 66%);
  mask: radial-gradient(closest-side, transparent 64%, #000 66%);
}

@media (max-width: 880px) { .kpi-row { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 520px) {
  .kpi-row { grid-template-columns: 1fr; }
  .kpi-value { font-size: 22px; }
}

/* 分区内小标题 */
.sub { font-size: 13px; font-weight: 700; color: var(--c-text-2); margin: 18px 0 10px; padding-left: 9px; position: relative; }
.sub::before { content: ''; position: absolute; left: 0; top: 2px; bottom: 2px; width: 3px; border-radius: 2px; background: var(--c-primary); }


/* 收藏情况总览卡（横向紧凑，与 KPI 等高） */
.filter-cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-top: 16px; }
@media (max-width: 720px) { .filter-cards { grid-template-columns: repeat(2, 1fr); } }

/* 分类切换标签（胶囊分段） */
.tabs {
  display: flex; gap: 6px; margin-top: 22px; flex-wrap: wrap;
  background: var(--c-surface-2);
  border: 1px solid var(--c-line);
  border-radius: 999px;
  padding: 4px;
}
.tabs button {
  appearance: none; border: 0; background: none; font: inherit; font-size: 13px; font-weight: 600;
  color: var(--c-text-3); padding: 8px 16px; cursor: pointer; border-radius: 999px;
  transition: color var(--t-fast), background var(--t-fast); white-space: nowrap;
}
.tabs button:hover { color: var(--c-text-1); }
.tabs button.on { color: #fff; background: var(--c-primary); box-shadow: 0 4px 12px color-mix(in srgb, var(--c-primary) 40%, transparent); }
.fcard {
  display: flex; flex-direction: row; align-items: center; gap: 14px;
  height: 84px;
  border: 1px solid var(--c-line); background: var(--c-surface); border-radius: var(--r-lg);
  padding: 14px 18px; cursor: pointer; color: var(--c-text); font: inherit; text-align: left;
  transition: transform .12s, border-color .12s, background .12s;
}
.fcard:hover { transform: translateY(-2px); border-color: var(--c-primary); background: var(--c-surface-2); }
.fi {
  width: 40px; height: 40px; flex: none;
  display: grid; place-items: center; font-size: 20px;
  border-radius: 12px; background: var(--c-surface-3); border: 1px solid var(--c-line);
}
.fcard-body { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.fv { font-size: 20px; font-weight: 800; line-height: 1.1; }
.fl { font-size: 11px; color: var(--c-text-3); }
.fcard.gold .fv { color: var(--c-gold); }
.fcard.accent .fv { color: var(--c-accent); }
.fcard.ok .fv { color: #2fae6a; }
.fcard.gold .fi { color: var(--c-gold); }
.fcard.accent .fi { color: var(--c-accent); }
.fcard.ok .fi { color: #2fae6a; }

/* 收藏质量 */
.quality { display: flex; flex-direction: column; gap: 14px; }
.q-row { display: flex; flex-direction: column; gap: 6px; }
.q-top { display: flex; justify-content: space-between; font-size: 13px; }
.q-label { font-weight: 600; color: var(--c-text-2); }
.q-val { color: var(--c-text-3); }
.q-hint { font-size: 12px; margin: 4px 0 0; }

/* 条形 / 轨道 */
.bars { display: flex; flex-direction: column; gap: 8px; }
.bar-row {
  display: grid; grid-template-columns: minmax(72px, auto) minmax(0, 1fr) minmax(0, 2.4fr) 56px; align-items: center; gap: 12px;
  min-height: 40px;
  border: 0; background: none; color: var(--c-text); font: inherit; text-align: left; cursor: pointer;
  padding: 4px 8px; border-radius: 10px; min-width: 0;
  transition: background .12s;
}
.bar-row:hover { background: var(--c-surface-2); }
.rank { color: var(--c-primary); font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; color: var(--c-text-1); }
.track { height: 8px; background: var(--c-surface-2); border-radius: 99px; overflow: hidden; }
.track i { display: block; height: 100%; background: linear-gradient(90deg, var(--c-primary), var(--c-accent)); border-radius: 99px; transition: width .4s; }
.track i.disk { background: linear-gradient(90deg, var(--c-primary), color-mix(in srgb, var(--c-primary) 55%, var(--c-accent))); }
.track i.disk2 { background: linear-gradient(90deg, var(--c-accent), color-mix(in srgb, var(--c-accent) 55%, var(--c-primary))); }
.track i.ok { background: linear-gradient(90deg, #2fae6a, #4cd295); }
.track i.warn { background: linear-gradient(90deg, #e0a32e, #f3c45a); }
.val { text-align: right; font-variant-numeric: tabular-nums; color: var(--c-text-2); white-space: nowrap; }
.file-ext {
  font-size: 11px; font-weight: 700; letter-spacing: .4px; text-align: center;
  padding: 3px 8px; border-radius: 6px; color: var(--c-accent);
  background: color-mix(in srgb, var(--c-accent) 16%, transparent);
  white-space: nowrap;
}
.file-row { cursor: pointer; }
.file-row:hover { background: var(--c-surface-3); }

.row { display: grid; grid-template-columns: 1fr 280px; gap: 18px; align-items: start; }
@media (max-width: 720px) { .row { grid-template-columns: 1fr; } }

/* 观看进度环 */
.watch-ring .ring-wrap { position: relative; display: grid; place-items: center; padding: 14px 0; }
.ring { width: 150px; height: 150px; }
.ring-bg { fill: none; stroke: var(--c-surface-2); stroke-width: 10; }
.ring-fg { fill: none; stroke: var(--c-primary); stroke-width: 10; stroke-linecap: round; transition: stroke-dasharray .5s; }
.ring-center { position: absolute; text-align: center; }
.ring-center b { font-size: 26px; }
.ring-center small { display: block; color: var(--c-text-3); }

/* 年份双趋势 */
.legend { display: flex; gap: 14px; font-size: 12px; color: var(--c-text-3); }
.legend .lg::before { content: ''; display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 5px; vertical-align: -1px; }
.legend .lg.count::before { background: var(--c-primary); }
.legend .lg.mins::before { background: var(--c-accent); }
.year-trend { display: flex; flex-direction: column; gap: 10px; }
.yt-row { display: grid; grid-template-columns: 48px 1fr 150px; align-items: center; gap: 10px; }
.yt-year { font-weight: 700; color: var(--c-text-2); font-variant-numeric: tabular-nums; }
.yt-bars { display: flex; flex-direction: column; gap: 5px; }
.track.sm { height: 7px; }
.track.sm i.count { background: var(--c-primary); }
.track.sm i.mins { background: var(--c-accent); }
.yt-val { text-align: right; font-size: 12px; color: var(--c-text-3); }

/* 趣味榜单 */
.fun-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
@media (max-width: 1100px) { .fun-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 520px) { .fun-grid { grid-template-columns: 1fr; } }
.fun-col { min-width: 0; }
.fun-col h3 { font-size: 13px; margin: 0 0 8px; color: var(--c-text-2); }
.fun-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.fun-list li { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 4px 6px; border-radius: 6px; cursor: pointer; min-width: 0; }
.fun-list li:hover { background: var(--c-surface-2); }
.fun-list .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: block; }
.gold { color: var(--c-gold); flex: none; }
.dim { color: var(--c-text-3); flex: none; }

.tagcloud { display: flex; flex-wrap: wrap; gap: 8px 12px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--c-line); }
.tc { color: var(--c-primary); font-weight: 600; }

/* 一览表 */
.tbl { display: flex; flex-direction: column; border-radius: 10px; min-width: 0; }
.trow { display: grid; grid-template-columns: minmax(0, 1fr) 60px 70px 60px 110px; align-items: center; gap: 10px; padding: 8px 10px; border-bottom: 1px solid var(--c-line); cursor: pointer; font-size: 13px; min-width: 0; background: none; color: inherit; font: inherit; text-align: left; width: 100%; border-left: 0; border-right: 0; border-top: 0; }
.trow:hover { background: var(--c-surface-2); }
.trow.th { background: var(--c-surface); font-weight: 700; color: var(--c-text-2); cursor: default; border-bottom: 2px solid var(--c-line); }
.trow.th:hover { background: var(--c-surface); }
.c-title { display: block; min-width: 0; }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tabular { font-variant-numeric: tabular-nums; }
.c-rating, .c-runtime, .c-year { text-align: right; }
.pill { display: inline-block; padding: 1px 7px; border-radius: 99px; font-size: 11px; font-weight: 600; }
.pill.gold { background: rgba(240,180,40,.16); color: var(--c-gold); }
.pill.accent { background: rgba(120,120,255,.16); color: var(--c-accent); }
.pill.ok { background: rgba(47,174,106,.16); color: #2fae6a; }
.pill.muted { background: var(--c-surface-2); color: var(--c-text-3); }

/* 观看明细：筛选栏与汇总 */
.wh-filter {
  display: flex; flex-direction: column; gap: var(--sp-3);
  margin-top: 18px; padding: var(--sp-3) var(--sp-4);
  background: var(--c-surface); border: 1px solid var(--c-line); border-radius: var(--r-md);
}
.wh-fields { display: flex; flex-wrap: wrap; align-items: flex-end; gap: var(--sp-3); }
.wh-field { display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-xs); color: var(--c-text-3); }
.wh-field.grow { flex: 1; min-width: 180px; }
.wh-field span { padding-left: 2px; }
.wh-field input, .wh-field select {
  height: 32px; padding: 0 var(--sp-2); border-radius: var(--r-sm);
  border: 1px solid var(--c-line); background: var(--c-surface-2); color: var(--c-text);
  font-size: var(--fs-sm); font-family: inherit;
}
.wh-field input:focus, .wh-field select:focus { outline: none; border-color: var(--c-primary); }

.wh-summary { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sp-3); }
.wh-card {
  display: flex; flex-direction: column; gap: 2px; padding: var(--sp-3);
  border-radius: var(--r-sm); background: var(--c-surface-2); border: 1px solid var(--c-line);
}
.wh-card-val { font-size: var(--fs-xl, 20px); font-weight: 700; color: var(--c-primary); }
.wh-card-label { font-size: var(--fs-xs); color: var(--c-text-3); }

.wh-pager {
  display: flex; align-items: center; justify-content: center; gap: var(--sp-3);
  padding: var(--sp-3); font-size: var(--fs-sm); color: var(--c-text-2);
}
.wh-pager button:disabled { opacity: .45; cursor: default; }

/* 观看明细表列 */
.wl-tbl .trow { grid-template-columns: minmax(0, 1fr) 130px 90px 110px; }
.wl-tbl .c-when, .wl-tbl .c-dur { text-align: right; }
.wl-tbl .c-method { text-align: right; }
.wl-tbl .pill { display: inline-block; padding: 1px 9px; border-radius: 99px; font-size: 11px; font-weight: 600; }
.wl-tbl .pill.ok { background: rgba(47,174,106,.16); color: #2fae6a; }
.wl-tbl .pill.accent { background: rgba(120,120,255,.16); color: var(--c-accent); }
.wl-tbl .pill.muted { background: var(--c-surface-2); color: var(--c-text-3); }
.wl-tbl .trow.no-src .c-movie { color: var(--c-text-3); }
.wl-tbl .pill.nosrc { background: var(--c-surface-2); color: var(--c-text-3); border: 1px solid var(--c-line); }

@media (max-width: 640px) {
  .wh-summary { grid-template-columns: repeat(2, 1fr); }
  .wl-tbl .trow { grid-template-columns: minmax(0,1fr) 96px; }
  .wl-tbl .c-dur, .wl-tbl .c-method { display: none; }
}

/* ===== 观看分析 ===== */
.wa-kpis { display: grid; grid-template-columns: repeat(6, 1fr); gap: var(--sp-3); padding: var(--sp-3); }
.wa-kpi { display: flex; flex-direction: column; gap: 2px; padding: var(--sp-3); border-radius: var(--r-sm); background: var(--c-surface-2); border: 1px solid var(--c-line); }
.wa-kpi-val { font-size: var(--fs-xl, 20px); font-weight: 700; color: var(--c-primary); }
.wa-kpi-label { font-size: var(--fs-xs); color: var(--c-text-3); }

.wa-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: var(--sp-4); margin-top: var(--sp-4); }
.wa-hours { display: flex; align-items: flex-end; gap: 3px; height: 160px; padding: 8px 4px 0; }
.wa-hour { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; gap: 4px; }
.wa-hour-bar { width: 70%; min-height: 2px; border-radius: 3px 3px 0 0; background: linear-gradient(180deg, var(--c-primary), color-mix(in srgb, var(--c-primary) 55%, #000)); }
.wa-hour-label { font-size: 9px; color: var(--c-text-3); }

.wa-months { display: flex; align-items: flex-end; gap: 6px; height: 180px; padding: 8px 4px 0; }
.wa-month { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; gap: 4px; }
.wa-month-bar { width: 70%; min-height: 2px; border-radius: 3px 3px 0 0; background: linear-gradient(180deg, var(--c-accent), color-mix(in srgb, var(--c-accent) 55%, #000)); cursor: default; }
.wa-month-label { font-size: 10px; color: var(--c-text-3); }

.wa-pref { display: grid; grid-template-columns: 1fr; gap: var(--sp-4); }
.wa-pref-col { min-width: 0; }
.wa-pref-title { font-size: var(--fs-sm); font-weight: 700; color: var(--c-text-2); margin-bottom: var(--sp-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bars.compact .bar-row {
  grid-template-columns: minmax(0, 200px) minmax(0, 1fr) auto;
  gap: 12px; margin-bottom: 6px; min-height: 34px; padding: 4px 6px;
}
.bars.compact .bar-label {
  font-size: 13px; line-height: 1.3; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; min-width: 0;
}
.bars.compact .bar-val { font-size: 12px; }
.pad { padding: var(--sp-3); }

/* 观看分析子页切换 */
.wa-seg { display: flex; gap: var(--sp-2); margin-bottom: var(--sp-3); flex-wrap: wrap; }
.wa-seg button { padding: 7px 16px; border-radius: 999px; border: 1px solid var(--c-border); background: var(--c-bg-soft); color: var(--c-text-2); font-size: var(--fs-sm); cursor: pointer; transition: .15s; }
.wa-seg button:hover { border-color: var(--c-accent); }
.wa-seg button.on { background: var(--c-accent); color: #fff; border-color: var(--c-accent); }

/* 洞察卡片 */
.wa-insights { display: flex; flex-direction: column; gap: var(--sp-2); }
.wa-insight { display: flex; align-items: flex-start; gap: var(--sp-2); padding: 10px 12px; border-radius: 10px; background: var(--c-bg-soft); border-left: 3px solid var(--c-border); font-size: var(--fs-sm); line-height: 1.5; }
.wa-insight-icon { font-size: 18px; line-height: 1.2; }
.wa-insight.tone-ok { border-left-color: var(--c-ok); }
.wa-insight.tone-warn { border-left-color: #e0a000; }
.wa-insight.tone-accent { border-left-color: var(--c-accent); }
.wa-insight.tone-muted { border-left-color: var(--c-text-3); }

/* 星期节律 */
.wa-weekday { display: flex; align-items: flex-end; gap: 6px; height: 160px; padding: 8px 4px 0; }
.wa-wd { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; gap: 4px; }
.wa-wd-bar { width: 60%; min-height: 2px; border-radius: 3px 3px 0 0; background: linear-gradient(180deg, var(--c-accent), color-mix(in srgb, var(--c-accent) 55%, #000)); }
.wa-wd-label { font-size: 10px; color: var(--c-text-3); }

/* 趋势 */
.wa-trend { display: flex; gap: var(--sp-4); align-items: center; flex-wrap: wrap; }
.wa-trend-side { text-align: center; min-width: 90px; }
.wa-trend-num { font-size: 28px; font-weight: 800; }
.wa-trend-num.up { color: var(--c-ok); }
.wa-trend-num.down { color: #e05555; }
.wa-trend-detail { flex: 1; min-width: 220px; }

/* 偏好子页全宽单列 */
.wa-pref-grid { display: grid; grid-template-columns: 1fr; gap: var(--sp-3); }

/* 时长档位 拥有 vs 观看 */
.wa-dur { display: flex; flex-direction: column; gap: var(--sp-3); }
.wa-dur-row { display: flex; align-items: center; gap: var(--sp-3); }
.wa-dur-label { width: 92px; font-size: var(--fs-sm); color: var(--c-text-2); flex-shrink: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.wa-dur-bars { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.wa-dur-line { display: flex; align-items: center; gap: var(--sp-2); }
.wa-dur-tag { width: 48px; font-size: 10px; text-align: center; padding: 2px 0; border-radius: 4px; flex-shrink: 0; }
.wa-dur-tag.watched { background: color-mix(in srgb, var(--c-ok) 18%, transparent); color: var(--c-ok); }
.wa-dur-tag.library { background: var(--c-bg-sunken); color: var(--c-text-3); }

@media (max-width: 900px) {
  .wa-kpis { grid-template-columns: repeat(3, 1fr); }
  .wa-grid { grid-template-columns: 1fr; }
}
@media (max-width: 640px) {
  .wa-kpis { grid-template-columns: repeat(2, 1fr); }
  .wa-insights { gap: var(--sp-2); }
}
</style>
