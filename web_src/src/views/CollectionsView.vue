<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { state } from '../state.js'
import {
  listCollections, createCollection, updateCollection, deleteCollection,
  getCollection, removeFromCollection, getProfile,
} from '../api.js'
import { coverThumbUrl as thumbUrl } from '../api.js'
import MultiSelect from '../components/MultiSelect.vue'
import { toast, confirmDialog, coverFallback } from '../utils.js'
import { t } from '../i18n/index.js'
import MovieGrid from '../components/MovieGrid.vue'
import Pager from '../components/Pager.vue'
import PlaylistPlayer from '../components/PlaylistPlayer.vue'

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
const rule = ref({
  conds: [],            // 状态多选：favorite / watchlist / watched / unwatched
  genres: [],           // 类型多选
  actresses: [],        // 女优多选
  seriesArr: [],        // 系列单选（MultiSelect 返回数组，取首项）
  op: 'AND',            // 多值逻辑：AND=全部满足 / OR=任一满足
  min_rating: '',
  sort: 'rating_desc',
})

const condOptions = [
  { v: 'unwatched', t: t('collections.condUnwatched') },
  { v: 'watched', t: t('collections.condWatched') },
  { v: 'favorite', t: t('collections.condFav') },
  { v: 'watchlist', t: t('collections.condWatch') },
]

/* 智能片单下拉数据源（来自 /profile 高频词） */
const taxGenres = ref([])
const taxActresses = ref([])
const taxSeries = ref([])
async function loadTaxonomy() {
  try {
    const p = await getProfile()
    taxGenres.value = (p.genres || []).map((g) => ({ value: g.name, label: g.name, count: g.count }))
    taxActresses.value = (p.actresses || []).map((a) => ({ value: a.name, label: a.name, count: a.count }))
    taxSeries.value = (p.series || []).map((s) => ({ value: s.name, label: s.name, count: s.count }))
  } catch (e) { /* 下拉为空也能手填回退 */ }
}

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

/* 播放片单：分页拉取全量影片，构建连播队列 */
const playing = ref(false)
const playlist = ref([])
const playStart = ref(0)
async function playCollection() {
  if (!current.value) return
  const all = []
  let p = 1
  const size = 100
  try {
    while (true) {
      const r = await getCollection(current.value.id, { page: p, page_size: size })
      const items = (r && r.items) || []
      all.push(...items)
      if (!items.length || all.length >= (Number(r.total) || all.length)) break
      if (items.length < size) break
      p += 1
    }
  } catch (e) { toast(e.message, 'err'); return }
  if (!all.length) { toast(t('playlist.emptyNoMovies'), 'err'); return }
  // 从第一部未看完的可播影片开始（顺序模式更顺手）；没有则从 0
  let start = 0
  for (let i = 0; i < all.length; i++) {
    if (all[i].playable && !(all[i].progress_finished)) { start = i; break }
  }
  if (!all[start] || !all[start].playable) {
    const firstPlayable = all.findIndex((m) => m.playable)
    start = firstPlayable >= 0 ? firstPlayable : 0
  }
  // 优先沿用片单续播光标（上次看到第几部），前提它仍在队列且未看完
  const ph = resPlayhead
  if (ph) {
    const hi = all.findIndex((m) => m.id === ph)
    if (hi >= 0 && all[hi].playable && !all[hi].progress_finished) start = hi
  }
  playlist.value = all.map((m) => ({
    id: m.id, code: m.code || m.title, title: m.title, cover: m.cover,
    playable: !!m.playable, progress_seconds: m.progress_seconds || 0,
    duration_seconds: m.duration_seconds || 0,
  }))
  playStart.value = start
  playing.value = true
}

