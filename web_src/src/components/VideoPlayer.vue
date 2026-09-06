<script setup>
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import videojs from 'video.js'
import 'video.js/dist/video-js.css'
import { streamUrl, setProgress, playMovie, markPlayed, startSession, updateSession, endSession, getPreviews, genPreviews } from '../api.js'
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

// 每部影片只自动降级一次：video.js 可能对同一次故障连续派发多个 error 事件，
// 不防重会反复拉起系统播放器。切换影片时随播放器重建而重置。
let autoExternalDone = false

function onError() {
  failed.value = true
  // 网页端解码/拉流失败时自动改用系统播放器。绝大多数失败其实是浏览器不支持该
  // 编码（HEVC、DTS 音轨等），文件本身是好的，系统播放器能直接播，
  // 没必要让用户再手动点一次按钮。
  // 远程访问时不自动拉起：系统播放器只会开在服务端机器上，访问者看不到。
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
    overlayEl.addEventListener('click', onOverlayClickWithGesture)
    overlayEl.addEventListener('dblclick', onOverlayDblClick)
    // 桌面：鼠标滚轮调音量 + 按下拖动调音量
    overlayEl.addEventListener('wheel', onWheel, { passive: false })
    overlayEl.addEventListener('mousedown', onMouseDown)
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

onMounted(() => {
  window.addEventListener('avm-pause-player', onPauseRequest)
  nextTick(initPlayer)
})

onBeforeUnmount(() => {
  window.removeEventListener('avm-pause-player', onPauseRequest)
  clearInterval(saveTimer)
  destroyPlayer()
})

watch(() => props.movieId, () => {
  // 切换影片：结束上一个会话并销毁重建播放器
  destroyPlayer()
  failed.value = false
  autoExternalDone = false
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
