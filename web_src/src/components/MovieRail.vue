<script setup>
/**
 * MovieRail —— 横向影片行（成熟视频网站式）
 *
 * - 隐藏滚动条：横向滚动容器不显示滚动条，改为左右箭头整屏翻页
 * - 翻页：箭头按「一屏」滚动 + scroll-snap 对齐卡片，触屏保留原生惯性滑动
 * - 按需加载：区块进入视口才请求；滑到接近末尾（或首屏没填满）自动追加下一页，
 *   只要后端还有数据就能一直加载，尾部有「加载中」占位卡
 * - v-model:items 把已加载列表同步给父组件（供「连播本区」等区块级操作使用）
 */
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import MovieCard from './MovieCard.vue'

const props = defineProps({
  /** 取数：(page, pageSize) => Promise<{ items, total?, has_more? }> */
  fetchPage: { type: Function, required: true },
  /** v-model:items —— 已加载的影片列表 */
  items: { type: Array, default: () => [] },
  pageSize: { type: Number, default: 12 },
  gap: { type: Number, default: 14 },
  /** 卡片宽度，0 = 按视口宽度自适应 */
  cardWidth: { type: Number, default: 0 },
  /** 推荐理由：(movie) => string（猜你喜欢场景） */
  reasonOf: { type: Function, default: null },
  /** 变更即清空重载（「换一批」） */
  reloadKey: { type: [String, Number], default: 0 },
})
const emit = defineEmits(['update:items', 'loaded', 'open'])

const list = ref([])
const loading = ref(true)      // 首次加载（骨架屏）
const loadingMore = ref(false) // 追加中
const hasMore = ref(true)
const failed = ref('')
const page = ref(0)
const atStart = ref(true)
const atEnd = ref(true)
const winW = ref(typeof window === 'undefined' ? 1440 : window.innerWidth)
const rootEl = ref(null)
const vpEl = ref(null)
const sentinel = ref(null)

let busy = false
let io = null
let raf = 0
let gen = 0 // 代次：reset（换一批）后旧请求的结果直接丢弃，避免脏数据混入

const cardW = computed(() => {
  if (props.cardWidth) return props.cardWidth
  const w = winW.value
  if (w <= 480) return 128
  if (w <= 900) return 146
  if (w <= 1400) return 166
  return 176
})
const stepPx = computed(() => cardW.value + props.gap)
const skeletonCount = computed(() => Math.max(4, Math.ceil((winW.value - 140) / stepPx.value)))

function sync() {
  emit('update:items', list.value)
}

/** 拉取下一页（首屏与追加共用，busy 防并发） */
async function loadMore() {
  if (busy || !hasMore.value) return
  const myGen = gen
  busy = true
  const first = page.value === 0
  if (first) loading.value = true
  else loadingMore.value = true
  failed.value = ''
  try {
    const next = page.value + 1
    const r = await props.fetchPage(next, props.pageSize)
    if (myGen !== gen) return
    const batch = (r && r.items) || []
    list.value = first ? batch : list.value.concat(batch)
    page.value = next
    const total = Number(r && r.total)
    if (r && r.has_more !== undefined) hasMore.value = !!r.has_more && batch.length > 0
    else if (Number.isFinite(total) && total > 0) hasMore.value = list.value.length < total
    else hasMore.value = batch.length >= props.pageSize
    sync()
    emit('loaded', list.value)
  } catch (e) {
    if (myGen !== gen) return
    failed.value = (e && e.message) || '加载失败'
    hasMore.value = false
  } finally {
    if (myGen === gen) {
      busy = false
      loading.value = false
      loadingMore.value = false
      // 首屏太少（右侧会空一大片）时继续补，直到填满一屏或没有更多
      if (hasMore.value && list.value.length && list.value.length < skeletonCount.value) loadMore()
      updateEdges()
      nextTick(updateEdges)
    }
  }
}

/** 重置并重新加载（换一批 / 刷新） */
function reset() {
  gen++
  busy = false
  list.value = []
  page.value = 0
  hasMore.value = true
  failed.value = ''
  loading.value = true
  loadingMore.value = false
  atStart.value = true
  atEnd.value = true
  if (vpEl.value) vpEl.value.scrollLeft = 0
  sync()
  loadMore()
}

function updateEdges() {
  const el = vpEl.value
  if (!el) return
  atStart.value = el.scrollLeft <= 4
  atEnd.value = el.scrollLeft + el.clientWidth >= el.scrollWidth - 8
}