// 续播光标（详情接口返回），用于显示"继续播放"
const resPlayhead = ref(null)
// 直接从上次位置继续（不自动跳到第一部未看完）
async function resumeCollection() {
  if (!current.value) return
  const all = []
  let p = 1
  const size = 100
  try {
    while (true) {
      const r = await getCollection(current.value.id, { page: p, page_size: size })
      const items = (r && r.items) || []
      all.push(...items)
      if (!items.length || all.length >= (Number(r.total) || all.length)) break
      if (items.length < size) break
      p += 1
    }
  } catch (e) { toast(e.message, 'err'); return }
  if (!all.length) { toast(t('playlist.emptyNoMovies'), 'err'); return }
  const hi = all.findIndex((m) => m.id === resPlayhead.value)
  const start = hi >= 0 ? hi : 0
  playlist.value = all.map((m) => ({
    id: m.id, code: m.code || m.title, title: m.title, cover: m.cover,
    playable: !!m.playable, progress_seconds: m.progress_seconds || 0,
    duration_seconds: m.duration_seconds || 0,
  }))
  playStart.value = start
  playing.value = true
}

async function loadMovies() {
  if (!current.value) return
  detailLoading.value = true
  try {
    const r = await getCollection(current.value.id)
    movies.value = r.items || []
    total.value = Number(r.total) || movies.value.length
    resPlayhead.value = r.playhead || null
    if (r.name) current.value = Object.assign({}, current.value, r)
  } catch (e) { toast(e.message, 'err') } finally { detailLoading.value = false }
}

function back() { current.value = null; loadList() }

// 续播序号：playhead 指向的影片仍在列表且未看完
const resumeIndex = computed(() => {
  if (!resPlayhead.value || !movies.value.length) return -1
  const i = movies.value.findIndex((m) => m.id === resPlayhead.value)
  if (i < 0) return -1
  const m = movies.value[i]
  if (m && m.playable && !m.progress_finished) return i
  return -1
})

function openNew(kind) {
  nkind.value = kind
  nname.value = ''
  rule.value = { conds: [], genres: [], actresses: [], seriesArr: [], op: 'AND', min_rating: '', sort: 'rating_desc' }
  showNew.value = true
}

