<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { state } from '../state.js'
import { listActresses, toggleActressFav, toggleActressFollow, avatarUrl } from '../api.js'
import { toast, avatarFallback, debounce, AVATAR_PLACEHOLDER } from '../utils.js'

const all = ref([])          // 后端一次性返回，前端做过滤/分页
const loading = ref(false)
const kw = ref('')
const sort = ref('count')
const onlyFav = ref(false)
const onlyFollow = ref(false)
const page = ref(1)
const pageSize = 60

/* 后端 /actresses 仅接受 q / sort / limit */
const SORTS = [
  ['count', '作品最多'],
  ['name', '名称 A-Z'],
  ['recent', '最近添加'],
  ['followed', '关注优先'],
]

async function load() {
  loading.value = true
  try {
    const r = await listActresses({ q: kw.value.trim() || undefined, sort: sort.value, limit: 1000 })
    all.value = (r && r.items) || []
  } catch (e) {
    toast(e.message, 'err')
    all.value = []
  } finally { loading.value = false }
}
const reload = debounce(load, 240)

/* 前端筛选 + 分页 */
const filtered = computed(() =>
  all.value.filter((a) => (!onlyFav.value || a.favorite) && (!onlyFollow.value || a.followed)),
)
const total = computed(() => filtered.value.length)
const items = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize))

watch([kw, sort], () => { page.value = 1; reload() })
watch([onlyFav, onlyFollow], () => { page.value = 1 })

/** 头像策略：优先本地/远程头像（统一走后端接口，兼容 URL 与本地文件名），无头像直接用内联占位 */
function avatarOf(a) {
  if (a.avatar) return avatarUrl(a.avatar)
  return AVATAR_PLACEHOLDER
}
function avatarErr(e) {
  const img = e && e.target ? e.target : e
  if (img && !img.dataset.fb) { img.dataset.fb = '1'; img.src = AVATAR_PLACEHOLDER }
}

function openActress(a) {
  state.actressCurrent = a.name
  state.actressCurrentId = a.id
  state.actressReturnView = 'actress'
  state.view = 'actressDetail'
}

async function fav(a, e) {
  e.stopPropagation()
  try {
    const r = await toggleActressFav(a.id || a.name)
    a.favorite = r && r.favorite != null ? r.favorite : !a.favorite
  } catch (err) { toast(err.message, 'err') }
}

async function follow(a, e) {
  e.stopPropagation()
  try {
    const r = await toggleActressFollow(a.id || a.name)
    a.followed = r && r.followed != null ? r.followed : !a.followed
    toast(a.followed ? '已关注' : '已取消关注', 'ok')
  } catch (err) { toast(err.message, 'err') }
}

function browse(a, e) {
  e.stopPropagation()
  state.actress = [a.name]
  state.genre = []
  state.q = ''
  state.page = 1
  state.view = 'gallery'
}

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

onMounted(load)
</script>

