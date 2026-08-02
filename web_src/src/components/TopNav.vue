<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { state } from '../state.js'
import { useTasks, pct } from '../composables/useTasks.js'
import { aiStatus, aiSearchIntent } from '../api.js'
import { toast } from '../utils.js'

const emit = defineEmits(['search'])
const { anyRunning, activeTasks, overallPct, runScan, runScrape, abort } = useTasks()

const searchEl = ref(null)
const kw = ref(state.q)
const aiEnabled = ref(false)
const semantic = ref(false)       // 语义搜索模式
const semanticBusy = ref(false)
const semanticActive = ref(false) // 当前是否为语义搜索结果

async function refreshAi() {
  try { const r = await aiStatus(); aiEnabled.value = !!r.enabled } catch { aiEnabled.value = false }
}

function resetFilters() {
  state.actress = []
  state.genre = []
  state.studio = ''
  state.series = ''
  state.prefix = ''
  state.year = null
  state.flags = []
}

async function submit() {
  const text = kw.value.trim()
  // 语义搜索：把自然语言转成结构化检索条件
  if (semantic.value && aiEnabled.value && text) {
    semanticBusy.value = true
    try {
      const r = await aiSearchIntent(text)
      const c = r.conditions || {}
      resetFilters()
      if (c.genres) state.genre = Array.isArray(c.genres) ? c.genres : [c.genres]
      if (c.actress) state.actress = [c.actress]
      if (c.studio) state.studio = c.studio
      if (c.series) state.series = c.series
      if (c.year) { state.year = c.year; state.sort = 'year_asc' }
      // 关键词仍作为补充文本检索
      state.q = (c.keywords && c.keywords.join(' ')) || text
      semanticActive.value = true
      toast('已按语义理解检索', 'ok')
    } catch (e) {
      toast(e.message || '语义解析失败，已退回关键词', 'err')
      state.q = text
    } finally { semanticBusy.value = false }
  } else {
    state.q = text
    semanticActive.value = false
  }
  state.page = 1
  emit('search')
}
function clearKw() {
  kw.value = ''
  semanticActive.value = false
  resetFilters()
  submit()
  searchEl.value && searchEl.value.focus()
}

/* 全局快捷键：/ 或 Ctrl+K 聚焦搜索，Esc 失焦 */
function onKey(e) {
  const tag = (e.target.tagName || '').toLowerCase()
  const typing = tag === 'input' || tag === 'textarea' || e.target.isContentEditable
  if ((e.key === '/' && !typing) || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k')) {
    e.preventDefault()
    searchEl.value && searchEl.value.focus()
  } else if (e.key === 'Escape' && document.activeElement === searchEl.value) {
    searchEl.value.blur()
  }
}
onMounted(() => { window.addEventListener('keydown', onKey); refreshAi() })
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

function toggleTheme() { state.theme = state.theme === 'dark' ? 'light' : 'dark' }
const themeIcon = computed(() => (state.theme === 'dark' ? '☾' : '☀'))
</script>

