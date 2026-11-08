<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import videojs from 'video.js'
import 'video.js/dist/video-js.css'
import { streamUrl, strmHlsUrl, streamInfo, setProgress, playMovie, markPlayed, startSession, updateSession, endSession, getPreviews, genPreviews, playbackInfo, prepareStream, prepareStreamStatus, subtitleTracks, setSkipPoints, saveSubtitleSync, anchorSubtitleSync, reportSourceFailure } from '../api.js'
import { toast, fmtClock } from '../utils.js'
import { t } from '../i18n/index.js'
import SubtitleConsole from './player/SubtitleConsole.vue'
import VideoConsole from './player/VideoConsole.vue'
import PlayerSettings from './player/PlayerSettings.vue'
import { useSubtitleSync } from '../composables/useSubtitleSync.js'

const props = defineProps({
  movieId: { type: Number, required: true },
  startAt: { type: Number, default: 0 },
  autoplay: { type: Boolean, default: true },   // 打开播放器后是否自动开始播放
})
const emit = defineEmits(['close', 'progress', 'ended'])

const containerEl = ref(null)
const failed = ref(false)
const ready = ref(false)
const cur = ref(0)
const dur = ref(0)
let player = null          // video.js player 实例
let videoEl = null         // 底层 <video> 元素
/* ---- 播放器浮层：统一设置面板（字幕 / 画面） / 片头片尾 ---- */
const uiReady = ref(false)
const setMenuOpen = ref(false)
const markOpen = ref(false)
/* CC 按钮要挂进 video.js 控制条（原生按钮那一排），用 Teleport 渲染进去，
   这样它的位置、显示/隐藏（含自动隐藏）都跟着控制条走，不用自己维护 */
const ccHost = ref(null)
/* 已播时间：video.js 皮肤把 current-time 和 duration 一起 `display:none` 了
   （见官方 video-js.css），硬打开会缺配套布局、和右边不齐。这里改用同一套
   类名（vjs-time-control / vjs-control / vjs-current-time-display）渲染一个
   节点插到 duration 前面，字号行高内边距全部继承官方样式，天然一致。 */
const timeHost = ref(null)
function fmtShort(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = String(s % 60).padStart(2, '0')
  return h ? h + ':' + String(m).padStart(2, '0') + ':' + ss : m + ':' + ss
}
function mountTimeHost() {
  const bar = player && player.controlBar && player.controlBar.el()
  if (!bar) return
  let host = bar.querySelector('.vp-time-host')
  if (!host) {
    host = document.createElement('div')
    host.className = 'vp-time-host'
    // display:contents 让容器不生成盒子，内部控件直接成为控制条的 flex item
    host.style.display = 'contents'
    // 必须插在 timeDivider 之前：控制条原生顺序是 current / divider / duration，
    // 插到 duration 前会跑到分隔符右边，变成「/ 1:54 2:01:44」。
    const divider = bar.querySelector('.vjs-time-divider')
    const dur = bar.querySelector('.vjs-duration')
    if (divider) bar.insertBefore(host, divider)
    else if (dur) bar.insertBefore(host, dur)
    else bar.appendChild(host)
  }
  timeHost.value = host
}
/* 设置面板是重交互面板（滑块 / 候选列表 / 分段按钮），不搞悬停展开也不搞
   悬停收起：鼠标扫过控制条就弹面板、点完按钮又被悬停顶开，都是「关不掉」的来源。
   统一规则：点击 ⚙ 开 / 再点关；点面板外、Esc、面板右上角 ✕ 都能关；
   选完字幕轨也自动收起（和原生播放器的轨道菜单行为一致）。 */
function toggleSetMenu() {
  setMenuOpen.value = !setMenuOpen.value
}
function pickSub(id) {
  selectSub(id)
  setMenuOpen.value = false
}
function onDocClick(e) {
  if (!setMenuOpen.value) return
  const host = ccHost.value
  if (host && (host === e.target || host.contains(e.target))) return
  // 控制条里的其它操作（拖进度条、暂停、调音量）不该收起面板：
  // 校准时经常要边看画面边调时间，收起后又要重开、重切 tab。
  const bar = player && player.controlBar && player.controlBar.el()
  if (bar && (bar === e.target || bar.contains(e.target))) return
  setMenuOpen.value = false
}
/* 右上角浮层跟着控制条同节奏淡出：静止播放时隐藏，鼠标一动就回来 */
const barActive = ref(true)
/* 字幕台挂在控制条里，video.js 判定用户静止后会连控制条一起淡出。
   面板打开期间持续把 player 标成 active，否则看候选列表读到一半就被藏掉。
   跳过标记菜单不在 player 元素内，鼠标移过去不会触发 useractive，同样要保活。 */
let barKeepTimer = 0
function keepBarActive() {
  barActive.value = true
  if (player && typeof player.userActive === 'function') player.userActive(true)
}
watch([setMenuOpen, markOpen], ([settings, mark]) => {
  clearInterval(barKeepTimer)
  if (!settings && !mark) return
  keepBarActive()
  barKeepTimer = setInterval(keepBarActive, 1000)
})
/* 字幕样式：字号 / 背景 / 垂直位置，控件在 SubtitleConsole 里 */
function setSubStyle(key, val) {
  subStyle.value[key] = val
  applySubStyle()   // 立即生效，不必关掉播放器再打开
  savePrefs()
}
/** 在控制条里插一个空位（全屏按钮左边），CC 用 Teleport 渲染进去 */
function mountCcHost() {
  const bar = player && player.controlBar && player.controlBar.el()
  if (!bar) return
  let host = bar.querySelector('.vjs-cc-host')
  if (!host) {
    host = document.createElement('div')
    host.className = 'vjs-cc-host'
    const fs = bar.querySelector('.vjs-fullscreen-control')
    if (fs) bar.insertBefore(host, fs)
    else bar.appendChild(host)
  }
  ccHost.value = host
}

/* ---- 画面调整：旋转 / 翻转 / 缩放（按影片记忆）+ 画质（全局偏好） ----
 * 全部是显示层调整（CSS transform / filter），实时生效、不转码：
 * - 方向跟具体片源走（竖拍 / 旋转编码的源），按影片记忆到 localStorage；
 * - 画质是观看偏好，全局生效；
 * - 锐化用 SVG feConvolveMatrix 卷积核，强度 0 时是恒等核（等于关闭）。 */
const ADJ_DEFAULT = {
  rot: 0, flipH: false, flipV: false, zoom: 1,
  bright: 100, contrast: 100, sat: 100, hue: 0, sharpen: 0,
}
const PICTURE_KEY = 'avm.vp.picture'
const ORIENT_KEY = 'avm.vp.orient'

function loadAdj() {
  let pic = {}
  let orient = {}
  try { pic = JSON.parse(localStorage.getItem(PICTURE_KEY) || '{}') } catch (e) { /* 忽略 */ }
  try { orient = JSON.parse(localStorage.getItem(ORIENT_KEY) || '{}') } catch (e) { /* 忽略 */ }
  const mine = (orient.byKey || {})[String(props.movieId)] || {}
  return { ...ADJ_DEFAULT, ...(pic || {}), rot: Number(mine.rot) || 0,
           flipH: !!mine.flipH, flipV: !!mine.flipV }
}
const adj = reactive(loadAdj())

watch(() => props.movieId, () => { Object.assign(adj, loadAdj()) })

function savePicture() {
  const { zoom, bright, contrast, sat, hue, sharpen } = adj
  try {
    localStorage.setItem(PICTURE_KEY, JSON.stringify({ zoom, bright, contrast, sat, hue, sharpen }))
  } catch (e) { /* 隐私模式忽略 */ }
}
function saveOrient() {
  let o = {}
  try { o = JSON.parse(localStorage.getItem(ORIENT_KEY) || '{}') } catch (e) { /* 忽略 */ }
  o.byKey = o.byKey || {}
  o.byKey[String(props.movieId)] = { rot: adj.rot, flipH: adj.flipH, flipV: adj.flipV }
  try { localStorage.setItem(ORIENT_KEY, JSON.stringify(o)) } catch (e) { /* 忽略 */ }
}
function onAdjChange(next) {
  Object.assign(adj, next)
  savePicture()
  saveOrient()
}

/* 容器尺寸：旋转 90/270 后内容包围盒会转出容器，按「容器高/宽」补偿缩放，
   转正的画面才能完整放进可视区（等比缩小、左右留黑边，和桌面播放器一致）。 */
const wrapSize = reactive({ w: 0, h: 0 })
let wrapRO = null
function watchWrapSize() {
  if (!containerEl.value || typeof ResizeObserver === 'undefined') return
  if (wrapRO) wrapRO.disconnect()
  wrapRO = new ResizeObserver((entries) => {
    const r = entries && entries[0]
    if (!r) return
    wrapSize.w = Math.round(r.contentRect.width)
    wrapSize.h = Math.round(r.contentRect.height)
  })
  wrapRO.observe(containerEl.value)
}

const videoVars = computed(() => {
  const { rot, flipH, flipV, zoom, bright, contrast, sat, hue, sharpen } = adj
  const deg = Number(rot) || 0
  const turned = deg % 180 !== 0
  const comp = turned && wrapSize.w > 0 && wrapSize.h > 0 ? wrapSize.h / wrapSize.w : 1
  const sx = (flipH ? -1 : 1) * zoom * comp
  const sy = (flipV ? -1 : 1) * zoom * comp
  const changed = deg || flipH || flipV || zoom !== 1
  const fx = []
  if (bright !== 100) fx.push(`brightness(${(bright / 100).toFixed(2)})`)
  if (contrast !== 100) fx.push(`contrast(${(contrast / 100).toFixed(2)})`)
  if (sat !== 100) fx.push(`saturate(${(sat / 100).toFixed(2)})`)
  if (hue) fx.push(`hue-rotate(${hue}deg)`)
  if (sharpen > 0) fx.push('url(#vp-sharpen)')
  return {
    '--vp-transform': changed ? `rotate(${deg}deg) scale(${sx.toFixed(4)}, ${sy.toFixed(4)})` : 'none',
    '--vp-filter': fx.length ? fx.join(' ') : 'none',
  }
})

/* 锐化卷积核：0 -k 0 / -k 1+4k -k / 0 -k 0，k∈[0,1]。
 * k=0 是恒等核（等于关闭），k=1 是经典 3×3 锐化；面板上的滑块直接驱动它。 */
const sharpenKernel = computed(() => {
  const k = (Number(adj.sharpen) || 0) / 100
  return `0 ${-k} 0 ${-k} ${1 + 4 * k} ${-k} 0 ${-k} 0`
})
const subTracks = ref([])
const activeSub = ref('')
const subStyle = ref({ size: 'm', bg: '1', posY: 0 })
const subStatus = ref('unchecked')
/* 当前显示轨的字幕覆盖区间：让用户一眼看到「字幕从第几秒开始」，
   而不是在画面里干等（校准偏移过大时最需要这个反馈）。 */
