<script setup>
/**
 * AI 工作台里的一张卡片：可折叠，收起时只留标题、状态与一句摘要。
 * 动作按钮放在 head 插槽，标题区整体可点，折叠状态由调用方持有
 * （这样切换影片、关闭再打开都能保持用户的习惯）。
 */
const props = defineProps({
  title: { type: String, default: '' },
  // 卡片右上角的状态（例如「AI · 字幕」「已自定义」）
  badge: { type: String, default: '' },
  // 收起后显示的一行摘要
  summary: { type: String, default: '' },
  open: { type: Boolean, default: true },
})
const emit = defineEmits(['update:open'])

function toggle() {
  emit('update:open', !props.open)
}
</script>

<template>
  <article class="aiw-card" :class="{ 'is-collapsed': !open }">
    <div class="aiw-card-head">
      <button class="aiw-caret" :aria-expanded="open" :title="title" @click="toggle">{{ open ? '▾' : '▸' }}</button>
      <b class="aiw-card-title" @click="toggle">{{ title }}</b>
      <span v-if="badge" class="badge">{{ badge }}</span>
      <span class="spacer"></span>
      <span v-if="summary" class="muted sm ellipsis aiw-card-summary">{{ summary }}</span>
      <slot name="head" />
    </div>
    <div v-show="open" class="aiw-card-body">
      <slot />
    </div>
  </article>
</template>

<style scoped>
.aiw-card {
  display: flex; flex-direction: column; gap: 8px;
  padding: 10px 12px; border-radius: var(--r-md);
  border: 1px solid var(--c-line); background: var(--c-surface-2);
}
.aiw-card.is-collapsed { gap: 0; padding: 8px 12px; }
.aiw-card-head { display: flex; align-items: center; gap: 8px; font-size: var(--fs-sm); }
.aiw-card-title { cursor: pointer; }
/* 摘要固定在右侧（收起时是唯一信息，展开时也不挤走操作按钮） */
.aiw-card-summary { flex: 0 1 auto; max-width: 45%; }
.aiw-caret {
  border: 0; background: transparent; color: var(--c-text-3); cursor: pointer;
  font-size: 11px; line-height: 1; padding: 2px 0; width: 14px;
}
.aiw-caret:hover { color: var(--c-text); }
.aiw-card-body { display: flex; flex-direction: column; gap: 8px; }
</style>
