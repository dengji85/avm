<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { state } from '../state.js'
import {
  getConfig, putConfig, listProviders, testScraper as apiTest,
  parsePreview as apiParse, fsList, sniffCovers, csvUrl, cacheAvatars,
  fillActressAvatars, rescanLocalCovers,   getServerInfo, resetToken, checkUpdate,
} from '../api.js'
import { toast, confirmDialog } from '../utils.js'
import { useTasks } from '../composables/useTasks.js'
import { LANGUAGES, i18nState, setLang } from '../i18n'
import { t } from '../i18n/index.js'

const { runScan, runScrape } = useTasks()

const TABS = [
  ['library', 'settings.tab.library'],
  ['scraper', 'settings.tab.scraper'],
  ['parser', 'settings.tab.parser'],
  ['ai', 'settings.tab.ai'],
  ['appearance', 'settings.tab.appearance'],
  ['remote', 'settings.tab.remote'],
  ['about', 'settings.tab.about'],
]
const tab = ref('library')

/* 预置结构，避免异步加载前模板访问 undefined */
const cfg = reactive({
  library: { paths: [], min_size_mb: 0, ignore_keywords: [], video_extensions: [] },
  cover: { auto_local: false, download: false },
  media: { avatar_dir: 'avatars', fanart_dir: 'fanarts', avatar_download: false, fanart_download: true },
  scraper: {
    order: [], timeout: 20, delay_ms: 0, workers: 4, proxy: '', overwrite: false,
    chrome_debug_port: 9222,
    avwiki: { base_url: '', cookie: '' },
    javbus: { base_url: '', cookie: '' },
    javdb: { base_url: '', cookie: '' },
    http_json: {}, http_html: {},
  },
  ffmpeg_path: '',
  ai: { enabled: false, base_url: 'https://api.openai.com/v1', api_key: '', model: 'gpt-4o-mini', temperature: 0.4 },
})

const providers = reactive({ available: [], active: [] })
const provOn = reactive({})
const saving = ref(false)
const loading = ref(false)

/* 文本镜像（数组 ↔ 逗号串） */
const ignoreText = ref('')
const extText = ref('')
const jsonText = ref('{}')
const htmlText = ref('{}')

async function load() {
  loading.value = true
  try {
    const c = await getConfig()
    Object.assign(cfg, c)
    cfg.library = Object.assign({ paths: [], min_size_mb: 0, ignore_keywords: [], video_extensions: [], auto_scan_interval: 0 }, c.library)
    autoScan.value = Number(cfg.library.auto_scan_interval) > 0
    autoScanInterval.value = Number(cfg.library.auto_scan_interval) > 0 ? Number(cfg.library.auto_scan_interval) : 10
    cfg.cover = Object.assign({ auto_local: false, download: false }, c.cover)
    cfg.media = Object.assign(
      { avatar_dir: 'avatars', fanart_dir: 'fanarts', avatar_download: false, fanart_download: true },
      c.media,
    )
    cfg.scraper = Object.assign(
      { order: [], timeout: 20, delay_ms: 0, workers: 4, proxy: '', overwrite: false, chrome_debug_port: 9222, avwiki: {}, javbus: {}, javdb: {}, http_json: {}, http_html: {} },
      c.scraper,
    )
    cfg.scraper.avwiki = Object.assign({ base_url: '', cookie: '' }, cfg.scraper.avwiki)
    cfg.scraper.javbus = Object.assign({ base_url: '', cookie: '' }, cfg.scraper.javbus)
    cfg.scraper.javdb = Object.assign({ base_url: '', cookie: '' }, cfg.scraper.javdb)

    ignoreText.value = (cfg.library.ignore_keywords || []).join(', ')
    extText.value = (cfg.library.video_extensions || []).join(', ')
    jsonText.value = JSON.stringify(cfg.scraper.http_json || {}, null, 2)
    htmlText.value = JSON.stringify(cfg.scraper.http_html || {}, null, 2)

    const pv = await listProviders()
    providers.available = pv.available || []
    providers.active = pv.active || []
    providers.available.forEach((p) => { provOn[p.id] = (cfg.scraper.order || []).includes(p.id) })
    state.config = cfg
  } catch (e) { toast(e.message, 'err') } finally { loading.value = false }
}

