<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { state, SORTS, FLAGS, resetFilters, hasActiveFilter, openPlayQueue } from '../state.js'
import { useLibrary } from '../composables/useLibrary.js'
import { getContinueWatching, clearContinueWatching, deleteMovie, scrapeOne, addToCollection, coverThumbUrl, listMovies } from '../api.js'
import { toast, confirmDialog, coverFallback } from '../utils.js'
import { t } from '../i18n'
import { useTasks } from '../composables/useTasks.js'

import MovieGrid from '../components/MovieGrid.vue'
import MovieCard from '../components/MovieCard.vue'
import BulkBar from '../components/BulkBar.vue'
import MovieFilter from '../components/MovieFilter.vue'
import ContextMenu from '../components/ContextMenu.vue'

const { items, total, loading, loadingMore, hasMore, load, loadMore, patchItem } = useLibrary()
const { runScan } = useTasks()

const cont = ref([])
const contLoading = ref(false)

/* 视图模式：grid | list | waterfall */
const viewMode = ref('grid')

/* 播放模式（迅雷式）：把当前筛选结果连播 */
const playingAll = ref(false)

/* 构造与 useLibrary 一致的当前筛选参数 */
function filterParams(page, pageSize) {
  return {
    q: state.q || undefined,
    actress: state.actress.length ? state.actress.join(',') : undefined,
    genre: state.genre.length ? state.genre.join(',') : undefined,
    tag: state.tag.length ? state.tag.join(',') : undefined,
    studio: state.studio || undefined,
    series: state.series || undefined,
    prefix: state.prefix || undefined,
    year: state.year || undefined,
    flags: state.flags.length ? state.flags.join(',') : undefined,
    sort: state.sort,
    page, page_size: pageSize,
    op: state.multiOp,
    min_rating: state.minRating || undefined,
  }
}

/* 拉取当前筛选下的全部影片（循环分页）并打开全局播放器连播 */
async function playFiltered() {
  if (playingAll.value) return
  playingAll.value = true
  const all = []
  const size = 200
  try {
    let p = 1
    let totalHits = 0
    while (true) {
      const r = await listMovies(filterParams(p, size))
      const batch = (r && r.items) || []
      totalHits = Number((r && r.total) || 0)
      all.push(...batch)
      if (!batch.length || all.length >= totalHits) break
      if (batch.length < size) break
      p += 1
    }
    if (!all.length) { toast(t('playlist.emptyNoMovies'), 'err'); return }
    openPlayQueue(all, t('view.gallery'))
  } catch (e) {
    toast(e.message || '播放失败', 'err')
  } finally {
    playingAll.value = false
  }
}

/* 手机端筛选栏折叠状态：默认收起，点击「筛选条件」以浮层弹出，不遮盖影片内容 */
const showFilter = ref(false)

