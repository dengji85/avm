<script setup>
import { ref, computed, onMounted } from 'vue'
import { state, SORTS, FLAGS, resetFilters, hasActiveFilter } from '../state.js'
import { useLibrary } from '../composables/useLibrary.js'
import { getContinueWatching, clearContinueWatching } from '../api.js'
import { toast, confirmDialog } from '../utils.js'
import { useTasks } from '../composables/useTasks.js'

import MovieGrid from '../components/MovieGrid.vue'
import MovieCard from '../components/MovieCard.vue'
import Pager from '../components/Pager.vue'
import BulkBar from '../components/BulkBar.vue'

const { items, total, loading, pageCount, load, patchItem } = useLibrary()
const { runScan } = useTasks()

const cont = ref([])
const contLoading = ref(false)

const showContinue = computed(() =>
  !hasActiveFilter() && state.page === 1 && cont.value.length > 0,
)

async function loadContinue() {
  contLoading.value = true
  try { cont.value = (await getContinueWatching(12)) || [] }
  catch (e) { cont.value = [] }
  finally { contLoading.value = false }
}

async function dismissContinue() {
  if (!(await confirmDialog('清空继续观看', '将移除所有未看完记录，不影响影片本身。'))) return
  try { await clearContinueWatching(); cont.value = []; toast('已清空', 'ok') }
  catch (e) { toast(e.message, 'err') }
}

function openDetail(id) { state.currentId = id }

/* 筛选摘要（用于顶部筛选条） */
const activeChips = computed(() => {
  const out = []
  if (state.q) out.push({ k: 'q', label: `搜索：${state.q}` })
  state.actress.forEach((a) => out.push({ k: 'actress', v: a, label: `女优：${a}` }))
  state.genre.forEach((g) => out.push({ k: 'genre', v: g, label: `类型：${g}` }))
  if (state.studio) out.push({ k: 'studio', label: `厂商：${state.studio}` })
  if (state.series) out.push({ k: 'series', label: `系列：${state.series}` })
  if (state.prefix) out.push({ k: 'prefix', label: `前缀：${state.prefix}` })
  if (state.year) out.push({ k: 'year', label: `年份：${state.year}` })
  state.flags.forEach((f) => {
    const hit = FLAGS.find((x) => x[0] === f)
    out.push({ k: 'flag', v: f, label: hit ? hit[1] : f })
  })
  return out
})

function removeChip(c) {
  if (c.k === 'q') state.q = ''
  else if (c.k === 'actress') state.actress.splice(state.actress.indexOf(c.v), 1)
  else if (c.k === 'genre') state.genre.splice(state.genre.indexOf(c.v), 1)
  else if (c.k === 'flag') state.flags.splice(state.flags.indexOf(c.v), 1)
  else if (c.k === 'year') state.year = null
  else state[c.k] = ''
  state.page = 1
}

function toggleSelMode() {
  state.selMode = !state.selMode
  if (!state.selMode) state.selected = new Set()
}
function selectPage() {
  state.selMode = true
  state.selected = new Set(items.value.map((m) => m.id))
}

function goPage(p) {
  state.page = p
  document.querySelector('.view-body')?.scrollTo({ top: 0, behavior: 'smooth' })
}

function onBulkDone() {
  state.selected = new Set()
  state.selMode = false
  load()
}

onMounted(() => {
  load()
  loadContinue()
})
</script>

<template>
  <section class="view">
    <!-- 工具栏 -->
    <div class="toolbar">
      <h1 class="tb-title">影片库</h1>
      <span class="tb-sub tabular" v-if="!loading">{{ total }} 部</span>
      <span v-else class="spinner"></span>

      <div class="spacer"></div>

      <!-- 多条件逻辑 -->
      <div class="btn-group" v-if="state.actress.length > 1 || state.genre.length > 1">
        <button class="btn tiny" :class="{ active: state.multiOp === 'OR' }" @click="state.multiOp = 'OR'" data-tip="任一匹配">任一</button>
        <button class="btn tiny" :class="{ active: state.multiOp === 'AND' }" @click="state.multiOp = 'AND'" data-tip="全部匹配">全部</button>
      </div>

      <select class="sort-sel" v-model="state.sort">
        <option v-for="[v, t] in SORTS" :key="v" :value="v">{{ t }}</option>
      </select>

      <!-- 卡片尺寸 -->
      <div class="btn-group">
        <button class="btn tiny icon" :class="{ active: state.cardSize === 'dense' }" @click="state.cardSize = 'dense'" data-tip="小图">▪</button>
        <button class="btn tiny icon" :class="{ active: state.cardSize === 'normal' }" @click="state.cardSize = 'normal'" data-tip="中图">◼</button>
        <button class="btn tiny icon" :class="{ active: state.cardSize === 'large' }" @click="state.cardSize = 'large'" data-tip="大图">⬛</button>
      </div>

      <button class="btn tiny" :class="{ active: state.selMode }" @click="toggleSelMode">
        {{ state.selMode ? '退出多选' : '多选' }}
      </button>
      <button v-if="state.selMode" class="btn tiny" @click="selectPage">选中本页</button>

      <button class="btn tiny icon" @click="load()" data-tip="刷新">⟳</button>
    </div>

    <!-- 激活筛选条 -->
    <div v-if="activeChips.length" class="filter-bar">
      <span class="fb-label">筛选</span>
      <button v-for="(c, i) in activeChips" :key="i" class="chip on" @click="removeChip(c)">
        {{ c.label }} <span class="x">✕</span>
      </button>
      <button class="btn tiny ghost" @click="resetFilters">全部清除</button>
    </div>

    <!-- 内容 -->
    <div class="view-body">
      <!-- 继续观看 -->
      <section v-if="showContinue" class="cw">
        <div class="section-title">
          继续观看
          <span class="count">{{ cont.length }}</span>
          <div class="spacer"></div>
          <button class="btn tiny ghost" @click="dismissContinue">清空</button>
        </div>
        <div class="rail">
          <MovieCard
            v-for="m in cont"
            :key="'cw' + m.id"
            :movie="m"
            :selectable="false"
            @open="openDetail"
          />
        </div>
      </section>

      <section class="lib">
        <div class="section-title" v-if="showContinue">
          全部影片 <span class="count">{{ total }}</span>
        </div>

        <MovieGrid
          :items="items"
          :loading="loading"
          @open="openDetail"
          @changed="() => {}"
        >
          <template #empty-action>
            <button v-if="hasActiveFilter()" class="btn" @click="resetFilters">清除筛选条件</button>
            <button v-else class="btn primary" @click="runScan({})">扫描媒体库</button>
          </template>
        </MovieGrid>

        <Pager :page="state.page" :page-count="pageCount" :total="total" @go="goPage" />
      </section>
    </div>

    <BulkBar @done="onBulkDone" />
  </section>
</template>

<style scoped>
.sort-sel { width: auto; min-width: 116px; height: 28px; font-size: var(--fs-sm); }
.cw .rail { padding-bottom: var(--sp-3); }
</style>
