<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { state } from '../state.js'
import {
  getConfig, putConfig, listProviders, testScraper as apiTest,
  parsePreview as apiParse, fsList, sniffCovers, csvUrl, cacheAvatars,
  fillActressAvatars, rescanLocalCovers,
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
    cfg.library = Object.assign({ paths: [], min_size_mb: 0, ignore_keywords: [], video_extensions: [] }, c.library)
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

onMounted(load)
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

      <!-- ============ 关于 ============ -->
      <template v-else>
        <div class="panel">
          <div class="panel-head">{{ $t('settings.exportData') }}</div>
          <div class="panel-body">
            <p class="muted">{{ $t('settings.exportCsvDesc') }}</p>
            <div><a class="btn" :href="csvUrl()" target="_blank">{{ $t('settings.exportCsv') }}</a></div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head">{{ $t('settings.backupMig') }}</div>
          <div class="panel-body">
            <p class="muted">{{ $t('settings.backupMigDesc') }}</p>
            <ul class="tips">
              <li><b>{{ $t('settings.tipUpgrade').split('：')[0] }}</b>：{{ $t('settings.tipUpgrade').split('：').slice(1).join('：') }}</li>
              <li><b>{{ $t('settings.tipMove').split('：')[0] }}</b>：{{ $t('settings.tipMove').split('：').slice(1).join('：') }}</li>
              <li><b>{{ $t('settings.tipCustom').split('：')[0] }}</b>：{{ $t('settings.tipCustom').split('：').slice(1).join('：') }}</li>
              <li><b>{{ $t('settings.tipCsv').split('：')[0] }}</b>：{{ $t('settings.tipCsv').split('：').slice(1).join('：') }}</li>
            </ul>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head">{{ $t('settings.aboutTitle') }}</div>
          <div class="panel-body">
            <p><b>{{ $t('settings.brand') }}</b> — {{ $t('settings.aboutSub') }}</p>
            <p class="muted">{{ $t('settings.aboutLocal') }}</p>
            <div class="kbd-list">
              <div class="section-title">{{ $t('settings.shortcuts') }}</div>
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
</style>
