<script setup>
import { state } from '../state.js'
import PageHead from '../components/PageHead.vue'
import OverviewTab from './maintenance/OverviewTab.vue'
import StorageHealthTab from './maintenance/StorageHealthTab.vue'
import MissingTab from './maintenance/MissingTab.vue'
import ScrapeLogsTab from './maintenance/ScrapeLogsTab.vue'
import SubtitleTab from './maintenance/SubtitleTab.vue'
import ScrapeFailuresTab from './maintenance/ScrapeFailuresTab.vue'
import NfoExportTab from './maintenance/NfoExportTab.vue'
import AdCheckTab from './maintenance/AdCheckTab.vue'

// 维护中心子标签：直接绑定全局 state.maintTab，便于跨视图/重渲染保持位置
const TABS = [
  { id: 'overview', label: 'maint.overview', icon: '🏠' },
  { id: 'missing', label: 'maint.missingTab', icon: '⛔' },
  { id: 'storage', label: 'maint.storage', icon: '🩺' },
  { id: 'ad', label: 'maint.adTab', icon: '📢' },
  { id: 'subtitle', label: 'maint.subtitle', icon: '💬' },
  { id: 'failures', label: 'maint.failTitle', icon: '🔎' },
  { id: 'logs', label: 'maint.logs', icon: '📜' },
  { id: 'export', label: 'maint.export', icon: '⤓' },
]
const setTab = (id) => { state.maintTab = id }
</script>

<template>
  <section class="view maint">
    <PageHead :title="$t('view.maintenance')" :sub="$t('maint.sub')" icon="🛠️">
      <template #actions>
        <span class="hint">{{ $t('maint.hint') }}</span>
      </template>
    </PageHead>

    <div class="tabs tabbar" role="tablist">
      <button v-for="t in TABS" :key="t.id" class="tab" :class="{ on: state.maintTab === t.id }" role="tab" @click="setTab(t.id)">
        <span class="t-ico">{{ t.icon }}</span>{{ $t(t.label) }}
      </button>
    </div>

    <!-- v-show 保留组件实例，避免重复请求与切换闪烁；onMounted 内的 fetch 只跑一次 -->
    <div class="view-body tab-body">
      <OverviewTab v-show="state.maintTab === 'overview'" />
      <MissingTab v-show="state.maintTab === 'missing'" />
      <StorageHealthTab v-show="state.maintTab === 'storage'" />
      <AdCheckTab v-show="state.maintTab === 'ad'" />
      <SubtitleTab v-show="state.maintTab === 'subtitle'" />
      <ScrapeFailuresTab v-show="state.maintTab === 'failures'" />
      <ScrapeLogsTab v-show="state.maintTab === 'logs'" />
      <NfoExportTab v-show="state.maintTab === 'export'" />
    </div>
  </section>
</template>

<style scoped>
.maint { display: flex; flex-direction: column; min-height: 0; }
/* 页签过多时换行而不是被裁掉（窄屏下 7 个页签放不进一行） */
.tabs { flex-wrap: wrap; }
/* 固定列宽：七个页签共用一条稳定的内容列，切换与加载时不再随内容伸缩闪烁。
   内边距与其它视图保持一致（--sp-5），避免维护页看起来比别的页面"贴边" */
.tab-body {
  width: min(1080px, 100%);
  margin: 0 auto;
  scrollbar-gutter: stable;
  padding: var(--sp-5);
}
.hint { font-size: var(--fs-xs); color: var(--c-text-3); }
</style>
