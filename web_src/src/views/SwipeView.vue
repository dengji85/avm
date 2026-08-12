<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { state } from '../state.js'
import { listMovies, updateMovie, coverUrl, coverThumbUrl } from '../api.js'
import { toast, coverFallback, fmtMin, qualityTag } from '../utils.js'

const deck = ref([])
const idx = ref(0)
const loading = ref(false)
const started = ref(false)
const mode = ref('unrated')
const stats = ref({ rated: 0, skipped: 0, faved: 0 })

/* 拖拽 */
const dragX = ref(0)
const dragging = ref(false)
let startX = 0

const MODES = [
  ['unrated', 'swipe.unrated', { sort: 'added_desc' }],
  ['unwatched', 'swipe.unwatched', { flags: 'unwatched' }],
  ['random', 'swipe.random', { sort: 'random' }],
  ['favorite', 'swipe.favorite', { flags: 'favorite' }],
]

const cur = computed(() => deck.value[idx.value] || null)
const next = computed(() => deck.value[idx.value + 1] || null)
const done = computed(() => started.value && idx.value >= deck.value.length)
const progress = computed(() => (deck.value.length ? Math.round((idx.value / deck.value.length) * 100) : 0))

const hint = computed(() => {
  if (dragX.value > 60) return { text: 'swipe.want', cls: 'like' }
  if (dragX.value < -60) return { text: 'swipe.skip', cls: 'nope' }
  return null
})

async function start() {
  loading.value = true
  started.value = true
  idx.value = 0
  stats.value = { rated: 0, skipped: 0, faved: 0 }
  try {
    const m = MODES.find((x) => x[0] === mode.value)
    const r = await listMovies(Object.assign({ page: 1, page_size: 100 }, m ? m[2] : {}))
    deck.value = r.items || []
    if (!deck.value.length) toast(t('swipe.noMatch'), 'err')
  } catch (e) { toast(e.message, 'err'); deck.value = [] } finally { loading.value = false }
}

function advance() {
  dragX.value = 0
  idx.value++
}

async function act(kind) {
  const m = cur.value
  if (!m) return
  try {
    if (kind === 'want') { await updateMovie(m.id, { watchlist: 1 }); stats.value.rated++ }
    else if (kind === 'fav') { await updateMovie(m.id, { favorite: 1 }); stats.value.faved++ }
    else stats.value.skipped++
  } catch (e) { toast(e.message, 'err') }
  advance()
}

async function rate(v) {
  const m = cur.value
  if (!m) return
  try { await updateMovie(m.id, { rating: v }); stats.value.rated++ }
  catch (e) { toast(e.message, 'err') }
  advance()
}

function undo() {
  if (idx.value > 0) { idx.value--; dragX.value = 0 }
}

/* 指针拖拽 */
function onDown(e) {
  if (!cur.value) return
  dragging.value = true
  startX = e.clientX
  e.target.setPointerCapture && e.target.setPointerCapture(e.pointerId)
}
function onMove(e) {
  if (!dragging.value) return
  dragX.value = e.clientX - startX
}
function onUp() {
  if (!dragging.value) return
  dragging.value = false
  if (dragX.value > 110) act('want')
  else if (dragX.value < -110) act('skip')
  else dragX.value = 0
}

function onKey(e) {
  if (!started.value || !cur.value) return
  if (e.key === 'ArrowLeft') act('skip')
  else if (e.key === 'ArrowRight') act('want')
  else if (e.key.toLowerCase() === 'f') act('fav')
  else if (e.key.toLowerCase() === 'z') undo()
  else if (e.key >= '1' && e.key <= '5') rate(Number(e.key))
}

onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

const cardStyle = computed(() => ({
  transform: `translateX(${dragX.value}px) rotate(${dragX.value / 26}deg)`,
  transition: dragging.value ? 'none' : 'transform .28s cubic-bezier(.16,1,.3,1)',
}))

function sub(m) {
  return [m.code, m.studio, m.year].filter(Boolean).join(' · ')
}
function openDetail() { if (cur.value) state.currentId = cur.value.id }
</script>

