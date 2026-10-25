<script setup>
/** 标签卡：一次给 3–8 个候选，已存在的默认不勾，确认后才写库。 */
import { computed, ref } from 'vue'
import { aiWorkspace as ws } from '../../composables/aiWorkspace.js'
import { t } from '../../i18n/index.js'
import AiCard from '../AiCard.vue'
import TraceBlock from './TraceBlock.vue'

const ai = ws.ai
const open = ref(true)
const count = computed(() => (ws.movie && ws.movie.tags ? ws.movie.tags.length : 0))
const picked = computed(() => ai.pickedTags.length)
</script>

<template>
  <AiCard v-model:open="open" :title="$t('ai.tagsTitle')" :summary="$t('ai.tagsCurrent', { n: count })">
    <template #head>
      <span v-if="ai.tags.loading" class="asst-sm">{{ $t('ai.working') }}</span>
      <button v-else class="btn tiny primary" @click="ai.suggestTags()">{{ $t('ai.tagsRun') }}</button>
    </template>

    <p v-if="ai.tags.loading && !ai.tags.items.length" class="asst-hint">{{ $t('ai.chat.busyTags') }}</p>

    <div v-if="ai.tags.items.length" class="asst-chips">
      <button
        v-for="it in ai.tags.items"
        :key="it.name"
        class="asst-chip"
        :class="{ on: it.picked, existing: it.existing }"
        @click="ai.toggleTag(it.name)"
      >
        <span v-if="it.picked">✓ </span>{{ it.name }}
        <em v-if="it.existing">{{ $t('ai.exists') }}</em>
      </button>
    </div>

    <p v-if="ai.tags.error" class="asst-err">{{ ai.tags.error }}</p>

    <div v-if="ai.tags.items.length" class="asst-actions">
      <button class="btn tiny ghost" @click="ai.pickAllTags(true)">{{ $t('ai.selectAll') }}</button>
      <button class="btn tiny ghost" @click="ai.pickAllTags(false)">{{ $t('ai.clearSel') }}</button>
      <button class="btn tiny" :disabled="!picked" @click="ai.applyTags">
        {{ $t('ai.tagsAdd', { n: picked }) }}
      </button>
    </div>

    <TraceBlock k="tags" prompt-id="tags" />
  </AiCard>
</template>
