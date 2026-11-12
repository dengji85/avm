<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { state } from '../state.js'
import { allMovieMarkers } from '../api.js'
import { t } from '../i18n/index.js'
import PageHead from '../components/PageHead.vue'
import Pager from '../components/Pager.vue'

const items = ref([])
const tags = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 24
const selected = ref([])          // 已选标签名（OR 匹配）
const loading = ref(false)
const error = ref('')

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

function fmtClock(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = String(s % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}

function thumbUrl(m) {
  return m && m.thumb ? '/api/covers/' + m.thumb : ''
}

function previewUrl(m) {
  return m && m.preview ? '/api/covers/' + m.preview : ''
}

/* 悬停预览：同一时刻只挂一个 <video>，移出即卸载（释放解码与内存）。
   触屏设备或不支持自动播放的环境保持静态缩略图。 */
const hoverKey = ref(0)
const canHoverPlay = typeof window !== 'undefined' && !!window.matchMedia
  && !window.matchMedia('(hover: none)').matches

function onHover(m) {
  if (canHoverPlay && m && m.preview) hoverKey.value = m.id
}
function onLeave() {
  hoverKey.value = 0
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await allMovieMarkers({
      page: page.value,
      page_size: pageSize,
      tag: selected.value.join(','),
    })
    items.value = (r && r.items) || []
    tags.value = (r && r.tags) || []
    total.value = Number((r && r.total) || 0)
  } catch (e) {
    error.value = e.message || t('markers.loadFailed')
    items.value = []
  } finally {
    loading.value = false
  }
}

function toggleTag(name) {
  const i = selected.value.indexOf(name)
  if (i >= 0) selected.value.splice(i, 1)
  else selected.value.push(name)
  page.value = 1
  load()
}

function clearTags() {
  if (!selected.value.length) return
  selected.value = []
  page.value = 1
  load()
}

function goPage(p) {
  page.value = p
  load()
  const el = document.querySelector('.view-body')
  if (el) el.scrollTop = 0
}

/* 点卡片：打开详情页的内嵌播放器并跳到该段起点（沿用 AI 时间戳那套 avm-seek 链路） */
function openMarker(m) {
  if (!m || !m.movie) return
  state.currentId = m.movie.id
  state.aiSeek = { id: m.movie.id, seconds: m.start, n: Date.now() }
}

/* 从详情页标签跳过来：带上该标签筛一次，然后清掉来源标记 */
function applyExternalFilter() {
  const name = String(state.markersFilter || '').trim()
  if (!name) return false
  state.markersFilter = null
  selected.value = [name]
  page.value = 1
  load()
  return true
}

onMounted(() => {
  if (applyExternalFilter()) return
  load()
})

watch(() => state.markersFilter, (v) => { if (v) applyExternalFilter() })
</script>

<template>
  <section class="view">
    <div class="view-body mk-view">
      <PageHead :title="$t('markers.title')" :sub="$t('markers.sub')">
        <template #actions>
          <button class="btn ghost" @click="load">{{ $t('markers.refresh') }}</button>
        </template>
      </PageHead>

      <div v-if="tags.length || selected.length" class="mk-filter chip-list">
        <button
          v-for="tg in tags"
          :key="tg.name"
          class="chip"
          :class="{ on: selected.includes(tg.name) }"
          @click="toggleTag(tg.name)"
        >{{ tg.name }}<span class="mk-n">{{ tg.count }}</span></button>
        <button v-if="selected.length" class="chip" @click="clearTags">✕ {{ $t('markers.clearTags') }}</button>
      </div>

      <div v-if="loading" class="mk-hint">{{ $t('markers.loading') }}</div>
      <div v-else-if="error" class="mk-hint err">{{ error }}</div>
      <div v-else-if="!items.length" class="mk-empty">
        <div class="mk-empty-ico">⚑</div>
        <b>{{ selected.length ? $t('markers.emptyFiltered') : $t('markers.empty') }}</b>
        <p>{{ $t('markers.emptyHint') }}</p>
        <button v-if="selected.length" class="btn tiny" @click="clearTags">{{ $t('markers.clearTags') }}</button>
      </div>

      <div v-else class="mk-grid">
        <div
          v-for="m in items"
          :key="m.id"
          class="mk-card"
          role="button"
          tabindex="0"
          :title="$t('markers.open')"
          @click="openMarker(m)"
          @keydown.enter="openMarker(m)"
        >
          <div class="mk-thumb" @mouseenter="onHover(m)" @mouseleave="onLeave">
            <video
              v-if="m.preview && hoverKey === m.id"
              class="mk-video"
              :src="previewUrl(m)"
              :poster="thumbUrl(m) || undefined"
              muted loop playsinline autoplay preload="none"
              @error="hoverKey = 0"
            ></video>
            <img v-else-if="m.thumb" :src="thumbUrl(m)" alt="" loading="lazy" />
            <div v-else class="mk-ph">▶</div>
            <span class="mk-time tabular">{{ fmtClock(m.start) }}</span>
            <span v-if="m.primary" class="mk-star" :title="$t('player.markerPrimary')">★</span>
          </div>
          <div class="mk-body">
            <div class="mk-top">
              <span class="mk-code ellipsis">{{ m.movie.code || m.movie.title }}</span>
              <span class="mk-range tabular">{{ fmtClock(m.start) }}–{{ fmtClock(m.end) }}</span>
            </div>
            <div class="mk-label ellipsis">{{ m.label || $t('markers.moment') }}</div>
            <div v-if="m.tags.length" class="mk-tagrow">
              <button
                v-for="tg in m.tags"
                :key="tg"
                class="mk-tag"
                :class="{ on: selected.includes(tg) }"
                @click.stop="toggleTag(tg)"
              >{{ tg }}</button>
            </div>
          </div>
        </div>
      </div>

      <Pager v-if="!loading && pageCount > 1" :page="page" :page-count="pageCount" :total="total" @go="goPage" />
    </div>
  </section>
