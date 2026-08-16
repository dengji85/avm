<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { state, NAV_TABS } from '../state.js'
import { useTasks, pct, etaSec, phaseLabel, fmtDur, fetchScrapeLogs } from '../composables/useTasks.js'
import { aiStatus, aiSearchIntent } from '../api.js'
import { toast, fmtAgo } from '../utils.js'
import { t } from '../i18n/index.js'

const emit = defineEmits(['search'])
const { anyRunning, activeTasks, lastFinished, taskHistory, overallPct, abort, clearHistory } = useTasks()
const showScrapeLogs = ref(null)   // 当前展开的明细所属任务 key（或历史 id）
const openHistory = ref(new Set()) // 展开的历史记录 id 集合

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
      toast(t('nav.semanticSearched'), 'ok')
    } catch (e) {
      toast(e.message || t('nav.semanticFallback'), 'err')
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

// 任务中心「前往维护中心」：切到维护视图并关闭任务面板，避免浮层遮挡
function goMaintenance() {
  state.taskPanelOpen = false
  state.view = 'maintenance'
}
const themeIcon = computed(() => (state.theme === 'dark' ? '☾' : '☀'))

// 窄屏下隐藏非核心 Tab（如统计/赏析），保留影片库等核心入口；窗口宽度响应式
const isNarrow = ref(typeof window !== 'undefined' && window.innerWidth <= 640)
function onResize() { isNarrow.value = window.innerWidth <= 640 }
onMounted(() => window.addEventListener('resize', onResize))
onBeforeUnmount(() => window.removeEventListener('resize', onResize))
// 手机上保留：首页、影片库；统计等可隐藏（用户确认手机上非必需）
const NAV_TABS_CORE = ['home', 'gallery', 'collections']
const navTabsVisible = computed(() => NAV_TABS.filter(t => !isNarrow.value || NAV_TABS_CORE.includes(t.id)))

// 进度环：用 conic-gradient 模拟（indeterminate 时走 CSS 旋转动画）
function ringStyle(p) {
  if (p < 0) return {}
  const deg = Math.min(100, Math.max(0, p)) * 3.6
  const c1 = 'var(--c-primary-h, #4f8cff)'
  const c2 = 'var(--c-line-strong, #2a2f3a)'
  return { background: `conic-gradient(${c1} ${deg}deg, ${c2} ${deg}deg)` }
}

function toggleHistory(id) {
  const s = new Set(openHistory.value)
  s.has(id) ? s.delete(id) : s.add(id)
  openHistory.value = s
}

// 从 counters 中提取自动清理的孤儿元数据计数（cleaned_actresses 等）
function cleanedOrphans(t) {
  const c = (t && t.counters) || {}
  const labels = { actresses: 'tasks.kActress', genres: 'tasks.kGenre', tags: 'tasks.kTag', studios: 'tasks.kStudio', series: 'tasks.kSeries' }
  const out = []
  for (const [k, label] of Object.entries(labels)) {
    const v = c['cleaned_' + k]
    if (v) out.push({ k: t(label), v })
  }
  return out
}

// 历史展开：刮削任务的逐文件日志（来自 DB 持久化记录）
const scrapeFilters = [
  { v: '', label: 'tasks.all' },
  { v: 'error', label: 'tasks.error' },
  { v: 'miss', label: 'tasks.miss' },
  { v: 'ok', label: 'tasks.ok' },
]
const scrapeFilter = ref('')
const scrapeLogItems = ref([])
const scrapeLogTotal = ref(0)
const scrapeLogBusy = ref(false)

async function setScrapeFilter(taskId, f) {
  scrapeFilter.value = f
  scrapeLogBusy.value = true
  const r = await fetchScrapeLogs(taskId, { status: f, page: 1, size: 100 })
  scrapeLogItems.value = r.items || []
  scrapeLogTotal.value = r.total || 0
  scrapeLogBusy.value = false
}
</script>

<template>
  <header class="topnav">
    <button class="btn ghost icon only-mobile" @click="state.mobileNavOpen = !state.mobileNavOpen" :data-tip="$t('nav.menu')">☰</button>

    <div class="brand" @click="state.view = 'home'">
      <div class="logo">匣</div>
      <span class="brand-text">{{ $t('nav.brand') }}</span>
    </div>

    <nav class="main-tabs" aria-label="主导航">
      <button
        v-for="t in navTabsVisible"
        :key="t.id"
        class="tab"
        :class="{ active: state.view === t.id }"
        @click="state.view = t.id"
      >{{ $t(t.label) }}</button>
    </nav>

    <div class="search">
      <span class="si">⌕</span>
      <input
        ref="searchEl"
        v-model="kw"
        type="text"
        :placeholder="semantic && aiEnabled ? $t('tasks.semanticOn') : $t('tasks.searchPlaceholder')"
        @keydown.enter="submit"
      />
      <button
        v-if="aiEnabled"
        class="sem-toggle"
        :class="{ on: semantic, active: semanticActive }"
        @click="semantic = !semantic"
        :data-tip="semantic ? $t('tasks.semanticOn') : $t('tasks.semanticOff')"
      >AI</button>
      <div class="kbd" v-if="!kw && !semantic"><kbd>Ctrl</kbd><kbd>K</kbd></div>
      <button v-if="kw" class="clr" @click="clearKw" :data-tip="$t('common.clear')">✕</button>
      <span v-if="semanticBusy" class="si busy">…</span>
    </div>

    <div class="actions">
      <!-- 任务指示器 -->
      <div class="task-wrap">
        <button
          class="btn ghost task-btn"
          :class="{ busy: anyRunning }"
          @click="state.taskPanelOpen = !state.taskPanelOpen"
          :data-tip="$t('tasks.center')"
        >
          <span v-if="anyRunning" class="spinner"></span>
          <span v-else class="ico">🗂</span>
          <span v-if="anyRunning" class="pctn">{{ overallPct }}%</span>
        </button>

        <div v-if="state.taskPanelOpen" class="task-pop" @click.stop>
          <div class="tp-head">
            <b>{{ $t('tasks.center') }}</b>
            <span class="tp-head-sub">{{ anyRunning ? $t('tasks.running') : $t('tasks.idle') }}</span>
            <button class="btn ghost icon tiny" @click="state.taskPanelOpen = false">✕</button>
          </div>

          <!-- 进行中：当前运行 / 最近一次完成的任务 -->
          <div class="tp-section">
            <div v-if="!activeTasks.length && !lastFinished.length" class="tp-idle">
              <div class="tp-idle-ico">✓</div>
              <p class="muted">{{ $t('tasks.noRunning') }}</p>
              <p class="muted sm">{{ $t('tasks.idleHint') }}</p>
            </div>

            <div
              v-for="t in [...activeTasks, ...lastFinished]"
              :key="t.key"
              class="tp-item"
              :class="{ done: !t.running }"
            >
              <!-- 顶部：名称 + 状态徽标 -->
              <div class="tp-top">
                <span class="tp-name">
                  <span class="tp-dot" :class="{ live: t.running }"></span>
                  {{ t.label }}
                </span>
                <span class="tp-pill" :class="t.running ? 'run' : (t.cancelled ? 'cancel' : 'ok')">
                  {{ phaseLabel(t) }}
                </span>
              </div>

              <!-- 进度环 + 数字 -->
              <div class="tp-body">
                <div
                  class="tp-ring"
                  :class="{ indeterminate: pct(t) < 0 }"
                  :style="ringStyle(pct(t))"
                >
                  <template v-if="pct(t) >= 0"><b class="tabular">{{ pct(t) }}%</b></template>
                  <span v-else class="tp-ring-q">…</span>
                </div>
                <div class="tp-stats">
                  <div class="tp-stat">
                    <span class="v tabular">{{ t.done }}<i v-if="t.total"> / {{ t.total }}</i></span>
                    <span class="k">{{ $t('tasks.processed') }}</span>
                  </div>
                  <div class="tp-stat ok"><span class="v tabular">{{ t.ok }}</span><span class="k">{{ $t('tasks.ok') }}</span></div>
                  <div class="tp-stat bad"><span class="v tabular">{{ t.fail || t.error_count || 0 }}</span><span class="k">{{ $t('tasks.error') }}</span></div>
                  <div class="tp-stat"><span class="v tabular">{{ fmtDur(t.elapsed) }}</span><span class="k">{{ $t('tasks.elapsed') }}</span></div>
                  <div class="tp-stat" v-if="etaSec(t) != null"><span class="v tabular">{{ fmtDur(etaSec(t)) }}</span><span class="k">{{ $t('tasks.eta') }}</span></div>
                </div>
              </div>

              <!-- 进度条 -->
              <div class="progress" :class="{ indeterminate: pct(t) < 0 }">
                <i v-if="pct(t) >= 0" :style="{ width: pct(t) + '%' }"></i>
              </div>

              <!-- 当前项：正在刮削/扫描哪个 -->
              <div v-if="t.running" class="tp-current ellipsis" :title="t.current || t.message">
                <span class="tk">{{ t.key === 'scan' ? $t('tasks.scan') : $t('tasks.scrape') }}</span>
                {{ t.current || t.message || $t('tasks.processing') }}
              </div>

              <!-- 操作 + 明细 + 失败统计 -->
              <div class="tp-actions">
                <span v-if="t.logs && t.logCounts.error" class="tp-badge err" :title="$t('tasks.errMovies')">⚠ {{ $t('tasks.error') }} {{ t.logCounts.error }}</span>
                <span v-if="t.logCounts.miss" class="tp-badge warn" :title="$t('tasks.missMovies')">{{ $t('tasks.miss') }} {{ t.logCounts.miss }}</span>
                <button
                  v-if="t.logs && t.logs.length"
                  class="btn tiny ghost"
                  :class="{ on: showScrapeLogs === t.key }"
                  @click="showScrapeLogs = showScrapeLogs === t.key ? null : t.key"
                >{{ $t('tasks.detail') }} {{ t.logs.length }}</button>
                <button v-if="t.running" class="btn tiny ghost danger" @click="abort(t.key)">{{ $t('common.cancel') }}</button>
              </div>

              <!-- 失败原因汇总 -->
              <div v-if="t.reasons && t.reasons.length" class="tp-reasons">
                <span class="tr-h">{{ $t('tasks.reasonTop') }}</span>
                <span v-for="r in t.reasons" :key="r.name" class="tr-chip" :class="r.cls">{{ r.name }} ×{{ r.n }}</span>
              </div>

              <!-- 自动清理结果 -->
              <div v-if="t.counters && t.counters.cleaned_files != null" class="tp-clean">
                <span class="tc-ico">🧹</span>
                {{ $t('tasks.cleanAuto', { n: t.counters.cleaned_files || 0 }) }}
                <template v-if="cleanedOrphans(t).length">
                  {{ $t('tasks.cleanOrphans') }}
                  <span v-for="o in cleanedOrphans(t)" :key="o.k">{{ o.k }} {{ o.v }} </span>
                </template>
              </div>

              <!-- 未命中 / 错误明细 -->
              <transition name="tp-slide">
                <div v-if="showScrapeLogs === t.key && t.logs && t.logs.length" class="tp-logs">
                  <div v-for="(l, i) in t.logs.slice().reverse()" :key="i" class="tp-log" :class="l.level">
                    <span class="lt tabular">{{ l.t }}</span>
                    <span v-if="l.code" class="lc">{{ l.code }}</span>
                    <span class="lm">{{ l.msg }}</span>
                  </div>
                </div>
              </transition>
            </div>
          </div>

          <!-- 历史记录：任务结束后仍可回看失败明细 -->
          <div v-if="taskHistory.length" class="tp-section history">
            <div class="tp-sec-head">
              <span>{{ $t('tasks.historyTitle') }}</span>
              <button class="btn ghost tiny" @click="clearHistory">{{ $t('common.clear') }}</button>
            </div>
            <div v-for="h in taskHistory" :key="h.id" class="tp-hist" :class="{ fail: h.fail }">
              <button class="tp-hist-row" @click="toggleHistory(h.id)">
                <span class="th-ico" :class="{ err: h.fail, warn: !h.fail && h.logCounts?.miss }">
                  {{ h.fail ? '⚠' : (h.cancelled ? '⊘' : '✓') }}
                </span>
                <span class="th-label">{{ h.label }}</span>
                <span class="th-meta tabular">{{ fmtAgo(h.finishedAt) }}</span>
                <span class="th-stat tabular">{{ $t('tasks.okBad', { ok: h.ok, bad: h.fail }) }}</span>
                <span class="th-caret" :class="{ open: openHistory.has(h.id) }">▾</span>
              </button>
              <transition name="tp-slide">
                <div v-if="openHistory.has(h.id)" class="tp-hist-body">
                  <!-- 失败原因 -->
                  <div v-if="h.reasons && h.reasons.length" class="tp-reasons">
                    <span class="tr-h">{{ $t('tasks.failReasonTop') }}</span>
                    <span v-for="r in h.reasons" :key="r.name" class="tr-chip" :class="r.cls">{{ r.name }} ×{{ r.n }}</span>
                  </div>
                  <!-- 扫描类：运行期日志明细 -->
                  <div v-if="h.key === 'scan' && h.logs && h.logs.length" class="tp-logs">
                    <div v-for="(l, i) in h.logs.slice().reverse()" :key="i" class="tp-log" :class="l.level">
                      <span class="lt tabular">{{ l.t }}</span>
                      <span v-if="l.code" class="lc">{{ l.code }}</span>
                      <span class="lm">{{ l.msg }}</span>
                    </div>
                  </div>
                  <!-- 刮削类：从 DB 拉取逐文件日志，可定位失败文件与原因 -->
                  <div v-if="h.key === 'scrape' && h.task_id" class="tp-scrapelogs">
                    <div class="ts-filters">
                      <button
                        v-for="f in scrapeFilters"
                        :key="f.v"
                        class="ts-f"
                        :class="{ on: scrapeFilter === f.v }"
                        @click="setScrapeFilter(h.task_id, f.v)"
                      >{{ $t(f.label) }}</button>
                      <span class="ts-meta tabular">{{ $t('tasks.logTotal', { n: scrapeLogTotal }) }}</span>
                    </div>
                    <div v-if="scrapeLogBusy" class="muted sm" style="padding:6px 8px">{{ $t('tasks.loading') }}</div>
                    <div v-else-if="!scrapeLogItems.length" class="muted sm" style="padding:6px 8px">{{ $t('tasks.noRecord') }}</div>
                    <div v-else class="tp-logs slim">
                      <div
                        v-for="(l, i) in scrapeLogItems"
                        :key="l.id"
                        class="tp-log" :class="l.status"
                        @click="state.view = 'detail'; state.currentId = l.movie_id"
                        :data-tip="l.movie_id ? $t('tasks.clickView') : ''"
                      >
                        <span class="ls-dot" :class="l.status"></span>
                        <span class="lc">{{ l.code || '—' }}</span>
                        <span class="lm">{{ l.reason }}</span>
                        <span class="lt tabular">{{ l.elapsed_ms }}ms</span>
                      </div>
                    </div>
                  </div>
                  <p v-if="h.key !== 'scrape' && !(h.key === 'scan' && h.logs?.length) && !(h.reasons?.length)" class="muted sm" style="padding:6px 8px">{{ $t('tasks.noMissOrErr') }}</p>
                </div>
              </transition>
            </div>
          </div>

          <div class="tp-foot">
            <span class="muted sm" style="margin-right:auto">{{ $t('tasks.maintHint') }}</span>
            <button class="btn tiny ghost" @click="goMaintenance">{{ $t('tasks.goMaint') }}</button>
          </div>
        </div>
      </div>

      <button class="btn ghost icon" @click="toggleTheme" :data-tip="state.theme === 'dark' ? $t('nav.themeLight') : $t('nav.themeDark')">{{ themeIcon }}</button>
      <button class="btn ghost icon" :class="{ active: state.view === 'settings' }" @click="state.view = 'settings'" :data-tip="$t('nav.settings')">⚙</button>
    </div>
  </header>

  <div v-if="state.taskPanelOpen" class="pop-catcher" @click="state.taskPanelOpen = false"></div>
</template>

<style scoped>
.topnav {
  position: relative;
  z-index: 60;
}
.only-mobile { display: none; }
@media (max-width: 900px) {
  .only-mobile { display: grid; }
  .brand-text { display: none; }
}

.brand { cursor: pointer; }

.main-tabs {
  display: flex;
  align-items: center;
  gap: 2px;
  margin: 0 4px;
  padding: 3px;
  background: var(--c-surface-2, #161a22);
  border: 1px solid var(--c-line, #232833);
  border-radius: var(--r-pill, 999px);
  overflow-x: auto;
  scrollbar-width: none;
  max-width: 100%;
}
.main-tabs::-webkit-scrollbar { display: none; }
.tab {
  appearance: none;
  border: 0;
  background: transparent;
  color: var(--c-text-dim, #9aa3b2);
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  padding: 6px 14px;
  border-radius: var(--r-pill, 999px);
  cursor: pointer;
  transition: background .15s, color .15s;
  white-space: nowrap;
  flex: 0 0 auto;
}
.tab:hover { color: var(--c-text, #e8ebf0); background: var(--c-surface-3, #1e232e); }
.tab.active {
  color: #fff;
  background: linear-gradient(180deg, var(--c-primary, #4f8cff), var(--c-primary-2, #3a6fd8));
  box-shadow: 0 1px 6px rgba(79,140,255,.35);
}

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

/* 任务中心按钮在最右侧，tooltip 改为右对齐，避免超出视口被裁切 */
.task-btn[data-tip]::after { left: auto; right: 0; transform: none; }

.pop-catcher { position: fixed; inset: 0; z-index: 40; }

.task-pop {
  position: absolute;
  top: calc(100% + 8px); right: 0;
  z-index: 50;
  width: 340px;
  max-height: min(78vh, 620px);
  display: flex; flex-direction: column;
  background: var(--c-surface);
  border: 1px solid var(--c-line-strong);
  border-radius: var(--r-md);
  box-shadow: var(--sh-3);
  overflow: hidden;
  animation: rise-in var(--t-slow);
}
.tp-head {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--c-line);
  flex: 0 0 auto;
}
.tp-head b { font-size: var(--fs-md); }
.tp-head-sub {
  font-size: var(--fs-xs); color: var(--c-text-3);
  margin-right: auto;
}
.tp-idle {
  padding: var(--sp-6) var(--sp-4);
  text-align: center;
}
.tp-idle-ico {
  width: 44px; height: 44px; margin: 0 auto var(--sp-3);
  display: grid; place-items: center;
  border-radius: 50%;
  background: color-mix(in srgb, var(--c-ok, #2e9e5b) 16%, transparent);
  color: var(--c-ok, #2e9e5b);
  font-size: 22px; font-weight: 700;
}
.tp-idle .sm { font-size: var(--fs-xs); margin-top: 2px; }

.tp-item {
  padding: var(--sp-3);
  display: flex; flex-direction: column; gap: var(--sp-2);
  border-bottom: 1px solid var(--c-line);
  overflow-y: auto;
}
.tp-item.done { opacity: .96; }

.tp-top { display: flex; align-items: center; justify-content: space-between; }
.tp-name { display: flex; align-items: center; gap: 6px; font-size: var(--fs-md); font-weight: 600; }
.tp-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--c-text-3); flex: 0 0 auto;
}
.tp-dot.live {
  background: var(--c-primary-h, #4f8cff);
  box-shadow: 0 0 0 0 color-mix(in srgb, var(--c-primary-h, #4f8cff) 60%, transparent);
  animation: tp-pulse 1.4s infinite;
}
.tp-pill {
  font-size: var(--fs-xs); font-weight: 600;
  padding: 2px 8px; border-radius: 999px;
  background: var(--c-surface-3); color: var(--c-text-3);
}
.tp-pill.run { background: color-mix(in srgb, var(--c-primary-h, #4f8cff) 18%, transparent); color: var(--c-primary-h, #4f8cff); }
.tp-pill.ok { background: color-mix(in srgb, var(--c-ok, #2e9e5b) 18%, transparent); color: var(--c-ok, #2e9e5b); }
.tp-pill.cancel { background: color-mix(in srgb, var(--c-warning, #d9a23a) 18%, transparent); color: var(--c-warning, #d9a23a); }

.tp-body { display: flex; align-items: center; gap: var(--sp-3); }
.tp-ring {
  --sz: 52px;
  width: var(--sz); height: var(--sz); flex: 0 0 auto;
  border-radius: 50%;
  display: grid; place-items: center;
  position: relative;
}
.tp-ring::after {
  content: ''; position: absolute; inset: 5px;
  border-radius: 50%; background: var(--c-surface);
}
.tp-ring > * { position: relative; z-index: 1; }
.tp-ring b { font-size: var(--fs-md); font-variant-numeric: tabular-nums; }
.tp-ring-q { font-size: 18px; color: var(--c-primary-h, #4f8cff); }
.tp-ring.indeterminate { animation: tp-spin 1s linear infinite; }
.tp-ring.indeterminate::before {
  content: ''; position: absolute; inset: 0; border-radius: 50%;
  background: conic-gradient(var(--c-primary-h, #4f8cff) 0 35%, transparent 35% 100%);
}

.tp-stats {
  flex: 1; display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px var(--sp-2);
}
.tp-stat { display: flex; flex-direction: column; line-height: 1.15; }
.tp-stat .v { font-size: var(--fs-md); font-weight: 600; }
.tp-stat .v i { font-style: normal; color: var(--c-text-3); font-weight: 400; font-size: var(--fs-xs); }
.tp-stat .k { font-size: var(--fs-xs); color: var(--c-text-3); }
.tp-stat.ok .v { color: var(--c-ok, #2e9e5b); }
.tp-stat.bad .v { color: var(--c-danger, #e5484d); }

.tp-current {
  font-size: var(--fs-xs); color: var(--c-text-2);
  background: var(--c-surface-2);
  border: 1px solid var(--c-line);
  border-radius: var(--r-sm);
  padding: 4px 8px;
  max-width: 100%;
}
.tp-current .tk {
  display: inline-block;
  font-size: 10px; font-weight: 700;
  color: #fff; background: var(--c-primary-h, #4f8cff);
  padding: 1px 6px; border-radius: 4px; margin-right: 6px;
}

.tp-badge {
  font-size: var(--fs-xs); font-weight: 600;
  padding: 2px 8px; border-radius: 999px; white-space: nowrap;
}
.tp-badge.err { background: color-mix(in srgb, var(--c-danger, #e5484d) 18%, transparent); color: var(--c-danger, #e5484d); }
.tp-badge.warn { background: color-mix(in srgb, var(--c-warning, #d9a23a) 18%, transparent); color: var(--c-warning, #d9a23a); }

.tp-reasons {
  display: flex; flex-wrap: wrap; gap: 4px 6px; align-items: center;
  font-size: var(--fs-xs);
  border-top: 1px dashed var(--c-line); padding-top: var(--sp-2);
}
.tp-reasons .tr-h { color: var(--c-text-3); }
.tr-chip {
  padding: 1px 7px; border-radius: 999px;
  background: var(--c-surface-3); color: var(--c-text-2);
}
.tr-chip.err { background: color-mix(in srgb, var(--c-danger, #e5484d) 16%, transparent); color: var(--c-danger, #e5484d); }
.tr-chip.warn { background: color-mix(in srgb, var(--c-warning, #d9a23a) 16%, transparent); color: var(--c-warning, #d9a23a); }

.tp-clean {
  font-size: var(--fs-xs); color: var(--c-text-2);
  display: flex; flex-wrap: wrap; gap: 4px; align-items: center;
  background: color-mix(in srgb, var(--c-ok, #2e9e5b) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--c-ok, #2e9e5b) 30%, transparent);
  border-radius: var(--r-sm); padding: 3px 8px;
}
.tp-clean .tc-ico { margin-right: 2px; }

.tp-actions { display: flex; gap: var(--sp-2); align-items: center; }
.btn.tiny.ghost.on { color: var(--c-primary-h, #4f8cff); border-color: color-mix(in srgb, var(--c-primary-h, #4f8cff) 50%, transparent); }
.btn.tiny.ghost.danger { color: var(--c-danger, #e5484d); }
.btn.tiny.ghost.danger:hover { background: color-mix(in srgb, var(--c-danger, #e5484d) 16%, transparent); color: var(--c-danger, #e5484d); border-color: color-mix(in srgb, var(--c-danger, #e5484d) 50%, transparent); }

.tp-logs {
  max-height: 200px;
  overflow-y: auto;
  border: 1px solid var(--c-line);
  border-radius: var(--r-sm);
  background: var(--c-surface-2);
  font-size: var(--fs-xs);
}
.tp-log {
  display: flex;
  gap: 6px;
  padding: 4px 8px;
  border-bottom: 1px solid var(--c-line);
  line-height: 1.4; align-items: baseline;
}
.tp-log:last-child { border-bottom: 0; }
.tp-log .lt { color: var(--c-text-3); flex: 0 0 auto; font-variant-numeric: tabular-nums; }
.tp-log .lc {
  flex: 0 0 auto; font-weight: 700; font-family: var(--font-mono, monospace);
  font-size: 11px; padding: 0 4px; border-radius: 4px;
  background: var(--c-surface-3); color: var(--c-text-2);
}
.tp-log .lm { color: var(--c-text-2); flex: 1; word-break: break-all; }
.tp-log.error .lm { color: var(--c-danger, #e5484d); }
.tp-log.warn .lm { color: var(--c-warning, #d9a23a); }

/* 刮削逐文件日志（持久化 DB） */
.tp-scrapelogs { margin-top: var(--sp-2); border-top: 1px dashed var(--c-line); padding-top: var(--sp-2); }
.ts-filters { display: flex; gap: 4px; align-items: center; margin-bottom: 6px; }
.ts-f {
  font-size: var(--fs-xs); padding: 1px 8px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--c-line-strong); background: var(--c-surface-2); color: var(--c-text-2);
}
.ts-f.on { color: #fff; background: var(--c-primary-h, #4f8cff); border-color: var(--c-primary-h, #4f8cff); }
.ts-meta { margin-left: auto; color: var(--c-text-3); font-size: var(--fs-xs); }
.tp-logs.slim { max-height: 240px; }
.tp-logs.slim .tp-log { cursor: default; }
.tp-logs.slim .tp-log[data-tip] { cursor: pointer; }
.ls-dot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto; align-self: center; }
.ls-dot.ok { background: var(--c-ok, #2e9e5b); }
.ls-dot.miss { background: var(--c-warning, #d9a23a); }
.ls-dot.error { background: var(--c-danger, #e5484d); }
.tp-log .ls-dot + .lc { margin-left: 2px; }

.tp-foot {
  padding: var(--sp-2) var(--sp-3); display: flex; gap: var(--sp-2);
  border-top: 1px solid var(--c-line); flex: 0 0 auto;
  background: var(--c-surface);
}

/* 过渡 */
.tp-slide-enter-active, .tp-slide-leave-active { transition: opacity .18s, max-height .18s ease; overflow: hidden; }
.tp-slide-enter-from, .tp-slide-leave-to { opacity: 0; max-height: 0; }

@keyframes tp-pulse {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--c-primary-h, #4f8cff) 60%, transparent); }
  70% { box-shadow: 0 0 0 6px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}
@keyframes tp-spin { to { transform: rotate(360deg); } }

/* 历史分区 */
.tp-section { overflow-y: auto; }
.tp-section.history {
  border-top: 1px solid var(--c-line-strong);
  background: var(--c-surface-2);
}
.tp-sec-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-xs); font-weight: 600; color: var(--c-text-3);
  position: sticky; top: 0; background: var(--c-surface-2); z-index: 1;
}
.tp-hist { border-bottom: 1px solid var(--c-line); }
.tp-hist:last-child { border-bottom: 0; }
.tp-hist-row {
  width: 100%; display: flex; align-items: center; gap: 8px;
  padding: var(--sp-2) var(--sp-3);
  background: none; border: 0; cursor: pointer; text-align: left;
  color: var(--c-text-2); font-size: var(--fs-sm);
}
.tp-hist-row:hover { background: var(--c-surface-3); }
.th-ico { flex: 0 0 auto; font-weight: 700; }
.th-ico.err { color: var(--c-danger, #e5484d); }
.th-ico.warn { color: var(--c-warning, #d9a23a); }
.th-label { font-weight: 500; }
.th-meta { margin-left: auto; font-size: var(--fs-xs); color: var(--c-text-3); }
.th-stat { font-size: var(--fs-xs); color: var(--c-text-3); white-space: nowrap; }
.th-caret { transition: transform .15s; color: var(--c-text-3); }
.th-caret.open { transform: rotate(180deg); }
.tp-hist-body { padding: 0 var(--sp-3) var(--sp-2); }

@media (max-width: 900px) {
  /* 任务中心在手机上改为视口级浮层（fixed），避免基于右上角按钮定位溢出屏幕 */
  .task-pop {
    position: fixed;
    width: auto;
    left: 12px; right: 12px;
    top: 60px;
    max-height: min(82vh, 680px);
  }
  /* 内部进度区在窄屏防止挤压 */
  .tp-current { word-break: break-word; }
  .tp-actions { flex-wrap: wrap; }
  .tp-foot { flex-wrap: wrap; }
}
@media (max-width: 480px) {
  .task-pop { left: 8px; right: 8px; top: 56px; max-height: 86vh; }
  .tp-head b { font-size: var(--fs-sm); }
}

</style>
