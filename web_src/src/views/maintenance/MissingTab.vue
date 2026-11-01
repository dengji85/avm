<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { missingOverview, resolveMissing, archiveList, restoreArchived, coverThumbUrl } from '../../api.js'
import { toast, confirmDialog } from '../../utils.js'
import { t } from '../../i18n/index.js'

/* 文件缺失处置面板。
   三种处置的语义差别必须让用户看得懂：
   · 归档   —— 记录与历史都留着，只是从列表藏起来（可恢复）
   · 移除记录 —— 确认文件不会回来，删掉缺失记录；影片一并归档，避免变成空壳
   · 彻底删除 —— 先写历史快照，再删影片；不可恢复
   离线位置下的缺失（盘没插）一律不允许处置，避免误删。 */

const data = ref(null)
const loading = ref(false)
const busy = ref(false)
const picked = reactive({})
const tab = ref('pending')      // pending | offline | archive
const arch = ref(null)
const archLoading = ref(false)

async function load() {
  loading.value = true
  try { data.value = await missingOverview() } catch (e) { toast(e.message, 'err') } finally { loading.value = false }
}
async function loadArchive() {
  archLoading.value = true
  try { arch.value = await archiveList({ size: 100 }) } catch (e) { toast(e.message, 'err') } finally { archLoading.value = false }
}

const items = computed(() => (data.value && data.value.items) || [])
const pendingItems = computed(() => items.value.filter((i) => !i.offline))
const offlineItems = computed(() => items.value.filter((i) => i.offline))
const list = computed(() => (tab.value === 'offline' ? offlineItems.value : pendingItems.value))
const downRoots = computed(() => (data.value && data.value.roots_down) || [])
const graceDays = computed(() => (data.value && data.value.grace_days) || 0)

const pickedIds = computed(() => list.value.filter((i) => picked[i.movie_id]).map((i) => i.movie_id))
const pickedAll = computed(() => list.value.length > 0 && pickedIds.value.length === list.value.length)
const pickedHist = computed(() => list.value.filter((i) => picked[i.movie_id] && i.has_history).length)

function toggleAll(v) { list.value.forEach((i) => { picked[i.movie_id] = v }) }
function switchTab(id) {
  tab.value = id
  if (id === 'archive' && !arch.value) loadArchive()
}

async function act(action) {
  const ids = pickedIds.value.slice()
  if (!ids.length) return
  const n = ids.length
  const h = pickedHist.value
  let opt = { danger: false }
  if (action === 'archive') {
    opt = { danger: false, okText: t('maint.missArchive'),
            title: t('maint.missArchiveTitle', { n }), desc: t('maint.missArchiveDesc') }
  } else if (action === 'forget') {
    opt = { danger: true, okText: t('maint.missForget'),
            title: t('maint.missForgetTitle', { n }), desc: t('maint.missForgetDesc') }
  } else {
    opt = { danger: true, okText: t('maint.missPurge'),
            title: t('maint.missPurgeTitle', { n }), desc: t('maint.missPurgeDesc', { n, h }) }
  }
  const ok = await confirmDialog(opt.title, opt.desc, { danger: opt.danger, okText: opt.okText })
  if (!ok) return
  busy.value = true
  try {
    const r = await resolveMissing({ ids, action })
    const msg = action === 'purge'
      ? t('maint.missPurged', { n: r.deleted || 0, s: r.snapshots || 0 })
      : action === 'forget'
        ? t('maint.missForgot', { n: r.removed_files || 0, a: r.archived || 0 })
        : t('maint.missArchived', { n: r.archived || 0 })
    toast(msg, 'ok')
    ids.forEach((id) => { delete picked[id] })
    await load()
  } catch (e) { toast(e.message, 'err') } finally { busy.value = false }
}

async function doRestore(m) {
  try {
    await restoreArchived(m.id)
    toast(t('maint.missRestored', { n: 1 }), 'ok')
    await loadArchive(); await load()
  } catch (e) { toast(e.message, 'err') }
}

