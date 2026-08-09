<script setup>
// 统一空状态：图标 + 标题 + 描述 + 可选动作按钮
defineProps({
  icon: { type: String, default: '✦' },
  title: { type: String, default: '' },
  desc: { type: String, default: '' },
  action: { type: String, default: '' },
})
const emit = defineEmits(['action'])
</script>

<template>
  <div class="empty">
    <div class="icon">{{ icon }}</div>
    <div class="title" v-if="title">{{ title }}</div>
    <div class="desc" v-if="desc">{{ desc }}</div>
    <slot name="actions" />
    <button v-if="action" class="btn primary" @click="emit('action')">{{ action }}</button>
  </div>
</template>
