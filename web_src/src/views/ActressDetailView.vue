<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { state } from '../state.js'
import { getActress, toggleActressFav, toggleActressFollow, updateActress, coverThumbUrl, avatarUrl } from '../api.js'
import { toast, avatarFallback, fmtSize } from '../utils.js'
import MovieGrid from '../components/MovieGrid.vue'
import Pager from '../components/Pager.vue'

const info = ref(null)
const movies = ref([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = 30
const editing = ref(false)
const draft = ref({})

const name = computed(() => state.actressCurrent)
const ident = computed(() => state.actressCurrentId || state.actressCurrent)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

/** 合作女优（后端返回 co_actresses） */
const coActresses = computed(() => (info.value && info.value.co_actresses) || [])
/** 统计信息 */
const aStats = computed(() => (info.value && info.value.stats) || {})

/** 头像：优先本地/远程头像（统一走后端接口），无头像时用样片封面兜底 */
const avatarSrc = computed(() => {
  const i = info.value
  if (!i) return ''
  if (i.avatar) return avatarUrl(i.avatar)
  return i.sample_id ? coverThumbUrl(i.sample_id, 240) : ''
})

async function load() {
  if (!ident.value) return
  loading.value = true
  try {
    const r = await getActress(ident.value, page.value, pageSize)
    info.value = r.info || {}
    movies.value = r.items || []
    total.value = Number(r.total) || movies.value.length
  } catch (e) {
    toast(e.message, 'err')
    back()
  } finally { loading.value = false }
}

function back() { state.view = state.actressReturnView || 'actress' }

function openDetail(id) { state.currentId = id }

/** 跳到合作女优 */
function goCo(n) {
  state.actressCurrent = n
  state.actressCurrentId = null
  page.value = 1
  load()
}

function browseAll() {
  state.actress = [name.value]
  state.genre = []
  state.q = ''
  state.page = 1
  state.view = 'gallery'
}

async function fav() {
  try {
    const r = await toggleActressFav(info.value.id || name.value)
    info.value.favorite = r && r.favorite != null ? r.favorite : (info.value.favorite ? 0 : 1)
  } catch (e) { toast(e.message, 'err') }
}

async function follow() {
  try {
    const r = await toggleActressFollow(info.value.id || name.value)
    info.value.followed = r && r.followed != null ? r.followed : (info.value.followed ? 0 : 1)
    toast(info.value.followed ? '已关注' : '已取消关注', 'ok')
  } catch (e) { toast(e.message, 'err') }
}

function startEdit() {
  draft.value = {
    alias: info.value.alias || '',
    avatar: info.value.avatar || '',
    birthday: info.value.birthday || '',
    note: info.value.note || '',
  }
  editing.value = true
}

async function save() {
  try {
    await updateActress(info.value.id, draft.value)
    Object.assign(info.value, draft.value)
    editing.value = false
    toast('已保存', 'ok')
  } catch (e) { toast(e.message, 'err') }
}

watch(name, () => { page.value = 1; load() })
watch(page, load)
onMounted(load)
</script>

<template>
  <section class="view">
    <div class="toolbar">
      <button class="btn tiny ghost" @click="back">‹ 返回</button>
      <h1 class="tb-title">{{ name }}</h1>
      <span class="tb-sub tabular" v-if="!loading">{{ total }} 部作品</span>
      <span v-else class="spinner"></span>
      <div class="spacer"></div>
      <button class="btn tiny" @click="browseAll">在影片库中筛选</button>
    </div>

    <div class="view-body">
      <!-- 档案卡 -->
      <div v-if="info" class="profile panel">
        <div class="pf-body">
          <img class="pf-av" :src="avatarSrc" alt="" @error="avatarFallback" />

          <div class="pf-main">
            <div v-if="!editing">
              <div class="pf-name">
                {{ info.name || name }}
                <span v-if="info.alias" class="pf-alias">{{ info.alias }}</span>
              </div>
              <div class="pf-stats">
                <div class="pf-stat"><b class="tabular">{{ total }}</b><span>作品</span></div>
                <div class="pf-stat" v-if="aStats.avg_rating"><b class="tabular">{{ Number(aStats.avg_rating).toFixed(1) }}</b><span>平均分</span></div>
                <div class="pf-stat" v-if="aStats.watched != null"><b class="tabular">{{ aStats.watched }}</b><span>已看</span></div>
                <div class="pf-stat" v-if="aStats.size"><b>{{ fmtSize(aStats.size) }}</b><span>占用</span></div>
              </div>
              <p v-if="info.birthday" class="muted sm">生日：{{ info.birthday }}</p>
              <p v-if="info.note" class="pf-note">{{ info.note }}</p>

              <div v-if="coActresses.length" class="co-wrap">
                <span class="muted sm">常合作：</span>
                <div class="chip-list">
                  <button v-for="c in coActresses" :key="c.name" class="chip" @click="goCo(c.name)">
                    {{ c.name }} <span class="dim">{{ c.count }}</span>
                  </button>
                </div>
              </div>
            </div>

            <div v-else class="edit-form">
              <div class="two">
                <div class="field"><label>别名</label><input v-model="draft.alias" /></div>
                <div class="field"><label>生日</label><input v-model="draft.birthday" placeholder="YYYY-MM-DD" /></div>
              </div>
              <div class="field"><label>头像 URL</label><input v-model="draft.avatar" /></div>
              <div class="field"><label>备注</label><textarea v-model="draft.note" rows="2"></textarea></div>
              <div class="hstack">
                <button class="btn primary tiny" @click="save">保存</button>
                <button class="btn ghost tiny" @click="editing = false">取消</button>
              </div>
            </div>
          </div>

          <div class="pf-acts" v-if="!editing">
            <button class="btn" :class="{ active: info.favorite }" @click="fav">{{ info.favorite ? '♥ 已收藏' : '♡ 收藏' }}</button>
            <button class="btn" :class="{ active: info.followed }" @click="follow">{{ info.followed ? '已关注' : '关注' }}</button>
            <button class="btn ghost" @click="startEdit">编辑资料</button>
          </div>
        </div>
      </div>

      <!-- 作品 -->
      <section>
        <div class="section-title">全部作品 <span class="count">{{ total }}</span></div>
        <MovieGrid
          :items="movies"
          :loading="loading"
          empty-title="该女优暂无作品"
          empty-desc="刮削元数据后作品会自动关联。"
          @open="openDetail"
        />
        <Pager :page="page" :page-count="pageCount" :total="total" @go="(p) => (page = p)" />
      </section>
    </div>
  </section>
</template>

<style scoped>
.profile { flex: none; }
.pf-body { display: flex; gap: var(--sp-5); padding: var(--sp-5); align-items: flex-start; }

.pf-av {
  width: 108px; height: 108px; flex: none;
  border-radius: 50%;
  object-fit: cover;
  background: var(--c-surface-2);
  border: 3px solid var(--c-line);
}
.pf-main { flex: 1; min-width: 0; }
.pf-name { font-size: var(--fs-2xl); font-weight: 650; letter-spacing: -.01em; }
.pf-alias { font-size: var(--fs-md); color: var(--c-text-3); font-weight: 400; margin-left: var(--sp-2); }

.pf-stats { display: flex; gap: var(--sp-6); margin: var(--sp-3) 0; }
.pf-stat { display: flex; flex-direction: column; }
.pf-stat b { font-size: var(--fs-xl); font-weight: 650; }
.pf-stat span { font-size: var(--fs-xs); color: var(--c-text-3); }

.sm { font-size: var(--fs-sm); }
.pf-note { margin-top: var(--sp-2); color: var(--c-text-2); font-size: var(--fs-md); line-height: 1.7; }

.pf-acts { display: flex; flex-direction: column; gap: var(--sp-2); flex: none; }

.co-wrap { margin-top: var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-2); }
.co-wrap .chip { height: 24px; font-size: var(--fs-xs); }

.edit-form { display: flex; flex-direction: column; gap: var(--sp-3); max-width: 520px; }
.edit-form .two { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }

@media (max-width: 760px) {
  .pf-body { flex-direction: column; align-items: stretch; }
  .pf-acts { flex-direction: row; }
}
</style>
