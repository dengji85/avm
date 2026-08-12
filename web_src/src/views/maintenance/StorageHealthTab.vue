<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { state } from '../../state.js'
import { getStorage, getIntegrity, getHealthCheck, getDedup, resolveDedup, getQuality, sniffCovers } from '../../api.js'
import { toast, confirmDialog, fmtSize } from '../../utils.js'
import { t } from '../../i18n/index.js'
import PageHead from '../../components/PageHead.vue'
import SectionTitle from '../../components/SectionTitle.vue'

const st = reactive({ total: {}, by_disk: [], by_studio: [], by_year: [] })
const iss = reactive({ missing_files: 0, missing_cover: 0, unrecognized: 0 })
const health = ref(null)
const dedup = ref(null)
const quality = ref(null)
const loading = ref(false)
const healthLoading = ref(false)
const dedupLoading = ref(false)
const qualityLoading = ref(false)
const openGroup = ref({})
const qOpen = ref({})

const HEALTH_GROUPS = [
  ['missing_files', 'maint.hMissingFiles', 'maint.hMissingFilesDesc', 'err'],
  ['split_incomplete', 'maint.hSplit', 'maint.hSplitDesc', 'warn'],
  ['missing_cover', 'maint.hMissingCover', 'maint.hMissingCoverDesc', 'warn'],
  ['unrecognized', 'maint.hUnrecognized', 'maint.hUnrecognizedDesc', 'warn'],
  ['duplicates', 'maint.hDuplicates', 'maint.hDuplicatesDesc', 'accent'],
]

async function load() {
  loading.value = true
  try {
    const [s, i] = await Promise.all([getStorage(), getIntegrity()])
    st.total = s.total || {}
    st.by_disk = s.by_disk || []; st.by_studio = s.by_studio || []; st.by_year = s.by_year || []
    iss.missing_files = i.missing_files || 0; iss.missing_cover = i.missing_cover || 0; iss.unrecognized = i.unrecognized || 0
  } catch (e) { toast(e.message, 'err') } finally { loading.value = false }
}
async function runHealth() { healthLoading.value = true; try { health.value = await getHealthCheck(); toast(t('maint.healthDone'), 'ok') } catch (e) { toast(e.message, 'err') } finally { healthLoading.value = false } }
async function runDedup() { dedupLoading.value = true; try { dedup.value = await getDedup(); toast(t('maint.detectDone'), 'ok') } catch (e) { toast(e.message, 'err') } finally { dedupLoading.value = false } }
async function runQuality() { qualityLoading.value = true; try { quality.value = await getQuality(); toast(t('maint.qualityDone'), 'ok') } catch (e) { toast(e.message, 'err') } finally { qualityLoading.value = false } }

async function keepFile(kind, fileId, group) {
  const ok = await confirmDialog(t('maint.keepFileTitle'), t('maint.keepFileDesc', { n: (group.files || []).length - 1 }), { danger: true, okText: t('maint.confirmClean') })
  if (!ok) return
  try { const r = await resolveDedup({ kind, keep_file_id: fileId, delete_files: true }); toast(t('maint.cleaned', { n: r.removed || 0 }), 'ok'); await runDedup(); await load() } catch (e) { toast(e.message, 'err') }
}
async function cleanQuality(it) {
  if (it.keep_file_id) {
    const ok = await confirmDialog(t('maint.cleanLowTitle'), t('maint.cleanLowDesc', { keep: fmtSize(it.keep_size), cur: fmtSize(it.size) }), { danger: true, okText: t('maint.confirmClean') })
    if (!ok) return
    try { const r = await resolveDedup({ kind: 'version', keep_file_id: it.keep_file_id, delete_files: true }); toast(t('maint.cleaned', { n: r.removed || 0 }), 'ok'); await runQuality(); await load() } catch (e) { toast(e.message, 'err') }
  } else {
    const ok = await confirmDialog(t('maint.delRecTitle'), t('maint.delRecDesc', { path: it.path || it.title || '' }), { danger: true, okText: t('maint.confirmDelete') })
    if (!ok) return
    try { const r = await resolveDedup({ kind: 'exact', keep_file_id: it.file_id, delete_files: true }); toast(t('maint.cleaned', { n: r.removed || 0 }), 'ok'); await runQuality(); await load() } catch (e) { toast(e.message, 'err') }
  }
}
async function doSniff() { try { const r = await sniffCovers(); toast(t('maint.sniffMatched', { n: (r && r.matched) || 0 }), 'ok'); await load() } catch (e) { toast(e.message, 'err') } }

