<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { state } from '../state.js'
import {
  getMovie, updateMovie, deleteMovie, toggleFlag, playMovie,
  exportNfo, getPreviews, getSimilar, coverUrl, coverThumbUrl, uploadCover, clearCover,
  listCollections, addToCollection, createCollection, listTags, renameTag, deleteTag,
  aiGenerateSynopsis, aiSuggestTags, aiStatus,
} from '../api.js'
import {
  toast, confirmDialog, copyText, coverFallback, fmtSize, fmtDuration,
  fmtDate, fmtAgo, qualityTag,
} from '../utils.js'
import { t } from '../i18n/index.js'
import { useTasks } from '../composables/useTasks.js'

const { runScrape } = useTasks()
import VideoPlayer from './VideoPlayer.vue'

const mv = ref(null)
const loading = ref(false)
const tab = ref('info')
const playing = ref(false)
const previews = ref([])
const pvLoading = ref(false)
const similar = ref([])
const editing = ref(false)
const draft = ref({})
const lightbox = ref('')
const collList = ref([])
const showColl = ref(false)
const aiReady = ref(false)
const aiBusy = ref(false)

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
  tab.value = 'info'
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
  showColl.value = false
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

/* ---------- 片单 ---------- */
const newCollName = ref('')
const creatingColl = ref(false)
const collWrap = ref(null)
async function openColl() {
  showColl.value = !showColl.value
  if (showColl.value && !collList.value.length) {
    try { const r = await listCollections(); collList.value = (r && r.items) || [] } catch (e) { toast(e.message, 'err') }
  }
}
function onDocClick(e) {
  if (showColl.value && collWrap.value && !collWrap.value.contains(e.target)) showColl.value = false
}
function onCollBtn(e) {
  e.stopPropagation()
  openColl()
}
async function addColl(cid) {
  try { await addToCollection(cid, id.value); toast(t('detail.joinedCollection'), 'ok'); showColl.value = false }
  catch (e) { toast(e.message, 'err') }
}
async function createColl() {
  const name = newCollName.value.trim()
  if (!name || creatingColl.value) return
  creatingColl.value = true
  try {
    const c = await createCollection({ name })
    const item = c && c.collection ? c.collection : { id: c && c.id, name }
    collList.value = [...collList.value, item]
    newCollName.value = ''
    toast(t('detail.collCreated'), 'ok')
  } catch (e) { toast(e.message, 'err') }
  finally { creatingColl.value = false }
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

function openActress(name) {
  state.actressCurrent = name
  state.actressReturnView = 'gallery'
  state.view = 'actressDetail'
  close()
}

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
watch(id, (v) => { if (v) { bust.value = Date.now(); load() } })
watch(tab, (t) => {
  if (t === 'preview' && !previews.value.length) loadPreviews(false)
})

function onKey(e) {
  if (!open.value) return
  if (e.key === 'Escape') { lightbox.value ? (lightbox.value = '') : close() }
}
onMounted(() => { window.addEventListener('keydown', onKey); document.addEventListener('click', onDocClick, true) })
onBeforeUnmount(() => { window.removeEventListener('keydown', onKey); document.removeEventListener('click', onDocClick, true) })
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="drawer-mask" @click="close"></div>
    <aside class="drawer" :class="{ open }">
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
                <div class="cov-acts">
                  <button class="btn tiny" @click="fileInput.click()">{{ $t('detail.changeCover') }}</button>
                  <button class="btn tiny ghost" @click="removeCover">{{ $t('detail.clearCover') }}</button>
                  <input ref="fileInput" type="file" accept="image/*" hidden @change="onUpload" />
                </div>
              </div>

              <div class="dd-main">
                <!-- 主操作 -->
                <div class="dd-actions">
                  <button class="btn primary" @click="playing = !playing">
                    {{ playing ? $t('detail.collapsePlayer') : (progressPct > 0 ? $t('detail.continue', { p: progressPct }) : $t('detail.onlinePlay')) }}
                  </button>
                  <button class="btn" @click="play">{{ $t('player.external') }}</button>
                  <button class="btn icon" :class="{ active: mv.favorite }" @click="flip('favorite')" :data-tip="$t('flag.favorite')">{{ mv.favorite ? '♥' : '♡' }}</button>
                  <button class="btn icon" :class="{ active: mv.watchlist }" @click="flip('watchlist')" :data-tip="$t('flag.watchlist')">⌚</button>
                  <button class="btn icon" :class="{ active: mv.watched }" @click="flip('watched')" :data-tip="$t('flag.watched')">{{ mv.watched ? '●' : '○' }}</button>

                  <div class="coll-wrap" ref="collWrap">
                    <button class="btn icon" @click="onCollBtn" :data-tip="$t('detail.addToCollection')">＋</button>
                    <div v-if="showColl" class="coll-pop" @click.stop>
                      <div v-if="!collList.length" class="cp-empty muted">{{ $t('detail.noCollYet') }}</div>
                      <button v-for="c in collList" :key="c.id" class="cp-item" @click="addColl(c.id)">{{ c.name }}</button>
                      <div class="cp-new">
                        <input
                          v-model="newCollName"
                          class="tag-input"
                          :placeholder="$t('detail.newCollName')"
                          @keydown.enter.prevent="createColl"
                        />
                        <button class="btn tiny" :disabled="creatingColl || !newCollName.trim()" @click="createColl">{{ $t('detail.newColl') }}</button>
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
                  <span v-if="mv.uncensored" class="badge warn">{{ $t('detail.uncensored') }}</span>
                  <span v-if="quality" class="badge accent">{{ quality }}</span>
                  <span v-if="mv.vr" class="badge">VR</span>
                  <span v-if="mv.leak" class="badge err">{{ $t('detail.leak') }}</span>
                </div>

                <!-- 关键信息 -->
                <dl class="dd-facts">
                  <template v-if="mv.actresses && mv.actresses.length">
                    <dt>{{ $t('detail.fActress') }}</dt>
                    <dd class="chip-list">
                      <button v-for="a in mv.actresses" :key="a" class="chip" @click="openActress(a)">{{ a }}</button>
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
              <button class="tab" :class="{ on: tab === 'info' }" @click="tab = 'info'">{{ $t('detail.tabInfo') }}</button>
              <button class="tab" :class="{ on: tab === 'preview' }" @click="tab = 'preview'">{{ $t('detail.tabPreview') }}</button>
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
  </Teleport>
</template>

<style scoped>
.dh-title { font-size: var(--fs-lg); font-weight: 600; flex: 1; min-width: 0; }

/* 主体：左主区 + 右相似推荐侧栏，常驻并排 */
.drawer-body { display: flex; flex-direction: row; overflow: hidden; }
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
.cov-acts { display: flex; gap: var(--sp-2); margin-top: var(--sp-2); }

.dd-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.dd-actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
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

/* 窄屏：相似推荐侧栏移到主区下方，全宽 */
@media (max-width: 1100px) {
  .drawer-body { flex-direction: column; }
  .dd-similar-rail { width: auto; border-left: 0; border-top: 1px solid var(--c-line); }
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
