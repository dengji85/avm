<script setup>
import { ref, reactive, computed, onBeforeUnmount, onMounted, watch } from 'vue'
import { state } from '../state.js'
import ExtPanel from '../components/ExtPanel.vue'
import MetatubePanel from '../components/MetatubePanel.vue'
import {
  API, getConfig, putConfig, listProviders, testScraper as apiTest, aiAsrStatus,
  parsePreview as apiParse, fsList, sniffCovers, extractAllCovers, csvUrl, cacheAvatars,
  fillActressAvatars, rescanLocalCovers, fetchActressProfile, getServerInfo, revealToken, rotateToken, checkUpdate,
  posterTaskStart, posterTaskState, posterTaskCancel,
  fsMounts,
  listPlugins, togglePlugin, regenAllPreviews, streamCacheStats, clearStreamCache,
  listSessions, revokeSession, getAudit, logout as apiLogout, logoutAll as apiLogoutAll,
  setPin, privacyState, privacyLock, trustedDevices, revokeTrusted,
  totpStatus, totpSetup, totpEnable, totpDisable,
  listApiKeys, createApiKey, revokeApiKey,
  embyStatus, embyTest, embyRefresh,
} from '../api.js'
import { toast, confirmDialog, fmtAgo, fmtDateTime, prettyHostPath, setSiteTitle } from '../utils.js'
import { useTasks } from '../composables/useTasks.js'
import { LANGUAGES, i18nState, setLang } from '../i18n'
import { t } from '../i18n/index.js'

const { runScan, runScrape } = useTasks()

const TABS = [
  ['library', 'settings.tab.library'],
  ['scraper', 'settings.tab.scraper'],
  ['parser', 'settings.tab.parser'],
  ['appearance', 'settings.tab.appearance'],
  ['sites', 'settings.tab.sites'],
  ['remote', 'settings.tab.remote'],
  ['ext', 'settings.tab.ext'],
  ['privacy', 'settings.tab.privacy'],
  ['about', 'settings.tab.about'],
]
const tab = ref('library')
/* 其他页面（如解锁码提示条）可以请求直接打开某个标签 */
watch(() => state.settingsTab, (v) => {
  if (!v) return
  tab.value = v
  state.settingsTab = ''
}, { immediate: true })

/* 预置结构，避免异步加载前模板访问 undefined */
const cfg = reactive({
  library: { paths: [], min_size_mb: 0, ignore_keywords: [], video_extensions: [], missing_grace_days: 30, delete_to_recycle_bin: true },
  cover: { auto_local: false, download: false },
  media: { avatar_dir: 'avatars', fanart_dir: 'fanarts', avatar_download: false,
    fanart_download: true, poster_strategy: 'right', poster_dmm: true },
  // 导出到媒体目录的工具整体搬到了 维护页 → 导出（含其配置），这里只留 Emby 联动
  emby: {
    enabled: false, base_url: '', api_key: '', user_id: '',
    refresh_mode: 'library', refresh_debounce_sec: 30,
  },
  scraper: {
    order: [], timeout: 20, delay_ms: 0, workers: 4, proxy: '', overwrite: false,
    chrome_debug_port: 9222,
    avwiki: { base_url: '', cookie: '' },
    javbus: { base_url: '', cookie: '' },
    javdb: { base_url: '', cookie: '' },
    http_json: {}, http_html: {},
  },
  ffmpeg_path: '',
  previews_quality: 'smart',
  ai: { enabled: false, base_url: 'https://api.openai.com/v1', api_key: '', model: 'gpt-4o-mini', temperature: 0.4 },
  asr: { libs_dir: '', model_dir: '', tmp_dir: '', model: 'small', language: 'ja' },
  stream: { auto_remux: true, remux_cache_mb: 4096 },
  wiki: { languages: ['ja', 'zh'] },
  online_sites: [],
  server: {
    session_idle_days: 7, session_abs_days: 30, session_persist: false,
    expose_docs: false, trusted_proxies: [],
  },
  privacy: {
    idle_lock_minutes: 10, lock_unlock: 'token', panic_key: 'Ctrl+Shift+H',
    neutral_title: true, pause_when_hidden: true, lock_on_start: false,
    site_subtitle: '',
  },
})

/* 常见站点预设：首次进入或点「添加常见站点」时一次性补入。
   lower=true 表示跳转前把 {code} 转为小写（如 JavBus / 123av 的路径为小写番号）。 */
const PRESET_SITES = [
  { id: 'preset_javbus', name: 'JavBus', url: 'https://javbus.com/{code}', lower: true, enabled: true },
  { id: 'preset_javwiki', name: 'AV-Wiki', url: 'https://av-wiki.net/{code}/', enabled: true },
  { id: 'preset_javdb', name: 'JavDB', url: 'https://javdb.com/search?q={code}', lower: true, enabled: true },
  { id: 'preset_javlibrary', name: 'JavLibrary', url: 'https://www.javlibrary.com/cn/vl_searchbyid.php?keyword={code}', enabled: true },
  { id: 'preset_missav', name: '123av', url: 'https://123av.com/cn/v/{code}', lower: true, enabled: true },
  { id: 'preset_jable', name: 'Jable', url: 'https://jable.tv/videos/{code}/', lower: true, enabled: true },
]

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
    cfg.library = Object.assign({ paths: [], min_size_mb: 0, ignore_keywords: [], video_extensions: [], auto_scan_interval: 0, missing_grace_days: 30, delete_to_recycle_bin: true }, c.library)
    autoScan.value = Number(cfg.library.auto_scan_interval) > 0
    autoScanInterval.value = Number(cfg.library.auto_scan_interval) > 0 ? Number(cfg.library.auto_scan_interval) : 10
    cfg.cover = Object.assign({ auto_local: false, download: false }, c.cover)
    cfg.stream = Object.assign({ auto_remux: true, remux_cache_mb: 4096 }, c.stream)
    cfg.media = Object.assign(
      { avatar_dir: 'avatars', fanart_dir: 'fanarts', avatar_download: false, fanart_download: true },
      c.media,
    )
    cfg.emby = Object.assign(
      { enabled: false, base_url: '', api_key: '', user_id: '', refresh_mode: 'library', refresh_debounce_sec: 30 },
      c.emby,
    )
    // 后端不下发 api_key，只能问「有没有设过」
    loadEmby()
    cfg.scraper = Object.assign(
      { order: [], timeout: 20, delay_ms: 0, workers: 4, proxy: '', overwrite: false, chrome_debug_port: 9222, avwiki: {}, javbus: {}, javdb: {}, http_json: {}, http_html: {} },
      c.scraper,
    )
    cfg.scraper.avwiki = Object.assign({ base_url: '', cookie: '' }, cfg.scraper.avwiki)
    cfg.scraper.javbus = Object.assign({ base_url: '', cookie: '' }, cfg.scraper.javbus)
    cfg.scraper.javdb = Object.assign({ base_url: '', cookie: '' }, cfg.scraper.javdb)
    cfg.wiki = Object.assign({ languages: ['ja', 'zh'] }, c.wiki)
    cfg.online_sites = Array.isArray(c.online_sites) ? c.online_sites : []
    // 首次使用（后端尚无任何站点配置）时，预填一组常见站点，用户可自由增删改
    if (!cfg.online_sites.length && !c.online_sites) {
      cfg.online_sites = PRESET_SITES.map((s) => ({ ...s }))
    }
    cfg.previews_quality = c.previews_quality || 'smart'

    ignoreText.value = (cfg.library.ignore_keywords || []).join(', ')
    extText.value = (cfg.library.video_extensions || []).join(', ')
    jsonText.value = JSON.stringify(cfg.scraper.http_json || {}, null, 2)
    htmlText.value = JSON.stringify(cfg.scraper.http_html || {}, null, 2)

    const pv = await listProviders()
    // 按用户已保存的 order 重排数据源。否则每次打开设置页都回到后端注册顺序，
    // 用户调整过的优先级（例如把本地 NFO 拖到最前）看起来就像没保存成功。
    const orderList = cfg.scraper.order || []
    const orderIdx = new Map(orderList.map((id, i) => [id, i]))
    providers.available = (pv.available || []).slice().sort((a, b) => {
      const ia = orderIdx.has(a.id) ? orderIdx.get(a.id) : Number.MAX_SAFE_INTEGER
      const ib = orderIdx.has(b.id) ? orderIdx.get(b.id) : Number.MAX_SAFE_INTEGER
      return ia - ib
    })
    providers.active = pv.active || []
    providers.available.forEach((p) => { provOn[p.id] = orderList.includes(p.id) })
    // 安全 / 会话配置（server 段）
    cfg.server = Object.assign(
      {
        session_idle_days: 7, session_abs_days: 30, session_persist: false,
        expose_docs: false, trusted_proxies: [],
        insecure_skip_verify: false,
      },
      c.server,
    )
    trustedProxiesText.value = (cfg.server.trusted_proxies || []).join(', ')
    // 隐私保护（桌面防窥）
    cfg.privacy = Object.assign(
      {
        idle_lock_minutes: 10, lock_unlock: 'token', panic_key: 'Ctrl+Shift+H',
        neutral_title: true, pause_when_hidden: true, lock_on_start: false,
        site_subtitle: '',
      },
      c.privacy,
    )
    loadStreamCache()
    loadSecurity()
    loadAsr()
    // 先拿到「是否已设置简单密码」，否则解锁方式下拉的校验会误判
    await loadPinStatus()
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
      // ffmpeg_path 已迁到「AI 中心 → 扩展组件」编辑；这里不回写，避免用旧值覆盖
      previews_quality: cfg.previews_quality || 'smart',
      library: {
        paths: cfg.library.paths || [],
        min_size_mb: Number(cfg.library.min_size_mb) || 0,
        ignore_keywords: ignoreText.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        video_extensions: extText.value.split(/[,，]/).map((s) => s.trim().toLowerCase())
          .filter(Boolean).map((s) => (s.startsWith('.') ? s : '.' + s)),
        auto_scan_interval: autoScan.value ? Math.max(1, Number(autoScanInterval.value) || 10) : 0,
        missing_grace_days: Math.max(0, Number(cfg.library.missing_grace_days) || 0),
        delete_to_recycle_bin: !!cfg.library.delete_to_recycle_bin,
      },
      stream: {
        auto_remux: !!cfg.stream.auto_remux,
        remux_cache_mb: Math.max(0, Number(cfg.stream.remux_cache_mb) || 0),
      },
      cover: { auto_local: !!cfg.cover.auto_local, download: !!cfg.cover.download },
      media: {
        avatar_dir: (cfg.media.avatar_dir || 'avatars').toString().trim() || 'avatars',
        fanart_dir: (cfg.media.fanart_dir || 'fanarts').toString().trim() || 'fanarts',
        avatar_download: !!cfg.media.avatar_download,
        fanart_download: !!cfg.media.fanart_download,
        poster_strategy: cfg.media.poster_strategy || 'right',
        poster_dmm: cfg.media.poster_dmm !== false,
      },
    }
    await putConfig(patch)
    toast(t('settings.libSaved'), 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') } finally { saving.value = false }
}