const cueRange = ref(null)
const sync = useSubtitleSync()
/* 预览一律走后端：把字幕轨的 src 换成带 offset/scale 的 VTT 地址，
   浏览器原生解析，时间轴顺序与显示区间都由后端保证。这样暂停也能生效，
   不存在「改了 cue 浏览器不重算」的问题。拖动滑块时防抖 300ms，避免连续重载。 */
let syncPreviewTimer = 0
let syncPreviewBusy = false
/* 拖动时先直接改 cue（零延迟跟随），停手 450ms 后再让后端重渲染一次做校正。
   这样既是「拖着字幕就动」的手感，最终值又由后端保证，不依赖浏览器行为。 */
function scheduleSyncApply() {
  if (!player || typeof player.textTracks !== 'function') return
  sync.applyToTracks(player.textTracks())
  clearTimeout(syncPreviewTimer)
  // 600ms 是为了让「连续拖动」期间不重载：重载会闪一下，停手后再校正一次最平滑
  syncPreviewTimer = setTimeout(() => { reloadPreviewTracks() }, 600)
}
async function reloadPreviewTracks() {
  if (!sync.dirty.value || syncPreviewBusy) return
  syncPreviewBusy = true
  try {
    clearRemoteTracks()
    await loadSubtitleTracks(true)
  } catch (e) {
    /* 预览失败保持原状，不影响已保存的轨道 */
  } finally {
    syncPreviewBusy = false
  }
}
const skipIntro = ref(0)
const skipOutro = ref(0)
const skipToast = ref('')
let skipUndoAt = 0
/* 每次加载只自动跳一次，避免用户想回看片头/片尾时被反复拉走 */
let introJumped = false
let outroJumped = false
const canMarkIntro = ref(true)
const canMarkOutro = ref(false)

const PREF_KEY = 'avm.player.prefs'

function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(PREF_KEY) || '{}')
    if (p.subStyle) subStyle.value = Object.assign(subStyle.value, p.subStyle)
    if (p.sub) activeSub.value = p.sub || ''
  } catch (e) { /* 忽略 */ }
}
function savePrefs() {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify({
      subStyle: subStyle.value, sub: activeSub.value,
      volume: player ? Math.round(player.volume() * 100) : undefined,
      rate: player ? player.playbackRate() : undefined,
    }))
  } catch (e) { /* 忽略 */ }
}
function applySubStyle() {
  const sizes = { s: '16px', m: '22px', l: '30px' }
  const bgs = { '0': 'transparent', '1': 'rgba(0,0,0,.55)', '2': 'rgba(0,0,0,.9)' }
  const el = containerEl.value
  if (!el) return
  el.style.setProperty('--vp-sub-size', sizes[subStyle.value.size] || sizes.m)
  el.style.setProperty('--vp-sub-bg', bgs[subStyle.value.bg] || bgs['1'])
  // 正值上移：影片自带烧录硬字幕时把外挂字幕挪开，避免两行字叠在一起
  el.style.setProperty('--vp-sub-y', (Number(subStyle.value.posY || 0) * 12) + 'px')
  // 无背景时给白字补描边（否则压在亮画面上基本看不清）
  el.classList.toggle('vp-sub-plain', String(subStyle.value.bg) === '0')
}

function onConsoleStyle(key, val) {
  if (key === 'posY') {
    subStyle.value.posY = Number(val) || 0
    sync.setPosY(Number(val) || 0)
    applySubStyle()
    savePrefs()
    scheduleSyncApply()
  } else {
    setSubStyle(key, val)
  }
}
/** 恢复上次的播放偏好（音量 / 倍速 / 字幕），没有则给保守默认 */
function restorePrefs() {
  if (!player) return
  let p = {}
  try { p = JSON.parse(localStorage.getItem(PREF_KEY) || '{}') } catch (e) { p = {} }
  player.muted(false)
  player.volume(typeof p.volume === 'number' ? Math.min(1, Math.max(0, p.volume / 100)) : 0.3)
  if (typeof p.rate === 'number') player.playbackRate(p.rate)
}

/** 系统媒体控制（手机锁屏 / 耳机按键）：显示标题封面，可播放暂停与 seek */
function setupMediaSession() {
  if (!('mediaSession' in navigator)) return
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: props.movieId ? ('#' + props.movieId) : '片匣',
      artist: 'AVM',
    })
    navigator.mediaSession.setActionHandler('play', () => player && player.play())
    navigator.mediaSession.setActionHandler('pause', () => player && player.pause())
    navigator.mediaSession.setActionHandler('seekbackward', () => {
      if (videoEl) videoEl.currentTime = Math.max(0, videoEl.currentTime - 10)
    })
    navigator.mediaSession.setActionHandler('seekforward', () => {
      if (videoEl) videoEl.currentTime = videoEl.currentTime + 10
    })
  } catch (e) { /* 旧浏览器忽略 */ }
}

async function loadSubtitleTracks(keepPreview = false) {
  // 每次重载先清干净：残留的同名轨会让 applySubMode 按 label 匹配到多条，
  // 轻则重复、重则全都 disabled（字幕整个不见）。初次加载时这里是空操作。
  clearRemoteTracks()
  try {
    const r = await subtitleTracks(props.movieId)
    subTracks.value = r.tracks || []
    skipIntro.value = r.skip_intro || 0
    skipOutro.value = r.skip_outro || 0
    subStatus.value = r.status || (subTracks.value.length ? 'unchecked' : 'none')
    // 偏好命中则用它，否则默认第一条（优先中文）
    const want = activeSub.value
    const pick = (want && subTracks.value.find((x) => x.id === want))
      || subTracks.value.find((x) => (x.lang || '').startsWith('zh'))
      || subTracks.value[0]
    // 本片校准值优先于全局显示偏好；没有记录时沿用全局。
    // 预览重载（keepPreview）不能覆盖正在编辑的值。
    const s = pick && pick.sync
    if (!keepPreview) {
      if (s) {
        subStyle.value.posY = Number(s.pos_y || 0)
        sync.setPosY(Number(s.pos_y || 0))
      }
      sync.load(pick ? pick.id : '', s)
    }
    applySubStyle()
    subTracks.value.forEach((track) => {
      player.addRemoteTextTrack({
        kind: 'subtitles', src: sync.trackUrl(track.url),
        srclang: track.lang || 'und',
        label: track.label, default: !!pick && track.id === pick.id,
      }, false)
    })
    activeSub.value = pick ? pick.id : ''
    // 动态添加的 track 靠 default 属性并不能保证显示：保存后重载轨道时若不显式
    // 指定 mode，所有轨都是 disabled，表现就是「校准完字幕整个不见了」。
    applySubMode(activeSub.value)
    updateCueRange()
    // 记下这一轮 VTT 是按什么参数渲染的，后续 drag 的差量以它为原点
    sync.markRendered()
  } catch (e) { /* 没有字幕轨时忽略 */ }
  finally { uiReady.value = true }
}

/** 刷新字幕覆盖区间（cues 变化不是响应式的，只能在时间推进/重载时主动读）。 */
function updateCueRange() {
  if (!player || typeof player.textTracks !== 'function') return
  const list = player.textTracks()
  for (let i = 0; i < list.length; i++) {
    const tt = list[i]
    if (!tt || tt.mode !== 'showing') continue
    const cues = tt.cues || []
    if (!cues.length) continue
    cueRange.value = {
      first: cues[0].startTime,
      last: cues[cues.length - 1].endTime,
    }
    return
  }
  cueRange.value = null
}

/** 显式指定哪条字幕轨显示（按 label 对齐，video.js 会在原生轨之后追加远程轨）。 */
function applySubMode(id) {
  const tracks = (player && typeof player.textTracks === 'function' && player.textTracks())
    || (videoEl && videoEl.textTracks) || []
  const list = subTracks.value
  for (let i = 0; i < tracks.length; i++) {
    const tt = tracks[i]
    if (tt.kind !== 'subtitles' && tt.kind !== 'captions') continue
    const hit = list.find((x) => x.label === tt.label)
    tt.mode = (hit && hit.id === id) ? 'showing' : 'disabled'
  }
}

/** 保存后重新拉轨道：后端 VTT 会带上绝对偏移，前端差量归零，避免双倍平移。 */
function clearRemoteTracks() {
  if (!player) return
  // removeRemoteTextTrack 要传 <track> 元素，传 TextTrack 对象是无效的：
  // 旧实现用 textTracks() 导致轨删不掉，重载后同名轨越叠越多、mode 也被打乱。
  try {
    const els = typeof player.remoteTextTrackEls === 'function' && player.remoteTextTrackEls()
    if (els) {
      for (let i = els.length - 1; i >= 0; i--) {
        if (els[i]) player.removeRemoteTextTrack(els[i])
      }
      return
    }
  } catch (e) { /* 回退到下面的兜底 */ }
  if (typeof player.textTracks !== 'function') return
  const list = player.textTracks()
  for (let i = list.length - 1; i >= 0; i--) {
    const tt = list[i]
    if (tt && (tt.kind === 'subtitles' || tt.kind === 'captions')) {
      try { player.removeRemoteTextTrack(tt) } catch (e) { /* 原生轨不可移除 */ }
    }
  }
}
async function saveSyncNow() {
  if (!sync.subId.value) return false
  try {
    const r = await saveSubtitleSync(props.movieId, sync.subId.value, sync.payload())
    sync.markSaved(r)
    toast(t('player.scSaved'), 'ok')
    clearRemoteTracks()
    await loadSubtitleTracks()
    return true
  } catch (e) {
    toast(e.message, 'err')
    return false
  }
}
async function reloadTracks() {
  clearRemoteTracks()
  await loadSubtitleTracks()
}
function onConsoleOffset(ms) {
  sync.setOffset(ms)
  scheduleSyncApply()
}
function onConsoleSuggest(s) {
  sync.applySuggestion(s)
  scheduleSyncApply()
}
function onConsoleScale(v) {
  sync.setScale(v)
  scheduleSyncApply()
}
/* 只重置偏移，保留帧率比：这部片一旦把 0.959 清成 1.0，开头对齐了后面也必然漂 */
function onConsoleReset() {
  sync.setOffset(0)
  sync.setPosY(0)
  applySubStyle()
  scheduleSyncApply()
}
/* 对齐当前正在显示的那条字幕：用户在画面里看到哪句台词，就把它锚到当前时间。
   纯前端计算，不需要请求；粗调过偏移、字幕已经在显示时最顺手。 */
function onConsoleAnchorActive() {
  if (!sync.subId.value) return
  const now = (videoEl && videoEl.currentTime) || 0
  if (!now) {
    toast(t('player.scAnchorNoTime'), 'err')
    return
  }
  const tracks = (player && typeof player.textTracks === 'function') ? player.textTracks() : null
  const origStart = sync.activeCueOriginalStart(tracks, now)
  if (origStart == null) {
    toast(t('player.scNoActiveCue'), 'err')
    return
  }
  const scale = sync.current.value.scale || 1
  sync.applySuggestion({
    offset_ms: Math.round((now - origStart * scale) * 1000),
    scale,
    method: 'anchor',
    confidence: 0.95,
  })
  scheduleSyncApply()
  toast(t('player.scAnchored', { time: fmtClock(now) }), 'ok')
}

