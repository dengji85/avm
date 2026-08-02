<script setup>
import { toasts } from '../utils.js'
</script>

<template>
  <div class="toast-layer">
    <TransitionGroup name="toast">
      <div v-for="t in toasts" :key="t.id" class="toast" :class="t.type">
        <span class="dot"></span>
        <span>{{ t.msg }}</span>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translateY(10px); }
.toast-enter-active, .toast-leave-active { transition: all var(--t-slow); }
.toast-leave-active { position: absolute; }
</style>
