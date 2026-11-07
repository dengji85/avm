<script setup>
import { ref, computed, onMounted, nextTick, toRef, watch } from 'vue'
import { subtitleTracks } from '../api.js'
import { t } from '../i18n/index.js'
import { toast } from '../utils.js'
import { aiWorkspace } from '../composables/aiWorkspace.js'
import { useOnlineSubtitles } from '../composables/useOnlineSubtitles.js'
import SubtitleReader from './SubtitleReader.vue'

const props = defineProps({
  movieId: { type: Number, required: true },
  subtitles: { type: Array, default: () => [] },
})
// goto-asr：切到详情抽屉的「信息」tab（对白提取控件在那）
// goto-settings：跳 设置 → 在线字幕（由父组件收起抽屉后切换）
const emit = defineEmits(['refresh', 'goto-asr', 'goto-settings'])

const status = ref('unchecked')
const open = ref(false)
const panelEl = ref(null)
const picked = ref(null)                 // 刚采纳、且需要翻译的字幕 id
const reader = ref(null)                 // 当前打开阅读的字幕 / 预览的候选
const count = computed(() => (props.subtitles || []).length)
const translating = computed(() => !!aiWorkspace.ai.subs.loading)
const transPercent = computed(() => aiWorkspace.ai.subs.percent || 0)

async function refreshStatus() {
  try {
    const r = await subtitleTracks(props.movieId)
    status.value = r.status || (r.tracks?.length ? 'unchecked' : 'none')
  } catch (e) { /* 保持当前状态，不打断用户 */ }
}
onMounted(refreshStatus)
// 详情抽屉会被复用（切下一部不销毁），换片时要重置展开态与候选
watch(() => props.movieId, () => {
  open.value = false
  status.value = 'unchecked'
  picked.value = null
  reader.value = null
  online.reset()
  refreshStatus()
})

const online = useOnlineSubtitles(toRef(props, 'movieId'), async (r) => {
  await emit('refresh')
  await refreshStatus()
  if (r.can_translate && r.sub_id) {
    // 保持面板展开，直接给出「翻译这一条」——用户不必再跑去别的 tab 找 AI 翻译
    picked.value = r.sub_id
    toast(t('detail.onlineSubsDoneTranslate', { score: r.score }), 'ok')
  } else {
    picked.value = null
    open.value = false
    toast(t('detail.onlineSubsDone', { score: r.score }), 'ok')
  }
})

async function translatePicked() {
  if (!picked.value || translating.value) return
  const subId = picked.value
  picked.value = null
  await aiWorkspace.ai.translateSubs(subId)
  toast(t('detail.subTranslateStarted'), 'ok')
}

function needSources(hint) {
  return hint === 'need_credentials' || hint === 'no_source'
}

function openReader(s) {
  if (s.exists === false) return        // 文件已丢失，后端会 404，别让用户白点
  reader.value = { subId: s.id, candidateId: '', title: s.filename || '' }
}

function openPreview(c) {
  reader.value = { subId: null, candidateId: c.id, title: c.name || '' }
}

function srcLabel(s) {
  const src = s.source || ''
  if (src === 'ai-translate') return t('detail.subSrcAi')
  if (src === 'asr') return t('detail.subSrcAsr')
  if (src === 'sidecar' || src === 'matched') return t('detail.subSrcLocal')
  if (src.startsWith('external:')) {
    const p = s.provider || src.split(':', 1)[1]
    return t('detail.subSrcOnline') + (p ? ' · ' + p : '')
  }
  return src || t('detail.subSrcLocal')
}

// 翻译在后台跑完（或失败/取消）后，把新登记的中文字幕刷进列表与状态点
watch(translating, (now, was) => {
  if (was && !now) {
    emit('refresh')
    refreshStatus()
  }
})

/**
 * 展开用内联面板而不是绝对定位浮层：详情抽屉的 .dd-content 是滚动容器，
 * 浮层一旦超出可视区就会被裁掉，表现成「点了没反应」。内联展开必然可见，
 * 再把面板滚进视野，点一次就自动开搜，不需要用户再找按钮。
 */