/* 从字幕列表挑一条对齐：用户读出「就是这句」，把它锚到当前画面（纯前端算） */
function onConsoleAnchorCue(payload) {
  if (!sync.subId.value) return
  const now = (videoEl && videoEl.currentTime) || 0
  if (!now) {
    toast(t('player.scAnchorNoTime'), 'err')
    return
  }
  const start = Number(payload && payload.start)
  if (!Number.isFinite(start)) return
  const scale = sync.current.value.scale || 1
  sync.applySuggestion({
    offset_ms: Math.round((now - start * scale) * 1000),
    scale,
    method: 'anchor',
    confidence: 0.95,
  })
  scheduleSyncApply()
  toast(t('player.scAnchored', { time: fmtClock(now) }), 'ok')
}

/* 一键锚点：用户播到「应该是字幕第 N 条」的画面时点一下，直接解出 offset */
async function onConsoleAnchor(payload) {
  if (!sync.subId.value) return
  const videoTime = (videoEl && videoEl.currentTime) || 0
  if (!videoTime) {
    toast(t('player.scAnchorNoTime'), 'err')
    return
  }
  try {
    const r = await anchorSubtitleSync(props.movieId, sync.subId.value, {
      video_time: videoTime,
      cue_index: Number((payload && payload.cueIndex) || 0),
      scale: sync.current.value.scale,
    })
    sync.applySuggestion(r)
    scheduleSyncApply()
    toast(t('player.scAnchored', { time: fmtClock(videoTime) }), 'ok')
  } catch (e) {
    toast(e.message, 'err')
  }
}
async function selectSub(id) {
  // 切轨前先落盘未保存的微调，避免用户调好的偏移被悄悄丢掉
  if (sync.dirty.value && String(id) !== sync.subId.value) await saveSyncNow()
  activeSub.value = id
  const chosen = subTracks.value.find((x) => x.id === id)
  sync.load(id || '', chosen ? chosen.sync : null)
  // 远程字幕轨（addRemoteTextTrack）在 video.js 里不一定会生成 <video><track>，
  // 所以必须走 player.textTracks()；只看 videoEl.textTracks 会得到空列表，
  // 表现就是「点了字幕没有任何反应」。
  applySubMode(id)
  savePrefs()
}
function fmtSkip(sec) {
  const s = Math.max(0, Math.round(sec || 0))
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0')
}
function markSkip(kind) {
  const v = videoEl
  if (!v) return
  const sec = Math.round(v.currentTime)
  const payload = kind === 'intro' ? { intro_end: sec } : { outro_start: sec }
  setSkipPoints(props.movieId, payload).then(() => {
    if (kind === 'intro') { skipIntro.value = sec; toast(t('player.markIntroDone', { t: fmtSkip(sec) }), 'ok') }
    else { skipOutro.value = sec; toast(t('player.markOutroDone', { t: fmtSkip(sec) }), 'ok') }
    markOpen.value = false
  }).catch((e) => toast(e.message, 'err'))
}
function clearSkip(kind) {
  const payload = kind === 'intro' ? { intro_end: 0 } : kind === 'outro' ? { outro_start: 0 } : { intro_end: 0, outro_start: 0 }
  setSkipPoints(props.movieId, payload).then(() => {
    if (kind !== 'outro') skipIntro.value = 0
    if (kind !== 'intro') skipOutro.value = 0
    markOpen.value = false
    toast(t('player.skipCleared'), 'ok')
  }).catch((e) => toast(e.message, 'err'))
}
function markIntro() { markSkip('intro') }
function markOutro() { markSkip('outro') }
function showSkipToast(msg, target) {
  skipToast.value = msg
  skipUndoAt = target
  clearTimeout(showSkipToast._t)
  showSkipToast._t = setTimeout(() => { skipToast.value = '' }, 6000)
}
function undoSkip() {
  const v = videoEl
  if (v && skipUndoAt > 0) v.currentTime = skipUndoAt
  skipToast.value = ''
}
function checkSkip() {
  const v = videoEl
  if (!v || !Number.isFinite(v.duration)) return
  const now = v.currentTime
  if (!introJumped && skipIntro.value > 5 && now > 1 && now < skipIntro.value - 1) {
    introJumped = true
    v.currentTime = skipIntro.value
    showSkipToast(t('player.skippedIntro', { t: fmtSkip(skipIntro.value) }), now)
    return
  }
  // skipOutro 存的是「片尾开始」的绝对秒数，不能当成距结束的剩余时长来比较
  if (!outroJumped && skipOutro.value > 0 && now >= skipOutro.value && v.duration - now > 2) {
    outroJumped = true
    v.currentTime = v.duration
    showSkipToast(t('player.skippedOutro', { t: fmtSkip(skipOutro.value) }), now)
  }
}
let saveTimer = null
let lastSaved = 0
let sessionId = null      // 当前观看会话 id（用于写入观看明细）
let watchedSec = 0        // 本次会话累计真实观看秒数
let lastTick = 0          // 上一次 timeupdate 的 currentTime
let clickTimer = null     // 区分单击/双击的定时器
let overlayEl = null      // 透明覆盖层，独占画面点击，隔离 video.js 内部 click 竞争

// ---- 移动端横滑快进/快退 ----
let swipeTipEl = null     // 滑动进度提示
let swipe = { active: false, dir: 0, startX: 0, startY: 0, startTime: 0, targetTime: 0 }

// ---- 进度条 hover 预览（视频进度条预览）----
let progressCtrl = null   // video.js 进度条容器 (.vjs-progress-control)
let tipEl = null          // 预览浮层
let tipImgEl = null       // 预览缩略图 <img>
let tipTimeEl = null      // 预览时间标签
let previewUrls = []      // 预览图 URL 数组（与后端等距抽帧顺序一致）
let hoverBound = false    // 是否已绑定进度条 hover 事件

// 网络串流（.strm）：'' = 普通文件；'strm' = 直连地址；'hls' = m3u8（走同源代理播放列表）
const streamMode = ref('')
const directSrc = ref('')   // HLS 浏览器直连地址（仅开关开启时后端下发）
let proxyTried = false
let hlsProxyTried = false
let sourceReady = false
let failoverTried = false

/** 播放前问一次后端：这部片是不是网络串流、要不要按 HLS 处理（轻量接口，无 ffprobe） */
async function resolveSource() {
  streamMode.value = ''
  directSrc.value = ''
  proxyTried = false
  hlsProxyTried = false
  try {
    const info = await streamInfo(props.movieId)
    if (info && info.stream) streamMode.value = info.hls ? 'hls' : 'strm'
    // 开关开启时优先进浏览器直连（源站对人机校验敏感、服务端代理被拦的场景）
    if (info && info.hls && info.hls_direct && info.direct_url) directSrc.value = info.direct_url
  } catch (e) { /* 忽略：按普通文件处理 */ }
  sourceReady = true
}

function currentSource() {
  if (streamMode.value === 'hls') {
    return { src: directSrc.value || strmHlsUrl(props.movieId), type: 'application/x-mpegURL' }
  }
  return { src: streamUrl(props.movieId), type: 'video/mp4' }
}

// 仅当访问地址为 localhost/127.0.0.1 时才提供"系统播放器"（远程设备点了也无效）
const isRemote = !['localhost', '127.0.0.1'].includes(location.hostname)
// 触摸设备检测（移动端/平板）：播放器交互策略与桌面不同
const isTouch = typeof window !== 'undefined' && ('ontouchstart' in window || (navigator.maxTouchPoints || 0) > 0)

function onLoaded() {
  ready.value = true
  const v = videoEl
  if (!v) return
  // 在元数据就绪后显式设置初始音量（video.js 的 volume 选项在 autoplay 下常被忽略，
  // 这里强制设为较小音量，避免一打开声音过大）
  restorePrefs()
  // 直播流（HLS）duration 为 Infinity：不要写进 dur，否则进度条会算错
  dur.value = Number.isFinite(v.duration) ? v.duration : 0
  introJumped = false
  outroJumped = false
  canMarkOutro.value = dur.value > 0 && v.currentTime > dur.value * 0.5
  setupMediaSession()
  // AI 问答里点的时间戳：优先于「继续观看」的起始位置
  const jump = pendingSeek
  pendingSeek = 0
  if (jump > 0 && jump < (v.duration || 0) - 2) v.currentTime = jump
  // 重封装后重新加载：只恢复播放进度，不重复计数、不重复开会话
  if (reloading) {
    reloading = false
    if (!jump && props.startAt > 0 && props.startAt < (v.duration || 0) - 5) v.currentTime = props.startAt
    return
  }
  if (!jump && props.startAt > 0 && props.startAt < (v.duration || 0) - 5) {
    v.currentTime = props.startAt
    toast(t('player.jumpedTo', { time: fmtClock(props.startAt) }), '', 2200)
  }
  // 网页播放器开始播放时记录一次播放次数（与外部播放器保持一致）
  markPlayed(props.movieId).catch(() => {})
  // 开启观看会话，让本次播放进入"观看明细/分析"
  watchedSec = 0
  lastTick = v.currentTime || 0
  startSession(props.movieId, { start_pos: props.startAt || 0, method: 'web' })
    .then((r) => { sessionId = r.session_id ?? null })
    .catch(() => { sessionId = null })
  // 进度条 hover 预览：读取已有预览图，没有则后台异步生成（不阻塞播放）
  setupPreviewHover()
}

function onTime() {
  const v = videoEl
  if (!v) return
  const now = v.currentTime
  // 累计真实观看秒数（拖动进度条不计入；钳制到合理区间）
  if (lastTick >= 0 && now >= lastTick && now - lastTick <= 10) {
    watchedSec += now - lastTick
  }
  lastTick = now
  cur.value = now
  if (Number.isFinite(v.duration)) dur.value = v.duration
  if (dur.value > 0) canMarkOutro.value = now > dur.value * 0.5
  checkSkip()
  updateCueRange()
}

/** 每 10 秒回写一次进度与观看时长 */
async function saveProgress(force = false) {
  const v = videoEl
  // 直播流时长为 Infinity：没有"进度"可存，跳过（否则后端会存进一个巨大数）
  if (!v || !Number.isFinite(v.duration) || v.duration <= 0) return
  const pos = v.currentTime
  if (!force && Math.abs(pos - lastSaved) < 10) return
  lastSaved = pos
  try {
    await setProgress(props.movieId, { position: pos, duration: v.duration })
    emit('progress', { position: pos, duration: v.duration })
    if (sessionId != null) {
      await updateSession(props.movieId, sessionId, { watched_sec: Math.round(watchedSec) }).catch(() => {})
    }
  } catch (e) { /* 静默 */ }
}

// 每部影片只自动降级一次：video.js 可能对同一次故障连续派发多个 error 事件，
// 不防重会反复拉起系统播放器。切换影片时随播放器重建而重置。
let autoExternalDone = false
// 是否已尝试过「重封装后网页播放」（每部影片只试一次，避免失败后死循环）
let remuxTried = false
// 重封装完成后重设视频源时置真：让 onLoaded 跳过重复计数/重复开会话
let reloading = false

const preparing = ref(false)
const preparePercent = ref(0)
const prepareMsg = ref('')

