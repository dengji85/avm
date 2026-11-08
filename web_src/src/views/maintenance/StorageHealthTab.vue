<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { state } from '../../state.js'
import { getStorage, getIntegrity, getHealthCheck, getDedup, resolveDedup, getQuality, sniffCovers, getSoftwareStorage, cleanSoftwareStorage, checkMissing, storageRoots, getOrphans, cleanOrphans, normalizeGenres } from '../../api.js'
import { toast, confirmDialog, fmtSize } from '../../utils.js'
import { t } from '../../i18n/index.js'
import PageHead from '../../components/PageHead.vue'

const st = reactive({ total: {} })
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
    iss.missing_files = i.missing_files || 0; iss.missing_cover = i.missing_cover || 0; iss.unrecognized = i.unrecognized || 0
  } catch (e) { toast(e.message, 'err') } finally { loading.value = false }
}
async function runHealth() { healthLoading.value = true; try { health.value = await getHealthCheck(); toast(t('maint.healthDone'), 'ok') } catch (e) { toast(e.message, 'err') } finally { healthLoading.value = false } }
// 快速核对缺失：只检查已入库路径是否还在（不遍历目录），比全盘扫描快得多
const checkMissingLoading = ref(false)
async function runCheckMissing() {
  checkMissingLoading.value = true
  try {
    const r = await checkMissing()
    // 后端会跳过「离线存储位置」下的文件，并顺手复活插回来的盘上的记录
    const extra = []
    if (r.restored) extra.push(t('maint.checkMissingRestored', { n: r.restored }))
    if (r.skipped_offline) extra.push(t('maint.checkMissingSkipped', { n: r.skipped_offline }))
    const base = t('maint.checkMissingDone', { n: r.checked || 0, m: r.marked || 0 })
    toast(extra.length ? `${base} · ${extra.join('，')}` : base, 'ok')
    if (r.roots) rootStates.value = r.roots
    await load()
  } catch (e) { toast(e.message, 'err') } finally { checkMissingLoading.value = false }
}

/* 类型归一：同一题材被不同源写成多个名字（絲襪 / パンスト / stockings），
   入库时已会自动收敛，这里修的是早先存下来的历史数据。 */
