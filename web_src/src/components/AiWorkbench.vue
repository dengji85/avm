<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { state } from '../state.js'
import { aiWorkspace as ws } from '../composables/aiWorkspace.js'
import { aiLibraryQa, coverThumbUrl, getRecommend, listMovies, putConfig } from '../api.js'
import { fmtAgo, recommendReason } from '../utils.js'
import { t } from '../i18n/index.js'
import AiCard from './AiCard.vue'
import MovieCard from './MovieCard.vue'

const ai = ws.ai
const movie = computed(() => ws.movie)

/* ---------------- 库模式：不依赖具体影片，面向整个收藏 ----------------
 * 没打开详情页时助手不再是「请先选片」，而是全局找片 / 推荐：
 * - 自然语言找片：/ai/search-intent 把一句话解析成结构化条件 → 普通影片检索
 * - 今晚看什么：/movies/recommend 口味加权推荐（规则计算，不花 token） */
const find = reactive({
  q: '', busy: false, done: false, error: '', note: '', items: [], rows: [],
  answer: '', privacy: 'off', polishError: '',
})

const libPresets = computed(() => [
  { label: t('ai.libTonight'), kind: 'recommend' },
  { label: t('ai.libRecent'), kind: 'recent' },
])

async function runFind() {
  const q = find.q.trim()
  if (!q || find.busy) return
  find.busy = true; find.done = false; find.error = ''; find.note = ''
  find.items = []; find.rows = []; find.answer = ''; find.polishError = ''; find.privacy = 'off'
  try {
    // 问库：模型只负责把问题解析成查询意图，库数据全程在本机检索（不出机器）。
    // 若用户开启了「AI 解读」，本地执行的统计摘要才会发给模型润色（见 ai.insights）。
    const r = await aiLibraryQa(q)
    find.note = r.note || ''
    find.items = r.items || []
    find.rows = r.rows || []
    find.answer = r.answer || ''
    find.privacy = r.privacy || 'off'
    find.polishError = r.polish_error || ''
    find.done = true
  } catch (e) {
    find.error = e.message || t('ai.failed')
  } finally {
    find.busy = false
  }
}

/** 首次点「AI 解读」：说明会发送什么，确认后开启 ai.insights 并重跑本次问题 */
async function enablePolish() {
  const ok = await confirmDialog(t('ai.libPolish'), t('ai.libPolishConfirm'), { danger: false })
  if (!ok) return
  try {
    await putConfig({ ai: { insights: true } })
    toast(t('aiCenter.saved'), 'ok')
    await runFind()
  } catch (e) {
    toast(e.message, 'err')
  }
}

function noteText(n) {
  if (!n) return ''
  if (n === 'taste') return t('ai.libNote_taste')
  if (n === 'watch_stats') return t('ai.libNote_watch_stats')
  if (n === 'health') return t('ai.libNote_health')
  if (n === 'recommend') return t('ai.libTasteNote')
  return n
}

function rowVal(r) {
  if (r.value === 'ready') return t('aiCenter.extState.ready')
  if (r.value === 'missing') return t('aiCenter.extState.missing')
  return r.value
}

function rowLabel(r) {
  return r.i18n ? t(r.i18n) : r.key
}

async function runPreset(p) {
  if (find.busy) return
  find.busy = true; find.done = false; find.error = ''; find.note = ''
  find.items = []; find.rows = []; find.q = ''
  try {
    if (p.kind === 'recommend') {
      const r = await getRecommend({ limit: 8, exclude_watched: true })
      find.items = (r.items || []).map((m) => ({ ...m, __reason: recommendReason(m.reasons, t) }))
      find.note = 'recommend'
    } else {
      const r = await listMovies({ sort: 'added_desc', page_size: 12 })
      find.items = r.items || []
    }
    find.done = true
  } catch (e) {
    find.error = e.message || t('ai.failed')
  } finally {
    find.busy = false
  }
}

/* ---------------- 文案与派生 ---------------- */
/** 当前简介来源标签：抓取 / AI 字幕 / AI 元数据 / 人工 */
const plotSourceLabel = computed(() => {
  const s = ai.plotSource
  if (s === 'subtitle') return t('ai.srcAiSubtitle')
  if (s === 'dialog') return t('ai.srcAiDialog')
  if (s === 'meta') return t('ai.srcAiMeta')
  if (s === 'manual') return t('ai.srcManual')
  if (s === 'scraped') return t('ai.srcScraped')
  return ''
})
/** 收藏者最常问的几件事，点一下就能问，不用想措辞 */
const quickAsk = computed(() => [t('ai.askQuick1'), t('ai.askQuick2'), t('ai.askQuick3')])

const tagCount = computed(() => (movie.value?.tags || []).length)
const subsSummary = computed(() => (
  ai.hasSubtitles
    ? t('ai.subsTracks', { n: ai.subs.tracks.length || ai.aiSubs.length })
    : t('ai.subsNone')
))
const askSummary = computed(() => (ai.ask.history.length ? t('ai.askCount', { n: ai.ask.history.length }) : ''))
const simSummary = computed(() => (ai.similar.items.length ? t('ai.simCount', { n: ai.similar.items.length }) : ''))
const plotSummary = computed(() => (ai.plot.chars ? t('ai.plotBasis', { n: ai.plot.chars }) : ''))