/**
 * 网页播放失败时的降级链：
 *   ① 视频是 H.264、只是容器/音轨不兼容 → 后端快速重封装为 MP4（-c:v copy）后再播；
 *   ② 否则（HEVC 等浏览器解不了）→ 回退系统播放器。
 * 重封装只复制视频流、不重新编码，耗时受磁盘 IO 限制，弱 CPU 的 NAS 也能胜任。
 */
async function onError() {
  // 隐私锁定期间媒体流会被 423 拒绝，属预期行为：忽略错误，
  // 解锁后会恢复播放（见 PrivacyLock 的 resumePlayback），避免误触发降级。
  if (window.__avmLocked) return
  failed.value = true
  // HLS 浏览器直连被跨域 / 站点策略拦住：回退服务端代理再试一次
  if (streamMode.value === 'hls' && directSrc.value && !hlsProxyTried) {
    hlsProxyTried = true
    directSrc.value = ''
    failed.value = false
    if (player) {
      player.src(currentSource())
      player.play().catch(() => {})
    }
    return
  }
  // 网络串流：直连被防盗链 / CORS 拦时，自动改用服务端代理重试一次
  if (streamMode.value === 'strm' && !proxyTried) {
    proxyTried = true
    failed.value = false
    if (player) {
      player.src({ src: streamUrl(props.movieId) + '?proxy=1', type: 'video/mp4' })
      player.play().catch(() => {})
    }
    return
  }
  // 在线来源失效：后端会标记失效并切到下一可用来源，这里重建播放地址再试一次
  if (streamMode.value && !failoverTried) {
    failoverTried = true
    try {
      const r = await reportSourceFailure(props.movieId, 'playback error')
      if (r && r.switched) {
        failed.value = false
        await resolveSource()
        if (player) {
          const src = currentSource()
          src.src += (src.src.includes('?') ? '&' : '?') + 'r=' + Date.now()
          player.src(src)
          player.play().catch(() => {})
        }
        return
      }
    } catch (e) { /* 后端不可用时继续原有降级链 */ }
  }
  if (remuxTried) { fallbackExternal(); return }
  try {
    const info = await playbackInfo(props.movieId)
    if (info && info.auto_remux && info.remuxable) {
      remuxTried = true
      preparing.value = true
      preparePercent.value = info.prepared ? 100 : (info.percent || 0)
      prepareMsg.value = info.message || ''
      if (!info.prepared) {
        await prepareStream(props.movieId)
        const ok = await pollPrepare()
        preparing.value = false
        if (!ok) { fallbackExternal(); return }
      } else {
        preparing.value = false
      }
      failed.value = false
      reloadSource()
      return
    }
  } catch (e) { /* 忽略，继续回退系统播放器 */ }
  preparing.value = false
  fallbackExternal()
}

/** 轮询重封装进度，完成返回 true（最长等 5 分钟） */
async function pollPrepare() {
  const deadline = Date.now() + 5 * 60 * 1000
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 1200))
    try {
      const st = await prepareStreamStatus(props.movieId)
      if (st.percent) preparePercent.value = st.percent
      if (st.message) prepareMsg.value = st.message
      if (st.status === 'done') return true
      if (st.status === 'failed' || st.status === 'unsupported') {
        prepareMsg.value = st.message || ''
        return false
      }
    } catch (e) { /* 网络抖动，继续轮询 */ }
  }
  return false
}

/** 重封装完成后，把播放器源切到已就绪的流，并恢复播放位置 */
function reloadSource() {
  nextTick(() => {
    // 网络串流没有本地重封装产物，重载没有意义
    if (!player || streamMode.value) return
    const v = videoEl
    const pos = v && v.currentTime > 1 ? v.currentTime : (props.startAt || 0)
    reloading = true
    player.trigger('resize')
    player.src({ src: streamUrl(props.movieId) + '?r=' + Date.now(), type: 'video/mp4' })

    player.one('loadedmetadata', () => {
      const el = videoEl
      if (el && pos > 0 && pos < (el.duration || 0) - 5) el.currentTime = pos
    })
    player.play().catch(() => {})
  })
}

/** 回退系统播放器（远程访问不自动拉起：只会开在服务端机器上） */
function fallbackExternal() {
  if (!isRemote && !autoExternalDone) {
    autoExternalDone = true
    openExternal()
  }
}

function onEnded() {
  saveProgress(true)
  endSessionNow(1)
  emit('ended')
}

/** 结束观看会话，落库真实观看时长 */
async function endSessionNow(finished = 0) {
  const v = videoEl
  if (sessionId != null) {
    const endPos = v ? v.currentTime : 0
    await endSession(props.movieId, sessionId, {
      end_pos: endPos,
      watched_sec: Math.round(watchedSec),
      finished,
    }).catch(() => {})
    sessionId = null
  }
}

async function openExternal() {
  try {
    await playMovie(props.movieId)
    toast(t('player.launchedExternal'), 'ok')
  } catch (e) { toast(e.message, 'err') }
}

/* ---- 全屏 ----
 * 各端能力差别很大，按「能用的最强方案」逐级降级：
 *   1. 标准 Fullscreen API —— 桌面 Chrome/Edge/Firefox、Android Chrome
 *   2. webkit 前缀元素全屏 —— 旧 Safari、部分 Android WebView
 *   3. iOS Safari：元素级全屏至今不支持，只能让 <video> 进系统原生全屏
 *   4. 都不支持（老 WebView）：退化为「网页全屏」（纯 CSS 铺满视口）
 * 原实现只写了第 1 种：iPhone 上 el.requestFullscreen 根本不存在，调用处直接
 * 抛 TypeError（.catch 挂在返回值上，兜不住调用本身），表现为「点了没反应」。
 */
const webFs = ref(false)      // 是否处于网页全屏（CSS 兜底态）
const needOwnFs = ref(false)  // 控制条没有原生全屏按钮时（iOS），右上角补一个

function fullscreenEl() {
  return document.fullscreenElement || document.webkitFullscreenElement || null
}

/** video.js 在 iOS 上不渲染 fullscreen 控件，此时需要自带一个入口 */
function detectOwnFullscreen() {
  const bar = player && player.controlBar && player.controlBar.el()
  needOwnFs.value = !bar || !bar.querySelector('.vjs-fullscreen-control')
}

/** 网页全屏：不依赖任何浏览器 API，纯 CSS 把播放器铺满视口 */
function toggleWebFullscreen() {
  webFs.value = !webFs.value
}
// 兜底全屏铺满时锁住页面滚动（移动端尤其重要，否则手指一滑整页跟着动）
watch(webFs, (on) => {
  document.body.style.overflow = on ? 'hidden' : ''
})

/** 切换全屏（不暂停） */
function toggleNativeFullscreen() {
  const root = player ? player.el() : null
  const video = videoEl || (root && root.querySelector('video'))
  // 1) 已在元素全屏：退出（标准 / webkit 前缀两种 API）
  if (fullscreenEl()) {
    if (document.exitFullscreen) document.exitFullscreen().catch(() => {})
    else if (document.webkitExitFullscreen) document.webkitExitFullscreen()
    return
  }
  // 2) 处于 iOS 原生全屏（此时 document.fullscreenElement 仍是 null）
  if (video && video.webkitDisplayingFullscreen) {
    if (video.webkitExitFullscreen) video.webkitExitFullscreen()
    return
  }
  if (webFs.value) { webFs.value = false; return }
  // 3) 元素级全屏：必须对 .video-js 根元素请求，只全屏 <video> 会让
  //    overlay/控制条留在原位，全屏画面里点不到暂停。
  const el = root || video
  if (el && el.requestFullscreen) { el.requestFullscreen().catch(() => {}); return }
  if (el && el.webkitRequestFullscreen) { el.webkitRequestFullscreen(); return }
  // 4) iOS Safari：整块 <video> 走系统原生全屏（字幕样式/浮层不参与，
  //    但这是 iPhone 上唯一可行的全屏，比什么都不做强）
  if (video && video.webkitEnterFullscreen) { video.webkitEnterFullscreen(); return }
  // 5) 最后兜底
  toggleWebFullscreen()
}

/* ---- 横屏 ----
 * Android Chrome：进入元素全屏后可以调用 Screen Orientation API 锁横屏，
 *   退出全屏时解锁（不锁的话竖着拿手机就是竖屏铺满，画面很小）。
 * iOS Safari：没有 screen.orientation.lock（元素全屏本身也不支持），
 *   但走 webkitEnterFullscreen 后是系统播放器，方向由系统按视频比例处理，
 *   所以 iPhone 进全屏本来就是横屏的。
 * 任何一步失败都静默，绝不因为它影响全屏本身。 */
function lockLandscape() {
  try {
    const o = screen.orientation
    if (o && typeof o.lock === 'function') Promise.resolve(o.lock('landscape')).catch(() => {})
  } catch (e) { /* 不支持：忽略 */ }
}

function unlockOrientation() {
  try {
    const o = screen.orientation
    if (o && typeof o.unlock === 'function') o.unlock()
  } catch (e) { /* 忽略 */ }
}

function onFullscreenChange() {
  if (fullscreenEl()) lockLandscape()
  else unlockOrientation()
}

// 键盘方向键：左右快退/快进（每次 5 秒），上下调音量（每次 0.1）。在 document 上监听，
// 非全屏与全屏态均生效；输入框聚焦时不拦截，避免影响其它输入。
function onKeydown(e) {
  if (!player || !videoEl) return
  const tag = (e.target && e.target.tagName) || ''
  if (tag === 'INPUT' || tag === 'TEXTAREA') return
  // Esc 优先关设置面板，其次退出网页全屏
  if (e.key === 'Escape' && setMenuOpen.value) {
    setMenuOpen.value = false
    return
  }
  // 网页全屏兜底态没有浏览器自带的 Esc 退出，这里补上
  if (e.key === 'Escape' && webFs.value) {
    webFs.value = false
    return
  }
  if (e.key === 'ArrowLeft') {
    e.preventDefault()
    player.currentTime(Math.max(0, (videoEl.currentTime || 0) - 5))
  } else if (e.key === 'ArrowRight') {
    e.preventDefault()
    const d = videoEl.duration || 0
    player.currentTime(Math.min(d, (videoEl.currentTime || 0) + 5))
  } else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
    e.preventDefault()
    // 调音量时若处于静音，先解除静音
    if (player.muted()) player.muted(false)
    const cur = player.volume()
    const next = Math.min(1, Math.max(0, cur + (e.key === 'ArrowUp' ? 0.1 : -0.1)))
    player.volume(next)
  } else if (e.code === 'BracketLeft' || e.code === 'BracketRight'
             || e.key === '[' || e.key === ']' || e.key === '{' || e.key === '}') {
    // 字幕同步微调：边看边调，不暂停、不发请求（cue 时间在内存里改）
    // Shift+[ / Shift+] 在标准键盘上 e.key 会变成 { / }，靠 e.code 认同一颗物理键
    e.preventDefault()
    const back = e.code === 'BracketLeft' || e.key === '[' || e.key === '{'
    sync.nudge((back ? -1 : 1) * (e.shiftKey ? 100 : 500))
    scheduleSyncApply()
  } else if ((e.key === 's' || e.key === 'S') && sync.dirty.value) {
    e.preventDefault()
    saveSyncNow()
  }
}

