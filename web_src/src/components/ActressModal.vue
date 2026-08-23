<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { state } from '../state.js'
import {
  getActress, toggleActressFav, toggleActressFollow, coverThumbUrl, avatarUrl,
} from '../api.js'
import { toast, avatarFallback, fmtSize } from '../utils.js'
import MovieGrid from './MovieGrid.vue'
import Pager from './Pager.vue'

const props = defineProps({
  ident: { type: String, default: '' },
})
const emit = defineEmits(['close'])

const info = ref(null)
const movies = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 24
const loading = ref(false)
const sort = ref('date')
const showAvatar = ref(false)

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const avatarSrc = computed(() => {
  const i = info.value
  if (!i) return ''
  if (i.avatar) return avatarUrl(i.avatar)
  return i.sample_id ? coverThumbUrl(i.sample_id, 240) : ''
})
const measurements = computed(() => {
  const i = info.value
  if (!i) return ''
  const parts = []
  if (i.bust) parts.push(`B${i.bust}`)
  if (i.waist) parts.push(`W${i.waist}`)
  if (i.hip) parts.push(`H${i.hip}`)
  return parts.join(' / ')
})
const age = computed(() => {
  const b = info.value && info.value.birthday
  if (!b) return null
  const y = Number(b.slice(0, 4))
  if (!y) return null
  return Math.max(0, new Date().getFullYear() - y)
})
const aStats = computed(() => (info.value && info.value.stats) || {})

async function load() {
  if (!props.ident) return
  loading.value = true
  try {
    const r = await getActress(props.ident, page.value, pageSize, sort.value)
    info.value = r.info || {}
    movies.value = r.items || []
    total.value = Number(r.total) || movies.value.length
  } catch (e) {
    toast(e.message, 'err')
  } finally { loading.value = false }
}

const sortOptions = [
  { v: 'date', l: '按时间' },
  { v: 'rating', l: '按评分' },
  { v: 'favorite', l: '按收藏' },
  { v: 'watched', l: '按最近观看' },
  { v: 'recent', l: '按最近入库' },
]
function changeSort(v) { sort.value = v; page.value = 1; load() }

// 点影片：关闭弹框并打开该影片详情，视频在详情抽屉/播放器正常显示，不被弹框遮挡
function openMovie(id) {
  emit('close')
  state.currentId = id
}
function goPage(p) { page.value = p; load() }

async function fav() {
  try {
    const r = await toggleActressFav(info.value.id || props.ident)
    info.value.favorite = r && r.favorite != null ? r.favorite : (info.value.favorite ? 0 : 1)
  } catch (e) { toast(e.message, 'err') }
}
async function follow() {
  try {
    const r = await toggleActressFollow(info.value.id || props.ident)
    info.value.followed = r && r.followed != null ? r.followed : (info.value.followed ? 0 : 1)
  } catch (e) { toast(e.message, 'err') }
}

function onKey(e) { if (e.key === 'Escape') emit('close') }
watch(() => props.ident, () => { page.value = 1; sort.value = 'date'; load() })
onMounted(() => { load(); window.addEventListener('keydown', onKey) })
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="am-mask" @click.self="emit('close')">
    <div class="am">
      <header class="am-head">
        <b class="am-title ellipsis">{{ (info && (info.name || props.ident)) || props.ident }}</b>
        <div class="am-tools">
          <select class="am-sort" :value="sort" @change="changeSort($event.target.value)">
            <option v-for="o in sortOptions" :key="o.v" :value="o.v">{{ o.l }}</option>
          </select>
          <button class="am-close" @click="emit('close')">×</button>
        </div>
      </header>

      <div class="am-body">
        <!-- 档案 -->
        <div class="am-prof">
          <img class="am-av" :src="avatarSrc" alt="" @error="avatarFallback"
               :class="{ clickable: !!avatarSrc }" @click="showAvatar = !!avatarSrc" />
          <div class="am-main">
            <div class="am-name">
              {{ (info && (info.name || props.ident)) || props.ident }}
              <span v-if="info && info.alias" class="am-alias">{{ info.alias }}</span>
            </div>
            <div class="am-stats">
              <span><b class="tabular">{{ total }}</b> 作品</span>
              <span v-if="aStats.avg_rating"><b class="tabular">{{ Number(aStats.avg_rating).toFixed(1) }}</b> 均分</span>
              <span v-if="aStats.watched != null"><b class="tabular">{{ aStats.watched }}</b> 已看</span>
              <span v-if="aStats.size"><b class="tabular">{{ fmtSize(aStats.size) }}</b> 占用</span>
            </div>
            <div v-if="info && (info.birthday || info.height || measurements)" class="am-attr muted sm">
              <span v-if="info.birthday">生日：{{ info.birthday }}<template v-if="age != null">（{{ age }}岁）</template></span>
              <span v-if="info.height">身高：{{ info.height }}cm</span>
              <span v-if="measurements">{{ measurements }}</span>
              <span v-if="info.cup">罩杯：{{ info.cup }}</span>
            </div>
            <div v-if="info && (info.birthplace || info.hobby)" class="am-attr muted sm">
              <span v-if="info.birthplace">出生地：{{ info.birthplace }}</span>
              <span v-if="info.hobby">爱好：{{ info.hobby }}</span>
            </div>
            <p v-if="info && info.profile" class="am-prof-text">{{ info.profile }}</p>
            <div class="am-acts" v-if="info">
              <button class="btn tiny" :class="{ active: info.favorite }" @click="fav">{{ info.favorite ? '♥ 已收藏' : '♡ 收藏' }}</button>
              <button class="btn tiny" :class="{ active: info.followed }" @click="follow">{{ info.followed ? '已关注' : '关注' }}</button>
            </div>
          </div>
        </div>

        <!-- 作品 -->
        <div class="am-movies">
          <div class="am-sec-title">全部作品 <span class="count">{{ total }}</span></div>
          <MovieGrid :items="movies" :loading="loading"
                     empty-title="该女优暂无作品" empty-desc="刮削元数据后作品会自动关联。"
                     @open="openMovie" />
          <Pager :page="page" :page-count="pageCount" :total="total" @go="goPage" />
        </div>
      </div>
    </div>

    <!-- 头像大图 -->
    <div v-if="showAvatar" class="am-lb" @click.self="showAvatar = false">
      <button class="am-close am-lb-close" @click="showAvatar = false">×</button>
      <img :src="avatarSrc" alt="" @click.stop @error="avatarFallback" />
    </div>
  </div>
