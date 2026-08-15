<script setup>
import { onMounted, onBeforeUnmount, watch, ref } from 'vue'
import { state, applyTheme } from './state.js'
import { getFacets, getConfig, onNoToken } from './api.js'
import { toast } from './utils.js'
import { useTasks } from './composables/useTasks.js'

import TopNav from './components/TopNav.vue'
import ToastLayer from './components/ToastLayer.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import DetailDrawer from './components/DetailDrawer.vue'
import TokenGate from './components/TokenGate.vue'

const tokenGate = ref(null)

import HomeView from './views/HomeView.vue'
import GalleryView from './views/GalleryView.vue'
import ActressView from './views/ActressView.vue'
import ActressDetailView from './views/ActressDetailView.vue'
import CollectionsView from './views/CollectionsView.vue'
import RankingsView from './views/RankingsView.vue'
import SwipeView from './views/SwipeView.vue'
import StatsView from './views/StatsView.vue'
import MaintenanceView from './views/MaintenanceView.vue'
import SettingsView from './views/SettingsView.vue'

const VIEWS = {
  home: HomeView,
  gallery: GalleryView,
  actress: ActressView,
  actressDetail: ActressDetailView,
  collections: CollectionsView,
  rankings: RankingsView,
  swipe: SwipeView,
  stats: StatsView,
  maintenance: MaintenanceView,
  settings: SettingsView,
}

const tasks = useTasks()

async function loadFacets() {
  try { state.facets = await getFacets() } catch (e) { /* 非致命 */ }
}

async function loadConfig() {
  try { state.config = await getConfig() } catch (e) { /* 非致命 */ }
}

function onGlobalRefresh() {
  loadFacets()
  window.dispatchEvent(new CustomEvent('avm-reload-view'))
}

/* 切换视图时回到顶部 */
watch(() => state.view, () => {
  const el = document.querySelector('.view-body')
  if (el) el.scrollTop = 0
})

onMounted(async () => {
  applyTheme()
  onNoToken(() => tokenGate.value && tokenGate.value.open())
  await Promise.all([loadFacets(), loadConfig()])
  tasks.start()
  window.addEventListener('avm-refresh', onGlobalRefresh)
})

onBeforeUnmount(() => {
  tasks.stop()
  window.removeEventListener('avm-refresh', onGlobalRefresh)
})

/* 让子视图能触发分面刷新 */
function onFilterChange() { /* 由各视图自行响应 state 变化 */ }
</script>

<template>
  <div class="app">
    <TopNav />

    <div class="app-main">
      <component :is="VIEWS[state.view] || VIEWS.home" />
    </div>

    <DetailDrawer />
    <ToastLayer />
    <ConfirmDialog />
    <TokenGate ref="tokenGate" />
  </div>
</template>
