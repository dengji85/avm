<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { state } from '../../state.js'
import {
  listAllSources, batchSources, checkSource, refreshSource, toggleSource,
  patrolStart, patrolState, patrolCancel,
  probeSource, probeStart, probeState, probeCancel,
} from '../../api.js'
import { toast, fmtDateTime, isLocalHost } from '../../utils.js'
import { t } from '../../i18n/index.js'

/* 在线来源体检：跨库列出所有来源，按健康状态筛选与批量处理。
   异常优先排序（需回源站 > 失效 > 待验证 > 检测中 > 停用 > 其余），
   避免「链接坟场」里真正需要处理的来源被埋在后面。 */

const data = ref(null)
const loading = ref(false)
const busy = ref(false)
const q = ref('')
const status = ref('')
const page = ref(1)
const SIZE = 50
const picked = reactive({})
let timer = null
let pollStart = 0
/* 巡检任务：后端长任务，1 秒轮询进度；离开页面时停止轮询（任务在后台继续） */
const patrol = ref(null)
let patrolTimer = null
/* 清晰度 / 字幕探测：同样是后端长任务，可取消 */
const probe = ref(null)
let probeTimer = null
const localMode = isLocalHost()

const items = computed(() => (data.value && data.value.items) || [])
const summary = computed(() => (data.value && data.value.summary) || {})
const total = computed(() => (data.value && data.value.total) || 0)
const pickedIds = computed(() => items.value.filter((i) => picked[i.id]).map((i) => i.id))
const pickedAll = computed(() => items.value.length > 0 && pickedIds.value.length === items.value.length)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / SIZE)))

const FILTERS = computed(() => [
  ['', 'maint.srcAll', summary.value.total || 0],
  ['needs_site', 'maint.srcNeedsSite', summary.value.needs_site || 0],
  ['invalid', 'maint.srcInvalid', summary.value.invalid || 0],
  ['stale', 'maint.srcStale', summary.value.stale || 0],
  ['expired', 'maint.srcExpired', summary.value.expired || 0],
  ['disabled', 'maint.srcDisabled', summary.value.disabled || 0],
  ['valid', 'maint.srcValid', summary.value.valid || 0],
])

async function load() {
  loading.value = true
  try {
    const params = { page: page.value, size: SIZE }
    if (status.value) params.status = status.value
    if (q.value.trim()) params.q = q.value.trim()
    data.value = await listAllSources(params)
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    loading.value = false
    schedulePoll()
  }
}

/** 有 checking 来源时 1.5 秒轮询一次，最多 3 分钟（批量验证会排队） */
function schedulePoll() {
  if (timer) { clearTimeout(timer); timer = null }
  if (!items.value.some((i) => i.status === 'checking')) { pollStart = 0; return }
  if (!pollStart) pollStart = Date.now()
  if (Date.now() - pollStart > 180000) { pollStart = 0; return }
  timer = setTimeout(load, 1500)
}

function setFilter(v) {
  status.value = v
  page.value = 1
  clearPicked()
  load()
}
function search() {
  page.value = 1
  clearPicked()
  load()
}
function clearPicked() {
  Object.keys(picked).forEach((k) => { delete picked[k] })
}
function toggleAll(v) {
  items.value.forEach((i) => { picked[i.id] = v })
}
function goto(p) {
  if (p < 1 || p > totalPages.value) return
  page.value = p
  clearPicked()
  load()
}
function openMovie(i) {
  state.currentId = i.movie_id
}

function statusText(i) {
  if (i.needs_site) return t('maint.srcNeedsSite')
  if (i.status === 'checking') return t('maint.srcChecking')
  if (i.status === 'disabled') return t('maint.srcDisabled')
  if (i.status === 'unsupported') return t('maint.srcUnsupported')
  if (i.status === 'invalid') return t('maint.srcInvalid')
  if (i.expired) return t('maint.srcExpired')
  if (i.status === 'stale') return t('maint.srcStale')
  return t('maint.srcValid')
}
function statusTone(i) {
  if (i.needs_site || i.status === 'invalid' || i.status === 'unsupported') return 'err'
  if (i.expired || i.status === 'stale' || i.status === 'checking') return 'warn'
  if (i.status === 'disabled') return ''
  return 'ok'
}

