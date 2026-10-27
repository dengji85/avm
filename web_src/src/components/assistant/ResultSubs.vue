<script setup>
/**
 * 字幕翻译卡：只摆选项，翻译由用户点按钮触发。
 * 翻译会落盘并消耗额度，不做「打开就自动跑」。
 */
import { computed, ref } from 'vue'
import { aiWorkspace as ws } from '../../composables/aiWorkspace.js'
import { t } from '../../i18n/index.js'
import AiCard from '../AiCard.vue'
import TraceBlock from './TraceBlock.vue'

const ai = ws.ai
const open = ref(true)
const summary = computed(() => {
  if (ai.aiSubs.length) return t('ai.subsTracks', { n: ai.aiSubs.length })
  return ai.hasSubtitles ? t('ai.subsTracks', { n: ai.subs.tracks.length }) : t('ai.subsNone')
})
</script>

<template>
  <AiCard v-model:open="open" :title="$t('ai.subsTitle')" :summary="summary">
    <div class="asst-row">
      <select v-model="ai.subs.target" class="inp tiny">
        <option value="zh-Hans">{{ $t('detail.aiTargetHans') }}</option>
        <option value="zh-Hant">{{ $t('detail.aiTargetHant') }}</option>
        <option value="en">{{ $t('detail.aiTargetEn') }}</option>
      </select>
      <label class="asst-check">
        <input v-model="ai.subs.bilingual" type="checkbox" />
        <span>{{ $t('detail.aiBilingual') }}</span>
      </label>
      <button class="btn tiny primary" :disabled="ai.subs.loading || !ai.hasSubtitles" @click="ai.translateSubs">
        {{ ai.subs.loading ? $t('ai.subsProgress', { p: ai.subs.percent }) : $t('detail.aiTranslate') }}
      </button>
      <button v-if="ai.subs.loading" class="btn tiny ghost" @click="ai.cancelTranslateSubs">
        {{ $t('common.cancel') }}
      </button>
    </div>

    <p v-if="!ai.hasSubtitles && !ai.probing" class="asst-hint">{{ $t('ai.subsNoSource') }}</p>

    <div v-if="ai.aiSubs.length" class="asst-list">
      <div v-for="s in ai.aiSubs" :key="s.id" class="asst-list-row">
        <span class="asst-mono">{{ s.lang || 'AI' }}</span>
        <span class="ellipsis dim">{{ s.filename }}</span>
        <button class="btn tiny ghost" @click="ai.downloadSub(s)">{{ $t('common.download') }}</button>
        <button class="btn tiny ghost" @click="ai.removeAiSub(s)">{{ $t('common.delete') }}</button>
      </div>
    </div>

    <p v-if="ai.subs.error" class="asst-err">{{ ai.subs.error }}</p>

    <TraceBlock k="subs" prompt-id="subtitle.translate" />
  </AiCard>
</template>
