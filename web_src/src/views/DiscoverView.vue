<script setup>
/**
 * DiscoverView —— 影片推荐首页
 *
 * 布局参照主流视频网站首页：
 *   1) 全屏海报区：推荐影片的画面占满视口，顶部是悬浮在海报之上的透明菜单
 *   2) 海报底部一排可横向滑动的小缩略图：点击切换当前推荐片，悬停浮出静帧预览
 *   3) 下方按「喜好 / 主题 / 标签 / 系列 / 片商 / 画质」等维度分行组织内容
 *      —— 绝大多数筛选浏览在首页完成，复杂组合仍可进入影片库搜索
 *
 * 复用经典界面的 MovieRail（横向行）与 MovieCard（卡片），沿用 --c-* 设计变量。
 */
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { state, NAV_TABS, openMoviePlayer, openPlayQueue, clearPlayQueue, goSearch } from '../state.js'
import { t } from '../i18n/index.js'
import { toast, fmtMin, fmtSize, qualityTag } from '../utils.js'
import {
  listMovies, getRecommend, getFacets, getMovie, coverThumbUrl, toggleFlag,
  streamUrl, strmHlsUrl, strmCacheProgress,
} from '../api.js'
import videojs from 'video.js'
import 'video.js/dist/video-js.css'
import MovieRail from '../components/MovieRail.vue'
import GettingStarted from '../components/GettingStarted.vue'
import { checklistHidden } from '../composables/useGuide.js'

const emit = defineEmits([])

/* ---------------- 顶部悬浮菜单 ----------------
   导航项复用全局 NAV_TABS（单一数据源），这里只决定「全部按文字呈现」，
   不再自己维护一份 id 列表——加页/改名只需改 state.js 一处。 */
const navItems = computed(() => NAV_TABS.map((n) => ({ id: n.id, label: t(n.label) })))
const keyword = ref('')

/* ---------------- 全屏海报区 ---------------- */
const heroList = ref([])       // 顶部推荐候选
const heroFrames = reactive({})// id -> { hero, peek, frames: [] }
const heroId = ref(null)   // 用 id 记住当前片：候选重排也不会把焦点弄丢
const heroFallback = ref(0)    // 0=抽帧 1=fanart 2=原图
const heroLoading = ref(true)
/* 真实播放：本地文件直接播，在线的播已缓存分片，没有缓存就不播 */
const videoEl = ref(null)
const videoMode = ref('none')   // none | native | hls
const videoVisible = ref(false)
let vjsPlayer = null

/* 自动预览：停留一段时间后，轮播该片的抽帧（腾讯视频首页的「自动播片段」） */
const previewing = ref(false)
const previewIdx = ref(0)
/* 触摸设备没有 hover，默认不开自动预览：否则点一下缩略图就会自动播视频 */
const canHover = !(typeof window !== 'undefined' && window.matchMedia)
  || window.matchMedia('(hover: hover)').matches
const autoPreview = ref(canHover)
let previewStartTimer = null
let previewTimer = null
/* hover 快速划过时的切换冷却，避免一路扫过去连闪 */
let lastSwitchAt = 0
const SWITCH_COOLDOWN = 480

const hero = computed(() =>
  heroList.value.find((m) => m.id === heroId.value) || heroList.value[0] || null)
const heroImg = computed(() => {
  const m = hero.value
  if (!m) return ''
  const f = heroFrames[m.id]
  if (f && f.hero && heroFallback.value === 0) return f.hero
  if (heroFallback.value === 1) return `/api/fanart/${m.id}`
  return `/api/cover/${m.id}`
})
const heroFrameList = computed(() => {
  const m = hero.value
  if (!m) return []
  const f = heroFrames[m.id]
  return (f && f.frames) || []
})
/** 大图实际显示的图：预览时逐帧轮播，否则是静态大图 */
const heroSrc = computed(() => {
  if (previewing.value && heroFrameList.value.length > 1) {
    return heroFrameList.value[previewIdx.value % heroFrameList.value.length]
  }
  return heroImg.value
})
function onHeroError() {
  if (previewing.value) return   // 预览帧偶发失败不影响整体
  heroFallback.value = heroFallback.value < 2 ? heroFallback.value + 1 : 0
}
function stopPreview() {
  previewing.value = false
  clearTimeout(previewStartTimer)
  clearInterval(previewTimer)
}
/** 停留一会儿后开始预览：优先真实播放，没有就退回抽帧慢速轮播 */
function schedulePreview() {
  clearTimeout(previewStartTimer)
  clearInterval(previewTimer)
  previewing.value = false
  if (!autoPreview.value) return
  if (videoVisible.value) return
  if (heroFrameList.value.length < 3) return
  previewStartTimer = setTimeout(() => {
    if (videoVisible.value) return
    previewing.value = true
    previewIdx.value = 0
    previewTimer = setInterval(() => {
      previewIdx.value = (previewIdx.value + 1) % heroFrameList.value.length
    }, 1300)   // 抽帧之间场景差异大，放慢节奏并靠长淡入柔化
  }, 1600)
}
function teardownVideo() {
  if (vjsPlayer) { try { vjsPlayer.dispose() } catch (e) {} vjsPlayer = null }
  const el = videoEl.value
  if (el) { try { el.pause(); el.removeAttribute('src'); el.load() } catch (e) {} }
  videoVisible.value = false
  videoMode.value = 'none'
}
/** 本地文件直接播；.strm 只在已有缓存分片时播，否则留给抽帧 */
async function setupVideo(m) {
  teardownVideo()
  if (!m || !autoPreview.value) return
  try {
    if (m.local_playable && !m.has_strm) {
      videoMode.value = 'native'
      await nextTick()
      const el = videoEl.value
      if (!el) return
      el.src = streamUrl(m.id)
      el.muted = true
      el.load()
      const p = el.play()
      if (p && p.catch) p.catch(() => {})
    } else if (m.has_strm) {
      const info = await strmCacheProgress(m.id)
      const ranges = (info && info.ranges) || []
      if (!ranges.length) return      // 没有缓存 → 不播，继续用抽帧
      videoMode.value = 'hls'
      await nextTick()
      const el = videoEl.value
      if (!el) return
      vjsPlayer = videojs(el, {
        controls: false, autoplay: 'muted', muted: true, preload: 'auto',
        sources: [{ src: strmHlsUrl(m.id), type: 'application/x-mpegURL' }],
      })
    }
  } catch (e) {
    videoMode.value = 'none'
  }
}
function onVideoReady() {
  videoVisible.value = true
  stopPreview()
}
/** 预览是「精彩片段」：从四分之一处起播，避开片头与字幕条 */
function onVideoMeta() {
  const el = videoEl.value
  if (!el || !Number.isFinite(el.duration)) return
  try { el.currentTime = Math.min(el.duration * 0.25, 90) } catch (e) {}
}
/** 播到 85% 回到同一起点，避免整段循环回片头 */
function onVideoTime() {
  const el = videoEl.value
  if (!el || !Number.isFinite(el.duration) || el.duration <= 0) return
  if (el.currentTime > el.duration * 0.85) {
    try { el.currentTime = el.duration * 0.25 } catch (e) {}
  }
}
function onVideoError() {
  videoVisible.value = false
  videoMode.value = 'none'
  schedulePreview()
}
function selectHero(i) {
  const m = heroList.value[i]
  if (m && m.id !== heroId.value) {
    heroId.value = m.id
    heroFallback.value = 0
  }
  lastSwitchAt = Date.now()
  stopPreview()
  nextTick(() => {
    setupVideo(hero.value)
    schedulePreview()
  })
}
/* 把焦点切到某部已就绪的片并重启动画：用于异步补帧后纠正焦点 */
function focusPreviewReady(m) {
  if (!m || m.id === heroId.value) {
    // 已经是焦点就直接重排一次预览（帧可能刚刚才就绪）
    stopPreview()
    nextTick(() => { setupVideo(hero.value); schedulePreview() })
    return
  }
  const i = heroList.value.findIndex((x) => x.id === m.id)
  if (i >= 0) selectHero(i)
  else { heroId.value = m.id; stopPreview(); nextTick(() => { setupVideo(hero.value); schedulePreview() }) }
}
const heroCode = computed(() => (hero.value && (hero.value.display_code || hero.value.code)) || '')
const heroMeta = computed(() => {
  const m = hero.value
  if (!m) return []
  const out = []
  const y = String(m.release_date || m.year || '').slice(0, 4)
  if (y) out.push(y)
  if (m.runtime) out.push(`${Math.round(m.runtime / 60) || ''}′`.trim())
  if (m.rating) out.push('★ ' + Number(m.rating).toFixed(1))
  if (m.hd4k) out.push('4K')
  if (m.source_state === 'local') out.push(t('discover.local'))
  return out
})
const heroTags = computed(() => {
  const m = hero.value
  if (!m) return []
  // 演员放在最前（影片库里最有用的识别信息），其次类型、标签
  return [...(m.actresses || []).slice(0, 2), ...(m.genres || []), ...(m.tags || [])].slice(0, 6)
})
const heroPlot = computed(() => {
  const m = hero.value
  return (m && (m.plot || '')) ? String(m.plot).slice(0, 150) : ''
})

