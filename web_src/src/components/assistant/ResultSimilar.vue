<script setup>
/** 相似推荐卡：带「为什么相似」，可解释才敢点进去。 */
import { computed, ref } from 'vue'
import { aiWorkspace as ws } from '../../composables/aiWorkspace.js'
import { coverThumbUrl } from '../../api.js'
import { t } from '../../i18n/index.js'
import AiCard from '../AiCard.vue'

const ai = ws.ai
const open = ref(true)
const summary = computed(() => (ai.similar.items.length ? t('ai.simCount', { n: ai.similar.items.length }) : ''))

function reasonText(r) {
  if (!r) return ''
  const names = (r.names || []).filter(Boolean).join(' / ')
  if (r.kind === 'actress') return names ? t('ai.whyActress', { names }) : t('ai.whyActressBare')
  if (r.kind === 'series') return t('ai.whySeries', { names })
  if (r.kind === 'studio') return t('ai.whyStudio', { names })
  if (r.kind === 'tag') return t('ai.whyTag', { names })
  return t('ai.whyPlot')
}
</script>

<template>
  <AiCard v-model:open="open" :title="$t('ai.simTitle')" :summary="summary">
    <template #head>
      <button class="btn tiny ghost" :disabled="ai.similar.loading" @click="ai.loadSimilar">
        {{ $t('ai.simRefresh') }}
      </button>
    </template>

    <div v-if="ai.similar.items.length" class="asst-sim">
      <div v-for="s in ai.similar.items" :key="s.id" class="asst-sim-card">
        <button class="asst-sim-open" @click="ws.gotoMovie(s.id)">
          <img v-if="s.cover" :src="coverThumbUrl(s.id)" alt="" loading="lazy" />
          <span class="ellipsis">{{ s.code || s.title }}</span>
        </button>
        <div v-if="(s.reasons || []).length" class="asst-reasons">
          <span v-for="(r, i) in s.reasons" :key="i" class="asst-reason">{{ reasonText(r) }}</span>
        </div>
      </div>
    </div>
    <p v-else-if="ai.similar.loading" class="asst-hint">{{ $t('ai.simLoading') }}</p>
    <p v-else-if="!ai.similar.error" class="asst-hint">{{ $t('ai.simEmpty') }}</p>
    <p v-if="ai.similar.error" class="asst-err">{{ ai.similar.error }}</p>
  </AiCard>
</template>
