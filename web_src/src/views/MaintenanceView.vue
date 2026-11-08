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
import SourceHealthTab from './maintenance/SourceHealthTab.vue'
import ImportTab from './maintenance/ImportTab.vue'

// 维护中心导航：左侧竖排固定列出全部入口，直接绑定 state.maintTab。
// 体检 = 只读诊断，工具 = 一次性动作；全部平铺可见，不折页签、不做下拉
const CHECK_TABS = [
  { id: 'overview', label: 'maint.overview', icon: '🏠' },
  { id: 'missing', label: 'maint.missingTab', icon: '⛔' },
  { id: 'storage', label: 'maint.storage', icon: '🩺' },
  { id: 'sources', label: 'maint.sourcesTab', icon: '🔗' },
  { id: 'failures', label: 'maint.failTitle', icon: '🔎' },
  { id: 'logs', label: 'maint.logs', icon: '📜' },
]
const TOOL_TABS = [
  { id: 'import', label: 'maint.importTab', icon: '📥' },
  { id: 'ad', label: 'maint.adTab', icon: '📢' },
  { id: 'subtitle', label: 'maint.subtitle', icon: '💬' },
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

    <!-- 左栏固定列出全部入口，右侧铺内容；窄屏下左栏转为横向滚动条 -->
    <div class="maint-shell">
      <aside class="maint-rail" role="tablist">
        <div class="rail-group">{{ $t('maint.groupCheck') }}</div>
        <button
          v-for="t in CHECK_TABS"
          :key="t.id"
          class="rail-item"
          :class="{ on: state.maintTab === t.id }"
          role="tab"
          @click="setTab(t.id)"
        >
          <span class="t-ico">{{ t.icon }}</span><span class="rail-label">{{ $t(t.label) }}</span>
        </button>
        <div class="rail-group">{{ $t('maint.groupTools') }}</div>
        <button
          v-for="t in TOOL_TABS"
          :key="t.id"
          class="rail-item"
          :class="{ on: state.maintTab === t.id }"
          role="tab"
          @click="setTab(t.id)"
        >
          <span class="t-ico">{{ t.icon }}</span><span class="rail-label">{{ $t(t.label) }}</span>
        </button>
      </aside>

      <div class="view-body tab-body">
        <OverviewTab v-show="state.maintTab === 'overview'" />
        <MissingTab v-show="state.maintTab === 'missing'" />
        <StorageHealthTab v-show="state.maintTab === 'storage'" />
        <SourceHealthTab v-show="state.maintTab === 'sources'" />
        <ImportTab v-show="state.maintTab === 'import'" />
        <AdCheckTab v-show="state.maintTab === 'ad'" />
        <SubtitleTab v-show="state.maintTab === 'subtitle'" />
        <ScrapeFailuresTab v-show="state.maintTab === 'failures'" />
        <ScrapeLogsTab v-show="state.maintTab === 'logs'" />
        <NfoExportTab v-show="state.maintTab === 'export'" />
      </div>
    </div>
  </section>
</template>

<style scoped>
.maint { display: flex; flex-direction: column; min-height: 0; }
.maint-shell { flex: 1; min-height: 0; display: flex; }
/* 左栏：入口全量可见，分组用小字标签；固定宽度，内容多时自身滚动 */
.maint-rail {
  flex: none; width: 152px; display: flex; flex-direction: column; gap: 2px;
  padding: 10px 8px;
  border-right: 1px solid var(--c-line);
  overflow-y: auto; scrollbar-gutter: stable;
}
.rail-group {
  margin: 10px 8px 4px; font-size: var(--fs-xs); color: var(--c-text-3);
  letter-spacing: .04em;
}
.rail-group:first-child { margin-top: 0; }
.rail-item {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 7px 9px; border: 0; border-radius: var(--r-sm);
  background: none; color: var(--c-text-2); font: inherit; font-size: var(--fs-sm);
  text-align: left; cursor: pointer;
  transition: background .12s, color .12s;
}
.rail-item:hover { background: var(--c-surface-2); color: var(--c-text); }
.rail-item.on {
  background: var(--c-primary-soft); color: var(--c-primary-h);
  font-weight: 600; box-shadow: inset 2px 0 0 var(--c-primary);
}
.rail-item .t-ico { width: 18px; text-align: center; flex: none; }
.rail-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 右侧内容列：保留最大宽度居中，切换与加载时不随内容伸缩闪烁 */
.tab-body {
  flex: 1 1 0; min-width: 0;
  max-width: 1080px; margin: 0 auto;
  scrollbar-gutter: stable;
  padding: var(--sp-5);
}
.hint { font-size: var(--fs-xs); color: var(--c-text-3); }
/* 窄屏：左栏转成横向滚动条，避免竖栏挤掉内容宽度 */
@media (max-width: 560px) {
  .maint-shell { flex-direction: column; }
  .maint-rail {
    width: 100%; flex-direction: row; gap: 2px;
    padding: 6px; overflow-x: auto; overflow-y: hidden;
    border-right: 0; border-bottom: 1px solid var(--c-line);
  }
  .rail-group { display: none; }
  .rail-item { width: auto; flex: none; }
}
</style>