/* ---------------- Emby 联动（导出工具整体搬到了维护页 → 导出） ---------------- */
const savingEmby = ref(false)

const embyHasKey = ref(false)
const embyMsg = ref('')
const testingEmby = ref(false)
const refreshingEmby = ref(false)

async function loadEmby() {
  try {
    const r = await embyStatus()
    embyHasKey.value = !!r.has_key
  } catch (e) { /* 非致命 */ }
}

async function saveEmby() {
  savingEmby.value = true
  try {
    const patch = {
      emby: {
        enabled: !!cfg.emby.enabled,
        base_url: (cfg.emby.base_url || '').toString().trim(),
        user_id: (cfg.emby.user_id || '').toString().trim(),
        refresh_mode: cfg.emby.refresh_mode || 'library',
        refresh_debounce_sec: Math.max(1, Number(cfg.emby.refresh_debounce_sec) || 30),
      },
    }
    // 留空 = 不修改已保存的 Key（后端也会剔除空串）
    const key = (cfg.emby.api_key || '').toString().trim()
    if (key) patch.emby.api_key = key
    await putConfig(patch)
    toast(t('settings.exportSaved'), 'ok')
    cfg.emby.api_key = ''
    await load()
  } catch (e) { toast(e.message, 'err') } finally { savingEmby.value = false }
}

async function doTestEmby() {
  testingEmby.value = true
  embyMsg.value = ''
  try {
    const r = await embyTest({ base_url: cfg.emby.base_url, api_key: cfg.emby.api_key })
    if (r.ok) {
      embyMsg.value = t('settings.embyOk', { v: [r.server_name, r.version].filter(Boolean).join(' ') })
      toast(embyMsg.value, 'ok')
    } else {
      embyMsg.value = r.error || t('settings.embyFail')
      toast(embyMsg.value, 'err')
    }
  } catch (e) {
    embyMsg.value = e.message
    toast(e.message, 'err')
  } finally {
    testingEmby.value = false
  }
}

async function doEmbyRefresh() {
  refreshingEmby.value = true
  try {
    const r = await embyRefresh()
    toast(r.ok ? t('settings.embyRefreshed') : (r.error || t('settings.embyFail')), r.ok ? 'ok' : 'err')
  } catch (e) { toast(e.message, 'err') } finally { refreshingEmby.value = false }
}

/* ---------------- 网页播放兼容（浏览器不认的容器 → 快速重封装） ---------------- */
const cacheMb = ref(0)
const clearingCache = ref(false)
async function loadStreamCache() {
  try {
    const r = await streamCacheStats()
    cacheMb.value = Math.round((r.bytes || 0) / 1024 / 1024)
  } catch (e) { /* 非致命 */ }
}
async function clearCacheNow() {
  clearingCache.value = true
  try {
    const r = await clearStreamCache()
    toast(t('settings.playCleared', { n: r.removed || 0 }), 'ok')
    await loadStreamCache()
  } catch (e) { toast(e.message, 'err') } finally { clearingCache.value = false }
}

/* ---------------- 登录与会话（安全，见 docs/安全与隐私设计.md） ---------------- */
const trustedProxiesText = ref('127.0.0.1, ::1')
const sessions = ref([])
const audit = ref([])
const savingSec = ref(false)

async function loadSecurity() {
  try {
    const r = await listSessions()
    sessions.value = r.items || []
  } catch (e) { sessions.value = [] }
  try {
    const a = await getAudit(30)
    audit.value = a.items || []
  } catch (e) { audit.value = [] }
  await Promise.all([loadTotp(), loadKeys()])
}

/* ---------------- 两步验证（TOTP） ---------------- */
const totp = reactive({ enabled: false, recovery_left: 0, pending: false })
const totpCodeInput = ref('')
const totpSetupInfo = ref(null)     // { secret, uri }
const totpQrSeq = ref(0)
const recoveryCodes = ref([])
const busyTotp = ref(false)
// 密钥由服务端暂存直接渲染，不经过 URL / 响应正文（避免进历史与反代日志）
// 前缀走 api.js 的 API 常量，避免子路径部署时这里漏改（表现为二维码静默 404）
const totpQrUrl = computed(() => `${API}/auth/totp/qr?_=${totpQrSeq.value}`)
onBeforeUnmount(() => {
  if (clockTimer) clearInterval(clockTimer)
  if (revealTimer) clearInterval(revealTimer)   // 令牌明文 30 秒倒计时，离开页面也要停
})

// 服务器时间：验证码对不上十有八九是手机时钟偏了，绑定面板里直接显示对比
const serverSkewMs = ref(0)
const clockNow = ref(Date.now())
let clockTimer = null
const serverClockText = computed(() => {
  const d = new Date(clockNow.value + serverSkewMs.value)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
})
function startClock() {
  stopClock()
  clockNow.value = Date.now()
  clockTimer = setInterval(() => { clockNow.value = Date.now() }, 1000)
}
function stopClock() {
  if (clockTimer) clearInterval(clockTimer)
  clockTimer = null
}

/** 密钥按 4 位分组，便于手动输入（微软验证器手动添加时最好抄这个） */
function groupSecret(s) {
  return String(s || '').replace(/\s+/g, '').replace(/(.{4})/g, '$1 ').trim()
}

async function loadTotp() {
  try { Object.assign(totp, await totpStatus()) } catch (e) { /* 未登录等场景忽略 */ }
}

async function startTotpBind() {
  busyTotp.value = true
  try {
    const r = await totpSetup()
    totpSetupInfo.value = { secret: r.secret, uri: r.uri }
    totpCodeInput.value = ''
    totpQrSeq.value += 1
    // 与服务器时钟对齐（只是显示用，校验仍在服务端）
    serverSkewMs.value = r.server_time ? (r.server_time * 1000 - Date.now()) : 0
    startClock()
    await loadTotp()
  } catch (e) { toast(e.message, 'err') } finally { busyTotp.value = false }
}

function cancelTotpBind() {
  totpSetupInfo.value = null
  totpCodeInput.value = ''
  stopClock()
}

async function confirmTotpBind() {
  // 允许 123 456 / 123-456 / 全角数字，服务端会统一规整
  const code = (totpCodeInput.value || '').replace(/[^\d０-９]/g, '')
  if (!code) { toast(t('settings.totpCodePh'), 'err'); return }
  busyTotp.value = true
  try {
    const r = await totpEnable(code)
    recoveryCodes.value = r.recovery_codes || []
    totpSetupInfo.value = null
    totpCodeInput.value = ''
    stopClock()
    await loadTotp()
    toast(t('settings.totpOn'), 'ok')
  } catch (e) { toast(e.message, 'err') } finally { busyTotp.value = false }
}

async function disableTotp() {
  const code = (totpCodeInput.value || '').trim()
  if (!code) { toast(t('settings.totpCodePh'), 'err'); return }
  const ok = await confirmDialog(t('settings.totpDisable'), t('settings.totpDisableHint'), { danger: true, okText: t('settings.totpDisable') })
  if (!ok) return
  busyTotp.value = true
  try {
    await totpDisable(code)
    recoveryCodes.value = []
    totpCodeInput.value = ''
    await loadTotp()
    toast(t('settings.totpDisabled'), 'ok')
  } catch (e) { toast(e.message, 'err') } finally { busyTotp.value = false }
}

/* ---------------- 事务 API Key ---------------- */
const apiKeys = ref([])
const newKeyName = ref('')
const newKeyScope = ref('full')
const createdKey = ref('')
const busyKey = ref(false)

async function loadKeys() {
  try { const r = await listApiKeys(); apiKeys.value = r.items || [] } catch (e) { apiKeys.value = [] }
}

async function doCreateKey() {
  busyKey.value = true
  try {
    const r = await createApiKey((newKeyName.value || '').trim(), newKeyScope.value)
    createdKey.value = r.key || ''
    newKeyName.value = ''
    await loadKeys()
    toast(t('settings.keyCreated'), 'ok')
  } catch (e) { toast(e.message, 'err') } finally { busyKey.value = false }
}

async function doRevokeKey(id) {
  const ok = await confirmDialog(t('settings.keyRevoke'), t('settings.keyRevokeHint'), { danger: true, okText: t('settings.keyRevoke') })
  if (!ok) return
  try { await revokeApiKey(id); await loadKeys(); toast(t('settings.keyRevoked'), 'ok') }
  catch (e) { toast(e.message, 'err') }
}

/* 关闭出站证书校验 = 失去中间人防护，属「降低安全性」的开关：
   勾选时先弹一次确认，取消就把勾选还原，避免误点。 */
async function onToggleInsecureTls(e) {
  const on = !!(e && e.target ? e.target.checked : cfg.server.insecure_skip_verify)
  if (!on) return
  const ok = await confirmDialog(
    t('settings.secInsecureTls'),
    t('settings.secInsecureTlsWarn'),
    { danger: true, okText: t('common.confirm') },
  )
  if (!ok) cfg.server.insecure_skip_verify = false
}