const gnBusy = ref(false)
async function runMergeGenres() {
  gnBusy.value = true
  try {
    const r = await normalizeGenres()
    toast(t('maint.mergeGenresDone', { g: r.groups || 0, n: r.removed || 0 }), 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') } finally { gnBusy.value = false }
}

/* 孤儿与残留：正常删除影片时媒体文件会一起清掉，这里只兜底「历史遗留 / 手动改库 /
   程序异常退出」。磁盘文件在删除前会按库内引用重新核对一遍，不依赖前端这份统计。 */
const orphans = ref(null)
const orphanBusy = ref(false)
// 与后端 orphan_media_stats 的键一一对应；db 那一项是数据库悬空行，不带体积
const ORPHAN_KINDS = [
  { key: 'covers', label: 'maint.orphanCovers', withSize: true },
  { key: 'fanarts', label: 'maint.orphanFanarts', withSize: true },
  { key: 'previews', label: 'maint.orphanPreviews', withSize: true },
  { key: 'subs', label: 'maint.orphanSubs', withSize: true },
  { key: 'adcheck', label: 'maint.orphanAdcheck', withSize: true },
  { key: 'remux', label: 'maint.orphanRemux', withSize: true },
  { key: 'db', label: 'maint.orphanDb' },
]
async function loadOrphans() {
  try { orphans.value = await getOrphans() } catch (e) { orphans.value = null }
}
function orphanCount(kind) {
  const o = orphans.value
  if (!o) return 0
  if (kind === 'db') {
    const d = o.dangling || {}
    return (d.ai_qa || 0) + (d.ai_jobs || 0) + (d.scrape_skip || 0) + (d.collection_cover || 0)
  }
  return ((o[kind] && o[kind].files) || 0)
}
const orphanBytes = (kind) => ((orphans.value && orphans.value[kind] && orphans.value[kind].bytes) || 0)
const orphanFiles = computed(() => (orphans.value && orphans.value.total && orphans.value.total.files) || 0)
const orphanSize = computed(() => (orphans.value && orphans.value.total && orphans.value.total.bytes) || 0)
const orphanHasAny = computed(() => orphanFiles.value > 0 || orphanCount('db') > 0)

/* 清理成功后就地刷新两个面板：孤儿少了，「软件数据占用」的数字也变了 */
async function afterOrphanClean(r) {
  const dbN = Object.values(r.db || {}).reduce((a, b) => a + (Number(b) || 0), 0)
  const mediaN = ORPHAN_KINDS.filter((k) => k.key !== 'db')
    .reduce((a, k) => a + (Number(r[k.key]) || 0), 0)
  toast(t('maint.orphanCleaned', { n: dbN + mediaN }), 'ok')
  await loadOrphans()
  await loadSoftware()
}
async function cleanOrph(kind) {
  const n = orphanCount(kind)
  if (!n) return
  const ok = await confirmDialog(t('maint.orphanCleanTitle'),
    t('maint.orphanCleanDesc', { n }), { danger: true })
  if (!ok) return
  orphanBusy.value = true
  try {
    await afterOrphanClean(await cleanOrphans({ targets: [kind] }))
  } catch (e) { toast(e.message, 'err') } finally { orphanBusy.value = false }
}
async function cleanAllOrphans() {
  const kinds = ORPHAN_KINDS.filter((k) => orphanCount(k.key)).map((k) => k.key)
  if (!kinds.length) return
  const ok = await confirmDialog(t('maint.orphanCleanAllTitle'),
    t('maint.orphanCleanAllDesc', { n: orphanFiles.value, s: fmtSize(orphanSize.value) }),
    { danger: true, okText: t('maint.confirmClean') })
  if (!ok) return
  orphanBusy.value = true
  try {
    await afterOrphanClean(await cleanOrphans({ targets: kinds }))
  } catch (e) { toast(e.message, 'err') } finally { orphanBusy.value = false }
}

/* 存储位置健康：外接盘 / NAS 掉线时，这些位置下的影片不会被判缺失，
   也不该被清理。面板里明确告诉用户「为什么这次没判缺失」。 */
const rootStates = ref([])
const downRoots = computed(() => (rootStates.value || []).filter((r) => !r.usable))
async function loadRoots() {
  try {
    const r = await storageRoots()
    rootStates.value = r.roots || []
  } catch (e) { rootStates.value = [] }
}
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

/* ---------------- 软件数据占用与清理 ---------------- */
const sw = ref(null)
const swLoading = ref(false)
const swCleaning = ref(false)
async function loadSoftware() {
  swLoading.value = true
  try { sw.value = await getSoftwareStorage() } catch (e) { toast(e.message, 'err') }
  finally { swLoading.value = false }
}
const DIR_LABELS = {
  covers: 'maint.swCovers', previews: 'maint.swPreviews', avatars: 'maint.swAvatars',
  fanarts: 'maint.swFanarts', subs: 'maint.swSubs', adcheck: 'maint.swAdcheck',
  db: 'maint.swDb', config: 'maint.swConfig',
  temp: 'maint.swTemp', remux: 'maint.swRemux', asr: 'maint.swAsr',
  chrome: 'maint.swChrome', logs: 'maint.swLogs', backup: 'maint.swBackup',
  ext: 'maint.swExt', models: 'maint.swModels',
}
const DIR_ORDER = ['covers', 'previews', 'avatars', 'fanarts', 'subs', 'adcheck', 'db', 'config',
  'temp', 'remux', 'asr', 'chrome', 'logs', 'backup', 'ext', 'models']
/* 三组分开看：内容数据动不得、缓存可清、组件删了功能就坏（只展示不清理） */
const SW_GROUPS = [
  ['data', 'maint.swGroupData', 'maint.swGroupDataHint'],
  ['cache', 'maint.swGroupCache', 'maint.swGroupCacheHint'],
  ['component', 'maint.swGroupComponent', 'maint.swGroupComponentHint'],
]
const swDirs = computed(() => {
  if (!sw.value) return []
  const map = {}
  ;(sw.value.dirs || []).forEach((d) => { map[d.label] = d })
  return DIR_ORDER.filter((k) => map[k]).map((k) => map[k])
})
const swDirsOf = (g) => swDirs.value.filter((d) => (d.group || 'data') === g)
const swGroupBytes = (g) => swDirsOf(g).reduce((s, d) => s + (Number(d.bytes) || 0), 0)
const swGroupFiles = (g) => swDirsOf(g).reduce((s, d) => s + (Number(d.files) || 0), 0)

/* 可清理项：这里只放「缓存 / 临时 / 日志」这类删了不影响任何数据的；
   孤儿（无主封面、孤儿预览图、孤儿字幕、孤儿证据图等）统一在下方「孤儿与残留」面板处理，
   避免同一个东西在两个地方各有一个清理按钮。 */
const CLEAN_ITEMS = [
  { key: 'temp', label: 'maint.cleanTemp', safe: true },
  { key: 'asr', label: 'maint.cleanAsr', safe: true },
  { key: 'remux', label: 'maint.cleanRemux', safe: false, desc: 'maint.cleanRemuxDesc' },
  { key: 'chrome', label: 'maint.cleanChrome', safe: false, desc: 'maint.cleanChromeDesc' },
  { key: 'logs', label: 'maint.cleanLogs', safe: false },
  { key: 'backup', label: 'maint.cleanBackup', safe: false, desc: 'maint.cleanBackupDesc' },
]
const SAFE_KEYS = CLEAN_ITEMS.filter((i) => i.safe).map((i) => i.key)
const cleanItems = computed(() => {
  const c = sw.value && sw.value.cleanable
  if (!c) return []
  return CLEAN_ITEMS.map((i) => ({
    key: i.key, label: i.label,
    files: (c[i.key] && c[i.key].files) || 0,
    bytes: (c[i.key] && c[i.key].bytes) || 0,
  })).filter((it) => it.files || it.bytes)
})

async function cleanOne(target, key) {
  const special = {
    preview_all: { title: t('maint.cleanPreviewTitle'), desc: t('maint.cleanPreviewDesc') },
    db_vacuum: { title: t('maint.cleanDbTitle'), desc: t('maint.cleanDbDesc') },
  }[key]
  const item = CLEAN_ITEMS.find((i) => i.key === key)
  const title = special ? special.title : (item ? t(item.label) : t('maint.swCleanable'))
  const desc = special ? special.desc : t((item && item.desc) || 'maint.cleanGenericDesc')
  const ok = await confirmDialog(title, desc, { danger: true, okText: t('maint.confirmClean') })
  if (!ok) return
  swCleaning.value = true
  try {
    const r = await cleanSoftwareStorage([target])
    toast(t('maint.cleaned', { n: r.removed || 0 }), 'ok')
    await loadSoftware()
  } catch (e) { toast(e.message, 'err') }
  finally { swCleaning.value = false }
}
async function cleanAllSafe() {
  const n = cleanItems.value.filter((it) => SAFE_KEYS.includes(it.key))
    .reduce((s, it) => s + it.files, 0)
  const ok = await confirmDialog(t('maint.cleanSafeTitle'), t('maint.cleanSafeDesc', { n }),
    { danger: true, okText: t('maint.confirmClean') })
  if (!ok) return
  swCleaning.value = true
  try {
    const r = await cleanSoftwareStorage([...SAFE_KEYS, 'db_vacuum'])
    toast(t('maint.cleaned', { n: r.removed || 0 }), 'ok')
    await loadSoftware(); await load()
  } catch (e) { toast(e.message, 'err') }
  finally { swCleaning.value = false }
}

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
const healthCount = (k) => (health.value && health.value.counts && health.value.counts[k]) || 0
const healthList = (k) => (health.value && health.value[k]) || []

onMounted(() => { load(); loadSoftware(); loadRoots(); loadOrphans() })
</script>

<template>
  <div class="sh">
    <div class="stat-cards">
      <div v-for="c in cards" :key="c.l" class="stat-card" :class="c.tone">
        <div class="stat-value">{{ c.v }}</div>
        <div class="stat-label">{{ $t(c.l) }}</div>
      </div>
    </div>

    <!-- 软件数据占用与清理 -->
    <div class="panel">
      <div class="panel-head">
        {{ $t('maint.swTitle') }}
        <span class="sub">{{ $t('maint.swSub') }}</span>
        <div class="spacer"></div>
        <button class="btn tiny" :disabled="swLoading || swCleaning" @click="loadSoftware">{{ $t('maint.swRefresh') }}</button>
      </div>
      <div class="panel-body">
        <div v-if="swLoading" class="empty compact"><span class="spinner large"></span></div>
        <template v-else-if="sw">
          <div v-for="g in SW_GROUPS" :key="g[0]" class="sw-group">
            <div v-if="swDirsOf(g[0]).length" class="sw-group-head">
              <b>{{ $t(g[1]) }}</b>
              <span class="muted">{{ fmtSize(swGroupBytes(g[0])) }} · {{ swGroupFiles(g[0]) }} {{ $t('maint.files') }}</span>
              <span class="sw-group-hint">{{ $t(g[2]) }}</span>
            </div>
            <div v-if="swDirsOf(g[0]).length" class="sw-cards">
              <div v-for="d in swDirsOf(g[0])" :key="d.label" class="sw-card">
                <div class="sw-name">{{ $t(DIR_LABELS[d.label] || d.label) }}</div>
                <div class="sw-size">{{ fmtSize(d.bytes) }}</div>
                <div class="sw-files">{{ d.files }} {{ $t('maint.files') }}</div>
              </div>
            </div>
          </div>
          <div class="sw-total">
            {{ $t('maint.swTotal') }}：<b>{{ fmtSize(sw.total.bytes) }}</b>
            <span class="muted">（{{ sw.total.files }} {{ $t('maint.files') }}）</span>
          </div>

          <div class="sw-clean">
            <div class="sw-clean-title">{{ $t('maint.swCleanable') }}</div>
            <div v-if="!cleanItems.length" class="muted sm">{{ $t('maint.swCleanNone') }}</div>
            <div v-for="it in cleanItems" :key="it.key" class="sw-clean-item">
              <span>
                {{ $t(it.label) }} · {{ fmtSize(it.bytes) }}
                <span class="muted">（{{ it.files }} {{ $t('maint.files') }}）</span>
              </span>
              <button class="btn tiny" :disabled="swCleaning" @click="cleanOne(it.key, it.key)">{{ $t('maint.clean') }}</button>
            </div>
            <div class="sw-clean-row">
              <button class="btn tiny danger" :disabled="swCleaning" @click="cleanAllSafe">{{ $t('maint.cleanSafe') }}</button>
              <button class="btn tiny" :disabled="swCleaning" @click="cleanOne('db_vacuum','db_vacuum')">{{ $t('maint.cleanDb') }}</button>
              <button class="btn tiny" :disabled="swCleaning" @click="cleanOne('preview_all','preview_all')">{{ $t('maint.cleanPreview') }}</button>
            </div>
          </div>
        </template>
      </div>
    </div>

    <!-- 体检 -->
    <div class="panel">
      <div class="panel-head">
        {{ $t('maint.healthCheck') }}
        <span class="sub">{{ $t('maint.healthCheckSub') }}</span>
        <div class="spacer"></div>
        <button class="btn tiny" :disabled="checkMissingLoading" @click="runCheckMissing">{{ checkMissingLoading ? $t('maint.checking') : $t('maint.checkMissing') }}</button>
        <button class="btn tiny primary" :disabled="healthLoading" @click="runHealth">{{ healthLoading ? $t('maint.checking') : (health ? $t('maint.rerunHealth') : $t('maint.runHealth')) }}</button>
      </div>
      <div class="panel-body">
        <!-- 存储位置不可用：外接盘 / NAS 没挂载时，这些位置下的影片不会被判缺失，
             也不会被清理；明确告知原因，避免用户以为「检查坏了」 -->
        <div v-if="downRoots.length" class="root-warn">
          <span class="rw-ico">⚠</span>
          <div class="rw-main">
            <div class="rw-title">{{ $t('maint.rootsDown', { n: downRoots.length }) }}</div>
            <div v-for="r in downRoots" :key="r.path" class="rw-line">
              <code>{{ r.path }}</code>
              <span>· {{ r.reason }}</span>
              <span v-if="r.last_ok_at" class="rw-last">{{ $t('maint.rootLastOk') }} {{ r.last_ok_at }}</span>
            </div>
            <div class="rw-hint">{{ $t('maint.rootsDownHint') }}</div>
          </div>
          <button class="btn tiny" @click="loadRoots">{{ $t('maint.rootRecheck') }}</button>
        </div>
        <div v-if="!health && !healthLoading" class="empty compact"><div class="desc">{{ $t('maint.clickToHealth') }}</div></div>
        <div v-else-if="healthLoading" class="empty compact"><span class="spinner large"></span></div>
        <template v-else>
          <div class="hs-row">
            <div v-for="[k, label, , tone] in HEALTH_GROUPS" :key="k" class="hs-card" :class="[healthCount(k) ? tone : 'ok', { on: openGroup[k] }]" @click="openGroup[k] = !openGroup[k]">
              <b class="tabular">{{ healthCount(k) }}</b><span>{{ $t(label) }}</span>
            </div>
          </div>
          <!-- 类型归一：同一题材被不同源写成多个名字时，合并成统一叫法 -->
          <div class="gn-row">
            <span class="muted sm">{{ $t('maint.mergeGenresHint') }}</span>
            <button class="btn tiny" :disabled="gnBusy" @click="runMergeGenres">
              {{ gnBusy ? $t('maint.checking') : $t('maint.mergeGenres') }}
            </button>
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

    <!-- 孤儿数据 -->
    <div class="panel">
      <div class="panel-head">
        {{ $t('maint.orphanTitle') }}
        <span class="sub">{{ $t('maint.orphanSub') }}</span>
        <div class="spacer"></div>
        <button class="btn tiny" :disabled="orphanBusy" @click="loadOrphans">
          {{ orphanBusy ? $t('maint.checking') : $t('maint.missRefresh') }}
        </button>
      </div>
      <div class="panel-body">
        <div v-if="!orphans" class="empty compact"><div class="desc">{{ $t('maint.orphanHint') }}</div></div>
        <template v-else>
          <div class="orph-row">
            <button v-for="k in ORPHAN_KINDS" :key="k.key" class="orph-card"
                    :class="{ zero: !orphanCount(k.key) }"
                    :disabled="orphanBusy || !orphanCount(k.key)" @click="cleanOrph(k.key)">
              <b class="tabular">{{ orphanCount(k.key) }}</b>
              <span>
                {{ $t(k.label) }}
                <template v-if="k.withSize && orphanBytes(k.key)"> · {{ fmtSize(orphanBytes(k.key)) }}</template>
              </span>
            </button>
          </div>
          <div class="orph-sum">
            <span class="muted sm">{{ $t('maint.orphanTotal', { n: orphanFiles, s: fmtSize(orphanSize) }) }}</span>
            <button class="btn tiny danger" :disabled="orphanBusy || !orphanHasAny" @click="cleanAllOrphans">
              {{ orphanBusy ? $t('maint.checking') : $t('maint.orphanCleanAll') }}
            </button>
          </div>
          <p class="muted sm orph-tip">{{ $t('maint.orphanTip') }}</p>
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
        <!-- 边界说明：这里只按文件名 / 体积判，认不出「正片内嵌广告」，给出入口 -->
        <div class="q-scope">
          <span>{{ $t('maint.qualityAdHint') }}</span>
          <button class="btn tiny ghost" @click="state.maintTab = 'ad'">{{ $t('maint.gotoAdCheck') }} →</button>
        </div>
      </div>
    </div>

    <div class="foot-actions">
      <button class="btn tiny" @click="doSniff">{{ $t('maint.matchCovers') }}</button>
    </div>
  </div>
</template>

<style scoped>
.sh { display: flex; flex-direction: column; gap: var(--sp-4); }

.sw-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: var(--sp-2); margin-bottom: var(--sp-3); }
/* 数据 / 缓存 / 组件三组：每组一行标题 + 合计，避免十几张卡片平铺分不清性质 */
.sw-group { margin-bottom: var(--sp-2); }
.sw-group-head { display: flex; align-items: baseline; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: var(--sp-2); }
.sw-group-head b { font-size: var(--fs-sm); color: var(--c-text-2); }
.sw-group-hint { flex: 1 1 100%; font-size: var(--fs-xs); color: var(--c-text-3); }
.sw-card { display: flex; flex-direction: column; gap: 2px; padding: var(--sp-2) var(--sp-3); border-radius: var(--r-md); background: var(--c-surface-2); border: 1px solid var(--c-line); }
.sw-name { font-size: var(--fs-xs); color: var(--c-text-3); }
.sw-size { font-size: var(--fs-md); font-weight: 650; color: var(--c-text); font-family: var(--font-mono, monospace); }
.sw-files { font-size: var(--fs-xs); color: var(--c-text-3); }
.sw-total { margin-bottom: var(--sp-3); font-size: var(--fs-md); }
.sw-total b { font-family: var(--font-mono, monospace); }
.sw-clean { padding-top: var(--sp-3); border-top: 1px dashed var(--c-line); display: flex; flex-direction: column; gap: var(--sp-2); }
.sw-clean-title { font-weight: 600; font-size: var(--fs-md); }
.sw-clean-item { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); font-size: var(--fs-sm); color: var(--c-text-2); }
.sw-clean-row { display: flex; gap: var(--sp-2); flex-wrap: wrap; margin-top: var(--sp-1); }
.empty.compact { padding: var(--sp-6) var(--sp-4); }
/* 类型归一按钮行 */
.gn-row { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; padding-top: var(--sp-2); border-top: 1px dashed var(--c-line); }
.gn-row .muted { flex: 1; min-width: 220px; }
/* 劣质片面板底部的边界说明：指向内容级广告筛查 */
.q-scope {
  display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap;
  margin-top: var(--sp-3); padding-top: var(--sp-3);
  border-top: 1px dashed var(--c-line);
  font-size: var(--fs-xs); color: var(--c-text-3);
}
.q-scope > span { flex: 1; min-width: 240px; }

