<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { state, SORTS, FLAGS, resetFilters, hasActiveFilter } from '../state.js'
import { useLibrary } from '../composables/useLibrary.js'
import { getContinueWatching, clearContinueWatching, deleteMovie, scrapeOne, addToCollection, coverThumbUrl } from '../api.js'
import { toast, confirmDialog, coverFallback } from '../utils.js'
import { t } from '../i18n'
import { useTasks } from '../composables/useTasks.js'

import MovieGrid from '../components/MovieGrid.vue'
import MovieCard from '../components/MovieCard.vue'
import Pager from '../components/Pager.vue'
import BulkBar from '../components/BulkBar.vue'
import MovieFilter from '../components/MovieFilter.vue'
import ContextMenu from '../components/ContextMenu.vue'

const { items, total, loading, pageCount, load, patchItem } = useLibrary()
const { runScan } = useTasks()

const cont = ref([])
const contLoading = ref(false)

/* 视图模式：grid | list */
const viewMode = ref('grid')

/* 手机端筛选栏折叠状态：默认收起，点击「筛选条件」以浮层弹出，不遮盖影片内容 */
const showFilter = ref(false)

/* 移动端沉浸式：向上浏览隐藏顶栏/工具栏，下拉到顶部恢复 */
const navHidden = ref(false)
let vbEl = null
let mqMobile = null
function onViewScroll() {
  if (!mqMobile || !mqMobile.matches) { navHidden.value = false; return }
  if (!vbEl) return
  navHidden.value = vbEl.scrollTop > 80
}
function syncBodyClass() {
  document.body.classList.toggle('nav-hidden', navHidden.value)
}
watch(navHidden, syncBodyClass)

const showContinue = computed(() =>
  !hasActiveFilter() && state.page === 1 && cont.value.length > 0,
)

async function loadContinue() {
  contLoading.value = true
  try { cont.value = (await getContinueWatching(12)) || [] }
  catch (e) { cont.value = [] }
  finally { contLoading.value = false }
}

async function dismissContinue() {
  if (!(await confirmDialog('清空继续观看', '将移除所有未看完记录，不影响影片本身。'))) return
  try { await clearContinueWatching(); cont.value = []; toast('已清空', 'ok') }
  catch (e) { toast(e.message, 'err') }
}

function openDetail(id) { state.currentId = id }

/* 从详情页点筛选条件跳来后，一键返回原详情 */
function returnToDetail() {
  if (state.returnFromFilter) state.currentId = state.returnFromFilter.id
}

/* 右键菜单 */
const ctx = reactive({ visible: false, x: 0, y: 0, movie: null })
const ctxItems = computed(() => [
  { label: 'common.editMeta', icon: '✎', action: 'edit' },
  { label: 'common.rescrape', icon: '⟳', action: 'scrape' },
  { label: 'detail.addToCollection', icon: '＋', action: 'collection' },
  { label: 'common.delete', icon: '🗑', danger: true, action: 'delete' },
])
function openCtx(movie, ev) {
  ctx.movie = movie
  ctx.x = ev.clientX
  ctx.y = ev.clientY
  ctx.visible = true
}
function onCtxSelect(it) {
  const m = ctx.movie
  if (!m) return
  if (it.action === 'edit') { state.currentId = m.id; state.detailOpen = true }
  else if (it.action === 'scrape') { scrapeOne(m.id).then(() => toast(t('common.scrapeQueued'), 'ok')).catch(e => toast('失败：' + e.message, 'err')) }
  else if (it.action === 'collection') { addToCollection(m.id).then(() => toast(t('detail.joinedCollection'), 'ok')).catch(e => toast('失败：' + e.message, 'err')) }
  else if (it.action === 'delete') {
    if (window.confirm(`确定删除《${m.title || m.code}》？`)) {
      deleteMovie(m.id).then(() => { toast('已删除', 'ok'); load() }).catch(e => toast('删除失败：' + e.message, 'err'))
    }
  }
}
function closeCtx() { ctx.visible = false }

