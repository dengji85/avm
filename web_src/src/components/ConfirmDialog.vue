<script setup>
import { confirmState, resolveConfirm } from '../utils.js'
</script>

<template>
  <Teleport to="body">
    <div v-if="confirmState.open" class="modal-mask confirm-mask" @click.self="resolveConfirm(false)" @keydown.esc="resolveConfirm(false)">
      <div class="modal confirm">
        <div class="modal-head">{{ confirmState.title }}</div>
        <div class="modal-body">
          <p v-if="confirmState.desc" class="muted desc">{{ confirmState.desc }}</p>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="resolveConfirm(false)">取消</button>
          <button
            class="btn"
            :class="confirmState.danger ? 'danger' : 'primary'"
            @click="resolveConfirm(true)"
            autofocus
          >{{ confirmState.okText }}</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 确认框可能从任意弹窗里弹出（AI 助手面板、投屏弹窗、标签管理…），
   单独占一层，保证永远压在它们上面，只低于 toast */
.confirm-mask { z-index: var(--z-confirm); }
.confirm { width: min(420px, 100%); }
.desc { white-space: pre-wrap; line-height: 1.7; }
</style>