<template>
  <header class="topnav">
    <button class="btn ghost icon only-mobile" @click="state.mobileNavOpen = !state.mobileNavOpen" data-tip="菜单">☰</button>

    <div class="brand" @click="state.view = 'gallery'">
      <div class="logo">A</div>
      <span class="brand-text">AV 博物馆</span>
    </div>

    <div class="search">
      <span class="si">⌕</span>
      <input
        ref="searchEl"
        v-model="kw"
        type="search"
        :placeholder="semantic && aiEnabled ? '用自然语言描述想找的影片…' : '搜索番号、标题、女优…'"
        @keydown.enter="submit"
        @search="submit"
      />
      <button
        v-if="aiEnabled"
        class="sem-toggle"
        :class="{ on: semantic, active: semanticActive }"
        @click="semantic = !semantic"
        :data-tip="semantic ? '语义搜索：开（自然语言理解）' : '语义搜索：关（关键词）'"
      >AI</button>
      <div class="kbd" v-if="!kw && !semantic"><kbd>Ctrl</kbd><kbd>K</kbd></div>
      <button v-if="kw" class="clr" @click="clearKw" data-tip="清空">✕</button>
      <span v-if="semanticBusy" class="si busy">…</span>
    </div>

    <div class="actions">
      <!-- 任务指示器 -->
      <div class="task-wrap">
        <button
          class="btn ghost task-btn"
          :class="{ busy: anyRunning }"
          @click="state.taskPanelOpen = !state.taskPanelOpen"
          data-tip="任务中心"
        >
          <span v-if="anyRunning" class="spinner"></span>
          <span v-else class="ico">⟳</span>
          <span v-if="anyRunning" class="pctn">{{ overallPct }}%</span>
        </button>

        <div v-if="state.taskPanelOpen" class="task-pop" @click.stop>
          <div class="tp-head">
            <b>任务中心</b>
            <button class="btn ghost icon tiny" @click="state.taskPanelOpen = false">✕</button>
          </div>

          <div v-if="!activeTasks.length" class="tp-idle">
            <p class="muted">当前没有正在运行的任务</p>
          </div>

          <div v-for="t in activeTasks" :key="t.key" class="tp-item">
            <div class="tp-row">
              <span class="tp-name">{{ t.label }}</span>
              <span class="tp-n tabular">{{ t.done }} / {{ t.total || '?' }}</span>
            </div>
            <div class="progress" :class="{ indeterminate: pct(t) < 0 }">
              <i v-if="pct(t) >= 0" :style="{ width: pct(t) + '%' }"></i>
            </div>
            <div class="tp-row">
              <span class="tp-msg ellipsis">{{ t.message || '处理中…' }}</span>
              <button class="btn tiny ghost" @click="abort(t.key)">取消</button>
            </div>
          </div>

          <div class="tp-foot">
            <button class="btn tiny" :disabled="state.task.scan.running" @click="runScan({})">扫描媒体库</button>
            <button class="btn tiny" :disabled="state.task.scrape.running" @click="runScrape({ missing_only: true })">刮削缺失</button>
          </div>
        </div>
      </div>

      <button class="btn ghost icon" @click="toggleTheme" :data-tip="state.theme === 'dark' ? '浅色主题' : '深色主题'">{{ themeIcon }}</button>
      <button class="btn ghost icon" :class="{ active: state.view === 'settings' }" @click="state.view = 'settings'" data-tip="设置">⚙</button>
    </div>
  </header>

  <div v-if="state.taskPanelOpen" class="pop-catcher" @click="state.taskPanelOpen = false"></div>
</template>

<style scoped>
.only-mobile { display: none; }
@media (max-width: 900px) {
  .only-mobile { display: grid; }
  .brand-text { display: none; }
}

.brand { cursor: pointer; }

.search .clr {
  position: absolute; right: 10px; top: 50%;
  transform: translateY(-50%);
  width: 20px; height: 20px;
  display: grid; place-items: center;
  border-radius: 50%;
  color: var(--c-text-3);
  font-size: 11px;
}
.search .clr:hover { background: var(--c-surface-3); color: var(--c-text); }
.sem-toggle {
  position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
  padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 700;
  letter-spacing: .3px; border: 1px solid var(--c-line-strong);
  color: var(--c-text-3); background: var(--c-surface-2); cursor: pointer;
}
.sem-toggle.on { color: #fff; background: var(--c-accent); border-color: var(--c-accent); }
.sem-toggle.active { box-shadow: 0 0 0 2px color-mix(in srgb, var(--c-accent) 40%, transparent); }
.si.busy { position: absolute; right: 10px; top: 50%; transform: translateY(-50%); color: var(--c-accent); }

.task-wrap { position: relative; }
.task-btn { gap: 6px; padding: 0 var(--sp-2); min-width: 32px; }
.task-btn.busy { color: var(--c-primary-h); }
.task-btn .ico { font-size: 15px; }
.task-btn .pctn { font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }

.pop-catcher { position: fixed; inset: 0; z-index: 40; }

.task-pop {
  position: absolute;
  top: calc(100% + 8px); right: 0;
  z-index: 50;
  width: 320px;
  background: var(--c-surface);
  border: 1px solid var(--c-line-strong);
  border-radius: var(--r-md);
  box-shadow: var(--sh-3);
  overflow: hidden;
  animation: rise-in var(--t-slow);
}
.tp-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--c-line);
}
.tp-idle { padding: var(--sp-5); text-align: center; font-size: var(--fs-sm); }
.tp-item {
  padding: var(--sp-3);
  display: flex; flex-direction: column; gap: var(--sp-2);
  border-bottom: 1px solid var(--c-line);
}
.tp-row { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); }
.tp-name { font-size: var(--fs-md); font-weight: 500; }
.tp-n { font-size: var(--fs-sm); color: var(--c-text-3); }
.tp-msg { font-size: var(--fs-xs); color: var(--c-text-3); flex: 1; }
.tp-foot { padding: var(--sp-2) var(--sp-3); display: flex; gap: var(--sp-2); }
</style>
