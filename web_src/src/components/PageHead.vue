<script setup>
// 统一的页面标题区：标题 + 副信息 + 右侧操作槽
defineProps({
  title: { type: String, required: true },
  sub: { type: String, default: '' },
  icon: { type: String, default: '' },
})
</script>

<template>
  <header class="page-head">
    <div class="ph-main">
      <span v-if="icon" class="ph-ico">{{ icon }}</span>
      <h1 class="ph-title">{{ title }}</h1>
      <span v-if="sub" class="ph-sub tabular">{{ sub }}</span>
    </div>
    <div class="ph-actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<style scoped>
.page-head {
  flex: none;
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: 2px var(--sp-4);
  border-bottom: 1px solid var(--c-line);
  background: var(--c-bg);
  min-height: 36px;
}
.ph-main { display: flex; align-items: baseline; gap: var(--sp-2); min-width: 0; flex: 1; }
.ph-ico { font-size: var(--fs-lg); line-height: 1; }
.ph-title {
  font-size: var(--fs-lg);
  font-weight: 650;
  letter-spacing: -.01em;
  margin: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ph-sub { color: var(--c-text-3); font-size: var(--fs-sm); flex: none; }
.ph-actions { display: flex; align-items: center; gap: var(--sp-2); flex: none; flex-wrap: wrap; }
</style>
