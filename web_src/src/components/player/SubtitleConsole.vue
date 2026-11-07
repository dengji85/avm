<script setup>
import { ref, computed, toRef, watch, onMounted } from 'vue'
import { suggestSubtitleSync, subtitleCueList } from '../../api.js'
import { t } from '../../i18n/index.js'
import { useOnlineSubtitles } from '../../composables/useOnlineSubtitles.js'

/* 面板由 v-if 控制，收起时组件实例会被销毁、下次打开重建。把字幕列表的
   展开状态放到模块作用域并落到 localStorage，避免「拖一下进度条回来又得
   重新展开列表」。候选字幕只留在内存里，不写盘。
   tab 记忆由外层 PlayerSettings 的统一标签管理，这里不再自存。 */
const PANEL_KEY = 'avm.player.subPanel'
let panelSession = null
function getPanel() {
  if (panelSession) return panelSession
  let saved = {}
  try { saved = JSON.parse(localStorage.getItem(PANEL_KEY) || '{}') } catch (e) { /* 忽略 */ }
  panelSession = {
    cueOpen: !!saved.cueOpen,
    cueQuery: String(saved.cueQuery || ''),
    cueItems: [],
    cueTotal: 0,
    cueOffset: 0,
  }
  return panelSession
}
function savePanel(s) {
  try {
    localStorage.setItem(PANEL_KEY, JSON.stringify({
      cueOpen: s.cueOpen, cueQuery: s.cueQuery,
    }))
  } catch (e) { /* 隐私模式忽略 */ }
}
const panel = getPanel()

const props = defineProps({
  movieId: { type: Number, required: true },
  tracks: { type: Array, default: () => [] },
  activeSubId: { type: String, default: '' },
  subStyle: { type: Object, default: () => ({ size: 'm', bg: '1', posY: 0 }) },
  syncCurrent: { type: Object, default: () => ({ offset_ms: 0, scale: 1, pos_y: 0 }) },
  syncDirty: { type: Boolean, default: false },
  syncState: { type: String, default: 'unchecked' },
  currentTime: { type: Number, default: 0 },
  duration: { type: Number, default: 0 },
  cueRange: { type: Object, default: null },
  tab: { type: String, default: 'tracks' },   // 'tracks' | 'sync' | 'display'，由 PlayerSettings 控制
})
const emit = defineEmits([
  'select-sub', 'set-style', 'offset-change', 'apply-suggestion',
  'anchor-now', 'anchor-active', 'anchor-cue', 'set-scale', 'reset-sync',
  'save', 'refresh-tracks',
])

const suggesting = ref(false)
const saving = ref(false)
const suggestion = ref(null)
const suggestError = ref('')
const scaleHint = ref(null)

/* 固定预设，不是历史记录：帧率差只可能是这几种组合。
   23.976 与 25 之间差 4.17%，两小时的片子累积到片尾会差 5 分钟，
   光调偏移治不了，必须先把帧率比定住。 */
const scalePresets = computed(() => [
  { v: 1.0, label: '1.0', hint: t('player.scScaleSame') },
  { v: 0.95904, label: '23.976/25', hint: t('player.scScale2398') },
  { v: 1.04271, label: '25/23.976', hint: t('player.scScale25') },
])
const scaleText = computed(() => Number(props.syncCurrent.scale || 1).toFixed(5))
const offsetSecText = computed(() => ((props.syncCurrent.offset_ms || 0) / 1000).toFixed(1))
function fmtClock(sec) {
  const s = Math.max(0, Math.floor(sec || 0))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = String(s % 60).padStart(2, '0')
  return h ? h + ':' + String(m).padStart(2, '0') + ':' + ss : m + ':' + ss
}
/* 当前时间 / 总时长：知道正片从第几秒开始，偏移就能直接读出来 */
const playClock = computed(() => {
  const cur = fmtClock(props.currentTime)
  return props.duration > 0 ? cur + ' / ' + fmtClock(props.duration) : cur
})
function onOffsetInput(e) {
  const v = Number(e && e.target && e.target.value)
  if (Number.isFinite(v)) emit('offset-change', Math.round(v * 1000))
}
function onScaleInput(e) {
  const v = Number(e && e.target && e.target.value)
  if (Number.isFinite(v) && v > 0) emit('set-scale', v)
}

