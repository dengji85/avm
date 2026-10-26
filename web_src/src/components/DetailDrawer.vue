<script setup>
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { state } from '../state.js'
import {
  getMovie, updateMovie, deleteMovie, toggleFlag, playMovie,
  exportNfo, getPreviews, getSimilar, coverUrl, coverThumbUrl, uploadCover, clearCover,
  extractCover,
  listTags, renameTag, deleteTag, aiPromoteTags, cleanupTags,
  dlnaDevices, dlnaAddDevice, dlnaCast, dlnaStop,
  getActress, avatarUrl, getConfig, scanMovieSubtitles, extStatus,
} from '../api.js'
import AddToCollectionBtn from './AddToCollectionBtn.vue'
import {
  toast, confirmDialog, copyText, coverFallback, avatarFallback, fmtSize, fmtDuration,
  fmtDate, fmtAgo, qualityTag,
} from '../utils.js'
import { t } from '../i18n/index.js'
import { useTasks } from '../composables/useTasks.js'
import { aiWorkspace as ws } from '../composables/aiWorkspace.js'

const { runScrape } = useTasks()
import VideoPlayer from './VideoPlayer.vue'
import ActressModal from './ActressModal.vue'

const mv = ref(null)
const loading = ref(false)
const tab = ref('preview')
const playing = ref(false)
const previews = ref([])
const pvLoading = ref(false)
const similar = ref([])
const pendingAutoplay = ref(false)
const hasPlayable = computed(() => !!(mv.value && mv.value.files && mv.value.files.some(f => !f.missing)))
// 容器 / NAS 无桌面环境时，这两个入口无效，按运行能力隐藏
const canExternal = computed(() => state.capabilities?.external_player !== false)
const canReveal = computed(() => state.capabilities?.reveal_folder !== false)
const editing = ref(false)
const draft = ref({})
/* 放大查看：支持整组预览图左右翻页（← → / 按钮 / 底部缩略图） */
const lightboxList = ref([])
const lightboxIndex = ref(0)
const lightboxSrc = computed(() => lightboxList.value[lightboxIndex.value] || '')
const lightboxNav = computed(() => lightboxList.value.length > 1)
function openLightbox(urls, index = 0) {
  const list = (urls || []).filter(Boolean)
  if (!list.length) return
  lightboxList.value = list
  lightboxIndex.value = Math.min(Math.max(index, 0), list.length - 1)
}
function closeLightbox() {
  lightboxList.value = []
  lightboxIndex.value = 0
}
function stepLightbox(delta) {
  const n = lightboxList.value.length
  if (n < 2) return
  lightboxIndex.value = (lightboxIndex.value + delta + n) % n
}
/* ---------- AI 助手：状态在全局浮窗（composables/aiWorkspace）里，这里只留入口 ---------- */
const ai = ws.ai
const manualSubs = computed(() => (mv.value?.subtitles || []).filter((s) => s.source !== 'ai-translate'))
const aiSubs = computed(() => (mv.value?.subtitles || []).filter((s) => s.source === 'ai-translate'))

/** 打开全局 AI 浮窗：把详情页已加载的影片对象一并交过去，省一次请求 */
function openAi() {
  ws.openWorkspace(mv.value)
}
/** 简介旁的「AI 重写」：打开对话面板并直接跑简介，少点一次 */
function openAiWithPlot() {
  ws.openWorkspace(mv.value, { skill: 'plot' })
}
/** 标签旁的「AI 建议」 */
function openAiWithTags() {
  ws.openWorkspace(mv.value, { skill: 'tags' })
}
/**
 * 详情页的轻量 AI：写简介 / 问剧情都直接在页面上完成。
 * 写简介没有字幕也能按元数据兜底；问剧情要读字幕时间点，必须先把字幕补上。
 */
const canQuickAi = computed(() => ai.ready && ai.hasSubtitles)
/** 元数据有多厚：决定「按元数据生成」能给出多少东西 */
const metaRich = computed(() => !!(
  (mv.value?.actresses && mv.value.actresses.length)
  || (mv.value?.series || '')
  || (mv.value?.studio || '')
  || (mv.value?.genres && mv.value.genres.length)
  || (mv.value?.tags && mv.value.tags.length)))
/** 语音提取的时长上限：0 = 全片；只取前 N 分钟能快 4~8 倍，梗概/问答通常够用 */
const asrLimit = ref('0')
/** 需要深功能（提示词、调用日志、对白提取）时去 AI 中心 */
function openAiCenter() {
  close()
  state.view = 'ai'
}

/** 直达 设置 → 扩展组件（装语音组件 / 下模型） */
function openExt() {
  close()
  state.view = 'settings'
  state.settingsTab = 'ext'
}

/**
 * 提取 / 重新提取对白：入口任何状态下都可点。
 * 未就绪 → 带去扩展组件页；已提取过 → 确认覆盖后再提取。
 */
async function onExtractDialog() {
  if (ai.dialog.status === 'running') return
  if (!ai.asr.lib_available || !ai.asr.model_available) {
    toast(t('ai.dialogGoExt'), 'err')
    openExt()
    return
  }
  if (ai.dialog.has_text) {
    const ok = await confirmDialog(t('ai.reExtractConfirm'), t('ai.reExtractHint'), { danger: false })
    if (!ok) return
  }
  ai.startDialog(asrLimit)
}

/* 扫描同目录外挂字幕：放好 srt/ass 后点一下即可登记 */
const scanningSubs = ref(false)
async function scanSidecars() {
  if (scanningSubs.value || !id.value) return
  scanningSubs.value = true
  try {
    const r = await scanMovieSubtitles(id.value)
    await refreshMovie()
    if (r.added) toast(t('detail.subsFound', { n: r.added }), 'ok')
    else toast(t('detail.subsNone'), '')
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    scanningSubs.value = false
  }
}
/** AI 操作后的轻量刷新：只重取影片数据，不重置当前 tab 与播放状态 */
async function refreshMovie() {
  if (!state.currentId) return
  try {
    const m = await getMovie(state.currentId)
    if (!m) return
    let tg = m.tags
    if (typeof tg === 'string') tg = tg ? tg.split(',').map((s) => s.trim()).filter(Boolean) : []
    else if (!Array.isArray(tg)) tg = []
    m.tags = tg
    mv.value = m
    ws.syncMovie(m)
  } catch (e) { /* 静默：AI 卡片内已有错误提示 */ }
}

/**
 * AI 问答里点时间戳：打开内嵌播放器并跳到那一秒。
 * 浮窗是全局的，所以由它写 state.aiSeek，这里负责真正去播。
 */
watch(() => state.aiSeek, (req) => {
  if (!req || req.id !== id.value) return
  if (!playing.value) {
    playing.value = true
    // 播放器是 v-if 挂载的，等它初始化完再发跳转请求
    setTimeout(() => window.dispatchEvent(new CustomEvent('avm-seek', { detail: { seconds: req.seconds } })), 700)
    return
  }
  window.dispatchEvent(new CustomEvent('avm-seek', { detail: { seconds: req.seconds } }))
})

/* 常用在线站点：前 3 个平铺，其余收进「更多」——站点行永远只占一行 */
const sites = ref([])
const topSites = computed(() => sites.value.slice(0, 3))
const moreSites = computed(() => sites.value.slice(3))
const sitesMoreOpen = ref(false)

/* ffmpeg 是否已配置：配置了就不再显示「需要 ffmpeg」的提示 */
const extSt = reactive({ ffmpeg: false })
async function loadExt() {
  try {
    const r = await extStatus()
    const ff = (r.components || []).find((c) => c.id === 'ffmpeg')
    extSt.ffmpeg = !!ff && ff.state === 'ready'
  } catch { extSt.ffmpeg = false }
}
async function loadSites() {
  try {
    const cfg = await getConfig()
    const list = (cfg && cfg.online_sites) || []
    sites.value = list.filter((s) => s.enabled && s.url && s.url.includes('{code}'))
  } catch { sites.value = [] }
}
function renderSiteUrl(site, code) {
  // 同时支持 {code}（番号）与 {title}（标题）占位
  const raw = (mv.value && mv.value.title) || code || ''
  const codeVal = site.lower ? String(code || '').toLowerCase() : (code || '')
  const titleVal = site.lower ? String(raw).toLowerCase() : raw
  return String(site.url)
    .replace(/\{code\}/g, encodeURIComponent(codeVal))
    .replace(/\{title\}/g, encodeURIComponent(titleVal))
}
function openSite(site) {
  const code = (mv.value && mv.value.code) || ''
  const final = renderSiteUrl(site, code)
  if (final) {
    window.open(final, '_blank', 'noopener')
    sitesMoreOpen.value = false
  }
}

/* ---------- DLNA 投屏 ---------- */
const castOpen = ref(false)
const castBusy = ref(false)
const castDevices = ref([])
const castIp = ref('')
const castTo = ref('')
/* 记住本次投屏设备的 usn：设备列表刷新/关闭后仍能准确停止 */
const castUsn = ref('')

async function scanCast() {
  if (castBusy.value) return
  castBusy.value = true
  try {
    const r = await dlnaDevices()
    castDevices.value = r.items || []
  } catch (e) { toast(e.message, 'err') }
  finally { castBusy.value = false }
}
function openCast() {
  castOpen.value = true
  castDevices.value = []
  scanCast()
}
/** 设备副标题：优先显示 IP（从 description 地址里取），再带上厂商型号 */
function castSub(d) {
  const loc = (d && d.location) ? String(d.location) : ''
  const m = loc.match(/^https?:\/\/([^/]+)/)
  const host = m ? m[1] : ''
  const bits = [d && d.manufacturer, d && d.model].filter(Boolean).join(' ')
  if (host && bits) return host + ' · ' + bits
  return host || bits || (d && d.usn) || ''
}
async function addCastDevice() {
  const ip = castIp.value.trim()
  if (!ip) return
  castBusy.value = true
  try {
    const d = await dlnaAddDevice(ip)
    if (!castDevices.value.some((x) => x.usn === d.usn)) castDevices.value.push(d)
    toast(t('detail.castFound', { name: d.name }), 'ok')
  } catch (e) { toast(e.message, 'err') }
  finally { castBusy.value = false }
}
async function doCast(d) {
  castBusy.value = true
  try {
    const r = await dlnaCast(id.value, d.usn)
    castTo.value = r.name || d.name
    castUsn.value = d.usn || ''
    toast(t('detail.castOk', { name: castTo.value }), 'ok')
    castOpen.value = false
  } catch (e) { toast(e.message || t('detail.castFail'), 'err') }
  finally { castBusy.value = false }
}
async function stopCast() {
  const usn = castUsn.value
    || (castDevices.value.find((x) => x.name === castTo.value) || {}).usn
  try {
    if (usn) await dlnaStop(usn)
    toast(t('detail.castStopped'), 'ok')
  } catch (e) { toast(e.message, 'err') }
  castTo.value = ''
  castUsn.value = ''
}

const open = computed(() => !!state.currentId)
const id = computed(() => state.currentId)
// 片单连播是全屏层（--z-player），层级高于普通抽屉。连播途中打开详情时若不提升
// 层级，抽屉会被播放层完全盖住，看起来像"点了没反应"。
const overPlayer = computed(() => !!state.playQueue?.open)

