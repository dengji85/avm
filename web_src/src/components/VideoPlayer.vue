<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import videojs from 'video.js'
import 'video.js/dist/video-js.css'
import { streamUrl, strmHlsUrl, streamInfo, setProgress, playMovie, markPlayed, startSession, updateSession, endSession, getPreviews, genPreviews, playbackInfo, prepareStream, prepareStreamStatus, subtitleTracks, setSkipPoints } from '../api.js'
import { toast, fmtClock } from '../utils.js'
import { t } from '../i18n/index.js'

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
/* ---- 播放器浮层：字幕轨 / 片头片尾 ---- */
const uiReady = ref(false)
const subOpen = ref(false)
const markOpen = ref(false)
/* CC 按钮要挂进 video.js 控制条（原生按钮那一排），用 Teleport 渲染进去，
   这样它的位置、显示/隐藏（含自动隐藏）都跟着控制条走，不用自己维护 */
const ccHost = ref(null)
/* 字幕菜单：悬停展开、移开延时收起，点一下也能开关。
   延时是为了「从按钮滑到菜单」这段移动不被打断（触屏没有 hover，走点击）。 */
let subTimer = 0
function openSubMenu() {
  clearTimeout(subTimer)
  subOpen.value = true
}
function closeSubMenuSoon() {
  clearTimeout(subTimer)
  subTimer = setTimeout(() => { subOpen.value = false }, 200)
}
function toggleSubMenu() {
  if (subOpen.value) {
    clearTimeout(subTimer)
    subOpen.value = false
  } else {
    openSubMenu()
  }
}
function pickSub(id) {
  selectSub(id)
  clearTimeout(subTimer)
  subOpen.value = false
}
/* 字幕样式：用分段按钮而不是原生下拉——原生 select 又高又和播放器风格不搭 */
const subSizeOpts = computed(() => [
  { v: 's', t: t('player.subSizeS') },
  { v: 'm', t: t('player.subSizeM') },
  { v: 'l', t: t('player.subSizeL') },
])
const subBgOpts = computed(() => [
  { v: '0', t: t('player.subBgNone') },
  { v: '1', t: t('player.subBgHalf') },
  { v: '2', t: t('player.subBgFull') },
])
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
const subTracks = ref([])
const activeSub = ref('')
const subStyle = ref({ size: 'm', bg: '1' })
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
  // 无背景时给白字补描边（否则压在亮画面上基本看不清）
  el.classList.toggle('vp-sub-plain', String(subStyle.value.bg) === '0')
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