/** 相似推荐的原因文案 */
function reasonText(r) {
  if (!r) return ''
  const names = (r.names || []).filter(Boolean).join(' / ')
  if (r.kind === 'actress') return names ? t('ai.whyActress', { names }) : t('ai.whyActressBare')
  if (r.kind === 'series') return t('ai.whySeries', { names })
  if (r.kind === 'studio') return t('ai.whyStudio', { names })
  if (r.kind === 'tag') return t('ai.whyTag', { names })
  return t('ai.whyPlot')
}
function fmtTraceTime(at) {
  if (!at) return ''
  try {
    return new Date(at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch (e) { return '' }
}

/* ---------------- 跳转 ---------------- */
/** 打开独立的 AI 中心（模型 / 提示词 / 任务 / 日志） */
function goAiCenter() {
  ws.close()
  // AI 中心是整页视图，详情抽屉会挡住它，先收起
  state.currentId = null
  state.view = 'ai'
}
function openSettings() {
  ws.close()
  state.currentId = null
  state.view = 'settings'
}
/** 提示词视图里编辑某一项（从调用详情跳过来时自动定位） */
function openPromptEdit(id) {
  ws.showPrompts(id)
}
/** 提示词卡片展开状态：默认展开，改过的乱长文本可以随时收起 */
const promptOpen = reactive({})
function isPromptOpen(id) {
  return promptOpen[id] !== false
}

/* ---------------- 窗口几何：拖动 + 缩放 + 记忆 ---------------- */
const LS_KEY = 'avm.aiw.geom'
const winEl = ref(null)
const narrow = ref(false)
const dragging = ref(false)
const geom = reactive({ x: 0, y: 0, w: 780, h: 720 })

function loadGeom() {
  try {
    const g = JSON.parse(localStorage.getItem(LS_KEY) || 'null')
    if (g && Number(g.w) > 320 && Number(g.h) > 240) {
      Object.assign(geom, { x: Number(g.x) || 0, y: Number(g.y) || 0, w: Number(g.w), h: Number(g.h) })
      return true
    }
  } catch (e) { /* 忽略损坏的本地值 */ }
  return false
}
function saveGeom() {
  if (narrow.value) return
  try { localStorage.setItem(LS_KEY, JSON.stringify({ x: geom.x, y: geom.y, w: geom.w, h: geom.h })) } catch (e) { /* 隐私模式下写不了，忽略 */ }
}
/** 默认贴在右上角，避开顶栏 */
function placeDefault() {
  const vw = window.innerWidth
  const vh = window.innerHeight
  geom.w = Math.min(780, Math.max(360, vw - 64))
  geom.h = Math.min(760, Math.max(320, vh - 140))
  geom.x = Math.max(12, vw - geom.w - 24)
  geom.y = Math.max(12, Math.min(76, vh - geom.h - 16))
}
function resetGeom() {
  placeDefault()
  saveGeom()
}

let drag = null
function onHeadDown(e) {
  if (narrow.value || e.button !== 0) return
  if (e.target.closest('button')) return
  drag = { dx: e.clientX - geom.x, dy: e.clientY - geom.y }
  dragging.value = true
  window.addEventListener('pointermove', onDragMove)
  window.addEventListener('pointerup', onDragEnd)
  e.preventDefault()
}
function onDragMove(e) {
  if (!drag) return
  const w = winEl.value ? winEl.value.offsetWidth : geom.w
  const h = winEl.value ? winEl.value.offsetHeight : geom.h
  geom.x = Math.min(Math.max(0, e.clientX - drag.dx), Math.max(0, window.innerWidth - w))
  geom.y = Math.min(Math.max(0, e.clientY - drag.dy), Math.max(0, window.innerHeight - Math.min(h, 80)))
}
function onDragEnd() {
  dragging.value = false
  drag = null
  window.removeEventListener('pointermove', onDragMove)
  window.removeEventListener('pointerup', onDragEnd)
  saveGeom()
}

const winStyle = computed(() => (
  narrow.value
    ? {}
    : { left: geom.x + 'px', top: geom.y + 'px', width: geom.w + 'px', height: geom.h + 'px' }
))

/* ---------------- 生命周期 ---------------- */
let ro = null
let saveTimer = 0
let mq = null

function onMq(e) { narrow.value = e.matches }
function onKey(e) {
  if (e.key !== 'Escape' || !ws.open) return
  const el = document.activeElement
  // 正在改提示词 / 输入问题时不打断，ESC 只负责失焦
  if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) {
    el.blur()
    return
  }
  ws.close()
}

onMounted(() => {
  narrow.value = window.matchMedia('(max-width: 720px)').matches
  mq = window.matchMedia('(max-width: 720px)')
  if (mq.addEventListener) mq.addEventListener('change', onMq)
  if (!loadGeom()) placeDefault()
  // 用户拖动右下角改变尺寸后记住它（debounce 避免每帧写盘）
  if (window.ResizeObserver && winEl.value) {
    ro = new ResizeObserver(() => {
      if (!ws.open || narrow.value || dragging.value) return
      const el = winEl.value
      if (!el) return
      geom.w = el.offsetWidth
      geom.h = el.offsetHeight
      clearTimeout(saveTimer)
      saveTimer = setTimeout(saveGeom, 400)
    })
    ro.observe(winEl.value)
  }
  window.addEventListener('keydown', onKey)
})

onBeforeUnmount(() => {
  if (mq && mq.removeEventListener) mq.removeEventListener('change', onMq)
  if (ro) ro.disconnect()
  clearTimeout(saveTimer)
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('pointermove', onDragMove)
  window.removeEventListener('pointerup', onDragEnd)
})
</script>

<template>
  <Teleport to="body">
    <!-- 全局入口：右下角悬浮球。详情页有自己的 AI 面板，浮窗开着时不需要它 -->
    <transition name="aiw-fade">
      <button v-if="!ws.open && !state.currentId && state.aiFab" class="aiw-fab" :title="$t('ai.title')"
              @click="ws.openWorkspace()">✨</button>
    </transition>
    <transition name="aiw-fade">
      <section
        v-if="ws.open"
        ref="winEl"
        class="aiw-win"
        :class="{ dragging, narrow }"
        :style="winStyle"
        role="dialog"
        aria-modal="false"
        :aria-label="$t('ai.title')"
      >
        <!-- 标题栏：拖动移动、双击复位；按钮区不参与拖动 -->
        <header class="aiw-win-head" :title="$t('ai.winHint')" @pointerdown="onHeadDown" @dblclick="resetGeom">
          <span class="aiw-win-ic">✨</span>
          <b class="aiw-win-title">{{ $t('ai.title') }}</b>
          <span v-if="ai.ready" class="badge ok">{{ ai.model || $t('ai.modelFallback') }}</span>
          <span v-else-if="!ai.checking" class="badge warn">{{ $t('ai.notReady') }}</span>
          <span v-if="movie" class="aiw-win-sub ellipsis">{{ movie.code || movie.title }}</span>
          <span class="spacer"></span>
          <div v-if="movie" class="aiw-seg">
            <button class="aiw-seg-btn" :class="{ on: ws.view === 'tasks' }" @click="ws.toggleView('tasks')">
              {{ $t('ai.viewTasks') }}
            </button>
            <button class="aiw-seg-btn" :class="{ on: ws.view === 'prompts' }" @click="ws.toggleView('prompts')">
              {{ $t('ai.viewPrompts') }}
            </button>
          </div>
          <button class="icon-btn" :title="$t('aiCenter.open')" @click="goAiCenter">⚙</button>
          <button class="icon-btn" :title="$t('common.close')" @click="ws.close()">✕</button>
        </header>

        <!-- 状态条：一眼看清哪些已经做过 -->
        <div v-if="movie && ai.ready && ws.view === 'tasks'" class="aiw-status">
          <span class="aiw-stat" :class="{ on: !!movie.plot }">{{ $t('ai.statPlot') }}</span>
          <span class="aiw-stat" :class="{ on: tagCount > 0 }">{{ $t('ai.statTags', { n: tagCount }) }}</span>
          <span class="aiw-stat" :class="{ on: !!ai.hasSubtitles }">{{ $t('ai.statSubs') }}</span>
          <span class="aiw-stat" :class="{ on: !!ai.hasDraft }">{{ $t('ai.statDraft') }}</span>
          <span class="spacer"></span>
          <span class="hint">{{ $t('ai.statusHint') }}</span>
        </div>

        <div class="aiw-win-body">
          <!-- 库模式：没有影片上下文时，助手面向整个收藏（找片 / 推荐），不再要求先开详情页 -->
          <div v-if="!movie && ai.checking" class="aiw-empty">
            <p class="hint">{{ $t('ai.working') }}</p>
          </div>
          <div v-else-if="!movie && !ai.ready" class="aiw-empty">
            <p>{{ $t('ai.notReadyHint') }}</p>
            <button class="btn primary" @click="openSettings">{{ $t('ai.goSettings') }}</button>
          </div>
          <div v-else-if="!movie" class="aiw-lib">
            <p class="aiw-lib-title">{{ $t('ai.libTitle') }}</p>
            <form class="aiw-find-row" @submit.prevent="runFind">
              <input v-model="find.q" :placeholder="$t('ai.libFindPh')" />
              <button class="btn tiny" :disabled="find.busy || !find.q.trim()">{{ $t('ai.libFind') }}</button>
            </form>
            <div class="aiw-chips">
              <button v-for="p in libPresets" :key="p.kind" class="aiw-chip"
                      :disabled="find.busy" @click="runPreset(p)">{{ p.label }}</button>
            </div>
            <p v-if="find.busy" class="hint">{{ $t('ai.working') }}</p>
            <p v-if="find.error" class="aiw-err">{{ find.error }}</p>
            <p v-if="find.note && !find.busy && !find.rows.length" class="hint">{{ noteText(find.note) }}</p>
            <div v-if="find.answer" class="aiw-answer">{{ find.answer }}</div>
            <div v-else-if="find.rows.length && find.privacy === 'off' && !find.busy" class="aiw-bar-ops">
              <button class="btn tiny ghost" @click="enablePolish">✨ {{ $t('ai.libPolish') }}</button>
              <span class="hint">{{ $t('ai.libPolishHint') }}</span>
            </div>
            <p v-if="find.polishError" class="aiw-err">{{ $t('ai.libPolishFail') }}{{ find.polishError }}</p>
            <div v-if="find.rows.length" class="aiw-rows">
              <div v-for="r in find.rows" :key="r.key" class="aiw-row-line">
                <span class="hint">{{ rowLabel(r) }}</span>
                <span class="spacer"></span>
                <span class="aiw-row-val">{{ rowVal(r) }}<template v-if="r.top"> · {{ r.top }}</template></span>
              </div>
            </div>
            <div v-if="find.items.length" class="aiw-grid">
              <MovieCard v-for="m in find.items" :key="m.id" :movie="m"
                         :selectable="false" :reason="m.__reason || ''" @open="ws.gotoMovie(m.id)" />
            </div>
            <p v-else-if="find.done && !find.items.length" class="hint">{{ $t('ai.libNoResult') }}</p>
          </div>

          <!-- 任务视图：每件事一张卡，确认后才写库 -->
          <div v-else-if="ws.view === 'tasks'" class="aiw-body">
            <!-- 1. 剧情简介 -->
            <AiCard
              v-model:open="ws.cards.plot"
              :title="$t('ai.plotTitle')"
              :badge="plotSourceLabel"
              :summary="plotSummary"
            >
              <p class="hint">{{ $t('ai.plotHint') }}</p>
              <p v-if="!ai.hasDraft" class="aiw-text">{{ movie.plot || $t('ai.plotEmpty') }}</p>
              <div v-else class="aiw-compare">
                <div class="aiw-col">
                  <div class="aiw-col-head">{{ $t('ai.oldVersion') }}</div>
                  <p class="aiw-text">{{ ai.plot.old || $t('ai.none') }}</p>
                </div>
                <div class="aiw-col new">
                  <div class="aiw-col-head">
                    {{ $t('ai.newVersion') }}
                    <span class="badge ok">{{ ai.plot.source === 'subtitle' ? $t('ai.srcSubtitle') : $t('ai.srcMeta') }}</span>
                  </div>
                  <p class="aiw-text">{{ ai.plot.draft }}</p>
                </div>
              </div>

              <p v-if="ai.plot.track" class="hint">{{ $t('ai.plotTrack', { track: ai.plot.track }) }}</p>
              <p v-if="ai.plot.degradedFrom" class="hint">
                {{ ai.plot.degradedFrom === 'subtitle' ? $t('ai.plotDegradedSubtitle') : $t('ai.plotDegradedDialog') }}
              </p>
              <p v-if="ai.plot.error" class="aiw-err">{{ ai.plot.error }}</p>

              <div class="aiw-actions">
                <button class="btn tiny primary" :disabled="ai.plot.loading" @click="ai.generatePlot">
                  {{ ai.plot.loading ? $t('ai.working') : (ai.hasDraft ? $t('ai.regenerate') : $t('ai.generate')) }}
                </button>
                <template v-if="ai.hasDraft">
                  <button class="btn tiny" @click="ai.applyPlot">{{ $t('ai.useNew') }}</button>
                  <button class="btn tiny ghost" @click="ai.discardPlot">{{ $t('ai.keepOld') }}</button>
                </template>
              </div>

              <!-- 语音对白提取入口在影片详情页（逐部提取，不做批量） -->

              <!-- 审查：这次到底发了什么、模型原样回了什么 -->
              <div class="aiw-trace">
                <button class="link-btn" @click="ai.toggleTrace('plot')">
                  {{ ai.traces.open === 'plot' ? $t('ai.traceHide') : $t('ai.traceShow') }}
                </button>
                <button class="link-btn" @click="openPromptEdit(ai.plot.source === 'subtitle' ? 'plot.subtitle' : 'synopsis')">
                  {{ $t('ai.editPrompt') }}
                </button>
                <span v-if="ai.traces.summary.plot" class="muted sm">
                  {{ ai.traces.summary.plot.model }} · {{ ai.traces.summary.plot.ms }} ms
                </span>
              </div>
              <div v-if="ai.traces.open === 'plot'" class="aiw-trace-box">
                <p v-if="ai.traces.loadingKey === 'plot'" class="hint">{{ $t('ai.traceLoading') }}</p>
                <p v-else-if="ai.traces.error" class="aiw-err">{{ ai.traces.error }}</p>
                <p v-else-if="!ai.traceItems('plot').length" class="hint">{{ $t('ai.traceEmpty') }}</p>
                <div v-for="tr in ai.traceItems('plot')" :key="tr.id" class="aiw-trace-item">
                  <div class="aiw-trace-meta">
                    <span class="badge">{{ tr.purpose }}</span>
                    <span>{{ tr.model }}</span>
                    <span>{{ tr.ms }} ms</span>
                    <span v-if="tr.cached" class="badge ok">{{ $t('ai.traceCached') }}</span>
                    <span class="badge">{{ $t('ai.traceOrigin.' + (tr.origin || 'internal')) }}</span>
                    <span class="muted sm">{{ fmtTraceTime(tr.at) }}</span>
                  </div>
                  <div class="hint">{{ $t('ai.traceSystem') }}</div>
                  <pre class="aiw-pre">{{ tr.messages[0] ? tr.messages[0].content : '' }}</pre>
                  <div class="hint">{{ $t('ai.traceUser') }}</div>
                  <pre class="aiw-pre">{{ tr.messages[1] ? tr.messages[1].content : '' }}</pre>
                  <div class="hint">{{ tr.error ? $t('ai.traceError') : $t('ai.traceReply') }}</div>
                  <pre class="aiw-pre" :class="{ err: tr.error }">{{ tr.error || tr.reply }}</pre>
                </div>
              </div>
            </AiCard>

            <!-- 2. 标签：逐条勾选；已存在的默认不勾，避免无意义写入 -->
            <AiCard
              v-model:open="ws.cards.tags"
              :title="$t('ai.tagsTitle')"
              :summary="$t('ai.tagsCurrent', { n: tagCount })"
            >
              <p class="hint">{{ $t('ai.tagsHint') }}</p>

              <div v-if="ai.tags.items.length" class="aiw-chips">
                <button
                  v-for="it in ai.tags.items"
                  :key="it.name"
                  class="chip aiw-chip"
                  :class="{ on: it.picked, existing: it.existing }"
                  @click="ai.toggleTag(it.name)"
                >
                  <span v-if="it.picked">✓ </span>{{ it.name }}
                  <em v-if="it.existing">{{ $t('ai.exists') }}</em>
                </button>
              </div>
              <p v-if="ai.tags.error" class="aiw-err">{{ ai.tags.error }}</p>

              <div class="aiw-actions">
                <button class="btn tiny primary" :disabled="ai.tags.loading" @click="ai.suggestTags">
                  {{ ai.tags.loading ? $t('ai.working') : $t('ai.tagsRun') }}
                </button>
                <template v-if="ai.tags.items.length">
                  <button class="btn tiny ghost" @click="ai.pickAllTags(true)">{{ $t('ai.selectAll') }}</button>
                  <button class="btn tiny ghost" @click="ai.pickAllTags(false)">{{ $t('ai.clearSel') }}</button>
                  <button class="btn tiny" :disabled="!ai.pickedTags.length" @click="ai.applyTags">
                    {{ $t('ai.tagsAdd', { n: ai.pickedTags.length }) }}
                  </button>
                </template>
              </div>

              <div class="aiw-trace">
                <button class="link-btn" @click="ai.toggleTrace('tags')">
                  {{ ai.traces.open === 'tags' ? $t('ai.traceHide') : $t('ai.traceShow') }}
                </button>
                <button class="link-btn" @click="openPromptEdit('tags')">{{ $t('ai.editPrompt') }}</button>
                <span v-if="ai.traces.summary.tags" class="muted sm">
                  {{ ai.traces.summary.tags.model }} · {{ ai.traces.summary.tags.ms }} ms
                </span>
              </div>
              <div v-if="ai.traces.open === 'tags'" class="aiw-trace-box">
                <p v-if="ai.traces.loadingKey === 'tags'" class="hint">{{ $t('ai.traceLoading') }}</p>
                <p v-else-if="ai.traces.error" class="aiw-err">{{ ai.traces.error }}</p>
                <p v-else-if="!ai.traceItems('tags').length" class="hint">{{ $t('ai.traceEmpty') }}</p>
                <div v-for="tr in ai.traceItems('tags')" :key="tr.id" class="aiw-trace-item">
                  <div class="aiw-trace-meta">
                    <span class="badge">{{ tr.purpose }}</span>
                    <span>{{ tr.model }}</span>
                    <span>{{ tr.ms }} ms</span>
                    <span v-if="tr.cached" class="badge ok">{{ $t('ai.traceCached') }}</span>
                    <span class="badge">{{ $t('ai.traceOrigin.' + (tr.origin || 'internal')) }}</span>
                    <span class="muted sm">{{ fmtTraceTime(tr.at) }}</span>
                  </div>
                  <div class="hint">{{ $t('ai.traceSystem') }}</div>
                  <pre class="aiw-pre">{{ tr.messages[0] ? tr.messages[0].content : '' }}</pre>
                  <div class="hint">{{ $t('ai.traceUser') }}</div>
                  <pre class="aiw-pre">{{ tr.messages[1] ? tr.messages[1].content : '' }}</pre>
                  <div class="hint">{{ tr.error ? $t('ai.traceError') : $t('ai.traceReply') }}</div>
                  <pre class="aiw-pre" :class="{ err: tr.error }">{{ tr.error || tr.reply }}</pre>
                </div>
              </div>
            </AiCard>

            <!-- 3. 字幕翻译：没有字幕就直接说明原因，而不是给个点了报错的按钮 -->
            <AiCard
              v-model:open="ws.cards.subs"
              :title="$t('ai.subsTitle')"
              :summary="subsSummary"
            >
              <div class="aiw-row">
                <select v-model="ai.subs.target" class="inp tiny">
                  <option value="zh-Hans">{{ $t('detail.aiTargetHans') }}</option>
                  <option value="zh-Hant">{{ $t('detail.aiTargetHant') }}</option>
                  <option value="en">{{ $t('detail.aiTargetEn') }}</option>
                </select>
                <label class="aiw-check">
                  <input v-model="ai.subs.bilingual" type="checkbox" />
                  <span>{{ $t('detail.aiBilingual') }}</span>
                </label>
                <button class="btn tiny primary" :disabled="ai.subs.loading || !ai.hasSubtitles" @click="ai.translateSubs">
                  {{ ai.subs.loading ? $t('ai.working') : $t('detail.aiTranslate') }}
                </button>
              </div>
              <p v-if="!ai.hasSubtitles && !ai.probing" class="hint">{{ $t('ai.subsNoSource') }}</p>
              <div v-if="ai.aiSubs.length" class="aiw-list">
                <div v-for="s in ai.aiSubs" :key="s.id" class="aiw-list-row">
                  <span class="mono">{{ s.lang || 'AI' }}</span>
                  <span class="ellipsis dim">{{ s.filename }}</span>
                  <button class="btn tiny ghost" @click="ai.downloadSub(s)">{{ $t('common.download') }}</button>
                  <button class="btn tiny ghost" @click="ai.removeAiSub(s)">{{ $t('common.delete') }}</button>
                </div>
              </div>
              <p v-if="ai.subs.error" class="aiw-err">{{ ai.subs.error }}</p>

              <div class="aiw-trace">
                <button class="link-btn" @click="ai.toggleTrace('subs')">
                  {{ ai.traces.open === 'subs' ? $t('ai.traceHide') : $t('ai.traceShow') }}
                </button>
                <button class="link-btn" @click="openPromptEdit('subtitle.translate')">{{ $t('ai.editPrompt') }}</button>
                <span v-if="ai.traces.summary.subs" class="muted sm">
                  {{ ai.traces.summary.subs.model }} · {{ ai.traces.summary.subs.ms }} ms
                </span>
              </div>
              <div v-if="ai.traces.open === 'subs'" class="aiw-trace-box">
                <p v-if="ai.traces.loadingKey === 'subs'" class="hint">{{ $t('ai.traceLoading') }}</p>
                <p v-else-if="ai.traces.error" class="aiw-err">{{ ai.traces.error }}</p>
                <p v-else-if="!ai.traceItems('subs').length" class="hint">{{ $t('ai.traceEmpty') }}</p>
                <div v-for="tr in ai.traceItems('subs')" :key="tr.id" class="aiw-trace-item">
                  <div class="aiw-trace-meta">
                    <span class="badge">{{ tr.purpose }}</span>
                    <span>{{ tr.model }}</span>
                    <span>{{ tr.ms }} ms</span>
                    <span v-if="tr.cached" class="badge ok">{{ $t('ai.traceCached') }}</span>
                    <span class="badge">{{ $t('ai.traceOrigin.' + (tr.origin || 'internal')) }}</span>
                    <span class="muted sm">{{ fmtTraceTime(tr.at) }}</span>
                  </div>
                  <div class="hint">{{ $t('ai.traceSystem') }}</div>
                  <pre class="aiw-pre">{{ tr.messages[0] ? tr.messages[0].content : '' }}</pre>
                  <div class="hint">{{ $t('ai.traceUser') }}</div>
                  <pre class="aiw-pre">{{ tr.messages[1] ? tr.messages[1].content : '' }}</pre>
                  <div class="hint">{{ tr.error ? $t('ai.traceError') : $t('ai.traceReply') }}</div>
                  <pre class="aiw-pre" :class="{ err: tr.error }">{{ tr.error || tr.reply }}</pre>
                </div>
              </div>
            </AiCard>

            <!-- 4. 剧情问答：时间戳可点击直接跳到片中的那一刻 -->
            <AiCard
              v-model:open="ws.cards.ask"
              :title="$t('ai.askTitle')"
              :summary="askSummary"
            >
              <template #head>
                <span v-if="ai.ask.track" class="muted sm">{{ ai.ask.track }}</span>
              </template>

              <p class="hint">{{ $t('ai.askHint') }}</p>

              <div class="aiw-row">
                <input v-model="ai.ask.question" class="inp" :placeholder="$t('ai.askPh')" @keyup.enter="ai.sendAsk" />
                <button class="btn tiny primary" :disabled="ai.ask.loading || !ai.ask.question.trim()" @click="ai.sendAsk">
                  {{ ai.ask.loading ? $t('ai.working') : $t('ai.askRun') }}
                </button>
              </div>
              <div class="aiw-chips">
                <button
                  v-for="q in quickAsk"
                  :key="q"
                  class="chip"
                  :disabled="!ai.hasSubtitles"
                  @click="ai.ask.question = q; ai.sendAsk()"
                >{{ q }}</button>
              </div>

              <p v-if="!ai.hasSubtitles && !ai.probing" class="hint">{{ $t('ai.askNoSubs') }}</p>
              <p v-if="ai.ask.error" class="aiw-err">{{ ai.ask.error }}</p>

              <div v-if="ai.ask.answer" class="aiw-answer">
                <p class="aiw-text">{{ ai.ask.answer }}</p>
                <div v-if="ai.ask.refs.length" class="aiw-refs">
                  <button v-for="(r, i) in ai.ask.refs" :key="i" class="aiw-ref" :title="r.text" @click="ws.seekToRef(r.time)">
                    [{{ r.time }}]
                  </button>
                  <span class="muted sm">{{ ai.ask.mode === 'digest' ? $t('ai.refHintDigest') : $t('ai.refHint') }}</span>
                </div>
              </div>

              <div v-if="ai.ask.history.length" class="aiw-history">
                <div class="hint">{{ $t('ai.askHistory') }}</div>
                <button
                  v-for="h in ai.ask.history"
                  :key="h.id"
                  class="aiw-hist-row"
                  :class="{ on: ai.ask.openId === h.id }"
                  @click="ai.toggleHistory(h)"
                >
                  <span class="ellipsis">{{ h.question }}</span>
                  <span class="muted sm">{{ fmtAgo(h.created_at) }}</span>
                </button>
              </div>

              <div class="aiw-trace">
                <button class="link-btn" @click="ai.toggleTrace('ask')">
                  {{ ai.traces.open === 'ask' ? $t('ai.traceHide') : $t('ai.traceShow') }}
                </button>
                <button class="link-btn" @click="openPromptEdit('plot.qa')">{{ $t('ai.editPrompt') }}</button>
                <span v-if="ai.traces.summary.ask" class="muted sm">
                  {{ ai.traces.summary.ask.model }} · {{ ai.traces.summary.ask.ms }} ms
                </span>
              </div>
              <div v-if="ai.traces.open === 'ask'" class="aiw-trace-box">
                <p v-if="ai.traces.loadingKey === 'ask'" class="hint">{{ $t('ai.traceLoading') }}</p>
                <p v-else-if="ai.traces.error" class="aiw-err">{{ ai.traces.error }}</p>
                <p v-else-if="!ai.traceItems('ask').length" class="hint">{{ $t('ai.traceEmpty') }}</p>
                <div v-for="tr in ai.traceItems('ask')" :key="tr.id" class="aiw-trace-item">
                  <div class="aiw-trace-meta">
                    <span class="badge">{{ tr.purpose }}</span>
                    <span>{{ tr.model }}</span>
                    <span>{{ tr.ms }} ms</span>
                    <span v-if="tr.cached" class="badge ok">{{ $t('ai.traceCached') }}</span>
                    <span class="badge">{{ $t('ai.traceOrigin.' + (tr.origin || 'internal')) }}</span>
                    <span class="muted sm">{{ fmtTraceTime(tr.at) }}</span>
                  </div>
                  <div class="hint">{{ $t('ai.traceSystem') }}</div>
                  <pre class="aiw-pre">{{ tr.messages[0] ? tr.messages[0].content : '' }}</pre>
                  <div class="hint">{{ $t('ai.traceUser') }}</div>
                  <pre class="aiw-pre">{{ tr.messages[1] ? tr.messages[1].content : '' }}</pre>
                  <div class="hint">{{ tr.error ? $t('ai.traceError') : $t('ai.traceReply') }}</div>
                  <pre class="aiw-pre" :class="{ err: tr.error }">{{ tr.error || tr.reply }}</pre>
                </div>
              </div>
            </AiCard>

            <!-- 5. 相似推荐：带「为什么相似」，可解释才敢信 -->
            <AiCard
              v-model:open="ws.cards.similar"
              :title="$t('ai.simTitle')"
              :summary="simSummary"
            >
              <template #head>
                <button class="btn tiny ghost" :disabled="ai.similar.loading" @click="ai.loadSimilar">
                  {{ $t('ai.simRefresh') }}
                </button>
              </template>

              <div v-if="ai.similar.items.length" class="aiw-sim">
                <div v-for="s in ai.similar.items" :key="s.id" class="aiw-sim-card">
                  <button class="aiw-sim-open" @click="ws.gotoMovie(s.id)">
                    <img v-if="s.cover" :src="coverThumbUrl(s.id)" alt="" loading="lazy" />
                    <span class="ellipsis">{{ s.code || s.title }}</span>
                  </button>
                  <div v-if="(s.reasons || []).length" class="aiw-reasons">
                    <span v-for="(r, i) in s.reasons" :key="i" class="aiw-reason">{{ reasonText(r) }}</span>
                  </div>
                </div>
              </div>
              <p v-else-if="ai.similar.loading" class="hint">{{ $t('ai.simLoading') }}</p>
              <p v-else-if="!ai.similar.error" class="hint">{{ $t('ai.simEmpty') }}</p>
              <p v-if="ai.similar.error" class="aiw-err">{{ ai.similar.error }}</p>
            </AiCard>
          </div>

          <!-- 提示词视图：保存后立即生效，可随时恢复默认 -->
          <div v-else class="aiw-body">
            <p class="hint">{{ $t('ai.promptsHint') }}</p>
            <p v-if="ai.prompts.loading" class="hint">{{ $t('ai.working') }}</p>
            <p v-if="ai.prompts.error" class="aiw-err">{{ ai.prompts.error }}</p>

            <AiCard
              v-for="p in ai.prompts.items"
              :key="p.id"
              :title="p.title"
              :badge="p.overridden ? $t('ai.customized') : $t('ai.builtin')"
              :open="isPromptOpen(p.id)"
              @update:open="(v) => (promptOpen[p.id] = v)"
            >
              <template #head>
                <span class="muted sm">{{ p.id }} · {{ p.version || '-' }}</span>
              </template>

              <p class="hint">{{ p.desc }}</p>
              <p class="aiw-vars">{{ $t('ai.promptVars') }}：{{ p.variables.length ? p.variables.join(' / ') : $t('ai.none') }}</p>

              <template v-if="ai.prompts.editingId === p.id">
                <textarea v-model="ai.prompts.draft" class="inp aiw-ta" rows="8" spellcheck="false"></textarea>
                <div class="aiw-actions">
                  <button class="btn tiny primary" :disabled="ai.prompts.saving" @click="ai.savePrompt">
                    {{ ai.prompts.saving ? $t('ai.working') : $t('common.save') }}
                  </button>
                  <button class="btn tiny ghost" @click="ai.cancelPrompt">{{ $t('common.cancel') }}</button>
                  <button class="btn tiny ghost" :disabled="!p.overridden" @click="ai.restorePrompt(p.id)">
                    {{ $t('ai.restoreDefault') }}
                  </button>
                </div>
              </template>
              <template v-else>
                <p class="aiw-text">{{ p.system }}</p>
                <div class="aiw-actions">
                  <button class="btn tiny" @click="ai.startEditPrompt(p)">{{ $t('ai.editPrompt') }}</button>
                  <button class="btn tiny ghost" :disabled="!p.overridden" @click="ai.restorePrompt(p.id)">
                    {{ $t('ai.restoreDefault') }}
                  </button>
                </div>
              </template>
            </AiCard>
          </div>
        </div>
      </section>
    </transition>
  </Teleport>
</template>

<style scoped>
/* ===== 全局入口悬浮球：右下角常驻，浮窗打开时隐藏 ===== */
.aiw-fab {
  position: fixed; right: 20px; bottom: 20px;
  z-index: calc(var(--z-ai) - 1);
  width: 48px; height: 48px; border-radius: 999px;
  border: 1px solid var(--c-primary-line, transparent);
  background: linear-gradient(135deg, var(--c-primary-d, var(--c-primary)), var(--c-primary));
  color: #fff; font-size: 20px; line-height: 1;
  box-shadow: var(--sh-4);
  cursor: pointer;
  transition: transform .15s, box-shadow .15s;
}
.aiw-fab:hover { transform: translateY(-2px) scale(1.04); box-shadow: var(--sh-4); }

/* ===== 库模式 ===== */
.aiw-lib { display: flex; flex-direction: column; gap: 10px; }
.aiw-lib-title { margin: 0; font-weight: 600; color: var(--c-text); }
.aiw-find-row { display: flex; gap: 8px; }
.aiw-find-row input { flex: 1; min-width: 0; }
.aiw-chips { display: flex; gap: 8px; flex-wrap: wrap; }
.aiw-chip {
  padding: 3px 10px; border-radius: 999px; font-size: 12px;
  background: var(--c-surface-2); border: 1px solid var(--c-line);
  color: var(--c-text-2); cursor: pointer;
}
.aiw-chip:hover { border-color: var(--c-primary); color: var(--c-primary-h); }
.aiw-rows { display: flex; flex-direction: column; }
.aiw-row-line {
  display: flex; align-items: baseline; gap: 8px;
  padding: 5px 0; border-bottom: 1px dashed var(--c-line);
}
.aiw-row-line:last-child { border-bottom: 0; }
.aiw-row-line .hint { margin: 0; }
.aiw-row-val { font-weight: 600; color: var(--c-text); }
.aiw-answer {
  padding: 10px 12px; border-radius: 10px;
  background: var(--c-primary-soft); color: var(--c-text);
  font-size: 13px; line-height: 1.7; white-space: pre-wrap;
}
.aiw-grid {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px; margin-top: 4px;
}
@media (max-width: 480px) { .aiw-grid { grid-template-columns: 1fr; } }

/* ===== 浮窗本体：最高层，盖住详情抽屉与全屏播放层 ===== */
.aiw-win {
  position: fixed;
  z-index: var(--z-ai);
  display: flex; flex-direction: column;
  min-width: 360px; min-height: 240px;
  max-width: calc(100vw - 16px); max-height: calc(100vh - 16px);
  background: var(--c-surface);
  border: 1px solid var(--c-line-strong);
  border-radius: var(--r-lg);
  box-shadow: 0 24px 60px rgba(0, 0, 0, .5);
  overflow: hidden;
  /* 右下角可拖拽改变尺寸（原生 resize 手柄） */
  resize: both;
}
.aiw-win.narrow { resize: none; }
.aiw-win.dragging { user-select: none; }
@media (max-width: 720px) {
  .aiw-win {
    left: 0 !important; top: 0 !important;
    width: 100% !important; height: 100% !important;
    max-width: none; max-height: none;
    border-radius: 0; border: 0;
  }
}

.aiw-win-head {
  flex: none;
  display: flex; align-items: center; gap: 8px;
  padding: 9px 10px 9px 12px;
  border-bottom: 1px solid var(--c-line);
  background: var(--c-surface-2);
  cursor: grab;
  touch-action: none;
}
.aiw-win.dragging .aiw-win-head { cursor: grabbing; }
@media (max-width: 720px) { .aiw-win-head { cursor: default; } }
.aiw-win-ic { font-size: 14px; }
.aiw-win-title { font-size: var(--fs-md); }
.aiw-win-sub { color: var(--c-text-3); font-size: var(--fs-sm); max-width: 34%; }
.aiw-seg {
  display: flex; gap: 2px; padding: 2px;
  border: 1px solid var(--c-line); border-radius: 999px; background: var(--c-surface);
}
.aiw-seg-btn {
  border: 0; background: transparent; color: var(--c-text-3);
  font: inherit; font-size: var(--fs-xs); cursor: pointer;
  padding: 2px 10px; border-radius: 999px;
}
.aiw-seg-btn.on { background: var(--c-primary-soft); color: var(--c-primary-text); }

.aiw-status {
  flex: none;
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 8px 12px; border-bottom: 1px solid var(--c-line);
}
.aiw-stat {
  font-size: var(--fs-xs); padding: 2px 8px; border-radius: 999px;
  background: var(--c-surface-2); color: var(--c-text-3);
  border: 1px solid var(--c-line);
}
.aiw-stat.on { color: #2ea043; border-color: currentColor; }

.aiw-win-body {
  flex: 1; min-height: 0; overflow-y: auto;
  padding: 10px 12px 14px;
}
.aiw-body { display: flex; flex-direction: column; gap: 12px; }
.aiw-empty {
  padding: 28px 18px; display: flex; flex-direction: column;
  align-items: center; gap: 14px; text-align: center;
}
.aiw-empty p { margin: 0; color: var(--c-text-2); font-size: var(--fs-sm); line-height: 1.7; }

/* ===== 卡片内部元素 ===== */
.aiw-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.aiw-row .inp { flex: 1; min-width: 150px; }
.aiw-check { display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-sm); color: var(--c-text-2); }
.aiw-text {
  margin: 0; font-size: var(--fs-sm); line-height: 1.75; color: var(--c-text-2);
  white-space: pre-wrap; max-height: 260px; overflow: auto;
}
.aiw-compare { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
@media (max-width: 640px) { .aiw-compare { grid-template-columns: 1fr; } }
.aiw-col {
  min-width: 0; padding: 8px; border-radius: var(--r-sm);
  border: 1px solid var(--c-line); background: var(--c-surface);
}
.aiw-col.new { border-color: var(--c-primary); }
.aiw-col-head {
  display: flex; align-items: center; gap: 6px;
  margin-bottom: 4px; font-size: var(--fs-xs); color: var(--c-text-3);
}
.aiw-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.aiw-vars { margin: 0; font-size: var(--fs-xs); color: var(--c-text-3); }
.aiw-ta {
  width: 100%; resize: vertical; font-size: 12px; line-height: 1.6;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
/* 调用详情：提示词 / 原始返回，等宽可滚动，方便逐字核对 */
.aiw-trace { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.aiw-trace-box {
  display: flex; flex-direction: column; gap: 6px;
  border-top: 1px dashed var(--c-line); padding-top: 8px;
}
.aiw-trace-item {
  display: flex; flex-direction: column; gap: 4px;
  padding: 8px; border-radius: var(--r-sm);
  border: 1px solid var(--c-line); background: var(--c-surface);
}
.aiw-trace-meta {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: var(--fs-xs); color: var(--c-text-2);
}
.aiw-pre {
  margin: 0; padding: 6px 8px; border-radius: var(--r-sm);
  max-height: 180px; overflow: auto;
  background: var(--c-surface-2); color: var(--c-text-2);
  font-size: 11px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.aiw-pre.err { color: var(--c-err); }
.aiw-err { margin: 0; font-size: var(--fs-sm); color: var(--c-err); }
.aiw-chips { display: flex; gap: 6px; flex-wrap: wrap; }
.aiw-chip { display: inline-flex; align-items: center; gap: 4px; }
.aiw-chip em { font-style: normal; font-size: 10px; opacity: .6; }
.aiw-chip.existing { opacity: .7; }
.aiw-list { display: flex; flex-direction: column; gap: 4px; }
.aiw-list-row { display: flex; align-items: center; gap: 8px; font-size: var(--fs-sm); }
.aiw-list-row .ellipsis { flex: 1; min-width: 0; }
.aiw-answer {
  display: flex; flex-direction: column; gap: 6px;
  border-top: 1px dashed var(--c-line); padding-top: 8px;
}
.aiw-refs { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.aiw-ref {
  padding: 2px 7px; border-radius: 999px; cursor: pointer;
  font-size: 11px; font-variant-numeric: tabular-nums;
  border: 1px solid var(--c-line); background: var(--c-surface); color: var(--c-primary);
}
.aiw-ref:hover { border-color: var(--c-primary); }
.aiw-history {
  display: flex; flex-direction: column; gap: 4px;
  border-top: 1px dashed var(--c-line); padding-top: 8px;
}
.aiw-hist-row {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 5px 6px; border-radius: var(--r-sm); cursor: pointer; text-align: left;
  font-size: var(--fs-sm); color: var(--c-text-2);
  background: transparent; border: 1px solid transparent;
}
.aiw-hist-row:hover { background: var(--c-surface); }
.aiw-hist-row.on { border-color: var(--c-primary); }
.aiw-sim { display: grid; grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 8px; }
.aiw-sim-card { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.aiw-sim-open {
  display: flex; flex-direction: column; gap: 4px; padding: 4px; cursor: pointer;
  border: 1px solid var(--c-line); border-radius: var(--r-md);
  background: var(--c-surface); color: var(--c-text-2);
  font-size: 11px; text-align: left;
}
.aiw-sim-open:hover { border-color: var(--c-primary); }
.aiw-sim-open img { width: 100%; aspect-ratio: 3/4; object-fit: cover; border-radius: var(--r-sm); }
.aiw-reasons { display: flex; gap: 3px; flex-wrap: wrap; }
.aiw-reason {
  padding: 1px 5px; border-radius: 999px; font-size: 10px;
  background: var(--c-surface-2); border: 1px solid var(--c-line); color: var(--c-text-3);
}

.aiw-fade-enter-active, .aiw-fade-leave-active { transition: opacity .16s ease, transform .16s ease; }
.aiw-fade-enter-from, .aiw-fade-leave-to { opacity: 0; transform: translateY(8px) scale(.985); }
</style>
