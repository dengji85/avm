<script setup>
import { onMounted, onBeforeUnmount, watch, ref, computed } from 'vue'
import { state, applyTheme } from './state.js'
import { getFacets, getConfig, getServerInfo, getSession, onNoToken, serverOffline } from './api.js'
import { toast, setSiteTitle } from './utils.js'
import { useTasks } from './composables/useTasks.js'

import TopNav from './components/TopNav.vue'
import MobileNav from './components/MobileNav.vue'
import ToastLayer from './components/ToastLayer.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import DetailDrawer from './components/DetailDrawer.vue'
import TokenGate from './components/TokenGate.vue'
import PlaylistPlayer from './components/PlaylistPlayer.vue'
import MiniPlayBar from './components/MiniPlayBar.vue'
import PrivacyLock from './components/PrivacyLock.vue'

const tokenGate = ref(null)

import HomeView from './views/HomeView.vue'
import GalleryView from './views/GalleryView.vue'
import ActressView from './views/ActressView.vue'
import ActressDetailView from './views/ActressDetailView.vue'
import CollectionsView from './views/CollectionsView.vue'
import RecentView from './views/RecentView.vue'
import RankingsView from './views/RankingsView.vue'
import SwipeView from './views/SwipeView.vue'
import StatsView from './views/StatsView.vue'
import MaintenanceView from './views/MaintenanceView.vue'
import SettingsView from './views/SettingsView.vue'
import YearReviewView from './views/YearReviewView.vue'
import OnboardingView from './views/OnboardingView.vue'

const VIEWS = {
  home: HomeView,
  gallery: GalleryView,
  actress: ActressView,
  actressDetail: ActressDetailView,
  collections: CollectionsView,
  recent: RecentView,
  rankings: RankingsView,
  swipe: SwipeView,
  stats: StatsView,
  maintenance: MaintenanceView,
  settings: SettingsView,
  yearReview: YearReviewView,
}

const tasks = useTasks()

/* 全局队列当前播放项（供迷你播放条显示） */
const currentQueueItem = computed(() => {
  const q = state.playQueue.queue || []
  return q[Math.min(state.playQueue.idx, q.length - 1)] || null
})

async function loadFacets() {
  try { state.facets = await getFacets() } catch (e) { /* 非致命 */ }
}

async function loadConfig() {
  try {
    state.config = await getConfig()
    // 首次启动引导：config.server.setup_done 未置真则弹出引导遮罩
    const done = !!(state.config && state.config.server && state.config.server.setup_done)
    state.onboarded = done
    applyTitleFromConfig()
  } catch (e) { /* 非致命 */ }
}

/* 浏览器标题 = 品牌名 + 自定义后缀（混淆用，见 docs/安全与隐私设计.md 的 L4） */
function applyTitleFromConfig() {
  setSiteTitle(state.config && state.config.privacy && state.config.privacy.site_subtitle)
}

/* 设置页改动隐私配置后：重新拉配置并刷新标题（遮挡层也监听同一事件） */
async function onPrivacyChanged() {
  await loadConfig()
}

/* 运行环境能力：容器 / NAS 下隐藏「用系统播放器」「在文件夹中显示」等无效入口 */
async function loadServerCaps() {
  try {
    const info = await getServerInfo()
    if (info && info.capabilities) state.capabilities = info.capabilities
  } catch (e) { /* 非致命 */ }
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

/* 后端离线 / 恢复提示 */
watch(serverOffline, (off, was) => {
  if (off && !was) toast('无法连接服务器，请确认后端服务已启动', 'err', 4000)
  else if (!off && was) toast('已重新连接服务器', 'ok', 2500)
})

/* 主动确认登录状态：未登录时直接弹登录框，避免首屏一堆 401 请求之后才提示 */
async function checkSession() {
  try {
    const s = await getSession()
    if (s && !s.authenticated && tokenGate.value) tokenGate.value.open()
  } catch (e) { /* 后端离线等场景忽略，后续请求 401 会兜底 */ }
}

onMounted(async () => {
  applyTheme()
  onNoToken(() => tokenGate.value && tokenGate.value.open())
  await Promise.all([loadFacets(), loadConfig(), loadServerCaps()])
  checkSession()
  tasks.start()
  window.addEventListener('avm-refresh', onGlobalRefresh)
  window.addEventListener('avm-privacy-changed', onPrivacyChanged)
})

onBeforeUnmount(() => {
  tasks.stop()
  window.removeEventListener('avm-refresh', onGlobalRefresh)
  window.removeEventListener('avm-privacy-changed', onPrivacyChanged)
})

/* 让子视图能触发分面刷新 */
function onFilterChange() { /* 由各视图自行响应 state 变化 */ }
</script>

<template>
  <div class="app">
    <!-- 后端离线常驻横幅：服务未启动 / 连接被拒时显示，恢复后自动消失 -->
    <div v-if="serverOffline" class="offline-banner" role="alert">
      ⚠ 无法连接服务器，请确认后端服务（avm）已启动，当前数据可能未刷新。
    </div>
    <TopNav />
    <MobileNav />

    <div class="app-main">
      <component :is="VIEWS[state.view] || VIEWS.home" />
    </div>

    <DetailDrawer />
    <ToastLayer />
    <ConfirmDialog />
    <TokenGate ref="tokenGate" />
    <PrivacyLock />

    <!-- 全局播放队列：全屏播放器（打开时）+ 迷你播放条（关闭但队列存在时） -->
    <PlaylistPlayer v-if="state.playQueue.open" />
    <MiniPlayBar
      v-if="!state.playQueue.open && state.playQueue.queue.length && currentQueueItem"
      :item="currentQueueItem"
    />

    <OnboardingView v-if="state.config && !state.onboarded" />
  </div>
</template>

<style scoped>
/* 后端离线常驻横幅：覆盖在顶部导航之上，恢复后由 v-if 自动移除 */
.offline-banner {
  position: fixed; top: 0; left: 0; right: 0; z-index: 900;
  background: #e5484d; color: #fff;
  text-align: center; padding: 9px 14px;
  font-size: 13px; font-weight: 600; line-height: 1.4;
  box-shadow: 0 2px 12px rgba(0,0,0,.35);
  animation: offbar-in .2s ease;
}
@keyframes offbar-in { from { transform: translateY(-100%); } to { transform: translateY(0); } }
</style>
