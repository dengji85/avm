<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { state } from '../state.js'
import { getStats, getStatsEnhanced, getWatchAnalytics } from '../api.js'
import { toast, fmtSize, fmtDuration, fmtNum } from '../utils.js'
import MovieGrid from '../components/MovieGrid.vue'

const s = reactive({})
const enh = reactive({ tag_cloud: [], runtime_by_year: [], watch_calendar: [] })
const watch = ref(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const [d, e, w] = await Promise.all([
      getStats(),
      getStatsEnhanced().catch(() => ({})),
      getWatchAnalytics().catch(() => null),
    ])
    Object.assign(s, d)
    enh.tag_cloud = e.tag_cloud || []
    enh.runtime_by_year = e.runtime_by_year || []
    enh.watch_calendar = e.watch_calendar || []
    watch.value = w
  } catch (e) { toast(e.message, 'err') } finally { loading.value = false }
}

/* ---- 概览卡 ---- */
const cards = computed(() => [
  { v: fmtNum(s.movies), l: '馆藏影片' },
  { v: fmtNum(s.files), l: '视频文件' },
  { v: fmtSize(s.size), l: '总容量' },
  { v: fmtNum(s.actresses), l: '女优' },
  { v: fmtNum(s.studios), l: '厂商' },
  { v: fmtNum(s.genres), l: '类型' },
])

/* 完成度环形指标 */
const rings = computed(() => {
  const total = Number(s.movies) || 0
  const mk = (label, val) => ({
    label,
    val: Number(val) || 0,
    total,
    pct: total ? Math.round(((Number(val) || 0) / total) * 100) : 0,
  })
  return [
    mk('已有封面', s.with_cover),
    mk('已刮削', s.scraped),
    mk('中文字幕', s.subtitle),
    mk('已收藏', s.favorite),
  ]
})

/* ---- 条形图数据 ---- */
function bars(items, labelKey, valKey = 'count', fmt = null) {
  if (!items || !items.length) return []
  const max = Math.max(...items.map((i) => Number(i[valKey]) || 0), 1)
  return items.slice(0, 12).map((i) => ({
    label: String(i[labelKey] ?? '未知'),
    pct: Math.max(2, ((Number(i[valKey]) || 0) / max) * 100),
    text: fmt ? fmt(i[valKey]) : `${i[valKey]} 部`,
  }))
}

const yearBars = computed(() => {
  const list = (s.by_year || []).slice().sort((a, b) => Number(b.year) - Number(a.year))
  return bars(list, 'year')
})
const actressBars = computed(() => bars(s.top_actresses, 'name'))
const studioBars = computed(() => bars(s.top_studios, 'name'))
const genreBars = computed(() => bars(s.top_genres, 'name'))

/* ---- 标签云 ---- */
const tagCloud = computed(() => {
  const list = (enh.tag_cloud || []).slice(0, 60)
  if (!list.length) return []
  const max = Math.max(...list.map((t) => t.count), 1)
  return list.map((t) => ({
    name: t.name,
    count: t.count,
    size: 11 + Math.round((t.count / max) * 12),
    op: 0.55 + (t.count / max) * 0.45,
  }))
})

/* ---- 观看热力图 ---- */
const calendar = computed(() => {
  const list = enh.watch_calendar || []
  if (!list.length) return []
  const max = Math.max(...list.map((d) => Number(d.sec) || 0), 1)
  return list.map((d) => {
    const r = (Number(d.sec) || 0) / max
    return {
      day: d.day,
      hours: ((Number(d.sec) || 0) / 3600).toFixed(1),
      level: r <= 0 ? 0 : r < 0.25 ? 1 : r < 0.5 ? 2 : r < 0.75 ? 3 : 4,
    }
  })
})

/* ---- 跳转筛选 ---- */
function jump(kind, value) {
  state.actress = kind === 'actress' ? [value] : []
  state.genre = kind === 'genre' ? [value] : []
  if (kind === 'studio') state.studio = value
  else state.studio = ''
  if (kind === 'year') state.year = value
  else state.year = null
  state.q = ''
  state.page = 1
  state.view = 'gallery'
}