async function one(fn, okKey) {
  if (busy.value) return
  busy.value = true
  try {
    await fn()
    toast(t(okKey), 'ok')
    await load()
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    busy.value = false
  }
}
const checkOne = (i) => one(() => checkSource(i.id), 'maint.srcCheckStarted')
const refreshOne = (i) => one(() => refreshSource(i.id), 'maint.srcRefreshStarted')
const toggleOne = (i) => one(() => toggleSource(i.id, !i.available), 'maint.srcToggled')

async function batch(action) {
  const ids = pickedIds.value.slice()
  if (!ids.length || busy.value) return
  busy.value = true
  try {
    const r = await batchSources(action, ids)
    if (action === 'check' || action === 'refresh') {
      toast(t('maint.srcQueued', { n: r.queued || 0 }), 'ok')
    } else {
      toast(t('maint.srcBatchDone', { n: r.done || 0 }), 'ok')
    }
    clearPicked()
    await load()
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    busy.value = false
  }
}

/* ---- 立即巡检：后台批量重新解析过期 / 失效地址 ---- */
async function loadPatrol() {
  try {
    patrol.value = await patrolState()
  } catch (e) { /* 后端不可用时保留上一次进度 */ }
}

function pollPatrol() {
  if (patrolTimer) { clearTimeout(patrolTimer); patrolTimer = null }
  if (!patrol.value || !patrol.value.running) return
  patrolTimer = setTimeout(async () => {
    await loadPatrol()
    if (patrol.value && patrol.value.running) pollPatrol()
    else load()
  }, 1000)
}

async function startPatrol() {
  try {
    patrol.value = await patrolStart()
    toast(t('maint.patrolStarted'), 'ok')
    pollPatrol()
  } catch (e) {
    toast(e.message, 'err')
  }
}

async function stopPatrol() {
  try {
    patrol.value = await patrolCancel()
  } catch (e) { toast(e.message, 'err') }
}

/* ---- 清晰度 / 字幕探测：后台批量，进度轮询 ---- */
async function loadProbe() {
  try {
    probe.value = await probeState()
  } catch (e) { /* 后端不可用时保留上一次进度 */ }
}
function pollProbe() {
  if (probeTimer) { clearTimeout(probeTimer); probeTimer = null }
  if (!probe.value || !probe.value.running) return
  probeTimer = setTimeout(async () => {
    await loadProbe()
    if (probe.value && probe.value.running) pollProbe()
    else load()
  }, 1000)
}
async function startProbe() {
  try {
    probe.value = await probeStart({})
    toast(t('maint.probeStarted'), 'ok')
    pollProbe()
  } catch (e) { toast(e.message, 'err') }
}
async function stopProbe() {
  try {
    probe.value = await probeCancel()
  } catch (e) { toast(e.message, 'err') }
}
const probeOne = (i) => one(() => probeSource(i.id), 'maint.probeOneDone')

/* 画质 / 字幕：结论 + 依据；未知不显示成「无」 */
function qualityLabel(i) {
  const h = Number(i.media_height || 0)
  if (!h) return ''
  return h >= 2160 ? '4K' : h + 'p'
}
function subLabel(i) {
  if (i.sub_zh === 'zh') return t('maint.srcSubZh')
  if (i.sub_zh === 'none') return t('maint.srcSubNone')
  return t('maint.srcSubUnknown')
}
function subTone(i) {
  if (i.sub_zh === 'zh') return 'ok'
  if (i.sub_zh === 'none') return ''
  return 'muted'
}
function subTitle(i) {
  const why = i.sub_evidence === 'hls_track' ? t('detail.srcSubWhyTrack')
    : i.sub_evidence === 'site_label' ? t('detail.srcSubWhySite')
      : i.sub_evidence === 'none' ? t('detail.srcSubWhyNone') : t('detail.srcSubWhyUnknown')
  return why + (i.media_probe_note ? '\n' + i.media_probe_note : '')
}

