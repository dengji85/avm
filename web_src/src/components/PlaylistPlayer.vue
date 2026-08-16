<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import VideoPlayer from './VideoPlayer.vue'
import { coverThumbUrl as thumbUrl, setCollectionPlayhead } from '../api.js'
import { t } from '../i18n/index.js'

const props = defineProps({
  // 所属片单 id（用于续播记忆）；非片单连播可为 0
  cid: { type: Number, default: 0 },
  // 队列：[{ id, code, title, cover, playable, progress_seconds, duration_seconds }]
  queue: { type: Array, required: true },
  // 起始索引（仅用一次，之后遵守顺序/乱序）
  startIndex: { type: Number, default: 0 },
  autoplay: { type: Boolean, default: true },
})
const emit = defineEmits(['close'])

function savePlayhead() {
  if (props.cid && currentId.value) {
    setCollectionPlayhead(props.cid, currentId.value).catch(() => {})
  }
}

const order = ref('sequential') // sequential | shuffle
const played = ref(new Set())   // 已播放过的 id
const idx = ref(0)              // 当前播放在 orderedQueue 中的下标
const showList = ref(true)
let reshuffleGuard = false

// 顺序队列：基于 props.queue 当前内容
const baseQueue = computed(() => props.queue)

// 实际渲染队列：shuffle 时打散，否则原序
const orderedQueue = ref([])
function buildOrder() {
  if (order.value === 'shuffle') {
    const arr = baseQueue.value.slice()
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1))
      ;[arr[i], arr[j]] = [arr[j], arr[i]]
    }
    orderedQueue.value = arr
  } else {
    orderedQueue.value = baseQueue.value.slice()
  }
  // 把 startIndex 指向的影片排到队首（避免点了某部却从第一开始）
  const target = baseQueue.value[Math.min(props.startIndex, baseQueue.value.length - 1)]
  if (target) {
    const at = orderedQueue.value.findIndex((m) => m.id === target.id)
    if (at > 0) {
      const [first] = orderedQueue.value.splice(at, 1)
      orderedQueue.value.unshift(first)
    }
  }
  idx.value = 0
}
buildOrder()

const current = computed(() => orderedQueue.value[idx.value] || null)
const currentId = computed(() => (current.value ? current.value.id : 0))

const progress = ref({ position: 0, duration: 0 })
const playedCount = computed(() => played.value.size)
const total = computed(() => orderedQueue.value.length)

function onProgress(p) { progress.value = p || progress.value }

// 自动下一部（VideoPlayer 在 ended 时已落库）
function next(auto = false) {
  played.value.add(currentId.value)
  savePlayhead()
  if (idx.value < orderedQueue.value.length - 1) {
    idx.value += 1
  } else {
    // 队列播完：顺序模式停在末尾；shuffle 模式已播完全部则停
    toastDone()
    return
  }
  if (auto) { /* 自动连播：VideoPlayer 会因 movieId 变化重建并 autoplay */ }
}

function toastDone() {
  // 轻提示：已到片单末尾
  // 通过 window 事件交由外部 toast（PlaylistPlayer 不自带 toast，避免重复依赖）
  window.dispatchEvent(new CustomEvent('avm-toast', { detail: { msg: t('playlist.finished'), kind: 'ok' } }))
}

function jumpTo(i) {
  played.value.add(currentId.value)
  savePlayhead()
  idx.value = i
}

function toggleOrder() {
  order.value = order.value === 'sequential' ? 'shuffle' : 'sequential'
  // 重建顺序：保持当前影片继续（已在 played 集合中）
  reshuffleGuard = true
  buildOrder()
  // 让当前影片重新成为队首起点
  if (currentId.value) {
    const at = orderedQueue.value.findIndex((m) => m.id === currentId.value)
    if (at > 0) {
      const [c] = orderedQueue.value.splice(at, 1)
      orderedQueue.value.unshift(c)
      idx.value = 0
    }
  }
}

function openExternalAll() {
  // 顺序调用系统播放器（逐部），受本机播放器支持度限制
  if (!current.value) return
  import('../api.js').then(({ playMovie }) => {
    playMovie(currentId.value).catch(() => {})
  })
}

function close() {
  savePlayhead()
  emit('close')
}

// queue 内容变化时若还在首部、未播放，重建顺序
watch(() => props.queue, () => {
  if (!played.value.size) buildOrder()
}, { deep: false })