</template>

<style scoped>
.am-mask {
  position: fixed; inset: 0; z-index: 1100;
  background: rgba(0, 0, 0, .78);
  display: flex; align-items: center; justify-content: center;
  padding: var(--sp-4);
}
.am {
  width: min(1200px, 100%); height: min(90vh, 860px);
  background: var(--c-surface);
  border: 1px solid var(--c-line-strong);
  border-radius: var(--r-lg);
  display: flex; flex-direction: column; overflow: hidden;
  box-shadow: var(--sh-3);
}
.am-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: var(--sp-3) var(--sp-4);
  border-bottom: 1px solid var(--c-line);
}
.am-title { font-size: var(--fs-lg); }
.am-tools { display: flex; align-items: center; gap: var(--sp-2); }
.am-sort {
  background: var(--c-surface-2); color: var(--c-text);
  border: 1px solid var(--c-line); border-radius: 6px; padding: 4px 8px; font-size: var(--fs-sm);
}
.am-close {
  width: 34px; height: 34px; border: none; border-radius: 50%;
  background: var(--c-surface-2); color: var(--c-text);
  font-size: 20px; line-height: 1; cursor: pointer;
}
.am-close:hover { background: var(--c-line-strong); }

.am-body { display: flex; flex: 1; min-height: 0; }
.am-prof {
  width: 340px; flex: none; padding: var(--sp-5); overflow-y: auto;
  border-right: 1px solid var(--c-line);
}
.am-av {
  width: 120px; height: 120px; border-radius: 50%; object-fit: cover;
  background: var(--c-surface-2); border: 3px solid var(--c-line);
}
.am-av.clickable { cursor: zoom-in; }
.am-main { margin-top: var(--sp-3); }
.am-name { font-size: var(--fs-lg); font-weight: 700; display: flex; align-items: baseline; gap: var(--sp-2); }
.am-alias { color: var(--c-text-2); font-weight: 400; font-size: var(--fs-sm); }
.am-stats { display: flex; flex-wrap: wrap; gap: var(--sp-3); margin-top: var(--sp-2); color: var(--c-text-2); font-size: var(--fs-sm); }
.am-attr { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: var(--sp-2); }
.am-prof-text {
  margin-top: var(--sp-3); color: var(--c-text-2); font-size: var(--fs-sm);
  line-height: 1.7; white-space: pre-line; max-height: 20em; overflow-y: auto;
}
.am-acts { margin-top: var(--sp-3); display: flex; gap: var(--sp-2); }

.am-movies { flex: 1; min-width: 0; padding: var(--sp-4); overflow-y: auto; }
.am-sec-title { font-weight: 700; margin-bottom: var(--sp-3); }
.am-sec-title .count { color: var(--c-text-2); font-weight: 400; font-size: var(--fs-sm); margin-left: 6px; }

.am-lb {
  position: absolute; inset: 0; background: rgba(0, 0, 0, .85);
  display: flex; align-items: center; justify-content: center; padding: 40px;
}
.am-lb img { max-width: 90vw; max-height: 90vh; border-radius: 8px; background: var(--c-surface-2); }
.am-lb-close { position: absolute; top: 16px; right: 20px; }

@media (max-width: 760px) {
  .am-body { flex-direction: column; }
  .am-prof { width: auto; border-right: none; border-bottom: 1px solid var(--c-line); max-height: 40vh; }
}
</style>