onMounted(load)
</script>

<template>
  <div class="mt">
    <div class="mt-head">
      <div class="mt-tabs">
        <button class="mt-tab" :class="{ on: tab === 'pending' }" @click="switchTab('pending')">
          {{ $t('maint.missPending') }} <b>{{ pendingItems.length }}</b>
        </button>
        <button class="mt-tab" :class="{ on: tab === 'offline' }" @click="switchTab('offline')">
          {{ $t('maint.missOffline') }} <b>{{ offlineItems.length }}</b>
        </button>
        <button class="mt-tab" :class="{ on: tab === 'archive' }" @click="switchTab('archive')">
          {{ $t('maint.missArchivedTab') }} <b>{{ (arch && arch.total) || 0 }}</b>
        </button>
      </div>
      <button class="btn tiny" :disabled="loading" @click="tab === 'archive' ? loadArchive() : load()">
        {{ loading || archLoading ? $t('maint.checking') : $t('maint.missRefresh') }}
      </button>
    </div>

    <div v-if="downRoots.length" class="mt-warn">
      <span>⚠ {{ $t('maint.rootsDown', { n: downRoots.length }) }}</span>
      <span class="mt-warn-hint">{{ $t('maint.rootsDownHint') }}</span>
    </div>

    <!-- 待处置 / 离线等待 -->
    <template v-if="tab !== 'archive'">
      <div v-if="!list.length && !loading" class="empty compact">
        <div class="desc">{{ $t('maint.missEmpty') }}</div>
      </div>
      <div v-else class="mt-body">
        <label class="mt-all">
          <input type="checkbox" :checked="pickedAll" @change="toggleAll($event.target.checked)" />
          <span>{{ $t('maint.missPickAll') }}</span>
        </label>
        <div v-for="m in list" :key="m.movie_id" class="mt-row" :class="{ off: m.offline }">
          <input type="checkbox" :disabled="m.offline" :checked="!!picked[m.movie_id]"
                 @change="picked[m.movie_id] = $event.target.checked" />
          <div class="mt-cover" :style="m.cover ? { backgroundImage: `url(${coverThumbUrl(m.movie_id, 120)})` } : {}">
            <span v-if="!m.cover">{{ (m.code || '—').slice(0, 7) }}</span>
          </div>
          <div class="mt-main">
            <div class="mt-title">
              <code v-if="m.code">{{ m.code }}</code>
              <span class="ellipsis">{{ m.title || $t('maint.missNoTitle') }}</span>
            </div>
            <div class="mt-meta">
              <span>{{ $t('maint.missFiles', { n: m.missing_files, t: m.total_files }) }}</span>
              <span v-if="m.days">{{ $t('maint.missDays', { n: m.days }) }}</span>
              <span v-if="m.offline" class="tag off">{{ $t('maint.missOfflineTag') }}</span>
              <span v-else-if="m.expired" class="tag exp">{{ $t('maint.missExpired') }}</span>
              <span v-else class="tag gr">{{ $t('maint.missGrace', { n: Math.max(0, graceDays - m.days) }) }}</span>
              <span v-if="m.has_history" class="tag hist">{{ $t('maint.missHistory', { n: m.play_count }) }}</span>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 已归档 -->
    <template v-else>
      <div v-if="!((arch && arch.items) || []).length && !archLoading" class="empty compact">
        <div class="desc">{{ $t('maint.missArchiveEmpty') }}</div>
      </div>
      <div v-else class="mt-body">
        <div v-for="m in (arch && arch.items) || []" :key="m.id" class="mt-row">
          <div class="mt-cover" :style="m.cover ? { backgroundImage: `url(${coverThumbUrl(m.id, 120)})` } : {}">
            <span v-if="!m.cover">{{ (m.code || '—').slice(0, 7) }}</span>
          </div>
          <div class="mt-main">
            <div class="mt-title">
              <code v-if="m.code">{{ m.code }}</code>
              <span class="ellipsis">{{ m.title || $t('maint.missNoTitle') }}</span>
            </div>
            <div class="mt-meta">
              <span>{{ $t('maint.missArchivedAt') }} {{ m.archived_at }}</span>
              <span v-if="m.play_count" class="tag hist">{{ $t('maint.missHistory', { n: m.play_count }) }}</span>
            </div>
          </div>
          <button class="btn tiny" @click="doRestore(m)">{{ $t('maint.missRestore') }}</button>
        </div>
      </div>
    </template>

    <!-- 批量操作条 -->
    <div v-if="tab !== 'archive' && pickedIds.length" class="mt-bulk">
      <span>{{ $t('maint.missPicked', { n: pickedIds.length, h: pickedHist }) }}</span>
      <div class="spacer"></div>
      <button class="btn tiny" :disabled="busy" @click="act('archive')">{{ $t('maint.missArchive') }}</button>
      <button class="btn tiny" :disabled="busy" @click="act('forget')">{{ $t('maint.missForget') }}</button>
      <button class="btn tiny danger" :disabled="busy" @click="act('purge')">{{ $t('maint.missPurge') }}</button>
    </div>
  </div>
