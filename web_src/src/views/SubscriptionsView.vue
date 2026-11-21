<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { state } from '../state.js'
import {
  subTargets, subLibraryOptions, subTargetFromLibrary, subTargetsFromLibraryBulk,
  subTargetUpdate, subTargetDelete,
  subFindings, subFindingActresses, subFindingCover,
  subRead, subMarkUnread, subSeen, subIgnore, subRestore, subSave, subBulk,
  subRules, subRuleCreate, subRuleUpdate, subRuleDelete, subRulesPreview,
  subScanStart, subScanState, subScanCancel, subUnread,
  subSchedule, subSchedulePreview,
  subProbeStart, subProbeState, subProbeCancel, subProbeOne,
  getConfig, putConfig,
} from '../api.js'
import { t } from '../i18n/index.js'
import { toast } from '../utils.js'
import PageHead from '../components/PageHead.vue'

const TABS = [
  { id: 'new', label: 'subscriptions.tabs.new' },
  { id: 'targets', label: 'subscriptions.tabs.targets' },
  { id: 'rules', label: 'subscriptions.tabs.rules' },
]
const STATUSES = [
  { id: '', label: 'subscriptions.status.all' },
  { id: 'unread', label: 'subscriptions.status.unread' },
  { id: 'read', label: 'subscriptions.status.read' },
  { id: 'filtered', label: 'subscriptions.status.filtered' },
  { id: 'ignored', label: 'subscriptions.status.ignored' },
  { id: 'archived', label: 'subscriptions.status.archived' },
]
// 高级规则：普通收藏者用不到，收进折叠区
const ADVANCED_TYPES = ['exclude_code', 'code_regex', 'release_after', 'release_before', 'exclude_co_star']
// 常用关键词预设：VR / 合集这类一眼就知道不要的
const KW_PRESETS = ['VR', '合集', '総集編', 'BEST', '素人', '无修正']

const tab = ref(state.subTab || 'new')
const loading = ref(false)
const error = ref('')
// 女优页「规则」入口：直接定位到该目标的单独偏好
const prefTargetId = ref(state.subTarget || null)
state.subTab = ''
state.subTarget = null

/* 新片 */
const status = ref('')
const targetFilter = ref('')
const q = ref('')
const actress = ref('')
const actressOpts = ref([])
const sortBy = ref('release')
const items = ref([])
const cursor = ref('')
const hasMore = ref(false)
// 批量操作：勾选当前列表里的条目，一次标已读 / 归档 / 忽略
const selectedIds = ref([])
const selectedCount = computed(() => selectedIds.value.length)
const allSelected = computed(
  () => items.value.length > 0 && selectedIds.value.length === items.value.length)
function isSelected(id) {
  return selectedIds.value.includes(id)
}
function toggleSelect(id) {
  const i = selectedIds.value.indexOf(id)
  if (i >= 0) selectedIds.value.splice(i, 1)
  else selectedIds.value.push(id)
}
function toggleSelectAll() {
  selectedIds.value = allSelected.value ? [] : items.value.map(x => x.id)
}
// 只在换筛选条件时清空勾选：翻页 / 新增一页都保留，跨页批量才不会白勾
function clearSelection() {
  selectedIds.value = []
}
// 列表形态：紧凑（一行一条，翻得快）/ 大卡片（封面更大、信息更全）；记住上次选择
const layout = ref(localStorage.getItem('avm.subLayout') === 'large' ? 'large' : 'compact')
function setLayout(v) {
  if (layout.value === v) return
  layout.value = v
  try { localStorage.setItem('avm.subLayout', v) } catch (e) { /* 隐私模式忽略 */ }
}
// 卡片上的「不想看」菜单：同时只开一个（站外站点菜单靠悬停标题展开，不需要状态）
const menuFor = ref(null)
// 目标行的「⋯」菜单：同时只开一个
const tgMenu = ref(null)
// 新片筛选：常用状态+搜索常驻，女优/目标/排序收进「筛选」
const showFilters = ref(false)
const hasAdvFilters = computed(() => !!(actress.value || targetFilter.value))
function toggleTgMenu(id) {
  tgMenu.value = tgMenu.value === id ? null : id
}
const onlineSites = ref([])
const cardKw = ref('')
// 放大看封面（灯箱）
const zoom = ref('')
// 按订阅目标分组看新片：一次只装当前这一页，翻页仍是同一套游标
const groupByTarget = ref(localStorage.getItem('avm.subGroup') === '1')
const renderRows = computed(() => {
  if (!groupByTarget.value) return items.value.map(it => ({ type: 'item', it }))
  const groups = new Map()
  for (const it of items.value) {
    const name = (it.targets && it.targets[0]) || t('subscriptions.groups.untargeted')
    if (!groups.has(name)) groups.set(name, [])
    groups.get(name).push(it)
  }
  const rows = []
  for (const [name, list] of groups) {
    rows.push({ type: 'group', name, count: list.length })
    for (const it of list) rows.push({ type: 'item', it })
  }
  return rows
})
function toggleGroup() {
  groupByTarget.value = !groupByTarget.value
  try {
    localStorage.setItem('avm.subGroup', groupByTarget.value ? '1' : '0')
  } catch (e) { /* 隐私模式忽略 */ }
}
let findTimer = null

/* 目标：从库里选，不再手填站点 ID / 页面地址 */
const targets = ref([])
// 已订阅列表的本地筛选：搜索 + 类型 / 停用
const tgQ = ref('')
const tgFilter = ref('')
const shownTargets = computed(() => {
  const kw = tgQ.value.trim().toLowerCase()
  return targets.value.filter((x) => {
    if (tgFilter.value === 'off') {
      if (x.enabled) return false
    } else if (tgFilter.value && x.kind !== tgFilter.value) return false
    if (!kw) return true
    return String(x.name || '').toLowerCase().includes(kw)
      || String(x.site_id || '').toLowerCase().includes(kw)
  })
})
const candKind = ref('actress')
const candQ = ref('')
const followedOnly = ref(false)
// 只看已订阅：从订阅目标出发反查库内信息，用来查看 / 清理（不受候选 Top N 限制）
const subOnly = ref(false)
const candidates = ref([])
const candLoading = ref(false)
let candTimer = null

/* 过滤偏好：把 rule_type / value 这类术语藏起来，只留收藏者能直接选的东西 */
const rules = ref([])
const advForm = ref({ rule_type: 'exclude_code', value: '' })
const preview = ref(null)
const showAdv = ref(false)
const kwInput = ref('')
const actressInput = ref('')
const prefs = ref({ watched: true, ignored: true, recent_days: '', keywords: [], actresses: [] })
const savedAt = ref(0)
let savedTimer = null
let previewTimer = null

/* 扫描与扫描设置（写回 config.subscription） */
const subCfg = ref({
  enabled: true,
  scan_mode: 'interval',
  scan_cron: '0 8 * * *',
  catch_up_on_start: true,
  scan_interval_minutes: 360,
  auto_probe: true,
})
/* 自动扫描的定时状态与表达式预览：下次何时跑、表达式是否合法 */
const schedule = ref({})
const cronPreview = ref(null)
let cronTimer = null

const scan = ref(null)
let scanTimer = null
let unreadTimer = null
const unread = ref(0)
/* 在线资源探测（后台任务，复用扫描那套轮询） */
const probe = ref(null)
let probeTimer = null
// 慢轮询：扫描后会自动在后台探测在线资源，页面开着时也要能自己刷出新标记
let probeWatchTimer = null

function errText(e) {
  return (e && e.message) || t('subscriptions.msg.failed')
}

function coverOf(it) {
  // 外站封面直链在浏览器里常被墙 / 防盗链，统一走本地缓存接口；
  // 带上探测时间做版本号，封面升级成大图后浏览器会重新取。
  // 大卡片要铺满整列，缩略图（约 147×200）放大会糊，直接取官方大图（拿不到后端自动回退）。
  if (!it.cover) return ''
  return subFindingCover(it.id, it.probe_at || '', layout.value === 'large' ? 'large' : '')
}

// 类型标签：探测详情时顺带抓到；紧凑模式少显示，大卡片多显示
function tagsOf(it) {
  const g = (it.meta && it.meta.genres) || []
  return Array.isArray(g) ? g.slice(0, layout.value === 'large' ? 8 : 4) : []
}

function openZoom(it) {
  if (!it.cover) return
  // 放大用官方大图；后端拿不到大图会自动退回缩略图，所以不会白屏
  zoom.value = subFindingCover(it.id, it.probe_at || '', 'large')
  if (it.status === 'new') markRead(it)
}

function onKeydown(e) {
  if (e.key === 'Escape') {
    zoom.value = ''
    menuFor.value = null
  }
}

function reasonsText(it) {
  return (it.reasons || []).map(r => t('subscriptions.reasons.' + r)).join('、')
}

function blockedText(b) {
  return (b.reasons || []).map(r => t('subscriptions.reasons.' + r)).join('、')
}

async function loadTargets() {
  const r = await subTargets()
  targets.value = (r && r.items) || []
}

async function loadFindings(reset = true) {
  loading.value = true
  error.value = ''
  try {
    const r = await subFindings({
      status: status.value,
      target_id: targetFilter.value || '',
      q: q.value.trim(),
      actress: actress.value,
      sort: sortBy.value,
      cursor: reset ? '' : cursor.value,
      limit: 30,
    })
    const list = (r && r.items) || []
    items.value = reset ? list : items.value.concat(list)
    cursor.value = (r && r.next_cursor) || ''
    hasMore.value = !!(r && r.has_more && cursor.value)
  } catch (e) {
    error.value = errText(e)
  } finally {
    loading.value = false
    // 列表已经一屏放得下 = 用户一眼看完了：等同于滚到底
    setTimeout(checkSeenAtBottom, 60)
  }
}

async function loadFindingActresses() {
  try {
    const r = await subFindingActresses({ target_id: targetFilter.value || '' })
    actressOpts.value = (r && r.items) || []
    // 选中的女优在当前范围里已经不存在了，就别让它继续当隐形筛选条件
    if (actress.value && !actressOpts.value.some(x => x.name === actress.value)) {
      actress.value = ''
    }
  } catch (e) { actressOpts.value = [] }
}

function onSearchInput() {
  if (findTimer) clearTimeout(findTimer)
  findTimer = setTimeout(() => { clearSelection(); loadFindings() }, 300)
}

function onTargetFilter() {
  actress.value = ''
  clearSelection()
  loadFindingActresses()
  loadFindings()
}

function onActressFilter() {
  clearSelection()
  loadFindings()
}

// 清除「筛选」里的高级条件（状态与搜索保留，用户能一眼看到自己点了什么）
function clearFilters() {
  actress.value = ''
  targetFilter.value = ''
  clearSelection()
  loadFindingActresses()
  loadFindings()
}

function setSort(v) {
  if (sortBy.value === v) return
  sortBy.value = v
  clearSelection()
  loadFindings()
}

async function loadRules() {
  const r = await subRules(prefTargetId.value || undefined)
  rules.value = (r && r.items) || []
  syncPrefs()
  schedulePreview()
}

async function refreshUnread() {
  try {
    const r = await subUnread()
    unread.value = Number((r && r.count) || 0)
  } catch (e) { /* 角标失败不影响页面 */ }
}

