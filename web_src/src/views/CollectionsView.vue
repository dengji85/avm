<script setup>
import { ref, computed, onMounted } from 'vue'
import { state } from '../state.js'
import {
  listCollections, createCollection, updateCollection, deleteCollection,
  getCollection, removeFromCollection,
} from '../api.js'
import { coverThumbUrl as thumbUrl } from '../api.js'
import { toast, confirmDialog, coverFallback } from '../utils.js'
import { t } from '../i18n/index.js'
import MovieGrid from '../components/MovieGrid.vue'
import Pager from '../components/Pager.vue'

const list = ref([])
const loading = ref(false)
const current = ref(null)
const movies = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 60
const detailLoading = ref(false)

/* 新建弹窗 */
const showNew = ref(false)
const nkind = ref('manual')
const nname = ref('')
const rule = ref({ cond: '', min_rating: '', genre: '', actress: '', sort: 'rating_desc' })

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

async function loadList() {
  loading.value = true
  try {
    const r = await listCollections()
    list.value = (r && (r.items || r.collections)) || (Array.isArray(r) ? r : [])
  } catch (e) { toast(e.message, 'err') } finally { loading.value = false }
}

async function open(c) {
  current.value = c
  page.value = 1
  await loadMovies()
}

async function loadMovies() {
  if (!current.value) return
  detailLoading.value = true
  try {
    const r = await getCollection(current.value.id)
    movies.value = r.items || []
    total.value = Number(r.total) || movies.value.length
    if (r.name) current.value = Object.assign({}, current.value, r)
  } catch (e) { toast(e.message, 'err') } finally { detailLoading.value = false }
}

function back() { current.value = null; loadList() }

function openNew(kind) {
  nkind.value = kind
  nname.value = ''
  rule.value = { cond: '', min_rating: '', genre: '', actress: '', sort: 'rating_desc' }
  showNew.value = true
}

async function submitNew() {
  const name = nname.value.trim()
  if (!name) { toast(t('collections.nameEmpty'), 'err'); return }
  const body = { name }
  if (nkind.value === 'smart') {
    const params = {}
    if (rule.value.cond) params[rule.value.cond] = 1
    if (rule.value.min_rating) params.min_rating = Number(rule.value.min_rating)
    if (rule.value.genre.trim()) params.genre = rule.value.genre.trim()
    if (rule.value.actress.trim()) params.actress = rule.value.actress.trim()
    params.sort = rule.value.sort
    body.kind = 'smart'
    body.rule = { params }
  }
  try {
    const r = await createCollection(body)
    toast(t('collections.created'), 'ok')
    showNew.value = false
    await loadList()
    const created = list.value.find((c) => c.id === (r && r.id))
    if (created) open(created)
  } catch (e) { toast(e.message, 'err') }
}

const renaming = ref(false)
const renameText = ref('')
function startRename() { renameText.value = current.value.name; renaming.value = true }
async function submitRename() {
  const name = renameText.value.trim()
  if (!name) return
  try {
    await updateCollection(current.value.id, { name })
    current.value.name = name
    renaming.value = false
    toast(t('collections.renamed'), 'ok')
  } catch (e) { toast(e.message, 'err') }
}

async function remove(c) {
  if (!(await confirmDialog(t('collections.deleteTitle'), t('collections.deleteDesc', { name: c.name }), { danger: true }))) return
  try {
    await deleteCollection(c.id)
    toast(t('collections.deleted'), 'ok')
    if (current.value && current.value.id === c.id) current.value = null
    await loadList()
  } catch (e) { toast(e.message, 'err') }
}

async function removeMovie(id) {
  try {
    await removeFromCollection(current.value.id, id)
    movies.value = movies.value.filter((m) => m.id !== id)
    total.value = Math.max(0, total.value - 1)
    toast(t('collections.movedOut'), 'ok')
  } catch (e) { toast(e.message, 'err') }
}

function exportCsv() { window.open(`/api/collections/${current.value.id}/export/csv`) }
function openDetail(id) { state.currentId = id }

function ruleText(c) {
  const p = c.rule && c.rule.params
  if (!p) return ''
  const out = []
  if (p.favorite) out.push(t('collections.condFav'))
  if (p.watchlist) out.push(t('collections.condWatch'))
  if (p.watched) out.push(t('collections.condWatched'))
  if (p.unwatched) out.push(t('collections.condUnwatched'))
  if (p.min_rating) out.push(`≥${p.min_rating}★`)
  if (p.genre) out.push(p.genre)
  if (p.actress) out.push(p.actress)
  return out.join(' · ')
}