// video.js 内部也会在 <video> 上处理 click（toggle 控制条/大播放按钮），
// 与其在 video 上抢事件会导致"暂停又播放"等竞争。
// 解决方案：在播放器之上放一个透明覆盖层（overlay），画面点击完全由它独占，
// 把 video.js 完全隔离在外，从而单击/双击行为 100% 由我们控制。
// 覆盖层只覆盖画面区域（底部留出控制条），控制条按钮仍可正常点击。

function onOverlayClick() {
  // 触摸设备：点击画面仅唤起控制条/进度条，不直接暂停，避免移动端误触导致"一点就暂停"
  if (isTouch) {
    if (player) player.trigger('useractive')
    return
  }
  // 桌面端：单击切换播放/暂停（双击全屏由 dblclick 处理）
  if (clickTimer) return
  clickTimer = setTimeout(() => {
    clickTimer = null
    if (!player) return
    if (player.paused()) player.play().catch(() => {})
    else player.pause()
  }, 220)
}
function onOverlayDblClick(e) {
  e.preventDefault()
  if (clickTimer) { clearTimeout(clickTimer); clickTimer = null }
  toggleNativeFullscreen()
}

// ---- 移动端横滑快进/快退（类似成熟视频站：拖动快进，松手定位）----
/** 按时间反推预览图帧下标（与后端等距抽帧一致） */
function frameIndexAt(time, total) {
  if (!total || total <= 0) return -1
  const ratio = Math.max(0, Math.min(1, time / total))
  const idx = Math.round(ratio * (previewUrls.length + 1)) - 1
  return Math.max(0, Math.min(previewUrls.length - 1, idx))
}
function buildSwipeTip() {
  if (swipeTipEl || !player) return
  swipeTipEl = document.createElement('div')
  swipeTipEl.className = 'vjs-swipe-tip'
  swipeTipEl.style.display = 'none'
  swipeTipEl.innerHTML = '<img alt="" class="st-img"><div class="st-text"></div>'
  player.el().appendChild(swipeTipEl)
}
function showSwipeTip(dir, time) {
  if (!swipeTipEl) return
  const img = swipeTipEl.querySelector('.st-img')
  const txt = swipeTipEl.querySelector('.st-text')
  if (img) {
    if (previewUrls.length && videoEl && videoEl.duration) {
      const ci = frameIndexAt(time, videoEl.duration)
      img.src = previewUrls[ci]
      img.style.display = 'block'
    } else {
      img.style.display = 'none'
    }
  }
  if (txt) txt.textContent = (dir > 0 ? '快进 ▸ ' : '快退 ◂ ') + fmtClock(time)
  swipeTipEl.style.display = 'block'
}
function hideSwipeTip() {
  if (swipeTipEl) swipeTipEl.style.display = 'none'
}
function onTouchStart(e) {
  const t = e.touches[0]
  swipe.active = true
  swipe.dir = 0
  swipe.startX = t.clientX
  swipe.startY = t.clientY
  swipe.startTime = videoEl ? videoEl.currentTime : 0
  swipe.targetTime = swipe.startTime
  startVolGesture(t.clientX, t.clientY, true)
}
function onTouchMove(e) {
  if (!swipe.active) return
  const t = e.touches[0]
  const dx = t.clientX - swipe.startX
  const dy = t.clientY - swipe.startY
  // 垂直音量手势：竖直位移明显大于水平（与横滑快进区分）
  if (Math.abs(dy) > 15 && Math.abs(dy) > Math.abs(dx)) {
    if (e.cancelable) e.preventDefault()
    moveVolGesture(t.clientX, t.clientY)
    return
  }
  // 横滑快进/快退：水平位移明显大于竖直位移
  if (Math.abs(dx) > 30 && Math.abs(dx) > Math.abs(dy)) {
    if (e.cancelable) e.preventDefault()
    if (!videoEl || !videoEl.duration) return
    // 滑满整个屏宽 ≈ 跳 20% 时长（可调）
    const rate = (videoEl.duration * 0.2) / window.innerWidth
    swipe.dir = dx > 0 ? 1 : -1
    swipe.targetTime = Math.max(0, Math.min(videoEl.duration, swipe.startTime + dx * rate))
    showSwipeTip(swipe.dir, swipe.targetTime)
  }
}
function onTouchEnd() {
  if (!swipe.active) return
  const moved = Math.abs(swipe.targetTime - swipe.startTime)
  swipe.active = false
  hideSwipeTip()
  // 有实际位移才 seek，避免纯点击（tap）误触
  if (moved > 0.5 && player) {
    player.currentTime(swipe.targetTime)
  }
  endVolGesture()
}

/* ---------- 音量手势（垂直滑动调音量）：桌面全屏鼠标拖动 + 移动端手指垂直滑动 ---------- */
let volGesture = { active: false, startX: 0, startY: 0, startVol: 0, moved: false, touch: false }
let volOsdEl = null
let volOsdTimer = null
let volOsdTxt = null
let volOsdBar = null
// 垂直音量手势结束后，抑制随后的 click（暂停），避免"拖动调音量却触发暂停"
let suppressNextClick = false

function setPlayerVolume(v) {
  if (!player) return
  v = Math.max(0, Math.min(1, v))
  if (player.muted()) player.muted(false)
  player.volume(v)
  showVolumeOsd(v)
}
function showVolumeOsd(v) {
  if (!volOsdEl || !player) return
  const pct = Math.round(v * 100)
  if (volOsdTxt) volOsdTxt.textContent = pct + '%'
  if (volOsdBar) volOsdBar.style.width = pct + '%'
  const ico = volOsdEl.querySelector('.vjs-vol-ico')
  if (ico) ico.textContent = v <= 0 ? '🔇' : '🔊'
  volOsdEl.style.display = 'flex'
  clearTimeout(volOsdTimer)
  volOsdTimer = setTimeout(() => { if (volOsdEl) volOsdEl.style.display = 'none' }, 1000)
}
function buildVolumeOsd() {
  if (volOsdEl || !player) return
  volOsdEl = document.createElement('div')
  volOsdEl.className = 'vjs-vol-osd'
  volOsdEl.style.display = 'none'
  volOsdEl.innerHTML = '<div class="vjs-vol-ico">🔊</div><div class="vjs-vol-num"></div><div class="vjs-vol-track"><i class="vjs-vol-fill"></i></div>'
  volOsdTxt = volOsdEl.querySelector('.vjs-vol-num')
  volOsdBar = volOsdEl.querySelector('.vjs-vol-fill')
  player.el().appendChild(volOsdEl)
}
function startVolGesture(x, y, isTouch) {
  if (!player) return
  volGesture.active = true
  volGesture.touch = isTouch
  volGesture.moved = false
  volGesture.startX = x
  volGesture.startY = y
  volGesture.startVol = player.volume()
}
function moveVolGesture(x, y) {
  if (!volGesture.active) return
  const dy = y - volGesture.startY
  // 竖直位移超过阈值才视为音量手势（避免误触发）
  if (Math.abs(dy) > 15) {
    volGesture.moved = true
    // 上下各约 320px 对应满音量；上滑增大、下滑减小
    const next = volGesture.startVol - (dy / 320)
    setPlayerVolume(next)
  }
}
function endVolGesture() {
  if (!volGesture.active) return
  volGesture.active = false
  if (volGesture.moved) suppressNextClick = true
}

// 桌面鼠标：在播放画面上按下→垂直拖动调音量（非全屏也生效）
let mouseGestBound = false
function onMouseDown(e) {
  // 排除控制条/进度条等交互（它们不在 overlay 上，此处只收到画面区域按下）
  if (e.button !== 0) return
  startVolGesture(e.clientX, e.clientY, false)
  if (!mouseGestBound) {
    document.addEventListener('mousemove', onMouseMove)
    document.addEventListener('mouseup', onMouseUp)
    mouseGestBound = true
  }
}
function onMouseMove(e) {
  moveVolGesture(e.clientX, e.clientY)
  // 手势进行中阻止文本选中/拖拽
  if (volGesture.active) {
    if (e.cancelable) e.preventDefault()
  }
}
function onMouseUp() {
  endVolGesture()
  if (mouseGestBound) {
    document.removeEventListener('mousemove', onMouseMove)
    document.removeEventListener('mouseup', onMouseUp)
    mouseGestBound = false
  }
}
// 桌面鼠标滚轮调音量（最常见、最直接）：在画面上滚动即可
function onWheel(e) {
  if (!player) return
  // 只在播放画面上生效，不影响其它区域滚动
  e.preventDefault()
  const cur = player.volume()
  const delta = e.deltaY < 0 ? 0.05 : -0.05
  const next = Math.max(0, Math.min(1, cur + delta))
  setPlayerVolume(next)
}
// 包装点击：若刚结束音量手势，则跳过本次点击（避免触发暂停）
function onOverlayClickWithGesture(e) {
  if (suppressNextClick) { suppressNextClick = false; return }
  onOverlayClick(e)
}

function initPlayer() {
  if (!containerEl.value) return
  // 延迟初始化：等详情抽屉滑入动画结束、容器尺寸稳定后再创建播放器，
  // 否则初始宽度/高度为 0 会把内部 video 尺寸算死成 0（有声音无画面）。
  setTimeout(() => {
    if (!containerEl.value || player) return
    const el = document.createElement('video-js')
    containerEl.value.appendChild(el)

    player = videojs(el, {
      autoplay: props.autoplay,
      controls: true,
      preload: 'auto',
      // 移动端内联播放：不设这个，iOS 会一按播放就跳到系统全屏，
      // 我们自己的控制条/浮层/字幕样式全部失效
      playsinline: true,
      fluid: false,
      fill: true,
      // 初始音量调小，避免一打开声音过大
      volume: 0.3,
      // 关键：禁用大播放按钮覆盖层。该覆盖层在暂停时会浮现并覆盖画面，
      // 自带 click→play 行为，会与我们的单击 pause 竞争，导致"暂停不到一秒又播放"。
      bigPlayButton: false,
      playbackRates: [0.5, 1, 1.25, 1.5, 2],
      sources: [currentSource()],
      // 浏览器直连 HLS 时携带站点 Cookie：用户浏览器已通过 CF 校验的情况下
      // 才有意义；源站不允许 credentials 时会自动回退服务端代理
      html5: {
        vhs: {
          withCredentials: true,
        },
      },
      controlBar: {
        pictureInPictureToggle: true,
      },
    })

    videoEl = player.el().querySelector('video')
    mountCcHost()
    mountTimeHost()
    detectOwnFullscreen()

    barActive.value = true
    player.on('loadedmetadata', () => { onLoaded() })
    player.on('timeupdate', () => onTime())
    player.on('ended', () => onEnded())
    player.on('error', () => onError())
    // 右上角浮层跟着控制条同节奏显隐；菜单打开 / 暂停时保持可见
    player.on('useractive', () => { barActive.value = true })
    player.on('userinactive', () => {
      if (setMenuOpen.value || markOpen.value) return
      if (player && typeof player.paused === 'function' && player.paused()) return
      barActive.value = false
    })

    // 透明覆盖层：独占画面区域的单击/双击，隔离 video.js 对 video 的内部 click 处理，
    // 避免"暂停又播放"的竞争。覆盖层放在控制条之上、画面区域，控制条按钮仍可点击。
    overlayEl = document.createElement('div')
    overlayEl.className = 'vjs-click-overlay'
    player.el().appendChild(overlayEl)
    overlayEl.addEventListener('click', onOverlayClickWithGesture)
    overlayEl.addEventListener('dblclick', onOverlayDblClick)
    // 桌面：鼠标滚轮调音量 + 按下拖动调音量
    overlayEl.addEventListener('wheel', onWheel, { passive: false })
    overlayEl.addEventListener('mousedown', onMouseDown)
    // 先应用字号/背景（轨道接口失败时也要生效），再拉轨道列表
    applySubStyle()
    loadSubtitleTracks()
    // 移动端：画面横滑快进/快退 + 垂直滑动调音量（passive:false 以便阻止默认/页面滚动）
    overlayEl.addEventListener('touchstart', onTouchStart, { passive: true })
    buildVolumeOsd()
    overlayEl.addEventListener('touchmove', onTouchMove, { passive: false })
    overlayEl.addEventListener('touchend', onTouchEnd)
    buildSwipeTip()
    // 键盘方向键快进/快退（全局监听，全屏/非全屏均生效）
    document.addEventListener('keydown', onKeydown)
    // 字幕台：点面板外收起。用捕获阶段的 pointerdown，画面覆盖层即使
    // stopPropagation 也能收到；target 在 ccHost 内（含菜单）时不关。
    document.addEventListener('pointerdown', onDocClick, true)
    // 初始化完成后再 resize 一次，确保按当前稳定尺寸渲染
    player.on('ready', () => { player && player.trigger('resize') })
    setTimeout(() => { player && player.trigger('resize') }, 300)
  }, 360)
}