/* ---------------- 媒体库 ---------------- */
const autoScan = ref(false)
const autoScanInterval = ref(10)
async function saveLibrary() {
  saving.value = true
  try {
    const patch = {
      ffmpeg_path: cfg.ffmpeg_path || '',
      library: {
        paths: cfg.library.paths || [],
        min_size_mb: Number(cfg.library.min_size_mb) || 0,
        ignore_keywords: ignoreText.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        video_extensions: extText.value.split(/[,，]/).map((s) => s.trim().toLowerCase())
          .filter(Boolean).map((s) => (s.startsWith('.') ? s : '.' + s)),
        auto_scan_interval: autoScan.value ? Math.max(1, Number(autoScanInterval.value) || 10) : 0,
      },
      cover: { auto_local: !!cfg.cover.auto_local, download: !!cfg.cover.download },
      media: {
        avatar_dir: (cfg.media.avatar_dir || 'avatars').toString().trim() || 'avatars',
        fanart_dir: (cfg.media.fanart_dir || 'fanarts').toString().trim() || 'fanarts',
        avatar_download: !!cfg.media.avatar_download,
        fanart_download: !!cfg.media.fanart_download,
      },
    }
    await putConfig(patch)
    toast(t('settings.libSaved'), 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') } finally { saving.value = false }
}

const newPath = ref('')
async function addPath() {
  const v = newPath.value.trim()
  if (!v) return
  if ((cfg.library.paths || []).includes(v)) { toast(t('settings.dirExists'), 'err'); return }
  cfg.library.paths.push(v)
  newPath.value = ''
  await saveLibrary()
}
async function delPath(i) {
  const p = cfg.library.paths[i]
  if (!(await confirmDialog(t('settings.removeDirTitle'), t('settings.removeDirDesc') + '\n' + p, { danger: true, okText: t('settings.remove') }))) return
  cfg.library.paths.splice(i, 1)
  await saveLibrary()
}

/* 目录浏览器 */
const browser = reactive({ open: false, path: '', dirs: [] })
async function browse(p) {
  try {
    const d = await fsList(p || '')
    browser.path = d.path || ''
    browser.dirs = d.dirs || []
    browser.open = true
  } catch (e) { toast(e.message, 'err') }
}
function pickDir() {
  if (!browser.path) return
  newPath.value = browser.path
  browser.open = false
}

async function doSniff() {
  try {
    const r = await sniffCovers()
    toast(t('settings.coverCheck', { n: r.checked || 0, m: r.found || 0 }), 'ok')
  } catch (e) { toast(e.message, 'err') }
}

// 把仍是远程 URL 的女优头像批量下载落盘到自定义 avatar_dir
const cachingAvatars = ref(false)
async function doCacheAvatars() {
  if (!cfg.media.avatar_download) {
    toast(t('settings.avatarWarn'), 'warn')
    return
  }
  const ok = await confirmDialog(
    t('settings.avatarConfirmTitle'),
    t('settings.avatarConfirmDesc'),
    { danger: true, okText: t('settings.fill') },
  )
  if (!ok) return
  cachingAvatars.value = true
  try {
    const r = await cacheAvatars()
    toast(t('settings.avatarCached', { n: r.downloaded || 0, bad: r.failed || 0 }), 'ok')
  } catch (e) { toast(e.message, 'err') } finally { cachingAvatars.value = false }
}

// 通过重新抓取已刮削影片的元数据，补全女优头像（落盘到本地），需联网+代理
const fillingAvatars = ref(false)
async function doFillActressAvatars() {
  const ok = await confirmDialog(
    t('settings.avatarFillConfirmTitle'),
    t('settings.avatarFillConfirmDesc'),
    { danger: true, okText: t('settings.fill') },
  )
  if (!ok) return
  fillingAvatars.value = true
  try {
    const r = await fillActressAvatars()
    toast(t('settings.avatarFilled', { n: r.filled || 0, bad: r.failed || 0 }), 'ok')
  } catch (e) { toast(e.message, 'err') } finally { fillingAvatars.value = false }
}

// 重新嗅探本地已有的封面图片，命中则落盘写回（无需全量重扫）
const rescanningCovers = ref(false)
async function doRescanLocalCovers() {
  rescanningCovers.value = true
  try {
    const r = await rescanLocalCovers()
    toast(t('settings.coverFillDesc', { n: r.movies_scanned || 0, m: r.filled || 0, k: r.covers_after_empty || 0 }), 'ok')
  } catch (e) { toast(e.message, 'err') } finally { rescanningCovers.value = false }
}

/* ---------------- 刮削 ---------------- */
async function saveScraper() {
  let hj, hh
  try { hj = JSON.parse(jsonText.value || '{}') } catch (e) { toast(t('settings.jsonInvalid') + e.message, 'err'); return }
  try { hh = JSON.parse(htmlText.value || '{}') } catch (e) { toast(t('settings.htmlInvalid') + e.message, 'err'); return }

  const order = providers.available.filter((p) => provOn[p.id]).map((p) => p.id)
  if (!order.length) { toast(t('settings.atLeastOneSource'), 'err'); return }

  saving.value = true
  try {
    await putConfig({
      scraper: {
        order,
        timeout: Number(cfg.scraper.timeout) || 20,
        delay_ms: Number(cfg.scraper.delay_ms) || 0,
        workers: Math.max(1, Number(cfg.scraper.workers) || 1),
        proxy: cfg.scraper.proxy || '',
        overwrite: !!cfg.scraper.overwrite,
        chrome_debug_port: Number(cfg.scraper.chrome_debug_port) || 9222,
        http_json: hj,
        http_html: hh,
        avwiki: cfg.scraper.avwiki,
        javbus: cfg.scraper.javbus,
        javdb: cfg.scraper.javdb,
      },
    })
    toast(t('settings.sourceSaved'), 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') } finally { saving.value = false }
}

function moveProvider(id, dir) {
  const arr = providers.available
  const i = arr.findIndex((p) => p.id === id)
  const j = i + dir
  if (i < 0 || j < 0 || j >= arr.length) return
  arr.splice(j, 0, arr.splice(i, 1)[0])
}

const testCode = ref('')
const testing = ref(false)
const testOut = reactive({ lines: [], cls: '' })

async function runTest() {
  testing.value = true
  testOut.lines = [t('settings.testing')]
  testOut.cls = ''
  try {
  const order = providers.available.filter((p) => provOn[p.id]).map((p) => p.id)
  const override = { scraper: { order, proxy: cfg.scraper.proxy || '', chrome_debug_port: Number(cfg.scraper.chrome_debug_port) || 9222 } }
  const aw = cfg.scraper.avwiki || {}
  if (aw.base_url || aw.cookie) override.scraper.avwiki = { ...aw }
  if (cfg.scraper.javbus.base_url || cfg.scraper.javbus.cookie) override.scraper.javbus = { ...cfg.scraper.javbus }
  if (cfg.scraper.javdb.base_url || cfg.scraper.javdb.cookie) override.scraper.javdb = { ...cfg.scraper.javdb }

  const r = await apiTest({ code: testCode.value.trim() || undefined, override })
  const lines = [t('settings.testCode') + (r.code || t('settings.firstInLib'))]
  let hit = false
  ;(r.results || []).forEach((res) => {
    if (res.ok) {
      hit = true
      const f = res.fields || {}
      const bits = []
      if (f.title) bits.push(t('settings.tTitle') + f.title)
      if (f.actresses && f.actresses.length) bits.push(t('settings.tActress') + f.actresses.join('、'))
      if (f.genres && f.genres.length) bits.push(t('settings.tGenre') + f.genres.join('、'))
      if (f.studio) bits.push(t('settings.tStudio') + f.studio)
      lines.push(`✓ [${res.provider}] ${bits.join('；') || t('settings.connectedOK', { p: res.provider })}`)
    } else {
      lines.push(`✗ [${res.provider}] ${res.reason || t('settings.connectedFail', { p: res.provider })}`)
    }
  })
  if (r.cover_ok) lines.push(t('settings.coverOK'))
  else if (r.cover_url) lines.push(t('settings.coverFail'))
  testOut.lines = lines
  testOut.cls = hit ? 'ok' : 'err'
  } catch (e) {
  testOut.lines = [t('settings.reqFail') + (e.message || e)]
  testOut.cls = 'err'
  } finally { testing.value = false }
}

/* ---------------- 解析预览 ---------------- */
const parseInput = ref('')
const parseRows = ref([])
async function runParse() {
  const names = parseInput.value.split('\n').map((s) => s.trim()).filter(Boolean)
  if (!names.length) { toast(t('settings.filenameRequired'), 'err'); return }
  try {
    const r = await apiParse(names)
    parseRows.value = r.items || []
  } catch (e) { toast(e.message, 'err') }
}

const parseStats = computed(() => {
  const n = parseRows.value.length
  const ok = parseRows.value.filter((r) => r.matched).length
  return { n, ok, rate: n ? Math.round((ok / n) * 100) : 0 }
})

/* ---------------- 访问与远程 ---------------- */
const serverInfo = reactive({ host: '127.0.0.1', port: 8770, access_token: '', require_token_remote: true })
const showTok = ref(false)
const tokInput = ref('')
const savingTok = ref(false)
const requireToken = ref(true)
function maskToken(tok) {
  if (!tok) return '—'
  if (tok.length <= 8) return tok
  return tok.slice(0, 4) + '•'.repeat(Math.min(24, tok.length - 8)) + tok.slice(-4)
}
async function copyTok() {
  try { await navigator.clipboard.writeText(serverInfo.access_token); toast(t('settings.copied'), 'ok') }
  catch (e) { toast(t('settings.copyFailed'), 'err') }
}
async function doResetToken() {
  try { const r = await resetToken(); serverInfo.access_token = r.access_token; tokInput.value = r.access_token; toast(t('settings.tokenReset'), 'ok') }
  catch (e) { toast(e.message, 'err') }
}
async function saveToken() {
  const v = (tokInput.value || '').trim()
  if (v && (v.length < 8 || /\s/.test(v))) { toast(t('settings.tokenInvalid'), 'err'); return }
  savingTok.value = true
  try {
    await putConfig({ server: { access_token: v } })
    serverInfo.access_token = v || serverInfo.access_token
    toast(t('settings.tokenSaved'), 'ok')
  } catch (e) { toast(e.message, 'err') }
  finally { savingTok.value = false }
}
const qrAddr = ref('')
const qrUrl = computed(() => `/api/server/qr?addr=${encodeURIComponent(qrAddr.value || '')}&_=${Date.now()}`)
const isLocal = (u) => /(^|[/:])(127\.0\.0\.1|localhost|0\.0\.0\.0)(:|\/|$)/i.test(u)
const lanUrls = computed(() => (serverInfo.access_urls || []).filter((u) => !isLocal(u)))
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast(t('settings.copied'), 'ok') }
  catch (e) { toast(t('settings.copyFailed'), 'err') }
}
async function saveRequireToken() {
  try { await putConfig({ server: { require_token_remote: requireToken.value } }); toast(t('settings.saved'), 'ok') }
  catch (e) { toast(e.message, 'err') }
}

async function loadServerInfo() {
  try {
    const r = await getServerInfo()
    Object.assign(serverInfo, r)
    requireToken.value = r.require_token_remote
    const urls = (r.access_urls || []).filter((u) => !/(^|[/:])(127\.0\.0\.1|localhost|0\.0\.0\.0)(:|\/|$)/i.test(u))
    if (urls.length) qrAddr.value = urls[0]
    if (r.access_token) tokInput.value = r.access_token
    if (r.app_version) appVersion.value = r.app_version
    if (r.build_date) buildDate.value = r.build_date
  } catch (e) { /* 非致命 */ }
}

/* ---------------- 版本与下载 ---------------- */
const RELEASE_URL = 'https://github.com/dengji85/avm/releases'
const REPO_URL = 'https://github.com/dengji85/avm'
const releaseUrl = RELEASE_URL
const repoUrl = REPO_URL
const appVersion = ref('')
const buildDate = ref('')

/* 检查更新 */
const checkingUpdate = ref(false)
const updateState = ref('idle') // idle | upToDate | newVersion | error
const updateInfo = reactive({ latest: '', downloadUrl: '', released: '', notes: '', error: '' })
async function doCheckUpdate() {
  if (checkingUpdate.value) return
  checkingUpdate.value = true
  updateState.value = 'idle'
  try {
    const r = await checkUpdate('stable')
    updateInfo.latest = r.latest || appVersion.value
    updateInfo.downloadUrl = r.download_url || ''
    updateInfo.released = r.released || ''
    updateInfo.notes = r.notes || ''
    updateInfo.error = r.error || ''
    if (r.error) updateState.value = 'error'
    else if (r.update_available) updateState.value = 'newVersion'
    else updateState.value = 'upToDate'
  } catch (e) {
    updateState.value = 'error'
    updateInfo.error = e.message || String(e)
  } finally {
    checkingUpdate.value = false
  }
}

onMounted(async () => { await load(); await loadServerInfo() })
</script>

<template>
  <section class="view">
    <div class="toolbar">
      <h1 class="tb-title">{{ $t('view.settings') }}</h1>
      <span v-if="loading || saving" class="spinner"></span>
      <div class="spacer"></div>
      <div class="tabs st-tabs">
        <button v-for="t in TABS" :key="t[0]" class="tab" :class="{ on: tab === t[0] }" @click="tab = t[0]">{{ $t(t[1]) }}</button>
      </div>
    </div>

    <div class="view-body block-scroll">
      <!-- ============ 媒体库 ============ -->
      <template v-if="tab === 'library'">
        <div class="panel">
          <div class="panel-head">{{ $t('settings.scanDir') }} <span class="sub">{{ $t('settings.scanDirSub') }}</span></div>
          <div class="panel-body">
            <ul v-if="(cfg.library.paths || []).length" class="path-list">
              <li v-for="(p, i) in cfg.library.paths" :key="i">
                <span class="pi">📁</span>
                <span class="pp ellipsis" :title="p">{{ p }}</span>
                <button class="btn tiny ghost" @click="delPath(i)">{{ $t('settings.remove') }}</button>
              </li>
            </ul>
            <p v-else class="muted">{{ $t('settings.noDirYet') }}</p>

            <div class="hstack">
              <input v-model="newPath" :placeholder="$t('settings.scanDirPh')" @keydown.enter="addPath" />
              <button class="btn" @click="browse('')">{{ $t('settings.browseDir') }}</button>
              <button class="btn primary" @click="addPath">{{ $t('common.add') }}</button>
            </div>
          </div>
          <div class="panel-foot">
            <button class="btn primary" @click="runScan({})">{{ $t('settings.scanNow') }}</button>
            <button class="btn" @click="runScrape({ missing_only: true })">{{ $t('settings.scrapeMissing') }}</button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">{{ $t('settings.scanRule') }}</div>
          <div class="panel-body">
            <div class="field-row">
              <label>{{ $t('settings.minSize') }}</label>
              <div class="hstack">
                <input type="number" v-model="cfg.library.min_size_mb" min="0" style="width:120px" />
                <span class="muted">{{ $t('settings.minSizeSub') }}</span>
              </div>
            </div>
            <div class="field">
              <label>{{ $t('settings.ignoreKw') }}</label>
              <input v-model="ignoreText" :placeholder="$t('settings.ignoreKwPh')" />
              <span class="hint">{{ $t('settings.ignoreKwHint') }}</span>
            </div>
            <div class="field">
              <label>{{ $t('settings.videoExt') }}</label>
              <input v-model="extText" :placeholder="$t('settings.videoExtPh')" />
              <span class="hint">{{ $t('settings.videoExtHint') }}</span>
            </div>
            <div class="field">
              <label>{{ $t('settings.ffmpegPath') }}</label>
              <input v-model="cfg.ffmpeg_path" :placeholder="$t('settings.ffmpegPh')" />
              <span class="hint">{{ $t('settings.ffmpegHint') }}</span>
            </div>
            <div class="field">
              <label class="switch">
                <input type="checkbox" v-model="autoScan" @change="saveLibrary" />
                <span>{{ $t('settings.autoScan') }}</span>
              </label>
              <span class="hint">{{ $t('settings.autoScanHint') }}</span>
            </div>
            <div class="field-row" v-if="autoScan">
              <label>{{ $t('settings.autoScanInterval') }}</label>
              <div class="hstack">
                <input type="number" v-model="autoScanInterval" min="1" style="width:120px" @change="saveLibrary" />
                <span class="muted">{{ $t('settings.minutes') }}</span>
              </div>
            </div>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="saving" @click="saveLibrary">{{ $t('common.save') }}</button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">{{ $t('settings.cover') }}</div>
          <div class="panel-body">
            <div class="field-row checkbox">
              <label>{{ $t('settings.autoLocalCover') }}</label>
              <label class="toggle">
                <input type="checkbox" v-model="cfg.cover.auto_local" /><span class="track"></span>
              </label>
            </div>
            <div class="field-row checkbox">
              <label>{{ $t('settings.downloadCover') }}</label>
              <label class="toggle">
                <input type="checkbox" v-model="cfg.cover.download" /><span class="track"></span>
              </label>
            </div>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="saving" @click="saveLibrary">{{ $t('common.save') }}</button>
            <button class="btn" @click="doSniff">{{ $t('settings.sniffNow') }}</button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">{{ $t('settings.mediaResPath') }} <span class="sub">{{ $t('settings.mediaResSub') }}</span></div>
          <div class="panel-body">
            <div class="field">
              <label>{{ $t('settings.avatarDir') }}</label>
              <input v-model="cfg.media.avatar_dir" :placeholder="$t('settings.avatarDirPh')" />
              <span class="hint">{{ $t('settings.avatarDirHint') }}</span>
            </div>
            <div class="field-row checkbox">
              <label>{{ $t('settings.dlAvatar') }}</label>
              <label class="toggle">
                <input type="checkbox" v-model="cfg.media.avatar_download" /><span class="track"></span>
              </label>
            </div>
            <div class="field">
              <label>{{ $t('settings.fanartDir') }}</label>
              <input v-model="cfg.media.fanart_dir" :placeholder="$t('settings.fanartDirPh')" />
              <span class="hint">{{ $t('settings.fanartHint') }}</span>
            </div>
            <div class="field-row checkbox">
              <label>{{ $t('settings.dlFanart') }}</label>
              <label class="toggle">
                <input type="checkbox" v-model="cfg.media.fanart_download" /><span class="track"></span>
              </label>
            </div>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="saving" @click="saveLibrary">{{ $t('common.save') }}</button>
            <button class="btn" :disabled="cachingAvatars" @click="doCacheAvatars">
              {{ cachingAvatars ? $t('settings.caching') : $t('settings.cacheNowBtn') }}
            </button>
            <button class="btn" :disabled="fillingAvatars" @click="doFillActressAvatars">
              {{ fillingAvatars ? $t('settings.filling') : $t('settings.fillAvatarsBtn') }}
            </button>
            <button class="btn" :disabled="rescanningCovers" @click="doRescanLocalCovers">
              {{ rescanningCovers ? $t('settings.sniffing') : $t('settings.rescanCoverBtn') }}
            </button>
          </div>
        </div>
      </template>

      <!-- ============ 刮削 ============ -->
      <template v-else-if="tab === 'scraper'">
        <div class="panel">
          <div class="panel-head">{{ $t('settings.dataSource') }} <span class="sub">{{ $t('settings.dataSourceSub') }}</span></div>
          <div class="panel-body">
            <div v-for="(p, i) in providers.available" :key="p.id" class="prov">
              <label class="toggle">
                <input type="checkbox" v-model="provOn[p.id]" /><span class="track"></span>
              </label>
              <div class="prov-main">
                <div class="prov-name">
                  {{ p.label }}
                  <code>{{ p.id }}</code>
                  <span v-if="providers.active.includes(p.id)" class="badge ok">{{ $t('settings.active') }}</span>
                </div>
                <div class="prov-desc">{{ p.desc }}</div>
              </div>
              <div class="prov-ord">
                <button class="btn tiny ghost" :disabled="i === 0" @click="moveProvider(p.id, -1)" :data-tip="$t('settings.moveUp')">↑</button>
                <button class="btn tiny ghost" :disabled="i === providers.available.length - 1" @click="moveProvider(p.id, 1)" :data-tip="$t('settings.moveDown')">↓</button>
              </div>
            </div>
            <p v-if="!providers.available.length" class="muted">{{ $t('settings.noSource') }}</p>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">{{ $t('settings.reqSettings') }}</div>
          <div class="panel-body">
            <div class="field-row">
              <label>{{ $t('settings.timeout') }}</label>
              <div class="hstack"><input type="number" v-model="cfg.scraper.timeout" style="width:100px" /><span class="muted">{{ $t('settings.timeoutSub') }}</span></div>
            </div>
            <div class="field-row">
              <label>{{ $t('settings.delay') }}</label>
              <div class="hstack"><input type="number" v-model="cfg.scraper.delay_ms" style="width:100px" /><span class="muted">{{ $t('settings.delaySub') }}</span></div>
            </div>
            <div class="field-row">
              <label>{{ $t('settings.workers') }}</label>
              <div class="hstack"><input type="number" min="1" max="16" v-model="cfg.scraper.workers" style="width:100px" /><span class="muted">{{ $t('settings.workersSub') }}</span></div>
            </div>
            <div class="field-row">
              <label>{{ $t('settings.proxy') }}</label>
              <input v-model="cfg.scraper.proxy" placeholder="http://127.0.0.1:7890" />
            </div>
            <div class="field-row checkbox">
              <label>{{ $t('settings.overwrite') }}</label>
              <label class="toggle"><input type="checkbox" v-model="cfg.scraper.overwrite" /><span class="track"></span></label>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">{{ $t('settings.siteParams') }}</div>
          <div class="panel-body">
            <div class="site">
              <b>AV-Wiki</b>
              <div class="field"><label>Base URL</label><input v-model="cfg.scraper.avwiki.base_url" placeholder="https://av-wiki.net" /></div>
              <div class="field"><label>Cookie</label><input v-model="cfg.scraper.avwiki.cookie" :placeholder="$t('settings.cookieLoader')" /></div>
              <div class="field"><label>Chrome {{ $t('settings.debugPort') }}</label><input v-model="cfg.scraper.chrome_debug_port" type="number" style="width:120px" /><span class="muted">{{ $t('settings.chromePortHint') }}</span></div>
            </div>
            <div class="site">
              <b>JavBus</b>
              <div class="field"><label>Base URL</label><input v-model="cfg.scraper.javbus.base_url" placeholder="https://www.javbus.com" /></div>
              <div class="field"><label>Cookie</label><input v-model="cfg.scraper.javbus.cookie" :placeholder="$t('settings.cookieOpt')" /></div>
            </div>
            <div class="site">
              <b>JavDB</b>
              <div class="field"><label>Base URL</label><input v-model="cfg.scraper.javdb.base_url" placeholder="https://javdb.com" /></div>
              <div class="field"><label>Cookie</label><input v-model="cfg.scraper.javdb.cookie" :placeholder="$t('settings.cookieOpt2')" /></div>
            </div>

            <details class="adv">
              <summary>{{ $t('settings.advJson') }}</summary>
              <textarea v-model="jsonText" rows="7" spellcheck="false"></textarea>
            </details>
            <details class="adv">
              <summary>{{ $t('settings.advHtml') }}</summary>
              <textarea v-model="htmlText" rows="7" spellcheck="false"></textarea>
            </details>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="saving" @click="saveScraper">{{ $t('settings.srcSave') }}</button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">{{ $t('settings.connTest') }}</div>
          <div class="panel-body">
            <div class="hstack">
              <input v-model="testCode" :placeholder="$t('settings.testPh')" style="max-width:280px" />
              <button class="btn" :disabled="testing" @click="runTest">{{ testing ? $t('settings.testing') : $t('settings.startTest') }}</button>
            </div>
            <pre v-if="testOut.lines.length" class="test-out" :class="testOut.cls">{{ testOut.lines.join('\n') }}</pre>
          </div>
        </div>
      </template>

      <!-- ============ AI 增强 ============ -->
      <template v-else-if="tab === 'ai'">
        <div class="panel">
          <div class="panel-head">
            {{ $t('settings.aiTitle') }}
            <span class="sub">{{ $t('settings.aiSub') }}</span>
          </div>
          <div class="panel-body">
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="cfg.ai.enabled" /><span class="track"></span>
              </label>
              <span>{{ $t('settings.aiEnable') }}</span>
            </div>
            <div class="field">
              <label>Base URL</label>
              <input v-model="cfg.ai.base_url" :placeholder="$t('settings.aiBasePh')" />
              <span class="hint">{{ $t('settings.aiBaseHint') }}</span>
            </div>
            <div class="field">
              <label>API Key</label>
              <input v-model="cfg.ai.api_key" type="password" :placeholder="$t('settings.aiKeyPh')" />
            </div>
            <div class="field">
              <label>{{ $t('settings.aiModel') }}</label>
              <input v-model="cfg.ai.model" :placeholder="$t('settings.aiModelPh')" />
            </div>
            <div class="field">
              <label>{{ $t('settings.aiTemp') }}</label>
              <input type="number" step="0.1" min="0" max="1" v-model.number="cfg.ai.temperature" style="width:100px" />
              <span class="muted">{{ $t('settings.aiTempSub') }}</span>
            </div>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="saving" @click="save">{{ $t('settings.aiSave') }}</button>
            <span v-if="aiOk===true" class="ok">{{ $t('settings.aiSaved') }}</span>
            <span v-else-if="aiOk===false" class="err">{{ $t('settings.aiSaveFail') }}</span>
          </div>
        </div>
      </template>

      <!-- ============ 解析预览 ============ -->
      <template v-else-if="tab === 'parser'">
        <div class="panel">
          <div class="panel-head">
            {{ $t('settings.parseTitle') }}
            <span class="sub">{{ $t('settings.parseSub') }}</span>
          </div>
          <div class="panel-body">
            <textarea v-model="parseInput" rows="7" :placeholder="$t('settings.parsePh') + '\nABP-123 中文字幕.mp4\n[JAVBUS]SSIS-456-CD1.mkv'" spellcheck="false"></textarea>
            <div class="hstack">
              <button class="btn primary" @click="runParse">{{ $t('settings.parseBtn') }}</button>
              <span v-if="parseRows.length" class="muted">
                {{ $t('settings.parseStat', { ok: parseStats.ok, n: parseStats.n, rate: parseStats.rate }) }}
              </span>
            </div>

            <table v-if="parseRows.length" class="ptable">
              <thead><tr><th>{{ $t('settings.code') }}</th><th>{{ $t('settings.rule') }}</th><th>{{ $t('settings.origName') }}</th></tr></thead>
              <tbody>
                <tr v-for="(r, i) in parseRows" :key="i">
                  <td><span class="badge" :class="r.matched ? 'ok' : 'err'">{{ r.matched ? r.code : $t('settings.unmatched') }}</span></td>
                  <td class="muted">
                    {{ r.rule || '—' }}
                    <span v-if="r.part > 1"> · CD{{ r.part }}</span>
                    <span v-if="r.subtitle"> · {{ $t('detail.subtitle') }}</span>
                    <span v-if="r.uncensored"> · {{ $t('detail.uncensored') }}</span>
                  </td>
                  <td class="pin ellipsis" :title="r.input">{{ r.input }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>

      <!-- ============ 外观 ============ -->
      <template v-else-if="tab === 'appearance'">
        <div class="panel">
          <div class="panel-head">{{ $t('settings.themeDisplay') }}</div>
          <div class="panel-body">
            <div class="field-row">
              <label>{{ $t('settings.language') }}</label>
              <select :value="i18nState.lang" @change="setLang($event.target.value)" style="width:160px">
                <option v-for="l in LANGUAGES" :key="l.code" :value="l.code">{{ l.label }}</option>
              </select>
            </div>
            <div class="field-row">
              <label>{{ $t('settings.theme') }}</label>
              <div class="btn-group">
                <button class="btn tiny" :class="{ active: state.theme === 'dark' }" @click="state.theme = 'dark'">{{ $t('settings.themeDark') }}</button>
                <button class="btn tiny" :class="{ active: state.theme === 'light' }" @click="state.theme = 'light'">{{ $t('settings.themeLight') }}</button>
              </div>
            </div>
            <div class="field-row">
              <label>{{ $t('settings.density') }}</label>
              <div class="btn-group">
                <button class="btn tiny" :class="{ active: state.density === 'cozy' }" @click="state.density = 'cozy'">{{ $t('settings.densityCozy') }}</button>
                <button class="btn tiny" :class="{ active: state.density === 'compact' }" @click="state.density = 'compact'">{{ $t('settings.densityCompact') }}</button>
              </div>
            </div>
            <div class="field-row">
              <label>{{ $t('settings.cardSize') }}</label>
              <div class="btn-group">
                <button class="btn tiny" :class="{ active: state.cardSize === 'dense' }" @click="state.cardSize = 'dense'">{{ $t('settings.cardSizeSmall') }}</button>
                <button class="btn tiny" :class="{ active: state.cardSize === 'normal' }" @click="state.cardSize = 'normal'">{{ $t('settings.cardSizeNormal') }}</button>
                <button class="btn tiny" :class="{ active: state.cardSize === 'large' }" @click="state.cardSize = 'large'">{{ $t('settings.cardSizeLarge') }}</button>
              </div>
            </div>
            <div class="field-row">
              <label>{{ $t('settings.pageSize') }}</label>
              <select v-model.number="state.page_size" style="width:120px">
                <option :value="30">30</option>
                <option :value="60">60</option>
                <option :value="120">120</option>
                <option :value="240">240</option>
              </select>
            </div>
          </div>
        </div>
      </template>

      <!-- ============ 远程（访问与远程） ============ -->
      <template v-else-if="tab === 'remote'">
        <div class="panel">
          <div class="panel-head">{{ $t('settings.access') }}</div>
          <div class="panel-body">
            <div class="field">
              <label>{{ $t('settings.listenAddr') }}</label>
              <span class="mono">{{ serverInfo.host }}:{{ serverInfo.port }}</span>
              <span class="muted">{{ serverInfo.host === '0.0.0.0' ? $t('settings.lanOn') : $t('settings.lanOff') }}</span>
            </div>
            <div class="field col">
              <label>{{ $t('settings.accessToken') }}</label>
              <div class="tok-row">
                <input class="inp mono" v-model="tokInput" :type="showTok ? 'text' : 'password'" :placeholder="$t('settings.tokenPh')" @keyup.enter="saveToken" />
                <button class="btn tiny ghost" @click="showTok = !showTok">{{ showTok ? '🙈' : '👁' }}</button>
                <button class="btn tiny ghost" @click="copyTok" :disabled="!serverInfo.access_token">{{ $t('settings.copy') }}</button>
                <button class="btn tiny" :disabled="savingTok" @click="saveToken">{{ savingTok ? $t('settings.saving') : $t('settings.saveFeed') }}</button>
                <button class="btn tiny ghost" @click="doResetToken" :title="$t('settings.resetToken')">🔄</button>
              </div>
              <span class="muted">{{ $t('settings.tokenHint') }}</span>
            </div>
            <div class="fw-tip" v-if="lanUrls.length">
              <div class="fw-tip-title">⚠️ {{ $t('settings.fwTitle') }}</div>
              <div class="fw-tip-body">{{ $t('settings.fwBody') }}</div>
              <code class="fw-cmd mono">netsh advfirewall firewall add rule name="AVM" dir=in action=allow protocol=TCP localport={{ serverInfo.port }} profile=any edge=yes</code>
            </div>
            <div class="field col lan-share" v-if="lanUrls.length">
              <label>{{ $t('settings.lanAccess') }}</label>
              <div class="lan-share-body">
                <div class="qr-wrap">
                  <img class="qr-img" :src="qrUrl" alt="QR" />
                  <div class="muted sm center">{{ $t('settings.scanHint') }}</div>
                </div>
                <div class="lan-urls">
                  <div
                    v-for="u in lanUrls"
                    :key="u"
                    class="url-row"
                    :class="{ active: qrAddr === u }"
                    @click="qrAddr = u"
                  >
                    <code class="mono url">{{ u }}</code>
                    <button class="btn tiny ghost" @click.stop="copyText(u)">{{ $t('settings.copy') }}</button>
                  </div>
                  <span class="muted sm">{{ $t('settings.tapToQr') }}</span>
                </div>
              </div>
            </div>
            <div class="field">
              <label class="switch">
                <input type="checkbox" v-model="requireToken" @change="saveRequireToken" />
                <span>{{ $t('settings.requireToken') }}</span>
              </label>
            </div>
          </div>
        </div>
      </template>

      <!-- ============ 关于 ============ -->
      <template v-else>
        <!-- 主视觉：品牌 + 版本 + 操作 -->
        <div class="about-hero">
          <div class="about-brand">{{ $t('settings.brand') }}</div>
          <div class="about-sub">{{ $t('settings.aboutSub') }}</div>
          <div class="about-ver">
            <span class="about-ver-tag">{{ $t('settings.curVersion') }}</span>
            <span class="about-ver-num mono">{{ appVersion || '—' }}</span>
            <span class="about-build muted">{{ buildDate || '' }}</span>
          </div>
          <div class="about-actions">
            <button class="btn ghost" :disabled="checkingUpdate" @click="doCheckUpdate">
              <span v-if="checkingUpdate" class="spinner sm"></span>
              {{ checkingUpdate ? $t('settings.checking') : $t('settings.checkUpdate') }}
            </button>
            <a class="btn ghost" :href="repoUrl" target="_blank" rel="noopener">{{ $t('settings.homepage') }}</a>
          </div>
          <p class="about-local muted">{{ $t('settings.aboutLocal') }}</p>
          <div v-if="updateState === 'upToDate'" class="about-update ok">
            {{ $t('settings.upToDate') }} · v{{ appVersion }}
          </div>
          <div v-else-if="updateState === 'newVersion'" class="about-update warn">
            <span>{{ $t('settings.newVersion') }}：v{{ updateInfo.latest }}<template v-if="updateInfo.released">（{{ updateInfo.released }}）</template></span>
            <a class="btn tiny primary" :href="updateInfo.downloadUrl || releaseUrl" target="_blank" rel="noopener">{{ $t('settings.download') }}</a>
            <p v-if="updateInfo.notes" class="about-notes">{{ updateInfo.notes }}</p>
          </div>
          <div v-else-if="updateState === 'error'" class="about-update err">
            <span>{{ $t('settings.updateFailed') }}</span>
            <a class="btn tiny" :href="releaseUrl" target="_blank" rel="noopener">{{ $t('settings.goRelease') }}</a>
          </div>
        </div>

        <!-- 次：数据管理 -->
        <div class="panel">
          <div class="panel-head">{{ $t('settings.dataMgmt') }}</div>
          <div class="panel-body">
            <div class="sub-block">
              <div class="sub-title">{{ $t('settings.exportData') }}</div>
              <p class="muted">{{ $t('settings.exportCsvDesc') }}</p>
              <a class="btn tiny" :href="csvUrl()" target="_blank">{{ $t('settings.exportCsv') }}</a>
            </div>
            <div class="sub-block">
              <div class="sub-title">{{ $t('settings.backupMig') }}</div>
              <p class="muted">{{ $t('settings.backupMigDesc') }}</p>
              <ul class="tips">
                <li><b>{{ $t('settings.tipUpgrade').split('：')[0] }}</b>：{{ $t('settings.tipUpgrade').split('：').slice(1).join('：') }}</li>
                <li><b>{{ $t('settings.tipMove').split('：')[0] }}</b>：{{ $t('settings.tipMove').split('：').slice(1).join('：') }}</li>
                <li><b>{{ $t('settings.tipCustom').split('：')[0] }}</b>：{{ $t('settings.tipCustom').split('：').slice(1).join('：') }}</li>
                <li><b>{{ $t('settings.tipCsv').split('：')[0] }}</b>：{{ $t('settings.tipCsv').split('：').slice(1).join('：') }}</li>
              </ul>
            </div>
          </div>
        </div>

        <!-- 次：快捷键 -->
        <div class="panel">
          <div class="panel-head">{{ $t('settings.shortcuts') }}</div>
          <div class="panel-body">
            <div class="kbd-list">
              <div class="kb"><kbd>Ctrl</kbd>+<kbd>K</kbd> 或 <kbd>/</kbd><span>{{ $t('settings.focusSearch') }}</span></div>
              <div class="kb"><kbd>Esc</kbd><span>{{ $t('settings.closeModal') }}</span></div>
              <div class="kb"><kbd>←</kbd> <kbd>→</kbd><span>{{ $t('settings.swipeSkip') }}</span></div>
              <div class="kb"><kbd>1</kbd>–<kbd>5</kbd><span>{{ $t('settings.swipeRate') }}</span></div>
              <div class="kb"><kbd>F</kbd> / <kbd>Z</kbd><span>{{ $t('settings.swipeFav') }}</span></div>
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- 目录浏览器 -->
    <Teleport to="body">
      <div v-if="browser.open" class="modal-mask" @click.self="browser.open = false">
        <div class="modal">
          <div class="modal-head">{{ $t('settings.selectDir') }}</div>
          <div class="modal-body">
            <div class="bc">
              <button class="btn tiny ghost" @click="browse('')">{{ $t('settings.rootDir') }}</button>
              <code class="bc-path">{{ browser.path || '/' }}</code>
            </div>
            <ul class="dir-list">
              <li v-for="d in browser.dirs" :key="d.path" @click="browse(d.path)">
                <span>📁</span><span class="ellipsis">{{ d.name }}</span>
              </li>
              <li v-if="!browser.dirs.length" class="muted nohover">{{ $t('settings.noSubDir') }}</li>
            </ul>
          </div>
          <div class="modal-foot">
            <button class="btn" @click="browser.open = false">{{ $t('common.cancel') }}</button>
            <button class="btn primary" :disabled="!browser.path" @click="pickDir">{{ $t('settings.chooseCurrentDir') }}</button>
          </div>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.st-tabs { border-bottom: none; }
/* 居中 + 舒适最大宽度 + 充足底部留白 */
.view-body {
  max-width: 900px;
  margin: 0 auto;
  padding: var(--sp-5) var(--sp-6) var(--sp-10);
  display: block;
}
.view-body .panel { margin-bottom: var(--sp-5); }
.view-body .panel:last-child { margin-bottom: 0; }

.path-list { display: flex; flex-direction: column; gap: var(--sp-1); }
.path-list li {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm);
  background: var(--c-surface-2);
}
.pp { flex: 1; min-width: 0; font-family: 'JetBrains Mono', monospace; font-size: var(--fs-sm); }

.prov {
  display: flex; align-items: center; gap: var(--sp-3);
  padding: var(--sp-3);
  border-radius: var(--r-md);
  background: var(--c-surface-2);
}
.prov-main { flex: 1; min-width: 0; }
.prov-name { display: flex; align-items: center; gap: var(--sp-2); font-weight: 500; }
.prov-name code { font-size: var(--fs-xs); color: var(--c-text-3); }
.prov-desc { font-size: var(--fs-sm); color: var(--c-text-3); margin-top: 2px; }
.prov-ord { display: flex; gap: 2px; }

.site {
  padding: var(--sp-3);
  border-radius: var(--r-md);
  background: var(--c-surface-2);
  display: flex; flex-direction: column; gap: var(--sp-2);
}
.site > b { font-size: var(--fs-md); }

.adv summary { cursor: pointer; font-size: var(--fs-md); color: var(--c-text-2); padding: var(--sp-1) 0; }
.adv summary:hover { color: var(--c-text); }
.adv textarea { margin-top: var(--sp-2); font-family: 'JetBrains Mono', monospace; font-size: var(--fs-sm); }

.ver-check { margin-top: var(--sp-4); padding-top: var(--sp-4); border-top: 1px dashed var(--c-border, #2a2a2a); }
.ver-row { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.ver-now { font-size: var(--fs-sm); color: var(--c-text-2); }
.ver-now b { color: var(--c-text); }
.ver-msg { margin-top: var(--sp-2); font-size: var(--fs-sm); }
.ver-msg.upd { color: var(--c-warn, #ffb454); }
.ver-msg.ok { color: var(--c-ok, #4caf50); }
.ver-dl { margin-left: var(--sp-2); color: var(--c-accent, #5b9dff); text-decoration: underline; }
.ver-notes { margin-top: var(--sp-1); color: var(--c-text-3); white-space: pre-wrap; line-height: 1.5; }

.test-out {
  margin: 0;
  padding: var(--sp-3);
  border-radius: var(--r-md);
  background: var(--c-bg-sunken);
  border: 1px solid var(--c-line);
  font-size: var(--fs-sm);
  line-height: 1.7;
  white-space: pre-wrap;
  max-height: 260px;
  overflow-y: auto;
}
.test-out.ok  { border-color: var(--c-ok); }
.test-out.err { border-color: var(--c-err); }

.ptable { width: 100%; border-collapse: collapse; font-size: var(--fs-sm); }
.ptable th, .ptable td { text-align: left; padding: var(--sp-2); border-bottom: 1px solid var(--c-line); }
.ptable th { color: var(--c-text-3); font-weight: 500; }
.ptable .pin { max-width: 340px; font-family: 'JetBrains Mono', monospace; font-size: var(--fs-xs); }

.bc { display: flex; align-items: center; gap: var(--sp-2); }
.bc-path { font-size: var(--fs-sm); color: var(--c-text-2); overflow: hidden; text-overflow: ellipsis; }
.dir-list { max-height: 320px; overflow-y: auto; display: flex; flex-direction: column; gap: 1px; }
.dir-list li {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm);
  cursor: pointer;
  font-size: var(--fs-md);
}
.dir-list li:hover:not(.nohover) { background: var(--c-surface-3); }
.dir-list li.nohover { cursor: default; }

.kbd-list { margin-top: var(--sp-3); }
.kb { display: flex; align-items: center; gap: var(--sp-2); padding: 3px 0; font-size: var(--fs-sm); }
.kb span { color: var(--c-text-3); margin-left: var(--sp-2); }
.tips { margin: 0; padding-left: 1.2em; line-height: 1.7; }
.tips li { margin: 4px 0; font-size: var(--fs-sm); color: var(--c-text-2); }
.tips b { color: var(--c-text); }
kbd {
  padding: 1px 6px;
  border-radius: var(--r-xs);
  border: 1px solid var(--c-line-strong);
  background: var(--c-surface-2);
  font-size: var(--fs-xs);
}

/* ---- 关于页：主视觉（与 .panel 卡片风格一致） ---- */
.about-hero {
  padding: var(--sp-5);
  border-radius: var(--r-lg);
  background: var(--c-surface);
  border: 1px solid var(--c-line);
  margin-bottom: var(--sp-4);
}
.about-brand { font-size: 1.9rem; font-weight: 700; letter-spacing: .02em; }
.about-sub { margin-top: var(--sp-1); color: var(--c-text-2); font-size: var(--fs-md); }
.about-ver { margin-top: var(--sp-4); display: flex; align-items: baseline; gap: var(--sp-2); flex-wrap: wrap; }
.about-build { font-size: var(--fs-sm); color: var(--c-text-3); }
.about-ver-tag { font-size: var(--fs-sm); color: var(--c-text-3); }
.about-ver-num { font-size: 1.4rem; font-weight: 700; color: var(--c-accent, #5b9dff); }
.about-actions { margin-top: var(--sp-4); display: flex; gap: var(--sp-2); flex-wrap: wrap; }
.about-local { margin-top: var(--sp-4); font-size: var(--fs-sm); }

/* ---- 关于页：次区块 ---- */
.sub-block { padding: var(--sp-3) 0; border-bottom: 1px solid var(--c-line); }
.sub-block:first-child { padding-top: 0; }
.sub-block:last-child { padding-bottom: 0; border-bottom: none; }
.sub-title { font-weight: 600; margin-bottom: var(--sp-1); }
.sub-block .btn.tiny { margin-top: var(--sp-1); }

/* ---- 远程：防火墙提示 ---- */
.fw-tip {
  margin-bottom: var(--sp-4);
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--c-warn-line, #6b5320);
  background: var(--c-warn-soft, rgba(255,180,84,.10));
  border-radius: var(--r-md);
}
.fw-tip-title { font-weight: 700; color: var(--c-warn, #ffb454); margin-bottom: var(--sp-1); }
.fw-tip-body { font-size: var(--fs-sm); color: var(--c-text-2); line-height: 1.6; }
.fw-cmd {
  display: block;
  margin-top: var(--sp-2);
  padding: var(--sp-2);
  background: var(--c-surface-2);
  border-radius: var(--r-sm);
  font-size: var(--fs-xs);
  color: var(--c-text);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