/* 快捷键：f 聚焦筛选 / Esc 关闭菜单、退出多选 */
function onKey(e) {
  if (e.target.matches && e.target.matches('input, textarea, select')) return
  if (e.key === 'Escape') {
    if (ctx.visible) { closeCtx(); return }
    if (state.selMode) { toggleSelMode(); return }
  }
  if ((e.key === 'f' || e.key === 'F') && !ctx.visible) {
    e.preventDefault()
    const el = document.querySelector('.movie-filter input[type="search"]')
    if (el) el.focus()
  }
}
onMounted(() => {
  window.addEventListener('keydown', onKey)
  window.addEventListener('click', closeCtx)
  window.addEventListener('scroll', closeCtx, true)
  mqMobile = window.matchMedia('(max-width: 900px)')
  vbEl = document.querySelector('.view-body')
  if (vbEl) vbEl.addEventListener('scroll', onViewScroll, { passive: true })
  mqMobile.addEventListener('change', onViewScroll)
  onViewScroll()
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('click', closeCtx)
  window.removeEventListener('scroll', closeCtx, true)
  if (vbEl) vbEl.removeEventListener('scroll', onViewScroll)
  if (mqMobile) mqMobile.removeEventListener('change', onViewScroll)
  document.body.classList.remove('nav-hidden')
})

/* 筛选摘要（用于顶部筛选条） */
const activeChips = computed(() => {
  const out = []
  if (state.q) out.push({ k: 'q', label: `搜索：${state.q}` })
  state.actress.forEach((a) => out.push({ k: 'actress', v: a, label: `女优：${a}` }))
  state.genre.forEach((g) => out.push({ k: 'genre', v: g, label: `类型：${g}` }))
  if (state.studio) out.push({ k: 'studio', label: `厂商：${state.studio}` })
  if (state.series) out.push({ k: 'series', label: `系列：${state.series}` })
  if (state.prefix) out.push({ k: 'prefix', label: `前缀：${state.prefix}` })
  if (state.year) out.push({ k: 'year', label: `年份：${state.year}` })
  state.flags.forEach((f) => {
    const hit = FLAGS.find((x) => x[0] === f)
    out.push({ k: 'flag', v: f, label: hit ? hit[1] : f })
  })
  return out
})

function removeChip(c) {
  if (c.k === 'q') state.q = ''
  else if (c.k === 'actress') state.actress.splice(state.actress.indexOf(c.v), 1)
  else if (c.k === 'genre') state.genre.splice(state.genre.indexOf(c.v), 1)
  else if (c.k === 'flag') state.flags.splice(state.flags.indexOf(c.v), 1)
  else if (c.k === 'year') state.year = null
  else state[c.k] = ''
  state.page = 1
}

function toggleSelMode() {
  state.selMode = !state.selMode
  if (!state.selMode) state.selected = new Set()
}
function selectPage() {
  state.selMode = true
  state.selected = new Set(items.value.map((m) => m.id))
}

function toggleRow(id) {
  const s = new Set(state.selected)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  state.selected = s
}

function goPage(p) {
  state.page = p
  document.querySelector('.view-body')?.scrollTo({ top: 0, behavior: 'smooth' })
}

function onBulkDone() {
  state.selected = new Set()
  state.selMode = false
  load()
}

onMounted(() => {
  load()
  loadContinue()
})
</script>

