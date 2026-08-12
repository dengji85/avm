<script setup>
import { ref, onBeforeUnmount, watch } from 'vue'
import { streamUrl, setProgress, playMovie, markPlayed } from '../api.js'
import { toast, fmtClock } from '../utils.js'
import { t } from '../i18n/index.js'

const props = defineProps({
  movieId: { type: Number, required: true },
  startAt: { type: Number, default: 0 },
})
const emit = defineEmits(['close', 'progress'])

const videoEl = ref(null)
const failed = ref(false)
const ready = ref(false)
const cur = ref(0)
const dur = ref(0)
let saveTimer = null
let lastSaved = 0

function onLoaded() {
  ready.value = true
  const v = videoEl.value
  if (!v) return
  // 默认音量较小，避免一打开就炸耳；用户拖音量条会覆盖此值
  v.volume = 0.3
  dur.value = v.duration || 0
  if (props.startAt > 0 && props.startAt < (v.duration || 0) - 5) {
    v.currentTime = props.startAt
    toast(t('player.jumpedTo', { time: fmtClock(props.startAt) }), '', 2200)
  }
  v.play().catch(() => {})
  // 网页播放器开始播放时记录一次播放次数（与外部播放器保持一致）
  markPlayed(props.movieId).catch(() => {})
}

function onTime() {
  const v = videoEl.value
  if (!v) return
  cur.value = v.currentTime
  dur.value = v.duration || dur.value
}

/** 每 10 秒回写一次进度 */
async function saveProgress(force = false) {
  const v = videoEl.value
  if (!v || !v.duration) return
  const pos = v.currentTime
  if (!force && Math.abs(pos - lastSaved) < 10) return
  lastSaved = pos
  try {
    await setProgress(props.movieId, { position: pos, duration: v.duration })
    emit('progress', { position: pos, duration: v.duration })
  } catch (e) { /* 静默 */ }
}

function onError() {
  failed.value = true
}

async function openExternal() {
  try {
    await playMovie(props.movieId)
    toast(t('player.launchedExternal'), 'ok')
  } catch (e) { toast(e.message, 'err') }
}

function seek(delta) {
  const v = videoEl.value
  if (!v || !v.duration) return
  v.currentTime = Math.max(0, Math.min(v.duration, v.currentTime + delta))
}

saveTimer = setInterval(() => saveProgress(false), 10000)

onBeforeUnmount(() => {
  clearInterval(saveTimer)
  saveProgress(true)
})

watch(() => props.movieId, () => {
  failed.value = false
  ready.value = false
  lastSaved = 0
})
</script>

<template>
  <div class="player">
    <video
      v-show="!failed"
      ref="videoEl"
      class="vid"
      controls
      preload="metadata"
      :src="streamUrl(movieId)"
      @loadedmetadata="onLoaded"
      @timeupdate="onTime"
      @pause="saveProgress(true)"
      @ended="saveProgress(true)"
      @error="onError"
    ></video>

    <div v-if="failed" class="p-fail">
      <div class="icon">▶</div>
      <div class="title">{{ $t('player.cannotDecode') }}</div>
      <p class="desc">{{ $t('player.cannotDecodeDesc') }}</p>
      <button class="btn primary" @click="openExternal">{{ $t('player.openExternal') }}</button>
    </div>

    <div v-if="ready && !failed" class="p-bar">
      <button class="btn tiny ghost" @click="seek(-10)" :data-tip="$t('player.back10')">« 10s</button>
      <button class="btn tiny ghost" @click="seek(30)" :data-tip="$t('player.fwd30')">30s »</button>
      <span class="t tabular">{{ fmtClock(cur) }} / {{ fmtClock(dur) }}</span>
      <div class="spacer"></div>
      <button class="btn tiny ghost" @click="openExternal">{{ $t('player.external') }}</button>
    </div>
  </div>
</template>

<style scoped>
.player { display: flex; flex-direction: column; background: var(--c-bg-sunken); }
.vid {
  width: 100%;
  max-height: 60vh;
  background: #000;
  display: block;
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
.p-bar {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-top: 1px solid var(--c-line);
  background: var(--c-surface);
}
.p-bar .t { font-size: var(--fs-sm); color: var(--c-text-2); }
</style>
