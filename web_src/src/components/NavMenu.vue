<script setup>
import { ref, computed, reactive, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { state, NAV_ICONS } from '../state.js'

const props = defineProps({
  // 按钮文字（如「全部」）
  label: { type: String, required: true },
  // 分组菜单：[{ key, title, items: [{ id, label, icon }] }]
  menus: { type: Array, required: true },
  // hover 模式：鼠标悬停展开，移开关闭（默认点击切换）
  hover: { type: Boolean, default: false },
})

// 「更多」省略号图标（横向三点）
const moreDots = 'M6 10c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm12 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm-6 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z'

const open = ref(false)
const btnEl = ref(null)
const panelEl = ref(null)
// 下拉面板固定定位坐标（基于按钮位置计算，避免被 main-tabs 的 overflow 裁剪）
const pos = reactive({ left: 0, top: 0 })
// hover 展开/收起延迟，避免快速移过时闪烁
let hoverTimer = null

/* 当前是否有任一菜单项处于激活 */
const anyActive = computed(() => {
  const m = props.menus || []
  return m.some((g) => (g.items || []).some((it) => it.id === state.view))
})

const activeView = computed(() => state.view)

function go(id) {
  state.view = id
  open.value = false
}
function toggle() {
  if (props.hover) return
  open.value = !open.value
  if (open.value) nextTick(computePos)
}
// hover 模式：悬停展开 / 移开关闭（带延迟防抖）
function onEnter() {
  if (!props.hover) return
  clearTimeout(hoverTimer)
  hoverTimer = setTimeout(() => { open.value = true; nextTick(computePos) }, 120)
}
function onLeave() {
  if (!props.hover) return
  clearTimeout(hoverTimer)
  // 稍长的关闭延迟，给鼠标从按钮移动到面板留出时间
  hoverTimer = setTimeout(() => { open.value = false }, 250)
}
// 面板自身 hover：保持展开，防止从按钮移到面板的间隙触发关闭
function onPanelEnter() {
  if (!props.hover) return
  clearTimeout(hoverTimer)
}
function onPanelLeave() {
  if (!props.hover) return
  onLeave()
}
/* 计算面板位置：贴近按钮左下，若右侧越界则右对齐 */
function computePos() {
  if (!btnEl.value) return
  const r = btnEl.value.getBoundingClientRect()
  const panelW = panelEl.value ? panelEl.value.offsetWidth : 200
  const vw = window.innerWidth
  let left = r.left
  if (left + panelW > vw - 8) left = Math.max(8, vw - panelW - 8)
  pos.left = left
  pos.top = r.bottom + 8
}

/* 点击外部关闭 */
function onDocClick(e) {
  if (btnEl.value && !btnEl.value.contains(e.target)) open.value = false
}
/* 滚动/缩放时重算位置 */
function onReposition() { if (open.value) computePos() }
onMounted(() => {
  document.addEventListener('click', onDocClick)
  window.addEventListener('scroll', onReposition, true)
  window.addEventListener('resize', onReposition)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  window.removeEventListener('scroll', onReposition, true)
  window.removeEventListener('resize', onReposition)
})
</script>

<template>
  <div ref="btnEl" class="navmenu" :class="{ open }" @mouseenter="onEnter" @mouseleave="onLeave">
    <button
      class="tab pure-icon more-btn"
      :class="{ active: anyActive }"
      :title="$t('nav.more')"
      @click.stop="toggle"
    >
      <svg viewBox="0 0 24 24" fill="currentColor"><path :d="moreDots" /></svg>
    </button>

    <!-- Teleport 到 body，脱离 main-tabs 的 overflow 裁剪 -->
    <Teleport to="body">
      <transition name="nm">
        <div
          v-if="open"
          ref="panelEl"
          class="nm-panel"
          :style="{ left: pos.left + 'px', top: pos.top + 'px' }"
          @mouseenter="onPanelEnter"
          @mouseleave="onPanelLeave"
        >
          <div v-for="g in menus" :key="g.key" class="nm-group">
            <div class="nm-group-title">{{ $t(g.title) }}</div>
            <button
              v-for="it in g.items"
              :key="it.id"
              class="nm-item"
              :class="{ on: activeView === it.id }"
              @click="go(it.id)"
            >
              <span v-if="NAV_ICONS[it.icon]" class="nm-ico">
                <svg viewBox="0 0 24 24" fill="currentColor"><path :d="NAV_ICONS[it.icon]" /></svg>
              </span>
              <span class="nm-label">{{ $t(it.label) }}</span>
              <span v-if="activeView === it.id" class="nm-check">✓</span>
            </button>
          </div>
        </div>
      </transition>
    </Teleport>
  </div>
</template>

<style scoped>
.navmenu { position: relative; }
.more-btn { display: grid; place-items: center; width: 32px; padding: 0; }
.more-btn svg { width: 17px; height: 17px; display: block; }

.nm-panel {
  position: fixed; /* Teleport 到 body，用固定定位 */
  min-width: 190px;
  background: var(--c-surface);
  border: 1px solid var(--c-line-strong);
  border-radius: var(--r-md);
  box-shadow: var(--sh-3);
  padding: 6px;
  z-index: 200;
}
.nm-group { padding-bottom: 4px; }
.nm-group + .nm-group { border-top: 1px solid var(--c-line); margin-top: 4px; padding-top: 4px; }
.nm-group-title {
  font-size: 11px; font-weight: 600; color: var(--c-text-3);
  padding: 4px 8px 2px; text-transform: uppercase; letter-spacing: .4px;
}
.nm-item {
  width: 100%; display: flex; align-items: center; gap: 8px;
  padding: 7px 10px; border: 0; background: none; border-radius: var(--r-sm);
  color: var(--c-text-2); font-size: var(--fs-sm); cursor: pointer; text-align: left;
}
.nm-item:hover { background: var(--c-surface-3); color: var(--c-text); }
.nm-item.on { background: color-mix(in srgb, var(--c-accent, #4f8cff) 14%, transparent); color: var(--c-accent, #4f8cff); }
.nm-ico { width: 18px; height: 18px; flex: none; opacity: .85; }
.nm-ico svg { width: 18px; height: 18px; display: block; }
.nm-label { flex: 1; }
.nm-check { color: var(--c-accent, #4f8cff); font-weight: 700; }

.nm-enter-active, .nm-leave-active { transition: opacity .12s, transform .12s; }
.nm-enter-from, .nm-leave-to { opacity: 0; transform: translateY(-4px); }
</style>