<template>
  <section class="view gallery-layout">
    <!-- 手机端：筛选收起为按钮，点击展开 -->
    <button class="btn filter-toggle" :class="{ active: showFilter }" @click="showFilter = !showFilter">
      {{ showFilter ? '收起筛选 ▲' : '筛选条件 ▼' }}
    </button>

    <!-- 手机端：筛选浮层遮罩，点击关闭 -->
    <div class="filter-mask" v-if="showFilter" @click="showFilter = false"></div>

    <!-- 常驻筛选侧栏（手机上作为浮层） -->
    <aside class="filter-rail" :class="{ open: showFilter }">
      <MovieFilter />
      <button class="btn filter-done" @click="showFilter = false">完成</button>
    </aside>

    <!-- 主区 -->
    <div class="gallery-main">
      <!-- 工具栏 -->
      <div class="toolbar">
        <h1 class="tb-title">{{ $t('view.gallery') }}</h1>
        <button v-if="state.returnFromFilter" class="btn tiny back-detail" @click="returnToDetail" data-tip="返回你刚才看的详情">
          ← 返回《{{ state.returnFromFilter.title }}》
        </button>
        <span class="tb-sub tabular" v-if="!loading">{{ total }} 部</span>
        <span v-else class="spinner"></span>

        <div class="spacer"></div>

        <!-- 多条件逻辑 -->
        <div class="btn-group" v-if="state.actress.length > 1 || state.genre.length > 1">
          <button class="btn tiny" :class="{ active: state.multiOp === 'OR' }" @click="state.multiOp = 'OR'" data-tip="任一匹配">任一</button>
          <button class="btn tiny" :class="{ active: state.multiOp === 'AND' }" @click="state.multiOp = 'AND'" :data-tip="$t('view.andMatch')">{{ $t('common.all') }}</button>
        </div>

        <div class="seg">
          <button :class="{ on: viewMode === 'grid' }" @click="viewMode = 'grid'" data-tip="网格">▦</button>
          <button :class="{ on: viewMode === 'list' }" @click="viewMode = 'list'" data-tip="列表">☰</button>
        </div>

        <select class="sort-sel" v-model="state.sort">
          <option v-for="[v, t] in SORTS" :key="v" :value="v">{{ $t(t) }}</option>
        </select>

        <!-- 卡片尺寸 -->
        <div class="btn-group">
          <button class="btn tiny icon" :class="{ active: state.cardSize === 'dense' }" @click="state.cardSize = 'dense'" data-tip="小图">▪</button>
          <button class="btn tiny icon" :class="{ active: state.cardSize === 'normal' }" @click="state.cardSize = 'normal'" data-tip="中图">◼</button>
          <button class="btn tiny icon" :class="{ active: state.cardSize === 'large' }" @click="state.cardSize = 'large'" data-tip="大图">⬛</button>
        </div>

        <button class="btn tiny" :class="{ active: state.selMode }" @click="toggleSelMode">
          {{ state.selMode ? '退出多选' : '多选' }}
        </button>
        <button v-if="state.selMode" class="btn tiny" @click="selectPage">选中本页</button>

        <button class="btn tiny icon" @click="load()" data-tip="刷新">⟳</button>
      </div>

      <!-- 激活筛选条 -->
      <div v-if="activeChips.length" class="filter-bar">
        <span class="fb-label">{{ $t('view.filter') }}</span>
        <button v-for="(c, i) in activeChips" :key="i" class="chip on" @click="removeChip(c)">
          {{ c.label }} <span class="x">✕</span>
        </button>
        <button class="btn tiny ghost" @click="resetFilters">{{ $t('view.clearAll') }}</button>
      </div>

      <!-- 内容 -->
      <div class="view-body">
        <!-- 继续观看 -->
        <section v-if="showContinue" class="cw">
          <div class="section-title">
            继续观看
            <span class="count">{{ cont.length }}</span>
            <div class="spacer"></div>
            <button class="btn tiny ghost" @click="dismissContinue">清空</button>
          </div>
          <div class="rail">
            <MovieCard
              v-for="m in cont"
              :key="'cw' + m.id"
              :movie="m"
              :selectable="false"
              @open="openDetail"
            />
          </div>
        </section>

        <section class="lib">
          <div class="section-title" v-if="showContinue">
            {{ $t('view.allMovies') }} <span class="count">{{ total }}</span>
          </div>

          <!-- 网格 -->
          <MovieGrid
            v-if="viewMode === 'grid'"
            :items="items"
            :loading="loading"
            @open="openDetail"
            @changed="() => {}"
          >
            <template #empty-action>
              <button v-if="hasActiveFilter()" class="btn" @click="resetFilters">{{ $t('view.clearFilters') }}</button>
              <button v-else class="btn primary" @click="runScan({})">扫描媒体库</button>
            </template>
          </MovieGrid>

          <!-- 列表 -->
          <div v-else class="movie-list">
            <div
              v-for="m in items"
              :key="m.id"
              class="lrow"
              :class="{ on: state.selected.has(m.id) }"
              @click="state.selMode ? toggleRow(m.id) : openDetail(m.id)"
              @contextmenu.prevent="openCtx(m, $event)"
            >
              <div class="lthumb">
                <img :src="coverThumbUrl(m.id, 120)" alt="" loading="lazy" @error="coverFallback" />
              </div>
              <div class="ltitle ellipsis">{{ m.title || m.code }}</div>
              <div class="lmeta ellipsis">{{ (m.actresses || []).join('、') || '—' }}</div>
              <div class="lrating">{{ m.rating ? '★' + m.rating : '—' }}</div>
              <div class="ldur">{{ m.runtime ? m.runtime + '′' : '—' }}</div>
            </div>
          </div>

          <Pager :page="state.page" :page-count="pageCount" :total="total" @go="goPage" />
        </section>
      </div>

      <BulkBar @done="onBulkDone" />
    </div>

    <ContextMenu
      :visible="ctx.visible"
      :x="ctx.x"
      :y="ctx.y"
      :items="ctxItems"
      @select="onCtxSelect"
      @close="closeCtx"
    />
  </section>