// 连播途中打开详情时暂停播放：抽屉几乎占满右侧，画面被完全盖住，
// 声音却还在响会让人以为播放器失控。改用广播事件而不是组件间传 ref，
// 详情抽屉不必知道当前是谁在播放（连播播放器 / 迷你条都可能）。
// 注意：关闭详情后不自动续播——用户在详情里可能已切走或打算离开，
// 交给播放器控件手动继续更可控。
function pausePlayer() {
  window.dispatchEvent(new CustomEvent('avm-pause-player'))
}
watch(
  () => [overPlayer.value, open.value],
  ([inPlayer, isOpen]) => { if (inPlayer && isOpen) pausePlayer() },
)
// 打开详情时加载常用站点配置（只取启用的）
watch(open, (v) => { if (v) { loadSites(); loadExt() } })

const progressPos = computed(() => Number(mv.value?.progress?.position) || 0)
const progressPct = computed(() => {
  const d = Number(mv.value?.progress?.duration) || 0
  return d > 0 ? Math.min(100, Math.round((progressPos.value / d) * 100)) : 0
})

const mainFile = computed(() => (mv.value?.files || [])[0] || null)

/** 网络串流只展示域名，避免把带签名的完整地址摊在界面上 */
function hostOf(url) {
  try { return new URL(url).host } catch (e) { return url }
}
const totalSize = computed(() => (mv.value?.files || []).reduce((s, f) => s + (Number(f.size) || 0), 0))
const quality = computed(() => qualityTag(mv.value?.resolution))

async function load() {
  if (!id.value) return
  loading.value = true
  sitesMoreOpen.value = false
  tab.value = 'preview'
  playing.value = false
  previews.value = []
  similar.value = []
  editing.value = false
  try {
    const m = await getMovie(id.value)
    // 后端 tags 可能为数组或逗号分隔字符串，统一规整为数组，避免 .filter 报错
    if (m) {
      let t = m.tags
      if (typeof t === 'string') t = t ? t.split(',').map((s) => s.trim()).filter(Boolean) : []
      else if (!Array.isArray(t)) t = []
      m.tags = t
    }
    mv.value = m
    // 交给全局 AI 浮窗共用同一个影片对象，并顺手探测一次 AI 是否可用（按钮据此高亮）
    ws.syncMovie(m)
    ws.ensureStatus()
  } catch (e) {
    toast(e.message, 'err')
    close()
  } finally {
    loading.value = false
    loadSimilar()
  }
}

function close() {
  state.currentId = null
  playing.value = false
  closeLightbox()
}

/* ---------- 操作 ---------- */
async function flip(field) {
  try {
    const r = await toggleFlag(id.value, field)
    const v = r && r.value != null ? r.value : !mv.value[field]
    mv.value[field] = v
    window.dispatchEvent(new CustomEvent('avm-reload-view'))
  } catch (e) { toast(e.message, 'err') }
}

async function setRating(v) {
  const next = mv.value.rating === v ? 0 : v
  try {
    await updateMovie(id.value, { rating: next })
    mv.value.rating = next
    toast(next ? t('detail.ratedStars', { n: next }) : t('detail.clearedRating'), 'ok')
    window.dispatchEvent(new CustomEvent('avm-reload-view'))
  } catch (e) { toast(e.message, 'err') }
}

async function play() {
  try { await playMovie(id.value); toast(t('player.launchedExternal'), 'ok') }
  catch (e) { toast(e.message, 'err') }
}
// 在文件管理器中定位影片所在文件夹（仅在运行软件的电脑上生效，本地优先专属）
async function openFolder() {
  try { await playMovie(id.value, { reveal: true }); toast(t('detail.revealFolder'), 'ok') }
  catch (e) { toast(e.message || t('detail.revealFolderErr'), 'err') }
}
function playSimilar(s) {
  state.currentId = s.id
  pendingAutoplay.value = true
}

// 详情页「重新刮削」复用批量刮削任务管线，进度会出现在右上角任务中心。
async function doScrape() {
  try {
    await runScrape({ ids: [id.value], overwrite: true })
    toast(t('common.scrapeQueued'), 'ok')
  } catch (e) { toast(e.message, 'err') }
}

// 刮削任务结束后自动刷新详情（任务中心轮询会在结束时广播 avm-refresh）
watch(
  () => state.task.scrape.running,
  (running, was) => {
    if (was && !running && id.value) load()
  },
)

async function doNfo() {
  try {
    const r = await exportNfo(id.value)
    // 导出可能只写了一半（例如目录里已有 NFO 被跳过、只复制了封面），
    // 所以用 skipped/message 把「为什么没写全」直接告诉用户。
    if (r.ok) toast(t('detail.nfoExported', { path: r.nfo || r.path || 'NFO' }), 'ok')
    else toast(r.message || t('detail.nfoExportSkipped'), 'err')
  } catch (e) { toast(e.message, 'err') }
}

// mode: 'db' 仅移出库（保留文件） / 'trash' 删除到系统回收站（可恢复） / 'permanent' 永久删除
async function doDelete(mode) {
  const isTrash = mode === 'trash'
  const isPermanent = mode === 'permanent'
  const path = mainFile.value?.path || ''
  const ok = await confirmDialog(
    isPermanent ? t('detail.delWithFile') : isTrash ? t('detail.delToTrash') : t('detail.delFromDb'),
    isPermanent
      ? t('detail.delWithFileDesc', { path })
      : isTrash
        ? t('detail.delToTrashDesc', { path })
        : t('detail.delFromDbDesc'),
    { danger: true, okText: isPermanent ? t('detail.permanent') : isTrash ? t('detail.delToTrash') : t('detail.remove') },
  )
  if (!ok) return
  try {
    const r = await deleteMovie(id.value, isPermanent, isTrash)
    const failed = (r && r.failed_files) || []
    toast(t('detail.deleted'), failed.length ? 'warn' : 'ok')
    close()
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  } catch (e) { toast(e.message, 'err') }
}

/* ---------- 预览图 ---------- */
async function loadPreviews(generate = false) {
  pvLoading.value = true
  try {
    const r = await getPreviews(id.value, generate)
    previews.value = (r && r.urls) ? r.urls.map((u) => '/api' + u) : []
    if (generate && !previews.value.length) toast(r.error || t('detail.genFailed'), 'err')
  } catch (e) { toast(e.message, 'err') } finally { pvLoading.value = false }
}

/* ---------- 相似推荐 ---------- */
async function loadSimilar() {
  try {
    const r = await getSimilar(id.value, 12)
    similar.value = (r && (r.items || r.movies)) || (Array.isArray(r) ? r : [])
  } catch (e) { similar.value = [] }
}

/* ---------- 编辑 ---------- */
function startEdit() {
  draft.value = {
    title: mv.value.title || '',
    code: mv.value.code || '',
    release_date: fmtDate(mv.value.release_date),
    runtime: mv.value.runtime || '',
    director: mv.value.director || '',
    plot: mv.value.plot || '',
    note: mv.value.note || '',
  }
  editing.value = true
}

async function saveEdit() {
  try {
    const patch = { ...draft.value }
    if (patch.runtime !== '') patch.runtime = Number(patch.runtime) || 0
    await updateMovie(id.value, patch)
    toast(t('detail.saved'), 'ok')
    editing.value = false
    await load()
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  } catch (e) { toast(e.message, 'err') }
}

/* ---------- 自定义标签（轻量增删，支持选择已有标签 / 创建新标签） ---------- */
const newTag = ref('')
const tagBusy = ref(false)
const allTags = ref([])            // 全库已有标签，用于输入建议
const showTagSuggest = ref(false)
function curTags() {
  const t = mv.value && mv.value.tags
  if (Array.isArray(t)) return t
  if (typeof t === 'string' && t) return t.split(',').map((s) => s.trim()).filter(Boolean)
  return []
}
async function ensureTags() {
  if (!allTags.value.length) {
    try { allTags.value = await listTags() } catch (e) { /* 忽略 */ }
  }
}
const tagSuggest = computed(() => {
  const q = newTag.value.trim().toLowerCase()
  const picked = new Set(curTags())
  return allTags.value
    .filter((t) => !picked.has(t.name))
    .filter((t) => !q || t.name.toLowerCase().includes(q))
    .slice(0, 8)
})
async function addTag() {
  const name = newTag.value.trim()
  if (!name || tagBusy.value) return
  if (curTags().includes(name)) { newTag.value = ''; showTagSuggest.value = false; return }
  tagBusy.value = true
  try {
    const next = [...curTags(), name]
    await updateMovie(id.value, { tags: next })
    mv.value = { ...mv.value, tags: next }
    if (!allTags.value.some((t) => t.name === name)) allTags.value.push({ name, count: 1 })
    newTag.value = ''
    showTagSuggest.value = false
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  } catch (e) { toast(e.message, 'err') }
  finally { tagBusy.value = false }
}
function pickTag(name) {
  if (curTags().includes(name)) return
  newTag.value = name
  addTag()
}
async function removeTag(name) {
  if (tagBusy.value) return
  tagBusy.value = true
  try {
    const next = curTags().filter((t) => t !== name)
    await updateMovie(id.value, { tags: next })
    mv.value = { ...mv.value, tags: next }
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  } catch (e) { toast(e.message, 'err') }
  finally { tagBusy.value = false }
}

/* ---------- AI 建议标签：待定区（tags_ai 列），不进标签库 ----------
   转正走专用端点：新标签以 source='ai' 入库（后期可在标签管理器批量清理），
   已有标签只加关联不动来源；待定区由端点同步清理 */
const aiTags = computed(() => (Array.isArray(mv.value?.tags_ai) ? mv.value.tags_ai : []))
async function saveAiTags(list) {
  const r = await updateMovie(id.value, { tags_ai: list })
  mv.value = { ...mv.value, tags_ai: (r && r.tags_ai) || list }
}
async function promoteAiTags(names) {
  if (tagBusy.value || !names.length) return
  tagBusy.value = true
  try {
    const r = await aiPromoteTags(id.value, names)
    if (r.movie) mv.value = { ...mv.value, ...r.movie }
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  } catch (e) { toast(e.message, 'err') }
  finally { tagBusy.value = false }
}
function promoteAiTag(name) { return promoteAiTags([name]) }
function promoteAllAiTags() { return promoteAiTags([...aiTags.value]) }
async function dropAiTag(name) {
  try { await saveAiTags(aiTags.value.filter((x) => x !== name)) } catch (e) { toast(e.message, 'err') }
}
async function clearAiTags() {
  try { await saveAiTags([]) } catch (e) { toast(e.message, 'err') }
}