function openDetail(id) { state.currentId = id }

onMounted(load)
</script>

<template>
  <section class="view">
    <div class="toolbar">
      <h1 class="tb-title">统计分析</h1>
      <span v-if="loading" class="spinner"></span>
      <div class="spacer"></div>
      <button class="btn tiny icon" @click="load" data-tip="刷新">⟳</button>
    </div>

    <div class="view-body">
      <!-- 概览 -->
      <div class="stat-cards">
        <div v-for="c in cards" :key="c.l" class="stat-card">
          <div class="stat-value">{{ c.v }}</div>
          <div class="stat-label">{{ c.l }}</div>
        </div>
      </div>

      <!-- 完成度 -->
      <div class="panel">
        <div class="panel-head">元数据完成度</div>
        <div class="panel-body ring-row">
          <div v-for="r in rings" :key="r.label" class="ring-item">
            <div class="ring" :style="{ '--p': r.pct }">
              <span class="tabular">{{ r.pct }}%</span>
            </div>
            <div class="ring-label">{{ r.label }}</div>
            <div class="ring-sub tabular">{{ r.val }} / {{ r.total }}</div>
          </div>
        </div>
      </div>

      <!-- 观看行为 -->
      <div v-if="watch" class="panel">
        <div class="panel-head">观看行为</div>
        <div class="panel-body">
          <div class="stat-cards">
            <div class="stat-card"><div class="stat-value">{{ fmtDuration(watch.total_seconds) }}</div><div class="stat-label">累计观看</div></div>
            <div class="stat-card"><div class="stat-value tabular">{{ watch.sessions || 0 }}</div><div class="stat-label">观看次数</div></div>
            <div class="stat-card"><div class="stat-value tabular">{{ watch.movies_watched || 0 }}</div><div class="stat-label">看过影片</div></div>
            <div class="stat-card"><div class="stat-value">{{ fmtDuration(watch.avg_session_seconds) }}</div><div class="stat-label">场均时长</div></div>
          </div>
        </div>
      </div>

      <!-- 分布 -->
      <div class="dist-grid">
        <div class="panel">
          <div class="panel-head">年份分布</div>
          <div class="panel-body">
            <div v-for="b in yearBars" :key="b.label" class="bar-row">
              <div class="bl" data-jump @click="jump('year', b.label)">{{ b.label }}</div>
              <div class="bar-track"><div class="bar-fill" :style="{ width: b.pct + '%' }"></div></div>
              <div class="bv">{{ b.text }}</div>
            </div>
            <p v-if="!yearBars.length" class="muted">暂无数据</p>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">女优 Top 12</div>
          <div class="panel-body">
            <div v-for="b in actressBars" :key="b.label" class="bar-row">
              <div class="bl" data-jump :title="b.label" @click="jump('actress', b.label)">{{ b.label }}</div>
              <div class="bar-track"><div class="bar-fill" :style="{ width: b.pct + '%' }"></div></div>
              <div class="bv">{{ b.text }}</div>
            </div>
            <p v-if="!actressBars.length" class="muted">暂无数据</p>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">厂商 Top 12</div>
          <div class="panel-body">
            <div v-for="b in studioBars" :key="b.label" class="bar-row">
              <div class="bl" data-jump :title="b.label" @click="jump('studio', b.label)">{{ b.label }}</div>
              <div class="bar-track"><div class="bar-fill" :style="{ width: b.pct + '%' }"></div></div>
              <div class="bv">{{ b.text }}</div>
            </div>
            <p v-if="!studioBars.length" class="muted">暂无数据</p>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">类型 Top 12</div>
          <div class="panel-body">
            <div v-for="b in genreBars" :key="b.label" class="bar-row">
              <div class="bl" data-jump :title="b.label" @click="jump('genre', b.label)">{{ b.label }}</div>
              <div class="bar-track"><div class="bar-fill" :style="{ width: b.pct + '%' }"></div></div>
              <div class="bv">{{ b.text }}</div>
            </div>
            <p v-if="!genreBars.length" class="muted">暂无数据</p>
          </div>
        </div>
      </div>

      <!-- 标签云 -->
      <div class="panel">
        <div class="panel-head">标签云 <span class="sub">点击可筛选</span></div>
        <div class="panel-body">
          <div v-if="tagCloud.length" class="tag-cloud">
            <button
              v-for="t in tagCloud"
              :key="t.name"
              class="tc"
              :style="{ fontSize: t.size + 'px', opacity: t.op }"
              :title="`${t.name} · ${t.count} 部`"
              @click="jump('genre', t.name)"
            >{{ t.name }}</button>
          </div>
          <p v-else class="muted">暂无标签数据</p>
        </div>
      </div>

      <!-- 观看日历 -->
      <div class="panel">
        <div class="panel-head">观看热力图 <span class="sub">最近一年</span></div>
        <div class="panel-body">
          <div v-if="calendar.length" class="cal-wrap">
            <div class="cal-heat">
              <div
                v-for="(d, i) in calendar"
                :key="i"
                class="cell"
                :class="'l' + d.level"
                :title="`${d.day}：${d.hours} 小时`"
              ></div>
            </div>
            <div class="cal-legend">
              <span class="muted">少</span>
              <div class="cell l0"></div><div class="cell l1"></div>
              <div class="cell l2"></div><div class="cell l3"></div><div class="cell l4"></div>
              <span class="muted">多</span>
            </div>
          </div>
          <p v-else class="muted">还没有观看记录，用内置播放器观看后会自动统计。</p>
        </div>
      </div>

      <!-- 最近添加 -->
      <section v-if="(s.recent || []).length">
        <div class="section-title">最近添加</div>
        <MovieGrid :items="s.recent" @open="openDetail" />
      </section>
    </div>
  </section>
