<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { state, clearPlayQueue } from '../state.js'
import { coverThumbUrl } from '../api.js'

const props = defineProps({
  item: { type: Object, default: null },
})

// 展开态 / 收起态（定时自动收起）
const expanded = ref(true)
let hideTimer = null
let hovered = false

const cover = computed(() => (props.item ? coverThumbUrl(props.item.id, 120) : ''))

function resume() {
  state.playQueue.open = true
}
function clear() {
  clearPlayQueue()
}
function toggleExpand() {
  expanded.value = !expanded.value
  if (expanded.value) scheduleHide()
}

// 定时自动收起
function scheduleHide() {
  clearTimeout(hideTimer)
  hideTimer = setTimeout(() => {
    if (!hovered) expanded.value = false
  }, 5000)
}
function onEnter() { hovered = true; clearTimeout(hideTimer) }
function onLeave() { hovered = false; if (expanded.value) scheduleHide() }

watch(() => state.playQueue.open, (open) => {
  // 重新打开播放器时隐藏
  if (open) expanded.value = false
})

/* 上报底部占用高度，让 toast 等底部浮层上移避让（展开态条更高） */
function syncDock() {
  if (typeof document === 'undefined') return
  document.documentElement.style.setProperty('--dock-mini', expanded.value ? '72px' : '48px')
}

watch(expanded, syncDock)
onMounted(() => { scheduleHide(); syncDock() })
onBeforeUnmount(() => {
  clearTimeout(hideTimer)
  if (typeof document !== 'undefined') document.documentElement.style.setProperty('--dock-mini', '0px')
})
</script>

<template>
  <transition name="mpb">
    <!-- 展开的迷你播放条 -->
    <div
      v-if="expanded"
      class="mpb"
      @click="resume"
      @mouseenter="onEnter"
      @mouseleave="onLeave"
    >
      <img v-if="cover" :src="cover" class="mpb-thumb" alt="" />
      <div v-else class="mpb-thumb ph">▶</div>
      <div class="mpb-meta">
        <div class="mpb-code ellipsis">{{ item.code || item.title }}</div>
        <div class="mpb-title ellipsis">{{ item.title }}</div>
        <div class="mpb-count">{{ state.playQueue.queue.length }} 部 · {{ state.playQueue.source }}</div>
      </div>
      <div class="spacer"></div>
      <button class="btn tiny primary" @click.stop="resume">▶ 继续播放</button>
      <button class="btn tiny ghost" @click.stop="clear" title="清空队列">✕</button>
    </div>

    <!-- 收起的悬浮点（点击继续播放 / 展开） -->
    <button
      v-else
      class="mpb-dot"
      :title="item.code || item.title"
      @click="resume"
    >
      <span v-if="cover" class="mpb-dot-thumb"><img :src="cover" alt="" /></span>
      <span class="mpb-dot-ico">▶</span>
    </button>
  </transition>
</template>

<style scoped>
.mpb {
  position: fixed; left: 50%;
  bottom: calc(16px + var(--dock-bulk, 0px)); /* 批量操作栏出现时整体上移 */
  transform: translateX(-50%);
  transition: bottom var(--t-base, .2s);
  z-index: 150;
  display: flex; align-items: center; gap: var(--sp-3);
  width: min(560px, 94vw);
  padding: 10px 14px;
  background: var(--c-surface-2);
  border: 1px solid var(--c-line);
  border-radius: var(--r-lg);
  box-shadow: 0 8px 30px rgba(0,0,0,.35);
  cursor: pointer;
}
.mpb:hover { border-color: var(--c-accent, #4f8cff); }
.mpb-thumb { width: 72px; height: 48px; object-fit: cover; border-radius: 6px; background: var(--c-surface-3); flex: none; }
.mpb-thumb.ph { display: grid; place-items: center; font-size: 15px; color: var(--c-text-3); }
.mpb-meta { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.mpb-code { font-weight: 700; font-size: var(--fs-sm); }
.mpb-title { font-size: var(--fs-xs); color: var(--c-text-2); }
.mpb-count { font-size: var(--fs-xs); color: var(--c-text-3); }

/* 收起的悬浮点 */
.mpb-dot {
  position: fixed; left: 50%;
  bottom: calc(20px + var(--dock-bulk, 0px));
  transform: translateX(-50%);
  transition: bottom var(--t-base, .2s);
  z-index: 150;
  display: flex; align-items: center; gap: 8px;
  padding: 4px;
  background: var(--c-surface-2);
  border: 1px solid var(--c-line);
  border-radius: 999px;
  box-shadow: 0 4px 16px rgba(0,0,0,.3);
  cursor: pointer;
}
.mpb-dot:hover { border-color: var(--c-accent, #4f8cff); }
.mpb-dot-thumb { width: 34px; height: 34px; border-radius: 50%; overflow: hidden; flex: none; }
.mpb-dot-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.mpb-dot-ico {
  width: 34px; height: 34px; display: grid; place-items: center;
  background: var(--c-accent, #4f8cff); color: #fff; border-radius: 50%;
  font-size: 13px;
}

.mpb-enter-active, .mpb-leave-active { transition: opacity .25s, transform .25s; }
.mpb-enter-from, .mpb-leave-to { opacity: 0; transform: translate(-50%, 10px); }
</style>
