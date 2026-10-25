<script setup>
/** 剧情问答卡：答案里带时间点，点一下直接跳到片中那一刻。 */
import { ref } from 'vue'
import { aiWorkspace as ws } from '../../composables/aiWorkspace.js'
import AiCard from '../AiCard.vue'
import TraceBlock from './TraceBlock.vue'

const ai = ws.ai
const open = ref(true)
</script>

<template>
  <AiCard v-model:open="open" :title="$t('ai.askTitle')">
    <template #head>
      <span v-if="ai.ask.track" class="asst-sm">{{ ai.ask.track }}</span>
    </template>

    <p v-if="ai.ask.loading" class="asst-hint">{{ $t('ai.chat.busyAsk') }}</p>

    <div v-if="ai.ask.answer" class="asst-answer">{{ ai.ask.answer }}</div>

    <div v-if="ai.ask.refs.length" class="asst-refs">
      <button v-for="(r, i) in ai.ask.refs" :key="i" class="asst-ref" :title="r.text" @click="ws.seekToRef(r.time)">
        [{{ r.time }}]
      </button>
      <span class="asst-hint">{{ ai.ask.mode === 'digest' ? $t('ai.refHintDigest') : $t('ai.refHint') }}</span>
    </div>

    <p v-if="ai.ask.error" class="asst-err">{{ ai.ask.error }}</p>

    <TraceBlock k="ask" prompt-id="plot.qa" />
  </AiCard>
</template>