/* ---------- 全局标签管理（改名 / 删除无关联标签） ---------- */
const showTagMgr = ref(false)
const allTagList = ref([])
const tagMgrBusy = ref(false)
const editingTag = ref('')
const editingTagNew = ref('')
async function openTagMgr() {
  showTagMgr.value = true
  await refreshTagMgr()
}
async function refreshTagMgr() {
  try { allTagList.value = await listTags() } catch (e) { toast(e.message, 'err') }
}
async function doRenameTag() {
  const oldN = editingTag.value
  const newN = (editingTagNew.value || '').trim()
  if (!oldN || !newN || tagMgrBusy.value) return
  tagMgrBusy.value = true
  try {
    const r = await renameTag(oldN, newN)
    toast(r.merged ? t('detail.mergedTo', { name: newN }) : t('detail.renamed'), 'ok')
    editingTag.value = ''
    editingTagNew.value = ''
    await refreshTagMgr()
    // 若当前影片命中该标签，同步显示名
    if (curTags().includes(oldN)) {
      mv.value = { ...mv.value, tags: curTags().map((t) => (t === oldN ? newN : t)) }
    }
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  } catch (e) { toast(e.message, 'err') }
  finally { tagMgrBusy.value = false }
}
async function doDeleteTag(name) {
  if (tagMgrBusy.value) return
  if (!(await confirmDialog(t('detail.deleteTagTitle'), t('detail.deleteTagDesc', { name }), { danger: true }))) return
  tagMgrBusy.value = true
  try {
    await deleteTag(name)
    toast(t('detail.deletedTag', { name }), 'ok')
    await refreshTagMgr()
    if (curTags().includes(name)) {
      const next = curTags().filter((t) => t !== name)
      await updateMovie(id.value, { tags: next })
      mv.value = { ...mv.value, tags: next }
    }
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  } catch (e) { toast(e.message, 'err') }
  finally { tagMgrBusy.value = false }
}

/* 批量清理 AI 来源标签：删除标签及其全部影片关联（确认后执行） */
const aiTagCount = computed(() => allTagList.value.filter((x) => x.source === 'ai').length)
async function doCleanupAiTags() {
  if (tagMgrBusy.value || !aiTagCount.value) return
  const names = allTagList.value.filter((x) => x.source === 'ai').map((x) => x.name).join('、')
  if (!(await confirmDialog(t('detail.aiTagCleanup'), t('detail.aiTagCleanupDesc', { n: aiTagCount.value, names }), { danger: true }))) return
  tagMgrBusy.value = true
  try {
    const r = await cleanupTags('ai')
    toast(t('detail.aiTagCleaned', { n: r.removed }), 'ok')
    await refreshTagMgr()
    if (id.value) await load()
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  } catch (e) { toast(e.message, 'err') }
  finally { tagMgrBusy.value = false }
}
const suggestIdx = ref(-1)
function onTagBlur() { setTimeout(() => { showTagSuggest.value = false; suggestIdx.value = -1 }, 150) }
function moveSuggest(dir) {
  const n = tagSuggest.value.length
  if (!n) return
  // -1 表示停留在输入框文本；0..n-1 表示选中某建议
  let i = suggestIdx.value + dir
  if (i < -1) i = n - 1
  if (i > n - 1) i = -1
  suggestIdx.value = i
  if (i >= 0) newTag.value = tagSuggest.value[i].name
}
const fileInput = ref(null)
async function onUpload(e) {
  const f = e.target.files && e.target.files[0]
  if (!f) return
  try {
    await uploadCover(id.value, f)
    toast(t('detail.coverUpdated'), 'ok')
    bust.value = Date.now()
  } catch (err) { toast(err.message, 'err') }
  e.target.value = ''
}
const bust = ref(Date.now())
const coverSrc = computed(() => `${coverUrl(id.value)}?t=${bust.value}`)

async function removeCover() {
  if (!(await confirmDialog(t('detail.clearCoverTitle'), t('detail.clearCoverDesc'), { danger: true }))) return
  try { await clearCover(id.value); bust.value = Date.now(); toast(t('detail.cleared'), 'ok') }
  catch (e) { toast(e.message, 'err') }
}

// 从视频抽一帧作为封面（临时占位，刮削到正式海报时会被覆盖）
const extracting = ref(false)
const isVideoCover = computed(() => mv.value && mv.value.cover_source === 'video')
async function extractFromVideo() {
  extracting.value = true
  try {
    await extractCover(id.value)
    bust.value = Date.now()
    toast(t('detail.coverExtracted'), 'ok')
  } catch (e) { toast(e.message, 'err') }
  finally { extracting.value = false }
}

/* ---------- 跳转筛选 ---------- */
function filterBy(key, value) {
  state.returnFromFilter = { id: id.value, title: (mv.value && (mv.value.title || mv.value.code)) || '' }
  if (key === 'actress' || key === 'genre') {
    state[key] = [value]
    state.actress = key === 'actress' ? [value] : []
    state.genre = key === 'genre' ? [value] : []
  } else {
    state.actress = []; state.genre = []
    state[key] = value
  }
  state.q = ''
  state.page = 1
  state.view = 'gallery'
  close()
}

/** 在影片详情页内以弹框形式打开女优介绍，不切换视图、不关闭当前详情。 */
const actressModal = ref('')
function openActress(name) {
  actressModal.value = name
}

/* ---------- 女优悬停预览（鼠标移到名字上显示基本信息浮层） ---------- */
const hoverActress = ref('')      // 当前 hover 的女优名
const hoverInfo = ref(null)       // 该女优的基本信息
const hoverLoading = ref(false)
const hoverShow = ref(false)
const hoverPos = ref({ x: 0, y: 0 })
const hoverDir = ref('down')      // down=chip 下方展开；up=上方展开
let hoverTimer = null
let hoverReq = 0                  // 防止快速切换时旧请求覆盖新结果

async function actressHoverStart(name, ev) {
  const rect = ev && ev.currentTarget && ev.currentTarget.getBoundingClientRect()
  if (rect) {
    // 下方空间足够则向下展开，否则向上翻转，避免溢出视口
    const vh = window.innerHeight || document.documentElement.clientHeight
    const down = rect.bottom + 8 + 320 <= vh
    hoverDir.value = down ? 'down' : 'up'
    hoverPos.value = down
      ? { x: rect.left, y: rect.bottom + 8 }
      : { x: rect.left, y: rect.top - 8 }
  }
  hoverActress.value = name
  hoverShow.value = false
  hoverInfo.value = null
  // 小延迟，避免快速扫过就弹
  clearTimeout(hoverTimer)
  hoverTimer = setTimeout(async () => {
    if (hoverActress.value !== name) return
    hoverShow.value = true
    hoverLoading.value = true
    const reqId = ++hoverReq
    try {
      const r = await getActress(name, 1, 1)
      if (reqId === hoverReq && hoverActress.value === name) {
        hoverInfo.value = (r && r.info) || null
      }
    } catch (e) {
      if (reqId === hoverReq) hoverInfo.value = null
    } finally {
      if (reqId === hoverReq) hoverLoading.value = false
    }
  }, 200)
}
let hoverCloseTimer = null
// 鼠标移出 chip / 浮层时：延迟一小段再关闭，给鼠标留出从 chip 滑到浮层的时间
function scheduleHoverEnd() {
  clearTimeout(hoverTimer)
  clearTimeout(hoverCloseTimer)
  hoverCloseTimer = setTimeout(() => {
    hoverActress.value = ''
    hoverShow.value = false
    hoverInfo.value = null
  }, 200)
}
// 鼠标进入浮层：取消关闭，允许在浮层内滚动/阅读
function cancelHoverEnd() {
  clearTimeout(hoverCloseTimer)
}
// 立即关闭（供切换 chip 等场景）
function actressHoverEnd() {
  clearTimeout(hoverTimer)
  clearTimeout(hoverCloseTimer)
  hoverActress.value = ''
  hoverShow.value = false
  hoverInfo.value = null
}
const hoverAvatar = computed(() => {
  const i = hoverInfo.value
  if (!i) return ''
  if (i.avatar) return avatarUrl(i.avatar)
  return i.sample_id ? coverThumbUrl(i.sample_id, 240) : ''
})
const hoverMeasure = computed(() => {
  const i = hoverInfo.value
  if (!i) return ''
  return [i.bust, i.waist, i.hip].filter(Boolean).join(' / ')
})
const hoverAge = computed(() => {
  const b = hoverInfo.value && hoverInfo.value.birthday
  if (!b) return null
  const y = Number(String(b).slice(0, 4))
  return y ? Math.max(0, new Date().getFullYear() - y) : null
})

/* ---------- 跳转筛选（按自定义标签） ---------- */
function filterByTag(tag) {
  state.returnFromFilter = { id: id.value, title: (mv.value && (mv.value.title || mv.value.code)) || '' }
  state.actress = []
  state.genre = []
  state.studio = ''
  state.series = ''
  state.tag = [tag]
  state.q = ''
  state.page = 1
  state.view = 'gallery'
  close()
}

/* ---------- 生命周期 ---------- */
watch(id, async (v) => {
  if (v) {
    bust.value = Date.now()
    await load()
    if (pendingAutoplay.value) {
      pendingAutoplay.value = false
      playing.value = true
    }
  }
})
watch(tab, (t) => {
  if (t === 'preview' && !previews.value.length) loadPreviews(false)
})