const online = useOnlineSubtitles(toRef(props, 'movieId'), async () => {
  emit('refresh-tracks')
})

/* 内嵌轨的 id 形如 "12:0"，不是可登记的字幕记录；校准接口只认数字 id */
const canSync = computed(() => /^\d+$/.test(String(props.activeSubId || '')))
const offsetSec = computed(() => (props.syncCurrent.offset_ms || 0) / 1000)
const offsetText = computed(() => {
  const s = offsetSec.value
  return (s > 0 ? '+' : '') + s.toFixed(1) + 's'
})
const sizeOpts = [
  { v: 's', t: () => t('player.subSizeS') },
  { v: 'm', t: () => t('player.subSizeM') },
  { v: 'l', t: () => t('player.subSizeL') },
]
const bgOpts = [
  { v: '0', t: () => t('player.subBgNone') },
  { v: '1', t: () => t('player.subBgHalf') },
  { v: '2', t: () => t('player.subBgFull') },
]
const posOpts = [
  { v: -2, t: () => t('player.scPosUp') },
  { v: 0, t: () => t('player.scPosDefault') },
  { v: 2, t: () => t('player.scPosDown') },
]

function nudge(ms) { emit('offset-change', (props.syncCurrent.offset_ms || 0) + ms) }
function onSlider(e) { emit('offset-change', Number(e.target.value)) }

async function autoDetect() {
  if (!props.activeSubId || suggesting.value) return
  suggesting.value = true
  suggestion.value = null
  suggestError.value = ''
  scaleHint.value = null
  try {
    const r = await suggestSubtitleSync(props.movieId, props.activeSubId)
    if (r.suggested) {
      suggestion.value = r
    } else if (r.scale_only && r.scale) {
      // 整体对不齐（多半是片头广告长度未知），但帧率差仍然可信
      scaleHint.value = r
      suggestError.value = ''
    } else {
      suggestError.value = r.reason || t('player.scSuggestFailed')
    }
  } catch (e) {
    suggestError.value = e.message
  } finally {
    suggesting.value = false
  }
}
function applyScaleHint() {
  if (!scaleHint.value) return
  emit('set-scale', scaleHint.value.scale)
  scaleHint.value = null
}
function anchorNow() {
  emit('anchor-now', { cueIndex: 0 })
}
/* 校准值调坏了（字幕整个跑出画面）时一键归零，再重新对齐 */
function resetAll() {
  emit('reset-sync')
}

/* 字幕列表：用户从台词里认出「就是这句」，比猜序号可靠得多 */
const cueOpen = ref(panel.cueOpen)
const cueLoading = ref(false)
const cueError = ref('')
const cueItems = ref(panel.cueItems)
const cueTotal = ref(panel.cueTotal)
const cueOffset = ref(panel.cueOffset)
const cueQuery = ref(panel.cueQuery)

/* 状态回写：面板关闭（组件销毁）后重新打开时，直接恢复到上次的列表状态。 */
watch([cueOpen, cueQuery, cueItems, cueTotal, cueOffset], () => {
  panel.cueOpen = cueOpen.value
  panel.cueQuery = cueQuery.value
  panel.cueItems = cueItems.value
  panel.cueTotal = cueTotal.value
  panel.cueOffset = cueOffset.value
  savePanel(panel)
})
/* 换片时清掉上一部片的候选，避免误把别片的台词锚到当前画面 */
watch(() => props.movieId, () => {
  cueItems.value = []
  cueTotal.value = 0
  cueOffset.value = 0
  cueQuery.value = ''
  cueOpen.value = false
  scaleHint.value = null
  suggestion.value = null
})
onMounted(() => {
  // 刷新页面后候选列表是空的（只在内存里），此时若记得列表是展开的，自动补载
  if (cueOpen.value && !cueItems.value.length) loadCues(true)
})

