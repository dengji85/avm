<script setup>
import { ref, onMounted } from 'vue'
import { state, openPlayQueue, NAV_ICONS } from '../state.js'
import { getContinueWatching, listMovies, playMovie, getSimilar } from '../api.js'
import { toast } from '../utils.js'
import MovieCard from '../components/MovieCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageHead from '../components/PageHead.vue'

/* 续看 */
const cw = ref([])
const cwLoading = ref(true)
const cwError = ref('')

/* 最近添加 */
const recent = ref([])
const recentLoading = ref(true)
const recentError = ref('')

/* 随机看看 */
const randomList = ref([])
const randomLoading = ref(true)
const randomError = ref('')

/* 猜你喜欢（基于最近观看做相似推荐） */
const similarList = ref([])
const similarLoading = ref(true)
const similarError = ref('')

/* 随机播放（一键开播一部随机影片） */
const shuffling = ref(false)
async function shufflePlay() {
  if (shuffling.value) return
  shuffling.value = true
  try {
    const r = await listMovies({ sort: 'random', page: 1, page_size: 1 })
    const pick = (r.items || [])[0]
    if (!pick) { toast('库里还没有影片', 'err'); return }
    await playMovie(pick.id)
    toast('已启动随机播放', 'ok')
  } catch (e) {
    toast(e.message || '随机播放失败', 'err')
  } finally {
    shuffling.value = false
  }
}

async function loadContinue() {
  cwLoading.value = true
  cwError.value = ''
  try {
    const r = await getContinueWatching()
    cw.value = Array.isArray(r) ? r : (r.items || [])
  } catch (e) {
    cwError.value = e.message || '加载失败'
  } finally {
    cwLoading.value = false
  }
}

async function loadRecent() {
  recentLoading.value = true
  recentError.value = ''
  try {
    const r = await listMovies({ sort: 'new', page: 1, page_size: 12 })
    recent.value = r.items || []
  } catch (e) {
    recentError.value = e.message || '加载失败'
  } finally {
    recentLoading.value = false
  }
}

async function loadRandom() {
  randomLoading.value = true
  randomError.value = ''
  try {
    const r = await listMovies({ sort: 'random', page: 1, page_size: 12 })
    randomList.value = r.items || []
  } catch (e) {
    randomError.value = e.message || '加载失败'
  } finally {
    randomLoading.value = false
  }
}

function shuffleRandom() {
  loadRandom()
}

/* 猜你喜欢：取续看里最新一部影片作种子，拉相似推荐 */
async function loadSimilar() {
  similarLoading.value = true
  similarError.value = ''
  try {
    const seed = cw.value[0]
    if (!seed) { similarList.value = []; return }
    const r = await getSimilar(seed.id)
    similarList.value = r.items || r || []
  } catch (e) {
    similarError.value = e.message || '加载失败'
  } finally {
    similarLoading.value = false
  }
}

function goGallery() {
  state.view = 'gallery'
}

/* 播放模式（迅雷式）：把某个区块的影片作为队列，打开全局播放器连播 */
function playQueue(movies) {
  openPlayQueue(movies || [], '')
}

onMounted(async () => {
  loadRecent()
  loadRandom()
  // 猜你喜欢依赖续看的第一部作为种子，须等续看加载完成
  await loadContinue()
  loadSimilar()
})
</script>

