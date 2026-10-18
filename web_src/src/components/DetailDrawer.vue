<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { state } from '../state.js'
import {
  getMovie, updateMovie, deleteMovie, toggleFlag, playMovie,
  exportNfo, getPreviews, getSimilar, coverUrl, coverThumbUrl, uploadCover, clearCover,
  extractCover,
  listTags, renameTag, deleteTag,
  dlnaDevices, dlnaAddDevice, dlnaCast, dlnaStop,
  getActress, avatarUrl, getConfig, scanMovieSubtitles,
} from '../api.js'
import AddToCollectionBtn from './AddToCollectionBtn.vue'
import {
  toast, confirmDialog, copyText, coverFallback, avatarFallback, fmtSize, fmtDuration,
  fmtDate, fmtAgo, qualityTag,
} from '../utils.js'
import { t } from '../i18n/index.js'
import { useTasks } from '../composables/useTasks.js'
import { useAiAssistant } from '../composables/useAiAssistant.js'

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
/* ---------- AI 工作台：状态与动作集中在 composable 中 ---------- */
const ai = useAiAssistant({
  movieId: computed(() => state.currentId),
  movie: mv,
  toast,
  confirm: confirmDialog,
  onChange: () => refreshMovie(),
})
const aiOpen = ref(false)
const manualSubs = computed(() => (mv.value?.subtitles || []).filter((s) => s.source !== 'ai-translate'))

/** 当前简介来源标签：抓取 / AI 字幕 / AI 元数据 / 人工 */
const plotSourceLabel = computed(() => {
  const s = ai.plotSource
  if (s === 'subtitle') return t('ai.srcAiSubtitle')
  if (s === 'dialog') return t('ai.srcAiDialog')
  if (s === 'meta') return t('ai.srcAiMeta')
  if (s === 'manual') return t('ai.srcManual')
  if (s === 'scraped') return t('ai.srcScraped')
  return ''
})
/** 收藏者最常问的几件事，点一下就能问，不用想措辞 */
const aiQuick = computed(() => [t('ai.askQuick1'), t('ai.askQuick2'), t('ai.askQuick3')])

/* AI 工作台视图：任务卡片 / 提示词审查 */
const aiView = ref('tasks')
function openAi() {
  aiOpen.value = true
  aiView.value = 'tasks'
  ai.refreshStatus()
  ai.loadTracks()
  ai.loadAskHistory()
  ai.loadAsrStatus()
  ai.loadDialog(ai.dialog.show ? 20000 : 0)
  if (!ai.similar.items.length) ai.loadSimilar()
}
function toggleAiView() {
  aiView.value = aiView.value === 'tasks' ? 'prompts' : 'tasks'
  if (aiView.value === 'prompts' && !ai.prompts.items.length) ai.loadPrompts()
}
/** 从调用详情直接跳到对应提示词并进入编辑 */
function openPromptEdit(id) {
  aiView.value = 'prompts'
  const pick = () => ai.startEditPrompt(ai.prompts.items.find((x) => x.id === id) || null)
  if (!ai.prompts.items.length) ai.loadPrompts().then(pick)
  else pick()
}
function fmtTraceTime(at) {
  if (!at) return ''
  try {
    return new Date(at * 1000).toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
  } catch (e) { return '' }
}
/** 简介旁的「AI 重写」：打开工作台并直接开始，少点一次 */
function openAiWithPlot() {
  openAi()
  if (!ai.plot.draft) ai.generatePlot()
}
/** 标签旁的「AI 建议」 */
function openAiWithTags() {
  openAi()
  if (!ai.tags.items.length) ai.suggestTags()
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
function closeAi() { aiOpen.value = false }

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
    ai.loadTracks()
  } catch (e) { /* 静默：AI 卡片内已有错误提示 */ }
}

/** 点击问答的时间戳依据：打开内嵌播放器并跳到该时间 */
function seekToRef(timeText) {
  const sec = ai.timeToSeconds(timeText)
  if (!Number.isFinite(sec) || sec <= 0) return
  if (!playing.value) {
    playing.value = true
    // 播放器是 v-if 挂载的，等它初始化完再发跳转请求
    setTimeout(() => window.dispatchEvent(new CustomEvent('avm-seek', { detail: { seconds: sec } })), 700)
    return
  }
  window.dispatchEvent(new CustomEvent('avm-seek', { detail: { seconds: sec } }))
}

function openSettingsFromAi() {
  closeAi()
  close()
  state.view = 'settings'
}

/** 相似推荐的原因文案 */
function reasonText(r) {
  if (!r) return ''
  const names = (r.names || []).filter(Boolean).join(' / ')
  if (r.kind === 'actress') return names ? t('ai.whyActress', { names }) : t('ai.whyActressBare')
  if (r.kind === 'series') return t('ai.whySeries', { names })
  if (r.kind === 'studio') return t('ai.whyStudio', { names })
  if (r.kind === 'tag') return t('ai.whyTag', { names })
  return t('ai.whyPlot')
}

/* 常用在线站点：详情页一键打开影片在各站点的页面 */
const sites = ref([])
const topSites = computed(() => sites.value.slice(0, 3))
const moreSites = computed(() => sites.value.slice(3))
const sitesMoreOpen = ref(false)
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