function fmtSec(sec) {
  const s = Math.max(0, Math.round(sec || 0))
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0')
}
async function loadCues(reset = false) {
  if (!canSync.value || cueLoading.value) return
  if (reset) {
    cueOffset.value = 0
    cueItems.value = []
  }
  cueLoading.value = true
  cueError.value = ''
  try {
    const r = await subtitleCueList(props.movieId, props.activeSubId, {
      offset: cueOffset.value,
      limit: 40,
      q: cueQuery.value.trim(),
    })
    const items = r.items || []
    cueItems.value = reset || cueQuery.value.trim() ? items : cueItems.value.concat(items)
    cueTotal.value = r.matched != null ? r.matched : (r.total || 0)
    cueOffset.value += items.length
  } catch (e) {
    cueError.value = e.message
  } finally {
    cueLoading.value = false
  }
}
async function toggleCueList() {
  if (cueOpen.value) {
    cueOpen.value = false
    return
  }
  cueOpen.value = true
  if (!cueItems.value.length) await loadCues(true)
}
function pickCue(c) {
  emit('anchor-cue', { start: c.start, index: c.index, text: c.text })
  cueOpen.value = false
}
function applySuggestion() {
  if (!suggestion.value) return
  emit('apply-suggestion', suggestion.value)
  suggestion.value = null
}
async function save() {
  if (saving.value) return
  saving.value = true
  try { emit('save') } finally { setTimeout(() => { saving.value = false }, 400) }
}
function fmtMin(ms) { return Math.round((ms || 0) / 60000) }
</script>