// 徽章清零时机：滚到底把这一页都看过，或主动打开「未读」页——不再进页面 5 秒就清
const viewBodyEl = ref(null)
let seenAt = 0
async function markSeen() {
  if (tab.value !== 'new' || !unread.value) return
  if (Date.now() - seenAt < 30000) return
  seenAt = Date.now()
  try {
    await subSeen()
    unread.value = 0
  } catch (e) { seenAt = 0 }
}

function checkSeenAtBottom() {
  if (tab.value !== 'new' || !unread.value) return
  // 页面滚动发生在 .view-body 里，不是 window
  const el = viewBodyEl.value
  if (!el) return
  if (el.scrollHeight - el.scrollTop - el.clientHeight < 120) markSeen()
}

async function loadCfg() {
  try {
    const c = await getConfig()
    const s = (c && c.subscription) || {}
    subCfg.value = {
      enabled: s.enabled !== false,
      scan_mode: String(s.scan_mode || '') === 'cron' ? 'cron' : 'interval',
      scan_cron: String(s.scan_cron || '') || '0 8 * * *',
      catch_up_on_start: s.catch_up_on_start !== false,
      scan_interval_minutes: Number(s.scan_interval_minutes) || 360,
      auto_probe: s.auto_probe !== false,
    }
    // 详情页那套第三方站点，卡片上直接复用
    onlineSites.value = ((c && c.online_sites) || []).filter(
      x => x.enabled && x.url && x.url.includes('{code}'))
  } catch (e) { /* 读不到就用默认值 */ }
  loadSchedule()
}

/** 自动扫描的定时状态：上次跑了没、下次什么时候跑 */
async function loadSchedule() {
  try { schedule.value = await subSchedule() } catch (e) { schedule.value = {} }
}

/** cron 输入防抖预览：停手 350ms 再请求，别每个字符打一次接口 */
function previewCron() {
  clearTimeout(cronTimer)
  if (subCfg.value.scan_mode !== 'cron') { cronPreview.value = null; return }
  const expr = String(subCfg.value.scan_cron || '').trim()
  if (!expr) { cronPreview.value = null; return }
  cronTimer = setTimeout(async () => {
    try { cronPreview.value = await subSchedulePreview(expr, 3) }
    catch (e) { cronPreview.value = { ok: false, error: errText(e), next: [] } }
  }, 350)
}

watch(() => subCfg.value.scan_cron, previewCron)
watch(() => subCfg.value.scan_mode, previewCron)

// 站外菜单按类别分组：观看站命中才算「有资源」，资料站只算有资料页
const watchSites = computed(() => onlineSites.value.filter((s) => s.kind === 'watch'))
const metaSites = computed(() => onlineSites.value.filter((s) => s.kind !== 'watch'))

// 数据来源站点：卡片上标出这条发现是从哪个站抓到的
const SITE_NAMES = { javbus: 'JavBus', javdb: 'JavDB' }
function siteLabel(s) {
  const v = String(s || '')
  if (!v) return ''
  if (v === 'manual') return t('subscriptions.sites.manual')
  return SITE_NAMES[v] || v
}

function siteUrl(site, it) {
  const codeVal = site.lower ? String(it.code || '').toLowerCase() : String(it.code || '')
  const titleVal = site.lower ? String(it.title || '').toLowerCase() : String(it.title || '')
  return String(site.url)
    .replace(/\{code\}/g, encodeURIComponent(codeVal))
    .replace(/\{title\}/g, encodeURIComponent(titleVal))
}

function openSite(site, it) {
  const final = siteUrl(site, it)
  if (!final) return
  window.open(final, '_blank', 'noopener')
  if (it.status === 'new') markRead(it)
}

async function saveCfg() {
  const mode = subCfg.value.scan_mode === 'cron' ? 'cron' : 'interval'
  const expr = String(subCfg.value.scan_cron || '').trim() || '0 8 * * *'
  if (mode === 'cron') {
    // 表达式写错就不保存：存下去后台会直接跳过扫描，等于静默失效
    try {
      const p = await subSchedulePreview(expr, 1)
      if (!p || !p.ok) {
        toast(t('subscriptions.cfg.cronBad', { msg: (p && p.error) || '?' }), 'err')
        return
      }
    } catch (e) { toast(errText(e), 'err'); return }
  }
  try {
    await putConfig({
      subscription: {
        enabled: !!subCfg.value.enabled,
        auto_probe: !!subCfg.value.auto_probe,
        scan_mode: mode,
        scan_cron: expr,
        catch_up_on_start: !!subCfg.value.catch_up_on_start,
        scan_interval_minutes: Math.max(30, Math.min(10080,
          Number(subCfg.value.scan_interval_minutes) || 360)),
      },
    })
    toast(t('subscriptions.msg.cfgSaved'), 'ok')
    loadSchedule()
  } catch (e) { toast(errText(e), 'err') }
}

async function loadScan() {
  try {
    const st = await subScanState()
    scan.value = st
    if (st && st.running) startScanPoll()
  } catch (e) { scan.value = null }
}

function startScanPoll() {
  if (scanTimer) return
  scanTimer = setInterval(async () => {
    // 页面在后台时不发请求：扫描不会因为前端不看就停，回来再续上
    if (document.hidden) return
    try {
      const st = await subScanState()
      scan.value = st
      if (!st.running) {
        clearInterval(scanTimer)
        scanTimer = null
        if (st.archived > 0) {
          toast(t('subscriptions.msg.scanArchived', { n: st.archived }), 'ok')
        }
        await Promise.all([loadFindings(), loadTargets(), refreshUnread(), loadSchedule()])
      }
    } catch (e) {
      clearInterval(scanTimer)
      scanTimer = null
    }
  }, 1500)
}

async function doScan() {
  try {
    scan.value = await subScanStart()
    startScanPoll()
  } catch (e) { toast(errText(e), 'err') }
}

// 只扫这一个目标：新增订阅后不用等整批；mode='backfill' 时往前翻补老片
async function scanOneTarget(tg, mode = '') {
  try {
    const body = { target_ids: [tg.id] }
    if (mode) body.mode = mode
    scan.value = await subScanStart(body)
    startScanPoll()
    toast(t(mode === 'backfill' ? 'subscriptions.msg.backfillStarted'
                                : 'subscriptions.msg.scanStarted', { name: tg.name }), 'ok')
  } catch (e) { toast(errText(e), 'err') }
}

async function cancelScan() {
  try { scan.value = await subScanCancel() } catch (e) { /* 幂等 */ }
}

async function loadProbe() {
  try {
    const st = await subProbeState()
    probe.value = st
    if (st && st.running) startProbePoll()
  } catch (e) { probe.value = null }
}

function startProbePoll() {
  if (probeTimer) return
  probeTimer = setInterval(async () => {
    if (document.hidden) return
    try {
      const st = await subProbeState()
      probe.value = st
      if (!st.running) {
        clearInterval(probeTimer)
        probeTimer = null
        // 完成时把「有资源 / 无资源 / 未知 / 跳过待确认」的结论直接告诉用户
        if (st.message && st.finished_at) toast(st.message, 'ok')
        await loadFindings()
      }
    } catch (e) {
      clearInterval(probeTimer)
      probeTimer = null
    }
  }, 1500)
}

function startProbeWatch() {
  if (probeWatchTimer) return
  probeWatchTimer = setInterval(async () => {
    if (document.hidden) return
    try {
      const st = await subProbeState()
      // 后台自动探测开跑时切到快轮询，跑完由 startProbePoll 刷新列表出徽章
      if (st && st.running && !probeTimer) {
        probe.value = st
        startProbePoll()
      }
    } catch (e) { /* 角标类轮询失败不打扰用户 */ }
  }, 15000)
}

async function doProbe() {
  try {
    probe.value = await subProbeStart({ force: false })
    startProbePoll()
  } catch (e) { toast(errText(e), 'err') }
}

async function cancelProbe() {
  try { probe.value = await subProbeCancel() } catch (e) { /* 幂等 */ }
}

// 单条立即重探：卡片上的「重新探测」，只问一条、不走批量任务
const probingIds = ref([])
function isProbing(id) {
  return probingIds.value.includes(id)
}
async function probeOne(it) {
  if (isProbing(it.id)) return
  probingIds.value.push(it.id)
  try {
    const r = await subProbeOne(it.id)
    it.probe_site = r.probe_site || ''
    it.probe_status = r.probe_status || ''
    it.probe_kind = r.probe_kind || ''
    it.probe_at = r.probe_at || ''
    if (r.result === 'ok') {
      toast(t('subscriptions.msg.probeFound', { site: it.probe_site || '' }), 'ok')
    } else if (r.result === 'none') {
      toast(t('subscriptions.msg.probeNone'))
    } else {
      toast(t('subscriptions.msg.probeUnknown'), 'err')
    }
  } catch (e) {
    toast(errText(e), 'err')
  } finally {
    probingIds.value = probingIds.value.filter(x => x !== it.id)
  }
}

async function setStatus(s) {
  status.value = s
  clearSelection()
  await loadFindings()
  // 主动打开「未读」页就看过了：徽章清零，但不动单条状态
  if (s === 'unread') markSeen()
}

// 点标题默认打开「命中的观看站」，没有命中就用第一个观看站，最后退回来源页
function defaultSiteUrl(it) {
  const hit = onlineSites.value.find(
    (s) => (s.name || s.id) === (it.probe_site || '') && s.kind === 'watch')
  const site = hit || watchSites.value[0]
  return site ? siteUrl(site, it) : (it.page_url || '')
}

async function openItem(it) {
  const target = defaultSiteUrl(it)
  if (!target) return
  window.open(target, '_blank', 'noopener')
  if (it.status === 'new') markRead(it)
}

async function markRead(it) {
  try {
    await subRead(it.id)
    it.status = 'read'
    refreshUnread()
  } catch (e) { toast(errText(e), 'err') }
}

// 手动标记：已读 ⇄ 未读（未读只用于自己识别，顶部徽章只认「上次查看之后的新片」）
async function toggleRead(it) {
  const wantUnread = it.status !== 'new'
  try {
    if (wantUnread) await subMarkUnread(it.id)
    else await subRead(it.id)
    it.status = wantUnread ? 'new' : 'read'
    refreshUnread()
  } catch (e) { toast(errText(e), 'err') }
}

async function doIgnore(it) {
  try {
    await subIgnore(it.id)
    it.status = 'ignored'
    items.value = items.value.filter(x => x.id !== it.id)
    refreshUnread()
    toast(t('subscriptions.msg.ignored'), 'ok')
  } catch (e) { toast(errText(e), 'err') }
}

async function doRestore(it) {
  try {
    await subRestore(it.id)
    it.status = 'new'
    await loadFindings()
    refreshUnread()
    toast(t('subscriptions.msg.restored'), 'ok')
  } catch (e) { toast(errText(e), 'err') }
}

// 批量：read / archive / ignore / restore
async function bulkDo(action) {
  if (!selectedIds.value.length) return
  const ids = selectedIds.value.slice()
  try {
    const r = await subBulk(ids, action)
    if (action === 'read') {
      items.value.forEach((x) => { if (ids.includes(x.id) && x.status === 'new') x.status = 'read' })
    } else if (action === 'unread') {
      items.value.forEach((x) => { if (ids.includes(x.id) && x.status !== 'ignored') x.status = 'new' })
    } else {
      // 归档 / 忽略 / 恢复：条目离开了当前这一栏，直接从列表移除
      items.value = items.value.filter(x => !ids.includes(x.id))
    }
    selectedIds.value = []
    refreshUnread()
    toast(t('subscriptions.msg.bulkDone', { n: (r && r.updated) || 0 }), 'ok')
  } catch (e) { toast(errText(e), 'err') }
}