<template>
  <section class="view">
    <div class="toolbar">
      <h1 class="tb-title">{{ $t('view.swipe') }}</h1>
      <span class="tb-sub" v-if="started && deck.length">{{ idx }} / {{ deck.length }}</span>
      <div class="spacer"></div>
      <select class="sel" v-model="mode">
        <option v-for="[v, t] in MODES" :key="v" :value="v">{{ $t(t) }}</option>
      </select>
      <button class="btn tiny primary" :disabled="loading" @click="start">
        {{ started ? $t('swipe.restart') : $t('swipe.start') }}
      </button>
    </div>

    <div v-if="started && deck.length" class="progress sw-progress"><i :style="{ width: progress + '%' }"></i></div>

    <div class="view-body sw-body">
      <!-- 未开始 -->
      <div v-if="!started" class="empty">
        <div class="icon">⇄</div>
        <div class="title">{{ $t('swipe.introTitle') }}</div>
        <div class="desc">
          {{ $t('swipe.kbdHint') }}
        </div>
        <button class="btn primary large" @click="start">{{ $t('swipe.start') }}</button>
      </div>

      <div v-else-if="loading" class="empty"><span class="spinner large"></span></div>

      <!-- 完成 -->
      <div v-else-if="done" class="empty">
        <div class="icon">✓</div>
        <div class="title">{{ $t('swipe.doneTitle') }}</div>
        <div class="desc">
          {{ $t('swipe.stats', stats) }}
        </div>
        <button class="btn primary" @click="start">{{ $t('swipe.again') }}</button>
      </div>

      <!-- 卡片堆 -->
      <div v-else-if="cur" class="stage">
        <div v-if="next" class="sw-card behind">
          <img :src="coverThumbUrl(next.id, 400)" alt="" @error="coverFallback" />
        </div>

        <div
          class="sw-card front"
          :style="cardStyle"
          @pointerdown="onDown"
          @pointermove="onMove"
          @pointerup="onUp"
          @pointercancel="onUp"
        >
          <img :src="coverUrl(cur.id)" alt="" draggable="false" @error="coverFallback" @dblclick="openDetail" />

          <div v-if="hint" class="sw-hint" :class="hint.cls">{{ $t(hint.text) }}</div>

          <div class="sw-info">
            <div class="sw-title clamp-2">{{ cur.title || cur.code }}</div>
            <div class="sw-sub">
              {{ sub(cur) }}
              <span v-if="cur.duration_minutes"> · {{ fmtMin(cur.duration_minutes) }}</span>
              <span v-if="qualityTag(cur.resolution)"> · {{ qualityTag(cur.resolution) }}</span>
            </div>
          </div>
        </div>

        <!-- 操作 -->
        <div class="sw-acts">
          <button class="rb skip" @click="act('skip')" :data-tip="$t('swipe.skip') + ' (←)'">✕</button>
          <button class="rb undo" :disabled="idx === 0" @click="undo" :data-tip="$t('swipe.undo') + ' (Z)'">↺</button>
          <div class="star-rate">
            <button v-for="v in 5" :key="v" class="sr" @click="rate(v)" :data-tip="`${v} ★`">★</button>
          </div>
          <button class="rb fav" @click="act('fav')" :data-tip="$t('swipe.fav') + ' (F)'">♥</button>
          <button class="rb want" @click="act('want')" :data-tip="$t('swipe.want') + ' (→)'">✓</button>
        </div>

        <p class="sw-tip muted">{{ $t('swipe.dragHint') }}</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.sel { width: auto; min-width: 110px; height: 28px; font-size: var(--fs-sm); }
.sw-progress { flex: none; border-radius: 0; height: 3px; }
.sw-body { align-items: center; justify-content: center; }

.stage {
  position: relative;
  display: flex; flex-direction: column; align-items: center;
  gap: var(--sp-4);
  width: 100%;
  padding-top: var(--sp-2);
}

.sw-card {
  position: relative;
  width: min(330px, 78vw);
  border-radius: var(--r-lg);
  overflow: hidden;
  background: var(--c-surface);
  box-shadow: var(--sh-4);
  user-select: none;
}
.sw-card img {
  width: 100%; aspect-ratio: 2/3;
  object-fit: cover;
  background: var(--c-surface-2);
  pointer-events: auto;
}
.sw-card.front { cursor: grab; touch-action: none; z-index: 2; }
.sw-card.front:active { cursor: grabbing; }
.sw-card.behind {
  position: absolute; top: 0; left: 50%;
  transform: translateX(-50%) scale(.94) translateY(14px);
  opacity: .45;
  z-index: 1;
  pointer-events: none;
}

.sw-info {
  position: absolute; left: 0; right: 0; bottom: 0;
  padding: var(--sp-6) var(--sp-3) var(--sp-3);
  background: linear-gradient(to top, rgba(4,6,10,.94), transparent);
  color: #fff;
}
.sw-title { font-size: var(--fs-lg); font-weight: 600; line-height: 1.4; }
.sw-sub { font-size: var(--fs-sm); opacity: .78; margin-top: 3px; }

.sw-hint {
  position: absolute; top: var(--sp-5);
  padding: var(--sp-1) var(--sp-4);
  border-radius: var(--r-sm);
  border: 3px solid;
  font-size: var(--fs-xl); font-weight: 800;
  letter-spacing: .06em;
  z-index: 3;
}
.sw-hint.like { right: var(--sp-4); color: var(--c-ok); border-color: var(--c-ok); transform: rotate(12deg); }
.sw-hint.nope { left: var(--sp-4); color: var(--c-err); border-color: var(--c-err); transform: rotate(-12deg); }

.sw-acts { display: flex; align-items: center; gap: var(--sp-3); }
.rb {
  width: 46px; height: 46px;
  display: grid; place-items: center;
  border-radius: 50%;
  border: 1px solid var(--c-line-strong);
  background: var(--c-surface);
  color: var(--c-text-2);
  font-size: 17px;
  box-shadow: var(--sh-1);
  transition: all var(--t-fast);
}
.rb:hover:not(:disabled) { transform: scale(1.1); box-shadow: var(--sh-2); }
.rb:disabled { opacity: .35; }
.rb.skip:hover { color: var(--c-err); border-color: var(--c-err); }
.rb.want:hover { color: var(--c-ok); border-color: var(--c-ok); }
.rb.fav:hover  { color: var(--c-primary); border-color: var(--c-primary); }
.rb.undo { width: 36px; height: 36px; font-size: 14px; }

.star-rate { display: flex; gap: 2px; }
.sr {
  width: 26px; height: 34px;
  color: var(--c-text-3); font-size: 17px;
  transition: color var(--t-fast), transform var(--t-fast);
}
.star-rate:hover .sr { color: var(--c-gold); }
.star-rate .sr:hover ~ .sr { color: var(--c-text-3); }
.sr:hover { transform: scale(1.2); }

.sw-tip { font-size: var(--fs-xs); }
kbd {
  padding: 1px 5px;
  border-radius: var(--r-xs);
  border: 1px solid var(--c-line-strong);
  background: var(--c-surface-2);
  font-size: 10px;
}
</style>