function onKey(e) {
  if (!open.value) return
  if (e.key === 'Escape') {
    if (actressModal.value) { actressModal.value = ''; return }
    if (sitesMoreOpen.value) { sitesMoreOpen.value = false; return }
    if (lightboxSrc.value) { closeLightbox(); return }
    close()
    return
  }
  // 放大查看时用方向键翻页（只在图层打开时生效，避免干扰播放器快捷键）
  if (lightboxSrc.value && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    e.preventDefault()
    stepLightbox(e.key === 'ArrowLeft' ? -1 : 1)
  }
}
onMounted(() => { window.addEventListener('keydown', onKey) })
onBeforeUnmount(() => { window.removeEventListener('keydown', onKey) })
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="drawer-mask" :class="{ 'over-player': overPlayer }" @click="close"></div>
    <aside class="drawer" :class="{ open, 'over-player': overPlayer }">
      <template v-if="mv">
        <!-- 头部 -->
        <header class="drawer-head">
          <span v-if="mv.display_code || mv.code" class="badge code primary">{{ mv.display_code || mv.code }}</span>
          <b class="dh-title ellipsis">{{ mv.title || $t('detail.untitled') }}</b>
          <div class="spacer"></div>
          <button class="btn ghost icon" @click="copyText(mainFile?.path || '')" :data-tip="$t('detail.copyPath')">⧉</button>
          <button class="btn ghost icon" @click="close" :data-tip="$t('detail.closeEsc')">✕</button>
        </header>

        <div class="drawer-body">
          <!-- 「站点·更多」展开时，透明遮罩负责点空白处关闭（不压暗整页） -->
          <div v-if="sitesMoreOpen" class="sites-shade" @click="sitesMoreOpen = false"></div>
          <!-- 左侧主内容 -->
          <div class="dd-content">
            <!-- 播放器 -->
            <VideoPlayer
              v-if="playing"
              :movie-id="mv.id"
              :start-at="progressPos"
              @progress="(p) => { if (mv.progress) mv.progress.position = p.position }"
            />

            <!-- 主区 -->
            <div class="dd-top">
              <div class="dd-cover">
                <img :src="coverSrc" alt="" @error="coverFallback" @click="openLightbox([coverSrc], 0)" />
                <div v-if="progressPct > 0" class="cw-bar"><i :style="{ width: progressPct + '%' }"></i></div>
                <span v-if="isVideoCover" class="cover-src-badge" :title="$t('detail.videoCoverTip')">{{ $t('detail.videoCover') }}</span>
                <div class="cov-acts">
                  <button class="btn tiny" @click="fileInput.click()">{{ $t('detail.changeCover') }}</button>
                  <button class="btn tiny" :disabled="extracting" @click="extractFromVideo">{{ $t('detail.extractCover') }}</button>
                  <button class="btn tiny ghost" @click="removeCover">{{ $t('detail.clearCover') }}</button>
                  <input ref="fileInput" type="file" accept="image/*" hidden @change="onUpload" />
                </div>
              </div>

              <div class="dd-main">
                <!-- 主操作：按「播放 / 标记 / 站点 / 维护」四组平铺，全部一级可见，不藏菜单 -->
                <div class="dd-actions">
                  <div class="dd-g">
                    <span class="dd-g-lbl">{{ $t('detail.grpPlay') }}</span>
                    <button
                      class="btn primary"
                      :disabled="!hasPlayable"
                      :title="hasPlayable ? '' : $t('detail.noSource')"
                      @click="playing = !playing"
                    >
                      {{ playing ? $t('detail.collapsePlayer') : (progressPct > 0 ? $t('detail.continue', { p: progressPct }) : $t('detail.onlinePlay')) }}
                    </button>
                    <button v-if="canExternal" class="btn" :disabled="!hasPlayable" @click="play">↗ {{ $t('player.external') }}</button>
                    <button class="btn" :disabled="!hasPlayable" :title="$t('detail.castHint')" @click="openCast">
                      📺 <span v-if="!castTo">{{ $t('detail.cast') }}</span>
                      <span v-else class="cast-on">{{ castTo }}</span>
                    </button>
                    <button v-if="castTo" class="btn ghost" @click="stopCast">{{ $t('detail.castStop') }}</button>
                  </div>

                  <div class="dd-g">
                    <span class="dd-g-lbl">{{ $t('detail.grpMark') }}</span>
                    <button class="btn icon" :class="{ active: mv.favorite }" @click="flip('favorite')" :data-tip="$t('flag.favorite')">{{ mv.favorite ? '♥' : '♡' }}</button>
                    <button class="btn icon" :class="{ active: mv.watchlist }" @click="flip('watchlist')" :data-tip="$t('flag.watchlist')">⌚</button>
                    <button class="btn icon" :class="{ active: mv.watched }" @click="flip('watched')" :data-tip="$t('flag.watched')">{{ mv.watched ? '●' : '○' }}</button>
                    <AddToCollectionBtn :movie-id="id" variant="detail" />
                    <button v-if="canReveal" class="btn" @click="openFolder" :data-tip="$t('detail.revealFolderTip')">📁 {{ $t('detail.revealFolder') }}</button>
                    <button class="btn" :class="{ 'ai-entry-on': ai.ready }" :title="$t('ai.entryTip')" @click="openAi">✨ {{ $t('ai.title') }}</button>
                  </div>

                  <div v-if="sites.length" class="dd-g">
                    <span class="dd-g-lbl">{{ $t('detail.grpSites') }}</span>
                    <!-- 前 3 个平铺，其余进「更多」：站点行只占一行 -->
                    <button v-for="s in topSites" :key="s.id" class="btn site-flat"
                            :title="renderSiteUrl(s, (mv && mv.code) || '')" @click="openSite(s)">
                      <span class="i">🔗</span> {{ s.name || s.url }}
                    </button>
                    <div v-if="moreSites.length" class="dd-sites-more" :class="{ open: sitesMoreOpen }">
                      <button class="btn site-flat" :aria-expanded="sitesMoreOpen"
                              @click.stop="sitesMoreOpen = !sitesMoreOpen">
                        {{ $t('detail.openSitesMore', { n: moreSites.length }) }} <span class="caret">▾</span>
                      </button>
                      <div class="sites-pop">
                        <div class="sites-pop-tip">{{ $t('detail.openSitesTip') }}</div>
                        <button v-for="s in moreSites" :key="s.id" class="sites-pop-item"
                                :title="renderSiteUrl(s, (mv && mv.code) || '')" @click="openSite(s)">
                          <span class="i">↗</span> {{ s.name || s.url }}
                        </button>
                      </div>
                    </div>
                  </div>

                  <div class="dd-g">
                    <span class="dd-g-lbl">{{ $t('detail.grpMaint') }}</span>
                    <button class="btn tiny ghost" @click="doScrape" :disabled="loading">⟳ {{ $t('detail.rescrape') }}</button>
                    <button class="btn tiny ghost" @click="doNfo">⤓ {{ $t('detail.exportNfo') }}</button>
                    <button class="btn tiny ghost" @click="removeCover">🖼 {{ $t('detail.clearCover') }}</button>
                  </div>

                  <!-- 投屏：选同网段的 DLNA 设备；扫不到就手动填电视 IP -->
                  <div v-if="castOpen" class="cast-mask" @click.self="castOpen = false">
                    <div class="cast-box">
                      <div class="cast-head">
                        <b>{{ $t('detail.castTitle') }}</b>
                        <button class="btn tiny ghost" @click="castOpen = false">✕</button>
                      </div>
                      <p class="hint">{{ $t('detail.castHint') }}</p>
                      <div class="cast-list">
                        <p v-if="castBusy" class="hint">{{ $t('detail.castScanning') }}</p>
                        <template v-else-if="castDevices.length">
                          <button v-for="d in castDevices" :key="d.usn" class="cast-item" @click="doCast(d)">
                            <b>{{ d.name }}</b>
                            <span class="muted sm">{{ castSub(d) }}</span>
                          </button>
                        </template>
                        <p v-else class="hint">{{ $t('detail.castNone') }}</p>
                      </div>
                      <div class="cast-add">
                        <input v-model="castIp" class="inp" :placeholder="$t('detail.castIpPh')"
                               @keydown.enter.prevent="addCastDevice" />
                        <button class="btn tiny" :disabled="castBusy || !castIp.trim()" @click="addCastDevice">
                          {{ $t('detail.castAdd') }}
                        </button>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- 评分 -->
                <div class="dd-rate">
                  <div class="stars">
                    <span v-for="i in 5" :key="i" class="s" :class="{ on: i <= (mv.rating || 0) }" @click="setRating(i)">★</span>
                  </div>
                  <span class="muted">{{ mv.rating ? mv.rating + ' ' + $t('detail.star') : $t('fmt.noRating') }}</span>
                  <div class="spacer"></div>
                  <span v-if="mv.play_count" class="badge">{{ $t('detail.playCount', { n: mv.play_count }) }}</span>
                </div>

                <!-- 标记 -->
                <div class="chip-list">
                  <span v-if="mv.subtitle" class="badge ok">{{ $t('detail.subtitle') }}</span>
                <span v-if="mv.embed_sub_zh" class="badge ok" :title="$t('flag.embedZh')">{{ $t('flag.embedZh') }}</span>
                  <span v-if="mv.uncensored" class="badge warn">{{ $t('detail.uncensored') }}</span>
                  <span v-if="quality" class="badge accent">{{ quality }}</span>
                  <span v-if="mv.vr" class="badge">VR</span>
                  <span v-if="mv.leak" class="badge err">{{ $t('detail.leak') }}</span>
                  <span v-for="s in manualSubs" :key="s.id" class="badge sub" :title="s.path">
                    {{ $t('detail.subFile', { lang: s.lang || '字幕' }) }}
                  </span>
                  <!-- 同目录外挂字幕登记：收进徽章行末尾，不再单独占一行 -->
                  <span class="spacer"></span>
                  <button class="btn tiny ghost" :disabled="scanningSubs"
                          :title="$t('detail.scanSubsHint')" @click="scanSidecars">
                    {{ scanningSubs ? $t('detail.scanningSubs') : $t('detail.scanSubs') }}
                  </button>
                </div>

                <!-- 已生成的 AI 字幕译文：下载 / 删除（生成入口统一在 AI 工作台） -->
                <div v-if="aiSubs.length" class="ai-subs">
                  <div v-for="s in aiSubs" :key="s.id" class="ai-sub-row">
                    <span class="mono">{{ s.lang }}{{ (s.filename || '').includes('-双语') ? ' · ' + $t('detail.aiBilingual') : '' }}</span>
                    <span class="spacer"></span>
                    <button class="btn tiny ghost" @click="ai.downloadSub(s)">{{ $t('common.download') }}</button>
                    <button class="btn tiny ghost" @click="ai.removeAiSub(s)">{{ $t('common.delete') }}</button>
                  </div>
                </div>

                <!-- 关键信息 -->
                <dl class="dd-facts">
                  <template v-if="mv.actresses && mv.actresses.length">
                    <dt>{{ $t('detail.fActress') }}</dt>
                    <dd class="chip-list actress-list">
                      <button
                        v-for="a in mv.actresses"
                        :key="a"
                        class="chip actress-chip"
                        @click="openActress(a)"
                        @mouseenter="actressHoverStart(a, $event)"
                        @mouseleave="scheduleHoverEnd"
                      >{{ a }}</button>
                    </dd>
                  </template>
                  <template v-if="mv.genres && mv.genres.length">
                    <dt>{{ $t('detail.fGenre') }}</dt>
                    <dd class="chip-list">
                      <button v-for="g in mv.genres" :key="g" class="chip" @click="filterBy('genre', g)">{{ g }}</button>
                    </dd>
                  </template>
                  <template v-if="mv.studio"><dt>{{ $t('detail.fStudio') }}</dt><dd><a @click="filterBy('studio', mv.studio)">{{ mv.studio }}</a></dd></template>
                  <template v-if="mv.series"><dt>{{ $t('detail.fSeries') }}</dt><dd><a @click="filterBy('series', mv.series)">{{ mv.series }}</a></dd></template>
                  <template v-if="mv.director"><dt>{{ $t('detail.fDirector') }}</dt><dd>{{ mv.director }}</dd></template>
                  <template v-if="mv.release_date"><dt>{{ $t('detail.fRelease') }}</dt><dd>{{ fmtDate(mv.release_date) }}</dd></template>
                  <template v-if="mv.runtime"><dt>{{ $t('detail.fRuntime') }}</dt><dd>{{ mv.runtime }} {{ $t('detail.minute') }}</dd></template>
                  <template v-if="mv.resolution"><dt>{{ $t('detail.fResolution') }}</dt><dd>{{ mv.resolution }}</dd></template>
                  <dt>{{ $t('detail.fSize') }}</dt><dd>{{ fmtSize(totalSize) }}<span v-if="mv.files && mv.files.length > 1" class="dim"> · {{ $t('detail.filesCount', { n: mv.files.length }) }}</span></dd>
                  <template v-if="mainFile && mainFile.strm_url">
                    <dt>{{ $t('detail.fSource') }}</dt>
                    <dd class="mono dim">🌐 {{ hostOf(mainFile.strm_url) }}</dd>
                  </template>
                  <template v-if="mv.added_at"><dt>{{ $t('detail.fAdded') }}</dt><dd>{{ fmtAgo(mv.added_at) }}</dd></template>
                </dl>

                <!-- 自定义标签：常驻主区可见，随时增删 -->
                <div class="dd-tags">
                  <div class="dd-tags-head">
                    <span class="lbl">{{ $t('detail.customTags') }}</span>
                    <button class="link-btn" @click="openTagMgr">{{ $t('detail.manageTags') }}</button>
                    <button v-if="ai.ready" class="link-btn" :title="$t('ai.tagsHint')" @click="openAiWithTags">✨ {{ $t('ai.tagsSuggest') }}</button>
                  </div>
                  <div v-if="mv.tags && mv.tags.length" class="chip-list wrap">
                    <span v-for="t in mv.tags" :key="t" class="badge tag-removable clickable" @click="filterByTag(t)">
                      {{ t }}
                      <button class="tag-x" :title="$t('detail.removeTagTitle') + t" @click.stop="removeTag(t)">×</button>
                    </span>
                  </div>
                  <div v-else class="dim small">{{ $t('detail.noTags') }}</div>

                  <!-- AI 建议标签：待定区，不进标签库；转正（＋）后才参与筛选/统计 -->
                  <div v-if="aiTags.length" class="ai-tags-row">
                    <div class="dd-tags-head">
                      <span class="lbl">✨ {{ $t('detail.aiTagsPending') }}</span>
                      <button class="link-btn" @click="promoteAllAiTags">{{ $t('detail.aiTagPromoteAll') }}</button>
                      <button class="link-btn" @click="clearAiTags">{{ $t('detail.aiTagClear') }}</button>
                    </div>
                    <div class="chip-list wrap">
                      <span v-for="t in aiTags" :key="'ai-' + t" class="badge ai-tag clickable" :title="$t('detail.aiTagTip')">
                        {{ t }}
                        <button class="tag-x promote" :title="$t('detail.aiTagPromote')" @click.stop="promoteAiTag(t)">＋</button>
                        <button class="tag-x" :title="$t('common.delete')" @click.stop="dropAiTag(t)">×</button>
                      </span>
                    </div>
                  </div>
                  <div class="tag-edit">
                    <div class="tag-input-wrap">
                      <input
                        v-model="newTag"
                        class="tag-input"
                        :placeholder="$t('detail.tagPlaceholder')"
                        @focus="ensureTags(); showTagSuggest = true"
                        @blur="onTagBlur"
                        @keydown.enter.prevent="addTag"
                        @keydown.down.prevent="moveSuggest(1)"
                        @keydown.up.prevent="moveSuggest(-1)"
                        @keydown.esc="showTagSuggest = false"
                      />
                      <div v-if="showTagSuggest && tagSuggest.length" class="tag-suggest">
                        <button
                          v-for="(s, i) in tagSuggest"
                          :key="s.name"
                          class="tag-suggest-item"
                          :class="{ on: i === suggestIdx }"
                          @mousedown.prevent="pickTag(s.name)"
                          @mouseenter="suggestIdx = i"
                        >
                          <span>{{ s.name }}</span>
                          <span class="dim small">{{ $t('detail.usedCount', { n: s.count }) }}</span>
                        </button>
                      </div>
                    </div>
                    <button class="btn tiny" :disabled="tagBusy || !newTag.trim()" @click="addTag">{{ $t('common.add') }}</button>
                  </div>

                  <!-- 全局标签管理弹窗 -->
                  <div v-if="showTagMgr" class="tag-mgr" @click.self="showTagMgr = false">
                    <div class="tag-mgr-box">
                      <div class="tm-head">
                        <b>{{ $t('detail.manageTags') }}</b>
                        <button class="icon-btn" :title="$t('common.close')" @click="showTagMgr = false">×</button>
                      </div>
                      <p class="muted small">{{ $t('detail.tagMgrHint') }}</p>
                      <div class="dd-tags-head">
                        <span v-if="aiTagCount" class="dim small">✨ {{ $t('detail.aiTagCount', { n: aiTagCount }) }}</span>
                        <button v-if="aiTagCount" class="link-btn danger"
                                :title="$t('detail.aiTagCleanupTip')" @click="doCleanupAiTags">{{ $t('detail.aiTagCleanup') }}</button>
                      </div>
                      <div v-if="!allTagList.length" class="dim">{{ $t('detail.noTags') }}</div>
                      <ul class="tm-list">
                        <li v-for="t in allTagList" :key="t.id" class="tm-item">
                          <template v-if="editingTag === t.name">
                            <input v-model="editingTagNew" class="tm-input" @keydown.enter.prevent="doRenameTag" :placeholder="$t('detail.newName')" />
                            <button class="btn tiny" :disabled="tagMgrBusy || !editingTagNew.trim()" @click="doRenameTag">{{ $t('common.save') }}</button>
                            <button class="btn tiny ghost" @click="editingTag = ''">{{ $t('common.cancel') }}</button>
                          </template>
                          <template v-else>
                            <span class="tm-name">{{ t.name }}<span v-if="t.source === 'ai'" class="badge ai-src" :title="$t('detail.aiTagCleanupTip')">✨</span><span v-else-if="t.source === 'scraper'" class="badge scraper-src" :title="$t('detail.scraperSrcTip')">⇩</span></span>
                            <span class="dim small">{{ $t('detail.usedCount', { n: t.count }) }}</span>
                            <span class="tm-actions">
                              <button class="link-btn" @click="editingTag = t.name; editingTagNew = t.name">{{ $t('detail.rename') }}</button>
                              <button class="link-btn danger" @click="doDeleteTag(t.name)">{{ $t('common.delete') }}</button>
                            </span>
                          </template>
                        </li>
                      </ul>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <!-- 标签页 -->
            <div class="tabs dd-tabs">
              <button class="tab" :class="{ on: tab === 'preview' }" @click="tab = 'preview'">{{ $t('detail.tabPreview') }}</button>
              <button class="tab" :class="{ on: tab === 'info' }" @click="tab = 'info'">{{ $t('detail.tabInfo') }}</button>
              <button class="tab" :class="{ on: tab === 'files' }" @click="tab = 'files'">{{ $t('detail.tabFiles') }}</button>
              <!-- 重刮削 / 导出 NFO 收进主区「⋯更多」，这里只留标签页 -->
            </div>

            <div class="dd-pane">
              <!-- 简介 -->
              <template v-if="tab === 'info'">
                <div v-if="!editing">
                  <p v-if="mv.plot" class="plot">{{ mv.plot }}</p>
                  <p v-else class="muted">{{ $t('detail.noSynopsis') }}</p>
                  <p v-if="mv.plot_source" class="plot-src">
                    {{ mv.plot_source === 'ai-subtitle' ? $t('detail.aiFromSubs') : $t('detail.aiFromMeta') }}
                    <template v-if="mv.ai_plot_at"> · {{ fmtAgo(mv.ai_plot_at) }}</template>
                    · {{ $t('detail.aiEditable') }}
                  </p>
                  <div v-if="mv.note" class="note-box"><b>{{ $t('detail.note') }}</b><p>{{ mv.note }}</p></div>

                  <!-- AI 快捷：写简介（就地生成、对照确认才写库）；问剧情要字幕 -->
                  <div v-if="ai.ready" class="aiq">
                    <!-- 写简介不强制要字幕：没字幕时后端按标题/演员/系列等元数据兜底，
                         依据在标题和角标上都写清楚，避免用户以为读了片子内容 -->
                    <div class="hstack wrap">
                      <button v-if="!ai.plot.draft" class="btn tiny ghost" :disabled="ai.plot.loading"
                              :title="canQuickAi ? $t('ai.plotHint') : $t('ai.plotHintMeta')"
                              @click="ai.generatePlot">
                        ✨ {{ ai.plot.loading ? $t('ai.working') : (mv.plot ? $t('ai.plotRewrite') : $t('ai.plotCreate')) }}
                      </button>
                      <button v-else class="btn tiny ghost" @click="openAiWithPlot">{{ $t('ai.openPanel') }}</button>
                    </div>
                    <p v-if="ai.plot.error" class="aiq-err">{{ ai.plot.error }}</p>
                    <!-- 没字幕时说明依据，别让用户以为 AI 读过片子内容 -->
                    <p v-else-if="!canQuickAi && !ai.plot.draft && !ai.probing" class="aiq-need">
                      {{ metaRich ? $t('ai.plotHintMeta') : $t('ai.plotHintThin') }}
                    </p>

                    <!-- 新旧对照：点「采用」才写入 -->
                    <div v-if="ai.plot.draft" class="aiq-compare">
                      <div class="aiq-col">
                        <div class="aiq-col-head">{{ $t('ai.oldVersion') }}</div>
                        <p class="aiq-text">{{ ai.plot.old || $t('ai.none') }}</p>
                      </div>
                      <div class="aiq-col new">
                        <div class="aiq-col-head">
                          {{ $t('ai.newVersion') }}
                          <span class="badge ok">{{ ai.plot.source === 'subtitle' ? $t('ai.srcSubtitle') : $t('ai.srcMeta') }}</span>
                        </div>
                        <p class="aiq-text">{{ ai.plot.draft }}</p>
                      </div>
                      <div class="hstack wrap">
                        <button class="btn tiny primary" @click="ai.applyPlot">{{ $t('ai.useNew') }}</button>
                        <button class="btn tiny ghost" @click="ai.discardPlot">{{ $t('ai.keepOld') }}</button>
                      </div>
                    </div>

                    <template v-if="canQuickAi">
                      <!-- 问剧情：答案带时间点，点了直接跳到片中那一刻 -->
                      <div class="aiq-ask">
                        <div class="aiq-ask-row">
                          <input v-model="ai.ask.question" class="inp" :placeholder="$t('ai.askPh')"
                                 @keydown.enter.prevent="ai.sendAsk()" />
                          <button class="btn tiny primary"
                                  :disabled="ai.ask.loading || !ai.ask.question.trim()"
                                  @click="ai.sendAsk">
                            {{ ai.ask.loading ? $t('ai.working') : $t('ai.askRun') }}
                          </button>
                        </div>
                        <p v-if="ai.ask.error" class="aiq-err">{{ ai.ask.error }}</p>
                        <div v-if="ai.ask.answer" class="aiq-answer">
                          <p class="aiq-text">{{ ai.ask.answer }}</p>
                          <div v-if="ai.ask.refs.length" class="aiq-refs">
                            <button v-for="(r, i) in ai.ask.refs" :key="i" class="aiq-ref"
                                    :title="r.text" @click="ws.seekToRef(r.time)">[{{ r.time }}]</button>
                            <span class="muted sm">{{ ai.ask.mode === 'digest' ? $t('ai.refHintDigest') : $t('ai.refHint') }}</span>
                          </div>
                        </div>
                      </div>
                    </template>

                    <!-- 没字幕：说清楚要怎么补（对白提取已独立成块，见下方 ai-dialog-sec） -->
                    <div v-else-if="!ai.probing" class="aiq-need-box">
                      <p class="aiq-need">{{ $t('ai.needSubs') }}</p>
                      <div class="hstack wrap">
                        <button class="btn tiny ghost" @click="scanSidecars">{{ $t('detail.scanSubs') }}</button>
                      </div>
                    </div>
                  </div>

                  <!-- 提取对白：独立成块、任何状态常显——本地 Whisper，不依赖 AI 开关与字幕；
                       有字幕的片也能提取（字幕缺内容时对白更准），提取过是「重新提取」 -->
                  <div class="ai-dialog-sec">
                    <div class="ai-dialog-head">
                      <b>{{ $t('ai.extractDialog') }}</b>
                      <span class="hint">{{ $t('ai.extractDialogHint') }}</span>
                    </div>
                    <div class="hstack wrap">
                      <select v-if="ai.dialog.status !== 'running'"
                              v-model="asrLimit" class="inp tiny asr-limit"
                              :title="$t('ai.asrLimitTip')">
                        <option value="0">{{ $t('ai.asrFull') }}</option>
                        <option value="600">{{ $t('ai.asrFirst10') }}</option>
                        <option value="1200">{{ $t('ai.asrFirst20') }}</option>
                        <option value="1800">{{ $t('ai.asrFirst30') }}</option>
                      </select>
                      <button
                        v-if="ai.dialog.status !== 'running'"
                        class="btn tiny"
                        :title="ai.dialog.has_text ? $t('ai.reExtractHint') : $t('ai.extractDialogHint')"
                        @click="onExtractDialog"
                      >{{ ai.dialog.has_text ? $t('ai.reExtract') : $t('ai.extractDialog') }}</button>
                      <span v-if="ai.dialog.status === 'running'" class="badge">{{ ai.dialog.percent }}%</span>
                      <span v-else-if="ai.dialog.has_text" class="badge ok">{{ $t('ai.dialogDone', { n: ai.dialog.segments }) }}</span>
                      <span v-else-if="ai.dialog.status === 'error'" class="badge err">{{ $t('ai.dialogFailed') }}</span>
                      <button v-if="ai.dialog.has_text" class="btn tiny ghost" @click="ai.toggleDialogText">
                        {{ ai.dialog.show ? $t('ai.hideDialog') : $t('ai.viewDialog') }}
                      </button>
                      <button v-if="ai.dialog.has_text" class="btn tiny" :title="$t('ai.dialogDraftHint')" @click="ai.exportSrt">
                        {{ $t('ai.dialogExportSrt') }}
                      </button>
                      <button v-if="ai.dialog.has_text" class="btn tiny ghost" @click="ai.deleteDialog">{{ $t('common.delete') }}</button>
                    </div>

                    <p v-if="ai.dialog.status === 'running'" class="hint">{{ ai.dialog.message || $t('ai.dialogRunning') }}</p>
                    <div v-if="ai.dialog.status === 'running'" class="aiq-bar">
                      <span :style="{ width: (ai.dialog.percent || 0) + '%' }"></span>
                    </div>
                    <p v-if="!ai.asr.lib_available" class="hint">{{ $t('ai.dialogNoLib') }}</p>
                    <p v-else-if="!ai.asr.model_available" class="hint">{{ $t('ai.dialogNoModel', { dir: ai.asr.model_dir }) }}</p>
                    <p v-if="ai.dialog.error" class="aiq-err">{{ ai.dialog.error }}</p>
                    <p v-if="ai.dialog.has_text" class="hint">{{ $t('ai.dialogThenExport') }}</p>
                    <pre v-if="ai.dialog.show && ai.dialog.text" class="aiq-dlg">{{ ai.dialog.text }}</pre>
                  </div>

                  <button class="btn tiny mt" @click="startEdit">{{ $t('detail.editMeta') }}</button>
                </div>

                <div v-else class="edit-form">
                  <div class="field"><label>{{ $t('detail.fTitle') }}</label><input v-model="draft.title" /></div>
                  <div class="two">
                    <div class="field"><label>{{ $t('detail.fCode') }}</label><input v-model="draft.code" /></div>
                    <div class="field"><label>{{ $t('detail.fRelease') }}</label><input v-model="draft.release_date" :placeholder="$t('detail.datePh')" /></div>
                  </div>
                  <div class="two">
                    <div class="field"><label>{{ $t('detail.fRuntime') }}</label><input v-model="draft.runtime" type="number" min="0" /></div>
                    <div class="field"><label>{{ $t('detail.fDirector') }}</label><input v-model="draft.director" /></div>
                  </div>
                  <div class="field"><label>{{ $t('detail.synopsis') }}</label><textarea v-model="draft.plot" rows="4"></textarea></div>
                  <div class="field"><label>{{ $t('detail.note') }}</label><textarea v-model="draft.note" rows="2"></textarea></div>
                  <div class="hstack">
                    <button class="btn primary" @click="saveEdit">{{ $t('common.save') }}</button>
                    <button class="btn ghost" @click="editing = false">{{ $t('common.cancel') }}</button>
                  </div>
                </div>
              </template>

              <!-- 预览图 -->
              <template v-else-if="tab === 'preview'">
                <div class="hstack mb">
                  <button class="btn tiny" :disabled="pvLoading" @click="loadPreviews(true)">
                    {{ pvLoading ? $t('detail.generating') : $t('detail.genPreview') }}
                  </button>
                  <span v-if="!extSt.ffmpeg" class="muted">{{ $t('detail.needFfmpeg') }}</span>
                </div>
                <div v-if="previews.length" class="pv-grid">
                  <img v-for="(u, i) in previews" :key="i" :src="u" alt="" @click="openLightbox(previews, i)" />
                </div>
                <div v-else-if="!pvLoading" class="empty"><div class="icon">▤</div><div class="desc">{{ $t('detail.noPreview') }}</div></div>
              </template>

              <!-- 文件 -->
              <template v-else>
                <table class="ftable">
                  <thead><tr><th>{{ $t('detail.fileName') }}</th><th>{{ $t('detail.fileSize') }}</th><th>{{ $t('detail.fileStatus') }}</th></tr></thead>
                  <tbody>
                    <tr v-for="f in mv.files" :key="f.id">
                      <td class="fname" :title="f.path">{{ f.filename }}</td>
                      <td class="tabular">{{ fmtSize(f.size) }}</td>
                      <td><span class="badge" :class="f.missing ? 'err' : 'ok'">{{ f.missing ? $t('detail.fileMissing') : $t('detail.fileOk') }}</span></td>
                    </tr>
                  </tbody>
                </table>
                <div class="danger-zone">
                  <b>{{ $t('detail.dangerZone') }}</b>
                  <div class="hstack">
                    <button class="btn tiny" @click="doDelete('db')">{{ $t('detail.delFromDbBtn') }}</button>
                    <button class="btn tiny" @click="doDelete('trash')">{{ $t('detail.delToTrashBtn') }}</button>
                    <button class="btn tiny danger" @click="doDelete('permanent')">{{ $t('detail.delWithFileBtn') }}</button>
                  </div>
                </div>
              </template>
            </div>
          </div>

          <!-- 右侧：相似推荐常驻，打开即展示，无需手动点击 -->
          <aside class="dd-similar-rail">
            <div class="rail-head">
              <span class="rail-title">{{ $t('detail.similar') }}</span>
              <span class="rail-sub">{{ $t('detail.similarSub') }}</span>
            </div>
            <div class="rail-list">
              <div v-for="s in similar" :key="s.id" class="sim" @click="state.currentId = s.id">
                <img :src="coverThumbUrl(s.id, 220)" alt="" @error="coverFallback" />
                <button class="sim-play" :title="$t('detail.playNow')" @click.stop="playSimilar(s)">▶</button>
                <div class="sim-t ellipsis">{{ s.title || s.code }}</div>
              </div>
              <div v-if="!similar.length" class="empty small"><div class="icon">≈</div><div class="desc">{{ $t('detail.noSimilar') }}</div></div>
            </div>
          </aside>
        </div>
      </template>

      <div v-else-if="loading" class="dd-loading"><span class="spinner large"></span></div>
    </aside>

    <!-- 灯箱 -->
    <div v-if="lightboxSrc" class="lightbox" @click="closeLightbox">
      <!-- 关闭按钮常驻右上角：之前只能靠 Esc 或点背景，关闭区域不明显 -->
      <button class="lb-close" :title="$t('common.close')" :aria-label="$t('common.close')"
              @click.stop="closeLightbox">✕</button>
      <div class="lb-box">
        <div class="lb-main">
          <button v-if="lightboxNav" class="lb-nav" :title="$t('detail.prevImage')"
                  :aria-label="$t('detail.prevImage')" @click.stop="stepLightbox(-1)">‹</button>
          <img :src="lightboxSrc" alt="" @click.stop />
          <button v-if="lightboxNav" class="lb-nav" :title="$t('detail.nextImage')"
                  :aria-label="$t('detail.nextImage')" @click.stop="stepLightbox(1)">›</button>
        </div>
        <div v-if="lightboxNav" class="lb-count">{{ lightboxIndex + 1 }} / {{ lightboxList.length }}</div>
        <div v-if="lightboxNav" class="lb-thumbs" @click.stop>
          <img v-for="(u, i) in lightboxList" :key="i" :src="u" :class="{ on: i === lightboxIndex }"
               :alt="String(i + 1)" @click="lightboxIndex = i" />
        </div>
      </div>
    </div>

    <!-- 女优弹框：在详情页内查看女优介绍，不关闭当前详情 -->
    <ActressModal v-if="actressModal" :ident="actressModal" @close="actressModal = ''" />
  </Teleport>
