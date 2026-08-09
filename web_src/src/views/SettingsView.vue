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

const { runScan, runScrape } = useTasks()

const TABS = [
  ['library', '媒体库'],
  ['scraper', '刮削数据源'],
  ['parser', '文件名解析'],
  ['ai', 'AI 增强'],
  ['appearance', '外观'],
  ['about', '关于'],
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
    toast('媒体库设置已保存', 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') } finally { saving.value = false }
}

const newPath = ref('')
async function addPath() {
  const v = newPath.value.trim()
  if (!v) return
  if ((cfg.library.paths || []).includes(v)) { toast('该目录已存在', 'err'); return }
  cfg.library.paths.push(v)
  newPath.value = ''
  await saveLibrary()
}
async function delPath(i) {
  const p = cfg.library.paths[i]
  if (!(await confirmDialog('移除目录', `将不再扫描：\n${p}\n已入库影片不会被删除。`, { danger: true, okText: '移除' }))) return
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
    toast(`检查 ${r.checked || 0} 部，新增 ${r.found || 0} 张封面`, 'ok')
  } catch (e) { toast(e.message, 'err') }
}

// 把仍是远程 URL 的女优头像批量下载落盘到自定义 avatar_dir
const cachingAvatars = ref(false)
async function doCacheAvatars() {
  if (!cfg.media.avatar_download) {
    toast('请先开启「下载女优头像」开关并保存，再执行缓存', 'warn')
    return
  }
  const ok = await confirmDialog(
    '批量缓存女优头像',
    '将把数据库中仍以远程 URL 存储的女优头像下载到本地头像目录。此操作需联网，建议先配置代理。是否继续？',
    { danger: true, okText: '开始下载' },
  )
  if (!ok) return
  cachingAvatars.value = true
  try {
    const r = await cacheAvatars()
    toast(`已缓存 ${r.downloaded || 0} 张头像，失败 ${r.failed || 0} 张`, 'ok')
  } catch (e) { toast(e.message, 'err') } finally { cachingAvatars.value = false }
}

// 通过重新抓取已刮削影片的元数据，补全女优头像（落盘到本地），需联网+代理
const fillingAvatars = ref(false)
async function doFillActressAvatars() {
  const ok = await confirmDialog(
    '补全女优头像',
    '将重新抓取已刮削影片的元数据，补全女优头像并落盘到本地目录。此操作会联网请求数据源，建议先配置代理。是否继续？',
    { danger: true, okText: '开始补全' },
  )
  if (!ok) return
  fillingAvatars.value = true
  try {
    const r = await fillActressAvatars()
    toast(`扫描 ${r.movies_scanned || 0} 部影片，补全 ${r.filled || 0} 个女优头像（剩余空 ${r.actresses_after_empty || 0}）`, 'ok')
  } catch (e) { toast(e.message, 'err') } finally { fillingAvatars.value = false }
}

// 重新嗅探本地已有的封面图片，命中则落盘写回（无需全量重扫）
const rescanningCovers = ref(false)
async function doRescanLocalCovers() {
  rescanningCovers.value = true
  try {
    const r = await rescanLocalCovers()
    toast(`扫描 ${r.movies_scanned || 0} 部影片，补上 ${r.filled || 0} 张本地封面（剩余无封面 ${r.covers_after_empty || 0}）`, 'ok')
  } catch (e) { toast(e.message, 'err') } finally { rescanningCovers.value = false }
}