async function toggleOpen() {
  if (open.value) {
    open.value = false
    picked.value = null
    // 不 reset：保留上一轮结果，下次展开直接复用，既省一次联网也不会闪
    return
  }
  open.value = true
  await nextTick()
  try {
    panelEl.value?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  } catch (e) { /* 老浏览器忽略 */ }
  if (!online.hasResult.value && !online.searching.value) online.search()
}
function fmtMin(ms) { return Math.round((ms || 0) / 60000) }
function sourceLabel(s) {
  const key = { ok: 'onlineSubsSrcOk', miss: 'onlineSubsSrcMiss',
                off: 'onlineSubsSrcOff', degraded: 'onlineSubsSrcDeg' }[s.status]
  return t('detail.' + (key || 'onlineSubsSrcDeg'), { n: s.count })
}
</script>

<template>
  <div class="sub-block">
    <div class="sb-head">
      <span class="sb-dot" :class="status"></span>
      <span class="sb-count">{{ count ? $t('detail.subCount', { n: count }) : $t('detail.subNone') }}</span>
      <span v-if="count" class="sb-state">
        {{ $t(status === 'ready' ? 'detail.subReady' : 'detail.subUnchecked') }}
      </span>
      <span class="spacer"></span>
      <button class="btn tiny" :class="open ? '' : 'primary'" @click.stop="toggleOpen">
        {{ open ? $t('common.cancel') : $t('detail.subFind') }}
      </button>
    </div>

    <!-- 已有字幕常驻显示：不需要先展开「找字幕」也能直接点开阅读 -->
    <div v-if="subtitles.length" class="sb-list">
      <div class="sb-list-head">{{ $t('detail.subLocalList') }}</div>
      <div v-for="s in subtitles" :key="s.id" class="sb-item">
        <span class="sb-src-badge">{{ srcLabel(s) }}</span>
        <button class="sb-read-name" :title="s.filename" :disabled="s.exists === false"
                @click="openReader(s)">{{ s.filename }}</button>
        <span class="sb-meta">{{ s.lang || '—' }}</span>
        <span v-if="s.score" class="sb-meta">{{ $t('detail.onlineSubsEstimate', { n: s.score }) }}</span>
        <button class="btn tiny ghost" :disabled="s.exists === false" @click="openReader(s)">
          {{ $t('detail.subRead') }}
        </button>
      </div>
    </div>

    <div v-if="open" ref="panelEl" class="sb-panel" @click.stop>
      <div v-if="online.searching.value" class="sb-progress"></div>
      <div class="sb-panel-head">
        <span class="sb-title">{{ $t('detail.onlineSubs') }}</span>
        <button class="sb-close" :title="$t('common.cancel')" @click="toggleOpen">×</button>
      </div>
      <div class="sb-hint">{{ $t('detail.onlineSubsHint') }}</div>

      <div class="sb-bar">
        <button class="btn tiny primary" :disabled="online.searching.value" @click="online.search()">
          {{ online.searching.value ? $t('detail.onlineSubsSearching') : $t('common.search') }}
        </button>
        <button v-if="online.best.value" class="btn tiny"
                :disabled="!!online.picking.value || online.best.value.id === online.pickedId.value"
                @click="online.pickBest()">
          {{ $t('detail.onlineSubsBest') }}
        </button>
        <span v-if="online.error.value" class="sb-err">{{ online.error.value }}</span>
        <span v-else-if="online.searched.value && !online.searching.value && !online.candidates.value.length" class="sb-hint">
          {{ $t('detail.onlineSubsEmpty') }}
        </span>
        <span v-else-if="online.candidates.value.length" class="sb-hint">
          {{ $t('detail.onlineSubsCount', { n: online.candidates.value.length }) }}
        </span>
        <span v-if="online.searched.value" class="sb-hint">
          {{ $t('detail.onlineSubsStats', { ms: online.elapsed.value, n: online.allSources.value.length }) }}
          <template v-if="online.cached.value">{{ $t('detail.onlineSubsCached') }}</template>
        </span>
      </div>

      <!-- 首轮加载用骨架占位，不再先白一下；已有结果时则保留旧列表半透明 -->
      <div v-if="online.searching.value && !online.hasResult.value" class="sb-skeleton">
        <div v-for="n in 4" :key="n" class="sb-sk-row"></div>
      </div>

      <div v-if="online.hint.value" class="sb-tip">
        <span>{{ $t('detail.onlineSubsTip.' + online.hint.value) }}</span>
        <span class="sb-tip-actions">
          <button v-if="needSources(online.hint.value)" class="btn tiny" @click="emit('goto-settings')">
            {{ $t('detail.subGoSources') }}
          </button>
          <button v-if="online.hint.value === 'no_result'" class="btn tiny" @click="emit('goto-asr')">
            {{ $t('detail.subGoAsr') }}
          </button>
          <button v-if="online.hint.value === 'all_foreign' && online.best.value" class="btn tiny"
                  :disabled="!!online.picking.value || online.best.value.id === online.pickedId.value"
                  @click="online.pickBest()">
            {{ $t('detail.onlineSubsBest') }}
          </button>
        </span>
      </div>

      <div v-if="picked" class="sb-tip sb-tip-ok">
        <span>{{ $t('detail.subReadyToTranslate') }}</span>
        <span class="sb-tip-actions">
          <button class="btn tiny primary" :disabled="translating" @click="translatePicked">
            {{ $t('detail.subTranslatePicked') }}
          </button>
        </span>
      </div>
      <div v-else-if="translating" class="sb-tip">
        {{ $t('detail.subTranslateRunning', { n: transPercent }) }}
      </div>

      <ul v-if="online.candidates.value.length" class="sb-rows"
          :class="{ 'is-loading': online.searching.value }">
        <li v-for="c in online.candidates.value.slice(0, 12)" :key="c.id">
          <span class="sb-score">{{ $t('detail.onlineSubsEstimate', { n: c.estimate }) }}</span>
          <span class="sb-name" :title="c.name">
            <span v-if="c.id === online.bestId.value" class="sb-badge">{{ $t('detail.onlineSubsBestTag') }}</span>
            {{ c.name }}
          </span>
          <span class="sb-meta">{{ c.provider_label }} · {{ c.lang }}</span>
          <span v-if="c.can_translate" class="sb-tag">{{ $t('detail.onlineSubsCanTranslate') }}</span>
          <span class="sb-meta" v-if="c.duration_ms">
            {{ $t('detail.onlineSubsDuration', { n: fmtMin(c.duration_ms) }) }}
          </span>
          <button class="btn tiny ghost" :disabled="!!online.picking.value" @click="openPreview(c)">
            {{ $t('detail.subPreview') }}
          </button>
          <button class="btn tiny primary"
                  :disabled="!!online.picking.value || c.id === online.pickedId.value"
                  @click="online.pick(c)">
            {{ online.picking.value === c.id ? $t('detail.onlineSubsPicking')
              : (c.id === online.pickedId.value ? $t('detail.subPicked') : $t('detail.onlineSubsPick')) }}
          </button>
        </li>
      </ul>

      <div v-if="online.allSources.value.length" class="sb-srcs">
        <span v-for="s in online.allSources.value" :key="s.provider" class="sb-src"
              :class="s.status" :title="s.message">
          {{ s.label }} · {{ sourceLabel(s) }}
        </span>
      </div>
    </div>

    <SubtitleReader v-if="reader" :movie-id="movieId" :sub-id="reader.subId"
                    :candidate-id="reader.candidateId" :title="reader.title"
                    @close="reader = null" />
  </div>