<template>
  <div class="sc-panel" @click.stop>

    <!-- 轨道：切换 + 播放中直接在线找字幕，不必退出播放 -->
    <div v-if="tab === 'tracks'" class="sc-body">
      <button class="sc-item" :class="{ on: !activeSubId }" @click="emit('select-sub', null)">
        {{ $t('player.subOff') }}
      </button>
      <button v-for="tr in tracks" :key="tr.id" class="sc-item"
              :class="{ on: activeSubId === tr.id }" @click="emit('select-sub', tr.id)">
        <span class="sc-ellipsis">{{ tr.label }}</span>
        <span v-if="tr.sync" class="sc-tag">{{ $t('player.subCalibrated') }}</span>
      </button>
      <div class="sc-sep"></div>
      <button class="sc-btn" :disabled="online.searching.value" @click="online.search()">
        {{ online.searching.value ? $t('detail.onlineSubsSearching') : $t('player.scFindOnline') }}
      </button>
      <div v-if="online.error.value" class="sc-err">{{ online.error.value }}</div>
      <div v-if="online.searching.value && !online.hasResult.value" class="sc-hint">
        {{ $t('detail.onlineSubsSearching') }}
      </div>
      <div v-if="online.best.value" class="sc-hint">
        <button class="sc-mini"
                :disabled="!!online.picking.value || online.best.value.id === online.pickedId.value"
                @click="online.pickBest()">
          {{ $t('detail.onlineSubsBest') }}
        </button>
      </div>
      <div v-if="online.hint.value" class="sc-hint">
        {{ $t('detail.onlineSubsTip.' + online.hint.value) }}
      </div>
      <div v-if="online.searched.value && !online.candidates.value.length" class="sc-hint">
        {{ $t('detail.onlineSubsEmpty') }}
      </div>
      <ul v-if="online.candidates.value.length" class="sc-cands"
          :class="{ 'is-loading': online.searching.value }">
        <li v-for="c in online.candidates.value.slice(0, 10)" :key="c.id">
          <span class="sc-score">{{ c.estimate }}</span>
          <span class="sc-ellipsis" :title="c.name">
            <span v-if="c.id === online.bestId.value" class="sc-tag">{{ $t('detail.onlineSubsBestTag') }}</span>
            {{ c.name }}
          </span>
          <span class="sc-meta">{{ c.provider_label }} · {{ c.lang }}</span>
          <span v-if="c.can_translate" class="sc-tag">{{ $t('detail.onlineSubsCanTranslate') }}</span>
          <button class="sc-mini"
                  :disabled="!!online.picking.value || c.id === online.pickedId.value"
                  @click="online.pick(c)">
            {{ online.picking.value === c.id ? '…'
              : (c.id === online.pickedId.value ? $t('detail.subPicked') : $t('detail.onlineSubsPick')) }}
          </button>
        </li>
      </ul>
    </div>

    <!-- 同步：前端即时预览，保存后由后端 VTT 统一应用 -->
    <div v-if="tab === 'sync'" class="sc-body">
      <div class="sc-row">
        <span class="sc-label">{{ $t('player.scOffset') }}</span>
        <input class="sc-num" type="number" step="0.1"
               :value="offsetSecText" @change="onOffsetInput" />
        <span class="sc-hint">s</span>
        <span class="sc-offset" :class="{ dirty: syncDirty }">{{ offsetText }}</span>
        <span v-if="syncDirty" class="sc-tag">{{ $t('player.scUnsaved') }}</span>
      </div>
      <div class="sc-row">
        <button class="sc-btn" @click="nudge(-500)">− 0.5s</button>
        <button class="sc-btn" @click="nudge(500)">+ 0.5s</button>
        <button class="sc-btn" @click="nudge(-100)">− 0.1s</button>
        <button class="sc-btn" @click="nudge(100)">+ 0.1s</button>
        <button class="sc-btn" @click="nudge(-30000)">− 30s</button>
        <button class="sc-btn" @click="nudge(30000)">+ 30s</button>
      </div>
      <input class="sc-range" type="range" min="-180000" max="180000" step="100"
             :value="syncCurrent.offset_ms || 0" @input="onSlider" />
      <!-- 帧率比：不同源版本常有 4% 漂移，先定住它，再用一键锚点定起点 -->
      <div class="sc-row">
        <span class="sc-label">{{ $t('player.scScale') }}</span>
        <input class="sc-num" type="number" step="0.001" min="0.9" max="1.1"
               :value="scaleText" @change="onScaleInput" />
        <div class="sc-seg">
          <button v-for="p in scalePresets" :key="p.v" :title="p.hint"
                  :class="{ on: Math.abs((syncCurrent.scale || 1) - p.v) < 1e-4 }"
                  @click="emit('set-scale', p.v)">{{ p.label }}</button>
        </div>
      </div>
      <div class="sc-row">
        <button class="sc-btn" :disabled="!canSync || suggesting" @click="autoDetect">
          {{ suggesting ? $t('player.scDetecting') : $t('player.scAutoDetect') }}
        </button>
        <button class="sc-btn" :disabled="!canSync || !currentTime" @click="anchorNow">
          {{ $t('player.scAnchor') }}
        </button>
        <button class="sc-btn" :disabled="!canSync || !currentTime"
                @click="emit('anchor-active')">
          {{ $t('player.scAnchorActive') }}
        </button>
        <button class="sc-btn" :disabled="!canSync || !syncDirty" @click="resetAll">
          {{ $t('player.scReset') }}
        </button>
        <button class="sc-btn primary" :disabled="!canSync || !syncDirty || saving" @click="save">
          {{ saving ? $t('player.scSaving') : $t('player.scSave') }}
        </button>
      </div>
      <div class="sc-hint">
        {{ playClock }} · {{ $t('player.scAnchorHint') }}
        <template v-if="cueRange">
          <br />{{ $t('player.scCueRange', { a: fmtSec(cueRange.first), b: fmtSec(cueRange.last) }) }}
          <span v-if="currentTime < cueRange.first || currentTime > cueRange.last" class="sc-warn">
            · {{ $t('player.scOutOfRange') }}
          </span>
        </template>
      </div>

      <!-- 字幕列表：从台词里认出「就是这句」，再锚到当前画面 -->
      <div class="sc-row">
        <button class="sc-btn" :disabled="!canSync" @click="toggleCueList">
          {{ cueOpen ? $t('common.cancel') : $t('player.scPickCue') }}
        </button>
      </div>
      <div v-if="cueOpen" class="sc-cues">
        <input v-model="cueQuery" class="sc-inp" :placeholder="$t('player.scCueSearch')"
               @keydown.enter.prevent="loadCues(true)" />
        <div v-if="cueError" class="sc-err">{{ cueError }}</div>
        <ul v-if="cueItems.length" class="sc-cue-rows">
          <li v-for="c in cueItems" :key="c.index">
            <span class="sc-cue-idx">{{ c.index + 1 }}</span>
            <span class="sc-cue-time">{{ fmtSec(c.start) }}</span>
            <span class="sc-cue-text" :title="c.text">{{ c.text }}</span>
            <button class="sc-mini" @click="pickCue(c)">{{ $t('player.scCuePick') }}</button>
          </li>
        </ul>
        <div v-else-if="!cueLoading" class="sc-hint">{{ $t('player.scCueEmpty') }}</div>
        <div class="sc-row">
          <button v-if="cueItems.length < cueTotal" class="sc-btn" :disabled="cueLoading"
                  @click="loadCues(false)">
            {{ cueLoading ? '…' : $t('player.scCueMore') }}
          </button>
          <span class="sc-hint">{{ $t('player.scCueHint') }}</span>
        </div>
      </div>
      <div v-if="scaleHint" class="sc-suggest">
        <div class="sc-suggest-head">{{ $t('player.scScaleOnly') }}</div>
        <div class="sc-hint">{{ scaleHint.evidence }}</div>
        <button class="sc-mini" @click="applyScaleHint">{{ $t('player.scScaleApply') }}</button>
      </div>
      <div v-if="suggestion" class="sc-suggest">
        <div class="sc-suggest-head">
          {{ $t('player.scSuggest', { n: ((suggestion.offset_ms || 0) / 1000).toFixed(1), c: Math.round((suggestion.confidence || 0) * 100) }) }}
        </div>
        <div class="sc-hint">{{ suggestion.evidence }}</div>
        <button class="sc-mini" @click="applySuggestion">{{ $t('player.scApply') }}</button>
      </div>
      <div v-if="suggestError" class="sc-hint">{{ suggestError }}</div>
      <div class="sc-hint">{{ $t('player.scKeysHint') }}</div>
    </div>

    <!-- 显示：字号 / 背景 / 垂直位置（避开画面烧录的硬字幕） -->
    <div v-if="tab === 'display'" class="sc-body">
      <div class="sc-row">
        <span class="sc-label">{{ $t('player.subSize') }}</span>
        <div class="sc-seg">
          <button v-for="o in sizeOpts" :key="o.v" :class="{ on: subStyle.size === o.v }"
                  @click="emit('set-style', 'size', o.v)">{{ o.t() }}</button>
        </div>
      </div>
      <div class="sc-row">
        <span class="sc-label">{{ $t('player.subBg') }}</span>
        <div class="sc-seg">
          <button v-for="o in bgOpts" :key="o.v" :class="{ on: subStyle.bg === o.v }"
                  @click="emit('set-style', 'bg', o.v)">{{ o.t() }}</button>
        </div>
      </div>
      <div class="sc-row">
        <span class="sc-label">{{ $t('player.scPosY') }}</span>
        <div class="sc-seg">
          <button v-for="o in posOpts" :key="o.v" :class="{ on: (subStyle.posY || 0) === o.v }"
                  @click="emit('set-style', 'posY', o.v)">{{ o.t() }}</button>
        </div>
      </div>
      <div class="sc-hint">{{ $t('player.scPosHint') }}</div>
      <button class="sc-btn primary" :disabled="!canSync || !syncDirty" @click="save">{{ $t('player.scSave') }}</button>
    </div>
  </div>