onMounted(loadList)
</script>

<template>
  <section class="view">
    <!-- 列表模式 -->
    <template v-if="!current">
      <div class="toolbar">
        <h1 class="tb-title">{{ $t('view.collections') }}</h1>
        <span class="tb-sub tabular" v-if="!loading">{{ $t('collections.count', { n: list.length }) }}</span>
        <span v-else class="spinner"></span>
        <div class="spacer"></div>
        <button class="btn tiny" @click="openNew('smart')">{{ $t('collections.newSmart') }}</button>
        <button class="btn tiny primary" @click="openNew('manual')">{{ $t('collections.newManual') }}</button>
      </div>

      <div class="view-body">
        <div v-if="!list.length && !loading" class="empty">
          <div class="icon">≡</div>
          <div class="title">{{ $t('collections.emptyTitle') }}</div>
          <div class="desc">{{ $t('collections.emptyDesc') }}</div>
          <div class="hstack">
            <button class="btn primary" @click="openNew('manual')">{{ $t('collections.createManual') }}</button>
            <button class="btn" @click="openNew('smart')">{{ $t('collections.createSmart') }}</button>
          </div>
        </div>

        <div v-else class="coll-grid">
          <article v-for="c in list" :key="c.id" class="coll-card" @click="open(c)">
            <div class="cc-covers">
              <img
                v-for="(cid, i) in (c.preview_ids || []).slice(0, 3)"
                :key="i" :src="thumbUrl(cid, 200)" alt="" @error="coverFallback"
              />
              <div v-if="!(c.preview_ids || []).length" class="cc-ph">≡</div>
            </div>
            <div class="cc-body">
              <div class="cc-name ellipsis">{{ c.name }}</div>
              <div class="cc-meta">
                <span class="badge" :class="c.kind === 'smart' ? 'accent' : ''">{{ c.kind === 'smart' ? $t('collections.smart') : $t('collections.manual') }}</span>
                <span class="tabular muted">{{ $t('collections.parts', { n: c.count || 0 }) }}</span>
              </div>
              <div v-if="c.kind === 'smart'" class="cc-rule ellipsis">{{ ruleText(c) }}</div>
            </div>
            <button class="cc-del" @click.stop="remove(c)" :data-tip="$t('common.delete')">✕</button>
          </article>
        </div>
      </div>
    </template>

    <!-- 详情模式 -->
    <template v-else>
      <div class="toolbar">
        <button class="btn tiny ghost" @click="back">‹ {{ $t('collections.back') }}</button>
        <h1 v-if="!renaming" class="tb-title">{{ current.name }}</h1>
        <input v-else class="rn" v-model="renameText" @keydown.enter="submitRename" @blur="submitRename" autofocus />
        <span class="badge" :class="current.kind === 'smart' ? 'accent' : ''">{{ current.kind === 'smart' ? $t('collections.smart') : $t('collections.manual') }}</span>
        <span class="tb-sub tabular">{{ $t('collections.parts', { n: total }) }}</span>
        <div class="spacer"></div>
        <button class="btn tiny ghost" @click="startRename">{{ $t('collections.rename') }}</button>
        <button class="btn tiny ghost" @click="exportCsv">{{ $t('collections.exportCsv') }}</button>
        <button class="btn tiny danger" @click="remove(current)">{{ $t('common.delete') }}</button>
      </div>

      <div v-if="current.kind === 'smart' && ruleText(current)" class="filter-bar">
        <span class="fb-label">{{ $t('collections.ruleLabel') }}</span>
        <span class="chip on">{{ ruleText(current) }}</span>
        <span class="muted sm">{{ $t('collections.autoHint') }}</span>
      </div>

      <div class="view-body">
        <MovieGrid
          :items="movies"
          :loading="detailLoading"
          :empty-title="$t('collections.emptyTitle')"
          empty-desc="{{ $t('collections.detailEmptyDesc') }}"
          @open="openDetail"
        />

        <div v-if="current.kind !== 'smart' && movies.length" class="rm-hint">
          <span class="muted sm">{{ $t('collections.moveOutHint') }}</span>
          <div class="chip-list">
            <button v-for="m in movies.slice(0, 40)" :key="m.id" class="chip" @click="removeMovie(m.id)">
              {{ m.code || m.title }} <span class="x">✕</span>
            </button>
          </div>
        </div>

        <Pager :page="page" :page-count="pageCount" :total="total" @go="(p) => { page = p; loadMovies() }" />
      </div>
    </template>

    <!-- 新建弹窗 -->
    <Teleport to="body">
      <div v-if="showNew" class="modal-mask" @click.self="showNew = false">
        <div class="modal">
          <div class="modal-head">{{ nkind === 'smart' ? $t('collections.createSmart') : $t('collections.createManual') }}</div>
          <div class="modal-body">
            <div class="field">
              <label>{{ $t('collections.nameLabel') }}</label>
              <input v-model="nname" :placeholder="$t('collections.namePlaceholder')" @keydown.enter="submitNew" />
            </div>

            <template v-if="nkind === 'smart'">
              <p class="muted sm">{{ $t('collections.ruleHint') }}</p>
              <div class="field">
                <label>{{ $t('collections.condLabel') }}</label>
                <select v-model="rule.cond">
                  <option value="">{{ $t('filter.all') }}</option>
                  <option value="unwatched">{{ $t('collections.condUnwatched') }}</option>
                  <option value="watched">{{ $t('collections.condWatched') }}</option>
                  <option value="favorite">{{ $t('collections.condFav') }}</option>
                  <option value="watchlist">{{ $t('collections.condWatch') }}</option>
                </select>
              </div>
              <div class="two">
                <div class="field">
                  <label>{{ $t('collections.minRating') }}</label>
                  <input v-model="rule.min_rating" type="number" min="0" max="5" step="0.5" :placeholder="$t('collections.minRatingPh')" />
                </div>
                <div class="field">
                  <label>{{ $t('collections.sortLabel') }}</label>
                  <select v-model="rule.sort">
                    <option value="rating_desc">{{ $t('sort.rating_desc') }}</option>
                    <option value="added_desc">{{ $t('sort.added_desc') }}</option>
                    <option value="play_desc">{{ $t('sort.play_desc') }}</option>
                    <option value="year_desc">{{ $t('sort.year_desc') }}</option>
                  </select>
                </div>
              </div>
              <div class="two">
                <div class="field"><label>{{ $t('collections.inGenre') }}</label><input v-model="rule.genre" :placeholder="$t('collections.inGenrePh')" /></div>
                <div class="field"><label>{{ $t('collections.inActress') }}</label><input v-model="rule.actress" /></div>
              </div>
            </template>
          </div>
          <div class="modal-foot">
            <button class="btn" @click="showNew = false">{{ $t('common.cancel') }}</button>
            <button class="btn primary" @click="submitNew">{{ $t('collections.create') }}</button>
          </div>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.coll-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(228px, 1fr));
  gap: var(--sp-4);
  align-content: start;
}
.coll-card {
  position: relative;
  border-radius: var(--r-md);
  background: var(--c-surface);
  border: 1px solid var(--c-line);
  overflow: hidden;
  cursor: pointer;
  transition: transform var(--t-base), border-color var(--t-base), box-shadow var(--t-base);
}
.coll-card:hover { transform: translateY(-3px); border-color: var(--c-line-strong); box-shadow: var(--sh-2); }