<template>
  <section class="view">
    <div class="view-body home tight">
      <PageHead
        :title="$t('view.home')"
        :subtitle="$t('home.subtitle')"
      >
        <template #actions>
          <button class="btn primary" :disabled="shuffling" @click="shufflePlay" :data-tip="$t('home.shuffleTip')">
            <svg v-if="!shuffling" class="btn-ico" viewBox="0 0 24 24" fill="currentColor"><path :d="NAV_ICONS.shuffle" /></svg>
            {{ shuffling ? '…' : $t('home.shufflePlay') }}
          </button>
          <button class="btn ghost" @click="goGallery">{{ $t('home.browseAll') }}</button>
        </template>
      </PageHead>

      <!-- 续看 rail -->
      <section class="block">
        <div class="block-head">
          <h2 class="block-title"><span class="dot"></span>{{ $t('home.continueWatching') }}</h2>
          <div class="block-actions">
            <button v-if="cw.length" class="link" @click="playQueue(cw)" :data-tip="$t('home.playBlockTip')">▶ {{ $t('home.playBlock') }}</button>
            <button v-if="cw.length" class="link" @click="goGallery">{{ $t('home.viewAll') }}</button>
          </div>
        </div>

        <div v-if="cwLoading" class="rail-skeleton">
          <div v-for="n in 4" :key="n" class="sk"></div>
        </div>
        <EmptyState
          v-else-if="cwError"
          icon="!"
          :title="cwError"
          action="重试"
          @action="loadContinue"
        />
        <EmptyState
          v-else-if="!cw.length"
          icon="▶"
          :title="$t('home.noContinue')"
          :desc="$t('home.continueDesc')"
        />
        <div v-else class="rail">
          <MovieCard v-for="m in cw" :key="m.id" :movie="m" :selectable="false" @open="(id) => { state.view = 'detail'; state.currentId = id }" />
        </div>
      </section>

      <!-- 最近添加 -->
      <section class="block">
        <div class="block-head">
          <h2 class="block-title"><span class="dot"></span>{{ $t('home.recentlyAdded') }}</h2>
          <div class="block-actions">
            <button v-if="recent.length" class="link" @click="playQueue(recent)" :data-tip="$t('home.playBlockTip')">▶ {{ $t('home.playBlock') }}</button>
            <button class="link" @click="goGallery">{{ $t('home.viewAll') }}</button>
          </div>
        </div>

        <div v-if="recentLoading" class="grid-skeleton">
          <div v-for="n in 8" :key="n" class="sk"></div>
        </div>
        <EmptyState
          v-else-if="recentError"
          icon="!"
          :title="recentError"
          action="重试"
          @action="loadRecent"
        />
        <EmptyState
          v-else-if="!recent.length"
          icon="▦"
          :title="$t('home.noFav')"
          :desc="$t('home.emptyDesc')"
        />
        <div v-else class="grid">
          <MovieCard v-for="m in recent" :key="m.id" :movie="m" :selectable="false" @open="(id) => { state.view = 'detail'; state.currentId = id }" />
        </div>
      </section>

      <!-- 猜你喜欢（相似推荐） -->
      <section class="block">
        <div class="block-head">
          <h2 class="block-title"><span class="dot"></span>{{ $t('home.forYou') }}</h2>
          <div class="block-actions">
            <button v-if="similarList.length" class="link" @click="playQueue(similarList)" :data-tip="$t('home.playBlockTip')">▶ {{ $t('home.playBlock') }}</button>
          </div>
        </div>

        <div v-if="similarLoading" class="grid-skeleton">
          <div v-for="n in 6" :key="n" class="sk"></div>
        </div>
        <EmptyState
          v-else-if="similarError"
          icon="!"
          :title="similarError"
          action="重试"
          @action="loadSimilar"
        />
        <EmptyState
          v-else-if="!similarList.length"
          icon="✦"
          :title="$t('home.noForYou')"
          :desc="$t('home.forYouEmpty')"
        />
        <div v-else class="grid">
          <MovieCard v-for="m in similarList" :key="m.id" :movie="m" :selectable="false" @open="(id) => { state.view = 'detail'; state.currentId = id }" />
        </div>
      </section>

      <!-- 随便看看（随机） -->
      <section class="block">
        <div class="block-head">
          <h2 class="block-title"><span class="dot"></span>{{ $t('home.randomTitle') }}</h2>
          <div class="block-actions">
            <button v-if="randomList.length" class="link" @click="playQueue(randomList)" :data-tip="$t('home.playBlockTip')">▶ {{ $t('home.playBlock') }}</button>
            <button class="link" @click="shuffleRandom">{{ $t('home.shuffle') }}</button>
          </div>
        </div>

        <div v-if="randomLoading" class="grid-skeleton">
          <div v-for="n in 8" :key="n" class="sk"></div>
        </div>
        <EmptyState
          v-else-if="randomError"
          icon="!"
          :title="randomError"
          action="重试"
          @action="loadRandom"
        />
        <EmptyState
          v-else-if="!randomList.length"
          icon="▦"
          :title="$t('home.noFav')"
          :desc="$t('home.emptyDesc')"
        />
        <div v-else class="grid">
          <MovieCard v-for="m in randomList" :key="m.id" :movie="m" :selectable="false" @open="(id) => { state.view = 'detail'; state.currentId = id }" />
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.home { padding-bottom: 28px; }
.block { margin-top: 0; }
.block-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 14px;
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

.rail {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: 168px;
  gap: 14px;
  overflow-x: auto;
  padding-bottom: 10px;
  scroll-snap-type: x proximity;
}
.rail > * { scroll-snap-align: start; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 16px;
}

.rail-skeleton, .grid-skeleton {
  display: grid; gap: 14px;
}
.rail-skeleton { grid-auto-flow: column; grid-auto-columns: 168px; }
.grid-skeleton { grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }
.sk {
  height: 230px; border-radius: var(--r-md);
  background: linear-gradient(100deg, var(--c-surface-2) 30%, var(--c-surface-3) 50%, var(--c-surface-2) 70%);
  background-size: 200% 100%;
  animation: shimmer 1.2s infinite;
}
.rail-skeleton .sk { height: 230px; }
@keyframes shimmer { to { background-position: -200% 0; } }
</style>