</template>

<style scoped>
/* 连播（全屏播放层）之上打开详情时，层级需高于 --z-player，否则会被完全盖住。
   .drawer-mask / .drawer 的基础样式在 styles/layout.css，这里只覆盖层级。 */
.drawer-mask.over-player { z-index: var(--z-drawer-over); }
.drawer.over-player { z-index: calc(var(--z-drawer-over) + 1); }

.dh-title { font-size: var(--fs-lg); font-weight: 600; flex: 1; min-width: 0; }

/* 主体：左主区 + 右相似推荐侧栏，常驻并排 */
.drawer-body { display: flex; flex-direction: row; overflow: hidden; flex: 1; min-height: 0; position: relative; }
.dd-content { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow-y: auto; }
.dd-similar-rail {
  flex: none; width: 300px;
  border-left: 1px solid var(--c-line);
  background: var(--c-surface);
  display: flex; flex-direction: column;
  min-height: 0;
}
.rail-head {
  flex: none; padding: var(--sp-4) var(--sp-4) var(--sp-3);
  border-bottom: 1px solid var(--c-line);
  display: flex; flex-direction: column; gap: 2px;
}
.rail-title { font-size: var(--fs-md); font-weight: 700; color: var(--c-text-1); }
.rail-sub { font-size: var(--fs-xs); color: var(--c-text-3); }
.rail-list {
  flex: 1; min-height: 0; overflow-y: auto;
  padding: var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-3);
}
.rail-list .sim img { box-shadow: var(--sh-1); }
.empty.small { padding: var(--sp-6) var(--sp-2); }