function onScroll() {
  if (raf) return
  raf = requestAnimationFrame(() => {
    raf = 0
    updateEdges()
  })
}

/** 按「一屏」翻页 */
function scrollPage(dir) {
  const el = vpEl.value
  if (!el) return
  const w = el.clientWidth
  el.scrollBy({ left: dir * Math.max(240, w - 2), behavior: 'smooth' })
}

function onResize() {
  winW.value = window.innerWidth
  nextTick(updateEdges)
}

function openMovie(id) {
  emit('open', id)
}

/**
 * 观察目标：
 * - 列表已渲染 → 观察轨道末尾哨兵，滑到右侧（或首屏没填满）即追加下一页
 * - 骨架态（哨兵还没渲染）→ 观察整行，等它进入视口才拉第一页（纵向懒加载）
 */
function observeSentinel() {
  if (!io) return
  io.disconnect()
  if (sentinel.value) io.observe(sentinel.value)
  else if (rootEl.value) io.observe(rootEl.value)
}

onMounted(() => {
  window.addEventListener('resize', onResize)
  io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting) { loadMore(); break }
      }
    },
    { root: null, rootMargin: '400px' },
  )
  observeSentinel()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (io) io.disconnect()
  if (raf) cancelAnimationFrame(raf)
})

// 骨架屏与真实列表是两套 DOM，加载完成后需要重新观察哨兵
watch(loading, (v) => { if (!v) nextTick(observeSentinel) })
// 追加/换一批后哨兵位置变化，重新观察
watch(() => list.value.length, () => nextTick(observeSentinel))
watch(() => props.reloadKey, () => reset())
</script>

<template>
  <div ref="rootEl" class="rail-wrap">
    <!-- 首屏骨架 -->
    <div v-if="loading" class="rail-track rail-track--sk" aria-hidden="true">
      <div
        v-for="n in skeletonCount"
        :key="n"
        class="rail-sk"
        :style="{ width: cardW + 'px' }"
      ></div>
    </div>

    <!-- 空 / 加载失败 -->
    <div v-else-if="!list.length" class="rail-empty">
      <slot name="empty">
        <span class="rail-empty-txt">{{ failed || '暂无内容' }}</span>
      </slot>
    </div>

    <template v-else>
      <button
        v-show="!atStart"
        class="rail-nav prev"
        type="button"
        aria-label="prev"
        @click="scrollPage(-1)"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4">
          <path d="M15 5 L8 12 L15 19" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </button>

      <div ref="vpEl" class="rail-vp" @scroll="onScroll">
        <div class="rail-track" :style="{ gap: gap + 'px' }">
          <div
            v-for="m in list"
            :key="m.id"
            class="rail-item"
            :style="{ width: cardW + 'px' }"
          >
            <MovieCard
              :movie="m"
              :selectable="false"
              :reason="reasonOf ? reasonOf(m) : ''"
              @open="openMovie"
            />
          </div>

          <!-- 还能继续加载：尾部占位卡，滑到这里时自动追加 -->
          <div
            v-if="hasMore && !failed"
            class="rail-item rail-tail"
            :style="{ width: cardW + 'px' }"
          >
            <div class="tail-box" :class="{ busy: loadingMore }">
              <span v-if="loadingMore" class="spinner"></span>
              <span v-else class="tail-ico">›</span>
            </div>
          </div>

          <div ref="sentinel" class="rail-sentinel"></div>
        </div>
      </div>

      <button
        v-show="!atEnd || hasMore"
        class="rail-nav next"
        type="button"
        aria-label="next"
        @click="scrollPage(1)"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4">
          <path d="M9 5 L16 12 L9 19" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </button>
    </template>
  </div>
</template>

<style scoped>
.rail-wrap {
  position: relative;
}

/* 横向滚动容器：隐藏滚动条，上下留 14px 余量给卡片 hover 位移 */
.rail-vp {
  overflow-x: auto;
  overflow-y: hidden;
  scrollbar-width: none;
  -ms-overflow-style: none;
  scroll-snap-type: x mandatory;
  scroll-behavior: smooth;
  padding: 14px 0;
  margin: -10px 0;
  overscroll-behavior-x: contain;
}
.rail-vp::-webkit-scrollbar {
  width: 0;
  height: 0;
  display: none;
}

