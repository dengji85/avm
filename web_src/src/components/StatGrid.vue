<script setup>
// 统一指标卡网格：props.items = [{label, value, tone, hint}]
defineProps({
  items: { type: Array, default: () => [] },
})
</script>

<template>
  <div class="stat-cards">
    <div v-for="s in items" :key="s.label" class="stat-card" :class="s.tone ? 'tone-' + s.tone : ''">
      <div class="stat-value tabular">{{ s.value }}</div>
      <div class="stat-label">{{ s.label }}</div>
    </div>
  </div>
</template>

<style scoped>
.stat-card.tone-ok    { border-color: var(--c-ok-line); }
.stat-card.tone-ok .stat-value { color: var(--c-ok); }
.stat-card.tone-warn  { border-color: var(--c-warn-line); }
.stat-card.tone-warn .stat-value { color: var(--c-warn); }
.stat-card.tone-err   { border-color: var(--c-err-line); }
.stat-card.tone-err .stat-value { color: var(--c-err); }
.stat-card.tone-accent .stat-value { color: var(--c-accent); }
</style>