<template>
  <section class="view">
    <div class="toolbar">
      <h1 class="tb-title">女优墙</h1>
      <span class="tb-sub tabular" v-if="!loading">{{ total }} 位</span>
      <span v-else class="spinner"></span>

      <div class="spacer"></div>

      <input class="kw" v-model="kw" type="search" placeholder="搜索女优…" />
      <select class="sel" v-model="sort">
        <option v-for="[v, t] in SORTS" :key="v" :value="v">{{ t }}</option>
      </select>
      <button class="btn tiny" :class="{ active: onlyFav }" @click="onlyFav = !onlyFav">♥ 收藏</button>
      <button class="btn tiny" :class="{ active: onlyFollow }" @click="onlyFollow = !onlyFollow">关注</button>
    </div>

    <div class="view-body">
      <div v-if="loading && !items.length" class="wall">
        <div v-for="i in 24" :key="i" class="a-sk">
          <div class="skeleton av"></div>
          <div class="skeleton text" style="width: 70%"></div>
        </div>
      </div>

      <div v-else-if="!items.length" class="empty">
        <div class="icon">♀</div>
        <div class="title">没有找到女优</div>
        <div class="desc">刮削影片元数据后，女优信息会自动汇总到这里。</div>
      </div>

      <div v-else class="wall">
        <article v-for="a in items" :key="a.id || a.name" class="a-card" @click="openActress(a)">
          <div class="av-wrap">
            <img :src="avatarOf(a)" alt="" @error="avatarFallback" loading="lazy" />
            <button class="a-fav" :class="{ on: a.favorite }" @click="fav(a, $event)" :data-tip="a.favorite ? '取消收藏' : '收藏'">
              {{ a.favorite ? '♥' : '♡' }}
            </button>
            <span v-if="a.followed" class="a-follow">关注中</span>
          </div>
          <div class="a-name ellipsis">{{ a.name }}</div>
          <div class="a-sub">
            <span class="tabular">{{ a.count || 0 }} 部</span>
          </div>
          <div class="a-acts">
            <button class="btn tiny ghost" @click="browse(a, $event)">看作品</button>
            <button class="btn tiny ghost" @click="follow(a, $event)">{{ a.followed ? '已关注' : '关注' }}</button>
          </div>
        </article>
      </div>

      <nav v-if="pageCount > 1" class="pager">
        <button class="btn tiny" :disabled="page <= 1" @click="page--">‹ 上一页</button>
        <span class="pinfo">{{ page }} / {{ pageCount }}</span>
        <button class="btn tiny" :disabled="page >= pageCount" @click="page++">下一页 ›</button>
      </nav>
    </div>
  </section>
</template>

<style scoped>
.kw { width: 190px; height: 28px; }
.sel { width: auto; min-width: 110px; height: 28px; font-size: var(--fs-sm); }

.wall {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
  gap: var(--sp-4);
  align-content: start;
}

.a-card {
  display: flex; flex-direction: column; align-items: center;
  gap: 5px;
  padding: var(--sp-3) var(--sp-2);
  border-radius: var(--r-md);
  background: var(--c-surface);
  border: 1px solid transparent;
  cursor: pointer;
  transition: transform var(--t-base), border-color var(--t-base), box-shadow var(--t-base);
}
.a-card:hover { transform: translateY(-3px); border-color: var(--c-line-strong); box-shadow: var(--sh-2); }

.av-wrap { position: relative; width: 88px; height: 88px; }
.av-wrap img {
  width: 100%; height: 100%;
  border-radius: 50%;
  object-fit: cover;
  background: var(--c-surface-2);
  border: 2px solid var(--c-line);
  transition: border-color var(--t-base);
}
.a-card:hover .av-wrap img { border-color: var(--c-primary-line); }

.a-fav {
  position: absolute; right: -2px; bottom: 2px;
  width: 24px; height: 24px;
  display: grid; place-items: center;
  border-radius: 50%;
  background: var(--c-surface-3);
  border: 1px solid var(--c-line-strong);
  color: var(--c-text-3);
  font-size: 12px;
  transition: color var(--t-fast), transform var(--t-fast);
}
.a-fav.on { color: var(--c-primary); }
.a-fav:hover { transform: scale(1.12); }

.a-follow {
  position: absolute; left: 50%; top: -6px;
  transform: translateX(-50%);
  padding: 1px 6px;
  border-radius: var(--r-full);
  background: var(--c-primary);
  color: #fff; font-size: 10px; font-weight: 600;
  white-space: nowrap;
}

.a-name { font-size: var(--fs-md); font-weight: 500; max-width: 100%; text-align: center; }
.a-sub { display: flex; gap: var(--sp-2); font-size: var(--fs-xs); color: var(--c-text-3); }
.a-sub .rate { color: var(--c-gold); }
.a-acts { display: flex; gap: 4px; opacity: 0; transition: opacity var(--t-base); }
.a-card:hover .a-acts { opacity: 1; }

.a-sk { display: flex; flex-direction: column; align-items: center; gap: var(--sp-2); padding: var(--sp-3); }
.a-sk .av { width: 88px; height: 88px; border-radius: 50%; }
</style>
