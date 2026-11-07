<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { subtitleCueList, previewOnlineSubtitle } from '../api.js'
import { t } from '../i18n/index.js'

/**
 * 字幕阅读器：把一条字幕按时间轴逐行读出来。
 *
 * 两种数据源：
 *  - subId：已登记字幕，走 /cues 分页 + 关键字搜索（full=1 拿完整台词）
 *  - candidateId：在线候选，走 /online-preview，服务端把字幕下载到内存解析，
 *    首次较慢，后续页直接读它的内存缓存，不再联网
 *
 * 滚动到底自动加载下一页；首次加载给骨架屏与 spinner，出站下载超过 4 秒
 * 追加「响应较慢」提示，避免长时间白屏让人以为卡死。
 * 用 Teleport 挂到 body：详情抽屉的 .dd-content 是滚动容器，内联浮层会被裁掉。
 */
const props = defineProps({
  movieId: { type: Number, required: true },
  subId: { type: [Number, String], default: null },
  candidateId: { type: String, default: '' },
  title: { type: String, default: '' },
})
const emit = defineEmits(['close'])

const PAGE = 60
const THRESHOLD = 180          // 距底部多少像素开始预加载
const SLOW_MS = 4000

const bodyEl = ref(null)
const items = ref([])
const total = ref(0)
const loading = ref(false)
const hasLoaded = ref(false)
const slow = ref(false)
const autoLoad = ref(true)
const error = ref('')
const query = ref('')
const offset = ref(0)
const isPreview = ref(false)
const showTime = ref(true)
const copied = ref(false)
let slowTimer = 0

const isCandidate = computed(() => !!props.candidateId)
const canMore = computed(() => !query.value.trim() && items.value.length < total.value)
const loadingText = computed(() => {
  if (!hasLoaded.value) {
    return isCandidate.value ? t('detail.subReadPreviewLoading') : t('detail.subReadLoading')
  }
  return t('detail.subReadLoadingMore')
})

