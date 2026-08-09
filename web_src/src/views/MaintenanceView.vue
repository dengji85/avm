<script setup>
import { state } from '../state.js'
import PageHead from '../components/PageHead.vue'
import OverviewTab from './maintenance/OverviewTab.vue'
import StorageHealthTab from './maintenance/StorageHealthTab.vue'
import ScrapeLogsTab from './maintenance/ScrapeLogsTab.vue'

// 维护中心子标签：直接绑定全局 state.maintTab，便于跨视图/重渲染保持位置
const TABS = [
  { id: 'overview', label: '总览', icon: '🏠' },
  { id: 'storage', label: '存储·体检', icon: '🩺' },
  { id: 'logs', label: '刮削日志', icon: '📜' },
]
const setTab = (id) => { state.maintTab = id }
</script>

<template>
  <section class="view maint">
    <PageHead title="维护中心" sub="库体诊断 · 任务调度 · 数据治理" icon="🛠️">
      <template #actions>
        <span class="hint">所有写操作与诊断集中于此</span>
      </template>
    </PageHead>

    <div class="tabs tabbar" role="tablist">
      <button v-for="t in TABS" :key="t.id" class="tab" :class="{ on: state.maintTab === t.id }" role="tab" @click="setTab(t.id)">
        <span class="t-ico">{{ t.icon }}</span>{{ t.label }}
      </button>
    </div>

    <!-- v-show 保留组件实例，避免重复请求与切换闪烁；onMounted 内的 fetch 只跑一次 -->
    <div class="view-body tab-body">
      <OverviewTab v-show="state.maintTab === 'overview'" />
      <StorageHealthTab v-show="state.maintTab === 'storage'" />
      <ScrapeLogsTab v-show="state.maintTab === 'logs'" />
    </div>
  </section>
</template>

<style scoped>
.maint { display: flex; flex-direction: column; min-height: 0; }
.tab-body { padding: var(--sp-4) var(--sp-5); }
.hint { font-size: var(--fs-xs); color: var(--c-text-3); }
</style>