async function doSave(it) {
  try {
    const r = await subSave(it.id)
    if (r && r.ok) {
      // 入库后就不再算「待处理的新片」：直接从列表移除（想看请去影片库）
      items.value = items.value.filter(x => x.id !== it.id)
      toast(t('subscriptions.msg.saved'), 'ok')
      refreshUnread()
    } else {
      toast((r && r.message) || t('subscriptions.msg.failed'), 'err')
    }
  } catch (e) { toast(errText(e), 'err') }
}

async function loadCandidates() {
  candLoading.value = true
  try {
    const r = await subLibraryOptions({
      kind: candKind.value,
      q: candQ.value.trim(),
      followed_only: candKind.value === 'actress' && followedOnly.value,
      subscribed_only: subOnly.value,
      limit: 60,
    })
    candidates.value = (r && r.items) || []
  } catch (e) {
    candidates.value = []
    toast(errText(e), 'err')
  } finally { candLoading.value = false }
}

function onCandKind(k) {
  if (candKind.value === k) return
  candKind.value = k
  loadCandidates()
}

function onCandSearch() {
  if (candTimer) clearTimeout(candTimer)
  candTimer = setTimeout(loadCandidates, 250)
}

// 取消订阅（候选列表里直接清理，不用滚到下面的目标列表）
async function unsubscribeCand(c) {
  if (!c.target_id) return
  if (!window.confirm(t('subscriptions.confirm.unsubscribe', { name: c.name }))) return
  try {
    await subTargetDelete(c.target_id)
    await Promise.all([loadTargets(), loadCandidates()])
    toast(t('subscriptions.msg.targetDeleted'), 'ok')
  } catch (e) { toast(errText(e), 'err') }
}

async function subscribeCand(c) {
  if (c.subscribed) return
  try {
    const r = await subTargetFromLibrary({ kind: c.kind, name: c.name })
    c.subscribed = true
    c.target_id = ((r && r.item) || {}).id || 0
    await loadTargets()
    toast(t('subscriptions.msg.targetAdded'), 'ok')
  } catch (e) { toast(errText(e), 'err') }
}

async function subscribeAllFollowed() {
  if (!window.confirm(t('subscriptions.confirm.subscribeAllFollowed'))) return
  try {
    const r = await subTargetsFromLibraryBulk({ kind: 'actress' })
    await Promise.all([loadTargets(), loadCandidates()])
    toast(t('subscriptions.msg.bulkAdded', { added: (r && r.added) || 0, skipped: (r && r.skipped) || 0 }), 'ok')
  } catch (e) { toast(errText(e), 'err') }
}

async function toggleTarget(row) {
  try {
    await subTargetUpdate(row.id, { enabled: !row.enabled })
    row.enabled = !row.enabled
  } catch (e) { toast(errText(e), 'err') }
}

// 改「起始日期」：早于该日的作品不进订阅；改早后重新扫描即可补进老片
async function onBaselineChange(row, ev) {
  const val = String(ev.target.value || '').trim()
  if (!val || val === row.baseline_at) return
  try {
    const r = await subTargetUpdate(row.id, { baseline_at: val })
    row.baseline_at = (r && r.item && r.item.baseline_at) || val
    toast(t('subscriptions.msg.baselineSaved'), 'ok')
  } catch (e) {
    ev.target.value = row.baseline_at || ''
    toast(errText(e), 'err')
  }
}

async function removeTarget(row) {
  if (!window.confirm(t('subscriptions.confirm.deleteTarget', { name: row.name }))) return
  try {
    await subTargetDelete(row.id)
    await loadTargets()
    toast(t('subscriptions.msg.targetDeleted'), 'ok')
  } catch (e) { toast(errText(e), 'err') }
}

/* ------------------------------ 过滤偏好 */
const prefTarget = computed(
  () => targets.value.find(x => String(x.id) === String(prefTargetId.value)) || null)
const isTargetScope = computed(() => !!prefTargetId.value)
const targetRules = computed(() => rules.value.filter(
  r => r.target_id != null && String(r.target_id) === String(prefTargetId.value)))
const globalRules = computed(() => rules.value.filter(r => r.target_id == null))
const activeRules = computed(() => (isTargetScope.value ? targetRules.value : globalRules.value))
// 「当前生效」条数：用于顶部摘要，比逐个看开关直观
const prefActiveCount = computed(() => {
  const p = prefs.value || {}
  return (p.watched ? 1 : 0) + (p.ignored ? 1 : 0)
    + (Number(p.recent_days) > 0 ? 1 : 0)
    + ((p.keywords || []).length ? 1 : 0)
    + ((p.actresses || []).length ? 1 : 0)
    // 只算高级规则：watched / ignored / recent_days 等已由上面的开关单独计数
    + (advancedRules.value.length ? 1 : 0)
})
const advancedRules = computed(() => rules.value.filter((r) => {
  if (!ADVANCED_TYPES.includes(r.rule_type)) return false
  if (r.target_id == null) return true
  return isTargetScope.value && String(r.target_id) === String(prefTargetId.value)
}))

function intVal(v) {
  return Math.floor(Number(v) || 0)
}

function splitValues(raw) {
  return String(raw || "").split(/[\n,，;；]+/).map(s => s.trim()).filter(Boolean)
}

// 目标范围内没单独设置的类型 → 跟随全局；两边都没有就用内置默认
function ruleOf(type) {
  const own = activeRules.value.find(r => r.rule_type === type)
  if (own) return own
  return isTargetScope.value ? globalRules.value.find(r => r.rule_type === type) : null
}

function syncPrefs() {
  const watched = ruleOf('exclude_watched')
  const ignored = ruleOf('exclude_ignored')
  const days = ruleOf('recent_days')
  const kw = ruleOf('keyword')
  const ac = ruleOf('exclude_actress')
  prefs.value = {
    watched: watched ? intVal(watched.value) > 0 : true,
    ignored: ignored ? intVal(ignored.value) > 0 : true,
    recent_days: days && intVal(days.value) > 0 ? String(intVal(days.value)) : '',
    keywords: kw ? splitValues(kw.value) : [],
    actresses: ac ? splitValues(ac.value) : [],
  }
}

// 改动即时落到本地面板，不再整页重载；右下角只闪一个「已保存」
function markSaved() {
  savedAt.value = Date.now()
  if (savedTimer) clearTimeout(savedTimer)
  savedTimer = setTimeout(() => { savedAt.value = 0 }, 1600)
}

function upsertLocal(item) {
  if (!item) return
  const i = rules.value.findIndex(r => r.id === item.id)
  if (i >= 0) rules.value.splice(i, 1, item)
  else rules.value.push(item)
}

// 只改当前范围的规则；目标范围会写入独立行来覆盖全局
async function writePref(type, value) {
  const own = activeRules.value.find(r => r.rule_type === type)
  let item = null
  if (own) {
    const r = await subRuleUpdate(own.id, { value, enabled: true })
    item = r && r.item
  } else if (isTargetScope.value || value !== '') {
    const r = await subRuleCreate({
      target_id: isTargetScope.value ? Number(prefTargetId.value) : null,
      rule_type: type,
      value,
    })
    item = r && r.item
  }
  upsertLocal(item)
  markSaved()
  schedulePreview()
}

async function setBoolPref(key, type) {
  prefs.value[key] = !prefs.value[key]
  const next = prefs.value[key]
  try {
    await writePref(type, next ? '1' : '0')
  } catch (e) {
    toast(errText(e), 'err')
    syncPrefs()
  }
}

async function saveRecentDays() {
  const n = Math.max(0, intVal(prefs.value.recent_days))
  prefs.value.recent_days = n > 0 ? String(n) : ''
  const own = activeRules.value.find(r => r.rule_type === 'recent_days')
  try {
    // 全局不限期就直接删掉，不留一条无意义的规则
    if (!isTargetScope.value && n <= 0) {
      if (own) {
        await subRuleDelete(own.id)
        rules.value = rules.value.filter(r => r.id !== own.id)
        markSaved()
        schedulePreview()
      }
      return
    }
    await writePref('recent_days', String(n))
  } catch (e) { toast(errText(e), 'err'); syncPrefs() }
}

async function saveTags(kind, type) {
  try {
    await writePref(type, prefs.value[kind].join('\n'))
  } catch (e) { toast(errText(e), 'err'); syncPrefs() }
}

async function addTag(kind, type, raw) {
  const v = String(raw || '').trim()
  if (!v || prefs.value[kind].includes(v)) return
  prefs.value[kind] = [...prefs.value[kind], v]
  await saveTags(kind, type)
}

async function removeTag(kind, type, v) {
  prefs.value[kind] = prefs.value[kind].filter(x => x !== v)
  await saveTags(kind, type)
}

async function togglePreset(p) {
  if (prefs.value.keywords.includes(p)) await removeTag('keywords', 'keyword', p)
  else await addTag('keywords', 'keyword', p)
}

function openTargetPrefs(tg) {
  prefTargetId.value = tg.id
  tab.value = 'rules'
}

function backToGlobal() {
  prefTargetId.value = null
  loadRules()
}

async function resetToGlobal() {
  if (!isTargetScope.value || !targetRules.value.length) return
  if (!window.confirm(t('subscriptions.confirm.resetScope'))) return
  try {
    const ids = targetRules.value.map(r => r.id)
    for (const id of ids) await subRuleDelete(id)
    rules.value = rules.value.filter(r => !ids.includes(r.id))
    syncPrefs()
    markSaved()
    schedulePreview()
  } catch (e) { toast(errText(e), 'err') }
}

/* 卡片上的「不想看」：屏蔽女优 / 关键词，直接写全局偏好 */
function actressesOf(it) {
  const names = (it && it.meta && it.meta.actresses) || []
  return [...new Set(names.map(x => String(x || '').trim()).filter(Boolean))]
}

function toggleMenu(it) {
  cardKw.value = ''
  menuFor.value = menuFor.value === it.id ? null : it.id
}

async function addGlobalValues(type, values) {
  try {
    // 卡片可能在没打开过过滤页时操作，先取一次最新全局规则再合并，避免写出重复行
    const listed = await subRules()
    const g = ((listed && listed.items) || []).find(r => r.rule_type === type)
    const merged = [...new Set([...(g ? splitValues(g.value) : []), ...values])]
    const value = merged.join('\n')
    const r = g
      ? await subRuleUpdate(g.id, { value, enabled: true })
      : await subRuleCreate({ target_id: null, rule_type: type, value })
    upsertLocal(r && r.item)
    syncPrefs()
    markSaved()
    schedulePreview()
    return true
  } catch (e) {
    toast(errText(e), 'err')
    return false
  }
}

async function blockActress(name) {
  if (!(await addGlobalValues('exclude_actress', [name]))) return
  items.value = items.value.filter(x => !actressesOf(x).includes(name))
  menuFor.value = null
  toast(t('subscriptions.msg.blockedActress', { name }), 'ok')
}