/** 悬停即切换主图（不再弹小预览框）；120ms 延迟避免快速划过时连续闪切 */
let stripTimer = null
function onStripEnter(m, el, i) {
  clearTimeout(stripTimer)
  if (Date.now() - lastSwitchAt < SWITCH_COOLDOWN) return
  stripTimer = setTimeout(() => selectHero(i), 120)
}
function onStripLeave() {
  clearTimeout(stripTimer)
}
/* 缩略条点击：桌面 hover 已承担切换，点击直接进播放页；
   触摸设备没有 hover，点击仍用于切换主图（播放走主图上的播放按钮）。 */
function onStripClick(m, i) {
  if (canHover) play(m)
  else selectHero(i)
}

/* ---------------- 下方内容行 ---------------- */
const facets = ref({})
const reloadKeys = reactive({})
const rows = computed(() => {
  const f = facets.value || {}
  const out = [
    {
      key: 'foryou',
      title: t('discover.forYou'),
      sub: f.hasHistory ? '' : t('discover.forYouHint'),
      fetch: (page, size) => (page === 1
        ? getRecommend({ limit: size, exclude_watched: false })
        : Promise.resolve({ items: [], total: 0 })),
      reasonOf: (m) => (m.reasons || []).map((r) => r.name).join(' · '),
    },
    {
      key: 'hd4k',
      title: t('discover.hd4k'),
      fetch: (page, size) => listMovies({ flags: 'hd4k', page, page_size: size }),
    },
    {
      key: 'recent',
      title: t('discover.recentlyAdded'),
      fetch: (page, size) => listMovies({ sort: 'added_desc', page, page_size: size }),
    },
    {
      key: 'top',
      title: t('discover.topRated'),
      fetch: (page, size) => listMovies({ sort: 'rating_desc', page, page_size: size }),
    },
    {
      key: 'random',
      title: t('discover.randomPick'),
      fetch: (page, size) => listMovies({ sort: 'random', page, page_size: size }),
    },
  ]
  const push = (prefix, list, limit, param) => {
    (list || []).slice(0, limit).forEach((it) => {
      out.push({
        key: prefix + it.name,
        title: it.name,
        sub: it.count != null ? `${it.count}` : '',
        fetch: (page, size) => listMovies({ [param]: it.name, page, page_size: size }),
      })
    })
  }
  push('g_', f.genres, 4, 'genre')
  push('t_', f.tags, 3, 'tag')
  push('s_', f.series, 2, 'series')
  push('st_', f.studios, 2, 'studio')
  return out
})

/* ---------------- 数据 ---------------- */
const toUrl = (u) => (String(u).startsWith('/api') ? String(u) : '/api' + u)

async function loadFrames(m) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      // 直接用同源 fetch：与浏览器里手动请求完全一致，避免封装层差异
      const res = await fetch(`/api/movies/${m.id}/previews`, { credentials: 'same-origin' })
      const p = res.ok ? await res.json() : null
      const urls = ((p && p.urls) || []).map(toUrl)
      if (urls.length) {
        heroFrames[m.id] = {
          hero: urls[0],
          peek: urls[Math.min(3, urls.length - 1)],
          frames: urls,
        }
        return
      }
      break   // 确实没抽过帧，不重试
    } catch (e) {
      if (attempt === 1) break
    }
  }
  // 没抽过帧或请求失败：用封面原图顶替，悬停/预览至少有画面
  heroFrames[m.id] = { hero: '', peek: `/api/cover/${m.id}`, frames: [] }
}

async function loadHero(seed = 0) {
  heroLoading.value = true
  let list = []
  try {
    // 多拉一些候选，好从中优先挑「刮削过 + 可播」的片，避免空壳占位图进首屏
    const r = await getRecommend({ limit: 60, exclude_watched: false, seed })
    list = (r.items || [])
  } catch (e) { list = [] }
  if (!list.length) {
    try {
      const r = await listMovies({ sort: 'rating_desc', page: 1, page_size: 12 })
      list = r.items || []
    } catch (e) { list = [] }
  }
  // 候选池分层：刮削过（有封面）且有文件可播的优先，其次刮削过的，
  // 无封面的空壳片排最后——它们只有占位图，进首屏很难看。
  // 每层内部保持 seed 的推荐顺序，换一批仍会换出不同内容。
  const covered = (m) => !!(m && m.cover)
  const playable = (m) => !!(m && m.local_playable && !m.has_strm)
  const tier1 = list.filter((m) => covered(m) && playable(m))
  const tier2 = list.filter((m) => covered(m) && !playable(m))
  const tier3 = list.filter((m) => !covered(m))
  const seen = new Set()
  const pool = [...tier1, ...tier2, ...tier3].filter((m) => {
    if (!m || seen.has(m.id)) return false
    seen.add(m.id)
    return true
  }).slice(0, 16)
  const first8 = pool.slice(0, 8)
  heroList.value = first8
  // 焦点用 id 记住：候选重排后缩略条的选中态才不会跟丢
  heroId.value = first8.length ? first8[0].id : null
  heroFallback.value = 0
  // 只等首屏这 8 部的帧，分批并发（一次全打过去容易有请求失败）
  for (let i = 0; i < first8.length; i += 3) {
    await Promise.all(first8.slice(i, i + 3).map((m) => loadFrames(m)))
  }
  // 有预览帧的片排前面（缩略条展示好看）；焦点优先级：
  //   可播+有封面（能自动播真实视频）> 有帧片 > 其它，
  // 这样首屏既好看又能自动播，空壳片不会成为主视觉。
  const withFrames = first8.filter((m) => (heroFrames[m.id]?.frames || []).length >= 3)
  const without = first8.filter((m) => (heroFrames[m.id]?.frames || []).length < 3)
  heroList.value = [...withFrames, ...without]
  const focus = first8.find((m) => covered(m) && playable(m))
    || withFrames[0]
    || heroList.value[0]
  heroId.value = focus ? focus.id : null
  heroLoading.value = false
  nextTick(() => {
    setupVideo(hero.value)
    schedulePreview()
  })
  pool.slice(8).forEach((m) => loadFrames(m).then(() => {
    if ((heroFrames[m.id]?.frames || []).length < 3) return
    const rest = heroList.value.filter((x) => x.id !== m.id)
    heroList.value = [m, ...rest].slice(0, 8)
    // 当前焦点仍无帧时，切到这部新就绪的片，让自动预览能启动
    const cur = heroFrames[heroId.value]
    if (!cur || (cur.frames || []).length < 3) focusPreviewReady(m)
  }))
}

async function loadFacets() {
  try { facets.value = await getFacets(120) } catch (e) { facets.value = {} }
}

/* ---------------- 交互 ---------------- */
/* ---------------- 影片详情（页面内视图，与播放界面形成闭环） ---------------- */
const detailId = ref(null)
const detailMovie = ref(null)
const detailLoading = ref(false)
/* 详情看图：封面 + 抽帧整组，可翻页 / 关闭（视觉对齐经典页 lightbox） */
const lbList = ref([])
const lbIndex = ref(0)
const lbSrc = computed(() => lbList.value[lbIndex.value] || '')
function openLightbox(urls, index = 0) {
  const list = (urls || []).filter(Boolean)
  if (!list.length) return
  lbList.value = list
  lbIndex.value = Math.min(Math.max(index, 0), list.length - 1)
}
function closeLightbox() { lbList.value = []; lbIndex.value = 0 }
function stepLightbox(d) {
  const n = lbList.value.length
  if (n < 2) return
  lbIndex.value = (lbIndex.value + d + n) % n
}
/* 详情页可看的图：封面原图排第一，其次该片抽帧；去重避免封面与首帧同图时出现两张。
   详情大图当前显示第几张由 detailImgIdx 控制，点大图才进全屏灯箱。 */
