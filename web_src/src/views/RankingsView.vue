<script setup>
import { ref, computed, onMounted } from 'vue'
import { state } from '../state.js'
import { getRankings, coverThumbUrl } from '../api.js'
import { toast, coverFallback, fmtDuration, fmtSize } from '../utils.js'

const KINDS = [
  ['watched', '观看时长', (m) => fmtDuration(m.watched_sec)],
  ['play', '播放次数', (m) => `${m.play_count || 0} 次`],
  ['rating', '最高评分', (m) => `${m.rating || 0} 星`],
  ['favorite', '收藏精选', (m) => `${m.rating || 0} 星`],
]

const kind = ref('watched')
const items = ref([])
const loading = ref(false)

const fmt = computed(() => (KINDS.find((k) => k[0] === kind.value) || KINDS[0])[2])
const maxVal = computed(() => {
  const key = kind.value === 'watched' ? 'watched_sec' : kind.value === 'play' ? 'play_count' : 'rating'
  return Math.max(1, ...items.value.map((m) => Number(m[key]) || 0))
})

function barPct(m) {
  const key = kind.value === 'watched' ? 'watched_sec' : kind.value === 'play' ? 'play_count' : 'rating'
  return Math.max(2, ((Number(m[key]) || 0) / maxVal.value) * 100)
}

async function load(k) {
  if (k) kind.value = k
  loading.value = true
  try {
    const r = await getRankings(kind.value, 50)
    items.value = (r && r.items) || []
  } catch (e) { toast(e.message, 'err'); items.value = [] } finally { loading.value = false }
}

function open(id) { state.currentId = id }
function medal(i) { return i === 0 ? 'gold' : i === 1 ? 'silver' : i === 2 ? 'bronze' : '' }
function sub(m) { return [m.code, m.studio, m.year].filter(Boolean).join(' · ') }

onMounted(() => load('watched'))
</script>

<template>
  <section class="view">
    <div class="toolbar">
      <h1 class="tb-title">排行榜</h1>
      <span class="tb-sub tabular" v-if="!loading">Top {{ items.length }}</span>
      <span v-else class="spinner"></span>
      <div class="spacer"></div>
      <div class="btn-group">
        <button
          v-for="[k, label] in KINDS"
          :key="k"
          class="btn tiny"
          :class="{ active: kind === k }"
          @click="load(k)"
        >{{ label }}</button>
      </div>
    </div>

    <div class="view-body">
      <div v-if="loading && !items.length" class="rk-list">
        <div v-for="i in 10" :key="i" class="rk-row sk">
          <div class="skeleton" style="width:26px;height:26px;border-radius:50%"></div>
          <div class="skeleton" style="width:46px;height:66px;border-radius:6px"></div>
          <div class="grow"><div class="skeleton text" style="width:60%"></div></div>
        </div>
      </div>

      <div v-else-if="!items.length" class="empty">
        <div class="icon">↑</div>
        <div class="title">暂无排行数据</div>
        <div class="desc">观看、评分或收藏影片后，这里会显示排名。</div>
      </div>

      <ol v-else class="rk-list">
        <li
          v-for="(m, i) in items"
          :key="m.id"
          class="rk-row"
          :class="medal(i)"
          @click="open(m.id)"
        >
          <div class="rk-no">{{ i + 1 }}</div>
          <img class="rk-cover" :src="coverThumbUrl(m.id, 160)" alt="" loading="lazy" @error="coverFallback" />
          <div class="rk-meta">
            <div class="rk-title ellipsis">{{ m.title || m.code || '未命名' }}</div>
            <div class="rk-sub ellipsis">{{ sub(m) }}</div>
            <div class="rk-track"><i :style="{ width: barPct(m) + '%' }"></i></div>
          </div>
          <div class="rk-val tabular">{{ fmt(m) }}</div>
        </li>
      </ol>
    </div>
  </section>
</template>

<style scoped>
.rk-list { display: flex; flex-direction: column; gap: var(--sp-2); }

.rk-row {
  display: grid;
  grid-template-columns: 34px 46px 1fr auto;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-md);
  background: var(--c-surface);
  border: 1px solid transparent;
  cursor: pointer;
  transition: border-color var(--t-fast), transform var(--t-fast), background var(--t-fast);
}
.rk-row:hover { border-color: var(--c-line-strong); transform: translateX(2px); }
.rk-row.sk { cursor: default; }

.rk-no {
  display: grid; place-items: center;
  width: 26px; height: 26px;
  border-radius: 50%;
  background: var(--c-surface-3);
  color: var(--c-text-3);
  font-size: var(--fs-sm);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.rk-row.gold   .rk-no { background: linear-gradient(135deg, #f7d774, #d9a520); color: #3a2c00; }
.rk-row.silver .rk-no { background: linear-gradient(135deg, #dfe4ec, #a8b0c0); color: #2a2f3a; }
.rk-row.bronze .rk-no { background: linear-gradient(135deg, #e0a878, #b3703c); color: #3a2010; }

.rk-cover {
  width: 46px; height: 66px;
  object-fit: cover;
  border-radius: var(--r-sm);
  background: var(--c-surface-2);
}

.rk-meta { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.rk-title { font-size: var(--fs-md); font-weight: 500; }
.rk-sub { font-size: var(--fs-xs); color: var(--c-text-3); }
.rk-track {
  height: 4px; border-radius: var(--r-full);
  background: var(--c-surface-3);
  overflow: hidden;
  margin-top: 2px;
}
.rk-track > i {
  display: block; height: 100%;
  background: linear-gradient(90deg, var(--c-primary-d), var(--c-primary));
  border-radius: inherit;
  transition: width var(--t-slow);
}

.rk-val {
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--c-primary-h);
  min-width: 68px;
  text-align: right;
}
</style>
