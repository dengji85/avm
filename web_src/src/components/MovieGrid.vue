<script setup>
import { computed } from 'vue'
import { state } from '../state.js'
import MovieCard from './MovieCard.vue'

const props = defineProps({
  items: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  emptyTitle: { type: String, default: '没有找到影片' },
  emptyDesc: { type: String, default: '试试调整筛选条件，或先扫描媒体库导入影片。' },
  skeletonCount: { type: Number, default: 18 },
  selectable: { type: Boolean, default: true },
})
const emit = defineEmits(['open', 'changed'])

const sizeClass = computed(() => (state.cardSize === 'normal' ? '' : state.cardSize))
</script>

<template>
  <!-- 首次加载：骨架 -->
  <div v-if="loading && !items.length" class="card-grid" :class="sizeClass">
    <div v-for="i in skeletonCount" :key="i" class="sk-card">
      <div class="skeleton poster"></div>
      <div class="skeleton text" style="width: 86%"></div>
      <div class="skeleton text" style="width: 54%"></div>
    </div>
  </div>

  <!-- 空态 -->
  <div v-else-if="!items.length" class="empty">
    <div class="icon">▦</div>
    <div class="title">{{ emptyTitle }}</div>
    <div class="desc">{{ emptyDesc }}</div>
    <slot name="empty-action" />
  </div>

  <!-- 列表 -->
  <div v-else class="card-grid" :class="[sizeClass, { 'pick-mode': state.selMode, busy: loading }]">
    <MovieCard
      v-for="m in items"
      :key="m.id"
      :movie="m"
      :selectable="selectable"
      @open="(id) => emit('open', id)"
      @changed="(id) => emit('changed', id)"
    />
  </div>
</template>

<style scoped>
.sk-card { display: flex; flex-direction: column; gap: 7px; }
.card-grid.busy { opacity: .55; transition: opacity var(--t-base); pointer-events: none; }
</style>