function destroyPlayer() {
  clearInterval(barKeepTimer)
  barKeepTimer = 0
  if (clickTimer) { clearTimeout(clickTimer); clickTimer = null }
  webFs.value = false  // 兜底全屏态要一起退出，否则页面会一直锁着滚动
  if (player) {
    try {
      saveProgress(true)
      endSessionNow(0)
      if (overlayEl) {
        overlayEl.removeEventListener('click', onOverlayClickWithGesture)
        overlayEl.removeEventListener('dblclick', onOverlayDblClick)
        overlayEl.removeEventListener('wheel', onWheel)
        overlayEl.removeEventListener('mousedown', onMouseDown)
        overlayEl.removeEventListener('touchstart', onTouchStart)
        overlayEl.removeEventListener('touchmove', onTouchMove)
        overlayEl.removeEventListener('touchend', onTouchEnd)
        overlayEl = null
      }
      document.removeEventListener('keydown', onKeydown)
      document.removeEventListener('pointerdown', onDocClick, true)
      if (mouseGestBound) {
        document.removeEventListener('mousemove', onMouseMove)
        document.removeEventListener('mouseup', onMouseUp)
        mouseGestBound = false
      }
      if (progressCtrl && hoverBound) {
        progressCtrl.removeEventListener('mousemove', onProgressMove)
        progressCtrl.removeEventListener('mouseleave', onProgressLeave)
      }
      // 先摘掉 Teleport 目标，避免播放器销毁后 Vue 还往已分离的节点里操作
      ccHost.value = null
      timeHost.value = null
      if (wrapRO) { wrapRO.disconnect(); wrapRO = null }
      player.dispose()
    } catch (e) { /* 静默 */ }
    player = null
    videoEl = null
    progressCtrl = null
    hoverBound = false
    previewUrls = []
    tipEl = null
    tipImgEl = null
    tipTimeEl = null
    swipeTipEl = null
    swipe = { active: false, dir: 0, startX: 0, startY: 0, startTime: 0, targetTime: 0 }
    volOsdEl = null
    volGesture = { active: false, startX: 0, startY: 0, startVol: 0, moved: false, touch: false }
    if (volOsdTimer) { clearTimeout(volOsdTimer); volOsdTimer = null }
  }
}

// ---- 进度条 hover 预览 ----
function buildTip() {
  if (!progressCtrl || tipEl) return
  // 让 absolute 定位的子浮层以进度条容器为基准
  progressCtrl.style.position = 'relative'
  tipEl = document.createElement('div')
  tipEl.className = 'vjs-preview-tip'
  tipEl.innerHTML = '<img alt=""><span class="pt-time"></span>'
  tipImgEl = tipEl.querySelector('img')
  tipTimeEl = tipEl.querySelector('.pt-time')
  tipEl.style.display = 'none'
  progressCtrl.appendChild(tipEl)
}

function onProgressMove(e) {
  if (!progressCtrl || !tipEl || !previewUrls.length) return
  const holder = progressCtrl.querySelector('.vjs-progress-holder')
  if (!holder) return
  const hRect = holder.getBoundingClientRect()
  if (hRect.width <= 0) return
  let ratio = (e.clientX - hRect.left) / hRect.width
  ratio = Math.max(0, Math.min(1, ratio))
  // 后端等距抽帧：t = dur*(i+1)/(count+1) -> index = round(ratio*(count+1))-1
  const idx = Math.round(ratio * (previewUrls.length + 1)) - 1
  const ci = Math.max(0, Math.min(previewUrls.length - 1, idx))
  tipImgEl.src = previewUrls[ci]
  tipTimeEl.textContent = fmtClock(ratio * (dur.value || 0))
  // 浮层跟随鼠标横向居中（相对进度条容器）
  const pRect = progressCtrl.getBoundingClientRect()
  tipEl.style.left = (e.clientX - pRect.left) + 'px'
  tipEl.style.display = 'block'
}

function onProgressLeave() {
  if (tipEl) tipEl.style.display = 'none'
}

function enableHover() {
  if (!progressCtrl || hoverBound) return
  hoverBound = true
  buildTip()
  progressCtrl.addEventListener('mousemove', onProgressMove)
  progressCtrl.addEventListener('mouseleave', onProgressLeave)
}

async function ensurePreviews() {
  if (!props.movieId) return
  try {
    const r = await getPreviews(props.movieId)
    if (r && r.available) {
      if (r.regenerating) {
        // 配置密度档位已变化：后台正在按新档位重抽，本次暂无预览
        toast(t('player.previewRegen'), '', 2500)
        return
      }
      if (r.urls && r.urls.length) {
        previewUrls = r.urls.map((u) => '/api' + u)
        enableHover()
        return
      }
    }
  } catch (e) { /* 忽略 */ }
  // 无预览图：后台异步生成，本次播放暂无 hover 预览，下次打开即出
  try { await genPreviews(props.movieId) } catch (e) { /* 忽略 */ }
}

function setupPreviewHover() {
  if (!player) return
  progressCtrl = player.el().querySelector('.vjs-progress-control')
  if (!progressCtrl) return
  ensurePreviews()
}

saveTimer = setInterval(() => saveProgress(false), 10000)

// 外部请求暂停（如连播途中打开影片详情抽屉，画面被盖住时不应继续出声）
function onPauseRequest() {
  if (player && !player.paused()) player.pause()
}

/* 外部请求跳转：AI 问答的时间戳依据点一下就跳到片中那一刻 */
let pendingSeek = 0
function onSeekRequest(e) {
  const sec = Number(e && e.detail && e.detail.seconds)
  if (!Number.isFinite(sec) || sec < 0) return
  const v = videoEl
  if (v && v.readyState > 0) {
    v.currentTime = sec
    if (player && player.paused()) player.play()
  } else {
    // 播放器还没就绪：先记下来，onLoaded 时应用
    pendingSeek = sec
  }
}

onMounted(() => {
  // 先恢复字幕样式/选中轨偏好，再初始化播放器，避免加载轨道时用默认值
  loadPrefs()
  window.addEventListener('avm-pause-player', onPauseRequest)
  window.addEventListener('avm-seek', onSeekRequest)
  // 全屏状态变化：进全屏锁横屏，退出时解锁（iOS 不支持 lock，静默跳过）
  document.addEventListener('fullscreenchange', onFullscreenChange)
  document.addEventListener('webkitfullscreenchange', onFullscreenChange)
  watchWrapSize()
  nextTick(async () => { failoverTried = false; await resolveSource(); initPlayer() })
})

onBeforeUnmount(() => {
  // 离开播放器时保存当前的音量/倍速/字幕偏好（音量拖动过程不写盘，避免频繁 IO）
  clearInterval(barKeepTimer)
  clearTimeout(syncPreviewTimer)
  savePrefs()
  window.removeEventListener('avm-pause-player', onPauseRequest)
  window.removeEventListener('avm-seek', onSeekRequest)
  document.removeEventListener('fullscreenchange', onFullscreenChange)
  document.removeEventListener('webkitfullscreenchange', onFullscreenChange)
  unlockOrientation()  // 别把设备方向锁留在退出播放器之后
  clearInterval(saveTimer)
  destroyPlayer()
})

watch(() => props.movieId, () => {
  // 切换影片：结束上一个会话并销毁重建播放器
  destroyPlayer()
  failed.value = false
  autoExternalDone = false
  remuxTried = false
  reloading = false
  preparing.value = false
  preparePercent.value = 0
  prepareMsg.value = ''
  ready.value = false
  lastSaved = 0
  sessionId = null
  watchedSec = 0
  sourceReady = false
  nextTick(async () => { failoverTried = false; await resolveSource(); initPlayer() })
})
</script>