async function loadSubtitleTracks() {
  try {
    const r = await subtitleTracks(props.movieId)
    subTracks.value = r.tracks || []
    skipIntro.value = r.skip_intro || 0
    skipOutro.value = r.skip_outro || 0
    applySubStyle()
    // 偏好命中则用它，否则默认第一条（优先中文）
    const want = activeSub.value
    const pick = (want && subTracks.value.find((x) => x.id === want))
      || subTracks.value.find((x) => (x.lang || '').startsWith('zh'))
      || subTracks.value[0]
    subTracks.value.forEach((track) => {
      player.addRemoteTextTrack({
        kind: 'subtitles', src: track.url, srclang: track.lang || 'und',
        label: track.label, default: !!pick && track.id === pick.id,
      }, false)
    })
    activeSub.value = pick ? pick.id : ''
  } catch (e) { /* 没有字幕轨时忽略 */ }
  finally { uiReady.value = true }
}
function selectSub(id) {
  activeSub.value = id
  const list = subTracks.value
  // 远程字幕轨（addRemoteTextTrack）在 video.js 里不一定会生成 <video><track>，
  // 所以必须走 player.textTracks()；只看 videoEl.textTracks 会得到空列表，
  // 表现就是「点了字幕没有任何反应」。
  const tracks = (player && typeof player.textTracks === 'function' && player.textTracks())
    || (videoEl && videoEl.textTracks) || []
  for (let i = 0; i < tracks.length; i++) {
    const tt = tracks[i]
    if (tt.kind !== 'subtitles' && tt.kind !== 'captions') continue
    // 按 label 对齐（video.js 会在原生轨之后追加远程轨，不能按下标猜）
    const hit = list.find((x) => x.label === tt.label)
    tt.mode = (hit && hit.id === id) ? 'showing' : 'disabled'
  }
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
let proxyTried = false
let sourceReady = false

/** 播放前问一次后端：这部片是不是网络串流、要不要按 HLS 处理（轻量接口，无 ffprobe） */
async function resolveSource() {
  streamMode.value = ''
  proxyTried = false
  try {
    const info = await streamInfo(props.movieId)
    if (info && info.stream) streamMode.value = info.hls ? 'hls' : 'strm'
  } catch (e) { /* 忽略：按普通文件处理 */ }
  sourceReady = true
}

function currentSource() {
  if (streamMode.value === 'hls') {
    return { src: strmHlsUrl(props.movieId), type: 'application/x-mpegURL' }
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

/** 切换系统原生全屏（不暂停） */
function toggleNativeFullscreen() {
  // 关键：对 player 根元素（.video-js）请求全屏，而不是底层 <video>。
  // 若只全屏 <video>，overlay/控制条会留在原位、不在全屏画面上，导致全屏下点不到暂停。
  // 全屏根元素后，overlay 与控制条随之一起全屏，单击/双击仍可用。
  const el = player ? player.el() : videoEl
  if (!el) return
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {})
  } else {
    el.requestFullscreen().catch(() => {})
  }
}

// 键盘方向键：左右快退/快进（每次 5 秒），上下调音量（每次 0.1）。在 document 上监听，
// 非全屏与全屏态均生效；输入框聚焦时不拦截，避免影响其它输入。
function onKeydown(e) {
  if (!player || !videoEl) return
  const tag = (e.target && e.target.tagName) || ''
  if (tag === 'INPUT' || tag === 'TEXTAREA') return
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
      fluid: false,
      fill: true,
      // 初始音量调小，避免一打开声音过大
      volume: 0.3,
      // 关键：禁用大播放按钮覆盖层。该覆盖层在暂停时会浮现并覆盖画面，
      // 自带 click→play 行为，会与我们的单击 pause 竞争，导致"暂停不到一秒又播放"。
      bigPlayButton: false,
      playbackRates: [0.5, 1, 1.25, 1.5, 2],
      sources: [currentSource()],
      controlBar: {
        pictureInPictureToggle: true,
      },
    })

    videoEl = player.el().querySelector('video')
    mountCcHost()

    player.on('loadedmetadata', () => { onLoaded() })
    player.on('timeupdate', () => onTime())
    player.on('ended', () => onEnded())
    player.on('error', () => onError())

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
    // 初始化完成后再 resize 一次，确保按当前稳定尺寸渲染
    player.on('ready', () => { player && player.trigger('resize') })
    setTimeout(() => { player && player.trigger('resize') }, 300)
  }, 360)
}

function destroyPlayer() {
  if (clickTimer) { clearTimeout(clickTimer); clickTimer = null }
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
  nextTick(async () => { await resolveSource(); initPlayer() })
})

onBeforeUnmount(() => {
  // 离开播放器时保存当前的音量/倍速/字幕偏好（音量拖动过程不写盘，避免频繁 IO）
  clearTimeout(subTimer)
  savePrefs()
  window.removeEventListener('avm-pause-player', onPauseRequest)
  window.removeEventListener('avm-seek', onSeekRequest)
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
  nextTick(async () => { await resolveSource(); initPlayer() })
})
</script>

