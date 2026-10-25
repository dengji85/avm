<script setup>
/**
 * 影片资料卡：把已刮到的元数据摊开给人看。
 *
 * 这是唯一一张「不需要模型」的影片卡：查资料不该先烧一次调用，
 * 也不该因为没配模型就什么都不给。底部的「同女优 / 同系列 / 同片商」
 * 直接查库（本机 SQLite），比让模型复述更准。
 */
import { computed, ref } from 'vue'
import { aiWorkspace as ws } from '../../composables/aiWorkspace.js'
import { fmtDate, fmtMin, fmtSize } from '../../utils.js'
import { t } from '../../i18n/index.js'
import AiCard from '../AiCard.vue'

const chat = ws.chat
const ai = ws.ai
const movie = computed(() => ws.movie)
const open = ref(true)

const rows = computed(() => {
  const m = movie.value
  if (!m) return []
  const out = []
  const add = (label, value) => { if (value !== '' && value != null) out.push({ label, value }) }
  const join = (v) => (Array.isArray(v) ? v.filter(Boolean).join(' / ') : v)

  add(t('detail.fCode'), m.code)
  add(t('detail.fTitle'), m.title)
  add(t('detail.fRelease'), m.release)
  add(t('detail.fRuntime'), fmtMin(Math.round((Number(m.duration_seconds) || 0) / 60)))
  add(t('detail.fStudio'), m.studio)
  add(t('detail.fSeries'), m.series)
  add(t('detail.fActress'), join(m.actresses))
  add(t('detail.fGenre'), join(m.genres))
  add(t('ai.tagsTitle'), join(m.tags))
  add(t('ai.chat.infoRating'), m.rating ? String(m.rating) : '')
  add(t('detail.fResolution'), m.resolution)
  add(t('detail.fSize'), fmtSize(m.size))
  if (ai.subs.tracks.length) add(t('ai.statSubs'), t('ai.subsTracks', { n: ai.subs.tracks.length }))
  add(t('detail.fAdded'), fmtDate(m.added_at))
  return out
})

/** 女优最多给 3 个入口，多了会挤成一片 */
const actresses = computed(() => ((movie.value && movie.value.actresses) || []).slice(0, 3))
</script>

<template>
  <AiCard v-model:open="open" :title="$t('ai.chat.infoTitle')" :badge="movie && movie.code" :summary="$t('ai.chat.chipInfo')">
    <div class="asst-rows">
      <div v-for="r in rows" :key="r.label" class="asst-row-line">
        <span class="asst-hint">{{ r.label }}</span>
        <span class="spacer"></span>
        <span class="asst-row-val">{{ r.value }}</span>
      </div>
    </div>

    <div v-if="actresses.length || (movie && (movie.series || movie.studio))" class="asst-actions">
      <button v-for="a in actresses" :key="a" class="asst-chip" @click="chat.runMore('actress', a)">
        {{ $t('ai.chat.moreActress') }} · {{ a }}
      </button>
      <button v-if="movie && movie.series" class="asst-chip" @click="chat.runMore('series')">
        {{ $t('ai.chat.moreSeries') }} · {{ movie.series }}
      </button>
      <button v-if="movie && movie.studio" class="asst-chip" @click="chat.runMore('studio')">
        {{ $t('ai.chat.moreStudio') }} · {{ movie.studio }}
      </button>
    </div>
  </AiCard>
</template>
