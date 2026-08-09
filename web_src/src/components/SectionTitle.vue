<script setup>
// 统一的区块标题：标题 + 数量 + 右侧操作槽
defineProps({
  title: { type: String, required: true },
  count: { type: [String, Number], default: '' },
})
</script>

<template>
  <div class="section-title">
    <span>{{ title }}</span>
    <span v-if="count !== ''" class="count tabular">({{ count }})</span>
    <div class="st-actions"><slot name="actions" /></div>
  </div>
</template>

<style scoped>
.section-title { display: flex; align-items: center; gap: var(--sp-2); }
.st-actions { margin-left: auto; display: flex; align-items: center; gap: var(--sp-2); }
</style>
