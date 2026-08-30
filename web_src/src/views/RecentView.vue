<script setup>
import { ref, computed, onMounted } from 'vue'
import { state, openPlayQueue } from '../state.js'
import { getRecentWatched } from '../api.js'
import { toast } from '../utils.js'
import { t } from '../i18n/index.js'
import PageHead from '../components/PageHead.vue'
import MovieGrid from '../components/MovieGrid.vue'
import Pager from '../components/Pager.vue'

const items = ref([])
const loading = ref(true)
const error = ref('')
const page = ref(1)
const total = ref(0)
const pageSize = 60

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await getRecentWatched({ page: page.value, page_size: pageSize })
    items.value = (r && r.items) || []
    total.value = Number((r && r.total) || items.value.length)
  } catch (e) {
    error.value = e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function goPage(p) { page.value = p; load() }

function openDetail(id) { state.currentId = id }

/* 连播最近观看：拉全量构建队列并打开全局播放器 */
async function playRecent() {
  const all = []
  let p = 1
  const size = 100
  try {
    while (true) {
      const r = await getRecentWatched({ page: p, page_size: size })
      const batch = (r && r.items) || []
      all.push(...batch)
      if (!batch.length || all.length >= (Number(r.total) || all.length)) break
      if (batch.length < size) break
      p += 1
    }
  } catch (e) { toast(e.message, 'err'); return false }
  if (!all.length) { toast(t('playlist.emptyNoMovies'), 'err'); return false }
  openPlayQueue(all, t('view.recent'), -1, 0, state.recentAutoPlay)
  return true
}

onMounted(async () => {
  load()
  // 进入即打开播放列表（迅雷式），可由设置关闭；无播放记录则留在网格
  if (state.recentOpenPlaylist) await playRecent()
})
</script>

<template>
  <section class="view">
    <div class="view-body recent">
      <PageHead :title="$t('view.recent')" :subtitle="$t('recent.sub')">
        <template #actions>
          <button v-if="items.length" class="btn primary" @click="playRecent">▶ {{ $t('recent.playAll') }}</button>
          <button class="btn ghost" @click="load">{{ $t('recent.refresh') }}</button>
        </template>
      </PageHead>

      <MovieGrid
        :items="items"
        :loading="loading"
        :empty-title="$t('recent.emptyTitle')"
        :empty-desc="$t('recent.emptyDesc')"
        :skeleton-count="12"
        @open="openDetail"
      />

      <Pager v-if="!loading && pageCount > 1" :page="page" :page-count="pageCount" :total="total" @go="goPage" />
    </div>
  </section>
</template>

<style scoped>
.recent { padding-bottom: 28px; }
</style>
