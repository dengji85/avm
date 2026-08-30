<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import VideoPlayer from './VideoPlayer.vue'
import { coverThumbUrl as thumbUrl, setCollectionPlayhead } from '../api.js'
import { state, closePlayQueue } from '../state.js'
import { t } from '../i18n/index.js'

/* 全局播放队列：数据源为 state.playQueue（跨视图保留、单一数据源） */

// 顺序队列（原始顺序，来自全局）
const baseQueue = computed(() => state.playQueue.queue || [])

// 当前顺序/循环模式（与全局同步）
const order = computed({
  get: () => state.playQueue.order || 'sequential',
  set: (v) => { state.playQueue.order = v },
})

const played = ref(new Set())   // 已播放过的 id（内部瞬态，不跨视图）
const showList = ref(true)
const searchQ = ref('')          // 队列内过滤关键词

// 队列内实时过滤（本地，零网络请求）
const isFiltering = computed(() => searchQ.value.trim().length > 0)
const filteredQueue = computed(() => {
  const kw = searchQ.value.trim().toLowerCase()
  if (!kw) return orderedQueue.value.map((m, i) => ({ m, oi: i }))
  return orderedQueue.value
    .map((m, i) => ({ m, oi: i }))
    .filter(({ m }) =>
      (m.code || '').toLowerCase().includes(kw) ||
      (m.title || '').toLowerCase().includes(kw),
    )
})

// 实际渲染队列：shuffle 时打散，否则原序
const orderedQueue = ref([])
// 全局 idx 指向原始 baseQueue 中的项；displayedIdx 指向 orderedQueue 中的当前显示位置
const displayedIdx = ref(0)

function buildOrder() {
  const base = baseQueue.value
  if (!base.length) { orderedQueue.value = []; displayedIdx.value = 0; return }
  if (order.value === 'shuffle') {
    const arr = base.slice()
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1))
      ;[arr[i], arr[j]] = [arr[j], arr[i]]
    }
    orderedQueue.value = arr
  } else {
    orderedQueue.value = base.slice()
  }
  // 让全局 idx 指向的影片作为起点
  const targetId = base[Math.min(state.playQueue.idx, base.length - 1)]?.id
  const at = targetId ? orderedQueue.value.findIndex((m) => m.id === targetId) : -1
  displayedIdx.value = at >= 0 ? at : 0
}
buildOrder()

const current = computed(() => orderedQueue.value[displayedIdx.value] || null)
const currentId = computed(() => (current.value ? current.value.id : 0))

const progress = ref({ position: 0, duration: 0 })
const playedCount = computed(() => played.value.size)
const total = computed(() => orderedQueue.value.length)
const hasPrev = computed(() => displayedIdx.value > 0)
const hasNext = computed(() => displayedIdx.value < orderedQueue.value.length - 1)

function onProgress(p) { progress.value = p || progress.value }

/** 片单续播记忆：记录在片单里播到了哪部（仅片单来源） */
function savePlayhead() {
  if (state.playQueue.cid && currentId.value) {
    setCollectionPlayhead(state.playQueue.cid, currentId.value).catch(() => {})
  }
}

/** 同步当前影片到全局 idx（在 baseQueue 中的位置），供跨视图续播 */
function syncGlobalIdx() {
  if (!currentId.value) return
  const at = baseQueue.value.findIndex((m) => m.id === currentId.value)
  if (at >= 0) state.playQueue.idx = at
  savePlayhead()
}

// 自动下一部（VideoPlayer 在 ended 时已落库）
function next(auto = false) {
  played.value.add(currentId.value)
  syncGlobalIdx()
  if (displayedIdx.value < orderedQueue.value.length - 1) {
    displayedIdx.value += 1
  } else if (order.value === 'shuffle') {
    // 乱序播完：重新打散再来一轮
    buildOrder()
  } else if (state.playQueue.loop) {
    // 顺序 + 循环：回到第一部
    displayedIdx.value = 0
    toastLoopRestart()
  } else {
    toastDone()
    return
  }
  if (auto) { /* 自动连播：VideoPlayer 会因 movieId 变化重建并 autoplay */ }
}

function toastDone() {
  window.dispatchEvent(new CustomEvent('avm-toast', { detail: { msg: t('playlist.finished'), kind: 'ok' } }))
}
function toastLoopRestart() {
  window.dispatchEvent(new CustomEvent('avm-toast', { detail: { msg: t('playlist.loopRestart'), kind: 'ok' } }))
}

function jumpTo(i) {
  played.value.add(currentId.value)
  syncGlobalIdx()
  displayedIdx.value = i
  syncGlobalIdx()
}

// 上一部 / 下一部（迅雷式快速切换）
function prev() { if (displayedIdx.value > 0) jumpTo(displayedIdx.value - 1) }
function nextManual() { if (displayedIdx.value < orderedQueue.value.length - 1) jumpTo(displayedIdx.value + 1) }

