<script setup>
import { computed } from 'vue'

const props = defineProps({
  page: { type: Number, required: true },
  pageCount: { type: Number, required: true },
  total: { type: Number, default: 0 },
})
const emit = defineEmits(['go'])

/** 生成页码序列（带省略号） */
const pages = computed(() => {
  const n = props.pageCount
  const c = props.page
  if (n <= 7) return Array.from({ length: n }, (_, i) => i + 1)
  const out = [1]
  const from = Math.max(2, c - 1)
  const to = Math.min(n - 1, c + 1)
  if (from > 2) out.push('…')
  for (let i = from; i <= to; i++) out.push(i)
  if (to < n - 1) out.push('…')
  out.push(n)
  return out
})

function go(p) {
  if (p === '…' || p === props.page || p < 1 || p > props.pageCount) return
  emit('go', p)
}
</script>

<template>
  <nav v-if="pageCount > 1" class="pager">
    <button class="btn tiny" :disabled="page <= 1" @click="go(1)">首页</button>
    <button class="btn tiny" :disabled="page <= 1" @click="go(page - 1)">‹</button>

    <button
      v-for="(p, i) in pages"
      :key="i"
      class="btn tiny pg"
      :class="{ active: p === page, dots: p === '…' }"
      :disabled="p === '…'"
      @click="go(p)"
    >{{ p }}</button>

    <button class="btn tiny" :disabled="page >= pageCount" @click="go(page + 1)">›</button>
    <button class="btn tiny" :disabled="page >= pageCount" @click="go(pageCount)">末页</button>

    <span class="pinfo">共 {{ total }} 部 · {{ pageCount }} 页</span>
  </nav>
</template>

<style scoped>
.pg { min-width: 28px; font-variant-numeric: tabular-nums; }
.pg.dots { border-color: transparent; background: none; opacity: .5; }
.pinfo { margin-left: var(--sp-3); }
</style>
