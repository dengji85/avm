<script setup>
import { ref, onMounted } from 'vue'
import { state } from '../state.js'
import { getContinueWatching, listMovies } from '../api.js'
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

function goGallery() {
  state.view = 'gallery'
}

onMounted(() => {
  loadContinue()
  loadRecent()
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
          <button class="btn ghost" @click="goGallery">{{ $t('home.browseAll') }}</button>
        </template>
      </PageHead>

      <!-- 续看 rail -->
      <section class="block">
        <div class="block-head">
          <h2 class="block-title"><span class="dot"></span>{{ $t('home.continueWatching') }}</h2>
          <button v-if="cw.length" class="link" @click="goGallery">{{ $t('home.viewAll') }}</button>
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
          <button class="link" @click="goGallery">{{ $t('home.viewAll') }}</button>
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