</template>

<style scoped>
.dist-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: var(--sp-4);
}

/* 环形进度 */
.ring-row { flex-direction: row; flex-wrap: wrap; gap: var(--sp-6); }
.ring-item { display: flex; flex-direction: column; align-items: center; gap: var(--sp-1); }
.ring {
  width: 76px; height: 76px;
  border-radius: 50%;
  display: grid; place-items: center;
  background: conic-gradient(var(--c-primary) calc(var(--p) * 1%), var(--c-surface-3) 0);
  position: relative;
}
.ring::before {
  content: '';
  position: absolute; inset: 8px;
  border-radius: 50%;
  background: var(--c-surface);
}
.ring span {
  position: relative;
  font-size: var(--fs-lg);
  font-weight: 650;
}
.ring-label { font-size: var(--fs-md); }
.ring-sub { font-size: var(--fs-xs); color: var(--c-text-3); }

/* 标签云 */
.tag-cloud {
  display: flex; flex-wrap: wrap;
  gap: var(--sp-2) var(--sp-3);
  align-items: baseline;
  line-height: 1.9;
}
.tc {
  color: var(--c-text-2);
  transition: color var(--t-fast), transform var(--t-fast);
}
.tc:hover { color: var(--c-primary-h); transform: scale(1.08); }

/* 热力图 */
.cal-wrap { display: flex; flex-direction: column; gap: var(--sp-3); }
.cal-heat {
  display: grid;
  grid-template-rows: repeat(7, 11px);
  grid-auto-flow: column;
  grid-auto-columns: 11px;
  gap: 3px;
  overflow-x: auto;
  padding-bottom: var(--sp-1);
}
.cell {
  width: 11px; height: 11px;
  border-radius: 2px;
  background: var(--c-surface-3);
}
.cell.l0 { background: var(--c-surface-3); }
.cell.l1 { background: rgba(224, 53, 90, .28); }
.cell.l2 { background: rgba(224, 53, 90, .5); }
.cell.l3 { background: rgba(224, 53, 90, .74); }
.cell.l4 { background: var(--c-primary); }
.cal-legend { display: flex; align-items: center; gap: 4px; font-size: var(--fs-xs); }
</style>
