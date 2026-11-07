<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { state, openPlayQueue, NAV_ICONS } from '../state.js'
import {
  listMovies, playMovie, getContinueWatching, getRecommend,
} from '../api.js'
import { toast, recommendReason } from '../utils.js'
import { t } from '../i18n/index.js'
import MovieRail from '../components/MovieRail.vue'
import PageHead from '../components/PageHead.vue'
import EmptyState from '../components/EmptyState.vue'
import GettingStarted from '../components/GettingStarted.vue'
import { checklistHidden, setChecklistHidden } from '../composables/useGuide.js'

/* 空库时用「起步清单」替代四条文字空态：进度由真实影片数推导，
   库里一有影片就自动消失，不打扰老用户（也可手动隐藏，从「? 上手指南」恢复）。 */
const libTotal = ref(0)
async function refreshLibTotal() {
  try {
    const r = await listMovies({ page: 1, page_size: 1 })
    libTotal.value = Number(r.total) || 0
  } catch (e) { /* 离线时保留上一次结果 */ }
}
const showStart = computed(() => !!state.config && !checklistHidden.value && libTotal.value === 0)
watch(() => state.task.scan.running, (running, was) => { if (was && !running) refreshLibTotal() })
onMounted(() => {
  refreshLibTotal()
  window.addEventListener('avm-refresh', refreshLibTotal)
})
onBeforeUnmount(() => window.removeEventListener('avm-refresh', refreshLibTotal))

/* ---------- 随机播放（一键开播一部随机影片） ---------- */
const shuffling = ref(false)
async function shufflePlay() {
  if (shuffling.value) return
  shuffling.value = true
  try {
    const r = await listMovies({ sort: 'random', page: 1, page_size: 1 })
    const pick = (r.items || [])[0]
    if (!pick) { toast(t('guide.scanEmpty'), 'err'); return }
    await playMovie(pick.id)
    toast(t('guide.randomOk'), 'ok')
  } catch (e) {
    toast(e.message || t('guide.randomFail'), 'err')
  } finally {
    shuffling.value = false
  }
}

/* ---------- 区块数据源 ----------
 * 统一为 (page, pageSize) => { items, total?, has_more? }，
 * 由 MovieRail 负责翻页展示与按需追加。
 * 一次性接口（继续观看 / 猜你喜欢）返回 has_more: false，只做翻页不追加。
 */
const RAIL_SIZE = 12

const cwItems = ref([])
const fetchContinue = async () => {
  const r = await getContinueWatching(30)
  return { items: Array.isArray(r) ? r : (r.items || []), has_more: false }
}

const recentItems = ref([])
const fetchRecentAdded = async (page, size) => {
  const r = await listMovies({ sort: 'added_desc', page, page_size: size })
  return { items: r.items || [], total: r.total || 0 }
}

const forYouItems = ref([])
const forYouKey = ref(0)
const fetchForYou = async () => {
  const r = await getRecommend({ limit: 24, exclude_watched: true })
  return { items: r.items || [], has_more: false }
}

/* 随机排序分页可能重复，前端按 id 去重，保证「一直加载」不出现重复卡 */
const randomSeen = new Set()
const randomItems = ref([])
const randomKey = ref(0)
const fetchRandom = async (page, size) => {
  const r = await listMovies({ sort: 'random', page, page_size: size })
  const items = (r.items || []).filter((m) => {
    if (randomSeen.has(m.id)) return false
    randomSeen.add(m.id)
    return true
  })
  return { items, total: r.total || 0 }
}
function shuffleRandom() {
  randomSeen.clear()
  randomKey.value++
}

/** 推荐理由文案 */
function whyOf(m) {
  return recommendReason(m.reasons, t)
}

function goGallery() {
  state.view = 'gallery'
}

function openDetail(id) {
  state.view = 'detail'
  state.currentId = id
}

/* 播放模式（迅雷式）：把某个区块已加载的影片作为队列，打开全局播放器连播 */
function playQueue(movies) {
  openPlayQueue(movies || [], '')
}
</script>

