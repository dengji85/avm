<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { state } from '../state.js'
import { getStats, getStatsEnhanced, getRankings, listMovies, getStorage } from '../api.js'
import { fmtMin, fmtSize } from '../utils.js'
import PageHead from '../components/PageHead.vue'
import StatGrid from '../components/StatGrid.vue'
import EmptyState from '../components/EmptyState.vue'

const loading = ref(true)
const error = ref('')
const activeTab = ref('storage')

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
    { label: '有封面', value: overview.withCover, pct: pct(overview.withCover), tone: 'ok' },
    { label: '已刮削', value: overview.scraped, pct: pct(overview.scraped), tone: 'accent' },
    { label: '无番号', value: overview.noCode, pct: pct(overview.noCode), tone: overview.noCode ? 'warn' : 'ok' },
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

const favNote = computed(() => (favDim.value === 'actress' ? '样本较少，多数女优仅 1 部' : ''))

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
    { label: '文件总数', value: (t.files || 0).toLocaleString() },
    { label: '影片数', value: (t.movies || 0).toLocaleString() },
    { label: '平均 / 部', value: fmtSize(avgSize.value) },
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

    // 观看最久：从已看且有真实时长的影片里取 Top10（不依赖 watched_sec 字段）
    watchedTop.value = (allMovies.value || [])
      .filter((m) => m && m.watched && (m.runtime || 0) > 0)
      .sort((a, b) => (b.runtime || 0) - (a.runtime || 0))
      .slice(0, 10)
  } catch (e) {
    error.value = e.message || '统计加载失败'
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
  { key: 'favorite', label: '收藏', value: overview.favorite, icon: '⭐', tone: 'gold' },
  { key: 'watchlist', label: '想看', value: overview.watchlist, icon: '🔖', tone: 'accent' },
  { key: 'watched', label: '已看', value: overview.watched, icon: '✅', tone: 'ok' },
  { key: 'unwatched', label: '未看', value: overview.totalMovies - overview.watched, icon: '⏳', tone: 'muted' },
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
</script>

<template>
  <section class="view">
    <div class="view-body stats block-scroll">
    <PageHead title="统计" :sub="`${overview.totalMovies} 部影片的收藏全景`">
      <template #actions>
        <button class="btn ghost" @click="loadAll">刷新</button>
      </template>
    </PageHead>

    <div v-if="loading" class="loading-block">
      <div class="spinner big"></div>
      <p class="muted">正在统计你的收藏…</p>
    </div>

    <EmptyState v-else-if="error" icon="!" :title="error" action="重试" @action="loadAll" />

    <template v-else>
      <!-- ===== 核心 KPI 行 ===== -->
      <div class="kpi-row">
        <div class="kpi storage" :class="{ empty: !diskTotal }">
          <span class="kpi-ico">💾</span>
          <div class="kpi-body">
            <span class="kpi-label">总存储占用</span>
            <span class="kpi-value tabular">{{ fmtSize(diskTotal) || '—' }}</span>
            <span class="kpi-sub muted">{{ (storage.total.files || 0).toLocaleString() }} 文件 · 均 {{ fmtSize(avgSize) }}/部</span>
          </div>
        </div>
        <div class="kpi">
          <span class="kpi-ico">🎞️</span>
          <div class="kpi-body">
            <span class="kpi-label">影片总数</span>
            <span class="kpi-value tabular">{{ overview.totalMovies.toLocaleString() }}</span>
            <span class="kpi-sub muted">本月 +{{ overview.addedThisMonth }}</span>
          </div>
        </div>
        <div class="kpi">
          <span class="kpi-ico">⏱️</span>
          <div class="kpi-body">
            <span class="kpi-label">总时长</span>
            <span class="kpi-value tabular">{{ fmtRuntime(overview.runtimeMin) }}</span>
            <span class="kpi-sub muted">{{ overview.actresses }} 女优 · {{ overview.series }} 系列</span>
          </div>
        </div>
        <div class="kpi">
          <span class="kpi-ico">👁️</span>
          <div class="kpi-body">
            <span class="kpi-label">已看占比</span>
            <span class="kpi-value tabular">{{ overview.watchedPct }}%</span>
            <span class="kpi-sub muted">{{ overview.watched }}/{{ overview.totalMovies }} 部</span>
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
        <button :class="{on: activeTab==='storage'}" @click="activeTab='storage'">存储分布</button>
        <button :class="{on: activeTab==='collect'}" @click="activeTab='collect'">收藏健康</button>
        <button :class="{on: activeTab==='favorite'}" @click="activeTab='favorite'">最爱榜单</button>
        <button :class="{on: activeTab==='trend'}" @click="activeTab='trend'">年份趋势</button>
        <button :class="{on: activeTab==='fun'}" @click="activeTab='fun'">趣味榜单</button>
        <button :class="{on: activeTab==='movies'}" @click="activeTab='movies'">影片一览</button>
      </div>

      <!-- 存储分布 -->
      <div v-show="activeTab==='storage'">
        <section class="card panel">
          <div class="panel-head">
            <h2>存储分布</h2>
            <span class="muted small">共 {{ storage.total.bytes ? fmtSize(diskTotal) : '0' }} · {{ storage.total.movies || 0 }} 部</span>
          </div>

          <div v-if="!diskBars.length" class="muted pad">暂无文件记录</div>
          <template v-else>
            <div class="bars">
              <div v-for="d in diskBars" :key="d.label" class="bar-row" style="cursor: default">
                <span class="rank disk">{{ d.label }}</span>
                <span class="name ellipsis">{{ fmtSize(d.value) }} · {{ d.movies }} 部 / {{ d.files }} 文件</span>
                <span class="track"><i class="disk" :style="{ width: d.pct + '%' }"></i></span>
                <span class="val tabular">{{ Math.round((d.value / diskTotal) * 100) }}%</span>
              </div>
            </div>

            <h3 class="sub">厂商占用 Top</h3>
            <div v-if="!studioBars.length" class="muted pad">暂无数据</div>
            <div v-else class="bars">
              <div v-for="d in studioBars" :key="d.label" class="bar-row" style="cursor: default">
                <span class="rank disk">{{ fmtSize(d.value) }}</span>
                <span class="name ellipsis">{{ d.label }}</span>
                <span class="track"><i class="disk2" :style="{ width: d.pct + '%' }"></i></span>
                <span class="val tabular">{{ Math.round((d.value / diskTotal) * 100) }}%</span>
              </div>
            </div>

            <h3 class="sub">按内容类型</h3>
            <div v-if="!byGenreBars.length" class="muted pad">暂无数据</div>
            <div v-else class="bars">
              <div v-for="d in byGenreBars" :key="d.label" class="bar-row" style="cursor: default">
                <span class="rank disk">{{ d.label }}</span>
                <span class="name ellipsis">{{ d.movies }} 部 · 占 {{ d.share }}%</span>
                <span class="track"><i class="disk" :style="{ width: d.pct + '%' }"></i></span>
                <span class="val tabular">{{ fmtSize(d.value) }}</span>
              </div>
            </div>

            <h3 class="sub">按文件格式</h3>
            <div v-if="!byExtBars.length" class="muted pad">暂无数据</div>
            <div v-else class="bars">
              <div v-for="d in byExtBars" :key="d.label" class="bar-row" style="cursor: default">
                <span class="rank disk">{{ d.label }}</span>
                <span class="name ellipsis">{{ d.files }} 个文件 · 占 {{ d.share }}%</span>
                <span class="track"><i class="disk2" :style="{ width: d.pct + '%' }"></i></span>
                <span class="val tabular">{{ fmtSize(d.value) }}</span>
              </div>
            </div>

            <h3 class="sub">最大文件 Top</h3>
            <div v-if="!largestFiles.length" class="muted pad">暂无数据</div>
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
            <div class="panel-head"><h2>收藏质量</h2></div>
            <div class="quality">
              <div v-for="q in quality" :key="q.label" class="q-row">
                <div class="q-top">
                  <span class="q-label">{{ q.label }}</span>
                  <span class="q-val tabular">{{ q.value }} / {{ overview.totalMovies }}（{{ q.pct }}%）</span>
                </div>
                <span class="track"><i :class="q.tone" :style="{ width: q.pct + '%' }"></i></span>
              </div>
              <p class="q-hint muted">
                收藏 {{ overview.favorite }} · 想看 {{ overview.watchlist }} · 有字幕 {{ overview.subtitle }} · 无码 {{ overview.uncensored }}
              </p>
            </div>
          </section>

          <section class="card panel watch-ring">
            <div class="panel-head"><h2>观看进度</h2></div>
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
                <small>已看 {{ overview.watched }}/{{ overview.totalMovies }}</small>
              </div>
            </div>
          </section>
        </div>
      </div>

      <!-- 最爱榜单 -->
      <div v-show="activeTab==='favorite'">
        <section class="card panel">
          <div class="panel-head">
            <h2>最爱榜单</h2>
            <div class="seg">
              <button :class="{on: favDim==='actress'}" @click="favDim='actress'">女优</button>
              <button :class="{on: favDim==='studio'}" @click="favDim='studio'">厂商</button>
              <button :class="{on: favDim==='series'}" @click="favDim='series'">系列</button>
              <button :class="{on: favDim==='genre'}" @click="favDim='genre'">类型</button>
            </div>
          </div>
          <p v-if="favNote" class="dim-note muted">{{ favNote }}</p>
          <div v-if="!favBars.length" class="muted pad">暂无数据</div>
          <div v-else class="bars">
            <button
              v-for="(b, i) in favBars"
              :key="b.label"
              class="bar-row"
              @click="drillTo(favDim, b.label)"
              :title="`查看 ${b.label} 的全部影片`"
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
            <h2>年份趋势</h2>
            <div class="legend">
              <span class="lg count">收藏数</span>
              <span class="lg mins">时长</span>
            </div>
          </div>
          <div v-if="!yearTrend.length" class="muted pad">暂无数据</div>
          <div v-else class="year-trend">
            <div v-for="y in yearTrend" :key="y.year" class="yt-row">
              <span class="yt-year">{{ y.year }}</span>
              <div class="yt-bars">
                <span class="track sm"><i class="count" :style="{ width: y.countPct + '%' }"></i></span>
                <span class="track sm"><i class="mins" :style="{ width: y.minPct + '%' }"></i></span>
              </div>
              <span class="yt-val tabular">{{ y.count }} 部 · {{ fmtRuntime(Math.round(y.minutes / 60)) }}</span>
            </div>
          </div>
        </section>
      </div>

      <!-- 趣味榜单 -->
      <div v-show="activeTab==='fun'">
        <section class="card panel">
          <div class="panel-head"><h2>趣味榜单</h2></div>
          <div class="fun-grid">
            <div class="fun-col">
              <h3>🏆 评分最高</h3>
              <ol class="fun-list">
                <li v-for="m in ratingTop" :key="m.id" @click="openDetail(m.id)">
                  <span class="t ellipsis">{{ m.title || m.code }}</span><span class="gold">★{{ m.rating }}</span>
                </li>
                <li v-if="!ratingTop.length" class="muted">暂无评分记录</li>
              </ol>
            </div>
            <div class="fun-col">
              <h3>💩 评分最低</h3>
              <ol class="fun-list">
                <li v-for="m in ratingLow" :key="m.id" @click="openDetail(m.id)">
                  <span class="t ellipsis">{{ m.title || m.code }}</span><span class="dim">★{{ m.rating }}</span>
                </li>
                <li v-if="!ratingLow.length" class="muted">暂无评分记录</li>
              </ol>
            </div>
            <div class="fun-col">
              <h3>🔥 播放最多</h3>
              <ol class="fun-list">
                <li v-for="m in playTop" :key="m.id" @click="openDetail(m.id)">
                  <span class="t ellipsis">{{ m.title || m.code }}</span><span class="dim">×{{ m.play_count }}</span>
                </li>
                <li v-if="!playTop.length" class="muted">暂无播放记录</li>
              </ol>
            </div>
            <div class="fun-col">
              <h3>📺 观看最久</h3>
              <ol class="fun-list">
                <li v-for="m in watchedTop" :key="m.id" @click="openDetail(m.id)">
                  <span class="t ellipsis">{{ m.title || m.code }}</span><span class="dim">{{ fmtMin(Math.round((m.watched_sec || 0) / 60)) }}</span>
                </li>
                <li v-if="!watchedTop.length" class="muted">暂无观看记录</li>
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
            <h2>所有影片一览（{{ allMovies.length }}）</h2>
            <div class="sort-tabs">
              <button :class="{on: sortKey==='rating'}" @click="setSort('rating')">评分</button>
              <button :class="{on: sortKey==='runtime'}" @click="setSort('runtime')">时长</button>
              <button :class="{on: sortKey==='year'}" @click="setSort('year')">年份</button>
              <button :class="{on: sortKey==='watched'}" @click="setSort('watched')">已看</button>
              <button :class="{on: sortKey==='favorite'}" @click="setSort('favorite')">收藏</button>
            </div>
          </div>
          <div class="tbl">
            <div class="trow th">
              <span class="c-title">影片</span>
              <span class="c-rating">评分</span>
              <span class="c-runtime">时长</span>
              <span class="c-year">年份</span>
              <span class="c-flags">状态</span>
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
                <span v-if="m.watchlist" class="pill accent">想看</span>
                <span v-if="m.watched" class="pill ok">已看</span>
                <span v-if="!m.watched && !m.watchlist && !m.favorite" class="pill muted">—</span>
              </span>
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
</style>