async function saveSecurity() {
  savingSec.value = true
  try {
    const list = trustedProxiesText.value.split(',').map((s) => s.trim()).filter(Boolean)
    await putConfig({
      server: {
        session_idle_days: Math.max(0, Number(cfg.server.session_idle_days) || 0),
        session_abs_days: Math.max(0, Number(cfg.server.session_abs_days) || 0),
        session_persist: !!cfg.server.session_persist,
        expose_docs: !!cfg.server.expose_docs,
        insecure_skip_verify: !!cfg.server.insecure_skip_verify,
        trusted_proxies: list,
      },
    })
    toast(t('settings.secSaved'), 'ok')
    await loadSecurity()
  } catch (e) { toast(e.message, 'err') } finally { savingSec.value = false }
}

/* ---------------- 隐私保护（桌面防窥） ---------------- */
/* 隐私设置改为「改动即自动保存」（debounce 500ms）：开关 / 下拉 / 数字输入
   都不必再滑到底部点按钮。简单密码属敏感输入，保留行内「设置 / 更新」按钮单独提交。 */
const savingPrivacy = ref(false)
let privacyTimer = null

async function savePrivacy(silent = true) {
  savingPrivacy.value = true
  try {
    await putConfig({
      privacy: {
        idle_lock_minutes: Math.max(0, Number(cfg.privacy.idle_lock_minutes) || 0),
        // 不在这里提交 lock_unlock：它由「设置/清除解锁码」接口维护。
        // 之前每次改空闲分钟数都会把它按前端旧值写回 token，导致设过 PIN 却又只给令牌输入框。
        panic_key: (cfg.privacy.panic_key || '').trim() || 'Ctrl+Shift+H',
        neutral_title: !!cfg.privacy.neutral_title,
        pause_when_hidden: !!cfg.privacy.pause_when_hidden,
        lock_on_start: !!cfg.privacy.lock_on_start,
        site_subtitle: String(cfg.privacy.site_subtitle || '').trim(),
      },
    })
    // 标题后缀改动立即生效（无需刷新）
    setSiteTitle(cfg.privacy.site_subtitle)
    // 让遮挡层立即重新读取配置（无需整页刷新）
    window.dispatchEvent(new CustomEvent('avm-privacy-changed'))
    if (!silent) toast(t('settings.secSaved'), 'ok')
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    savingPrivacy.value = false
  }
}

/* 保存只由控件事件（@change / @input）触发，不用 watch：
   watch 无法区分「用户改动」和「load() 重新赋值」，会在加载时误触发保存。 */
function scheduleSavePrivacy() {
  if (privacyTimer) clearTimeout(privacyTimer)
  privacyTimer = setTimeout(() => savePrivacy(true), 500)
}

/* 快速解锁码：明文只在提交瞬间传一次，服务端只保存 PBKDF2 哈希。
   设置成功后后端会同时把 unlock_mode 切到 pin；清除则回落到访问令牌。 */
const pinInput = ref('')
const showPin = ref(false)
const hasPin = ref(false)
const savingPin = ref(false)

async function loadPinStatus() {
  try {
    const st = await privacyState()
    hasPin.value = !!(st && st.has_pin)
  } catch (e) {
    // 读取失败时保持原值，避免误判成「尚未设置」
  }
}

async function savePin() {
  const v = pinInput.value.trim()
  if (v.length < 4) { toast(t('privacy.pinSetPh'), 'err'); return }
  savingPin.value = true
  try {
    const r = await setPin(v)
    hasPin.value = !!(r && r.has_pin)
    cfg.privacy.lock_unlock = hasPin.value ? 'pin' : 'token'
    pinInput.value = ''
    toast(t('privacy.pinSaved'), 'ok')
  } catch (e) { toast(e.message, 'err') } finally { savingPin.value = false }
}

async function clearPin() {
  savingPin.value = true
  try {
    await setPin('')
    hasPin.value = false
    cfg.privacy.lock_unlock = 'token'
    pinInput.value = ''
    toast(t('privacy.pinCleared'), 'ok')
  } catch (e) { toast(e.message, 'err') } finally { savingPin.value = false }
}

/* 立即锁定一次，验证解锁流程（刷新后由服务端返回锁屏页） */
async function testLock() {
  try {
    await privacyLock('manual')
    window.location.reload()
  } catch (e) { toast(e.message, 'err') }
}

/* 受信设备（「记住此设备 30 天」） */
const trusted = ref([])
async function loadTrusted() {
  try {
    const r = await trustedDevices()
    trusted.value = (r && r.items) || []
  } catch (e) { /* 未登录 / 旧版本后端：忽略 */ }
}
async function forgetTrusted() {
  const ok = await confirmDialog(t('privacy.trustedForget'), t('privacy.trustedForgetHint'),
    { danger: true, okText: t('privacy.trustedForget') })
  if (!ok) return
  try {
    const r = await revokeTrusted()
    trusted.value = []
    toast(t('privacy.trustedDone', { n: (r && r.revoked) || 0 }), 'ok')
  } catch (e) { toast(e.message, 'err') }
}

async function doLogout() {
  try {
    await apiLogout()
    toast(t('nav.loggedOut'), 'ok')
    setTimeout(() => window.location.reload(), 300)
  } catch (e) { toast(e.message, 'err') }
}

async function doLogoutAll() {
  const ok = await confirmDialog(t('settings.secLogoutAll'), t('settings.secSessionsHint'), { danger: true, okText: t('settings.secLogoutAll') })
  if (!ok) return
  try {
    await apiLogoutAll()
    setTimeout(() => window.location.reload(), 300)
  } catch (e) { toast(e.message, 'err') }
}

async function kickSession(id) {
  try {
    await revokeSession(id)
    toast(t('settings.secRevoke'), 'ok')
    await loadSecurity()
  } catch (e) { toast(e.message, 'err') }
}

/* 按当前密度档位重新生成全部预览图（改档后刷新已生成影片） */
const regenPv = ref(false)
async function regeneratePreviews() {
  if (!(await confirmDialog(
    t('settings.regenPreviewTitle'),
    t('settings.regenPreviewDesc'),
    { danger: true, okText: t('settings.regenPreviewOk') },
  ))) return
  regenPv.value = true
  try {
    await regenAllPreviews()
    toast(t('settings.regenPreviewStart'), 'ok')
  } catch (e) { toast(e.message, 'err') }
  finally { regenPv.value = false }
}

const newPath = ref('')

/* 容器里"已挂载的目录"：宿主机路径与容器内路径对不上，手输容易错，
   这里直接列出来点选（桌面环境 mounts 为空，界面不出现） */
const mounts = ref([])
const inContainer = ref(false)
async function loadMounts() {
  try {
    const r = await fsMounts()
    inContainer.value = !!r.in_container
    mounts.value = (r.items || []).filter((x) => x.target && x.target !== '/data')
  } catch (e) { /* 非容器 / 无权限：忽略 */ }
}
async function addMount(target) {
  const v = String(target || '').trim()
  if (!v) return
  if ((cfg.library.paths || []).includes(v)) { toast(t('settings.dirExists'), 'err'); return }
  cfg.library.paths.push(v)
  await saveLibrary()
}

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

// 批量：为所有没有封面的影片从视频抽一帧作为封面
const extractingAll = ref(false)
async function doExtractAll() {
  if (!(await confirmDialog(
    t('settings.extractAllTitle'),
    t('settings.extractAllDesc'),
  ))) return
  extractingAll.value = true
  try {
    const r = await extractAllCovers()
    toast(t('settings.extractAllDone', { n: r.extracted || 0, f: r.failed || 0 }), 'ok')
  } catch (e) { toast(e.message, 'err') }
  finally { extractingAll.value = false }
}

// 重新运行首次启动引导：重置 setup_done 并弹出遮罩
async function rerunOnboarding() {
  const ok = await confirmDialog(
    t('settings.rerunOnboardingTitle'),
    t('settings.rerunOnboardingDesc'),
  )
  if (!ok) return
  try {
    await putConfig({ server: { setup_done: false } })
    state.config = state.config || {}
    state.config.server = Object.assign({}, state.config.server, { setup_done: false })
    state.onboarded = false   // 触发 App.vue 遮罩重新出现
    toast(t('settings.rerunOnboardingDone'), 'ok')
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
    await fillActressAvatars()
    toast('已加入任务中心：女优头像抓取中…', 'ok')
  } catch (e) { toast(e.message, 'err') } finally { fillingAvatars.value = false }
}

// 用已启用的资料插件（如 JavBus 女优资料）补全女优文字档案（身高/三围/生日/出生地等）
const fetchingProfile = ref(false)
async function doFetchProfile() {
  const ok = await confirmDialog(
    t('settings.profileFetchConfirmTitle'),
    t('settings.profileFetchConfirmMsg'),
  )
  if (!ok) return
  fetchingProfile.value = true
  try {
    await fetchActressProfile(0)
    toast('已加入任务中心：女优资料抓取中…', 'ok')
  } catch (e) { toast(e.message, 'err') } finally { fetchingProfile.value = false }
}

// 重新嗅探本地已有的封面图片，命中则落盘写回（无需全量重扫）
const rescanningCovers = ref(false)
const posterMsg = ref('')
// 后台任务进度：大库（上千部）联网升级是分钟级，必须后台跑 + 轮询
const posterTask = reactive({
  running: false, total: 0, done: 0, percent: 0, message: '',
  generated: 0, skipped: 0, failed: 0, upgraded: 0,
})
const posterBusy = computed(() => posterTask.running)
let posterTimer = 0

function stopPosterPoll() {
  if (posterTimer) {
    clearInterval(posterTimer)
    posterTimer = 0
  }
}

async function pollPoster() {
  try {
    const s = await posterTaskState()
    Object.assign(posterTask, s)
    if (!s.running) {
      stopPosterPoll()
      posterMsg.value = s.message || ''
      if (s.total) toast(s.message, s.failed ? 'err' : 'ok')
    }
  } catch (e) {
    stopPosterPoll()
    posterTask.running = false
    posterMsg.value = e.message
  }
}

async function doGenPosters(force = false) {
  if (posterTask.running) return
  posterMsg.value = ''
  try {
    Object.assign(posterTask, await posterTaskStart(force, posterUpgrade.value))
    stopPosterPoll()
    posterTimer = setInterval(pollPoster, 800)
  } catch (e) {
    posterMsg.value = e.message
    toast(e.message, 'err')
  }
}

async function cancelGenPosters() {
  try {
    await posterTaskCancel()
  } catch (e) {
    toast(e.message, 'err')
  }
}

