<script setup>
import { toasts } from '../utils.js'

function runAction(t) {
  if (t.action && typeof t.action.fn === 'function') t.action.fn()
  const i = toasts.findIndex((x) => x.id === t.id)
  if (i >= 0) toasts.splice(i, 1)
}
</script>

<template>
  <div class="toast-layer">
    <TransitionGroup name="toast">
      <div v-for="t in toasts" :key="t.id" class="toast" :class="t.type">
        <span class="dot"></span>
        <span class="toast-msg">{{ t.msg }}</span>
        <button v-if="t.action" class="toast-act" @click="runAction(t)">{{ t.action.label }}</button>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-act {
  margin-left: 8px; padding: 2px 10px; border-radius: 999px;
  border: 1px solid currentColor; background: transparent; color: inherit;
  font-size: var(--fs-sm); line-height: 1.6; cursor: pointer; white-space: nowrap;
}
.toast-act:hover { background: rgba(255, 255, 255, .12); }
</style>