async function blockKeyword(word) {
  const v = String(word || '').trim()
  if (!v) return
  if (!(await addGlobalValues('keyword', [v]))) return
  const low = v.toLowerCase()
  items.value = items.value.filter(x => !String(x.title || '').toLowerCase().includes(low))
  cardKw.value = ''
  menuFor.value = null
  toast(t('subscriptions.msg.blockedKeyword', { word: v }), 'ok')
}

/* 高级规则：番号 / 正则 / 发行区间 */
async function addRule() {
  // 共演 / 合集是开关式规则，不需要填值
  const value = advForm.value.rule_type === 'exclude_co_star'
    ? '1' : advForm.value.value.trim()
  if (!value) { toast(t('subscriptions.msg.valueRequired'), 'err'); return }
  try {
    const r = await subRuleCreate({
      target_id: isTargetScope.value ? Number(prefTargetId.value) : null,
      rule_type: advForm.value.rule_type,
      value,
    })
    upsertLocal(r && r.item)
    advForm.value.value = ''
    markSaved()
    schedulePreview()
  } catch (e) { toast(errText(e), 'err') }
}

async function toggleRule(row) {
  try {
    const r = await subRuleUpdate(row.id, { enabled: !row.enabled })
    upsertLocal((r && r.item) || { ...row, enabled: !row.enabled })
    markSaved()
  } catch (e) { toast(errText(e), 'err') }
}

async function removeRule(row) {
  try {
    await subRuleDelete(row.id)
    rules.value = rules.value.filter(r => r.id !== row.id)
    syncPrefs()
    markSaved()
    schedulePreview()
  } catch (e) { toast(errText(e), 'err') }
}

async function runPreview() {
  try {
    preview.value = await subRulesPreview({ target_id: prefTargetId.value || null })
  } catch (e) { preview.value = null }
}

function schedulePreview() {
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = setTimeout(runPreview, 500)
}

function onVisible() {
  if (!document.hidden) refreshUnread()
}

// 进入过滤 Tab 时按当前范围加载；默认全局
watch(tab, (v) => {
  if (v === 'rules') loadRules()
})

onMounted(async () => {
  document.addEventListener('visibilitychange', onVisible)
  document.addEventListener('keydown', onKeydown)
  if (viewBodyEl.value) viewBodyEl.value.addEventListener('scroll', checkSeenAtBottom, { passive: true })
  await Promise.all([loadTargets(), loadFindings(), refreshUnread(), loadScan(), loadCfg(),
    loadCandidates()])
  loadFindingActresses()
  loadProbe()
  startProbeWatch()
  if (tab.value === 'rules') loadRules()
  unreadTimer = setInterval(() => { if (!document.hidden) refreshUnread() }, 60000)
})

onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', onVisible)
  document.removeEventListener('keydown', onKeydown)
  if (viewBodyEl.value) viewBodyEl.value.removeEventListener('scroll', checkSeenAtBottom)
  if (unreadTimer) clearInterval(unreadTimer)
  if (scanTimer) clearInterval(scanTimer)
  if (probeTimer) clearInterval(probeTimer)
  if (probeWatchTimer) clearInterval(probeWatchTimer)
  if (candTimer) clearTimeout(candTimer)
  if (previewTimer) clearTimeout(previewTimer)
  if (savedTimer) clearTimeout(savedTimer)
  if (findTimer) clearTimeout(findTimer)
  if (cronTimer) clearTimeout(cronTimer)
})
</script>