onBeforeUnmount(stopPosterPoll)

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

/* ---------------- AI 增强 ---------------- */
// 此前该面板只画了 UI：模板里的 @click="save" 与 aiOk 在脚本中都未定义，
// 点保存没有任何反应（刷新后依旧显示关闭）。这里补齐真正的保存逻辑。
const aiOk = ref(null)

/* 语音对白提取（可选组件）的路径配置与环境探测 */
const asrInfo = ref(null)
async function loadAsr() {
  try {
    asrInfo.value = await aiAsrStatus()
  } catch (e) {
    asrInfo.value = null
  }
}

/** 打开独立的 AI 中心页（模型/提示词/任务/日志） */
function openAiCenter() {
  state.view = 'ai'
}

async function save() {
  saving.value = true
  aiOk.value = null
  try {
    await putConfig({
      // AI（模型池/提示词/任务/术语表）与语音组件已迁到 /ai，
      // 这里不再提交 ai/asr，避免用旧表单值覆盖 AI 中心的修改。
    })
    await loadAsr()
    aiOk.value = true
    toast(t('settings.aiSaved'), 'ok')
    await load()
  } catch (e) {
    aiOk.value = false
    toast(e.message, 'err')
  } finally {
    saving.value = false
  }
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
const serverInfo = reactive({ host: '127.0.0.1', port: 8770, access_token: '', token_masked: true, token_env_locked: false })
const showTok = ref(false)
const tokInput = ref('')            // 只用于「设置新令牌」，留空表示不修改
const savingTok = ref(false)
const requireToken = ref(true)
// 完整令牌默认不可见：点「显示」需再次输入当前令牌，明文 30 秒后自动隐藏
const revealValue = ref('')
const revealLeft = ref(0)
let revealTimer = null
// 再确认对话框：查看 / 更换令牌共用（action 决定行为）
const confirmAct = ref('')          // '' | 'reveal' | 'rotate'
const confirmInput = ref('')
const confirmBusy = ref(false)

function askConfirm(act) {
  confirmAct.value = act
  confirmInput.value = ''
}

function stopReveal() {
  clearInterval(revealTimer)
  revealValue.value = ''
  revealLeft.value = 0
}

function startReveal(token) {
  stopReveal()
  revealValue.value = token || ''
  revealLeft.value = 30
  revealTimer = setInterval(() => {
    revealLeft.value -= 1
    if (revealLeft.value <= 0) stopReveal()
  }, 1000)
}

async function runConfirm() {
  const v = (confirmInput.value || '').trim()
  if (!v) { toast(t('settings.revealNeedToken'), 'err'); return }
  confirmBusy.value = true
  try {
    if (confirmAct.value === 'rotate') {
      const r = await rotateToken(v)
      startReveal(r.access_token)
      await loadServerInfo()
      toast(t('settings.tokenRotated'), 'ok')
    } else {
      const r = await revealToken(v)
      startReveal(r.token)
      toast(t('settings.tokenRevealed'), 'ok')
    }
    confirmAct.value = ''
    confirmInput.value = ''
  } catch (e) { toast(e.message, 'err') }
  finally { confirmBusy.value = false }
}

async function saveToken() {
  const v = (tokInput.value || '').trim()
  if (!v) return
  if (v.length < 8 || /\s/.test(v)) { toast(t('settings.tokenInvalid'), 'err'); return }
  savingTok.value = true
  try {
    await putConfig({ server: { access_token: v } })
    tokInput.value = ''
    await loadServerInfo()
    toast(t('settings.tokenSaved'), 'ok')
  } catch (e) { toast(e.message, 'err') }
  finally { savingTok.value = false }
}
const qrAddr = ref('')
// 二维码里是一次性短链（5 分钟、用一次失效），本机或已登录会话都能生成；
// 后端在 /server/info 里给判定结果，前端据此显示提示而不是一张 403 的碎图。
const qrAvailable = ref(true)
const qrUrl = computed(() => `/api/server/qr?addr=${encodeURIComponent(qrAddr.value || '')}&_=${Date.now()}`)
const isLocal = (u) => /(^|[/:])(127\.0\.0\.1|localhost|0\.0\.0\.0)(:|\/|$)/i.test(u)
const lanUrls = computed(() => (serverInfo.access_urls || []).filter((u) => !isLocal(u)))
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast(t('settings.copied'), 'ok') }
  catch (e) { toast(t('settings.copyFailed'), 'err') }
}
async function saveRequireToken() {
  try { await putConfig({ server: { token_required: requireToken.value } }); await loadServerInfo(); toast(t('settings.saved'), 'ok') }
  catch (e) { toast(e.message, 'err') }
}

