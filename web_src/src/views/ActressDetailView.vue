<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { state } from '../state.js'
import { getActress, toggleActressFav, toggleActressFollow, updateActress, renameActress, mergeActress, fetchOneActressProfile, coverThumbUrl, avatarUrl } from '../api.js'
import { toast, avatarFallback, fmtSize, confirmDialog } from '../utils.js'
import MovieGrid from '../components/MovieGrid.vue'
import Pager from '../components/Pager.vue'

const info = ref(null)
const movies = ref([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = 30
const sort = ref('date')
const editing = ref(false)
const draft = ref({})

const name = computed(() => state.actressCurrent)
const ident = computed(() => state.actressCurrentId || state.actressCurrent)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

/** 合作女优（后端返回 co_actresses） */
const coActresses = computed(() => (info.value && info.value.co_actresses) || [])
/** 统计信息 */
const aStats = computed(() => (info.value && info.value.stats) || {})

/** 年龄（根据生日计算，无生日则 null） */
const age = computed(() => {
  const b = info.value && info.value.birthday
  if (!b) return null
  const m = b.match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (!m) return null
  const birth = new Date(+m[1], +m[2] - 1, +m[3])
  const now = new Date()
  let a = now.getFullYear() - birth.getFullYear()
  if (now.getMonth() < birth.getMonth() || (now.getMonth() === birth.getMonth() && now.getDate() < birth.getDate())) a--
  return a >= 0 ? a : null
})
/** 三围字符串（B/W/H） */
const measurements = computed(() => {
  const i = info.value
  if (!i) return ''
  const parts = []
  if (i.bust) parts.push(`B${i.bust}`)
  if (i.waist) parts.push(`W${i.waist}`)
  if (i.hip) parts.push(`H${i.hip}`)
  return parts.join(' ')
})

/** 头像：优先本地/远程头像（统一走后端接口），无头像时用样片封面兜底 */
const avatarSrc = computed(() => {
  const i = info.value
  if (!i) return ''
  if (i.avatar) return avatarUrl(i.avatar)
  return i.sample_id ? coverThumbUrl(i.sample_id, 240) : ''
})

/** 头像大图预览 */
const showAvatar = ref(false)
function openAvatar() { if (avatarSrc.value) showAvatar.value = true }
function closeAvatar() { showAvatar.value = false }

async function load() {
  if (!ident.value) return
  loading.value = true
  try {
    const r = await getActress(ident.value, page.value, pageSize, sort.value)
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

function changeSort(v) { sort.value = v; page.value = 1; load() }

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
    height: info.value.height || '',
    bust: info.value.bust || '',
    waist: info.value.waist || '',
    hip: info.value.hip || '',
    cup: info.value.cup || '',
    birthplace: info.value.birthplace || '',
    hobby: info.value.hobby || '',
    profile: info.value.profile || '',
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

/** 重命名女优 */
async function doRename() {
  const newName = window.prompt(`重命名「${info.value.name}」为：`, info.value.name)
  if (newName === null) return
  const n = (newName || '').trim()
  if (!n || n === info.value.name) return
  try {
    const r = await renameActress(info.value.id, n)
    toast(r && r.merged ? '已改名（与同名女优合并）' : '已重命名', 'ok')
    state.actressCurrent = n
    state.actressCurrentId = null
    await load()
  } catch (e) { toast(e.message, 'err') }
}

/** 合并女优：把另一个女优（source）合并到当前（target） */
async function doMerge() {
  const ok = await confirmDialog(
    '合并女优到本档案',
    `把另一个女优合并到「${info.value.name}」，其作品、收藏、关注、档案信息都会归并过来。确定继续？`,
    { danger: true },
  )
  if (!ok) return
  const srcName = window.prompt(`要合并进来的女优名（其档案将归并到「${info.value.name}」）：`, '')
  if (srcName === null) return
  const s = (srcName || '').trim()
  if (!s) return
  if (s === info.value.name) { toast('不能合并同名', 'err'); return }
  try {
    // 通过 name 查 source 的 id
    const list = await import('../api.js').then(({ listActresses }) =>
      listActresses({ q: s, sort: 'name', limit: 100 }))
    const arr = Array.isArray(list) ? list : ((list && list.items) || [])
    const found = arr.find((a) => a.name === s)
    if (!found) { toast('未找到该女优', 'err'); return }
    const r = await mergeActress(info.value.id, found.id)
    toast(`已合并，共 ${r.total_movies} 部作品`, 'ok')
    await load()
  } catch (e) { toast(e.message, 'err') }
}

/** 抓取资料：用已启用的资料插件（如 JavBus 女优资料）补全当前女优档案 */
const fetchingProfile = ref(false)
async function doFetchProfile() {
  fetchingProfile.value = true
  try {
    const r = await fetchOneActressProfile(info.value.id)
    if (r.changed) {
      toast('已补全女优资料（身高/三围/生日等）', 'ok')
      await load()
    } else {
      toast('无可补全的资料（字段已存在或插件未返回）', 'info')
    }
  } catch (e) { toast(e.message, 'err') } finally { fetchingProfile.value = false }
}

watch(ident, () => { page.value = 1; load() })
watch(page, load)
function onKey(e) { if (e.key === 'Escape') closeAvatar() }
onMounted(() => { load(); window.addEventListener('keydown', onKey) })
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <section class="view">
    <div class="toolbar">
      <button class="btn tiny ghost" @click="back">‹ 返回</button>
      <h1 class="tb-title">{{ name }}</h1>
      <span class="tb-sub tabular" v-if="!loading">{{ total }} 部作品</span>
      <span v-else class="spinner"></span>
      <div class="spacer"></div>
      <select class="sort-sel" :value="sort" @change="changeSort($event.target.value)">
        <option value="date">按时间</option>
        <option value="rating">按评分</option>
        <option value="favorite">按收藏</option>
        <option value="watched">按最近观看</option>
        <option value="recent">按最近入库</option>
      </select>
      <button class="btn tiny" @click="browseAll">{{ $t('actress.filterInGallery') }}</button>
    </div>

    <div class="view-body">
      <!-- 档案卡 -->
      <div v-if="info" class="profile panel">
        <div class="pf-body">
          <img class="pf-av" :src="avatarSrc" alt="" @error="avatarFallback"
               :class="{ clickable: !!avatarSrc }" @click="openAvatar" />

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
              <div v-if="info.birthday || info.height || measurements" class="pf-attr muted sm">
                <span v-if="info.birthday">生日：{{ info.birthday }}<template v-if="age != null">（{{ age }}岁）</template></span>
                <span v-if="info.height">身高：{{ info.height }}cm</span>
                <span v-if="measurements">{{ measurements }}</span>
                <span v-if="info.cup">罩杯：{{ info.cup }}</span>
              </div>
              <div v-if="info.birthplace || info.hobby" class="pf-attr muted sm">
                <span v-if="info.birthplace">出生地：{{ info.birthplace }}</span>
                <span v-if="info.hobby">爱好：{{ info.hobby }}</span>
              </div>
              <p v-if="info.profile" class="pf-note pf-profile">{{ info.profile }}</p>
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
              <div class="two">
                <div class="field"><label>身高 (cm)</label><input v-model="draft.height" /></div>
                <div class="field"><label>罩杯</label><input v-model="draft.cup" placeholder="如 C" /></div>
              </div>
              <div class="three">
                <div class="field"><label>胸围</label><input v-model="draft.bust" placeholder="cm" /></div>
                <div class="field"><label>腰围</label><input v-model="draft.waist" placeholder="cm" /></div>
                <div class="field"><label>臀围</label><input v-model="draft.hip" placeholder="cm" /></div>
              </div>
              <div class="two">
                <div class="field"><label>出生地</label><input v-model="draft.birthplace" placeholder="如 神奈川县" /></div>
                <div class="field"><label>爱好</label><input v-model="draft.hobby" placeholder="如 料理、旅行" /></div>
              </div>
              <div class="field"><label>简介</label><textarea v-model="draft.profile" rows="3"></textarea></div>
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
            <button class="btn ghost" @click="doRename">重命名</button>
            <button class="btn ghost danger" @click="doMerge">合并女优</button>
            <button class="btn ghost" :disabled="fetchingProfile" @click="doFetchProfile">
              {{ fetchingProfile ? '抓取中…' : '抓取资料' }}
            </button>
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

    <!-- 头像大图预览 -->
    <div v-if="showAvatar" class="avatar-lightbox" @click.self="closeAvatar">
      <button class="al-close" @click="closeAvatar">×</button>
      <img :src="avatarSrc" alt="" @click.stop @error="avatarFallback" />
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
.pf-av.clickable { cursor: zoom-in; transition: transform .12s ease; }
.pf-av.clickable:hover { transform: scale(1.05); }
.pf-main { flex: 1; min-width: 0; }
.pf-name { font-size: var(--fs-2xl); font-weight: 650; letter-spacing: -.01em; }
.pf-alias { font-size: var(--fs-md); color: var(--c-text-3); font-weight: 400; margin-left: var(--sp-2); }

.pf-stats { display: flex; gap: var(--sp-6); margin: var(--sp-3) 0; }
.pf-stat { display: flex; flex-direction: column; }
.pf-stat b { font-size: var(--fs-xl); font-weight: 650; }
.pf-stat span { font-size: var(--fs-xs); color: var(--c-text-3); }

.sm { font-size: var(--fs-sm); }
.pf-attr { display: flex; flex-wrap: wrap; gap: var(--sp-3); margin-top: var(--sp-1); }
.pf-note { margin-top: var(--sp-2); color: var(--c-text-2); font-size: var(--fs-md); line-height: 1.7; }
.pf-profile { white-space: pre-line; max-height: 42em; overflow-y: auto; }

.pf-acts { display: flex; flex-direction: column; gap: var(--sp-2); flex: none; }
.pf-acts .danger { color: var(--c-danger, #f26d6d); border-color: color-mix(in srgb, var(--c-danger, #f26d6d) 40%, transparent); }

.edit-form .three { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--sp-3); }

.co-wrap { margin-top: var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-2); }
.co-wrap .chip { height: 24px; font-size: var(--fs-xs); }

.edit-form { display: flex; flex-direction: column; gap: var(--sp-3); max-width: 520px; }
.edit-form .two { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }

@media (max-width: 760px) {
  .pf-body { flex-direction: column; align-items: stretch; }
  .pf-acts { flex-direction: row; }
}

.avatar-lightbox {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(0, 0, 0, .85);
  display: flex; align-items: center; justify-content: center;
  padding: 40px;
}
.avatar-lightbox img {
  max-width: 90vw; max-height: 90vh;
  border-radius: 8px;
  box-shadow: 0 10px 40px rgba(0, 0, 0, .6);
  background: var(--c-surface-2);
  cursor: zoom-out;
}
.al-close {
  position: absolute; top: 16px; right: 20px;
  width: 44px; height: 44px;
  border: none; border-radius: 50%;
  background: rgba(255, 255, 255, .15); color: #fff;
  font-size: 26px; line-height: 1; cursor: pointer;
}
.al-close:hover { background: rgba(255, 255, 255, .3); }
</style>