<template>
  <div class="player">
    <div v-show="!failed" ref="containerEl" class="vjs-wrap" :class="{ 'web-fs': webFs }"
         :style="videoVars">
      <!-- 锐化滤镜：kernel 由画面控制台的「锐化」滑块驱动，0 时是恒等核（无效果） -->
      <svg class="vp-svg-filters" aria-hidden="true" focusable="false" width="0" height="0">
        <filter id="vp-sharpen" x="0" y="0" width="100%" height="100%">
          <feConvolveMatrix order="3" preserveAlpha="true" :kernel-matrix="sharpenKernel" />
        </filter>
      </svg>
      <!-- 播放器浮层：字幕轨选择/样式、画面调整、片头片尾跳过标记 -->
      <!-- 播放器浮层：统一设置面板（字幕 / 画面调整）、片头片尾跳过标记 -->
      <!-- 设置入口挂在 control-bar 里（见 mountCcHost），和原生按钮同一排 -->
      <Teleport v-if="uiReady && ccHost" :to="ccHost">
        <div class="vp-cc" :class="{ on: setMenuOpen }" :title="$t('player.psSettings')"
             @click.stop="toggleSetMenu">
          <svg class="vp-gear" viewBox="0 0 24 24" aria-hidden="true">
            <path fill="currentColor" d="M12 15.5A3.5 3.5 0 1 1 15.5 12 3.5 3.5 0 0 1 12 15.5m7.43-2.53c.04-.32.07-.64.07-.97s-.03-.66-.07-.98l2.11-1.65a.5.5 0 0 0 .12-.64l-2-3.46a.5.5 0 0 0-.61-.22l-2.49 1a7.3 7.3 0 0 0-1.69-.98l-.37-2.65A.5.5 0 0 0 14 2.5h-4a.5.5 0 0 0-.5.42l-.37 2.65c-.61.25-1.17.59-1.69.98l-2.49-1a.5.5 0 0 0-.61.22l-2 3.46a.5.5 0 0 0 .12.64l2.11 1.65c-.04.32-.07.65-.07.98s.03.65.07.97l-2.11 1.66a.5.5 0 0 0-.12.64l2 3.46c.13.23.4.31.61.22l2.49-1c.52.39 1.08.73 1.69.98l.37 2.65c.04.24.25.42.5.42h4c.25 0 .46-.18.5-.42l.37-2.65a7.3 7.3 0 0 0 1.69-.98l2.49 1c.21.09.48.01.61-.22l2-3.46a.5.5 0 0 0-.12-.64z" />
          </svg>
          <div v-if="setMenuOpen" @click.stop @mousemove="keepBarActive">
            <PlayerSettings @close="setMenuOpen = false">
              <template #sub="{ tab }">
                <SubtitleConsole
                  :tab="tab"
                  :movie-id="movieId" :tracks="subTracks" :active-sub-id="activeSub"
                  :sub-style="subStyle" :sync-current="sync.current.value"
                  :sync-dirty="sync.dirty.value" :sync-state="subStatus"
                  :current-time="cur" :duration="dur" :cue-range="cueRange"
                  @select-sub="pickSub" @set-style="onConsoleStyle"
                  @offset-change="onConsoleOffset" @apply-suggestion="onConsoleSuggest"
                  @anchor-now="onConsoleAnchor" @anchor-active="onConsoleAnchorActive"
                  @anchor-cue="onConsoleAnchorCue" @set-scale="onConsoleScale"
                  @reset-sync="onConsoleReset"
                  @save="saveSyncNow" @refresh-tracks="reloadTracks" />
              </template>
              <template #pic="{ tab }">
                <VideoConsole :tab="tab" :model-value="adj" @update:model-value="onAdjChange" />
              </template>
            </PlayerSettings>
          </div>
        </div>
      </Teleport>

      <!-- 已播时间：复用结束时间的官方类名，字号行高内边距完全一致 -->
      <Teleport v-if="uiReady && timeHost" :to="timeHost">
        <div class="vjs-current-time vjs-time-control vjs-control vp-now">
          <span class="vjs-current-time-display">{{ fmtShort(cur) }}</span>
        </div>
      </Teleport>

      <div v-if="uiReady" class="vp-ui" :class="{ 'vp-ui-hidden': !barActive }"
           @mouseenter="keepBarActive" @mousemove="keepBarActive">
        <!-- 已标记的跳过点：显示时间点与 ✕，点它清除；标记入口是右边的「⚑ 标记跳过」 -->
        <div v-if="skipIntro" class="vp-btn vp-tag" :title="$t('player.skipIntroClear')" @click.stop="clearSkip('intro')">
          {{ $t('player.introTag', { t: fmtSkip(skipIntro) }) }}<span class="vp-x">✕</span>
        </div>
        <div v-if="skipOutro" class="vp-btn vp-tag" :title="$t('player.skipOutroClear')" @click.stop="clearSkip('outro')">
          {{ $t('player.outroTag', { t: fmtSkip(skipOutro) }) }}<span class="vp-x">✕</span>
        </div>
        <div v-if="canMarkIntro || canMarkOutro" class="vp-btn vp-mark" :class="{ on: markOpen }"
             :title="$t('player.markSkipTip')" @click.stop="markOpen = !markOpen">
          ⚑ <span class="vp-mark-label">{{ $t('player.markSkip') }}</span>
          <div v-if="markOpen" class="vp-menu" @click.stop>
            <button v-if="canMarkIntro" class="vp-item" @click="markIntro">{{ $t('player.markIntro') }}</button>
            <button v-if="canMarkOutro" class="vp-item" @click="markOutro">{{ $t('player.markOutro') }}</button>
            <button v-if="skipIntro || skipOutro" class="vp-item" @click="clearSkip('both')">{{ $t('player.skipClear') }}</button>
          </div>
        </div>
        <!-- 移动端（iOS 等）控制条里没有原生全屏按钮，这里补一个入口 -->
        <div v-if="needOwnFs" class="vp-btn" :class="{ on: webFs }"
             :title="webFs ? $t('player.exitFullscreen') : $t('player.fullscreen')"
             @click.stop="toggleNativeFullscreen">{{ webFs ? '⤡' : '⛶' }}</div>
      </div>

      <div v-if="skipToast" class="vp-toast" @click.stop>
        <span>{{ skipToast }}</span>
        <button class="vp-undo" @click.stop="undoSkip">{{ $t('player.undo') }}</button>
      </div>
    </div>

    <!-- 浏览器不兼容的容器/音轨：正在快速重封装为 MP4（只复制视频流，不重编码） -->
    <div v-if="preparing" class="p-fail">
      <div class="icon">⏳</div>
      <div class="title">{{ $t('player.preparing') }}</div>
      <p class="desc">{{ prepareMsg || $t('player.preparingDesc') }}<span v-if="preparePercent"> · {{ preparePercent }}%</span></p>
      <div class="p-bar"><i :style="{ width: Math.max(3, preparePercent) + '%' }"></i></div>
    </div>

    <div v-if="failed && !preparing" class="p-fail">
      <div class="icon">▶</div>
      <div class="title">{{ $t('player.cannotDecode') }}</div>
      <p class="desc">{{ $t('player.cannotDecodeDesc') }}</p>
      <button v-if="!isRemote" class="btn primary" @click="openExternal">{{ $t('player.openExternal') }}</button>
    </div>
  </div>
</template>

<style scoped>
.player { display: flex; flex-direction: column; background: var(--c-bg-sunken); }
/* 设置按钮（⚙）：字幕 / 画面高级配置的统一入口 */
.vp-cc { position: relative; }
.vp-gear { width: 1.05em; height: 1.05em; display: block; }
/* 正值把外挂字幕上移，避开画面里烧录的硬字幕 */
.vjs-wrap :deep(.vjs-text-track-display) { transform: translateY(calc(-1 * var(--vp-sub-y, 0px))); }
/* 给容器明确高度（16:9 比例），避免 video.js 在抽屉里高度塌缩导致"有声音无画面" */
.vjs-wrap {
  /* 浮层（字幕 / 片头片尾 / 提示）都锚在这里：缺了 relative 会跑到整个抽屉的右上角，
     按钮和它的菜单就飘在「相似推荐」栏上，鼠标往下一挪就脱离按钮、菜单立刻收起 */
  position: relative;
  width: 100%;
  height: 70vh;
  background: #000;
  /* 字幕样式的默认值（applySubStyle 会以行内变量覆盖这两项） */
  --vp-sub-size: 22px;
  --vp-sub-bg: rgba(0, 0, 0, .55);
}
.vjs-wrap :deep(.video-js) {
  width: 100% !important;
  height: 100% !important;
}
.vjs-wrap :deep(.video-js .vjs-tech) {
  width: 100% !important;
  height: 100% !important;
  object-fit: contain !important;
  background: #000;
  /* 画面调整（旋转 / 翻转 / 缩放 / 画质）：变量从 .vjs-wrap 传下来。不用 :style
     直接绑 video 元素——video.js 会反复重写它的行内宽高，绑在容器上更稳 */
  transform: var(--vp-transform, none);
  filter: var(--vp-filter, none);
}
/* 锐化用的 SVG 卷积核定义：不占布局 */
.vp-svg-filters { position: absolute; width: 0; height: 0; }
/* 进度条 hover 预览浮层：跟随鼠标横向居中显示在进度条上方 */
.vjs-wrap :deep(.vjs-preview-tip) {
  position: absolute;
  bottom: 100%;
  margin-bottom: 8px;
  transform: translateX(-50%);
  background: rgba(0, 0, 0, .88);
  border: 1px solid rgba(255, 255, 255, .18);
  border-radius: 6px;
  overflow: hidden;
  box-shadow: 0 4px 14px rgba(0, 0, 0, .5);
  z-index: 8;
  pointer-events: none;
}
.vjs-wrap :deep(.vjs-preview-tip img) {
  display: block;
  width: 160px;
  height: 90px;
  object-fit: cover;
  background: #000;
}
.vjs-wrap :deep(.vjs-preview-tip .pt-time) {
  display: block;
  text-align: center;
  padding: 2px 0 4px;
  font-size: 11px;
  line-height: 1;
  color: #fff;
  background: rgba(0, 0, 0, .6);
}
/* 移动端横滑快进/快退：屏幕中央进度提示（含该时刻预览缩略图） */
.vjs-wrap :deep(.vjs-swipe-tip) {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 10px;
  border-radius: 12px;
  background: rgba(0, 0, 0, .78);
  border: 1px solid rgba(255, 255, 255, .2);
  z-index: 9;
  pointer-events: none;
  user-select: none;
}
.vjs-wrap :deep(.vjs-swipe-tip .st-img) {
  width: 180px;
  height: 101px;
  object-fit: cover;
  border-radius: 8px;
  background: #000;
}
.vjs-wrap :deep(.vjs-swipe-tip .st-text) {
  color: #fff;
  font-size: 16px;
  font-weight: 500;
  letter-spacing: .5px;
  white-space: nowrap;
}
/* 透明覆盖层：独占画面点击（排除底部控制条），隔离 video.js 内部 click 处理；
   控制条 z-index 更高，因此按钮仍可正常点击。 */
.vjs-wrap :deep(.vjs-click-overlay) {
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  bottom: 3.2em; /* 留出底部控制条高度，不拦截控制条点击 */
  z-index: 5;
  cursor: pointer;
  background: transparent;
}
/* 系统原生全屏时，覆盖层同样铺满，保证全屏态下单击/双击仍可用 */
.vjs-wrap :deep(.video-js:fullscreen .vjs-click-overlay),
.vjs-wrap :deep(.video-js::-webkit-full-screen .vjs-click-overlay) {
  bottom: 3.2em;
  z-index: 5;
}
/* ---- 播放器浮层：字幕轨 / 片头片尾标记 ---- */
.vp-ui {
  position: absolute; top: 8px; right: 10px; z-index: 12;
  display: flex; align-items: center; gap: 6px;
  transition: opacity .2s ease, visibility .2s ease;
}
/* 桌面端：和 video.js 控制条同节奏淡出；移动端控制条常驻，这里也保持常驻 */
@media (min-width: 769px) {
  .vp-ui-hidden { opacity: 0; visibility: hidden; pointer-events: none; }
}
.vp-btn {
  position: relative;
  min-width: 30px; height: 26px; padding: 0 7px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 5px; border: 1px solid rgba(255,255,255,.22);
  background: rgba(0,0,0,.5); color: rgba(255,255,255,.85);
  font-size: 12px; cursor: pointer; user-select: none;
}
.vp-btn:hover, .vp-btn.on { background: rgba(255,255,255,.18); }
/* 已标记的跳过点：带时间点与 ✕，一眼看出是「点它清除」而不是标记入口 */
.vp-tag, .vp-mark { gap: 5px; }
.vp-x { font-size: 10px; opacity: .6; }
.vp-tag:hover .vp-x { opacity: 1; }
/* 字幕按钮挂在控制条里：外观向原生按钮看齐，菜单向上弹 */
.vjs-cc-host { position: relative; display: flex; align-items: center; align-self: stretch; }
/* 时间显示：官方皮肤把 .vjs-current-time 与 .vjs-duration 一起隐藏了，
   这里只放行「我渲染的 current-time（带 .vp-now）+ 总时长 + 分隔符」，
   三者共用 .vjs-time-control 的字号/行高/内边距，所以高度自动一致。 */