/* 移动端沉浸式：向下浏览隐藏顶栏/工具栏；向上滑（哪怕一点）即显示，滚到顶部强制显示 */
const navHidden = ref(false)
const showTop = ref(false) // 无限滚动后回到顶部
let vbEl = null
let mqMobile = null
let lastScrollTop = 0
function onViewScroll() {
  if (!vbEl) return
  const st = vbEl.scrollTop
  showTop.value = st > 900
  if (!mqMobile || !mqMobile.matches) { navHidden.value = false; return }
  if (st <= 80) {
    // 接近顶部：始终显示
    navHidden.value = false
  } else if (st > lastScrollTop) {
    // 向下滑：隐藏顶栏/工具栏
    navHidden.value = true
  } else {
    // 向上滑：立即显示
    navHidden.value = false
  }
  lastScrollTop = st
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
  if (ioMore) { ioMore.disconnect(); ioMore = null }
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

/** 无限滚动：底部哨兵进入视口即追加下一页 */
const sentinel = ref(null)
let ioMore = null

function goTop() {
  if (vbEl) vbEl.scrollTo({ top: 0, behavior: 'smooth' })
}

/* 换筛选/排序时回到顶部：否则停在底部会立刻连续触发好几页加载 */
watch(
  () => [state.q, state.actress.slice(), state.genre.slice(), state.tag.slice(), state.studio,
         state.series, state.prefix, state.year, state.flags.slice(), state.multiOp,
         state.minRating, state.sort],
  () => { if (vbEl) vbEl.scrollTo({ top: 0 }) },
  { deep: true },
)

function onBulkDone() {
  state.selected = new Set()
  state.selMode = false
  load()
}

onMounted(() => {
  load()
  loadContinue()
  ioMore = new IntersectionObserver(
    (entries) => { if (entries.some((e) => e.isIntersecting)) loadMore() },
    { root: null, rootMargin: '700px 0px' },
  )
  if (sentinel.value) ioMore.observe(sentinel.value)
})

// 追加一批后重新观察：若哨兵仍在视口内（比如一页没填满屏幕）继续加载，避免卡住
watch(
  () => items.value.length,
  () => {
    if (!ioMore || !sentinel.value) return
    nextTick(() => {
      if (!ioMore || !sentinel.value) return
      ioMore.unobserve(sentinel.value)
      ioMore.observe(sentinel.value)
    })
  },
)
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
          <button :class="{ on: viewMode === 'waterfall' }" @click="viewMode = 'waterfall'" :data-tip="$t('view.waterfall')">▤</button>
          <button :class="{ on: viewMode === 'list' }" @click="viewMode = 'list'" data-tip="列表">☰</button>
          <button class="play-mode-btn" :class="{ on: state.playQueue.open }" @click="playFiltered" :title="$t('gallery.playFiltered')">▶</button>
        </div>
        <button class="btn tiny" :disabled="playingAll" @click="playFiltered" :title="$t('gallery.playAllTip')">
          {{ playingAll ? '…' : '▶ ' + $t('gallery.playAll') }}
        </button>

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
        <button v-if="state.selMode" class="btn tiny" @click="selectPage">选中已加载</button>

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

          <!-- 网格 / 瀑布流 -->
          <MovieGrid
            v-if="viewMode !== 'list'"
            :items="items"
            :loading="loading"
            :mode="viewMode === 'waterfall' ? 'waterfall' : 'grid'"
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

          <!-- 无限滚动：滚到底自动追加下一页 -->
          <div ref="sentinel" class="lib-sentinel" aria-hidden="true"></div>

          <div v-if="!loading && items.length" class="lib-foot">
            <template v-if="loadingMore">
              <span class="spinner"></span>
              <span class="ft-txt">{{ $t('gallery.loading') }}</span>
            </template>
            <button v-else-if="hasMore" class="btn tiny" @click="loadMore">
              {{ $t('gallery.loadMore') }}
            </button>
            <span v-else class="ft-txt dim">{{ $t('gallery.loadedAll', { n: total }) }}</span>
          </div>
        </section>
      </div>

      <BulkBar @done="onBulkDone" />

      <!-- 无限滚动后一键回顶 -->
      <button v-if="showTop" class="to-top" @click="goTop" :data-tip="$t('gallery.toTop')" :title="$t('gallery.toTop')">↑</button>
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
.gallery-main { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden; position: relative; }

/* 无限滚动：底部哨兵与状态行 */
.lib-sentinel { height: 1px; }
.lib-foot {
  display: flex; align-items: center; justify-content: center; gap: 8px;
  padding: 18px 0 8px;
  color: var(--c-text-3);
}
.ft-txt { font-size: 12px; }
.ft-txt.dim { opacity: .75; }

/* 回到顶部 */
.to-top {
  position: absolute;
  right: 18px; bottom: 18px;
  width: 38px; height: 38px;
  display: grid; place-items: center;
  border-radius: 50%;
  border: 1px solid var(--c-line-strong);
  background: var(--c-surface-3);
  color: var(--c-text-2);
  font-size: 17px;
  cursor: pointer;
  box-shadow: var(--sh-3, 0 8px 20px rgba(0, 0, 0, .35));
  z-index: 20;
  animation: rise-in var(--t-base, .18s);
}
.to-top:hover { color: #fff; background: var(--c-primary); border-color: var(--c-primary); }

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
.play-mode-btn { color: var(--c-ok, #3fb950) !important; }

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