</template>

<style scoped>
/* 与 VideoConsole 同一套视觉语言（sc-* 是 scoped 的，两边按同参数维护）。
   面板外壳（定位 / 背景 / 阴影 / 动画 / 滚动）由 PlayerSettings 统一提供。 */
.sc-panel { display: flex; flex-direction: column; gap: 12px; }

.sc-body { display: flex; flex-direction: column; gap: 10px; }

.sc-item { display: flex; align-items: center; gap: 6px; width: 100%; padding: 7px 10px; border: 0; border-radius: 6px; background: transparent; color: #e6e9ef; cursor: pointer; text-align: left; min-height: 30px; }
.sc-item:hover { background: rgba(255,255,255,.1); }
.sc-item.on { background: rgba(76,139,245,.35); }
.sc-ellipsis { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sc-tag { padding: 1px 6px; border-radius: 3px; background: rgba(255,255,255,.14); font-size: 10px; white-space: nowrap; }
.sc-sep { height: 1px; background: rgba(255,255,255,.14); margin: 2px 0; }

.sc-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.sc-label { min-width: 56px; color: #c3c7cf; }
.sc-offset { font-variant-numeric: tabular-nums; font-weight: 600; }
.sc-offset.dirty { color: #ffd479; }

.sc-btn { padding: 6px 11px; min-height: 28px; border: 1px solid rgba(255,255,255,.18); border-radius: 6px; background: rgba(255,255,255,.07); color: #e3e6ec; cursor: pointer; font-size: 12.5px; transition: background .12s; }
.sc-btn:hover:not(:disabled) { background: rgba(255,255,255,.16); }
.sc-btn:disabled { opacity: .4; cursor: default; }
.sc-btn.primary { background: var(--c-primary, #4c8bf5); border-color: transparent; color: #fff; }
.sc-btn.primary:hover:not(:disabled) { background: var(--c-primary, #4c8bf5); filter: brightness(1.1); }

.sc-seg { display: flex; gap: 2px; padding: 2px; border-radius: 8px; background: rgba(255,255,255,.09); }
.sc-seg button { padding: 5px 12px; border: 0; border-radius: 6px; background: transparent; color: #b9bcc4; cursor: pointer; font-size: 12px; }
.sc-seg button:hover { color: #fff; }
.sc-seg button.on { background: var(--c-primary, #4c8bf5); color: #fff; }

/* 滑条：自绘轨道 + 圆形滑块，深色面板里默认原生样式几乎看不见 */
.sc-range { -webkit-appearance: none; appearance: none; width: 100%; height: 18px; margin: 0; background: transparent; cursor: pointer; }
.sc-range::-webkit-slider-runnable-track { height: 4px; border-radius: 2px; background: rgba(255,255,255,.25); }
.sc-range::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 14px; height: 14px; margin-top: -5px; border: 0; border-radius: 50%; background: var(--c-primary, #4c8bf5); box-shadow: 0 1px 4px rgba(0,0,0,.5); }
.sc-range:hover::-webkit-slider-thumb { box-shadow: 0 0 0 4px rgba(76,139,245,.25); }
.sc-range::-moz-range-track { height: 4px; border-radius: 2px; background: rgba(255,255,255,.25); }
.sc-range::-moz-range-thumb { width: 14px; height: 14px; border: 0; border-radius: 50%; background: var(--c-primary, #4c8bf5); box-shadow: 0 1px 4px rgba(0,0,0,.5); }

.sc-hint { color: #a6aab3; font-size: 11.5px; line-height: 1.6; }
.sc-warn { color: #ffb454; }
.sc-err { color: #ff8a8a; font-size: 11.5px; }
.sc-suggest { padding: 8px 10px; border: 1px solid rgba(255,212,121,.4); border-radius: 6px; background: rgba(255,212,121,.08); }
.sc-suggest-head { font-weight: 600; color: #ffd479; }

.sc-cands { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; max-height: 180px; overflow: auto; transition: opacity .15s; }
.sc-cands.is-loading { opacity: .55; pointer-events: none; }
.sc-cands li { display: flex; align-items: center; gap: 5px; padding: 4px 6px; border-radius: 4px; background: rgba(255,255,255,.06); }
.sc-score { color: #9ec1ff; min-width: 26px; }
.sc-meta { color: #9a9ea7; font-size: 10.5px; white-space: nowrap; }
.sc-mini { padding: 3px 9px; min-height: 24px; border: 0; border-radius: 5px; background: var(--c-primary, #4c8bf5); color: #fff; cursor: pointer; font-size: 11.5px; }
.sc-mini:disabled { opacity: .5; cursor: default; }

/* 字幕列表：序号 / 起始时间 / 台词 / 对齐 */
.sc-cues { display: flex; flex-direction: column; gap: 6px; }
.sc-inp { width: 100%; padding: 5px 9px; border: 1px solid rgba(255,255,255,.2); border-radius: 6px; background: rgba(0,0,0,.35); color: #eee; font-size: 12.5px; }
.sc-inp::placeholder { color: #777; }
.sc-inp:focus { outline: none; border-color: var(--c-primary, #4c8bf5); }
/* 精确输入：偏移（秒）/ 帧率比，比拖滑块准，也便于直接照抄别人给的值 */
.sc-num { width: 78px; padding: 4px 7px; border: 1px solid rgba(255,255,255,.2); border-radius: 6px; background: rgba(0,0,0,.35); color: #eee; font-size: 12.5px; font-variant-numeric: tabular-nums; min-height: 28px; }
.sc-num:focus { outline: none; border-color: var(--c-primary, #4c8bf5); }
.sc-cue-rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; max-height: 200px; overflow: auto; }
.sc-cue-rows li { display: flex; align-items: center; gap: 6px; padding: 4px 6px; border-radius: 4px; background: rgba(255,255,255,.05); }
.sc-cue-rows li:hover { background: rgba(255,255,255,.1); }
.sc-cue-idx { color: #9ec1ff; min-width: 22px; font-size: 10.5px; text-align: right; }
.sc-cue-time { color: #9a9ea7; min-width: 38px; font-size: 10.5px; font-variant-numeric: tabular-nums; }
.sc-cue-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #d5d9e0; }
</style>