const QUALITY_GROUPS = [
  ['ad', 'maint.qAd', 'maint.qAdDesc', 'warn'],
  ['low_bitrate', 'maint.qLowBitrate', 'maint.qLowBitrateDesc', 'warn'],
  ['version_loser', 'maint.qVersionLoser', 'maint.qVersionLoserDesc', 'accent'],
  ['broken', 'maint.qBroken', 'maint.qBrokenDesc', 'err'],
]
const qCount = (k) => (quality.value && quality.value.counts && quality.value.counts[k]) || 0
const qList = (k) => (quality.value && quality.value[k]) || []

const cards = computed(() => [
  { v: st.total.movies || 0, l: 'maint.ovTotal' },
  { v: st.total.files || 0, l: 'maint.distFiles' },
  { v: fmtSize(st.total.bytes), l: 'maint.distTotal' },
  { v: iss.missing_files, l: 'maint.hMissingFiles', tone: iss.missing_files ? 'err' : 'ok' },
  { v: iss.missing_cover, l: 'maint.hMissingCover', tone: iss.missing_cover ? 'warn' : 'ok' },
  { v: iss.unrecognized, l: 'maint.hUnrecognized', tone: iss.unrecognized ? 'warn' : 'ok' },
])
function bars(list, key, labelKey) {
  if (!list || !list.length) return []
  const max = Math.max(...list.map((x) => Number(x[key]) || 0), 1)
  return list.slice(0, 12).map((x) => ({ label: x[labelKey] || '未知', pct: Math.max(2, ((Number(x[key]) || 0) / max) * 100), text: key === 'bytes' ? fmtSize(x[key]) : `${x[key]} 部`, raw: x }))
}
const diskBars = computed(() => bars(st.by_disk, 'bytes', 'drive'))
const studioBars = computed(() => bars(st.by_studio, 'bytes', 'studio'))
const yearBars = computed(() => bars(st.by_year, 'bytes', 'year'))
const healthCount = (k) => (health.value && health.value.counts && health.value.counts[k]) || 0
const healthList = (k) => (health.value && health.value[k]) || []

onMounted(load)
</script>