.cc-covers {
  display: grid; grid-template-columns: repeat(3, 1fr);
  gap: 2px; height: 104px;
  background: var(--c-surface-2);
}
.cc-covers img { width: 100%; height: 100%; object-fit: cover; }
.cc-ph { grid-column: 1 / -1; display: grid; place-items: center; font-size: 26px; color: var(--c-text-3); opacity: .4; }

.cc-body { padding: var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-1); }
.cc-name { font-size: var(--fs-lg); font-weight: 600; }
.cc-meta { display: flex; align-items: center; gap: var(--sp-2); font-size: var(--fs-sm); }
.cc-rule { font-size: var(--fs-xs); color: var(--c-text-3); }

.cc-del {
  position: absolute; top: var(--sp-2); right: var(--sp-2);
  width: 24px; height: 24px;
  display: grid; place-items: center;
  border-radius: 50%;
  background: rgba(8,10,15,.72);
  color: #fff; font-size: 11px;
  opacity: 0; transition: opacity var(--t-base), background var(--t-fast);
}
.coll-card:hover .cc-del { opacity: 1; }
.cc-del:hover { background: var(--c-err); }

.rn { width: 220px; height: 30px; font-size: var(--fs-lg); }
.sm { font-size: var(--fs-sm); }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }
.rm-hint { display: flex; flex-direction: column; gap: var(--sp-2); }
</style>
