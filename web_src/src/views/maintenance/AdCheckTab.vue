<script setup>
/**
 * 维护 → 广告筛查：按画面抽帧判断片头/片尾是否拼了宣传片、整片是否带跑马灯。
 *
 * 为什么做成维护页的一张卡：这是一次「操作」而不是「配置」——配置很少改，
 * 平时点的是「开始筛查」。判据与阈值说明见 app/adcheck.py 顶部注释。
 *
 * 交互要点：
 *  - 增量筛查（只测没测过的）与全量重测分开按钮，避免误点全库跑十几分钟；
 *  - 每行都能单独重测 / 标记「不是广告」（ignored=1 后不再出现在默认列表）；
 *  - 证据图是一张抽帧缩略图墙，红框=被判为带整行文字的帧（片头红框密=广告段）。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import {
  adCheckCancel, adCheckIgnore, adCheckResults, adCheckRunOne, adCheckStart,
  adCheckStatus, adSheetUrl, coverThumbUrl,
} from '../../api.js'
import { t } from '../../i18n/index.js'
import { toast } from '../../utils.js'

const job = reactive({
  running: false, total: 0, done: 0, percent: 0, checked: 0, flagged: 0,
  failed: 0, skipped: 0, message: '', current: '',
})
const items = ref([])
const counts = reactive({ likely: 0, suspect: 0, clean: 0, ignored: 0, total: 0 })
const level = ref('')            // '' 全部 | likely | suspect
const loading = ref(false)
const starting = ref(false)
const sheetId = ref(0)           // 正在查看证据图的影片
const busy = reactive({})        // movie_id -> true，单片操作中
let timer = null

const pct = computed(() =>
  job.total ? Math.round((job.done / job.total) * 100) : (Number(job.percent) || 0))

const LEVELS = [
  { id: '', label: 'maint.adFilterAll' },
  { id: 'likely', label: 'maint.adFilterLikely' },
  { id: 'suspect', label: 'maint.adFilterSuspect' },
]

function levelLabel(lv) {
  return lv === 'likely' ? t('maint.adLikely')
    : lv === 'suspect' ? t('maint.adSuspect')
      : lv === 'clean' ? t('maint.adClean') : lv
}

function fmtMin(sec) {
  const m = Math.round((Number(sec) || 0) / 60)
  return m > 0 ? `${m}min` : ''
}

async function load() {
  loading.value = true
  try {
    const r = await adCheckResults({ level: level.value, limit: 400 })
    items.value = r.items || []
    Object.assign(counts, r.counts || {})
  } catch (e) {
    /* 读取失败保持旧列表 */
  } finally {
    loading.value = false
  }
}

function setLevel(lv) {
  level.value = lv
  load()
}

async function start(force = false) {
  if (starting.value || job.running) return
  starting.value = true
  try {
    const s = await adCheckStart(force)
    Object.assign(job, s)
    if (!s.total && !s.running) {
      toast(t('maint.adNothing'), 'ok', 4000)
      starting.value = false
      return
    }
    poll()
  } catch (e) {
    toast(e.message, 'err')
    starting.value = false
  }
}

async function stop() {
  try {
    Object.assign(job, await adCheckCancel())
    toast(t('maint.adStopping'), 'ok')
  } catch (e) { toast(e.message, 'err') }
}

async function poll() {
  clearTimeout(timer)
  try {
    const s = await adCheckStatus()
    Object.assign(job, s)
    if (s.running) {
      timer = setTimeout(poll, 1000)
      return
    }
    starting.value = false
    toast(s.message || t('maint.adDone'), 'ok', 5000)
    await load()
  } catch (e) {
    starting.value = false
  }
}

async function retest(id) {
  if (busy[id]) return
  busy[id] = true
  try {
    const res = await adCheckRunOne(id)
    toast(`${levelLabel(res.level)} · ${res.score}`, res.level === 'clean' ? 'ok' : 'err', 4000)
    await load()
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    busy[id] = false
  }
}

async function toggleIgnore(it) {
  if (busy[it.movie_id]) return
  busy[it.movie_id] = true
  try {
    await adCheckIgnore(it.movie_id, !it.ignored)
    await load()
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    busy[it.movie_id] = false
  }
}

onMounted(async () => {
  await load()
  try {
    const s = await adCheckStatus()
    Object.assign(job, s)
    if (s.running) poll()
  } catch (e) { /* 后端未就绪时忽略 */ }
})

onBeforeUnmount(() => clearTimeout(timer))
</script>