</template>

<style scoped>
.mt { display: flex; flex-direction: column; gap: var(--sp-3); }
.mt-head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.mt-tabs { display: flex; gap: var(--sp-1); flex-wrap: wrap; flex: 1; }
.mt-tab { display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; border-radius: var(--r-sm); border: 1px solid var(--c-line); background: var(--c-surface-2); color: var(--c-text-2); font-size: var(--fs-sm); cursor: pointer; }
.mt-tab.on { border-color: var(--c-primary); color: var(--c-primary); background: var(--c-primary-soft); }
.mt-tab b { font-family: var(--font-mono, monospace); font-weight: 700; }
.mt-warn { display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center; padding: var(--sp-2) var(--sp-3); border-radius: var(--r-sm); border: 1px solid var(--c-warn); background: color-mix(in srgb, var(--c-warn) 10%, transparent); font-size: var(--fs-sm); }
.mt-warn-hint { color: var(--c-text-3); font-size: var(--fs-xs); }
.mt-body { display: flex; flex-direction: column; gap: 2px; }
.mt-all { display: flex; align-items: center; gap: 6px; font-size: var(--fs-xs); color: var(--c-text-3); padding: 2px 4px; }
.mt-row { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-2); border-radius: var(--r-sm); border: 1px solid transparent; }
.mt-row:hover { background: var(--c-surface-2); border-color: var(--c-line); }
.mt-row.off { opacity: .72; }
.mt-cover { flex: none; width: 44px; height: 60px; border-radius: var(--r-sm); background: var(--c-surface-2) center/cover no-repeat; display: grid; place-items: center; font-size: 10px; color: var(--c-text-3); overflow: hidden; }
.mt-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.mt-title { display: flex; align-items: center; gap: 8px; min-width: 0; font-size: var(--fs-md); }
.mt-title code { flex: none; font-family: var(--font-mono, monospace); font-size: var(--fs-xs); color: var(--c-text-2); background: var(--c-surface-2); padding: 1px 5px; border-radius: 4px; }
.mt-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: var(--fs-xs); color: var(--c-text-3); }
.tag { padding: 1px 6px; border-radius: 4px; border: 1px solid var(--c-line); }
.tag.off { color: var(--c-text-2); border-color: var(--c-line-strong); }
.tag.exp { color: var(--c-err); border-color: var(--c-err); }
.tag.gr { color: var(--c-warn); border-color: var(--c-warn); }
.tag.hist { color: var(--c-primary); border-color: var(--c-primary); }
.mt-bulk { position: sticky; bottom: 0; display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3); border-radius: var(--r-md); border: 1px solid var(--c-line-strong); background: var(--c-surface); box-shadow: var(--sh-2); font-size: var(--fs-sm); }
@media (max-width: 720px) {
  .mt-row { flex-wrap: wrap; }
  .mt-bulk { flex-wrap: wrap; }
}
</style>