</template>

<style scoped>
.mk-view { padding-bottom: 28px; }

.mk-filter {
  padding: var(--sp-2) var(--sp-4) var(--sp-1);
  align-items: center;
}
.mk-n {
  margin-left: 6px;
  font-size: 11px;
  opacity: .55;
  font-variant-numeric: tabular-nums;
}

.mk-hint { padding: var(--sp-6) var(--sp-4); color: var(--c-text-3); font-size: var(--fs-sm); }
.mk-hint.err { color: var(--c-danger); }

.mk-empty {
  margin: var(--sp-6) auto;
  max-width: 420px;
  text-align: center;
  color: var(--c-text-2);
  display: flex; flex-direction: column; gap: 6px; align-items: center;
}
.mk-empty-ico { font-size: 30px; opacity: .5; }
.mk-empty p { margin: 0; font-size: var(--fs-sm); color: var(--c-text-3); }

.mk-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-4);
}

.mk-card {
  display: flex; flex-direction: column;
  background: var(--c-surface);
  border: 1px solid var(--c-line);
  border-radius: var(--r-md);
  overflow: hidden;
  cursor: pointer;
  transition: border-color .15s, transform .15s, box-shadow .15s;
}
.mk-card:hover, .mk-card:focus-visible {
  border-color: var(--c-primary);
  transform: translateY(-1px);
  box-shadow: var(--sh-2);
  outline: none;
}

.mk-thumb {
  position: relative;
  aspect-ratio: 16 / 9;
  background: var(--c-surface-2);
}
.mk-thumb img,
.mk-thumb video {
  width: 100%; height: 100%;
  object-fit: cover;
  display: block;
}
.mk-ph {
  width: 100%; height: 100%;
  display: grid; place-items: center;
  color: var(--c-text-3); font-size: 22px;
}
.mk-time {
  position: absolute; left: 6px; bottom: 6px;
  padding: 1px 6px; border-radius: 4px;
  background: rgba(0, 0, 0, .66); color: #fff;
  font-size: 11px; font-weight: 600;
}
.mk-star {
  position: absolute; right: 6px; top: 6px;
  width: 20px; height: 20px;
  display: grid; place-items: center;
  border-radius: 50%;
  background: rgba(0, 0, 0, .6);
  color: #ffcf4a; font-size: 11px;
}

.mk-body { padding: var(--sp-2) var(--sp-2) var(--sp-3); display: flex; flex-direction: column; gap: 3px; }
.mk-top { display: flex; align-items: baseline; gap: var(--sp-2); }
.mk-code { font-size: var(--fs-sm); font-weight: 650; color: var(--c-text); min-width: 0; flex: 1; }
.mk-range { font-size: 11px; color: var(--c-text-3); flex: none; }
.mk-label { font-size: var(--fs-sm); color: var(--c-text-2); }
.mk-tagrow { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 2px; }
.mk-tag {
  appearance: none; cursor: pointer;
  padding: 1px 7px; border-radius: 999px;
  border: 1px solid var(--c-line-strong);
  background: var(--c-surface-2);
  color: var(--c-text-3);
  font-size: 11px; line-height: 1.5;
}
.mk-tag:hover { color: var(--c-text); border-color: var(--c-primary-line); }
.mk-tag.on { color: #fff; background: var(--c-primary); border-color: var(--c-primary); }

@media (max-width: 640px) {
  .mk-grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: var(--sp-2); padding: var(--sp-2); }
  .mk-filter { padding: var(--sp-2); }
}
</style>