/* 自定义标签管理 */
.tag-removable { display: inline-flex; align-items: center; gap: 4px; }
.tag-removable.clickable { cursor: pointer; }
.tag-removable.clickable:hover { filter: brightness(1.12); border-color: var(--c-primary); }
.tag-x {
  border: 0; background: transparent; color: var(--c-text-dim, #aaa); cursor: pointer;
  font-size: 14px; line-height: 1; width: 18px; height: 18px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center; opacity: .8; padding: 0;
}
.tag-x:hover { opacity: 1; background: #e5484d; color: #fff; }
.tag-x.promote:hover { background: var(--c-primary); }
/* AI 建议标签待定区：虚线边框与正式标签区分，＋ 转正 / × 丢弃 */
.ai-tags-row { margin-top: 8px; }
.badge.ai-tag { border-style: dashed; }
.badge.ai-tag .tag-x.promote { color: var(--c-primary); opacity: 1; }
/* 标签管理器里的来源徽章 */
.badge.ai-src, .badge.scraper-src { font-size: 10px; padding: 0 4px; margin-left: 4px; }
.badge.ai-src { color: var(--c-primary); }
.badge.scraper-src { color: var(--c-text-dim, #999); }
.tag-edit { display: flex; gap: var(--sp-2); align-items: center; }
.tag-input-wrap { position: relative; flex: 1; min-width: 0; }
.tag-input {
  width: 100%; box-sizing: border-box;
  background: var(--c-surface-2); border: 1px solid var(--c-line); color: var(--c-text);
  border-radius: 8px; padding: 6px 10px; font: inherit; font-size: var(--fs-sm);
}
.tag-input:focus { outline: none; border-color: var(--c-primary); }
.tag-suggest {
  position: absolute; top: calc(100% + 4px); left: 0; right: 0; z-index: 60;
  background: var(--c-surface-3); border: 1px solid var(--c-line-strong);
  border-radius: 8px; box-shadow: var(--sh-3); max-height: 220px; overflow-y: auto; padding: 4px;
}
.tag-suggest-item {
  width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 8px;
  border: 0; background: transparent; color: var(--c-text); cursor: pointer;
  font: inherit; font-size: var(--fs-sm); text-align: left;
  padding: 6px 8px; border-radius: 6px;
}
.tag-suggest-item:hover, .tag-suggest-item.on { background: var(--c-primary-soft); color: var(--c-primary-text); }

.dd-top { display: flex; gap: var(--sp-4); padding: var(--sp-4) var(--sp-5); }
.dd-cover { position: relative; width: 168px; flex: none; }
.dd-cover img {
  width: 100%; aspect-ratio: 2/3; object-fit: cover;
  border-radius: var(--r-md);
  background: var(--c-surface-2);
  cursor: zoom-in;
  box-shadow: var(--sh-2);
}
.cw-bar { position: absolute; left: 0; right: 0; bottom: 40px; height: 3px; background: rgba(0,0,0,.5); }
.cw-bar > i { display: block; height: 100%; background: var(--c-primary); }
.cover-src-badge {
  position: absolute; top: 6px; left: 6px; z-index: 2;
  font-size: 10px; line-height: 1; padding: 3px 6px;
  border-radius: 4px; color: #fff;
  background: color-mix(in srgb, var(--c-primary) 80%, #000);
  box-shadow: 0 1px 4px rgba(0,0,0,.35);
  pointer-events: none;
}
/* 封面上的操作：一直可见（都是实在功能），紧凑排列即可 */
.cov-acts { display: flex; gap: var(--sp-2); margin-top: var(--sp-2); flex-wrap: wrap; }

.dd-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
/* 主操作按组平铺：每行一个组，左侧小标签对齐——所有功能一级可见，不搞层级 */
.dd-actions { display: flex; flex-direction: column; gap: 7px; }
.dd-g { display: flex; align-items: center; gap: var(--sp-1); flex-wrap: wrap; }
.dd-g-lbl { flex: none; min-width: 36px; font-size: 11px; color: var(--c-text-3); letter-spacing: .05em; }
.site-flat { white-space: nowrap; font-size: 12px; padding: 5px 9px; }
/* 站点「更多」：点击展开其余站点的面板；遮罩只负责点空白关闭，不压暗整页 */
.dd-sites-more { position: relative; z-index: 41; }
.dd-sites-more .caret { font-size: 10px; opacity: .7; margin-left: 2px; }
.dd-sites-more .sites-pop { display: none; }
.dd-sites-more.open .sites-pop { display: flex; }
.sites-shade { position: absolute; inset: 0; z-index: 40; background: transparent; }
.sites-pop { position: absolute; top: 100%; left: 0; z-index: 60; min-width: 220px;
  background: var(--c-surface-2); border: 1px solid var(--c-line-strong); border-radius: var(--r-md);
  box-shadow: var(--sh-3); padding: 6px var(--sp-2) var(--sp-2); flex-direction: column; gap: 2px; }
.sites-pop-tip { font-size: 11px; color: var(--c-text-3); padding: 2px 6px 6px; }
.sites-pop-item { display: flex; align-items: center; gap: 6px; text-align: left; width: 100%;
  border: 0; background: transparent; color: var(--c-text); padding: 7px 8px; border-radius: var(--r-sm);
  cursor: pointer; font-size: 13px; }
.sites-pop-item:hover { background: var(--c-surface-3); color: var(--c-primary); }

/* 徽章行的尾部 spacer：把「登记字幕」按钮推到行尾 */
.chip-list .spacer { flex: 1 1 12px; }
.dd-rate { display: flex; align-items: center; gap: var(--sp-3); }

.coll-wrap { position: relative; }
.coll-pop {
  position: absolute; top: calc(100% + 6px); left: 0; z-index: 5;
  min-width: 170px; max-height: 220px; overflow-y: auto;
  background: var(--c-surface-2);
  border: 1px solid var(--c-line-strong);
  border-radius: var(--r-md);
  box-shadow: var(--sh-3);
  padding: var(--sp-1);
}
.cp-item { display: block; width: 100%; text-align: left; padding: var(--sp-2); border-radius: var(--r-sm); font-size: var(--fs-md); }
.cp-item:hover { background: var(--c-surface-3); }
.cp-empty { padding: var(--sp-3); font-size: var(--fs-sm); }

.dd-facts {
  display: grid;
  grid-template-columns: 52px 1fr;
  gap: 6px var(--sp-2);
  margin: 0;
  font-size: var(--fs-sm);
  align-items: center;
  padding-top: var(--sp-2);
  border-top: 1px dashed var(--c-line);
}
.dd-facts dt { color: var(--c-text-3); font-size: var(--fs-xs); }
.dd-facts dd { margin: 0; min-width: 0; color: var(--c-text-1); }
.dd-facts a { cursor: pointer; }

/* 女优悬停预览浮层 */
.actress-chip { position: relative; z-index: 1; }
.actress-pop {
  position: fixed; z-index: 1200;
  width: 300px; max-height: 320px; overflow-y: auto;
  padding: var(--sp-3); border-radius: var(--r-md);
  background: var(--c-surface); border: 1px solid var(--c-line-strong);
  box-shadow: var(--sh-2);
  /* 默认从 chip 下方展开；若下方空间不足则向上展开，避免溢出屏幕 */
  max-height: 320px;
}
.actress-pop.up { transform: translateY(-100%); }
.actress-pop:empty { display: none; }
.actress-pop-load { padding: var(--sp-3); text-align: center; }
.ap-head { display: flex; align-items: center; gap: var(--sp-3); }
.ap-av { width: 56px; height: 56px; border-radius: 50%; object-fit: cover; background: var(--c-surface-2); border: 2px solid var(--c-line); flex: none; }
.ap-id { min-width: 0; }
.ap-name { font-weight: 650; color: var(--c-text); }
.ap-alias { font-size: var(--fs-xs); color: var(--c-text-3); margin-top: 1px; }
.ap-attr { display: flex; flex-wrap: wrap; gap: 3px 12px; margin-top: var(--sp-2); font-size: var(--fs-sm); color: var(--c-text-2); }
.ap-text { margin-top: var(--sp-2); font-size: var(--fs-sm); color: var(--c-text-2); line-height: 1.6; white-space: pre-line; max-height: 9em; overflow-y: auto; }
.ap-none { padding: var(--sp-2) 0; font-size: var(--fs-sm); color: var(--c-text-3); }
.fade-enter-active, .fade-leave-active { transition: opacity .12s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

.dd-tabs { padding: 0 var(--sp-5); align-items: center; gap: var(--sp-2); border-top: 1px solid var(--c-line); }
.dd-pane { padding: var(--sp-4) var(--sp-5) var(--sp-6); }

.plot { line-height: 1.8; color: var(--c-text-2); white-space: pre-wrap; }
.plot-src {
  margin: 6px 0 0; font-size: var(--fs-sm); color: var(--c-text-3);
}

/* 详情页轻量 AI：一句话动作 + 就地确认，不进工作台 */
.aiq { margin-top: var(--sp-3); display: flex; flex-direction: column; gap: 8px; }
.aiq-need { margin: 0; font-size: var(--fs-sm); color: var(--c-text-3); line-height: 1.7; }
.aiq-need-box { display: flex; flex-direction: column; gap: 6px; }
/* 对白提取独立块：任何状态常显（本地 Whisper，不依赖 AI 开关与字幕） */
.ai-dialog-sec { border-top: 1px dashed var(--c-line); margin-top: var(--sp-3); padding-top: var(--sp-3); display: flex; flex-direction: column; gap: 8px; }
.ai-dialog-head { display: flex; align-items: baseline; gap: 10px; }
.ai-dialog-head .hint { margin: 0; }
/* 提取时长选择：跟着内容宽度，不要撑满一行 */
.asr-limit { width: auto; min-width: 132px; }
.aiq-bar {
  height: 4px; border-radius: 999px; overflow: hidden;
  background: var(--c-surface-2); border: 1px solid var(--c-line);
}
.aiq-bar > span { display: block; height: 100%; background: var(--c-primary); transition: width .3s; }
.aiq-dlg {
  margin: 0; padding: 8px; max-height: 200px; overflow: auto;
  background: var(--c-surface); border: 1px solid var(--c-line); border-radius: var(--r-sm);
  font-size: 11px; line-height: 1.6; color: var(--c-text-2);
  white-space: pre-wrap; word-break: break-word;
}
.aiq-err { margin: 0; font-size: var(--fs-sm); color: var(--c-err); }
.aiq-compare { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; align-items: start; }
.aiq-compare .hstack { grid-column: 1 / -1; }
.aiq-col {
  min-width: 0; padding: 8px 10px; border-radius: var(--r-md);
  border: 1px solid var(--c-line); background: var(--c-surface);
}
.aiq-col.new { border-color: var(--c-primary); }
.aiq-col-head {
  display: flex; align-items: center; gap: 6px; margin-bottom: 4px;
  font-size: var(--fs-xs); color: var(--c-text-3);
}
.aiq-text {
  margin: 0; font-size: var(--fs-sm); line-height: 1.75; color: var(--c-text-2);
  white-space: pre-wrap; max-height: 220px; overflow: auto;
}
.aiq-ask { display: flex; flex-direction: column; gap: 6px; }
.aiq-ask-row { display: flex; align-items: center; gap: 8px; }
.aiq-ask-row .inp { flex: 1; min-width: 0; }
.aiq-answer { display: flex; flex-direction: column; gap: 6px; }
.aiq-refs { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.aiq-ref {
  padding: 2px 7px; border-radius: 999px; cursor: pointer; font-size: 11px;
  font-variant-numeric: tabular-nums; border: 1px solid var(--c-line);
  background: var(--c-surface); color: var(--c-primary);
}
.aiq-ref:hover { border-color: var(--c-primary); }
@media (max-width: 720px) { .aiq-compare { grid-template-columns: 1fr; } }
/* AI 字幕翻译控件与产物列表 */
.ai-sub { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.ai-sub .inp.tiny { padding: 3px 8px; font-size: var(--fs-sm); }
.ai-sub-bi { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-sm); color: var(--c-text-2); }
.ai-sub-bi input { accent-color: var(--c-primary); }
.ai-subs { margin-top: 6px; }
/* DLNA 投屏弹窗与状态 */
.cast-mask {
  position: fixed; inset: 0; z-index: 1200;
  background: rgba(0,0,0,.62); backdrop-filter: blur(3px);
  display: flex; align-items: center; justify-content: center; padding: 18px;
}
.cast-box {
  width: min(420px, 94vw); max-height: 82vh; overflow: auto;
  padding: 16px; border-radius: 12px;
  background: var(--c-surface); border: 1px solid var(--c-line);
  box-shadow: 0 18px 50px rgba(0,0,0,.5);
}
.cast-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.cast-list { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
.cast-item {
  display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
  padding: 9px 11px; border-radius: 8px; text-align: left; cursor: pointer;
  background: var(--c-surface-2); border: 1px solid var(--c-line); color: var(--c-text);
}
.cast-item:hover { border-color: var(--c-primary); }
.cast-add { display: flex; align-items: center; gap: 8px; margin-top: 10px; }
.cast-add .inp { flex: 1; min-width: 0; }
.cast-on { color: var(--c-primary); font-weight: 600; }
/* AI 已就绪时入口按钮高亮，未启用时保持中性外观 */
.ai-entry-on { border-color: var(--c-primary); color: var(--c-primary); }

.ai-sub-row { display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: var(--fs-sm); }
.note-box {
  margin-top: var(--sp-3); padding: var(--sp-3);
  background: var(--c-surface); border-radius: var(--r-md);
  border-left: 3px solid var(--c-warn);
  font-size: var(--fs-md);
}
.note-box p { color: var(--c-text-2); margin-top: 4px; }
.mt { margin-top: var(--sp-3); }
.mb { margin-bottom: var(--sp-3); }

.edit-form { display: flex; flex-direction: column; gap: var(--sp-3); max-width: 620px; }
.edit-form .two { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }

.pv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: var(--sp-3); }
.pv-grid img { width: 100%; border-radius: var(--r-sm); cursor: zoom-in; background: var(--c-surface-2); }

.sim-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(112px, 1fr)); gap: var(--sp-3); }
.sim { cursor: pointer; }
.sim img { width: 100%; aspect-ratio: 2/3; object-fit: cover; border-radius: var(--r-sm); background: var(--c-surface-2); transition: transform var(--t-base); }
.sim:hover img { transform: translateY(-2px); box-shadow: var(--sh-2); }
.sim-t { font-size: var(--fs-xs); color: var(--c-text-2); margin-top: 4px; }
.sim-play {
  position: absolute; top: 6px; right: 6px;
  width: 30px; height: 30px; border: 0; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: rgba(0, 0, 0, 0.55); color: #fff; font-size: 13px; line-height: 1;
  cursor: pointer; opacity: 0; transform: scale(0.9); transition: opacity var(--t-base), transform var(--t-base);
  backdrop-filter: blur(2px);
}
.sim { position: relative; }
.sim:hover .sim-play, .sim:focus-within .sim-play { opacity: 1; transform: scale(1); }
.sim-play:hover { background: var(--c-accent, #e0457b); }
@media (hover: none) { .sim-play { opacity: 1; transform: scale(1); } }

/* 窄屏：相似推荐侧栏移到主区下方，全宽；各自独立滚动，避免覆盖/重叠 */
@media (max-width: 1100px) {
  .drawer-body { flex-direction: column; }
  .dd-content { flex: 1 1 auto; min-height: 0; }
  .dd-similar-rail { flex: none; width: auto; border-left: 0; border-top: 1px solid var(--c-line); max-height: 42vh; }
  .rail-list { flex-direction: row; flex-wrap: wrap; }
  .rail-list .sim { width: 112px; }
}

.ftable { width: 100%; border-collapse: collapse; font-size: var(--fs-md); }
.ftable th, .ftable td { text-align: left; padding: var(--sp-2); border-bottom: 1px solid var(--c-line); }
.ftable th { color: var(--c-text-3); font-size: var(--fs-sm); font-weight: 500; }
.ftable .fname { max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.danger-zone {
  margin-top: var(--sp-6); padding: var(--sp-3);
  border: 1px solid var(--c-err-soft); border-radius: var(--r-md);
  display: flex; flex-direction: column; gap: var(--sp-2);
}
.danger-zone b { color: var(--c-err); font-size: var(--fs-sm); }

.dd-loading { flex: 1; display: grid; place-items: center; }

.lightbox {
  position: fixed; inset: 0; z-index: var(--z-toast);
  background: rgba(0,0,0,.92);
  display: flex; align-items: center; justify-content: center;
  padding: 16px;
  animation: fade-in var(--t-base);
}
/* 整组预览图：左右翻页 + 计数 + 底部缩略图，避免一次只能看一张 */
.lb-box { display: flex; flex-direction: column; align-items: center; gap: 10px;
  max-width: 100%; max-height: 100%; min-height: 0; }
.lb-main { display: flex; align-items: center; gap: 10px; min-width: 0; }
.lb-main img {
  max-width: min(88vw, 1500px); max-height: calc(100vh - 190px);
  object-fit: contain; border-radius: 6px; cursor: default;
}
.lb-nav {
  flex: 0 0 auto; width: 40px; height: 40px; border-radius: 50%;
  border: 1px solid rgba(255,255,255,.28); background: rgba(0,0,0,.45);
  color: #fff; font-size: 22px; line-height: 1; cursor: pointer;
}
.lb-nav:hover { background: rgba(255,255,255,.16); }
/* 关闭按钮：右上角常驻，任何尺寸屏幕都一眼可见 */
.lb-close {
  position: absolute; top: 14px; right: 16px; width: 44px; height: 44px;
  border-radius: 50%; border: 1px solid rgba(255,255,255,.35);
  background: rgba(0,0,0,.55); color: #fff; font-size: 20px; line-height: 1;
  cursor: pointer; z-index: 2; transition: background var(--t-fast), transform var(--t-fast);
}
.lb-close:hover { background: rgba(255,255,255,.2); transform: scale(1.06); }
.lb-count { color: rgba(255,255,255,.72); font-size: 12px; font-variant-numeric: tabular-nums; }
.lb-thumbs { display: flex; gap: 6px; max-width: min(88vw, 1500px); overflow-x: auto; padding: 2px; }
.lb-thumbs img {
  flex: 0 0 auto; width: 88px; height: 50px; object-fit: cover;
  border-radius: 4px; opacity: .55; cursor: pointer;
  border: 2px solid transparent; transition: opacity .15s, border-color .15s;
}
.lb-thumbs img:hover { opacity: .85; }
.lb-thumbs img.on { opacity: 1; border-color: var(--c-primary); }

@media (max-width: 640px) {
  .lb-nav { width: 34px; height: 34px; font-size: 18px; }
  .lb-main { gap: 6px; }
  .lb-main img { max-width: 82vw; max-height: calc(100vh - 210px); }
  .lb-thumbs img { width: 64px; height: 38px; }
}

@media (max-width: 760px) {
  .dd-top { flex-direction: column; }
  .dd-cover { width: 160px; }
  .edit-form .two { grid-template-columns: 1fr; }
}

/* 极窄屏（竖屏手机）：缩小封面与内边距，播放器铺满，避免横向溢出 */
@media (max-width: 480px) {
  .dd-top { gap: var(--sp-3); }
  .dd-cover { width: 120px; }
  .dd-content { padding: var(--sp-3) var(--sp-3) var(--sp-10); }
  .dd-actions { gap: 5px; }
.dd-g-lbl { min-width: 30px; }
  .dd-actions .act { flex: 1 1 auto; }
}

/* 头部按钮贴近 drawer 顶边，tooltip 改为朝下显示，避免被顶边裁切 */
.drawer-head [data-tip]::after {
  bottom: auto; top: calc(100% + 6px);
}
/* 主区操作按钮（收藏/想看/已看/片单）上方有封面与标题，tooltip 改为朝下，避免被遮挡 */
.dd-actions [data-tip]::after {
  bottom: auto; top: calc(100% + 6px);
}
/* 片单弹窗内的新建行 */
.cp-new { display: flex; gap: var(--sp-2); padding: var(--sp-2) 0 0; margin-top: var(--sp-2); border-top: 1px dashed var(--c-line); }
.cp-new .tag-input { flex: 1; }

/* 标签区头部「管理全部标签」链接 + 通用小按钮 */
.link-btn { border: 0; background: transparent; color: var(--c-primary); cursor: pointer; font: inherit; font-size: var(--fs-sm); padding: 0 2px; }
.link-btn:hover { text-decoration: underline; }
.link-btn.danger { color: var(--c-danger, #e5484d); }
.dd-tags-head { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }

/* 全局标签管理弹窗 */
.tag-mgr { position: fixed; inset: 0; z-index: 1200; background: rgba(0,0,0,.5); display: flex; align-items: center; justify-content: center; padding: var(--sp-4); }
.tag-mgr-box { width: min(520px, 100%); max-height: 80vh; display: flex; flex-direction: column; background: var(--c-surface-2); border: 1px solid var(--c-line-strong); border-radius: var(--r-lg); box-shadow: var(--sh-3); overflow: hidden; }
.tm-head { display: flex; align-items: center; justify-content: space-between; padding: var(--sp-4) var(--sp-5); border-bottom: 1px solid var(--c-line); }
.tag-mgr-box .muted { margin: 0; padding: var(--sp-3) var(--sp-5); }
.tm-list { list-style: none; margin: 0; padding: var(--sp-2) var(--sp-3) var(--sp-4); overflow-y: auto; }
.tm-item { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-2) var(--sp-3); border-radius: var(--r-sm); }
.tm-item:nth-child(odd) { background: var(--c-surface); }
.tm-name { font-weight: 600; }
.tm-actions { margin-left: auto; display: flex; gap: var(--sp-3); }
.tm-input { flex: 1; box-sizing: border-box; background: var(--c-surface-2); border: 1px solid var(--c-line); color: var(--c-text); border-radius: 8px; padding: 6px 10px; font: inherit; font-size: var(--fs-sm); }
.tm-input:focus { outline: none; border-color: var(--c-primary); }
.icon-btn { border: 0; background: transparent; color: var(--c-text-3); cursor: pointer; font-size: 20px; line-height: 1; padding: 0 4px; }
.icon-btn:hover { color: var(--c-text-1); }
</style>