.rail-track {
  display: flex;
  align-items: flex-start;
}
.rail-track--sk {
  padding: 14px 0;
  margin: -10px 0;
}
.rail-item {
  flex: 0 0 auto;
  scroll-snap-align: start;
  position: relative;
}

/* 卡片 hover：只做轻微上浮与阴影，位移量在容器 padding 余量内，不会被裁切 */
.rail-item :deep(.card:hover) {
  transform: translateY(-4px);
  box-shadow: var(--sh-3, 0 12px 28px rgba(0, 0, 0, .35));
  border-color: var(--c-line-strong, rgba(255, 255, 255, .16));
  z-index: 3;
}

/* 尾部「继续加载」占位卡 */
.rail-tail {
  display: flex;
  align-items: center;
  justify-content: center;
  align-self: stretch;
  min-height: 220px;
}
.tail-box {
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  color: var(--c-text-3);
  background: var(--c-surface-2);
  border: 1px solid var(--c-border, rgba(255, 255, 255, .1));
}
.tail-ico {
  font-size: 22px;
  line-height: 1;
  opacity: .7;
}
.spinner {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 2px solid var(--c-border, rgba(255, 255, 255, .18));
  border-top-color: var(--c-primary, #4f8cff);
  animation: rail-spin .7s linear infinite;
}
@keyframes rail-spin {
  to { transform: rotate(360deg); }
}

.rail-sentinel {
  flex: 0 0 1px;
  width: 1px;
  height: 1px;
}

.rail-empty {
  padding: 18px 2px;
}
.rail-empty-txt {
  font-size: 13px;
  color: var(--c-text-3);
}

/* 骨架 */
.rail-sk {
  flex: 0 0 auto;
  aspect-ratio: 2 / 3;
  border-radius: var(--r-md, 12px);
  background: linear-gradient(100deg, var(--c-surface-2) 30%, var(--c-surface-3, #1c2230) 50%, var(--c-surface-2) 70%);
  background-size: 200% 100%;
  animation: rail-shimmer 1.2s infinite;
}
@keyframes rail-shimmer {
  to { background-position: -200% 0; }
}

/* 翻页箭头（Netflix 式：吸附两侧、覆盖整行高度，hover 才出现） */
.rail-nav {
  position: absolute;
  top: 4px;
  bottom: 4px;
  width: 46px;
  z-index: 6;
  display: grid;
  place-items: center;
  border: 0;
  cursor: pointer;
  color: #fff;
  background: linear-gradient(90deg, rgba(6, 8, 12, .88) 0%, rgba(6, 8, 12, .45) 62%, rgba(6, 8, 12, 0) 100%);
  opacity: 0;
  transition: opacity var(--t-base, .18s);
}
.rail-nav.next {
  right: -2px;
  background: linear-gradient(270deg, rgba(6, 8, 12, .88) 0%, rgba(6, 8, 12, .45) 62%, rgba(6, 8, 12, 0) 100%);
}
.rail-nav.prev { left: -2px; }
.rail-nav svg { width: 24px; height: 24px; }
.rail-wrap:hover .rail-nav { opacity: 1; }
.rail-nav:hover { opacity: 1; filter: brightness(1.25); }
.rail-nav:focus-visible { opacity: 1; outline: 2px solid var(--c-primary, #4f8cff); }

/* 触屏：无 hover，箭头常显但更轻，避免遮挡封面 */
@media (hover: none) {
  .rail-nav {
    opacity: .92;
    width: 34px;
    background: rgba(6, 8, 12, .55);
    border-radius: 8px;
    top: 12%;
    bottom: 12%;
  }
  .rail-nav.next { background: rgba(6, 8, 12, .55); }
}

/* 触屏窄屏：全局把卡片改成横排（左封面右信息），rail 内保持竖版海报 */
@media (max-width: 480px) {
  .rail-item :deep(.card) { flex-direction: column; }
  .rail-item :deep(.card .thumb) {
    width: 100%;
    height: auto;
    aspect-ratio: 2 / 3;
    border-radius: var(--r-md, 12px) var(--r-md, 12px) 0 0;
  }
  .rail-item :deep(.card .meta) {
    padding: var(--sp-2, 6px) var(--sp-2, 6px) var(--sp-3, 8px);
  }
  .rail-item :deep(.card .title) { -webkit-line-clamp: 2; min-height: 2.8em; }
  .rail-item :deep(.card .meta-act),
  .rail-item :deep(.card .meta-tags) { max-height: 18px; }
}
</style>
