<script setup>
import { computed, ref } from 'vue'
import { state } from '../state.js'
import { coverThumbUrl, toggleFlag, playMovie } from '../api.js'
import { coverFallback, fmtMin, fmtSize, toast, qualityTag } from '../utils.js'
import { t } from '../i18n/index.js'

const props = defineProps({
  movie: { type: Object, required: true },
  selectable: { type: Boolean, default: true },
})
const emit = defineEmits(['open', 'changed'])

const m = computed(() => props.movie)
const loaded = ref(false)
// 始终通过 id 向后端请求封面：后端无图时 404，由 @error 回退占位。
// 不再依赖列表 item 的 cover 字段（详情页走单独接口，列表字段可能未携带，导致有封面却不显示）。
const coverSrc = computed(() => coverThumbUrl(m.value.id))

const selected = computed(() => state.selected.has(m.value.id))
const quality = computed(() => qualityTag(m.value.resolution))

/** 观看进度百分比 */
const watchPct = computed(() => {
  const pos = Number(m.value.progress_seconds || m.value.last_position) || 0
  const dur = Number(m.value.duration_seconds) || 0
  if (pos <= 0 || dur <= 0) return 0
  return Math.min(100, Math.round((pos / dur) * 100))
})
const progressFinished = computed(() => !!m.value.progress_finished)

const subtitle = computed(() => {
  const parts = []
  if (m.value.code) parts.push(m.value.code)
  return parts
})

function onCardClick(e) {
  if (state.selMode) { pick(e); return }
  emit('open', m.value.id)
}

function pick(e) {
  e.stopPropagation()
  const s = new Set(state.selected)
  if (s.has(m.value.id)) s.delete(m.value.id)
  else s.add(m.value.id)
  state.selected = s
  if (s.size && !state.selMode) state.selMode = true
}

async function fav(e) {
  e.stopPropagation()
  try {
    const r = await toggleFlag(m.value.id, 'favorite')
    m.value.favorite = r && r.value != null ? r.value : !m.value.favorite
    emit('changed', m.value.id)
  } catch (err) { toast(err.message, 'err') }
}

async function play(e) {
  e.stopPropagation()
  try {
    await playMovie(m.value.id)
    toast(t('player.launchedExternal'), 'ok')
  } catch (err) { toast(err.message, 'err') }
}
</script>

<template>
  <article
    class="card"
    :class="{ selected }"
    @click="onCardClick"
    :title="m.title || m.code"
  >
    <div class="thumb">
      <img
        :src="coverSrc"
        :class="{ loading: !loaded }"
        loading="lazy"
        decoding="async"
        referrerpolicy="no-referrer"
        alt=""
        @load="(e) => { loaded = true }"
        @error="(e) => { loaded = true; coverFallback(e) }"
      />
      <div v-if="!loaded" class="skeleton abs"></div>

      <!-- 续播进度条 -->
      <div v-if="watchPct > 0 && !progressFinished" class="progress" :title="`已看 ${watchPct}%`">
        <i :style="{ width: watchPct + '%' }"></i>
      </div>
      <div v-else-if="progressFinished" class="done-badge" title="已看完">✓</div>

      <!-- 左上：多选 / 番号 -->
      <input
        v-if="selectable"
        class="pick"
        type="checkbox"
        :checked="selected"
        @click="pick"
      />
      <div class="tl" v-show="!state.selMode && !selected">
        <span v-if="m.code" class="tag code">{{ m.code }}</span>
      </div>

      <!-- 右上：收藏 -->
      <div class="tr">
        <button class="fav" :class="{ on: m.favorite }" @click="fav" :data-tip="m.favorite ? '取消收藏' : '收藏'">
          {{ m.favorite ? '♥' : '♡' }}
        </button>
      </div>

      <!-- 中央播放 -->
      <button class="play-fab" @click="play" :data-tip="$t('player.external')">▶</button>

      <!-- 底部标记 -->
      <div class="bl-tags">
        <span v-if="m.has_subtitle" class="tag sub">字幕</span>
        <span v-if="m.uncensored" class="tag unc">无码</span>
        <span v-if="quality" class="tag hd">{{ quality }}</span>
        <span v-if="m.duration_minutes" class="tag">{{ fmtMin(m.duration_minutes) }}</span>
      </div>

      <!-- 观看进度 -->
      <div v-if="watchPct > 0" class="watch-bar"><i :style="{ width: watchPct + '%' }"></i></div>
    </div>

    <div class="meta">
      <div class="title">{{ m.title || m.code || '未命名' }}</div>
      <div class="sub">
        <span v-if="m.rating" class="rate">★ {{ m.rating }}</span>
        <span v-if="m.actresses" class="ellipsis">{{ m.actresses }}</span>
        <span v-else-if="m.studio" class="ellipsis">{{ m.studio }}</span>
        <span v-else class="dim">{{ fmtSize(m.size_bytes) }}</span>
      </div>
    </div>
  </article>
</template>

<style scoped>
.skeleton.abs { position: absolute; inset: 0; border-radius: 0; }
.rate { color: var(--c-gold); flex: none; font-variant-numeric: tabular-nums; }
.pick { position: absolute; }
.progress { position: absolute; left: 0; right: 0; bottom: 0; height: 4px; background: rgba(0,0,0,.35); z-index: 3; }
.progress > i { display: block; height: 100%; background: linear-gradient(90deg, var(--c-accent), var(--c-accent-2)); }
.done-badge { position: absolute; left: 6px; bottom: 6px; z-index: 3; width: 18px; height: 18px; border-radius: 50%; background: var(--c-success, #3fb950); color: #fff; font-size: 11px; line-height: 18px; text-align: center; }

.card .thumb img.placeholder {
  filter: grayscale(100%) opacity(.55) contrast(.92);
}
[data-theme='light'] .card .thumb img.placeholder {
  filter: grayscale(80%) opacity(.45) contrast(.92);
}
</style>