const detailCover = computed(() => {
  const m = detailMovie.value
  return m && m.cover ? `/api/cover/${m.id}` : ''
})
const detailImages = computed(() => {
  const frames = (detailId.value && heroFrames[detailId.value]?.frames) || []
  const cover = detailCover.value
  const out = []
  const seen = new Set()
  for (const u of [cover, ...frames]) {
    if (u && !seen.has(u)) { seen.add(u); out.push(u) }
  }
  return out
})
const detailImgIdx = ref(0)
const detailImgSrc = computed(() => detailImages.value[detailImgIdx.value] || detailHero.value)
function selectDetailImg(i) {
  detailImgIdx.value = Math.min(Math.max(i, 0), detailImages.value.length - 1)
}
function openDetailLightbox() {
  openLightbox(detailImages.value, detailImgIdx.value)
}

async function openDetail(m) {
  const id = (m && typeof m === 'object') ? m.id : m
  if (!id) return
  detailId.value = id
  detailLoading.value = true
  detailImgIdx.value = 0
  detailMovie.value = (m && typeof m === 'object') ? m : null
  try {
    detailMovie.value = await getMovie(id)
    loadFrames(detailMovie.value)   // 详情大图优先用原片画面帧
  } catch (e) {
    toast(e.message || t('discover.playFailed'), 'err')
  } finally {
    detailLoading.value = false
  }
}
function closeDetail() {
  detailId.value = null
  detailMovie.value = null
}
const detailHero = computed(() => {
  const m = detailMovie.value
  if (!m) return ''
  const f = heroFrames[m.id]
  if (f && f.hero) return f.hero
  return m.cover ? `/api/cover/${m.id}` : ''
})
const detailMeta = computed(() => {
  const m = detailMovie.value
  if (!m) return []
  const rows = [
    [t('discover.number'), m.display_code || m.code || ''],
    [t('discover.released'), m.release_date || m.year || ''],
    [t('discover.runtime'), fmtMin(m.runtime)],
    [t('discover.rating'), m.rating ? '★ ' + Number(m.rating).toFixed(1) : ''],
    [t('discover.quality'), qualityTag(m.resolution) || ''],
    [t('discover.size'), fmtSize(m.size)],
    [t('discover.studio'), m.studio || ''],
    [t('discover.series'), m.series || ''],
  ]
  return rows.filter((r) => r[1])
})
const detailCast = computed(() => (detailMovie.value && detailMovie.value.actresses) || [])
const detailKinds = computed(() => {
  const m = detailMovie.value
  if (!m) return []
  return [...(m.genres || []), ...(m.tags || [])].filter(Boolean).slice(0, 14)
})
const detailFiles = computed(() => ((detailMovie.value && detailMovie.value.files) || []).slice(0, 5))

/* ---------------- 播放界面控制（覆盖在全局播放器之上） ---------------- */
const playingItem = computed(() => {
  if (!state.playQueue.open) return null
  const q = state.playQueue.queue || []
  return q[Math.min(state.playQueue.idx, q.length - 1)] || null
})
function closePlayer() {
  clearPlayQueue()
  closeDetail()          // 从详情页起播时，「返回首页」要连详情一起回到首页
  if (rootEl.value) rootEl.value.scrollTop = 0
}
function stepPlaying(delta) {
  const q = state.playQueue.queue || []
  if (!q.length) return
  const i = Math.min(state.playQueue.idx, q.length - 1)
  const target = q[(i + delta + q.length) % q.length]
  if (!target) return
  const rest = q.filter((m) => m.id !== target.id)
  openPlayQueue([target, ...rest], state.playQueue.source || '', 0)
}
async function play(m) {
  if (!m) return
  try { await openMoviePlayer(m) } catch (e) { toast(e.message || t('discover.playFailed'), 'err') }
}
async function fav(m) {
  if (!m) return
  const before = !!m.favorite
  m.favorite = before ? 0 : 1
  try {
    await toggleFlag(m.id, 'favorite')
  } catch (e) {
    m.favorite = before ? 1 : 0
    toast(t('discover.playFailed'), 'err')
  }
}
function toggleAutoPreview() {
  autoPreview.value = !autoPreview.value
  if (autoPreview.value) schedulePreview()
  else stopPreview()
}
/* 换一批推荐：用新 seed 真去拉一批不同的推荐，而不是只在已加载的 8 部里换焦点。
   seed 递增保证每次都不同（后端 recommend 的加权随机由 seed 决定）。 */
let recSeed = 0
async function shuffleHero() {
  if (heroLoading.value) return
  recSeed += 1 + Math.floor(Math.random() * 997)
  await loadHero(recSeed)
  try { await loadFacets() } catch (e) { /* 内容行刷新失败不影响海报 */ }
}
const shuffleRecommend = shuffleHero
/* 详情页里点「换一批」：先退回大画面再换，否则详情内容不变、点了像没反应 */
function shuffleFromDetail() {
  closeDetail()
  shuffleHero()
}
function reloadRow(key) { reloadKeys[key] = (reloadKeys[key] || 0) + 1 }

/* 顶栏滚过海报后加底色 */
const scrolled = ref(false)
const heroPull = ref(0)      // 滚动驱动的画面推近与暗化（0 → 1）
const rootEl = ref(null)
function onScroll() {
  const el = rootEl.value
  if (!el) return
  const y = el.scrollTop
  scrolled.value = y > (el.clientHeight * 0.55)
  heroPull.value = Math.min(1, y / (el.clientHeight * 0.72))
}
function scrollToTop() {
  const el = rootEl.value
  if (el) el.scrollTo({ top: 0, behavior: 'smooth' })
}

/* 空库时整页换成起步清单：发现页已是默认首页，新用户第一眼必须知道「去哪加影片」。
   库里一有影片就自动切回海报，和经典首页同一套判断（真实影片数 + 可手动隐藏）。 */
const libTotal = ref(0)
/* 影片数是否确认加载过：未加载前 libTotal 是占位的 0，不能当成空库，
   否则 config 先到、影片接口后到时会闪一下起步清单（库里其实有片）。 */
const libLoaded = ref(false)
const showStart = computed(() =>
  !!state.config && libLoaded.value && !checklistHidden.value && libTotal.value === 0)
async function refreshLibTotal() {
  try {
    const r = await listMovies({ page: 1, page_size: 1 })
    libTotal.value = Number(r.total) || 0
    libLoaded.value = true
  } catch (e) { /* 离线时保留上次结果；首次失败也不误判为空库 */ }
}

function onKey(e) {
  const tag = ((e.target && e.target.tagName) || '').toLowerCase()
  const typing = tag === 'input' || tag === 'textarea' || tag === 'select'
  if (e.key === 'Escape') {
    if (lbSrc.value) { e.preventDefault(); closeLightbox(); return }
    if (detailId.value) { e.preventDefault(); closeDetail(); return }
    if (typing) e.target.blur()
    return
  }
  // 看图时左右翻页，优先于首页的切换推荐
  if (lbSrc.value && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    e.preventDefault()
    stepLightbox(e.key === 'ArrowLeft' ? -1 : 1)
    return
  }
  // ← → 切换当前推荐
  if (!typing && !detailId.value && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    const n = heroList.value.length
    if (!n) return
    e.preventDefault()
    const cur = heroList.value.findIndex((m) => m.id === (hero.value && hero.value.id))
    const next = (cur < 0 ? 0 : cur + (e.key === 'ArrowRight' ? 1 : -1) + n) % n
    selectHero(next)
  }
}

onMounted(() => {
  loadHero()
  loadFacets()
  refreshLibTotal()
  window.addEventListener('keydown', onKey)
  window.addEventListener('avm-refresh', refreshLibTotal)
  nextTick(() => { if (rootEl.value) rootEl.value.addEventListener('scroll', onScroll, { passive: true }) })
})
onBeforeUnmount(() => {
  clearTimeout(stripTimer)
  clearTimeout(previewStartTimer)
  clearInterval(previewTimer)
  teardownVideo()
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('avm-refresh', refreshLibTotal)
  if (rootEl.value) rootEl.value.removeEventListener('scroll', onScroll)
})
// 扫描结束 / 清单隐藏后重新判断是否该显示起步清单
watch(() => state.task.scan.running, (running, was) => { if (was && !running) refreshLibTotal() })
watch(checklistHidden, refreshLibTotal)
</script>

