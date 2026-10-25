<script setup>
/** 剧情简介卡：先看草稿，确认后才写库；新旧并排，避免覆盖手改内容。 */
import { computed, ref } from 'vue'
import { aiWorkspace as ws } from '../../composables/aiWorkspace.js'
import { t } from '../../i18n/index.js'
import AiCard from '../AiCard.vue'
import TraceBlock from './TraceBlock.vue'

const ai = ws.ai
const movie = computed(() => ws.movie)
const open = ref(true)

const sourceLabel = computed(() => {
  const s = ai.plotSource
  if (s === 'subtitle') return t('ai.srcAiSubtitle')
  if (s === 'dialog') return t('ai.srcAiDialog')
  if (s === 'meta') return t('ai.srcAiMeta')
  if (s === 'manual') return t('ai.srcManual')
  if (s === 'scraped') return t('ai.srcScraped')
  return ''
})
const summary = computed(() => (ai.plot.chars ? t('ai.plotBasis', { n: ai.plot.chars }) : ''))
const promptId = computed(() => (ai.plot.source === 'subtitle' ? 'plot.subtitle' : 'synopsis'))
</script>

<template>
  <AiCard v-model:open="open" :title="$t('ai.plotTitle')" :badge="sourceLabel" :summary="summary">
    <template #head>
      <span v-if="ai.plot.loading" class="asst-sm">{{ $t('ai.working') }}</span>
      <button v-else class="btn tiny primary" @click="ai.generatePlot()">
        {{ ai.hasDraft ? $t('ai.regenerate') : $t('ai.generate') }}
      </button>
    </template>

    <p v-if="ai.plot.loading && !ai.hasDraft" class="asst-hint">{{ $t('ai.chat.busyPlot') }}</p>

    <div v-if="ai.hasDraft" class="asst-compare">
      <div class="asst-col">
        <div class="asst-col-head">{{ $t('ai.oldVersion') }}</div>
        <p class="asst-text">{{ ai.plot.old || $t('ai.none') }}</p>
      </div>
      <div class="asst-col new">
        <div class="asst-col-head">
          {{ $t('ai.newVersion') }}
          <span class="badge ok">{{ ai.plot.source === 'subtitle' ? $t('ai.srcSubtitle') : $t('ai.srcMeta') }}</span>
        </div>
        <p class="asst-text">{{ ai.plot.draft }}</p>
      </div>
    </div>
    <p v-else-if="!ai.plot.loading" class="asst-text">{{ (movie && movie.plot) || $t('ai.plotEmpty') }}</p>

    <p v-if="ai.plot.track && !ai.plot.loading" class="asst-hint">{{ $t('ai.plotTrack', { track: ai.plot.track }) }}</p>
    <p v-if="ai.plot.degradedFrom" class="asst-hint">
      {{ ai.plot.degradedFrom === 'subtitle' ? $t('ai.plotDegradedSubtitle') : $t('ai.plotDegradedDialog') }}
    </p>
    <p v-if="ai.plot.error" class="asst-err">{{ ai.plot.error }}</p>

    <div v-if="ai.hasDraft" class="asst-actions">
      <button class="btn tiny primary" @click="ai.applyPlot">{{ $t('ai.useNew') }}</button>
      <button class="btn tiny ghost" @click="ai.discardPlot">{{ $t('ai.keepOld') }}</button>
    </div>

    <TraceBlock k="plot" :prompt-id="promptId" />
  </AiCard>
</template>