</template>

<style scoped>
/* 手机端浮层相关元素：桌面默认隐藏，仅窄屏 media 内显示 */
.filter-toggle { display: none; }
.filter-done { display: none; }
.filter-mask { display: none; }

.gallery-layout { display: flex; flex-direction: row; gap: 18px; align-items: stretch; flex: 1; min-height: 0; }
.filter-rail {
  width: 240px; flex: none;
  position: sticky; top: 12px;
  align-self: flex-start;
  max-height: calc(100vh - 80px); overflow-y: auto;
  padding: 14px; background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--r-lg);
}
.gallery-main { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }

.back-detail {
  border-color: var(--c-primary);
  color: var(--c-primary);
  font-weight: 600;
  white-space: nowrap;
}
.back-detail:hover { background: var(--c-primary); color: #fff; }

.seg { display: flex; gap: 2px; background: var(--c-surface-2); border-radius: 8px; padding: 3px; }
.seg button { border: 0; background: none; color: var(--c-text-3); width: 30px; height: 26px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.seg button.on { background: var(--c-primary); color: #fff; }

.sort-sel { width: auto; min-width: 116px; height: 28px; font-size: var(--fs-sm); }
.cw .rail { padding-bottom: var(--sp-3); }

/* 列表视图 */
.movie-list { display: flex; flex-direction: column; gap: 6px; }
.lrow { display: grid; grid-template-columns: 56px 1fr 1.2fr 56px 64px; align-items: center; gap: 12px; padding: 8px 12px; border-radius: 10px; cursor: pointer; background: var(--c-surface); border: 1px solid transparent; }
.lrow:hover { background: var(--c-surface-2); }
.lrow.on { border-color: var(--c-primary); }
.lthumb { width: 56px; height: 38px; border-radius: 6px; overflow: hidden; background: var(--c-surface-3); flex: none; }
.lthumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.card .thumb img.placeholder,
.lthumb img.placeholder { filter: grayscale(100%) opacity(.55) contrast(.92); }
.ltitle { font-weight: 600; }
.lmeta, .lrating, .ldur { color: var(--c-text-3); font-size: 13px; text-align: right; }

@media (max-width: 900px) {
  .gallery-layout { flex-direction: column; }
  /* 手机端筛选栏：浮层抽屉式，默认隐藏，展开时从顶部滑下、可滚动、不挤压影片内容 */
  .filter-rail {
    position: fixed;
    left: 0; right: 0; top: 0;
    width: 100%;
    max-height: 72vh;
    display: none;
    z-index: 70;
    border-radius: 0 0 var(--r-lg) var(--r-lg);
    background: var(--c-surface);
    box-shadow: 0 12px 30px rgba(0,0,0,.35);
    padding-bottom: 56px; /* 给底部「完成」按钮留位 */
  }
  .filter-rail.open { display: block; overflow-y: auto; }
  .filter-toggle { display: inline-flex; align-self: flex-start; }
  .filter-mask {
    display: block;
    position: fixed; inset: 0;
    background: rgba(0,0,0,.45);
    z-index: 65;
  }
  .filter-done {
    position: sticky; bottom: 0;
    display: block;
    width: 100%;
    margin-top: 10px;
    padding: 12px;
    font-weight: 600;
    background: var(--c-primary); color: #fff;
    border: 0; border-radius: var(--r-md);
    cursor: pointer;
  }
}
</style>