onMounted(async () => {
  await load()
  await loadPatrol()
  if (patrol.value && patrol.value.running) pollPatrol()
  await loadProbe()
  if (probe.value && probe.value.running) pollProbe()
})
onBeforeUnmount(() => {
  if (timer) clearTimeout(timer)
  if (patrolTimer) clearTimeout(patrolTimer)
  if (probeTimer) clearTimeout(probeTimer)
})
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      {{ $t('maint.sourcesTab') }}
      <span class="hint">{{ $t('maint.sourcesHint') }}</span>
    </div>
    <div class="panel-body">
      <div class="hstack wrap src-filters">
        <button
          v-for="[v, label, n] in FILTERS"
          :key="v || 'all'"
          class="btn tiny"
          :class="{ primary: status === v }"
          @click="setFilter(v)"
        >{{ $t(label) }} <span class="tabular">{{ n }}</span></button>
      </div>

      <div class="hstack wrap">
        <input class="inp" v-model="q" :placeholder="$t('maint.srcSearch')" style="max-width:260px" @keyup.enter="search" />
        <button class="btn tiny" :disabled="loading" @click="search">{{ $t('maint.srcSearchBtn') }}</button>
        <button class="btn tiny ghost" :disabled="loading" @click="load">{{ $t('maint.srcRefreshList') }}</button>
        <span class="spacer"></span>
        <button class="btn tiny" :disabled="patrol && patrol.running" @click="startPatrol">{{ $t('maint.patrolStart') }}</button>
        <button v-if="patrol && patrol.running" class="btn tiny ghost" @click="stopPatrol">{{ $t('common.cancel') }}</button>
        <button class="btn tiny" :disabled="probe && probe.running" :title="$t('maint.probeHint')" @click="startProbe">{{ $t('maint.probeStart') }}</button>
        <button v-if="probe && probe.running" class="btn tiny ghost" @click="stopProbe">{{ $t('common.cancel') }}</button>
        <label class="hstack sm muted">
          <input type="checkbox" :checked="pickedAll" @change="toggleAll($event.target.checked)" />
          {{ $t('maint.srcSelectAll') }}
        </label>
      </div>

      <div v-if="patrol && (patrol.running || patrol.done)" class="src-patrol">
        <div class="hstack wrap">
          <span class="muted sm">{{ patrol.running ? $t('maint.patrolRunning') : $t('maint.patrolDone') }}</span>
          <span class="tabular sm">{{ patrol.done }} / {{ patrol.total }}</span>
          <span class="muted sm ellipsis">{{ patrol.message }}</span>
        </div>
        <div class="p-bar"><i :style="{ width: Math.max(2, patrol.percent || 0) + '%' }"></i></div>
      </div>

      <div v-if="probe && (probe.running || probe.done)" class="src-patrol">
        <div class="hstack wrap">
          <span class="muted sm">{{ probe.running ? $t('maint.probeRunning') : $t('maint.probeDone') }}</span>
          <span class="tabular sm">{{ probe.done }} / {{ probe.total }}</span>
          <span class="muted sm ellipsis">{{ probe.message }}</span>
        </div>
        <div class="p-bar"><i :style="{ width: Math.max(2, probe.percent || 0) + '%' }"></i></div>
      </div>

      <div v-if="pickedIds.length" class="hstack wrap src-batch">
        <span class="muted sm">{{ $t('maint.srcPicked', { n: pickedIds.length }) }}</span>
        <button class="btn tiny" :disabled="busy" @click="batch('check')">{{ $t('maint.srcBatchCheck') }}</button>
        <button class="btn tiny" :disabled="busy" @click="batch('refresh')">{{ $t('maint.srcBatchRefresh') }}</button>
        <button class="btn tiny ghost" :disabled="busy" @click="batch('enable')">{{ $t('maint.srcEnable') }}</button>
        <button class="btn tiny ghost" :disabled="busy" @click="batch('disable')">{{ $t('maint.srcDisable') }}</button>
      </div>

      <div v-if="loading && !items.length" class="empty"><span class="spinner"></span></div>
      <div v-else-if="!items.length" class="empty">
        <div class="desc">{{ localMode ? $t('maint.srcEmptyLocal') : $t('maint.srcEmpty') }}</div>
      </div>
      <table v-else class="srctable">
        <thead>
          <tr>
            <th style="width:28px"></th>
            <th>{{ $t('maint.srcMovie') }}</th>
            <th>{{ $t('maint.srcSite') }}</th>
            <th>{{ $t('maint.srcStatus') }}</th>
            <th>{{ $t('maint.srcQuality') }}</th>
            <th>{{ $t('maint.srcLast') }}</th>
            <th>{{ $t('maint.srcExpires') }}</th>
            <th>{{ $t('maint.srcNote') }}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="i in items" :key="i.id" :class="{ off: !i.available }">
            <td><input type="checkbox" v-model="picked[i.id]" /></td>
            <td class="ellipsis">
              <a class="lnk" @click="openMovie(i)">{{ i.code || i.movie_title || ('#' + i.movie_id) }}</a>
              <div class="muted sm ellipsis">{{ i.movie_title }}</div>
            </td>
            <td class="ellipsis">
              {{ i.site || '—' }}
              <div v-if="i.page_label" class="muted sm ellipsis" :title="i.page_url">{{ i.page_label }}</div>
            </td>
            <td><span class="tag" :class="statusTone(i)">{{ statusText(i) }}</span></td>
            <td class="qcell">
              <span v-if="qualityLabel(i)" class="tag q">{{ qualityLabel(i) }}</span>
              <span class="tag" :class="subTone(i)" :title="subTitle(i)">{{ subLabel(i) }}</span>
            </td>
            <td class="muted sm t">{{ i.last_checked ? fmtDateTime(i.last_checked) : $t('maint.srcNever') }}</td>
            <td class="muted sm t">{{ i.expires_at ? fmtDateTime(i.expires_at) : '—' }}</td>
            <td class="srcnote ellipsis" :title="i.check_note">{{ i.check_note || '—' }}</td>
            <td class="hstack">
              <button class="btn tiny ghost" :disabled="busy || i.status === 'checking'" @click="checkOne(i)">{{ $t('maint.srcCheck') }}</button>
              <button class="btn tiny ghost" :disabled="busy || i.status === 'checking'" :title="$t('maint.probeHint')" @click="probeOne(i)">{{ $t('maint.probeOne') }}</button>
              <button class="btn tiny ghost" :disabled="busy || i.status === 'checking'" @click="refreshOne(i)">{{ $t('maint.srcRefresh') }}</button>
              <button class="btn tiny ghost" :disabled="busy" @click="toggleOne(i)">{{ i.available ? $t('maint.srcDisable') : $t('maint.srcEnable') }}</button>
              <a v-if="i.open_url" class="btn tiny ghost" :href="i.open_url" target="_blank" rel="noopener noreferrer">{{ $t('maint.srcOpen') }}</a>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="total" class="hstack src-pager">
        <button class="btn tiny ghost" :disabled="page <= 1" @click="goto(page - 1)">{{ $t('maint.srcPrev') }}</button>
        <span class="muted sm tabular">{{ page }} / {{ totalPages }} · {{ total }}</span>
        <button class="btn tiny ghost" :disabled="page >= totalPages" @click="goto(page + 1)">{{ $t('maint.srcNext') }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.src-filters { gap: var(--sp-2); margin-bottom: var(--sp-3); }
.src-batch {
  gap: var(--sp-2); margin: var(--sp-2) 0 0; padding: var(--sp-2);
  border: 1px solid var(--c-line); border-radius: var(--r-md);
}
.srctable { width: 100%; border-collapse: collapse; font-size: var(--fs-sm); margin-top: var(--sp-3); }
.srctable th, .srctable td {
  padding: var(--sp-2); border-bottom: 1px solid var(--c-line);
  text-align: left; vertical-align: top;
}
.srctable th { color: var(--c-text-3); font-weight: 500; font-size: var(--fs-xs); }
.srctable tr.off { opacity: .62; }
.srctable .srcnote { max-width: 250px; color: var(--c-text-2); }
.srctable td.t { white-space: nowrap; }
.srctable .lnk { cursor: pointer; text-decoration: underline; }
.srctable .hstack { gap: var(--sp-1); flex-wrap: wrap; }
/* 画质 / 字幕：紧凑两枚徽标，字幕结论带 tooltip 说明依据 */
.srctable .qcell { min-width: 96px; }
.srctable .tag.q { font-variant-numeric: tabular-nums; }
.tag.muted { color: var(--c-text-3); }
.tag {
  display: inline-block; padding: 1px 6px; border-radius: var(--r-md);
  border: 1px solid var(--c-line); font-size: var(--fs-xs); white-space: nowrap;
}
.tag.ok { border-color: var(--c-line); }
.tag.warn { color: var(--c-warn); border-color: var(--c-warn); }
.tag.err { color: var(--c-err); border-color: var(--c-err); }
.src-pager { gap: var(--sp-3); margin-top: var(--sp-3); }
/* 巡检进度：长任务统一用细进度条，文案在上一行，避免挤压筛选区 */
.src-patrol { margin-top: var(--sp-2); }
.src-patrol .p-bar {
  height: 4px; margin-top: 4px; border-radius: 2px;
  background: var(--c-line); overflow: hidden;
}
.src-patrol .p-bar i { display: block; height: 100%; background: var(--c-accent); transition: width .3s; }
.hint { font-size: var(--fs-xs); color: var(--c-text-3); font-weight: 400; margin-left: var(--sp-2); }
</style>
