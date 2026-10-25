<script setup>
/**
 * AI 助手面板：对话流 + 结果卡。
 *
 * 与上一版（5 张折叠卡平铺）的区别：
 * - 只有一个输入框。用户说「这部讲什么」，面板自己决定调哪个能力；
 * - 结果是卡片，就地可确认/采用，不用跳页；
 * - 没配置模型时不消失：找片、口味、库体检照样能用（全部本机执行）。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { state } from '../state.js'
import { aiWorkspace as ws } from '../composables/aiWorkspace.js'
import { confirmDialog, toast } from '../utils.js'
import { t } from '../i18n/index.js'
import ResultPlot from './assistant/ResultPlot.vue'
import ResultTags from './assistant/ResultTags.vue'
import ResultSubs from './assistant/ResultSubs.vue'
import ResultAsk from './assistant/ResultAsk.vue'
import ResultSimilar from './assistant/ResultSimilar.vue'
import ResultInfo from './assistant/ResultInfo.vue'
import ResultLibrary from './assistant/ResultLibrary.vue'

const chat = ws.chat
const ai = ws.ai
const movie = computed(() => ws.movie)

/** 卡片类型 → 渲染组件；新增能力只要在这里加一行 */
const CARDS = {
  plot: ResultPlot,
  tags: ResultTags,
  subs: ResultSubs,
  ask: ResultAsk,
  similar: ResultSimilar,
  info: ResultInfo,
  library: ResultLibrary,
}

const tagCount = computed(() => ((movie.value && movie.value.tags) || []).length)

/**
 * 打字指示器只在「已经发出问题、还没摆出结果卡」时出现。
 * 卡片一旦上屏，进度就由卡片自己的 loading 文案表达，避免同一句提示出现两遍。
 */
const showTyping = computed(() => {
  const list = chat.messages
  return chat.busy && (!list.length || list[list.length - 1].role === 'user')
})

/* ---------------- 窗口几何：拖动 + 缩放 + 记忆 ---------------- */
const LS_KEY = 'avm.asst.geom'
const winEl = ref(null)
const streamEl = ref(null)
const inputEl = ref(null)
const narrow = ref(false)
const dragging = ref(false)
// 全屏模式：铺满整个视口。状态不持久化——每次打开都从正常大小开始，避免「上次全屏忘了退」
const maxed = ref(false)
const geom = reactive({ x: 0, y: 0, w: 460, h: 700 })

function toggleMax() {
  maxed.value = !maxed.value
  if (!maxed.value) nextTick(scrollToBottom)
}

function loadGeom() {
  try {
    const g = JSON.parse(localStorage.getItem(LS_KEY) || 'null')
    if (g && Number(g.w) >= 340 && Number(g.h) >= 420) {
      Object.assign(geom, { x: Number(g.x) || 0, y: Number(g.y) || 0, w: Number(g.w), h: Number(g.h) })
      return true
    }
  } catch (e) { /* 忽略损坏的本地值 */ }
  return false
}
function saveGeom() {
  if (narrow.value) return
  try { localStorage.setItem(LS_KEY, JSON.stringify({ x: geom.x, y: geom.y, w: geom.w, h: geom.h })) } catch (e) { /* 隐私模式写不了，忽略 */ }
}
/** 默认贴右上角，避开顶栏 */
function placeDefault() {
  const vw = window.innerWidth
  const vh = window.innerHeight
  geom.w = Math.min(460, Math.max(340, vw - 48))
  geom.h = Math.min(720, Math.max(420, vh - 120))
  geom.x = Math.max(12, vw - geom.w - 24)
  geom.y = Math.max(12, Math.min(72, vh - geom.h - 16))
}
function resetGeom() {
  placeDefault()
  saveGeom()
}