<template>
  <div class="player">
    <div v-show="!failed" ref="containerEl" class="vjs-wrap">
      <!-- 播放器浮层：字幕轨选择/样式、片头片尾跳过标记 -->
      <!-- 字幕入口挂在 control-bar 里（见 mountCcHost），和原生按钮同一排 -->
      <Teleport v-if="uiReady && ccHost" :to="ccHost">
        <div class="vp-cc" :class="{ on: subOpen }"
             @click.stop="toggleSubMenu"
             @mouseenter="openSubMenu"
             @mouseleave="closeSubMenuSoon">
          CC
          <div v-if="subOpen" class="vp-menu" @click.stop
               @mouseenter="openSubMenu" @mouseleave="closeSubMenuSoon">
            <button class="vp-item" :class="{ on: !activeSub }" @click="pickSub(null)">{{ $t('player.subOff') }}</button>
            <button v-for="t in subTracks" :key="t.id" class="vp-item"
                    :class="{ on: activeSub === t.id }" @click="pickSub(t.id)">
              <span class="ellipsis">{{ t.label }}</span>
            </button>
            <div class="vp-sep"></div>
            <div class="vp-row">
              <span class="vp-row-label">{{ $t('player.subSize') }}</span>
              <div class="vp-seg">
                <button v-for="o in subSizeOpts" :key="o.v" :class="{ on: subStyle.size === o.v }"
                        @click.stop="setSubStyle('size', o.v)">{{ o.t }}</button>
              </div>
            </div>
            <div class="vp-row">
              <span class="vp-row-label">{{ $t('player.subBg') }}</span>
              <div class="vp-seg">
                <button v-for="o in subBgOpts" :key="o.v" :class="{ on: subStyle.bg === o.v }"
                        @click.stop="setSubStyle('bg', o.v)">{{ o.t }}</button>
              </div>
            </div>
          </div>
        </div>
      </Teleport>

      <div v-if="uiReady" class="vp-ui">
        <div v-if="skipIntro" class="vp-btn" :title="$t('player.skipIntroClear')" @click.stop="clearSkip('intro')">片头</div>
        <div v-if="skipOutro" class="vp-btn" :title="$t('player.skipOutroClear')" @click.stop="clearSkip('outro')">片尾</div>
        <div v-if="canMarkIntro || canMarkOutro" class="vp-btn" :class="{ on: markOpen }" @click.stop="markOpen = !markOpen">
          ⚑
          <div v-if="markOpen" class="vp-menu" @click.stop>
            <button v-if="canMarkIntro" class="vp-item" @click="markIntro">{{ $t('player.markIntro') }}</button>
            <button v-if="canMarkOutro" class="vp-item" @click="markOutro">{{ $t('player.markOutro') }}</button>
            <button v-if="skipIntro || skipOutro" class="vp-item" @click="clearSkip('both')">{{ $t('player.skipClear') }}</button>
          </div>
        </div>
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
}
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
:fullscreen .vjs-click-overlay,
:-webkit-full-screen .vjs-click-overlay {
  bottom: 3.2em;
  z-index: 5;
}
/* ---- 播放器浮层：字幕轨 / 片头片尾标记 ---- */
.vp-ui {
  position: absolute; top: 8px; right: 10px; z-index: 12;
  display: flex; align-items: center; gap: 6px;
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
/* 字幕按钮挂在控制条里：外观向原生按钮看齐，菜单向上弹 */
.vjs-cc-host { position: relative; display: flex; align-items: center; align-self: stretch; }
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
/* 手机/平板：视频铺满可用高度；控制条（含进度条）常驻可见，便于拖动定位 */
@media (max-width: 768px) {
  .vjs-wrap { height: 56vh; }
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
/* 系统原生全屏态：根元素 .video-js 铺满视口 */
.video-js:fullscreen,
.video-js:-webkit-full-screen {
  width: 100vw !important;
  height: 100vh !important;
  background: #000 !important;
}
.video-js:fullscreen .vjs-tech,
.video-js:-webkit-full-screen .vjs-tech {
  width: 100% !important;
  height: 100% !important;
  object-fit: contain !important;
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