<template>
  <section class="view">
    <div ref="viewBodyEl" class="view-body sub-view">
      <PageHead :title="$t('subscriptions.title')" :sub="$t('subscriptions.sub')">
        <template #actions>
          <span v-if="unread" class="sub-unread">{{ $t('subscriptions.unreadN', { n: unread }) }}</span>
          <button class="btn ghost" :disabled="(probe && probe.running) || (scan && scan.running)"
                  @click="doProbe">
            {{ probe && probe.running ? $t('subscriptions.actions.probing') : $t('subscriptions.actions.probe') }}
          </button>
          <button class="btn" :disabled="scan && scan.running" @click="doScan">
            {{ scan && scan.running ? $t('subscriptions.actions.scanning') : $t('subscriptions.actions.scanNow') }}
          </button>
        </template>
      </PageHead>

      <div class="sub-scan" v-if="scan && scan.running">
        <div class="sub-scan-line">
          <div class="bar"><i :style="{ width: (scan.percent || 0) + '%' }"></i></div>
          <span class="muted sm ellipsis">{{ scan.message }}</span>
          <button class="link-x" @click="cancelScan">{{ $t('subscriptions.actions.cancelScan') }}</button>
        </div>
      </div>
      <div class="sub-scan" v-if="probe && probe.running">
        <div class="sub-scan-line">
          <div class="bar"><i :style="{ width: (probe.percent || 0) + '%' }"></i></div>
          <span class="muted sm ellipsis">
            {{ $t('subscriptions.probe.progress', {
              done: probe.done || 0, total: probe.total || 0,
              found: probe.found || 0, missing: probe.missing || 0,
              unknown: probe.unknown || 0 }) }}
          </span>
          <button class="link-x" @click="cancelProbe">{{ $t('subscriptions.actions.cancelProbe') }}</button>
        </div>
      </div>
      <!-- 上一轮探测结论：站点不可达 / 待确认跳过的条数在这里能看见 -->
      <div class="sub-scan" v-else-if="probe && probe.finished_at && probe.message">
        <div class="sub-scan-line">
          <span class="muted sm ellipsis" :title="probe.message">{{ probe.message }}</span>
        </div>
      </div>

      <div class="sub-tabs chip-list">
        <button v-for="tb in TABS" :key="tb.id" class="chip" :class="{ on: tab === tb.id }"
                @click="tab = tb.id">{{ $t(tb.label) }}</button>
      </div>

      <!-- 新片 -->
      <template v-if="tab === 'new'">
        <div class="sub-bar">
          <div class="chip-list">
            <button v-for="s in STATUSES" :key="s.id" class="chip" :class="{ on: status === s.id }"
                    @click="setStatus(s.id)">{{ $t(s.label) }}</button>
          </div>
          <input v-model="q" class="inp sm sub-search" type="search"
                 :placeholder="$t('subscriptions.filter.searchPh')" @input="onSearchInput" />
          <button v-if="items.length" class="btn ghost" :class="{ on: allSelected }"
                  @click="toggleSelectAll">
            {{ allSelected ? $t('subscriptions.actions.clearSelect')
                           : $t('subscriptions.actions.selectAll') }}
          </button>
          <button class="btn ghost filter-btn" :class="{ on: showFilters || hasAdvFilters }"
                  @click="showFilters = !showFilters">
            {{ $t('subscriptions.filter.filters') }}<i v-if="hasAdvFilters" class="dot"></i>
          </button>
          <div class="chip-list sub-sort">
            <button class="chip" :class="{ on: groupByTarget }" @click="toggleGroup">
              {{ $t('subscriptions.filter.groupByTarget') }}
            </button>
            <button class="chip" :class="{ on: layout === 'compact' }" @click="setLayout('compact')">
              {{ $t('subscriptions.filter.layoutCompact') }}
            </button>
            <button class="chip" :class="{ on: layout === 'large' }" @click="setLayout('large')">
              {{ $t('subscriptions.filter.layoutLarge') }}
            </button>
          </div>
        </div>
        <div class="sub-bar second" v-if="showFilters">
          <select v-model="actress" class="inp sm sub-sel" @change="onActressFilter">
            <option value="">{{ $t('subscriptions.filter.allActresses') }}</option>
            <option v-for="a in actressOpts" :key="a.name" :value="a.name">{{ a.name }} ({{ a.count }})</option>
          </select>
          <select v-model="targetFilter" class="inp sm sub-sel" @change="onTargetFilter">
            <option value="">{{ $t('subscriptions.fields.allTargets') }}</option>
            <option v-for="tg in targets" :key="tg.id" :value="tg.id">{{ tg.name }}</option>
          </select>
          <div class="chip-list sub-sort">
            <button class="chip" :class="{ on: sortBy === 'release' }" @click="setSort('release')">
              {{ $t('subscriptions.filter.sortRelease') }}
            </button>
            <button class="chip" :class="{ on: sortBy === 'found' }" @click="setSort('found')">
              {{ $t('subscriptions.filter.sortFound') }}
            </button>
          </div>
          <button v-if="hasAdvFilters" class="btn ghost" @click="clearFilters">
            {{ $t('subscriptions.filter.reset') }}
          </button>
        </div>

        <div v-if="selectedCount" class="sub-bar sel">
          <b class="sm">{{ $t('subscriptions.actions.selectedN', { n: selectedCount }) }}</b>
          <template v-if="status === 'ignored' || status === 'archived'">
            <button class="btn tiny ghost" @click="bulkDo('restore')">
              {{ $t('subscriptions.actions.restore') }}
            </button>
          </template>
          <template v-else>
            <button v-if="status !== 'read'" class="btn tiny ghost" @click="bulkDo('read')">
              {{ $t('subscriptions.actions.bulkRead') }}
            </button>
            <button class="btn tiny ghost" @click="bulkDo('archive')">
              {{ $t('subscriptions.actions.bulkArchive') }}
            </button>
            <button class="btn tiny ghost danger" @click="bulkDo('ignore')">
              {{ $t('subscriptions.actions.bulkIgnore') }}
            </button>
          </template>
          <span class="tg-grow"></span>
          <button class="link-x" @click="selectedIds = []">
            {{ $t('subscriptions.actions.clearSelect') }}
          </button>
        </div>

        <div v-if="loading && !items.length" class="sub-hint">{{ $t('subscriptions.hints.loading') }}</div>
        <div v-else-if="error" class="sub-hint err">{{ error }}</div>
        <div v-else-if="!items.length" class="sub-empty">
          <div class="ico">🔔</div>
          <b>{{ $t('subscriptions.empty.new') }}</b>
          <p>{{ $t('subscriptions.empty.newHint') }}</p>
        </div>

        <div v-else class="sub-list" :class="layout">
          <template v-for="row in renderRows"
                    :key="row.type === 'group' ? 'g:' + row.name : row.it.id">
            <div v-if="row.type === 'group'" class="sub-group">
              <b>{{ row.name }}</b><span class="muted sm">{{ row.count }}</span>
            </div>
            <article v-else class="sub-card"
                     :class="{ ignored: row.it.status === 'ignored', unread: row.it.status === 'new' && row.it.confirmed,
                               archived: row.it.status === 'archived', picked: isSelected(row.it.id) }">
              <div class="sc-cover" :class="{ zoomable: !!coverOf(row.it) }" @click="openZoom(row.it)">
                <label class="sc-check" :class="{ on: isSelected(row.it.id) }" @click.stop>
                  <input type="checkbox" :checked="isSelected(row.it.id)"
                         @change="toggleSelect(row.it.id)" />
                </label>
                <img v-if="coverOf(row.it)" :src="coverOf(row.it)" alt="" loading="lazy" @error="$event.target.style.display='none'" />
                <span v-else class="ph">🎬</span>
                <span v-if="row.it.release_date" class="sc-date tabular">{{ row.it.release_date }}</span>
              </div>
              <div class="sc-body">
                <div class="sc-top">
                  <b class="sc-code">{{ row.it.code }}</b>
                  <button v-if="row.it.status !== 'archived'" class="tag sc-read" :class="{ on: row.it.status === 'new' }"
                          :title="row.it.status === 'new' ? $t('subscriptions.actions.markRead') : $t('subscriptions.actions.markUnread')"
                          @click.stop="toggleRead(row.it)">
                    {{ row.it.status === 'new' ? $t('subscriptions.status.unread') : $t('subscriptions.status.read') }}
                  </button>
                  <span v-if="row.it.in_library" class="tag ok">{{ $t('subscriptions.badges.inLibrary') }}</span>
                  <span v-if="row.it.watched" class="tag">{{ $t('subscriptions.badges.watched') }}</span>
                  <!-- 共演 / 合集：一条作品里出现多位已订阅女优 -->
                  <span v-if="row.it.co_star" class="tag warn" :title="(row.it.co_names || []).join('、')">
                    {{ $t('subscriptions.badges.coStar') }}
                  </span>
                  <!-- 只见过一次：仍显示，但还不算「未读」，等下一轮扫描确认 -->
                  <span v-if="row.it.status === 'new' && !row.it.confirmed" class="tag"
                        :title="$t('subscriptions.badges.pendingHint')">
                    {{ $t('subscriptions.badges.pending') }}
                  </span>
                  <span v-if="row.it.probe_status === 'ok' && row.it.probe_kind === 'watch'"
                        class="tag ok" :title="row.it.probe_site">
                    {{ $t('subscriptions.badges.probeOk') }}<template v-if="row.it.probe_site"> · {{ row.it.probe_site }}</template>
                  </span>
                  <span v-else-if="row.it.probe_status === 'ok'" class="tag" :title="row.it.probe_site">
                    {{ $t('subscriptions.badges.probeMeta') }}
                  </span>
                  <span v-else-if="row.it.probe_status === 'none'" class="tag" :title="row.it.probe_site">
                    {{ $t('subscriptions.badges.probeNone') }}
                  </span>
                  <span v-if="!row.it.visible" class="tag warn">{{ $t('subscriptions.badges.filtered') }}</span>
                </div>
                <!-- 标题即站外入口：点标题打开命中的观看站，悬停可选其它站点 -->
                <div v-if="onlineSites.length" class="sc-menu-wrap sc-title-wrap">
                  <button class="sc-title" :class="{ unread: row.it.status === 'new' && row.it.confirmed, link: true }"
                          :title="$t('subscriptions.actions.openSiteHint')" @click="openItem(row.it)">
                    {{ row.it.title }}
                  </button>
                  <div class="sc-menu sc-site-menu">
                    <template v-if="watchSites.length">
                      <div class="sc-menu-tip">{{ $t('settings.sitesKindWatch') }}</div>
                      <button v-for="s in watchSites" :key="s.id || s.name" class="mi" @click="openSite(s, row.it)">
                        {{ s.name }}
                      </button>
                    </template>
                    <template v-if="metaSites.length">
                      <div class="sc-menu-tip" :class="{ sep: watchSites.length }">{{ $t('settings.sitesKindMeta') }}</div>
                      <button v-for="s in metaSites" :key="s.id || s.name" class="mi" @click="openSite(s, row.it)">
                        {{ s.name }}
                      </button>
                    </template>
                  </div>
                </div>
                <div v-else class="sc-title" :class="{ unread: row.it.status === 'new' && row.it.confirmed }" :title="row.it.title">
                  {{ row.it.title }}
                </div>
                <div class="sc-meta">
                  <span v-if="row.it.site" class="tag site" :title="$t('subscriptions.badges.source')">{{ siteLabel(row.it.site) }}</span>
                  <span v-for="n in row.it.targets" :key="n" class="tag src">{{ n }}</span>
                  <span v-if="row.it.reasons && row.it.reasons.length" class="reason">{{ reasonsText(row.it) }}</span>
                </div>
                <div v-if="tagsOf(row.it).length" class="sc-genres">
                  <span v-for="g in tagsOf(row.it)" :key="g" class="tag g">{{ g }}</span>
                  <span v-if="row.it.meta && row.it.meta.runtime" class="tag g">{{ row.it.meta.runtime }}min</span>
                </div>
                <div v-if="layout === 'large' && actressesOf(row.it).length" class="sc-genres">
                  <span v-for="n in actressesOf(row.it).slice(0, 4)" :key="n" class="tag">{{ n }}</span>
                </div>
                <div class="sc-acts">
                  <button class="btn tiny ghost" v-if="!row.it.in_library" @click="doSave(row.it)">
                    {{ $t('subscriptions.actions.save') }}
                  </button>
                  <!-- 单条重探：批量探测跑过之后仍可针对某条立即重问 -->
                  <button class="btn tiny ghost" :disabled="isProbing(row.it.id)" @click="probeOne(row.it)">
                    {{ isProbing(row.it.id) ? $t('subscriptions.actions.probing')
                                            : $t('subscriptions.actions.reprobe') }}
                  </button>
                  <button class="btn tiny ghost" v-if="row.it.status === 'ignored' || row.it.status === 'archived'"
                          @click="doRestore(row.it)">
                    {{ $t('subscriptions.actions.restore') }}
                  </button>
                  <div v-else class="sc-menu-wrap">
                    <button class="btn tiny ghost" :class="{ on: menuFor === row.it.id }" @click="toggleMenu(row.it)">
                      {{ $t('subscriptions.actions.blockMenu') }}
                    </button>
                    <div v-if="menuFor === row.it.id" class="sc-menu">
                      <button class="mi" @click="doIgnore(row.it); menuFor = null">
                        {{ $t('subscriptions.actions.ignore') }}
                      </button>
                      <button v-for="n in actressesOf(row.it)" :key="n" class="mi" @click="blockActress(n)">
                        {{ $t('subscriptions.actions.blockActress', { name: n }) }}
                      </button>
                      <div class="mi-kw">
                        <input v-model="cardKw" class="inp sm" :placeholder="$t('subscriptions.actions.blockKeywordPh')"
                               @keydown.enter.prevent="blockKeyword(cardKw)" />
                        <button class="btn tiny" @click="blockKeyword(cardKw)">
                          {{ $t('subscriptions.actions.block') }}
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </article>
          </template>
        </div>
        <div v-if="hasMore" class="sub-more">
          <button class="btn ghost" :disabled="loading" @click="loadFindings(false)">{{ $t('subscriptions.actions.more') }}</button>
        </div>
      </template>

      <!-- 目标：左侧管理已订阅，右侧添加 -->
      <template v-else-if="tab === 'targets'">
        <details class="sub-cfg">
          <summary>
            <b>{{ $t('subscriptions.cfg.title') }}</b>
            <span class="muted sm">
              {{ !subCfg.enabled ? $t('subscriptions.actions.disabled')
                 : (subCfg.scan_mode === 'cron' ? subCfg.scan_cron
                    : $t('subscriptions.cfg.everyMinutes', { n: subCfg.scan_interval_minutes })) }}
            </span>
          </summary>
          <div class="sub-form">
            <label class="toggle"><input type="checkbox" v-model="subCfg.enabled" /><span class="track"></span></label>
            <span>{{ $t('subscriptions.cfg.enabled') }}</span>

            <label class="muted sm">{{ $t('subscriptions.cfg.mode') }}</label>
            <select class="inp sm" v-model="subCfg.scan_mode" style="max-width:130px">
              <option value="interval">{{ $t('subscriptions.cfg.modeInterval') }}</option>
              <option value="cron">{{ $t('subscriptions.cfg.modeCron') }}</option>
            </select>

            <template v-if="subCfg.scan_mode === 'interval'">
              <label class="muted sm">{{ $t('subscriptions.cfg.interval') }}</label>
              <input class="inp sm" type="number" min="30" max="10080" style="max-width:120px"
                     v-model.number="subCfg.scan_interval_minutes" />
            </template>
            <template v-else>
              <label class="muted sm">{{ $t('subscriptions.cfg.cron') }}</label>
              <input class="inp sm" style="max-width:200px" v-model.trim="subCfg.scan_cron"
                     :placeholder="$t('subscriptions.cfg.cronPh')" spellcheck="false" />
            </template>

            <label class="toggle"><input type="checkbox" v-model="subCfg.catch_up_on_start" /><span class="track"></span></label>
            <span>{{ $t('subscriptions.cfg.catchUp') }}</span>
            <label class="toggle"><input type="checkbox" v-model="subCfg.auto_probe" /><span class="track"></span></label>
            <span>{{ $t('subscriptions.cfg.autoProbe') }}</span>
            <button class="btn ghost" @click="saveCfg">{{ $t('subscriptions.actions.saveCfg') }}</button>

            <!-- 预览：表达式是否合法 + 接下来几次；间隔模式显示下次实际运行时间 -->
            <div v-if="subCfg.scan_mode === 'cron' && cronPreview" class="cfg-full"
                 :class="{ 'cfg-bad': !cronPreview.ok }">
              {{ cronPreview.ok
                 ? $t('subscriptions.cfg.cronNext', { at: (cronPreview.next || []).join(' / ') })
                 : $t('subscriptions.cfg.cronBad', { msg: cronPreview.error }) }}
            </div>
            <div v-else-if="subCfg.scan_mode === 'cron'" class="cfg-full">
              {{ $t('subscriptions.cfg.cronPh') }}
            </div>
            <div v-else-if="subCfg.enabled && schedule.next_due_at" class="cfg-full">
              {{ $t('subscriptions.cfg.nextRun', { at: schedule.next_due_at }) }}
            </div>
          </div>
        </details>

        <div class="ws">
          <!-- 已订阅 -->
          <section class="ws-col">
            <header class="ws-head">
              <b>{{ $t('subscriptions.picker.myTargets') }}</b>
              <span class="muted sm">{{ shownTargets.length }} / {{ targets.length }}</span>
            </header>
            <div class="ws-tools">
              <input v-model="tgQ" class="inp sm grow" :placeholder="$t('subscriptions.fields.searchTargets')" />
              <div class="chip-list">
                <button class="chip" :class="{ on: tgFilter === '' }" @click="tgFilter = ''">
                  {{ $t('subscriptions.status.all') }}</button>
                <button class="chip" :class="{ on: tgFilter === 'actress' }" @click="tgFilter = 'actress'">
                  {{ $t('subscriptions.kinds.actress') }}</button>
                <button class="chip" :class="{ on: tgFilter === 'series' }" @click="tgFilter = 'series'">
                  {{ $t('subscriptions.kinds.series') }}</button>
                <button class="chip" :class="{ on: tgFilter === 'off' }" @click="tgFilter = 'off'">
                  {{ $t('subscriptions.actions.disabled') }}</button>
              </div>
            </div>
            <div class="ws-scroll">
              <div v-if="!targets.length" class="ws-empty">
                <b>{{ $t('subscriptions.empty.targets') }}</b>
                <p>{{ $t('subscriptions.empty.targetsHint') }}</p>
              </div>
              <div v-else-if="!shownTargets.length" class="ws-empty">
                <p>{{ $t('subscriptions.hints.noMatch') }}</p>
              </div>
              <div v-else class="sub-targets">
                <div v-for="tg in shownTargets" :key="tg.id" class="tg-row" :class="{ off: !tg.enabled }">
                  <div class="tg-top">
                    <b class="tg-name ellipsis" :title="tg.name">{{ tg.name }}</b>
                    <span class="tag">{{ tg.kind === 'series' ? $t('subscriptions.kinds.series') : $t('subscriptions.kinds.actress') }}</span>
                    <span class="tag src" :title="tg.page_url">{{ tg.site || 'manual' }}<template v-if="tg.site_id"> · {{ tg.site_id }}</template></span>
                    <span class="tg-grow"></span>
                    <button class="btn tiny ghost" @click="scanOneTarget(tg)">{{ $t('subscriptions.actions.scan') }}</button>
                    <div class="sc-menu-wrap">
                      <button class="btn tiny ghost tg-more" :class="{ on: tgMenu === tg.id }"
                              :title="$t('subscriptions.actions.moreActions')"
                              @click.stop="toggleTgMenu(tg.id)">⋯</button>
                    </div>
                    <div v-if="tgMenu === tg.id" class="tg-actions">
                      <button class="btn tiny ghost" @click="scanOneTarget(tg, 'backfill'); tgMenu = null">
                        {{ $t('subscriptions.actions.backfill') }}
                      </button>
                      <button class="btn tiny ghost" @click="openTargetPrefs(tg); tgMenu = null">
                        {{ $t('subscriptions.actions.prefs') }}
                      </button>
                      <button class="btn tiny ghost" @click="toggleTarget(tg); tgMenu = null">
                        {{ tg.enabled ? $t('subscriptions.actions.disable') : $t('subscriptions.actions.enable') }}
                      </button>
                      <button class="btn tiny ghost danger" @click="removeTarget(tg); tgMenu = null">
                        {{ $t('subscriptions.actions.delete') }}
                      </button>
                    </div>
                  </div>
                  <div class="tg-sub muted sm">
                    <span>{{ $t('subscriptions.fields.findingCount', { n: tg.finding_count || 0 }) }}</span>
                    <span class="sep">·</span>
                    <label class="tg-base" :title="$t('subscriptions.fields.baselineHint')">
                      <span>{{ $t('subscriptions.fields.baselineAt') }}</span>
                      <input class="inp sm" type="date" :value="tg.baseline_at || ''"
                             @change="onBaselineChange(tg, $event)" />
                    </label>
                    <!-- 订阅时自动匹配到站点页面：点开标题可以核对是不是同一个人 / 同一系列 -->
                    <span v-if="tg.site_id && tg.page_url" class="tag ok"
                          :title="$t('subscriptions.fields.autoLocatedHint')">
                      {{ $t('subscriptions.fields.autoLocated') }}
                    </span>
                    <template v-if="tg.last_scan_at">
                      <span class="sep">·</span>
                      <span>{{ $t('subscriptions.fields.lastScan') }} {{ tg.last_scan_at }}</span>
                    </template>
                    <template v-if="tg.last_status">
                      <span class="sep">·</span>
                      <span class="tg-status ellipsis" :class="{ err: String(tg.last_status).startsWith('error') }"
                            :title="tg.last_status">{{ tg.last_status }}</span>
                    </template>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <!-- 添加订阅 -->
          <section class="ws-col">
            <header class="ws-head">
              <b>{{ $t('subscriptions.picker.addTitle') }}</b>
              <span class="muted sm">{{ $t('subscriptions.hints.autoSourceShort') }}</span>
            </header>
            <div class="ws-tools">
              <div class="chip-list">
                <button class="chip" :class="{ on: candKind === 'actress' }"
                        @click="onCandKind('actress')">{{ $t('subscriptions.kinds.actress') }}</button>
                <button class="chip" :class="{ on: candKind === 'series' }"
                        @click="onCandKind('series')">{{ $t('subscriptions.kinds.series') }}</button>
              </div>
              <input v-model="candQ" class="inp sm grow" :placeholder="$t('subscriptions.picker.search')"
                     @input="onCandSearch" />
            </div>
            <div class="ws-tools wrap">
              <label v-if="candKind === 'actress'" class="cand-follow">
                <input type="checkbox" v-model="followedOnly" @change="loadCandidates" />
                {{ $t('subscriptions.picker.followedOnly') }}
              </label>
              <label class="cand-follow">
                <input type="checkbox" v-model="subOnly" @change="loadCandidates" />
                {{ $t('subscriptions.picker.subscribedOnly') }}
              </label>
              <span class="tg-grow"></span>
              <button v-if="candKind === 'actress'" class="btn tiny ghost" @click="subscribeAllFollowed">
                {{ $t('subscriptions.picker.subscribeAllFollowed') }}
              </button>
            </div>
            <div class="ws-scroll">
              <div v-if="candLoading" class="ws-empty"><p>{{ $t('subscriptions.hints.loading') }}</p></div>
              <div v-else-if="!candidates.length" class="ws-empty"><p>{{ $t('subscriptions.picker.empty') }}</p></div>
              <div v-else class="cand-list">
                <div v-for="c in candidates" :key="c.kind + ':' + c.name" class="cand-row"
                     :class="{ on: c.subscribed }">
                  <img v-if="c.cover" :src="c.cover" alt="" loading="lazy" class="cand-cover"
                       @error="$event.target.style.display='none'" />
                  <span v-else class="cand-cover ph">{{ c.kind === 'series' ? '🎞️' : '👤' }}</span>
                  <div class="cand-main">
                    <b class="ellipsis">{{ c.name }}</b>
                    <span class="muted sm">{{ $t('subscriptions.picker.count', { n: c.count }) }}</span>
                    <span v-if="c.followed" class="tag ok">{{ $t('subscriptions.picker.followed') }}</span>
                  </div>
                  <button v-if="c.subscribed" class="btn tiny ghost danger" @click="unsubscribeCand(c)">
                    {{ $t('subscriptions.picker.unsubscribe') }}
                  </button>
                  <button v-else class="btn tiny" @click="subscribeCand(c)">
                    {{ $t('subscriptions.picker.subscribe') }}
                  </button>
                </div>
              </div>
            </div>
          </section>
        </div>
      </template>

      <!-- 过滤偏好 -->
      <template v-else>
        <div class="pref-head">
          <div class="pref-head-main">
            <b v-if="isTargetScope">{{ $t('subscriptions.prefs.targetTitle', { name: prefTarget ? prefTarget.name : '' }) }}</b>
            <b v-else>{{ $t('subscriptions.prefs.globalTitle') }}</b>
            <span class="muted sm">{{ isTargetScope ? $t('subscriptions.prefs.targetInherit') : $t('subscriptions.prefs.globalHint') }}</span>
          </div>
          <div class="pref-head-acts">
            <span class="saved-tip" :class="{ show: savedAt }">{{ $t('subscriptions.prefs.saved') }}</span>
            <button v-if="isTargetScope" class="btn tiny ghost" @click="backToGlobal">
              {{ $t('subscriptions.prefs.backGlobal') }}
            </button>
            <button v-if="isTargetScope && targetRules.length" class="btn tiny ghost" @click="resetToGlobal">
              {{ $t('subscriptions.prefs.followGlobal') }}
            </button>
          </div>
        </div>

        <div v-if="preview" class="pref-summary">
          <b>{{ preview.visible }}</b> {{ $t('subscriptions.prefs.summaryVisible') }}
          <span class="muted">· {{ $t('subscriptions.prefs.summaryFiltered', { n: preview.filtered }) }}</span>
        </div>

        <!-- 被挡明细：哪几条没提醒、被哪条规则挡住；只列前 N 条 -->
        <details v-if="preview && preview.blocked && preview.blocked.length" class="pref-blocked">
          <summary class="muted sm">
            {{ $t('subscriptions.prefs.blockedTitle', { n: preview.blocked.length }) }}
          </summary>
          <div v-for="b in preview.blocked" :key="b.id" class="blocked-row">
            <b class="sc-code">{{ b.code }}</b>
            <span v-if="b.release_date" class="muted sm tabular">{{ b.release_date }}</span>
            <span class="muted sm ellipsis" :title="b.title">{{ b.title }}</span>
            <span class="reason">{{ blockedText(b) }}</span>
          </div>
        </details>

        <!-- 一眼看清当前生效的过滤条件 -->
        <div class="pref-active">
          <span class="pref-active-title">{{ $t('subscriptions.prefs.activeTitle') }}</span>
          <template v-if="prefActiveCount">
            <span v-if="prefs.watched" class="tag ok">{{ $t('subscriptions.prefs.watched') }}</span>
            <span v-if="prefs.ignored" class="tag ok">{{ $t('subscriptions.prefs.ignored') }}</span>
            <span v-if="Number(prefs.recent_days) > 0" class="tag ok">
              {{ $t('subscriptions.prefs.recentDays') }} {{ prefs.recent_days }}
            </span>
            <span v-if="prefs.keywords.length" class="tag ok">
              {{ $t('subscriptions.prefs.keywords') }} ×{{ prefs.keywords.length }}
            </span>
            <span v-if="prefs.actresses.length" class="tag ok">
              {{ $t('subscriptions.prefs.actresses') }} ×{{ prefs.actresses.length }}
            </span>
            <span v-if="advancedRules.length" class="tag ok">
              {{ $t('subscriptions.prefs.advanced') }} ×{{ advancedRules.length }}
            </span>
          </template>
          <span v-else class="muted sm">{{ $t('subscriptions.prefs.activeNone') }}</span>
        </div>

        <div class="pref-panel">
          <div class="pref-row">
            <div class="pref-main">
              <b>{{ $t('subscriptions.prefs.watched') }}</b>
              <span class="muted sm">{{ $t('subscriptions.prefs.watchedHint') }}</span>
            </div>
            <label class="toggle">
              <input type="checkbox" :checked="prefs.watched" @change="setBoolPref('watched', 'exclude_watched')" />
              <span class="track"></span>
            </label>
          </div>

          <div class="pref-row">
            <div class="pref-main">
              <b>{{ $t('subscriptions.prefs.ignored') }}</b>
              <span class="muted sm">{{ $t('subscriptions.prefs.ignoredHint') }}</span>
            </div>
            <label class="toggle">
              <input type="checkbox" :checked="prefs.ignored" @change="setBoolPref('ignored', 'exclude_ignored')" />
              <span class="track"></span>
            </label>
          </div>

          <div class="pref-row">
            <div class="pref-main">
              <b>{{ $t('subscriptions.prefs.recentDays') }}</b>
              <span class="muted sm">{{ $t('subscriptions.prefs.recentDaysHint') }}</span>
            </div>
            <div class="pref-days">
              <input v-model="prefs.recent_days" class="inp sm num" type="number" min="0" step="1"
                     :placeholder="$t('subscriptions.prefs.recentDaysPh')" @change="saveRecentDays" />
              <span class="muted sm">{{ $t('subscriptions.prefs.recentDaysUnit') }}</span>
            </div>
          </div>

          <div class="pref-block">
            <div class="pref-main">
              <b>{{ $t('subscriptions.prefs.keywords') }}</b>
              <span class="muted sm">{{ $t('subscriptions.prefs.keywordsHint') }}</span>
            </div>
            <div class="tag-box">
              <span v-for="k in prefs.keywords" :key="k" class="tag chip-tag">
                {{ k }}<button class="tag-x" @click="removeTag('keywords', 'keyword', k)">×</button>
              </span>
              <input v-model="kwInput" class="tag-inp" :placeholder="$t('subscriptions.prefs.keywordsPh')"
                     @keydown.enter.prevent="addTag('keywords', 'keyword', kwInput); kwInput = ''" />
            </div>
            <div class="pref-presets">
              <span class="muted sm">{{ $t('subscriptions.prefs.presets') }}</span>
              <button v-for="p in KW_PRESETS" :key="p" class="chip"
                      :class="{ on: prefs.keywords.includes(p) }" @click="togglePreset(p)">{{ p }}</button>
            </div>
          </div>

          <div class="pref-block">
            <div class="pref-main">
              <b>{{ $t('subscriptions.prefs.actresses') }}</b>
              <span class="muted sm">{{ $t('subscriptions.prefs.actressesHint') }}</span>
            </div>
            <div class="tag-box">
              <span v-for="n in prefs.actresses" :key="n" class="tag chip-tag">
                {{ n }}<button class="tag-x" @click="removeTag('actresses', 'exclude_actress', n)">×</button>
              </span>
              <input v-model="actressInput" class="tag-inp" :placeholder="$t('subscriptions.prefs.actressesPh')"
                     @keydown.enter.prevent="addTag('actresses', 'exclude_actress', actressInput); actressInput = ''" />
            </div>
          </div>

          <div class="pref-note muted sm">{{ $t('subscriptions.prefs.inLibrary') }}</div>
        </div>

        <div class="pref-adv-head">
          <button class="btn tiny ghost" @click="showAdv = !showAdv">
            {{ showAdv ? '▾' : '▸' }} {{ $t('subscriptions.prefs.advanced') }}
          </button>
          <span class="muted sm">{{ $t('subscriptions.prefs.advancedHint') }}</span>
        </div>

        <template v-if="showAdv">
          <div class="sub-form">
            <select v-model="advForm.rule_type" class="inp sm">
              <option v-for="rt in ADVANCED_TYPES" :key="rt" :value="rt">{{ $t('subscriptions.ruleTypes.' + rt) }}</option>
            </select>
            <input v-if="advForm.rule_type !== 'exclude_co_star'" v-model="advForm.value"
                   class="inp sm grow" :placeholder="$t('subscriptions.fields.ruleValue')" />
            <button class="btn" @click="addRule">{{ $t('subscriptions.actions.add') }}</button>
          </div>
          <p class="sub-tip muted sm">{{ $t('subscriptions.hints.ruleValue') }}</p>

          <div v-if="!advancedRules.length" class="sub-tip muted sm">{{ $t('subscriptions.prefs.advancedEmpty') }}</div>
          <div v-else class="sub-rules">
            <div v-for="r in advancedRules" :key="r.id" class="rule-row" :class="{ off: !r.enabled }">
              <span class="tag" :class="r.op === 'include' ? 'ok' : 'warn'">{{ r.op }}</span>
              <b class="rule-type">{{ $t('subscriptions.ruleTypes.' + r.rule_type) }}</b>
              <span v-if="r.rule_type !== 'exclude_co_star'" class="rule-val ellipsis"
                    :title="r.value">{{ r.value.replace(/\n/g, ' / ') }}</span>
              <span class="muted sm">{{ r.target_id ? $t('subscriptions.hints.targetScope') : $t('subscriptions.hints.globalScope') }}</span>
              <button class="btn tiny ghost" @click="toggleRule(r)">
                {{ r.enabled ? $t('subscriptions.actions.disable') : $t('subscriptions.actions.enable') }}
              </button>
              <button class="btn tiny danger" @click="removeRule(r)">{{ $t('subscriptions.actions.delete') }}</button>
            </div>
          </div>
        </template>
      </template>
    </div>

    <!-- 放大看封面：点图 / 点遮罩 / Esc 关闭 -->
    <div v-if="zoom" class="zoom-mask" @click="zoom = ''">
      <img :src="zoom" alt="" @click.stop />
    </div>
  </section>