function fmtTime(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0))
  const m = Math.floor(s / 60)
  return `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

function skWidth(n) {
  return [92, 78, 86, 64, 90, 72, 82][n % 7] + '%'
}

async function load(reset = false) {
  if (loading.value) return
  if (reset) {
    items.value = []
    offset.value = 0
    error.value = ''
    autoLoad.value = true
  }
  loading.value = true
  slow.value = false
  clearTimeout(slowTimer)
  slowTimer = setTimeout(() => { if (loading.value) slow.value = true }, SLOW_MS)
  const q = query.value.trim()
  try {
    const params = { offset: offset.value, limit: PAGE, q, full: 1 }
    const r = isCandidate.value
      ? await previewOnlineSubtitle(props.movieId, props.candidateId, params)
      : await subtitleCueList(props.movieId, props.subId, params)
    const got = r.items || []
    isPreview.value = isCandidate.value
    items.value = reset || q ? got : items.value.concat(got)
    total.value = r.matched != null ? r.matched : (r.total || 0)
    if (!q) offset.value += got.length
    // 服务端没再给数据就停下，别在空响应上空转
    if (!reset && !got.length) autoLoad.value = false
  } catch (e) {
    error.value = e.message || t('common.error')
    autoLoad.value = false
  } finally {
    clearTimeout(slowTimer)
    slow.value = false
    loading.value = false
    hasLoaded.value = true
    await nextTick()
    onScroll()                 // 内容没填满一屏时继续补，直到填满或加载完
  }
}

function onScroll() {
  if (!autoLoad.value || loading.value || !canMore.value) return
  const el = bodyEl.value
  if (!el) return
  if (el.scrollHeight - el.scrollTop - el.clientHeight < THRESHOLD) load(false)
}

async function copyAll() {
  const text = items.value
    .map(c => (showTime.value ? `[${fmtTime(c.start)}] ` : '') + c.text)
    .join('\n')
  try {
    await navigator.clipboard.writeText(text)
  } catch (e) {
    // 局域网是 http，navigator.clipboard 不可用；退回老接口
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      ta.remove()
    } catch (e2) { return }
  }
  copied.value = true
  setTimeout(() => { copied.value = false }, 2000)
}

function onKey(e) {
  if (e.key === 'Escape') emit('close')
}
onMounted(() => {
  document.addEventListener('keydown', onKey)
  load(true)
})
onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKey)
  clearTimeout(slowTimer)
})
watch([() => props.subId, () => props.candidateId], () => {
  hasLoaded.value = false
  isPreview.value = false
  load(true)
})
// 清空搜索词立刻恢复全文，否则列表里会混着上次搜索的结果、total 也不对
watch(query, (now, was) => {
  if (was && !now) load(true)
})
</script>

<template>
  <Teleport to="body">
    <div class="sr-mask" @click.self="emit('close')">
      <div class="sr-panel" role="dialog" aria-modal="true">
        <div class="sr-head">
          <div class="sr-title" :title="title">{{ title || $t('detail.subReadTitle') }}</div>
          <button class="sr-x" :title="$t('common.cancel')" @click="emit('close')">×</button>
        </div>

        <div class="sr-meta">
          <span>{{ $t('detail.subReadCount', { n: total }) }}</span>
          <span class="spacer"></span>
          <label class="sr-check">
            <input type="checkbox" v-model="showTime" />{{ $t('detail.subReadTime') }}
          </label>
        </div>

        <div class="sr-tools">
          <input v-model="query" class="sr-search"
                 :placeholder="$t('detail.subReadSearch')" @keydown.enter.prevent="load(true)" />
          <button class="btn tiny" :disabled="loading" @click="load(true)">
            {{ $t('common.search') }}
          </button>
          <button class="btn tiny ghost" :disabled="!items.length" @click="copyAll">
            {{ copied ? $t('detail.subReadCopied') : $t('detail.subReadCopy') }}
          </button>
        </div>

        <div ref="bodyEl" class="sr-body" @scroll.passive="onScroll">
          <div v-if="error" class="sr-err">{{ error }}</div>

          <div v-if="loading && !hasLoaded" class="sr-loading-first">
            <div class="sr-loading-row">
              <span class="sr-spinner"></span>
              <span>{{ loadingText }}<template v-if="slow"> · {{ $t('detail.subReadSlow') }}</template></span>
            </div>
            <div class="sr-skeleton">
              <div v-for="n in 7" :key="n" class="sr-sk-row" :style="{ width: skWidth(n) }"></div>
            </div>
          </div>

          <div v-for="c in items" :key="c.index" class="sr-line">
            <span v-if="showTime" class="sr-time">{{ fmtTime(c.start) }}</span>
            <span class="sr-text">{{ c.text }}</span>
          </div>

          <div v-if="!items.length && !loading && !error" class="sr-empty">
            {{ $t('detail.subReadEmpty') }}
          </div>

          <div class="sr-foot">
            <div v-if="loading && hasLoaded" class="sr-loading-inline">
              <span class="sr-spinner sm"></span>
              <span>{{ loadingText }}<template v-if="slow"> · {{ $t('detail.subReadSlow') }}</template></span>
            </div>
            <div v-else-if="canMore" class="sr-foot-hint">{{ $t('detail.subReadScrollMore') }}</div>
            <div v-else-if="items.length" class="sr-foot-hint">{{ $t('detail.subReadAllLoaded') }}</div>
            <button v-if="!autoLoad && canMore" class="btn tiny" :disabled="loading" @click="load(false)">
              {{ $t('detail.subReadMore') }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.sr-mask { position: fixed; inset: 0; z-index: 10000; display: flex; align-items: center; justify-content: center; padding: var(--sp-4); background: rgba(0, 0, 0, .62); }
.sr-panel { display: flex; flex-direction: column; width: min(760px, 100%); max-height: min(82vh, 900px); border: 1px solid var(--c-line); border-radius: var(--r-lg); background: var(--c-surface-2); box-shadow: 0 18px 60px rgba(0, 0, 0, .45); }
.sr-head { display: flex; align-items: center; gap: 8px; padding: var(--sp-3) var(--sp-4); border-bottom: 1px solid var(--c-line); }
.sr-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; color: var(--c-text-1); }
.sr-x { flex: none; border: 0; background: transparent; color: var(--c-text-3); font-size: 22px; line-height: 1; cursor: pointer; padding: 0 4px; }
.sr-x:hover { color: var(--c-text-1); }
.sr-meta { display: flex; align-items: center; gap: 6px; padding: 6px var(--sp-4); font-size: var(--fs-xs); color: var(--c-text-3); border-bottom: 1px solid var(--c-line); }
.spacer { flex: 1; }
.sr-check { display: flex; align-items: center; gap: 4px; cursor: pointer; white-space: nowrap; }
.sr-tools { display: flex; align-items: center; gap: 8px; padding: var(--sp-2) var(--sp-4); }
.sr-search { flex: 1; min-width: 0; }
.sr-body { flex: 1; min-height: 120px; overflow: auto; padding: 0 var(--sp-4) var(--sp-4); }
.sr-line { display: flex; align-items: baseline; gap: 10px; padding: 6px 4px; border-bottom: 1px solid var(--c-line); font-size: var(--fs-sm); line-height: 1.65; }
.sr-line:hover { background: var(--c-surface-3); }
.sr-time { flex: none; color: var(--c-primary); font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }
.sr-text { flex: 1; min-width: 0; color: var(--c-text-1); white-space: pre-wrap; overflow-wrap: anywhere; }
.sr-err { color: var(--c-err); font-size: var(--fs-sm); padding: var(--sp-3) 0; }
.sr-empty { color: var(--c-text-3); font-size: var(--fs-sm); padding: var(--sp-4) 0; text-align: center; }

.sr-loading-first { padding: var(--sp-4) 0; }
.sr-loading-row { display: flex; align-items: center; justify-content: center; gap: 9px; font-size: var(--fs-sm); color: var(--c-text-2); }
.sr-skeleton { display: flex; flex-direction: column; gap: 11px; margin-top: var(--sp-4); }
.sr-sk-row { height: 12px; border-radius: var(--r-sm); background: linear-gradient(90deg, var(--c-surface-3) 25%, var(--c-line) 50%, var(--c-surface-3) 75%); background-size: 200% 100%; animation: sr-shimmer 1.4s ease-in-out infinite; }
.sr-spinner { flex: none; width: 15px; height: 15px; border: 2px solid var(--c-line); border-top-color: var(--c-primary); border-radius: 50%; animation: sr-spin .75s linear infinite; }
.sr-spinner.sm { width: 12px; height: 12px; }
.sr-foot { display: flex; align-items: center; justify-content: center; gap: 8px; min-height: 32px; padding-top: var(--sp-2); font-size: var(--fs-xs); color: var(--c-text-3); }
.sr-loading-inline { display: flex; align-items: center; gap: 7px; color: var(--c-text-2); }
@keyframes sr-spin { to { transform: rotate(360deg); } }
@keyframes sr-shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
@media (prefers-reduced-motion: reduce) {
  .sr-spinner, .sr-sk-row { animation: none; }
}
@media (max-width: 640px) {
  .sr-mask { padding: 0; }
  .sr-panel { width: 100%; max-height: 100vh; height: 100vh; border-radius: 0; }
}
</style>