watch(() => order.value, () => { /* buildOrder 已在 toggleOrder 内调用 */ })
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
          <div class="spacer"></div>
          <button class="btn tiny ghost" :class="{ on: order === 'shuffle' }" @click="toggleOrder">
            {{ order === 'shuffle' ? $t('playlist.sequential') : $t('playlist.shuffle') }}
          </button>
          <button class="btn tiny ghost" @click="showList = !showList">
            {{ showList ? $t('playlist.hideList') : $t('playlist.showList') }}
          </button>
          <button class="btn tiny danger" @click="close()">✕</button>
        </div>

        <div v-if="current" class="pl-stage">
          <VideoPlayer :key="currentId" :movie-id="currentId" :start-at="current.progress_seconds || 0" @progress="onProgress" @ended="next(true)" />
        </div>
        <div v-else class="pl-empty">
          <div class="icon">▶</div>
          <div class="title">{{ $t('playlist.empty') }}</div>
        </div>
      </div>

      <aside v-if="showList" class="pl-side">
        <div class="pl-side-head">{{ $t('playlist.queue') }}</div>
        <div class="pl-items">
          <button
            v-for="(m, i) in orderedQueue"
            :key="m.id"
            class="pl-item"
            :class="{ on: i === idx, dim: !m.playable }"
            @click="jumpTo(i)"
          >
            <img v-if="m.cover" :src="thumbUrl(m.cover, 80)" class="pl-thumb" alt="" />
            <div v-else class="pl-thumb ph">▶</div>
            <div class="pl-meta">
              <div class="pl-code ellipsis">{{ m.code || m.title }}</div>
              <div class="pl-sub ellipsis">
                <span v-if="played.has(m.id)" class="dot done">✓</span>
                <span v-else-if="!m.playable" class="dot miss">∅</span>
                <span v-if="m.duration_seconds > 0" class="tabular muted">{{ Math.round(m.duration_seconds / 60) }}′</span>
              </div>
            </div>
            <span v-if="i === idx" class="pl-now">▸</span>
          </button>
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
  background: rgba(6, 8, 12, .82);
  display: flex; align-items: center; justify-content: center;
  padding: var(--sp-4);
}
.pl {
  display: flex; gap: var(--sp-4);
  width: min(1180px, 96vw); height: min(82vh, 760px);
  background: var(--c-surface);
  border: 1px solid var(--c-line);
  border-radius: var(--r-lg);
  overflow: hidden;
}
.pl-main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.pl-toolbar {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--c-line);
}
.pl-title { display: flex; align-items: baseline; gap: var(--sp-2); }
.pl-name { font-weight: 600; font-size: var(--fs-lg); }
.pl-count { font-size: var(--fs-sm); color: var(--c-text-3); }
.pl-stage { flex: 1; min-height: 0; display: flex; background: #000; }
.pl-stage :deep(.player) { flex: 1; }
.pl-empty { flex: 1; display: grid; place-items: center; color: var(--c-text-3); }

.pl-side {
  width: 300px; display: flex; flex-direction: column;
  border-left: 1px solid var(--c-line); background: var(--c-surface-2);
}
.pl-side-head { padding: var(--sp-3); font-weight: 600; border-bottom: 1px solid var(--c-line); }
.pl-items { flex: 1; overflow-y: auto; padding: var(--sp-2); display: flex; flex-direction: column; gap: 2px; }
.pl-item {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-2); border-radius: var(--r-sm);
  background: transparent; border: 1px solid transparent; cursor: pointer; text-align: left;
}
.pl-item:hover { background: var(--c-surface-3, var(--c-surface)); }
.pl-item.on { border-color: var(--c-accent, #4f8cff); background: color-mix(in srgb, var(--c-accent, #4f8cff) 14%, transparent); }
.pl-item.dim { opacity: .5; }
.pl-thumb { width: 44px; height: 28px; object-fit: cover; border-radius: 3px; background: var(--c-surface-3, #222); flex: none; }
.pl-thumb.ph { display: grid; place-items: center; font-size: 12px; color: var(--c-text-3); }
.pl-meta { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.pl-code { font-size: var(--fs-sm); }
.pl-sub { font-size: var(--fs-xs); display: flex; align-items: center; gap: var(--sp-1); }
.dot { font-size: 11px; }
.dot.done { color: var(--c-ok, #3fb950); }
.dot.miss { color: var(--c-text-3); }
.pl-now { color: var(--c-accent, #4f8cff); }
.pl-side-foot { padding: var(--sp-2) var(--sp-3); border-top: 1px solid var(--c-line); }

@media (max-width: 820px) {
  .pl { flex-direction: column; height: 92vh; }
  .pl-side { width: auto; border-left: none; border-top: 1px solid var(--c-line); max-height: 38vh; }
}
</style>