<template>
  <div ref="rootEl" class="disc">
    <!-- 悬浮在海报之上的顶部菜单 -->
    <header v-show="!detailId" class="disc-top" :class="{ 'is-solid': scrolled }">
      <button class="disc-top__brand" @click="scrollToTop">
        <span class="disc-top__logo">AVM</span>
        <span class="disc-top__tag">{{ $t('discover.brandTag') }}</span>
      </button>
      <nav class="disc-top__nav">
        <button v-for="n in navItems" :key="n.id" :class="{ on: state.view === n.id }"
                @click="state.view = n.id">{{ n.label }}</button>
      </nav>
      <div class="disc-top__right">
        <label class="disc-top__search">
          <span aria-hidden="true"><svg class="ic" viewBox="0 0 24 24" fill="currentColor" fill-rule="evenodd"><path d="M10.5 3a7.5 7.5 0 1 0 4.7 13.36l4.72 4.72 1.42-1.42-4.72-4.72A7.5 7.5 0 0 0 10.5 3zm0 2a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11z"/></svg></span>
          <input v-model="keyword" :placeholder="$t('discover.searchPlaceholder')"
                 @keyup.enter="goSearch(keyword)" />
        </label>
        <button class="disc-top__icon" :title="$t('nav.settings')" @click="state.view = 'settings'"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>
      </div>
    </header>

    <!-- 空库：起步清单顶替海报，和经典首页同一套；库里一有影片自动切回 -->
    <div v-if="showStart" class="disc-start">
      <GettingStarted />
    </div>

    <!-- 全屏海报区 -->
    <section v-else class="disc-hero" :style="{ '--pull': heroPull }">
      <div class="disc-hero__bg" :class="{ 'is-preview': previewing }"
           role="button" :aria-label="$t('discover.play') + ' · ' + heroCode"
           @click="play(hero)">
        <!-- 预览轮播用更长的溶解（不同场景需要时间看清），切换影片则要利落 -->
        <Transition name="disc-fade" :duration="previewing ? 900 : 320">
          <img v-if="heroSrc" :key="heroSrc" :src="heroSrc" @error="onHeroError" alt=""
               :class="{ 'is-still': previewing || videoVisible, 'is-hidden': videoVisible }" />
          <div v-else key="ph" class="disc-hero__placeholder"></div>
        </Transition>
        <!-- 真实播放层：本地文件直接播，在线的播已缓存分片；就绪后盖在抽帧之上。
             元素常驻（不用 key 重建），避免切换影片时 ref 竞态导致 src 丢失。 -->
        <video
          v-show="videoMode !== 'none'"
          ref="videoEl"
          class="disc-hero__video"
          :class="{ 'is-on': videoVisible }"
          muted autoplay playsinline
          @loadedmetadata="onVideoMeta"
          @timeupdate="onVideoTime"
          @canplay="onVideoReady"
          @playing="onVideoReady"
          @error="onVideoError"
        ></video>
        <!-- 大画面即播放入口：hover 时中央浮现播放图标提示（无按钮，保持画面纯净） -->
        <span class="disc-hero__playhint" aria-hidden="true">
          <svg class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l10.5-6.5z"/></svg>
        </span>
      </div>
      <div class="disc-hero__scrim" aria-hidden="true"></div>

      <div class="disc-hero__info">
        <Transition name="disc-slide" mode="out-in">
          <div v-if="hero" :key="hero.id" class="disc-hero__block">
            <div class="disc-hero__eyebrow">
              <span class="disc-hero__code">{{ heroCode }}</span>
              <span v-if="videoVisible || previewing" class="disc-hero__live">
                <i></i>{{ videoVisible ? $t('discover.nowPlaying') : $t('discover.previewing') }}
              </span>
              <button class="disc-hero__mini" :class="{ off: !autoPreview }"
                      :title="$t('discover.autoPreview')"
                      @click.stop="toggleAutoPreview">
                <svg v-if="autoPreview" class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/></svg>
                <svg v-else class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l10.5-6.5z"/></svg>
              </button>
              <button class="disc-hero__mini" :title="$t('discover.moreInfo')"
                      @click.stop="openDetail(hero)">
                <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>
              </button>
              <button class="disc-hero__mini" :title="$t('discover.shuffle')"
                      @click.stop="shuffleRecommend">
                <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 1 0-2.3 6.3M20 5v6h-6"/></svg>
              </button>
            </div>
            <h1 class="disc-hero__title" :title="$t('discover.moreInfo') + ' · ' + hero.title"
                @click.stop="openDetail(hero)">{{ hero.title }}</h1>
            <div class="disc-hero__meta">
              <span v-for="m in heroMeta" :key="m">{{ m }}</span>
            </div>
            <div v-if="heroTags.length" class="disc-hero__tags">
              <span v-for="tg in heroTags" :key="tg">{{ tg }}</span>
            </div>
            <p v-if="heroPlot" class="disc-hero__plot">{{ heroPlot }}</p>
          </div>
          <div v-else :key="'empty'" class="disc-hero__block">
            <span class="disc-hero__code">{{ $t('discover.heroEmpty') }}</span>
          </div>
        </Transition>
      </div>

      <!-- 缩略图切换条 -->
      <div v-if="heroList.length" class="disc-strip">
        <div class="disc-strip__track">
          <button
            v-for="(m, i) in heroList"
            :key="m.id"
            class="disc-strip__item"
            :class="{ on: m.id === heroId }"
            :aria-label="(canHover ? $t('discover.play') + ' · ' : '') + (m.display_code || m.code || m.title)"
            @click="onStripClick(m, i)"
            @mouseenter="onStripEnter(m, $event.currentTarget, i)"
            @mouseleave="onStripLeave"
          >
            <img class="disc-strip__thumb" :src="coverThumbUrl(m.id, 200)" :alt="m.title" loading="lazy" />
            <span v-if="canHover" class="disc-strip__play" aria-hidden="true">
              <svg class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l10.5-6.5z"/></svg>
            </span>
            <span class="disc-strip__label">{{ m.display_code || m.code || m.title }}</span>
          </button>
        </div>
      </div>

    </section>

    <!-- 维度内容行 -->
    <section v-if="!showStart" class="disc-rows">
      <div v-for="row in rows" :key="row.key" class="disc-row">
        <div class="disc-row__head">
          <h2 class="disc-row__title">{{ row.title }}</h2>
          <span v-if="row.sub" class="disc-row__sub">{{ row.sub }}</span>
          <button class="disc-row__more" @click="reloadRow(row.key)">{{ $t('discover.shuffle') }}</button>
        </div>
        <MovieRail
          :fetch-page="row.fetch"
          :page-size="12"
          :reason-of="row.reasonOf || null"
          :reload-key="reloadKeys[row.key] || 0"
          @open="openDetail"
        >
          <!-- 卡片悬停时浮出「播放 / 详情」两个入口 -->
          <template #overlay="{ movie: m }">
            <div class="disc-card-acts">
              <button class="disc-card-act disc-card-act--play" :aria-label="$t('discover.play')"
                      @click.stop="play(m)"><svg class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l10.5-6.5z"/></svg></button>
              <button class="disc-card-act" :aria-label="$t('discover.moreInfo')"
                      @click.stop="openDetail(m)">↗</button>
            </div>
          </template>
        </MovieRail>
      </div>

      <div class="disc-foot">
        <p>{{ $t('discover.footNote') }}</p>
        <button class="dbtn" @click="state.view = 'gallery'">{{ $t('discover.toGallery') }} ↗</button>
      </div>
    </section>

    <!-- 影片详情：页面内视图，可直接「立即播放」进入播放界面 -->
    <Transition name="disc-view">
      <div v-if="detailId" class="disc-detail">
        <button class="disc-detail__back" :title="$t('discover.backHome')"
                :aria-label="$t('discover.backHome')" @click="closeDetail">
          <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M15 18l-6-6 6-6" />
          </svg>
        </button>
        <div class="disc-detail__grid">
          <div class="disc-detail__media">
            <div class="disc-detail__frame">
              <img v-if="detailImgSrc" :src="detailImgSrc" alt="" @click="openDetailLightbox" />
              <div v-else class="disc-detail__blank">AVM</div>
              <span v-if="detailImgSrc" class="disc-detail__zoom" aria-hidden="true">⤢</span>
              <span v-if="detailLoading" class="disc-detail__loading">{{ $t('common.loading') }}</span>
            </div>
            <div v-if="detailImages.length > 1" class="disc-detail__strips">
              <img v-for="(u, i) in detailImages" :key="i" :src="u" :alt="String(i + 1)"
                   :class="{ on: i === detailImgIdx }" @click="selectDetailImg(i)" />
            </div>
            <div class="disc-detail__acts">
              <button class="dbtn dbtn--solid" @click="play(detailMovie)">
                <svg class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l10.5-6.5z"/></svg>
                {{ $t('discover.playNow') }}
              </button>
              <button class="dbtn" @click="state.currentId = detailId">
                {{ $t('discover.classicDetail') }}
              </button>
              <button class="dbtn dbtn--icon" :class="{ 'dbtn--heart': detailMovie && detailMovie.favorite }"
                      :title="$t('flag.favorite')" @click="fav(detailMovie)">
                <svg class="ic" viewBox="0 0 24 24" :fill="detailMovie && detailMovie.favorite ? 'currentColor' : 'none'"
                     stroke="currentColor" stroke-width="1.6">
                  <path d="M12 21C7.2 16.6 3 13.2 3 9.3 3 6.6 5.1 4.5 7.8 4.5c1.7 0 3.2.9 4.2 2.3 1-1.4 2.5-2.3 4.2-2.3 2.7 0 4.8 2.1 4.8 4.8 0 3.9-4.2 7.3-9 11.7z"/>
                </svg>
              </button>
              <button class="dbtn dbtn--icon" :title="$t('discover.shuffle')" @click="shuffleFromDetail">
                <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16 4h4v4M20 4L4 20M20 16v4h-4M14 14l6 6M4 4l5.5 5.5"/></svg>
              </button>
            </div>
          </div>
          <div class="disc-detail__info">
            <span class="disc-detail__code">
              {{ (detailMovie && (detailMovie.display_code || detailMovie.code)) || '' }}
            </span>
            <h2 class="disc-detail__title">{{ (detailMovie && detailMovie.title) || '' }}</h2>
            <p v-if="detailMovie && detailMovie.original_title && detailMovie.original_title !== detailMovie.title"
               class="disc-detail__orig">{{ detailMovie.original_title }}</p>
            <dl class="disc-detail__meta">
              <div v-for="(r, i) in detailMeta" :key="i"><dt>{{ r[0] }}</dt><dd>{{ r[1] }}</dd></div>
            </dl>
            <section class="disc-detail__block">
              <h3>{{ $t('discover.plot') }}</h3>
              <p class="disc-detail__plot">
                {{ (detailMovie && detailMovie.plot) || $t('discover.noPlot') }}
              </p>
            </section>
            <section v-if="detailCast.length" class="disc-detail__block">
              <h3>{{ $t('discover.cast') }}</h3>
              <div class="disc-detail__tags">
                <span v-for="(a, i) in detailCast" :key="i">{{ a }}</span>
              </div>
            </section>
            <section v-if="detailKinds.length" class="disc-detail__block">
              <h3>{{ $t('discover.genre') }}</h3>
              <div class="disc-detail__tags">
                <span v-for="(g, i) in detailKinds" :key="i">{{ g }}</span>
              </div>
            </section>
            <section v-if="detailFiles.length" class="disc-detail__block">
              <h3>{{ $t('discover.files') }}</h3>
              <ul class="disc-detail__files">
                <li v-for="(f, i) in detailFiles" :key="i">
                  <span :title="f.path">{{ f.filename || f.path }}</span><em>{{ fmtSize(f.size) }}</em>
                </li>
              </ul>
            </section>
          </div>
        </div>

        <!-- 看图：封面 + 抽帧整组，视觉对齐经典页灯箱 -->
        <div v-if="lbSrc" class="disc-lb" @click="closeLightbox">
          <div class="disc-lb__bg" :style="{ backgroundImage: `url(${lbSrc})` }"></div>
          <button class="disc-lb__close" :title="$t('common.close')" @click.stop="closeLightbox">
            <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
          </button>
          <button v-if="lbList.length > 1" class="disc-lb__nav prev" :title="$t('detail.prevImage')"
                  @click.stop="stepLightbox(-1)">
            <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
          </button>
          <img class="disc-lb__img" :src="lbSrc" alt="" @click.stop />
          <button v-if="lbList.length > 1" class="disc-lb__nav next" :title="$t('detail.nextImage')"
                  @click.stop="stepLightbox(1)">
            <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
          </button>
          <div class="disc-lb__count">{{ lbIndex + 1 }} / {{ lbList.length }}</div>
        </div>
      </div>
    </Transition>

    <!-- 播放界面控制条：叠在全局播放器之上，提供返回首页与切换影片 -->
    <Teleport to="body">
      <div v-if="playingItem" class="disc-playbar">
        <button class="disc-playbar__btn" @click="closePlayer">← {{ $t('discover.backHome') }}</button>
        <span class="disc-playbar__title">{{ playingItem.title || playingItem.display_code || '' }}</span>
        <button class="disc-playbar__btn" :aria-label="$t('discover.prevMovie')" @click="stepPlaying(-1)">‹</button>
        <button class="disc-playbar__btn" :aria-label="$t('discover.nextMovie')" @click="stepPlaying(1)">›</button>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.disc {
  position: fixed;
  inset: 0;
  z-index: 38;
  overflow-y: auto;
  overflow-x: hidden;
  /* 海报区是深色画面：让内部原生控件（搜索 input）按深色渲染，
     否则浅色主题下 input 会被浏览器刷成白底，盖掉半透明深底变成刺眼白块 */
  color-scheme: dark;
  background: var(--c-bg, #0b0d12);
  color: var(--c-text, #f1f3f8);
  font-family: inherit;
  /* 媒体层专用浅色：海报/视频是深色画面，压在其上的文字不跟随主题，
     否则浅色主题下会变成深灰字压深色海报，看不清。内容区仍用 var(--c-*) 跟随主题。 */
  --on-media: rgba(255, 255, 255, .9);
  --on-media-2: rgba(255, 255, 255, .7);
  --on-media-3: rgba(255, 255, 255, .52);
}
.disc *, .disc *::before, .disc *::after { box-sizing: border-box; }
.disc button, .disc input { font: inherit; }
.disc img { display: block; max-width: 100%; }

/* ---------- 悬浮顶栏 ---------- */
.disc-top {
  position: fixed; top: 0; left: 0; right: 0; z-index: 12;
  display: flex; align-items: center; gap: var(--sp-5, 20px);
  height: 62px; padding: 0 clamp(16px, 3vw, 44px);
  background: transparent;
  border-bottom: 1px solid transparent;
  transition: background var(--t-base, .2s), border-color var(--t-base, .2s), backdrop-filter var(--t-base, .2s);
}
.disc-top.is-solid {
  background: color-mix(in srgb, var(--c-bg, #0b0d12) 88%, transparent);
  border-bottom-color: var(--c-line, #232936);
  backdrop-filter: blur(14px);
}
.disc-top__brand { display: flex; align-items: baseline; gap: 10px; border: 0; background: none; cursor: pointer; }
.disc-top__logo {
  font: 700 19px/1 var(--c-serif, Georgia, serif);
  letter-spacing: .16em; color: var(--c-primary, #e0355a);
}
.disc-top__tag {
  font: 500 9px/1 var(--c-mono, monospace); letter-spacing: .24em;
  text-transform: uppercase; color: var(--on-media-3);
}
.disc-top__nav { display: flex; align-items: center; gap: 2px; }
.disc-top__nav button {
  height: 32px; padding: 0 12px; border: 0; border-radius: var(--r-sm, 6px);
  background: transparent; color: var(--on-media-2);
  font-size: 13px; cursor: pointer;
  transition: color var(--t-fast, .12s), background var(--t-fast, .12s);
}
.disc-top__nav button:hover { color: var(--on-media); background: rgba(255, 255, 255, .06); }
.disc-top__nav button.on { color: var(--on-media); font-weight: 600; }
/* 滚动后顶栏有了浅色实底：导航文字改回跟随主题，否则浅色实底配白字看不清 */
.disc-top.is-solid .disc-top__tag { color: var(--c-text-3); }
.disc-top.is-solid .disc-top__nav button { color: var(--c-text-2); }
.disc-top.is-solid .disc-top__nav button:hover,
.disc-top.is-solid .disc-top__nav button.on { color: var(--c-text); }
.disc-top__right { margin-left: auto; display: flex; align-items: center; gap: 10px; }
.disc-top__search {
  display: flex; align-items: center; gap: 7px;
  height: 34px; min-width: 190px; padding: 0 12px;
  border: 1px solid rgba(255, 255, 255, .18); border-radius: var(--r-full, 999px);
  /* 磨砂玻璃：半透明深底会把背后海报的图案透出来，圆的边界把连续图案切断，
     看起来像「圆形背景与图形错位」。加模糊让透出的内容均匀，边界不再割裂图案。 */
  background: rgba(10, 12, 18, .58);
  backdrop-filter: blur(12px) saturate(1.1);
  -webkit-backdrop-filter: blur(12px) saturate(1.1);
  color: var(--on-media-3);
  transition: border-color var(--t-fast, .12s), background var(--t-fast, .12s);
}
.disc-top__search:focus-within { border-color: var(--c-primary, #e0355a); background: rgba(10, 12, 18, .8); }
/* 用 html 前缀拉高优先级：否则浅色主题下 cinema.css 的 body.cinema-skin input
   规则（ specificity 更高）会把这里的 transparent 覆盖成浅灰底，变成白块 */
html [data-theme] .disc .disc-top__search input,
.disc .disc-top__search input {
  flex: 1; min-width: 0; border: 0; outline: 0;
  background: transparent !important;
  color-scheme: dark;
  color: var(--on-media) !important; font-size: 13px;
}
.disc-top__search input::placeholder { color: var(--on-media-3); }
.disc-top__search input:-webkit-autofill,
.disc-top__search input:-webkit-autofill:hover,
.disc-top__search input:-webkit-autofill:focus {
  -webkit-text-fill-color: var(--on-media);
  -webkit-box-shadow: 0 0 0 1000px rgba(10, 12, 18, .9) inset;
  transition: background-color 9999s ease-out;
}
.disc-top__icon {
  width: 34px; height: 34px; border: 1px solid rgba(255, 255, 255, .18);
  border-radius: 50%;
  /* 与搜索框同一套磨砂玻璃：避免圆形底色把背后海报图案切出一道弧、看着像错位 */
  background: rgba(10, 12, 18, .58);
  backdrop-filter: blur(12px) saturate(1.1);
  -webkit-backdrop-filter: blur(12px) saturate(1.1);
  color: var(--on-media-2); cursor: pointer; font-size: 14px;
  display: inline-grid; place-items: center;
  transition: background var(--t-fast, .12s), border-color var(--t-fast, .12s);
}
.disc-top__icon:hover { background: rgba(10, 12, 18, .78); border-color: rgba(255, 255, 255, .34); }

/* ---------- 空库起步清单：顶栏之下占满一屏，自身可滚动 ---------- */
.disc-start {
  position: relative; z-index: 6;
  min-height: 100%;
  padding: calc(var(--h-topnav, 52px) + 40px) clamp(16px, 4vw, 56px) 60px;
  background: var(--c-bg, #0b0d12);
}
.disc-start :deep(.gs) { max-width: 1080px; margin: 0 auto; }

/* ---------- 全屏海报 ---------- */
.disc-hero {
  position: relative;
  height: min(92vh, 900px);
  min-height: 520px;
  overflow: hidden;
  background: #05070b;
}
.disc-hero__bg {
  position: absolute; inset: 0;
  --fade-dur: .32s;               /* 切换影片：利落 */
  transition: opacity .5s ease;
  /* 滚动驱动：画面轻微推近、上移、暗化，内容行像镜头推上来一样接管 */
  transform: translateY(calc(var(--pull, 0) * -24px))
             scale(calc(1 + var(--pull, 0) * .02));
  filter: brightness(calc(1 - var(--pull, 0) * .32));
  will-change: transform, filter;
}
/* hero 与内容区之间溶接，去掉那条硬黑边（不遮信息层：info=4 / strip=5） */
.disc-hero::after {
  content: '';
  position: absolute; left: 0; right: 0; bottom: 0; height: 150px;
  z-index: 3; pointer-events: none;
  background: linear-gradient(180deg, transparent, var(--c-bg, #0b0d12));
}
.disc-hero__bg.is-preview { --fade-dur: .9s; }   /* 抽帧轮播：慢溶解，画面才看得清 */

/* 一次编排的入场：画面先揭示，信息再跟上，缩略条最后落位。
   用 backwards 而非 both，动画结束后不锁死样式，切换影片的过渡仍正常。 */
@keyframes disc-reveal {
  from { opacity: .5; transform: scale(1.055); filter: brightness(.72); }
  to   { opacity: 1;  transform: none;         filter: brightness(1); }
}
@keyframes disc-rise {
  from { opacity: 0; transform: translateY(16px); }
  to   { opacity: 1; transform: none; }
}
@media (prefers-reduced-motion: no-preference) {
  .disc-hero__bg { animation: disc-reveal 1.25s cubic-bezier(.16, .84, .3, 1) backwards; }
  .disc-top { animation: disc-rise .7s cubic-bezier(.16, .84, .3, 1) backwards; }
  .disc-hero__info { animation: disc-rise .65s cubic-bezier(.16, .84, .3, 1) .28s backwards; }
  .disc-strip { animation: disc-rise .65s cubic-bezier(.16, .84, .3, 1) .5s backwards; }
}
.disc-hero__bg img {
  width: 100%; height: 100%; object-fit: cover; object-position: center 26%;
  animation: disc-kb 12s ease-in-out infinite alternate;
  transition: opacity .5s ease;
}
/* 视频接管后，抽帧层退场，避免两层画面叠影（视频层是它的兄弟节点，不受影响） */
.disc-hero__bg img.is-hidden { opacity: 0; }
/* 预览时画面保持静止：推近叠在快速换景上会显得躁动 */
.disc-hero__bg img.is-still { animation: none; }
/* 抽帧之间常常是完全不同的场景：极慢的推近 + 长淡入，把硬切化成「呼吸」 */
@keyframes disc-kb { from { transform: scale(1.005); } to { transform: scale(1.05); } }
/* 真实播放层 */
.disc-hero__video {
  position: absolute; inset: 0; z-index: 2;
  width: 100%; height: 100%;
  object-fit: cover; object-position: center 26%;
  opacity: 0; transition: opacity .8s ease;
  pointer-events: none;
}
.disc-hero__video.is-on { opacity: 1; }
.disc-hero__video.video-js { display: block; width: 100%; height: 100%; }
.disc-hero__video.video-js .vjs-tech { object-fit: cover; }
/* 大画面即播放入口：hover 时中央浮现播放图标。
   bg 本身可点（@click），图标只是提示、不拦截事件。 */
.disc-hero__bg { cursor: pointer; }
.disc-hero__playhint {
  position: absolute; z-index: 3; top: 42%; left: 50%;
  width: 72px; height: 72px; margin: -36px 0 0 -36px;
  display: grid; place-items: center;
  border: 1px solid rgba(255, 255, 255, .5); border-radius: 50%;
  background: rgba(8, 10, 16, .42);
  backdrop-filter: blur(4px);
  opacity: 0; transform: scale(.9);
  transition: opacity .2s var(--ease), transform .2s var(--ease);
  pointer-events: none;
}
.disc-hero__playhint .ic { width: 30px; height: 30px; color: #fff; margin-left: 3px; }
.disc-hero__bg:hover .disc-hero__playhint { opacity: 1; transform: scale(1); }
.disc-hero__placeholder {
  width: 100%; height: 100%;
  background: radial-gradient(70% 60% at 50% 30%, #1b2130, #07090e);
}
.disc-hero__scrim {
  position: absolute; inset: 0; pointer-events: none;
  /* 遮罩恒为深色：它压在海报上给浅色文字垫底，不能跟随主题变浅，
     但最底部用 var(--c-bg) 溶接到下方内容区（浅色主题下就是浅色过渡）。 */
  background:
    linear-gradient(0deg, var(--c-bg, #0b0d12) 2%, rgba(11, 13, 18, .72) 22%,
                    rgba(11, 13, 18, .18) 48%, rgba(11, 13, 18, .1) 72%, transparent 92%),
    linear-gradient(90deg, rgba(6, 8, 12, .8) 0%, rgba(6, 8, 12, .38) 38%, transparent 66%);
}
.disc-hero__info {
  position: absolute; left: 0; right: 0; bottom: 0; z-index: 4;
  padding: 0 clamp(20px, 4vw, 64px) 224px;
  /* 信息层不拦截点击：让「点海报任意处播放」覆盖整块画面；
     其中的自动预览按钮单独恢复可点。 */
  pointer-events: none;
  /* 随滚动淡出：避免与固定顶栏相撞，也让画面在离场时更干净 */
  opacity: calc(1 - var(--pull, 0) * 1.6);
}
.disc-hero__mini { pointer-events: auto; }
.disc-hero__block { max-width: 640px; }
.disc-hero__eyebrow {
  display: flex; align-items: center; gap: 14px; margin-bottom: 10px;
}
.disc-hero__code {
  display: block; margin-bottom: 0;
  font: 600 10px/1 var(--c-mono, monospace); letter-spacing: .26em;
  text-transform: uppercase; color: var(--c-primary, #e0355a);
}
.disc-hero__live {
  display: inline-flex; align-items: center; gap: 7px;
  font: 600 11px/1 var(--c-mono, monospace); letter-spacing: .04em;
  color: var(--c-primary, #e0355a);
}
.disc-hero__live i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.disc-hero__mini {
  width: 22px; height: 22px; padding: 0;
  border: 0; border-radius: var(--r-sm, 6px);
  background: transparent; color: var(--on-media-2);
  cursor: pointer; line-height: 1;
  display: inline-grid; place-items: center;
  transition: background var(--t-fast, .12s), color var(--t-fast, .12s);
}
.disc-hero__mini .ic { width: 16px; height: 16px; }
.disc-hero__mini:hover { background: rgba(255, 255, 255, .12); color: var(--on-media); }
.disc-hero__mini.off { opacity: .5; }
.disc-hero__title {
  margin: 0 0 12px; max-width: 520px;
  font: 600 clamp(19px, 2.1vw, 30px)/1.32 var(--c-serif, Georgia, serif);
  letter-spacing: -.01em; color: #fff;
  text-shadow: 0 4px 30px rgba(0, 0, 0, .5);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  /* 标题即「更多信息」入口：hover 微亮提示可点（信息层整体不拦截点击，这里单独恢复） */
  pointer-events: auto; cursor: pointer; width: fit-content;
  transition: opacity var(--t-fast);
}
.disc-hero__title:hover { opacity: .82; }
.disc-hero__meta {
  display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 10px;
  font-size: 13px; color: var(--on-media-2);
}
.disc-hero__tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
.disc-hero__tags span {
  padding: 3px 9px; border: 1px solid rgba(255, 255, 255, .16);
  border-radius: var(--r-full, 999px); font-size: 11px; color: var(--on-media-2);
}
.disc-hero__plot {
  margin: 0 0 18px; font-size: 13px; line-height: 1.75;
  color: var(--on-media-2);
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.disc-hero__actions { display: flex; flex-wrap: wrap; gap: 10px; }

.dbtn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  height: 38px; padding: 0 18px;
  border: 1px solid rgba(255, 255, 255, .2); border-radius: var(--r-sm, 6px);
  background: rgba(18, 21, 29, .7); color: var(--on-media);
  font-size: 13px; cursor: pointer;
  transition: background var(--t-fast, .12s), border-color var(--t-fast, .12s), transform var(--t-fast, .12s);
}
.dbtn:hover { background: rgba(255, 255, 255, .12); transform: translateY(-1px); }
.dbtn--solid {
  background: var(--c-primary, #e0355a); border-color: var(--c-primary, #e0355a);
  color: #fff; font-weight: 600;
}
.dbtn--solid:hover { background: var(--c-primary-h, #f04a6d); border-color: var(--c-primary-h, #f04a6d); }
.dbtn--icon { width: 38px; padding: 0; }
.dbtn--heart { color: var(--c-primary, #e0355a); border-color: var(--c-primary-line, rgba(224, 53, 90, .4)); }

/* ---------- 缩略图切换条 ---------- */
.disc-strip {
  position: absolute; left: 0; right: 0; bottom: 0; z-index: 5;
  padding: 0 clamp(20px, 4vw, 64px) 22px;
  opacity: calc(1 - var(--pull, 0) * 1.6);
}
.disc-strip__track {
  display: flex; gap: 10px; overflow-x: auto; padding: 12px 2px 8px;
  scrollbar-width: none; scroll-snap-type: x proximity;
}
.disc-strip__track::-webkit-scrollbar { display: none; }
.disc-strip__item {
  position: relative; flex: 0 0 auto; width: 92px;
  border: 0; padding: 0; background: none; cursor: pointer;
  scroll-snap-align: center;
  opacity: .58; transition: opacity var(--t-base, .2s), transform var(--t-base, .2s);
}
.disc-strip__item:hover { opacity: 1; transform: translateY(-3px); }
.disc-strip__item.on { opacity: 1; }
.disc-strip__thumb {
  width: 100%; aspect-ratio: 2 / 3; object-fit: cover;
  border-radius: var(--r-sm, 6px);
  border: 2px solid transparent;
  background: var(--c-surface, #12151d);
  transition: border-color var(--t-base, .2s);
}
.disc-strip__item.on .disc-strip__thumb { border-color: var(--c-primary, #e0355a); }
/* 桌面 hover 时浮现播放角标，提示「点这张直接播放」 */
.disc-strip__play {
  position: absolute; top: 0; left: 0; right: 0;
  aspect-ratio: 2 / 3;
  display: grid; place-items: center;
  border-radius: var(--r-sm, 6px);
  background: rgba(6, 8, 12, .34);
  opacity: 0; transition: opacity var(--t-base);
  pointer-events: none;
}
.disc-strip__play .ic {
  width: 26px; height: 26px; color: #fff;
  filter: drop-shadow(0 1px 4px rgba(0, 0, 0, .6));
}
.disc-strip__item:hover .disc-strip__play { opacity: 1; }
.disc-strip__label {
  display: block; margin-top: 6px; text-align: center;
  font: 500 10px/1.3 var(--c-mono, monospace); letter-spacing: .06em;
  color: var(--on-media-3);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.disc-strip__item.on .disc-strip__label { color: var(--on-media); }

/* ---------- 内容行 ---------- */
.disc-rows {
  padding: 26px clamp(16px, 3vw, 44px) 80px;
  position: relative; z-index: 6;
}
.disc-row { margin-bottom: 30px; }
.disc-row__head {
  display: flex; align-items: baseline; gap: 10px; margin-bottom: 2px;
}
.disc-row__title {
  margin: 0; font: 600 17px/1.3 var(--c-sans, inherit); color: var(--c-text, #f1f3f8);
}
.disc-row__sub { font-size: 12px; color: var(--c-text-3, #6b7690); }
.disc-row__more {
  margin-left: auto; border: 0; background: none; cursor: pointer;
  font-size: 12px; color: var(--c-text-3, #6b7690);
  transition: color var(--t-fast, .12s);
}
.disc-row__more:hover { color: var(--c-primary, #e0355a); }
.disc-foot {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  margin-top: 40px; padding-top: 24px; border-top: 1px solid var(--c-line, #232936);
}
.disc-foot p { margin: 0; font-size: 12px; color: var(--c-text-3, #6b7690); }

/* ---------- 卡片悬停操作（播放 / 详情） ---------- */
.disc-card-acts {
  position: absolute; right: 10px; bottom: 10px; z-index: 4;
  display: flex; gap: 6px; pointer-events: auto;
}
.disc-card-act {
  width: 32px; height: 32px; padding: 0;
  display: grid; place-items: center;
  border: 1px solid rgba(255, 255, 255, .32); border-radius: 50%;
  background: rgba(8, 10, 14, .74); color: #fff;
  font-size: 12px; cursor: pointer;
  transition: transform var(--t-fast, .12s), background var(--t-fast, .12s), border-color var(--t-fast, .12s);
}
.disc-card-act:hover {
  background: var(--c-primary, #e0355a); border-color: var(--c-primary, #e0355a);
  transform: scale(1.08);
}

/* ---------- 影片详情（页面内视图） ---------- */
.disc-detail {
  /* 用 fixed：详情是整页视图，不能跟着底下的滚动容器一起滚走 */
  position: fixed; inset: 0; z-index: 9;
  overflow-y: auto;
  background: var(--c-bg, #0b0d12);
  padding: 74px clamp(16px, 4vw, 64px) 60px;
}
.disc-detail__back {
  /* 纯箭头圆形小按钮，固定在详情左上角：不与软件名/菜单并列，尽量不抢视觉 */
  position: fixed; top: 16px; left: clamp(16px, 4vw, 64px); z-index: 12;
  width: 32px; height: 32px; padding: 0; margin: 0;
  display: grid; place-items: center;
  border: 1px solid rgba(255, 255, 255, .14); border-radius: 50%;
  background: rgba(8, 10, 16, .5); color: var(--on-media, #f1f3f8);
  cursor: pointer; backdrop-filter: blur(8px);
  transition: background var(--t-fast), border-color var(--t-fast);
}
.disc-detail__back .ic { width: 17px; height: 17px; }
.disc-detail__back:hover { background: rgba(8, 10, 16, .8); border-color: var(--c-primary, #e0355a); }
.disc-detail__grid {
  display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(280px, 1fr);
  gap: clamp(24px, 4vw, 56px);
  max-width: 1240px; margin: 0 auto;
}
.disc-detail__frame {
  position: relative; overflow: hidden;
  aspect-ratio: 16 / 9; background: var(--c-surface, #12151d);
  border: 1px solid var(--c-line, #232936);
}
.disc-detail__frame img { width: 100%; height: 100%; object-fit: cover; cursor: zoom-in; }
/* 封面右下角放大提示 */
.disc-detail__zoom {
  position: absolute; right: 10px; bottom: 10px; z-index: 2;
  width: 30px; height: 30px; display: grid; place-items: center;
  border-radius: var(--r-sm, 6px); background: rgba(8, 10, 16, .6);
  color: #fff; font-size: 15px; pointer-events: none;
}
/* 抽帧缩略条：点任一张进看图 */
.disc-detail__strips {
  display: flex; gap: 6px; margin-top: 8px; overflow-x: auto; padding-bottom: 4px;
}
.disc-detail__strips img {
  flex: 0 0 auto; width: 84px; height: 48px; object-fit: cover;
  border-radius: var(--r-sm, 6px); border: 1px solid var(--c-line, #232936);
  cursor: pointer; opacity: .75; transition: opacity var(--t-fast), border-color var(--t-fast);
}
.disc-detail__strips img:hover, .disc-detail__strips img.on { opacity: 1; border-color: var(--c-primary, #e0355a); }
.disc-detail__blank { width: 100%; height: 100%; display: grid; place-items: center; color: var(--c-text-3, #6b7690); }
.disc-detail__loading {
  position: absolute; left: 12px; top: 12px;
  font: 500 10px/1 var(--c-mono, monospace); letter-spacing: .2em; color: var(--c-text-2, #b6bed0);
}
.disc-detail__acts { display: flex; gap: 8px; margin-top: 12px; }
.disc-detail__code {
  font: 600 10px/1 var(--c-mono, monospace); letter-spacing: .26em;
  text-transform: uppercase; color: var(--c-primary, #e0355a);
}
.disc-detail__title {
  margin: 12px 0 6px;
  font: 600 clamp(20px, 2.2vw, 30px)/1.3 var(--c-serif, Georgia, serif); color: var(--c-text, #f1f3f8);
}
.disc-detail__orig { margin: 0; font-size: 12px; color: var(--c-text-3, #6b7690); }
.disc-detail__meta { display: grid; grid-template-columns: 1fr; gap: 9px; margin: 16px 0 0; }
.disc-detail__meta div { display: flex; gap: 10px; }
.disc-detail__meta dt {
  flex: 0 0 64px; font: 500 9px/1.7 var(--c-mono, monospace);
  letter-spacing: .14em; text-transform: uppercase; color: var(--c-text-3, #6b7690);
}
.disc-detail__meta dd {
  margin: 0; font: 400 11px/1.7 var(--c-mono, monospace); color: var(--c-text, #f1f3f8);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.disc-detail__block { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--c-line, #232936); }
.disc-detail__block h3 {
  margin: 0 0 12px; font: 600 9px/1 var(--c-mono, monospace);
  letter-spacing: .3em; text-transform: uppercase; color: var(--c-primary, #e0355a);
}
.disc-detail__plot {
  margin: 0; font-size: 13px; line-height: 1.9; color: var(--c-text-2, #b6bed0); white-space: pre-line;
}
.disc-detail__tags { display: flex; flex-wrap: wrap; gap: 7px; }
.disc-detail__tags span {
  padding: 5px 11px; border: 1px solid var(--c-line, #232936);
  font-size: 11px; color: var(--c-text-2, #b6bed0);
}
.disc-detail__files { list-style: none; margin: 0; padding: 0; }
.disc-detail__files li {
  display: flex; justify-content: space-between; gap: 14px;
  padding: 8px 0; border-bottom: 1px solid var(--c-line, #232936);
  font: 400 10px/1.4 var(--c-mono, monospace); color: var(--c-text-2, #b6bed0);
}
.disc-detail__files span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.disc-detail__files em { flex: 0 0 auto; font-style: normal; color: var(--c-primary, #e0355a); }

/* ---------- 详情看图灯箱（对齐经典页 lightbox 视觉） ---------- */
.disc-lb {
  position: fixed; inset: 0; z-index: 30;
  display: flex; align-items: center; justify-content: center;
  background: #000; animation: disc-lb-in .18s ease-out;
}
@keyframes disc-lb-in { from { opacity: 0; } to { opacity: 1; } }
.disc-lb__bg {
  position: absolute; inset: 0; background-size: cover; background-position: center;
  filter: blur(48px) brightness(.4) saturate(1.15); pointer-events: none;
}
.disc-lb__img {
  position: relative; z-index: 1; max-width: 94vw; max-height: 92vh;
  object-fit: contain; border-radius: 6px;
}
.disc-lb__close, .disc-lb__nav {
  display: grid; place-items: center; padding: 0;
  width: 42px; height: 42px; border-radius: 50%;
  border: 1px solid rgba(255, 255, 255, .28); background: rgba(8, 10, 16, .62);
  color: #fff; cursor: pointer;
  transition: background var(--t-fast);
}
.disc-lb__close .ic, .disc-lb__nav .ic { width: 19px; height: 19px; }
.disc-lb__close:hover, .disc-lb__nav:hover { background: rgba(255, 255, 255, .2); }
.disc-lb__close { position: absolute; top: 14px; right: 16px; z-index: 2; }
.disc-lb__nav { position: absolute; top: 50%; transform: translateY(-50%); z-index: 2; }
.disc-lb__nav.prev { left: 14px; }
.disc-lb__nav.next { right: 14px; }
.disc-lb__count {
  position: absolute; bottom: 18px; left: 50%; transform: translateX(-50%); z-index: 2;
  color: rgba(255, 255, 255, .8); font-size: 12px; font-variant-numeric: tabular-nums;
  background: rgba(0, 0, 0, .55); padding: 5px 12px; border-radius: 999px;
}

/* ---------- 播放界面控制条（叠在全局播放器之上） ---------- */
.disc-playbar {
  position: fixed; top: 0; left: 0; right: 0; z-index: 320;
  display: flex; align-items: center; gap: 10px;
  height: 52px; padding: 0 clamp(12px, 2vw, 24px);
  background: rgba(8, 10, 14, .72);
  border-bottom: 1px solid rgba(255, 255, 255, .1);
  backdrop-filter: blur(12px);
}
.disc-playbar__btn {
  height: 30px; padding: 0 12px;
  border: 1px solid rgba(255, 255, 255, .22); border-radius: var(--r-sm, 6px);
  background: transparent; color: #f1f3f8; font-size: 11px; cursor: pointer;
  white-space: nowrap;
}
.disc-playbar__btn:hover { border-color: var(--c-primary, #e0355a); color: var(--c-primary, #e0355a); }
.disc-playbar__title {
  flex: 1; min-width: 0; text-align: center;
  font: 500 12px/1 var(--c-mono, monospace); color: rgba(242, 243, 248, .82);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

.disc-view-enter-active, .disc-view-leave-active { transition: opacity .22s ease, transform .22s ease; }
.disc-view-enter-from { opacity: 0; transform: translateY(10px); }
.disc-view-leave-to { opacity: 0; }

/* ---------- 过渡 ---------- */
.disc-fade-enter-active, .disc-fade-leave-active { transition: opacity var(--fade-dur, .32s) ease; }
.disc-fade-enter-from, .disc-fade-leave-to { opacity: 0; }
.disc-slide-enter-active, .disc-slide-leave-active { transition: opacity .34s ease, transform .34s ease; }
.disc-slide-enter-from { opacity: 0; transform: translateY(14px); }
.disc-slide-leave-to { opacity: 0; transform: translateY(-8px); }

/* ---------- 响应式 ---------- */
/* 尊重系统的「减少动态效果」：停掉背景推近 */
@media (prefers-reduced-motion: reduce) {
  .disc-hero__bg img { animation: none; }
}
@media (max-width: 900px) {
  .disc-top__nav { display: none; }
  .disc-top__search { min-width: 0; width: 150px; }
  .disc-detail__grid { grid-template-columns: 1fr; }
}
@media (max-width: 640px) {
  .disc-hero { height: 74vh; min-height: 440px; }
  .disc-hero__info { padding: 0 16px 168px; }
  .disc-hero__title { font-size: clamp(21px, 6vw, 30px); }
  .disc-hero__plot { -webkit-line-clamp: 2; font-size: 12px; }
  .disc-strip { padding: 0 16px 16px; }
  .disc-strip__item { width: 72px; }
  .disc-peek { display: none; }
  .disc-rows { padding: 20px 12px 60px; }
  .disc-row { margin-bottom: 22px; }
  .disc-row__title { font-size: 15px; }
  .disc-foot { flex-direction: column; align-items: flex-start; }
}
</style>