function gotoMovie(mid) {
  closeAi()
  state.currentId = mid
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
watch(open, (v) => { if (v) loadSites() })

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
    ai.reset()
    ai.refreshStatus()
    ai.loadTracks()
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
  try { const r = await exportNfo(id.value); toast(t('detail.nfoExported', { path: r.path || 'NFO' }), 'ok') }
  catch (e) { toast(e.message, 'err') }
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
          <!-- 展开「更多站点」时，半透明遮罩盖住详情页其余内容，只露出站点面板 -->
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
                <!-- 主操作 -->
                <div class="dd-actions">
                  <button
                    class="btn primary"
                    :disabled="!hasPlayable"
                    :title="hasPlayable ? '' : $t('detail.noSource')"
                    @click="playing = !playing"
                  >
                    {{ playing ? $t('detail.collapsePlayer') : (progressPct > 0 ? $t('detail.continue', { p: progressPct }) : $t('detail.onlinePlay')) }}
                  </button>
                  <button v-if="canExternal" class="btn" :disabled="!hasPlayable" @click="play">{{ $t('player.external') }}</button>
                  <button class="btn" :disabled="!hasPlayable" :title="$t('detail.castHint')" @click="openCast">
                    📺 <span v-if="!castTo">{{ $t('detail.cast') }}</span>
                    <span v-else class="cast-on">{{ castTo }}</span>
                  </button>
                  <button v-if="castTo" class="btn" @click="stopCast">{{ $t('detail.castStop') }}</button>
                  <button class="btn" :class="{ 'ai-entry-on': ai.ready }" :title="$t('ai.entryTip')" @click="openAi">
                    ✨ {{ $t('ai.title') }}
                  </button>
                  <button v-if="canReveal" class="btn" @click="openFolder" :data-tip="$t('detail.revealFolderTip')">{{ $t('detail.revealFolder') }}</button>
                  <button class="btn icon" :class="{ active: mv.favorite }" @click="flip('favorite')" :data-tip="$t('flag.favorite')">{{ mv.favorite ? '♥' : '♡' }}</button>
                  <button class="btn icon" :class="{ active: mv.watchlist }" @click="flip('watchlist')" :data-tip="$t('flag.watchlist')">⌚</button>
                  <button class="btn icon" :class="{ active: mv.watched }" @click="flip('watched')" :data-tip="$t('flag.watched')">{{ mv.watched ? '●' : '○' }}</button>

                  <AddToCollectionBtn :movie-id="id" variant="detail" />

                  <!-- 打开在线页面：前 3 个平铺，更多 hover 展开 -->
                  <template v-if="sites.length">
                    <button
                      v-for="s in topSites"
                      :key="s.id"
                      class="btn site-flat"
                      :title="s.url"
                      @click="openSite(s)"
                    >
                      <span class="i">🔗</span> {{ s.name || s.url }}
                    </button>
                    <div v-if="moreSites.length" class="dd-sites-more" :class="{ open: sitesMoreOpen }">
                      <button class="btn site-flat" :aria-expanded="sitesMoreOpen" @click.stop="sitesMoreOpen = !sitesMoreOpen">
                        <span class="i">🔗</span> {{ $t('detail.openSitesMore', { n: moreSites.length }) }}
                        <span class="caret">▾</span>
                      </button>
                      <div class="sites-pop">
                        <div class="sites-pop-tip">{{ $t('detail.openSitesTip') }}</div>
                        <button
                          v-for="s in moreSites"
                          :key="s.id"
                          class="sites-pop-item"
                          @click="openSite(s)"
                        >
                          <span class="i">↗</span> {{ s.name || s.url }}
                        </button>
                      </div>
                    </div>
                  </template>
                  <button v-else class="btn" disabled :title="$t('detail.openSitesTip')">
                    <span class="i">🔗</span> {{ $t('detail.openSitesEmpty') }}
                  </button>
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
                </div>

                <!-- 同目录外挂字幕：OCR / 下载来的字幕放进去，点一下登记 -->
                <div class="ai-sub">
                  <button class="btn tiny ghost" :disabled="scanningSubs"
                          :title="$t('detail.scanSubsHint')" @click="scanSidecars">
                    {{ scanningSubs ? $t('detail.scanningSubs') : $t('detail.scanSubs') }}
                  </button>
                  <span v-if="manualSubs.length" class="dim">{{ $t('detail.subsCount', { n: manualSubs.length }) }}</span>
                </div>

                <!-- 已生成的 AI 字幕译文：下载 / 删除（生成入口统一在 AI 工作台） -->
                <div v-if="ai.aiSubs.length" class="ai-subs">
                  <div v-for="s in ai.aiSubs" :key="s.id" class="ai-sub-row">
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
                      <div v-if="!allTagList.length" class="dim">{{ $t('detail.noTags') }}</div>
                      <ul class="tm-list">
                        <li v-for="t in allTagList" :key="t.id" class="tm-item">
                          <template v-if="editingTag === t.name">
                            <input v-model="editingTagNew" class="tm-input" @keydown.enter.prevent="doRenameTag" :placeholder="$t('detail.newName')" />
                            <button class="btn tiny" :disabled="tagMgrBusy || !editingTagNew.trim()" @click="doRenameTag">{{ $t('common.save') }}</button>
                            <button class="btn tiny ghost" @click="editingTag = ''">{{ $t('common.cancel') }}</button>
                          </template>
                          <template v-else>
                            <span class="tm-name">{{ t.name }}</span>
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
              <div class="spacer"></div>
              <button class="btn tiny ghost" @click="doScrape" :disabled="loading">{{ $t('detail.rescrape') }}</button>
              <button class="btn tiny ghost" @click="doNfo">{{ $t('detail.exportNfo') }}</button>
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
                  <div v-if="ai.ready" class="hstack mt wrap">
                    <button class="btn tiny ghost" :title="$t('ai.plotHint')" @click="openAiWithPlot">
                      ✨ {{ mv.plot ? $t('ai.plotRewrite') : $t('ai.plotCreate') }}
                    </button>
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
                  <span class="muted">{{ $t('detail.needFfmpeg') }}</span>
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
    <div v-if="lightboxSrc" class="lightbox" @click.self="closeLightbox">
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

    <!-- AI 工作台：卡片式分区；写库前先预览，避免一键覆盖人工整理的内容 -->
    <div v-if="aiOpen" class="aiw-mask" @click.self="closeAi">
      <section class="aiw-panel" role="dialog" aria-modal="true">
        <header class="aiw-head">
          <div class="aiw-head-title">
            <b>✨ {{ $t('ai.title') }}</b>
            <span v-if="ai.ready" class="badge ok">{{ ai.model || $t('ai.modelFallback') }}</span>
            <span v-else-if="!ai.checking" class="badge warn">{{ $t('ai.notReady') }}</span>
          </div>
          <div class="aiw-head-actions">
            <button v-if="ai.ready" class="btn tiny ghost" :class="{ 'ai-entry-on': aiView === 'prompts' }" @click="toggleAiView">
              {{ aiView === 'prompts' ? $t('ai.backToTasks') : $t('ai.prompts') }}
            </button>
            <button class="btn tiny ghost" @click="closeAi">{{ $t('common.close') }}</button>
          </div>
        </header>

        <!-- 未启用：直接给出口子，不让人在空面板里猜 -->
        <div v-if="ai.checking" class="aiw-empty">
          <p class="hint">{{ $t('ai.working') }}</p>
        </div>
        <div v-else-if="!ai.ready" class="aiw-empty">
          <p>{{ $t('ai.notReadyHint') }}</p>
          <button class="btn primary" @click="openSettingsFromAi">{{ $t('ai.goSettings') }}</button>
        </div>

        <div v-else-if="aiView === 'tasks'" class="aiw-body">
          <!-- 1. 剧情简介：新旧对照，确认才写 -->
          <article class="aiw-card">
            <div class="aiw-card-head">
              <b>{{ $t('ai.plotTitle') }}</b>
              <span v-if="plotSourceLabel" class="badge">{{ plotSourceLabel }}</span>
              <span class="spacer"></span>
              <span v-if="ai.plot.chars" class="muted sm">{{ $t('ai.plotBasis', { n: ai.plot.chars }) }}</span>
            </div>
            <p class="hint">{{ $t('ai.plotHint') }}</p>

            <p v-if="!ai.hasDraft" class="aiw-text">{{ (mv && mv.plot) || $t('ai.plotEmpty') }}</p>

            <div v-else class="aiw-compare">
              <div class="aiw-col">
                <div class="aiw-col-head">{{ $t('ai.oldVersion') }}</div>
                <p class="aiw-text">{{ ai.plot.old || $t('ai.none') }}</p>
              </div>
              <div class="aiw-col new">
                <div class="aiw-col-head">
                  {{ $t('ai.newVersion') }}
                  <span class="badge ok">{{ ai.plot.source === 'subtitle' ? $t('ai.srcSubtitle') : $t('ai.srcMeta') }}</span>
                </div>
                <p class="aiw-text">{{ ai.plot.draft }}</p>
              </div>
            </div>

            <p v-if="ai.plot.track" class="hint">{{ $t('ai.plotTrack', { track: ai.plot.track }) }}</p>
            <p v-if="ai.plot.error" class="aiw-err">{{ ai.plot.error }}</p>

            <div class="aiw-actions">
              <button class="btn tiny primary" :disabled="ai.plot.loading" @click="ai.generatePlot">
                {{ ai.plot.loading ? $t('ai.working') : (ai.hasDraft ? $t('ai.regenerate') : $t('ai.generate')) }}
              </button>
              <template v-if="ai.hasDraft">
                <button class="btn tiny" @click="ai.applyPlot">{{ $t('ai.useNew') }}</button>
                <button class="btn tiny ghost" @click="ai.discardPlot">{{ $t('ai.keepOld') }}</button>
              </template>
            </div>

            <!-- 没有字幕时的第二条输入：语音对白（只喂给 AI，不产出字幕） -->
            <div v-if="!ai.hasSubtitles" class="aiw-dialog">
              <div class="aiw-row">
                <b>{{ $t('ai.dialogTitle') }}</b>
                <span v-if="ai.dialog.status === 'running'" class="badge">{{ ai.dialog.percent }}%</span>
                <span v-else-if="ai.dialog.has_text" class="badge ok">{{ $t('ai.dialogDone', { n: ai.dialog.segments }) }}</span>
                <span v-else-if="ai.dialog.status === 'error'" class="badge err">{{ $t('ai.dialogFailed') }}</span>
                <span class="spacer"></span>
                <button v-if="ai.dialog.status !== 'running' && !ai.dialog.has_text"
                        class="btn tiny"
                        :disabled="!ai.asr.lib_available || !ai.asr.model_available"
                        @click="ai.startDialog">{{ $t('ai.extractDialog') }}</button>
                <button v-if="ai.dialog.has_text" class="btn tiny ghost" @click="ai.toggleDialogText">
                  {{ ai.dialog.show ? $t('ai.hideDialog') : $t('ai.viewDialog') }}
                </button>
                <button v-if="ai.dialog.has_text" class="btn tiny ghost" @click="ai.deleteDialog">{{ $t('common.delete') }}</button>
              </div>

              <p v-if="ai.dialog.status === 'running'" class="hint">{{ ai.dialog.message || $t('ai.dialogRunning') }}</p>
              <div v-if="ai.dialog.status === 'running'" class="aiw-bar">
                <span :style="{ width: (ai.dialog.percent || 0) + '%' }"></span>
              </div>

              <p v-if="!ai.asr.lib_available" class="hint">{{ $t('ai.dialogNoLib') }}</p>
              <p v-else-if="!ai.asr.model_available" class="hint">{{ $t('ai.dialogNoModel', { dir: ai.asr.model_dir }) }}</p>
              <p v-else-if="ai.dialog.status === 'idle' && !ai.dialog.has_text" class="hint">{{ $t('ai.extractDialogHint') }}</p>
              <p v-if="ai.dialog.error" class="aiw-err">{{ ai.dialog.error }}</p>

              <pre v-if="ai.dialog.show && ai.dialog.text" class="aiw-pre">{{ ai.dialog.text }}</pre>
            </div>

            <!-- 审查：这次到底发了什么、模型原样回了什么 -->
            <div class="aiw-trace">
              <button class="link-btn" @click="ai.toggleTrace('plot')">
                {{ ai.traces.open === 'plot' ? $t('ai.traceHide') : $t('ai.traceShow') }}
              </button>
              <button class="link-btn" @click="openPromptEdit(ai.plot.source === 'subtitle' ? 'plot.subtitle' : 'synopsis')">
                {{ $t('ai.editPrompt') }}
              </button>
              <span v-if="ai.traces.summary.plot" class="muted sm">
                {{ ai.traces.summary.plot.model }} · {{ ai.traces.summary.plot.ms }} ms
              </span>
            </div>
            <div v-if="ai.traces.open === 'plot'" class="aiw-trace-box">
              <p v-if="ai.traces.loadingKey === 'plot'" class="hint">{{ $t('ai.traceLoading') }}</p>
              <p v-else-if="ai.traces.error" class="aiw-err">{{ ai.traces.error }}</p>
              <p v-else-if="!ai.traceItems('plot').length" class="hint">{{ $t('ai.traceEmpty') }}</p>
              <div v-for="tr in ai.traceItems('plot')" :key="tr.id" class="aiw-trace-item">
                <div class="aiw-trace-meta">
                  <span class="badge">{{ tr.purpose }}</span>
                  <span>{{ tr.model }}</span>
                  <span>{{ tr.ms }} ms</span>
                  <span v-if="tr.cached" class="badge ok">{{ $t('ai.traceCached') }}</span>
                  <span class="muted sm">{{ fmtTraceTime(tr.at) }}</span>
                </div>
                <div class="hint">{{ $t('ai.traceSystem') }}</div>
                <pre class="aiw-pre">{{ tr.messages[0] ? tr.messages[0].content : '' }}</pre>
                <div class="hint">{{ $t('ai.traceUser') }}</div>
                <pre class="aiw-pre">{{ tr.messages[1] ? tr.messages[1].content : '' }}</pre>
                <div class="hint">{{ tr.error ? $t('ai.traceError') : $t('ai.traceReply') }}</div>
                <pre class="aiw-pre" :class="{ err: tr.error }">{{ tr.error || tr.reply }}</pre>
              </div>
            </div>
          </article>

          <!-- 2. 标签：逐条勾选；已存在的默认不勾，避免无意义写入 -->
          <article class="aiw-card">
            <div class="aiw-card-head">
              <b>{{ $t('ai.tagsTitle') }}</b>
              <span class="spacer"></span>
              <span class="muted sm">{{ $t('ai.tagsCurrent', { n: (mv && mv.tags ? mv.tags.length : 0) }) }}</span>
            </div>
            <p class="hint">{{ $t('ai.tagsHint') }}</p>

            <div v-if="ai.tags.items.length" class="aiw-chips">
              <button v-for="it in ai.tags.items" :key="it.name"
                      class="chip aiw-chip"
                      :class="{ on: it.picked, existing: it.existing }"
                      @click="ai.toggleTag(it.name)">
                <span v-if="it.picked">✓ </span>{{ it.name }}
                <em v-if="it.existing">{{ $t('ai.exists') }}</em>
              </button>
            </div>
            <p v-if="ai.tags.error" class="aiw-err">{{ ai.tags.error }}</p>

            <div class="aiw-actions">
              <button class="btn tiny primary" :disabled="ai.tags.loading" @click="ai.suggestTags">
                {{ ai.tags.loading ? $t('ai.working') : $t('ai.tagsRun') }}
              </button>
              <template v-if="ai.tags.items.length">
                <button class="btn tiny ghost" @click="ai.pickAllTags(true)">{{ $t('ai.selectAll') }}</button>
                <button class="btn tiny ghost" @click="ai.pickAllTags(false)">{{ $t('ai.clearSel') }}</button>
                <button class="btn tiny" :disabled="!ai.pickedTags.length" @click="ai.applyTags">
                  {{ $t('ai.tagsAdd', { n: ai.pickedTags.length }) }}
                </button>
              </template>
            </div>

            <div class="aiw-trace">
              <button class="link-btn" @click="ai.toggleTrace('tags')">
                {{ ai.traces.open === 'tags' ? $t('ai.traceHide') : $t('ai.traceShow') }}
              </button>
              <button class="link-btn" @click="openPromptEdit('tags')">{{ $t('ai.editPrompt') }}</button>
              <span v-if="ai.traces.summary.tags" class="muted sm">
                {{ ai.traces.summary.tags.model }} · {{ ai.traces.summary.tags.ms }} ms
              </span>
            </div>
            <div v-if="ai.traces.open === 'tags'" class="aiw-trace-box">
              <p v-if="ai.traces.loadingKey === 'tags'" class="hint">{{ $t('ai.traceLoading') }}</p>
              <p v-else-if="ai.traces.error" class="aiw-err">{{ ai.traces.error }}</p>
              <p v-else-if="!ai.traceItems('tags').length" class="hint">{{ $t('ai.traceEmpty') }}</p>
              <div v-for="tr in ai.traceItems('tags')" :key="tr.id" class="aiw-trace-item">
                <div class="aiw-trace-meta">
                  <span class="badge">{{ tr.purpose }}</span>
                  <span>{{ tr.model }}</span>
                  <span>{{ tr.ms }} ms</span>
                  <span v-if="tr.cached" class="badge ok">{{ $t('ai.traceCached') }}</span>
                  <span class="muted sm">{{ fmtTraceTime(tr.at) }}</span>
                </div>
                <div class="hint">{{ $t('ai.traceSystem') }}</div>
                <pre class="aiw-pre">{{ tr.messages[0] ? tr.messages[0].content : '' }}</pre>
                <div class="hint">{{ $t('ai.traceUser') }}</div>
                <pre class="aiw-pre">{{ tr.messages[1] ? tr.messages[1].content : '' }}</pre>
                <div class="hint">{{ tr.error ? $t('ai.traceError') : $t('ai.traceReply') }}</div>
                <pre class="aiw-pre" :class="{ err: tr.error }">{{ tr.error || tr.reply }}</pre>
              </div>
            </div>
          </article>

          <!-- 3. 字幕翻译：没有字幕就直接说明原因，而不是给个点了报错的按钮 -->
          <article class="aiw-card">
            <div class="aiw-card-head">
              <b>{{ $t('ai.subsTitle') }}</b>
              <span class="spacer"></span>
              <span class="muted sm">
                {{ ai.hasSubtitles ? $t('ai.subsTracks', { n: ai.subs.tracks.length || ai.aiSubs.length }) : $t('ai.subsNone') }}
              </span>
            </div>
            <div class="aiw-row">
              <select v-model="ai.subs.target" class="inp tiny">
                <option value="zh-Hans">{{ $t('detail.aiTargetHans') }}</option>
                <option value="zh-Hant">{{ $t('detail.aiTargetHant') }}</option>
                <option value="en">{{ $t('detail.aiTargetEn') }}</option>
              </select>
              <label class="aiw-check">
                <input type="checkbox" v-model="ai.subs.bilingual" />
                <span>{{ $t('detail.aiBilingual') }}</span>
              </label>
              <button class="btn tiny primary" :disabled="ai.subs.loading || !ai.hasSubtitles" @click="ai.translateSubs">
                {{ ai.subs.loading ? $t('ai.working') : $t('detail.aiTranslate') }}
              </button>
            </div>
            <p v-if="!ai.hasSubtitles && !ai.probing" class="hint">{{ $t('ai.subsNoSource') }}</p>
            <div v-if="ai.aiSubs.length" class="aiw-list">
              <div v-for="s in ai.aiSubs" :key="s.id" class="aiw-list-row">
                <span class="mono">{{ s.lang || 'AI' }}</span>
                <span class="ellipsis dim">{{ s.filename }}</span>
                <button class="btn tiny ghost" @click="ai.downloadSub(s)">{{ $t('common.download') }}</button>
                <button class="btn tiny ghost" @click="ai.removeAiSub(s)">{{ $t('common.delete') }}</button>
              </div>
            </div>
            <p v-if="ai.subs.error" class="aiw-err">{{ ai.subs.error }}</p>

            <div class="aiw-trace">
              <button class="link-btn" @click="ai.toggleTrace('subs')">
                {{ ai.traces.open === 'subs' ? $t('ai.traceHide') : $t('ai.traceShow') }}
              </button>
              <button class="link-btn" @click="openPromptEdit('subtitle.translate')">{{ $t('ai.editPrompt') }}</button>
              <span v-if="ai.traces.summary.subs" class="muted sm">
                {{ ai.traces.summary.subs.model }} · {{ ai.traces.summary.subs.ms }} ms
              </span>
            </div>
            <div v-if="ai.traces.open === 'subs'" class="aiw-trace-box">
              <p v-if="ai.traces.loadingKey === 'subs'" class="hint">{{ $t('ai.traceLoading') }}</p>
              <p v-else-if="ai.traces.error" class="aiw-err">{{ ai.traces.error }}</p>
              <p v-else-if="!ai.traceItems('subs').length" class="hint">{{ $t('ai.traceEmpty') }}</p>
              <div v-for="tr in ai.traceItems('subs')" :key="tr.id" class="aiw-trace-item">
                <div class="aiw-trace-meta">
                  <span class="badge">{{ tr.purpose }}</span>
                  <span>{{ tr.model }}</span>
                  <span>{{ tr.ms }} ms</span>
                  <span v-if="tr.cached" class="badge ok">{{ $t('ai.traceCached') }}</span>
                  <span class="muted sm">{{ fmtTraceTime(tr.at) }}</span>
                </div>
                <div class="hint">{{ $t('ai.traceSystem') }}</div>
                <pre class="aiw-pre">{{ tr.messages[0] ? tr.messages[0].content : '' }}</pre>
                <div class="hint">{{ $t('ai.traceUser') }}</div>
                <pre class="aiw-pre">{{ tr.messages[1] ? tr.messages[1].content : '' }}</pre>
                <div class="hint">{{ tr.error ? $t('ai.traceError') : $t('ai.traceReply') }}</div>
                <pre class="aiw-pre" :class="{ err: tr.error }">{{ tr.error || tr.reply }}</pre>
              </div>
            </div>
          </article>

          <!-- 4. 剧情问答：时间戳可点击直接跳到片中的那一刻 -->
          <article class="aiw-card">
            <div class="aiw-card-head">
              <b>{{ $t('ai.askTitle') }}</b>
              <span class="spacer"></span>
              <span v-if="ai.ask.track" class="muted sm">{{ ai.ask.track }}</span>
            </div>
            <p class="hint">{{ $t('ai.askHint') }}</p>

            <div class="aiw-row">
              <input v-model="ai.ask.question" class="inp" :placeholder="$t('ai.askPh')" @keyup.enter="ai.sendAsk" />
              <button class="btn tiny primary" :disabled="ai.ask.loading || !ai.ask.question.trim()" @click="ai.sendAsk">
                {{ ai.ask.loading ? $t('ai.working') : $t('ai.askRun') }}
              </button>
            </div>
            <div class="aiw-chips">
              <button v-for="q in aiQuick" :key="q" class="chip" :disabled="!ai.hasSubtitles" @click="ai.ask.question = q; ai.sendAsk()">{{ q }}</button>
            </div>

            <p v-if="!ai.hasSubtitles && !ai.probing" class="hint">{{ $t('ai.askNoSubs') }}</p>
            <p v-if="ai.ask.error" class="aiw-err">{{ ai.ask.error }}</p>

            <div v-if="ai.ask.answer" class="aiw-answer">
              <p class="aiw-text">{{ ai.ask.answer }}</p>
              <div v-if="ai.ask.refs.length" class="aiw-refs">
                <button v-for="(r, i) in ai.ask.refs" :key="i" class="aiw-ref" :title="r.text" @click="seekToRef(r.time)">
                  [{{ r.time }}]
                </button>
                <span class="muted sm">{{ $t('ai.refHint') }}</span>
              </div>
            </div>

            <div v-if="ai.ask.history.length" class="aiw-history">
              <div class="hint">{{ $t('ai.askHistory') }}</div>
              <button v-for="h in ai.ask.history" :key="h.id"
                      class="aiw-hist-row" :class="{ on: ai.ask.openId === h.id }"
                      @click="ai.toggleHistory(h)">
                <span class="ellipsis">{{ h.question }}</span>
                <span class="muted sm">{{ fmtAgo(h.created_at) }}</span>
              </button>
            </div>

            <div class="aiw-trace">
              <button class="link-btn" @click="ai.toggleTrace('ask')">
                {{ ai.traces.open === 'ask' ? $t('ai.traceHide') : $t('ai.traceShow') }}
              </button>
              <button class="link-btn" @click="openPromptEdit('plot.qa')">{{ $t('ai.editPrompt') }}</button>
              <span v-if="ai.traces.summary.ask" class="muted sm">
                {{ ai.traces.summary.ask.model }} · {{ ai.traces.summary.ask.ms }} ms
              </span>
            </div>
            <div v-if="ai.traces.open === 'ask'" class="aiw-trace-box">
              <p v-if="ai.traces.loadingKey === 'ask'" class="hint">{{ $t('ai.traceLoading') }}</p>
              <p v-else-if="ai.traces.error" class="aiw-err">{{ ai.traces.error }}</p>
              <p v-else-if="!ai.traceItems('ask').length" class="hint">{{ $t('ai.traceEmpty') }}</p>
              <div v-for="tr in ai.traceItems('ask')" :key="tr.id" class="aiw-trace-item">
                <div class="aiw-trace-meta">
                  <span class="badge">{{ tr.purpose }}</span>
                  <span>{{ tr.model }}</span>
                  <span>{{ tr.ms }} ms</span>
                  <span v-if="tr.cached" class="badge ok">{{ $t('ai.traceCached') }}</span>
                  <span class="muted sm">{{ fmtTraceTime(tr.at) }}</span>
                </div>
                <div class="hint">{{ $t('ai.traceSystem') }}</div>
                <pre class="aiw-pre">{{ tr.messages[0] ? tr.messages[0].content : '' }}</pre>
                <div class="hint">{{ $t('ai.traceUser') }}</div>
                <pre class="aiw-pre">{{ tr.messages[1] ? tr.messages[1].content : '' }}</pre>
                <div class="hint">{{ tr.error ? $t('ai.traceError') : $t('ai.traceReply') }}</div>
                <pre class="aiw-pre" :class="{ err: tr.error }">{{ tr.error || tr.reply }}</pre>
              </div>
            </div>
          </article>

          <!-- 5. 相似推荐：带「为什么相似」，可解释才敢信 -->
          <article class="aiw-card">
            <div class="aiw-card-head">
              <b>{{ $t('ai.simTitle') }}</b>
              <span class="spacer"></span>
              <button class="btn tiny ghost" :disabled="ai.similar.loading" @click="ai.loadSimilar">{{ $t('ai.simRefresh') }}</button>
            </div>
            <div v-if="ai.similar.items.length" class="aiw-sim">
              <div v-for="s in ai.similar.items" :key="s.id" class="aiw-sim-card">
                <button class="aiw-sim-open" @click="gotoMovie(s.id)">
                  <img v-if="s.cover" :src="coverThumbUrl(s.id)" alt="" loading="lazy" />
                  <span class="ellipsis">{{ s.code || s.title }}</span>
                </button>
                <div v-if="(s.reasons || []).length" class="aiw-reasons">
                  <span v-for="(r, i) in s.reasons" :key="i" class="aiw-reason">{{ reasonText(r) }}</span>
                </div>
              </div>
            </div>
            <p v-else-if="ai.similar.loading" class="hint">{{ $t('ai.simLoading') }}</p>
            <p v-else-if="!ai.similar.error" class="hint">{{ $t('ai.simEmpty') }}</p>
            <p v-if="ai.similar.error" class="aiw-err">{{ ai.similar.error }}</p>
          </article>
        </div>

        <!-- 提示词审查 / 修改：保存后立即生效，可随时恢复默认 -->
        <div v-else class="aiw-body">
          <p class="hint">{{ $t('ai.promptsHint') }}</p>
          <p v-if="ai.prompts.loading" class="hint">{{ $t('ai.working') }}</p>
          <p v-if="ai.prompts.error" class="aiw-err">{{ ai.prompts.error }}</p>

          <article v-for="p in ai.prompts.items" :key="p.id" class="aiw-card">
            <div class="aiw-card-head">
              <b>{{ p.title }}</b>
              <span class="badge" :class="{ ok: p.overridden }">
                {{ p.overridden ? $t('ai.customized') : $t('ai.builtin') }}
              </span>
              <span class="spacer"></span>
              <span class="muted sm">{{ p.id }} · {{ p.version || '-' }}</span>
            </div>
            <p class="hint">{{ p.desc }}</p>
            <p class="aiw-vars">{{ $t('ai.promptVars') }}：{{ p.variables.length ? p.variables.join(' / ') : $t('ai.none') }}</p>

            <template v-if="ai.prompts.editingId === p.id">
              <textarea v-model="ai.prompts.draft" class="inp aiw-ta" rows="6" spellcheck="false"></textarea>
              <div class="aiw-actions">
                <button class="btn tiny primary" :disabled="ai.prompts.saving" @click="ai.savePrompt">
                  {{ ai.prompts.saving ? $t('ai.working') : $t('common.save') }}
                </button>
                <button class="btn tiny ghost" @click="ai.cancelPrompt">{{ $t('common.cancel') }}</button>
                <button class="btn tiny ghost" :disabled="!p.overridden" @click="ai.restorePrompt(p.id)">
                  {{ $t('ai.restoreDefault') }}
                </button>
              </div>
            </template>
            <template v-else>
              <p class="aiw-text">{{ p.system }}</p>
              <div class="aiw-actions">
                <button class="btn tiny" @click="ai.startEditPrompt(p)">{{ $t('ai.editPrompt') }}</button>
                <button class="btn tiny ghost" :disabled="!p.overridden" @click="ai.restorePrompt(p.id)">
                  {{ $t('ai.restoreDefault') }}
                </button>
              </div>
            </template>
          </article>
        </div>
      </section>
    </div>

    <!-- DLNA 投屏：选设备（组播发现 + 按 IP 探测） -->
    <div v-if="castOpen" class="cast-mask" @click.self="castOpen = false">
      <div class="cast-box">
        <div class="cast-head">
          <b>{{ $t('detail.castTitle') }}</b>
          <button class="btn tiny ghost" @click="castOpen = false">{{ $t('common.cancel') }}</button>
        </div>
        <p class="hint">{{ $t('detail.castHint') }}</p>
        <div class="hstack" style="gap:6px;margin:6px 0">
          <input v-model="castIp" class="inp" :placeholder="$t('detail.castIpPh')" @keyup.enter="addCastDevice" />
          <button class="btn tiny" :disabled="castBusy || !castIp.trim()" @click="addCastDevice">{{ $t('detail.castAdd') }}</button>
        </div>
        <div class="cast-list">
          <button v-for="d in castDevices" :key="d.usn" class="cast-item" :disabled="castBusy" @click="doCast(d)">
            <b class="ellipsis">{{ d.name }}</b>
            <span class="muted sm ellipsis">{{ d.manufacturer }} {{ d.model }}</span>
          </button>
          <p v-if="!castDevices.length && !castBusy" class="muted sm">{{ $t('detail.castNone') }}</p>
          <p v-if="castBusy" class="muted sm">{{ $t('detail.castScanning') }}</p>
        </div>
      </div>
    </div>

    <!-- 女优悬停预览浮层（鼠标移到女优名字上显示基本信息） -->
    <transition name="fade">
      <div
        v-if="hoverShow"
        class="actress-pop"
        :class="{ up: hoverDir === 'up' }"
        :style="{ left: hoverPos.x + 'px', top: hoverPos.y + 'px' }"
        @mouseenter="cancelHoverEnd"
        @mouseleave="scheduleHoverEnd"
      >
        <div v-if="hoverLoading" class="actress-pop-load"><span class="spinner sm"></span></div>
        <template v-else-if="hoverInfo">
          <div class="ap-head">
            <img v-if="hoverAvatar" class="ap-av" :src="hoverAvatar" alt="" @error="avatarFallback" />
            <div class="ap-id">
              <div class="ap-name">{{ hoverInfo.name || hoverActress }}</div>
              <div v-if="hoverInfo.alias" class="ap-alias">{{ hoverInfo.alias }}</div>
            </div>
          </div>
          <div v-if="hoverInfo.birthday || hoverInfo.height || hoverMeasure" class="ap-attr">
            <span v-if="hoverInfo.birthday">{{ hoverInfo.birthday }}<template v-if="hoverAge != null">（{{ hoverAge }}岁）</template></span>
            <span v-if="hoverInfo.height">{{ hoverInfo.height }}cm</span>
            <span v-if="hoverMeasure">{{ hoverMeasure }}</span>
            <span v-if="hoverInfo.cup">罩杯 {{ hoverInfo.cup }}</span>
            <span v-if="hoverInfo.birthplace">{{ hoverInfo.birthplace }}</span>
          </div>
          <p v-if="hoverInfo.profile" class="ap-text">{{ hoverInfo.profile }}</p>
        </template>
        <div v-else class="ap-none">{{ $t('detail.noActressInfo') }}</div>
      </div>
    </transition>
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
.cov-acts { display: flex; gap: var(--sp-2); margin-top: var(--sp-2); }

.dd-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.dd-actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.site-flat { white-space: nowrap; font-size: 12px; padding: 5px 9px; }
.dd-sites-more { position: relative; z-index: 41; }
.dd-sites-more .caret { font-size: 10px; opacity: .7; margin-left: 2px; }
/* 更多：点击展开，半透明遮罩（.sites-shade）盖住详情页其余内容，只露出站点面板 */
.dd-sites-more .sites-pop { display: none; }
.dd-sites-more.open .sites-pop { display: flex; }
.sites-shade { position: absolute; inset: 0; z-index: 40; background: rgba(0,0,0,.34); }
.sites-pop { position: absolute; top: 100%; left: 0; z-index: 60; min-width: 220px;
  background: var(--c-bg-pop); border: 1px solid var(--c-border); border-radius: 10px;
  box-shadow: 0 10px 30px rgba(0,0,0,.28); padding: 6px var(--sp-2) var(--sp-2); flex-direction: column; gap: 2px; }
.sites-pop-tip { font-size: 11px; color: var(--c-text-3); padding: 2px 6px 6px; }
.sites-pop-item { display: flex; align-items: center; gap: 6px; text-align: left; width: 100%;
  border: 0; background: transparent; color: var(--c-text-1); padding: 7px 8px; border-radius: 7px; cursor: pointer; font-size: 13px; }
.sites-pop-item:hover { background: var(--c-bg-hover); color: var(--c-primary); }
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
.cast-on { color: var(--c-primary); font-weight: 600; }
/* AI 已就绪时入口按钮高亮，未启用时保持中性外观 */
.ai-entry-on { border-color: var(--c-primary); color: var(--c-primary); }

/* ===== AI 工作台：卡片式分区 ===== */
.aiw-mask {
  position: fixed; inset: 0; z-index: 1300;
  background: rgba(0, 0, 0, .55); backdrop-filter: blur(3px);
  display: flex; align-items: center; justify-content: center; padding: 16px;
}
.aiw-panel {
  width: min(560px, 96vw); max-height: 88vh;
  display: flex; flex-direction: column; overflow: hidden;
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--r-lg); box-shadow: 0 24px 60px rgba(0, 0, 0, .5);
}
.aiw-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 14px; border-bottom: 1px solid var(--c-line);
}
.aiw-head-title { display: flex; align-items: center; gap: 8px; }
.aiw-empty {
  padding: 28px 18px; display: flex; flex-direction: column;
  align-items: center; gap: 14px; text-align: center;
}
.aiw-body {
  padding: 12px 14px 16px; overflow: auto;
  display: flex; flex-direction: column; gap: 12px;
}
.aiw-card {
  display: flex; flex-direction: column; gap: 8px;
  padding: 10px 12px; border-radius: var(--r-md);
  border: 1px solid var(--c-line); background: var(--c-surface-2);
}
.aiw-card-head { display: flex; align-items: center; gap: 8px; font-size: var(--fs-sm); }
.aiw-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.aiw-row .inp { flex: 1; min-width: 150px; }
.aiw-check { display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-sm); color: var(--c-text-2); }
.aiw-text {
  margin: 0; font-size: var(--fs-sm); line-height: 1.75; color: var(--c-text-2);
  white-space: pre-wrap; max-height: 220px; overflow: auto;
}
.aiw-compare { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
@media (max-width: 640px) { .aiw-compare { grid-template-columns: 1fr; } }
.aiw-col {
  min-width: 0; padding: 8px; border-radius: var(--r-sm);
  border: 1px solid var(--c-line); background: var(--c-surface);
}
.aiw-col.new { border-color: var(--c-primary); }
.aiw-col-head {
  display: flex; align-items: center; gap: 6px;
  margin-bottom: 4px; font-size: var(--fs-xs); color: var(--c-text-3);
}
.aiw-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.aiw-head-actions { display: flex; align-items: center; gap: 6px; }
.aiw-vars { margin: 0; font-size: var(--fs-xs); color: var(--c-text-3); }
.aiw-ta {
  width: 100%; resize: vertical; font-size: 12px; line-height: 1.6;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
/* 语音对白区块：与剧情简介同卡，但用虚线分隔 */
.aiw-dialog {
  display: flex; flex-direction: column; gap: 6px;
  border-top: 1px dashed var(--c-line); padding-top: 8px;
}
.aiw-bar {
  height: 4px; border-radius: 999px; overflow: hidden;
  background: var(--c-surface); border: 1px solid var(--c-line);
}
.aiw-bar span { display: block; height: 100%; background: var(--c-primary); transition: width .3s; }
/* 调用详情：提示词 / 原始返回，等宽可滚动，方便逐字核对 */
.aiw-trace { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.aiw-trace-box {
  display: flex; flex-direction: column; gap: 6px;
  border-top: 1px dashed var(--c-line); padding-top: 8px;
}
.aiw-trace-item {
  display: flex; flex-direction: column; gap: 4px;
  padding: 8px; border-radius: var(--r-sm);
  border: 1px solid var(--c-line); background: var(--c-surface);
}
.aiw-trace-meta {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: var(--fs-xs); color: var(--c-text-2);
}
.aiw-pre {
  margin: 0; padding: 6px 8px; border-radius: var(--r-sm);
  max-height: 180px; overflow: auto;
  background: var(--c-surface-2); color: var(--c-text-2);
  font-size: 11px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.aiw-pre.err { color: var(--c-err); }
.aiw-err { margin: 0; font-size: var(--fs-sm); color: var(--c-err); }
.aiw-chips { display: flex; gap: 6px; flex-wrap: wrap; }
.aiw-chip { display: inline-flex; align-items: center; gap: 4px; }
.aiw-chip em { font-style: normal; font-size: 10px; opacity: .6; }
.aiw-chip.existing { opacity: .7; }
.aiw-list { display: flex; flex-direction: column; gap: 4px; }
.aiw-list-row { display: flex; align-items: center; gap: 8px; font-size: var(--fs-sm); }
.aiw-list-row .ellipsis { flex: 1; min-width: 0; }
.aiw-answer {
  display: flex; flex-direction: column; gap: 6px;
  border-top: 1px dashed var(--c-line); padding-top: 8px;
}
.aiw-refs { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.aiw-ref {
  padding: 2px 7px; border-radius: 999px; cursor: pointer;
  font-size: 11px; font-variant-numeric: tabular-nums;
  border: 1px solid var(--c-line); background: var(--c-surface); color: var(--c-primary);
}
.aiw-ref:hover { border-color: var(--c-primary); }
.aiw-history {
  display: flex; flex-direction: column; gap: 4px;
  border-top: 1px dashed var(--c-line); padding-top: 8px;
}
.aiw-hist-row {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 5px 6px; border-radius: var(--r-sm); cursor: pointer; text-align: left;
  font-size: var(--fs-sm); color: var(--c-text-2);
  background: transparent; border: 1px solid transparent;
}
.aiw-hist-row:hover { background: var(--c-surface); }
.aiw-hist-row.on { border-color: var(--c-primary); }
.aiw-sim { display: grid; grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 8px; }
.aiw-sim-card { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.aiw-sim-open {
  display: flex; flex-direction: column; gap: 4px; padding: 4px; cursor: pointer;
  border: 1px solid var(--c-line); border-radius: var(--r-md);
  background: var(--c-surface); color: var(--c-text-2);
  font-size: 11px; text-align: left;
}
.aiw-sim-open:hover { border-color: var(--c-primary); }
.aiw-sim-open img { width: 100%; aspect-ratio: 3/4; object-fit: cover; border-radius: var(--r-sm); }
.aiw-reasons { display: flex; gap: 3px; flex-wrap: wrap; }
.aiw-reason {
  padding: 1px 5px; border-radius: 999px; font-size: 10px;
  background: var(--c-surface-2); border: 1px solid var(--c-line); color: var(--c-text-3);
}
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
  .dd-actions { gap: var(--sp-1); }
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