/* 孤儿数据卡片 */
.orph-row { display: flex; gap: var(--sp-2); flex-wrap: wrap; }
.orph-card { display: flex; flex-direction: column; align-items: center; gap: 2px; min-width: 130px; padding: var(--sp-2) var(--sp-3); border-radius: var(--r-md); border: 1px solid var(--c-line); background: var(--c-surface-2); color: var(--c-text); cursor: pointer; }
.orph-card:hover:not(:disabled) { border-color: var(--c-primary); background: var(--c-primary-soft); }
.orph-card.zero { opacity: .5; cursor: default; }
.orph-card b { font-size: var(--fs-xl); font-weight: 650; }
.orph-card span { font-size: var(--fs-xs); color: var(--c-text-3); text-align: center; }
.orph-sum { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; margin-top: var(--sp-3); }
.orph-sum > span { flex: 1; min-width: 200px; }
.orph-tip { margin-top: var(--sp-2); }

/* 存储位置不可用提示条 */
.root-warn { display: flex; gap: var(--sp-3); align-items: flex-start; padding: var(--sp-3); margin-bottom: var(--sp-3); border-radius: var(--r-md); border: 1px solid var(--c-warn); background: color-mix(in srgb, var(--c-warn) 10%, transparent); }
.rw-ico { color: var(--c-warn); font-size: 16px; line-height: 1.3; }
.rw-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.rw-title { font-weight: 600; font-size: var(--fs-md); }
.rw-line { font-size: var(--fs-xs); color: var(--c-text-2); display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.rw-line code { font-family: var(--font-mono, monospace); }
.rw-last { color: var(--c-text-3); }
.rw-hint { font-size: var(--fs-xs); color: var(--c-text-3); margin-top: 2px; }
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
