<script setup>
/**
 * 调用详情：这次到底发了什么、模型原样回了什么。
 * 对话里不常看，但出问题时是唯一的证据，所以留在卡片底部、默认收起。
 */
import { computed } from 'vue'
import { aiWorkspace as ws } from '../../composables/aiWorkspace.js'

const props = defineProps({
  k: { type: String, required: true },
  promptId: { type: String, default: '' },
})

const ai = ws.ai
const open = computed(() => ai.traces.open === props.k)
const summary = computed(() => ai.traces.summary[props.k] || null)
const items = computed(() => (open.value ? ai.traceItems(props.k) : []))

function fmtTime(at) {
  if (!at) return ''
  try {
    return new Date(at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch (e) { return '' }
}
</script>

<template>
  <div class="asst-trace">
    <button class="asst-link" @click="ai.toggleTrace(k)">
      {{ open ? $t('ai.traceHide') : $t('ai.traceShow') }}
    </button>
    <button v-if="promptId" class="asst-link" @click="ws.openAiCenter('prompts')">
      {{ $t('ai.editPromptInCenter') }}
    </button>
    <span v-if="summary" class="asst-sm">{{ summary.model }} · {{ summary.ms }} ms</span>
  </div>

  <div v-if="open" class="asst-trace-box">
    <p v-if="ai.traces.loadingKey === k" class="asst-hint">{{ $t('ai.traceLoading') }}</p>
    <p v-else-if="ai.traces.error" class="asst-err">{{ ai.traces.error }}</p>
    <p v-else-if="!items.length" class="asst-hint">{{ $t('ai.traceEmpty') }}</p>
    <div v-for="tr in items" :key="tr.id" class="asst-trace-item">
      <div class="asst-trace-meta">
        <span class="badge">{{ tr.purpose }}</span>
        <span>{{ tr.model }}</span>
        <span>{{ tr.ms }} ms</span>
        <span v-if="tr.cached" class="badge ok">{{ $t('ai.traceCached') }}</span>
        <span class="badge">{{ $t('ai.traceOrigin.' + (tr.origin || 'internal')) }}</span>
        <span class="asst-sm">{{ fmtTime(tr.at) }}</span>
      </div>
      <div class="asst-hint">{{ $t('ai.traceSystem') }}</div>
      <pre class="asst-pre">{{ tr.messages[0] ? tr.messages[0].content : '' }}</pre>
      <div class="asst-hint">{{ $t('ai.traceUser') }}</div>
      <pre class="asst-pre">{{ tr.messages[1] ? tr.messages[1].content : '' }}</pre>
      <div class="asst-hint">{{ tr.error ? $t('ai.traceError') : $t('ai.traceReply') }}</div>
      <pre class="asst-pre" :class="{ err: tr.error }">{{ tr.error || tr.reply }}</pre>
    </div>
  </div>
</template>