/* ---------------- 刮削 ---------------- */
async function saveScraper() {
  let hj, hh
  try { hj = JSON.parse(jsonText.value || '{}') } catch (e) { toast('JSON 模板格式有误：' + e.message, 'err'); return }
  try { hh = JSON.parse(htmlText.value || '{}') } catch (e) { toast('HTML 模板格式有误：' + e.message, 'err'); return }

  const order = providers.available.filter((p) => provOn[p.id]).map((p) => p.id)
  if (!order.length) { toast('请至少启用一个数据源', 'err'); return }

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
    toast('数据源配置已保存', 'ok')
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
  testOut.lines = ['测试中…']
  testOut.cls = ''
  try {
    const order = providers.available.filter((p) => provOn[p.id]).map((p) => p.id)
    const override = { scraper: { order, proxy: cfg.scraper.proxy || '', chrome_debug_port: Number(cfg.scraper.chrome_debug_port) || 9222 } }
    const aw = cfg.scraper.avwiki || {}
    if (aw.base_url || aw.cookie) override.scraper.avwiki = { ...aw }
    if (cfg.scraper.javbus.base_url || cfg.scraper.javbus.cookie) override.scraper.javbus = { ...cfg.scraper.javbus }
    if (cfg.scraper.javdb.base_url || cfg.scraper.javdb.cookie) override.scraper.javdb = { ...cfg.scraper.javdb }

    const r = await apiTest({ code: testCode.value.trim() || undefined, override })
    const lines = [`测试番号：${r.code || '(库内第一个)'}`]
    let hit = false
    ;(r.results || []).forEach((res) => {
      if (res.ok) {
        hit = true
        const f = res.fields || {}
        const bits = []
        if (f.title) bits.push('标题：' + f.title)
        if (f.actresses && f.actresses.length) bits.push('女优：' + f.actresses.join('、'))
        if (f.genres && f.genres.length) bits.push('类型：' + f.genres.join('、'))
        if (f.studio) bits.push('厂商：' + f.studio)
        lines.push(`✓ [${res.provider}] ${bits.join('；') || '已连接'}`)
      } else {
        lines.push(`✗ [${res.provider}] ${res.reason || '失败'}`)
      }
    })
    if (r.cover_ok) lines.push('✓ 封面下载成功')
    else if (r.cover_url) lines.push('✗ 封面下载失败')
    testOut.lines = lines
    testOut.cls = hit ? 'ok' : 'err'
  } catch (e) {
    testOut.lines = ['请求失败：' + (e.message || e)]
    testOut.cls = 'err'
  } finally { testing.value = false }
}

