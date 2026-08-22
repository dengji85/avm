<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { state } from '../state.js'
import { t } from '../i18n/index.js'
import { getYearInReview } from '../api.js'
import { toast } from '../utils.js'

const YEARS = (() => {
  const cur = new Date().getFullYear()
  const arr = []
  for (let y = cur; y >= cur - 9; y--) arr.push(y)
  return arr
})()

const year = ref(new Date().getFullYear())
const loading = ref(false)
const data = reactive({})

function fmtDur(sec) {
  sec = Math.round(Number(sec) || 0)
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (h > 0) return `${h}${t('yr.hour')}${m > 0 ? m + t('yr.min') : ''}`
  return `${m}${t('yr.min')}`
}
function maxCount(list) {
  return Math.max(1, ...list.map((x) => x.count || 0))
}

async function load() {
  loading.value = true
  try {
    const r = await getYearInReview(year.value)
    Object.keys(data).forEach((k) => delete data[k])
    Object.assign(data, r)
  } catch (e) { toast(e.message, 'err') }
  finally { loading.value = false }
}

function openDetail(id) { state.currentId = id }

onMounted(load)
</script>

<template>
  <div class="view-body year-review">
    <div class="yr-head">
      <h1 class="yr-title">{{ t('yr.title') }}</h1>
      <div class="yr-year-pick">
        <button
          v-for="y in YEARS"
          :key="y"
          :class="['yr-year', year === y ? 'sel' : '']"
          @click="year = y; load()"
        >{{ y }}</button>
      </div>
    </div>

    <div v-if="loading" class="yr-loading">{{ t('yr.loading') }}</div>

    <template v-else>
      <!-- 年度总览 -->
      <section class="yr-section">
        <h2 class="yr-h2">{{ year }} {{ t('yr.overview') }}</h2>
        <div class="yr-cards">
          <div class="yr-card">
            <div class="yr-num">{{ data.summary?.movies ?? 0 }}</div>
            <div class="yr-lab">{{ t('yr.movies') }}</div>
          </div>
          <div class="yr-card">
            <div class="yr-num">{{ fmtDur(data.summary?.total_sec ?? 0) }}</div>
            <div class="yr-lab">{{ t('yr.totalTime') }}</div>
          </div>
          <div class="yr-card">
            <div class="yr-num">{{ data.summary?.sessions ?? 0 }}</div>
            <div class="yr-lab">{{ t('yr.sessions') }}</div>
          </div>
          <div class="yr-card">
            <div class="yr-num">{{ data.summary?.days ?? 0 }}</div>
            <div class="yr-lab">{{ t('yr.days') }}</div>
          </div>
          <div class="yr-card">
            <div class="yr-num">{{ data.summary?.longest_streak ?? 0 }}</div>
            <div class="yr-lab">{{ t('yr.streak') }}</div>
          </div>
          <div class="yr-card">
            <div class="yr-num">{{ data.summary?.added ?? 0 }}</div>
            <div class="yr-lab">{{ t('yr.added') }}</div>
          </div>
        </div>
      </section>

      <!-- 月度热力图 -->
      <section class="yr-section">
        <h2 class="yr-h2">{{ t('yr.monthHeat') }}</h2>
        <div class="yr-heat">
          <div v-for="m in data.months || []" :key="m.month" class="yr-heat-col">
            <div
              class="yr-heat-bar"
              :style="{ height: (m.count / maxCount(data.months || []) * 100) + '%' }"
              :title="`${year}-${m.month}： ${m.count} ${t('yr.sessions')} / ${fmtDur(m.sec)}`"
            ></div>
            <div class="yr-heat-m">{{ m.month }}</div>
          </div>
        </div>
      </section>

      <!-- 最爱 -->
      <div class="yr-grid2">
        <section class="yr-section">
          <h2 class="yr-h2">{{ t('yr.topStudio') }}</h2>
          <ul class="yr-rank">
            <li v-for="(s, i) in data.top_studios || []" :key="i">
              <span class="yr-rank-i">{{ i + 1 }}</span>
              <span class="yr-rank-n ellipsis">{{ s.name }}</span>
              <span class="yr-rank-c">{{ s.count }}</span>
            </li>
            <li v-if="!(data.top_studios || []).length" class="yr-empty">{{ t('yr.empty') }}</li>
          </ul>
        </section>
        <section class="yr-section">
          <h2 class="yr-h2">{{ t('yr.topActress') }}</h2>
          <ul class="yr-rank">
            <li v-for="(a, i) in data.top_actresses || []" :key="i">
              <span class="yr-rank-i">{{ i + 1 }}</span>
              <span class="yr-rank-n ellipsis">{{ a.name }}</span>
              <span class="yr-rank-c">{{ a.count }}</span>
            </li>
            <li v-if="!(data.top_actresses || []).length" class="yr-empty">{{ t('yr.empty') }}</li>
          </ul>
        </section>
      </div>

      <section class="yr-section">
        <h2 class="yr-h2">{{ t('yr.topGenre') }}</h2>
        <div class="yr-tags">
          <span v-for="(g, i) in data.top_genres || []" :key="i" class="yr-tag">
            {{ g.name }} <b>{{ g.count }}</b>
          </span>
          <span v-if="!(data.top_genres || []).length" class="yr-empty">{{ t('yr.empty') }}</span>
        </div>
      </section>

      <!-- 评分分布 -->
      <section class="yr-section">
        <h2 class="yr-h2">{{ t('yr.ratingDist') }}</h2>
        <div class="yr-rating">
          <div v-for="n in 5" :key="n" class="yr-rating-row">
            <span class="yr-rating-lab">{{ n }}<span class="yr-star">★</span></span>
            <div class="yr-rating-track">
              <div
                class="yr-rating-fill"
                :style="{ width: ((data.rating_dist?.[n] || 0) / Math.max(1, ...Object.values(data.rating_dist || {0:1}).map(Number)) * 100) + '%' }"
              ></div>
            </div>
            <span class="yr-rating-num">{{ data.rating_dist?.[n] || 0 }}</span>
          </div>
        </div>
      </section>

      <!-- 年度最佳 -->
      <section class="yr-section">
        <h2 class="yr-h2">{{ t('yr.best') }}</h2>
        <div class="yr-best-grid">
          <div
            v-for="m in data.best || []"
            :key="m.id"
            class="yr-best"
            @click="openDetail(m.id)"
          >
            <div class="yr-best-cover" :style="m.cover ? { backgroundImage: `url(/covers/${m.cover})` } : {}">
              <span v-if="!m.cover" class="yr-best-ph">{{ m.code || '—' }}</span>
            </div>
            <div class="yr-best-meta">
              <div class="yr-best-title ellipsis">{{ m.title || m.code }}</div>
              <div class="yr-best-rate">★ {{ m.rating }}</div>
            </div>
          </div>
          <div v-if="!(data.best || []).length" class="yr-empty">{{ t('yr.empty') }}</div>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.year-review { padding: 18px 20px 60px; max-width: 980px; margin: 0 auto; }
