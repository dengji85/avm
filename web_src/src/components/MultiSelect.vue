<template>
  <div class="ms" ref="rootEl">
    <button type="button" class="ms-trigger" :class="{ active: open }" @click.stop="toggle">
      <span v-if="modelValue.length" class="ms-tags">
        <span v-for="v in modelValue" :key="v" class="ms-tag">
          {{ displayOf(v) }}
          <i class="ms-x" @click.stop="remove(v)">×</i>
        </span>
      </span>
      <span v-else class="ms-ph">{{ placeholder }}</span>
      <i class="ms-caret">▾</i>
    </button>

    <Teleport to="body">
      <div v-if="open" class="ms-mask" @click="close"></div>
      <div
        v-if="open"
        class="ms-pop"
        :class="{ sheet: isSheet }"
        :style="isSheet ? {} : { top: pos.top + 'px', left: pos.left + 'px', width: pos.w + 'px' }"
        @click.stop
      >
        <div class="ms-pop-head">
          <span class="ms-pop-title">{{ placeholder || searchPh }}</span>
          <button type="button" class="ms-done" @click="close">{{ doneLabel }}</button>
        </div>
        <input
          v-model="q"
          class="ms-search"
          :placeholder="searchPh"
          @keydown.esc.stop="close"
        />
        <div class="ms-list">
          <label v-if="allowEmpty" class="ms-opt ms-clear" @click="clear">
            <input type="checkbox" :checked="modelValue.length === 0" />
            <span>{{ clearLabel }}</span>
          </label>
          <label v-for="opt in filtered" :key="optKey(opt)" class="ms-opt" @click="toggleOpt(optValue(opt))">
            <input type="checkbox" :checked="selected.has(optValue(opt))" />
            <span>{{ optLabel(opt) }}</span>
            <em v-if="opt.count != null" class="ms-count">{{ opt.count }}</em>
          </label>
          <div v-if="!filtered.length" class="ms-empty">{{ emptyLabel }}</div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onBeforeUnmount } from 'vue'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  options: { type: Array, default: () => [] },   // [{value,label,count}] 或 [string]
  placeholder: { type: String, default: '' },
  searchPh: { type: String, default: '' },
  emptyLabel: { type: String, default: '无' },
  clearLabel: { type: String, default: '不限' },
  allowEmpty: { type: Boolean, default: true },
  doneLabel: { type: String, default: '完成' },
})
const emit = defineEmits(['update:modelValue'])

const open = ref(false)
const q = ref('')
const rootEl = ref(null)
const pos = ref({ top: 0, left: 0, w: 220 })
const isSheet = ref(false)

const selected = computed(() => new Set(props.modelValue))
const filtered = computed(() => {
  const k = q.value.trim().toLowerCase()
  if (!k) return props.options
  return props.options.filter((o) => optLabel(o).toLowerCase().includes(k))
})

function optValue(o) { return o && typeof o === 'object' ? o.value : o }
function optLabel(o) { return o && typeof o === 'object' ? (o.label ?? o.value) : o }
function optKey(o) { return optValue(o) }
function displayOf(v) {
  const o = props.options.find((x) => optValue(x) === v)
  return o ? optLabel(o) : v
}

function place() {
  const r = rootEl.value?.getBoundingClientRect()
  if (!r) return
  isSheet.value = window.innerWidth < 560
  if (isSheet.value) return  // 移动端用底部抽屉，无需计算坐标
  const w = Math.max(r.width, 220)
  let top = r.bottom + 4
  if (top + 300 > window.innerHeight - 8) top = Math.max(8, r.top - 4 - 300)
  let left = r.left
  if (left + w > window.innerWidth - 8) left = window.innerWidth - 8 - w
  if (left < 8) left = 8
  pos.value = { top, left, w }
}

function toggle() {
  if (open.value) { open.value = false; return }
  open.value = true; q.value = ''
  nextTick(place)
}
function close() { open.value = false }
function toggleOpt(v) {
  const s = new Set(props.modelValue)
  if (s.has(v)) s.delete(v); else s.add(v)
  emit('update:modelValue', [...s])
}
function remove(v) {
  emit('update:modelValue', props.modelValue.filter((x) => x !== v))
}
function clear() { emit('update:modelValue', []) }