/* ---------------- 解析预览 ---------------- */
const parseInput = ref('')
const parseRows = ref([])
async function runParse() {
  const names = parseInput.value.split('\n').map((s) => s.trim()).filter(Boolean)
  if (!names.length) { toast('请先输入文件名', 'err'); return }
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
      <h1 class="tb-title">设置</h1>
      <span v-if="loading || saving" class="spinner"></span>
      <div class="spacer"></div>
      <div class="tabs st-tabs">
        <button v-for="[k, l] in TABS" :key="k" class="tab" :class="{ on: tab === k }" @click="tab = k">{{ l }}</button>
      </div>
    </div>

    <div class="view-body block-scroll">
      <!-- ============ 媒体库 ============ -->
      <template v-if="tab === 'library'">
        <div class="panel">
          <div class="panel-head">扫描目录 <span class="sub">影片文件所在的文件夹</span></div>
          <div class="panel-body">
            <ul v-if="(cfg.library.paths || []).length" class="path-list">
              <li v-for="(p, i) in cfg.library.paths" :key="i">
                <span class="pi">📁</span>
                <span class="pp ellipsis" :title="p">{{ p }}</span>
                <button class="btn tiny ghost" @click="delPath(i)">移除</button>
              </li>
            </ul>
            <p v-else class="muted">还没有添加任何目录，添加后即可扫描导入影片。</p>

            <div class="hstack">
              <input v-model="newPath" placeholder="输入目录路径，如 D:\Movies" @keydown.enter="addPath" />
              <button class="btn" @click="browse('')">浏览…</button>
              <button class="btn primary" @click="addPath">添加</button>
            </div>
          </div>
          <div class="panel-foot">
            <button class="btn primary" @click="runScan({})">立即扫描</button>
            <button class="btn" @click="runScrape({ missing_only: true })">刮削缺失元数据</button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">扫描规则</div>
          <div class="panel-body">
            <div class="field-row">
              <label>最小文件体积</label>
              <div class="hstack">
                <input type="number" v-model="cfg.library.min_size_mb" min="0" style="width:120px" />
                <span class="muted">MB，小于此体积的文件会被忽略</span>
              </div>
            </div>
            <div class="field">
              <label>忽略关键词</label>
              <input v-model="ignoreText" placeholder="sample, trailer, 预告" />
              <span class="hint">文件名包含这些词时跳过，用逗号分隔</span>
            </div>
            <div class="field">
              <label>视频扩展名</label>
              <input v-model="extText" placeholder=".mp4, .mkv, .avi" />
              <span class="hint">只扫描这些格式，用逗号分隔</span>
            </div>
            <div class="field">
              <label>ffmpeg 路径</label>
              <input v-model="cfg.ffmpeg_path" placeholder="留空则使用系统 PATH 中的 ffmpeg" />
              <span class="hint">用于生成预览图和读取视频信息</span>
            </div>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="saving" @click="saveLibrary">保存</button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">封面</div>
          <div class="panel-body">
            <div class="field-row checkbox">
              <label>自动嗅探本地封面</label>
              <label class="toggle">
                <input type="checkbox" v-model="cfg.cover.auto_local" /><span class="track"></span>
              </label>
            </div>
            <div class="field-row checkbox">
              <label>联网下载封面</label>
              <label class="toggle">
                <input type="checkbox" v-model="cfg.cover.download" /><span class="track"></span>
              </label>
            </div>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="saving" @click="saveLibrary">保存</button>
            <button class="btn" @click="doSniff">立即嗅探本地封面</button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">媒体资源路径 <span class="sub">封面已落盘；头像 / 背景图可自定义目录，便于独立磁盘与迁移</span></div>
          <div class="panel-body">
            <div class="field">
              <label>女优头像目录</label>
              <input v-model="cfg.media.avatar_dir" placeholder="avatars（相对 data/，或填绝对路径）" />
              <span class="hint">相对路径基于 data/ 目录；填绝对路径可放到独立磁盘。可整体迁移，只需改此处指向</span>
            </div>
            <div class="field-row checkbox">
              <label>下载女优头像（落盘到本地）</label>
              <label class="toggle">
                <input type="checkbox" v-model="cfg.media.avatar_download" /><span class="track"></span>
              </label>
            </div>
            <div class="field">
              <label>背景大图（fanart）目录</label>
              <input v-model="cfg.media.fanart_dir" placeholder="fanarts（相对 data/，或填绝对路径）" />
              <span class="hint">影片背景大图的落盘目录，支持自定义与迁移</span>
            </div>
            <div class="field-row checkbox">
              <label>下载背景大图（fanart）</label>
              <label class="toggle">
                <input type="checkbox" v-model="cfg.media.fanart_download" /><span class="track"></span>
              </label>
            </div>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="saving" @click="saveLibrary">保存</button>
            <button class="btn" :disabled="cachingAvatars" @click="doCacheAvatars">
              {{ cachingAvatars ? '缓存中…' : '批量缓存现有女优头像' }}
            </button>
            <button class="btn" :disabled="fillingAvatars" @click="doFillActressAvatars">
              {{ fillingAvatars ? '补全中…' : '补全女优头像（联网）' }}
            </button>
            <button class="btn" :disabled="rescanningCovers" @click="doRescanLocalCovers">
              {{ rescanningCovers ? '嗅探中…' : '重嗅探本地封面' }}
            </button>
          </div>
        </div>
      </template>

      <!-- ============ 刮削 ============ -->
      <template v-else-if="tab === 'scraper'">
        <div class="panel">
          <div class="panel-head">数据源 <span class="sub">按顺序尝试，先命中先采用</span></div>
          <div class="panel-body">
            <div v-for="(p, i) in providers.available" :key="p.id" class="prov">
              <label class="toggle">
                <input type="checkbox" v-model="provOn[p.id]" /><span class="track"></span>
              </label>
              <div class="prov-main">
                <div class="prov-name">
                  {{ p.label }}
                  <code>{{ p.id }}</code>
                  <span v-if="providers.active.includes(p.id)" class="badge ok">生效中</span>
                </div>
                <div class="prov-desc">{{ p.desc }}</div>
              </div>
              <div class="prov-ord">
                <button class="btn tiny ghost" :disabled="i === 0" @click="moveProvider(p.id, -1)" data-tip="上移">↑</button>
                <button class="btn tiny ghost" :disabled="i === providers.available.length - 1" @click="moveProvider(p.id, 1)" data-tip="下移">↓</button>
              </div>
            </div>
            <p v-if="!providers.available.length" class="muted">未检测到可用数据源</p>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">请求设置</div>
          <div class="panel-body">
            <div class="field-row">
              <label>超时时间</label>
              <div class="hstack"><input type="number" v-model="cfg.scraper.timeout" style="width:100px" /><span class="muted">秒</span></div>
            </div>
            <div class="field-row">
              <label>请求间隔</label>
              <div class="hstack"><input type="number" v-model="cfg.scraper.delay_ms" style="width:100px" /><span class="muted">毫秒，避免请求过快被封</span></div>
            </div>
            <div class="field-row">
              <label>并发线程数</label>
              <div class="hstack"><input type="number" min="1" max="16" v-model="cfg.scraper.workers" style="width:100px" /><span class="muted">同时刮削影片数，2-8 较稳妥，越高越快但易被限流</span></div>
            </div>
            <div class="field-row">
              <label>代理</label>
              <input v-model="cfg.scraper.proxy" placeholder="http://127.0.0.1:7890" />
            </div>
            <div class="field-row checkbox">
              <label>覆盖已有数据</label>
              <label class="toggle"><input type="checkbox" v-model="cfg.scraper.overwrite" /><span class="track"></span></label>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">站点参数</div>
          <div class="panel-body">
            <div class="site">
              <b>AV-Wiki</b>
              <div class="field"><label>Base URL</label><input v-model="cfg.scraper.avwiki.base_url" placeholder="https://av-wiki.net" /></div>
              <div class="field"><label>Cookie</label><input v-model="cfg.scraper.avwiki.cookie" placeholder="可选，如 cf_clearance=xxx; 用于绕过 Loader 验证" /></div>
              <div class="field"><label>Chrome 调试端口</label><input v-model="cfg.scraper.chrome_debug_port" type="number" style="width:120px" /><span class="muted">默认 9222。后端会自动拉起一个独立的无窗口 Chrome（profile 存于 data/chrome_profile）抓取 av-wiki，绕过「请稍候」验证；完全后台运行，无需手动开浏览器（需本机安装 Chrome）。</span></div>
            </div>
            <div class="site">
              <b>JavBus</b>
              <div class="field"><label>Base URL</label><input v-model="cfg.scraper.javbus.base_url" placeholder="https://www.javbus.com" /></div>
              <div class="field"><label>Cookie</label><input v-model="cfg.scraper.javbus.cookie" placeholder="可选，用于绕过验证" /></div>
            </div>
            <div class="site">
              <b>JavDB</b>
              <div class="field"><label>Base URL</label><input v-model="cfg.scraper.javdb.base_url" placeholder="https://javdb.com" /></div>
              <div class="field"><label>Cookie</label><input v-model="cfg.scraper.javdb.cookie" placeholder="可选" /></div>
            </div>

            <details class="adv">
              <summary>高级：自定义 JSON 数据源模板</summary>
              <textarea v-model="jsonText" rows="7" spellcheck="false"></textarea>
            </details>
            <details class="adv">
              <summary>高级：自定义 HTML 数据源模板</summary>
              <textarea v-model="htmlText" rows="7" spellcheck="false"></textarea>
            </details>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="saving" @click="saveScraper">保存数据源配置</button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">连通性测试</div>
          <div class="panel-body">
            <div class="hstack">
              <input v-model="testCode" placeholder="测试番号，留空使用库内第一个" style="max-width:280px" />
              <button class="btn" :disabled="testing" @click="runTest">{{ testing ? '测试中…' : '开始测试' }}</button>
            </div>
            <pre v-if="testOut.lines.length" class="test-out" :class="testOut.cls">{{ testOut.lines.join('\n') }}</pre>
          </div>
        </div>
      </template>

      <!-- ============ AI 增强 ============ -->
      <template v-else-if="tab === 'ai'">
        <div class="panel">
          <div class="panel-head">
            AI 增强（可选）
            <span class="sub">兼容 OpenAI 协议的任意端点：云端 / 国产中转 / 本地 Ollama</span>
          </div>
          <div class="panel-body">
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="cfg.ai.enabled" /><span class="track"></span>
              </label>
              <span>启用 AI（生成简介 / 建议标签 / 语义搜索）</span>
            </div>
            <div class="field">
              <label>Base URL</label>
              <input v-model="cfg.ai.base_url" placeholder="https://api.openai.com/v1 或 http://127.0.0.1:11434/v1" />
              <span class="hint">本地 Ollama 填 http://127.0.0.1:11434/v1（需先 ollama pull qwen2.5，免 key）</span>
            </div>
            <div class="field">
              <label>API Key</label>
              <input v-model="cfg.ai.api_key" type="password" placeholder="云端/中转必填；本地 Ollama 留空" />
            </div>
            <div class="field">
              <label>模型</label>
              <input v-model="cfg.ai.model" placeholder="gpt-4o-mini / deepseek-chat / qwen2.5" />
            </div>
            <div class="field">
              <label>温度</label>
              <input type="number" step="0.1" min="0" max="1" v-model.number="cfg.ai.temperature" style="width:100px" />
              <span class="muted">越低越稳定，越高越发散</span>
            </div>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="saving" @click="save">保存 AI 配置</button>
            <span v-if="aiOk===true" class="ok">配置已保存</span>
            <span v-else-if="aiOk===false" class="err">保存失败</span>
          </div>
        </div>
      </template>

      <!-- ============ 解析预览 ============ -->
      <template v-else-if="tab === 'parser'">
        <div class="panel">
          <div class="panel-head">
            文件名解析预览
            <span class="sub">检验番号识别规则是否覆盖你的命名习惯</span>
          </div>
          <div class="panel-body">
            <textarea v-model="parseInput" rows="7" placeholder="每行一个文件名，例如：&#10;ABP-123 中文字幕.mp4&#10;[JAVBUS]SSIS-456-CD1.mkv" spellcheck="false"></textarea>
            <div class="hstack">
              <button class="btn primary" @click="runParse">解析</button>
              <span v-if="parseRows.length" class="muted">
                识别 {{ parseStats.ok }} / {{ parseStats.n }} · 成功率 {{ parseStats.rate }}%
              </span>
            </div>

            <table v-if="parseRows.length" class="ptable">
              <thead><tr><th>番号</th><th>规则</th><th>原始文件名</th></tr></thead>
              <tbody>
                <tr v-for="(r, i) in parseRows" :key="i">
                  <td><span class="badge" :class="r.matched ? 'ok' : 'err'">{{ r.matched ? r.code : '未识别' }}</span></td>
                  <td class="muted">
                    {{ r.rule || '—' }}
                    <span v-if="r.part > 1"> · CD{{ r.part }}</span>
                    <span v-if="r.subtitle"> · 字幕</span>
                    <span v-if="r.uncensored"> · 无码</span>
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
          <div class="panel-head">主题与显示</div>
          <div class="panel-body">
            <div class="field-row">
              <label>主题</label>
              <div class="btn-group">
                <button class="btn tiny" :class="{ active: state.theme === 'dark' }" @click="state.theme = 'dark'">深色</button>
                <button class="btn tiny" :class="{ active: state.theme === 'light' }" @click="state.theme = 'light'">浅色</button>
              </div>
            </div>
            <div class="field-row">
              <label>界面密度</label>
              <div class="btn-group">
                <button class="btn tiny" :class="{ active: state.density === 'cozy' }" @click="state.density = 'cozy'">舒适</button>
                <button class="btn tiny" :class="{ active: state.density === 'compact' }" @click="state.density = 'compact'">紧凑</button>
              </div>
            </div>
            <div class="field-row">
              <label>封面尺寸</label>
              <div class="btn-group">
                <button class="btn tiny" :class="{ active: state.cardSize === 'dense' }" @click="state.cardSize = 'dense'">小</button>
                <button class="btn tiny" :class="{ active: state.cardSize === 'normal' }" @click="state.cardSize = 'normal'">中</button>
                <button class="btn tiny" :class="{ active: state.cardSize === 'large' }" @click="state.cardSize = 'large'">大</button>
              </div>
            </div>
            <div class="field-row">
              <label>每页数量</label>
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
          <div class="panel-head">数据导出</div>
          <div class="panel-body">
            <p class="muted">导出全部影片元数据为 CSV，可用 Excel 打开备份。</p>
            <div><a class="btn" :href="csvUrl()" target="_blank">导出 CSV</a></div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head">备份与迁移（升级不丢数据）</div>
          <div class="panel-body">
            <p class="muted">所有收藏数据都保存在程序目录下的 <code>data/</code> 文件夹（含 <code>library.db</code> 数据库、配置、封面、头像）。</p>
            <ul class="tips">
              <li><b>升级</b>：用新版本 <code>片匣.exe</code> <b>覆盖旧 exe 即可</b>，不要删除 <code>data/</code> 文件夹，收藏、设置、观看进度全部保留。</li>
              <li><b>换电脑 / 备份</b>：直接把整个程序文件夹（含 <code>data/</code>）整体拷贝即可。程序每次启动会自动为 <code>library.db</code> 留一份每日备份（最近 7 天），多一层保险。</li>
              <li><b>自定义目录</b>：若把头像/背景图设到了独立磁盘（设置 → 媒体资源路径），迁移时需在目标机同样挂载该路径，或改回相对 <code>data/</code> 的路径。</li>
              <li><b>导出兜底</b>：下方「数据导出」可把元数据导出为 CSV，作为纯文本备份。</li>
            </ul>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head">关于</div>
          <div class="panel-body">
            <p><b>片匣 (AVM)</b> — 本地 AV 收藏管理工具</p>
            <p class="muted">所有数据均保存在本地，不会上传到任何服务器。</p>
            <div class="kbd-list">
              <div class="section-title">快捷键</div>
              <div class="kb"><kbd>Ctrl</kbd>+<kbd>K</kbd> 或 <kbd>/</kbd><span>聚焦搜索框</span></div>
              <div class="kb"><kbd>Esc</kbd><span>关闭详情 / 弹窗</span></div>
              <div class="kb"><kbd>←</kbd> <kbd>→</kbd><span>滑动评分：跳过 / 想看</span></div>
              <div class="kb"><kbd>1</kbd>–<kbd>5</kbd><span>滑动评分：打分</span></div>
              <div class="kb"><kbd>F</kbd> / <kbd>Z</kbd><span>滑动评分：收藏 / 撤销</span></div>
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- 目录浏览器 -->
    <Teleport to="body">
      <div v-if="browser.open" class="modal-mask" @click.self="browser.open = false">
        <div class="modal">
          <div class="modal-head">选择目录</div>
          <div class="modal-body">
            <div class="bc">
              <button class="btn tiny ghost" @click="browse('')">根目录</button>
              <code class="bc-path">{{ browser.path || '/' }}</code>
            </div>
            <ul class="dir-list">
              <li v-for="d in browser.dirs" :key="d.path" @click="browse(d.path)">
                <span>📁</span><span class="ellipsis">{{ d.name }}</span>
              </li>
              <li v-if="!browser.dirs.length" class="muted nohover">该目录下没有子文件夹</li>
            </ul>
          </div>
          <div class="modal-foot">
            <button class="btn" @click="browser.open = false">取消</button>
            <button class="btn primary" :disabled="!browser.path" @click="pickDir">选择当前目录</button>
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
