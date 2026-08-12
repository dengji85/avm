<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  x: { type: Number, default: 0 },
  y: { type: Number, default: 0 },
  items: { type: Array, default: () => [] },   // [{label, icon?, danger?, disabled?, action}]
  visible: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'select'])

// 防止菜单超出视口右/下边界
const style = ref({})
watch(() => [props.visible, props.x, props.y], () => {
  if (!props.visible) return
  const w = 200, h = props.items.length * 34 + 12
  const vw = window.innerWidth, vh = window.innerHeight
  let left = props.x, top = props.y
  if (left + w > vw - 8) left = Math.max(8, vw - w - 8)
  if (top + h > vh - 8) top = Math.max(8, vh - h - 8)
  style.value = { left: left + 'px', top: top + 'px' }
})

function onSelect(it) {
  if (it.disabled) return
  emit('select', it)
  emit('close')
}
</script>

<template>
  <transition name="ctx-fade">
    <div
      v-if="visible"
      class="ctx-menu"
      :style="style"
      @contextmenu.prevent
      @click.stop
    >
      <button
        v-for="(it, i) in items"
        :key="i"
        class="ctx-item"
        :class="{ danger: it.danger, disabled: it.disabled }"
        :disabled="it.disabled"
        @click="onSelect(it)"
      >
        <span v-if="it.icon" class="ci">{{ it.icon }}</span>
        <span class="cl">{{ $t(it.label) }}</span>
      </button>
    </div>
  </transition>
</template>

<style scoped>
.ctx-menu {
  position: fixed;
  z-index: 1000;
  min-width: 180px;
  padding: 6px;
  background: var(--c-surface-3, #1e232e);
  border: 1px solid var(--c-line-strong, #2a3040);
  border-radius: 10px;
  box-shadow: 0 8px 30px rgba(0,0,0,.45);
}
.ctx-item {
  display: flex; align-items: center; gap: 8px;
  width: 100%;
  border: 0; background: none; color: var(--c-text, #e8ebf0);
  font: inherit; font-size: 13px; text-align: left;
  padding: 7px 10px; border-radius: 7px; cursor: pointer;
}
.ctx-item:hover { background: var(--c-surface-2, #161a22); }
.ctx-item.danger { color: var(--c-err, #ff6b6b); }
.ctx-item.disabled { color: var(--c-text-3); opacity: .5; cursor: default; }
.ci { width: 16px; text-align: center; opacity: .85; }
.ctx-fade-enter-active, .ctx-fade-leave-active { transition: opacity .12s; }
.ctx-fade-enter-from, .ctx-fade-leave-to { opacity: 0; }
</style>
