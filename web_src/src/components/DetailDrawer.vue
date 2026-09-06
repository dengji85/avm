<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { state } from '../state.js'
import {
  getMovie, updateMovie, deleteMovie, toggleFlag, playMovie,
  exportNfo, getPreviews, getSimilar, coverUrl, coverThumbUrl, uploadCover, clearCover,
  extractCover,
  listTags, renameTag, deleteTag,
  aiGenerateSynopsis, aiSuggestTags, aiStatus,
  getActress, avatarUrl, getConfig,
} from '../api.js'
import AddToCollectionBtn from './AddToCollectionBtn.vue'
import {
  toast, confirmDialog, copyText, coverFallback, avatarFallback, fmtSize, fmtDuration,
  fmtDate, fmtAgo, qualityTag,
} from '../utils.js'
import { t } from '../i18n/index.js'
import { useTasks } from '../composables/useTasks.js'

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
const editing = ref(false)
const draft = ref({})
const lightbox = ref('')
const aiReady = ref(false)
const aiBusy = ref(false)

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

async function checkAi() {
  try { const r = await aiStatus(); aiReady.value = !!r.enabled } catch { aiReady.value = false }
}
async function doAiSynopsis() {
  aiBusy.value = true
  try {     const r = await aiGenerateSynopsis(id.value); mv.value.plot = r.plot; toast(t('detail.aiSynopsisDone'), 'ok') }
  catch (e) { toast(e.message || t('detail.aiSynopsisFail'), 'err') }
  finally { aiBusy.value = false }
}
async function doAiTags() {
  aiBusy.value = true
  try {
    const r = await aiSuggestTags(id.value)
    const cur = (mv.value.tags || []).slice()
    const merged = [...new Set([...cur, ...(r.tags || [])])]
    mv.value.tags = merged
    await updateMovie(id.value, { tags: merged })
    toast(t('detail.aiTagsDone'), 'ok')
  } catch (e) { toast(e.message || t('detail.aiTagsFail'), 'err') }
  finally { aiBusy.value = false }
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
    checkAi()
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
  lightbox.value = ''
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

async function doDelete(withFile) {
  const ok = await confirmDialog(
    withFile ? t('detail.delWithFile') : t('detail.delFromDb'),
    withFile
      ? t('detail.delWithFileDesc', { path: mainFile.value?.path || '' })
      : t('detail.delFromDbDesc'),
    { danger: true, okText: withFile ? t('detail.permanent') : t('detail.remove') },
  )
  if (!ok) return
  try {
    await deleteMovie(id.value, withFile)
    toast(t('detail.deleted'), 'ok')
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
    lightbox.value ? (lightbox.value = '') : close()
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
                <img :src="coverSrc" alt="" @error="coverFallback" @click="lightbox = coverSrc" />
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
                  <button class="btn" :disabled="!hasPlayable" @click="play">{{ $t('player.external') }}</button>
                  <button class="btn" @click="openFolder" :data-tip="$t('detail.revealFolderTip')">{{ $t('detail.revealFolder') }}</button>
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
                  <span v-if="mv.uncensored" class="badge warn">{{ $t('detail.uncensored') }}</span>
                  <span v-if="quality" class="badge accent">{{ quality }}</span>
                  <span v-if="mv.vr" class="badge">VR</span>
                  <span v-if="mv.leak" class="badge err">{{ $t('detail.leak') }}</span>
                  <span v-for="s in (mv.subtitles || [])" :key="s.id" class="badge sub" :title="s.path">
                    {{ $t('detail.subFile', { lang: s.lang || '字幕' }) }}
                  </span>
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
                  <template v-if="mv.added_at"><dt>{{ $t('detail.fAdded') }}</dt><dd>{{ fmtAgo(mv.added_at) }}</dd></template>
                </dl>

                <!-- 自定义标签：常驻主区可见，随时增删 -->
                <div class="dd-tags">
                  <div class="dd-tags-head">
                    <span class="lbl">{{ $t('detail.customTags') }}</span>
                    <button class="link-btn" @click="openTagMgr">{{ $t('detail.manageTags') }}</button>
                    <span v-if="aiReady" class="dim">{{ $t('detail.aiTagHint') }}</span>
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
                  <div v-if="mv.note" class="note-box"><b>{{ $t('detail.note') }}</b><p>{{ mv.note }}</p></div>
                  <div v-if="aiReady" class="hstack mt wrap">
                    <button class="btn tiny ghost" :disabled="aiBusy" @click="doAiSynopsis">{{ aiBusy ? $t('detail.generating') : $t('detail.aiSynopsis') }}</button>
                    <button class="btn tiny ghost" :disabled="aiBusy" @click="doAiTags">{{ $t('detail.aiTags') }}</button>
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
                  <img v-for="(u, i) in previews" :key="i" :src="u" alt="" @click="lightbox = u" />
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
                    <button class="btn tiny" @click="doDelete(false)">{{ $t('detail.delFromDbBtn') }}</button>
                    <button class="btn tiny danger" @click="doDelete(true)">{{ $t('detail.delWithFileBtn') }}</button>
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
    <div v-if="lightbox" class="lightbox" @click="lightbox = ''">
      <img :src="lightbox" alt="" />
    </div>

    <!-- 女优弹框：在详情页内查看女优介绍，不关闭当前详情 -->
    <ActressModal v-if="actressModal" :ident="actressModal" @close="actressModal = ''" />

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
  background: rgba(0,0,0,.88);
  display: grid; place-items: center;
  padding: var(--sp-6);
  cursor: zoom-out;
  animation: fade-in var(--t-base);
}
.lightbox img { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: var(--r-sm); }

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