<template>
  <div class="ad-tab">
    <section class="panel">
      <div class="panel-head">
        {{ $t('maint.adTitle') }}
        <span class="sub">{{ $t('maint.adSub') }}</span>
      </div>
      <div class="panel-body">
        <p class="muted">{{ $t('maint.adHint') }}</p>
        <p class="ad-scope">{{ $t('maint.adScope') }}</p>

        <div class="ad-run">
          <button class="btn primary" :disabled="starting || job.running" @click="start(false)">
            {{ job.running ? $t('maint.adRunning') : $t('maint.adStart') }}
          </button>
          <button class="btn ghost" :disabled="starting || job.running" @click="start(true)">
            {{ $t('maint.adStartAll') }}
          </button>
          <button v-if="job.running" class="btn ghost" @click="stop">{{ $t('common.cancel') }}</button>
          <span v-if="job.running" class="hint">
            {{ job.done }}/{{ job.total }} · {{ job.current }}
          </span>
        </div>

        <div v-if="job.total || job.percent" class="xp-bar"><i :style="{ width: pct + '%' }"></i></div>
        <p v-if="job.message && !job.running" class="hint">{{ job.message }}</p>

        <div class="ad-stats">
          <button class="stat" :class="{ on: level === 'likely' }" @click="setLevel('likely')">
            <b class="s-likely">{{ counts.likely }}</b>{{ $t('maint.adLikely') }}
          </button>
          <button class="stat" :class="{ on: level === 'suspect' }" @click="setLevel('suspect')">
            <b class="s-suspect">{{ counts.suspect }}</b>{{ $t('maint.adSuspect') }}
          </button>
          <button class="stat" :class="{ on: level === '' }" @click="setLevel('')">
            <b>{{ counts.clean }}</b>{{ $t('maint.adClean') }}
          </button>
          <span class="stat plain" v-if="counts.ignored">
            <b>{{ counts.ignored }}</b>{{ $t('maint.adIgnored') }}
          </span>
        </div>

        <div class="ad-filter">
          <button v-for="lv in LEVELS" :key="lv.id" class="chip"
                  :class="{ on: level === lv.id }" @click="setLevel(lv.id)">
            {{ $t(lv.label) }}
          </button>
          <span class="hint">{{ $t('maint.adTip') }}</span>
        </div>

        <p v-if="!items.length && !loading" class="muted ad-empty">{{ $t('maint.adEmpty') }}</p>

        <ul class="ad-list">
          <li v-for="it in items" :key="it.movie_id" class="ad-item" :class="'lv-' + it.level">
            <img class="ad-cover" :src="coverThumbUrl(it.movie_id, 120)" :alt="it.code" loading="lazy" />
            <div class="ad-main">
              <div class="ad-line">
                <span class="ad-code">{{ it.code || ('#' + it.movie_id) }}</span>
                <span class="ad-badge" :class="'b-' + it.level">{{ levelLabel(it.level) }}</span>
                <span class="ad-score">{{ it.score }}</span>
                <span v-if="it.ignored" class="ad-badge b-ignored">{{ $t('maint.adIgnored') }}</span>
              </div>
              <div class="ad-title">{{ it.title }}</div>
              <ul class="ad-reasons">
                <li v-for="(r, i) in (it.signals && it.signals.reasons) || []" :key="i">{{ r }}</li>
              </ul>
              <div class="ad-meta">
                <span>{{ $t('maint.adHead') }} {{ Math.round((it.head_hi || 0) * 100) }}%</span>
                <span>{{ $t('maint.adBody') }} {{ Math.round((it.body_hi || 0) * 100) }}%</span>
                <span>{{ $t('maint.adTail') }} {{ Math.round((it.tail_hi || 0) * 100) }}%</span>
                <span v-if="it.signals && it.signals.intro_end">
                  {{ $t('maint.adIntroEnd', { n: Math.round(it.signals.intro_end) }) }}
                </span>
                <span>{{ fmtMin(it.duration) }}</span>
              </div>
            </div>
            <div class="ad-ops">
              <button class="btn xs" :disabled="!!busy[it.movie_id]" @click="retest(it.movie_id)">
                {{ busy[it.movie_id] ? '…' : $t('maint.adRetest') }}
              </button>
              <button class="btn xs ghost" @click="sheetId = it.movie_id">
                {{ $t('maint.adEvidence') }}
              </button>
              <button class="btn xs ghost" @click="toggleIgnore(it)">
                {{ it.ignored ? $t('maint.adUnignore') : $t('maint.adIgnore') }}
              </button>
            </div>
          </li>
        </ul>
      </div>
    </section>

    <!-- 证据图：抽帧缩略图墙，红框 = 带整行文字的帧 -->
    <div v-if="sheetId" class="ad-modal" @click.self="sheetId = 0">
      <div class="ad-modal-box">
        <div class="ad-modal-head">
          <span>{{ $t('maint.adEvidence') }}</span>
          <button class="btn xs ghost" @click="sheetId = 0">{{ $t('common.close') }}</button>
        </div>
        <img :src="adSheetUrl(sheetId)" :alt="'sheet-' + sheetId" />
        <p class="hint">{{ $t('maint.adEvidenceHint') }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ad-tab { display: flex; flex-direction: column; gap: var(--sp-4); }
.ad-tab .panel { width: 100%; }
.muted { margin: 0; }
/* 与「存储·体检 → 劣质片」的边界说明：两者检测对象不同，不是重复功能 */
.ad-scope {
  margin: 8px 0 0; padding: 8px 10px; border-radius: var(--r-sm, 6px);
  background: var(--c-surface-3); border-left: 2px solid var(--c-line);
  font-size: var(--fs-xs); color: var(--c-text-3); line-height: 1.6;
}
.ad-run { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 12px 0; }
.xp-bar { height: 6px; border-radius: 3px; background: var(--c-surface-3); overflow: hidden; }
.xp-bar i { display: block; height: 100%; background: var(--c-primary); transition: width .4s; }

.ad-stats { display: flex; gap: 10px; flex-wrap: wrap; margin: 14px 0 10px; }
.stat {
  display: flex; align-items: baseline; gap: 6px; padding: 8px 14px; cursor: pointer;
  border: 1px solid var(--c-line); border-radius: var(--r-sm, 8px);
  background: var(--c-surface-2); color: var(--c-text-2); font-size: var(--fs-sm);
}
.stat.on { border-color: var(--c-primary); color: var(--c-text); }
.stat b { font-size: var(--fs-lg, 18px); color: var(--c-text); }
.stat .s-likely { color: #e5534b; }
.stat .s-suspect { color: #d29922; }
.stat.plain { cursor: default; }

.ad-filter { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.chip {
  padding: 3px 10px; border-radius: 999px; border: 1px solid var(--c-line);
  background: transparent; color: var(--c-text-2); font-size: var(--fs-xs); cursor: pointer;
}
.chip.on { border-color: var(--c-primary); color: var(--c-text); background: var(--c-surface-3); }
.ad-empty { padding: 18px 0; }

.ad-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.ad-item {
  display: flex; gap: 12px; padding: 10px; border: 1px solid var(--c-line);
  border-radius: var(--r-sm, 8px); background: var(--c-surface-2);
  border-left: 3px solid var(--c-line);
}
.ad-item.lv-likely { border-left-color: #e5534b; }
.ad-item.lv-suspect { border-left-color: #d29922; }
.ad-item.lv-clean { border-left-color: #3fb950; }
.ad-cover { width: 54px; height: 72px; object-fit: cover; border-radius: 4px; flex: 0 0 auto; }
.ad-main { flex: 1 1 auto; min-width: 0; }
.ad-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ad-code { font-weight: 600; }
.ad-score { font-variant-numeric: tabular-nums; color: var(--c-text-3); font-size: var(--fs-sm); }
.ad-badge {
  font-size: var(--fs-xs); padding: 1px 7px; border-radius: 999px;
  border: 1px solid currentColor;
}
.b-likely { color: #e5534b; }
.b-suspect { color: #d29922; }
.b-clean { color: #3fb950; }
.b-ignored { color: var(--c-text-3); }
.ad-title {
  color: var(--c-text-2); font-size: var(--fs-sm); margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ad-reasons { margin: 4px 0 0; padding-left: 16px; color: var(--c-text-3); font-size: var(--fs-xs); }
.ad-meta { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 4px; color: var(--c-text-3); font-size: var(--fs-xs); }
.ad-ops { display: flex; flex-direction: column; gap: 6px; flex: 0 0 auto; justify-content: center; }

.ad-modal {
  position: fixed; inset: 0; z-index: 60; background: rgba(0, 0, 0, .72);
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.ad-modal-box {
  background: var(--c-surface); border-radius: var(--r-md, 12px); padding: 14px;
  max-width: min(1400px, 96vw); max-height: 92vh; overflow: auto;
}
.ad-modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.ad-modal-box img { display: block; width: 100%; height: auto; border-radius: 6px; }
</style>