.yr-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 18px; }
.yr-title { font-size: 22px; margin: 0; color: var(--text, #e8eaed); }
.yr-year-pick { display: flex; flex-wrap: wrap; gap: 6px; }
.yr-year {
  padding: 5px 11px; border-radius: 999px; cursor: pointer; font-size: 13px;
  background: var(--bg-input, #14161a); color: var(--muted, #9aa0a6);
  border: 1px solid var(--border, #2c2f36);
}
.yr-year.sel { background: var(--accent, #5b9cff); color: #fff; border-color: transparent; }
.yr-loading { padding: 40px; text-align: center; color: var(--muted, #9aa0a6); }
.yr-section { margin-bottom: 26px; }
.yr-h2 { font-size: 15px; color: var(--muted, #9aa0a6); margin: 0 0 12px; font-weight: 600; }
.yr-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.yr-card {
  background: var(--bg-panel, #1b1d22); border: 1px solid var(--border, #2c2f36);
  border-radius: 12px; padding: 16px; text-align: center;
}
.yr-num { font-size: 24px; font-weight: 700; color: var(--text, #e8eaed); }
.yr-lab { font-size: 12px; color: var(--muted, #9aa0a6); margin-top: 4px; }

.yr-heat { display: flex; align-items: flex-end; gap: 6px; height: 140px; padding: 0 4px; }
.yr-heat-col { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }
.yr-heat-bar { width: 70%; background: linear-gradient(180deg, var(--accent, #5b9cff), #3a6fc4); border-radius: 4px 4px 0 0; min-height: 2px; transition: height .3s; }
.yr-heat-m { font-size: 11px; color: var(--muted, #9aa0a6); margin-top: 4px; }

.yr-grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
.yr-rank { list-style: none; margin: 0; padding: 0; }
.yr-rank li { display: flex; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px dashed var(--border, #2c2f36); }
.yr-rank-i { width: 20px; height: 20px; line-height: 20px; text-align: center; border-radius: 50%; background: var(--bg-input, #14161a); font-size: 12px; color: var(--accent, #5b9cff); }
.yr-rank-n { flex: 1; color: var(--text, #e8eaed); }
.yr-rank-c { color: var(--muted, #9aa0a6); font-size: 13px; }
.yr-tags { display: flex; flex-wrap: wrap; gap: 8px; }
.yr-tag { padding: 6px 12px; border-radius: 999px; background: var(--bg-input, #14161a); color: var(--text, #e8eaed); border: 1px solid var(--border, #2c2f36); font-size: 13px; }
.yr-tag b { color: var(--accent, #5b9cff); margin-left: 4px; }

.yr-rating { display: flex; flex-direction: column; gap: 8px; }
.yr-rating-row { display: flex; align-items: center; gap: 10px; }
.yr-rating-lab { width: 36px; color: var(--text, #e8eaed); font-size: 13px; }
.yr-star { color: #f5c542; }
.yr-rating-track { flex: 1; height: 12px; border-radius: 6px; background: var(--bg-input, #14161a); overflow: hidden; }
.yr-rating-fill { height: 100%; background: linear-gradient(90deg, #f5c542, #e89b2d); }
.yr-rating-num { width: 30px; text-align: right; color: var(--muted, #9aa0a6); font-size: 13px; }

.yr-best-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.yr-best { cursor: pointer; background: var(--bg-panel, #1b1d22); border: 1px solid var(--border, #2c2f36); border-radius: 10px; overflow: hidden; }
.yr-best-cover { height: 120px; background-size: cover; background-position: center; background-color: var(--bg-input, #14161a); display: flex; align-items: center; justify-content: center; }
.yr-best-ph { color: var(--muted, #9aa0a6); font-size: 12px; }
.yr-best-meta { padding: 8px 10px; }
.yr-best-title { font-size: 13px; color: var(--text, #e8eaed); }
.yr-best-rate { font-size: 12px; color: #f5c542; margin-top: 2px; }
.yr-empty { color: var(--muted, #9aa0a6); font-size: 13px; padding: 8px 0; }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
