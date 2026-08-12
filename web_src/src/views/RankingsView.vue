<script setup>
import { ref, computed, onMounted } from 'vue'
import { state } from '../state.js'
import { getRankings, getWatchHistory, coverThumbUrl } from '../api.js'
import { toast, coverFallback, fmtDuration, fmtSize, fmtAgo, fmtDate } from '../utils.js'
import { t } from '../i18n/index.js'

const KINDS = [
  ['watched', 'rankings.watched', (m) => fmtDuration(m.watched_sec)],
  ['play', 'rankings.play', (m) => t('rankings.times', { n: m.play_count || 0 })],
  ['rating', 'rankings.rating', (m) => `${m.rating || 0} 星`],
  ['favorite', 'rankings.favorite', (m) => `${m.rating || 0} 星`],
]

const TABS = [...KINDS.map((k) => k[0]), 'history']

const kind = ref('watched')
const items = ref([])
const historyPage = ref(1)
const historyTotal = ref(0)
const loading = ref(false)

const isHistory = computed(() => kind.value === 'history')
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
    if (k === 'history') {
      const r = await getWatchHistory(historyPage.value, 50)
      items.value = (r && r.items) || []
      historyTotal.value = (r && r.total) || 0
    } else {
      const r = await getRankings(kind.value, 50)
      items.value = (r && r.items) || []
    }
  } catch (e) { toast(e.message, 'err'); items.value = [] } finally { loading.value = false }
}

async function changePage(delta) {
  const next = historyPage.value + delta
  if (next < 1 || (next - 1) * 50 >= historyTotal.value) return
  historyPage.value = next
  await load('history')
}

function open(id) { state.currentId = id }
function medal(i) { return i === 0 ? 'gold' : i === 1 ? 'silver' : i === 2 ? 'bronze' : '' }
function sub(m) { return [m.code, m.studio, m.year].filter(Boolean).join(' · ') }
function methodLabel(method) {
  return { builtin: t('rankings.methodBuiltin'), external: t('rankings.methodExternal'), system: t('rankings.methodSystem') }[method] || method || '—'
}
function histSub(h) { return [h.code, h.studio, h.year].filter(Boolean).join(' · ') }

onMounted(() => load('watched'))
</script>

<template>
  <section class="view">
    <div class="toolbar">
      <h1 class="tb-title">{{ $t('view.rankings') }}</h1>
      <span class="tb-sub tabular" v-if="!loading && !isHistory">Top {{ items.length }}</span>
      <span class="tb-sub tabular" v-else-if="!loading && isHistory">共 {{ historyTotal }} 次观看</span>
      <span v-else class="spinner"></span>
      <div class="spacer"></div>
      <div class="btn-group">
        <button
          v-for="[k, label] in KINDS"
          :key="k"
          class="btn tiny"
          :class="{ active: kind === k }"
          @click="load(k)"
        >{{ $t(label) }}</button>
        <button
          class="btn tiny"
          :class="{ active: kind === 'history' }"
          @click="historyPage = 1; load('history')"
        >{{ $t('rankings.history') }}</button>
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
        <div class="title">{{ isHistory ? $t('rankings.emptyHistory') : $t('rankings.emptyRank') }}</div>
        <div class="desc">{{ isHistory ? $t('rankings.emptyHistoryDesc') : $t('rankings.emptyRankDesc') }}</div>
      </div>

      <template v-else>
        <ol v-if="!isHistory" class="rk-list">
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

        <div v-else class="rk-list">
          <div
            v-for="h in items"
            :key="h.id"
            class="rk-row wh-row"
            @click="open(h.movie_id)"
          >
            <img class="rk-cover" :src="coverThumbUrl(h.movie_id, 160)" alt="" loading="lazy" @error="coverFallback" />
            <div class="rk-meta">
              <div class="rk-title ellipsis">{{ h.title || h.code || '未命名' }}</div>
              <div class="rk-sub ellipsis">{{ histSub(h) }}</div>
            </div>
            <div class="wh-info">
              <div class="wh-time tabular" :title="fmtDate(h.started_at)">{{ fmtAgo(h.started_at) }}</div>
              <div class="wh-tags">
                <span class="tag dur" v-if="h.watched_sec > 0">{{ fmtDuration(h.watched_sec) }}</span>
                <span class="tag done" v-if="h.finished">看完</span>
                <span class="tag method">{{ methodLabel(h.method) }}</span>
              </div>
            </div>
          </div>

          <div v-if="historyTotal > items.length" class="wh-pager">
            <button class="btn tiny ghost" :disabled="historyPage <= 1" @click="changePage(-1)">上一页</button>
            <span class="tabular">第 {{ historyPage }} / {{ Math.ceil(historyTotal / 50) }} 页</span>
            <button class="btn tiny ghost" :disabled="historyPage * 50 >= historyTotal" @click="changePage(1)">下一页</button>
          </div>
        </div>
      </template>
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

/* 观看历史明细行 */
.rk-row.wh-row { grid-template-columns: 46px 1fr auto; cursor: pointer; }
.wh-info {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  min-width: 130px;
}
.wh-time { font-size: var(--fs-sm); color: var(--c-text-2); font-variant-numeric: tabular-nums; }
.wh-tags { display: flex; flex-wrap: wrap; gap: 4px; justify-content: flex-end; }
.tag {
  font-size: var(--fs-xs);
  padding: 1px 6px;
  border-radius: var(--r-full);
  background: var(--c-surface-3);
  color: var(--c-text-3);
  white-space: nowrap;
}
.tag.done { background: color-mix(in srgb, var(--c-success, #3fb950) 20%, transparent); color: var(--c-success, #3fb950); }
.tag.method { background: color-mix(in srgb, var(--c-accent, #e0457b) 16%, transparent); color: var(--c-accent, #e0457b); }

.wh-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-3);
  padding: var(--sp-3);
  font-size: var(--fs-sm);
  color: var(--c-text-2);
}
</style>