</template>

<style scoped>
.sub-block { display: flex; flex-direction: column; gap: 8px; margin-top: var(--sp-2); }
.sb-head { display: flex; align-items: center; gap: 8px; font-size: var(--fs-sm); }
.sb-dot { flex: none; width: 7px; height: 7px; border-radius: 50%; background: var(--c-text-3); }
.sb-dot.ready { background: var(--c-ok); }
.sb-count { color: var(--c-text-2); }
.sb-state { font-size: var(--fs-xs); color: var(--c-text-3); }
.spacer { flex: 1; }
.sb-panel { padding: var(--sp-3); border: 1px solid var(--c-line); border-radius: var(--r-md); background: var(--c-surface-2); animation: sb-in .16s ease-out; }
@keyframes sb-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
.sb-progress { height: 2px; margin-bottom: 8px; border-radius: 2px; overflow: hidden; background: var(--c-surface-3); }
.sb-progress::after { content: ''; display: block; width: 40%; height: 100%; background: var(--c-primary); animation: sb-slide 1s ease-in-out infinite; }
@keyframes sb-slide { 0% { transform: translateX(-100%); } 100% { transform: translateX(350%); } }
.sb-skeleton { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
.sb-sk-row { height: 30px; border-radius: var(--r-sm); background: linear-gradient(90deg, var(--c-surface-3) 25%, var(--c-line) 50%, var(--c-surface-3) 75%); background-size: 200% 100%; animation: sb-shimmer 1.4s ease-in-out infinite; }
@keyframes sb-shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
@media (prefers-reduced-motion: reduce) {
  .sb-panel, .sb-progress::after, .sb-sk-row { animation: none; }
}
.sb-panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }
.sb-title { font-weight: 600; font-size: var(--fs-sm); color: var(--c-text-1); }
.sb-close { border: 0; background: transparent; color: var(--c-text-3); font-size: 18px; line-height: 1; cursor: pointer; padding: 0 2px; }
.sb-close:hover { color: var(--c-text-1); }
.sb-hint { font-size: var(--fs-xs); color: var(--c-text-3); }
.sb-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.sb-err { font-size: var(--fs-xs); color: var(--c-err); }
.sb-rows { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; max-height: 300px; overflow: auto; transition: opacity .15s; }
.sb-rows.is-loading { opacity: .55; pointer-events: none; }
.sb-rows li { display: flex; align-items: center; gap: 8px; padding: 5px 7px; border-radius: var(--r-sm); background: var(--c-surface-3); font-size: var(--fs-sm); }
.sb-score { flex: none; color: var(--c-primary); font-size: var(--fs-xs); min-width: 68px; white-space: nowrap; }
.sb-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sb-meta { flex: none; color: var(--c-text-3); font-size: var(--fs-xs); white-space: nowrap; }
.sb-badge, .sb-tag { flex: none; font-size: var(--fs-xs); padding: 1px 5px; border-radius: var(--r-sm); white-space: nowrap; }
.sb-badge { color: var(--c-primary); background: var(--c-surface-2); border: 1px solid var(--c-primary); }
.sb-tag { color: var(--c-text-3); border: 1px solid var(--c-line); }
.sb-tip { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 8px; padding: 6px 8px; border-radius: var(--r-sm); background: var(--c-surface-3); font-size: var(--fs-xs); color: var(--c-text-2); line-height: 1.5; }
.sb-tip > span:first-child { flex: 1; min-width: 180px; }
.sb-tip-actions { display: flex; flex: none; gap: 6px; }
.sb-tip-ok { background: var(--c-surface-2); border: 1px solid var(--c-primary); }
.sb-list { max-height: 200px; overflow: auto; }
.sb-list-head { margin-bottom: 5px; font-size: var(--fs-xs); color: var(--c-text-3); }
.sb-item { display: flex; align-items: center; gap: 7px; margin-bottom: 4px; padding: 4px 6px; border-radius: var(--r-sm); background: var(--c-surface-3); font-size: var(--fs-sm); }
.sb-src-badge { flex: none; max-width: 150px; padding: 1px 5px; border: 1px solid var(--c-line); border-radius: var(--r-sm); font-size: var(--fs-xs); color: var(--c-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sb-read-name { flex: 1; min-width: 0; padding: 0; border: 0; background: transparent; text-align: left; font-size: var(--fs-sm); color: var(--c-text-1); cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sb-read-name:hover:not(:disabled) { color: var(--c-primary); text-decoration: underline; }
.sb-read-name:disabled { color: var(--c-text-3); cursor: not-allowed; }
.sb-srcs { display: flex; flex-wrap: wrap; gap: 4px 10px; margin-top: 8px; font-size: var(--fs-xs); color: var(--c-text-3); }
.sb-src.ok { color: var(--c-ok); }
.sb-src.off, .sb-src.degraded { color: var(--c-warn, var(--c-text-3)); }
@media (max-width: 640px) {
  .sb-rows li { flex-wrap: wrap; }
  .sb-name { flex-basis: 100%; order: -1; }
}
</style>