async function submitNew() {
  const name = nname.value.trim()
  if (!name) { toast(t('collections.nameEmpty'), 'err'); return }
  const body = { name }
  if (nkind.value === 'smart') {
    const params = {}
    // 状态多选：每个条件置 1
    for (const c of rule.value.conds) params[c] = 1
    if (rule.value.min_rating) params.min_rating = Number(rule.value.min_rating)
    if (rule.value.genres.length) params.genre = rule.value.genres
    if (rule.value.actresses.length) params.actress = rule.value.actresses
    if (rule.value.seriesArr.length) params.series = rule.value.seriesArr[0]
    if (rule.value.genres.length || rule.value.actresses.length) params.op = rule.value.op
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

/* 手动片单拖拽排序 */
const sorting = ref(false)
const sortList = ref([]) // [{ id, code }]
const dragIdx = ref(-1)
function openSort() {
  sortList.value = movies.value.map((m) => ({ id: m.id, code: m.code || m.title }))
  sorting.value = true
}
function moveSort(i, dir) {
  const j = i + dir
  if (j < 0 || j >= sortList.value.length) return
  const arr = sortList.value
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
  sortList.value = arr.slice()
}
function onDragStart(i) { dragIdx.value = i }
function onDrop(i) {
  const from = dragIdx.value
  if (from < 0 || from === i) return
  const arr = sortList.value
  const [it] = arr.splice(from, 1)
  arr.splice(i, 0, it)
  sortList.value = arr.slice()
  dragIdx.value = -1
}
async function submitSort() {
  if (!current.value || current.value.kind === 'smart') { sorting.value = false; return }
  try {
    await reorderCollection(current.value.id, sortList.value.map((m) => m.id))
    // 重排后按新顺序刷新
    const order = sortList.value.map((m) => m.id)
    movies.value = order.map((id) => movies.value.find((m) => m.id === id)).filter(Boolean)
    toast(t('collections.reordered'), 'ok')
  } catch (e) { toast(e.message, 'err') } finally { sorting.value = false }
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

function onPlaylistToast(e) {
  const d = e.detail || {}
  if (d.msg) toast(d.msg, d.kind || 'ok')
}
onMounted(() => { loadList(); loadTaxonomy(); window.addEventListener('avm-toast', onPlaylistToast) })
onBeforeUnmount(() => { window.removeEventListener('avm-toast', onPlaylistToast) })
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
                <span class="badge" :class="c.kind === 'system' ? 'sys' : (c.kind === 'smart' ? 'accent' : '')">{{ c.kind === 'system' ? $t('collections.system') : (c.kind === 'smart' ? $t('collections.smart') : $t('collections.manual')) }}</span>
                <span class="tabular muted">{{ $t('collections.parts', { n: c.count || 0 }) }}</span>
              </div>
              <div v-if="c.kind !== 'manual'" class="cc-rule ellipsis">{{ c.kind === 'system' ? (c.system_desc || ruleText(c)) : ruleText(c) }}</div>
            </div>
            <button v-if="c.kind !== 'system'" class="cc-del" @click.stop="remove(c)" :data-tip="$t('common.delete')">✕</button>
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
        <span class="badge" :class="current.kind === 'system' ? 'sys' : (current.kind === 'smart' ? 'accent' : '')">{{ current.kind === 'system' ? $t('collections.system') : (current.kind === 'smart' ? $t('collections.smart') : $t('collections.manual')) }}</span>
        <span class="tb-sub tabular">{{ $t('collections.parts', { n: total }) }}</span>
        <div class="spacer"></div>
        <button v-if="resumeIndex >= 0" class="btn tiny primary" @click="resumeCollection">↻ {{ $t('playlist.resume', { n: resumeIndex + 1, total }) }}</button>
        <button class="btn tiny primary" @click="playCollection">▶ {{ $t('playlist.play') }}</button>
        <button v-if="current.kind === 'manual'" class="btn tiny ghost" @click="openSort">⇅ {{ $t('collections.sort') }}</button>
        <button v-if="current.kind !== 'system'" class="btn tiny ghost" @click="startRename">{{ $t('collections.rename') }}</button>
        <button v-if="current.kind !== 'system'" class="btn tiny ghost" @click="exportCsv">{{ $t('collections.exportCsv') }}</button>
        <button v-if="current.kind !== 'system'" class="btn tiny danger" @click="remove(current)">{{ $t('common.delete') }}</button>
      </div>

      <div v-if="current.kind !== 'manual' && (ruleText(current) || current.system_desc)" class="filter-bar">
        <span class="fb-label">{{ $t('collections.ruleLabel') }}</span>
        <span class="chip on">{{ current.system_desc || ruleText(current) }}</span>
        <span class="muted sm">{{ $t('collections.autoHint') }}</span>
      </div>

      <div class="view-body">
        <MovieGrid
          :items="movies"
          :loading="detailLoading"
          :empty-title="$t('collections.emptyTitle')"
          :empty-desc="$t('collections.detailEmptyDesc')"
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

    <!-- 片单播放器 -->
    <PlaylistPlayer
      v-if="playing"
      :cid="current ? current.id : 0"
      :queue="playlist"
      :start-index="playStart"
      @close="playing = false"
    />

    <!-- 手动片单排序 -->
    <Teleport to="body">
      <div v-if="sorting" class="modal-mask" @click.self="sorting = false">
        <div class="modal sort-modal">
          <div class="modal-head">
            {{ $t('collections.sortTitle') }}
            <button class="modal-x" @click="sorting = false">✕</button>
          </div>
          <div class="modal-body">
            <div class="sort-list">
              <div
                v-for="(m, i) in sortList"
                :key="m.id"
                class="sort-row"
                :class="{ drag: dragIdx === i }"
                draggable="true"
                @dragstart="onDragStart(i)"
                @dragover.prevent
                @drop="onDrop(i)"
              >
                <span class="sort-idx tabular">{{ i + 1 }}</span>
                <span class="sort-code ellipsis">{{ m.code }}</span>
                <span class="sort-act">
                  <button class="ic" :disabled="i === 0" @click="moveSort(i, -1)">↑</button>
                  <button class="ic" :disabled="i === sortList.length - 1" @click="moveSort(i, 1)">↓</button>
                </span>
              </div>
            </div>
          </div>
          <div class="modal-foot">
            <button class="btn tiny ghost" @click="sorting = false">{{ $t('common.cancel') }}</button>
            <button class="btn tiny primary" @click="submitSort">{{ $t('common.save') }}</button>
          </div>
        </div>
      </div>
    </Teleport>

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
                <div class="chips">
                  <label v-for="c in condOptions" :key="c.v" class="chip" :class="{ on: rule.conds.includes(c.v) }">
                    <input type="checkbox" :value="c.v" v-model="rule.conds" /> {{ c.t }}
                  </label>
                </div>
              </div>
              <div class="two">
                <div class="field"><label>{{ $t('collections.inGenre') }}</label>
                  <MultiSelect v-model="rule.genres" :options="taxGenres" :placeholder="$t('collections.anyGenre')" :search-ph="$t('collections.searchPh')" :empty-label="$t('collections.noMatch')" :done-label="$t('common.apply')" />
                </div>
                <div class="field"><label>{{ $t('collections.inActress') }}</label>
                  <MultiSelect v-model="rule.actresses" :options="taxActresses" :placeholder="$t('collections.anyActress')" :search-ph="$t('collections.searchPh')" :empty-label="$t('collections.noMatch')" :done-label="$t('common.apply')" />
                </div>
              </div>
              <div class="two">
                <div class="field"><label>{{ $t('collections.inSeries') }}</label>
                  <MultiSelect v-model="rule.seriesArr" :options="taxSeries" :placeholder="$t('collections.anySeries')" :search-ph="$t('collections.searchPh')" :empty-label="$t('collections.noMatch')" :allow-empty="true" :done-label="$t('common.apply')" />
                </div>
                <div class="field"><label>{{ $t('collections.opLabel') }}</label>
                  <select v-model="rule.op">
                    <option value="AND">{{ $t('collections.opAnd') }}</option>
                    <option value="OR">{{ $t('collections.opOr') }}</option>
                  </select>
                </div>
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
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px;
  border: 1px solid var(--c-line); border-radius: 999px; cursor: pointer;
  font-size: var(--fs-sm); color: var(--c-text-2); user-select: none;
}
.chip input { display: none; }
.chip.on { background: var(--c-accent-soft, rgba(91,140,255,.16)); border-color: var(--c-accent, #5b8cff); color: var(--c-text); }

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

/* 手动片单排序 */
.sort-modal { width: min(440px, 94vw); max-height: 82vh; display: flex; flex-direction: column; }
.sort-list { display: flex; flex-direction: column; gap: 6px; max-height: 60vh; overflow-y: auto; }
.sort-row {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3); border-radius: var(--r-sm);
  background: var(--c-surface-2); border: 1px solid var(--c-line); cursor: grab;
}
.sort-row.drag { opacity: .5; border-color: var(--c-accent); }
.sort-idx { width: 26px; text-align: center; color: var(--c-text-3); }
.sort-code { flex: 1; min-width: 0; }
.sort-act { display: flex; gap: 4px; }
.sort-act .ic {
  width: 26px; height: 26px; border-radius: var(--r-sm); border: 1px solid var(--c-line);
  background: var(--c-surface); cursor: pointer; color: var(--c-text-2);
}
.sort-act .ic:disabled { opacity: .35; cursor: default; }
</style>