let drag = null
function onHeadDown(e) {
  if (narrow.value || maxed.value || e.button !== 0) return
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

const winStyle = computed(() => {
  if (maxed.value) return { left: '0px', top: '0px', width: '100vw', height: '100vh' }
  if (narrow.value) return {}
  return { left: geom.x + 'px', top: geom.y + 'px', width: geom.w + 'px', height: geom.h + 'px' }
})

/* ---------------- 交互 ---------------- */
function goAiCenter() {
  ws.openAiCenter()
}

/** 复制文本：剪贴板 API 不可用时（http 访问 / 旧浏览器）退回 execCommand */
function copyText(text) {
  const s = String(text || '')
  const fallback = () => {
    try {
      const ta = document.createElement('textarea')
      ta.value = s
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      toast(t('ai.chat.copied'), 'ok')
    } catch (e) {
      toast(t('ai.chat.copyFail'), 'err')
    }
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(s).then(
      () => toast(t('ai.chat.copied'), 'ok'),
      fallback,
    )
  } else {
    fallback()
  }
}

async function newSession() {
  if (!chat.messages.length) return
  const ok = await confirmDialog(t('ai.chat.clear'), t('ai.chat.clearConfirm'), { danger: false })
  if (!ok) return
  chat.reset()
}

/** 新消息 / 结果变化后贴到底部：对话里最怕「答完了但看不见」 */
function scrollToBottom() {
  nextTick(() => {
    const el = streamEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}
watch(() => chat.messages.length, scrollToBottom)
watch(() => chat.busy, scrollToBottom)
watch(() => chat.library.done, scrollToBottom)
// 流式期间内容在长：步骤多一行、结论多一个字都跟着滚，不然最新的进度在视口外
watch(() => chat.library.steps.length, scrollToBottom)
watch(() => chat.library.answer.length, scrollToBottom)
watch(() => ws.open, (v) => { if (v) scrollToBottom() })
watch(() => chat.focusRequest, () => {
  nextTick(() => { if (inputEl.value) inputEl.value.focus() })
})

/* ---------------- 生命周期 ---------------- */
let ro = null
let saveTimer = 0
let mq = null

function onMq(e) { narrow.value = e.matches }
function onKey(e) {
  if (e.key !== 'Escape' || !ws.open) return
  // 全屏时先退全屏，再按一次才关面板
  if (maxed.value) {
    maxed.value = false
    return
  }
  const el = document.activeElement
  // 正在输入时不打断，ESC 只负责失焦
  if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) {
    el.blur()
    return
  }
  ws.close()
}

onMounted(() => {
  mq = window.matchMedia('(max-width: 720px)')
  narrow.value = mq.matches
  if (mq.addEventListener) mq.addEventListener('change', onMq)
  if (!loadGeom()) placeDefault()
  // 用户拖动右下角改变尺寸后记住它（debounce 避免每帧写盘）
  if (window.ResizeObserver && winEl.value) {
    ro = new ResizeObserver(() => {
      // 全屏时不记录尺寸，否则退出全屏会带着整屏大小回来
      if (!ws.open || narrow.value || dragging.value || maxed.value) return
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
    <transition name="asst-fade">
      <button v-if="!ws.open && !state.currentId && state.aiFab" class="asst-fab" :title="$t('ai.title')"
              @click="ws.openWorkspace()">✨</button>
    </transition>
    <transition name="asst-fade">
      <section
        v-if="ws.open"
        ref="winEl"
        class="asst-win"
        :class="{ dragging, narrow, maxed }"
        :style="winStyle"
        role="dialog"
        aria-modal="false"
        :aria-label="$t('ai.title')"
      >
        <!-- 标题栏：拖动移动、双击复位；按钮区不参与拖动 -->
        <header class="asst-head" :title="$t('ai.winHint')" @pointerdown="onHeadDown" @dblclick="resetGeom">
          <span class="asst-head-ic">✨</span>
          <b class="asst-head-title">{{ $t('ai.title') }}</b>
          <span class="asst-ctx" :class="{ lib: !movie }" :title="movie ? (movie.code || movie.title) : $t('ai.chat.ctxLibrary')">
            {{ movie ? (movie.code || movie.title) : $t('ai.chat.ctxLibrary') }}
          </span>
          <span class="spacer"></span>
          <span v-if="ai.ready" class="badge ok ellipsis asst-model">{{ ai.model || $t('ai.modelFallback') }}</span>
          <span v-else-if="!ai.checking" class="badge warn">{{ $t('ai.notReady') }}</span>
          <button v-if="chat.messages.length" class="asst-icon" :title="$t('ai.chat.clear')" @click="newSession">⟲</button>
          <button class="asst-icon" :title="maxed ? $t('ai.chat.restore') : $t('ai.chat.max')"
                  @click="toggleMax">{{ maxed ? '⤡' : '⛶' }}</button>
          <button class="asst-icon" :title="$t('aiCenter.open')" @click="goAiCenter">⚙</button>
          <button class="asst-icon" :title="$t('common.close')" @click="ws.close()">✕</button>
        </header>

        <div ref="streamEl" class="asst-stream">
          <!-- 空态：不是菜单，是「可以问什么」 -->
          <div v-if="!chat.messages.length" class="asst-welcome">
            <p class="asst-welcome-title">{{ movie ? $t('ai.chat.welcomeTitle') : $t('ai.chat.welcomeLibTitle') }}</p>
            <p class="asst-hint">{{ movie ? $t('ai.chat.welcomeDesc') : $t('ai.chat.welcomeLibDesc') }}</p>
            <p v-if="movie" class="asst-facts">
              <span class="asst-fact" :class="{ on: !!movie.plot }">{{ $t('ai.statPlot') }}</span>
              <span class="asst-fact" :class="{ on: tagCount > 0 }">{{ $t('ai.statTags', { n: tagCount }) }}</span>
              <span class="asst-fact" :class="{ on: !!ai.hasSubtitles }">
                {{ ai.hasSubtitles ? $t('ai.chat.factSubs', { n: ai.subs.tracks.length }) : $t('ai.chat.factNoSubs') }}
              </span>
            </p>
            <p v-if="!ai.ready && !ai.checking" class="asst-note">{{ $t('ai.chat.notReadyChip') }}</p>
            <!-- 后端是旧进程时，前端所有新能力都会静默降级——必须明确说出来 -->
            <p v-else-if="!ai.agentOk" class="asst-note">{{ $t('ai.chat.backendOld') }}</p>
          </div>

          <template v-for="m in chat.messages" :key="m.id">
            <div v-if="m.role === 'user'" class="asst-row-user">
              <!-- 历史输入词可一键复制：找片条件、番号经常要复用 -->
              <button class="asst-copy" :title="$t('ai.chat.copy')"
                      @click="copyText(m.text)">⧉</button>
              <div class="asst-bubble">{{ m.text }}</div>
            </div>
            <div v-else class="asst-row-bot">
              <p v-if="m.say" class="asst-say">{{ m.say }}</p>
              <p v-if="m.archived && !m.cards.length" class="asst-archived">{{ $t('ai.chat.cardArchived') }}</p>
              <div v-for="c in m.cards" :key="c" class="asst-card-wrap">
                <component :is="CARDS[c]" v-if="CARDS[c]" />
                <div v-else-if="c === 'setup'" class="asst-setup">
                  <button class="btn tiny primary" @click="goAiCenter">{{ $t('ai.chat.goCenter') }}</button>
                  <span class="asst-hint">{{ $t('ai.chat.setupHint') }}</span>
                </div>
              </div>
            </div>
          </template>

          <div v-if="showTyping" class="asst-typing">
            <i></i><i></i><i></i>
            <span>{{ chat.busyText }}</span>
          </div>
        </div>

        <footer class="asst-composer">
          <div v-if="!chat.busy" class="asst-chips">
            <button v-for="c in chat.quickChips" :key="c.label" class="asst-chip" @click="chat.runChip(c)">
              {{ c.label }}
            </button>
          </div>
          <form class="asst-input-row" @submit.prevent="chat.send()">
            <input
              ref="inputEl"
              v-model="chat.input"
              class="asst-input"
              :placeholder="$t('ai.chat.ph')"
              :disabled="chat.busy"
            />
            <button class="btn tiny primary" type="submit" :disabled="chat.busy || !chat.input.trim()">
              {{ $t('ai.chat.send') }}
            </button>
          </form>
        </footer>
      </section>
    </transition>
  </Teleport>
</template>