.vjs-wrap :deep(.video-js .vjs-time-control.vp-now) { display: block !important; }
.vjs-wrap :deep(.video-js .vjs-duration),
.vjs-wrap :deep(.video-js .vjs-time-divider) { display: block !important; }
.vjs-wrap :deep(.video-js .vjs-remaining-time) { display: none !important; }
/* 收紧时间区间：原生每个 .vjs-time-control 左右各 1em、min-width 2em，
   两个时间之间会空出 4em 以上。只压内边距与最小宽度，字号行高仍走原生值。 */
.vjs-wrap :deep(.video-js .vjs-current-time.vp-now) { padding-left: .4em; padding-right: .15em; }
.vjs-wrap :deep(.video-js .vjs-time-divider) { padding: 0; min-width: 0; }
.vjs-wrap :deep(.video-js .vjs-duration) { padding-left: .15em; padding-right: .5em; }
.vjs-cc-host .vp-cc {
  position: relative; height: 100%; min-width: 2.6em; padding: 0 .5em;
  display: flex; align-items: center; justify-content: center;
  font-size: .85em; letter-spacing: .5px; color: rgba(255,255,255,.85);
  cursor: pointer; user-select: none;
}
.vjs-cc-host .vp-cc:hover, .vjs-cc-host .vp-cc.on { color: #fff; background: rgba(255,255,255,.14); }
.vjs-cc-host .vp-menu { top: auto; bottom: calc(100% + 4px); right: 0; }
.vp-menu {
  /* 紧贴按钮、不留空隙：中间只要隔几个像素，鼠标往下移就会先离开按钮，
     菜单当场收起（这正是「想去点字幕却点不到」的原因） */
  position: absolute; top: 100%; right: 0; z-index: 13;
  width: 218px; max-height: 52vh; overflow: auto;
  padding: 5px; border-radius: 10px;
  /* 半透明 + 毛玻璃，和播放器其它浮层一个观感；配色吃主题变量 */
  background: var(--c-surface);
  background: color-mix(in srgb, var(--c-surface) 92%, transparent);
  border: 1px solid var(--c-line);
  box-shadow: var(--sh-3);
  color: var(--c-text-2);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
}
.vp-item {
  display: flex; align-items: center; gap: 6px;
  width: 100%; text-align: left;
  padding: 7px 9px; border: 0; border-radius: 7px;
  background: transparent; color: var(--c-text-2);
  font-size: 12.5px; line-height: 1.25; cursor: pointer;
  transition: background var(--t-fast, .12s) ease, color var(--t-fast, .12s) ease;
}
.vp-item:hover { background: var(--c-surface-2); color: var(--c-text); }
.vp-item.on { color: #fff; background: var(--c-primary); }
/* 字幕入口只保留浮层右上角的 CC：video.js 自带的按钮是「悬停弹出」，
   鼠标往菜单上一挪就收起，几乎点不中，留着只会让人以为是 bug */
.vjs-wrap :deep(.video-js .vjs-subs-caps-button) { display: none !important; }
.vp-sep { height: 1px; margin: 5px 4px; background: var(--c-line); }
/* 字号 / 背景：一行一个分段按钮，不再用原生下拉（又高又不像播放器） */
.vp-row {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 3px 4px 3px 9px;
}
.vp-row-label { flex: none; font-size: 12px; color: var(--c-text-3); }
.vp-seg {
  display: inline-flex; gap: 2px; padding: 2px;
  border-radius: 8px; background: var(--c-surface-2); border: 1px solid var(--c-line);
}
.vp-seg button {
  border: 0; background: transparent; color: var(--c-text-3);
  font-size: 11px; line-height: 1.2; padding: 4px 8px;
  border-radius: 6px; cursor: pointer; white-space: nowrap;
}
.vp-seg button:hover { color: var(--c-text); }
.vp-seg button.on { background: var(--c-primary); color: #fff; }
.vp-toast {
  position: absolute; left: 50%; bottom: 64px; transform: translateX(-50%);
  z-index: 14; display: flex; align-items: center; gap: 10px;
  padding: 7px 12px; border-radius: 999px;
  background: rgba(0,0,0,.78); color: #fff; font-size: 13px;
  white-space: nowrap;
}
.vp-undo {
  border: 0; border-radius: 999px; padding: 3px 10px; cursor: pointer;
  background: var(--c-primary); color: #fff; font-size: 12px;
}
/* 字幕显示样式（字号 / 背景由浮层控制）
   注意：默认值必须声明在 .vjs-wrap 自己身上。之前写在 .vjs-text-track-display
   （子元素）上，子元素的自有声明会覆盖从容器继承下来的行内变量，
   导致「无背景」永远被强制成半透明黑；字号选项同样失效。
   容器上的行内变量（applySubStyle）优先级高于这里的样式表默认值。 */
.vjs-wrap :deep(.vjs-text-track-cue) {
  font-size: var(--vp-sub-size, 22px) !important;
  background: var(--vp-sub-bg, rgba(0,0,0,.55)) !important;
  line-height: 1.35 !important;
}
.vjs-wrap :deep(.vjs-text-track-cue > div) {
  background: var(--vp-sub-bg, rgba(0,0,0,.55)) !important;
}
/* 无背景模式：白字在亮画面上会糊，补一层细描边（描边不算底色块） */
.vjs-wrap.vp-sub-plain :deep(.vjs-text-track-cue) {
  text-shadow: 0 0 3px rgba(0, 0, 0, .9), 0 1px 2px rgba(0, 0, 0, .9);
}
/* 网页全屏兜底：不支持 Fullscreen API 的浏览器（iOS 旧版、部分 WebView）
   用纯 CSS 把播放器铺满视口；100dvh 保证移动端地址栏收起/展开时不留白边 */
.vjs-wrap.web-fs {
  position: fixed !important;
  top: 0; left: 0; right: 0; bottom: 0;
  width: 100vw;
  height: 100vh;
  height: 100dvh;
  z-index: 9999;
}
/* 手机/平板：视频铺满可用高度；控制条（含进度条）常驻可见，便于拖动定位 */
@media (max-width: 768px) {
  /* dvh：移动浏览器地址栏收起/展开时不跳变（老浏览器忽略，仍用上面的 vh） */
  .vjs-wrap { height: 56vh; height: 56dvh; }
  .vjs-wrap :deep(.video-js .vjs-control-bar),
  .vjs-wrap :deep(.video-js.vjs-user-inactive .vjs-control-bar),
  .vjs-wrap :deep(.video-js.vjs-user-inactive.vjs-playing .vjs-control-bar) {
    display: flex !important;
    opacity: 1 !important;
    visibility: visible !important;
    transform: translateY(0) !important;
  }
  /* 加大进度条可点区域，移动端好拖 */
  .vjs-wrap :deep(.video-js .vjs-progress-control) { flex: 1 1 auto; }
  .vjs-wrap :deep(.video-js .vjs-progress-holder) { height: 0.5em; }
  /* 窄屏只留旗子图标，避免标记按钮和全屏按钮挤满右上角 */
  .vp-mark-label { display: none; }
}
/* 手机横屏：直接铺满视口高度。竖屏那套 56dvh 在横屏下会让画面缩成上下留黑，
   这里按「横屏即全屏」处理（限高 560px，避免影响桌面小窗口） */
@media (max-height: 560px) and (orientation: landscape) {
  .vjs-wrap { height: 100vh; height: 100dvh; }
}
.p-fail {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: var(--sp-3);
  padding: var(--sp-10) var(--sp-4);
  text-align: center;
  color: var(--c-text-3);
}
.p-fail .icon { font-size: 34px; opacity: .3; }
/* 重封装进度条 */
.p-bar {
  width: min(360px, 70%);
  height: 6px;
  border-radius: 999px;
  background: var(--c-surface-3, rgba(255,255,255,.12));
  overflow: hidden;
}
.p-bar > i {
  display: block;
  height: 100%;
  background: var(--c-primary);
  transition: width .3s ease;
}
.p-fail .title { font-size: var(--fs-lg); color: var(--c-text-2); }
.p-fail .desc { max-width: 400px; font-size: var(--fs-md); }
/* 系统原生全屏态：根元素 .video-js 铺满视口。
   同样必须 :deep —— video.js 的 DOM 是运行时创建的，scoped 属性选择器命中不了，
   原先这几条规则其实一直没生效。 */
.vjs-wrap :deep(.video-js:fullscreen),
.vjs-wrap :deep(.video-js::-webkit-full-screen) {
  width: 100vw !important;
  height: 100vh !important;
  height: 100dvh !important;
  background: #000 !important;
}
.vjs-wrap :deep(.video-js:fullscreen .vjs-tech),
.vjs-wrap :deep(.video-js::-webkit-full-screen .vjs-tech) {
  width: 100% !important;
  height: 100% !important;
  object-fit: contain !important;
}
/* 全屏时给刘海机底部手势条留位置，否则控制条会被系统横条压住点不到 */
.vjs-wrap :deep(.video-js:fullscreen .vjs-control-bar),
.vjs-wrap :deep(.video-js::-webkit-full-screen .vjs-control-bar) {
  height: calc(3em + env(safe-area-inset-bottom, 0px));
  padding-bottom: env(safe-area-inset-bottom, 0px);
}
/* 音量手势 OSD：垂直滑动/滚轮调音量时居中显示音量百分比与进度条
   （volOsdEl 是动态创建在 video.js 内部的元素，需用 :deep 穿透 scoped 才能命中） */
.vjs-wrap :deep(.vjs-vol-osd) {
  position: absolute;
  left: 50%; top: 42%;
  transform: translate(-50%, -50%);
  z-index: 30;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 18px;
  border-radius: 22px;
  background: rgba(0, 0, 0, .68);
  color: #fff;
  pointer-events: none;
  box-shadow: 0 4px 16px rgba(0,0,0,.4);
  white-space: nowrap;
}
.vjs-wrap :deep(.vjs-vol-ico) { font-size: 22px; line-height: 1; }
.vjs-wrap :deep(.vjs-vol-num) { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: .5px; min-width: 58px; text-align: center; }
.vjs-wrap :deep(.vjs-vol-track) { width: 150px; height: 6px; border-radius: 3px; background: rgba(255,255,255,.25); overflow: hidden; }
.vjs-wrap :deep(.vjs-vol-fill) { display: block; height: 100%; width: 50%; border-radius: 3px; background: #fff; transition: width .08s linear; }
</style>
