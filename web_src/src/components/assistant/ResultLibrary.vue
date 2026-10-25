<script setup>
/**
 * 问库卡：找片 / 口味 / 观看统计 / 库体检。
 *
 * 结果全部由本机 SQLite 执行，模型只参与「把问题解析成查询条件」；
 * 结构化结果直接渲染，不再让模型复述一遍——省钱，也不会说错数字。
 */
import { computed } from 'vue'
import { aiWorkspace as ws } from '../../composables/aiWorkspace.js'
import { t } from '../../i18n/index.js'
import AiCard from '../AiCard.vue'
import MovieCard from '../MovieCard.vue'

const chat = ws.chat
const ai = ws.ai
const lib = computed(() => chat.library)

const summary = computed(() => {
  if (lib.value.loading) return t('ai.working')
  if (lib.value.items.length) return t('ai.chat.foundN', { n: lib.value.items.length })
  if (lib.value.rows.length) return `${lib.value.rows.length}`
  return ''
})

/** 首次点「AI 解读」：说明会外发什么，确认后由 chat 重跑上一次查询 */
const canPolish = computed(() => !!lib.value.rows.length && lib.value.privacy === 'off')

function noteText(n) {
  if (!n) return ''
  if (n === 'taste') return t('ai.libNote_taste')
  if (n === 'watch_stats') return t('ai.libNote_watch_stats')
  if (n === 'health') return t('ai.libNote_health')
  if (n === 'recommend') return t('ai.libTasteNote')
  return n
}
function rowLabel(r) {
  return r.i18n ? t(r.i18n) : r.key
}
function rowVal(r) {
  if (r.value === 'ready') return t('aiCenter.extState.ready')
  if (r.value === 'missing') return t('aiCenter.extState.missing')
  return r.value
}
</script>

<template>
  <AiCard :title="$t('ai.chat.libCard')" :summary="summary">
    <p v-if="lib.loading" class="asst-hint">{{ $t('ai.chat.busyLibrary') }}</p>

    <!-- 模型对这句话的理解：让「它到底按什么搜的」可见，猜歪了用户能立刻发现 -->
    <p v-if="lib.plan && lib.plan.why" class="asst-hint">
      {{ $t('ai.libParsed') }}{{ lib.plan.why }}
    </p>

    <!-- 模型路径失败、退回本地规则时，必须把原因写出来，不能静默降级 -->
    <p v-if="lib.warning" class="asst-err">{{ $t('ai.chat.degraded') }}{{ lib.warning }}</p>

    <!-- 执行过程：跑的时候直接摊开（每完成一步立刻出现），跑完保持可见，不再折叠 -->
    <div v-if="lib.steps && lib.steps.length" class="asst-steps">
      <div v-for="(s, i) in lib.steps" :key="i" class="asst-step"
           :class="{ live: lib.streaming && i === lib.steps.length - 1 }">
        <span class="badge">{{ s.label || s.name }}</span>
        <span class="ellipsis">{{ s.summary }}</span>
        <span class="asst-sm">{{ s.ms }} ms</span>
      </div>
      <span v-if="lib.streaming" class="asst-hint">{{ $t('ai.chat.thinking') }}</span>
    </div>

    <p v-if="lib.note && !lib.loading && !lib.rows.length" class="asst-hint">{{ noteText(lib.note) }}</p>

    <!-- 流式结论：最后一个字后面跟着闪烁光标，直到输出结束 -->
    <div v-if="lib.answer" class="asst-answer">{{ lib.answer }}<span v-if="lib.streaming" class="asst-caret">▍</span></div>

    <div v-if="canPolish && !lib.loading" class="asst-actions">
      <button class="btn tiny ghost" :disabled="lib.polishing" @click="chat.enablePolish()">
        ✨ {{ lib.polishing ? $t('ai.working') : $t('ai.chat.polish') }}
      </button>
      <span class="asst-hint">{{ $t('ai.chat.polishHint') }}</span>
    </div>
    <p v-if="lib.polishError" class="asst-err">{{ $t('ai.chat.polishFail') }}{{ lib.polishError }}</p>

    <div v-if="lib.rows.length" class="asst-rows">
      <div v-for="r in lib.rows" :key="r.key" class="asst-row-line">
        <span class="asst-hint">{{ rowLabel(r) }}</span>
        <span class="spacer"></span>
        <span class="asst-row-val">{{ rowVal(r) }}<template v-if="r.top"> · {{ r.top }}</template></span>
      </div>
    </div>

    <div v-if="lib.items.length" class="asst-grid">
      <MovieCard v-for="m in lib.items" :key="m.id" :movie="m"
                 :selectable="false" :reason="m.__reason || ''" @open="ws.gotoMovie(m.id)" />
    </div>

    <p v-else-if="lib.done && !lib.loading && !lib.rows.length" class="asst-hint">
      {{ $t('ai.chat.none') }}
      <span v-if="!ai.ready">{{ $t('ai.chat.localHint') }}</span>
    </p>

    <p v-if="lib.error" class="asst-err">{{ lib.error }}</p>
  </AiCard>
</template>