</template>

<style scoped>
.sub-view { padding-bottom: 28px; }

/* 本页自带的输入框与标签：全局没有通用 .inp / .tag，避免跨组件依赖 */
.inp {
  height: 30px; padding: 0 var(--sp-2);
  background: var(--c-surface-2); border: 1px solid var(--c-line);
  border-radius: var(--r-sm); color: var(--c-text); font-size: var(--fs-sm);
}
.inp:focus { outline: none; border-color: var(--c-primary); }
.tag {
  display: inline-flex; align-items: center; height: 20px; padding: 0 6px;
  border: 1px solid var(--c-line); border-radius: 4px;
  font-size: var(--fs-xs); color: var(--c-text-3); white-space: nowrap;
}
.tag.ok { color: var(--c-ok); background: var(--c-ok-soft); border-color: transparent; }
.tag.warn { color: var(--c-warn); background: var(--c-warn-soft); border-color: transparent; }
.tag.src { color: var(--c-primary); }
.toggle {
  position: relative; display: inline-flex; width: 34px; height: 18px;
  flex: none; cursor: pointer;
}
.toggle input { position: absolute; opacity: 0; }
.toggle .track {
  width: 100%; height: 100%; background: var(--c-line);
  border-radius: 9px; transition: background .15s;
}
.toggle .track::after {
  content: ''; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px;
  background: #fff; border-radius: 50%; transition: transform .15s;
}
.toggle input:checked + .track { background: var(--c-primary); }
.toggle input:checked + .track::after { transform: translateX(16px); }
.sub-tabs { padding: var(--sp-2) var(--sp-4) 0; }
/* 新片工具条：状态 + 搜索 + 筛选 + 布局；高级条件下沉到第二行 */
.sub-bar {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-4); flex-wrap: wrap;
}
.sub-bar.second { padding-top: 0; }
/* 批量操作行：只在勾选了条目后出现，避免常驻占位 */
.sub-bar.sel {
  padding-top: 0; padding-bottom: var(--sp-1);
  background: color-mix(in srgb, var(--c-primary) 8%, transparent);
  border-radius: var(--r-sm);
}
.sub-search { width: 200px; flex: 1 1 200px; max-width: 320px; }
.sub-sel { width: auto; max-width: 132px; flex: 0 0 auto; }
.sub-sort { flex-wrap: nowrap; }
.filter-btn { position: relative; }
.filter-btn.on { color: var(--c-primary); border-color: var(--c-primary); }
.filter-btn .dot {
  position: absolute; top: 2px; right: 2px; width: 6px; height: 6px;
  border-radius: 50%; background: var(--c-primary);
}
.sub-unread {
  font-size: var(--fs-sm); color: var(--c-primary); font-variant-numeric: tabular-nums;
}
.link-x {
  border: 0; background: none; color: var(--c-text-3); font-size: var(--fs-sm);
  cursor: pointer; padding: 0 2px; flex: 0 0 auto;
}
.link-x:hover { color: var(--c-danger); }
.sub-scan { padding: var(--sp-2) var(--sp-4); }
.sub-scan-line { display: flex; align-items: center; gap: var(--sp-2); }
.sub-scan .bar {
  flex: 1 1 auto; height: 4px; background: var(--c-line);
  border-radius: var(--r-sm); overflow: hidden; min-width: 80px;
}
.sub-scan .bar i { display: block; height: 100%; background: var(--c-primary); transition: width .3s; }
.sub-scan .ellipsis { max-width: 46%; }