function toggleOrder() {
  order.value = order.value === 'sequential' ? 'shuffle' : 'sequential'
  buildOrder()          // 保持当前影片继续
  const at = orderedQueue.value.findIndex((m) => m.id === currentId.value)
  if (at > 0) { const [c] = orderedQueue.value.splice(at, 1); orderedQueue.value.unshift(c); displayedIdx.value = 0 }
  else { displayedIdx.value = Math.max(0, at) }
}

function toggleLoop() {
  state.playQueue.loop = !state.playQueue.loop
}

function openExternalAll() {
  if (!current.value) return
  import('../api.js').then(({ playMovie }) => {
    playMovie(currentId.value).catch(() => {})
  })
}

// ===== 队列内实时过滤（在右侧列表快速找到想播的影片） =====
// 点击过滤结果：跳到其在 orderedQueue 中的真实下标
function jumpFiltered(oi) {
  jumpTo(oi)
}

function close() {
  syncGlobalIdx()
  closePlayQueue()
}

// 全局队列变化时重建顺序（仅当外部替换了队列）
watch(() => state.playQueue.queue, () => {
  buildOrder()
}, { deep: false })

// 当播放器打开时，若全局队列非空，确保顺序已构建
watch(() => state.playQueue.open, (open) => {
  if (open) { played.value = new Set(); buildOrder() }
})
</script>

<template>
  <div class="pl-mask" @click.self="close()">
    <div class="pl">
      <div class="pl-main">
        <div class="pl-toolbar">
          <div class="pl-title">
            <span class="pl-name">{{ $t('playlist.title') }}</span>
            <span class="pl-count tabular">{{ playedCount }} / {{ total }}</span>
          </div>
          <div class="pl-nav">
            <button class="btn tiny" :disabled="!hasPrev" @click="prev" :data-tip="$t('playlist.prev')">‹</button>
            <button class="btn tiny" :disabled="!hasNext" @click="nextManual" :data-tip="$t('playlist.next')">›</button>
          </div>
          <div class="spacer"></div>
          <button class="btn tiny ghost" :class="{ on: order === 'shuffle' }" @click="toggleOrder">
            {{ order === 'shuffle' ? $t('playlist.sequential') : $t('playlist.shuffle') }}
          </button>
          <button class="btn tiny ghost" :class="{ on: state.playQueue.loop }" @click="toggleLoop" :data-tip="$t('playlist.loop')">
            {{ $t('playlist.loopBtn') }}
          </button>
          <button class="btn tiny ghost" @click="showList = !showList">
            {{ showList ? $t('playlist.hideList') : $t('playlist.showList') }}
          </button>
          <button class="btn tiny danger" @click="close()">✕</button>
        </div>

        <div v-if="current" class="pl-stage">
          <VideoPlayer :key="currentId" :movie-id="currentId" :start-at="current.progress_seconds || 0" :autoplay="state.playQueue.autoplay" @progress="onProgress" @ended="next(true)" />
        </div>
        <div v-else class="pl-empty">
          <div class="icon">▶</div>
          <div class="title">{{ $t('playlist.empty') }}</div>
        </div>
      </div>

      <aside v-if="showList" class="pl-side">
        <div class="pl-side-head">
          <span class="pl-side-title">{{ state.playQueue.source || $t('playlist.queue') }}</span>
          <span class="pl-side-count tabular">{{ isFiltering ? filteredQueue.length + '/' + total : total }}</span>
        </div>
        <div class="pl-side-filter">
          <span class="pl-sf-ico">⌕</span>
          <input
            v-model="searchQ"
            class="pl-filter-input"
            type="text"
            :placeholder="$t('playlist.filterPlaceholder')"
          />
          <button v-if="isFiltering" class="pl-filter-clear" @click="searchQ = ''">✕</button>
        </div>
        <div class="pl-items">
          <button
            v-for="it in filteredQueue"
            :key="it.m.id"
            class="pl-item"
            :class="{ on: it.oi === displayedIdx, dim: !it.m.playable }"
            @click="jumpFiltered(it.oi)"
          >
            <span class="pl-thumb-wrap">
              <img v-if="it.m.id" :src="thumbUrl(it.m.id, 200)" class="pl-thumb" alt="" />
              <div v-else class="pl-thumb ph">▶</div>
              <span v-if="it.oi === displayedIdx" class="pl-now">▸</span>
            </span>
            <div class="pl-meta">
              <div class="pl-code ellipsis">{{ it.m.code || it.m.title }}</div>
              <div class="pl-title ellipsis">{{ it.m.title }}</div>
              <div class="pl-sub ellipsis">
                <span v-if="played.has(it.m.id)" class="dot done">✓</span>
                <span v-else-if="!it.m.playable" class="dot miss">∅</span>
                <span v-if="it.m.duration_seconds > 0" class="tabular muted">{{ Math.round(it.m.duration_seconds / 60) }}′</span>
              </div>
            </div>
          </button>
          <div v-if="isFiltering && !filteredQueue.length" class="pl-filter-empty">{{ $t('playlist.filterNone') }}</div>
        </div>
        <div class="pl-side-foot">
          <button class="btn tiny ghost" @click="openExternalAll">{{ $t('playlist.external') }}</button>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.pl-mask {
  position: fixed; inset: 0; z-index: 200;
  background: #000;
  display: flex;
}
.pl {
  display: flex; flex: 1;
  width: 100%; height: 100%;
  background: var(--c-surface);
}
.pl-main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.pl-toolbar {
  display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap;
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--c-line);
}
.pl-title { display: flex; align-items: baseline; gap: var(--sp-2); }
.pl-name { font-weight: 600; font-size: var(--fs-lg); }
.pl-count { font-size: var(--fs-sm); color: var(--c-text-3); }