async function loadServerInfo() {
  loadMounts()   // 容器里的「已挂载目录」，随环境信息一起取
  try {
    const r = await getServerInfo()
    Object.assign(serverInfo, r)
    requireToken.value = r.token_required !== false
    qrAvailable.value = r.qr_available !== false
    const urls = (r.access_urls || []).filter((u) => !/(^|[/:])(127\.0\.0\.1|localhost|0\.0\.0\.0)(:|\/|$)/i.test(u))
    if (urls.length) qrAddr.value = urls[0]
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

/* 品牌图标：public 目录资源（Vite 不处理，直接以根路径相对部署） */
const logoUrl = `${import.meta.env.BASE_URL}logo.svg`

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

/* 女优资料插件管理 */
const pluginList = ref([])
async function loadPlugins() {
  try {
    const r = await listPlugins()
    pluginList.value = (r && r.plugins) || []
  } catch (e) { /* 忽略 */ }
}
async function togglePluginOn(p) {
  try {
    const r = await togglePlugin(p.id)
    p.enabled = !!r.enabled
    toast(`已${p.enabled ? '启用' : '停用'}「${p.name}」`, 'ok')
  } catch (e) { toast(e.message, 'err') }
}

/* 保存女优资料插件设置（语言偏好） */
const savingWiki = ref(false)
async function saveWiki() {
  savingWiki.value = true
  try {
    await putConfig({ wiki: { ...cfg.wiki } })
    toast(t('settings.wikiSaved'), 'ok')
  } catch (e) { toast(e.message, 'err') }
  finally { savingWiki.value = false }
}

/* ---------------- 常用在线站点 ---------------- */
const savingSites = ref(false)
const editingSite = ref(null) // 正在编辑的站点（null=关闭弹窗）
const draftSite = reactive({ id: '', name: '', url: '', enabled: true })

function openAddSite() {
  Object.assign(draftSite, { id: '', name: '', url: '', enabled: true })
  editingSite.value = 'new'
}
function openEditSite(s) {
  Object.assign(draftSite, { id: s.id, name: s.name, url: s.url, enabled: !!s.enabled })
  editingSite.value = s.id
}
function closeSiteEditor() { editingSite.value = null }
function saveSiteEditor() {
  const name = draftSite.name.trim()
  const url = draftSite.url.trim()
  if (!url || !url.includes('{code}')) {
    toast(t('settings.sitesInvalid'), 'err')
    return
  }
  if (editingSite.value === 'new') {
    cfg.online_sites.push({ id: 's_' + Date.now(), name: name || url, url, enabled: draftSite.enabled })
  } else {
    const i = cfg.online_sites.findIndex((s) => s.id === editingSite.value)
    if (i >= 0) Object.assign(cfg.online_sites[i], { name: name || url, url, enabled: draftSite.enabled })
  }
  closeSiteEditor()
}
async function deleteSite(s) {
  if (!(await confirmDialog(t('settings.sitesDeleteTitle'), t('settings.sitesDeleteDesc', { name: s.name }), { danger: true }))) return
  cfg.online_sites = cfg.online_sites.filter((x) => x.id !== s.id)
}
function moveSite(i, dir) {
  const j = i + dir
  if (j < 0 || j >= cfg.online_sites.length) return
  const arr = cfg.online_sites
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
}
async function saveSites() {
  savingSites.value = true
  try {
    await putConfig({ online_sites: cfg.online_sites.map((s) => ({ ...s })) })
    toast(t('settings.sitesSaved'), 'ok')
  } catch (e) { toast(e.message, 'err') }
  finally { savingSites.value = false }
}
// 把预设里尚未添加的站点一键补入（已存在的按 id 去重）
function addCommonSites() {
  const have = new Set(cfg.online_sites.map((s) => s.id))
  let added = 0
  for (const p of PRESET_SITES) {
    if (!have.has(p.id)) { cfg.online_sites.push({ ...p }); added++ }
  }
  if (added) toast(t('settings.sitesAddCommon') + ` (+${added})`, 'ok')
  else toast(t('settings.sitesAddCommon'), 'ok')
}
// 用番号渲染模板，用于预览（不实际跳转）
function renderUrl(site, code) {
  const codeVal = site.lower ? String(code || 'ABC-123').toLowerCase() : (code || 'ABC-123')
  return site.url.replace(/\{code\}/g, codeVal)
}
const sitesPreviewCode = ref('ABC-123')

onMounted(async () => {
  await load()
  await loadServerInfo()
  loadPlugins()
  loadTrusted()
})
</script>

<template>
  <section class="view">
    <div class="toolbar">
      <h1 class="tb-title">{{ $t('view.settings') }}</h1>
      <span v-if="loading || saving" class="spinner"></span>
      <div class="spacer"></div>
      <button class="btn tiny ghost" @click="openAiCenter">{{ $t('aiCenter.open') }}</button>
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

            <div v-if="inContainer && mounts.length" class="mount-chips">
              <span class="muted sm">{{ $t('settings.detectedMounts') }}</span>
              <button
                v-for="m in mounts"
                :key="m.target"
                :class="['btn', 'tiny', (cfg.library.paths || []).includes(m.target) ? 'primary' : 'ghost']"
                @click="addMount(m.target)"
              >
                📁 {{ m.target }}
                <span v-if="m.root && m.root !== '/' && prettyHostPath(m.root) !== m.target" class="muted">
                  ← {{ prettyHostPath(m.root) }}
                </span>
              </button>
            </div>

            <div class="hstack">
              <input v-model="newPath" :placeholder="$t('settings.scanDirPh')" @keydown.enter="addPath" />
              <button v-if="!inContainer" class="btn" @click="browse('')">{{ $t('settings.browseDir') }}</button>
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
              <label>{{ $t('settings.previewQuality') }}</label>
              <select v-model="cfg.previews_quality" class="sel" @change="saveLibrary">
                <option value="low">{{ $t('settings.pvLow') }}</option>
                <option value="smart">{{ $t('settings.pvSmart') }}</option>
                <option value="high">{{ $t('settings.pvHigh') }}</option>
              </select>
              <span class="hint">{{ $t('settings.previewQualityHint') }}</span>
            </div>
            <div class="field">
              <button class="btn" :disabled="regenPv" @click="regeneratePreviews">{{ $t('settings.regenPreviewBtn') }}</button>
              <span class="hint">{{ $t('settings.regenPreviewHint') }}</span>
            </div>
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="autoScan" @change="saveLibrary" /><span class="track"></span>
              </label>
              <span>{{ $t('settings.autoScan') }}</span>
              <span class="hint">{{ $t('settings.autoScanHint') }}</span>
            </div>
            <div class="field-row" v-if="autoScan">
              <label>{{ $t('settings.autoScanInterval') }}</label>
              <div class="hstack">
                <input type="number" v-model="autoScanInterval" min="1" style="width:120px" @change="saveLibrary" />
                <span class="muted">{{ $t('settings.minutes') }}</span>
              </div>
            </div>
            <div class="field-row">
              <label>{{ $t('settings.missingGrace') }}</label>
              <div class="hstack">
                <input type="number" v-model="cfg.library.missing_grace_days" min="0" style="width:120px" @change="saveLibrary" />
                <span class="muted">{{ $t('settings.missingGraceSub') }}</span>
              </div>
              <span class="hint">{{ $t('settings.missingGraceHint') }}</span>
            </div>
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="cfg.library.delete_to_recycle_bin" @change="saveLibrary" /><span class="track"></span>
              </label>
              <span>{{ $t('settings.delToTrashDefault') }}</span>
              <span class="hint">{{ $t('settings.delToTrashHint') }}</span>
            </div>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="saving" @click="saveLibrary">{{ $t('common.save') }}</button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">{{ $t('settings.playTitle') }}</div>
          <div class="panel-body">
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="cfg.stream.auto_remux" @change="saveLibrary" /><span class="track"></span>
              </label>
              <span>{{ $t('settings.playAutoRemux') }}</span>
              <span class="hint">{{ $t('settings.playAutoRemuxHint') }}</span>
            </div>
            <div class="field-row">
              <label>{{ $t('settings.playCache') }}</label>
              <div class="hstack">
                <span class="muted">{{ cacheMb }} MB</span>
                <button class="btn tiny" :disabled="clearingCache" @click="clearCacheNow">{{ $t('settings.playClear') }}</button>
              </div>
              <span class="hint">{{ $t('settings.playCacheHint') }}</span>
            </div>
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
            <button class="btn" :disabled="extractingAll" @click="doExtractAll">{{ $t('settings.extractAll') }}</button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">{{ $t('settings.onboarding') }}</div>
          <div class="panel-body">
            <p class="muted">{{ $t('settings.onboardingHint') }}</p>
          </div>
          <div class="panel-foot">
            <button class="btn" @click="rerunOnboarding">{{ $t('settings.rerunOnboarding') }}</button>
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
            <button class="btn" :disabled="fetchingProfile" @click="doFetchProfile">
              {{ fetchingProfile ? $t('settings.fetching') : $t('settings.fetchProfileBtn') }}
            </button>
            <button class="btn" :disabled="rescanningCovers" @click="doRescanLocalCovers">
              {{ rescanningCovers ? $t('settings.sniffing') : $t('settings.rescanCoverBtn') }}
            </button>
          </div>
        </div>

        <!-- 竖版海报：横版封面自动裁出 2:3 竖图，海报墙 / 媒体服务器都用它 -->
        <div class="panel">
          <div class="panel-head">{{ $t('settings.posterTitle') }} <span class="sub">{{ $t('settings.posterSub') }}</span></div>
          <div class="panel-body">
            <div class="field-row">
              <label>{{ $t('settings.posterStrategy') }}</label>
              <select v-model="cfg.media.poster_strategy" class="aic-sel-sm">
                <option value="right">{{ $t('settings.posterRight') }}</option>
                <option value="center">{{ $t('settings.posterCenter') }}</option>
                <option value="fit">{{ $t('settings.posterFit') }}</option>
              </select>
            </div>
            <span class="hint">{{ $t('settings.posterHint') }}</span>
            <div class="field-row checkbox">
              <label>{{ $t('settings.posterUpgrade') }}</label>
              <label class="toggle">
                <input type="checkbox" v-model="cfg.media.poster_dmm" /><span class="track"></span>
              </label>
            </div>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="saving" @click="saveLibrary">{{ $t('common.save') }}</button>
            <button class="btn" :disabled="posterBusy" @click="doGenPosters()">
              {{ posterBusy ? $t('settings.posterWorking') : $t('settings.posterGen') }}
            </button>
            <button class="btn ghost" :disabled="posterBusy" @click="doGenPosters(true)">
              {{ $t('settings.posterForce') }}
            </button>
            <button v-if="posterBusy" class="btn ghost" @click="cancelGenPosters">
              {{ $t('common.cancel') }}
            </button>
            <span v-if="posterMsg && !posterBusy" class="hint">{{ posterMsg }}</span>
          </div>
          <div v-if="posterTask.total || posterBusy" class="poster-progress">
            <div class="poster-bar">
              <div class="poster-bar-in" :style="{ width: posterTask.percent + '%' }"></div>
            </div>
            <span class="hint">
              {{ posterTask.percent }}% · {{ posterTask.done }}/{{ posterTask.total }}
              <template v-if="posterTask.message"> · {{ posterTask.message }}</template>
            </span>
          </div>
        </div>

<div class="panel">
          <div class="panel-head">{{ $t('settings.embyTitle') }} <span class="sub">{{ $t('settings.embySub') }}</span></div>
          <div class="panel-body">
            <div class="field-row checkbox">
              <label>{{ $t('settings.embyEnable') }}</label>
              <label class="toggle">
                <input type="checkbox" v-model="cfg.emby.enabled" /><span class="track"></span>
              </label>
            </div>
            <div class="field">
              <label>{{ $t('settings.embyUrl') }}</label>
              <input v-model="cfg.emby.base_url" :placeholder="$t('settings.embyUrlPh')" />
            </div>
            <div class="field">
              <label>{{ $t('settings.embyKey') }}</label>
              <input
                v-model="cfg.emby.api_key" type="password" autocomplete="new-password"
                :placeholder="embyHasKey ? $t('settings.embyKeySet') : $t('settings.embyKeyPh')"
              />
            </div>
            <div class="field">
              <label>{{ $t('settings.refreshDebounce') }}</label>
              <input type="number" v-model.number="cfg.emby.refresh_debounce_sec" min="1" style="width:120px" />
            </div>
            <p class="muted">{{ $t('settings.embyHint') }}</p>
            <p v-if="embyMsg" class="hint">{{ embyMsg }}</p>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="savingEmby" @click="saveEmby">{{ $t('common.save') }}</button>
            <button class="btn" :disabled="testingEmby" @click="doTestEmby">
              {{ testingEmby ? $t('settings.testing') : $t('settings.testBtn') }}
            </button>
            <button class="btn" :disabled="refreshingEmby" @click="doEmbyRefresh">
              {{ refreshingEmby ? $t('settings.refreshing') : $t('settings.refreshBtn') }}
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

        <!-- MetaTube 刮削引擎：补齐自研源没有的 FC2 / 无码 / 素人 / 官方源 -->
        <MetatubePanel />

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

        <!-- ============ 女优资料插件（数据源） ============ -->
        <div class="panel">
          <div class="panel-head">
            {{ $t('settings.wikiTitle') }}
            <span class="sub">{{ $t('settings.wikiSub') }}</span>
          </div>
          <div class="panel-body">
            <div class="field-row">
              <label>{{ $t('settings.wikiLang') }}</label>
              <div class="hstack">
                <input v-model="cfg.wiki.languages" placeholder="ja,zh" style="width:140px" />
                <span class="muted sm">{{ $t('settings.wikiLangHint') }}</span>
              </div>
            </div>
            <div class="prov" v-for="p in pluginList" :key="p.id">
              <label class="toggle">
                <input type="checkbox" :checked="p.enabled" @change="togglePluginOn(p)" />
                <span class="track"></span>
              </label>
              <div class="prov-main">
                <div class="prov-name">
                  {{ p.name }}
                  <code>{{ p.id }}</code>
                  <span v-if="p.enabled" class="badge ok">{{ $t('settings.active') }}</span>
                </div>
                <div class="prov-desc">{{ p.description }}</div>
              </div>
            </div>
            <p v-if="!pluginList.length" class="muted">{{ $t('settings.noPlugin') }}</p>
          </div>
          <div class="panel-foot">
            <button class="btn primary" :disabled="savingWiki" @click="saveWiki">{{ $t('settings.wikiSave') }}</button>
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

      <!-- ============ 常用在线站点 ============ -->
      <template v-else-if="tab === 'sites'">
        <div class="panel">
          <div class="panel-head">{{ $t('settings.sites') }}</div>
          <div class="panel-sub">{{ $t('settings.sitesSub') }}</div>
          <div class="panel-body">
            <div class="sites-toolbar">
              <button class="btn btn-primary" @click="openAddSite">
                <span class="i">＋</span> {{ $t('settings.sitesAdd') }}
              </button>
              <button class="btn" @click="addCommonSites">
                <span class="i">⚡</span> {{ $t('settings.sitesAddCommon') }}
              </button>
            </div>

            <div v-if="!cfg.online_sites.length" class="empty-hint">{{ $t('settings.sitesEmpty') }}</div>

            <ul v-else class="sites-list">
              <li v-for="(s, i) in cfg.online_sites" :key="s.id" class="sites-item">
                <label class="switch sm">
                  <input type="checkbox" v-model="s.enabled" />
                  <span class="track"></span>
                </label>
                <div class="sites-info">
                  <div class="sites-name">{{ s.name }}</div>
                  <div class="sites-url">{{ s.url }}</div>
                </div>
                <div class="sites-actions">
                  <button class="btn btn-ghost sm" :title="$t('settings.sitesUp')" @click="moveSite(i, -1)">↑</button>
                  <button class="btn btn-ghost sm" :title="$t('settings.sitesDown')" @click="moveSite(i, 1)">↓</button>
                  <button class="btn btn-ghost sm" @click="openEditSite(s)">{{ $t('settings.sitesEdit') }}</button>
                  <button class="btn btn-ghost sm danger" @click="deleteSite(s)">{{ $t('settings.sitesDelete') }}</button>
                </div>
              </li>
            </ul>

            <div class="form-actions">
              <button class="btn btn-primary" :disabled="savingSites" @click="saveSites">
                {{ savingSites ? '…' : $t('settings.sitesSave') }}
              </button>
            </div>
          </div>
        </div>

        <!-- 新增/编辑弹窗 -->
        <div v-if="editingSite" class="modal-mask" @click.self="closeSiteEditor">
          <div class="modal">
            <div class="modal-head">{{ editingSite === 'new' ? $t('settings.sitesAddTitle') : $t('settings.sitesEditTitle') }}</div>
            <div class="modal-body">
              <div class="form-row">
                <label>{{ $t('settings.sitesName') }}</label>
                <input v-model="draftSite.name" :placeholder="$t('settings.sitesUrlPh')" />
              </div>
              <div class="form-row">
                <label>{{ $t('settings.sitesUrl') }}</label>
                <input v-model="draftSite.url" :placeholder="$t('settings.sitesUrlPh')" />
              </div>
              <div class="form-hint">{{ $t('settings.sitesUrlHint') }}</div>
              <div class="form-row inline">
                <label class="switch sm">
                  <input type="checkbox" v-model="draftSite.enabled" />
                  <span class="track"></span>
                </label>
                <span>{{ $t('settings.sitesEnabled') }}</span>
              </div>
              <div class="preview-box" v-if="draftSite.url.includes('{code}')">
                <div class="preview-title">{{ $t('settings.sitesPreview') }}</div>
                <div class="form-row inline">
                  <label>{{ $t('settings.sitesPreviewDesc', { code: sitesPreviewCode }) }}</label>
                </div>
                <a class="preview-link" :href="renderUrl(draftSite, sitesPreviewCode)" target="_blank" rel="noopener">{{ renderUrl(draftSite, sitesPreviewCode) }}</a>
              </div>
            </div>
            <div class="modal-foot">
              <button class="btn btn-ghost" @click="closeSiteEditor">{{ $t('common.cancel') }}</button>
              <button class="btn btn-primary" @click="saveSiteEditor">{{ $t('common.save') }}</button>
            </div>
          </div>
        </div>
      </template>

      <!-- ============ 扩展组件（FFmpeg / 语音识别依赖 / 模型） ============ -->
      <ExtPanel v-else-if="tab === 'ext'" />

      <!-- ============ 隐私 ============ -->
      <template v-else-if="tab === 'privacy'">
        <!-- 隐私保护：桌面防窥（见 docs/安全与隐私设计.md 的 L4） -->
        <div class="panel">
          <div class="panel-head">{{ $t('privacy.title') }}</div>
          <div class="panel-body">
            <div class="field-row">
              <label>{{ $t('privacy.idleMinutes') }}</label>
              <input class="inp" type="number" min="0" style="max-width:110px" v-model.number="cfg.privacy.idle_lock_minutes" @input="scheduleSavePrivacy()" />
            </div>
            <span class="hint">{{ $t('privacy.idleHint') }}</span>

            <div class="field col">
              <label>{{ $t('privacy.unlockTitle') }}</label>
              <div class="hstack">
                <input class="inp mono" v-model="pinInput" :type="showPin ? 'text' : 'password'" name="avm-code" autocomplete="new-password" autocapitalize="off" spellcheck="false" data-lpignore="true" data-1p-ignore data-bwignore :placeholder="$t('privacy.pinSetPh')" style="max-width:280px" />
                <button class="btn tiny ghost" @click="showPin = !showPin">{{ showPin ? '🙈' : '👁' }}</button>
                <button class="btn tiny" :disabled="savingPin" @click="savePin">{{ $t('privacy.pinSet') }}</button>
                <button class="btn tiny ghost" :disabled="savingPin || !hasPin" @click="clearPin">{{ $t('privacy.pinClear') }}</button>
                <span class="muted sm">{{ hasPin ? $t('privacy.pinHas') : $t('privacy.pinNone') }}</span>
              </div>
              <span class="hint">{{ $t('privacy.unlockHint') }}</span>
            </div>

            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="cfg.privacy.neutral_title" @change="scheduleSavePrivacy()" /><span class="track"></span>
              </label>
              <span>{{ $t('privacy.neutralTitle') }}</span>
              <span class="hint">{{ $t('privacy.neutralTitleHint') }}</span>
            </div>
            <div class="field col">
              <label>{{ $t('privacy.siteSubtitle') }}</label>
              <input class="inp" v-model="cfg.privacy.site_subtitle" :placeholder="$t('privacy.siteSubtitlePh')" style="max-width:320px" @input="scheduleSavePrivacy()" />
              <span class="hint">{{ $t('privacy.siteSubtitleHint') }}</span>
            </div>
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="cfg.privacy.pause_when_hidden" @change="scheduleSavePrivacy()" /><span class="track"></span>
              </label>
              <span>{{ $t('privacy.pauseHidden') }}</span>
              <span class="hint">{{ $t('privacy.pauseHiddenHint') }}</span>
            </div>
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="cfg.privacy.lock_on_start" @change="scheduleSavePrivacy()" /><span class="track"></span>
              </label>
              <span>{{ $t('privacy.lockOnStart') }}</span>
              <span class="hint">{{ $t('privacy.lockOnStartHint') }}</span>
            </div>
            <div class="field col">
              <label>{{ $t('privacy.panicKey') }}</label>
              <input class="inp mono" v-model="cfg.privacy.panic_key" placeholder="Ctrl+Shift+H" style="max-width:260px" @input="scheduleSavePrivacy()" />
              <span class="hint">{{ $t('privacy.panicKeyHint') }}</span>
            </div>
            <div class="field col">
              <label>{{ $t('privacy.testTitle') }}</label>
              <div class="hstack" style="align-items:center;gap:10px;flex-wrap:wrap">
                <button class="btn tiny" @click="testLock">{{ $t('privacy.testBtn') }}</button>
                <span class="hint">{{ $t('privacy.testHint') }}</span>
              </div>
            </div>
            <span class="hint">{{ savingPrivacy ? $t('settings.saving') : $t('privacy.autoSaved') }}</span>
          </div>
        </div>

        <!-- 受信设备：勾选「记住此设备 30 天」的机器，可一键全部忘记 -->
        <div class="panel">
          <div class="panel-head">{{ $t('privacy.trustedTitle') }}</div>
          <div class="panel-body">
            <p class="hint">{{ $t('privacy.trustedHint') }}</p>
            <div class="hstack" style="align-items:center;gap:10px;flex-wrap:wrap">
              <span>{{ $t('privacy.trustedCount', { n: trusted.length }) }}</span>
              <button class="btn tiny" :disabled="!trusted.length" @click="forgetTrusted">
                {{ $t('privacy.trustedForget') }}
              </button>
            </div>
            <div v-if="trusted.length" class="field col" style="margin-top:10px">
              <div v-for="(d, i) in trusted" :key="i" class="sec-row">
                <span class="mono">{{ d.ip || '-' }}</span>
                <span class="muted sm sec-grow ellipsis">{{ d.ua }}</span>
                <span class="muted sm tabular" :title="fmtDateTime(d.last)">{{ fmtAgo(d.last) }}</span>
              </div>
            </div>
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
              <label>{{ $t('settings.detailLayout') }}</label>
              <div class="btn-group">
                <button class="btn tiny" :class="{ active: state.detailLayout === 'hero' }"
                        @click="state.detailLayout = 'hero'">{{ $t('settings.detailLayoutHero') }}</button>
                <button class="btn tiny" :class="{ active: state.detailLayout === 'split' }"
                        @click="state.detailLayout = 'split'">{{ $t('settings.detailLayoutSplit') }}</button>
                <button class="btn tiny" :class="{ active: state.detailLayout === 'compact' }"
                        @click="state.detailLayout = 'compact'">{{ $t('settings.detailLayoutCompact') }}</button>
              </div>
            </div>
            <span class="hint">{{ $t('settings.detailLayoutHint') }}</span>
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
            <div class="field-row">
              <label>{{ $t('settings.playerMode') }}</label>
              <div class="btn-group">
                <button class="btn tiny" :class="{ active: state.playerMode === 'auto' }" @click="state.playerMode = 'auto'" :title="$t('settings.playerModeAutoHint')">{{ $t('settings.playerModeAuto') }}</button>
                <button class="btn tiny" :class="{ active: state.playerMode === 'web' }" @click="state.playerMode = 'web'">{{ $t('settings.playerModeWeb') }}</button>
                <button class="btn tiny" :class="{ active: state.playerMode === 'external' }" @click="state.playerMode = 'external'">{{ $t('settings.playerModeExternal') }}</button>
              </div>
            </div>
            <span class="hint">{{ $t('settings.playerModeHint') }}</span>
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="state.recentOpenPlaylist" /><span class="track"></span>
              </label>
              <span>{{ $t('settings.recentOpenPlaylist') }}</span>
              <span class="hint">{{ $t('settings.recentOpenPlaylistHint') }}</span>
            </div>
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="state.recentAutoPlay" :disabled="!state.recentOpenPlaylist" /><span class="track"></span>
              </label>
              <span>{{ $t('settings.recentAutoPlay') }}</span>
              <span class="hint">{{ $t('settings.recentAutoPlayHint') }}</span>
            </div>
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="state.aiFab" /><span class="track"></span>
              </label>
              <span>{{ $t('settings.aiFab') }}</span>
              <span class="hint">{{ $t('settings.aiFabHint') }}</span>
            </div>
          </div>
        </div>
      </template>

      <!-- ============ 远程（访问与远程） ============ -->
      <template v-else-if="tab === 'remote'">
        <!-- ===== ① 访问保护 / ② 访问令牌 / ③ 扫码（用户只需理解这三件事） ===== -->
        <div class="panel">
          <div class="panel-head">{{ $t('settings.access') }}</div>
          <div class="panel-body">
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="requireToken" @change="saveRequireToken" /><span class="track"></span>
              </label>
              <span>{{ $t('settings.requireToken') }}</span>
              <span class="hint">{{ $t('settings.requireTokenHint') }}</span>
            </div>
            <div class="fw-tip" v-if="!requireToken">
              <div class="fw-tip-title">⚠️ {{ $t('settings.protectOffTitle') }}</div>
              <div class="fw-tip-body">{{ $t('settings.protectOffBody') }}</div>
            </div>

            <div class="field col">
              <label>{{ $t('settings.accessToken') }}</label>
              <div class="tok-row">
                <input class="inp mono" readonly :value="revealValue || serverInfo.access_token" />
                <span v-if="revealValue" class="muted sm tabular">{{ revealLeft }}s</span>
                <button class="btn tiny ghost" :disabled="!!confirmAct" @click="askConfirm('reveal')">{{ $t('settings.tokenShow') }}</button>
                <button class="btn tiny ghost" :disabled="!revealValue" @click="copyText(revealValue)">{{ $t('settings.copy') }}</button>
              </div>
              <span class="muted">{{ $t('settings.tokenHint') }}</span>
              <span class="hint">{{ $t('settings.tokenRevealHint') }}</span>
            </div>

            <div class="fw-tip" v-if="confirmAct">
              <div class="fw-tip-title">
                {{ confirmAct === 'rotate' ? $t('settings.tokenRotateConfirm') : $t('settings.tokenRevealConfirm') }}
              </div>
              <div class="hstack">
                <input class="inp mono" v-model="confirmInput" type="password" :placeholder="$t('settings.tokenPh')" style="max-width:240px" @keyup.enter="runConfirm" />
                <button class="btn tiny primary" :disabled="confirmBusy" @click="runConfirm">{{ $t('common.confirm') }}</button>
                <button class="btn tiny ghost" @click="confirmAct = ''">{{ $t('common.cancel') }}</button>
              </div>
            </div>

            <div class="field col" v-if="!confirmAct && !serverInfo.token_env_locked">
              <label>{{ $t('settings.tokenSetNew') }}</label>
              <div class="hstack">
                <input class="inp mono" v-model="tokInput" :type="showTok ? 'text' : 'password'" name="avm-code" autocomplete="new-password" autocapitalize="off" spellcheck="false" data-lpignore="true" data-1p-ignore data-bwignore :placeholder="$t('settings.tokenPh')" style="max-width:260px" />
                <button class="btn tiny ghost" @click="showTok = !showTok">{{ showTok ? '🙈' : '👁' }}</button>
                <button class="btn tiny" :disabled="savingTok || !tokInput.trim()" @click="saveToken">{{ savingTok ? $t('settings.saving') : $t('common.save') }}</button>
                <button class="btn tiny danger" :disabled="savingTok" @click="askConfirm('rotate')">{{ $t('settings.tokenRotate') }}</button>
              </div>
              <span class="hint">{{ $t('settings.tokenRotateHint') }}</span>
            </div>
            <div class="fw-tip" v-else-if="!confirmAct && serverInfo.token_env_locked">
              <div class="fw-tip-title">🔒 {{ $t('settings.tokenEnvLocked') }}</div>
              <div class="fw-tip-body">{{ $t('settings.tokenEnvLockedHint') }}</div>
            </div>

            <div class="field">
              <label>{{ $t('settings.listenAddr') }}</label>
              <span class="mono">{{ serverInfo.host }}:{{ serverInfo.port }}</span>
            </div>

            <div class="field col lan-share" v-if="lanUrls.length">
              <label>{{ $t('settings.lanAccess') }}</label>
              <div class="lan-share-body">
                <div class="qr-wrap" v-if="qrAvailable">
                  <img class="qr-img" :src="qrUrl" alt="QR" />
                  <div class="muted sm center">{{ $t('settings.scanHint') }}</div>
                </div>
                <div class="qr-off" v-else>
                  <div class="qr-off-title">🔒 {{ $t('settings.qrUnavailable') }}</div>
                  <div class="muted sm">{{ $t('settings.qrNeedLogin') }}</div>
                </div>
                <div class="lan-urls">
                  <div
                    v-for="u in lanUrls"
                    :key="u"
                    class="url-row"
                    :class="{ active: qrAvailable && qrAddr === u }"
                    @click="qrAddr = u"
                  >
                    <code class="mono url">{{ u }}</code>
                    <button class="btn tiny ghost" @click.stop="copyText(u)">{{ $t('settings.copy') }}</button>
                  </div>
                  <span class="muted sm" v-if="qrAvailable">{{ $t('settings.tapToQr') }}</span>
                </div>
              </div>
            </div>
            <div class="fw-tip" v-if="lanUrls.length">
              <div class="fw-tip-title">⚠️ {{ $t('settings.fwTitle') }}</div>
              <div class="fw-tip-body">{{ $t('settings.fwBody') }}</div>
              <code class="fw-cmd mono">netsh advfirewall firewall add rule name="AVM" dir=in action=allow protocol=TCP localport={{ serverInfo.port }} profile=any edge=yes</code>
            </div>
          </div>
        </div>

        <!-- ===== ④ 已登录设备 ===== -->
        <div class="panel">
          <div class="panel-head">{{ $t('settings.secSessions') }}</div>
          <div class="panel-body">
            <div class="field col" v-if="sessions.length">
              <div v-for="s in sessions" :key="s.id" class="sec-row">
                <span class="mono">{{ s.ip || '-' }}</span>
                <span class="muted sm sec-grow">{{ s.ua }}</span>
                <span class="muted sm tabular" :title="fmtDateTime(s.last)">{{ fmtAgo(s.last) }}</span>
                <span v-if="s.current" class="muted sm">· {{ $t('settings.secCurrent') }}</span>
                <button v-else class="btn tiny ghost" @click="kickSession(s.id)">{{ $t('settings.secRevoke') }}</button>
              </div>
              <span class="hint">{{ $t('settings.secSessionsHint') }}</span>
            </div>

            <div class="field col">
              <label>{{ $t('settings.secAudit') }}</label>
              <div v-if="audit.length">
                <div v-for="(a, i) in audit" :key="i" class="sec-row">
                  <span class="mono">{{ a.ip || '-' }}</span>
                  <span class="muted sm">{{ a.event }}</span>
                  <span class="muted sm sec-grow">{{ a.detail }}</span>
                  <span class="muted sm tabular" :title="fmtDateTime(a.ts)">{{ fmtAgo(a.ts) }}</span>
                </div>
              </div>
              <span v-else class="muted">{{ $t('settings.secAuditEmpty') }}</span>
            </div>

            <div class="hstack">
              <button class="btn tiny ghost" @click="doLogout">{{ $t('settings.secLogout') }}</button>
              <button class="btn tiny ghost" @click="doLogoutAll">{{ $t('settings.secLogoutAll') }}</button>
            </div>
          </div>
        </div>

        <!-- ===== 高级（默认折叠：会话策略 / 两步验证 / API Key） ===== -->
        <details class="adv">
          <summary>{{ $t('settings.advanced') }}</summary>

          <div class="panel">
            <div class="panel-head">{{ $t('settings.secTitle') }}</div>
          <div class="panel-body">
            <div class="field-row">
              <label>{{ $t('settings.secIdleDays') }}</label>
              <input class="inp" type="number" min="0" style="max-width:110px" v-model.number="cfg.server.session_idle_days" />
              <label>{{ $t('settings.secAbsDays') }}</label>
              <input class="inp" type="number" min="0" style="max-width:110px" v-model.number="cfg.server.session_abs_days" />
            </div>
            <span class="hint">{{ $t('settings.secExpHint') }}</span>
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="cfg.server.session_persist" /><span class="track"></span>
              </label>
              <span>{{ $t('settings.secPersist') }}</span>
              <span class="hint">{{ $t('settings.secPersistHint') }}</span>
            </div>
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="cfg.server.expose_docs" /><span class="track"></span>
              </label>
              <span>{{ $t('settings.secExposeDocs') }}</span>
              <span class="hint">{{ $t('settings.secExposeDocsHint') }}</span>
            </div>
            <div class="field">
              <label class="toggle">
                <input type="checkbox" v-model="cfg.server.insecure_skip_verify" @change="onToggleInsecureTls" /><span class="track"></span>
              </label>
              <span>{{ $t('settings.secInsecureTls') }}</span>
              <span class="hint">{{ $t('settings.secInsecureTlsHint') }}</span>
            </div>
            <div class="field col">
              <label>{{ $t('settings.secTrustedProxies') }}</label>
              <input class="inp mono" v-model="trustedProxiesText" placeholder="127.0.0.1, ::1" />
              <span class="hint">{{ $t('settings.secTrustedProxiesHint') }}</span>
            </div>
            <div class="hstack">
              <button class="btn tiny primary" :disabled="savingSec" @click="saveSecurity">{{ $t('common.save') }}</button>
            </div>
          </div>
          </div>

        <!-- 两步验证（TOTP） -->
        <div class="panel">
          <div class="panel-head">{{ $t('settings.totpTitle') }}</div>
          <div class="panel-body">
            <div class="field">
              <span>{{ totp.enabled ? $t('settings.totpOn') : $t('settings.totpOff') }}</span>
              <span class="hint">{{ $t('settings.totpHint') }}</span>
            </div>

            <template v-if="!totp.enabled">
              <div v-if="!totpSetupInfo" class="hstack">
                <button class="btn tiny primary" :disabled="busyTotp" @click="startTotpBind">{{ $t('settings.totpBind') }}</button>
              </div>
              <div v-else class="totp-bind">
                <img class="qr-img" :src="totpQrUrl" alt="TOTP" />
                <div class="totp-bind-side">
                  <span class="hint">{{ $t('settings.totpScanHint') }}</span>
                  <div class="field col">
                    <label>{{ $t('settings.totpSecret') }}</label>
                    <div class="hstack">
                      <input class="inp mono" :value="totpSetupInfo.secret" readonly @focus="$event.target.select()" />
                      <button class="btn tiny ghost" @click="copyText(totpSetupInfo.secret)">{{ $t('settings.copy') }}</button>
                    </div>
                    <span class="hint">{{ $t('settings.totpSecretGroup', { s: groupSecret(totpSetupInfo.secret) }) }}</span>
                  </div>
                  <div class="field col">
                    <label>{{ $t('settings.totpCode') }}</label>
                    <input class="inp mono" v-model="totpCodeInput" inputmode="numeric" maxlength="12" :placeholder="$t('settings.totpCodePh')" @keyup.enter="confirmTotpBind" />
                    <span class="hint mono">{{ $t('settings.totpServerTime', { t: serverClockText }) }}</span>
                  </div>
                  <div class="fw-tip">
                    <div class="fw-tip-title">📱 {{ $t('settings.totpMsTitle') }}</div>
                    <div class="fw-tip-body">{{ $t('settings.totpMsHint') }}</div>
                  </div>
                  <div class="hstack">
                    <button class="btn tiny primary" :disabled="busyTotp" @click="confirmTotpBind">{{ $t('settings.totpConfirm') }}</button>
                    <button class="btn tiny ghost" @click="cancelTotpBind">{{ $t('common.cancel') }}</button>
                  </div>
                </div>
              </div>
            </template>

            <template v-else>
              <div class="field col" v-if="recoveryCodes.length">
                <label>{{ $t('settings.totpRecovery') }}</label>
                <code class="mono rec-codes">{{ recoveryCodes.join('   ') }}</code>
                <span class="hint">{{ $t('settings.totpRecoveryHint') }}</span>
                <div class="hstack">
                  <button class="btn tiny ghost" @click="copyText(recoveryCodes.join('\n'))">{{ $t('settings.copy') }}</button>
                </div>
              </div>
              <div class="field col">
                <label>{{ $t('settings.totpCode') }}</label>
                <input class="inp mono" v-model="totpCodeInput" inputmode="numeric" maxlength="20" :placeholder="$t('settings.totpOffPh')" />
                <span class="hint">{{ $t('settings.totpRecoveryLeft', { n: totp.recovery_left }) }}</span>
              </div>
              <div class="hstack">
                <button class="btn tiny danger" :disabled="busyTotp" @click="disableTotp">{{ $t('settings.totpDisable') }}</button>
              </div>
            </template>
          </div>
        </div>

        <!-- 事务 API Key -->
        <div class="panel">
          <div class="panel-head">{{ $t('settings.keyTitle') }}</div>
          <div class="panel-body">
            <span class="hint">{{ $t('settings.keyHint') }}</span>
            <div class="hstack">
              <input class="inp" v-model="newKeyName" :placeholder="$t('settings.keyNamePh')" style="max-width:200px" />
              <select class="inp" v-model="newKeyScope" style="max-width:170px">
                <option value="full">{{ $t('settings.keyScopeFull') }}</option>
                <option value="read">{{ $t('settings.keyScopeRead') }}</option>
              </select>
              <button class="btn tiny primary" :disabled="busyKey" @click="doCreateKey">{{ $t('settings.keyCreate') }}</button>
            </div>
            <div v-if="createdKey" class="fw-tip">
              <div class="fw-tip-title">🔑 {{ $t('settings.keyCreatedTitle') }}</div>
              <code class="fw-cmd mono">{{ createdKey }}</code>
              <div class="hstack">
                <button class="btn tiny ghost" @click="copyText(createdKey)">{{ $t('settings.copy') }}</button>
                <button class="btn tiny ghost" @click="createdKey = ''">{{ $t('common.cancel') }}</button>
              </div>
              <div class="fw-tip-body">{{ $t('settings.keyCreatedHint') }}</div>
            </div>
            <div class="field col" v-if="apiKeys.length">
              <div v-for="k in apiKeys" :key="k.id" class="sec-row">
                <span class="mono">{{ k.id }}</span>
                <span class="sec-grow">{{ k.name }}</span>
                <span class="muted sm">{{ k.scope === 'read' ? $t('settings.keyScopeRead') : $t('settings.keyScopeFull') }}</span>
                <span class="muted sm tabular" :title="k.last_used ? fmtDateTime(k.last_used) : ''">{{ k.last_used ? fmtAgo(k.last_used) : $t('settings.keyNever') }}</span>
                <button class="btn tiny ghost" @click="doRevokeKey(k.id)">{{ $t('settings.secRevoke') }}</button>
              </div>
            </div>
            <span v-else class="muted">{{ $t('settings.keyEmpty') }}</span>
          </div>
        </div>

        </details>
      </template>

      <!-- ============ 关于 ============ -->
      <template v-else>
        <!-- 主视觉：品牌 + 版本 + 操作 -->
        <div class="about-hero">
          <img class="about-logo" :src="logoUrl" :alt="$t('settings.brand')" />
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
/* 安全面板：活跃会话 / 登录记录行 */
.sec-row { display: flex; align-items: center; gap: 10px; padding: 5px 0; border-bottom: 1px solid var(--c-line, rgba(128,128,128,.18)); }
.sec-row:last-child { border-bottom: none; }
.sec-grow { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 两步验证绑定：左侧二维码 + 右侧密钥与验证码 */
.totp-bind { display: flex; gap: var(--sp-4, 16px); align-items: flex-start; flex-wrap: wrap; }
.totp-bind-side { flex: 1; min-width: 220px; display: flex; flex-direction: column; gap: var(--sp-2, 8px); }
.rec-codes { display: block; padding: var(--sp-2, 8px); border-radius: var(--r-sm, 6px); background: var(--c-surface-2, rgba(128,128,128,.12)); word-spacing: 10px; user-select: all; }
.st-tabs { border-bottom: none; }
/* 竖版海报批量进度条 */
.poster-progress { padding: 0 var(--sp-4) var(--sp-3); display: flex; flex-direction: column; gap: 4px; }
.poster-bar { height: 6px; border-radius: 3px; background: var(--c-surface-2); overflow: hidden; }
.poster-bar-in { height: 100%; background: var(--c-primary); transition: width .3s ease; }

/* 居中 + 固定列宽 + 滚动条槽位常驻：内容加载 / 切换标签时不再左右闪烁。
   width 固定（而非 max-width），scrollbar-gutter 让滚动条出现与否都不挤占内容区。 */
.view-body {
  width: min(900px, 100%);
  margin: 0 auto;
  scrollbar-gutter: stable;
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
.about-logo {
  width: 112px; height: 112px;   /* 正方形 logo */
  display: block;
  margin-bottom: var(--sp-3);
  border-radius: var(--r-lg);
  box-shadow: 0 8px 22px rgba(224, 53, 90, .28);
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

/* ---- 常用在线站点 ---- */
.sites-toolbar { margin-bottom: var(--sp-3); }
.sites-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.sites-item {
  display: flex; align-items: center; gap: var(--sp-3);
  padding: var(--sp-3); border: 1px solid var(--c-line); border-radius: var(--r-lg);
  background: var(--c-surface);
}
.sites-item .sites-info { flex: 1; min-width: 0; }
.sites-name { font-weight: 600; }
.sites-url { font-size: var(--fs-sm); color: var(--c-text-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sites-actions { display: flex; gap: var(--sp-2); flex-shrink: 0; }
.btn.sm { padding: 4px 10px; font-size: var(--fs-sm); }
.btn.ghost { background: transparent; border-color: var(--c-line); color: var(--c-text-2); }
.btn.ghost:hover { color: var(--c-text-1); border-color: var(--c-text-3); }
.btn.ghost.danger:hover { color: #e6355a; border-color: #e6355a; }
.empty-hint { color: var(--c-text-3); padding: var(--sp-4) 0; }
.form-actions { margin-top: var(--sp-4); }
/* switch 开关（小号） */
.switch { position: relative; display: inline-flex; align-items: center; cursor: pointer; }
.switch.sm { --w: 34px; --h: 18px; }
.switch input { position: absolute; opacity: 0; width: 0; height: 0; }
.switch .track {
  width: var(--w); height: var(--h); border-radius: 999px; background: var(--c-line);
  transition: background .15s; position: relative; flex: none;
}
.switch .track::after {
  content: ''; position: absolute; top: 2px; left: 2px; width: calc(var(--h) - 4px); height: calc(var(--h) - 4px);
  border-radius: 50%; background: #fff; transition: transform .15s;
}
.switch input:checked + .track { background: var(--c-primary); }
.switch input:checked + .track::after { transform: translateX(calc(var(--w) - var(--h))); }
/* modal 弹窗 */
.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: flex; align-items: center; justify-content: center; z-index: 200; }
.modal { width: min(460px, 92vw); background: var(--c-bg-pop); border: 1px solid var(--c-line); border-radius: var(--r-lg); box-shadow: 0 18px 50px rgba(0,0,0,.35); overflow: hidden; }
.modal-head { padding: var(--sp-4) var(--sp-4) 0; font-size: 1.05rem; font-weight: 700; }
.modal-body { padding: var(--sp-4); display: flex; flex-direction: column; gap: var(--sp-3); }
.modal-foot { padding: var(--sp-3) var(--sp-4); display: flex; justify-content: flex-end; gap: var(--sp-2); border-top: 1px solid var(--c-line); }
.form-row { display: flex; flex-direction: column; gap: 6px; }
.form-row > label { font-size: var(--fs-sm); color: var(--c-text-2); }
.form-row input { padding: 8px 10px; border-radius: var(--r-md); border: 1px solid var(--c-line); background: var(--c-bg-input); color: var(--c-text-1); }
.form-row.inline { flex-direction: row; align-items: center; gap: 8px; }
.form-hint { font-size: var(--fs-sm); color: var(--c-text-3); }
.preview-box { padding: var(--sp-3); border: 1px dashed var(--c-line); border-radius: var(--r-md); background: var(--c-surface); }
.preview-title { font-size: var(--fs-sm); color: var(--c-text-2); margin-bottom: 6px; }
.preview-link { color: var(--c-primary); word-break: break-all; font-size: var(--fs-sm); }
</style>
