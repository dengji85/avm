<script setup>
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import videojs from 'video.js'
import 'video.js/dist/video-js.css'
import { streamUrl, setProgress, playMovie, markPlayed, startSession, updateSession, endSession } from '../api.js'
import { toast, fmtClock } from '../utils.js'
import { t } from '../i18n/index.js'

const props = defineProps({
  movieId: { type: Number, required: true },
  startAt: { type: Number, default: 0 },
})
const emit = defineEmits(['close', 'progress'])

const containerEl = ref(null)
const failed = ref(false)
const ready = ref(false)
const cur = ref(0)
const dur = ref(0)
let player = null          // video.js player 实例
let videoEl = null         // 底层 <video> 元素
let saveTimer = null
let lastSaved = 0
let sessionId = null      // 当前观看会话 id（用于写入观看明细）
let watchedSec = 0        // 本次会话累计真实观看秒数
let lastTick = 0          // 上一次 timeupdate 的 currentTime
let clickTimer = null     // 区分单击/双击的定时器
let overlayEl = null      // 透明覆盖层，独占画面点击，隔离 video.js 内部 click 竞争

// 仅当访问地址为 localhost/127.0.0.1 时才提供"系统播放器"（远程设备点了也无效）
const isRemote = !['localhost', '127.0.0.1'].includes(location.hostname)

function onLoaded() {
  ready.value = true
  const v = videoEl
  if (!v) return
  // 在元数据就绪后显式设置初始音量（video.js 的 volume 选项在 autoplay 下常被忽略，
  // 这里强制设为较小音量，避免一打开声音过大）
  if (player) { player.muted(false); player.volume(0.3) }
  dur.value = v.duration || 0
  if (props.startAt > 0 && props.startAt < (v.duration || 0) - 5) {
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
  dur.value = v.duration || dur.value
}

/** 每 10 秒回写一次进度与观看时长 */
async function saveProgress(force = false) {
  const v = videoEl
  if (!v || !v.duration) return
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

function onError() {
  failed.value = true
}

function onEnded() {
  saveProgress(true)
  endSessionNow(1)
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
  // 用定时器区分单击与双击：双击时第二个 click 会被 dblclick 取消
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

function initPlayer() {
  if (!containerEl.value) return
  // 延迟初始化：等详情抽屉滑入动画结束、容器尺寸稳定后再创建播放器，
  // 否则初始宽度/高度为 0 会把内部 video 尺寸算死成 0（有声音无画面）。
  setTimeout(() => {
    if (!containerEl.value || player) return
    const el = document.createElement('video-js')
    containerEl.value.appendChild(el)

    player = videojs(el, {
      autoplay: true,
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
      sources: [{
        src: streamUrl(props.movieId),
        type: 'video/mp4',
      }],
      controlBar: {
        pictureInPictureToggle: true,
      },
    })

    videoEl = player.el().querySelector('video')

    player.on('loadedmetadata', () => { onLoaded() })
    player.on('timeupdate', () => onTime())
    player.on('ended', () => onEnded())
    player.on('error', () => onError())

    // 透明覆盖层：独占画面区域的单击/双击，隔离 video.js 对 video 的内部 click 处理，
    // 避免"暂停又播放"的竞争。覆盖层放在控制条之上、画面区域，控制条按钮仍可点击。
    overlayEl = document.createElement('div')
    overlayEl.className = 'vjs-click-overlay'
    player.el().appendChild(overlayEl)
    overlayEl.addEventListener('click', onOverlayClick)
    overlayEl.addEventListener('dblclick', onOverlayDblClick)
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
        overlayEl.removeEventListener('click', onOverlayClick)
        overlayEl.removeEventListener('dblclick', onOverlayDblClick)
        overlayEl = null
      }
      document.removeEventListener('keydown', onKeydown)
      player.dispose()
    } catch (e) { /* 静默 */ }
    player = null
    videoEl = null
  }
}

saveTimer = setInterval(() => saveProgress(false), 10000)

onMounted(() => { nextTick(initPlayer) })

onBeforeUnmount(() => {
  clearInterval(saveTimer)
  destroyPlayer()
})

watch(() => props.movieId, () => {
  // 切换影片：结束上一个会话并销毁重建播放器
  destroyPlayer()
  failed.value = false
  ready.value = false
  lastSaved = 0
  sessionId = null
  watchedSec = 0
  nextTick(initPlayer)
})
</script>

<template>
  <div class="player">
    <div v-show="!failed" ref="containerEl" class="vjs-wrap"></div>

    <div v-if="failed" class="p-fail">
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
  width: 100%;
  height: 70vh;
  background: #000;
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
/* 手机/平板：视频铺满可用高度 */
@media (max-width: 768px) {
  .vjs-wrap { height: 56vh; }
}
.p-fail {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: var(--sp-3);
  padding: var(--sp-10) var(--sp-4);
  text-align: center;
  color: var(--c-text-3);
}
.p-fail .icon { font-size: 34px; opacity: .3; }
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
</style>