.pl-stage { flex: 1; min-height: 0; display: flex; background: #000; }
.pl-stage :deep(.player) { flex: 1; }
/* 播放列表页里让播放器占满舞台剩余高度，而不是 VideoPlayer 默认的固定 70vh */
.pl-stage :deep(.vjs-wrap) { flex: 1; height: 100% !important; }
.pl-stage :deep(.video-js) { height: 100% !important; }
.pl-empty { flex: 1; display: grid; place-items: center; color: var(--c-text-3); }

.pl-side {
  width: 320px; display: flex; flex-direction: column;
  border-left: 1px solid var(--c-line); background: var(--c-surface-2);
}
.pl-side-head {
  display: flex; align-items: center; gap: 8px;
  padding: var(--sp-2) var(--sp-3); font-weight: 600;
  border-bottom: 1px solid var(--c-line);
}
.pl-side-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pl-side-count { font-size: var(--fs-xs); color: var(--c-text-3); font-weight: 400; }
.pl-side-filter {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 12px; border-bottom: 1px solid var(--c-line);
  background: var(--c-surface-2);
}
.pl-sf-ico { color: var(--c-text-3); font-size: 13px; }
.pl-filter-input {
  flex: 1; min-width: 0; padding: 4px 8px;
  border: 1px solid var(--c-line); border-radius: var(--r-sm);
  background: var(--c-surface); color: var(--c-text);
  font-size: var(--fs-sm);
}
.pl-filter-input:focus { outline: none; border-color: var(--c-accent, #4f8cff); }
.pl-filter-clear {
  width: 20px; height: 20px; display: grid; place-items: center;
  border: 0; background: none; color: var(--c-text-3); cursor: pointer;
  border-radius: 50%; font-size: 11px;
}
.pl-filter-clear:hover { background: var(--c-surface-3); color: var(--c-text); }
.pl-filter-empty { padding: var(--sp-4); text-align: center; color: var(--c-text-3); font-size: var(--fs-sm); }
.pl-items { flex: 1; overflow-y: auto; padding: var(--sp-2); display: flex; flex-direction: column; gap: 4px; }
.pl-item {
  display: flex; align-items: center; gap: var(--sp-3);
  padding: var(--sp-2); border-radius: var(--r-sm);
  background: transparent; border: 1px solid transparent; cursor: pointer; text-align: left;
}
.pl-item:hover { background: var(--c-surface-3, var(--c-surface)); }
.pl-item.on { border-color: var(--c-accent, #4f8cff); background: color-mix(in srgb, var(--c-accent, #4f8cff) 14%, transparent); }
.pl-item.dim { opacity: .5; }
.pl-thumb-wrap { position: relative; width: 88px; flex: none; }
.pl-thumb { width: 88px; height: 60px; object-fit: cover; border-radius: 5px; background: var(--c-surface-3, #222); display: block; }
.pl-thumb.ph { display: grid; place-items: center; font-size: 16px; color: var(--c-text-3); }
.pl-now {
  position: absolute; left: 6px; top: 6px; z-index: 2;
  width: 22px; height: 22px; border-radius: 50%;
  display: grid; place-items: center;
  background: var(--c-accent, #4f8cff); color: #fff; font-size: 11px;
  box-shadow: 0 2px 6px rgba(0,0,0,.4);
}
.pl-meta { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.pl-code { font-size: var(--fs-sm); font-weight: 700; color: var(--c-text); }
.pl-title { font-size: var(--fs-xs); color: var(--c-text-2); }
.pl-sub { font-size: var(--fs-xs); display: flex; align-items: center; gap: var(--sp-1); }
.dot { font-size: 11px; }
.dot.done { color: var(--c-ok, #3fb950); }
.dot.miss { color: var(--c-text-3); }
.pl-side-foot { padding: var(--sp-2) var(--sp-3); border-top: 1px solid var(--c-line); }
.pl-nav { display: flex; gap: 4px; margin-left: var(--sp-2); }
.pl-nav .btn { min-width: 30px; padding: 4px 8px; }

@media (max-width: 820px) {
  .pl { flex-direction: column; }
  .pl-side { width: auto; border-left: none; border-top: 1px solid var(--c-line); max-height: 38vh; }
}
</style>