.sub-hint { padding: var(--sp-5) var(--sp-4); color: var(--c-text-3); font-size: var(--fs-sm); }
.sub-hint.err { color: var(--c-danger); }
.sub-empty {
  margin: var(--sp-6) auto; max-width: 420px; text-align: center;
  color: var(--c-text-2); display: flex; flex-direction: column; gap: 6px; align-items: center;
}
.sub-empty .ico { font-size: 30px; opacity: .6; }
.sub-empty p { margin: 0; font-size: var(--fs-sm); color: var(--c-text-3); }

.sub-list { display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-2) var(--sp-4); }
/* 分组头：按订阅目标分组时，一条细横线把每组分开 */
.sub-group {
  display: flex; align-items: baseline; gap: var(--sp-2);
  padding: var(--sp-2) 2px 0; color: var(--c-text-2);
  border-bottom: 1px solid var(--c-line); margin-bottom: 2px;
}
.sub-list.large .sub-group { grid-column: 1 / -1; }
.sub-card {
  display: flex; gap: var(--sp-3);
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--r-md); padding: var(--sp-2);
}
.sub-card.ignored { opacity: .6; }
/* 归档：老片收进来，不抢未读的注意力 */
.sub-card.archived { opacity: .72; }
/* 勾选中：整张卡片描边，扫一眼知道批量会作用在哪些条目上 */
.sub-card.picked { border-color: var(--c-primary); box-shadow: inset 0 0 0 1px var(--c-primary); }
/* 未读：左侧强调条 + 标题加粗，扫一眼就知道哪条还没处理 */
.sub-card.unread { border-left: 3px solid var(--c-primary); }
.sub-card.unread .sc-title.unread { color: var(--c-text); font-weight: 600; }
.sc-read {
  height: 18px; font-size: 11px; padding: 0 6px; cursor: pointer;
  border: 0; background: var(--c-surface-2); color: var(--c-text-3);
}
.sc-read.on {
  color: var(--c-primary);
  background: color-mix(in srgb, var(--c-primary) 14%, transparent);
}
/* 标题即站外入口：整行可点，悬停展开站点菜单 */
.sc-menu-wrap.sc-title-wrap { display: block; }
.sc-title-wrap .sc-title {
  display: block; width: 100%; text-align: left; padding: 0; border: 0; background: none;
  cursor: pointer; color: var(--c-text-2); font-size: var(--fs-sm);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sc-title-wrap .sc-title:hover { color: var(--c-primary); text-decoration: underline; }
/* 大卡片：网格排布，封面放大，信息更全 */
.sub-list.large { display: grid; grid-template-columns: repeat(auto-fill, minmax(248px, 1fr)); align-items: start; }
.sub-list.large .sub-card { flex-direction: column; gap: var(--sp-2); }
.sub-list.large .sc-cover { width: 100%; }
.sub-list.large .sc-title-wrap .sc-title,
.sub-list.large .sc-title {
  white-space: normal; display: -webkit-box; -webkit-box-orient: vertical;
  -webkit-line-clamp: 2; font-size: var(--fs-md); line-height: 1.35; min-height: 2.7em;
}
.sc-cover {
  position: relative; width: 96px; aspect-ratio: 2 / 3; flex: none;
  background: var(--c-surface-2); border-radius: var(--r-sm); overflow: hidden; cursor: pointer;
}
.sc-cover img { width: 100%; height: 100%; object-fit: cover; display: block; }
/* 批量勾选框：盖在封面左上角，hover 才明显出现，不挡封面浏览 */
.sc-check {
  position: absolute; top: 4px; left: 4px; z-index: 2;
  width: 20px; height: 20px; display: flex; align-items: center; justify-content: center;
  border-radius: var(--r-sm); background: rgba(0, 0, 0, .5); cursor: pointer;
  opacity: .35; transition: opacity .15s;
}
.sc-check:hover, .sc-check.on { opacity: 1; background: rgba(0, 0, 0, .72); }
.sc-check input { margin: 0; cursor: pointer; accent-color: var(--c-primary); }
.sc-cover.zoomable { cursor: zoom-in; }
.sc-cover.zoomable:hover img { filter: brightness(1.06); }
.sc-cover .ph { display: flex; width: 100%; height: 100%; align-items: center; justify-content: center; opacity: .4; }
.sc-date {
  position: absolute; left: 0; bottom: 0; width: 100%;
  font-size: 11px; padding: 1px 4px; color: #fff; background: rgba(0,0,0,.55);
}
.sc-body { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 5px; }
.sc-top { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.sc-code { font-size: var(--fs-md); letter-spacing: .02em; }
.sc-title { color: var(--c-text-2); font-size: var(--fs-sm); }
.sc-meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.sc-meta .reason { color: var(--c-warn); font-size: var(--fs-xs, 12px); }
/* 类型标签：探测时顺带抓的，字号压小一点，别抢标题的注意力 */
.sc-genres { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.sc-genres .tag { height: 18px; font-size: 11px; color: var(--c-text-4, var(--c-text-3)); }
/* 封面灯箱 */
.zoom-mask {
  position: fixed; inset: 0; z-index: 400; padding: 4vh 4vw;
  background: rgba(0, 0, 0, .84);
  display: flex; align-items: center; justify-content: center; cursor: zoom-out;
}
.zoom-mask img {
  max-width: 92vw; max-height: 92vh; object-fit: contain; cursor: default;
  border-radius: var(--r-md, 8px); box-shadow: 0 20px 60px rgba(0, 0, 0, .6);
}
.sc-acts { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 2px; }
.sc-res {
  margin: 4px 0 0; padding: 6px 10px; list-style: none;
  background: var(--c-surface-2); border-radius: var(--r-sm); font-size: var(--fs-sm);
  display: flex; flex-direction: column; gap: 4px;
}
.sc-res li { display: flex; gap: 8px; align-items: center; }
.sc-res a { color: var(--c-primary); text-decoration: none; }
.sub-more { display: flex; justify-content: center; padding: var(--sp-3); }

.sub-form {
  display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap;
  padding: var(--sp-2) var(--sp-4);
}
.sub-form .grow { flex: 1; min-width: 180px; }
/* 设置项下方的说明行：占满整行，错误用红色 */
.sub-form .cfg-full { flex-basis: 100%; font-size: var(--fs-sm); color: var(--c-text-3); }
.sub-form .cfg-full.cfg-bad { color: var(--c-err); }
.sub-tip { padding: 0 var(--sp-4); margin: 0 0 var(--sp-2); }

/* 过滤偏好面板 */
.pref-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-3); flex-wrap: wrap;
  padding: var(--sp-3) var(--sp-4) var(--sp-2);
}
.pref-head-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.pref-head-acts { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.saved-tip {
  font-size: var(--fs-xs); color: var(--c-ok);
  opacity: 0; transition: opacity .25s;
}
.saved-tip.show { opacity: 1; }
.pref-summary {
  margin: 0 var(--sp-4) var(--sp-2); padding: 8px 12px;
  background: var(--c-surface-2); border: 1px solid var(--c-line);
  border-radius: var(--r-sm); font-size: var(--fs-sm);
}
.pref-summary b { color: var(--c-ok); }
/* 被挡明细：折叠列表，一行一条「番号 日期 标题 → 原因」 */
.pref-blocked {
  margin: 0 var(--sp-4) var(--sp-2); padding: 6px 10px;
  background: var(--c-surface-2); border: 1px solid var(--c-line);
  border-radius: var(--r-sm);
}
.pref-blocked summary { cursor: pointer; }
.blocked-row {
  display: flex; align-items: baseline; gap: 8px;
  padding: 4px 0; border-top: 1px dashed var(--c-line);
}
.blocked-row .reason { color: var(--c-warn); font-size: var(--fs-xs, 12px); flex: none; }
.blocked-row .ellipsis { flex: 1; min-width: 0; }

/* 卡片「不想看」菜单 */
.sc-menu-wrap { position: relative; display: inline-flex; }
.sc-menu {
  position: absolute; right: 0; bottom: calc(100% + 6px); z-index: 30;
  min-width: 210px; padding: 6px;
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--r-md); box-shadow: 0 10px 30px rgba(0, 0, 0, .28);
  display: flex; flex-direction: column; gap: 2px;
}
.mi {
  text-align: left; border: 0; background: transparent; color: var(--c-text);
  padding: 7px 8px; border-radius: var(--r-sm); cursor: pointer; font-size: var(--fs-sm);
}
.mi:hover { background: var(--c-surface-2); }
/* 站外菜单的分组标题（在线观看 / 元数据查询） */
.sc-menu-tip {
  font-size: 11px; color: var(--c-text-3); padding: 4px 8px 2px;
}
.sc-menu-tip.sep { margin-top: 4px; border-top: 1px solid var(--c-line); padding-top: 7px; }
.mi-kw {
  display: flex; gap: 6px; padding: 6px 4px 2px;
  margin-top: 4px; border-top: 1px solid var(--c-line);
}
.mi-kw .inp { flex: 1; min-width: 0; }
/* 站点菜单：站点名短，不需要宽菜单；鼠标悬停标题展开、移开隐藏 */
.sc-site-menu { min-width: 132px; display: none; }
.sc-menu-wrap:hover .sc-site-menu { display: flex; }
/* 标题在卡片顶部，菜单向下展开，避免第一排卡片被顶出屏幕 */
.sc-title-wrap .sc-site-menu {
  top: calc(100% + 6px); right: auto; bottom: auto; left: 0;
}
.sc-title-wrap .sc-site-menu::before { top: auto; bottom: 100%; }
/* 菜单与按钮之间有 6px 空隙，用透明桥接，鼠标横穿过去不会闪没 */
.sc-site-menu::before {
  content: ''; position: absolute; left: 0; right: 0; top: 100%; height: 8px;
}

.pref-panel {
  display: flex; flex-direction: column; gap: 0;
  margin: var(--sp-2) var(--sp-4);
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--r-md); overflow: hidden;
}
.pref-row {
  display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-3);
  border-bottom: 1px solid var(--c-line);
}
.pref-block {
  display: flex; flex-direction: column; gap: 8px;
  padding: var(--sp-3) var(--sp-3);
  border-bottom: 1px solid var(--c-line);
}
.pref-main { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.pref-main b { font-size: var(--fs-sm); }
.pref-days { display: inline-flex; align-items: center; gap: 6px; flex: none; }
.pref-days .num { width: 78px; text-align: center; }
.tag-box {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  min-height: 34px; padding: 4px 6px;
  background: var(--c-surface-2); border: 1px solid var(--c-line);
  border-radius: var(--r-sm);
}
.chip-tag { gap: 2px; padding-right: 2px; }
.tag-x {
  border: 0; background: transparent; color: var(--c-text-3);
  cursor: pointer; font-size: 13px; line-height: 1; padding: 0 3px;
}
.tag-x:hover { color: var(--c-danger); }
.tag-inp {
  flex: 1; min-width: 120px; height: 24px; border: 0; background: transparent;
  color: var(--c-text); font-size: var(--fs-sm); outline: none;
}
.pref-presets { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.pref-note { padding: var(--sp-3) var(--sp-3); }
.pref-adv-head {
  display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap;
  padding: var(--sp-2) var(--sp-4) 0;
}

/* 从库中添加：候选行 */
.cand-follow { display: inline-flex; align-items: center; gap: 4px; font-size: var(--fs-sm); color: var(--c-text-2); }
.cand-list {
  display: flex; flex-direction: column; gap: 4px;
  padding: var(--sp-2) var(--sp-4); max-height: 320px; overflow-y: auto;
}
.cand-row {
  display: flex; align-items: center; gap: var(--sp-2);
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--r-sm); padding: 5px var(--sp-2);
}
.cand-row.on { opacity: .55; }
.cand-cover {
  width: 34px; height: 34px; flex: none; border-radius: 3px;
  object-fit: cover; background: var(--c-surface-2);
}
.cand-cover.ph {
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 16px; opacity: .5;
}
.cand-main { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; }

.sub-targets, .sub-rules { display: flex; flex-direction: column; gap: 6px; }
.sub-rules { padding: var(--sp-2) var(--sp-4); }
.tg-row, .rule-row {
  display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap;
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--r-md); padding: var(--sp-2) var(--sp-3);
}
.tg-row.off, .rule-row.off { opacity: .55; }
/* 目标行：两行结构（名称/类型/来源 + 计数/起始日期/扫描状态），操作只在右侧 */
.tg-row { flex-direction: column; align-items: stretch; gap: 4px; }
.tg-top { display: flex; align-items: center; gap: 6px; min-width: 0; }
.tg-name { font-size: var(--fs-md); min-width: 0; }
.tg-grow { flex: 1 1 auto; }
.tg-sub { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; min-height: 22px; }
.tg-sub .sep { opacity: .45; }
.tg-status { max-width: 260px; }
.tg-status.err { color: var(--c-danger); }
.tg-more { font-size: 14px; line-height: 1; padding: 3px 7px; }
.tg-base { display: inline-flex; align-items: center; gap: 4px; }
.tg-base .inp { width: 132px; height: 22px; padding: 0 4px; font-size: var(--fs-sm); }
/* 目标页：左右分栏工作台 */
.ws {
  display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(320px, .95fr);
  gap: var(--sp-3); padding: 0 var(--sp-4) var(--sp-3);
  align-items: start;
}
.ws-col {
  display: flex; flex-direction: column; min-width: 0;
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--r-md); overflow: hidden;
}
.ws-head {
  display: flex; align-items: baseline; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--c-line); background: var(--c-surface-2);
}
.ws-tools {
  display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap;
  padding: var(--sp-2) var(--sp-3); border-bottom: 1px solid var(--c-line);
}
.ws-tools.wrap { flex-wrap: wrap; border-bottom: 0; }
.ws-tools .grow { flex: 1 1 auto; min-width: 120px; }
.ws-scroll { padding: var(--sp-2) var(--sp-3); overflow-y: auto; max-height: calc(100vh - 300px); }
.ws-empty { padding: var(--sp-4) 0; text-align: center; color: var(--c-text-3); font-size: var(--fs-sm); }
.ws-empty p { margin: 4px 0 0; }
/* 折叠的扫描设置：默认收起，不抢视线 */
.sub-cfg { margin: 0 var(--sp-4) var(--sp-2); }
.sub-cfg > summary {
  display: flex; align-items: baseline; gap: var(--sp-2); cursor: pointer;
  padding: var(--sp-2) var(--sp-3); font-size: var(--fs-sm);
  background: var(--c-surface-2); border-radius: var(--r-sm); list-style: none;
}
.sub-cfg > summary::-webkit-details-marker { display: none; }
.sub-cfg[open] > summary { border-radius: var(--r-sm) var(--r-sm) 0 0; }
.sub-cfg .sub-form { border: 1px solid var(--c-line); border-top: 0; border-radius: 0 0 var(--r-sm) var(--r-sm); }
/* 偏好页「当前生效」摘要 */
.pref-active {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  margin: 0 var(--sp-4) var(--sp-2);
}
.pref-active-title { font-size: var(--fs-sm); color: var(--c-text-3); }
/* 目标行内联操作区：不用绝对定位下拉，避免被滚动容器裁掉 */
.tg-actions {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding-top: 6px; border-top: 1px dashed var(--c-line);
}
@media (max-width: 1100px) {
  .ws { grid-template-columns: 1fr; }
  .ws-scroll { max-height: none; }
}
.rule-type { font-size: var(--fs-sm); }
.rule-val { color: var(--c-text-2); font-size: var(--fs-sm); max-width: 320px; }
.sub-preview {
  margin: 0 var(--sp-4) var(--sp-2); padding: var(--sp-2) var(--sp-3);
  background: var(--c-surface-2); border-radius: var(--r-sm); font-size: var(--fs-sm);
}
</style>