<template>
  <div class="sh">
    <SectionTitle :title="$t('maint.overview')" />
    <div class="stat-cards">
      <div v-for="c in cards" :key="c.l" class="stat-card" :class="c.tone">
        <div class="stat-value">{{ c.v }}</div>
        <div class="stat-label">{{ $t(c.l) }}</div>
      </div>
    </div>

    <SectionTitle :title="$t('maint.distTitle')" />
    <div class="dist-grid">
      <div class="panel">
        <div class="panel-head">{{ $t('maint.distDisk') }} <span class="sub">{{ $t('maint.byCapacity') }}</span></div>
        <div class="panel-body">
          <div v-for="b in diskBars" :key="b.label" class="bar-row"><div class="bl">{{ b.label }}</div><div class="bar-track"><div class="bar-fill" :style="{ width: b.pct + '%' }"></div></div><div class="bv">{{ b.text }}</div></div>
          <p v-if="!diskBars.length" class="muted">{{ $t('maint.noData') }}</p>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head">{{ $t('maint.distStudio') }} <span class="sub">{{ $t('maint.top12') }}</span></div>
        <div class="panel-body">
          <div v-for="b in studioBars" :key="b.label" class="bar-row"><div class="bl" :title="b.label">{{ b.label }}</div><div class="bar-track"><div class="bar-fill" :style="{ width: b.pct + '%' }"></div></div><div class="bv">{{ b.text }}</div></div>
          <p v-if="!studioBars.length" class="muted">{{ $t('maint.noData') }}</p>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head">{{ $t('maint.distYear') }}</div>
        <div class="panel-body">
          <div v-for="b in yearBars" :key="b.label" class="bar-row"><div class="bl">{{ b.label }}</div><div class="bar-track"><div class="bar-fill" :style="{ width: b.pct + '%' }"></div></div><div class="bv">{{ b.text }}</div></div>
          <p v-if="!yearBars.length" class="muted">{{ $t('maint.noData') }}</p>
        </div>
      </div>
    </div>

    <!-- 体检 -->
    <div class="panel">
      <div class="panel-head">
        {{ $t('maint.healthCheck') }}
        <span class="sub">{{ $t('maint.healthCheckSub') }}</span>
        <div class="spacer"></div>
        <button class="btn tiny primary" :disabled="healthLoading" @click="runHealth">{{ healthLoading ? $t('maint.checking') : (health ? $t('maint.rerunHealth') : $t('maint.runHealth')) }}</button>
      </div>
      <div class="panel-body">
        <div v-if="!health && !healthLoading" class="empty compact"><div class="desc">{{ $t('maint.clickToHealth') }}</div></div>
        <div v-else-if="healthLoading" class="empty compact"><span class="spinner large"></span></div>
        <template v-else>
          <div class="hs-row">
            <div v-for="[k, label, , tone] in HEALTH_GROUPS" :key="k" class="hs-card" :class="[healthCount(k) ? tone : 'ok', { on: openGroup[k] }]" @click="openGroup[k] = !openGroup[k]">
              <b class="tabular">{{ healthCount(k) }}</b><span>{{ $t(label) }}</span>
            </div>
          </div>
          <div v-for="[k, label, desc] in HEALTH_GROUPS" :key="'d' + k">
            <div v-if="openGroup[k] && healthList(k).length" class="hd-group">
              <div class="hd-title">{{ $t(label) }} <span class="muted">· {{ $t(desc) }}</span></div>
              <ul class="hd-list">
                <li v-for="(it, i) in healthList(k).slice(0, 60)" :key="i" @click="it.id && (state.currentId = it.id)">
                  <template v-if="k === 'duplicates'">影片 #{{ it.a }} ↔ #{{ it.b }} · {{ fmtSize(it.size) }}</template>
                  <template v-else><b v-if="it.code" class="badge code">{{ it.code }}</b><span class="ellipsis">{{ it.title || it.path }}</span></template>
                </li>
              </ul>
              <p v-if="healthList(k).length > 60" class="muted sm">{{ $t('maint.onlyShow', { n: 60, total: healthList(k).length }) }}</p>
            </div>
          </div>
        </template>
      </div>
    </div>

    <!-- 去重 -->
    <div class="panel">
      <div class="panel-head">
        {{ $t('maint.dedupTitle') }}
        <span class="sub">{{ $t('maint.dedupSub') }}</span>
        <div class="spacer"></div>
        <button class="btn tiny" :disabled="dedupLoading" @click="runDedup">{{ dedupLoading ? $t('maint.checking') : (dedup ? $t('maint.rerunDetect') : $t('maint.detectDup')) }}</button>
      </div>
      <div class="panel-body">
        <div v-if="!dedup && !dedupLoading" class="empty compact"><div class="desc">{{ $t('maint.clickToDedup') }}</div></div>
        <div v-else-if="dedupLoading" class="empty compact"><span class="spinner large"></span></div>
        <template v-else>
          <div class="dd-sec">
            <div class="dd-h">{{ $t('maint.exactSame') }} <span class="badge" :class="dedup.exact_groups ? 'warn' : 'ok'">{{ dedup.exact_groups || 0 }} {{ $t('maint.groups') }} · {{ $t('maint.canClean') }} {{ dedup.exact_redundant || 0 }} {{ $t('maint.files') }}</span></div>
            <p v-if="!(dedup.exact || []).length" class="muted sm">{{ $t('maint.noExactDup') }}</p>
            <div v-for="(g, gi) in dedup.exact || []" :key="'e' + gi" class="dd-group">
              <div class="dg-head">{{ fmtSize(g.size) }} × {{ (g.files || []).length }} {{ $t('maint.files') }}</div>
              <div v-for="f in g.files" :key="f.id" class="dg-file">
                <span class="dg-path ellipsis" :title="f.path">{{ f.path }}</span>
                <span class="badge">{{ f.resolution || '—' }}</span>
                <button class="btn tiny" @click="keepFile('exact', f.id, g)">{{ $t('maint.keepThis') }}</button>
              </div>
            </div>
          </div>
          <div class="dd-sec">
            <div class="dd-h">{{ $t('maint.sameCodeVer') }} <span class="badge" :class="dedup.version_groups ? 'accent' : 'ok'">{{ dedup.version_groups || 0 }} {{ $t('maint.groups') }} · {{ $t('maint.canMerge') }} {{ dedup.version_redundant || 0 }} {{ $t('maint.files') }}</span></div>
            <p v-if="!(dedup.version || []).length" class="muted sm">{{ $t('maint.noVersionDup') }}</p>
            <div v-for="(g, gi) in dedup.version || []" :key="'v' + gi" class="dd-group" :class="{ 'mp': g.multi_part }">
              <div class="dg-head"><b class="badge code">{{ g.code || '?' }}</b> {{ g.title || '' }}
                <span v-if="g.multi_part" class="badge mp" :title="$t('maint.volumeTip')">{{ $t('maint.volume') }}</span>
              </div>
              <div v-for="f in g.files" :key="f.id" class="dg-file">
                <span class="dg-path ellipsis" :title="f.path">{{ f.path }}</span>
                <span class="badge">{{ f.resolution || '—' }}</span>
                <button v-if="!g.multi_part" class="btn tiny" @click="keepFile('version', f.id, g)">{{ $t('maint.keepThis') }}</button>
                <span v-else class="muted sm">{{ $t('maint.volume') }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>

    <!-- 劣质片 -->
    <div class="panel">
      <div class="panel-head">
        {{ $t('maint.qualityTitle') }}
        <span class="sub">{{ $t('maint.qualitySub') }}</span>
        <div class="spacer"></div>
        <button class="btn tiny" :disabled="qualityLoading" @click="runQuality">{{ qualityLoading ? $t('maint.checking') : (quality ? $t('maint.rerunScreen') : $t('maint.startScreen')) }}</button>
      </div>
      <div class="panel-body">
        <div v-if="!quality && !qualityLoading" class="empty compact"><div class="desc">{{ $t('maint.clickToScreen') }}</div></div>
        <div v-else-if="qualityLoading" class="empty compact"><span class="spinner large"></span></div>
        <template v-else>
          <div class="hs-row">
            <div v-for="[k, label, , tone] in QUALITY_GROUPS" :key="k" class="hs-card" :class="[qCount(k) ? tone : 'ok', { on: qOpen[k] }]" @click="qOpen[k] = !qOpen[k]">
              <b class="tabular">{{ qCount(k) }}</b><span>{{ $t(label) }}</span>
            </div>
            <div class="hs-card ok"><b class="tabular">{{ (quality.counts && quality.counts.total_flagged) || 0 }}</b><span>{{ $t('maint.totalPending') }}</span></div>
          </div>
          <div v-for="[k, label, desc, tone] in QUALITY_GROUPS" :key="'q' + k">
            <div v-if="qOpen[k] && qList(k).length" class="hd-group">
              <div class="hd-title">{{ $t(label) }} <span class="muted">· {{ $t(desc) }}</span><span class="badge" :class="tone === 'err' ? 'err' : tone === 'accent' ? 'accent' : 'warn'">{{ qCount(k) }}</span></div>
              <ul class="hd-list">
                <li v-for="(it, i) in qList(k).slice(0, 80)" :key="i" class="q-item">
                  <b v-if="it.code" class="badge code">{{ it.code }}</b>
                  <span class="q-title ellipsis" :title="it.title || it.path">{{ it.title || it.path }}</span>
                  <span class="badge">{{ it.resolution || '—' }}</span>
                  <span class="q-size">{{ fmtSize(it.size) }}</span>
                  <button class="btn tiny danger" @click="cleanQuality(it)">{{ $t('maint.clean') }}</button>
                </li>
              </ul>
              <p v-if="qList(k).length > 80" class="muted sm">{{ $t('maint.onlyShow', { n: 80, total: qList(k).length }) }}</p>
            </div>
          </div>
        </template>
      </div>
    </div>

    <div class="foot-actions">
      <button class="btn tiny" @click="doSniff">{{ $t('maint.matchCovers') }}</button>
    </div>
  </div>
</template>

<style scoped>
.sh { display: flex; flex-direction: column; gap: var(--sp-4); }
.dist-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: var(--sp-4); }
.empty.compact { padding: var(--sp-6) var(--sp-4); }
.hs-row { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.hs-card { display: flex; flex-direction: column; align-items: center; min-width: 92px; padding: var(--sp-2) var(--sp-3); border-radius: var(--r-md); background: var(--c-surface-2); border: 1px solid var(--c-line); cursor: pointer; transition: all var(--t-fast); }
.hs-card:hover { border-color: var(--c-line-strong); }
.hs-card.on { border-color: var(--c-primary); }
.hs-card b { font-size: var(--fs-xl); font-weight: 650; } .hs-card span { font-size: var(--fs-xs); color: var(--c-text-3); }
.hs-card.ok b { color: var(--c-ok); } .hs-card.err b { color: var(--c-err); } .hs-card.warn b { color: var(--c-warn); } .hs-card.accent b { color: var(--c-accent); }
.hd-group { margin-top: var(--sp-3); }
.hd-title { font-size: var(--fs-md); font-weight: 600; margin-bottom: var(--sp-2); display: flex; align-items: center; gap: var(--sp-2); }
.hd-list { display: flex; flex-direction: column; gap: 3px; max-height: 320px; overflow-y: auto; list-style: none; margin: 0; padding: 0; }
.hd-list li { display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-1) var(--sp-2); border-radius: var(--r-xs); background: var(--c-surface-2); font-size: var(--fs-sm); color: var(--c-text-2); min-width: 0; cursor: pointer; }
.hd-list li:hover { background: var(--c-surface-3); }
.dd-sec { margin-bottom: var(--sp-4); }
.dd-h { display: flex; align-items: center; gap: var(--sp-2); font-weight: 600; margin-bottom: var(--sp-2); }
.dd-group { margin-bottom: var(--sp-3); border: 1px solid var(--c-line); border-radius: var(--r-md); overflow: hidden; }
.dg-head { padding: var(--sp-2) var(--sp-3); background: var(--c-surface-2); font-size: var(--fs-sm); display: flex; align-items: center; gap: var(--sp-2); }
.dg-file { display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3); border-top: 1px solid var(--c-line); font-size: var(--fs-sm); }
.dg-path { flex: 1; min-width: 0; color: var(--c-text-2); font-family: 'JetBrains Mono', monospace; font-size: var(--fs-xs); }
.sm { font-size: var(--fs-sm); }
.q-item { gap: var(--sp-2); } .q-title { flex: 1; min-width: 0; color: var(--c-text-2); font-size: var(--fs-sm); } .q-size { font-family: 'JetBrains Mono', monospace; font-size: var(--fs-xs); color: var(--c-text-3); }
.badge.err { background: var(--c-err-soft); color: var(--c-err); } .badge.accent { background: var(--c-accent-soft); color: var(--c-accent); }
.badge.mp { background: color-mix(in srgb, var(--c-ok) 16%, transparent); color: var(--c-ok); }
.badge.code { font-family: var(--font-mono, monospace); background: var(--c-accent-soft); color: var(--c-accent); }
.foot-actions { display: flex; gap: var(--sp-2); }
</style>