<template>
  <section class="view">
    <div class="view-body home tight">
      <PageHead
        :title="$t('view.home')"
        :subtitle="showStart ? $t('guide.welcomeSub') : $t('home.subtitle')"
      >
        <template #actions>
          <template v-if="!showStart">
            <button class="btn primary" :disabled="shuffling" @click="shufflePlay" :data-tip="$t('home.shuffleTip')">
              <svg v-if="!shuffling" class="btn-ico" viewBox="0 0 24 24" fill="currentColor"><path :d="NAV_ICONS.shuffle" /></svg>
              {{ shuffling ? '…' : $t('home.shufflePlay') }}
            </button>
            <button class="btn ghost" @click="goGallery">{{ $t('home.browseAll') }}</button>
          </template>
          <button v-else class="btn ghost" @click="setChecklistHidden(true)">{{ $t('guide.hide') }}</button>
        </template>
      </PageHead>

      <!-- 空库：起步清单（有结果后自动消失） -->
      <GettingStarted v-if="showStart" />

      <template v-else>
      <!-- 续看 -->
      <section class="block">
        <div class="block-head">
          <h2 class="block-title"><span class="dot"></span>{{ $t('home.continueWatching') }}</h2>
          <div class="block-actions">
            <button v-if="cwItems.length" class="link" @click="playQueue(cwItems)" :data-tip="$t('home.playBlockTip')">▶ {{ $t('home.playBlock') }}</button>
            <button v-if="cwItems.length" class="link" @click="goGallery">{{ $t('home.viewAll') }}</button>
          </div>
        </div>
        <MovieRail v-model:items="cwItems" :fetch-page="fetchContinue" :page-size="30" @open="openDetail">
          <template #empty>
            <EmptyState icon="▶" :title="$t('home.noContinue')" :desc="$t('home.continueDesc')" />
          </template>
        </MovieRail>
      </section>

      <!-- 猜你喜欢（口味加权推荐） -->
      <section class="block">
        <div class="block-head">
          <h2 class="block-title"><span class="dot"></span>{{ $t('home.forYou') }}</h2>
          <div class="block-actions">
            <button v-if="forYouItems.length" class="link" @click="playQueue(forYouItems)" :data-tip="$t('home.playBlockTip')">▶ {{ $t('home.playBlock') }}</button>
            <button class="link" @click="forYouKey++">{{ $t('home.shuffle') }}</button>
          </div>
        </div>
        <MovieRail
          v-model:items="forYouItems"
          :fetch-page="fetchForYou"
          :page-size="24"
          :reload-key="forYouKey"
          :reason-of="whyOf"
          @open="openDetail"
        >
          <template #empty>
            <EmptyState icon="✦" :title="$t('home.noForYou')" :desc="$t('home.forYouEmpty')" />
          </template>
        </MovieRail>
      </section>

      <!-- 随便看看（随机） -->
      <section class="block">
        <div class="block-head">
          <h2 class="block-title"><span class="dot"></span>{{ $t('home.randomTitle') }}</h2>
          <div class="block-actions">
            <button v-if="randomItems.length" class="link" @click="playQueue(randomItems)" :data-tip="$t('home.playBlockTip')">▶ {{ $t('home.playBlock') }}</button>
            <button class="link" @click="shuffleRandom">{{ $t('home.shuffle') }}</button>
          </div>
        </div>
        <MovieRail
          v-model:items="randomItems"
          :fetch-page="fetchRandom"
          :page-size="RAIL_SIZE"
          :reload-key="randomKey"
          @open="openDetail"
        >
          <template #empty>
            <EmptyState icon="▦" :title="$t('home.noFav')" :desc="$t('home.emptyDesc')" />
          </template>
        </MovieRail>
      </section>

      <!-- 最近添加 -->
      <section class="block">
        <div class="block-head">
          <h2 class="block-title"><span class="dot"></span>{{ $t('home.recentlyAdded') }}</h2>
          <div class="block-actions">
            <button v-if="recentItems.length" class="link" @click="playQueue(recentItems)" :data-tip="$t('home.playBlockTip')">▶ {{ $t('home.playBlock') }}</button>
            <button class="link" @click="goGallery">{{ $t('home.viewAll') }}</button>
          </div>
        </div>
        <MovieRail v-model:items="recentItems" :fetch-page="fetchRecentAdded" :page-size="RAIL_SIZE" @open="openDetail">
          <template #empty>
            <EmptyState icon="▦" :title="$t('home.noFav')" :desc="$t('home.emptyDesc')" />
          </template>
        </MovieRail>
      </section>
      </template>
    </div>
  </section>
</template>

<style scoped>
.home { padding-bottom: 28px; }
.block { margin-top: 26px; }
.block:first-of-type { margin-top: 4px; }
.block-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 6px; gap: 12px;
}
.block-title {
  display: flex; align-items: center; gap: 9px;
  font-size: 17px; font-weight: 700; margin: 0; color: var(--c-text);
}
.dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: linear-gradient(180deg, var(--c-primary), var(--c-primary-2));
  box-shadow: 0 0 10px rgba(79,140,255,.5);
}
.link {
  border: 0; background: none; color: var(--c-primary);
  font: inherit; font-weight: 600; cursor: pointer; padding: 4px 6px;
}
.link:hover { text-decoration: underline; }
.block-actions { display: flex; align-items: center; gap: 4px; }
.block-actions .link { color: var(--c-ok, #3fb950); }
.btn-ico { width: 16px; height: 16px; display: inline-block; vertical-align: -2px; margin-right: 6px; }

@media (max-width: 480px) {
  .block-title { font-size: 15px; }
  .block { margin-top: 20px; }
}
</style>