function onReflow() {
  // 窗口尺寸变化导致布局改变时重定位；表单内滚动不关闭
  if (!open.value) return
  if (!isSheet.value) nextTick(place)
}
function onDocClick(e) {
  // 点击遮罩/外部关闭；点击选项已在各自 handler 内处理
  if (open.value && !e.target.closest('.ms-pop') && !e.target.closest('.ms-trigger')) open.value = false
}
window.addEventListener('resize', onReflow)
window.addEventListener('click', onDocClick)
onBeforeUnmount(() => {
  window.removeEventListener('resize', onReflow)
  window.removeEventListener('click', onDocClick)
})
</script>

<style scoped>
.ms { position: relative; width: 100%; }
.ms-trigger {
  width: 100%; min-height: 34px; display: flex; align-items: center; gap: 6px;
  padding: 4px 8px; border: 1px solid var(--c-line); border-radius: var(--r-sm);
  background: var(--c-surface-2); color: var(--c-text); cursor: pointer; text-align: left;
  font-size: var(--fs-sm);
}
.ms-trigger.active { border-color: var(--c-accent, #5b8cff); }
.ms-tags { display: flex; flex-wrap: wrap; gap: 4px; flex: 1; }
.ms-tag {
  display: inline-flex; align-items: center; gap: 3px; padding: 1px 6px;
  background: var(--c-accent-soft, rgba(91,140,255,.16)); border-radius: 999px; font-size: 12px;
}
.ms-x { cursor: pointer; opacity: .7; }
.ms-x:hover { opacity: 1; }
.ms-ph { color: var(--c-text-3); flex: 1; }
.ms-caret { margin-left: auto; opacity: .6; font-size: 10px; }
.ms-mask { position: fixed; inset: 0; z-index: 9998; background: rgba(0,0,0,.35); }
.ms-pop {
  position: fixed; z-index: 9999; background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--r-md); box-shadow: var(--shadow-pop, 0 8px 24px rgba(0,0,0,.35));
  overflow: hidden; display: flex; flex-direction: column;
}
.ms-pop-head { display: none; }
.ms-search {
  width: 100%; border: none; border-bottom: 1px solid var(--c-line); padding: 8px 10px;
  background: var(--c-surface-2); color: var(--c-text); outline: none; font-size: var(--fs-sm);
}
.ms-list { max-height: 260px; overflow-y: auto; padding: 4px; }
.ms-opt {
  display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: var(--r-sm);
  cursor: pointer; font-size: var(--fs-sm);
}
.ms-opt:hover { background: var(--c-surface-2); }
.ms-opt input { accent-color: var(--c-accent, #5b8cff); }
.ms-opt span { flex: 1; }
.ms-count { color: var(--c-text-3); font-size: 11px; font-style: normal; }
.ms-empty { padding: 10px; text-align: center; color: var(--c-text-3); font-size: var(--fs-sm); }
.ms-clear { color: var(--c-text-3); }

/* 移动端：底部抽屉式，避免定位错位、便于触屏滚动 */
.ms-pop.sheet {
  left: 0; right: 0; bottom: 0; top: auto; width: 100% !important;
  max-height: 70vh; border-radius: var(--r-lg) var(--r-lg) 0 0;
  animation: ms-up .18s ease-out;
}
.ms-pop.sheet .ms-pop-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 14px; border-bottom: 1px solid var(--c-line);
}
.ms-pop.sheet .ms-pop-title { font-weight: 600; font-size: var(--fs-md); }
.ms-pop.sheet .ms-done {
  border: none; background: var(--c-accent, #5b8cff); color: #fff;
  padding: 6px 14px; border-radius: 999px; font-size: var(--fs-sm); cursor: pointer;
}
.ms-pop.sheet .ms-list { max-height: calc(70vh - 104px); }
@keyframes ms-up { from { transform: translateY(20px); opacity: .6 } to { transform: none; opacity: 1 } }
</style>
