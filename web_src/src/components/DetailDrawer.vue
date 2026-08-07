<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { state } from '../state.js'
import {
  getMovie, updateMovie, deleteMovie, toggleFlag, playMovie,
  exportNfo, getPreviews, getSimilar, coverUrl, coverThumbUrl, uploadCover, clearCover,
  listCollections, addToCollection,
  aiGenerateSynopsis, aiSuggestTags, aiStatus,
} from '../api.js'
import {
  toast, confirmDialog, copyText, coverFallback, fmtSize, fmtDuration,
  fmtDate, fmtAgo, qualityTag,
} from '../utils.js'
import { useTasks } from '../composables/useTasks.js'

const { runScrape } = useTasks()
import VideoPlayer from './VideoPlayer.vue'

const mv = ref(null)
const loading = ref(false)
const tab = ref('info')
const playing = ref(false)
const previews = ref([])
const pvLoading = ref(false)
const similar = ref([])
const editing = ref(false)
const draft = ref({})
const lightbox = ref('')
const collList = ref([])
const showColl = ref(false)
const aiReady = ref(false)
const aiBusy = ref(false)

async function checkAi() {
  try { const r = await aiStatus(); aiReady.value = !!r.enabled } catch { aiReady.value = false }
}
async function doAiSynopsis() {
  aiBusy.value = true
  try { const r = await aiGenerateSynopsis(id.value); mv.value.plot = r.plot; toast('AI 简介已生成', 'ok') }
  catch (e) { toast(e.message || 'AI 生成失败', 'err') }
  finally { aiBusy.value = false }
}
async function doAiTags() {
  aiBusy.value = true
  try {
    const r = await aiSuggestTags(id.value)
    const cur = (mv.value.tags || []).slice()
    const merged = [...new Set([...cur, ...(r.tags || [])])]
    mv.value.tags = merged
    await updateMovie(id.value, { tags: merged })
    toast('AI 标签已补充', 'ok')
  } catch (e) { toast(e.message || 'AI 标签失败', 'err') }
  finally { aiBusy.value = false }
}

const open = computed(() => !!state.currentId)
const id = computed(() => state.currentId)

const progressPos = computed(() => Number(mv.value?.progress?.position) || 0)
const progressPct = computed(() => {
  const d = Number(mv.value?.progress?.duration) || 0
  return d > 0 ? Math.min(100, Math.round((progressPos.value / d) * 100)) : 0
})

const mainFile = computed(() => (mv.value?.files || [])[0] || null)
const totalSize = computed(() => (mv.value?.files || []).reduce((s, f) => s + (Number(f.size) || 0), 0))
const quality = computed(() => qualityTag(mv.value?.resolution))

async function load() {
  if (!id.value) return
  loading.value = true
  tab.value = 'info'
  playing.value = false
  previews.value = []
  similar.value = []
  editing.value = false
  try {
    mv.value = await getMovie(id.value)
    checkAi()
  } catch (e) {
    toast(e.message, 'err')
    close()
  } finally { loading.value = false }
}

function close() {
  state.currentId = null
  playing.value = false
  lightbox.value = ''
}

/* ---------- 操作 ---------- */
async function flip(field) {
  try {
    const r = await toggleFlag(id.value, field)
    const v = r && r.value != null ? r.value : !mv.value[field]
    mv.value[field] = v
    window.dispatchEvent(new CustomEvent('avm-reload-view'))
  } catch (e) { toast(e.message, 'err') }
}

async function setRating(v) {
  const next = mv.value.rating === v ? 0 : v
  try {
    await updateMovie(id.value, { rating: next })
    mv.value.rating = next
    toast(next ? `已评 ${next} 星` : '已清除评分', 'ok')
    window.dispatchEvent(new CustomEvent('avm-reload-view'))
  } catch (e) { toast(e.message, 'err') }
}

async function play() {
  try { await playMovie(id.value); toast('已调用系统播放器', 'ok') }
  catch (e) { toast(e.message, 'err') }
}

// 详情页「重新刮削」复用批量刮削任务管线，进度会出现在右上角任务中心。
async function doScrape() {
  try {
    await runScrape({ ids: [id.value], overwrite: true })
    toast('已加入刮削队列', 'ok')
  } catch (e) { toast(e.message, 'err') }
}

// 刮削任务结束后自动刷新详情（任务中心轮询会在结束时广播 avm-refresh）
watch(
  () => state.task.scrape.running,
  (running, was) => {
    if (was && !running && id.value) load()
  },
)

async function doNfo() {
  try { const r = await exportNfo(id.value); toast('已导出 ' + (r.path || 'NFO'), 'ok') }
  catch (e) { toast(e.message, 'err') }
}

async function doDelete(withFile) {
  const ok = await confirmDialog(
    withFile ? '删除影片和文件' : '从库中移除',
    withFile
      ? `将永久删除磁盘文件，无法恢复：\n${mainFile.value?.path || ''}`
      : '仅从数据库移除记录，磁盘文件保留。',
    { danger: true, okText: withFile ? '永久删除' : '移除' },
  )
  if (!ok) return
  try {
    await deleteMovie(id.value, withFile)
    toast('已删除', 'ok')
    close()
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  } catch (e) { toast(e.message, 'err') }
}

/* ---------- 预览图 ---------- */
async function loadPreviews(generate = false) {
  pvLoading.value = true
  try {
    const r = await getPreviews(id.value, generate)
    previews.value = (r && r.urls) ? r.urls.map((u) => '/api' + u) : []
    if (generate && !previews.value.length) toast(r.error || '生成失败', 'err')
  } catch (e) { toast(e.message, 'err') } finally { pvLoading.value = false }
}

/* ---------- 相似推荐 ---------- */
async function loadSimilar() {
  try {
    const r = await getSimilar(id.value, 12)
    similar.value = (r && (r.items || r.movies)) || (Array.isArray(r) ? r : [])
  } catch (e) { similar.value = [] }
}

/* ---------- 编辑 ---------- */
function startEdit() {
  draft.value = {
    title: mv.value.title || '',
    code: mv.value.code || '',
    release_date: fmtDate(mv.value.release_date),
    runtime: mv.value.runtime || '',
    director: mv.value.director || '',
    plot: mv.value.plot || '',
    note: mv.value.note || '',
  }
  editing.value = true
}

async function saveEdit() {
  try {
    const patch = { ...draft.value }
    if (patch.runtime !== '') patch.runtime = Number(patch.runtime) || 0
    await updateMovie(id.value, patch)
    toast('已保存', 'ok')
    editing.value = false
    await load()
    window.dispatchEvent(new CustomEvent('avm-refresh'))
  } catch (e) { toast(e.message, 'err') }
}

/* ---------- 封面 ---------- */
const fileInput = ref(null)
async function onUpload(e) {
  const f = e.target.files && e.target.files[0]
  if (!f) return
  try {
    await uploadCover(id.value, f)
    toast('封面已更新', 'ok')
    bust.value = Date.now()
  } catch (err) { toast(err.message, 'err') }
  e.target.value = ''
}
const bust = ref(Date.now())
const coverSrc = computed(() => `${coverUrl(id.value)}?t=${bust.value}`)

async function removeCover() {
  if (!(await confirmDialog('清除封面', '将删除该影片的封面图。', { danger: true }))) return
  try { await clearCover(id.value); bust.value = Date.now(); toast('已清除', 'ok') }
  catch (e) { toast(e.message, 'err') }
}

/* ---------- 片单 ---------- */
async function openColl() {
  showColl.value = !showColl.value
  if (showColl.value && !collList.value.length) {
    try { collList.value = (await listCollections()) || [] } catch (e) { toast(e.message, 'err') }
  }
}
async function addColl(cid) {
  try { await addToCollection(cid, id.value); toast('已加入片单', 'ok'); showColl.value = false }
  catch (e) { toast(e.message, 'err') }
}

/* ---------- 跳转筛选 ---------- */
function filterBy(key, value) {
  if (key === 'actress' || key === 'genre') {
    state[key] = [value]
    state.actress = key === 'actress' ? [value] : []
    state.genre = key === 'genre' ? [value] : []
  } else {
    state.actress = []; state.genre = []
    state[key] = value
  }
  state.q = ''
  state.page = 1
  state.view = 'gallery'
  close()
}

function openActress(name) {
  state.actressCurrent = name
  state.actressReturnView = 'gallery'
  state.view = 'actressDetail'
  close()
}

/* ---------- 生命周期 ---------- */
watch(id, (v) => { if (v) { bust.value = Date.now(); load() } })
watch(tab, (t) => {
  if (t === 'preview' && !previews.value.length) loadPreviews(false)
  if (t === 'similar' && !similar.value.length) loadSimilar()
})

function onKey(e) {
  if (!open.value) return
  if (e.key === 'Escape') { lightbox.value ? (lightbox.value = '') : close() }
}
onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="drawer-mask" @click="close"></div>
    <aside class="drawer" :class="{ open }">
      <template v-if="mv">
        <!-- 头部 -->
        <header class="drawer-head">
          <span v-if="mv.display_code || mv.code" class="badge code primary">{{ mv.display_code || mv.code }}</span>
          <b class="dh-title ellipsis">{{ mv.title || '未命名' }}</b>
          <div class="spacer"></div>
          <button class="btn ghost icon" @click="copyText(mainFile?.path || '')" data-tip="复制路径">⧉</button>
          <button class="btn ghost icon" @click="close" data-tip="关闭 (Esc)">✕</button>
        </header>

        <div class="drawer-body">
          <!-- 播放器 -->
          <VideoPlayer
            v-if="playing"
            :movie-id="mv.id"
            :start-at="progressPos"
            @progress="(p) => { if (mv.progress) mv.progress.position = p.position }"
          />

          <!-- 主区 -->
          <div class="dd-top">
            <div class="dd-cover">
              <img :src="coverSrc" alt="" @error="coverFallback" @click="lightbox = coverSrc" />
              <div v-if="progressPct > 0" class="cw-bar"><i :style="{ width: progressPct + '%' }"></i></div>
              <div class="cov-acts">
                <button class="btn tiny" @click="fileInput.click()">换封面</button>
                <button class="btn tiny ghost" @click="removeCover">清除</button>
                <input ref="fileInput" type="file" accept="image/*" hidden @change="onUpload" />
              </div>
            </div>

            <div class="dd-main">
              <!-- 主操作 -->
              <div class="dd-actions">
                <button class="btn primary" @click="playing = !playing">
                  {{ playing ? '收起播放器' : (progressPct > 0 ? `继续观看 ${progressPct}%` : '在线播放') }}
                </button>
                <button class="btn" @click="play">系统播放器</button>
                <button class="btn icon" :class="{ active: mv.favorite }" @click="flip('favorite')" data-tip="收藏">{{ mv.favorite ? '♥' : '♡' }}</button>
                <button class="btn icon" :class="{ active: mv.watchlist }" @click="flip('watchlist')" data-tip="想看">⌚</button>
                <button class="btn icon" :class="{ active: mv.watched }" @click="flip('watched')" data-tip="已看">{{ mv.watched ? '●' : '○' }}</button>

                <div class="coll-wrap">
                  <button class="btn icon" @click="openColl" data-tip="加入片单">＋</button>
                  <div v-if="showColl" class="coll-pop">
                    <div v-if="!collList.length" class="cp-empty muted">还没有片单</div>
                    <button v-for="c in collList" :key="c.id" class="cp-item" @click="addColl(c.id)">{{ c.name }}</button>
                  </div>
                </div>
              </div>

              <!-- 评分 -->
              <div class="dd-rate">
                <div class="stars">
                  <span v-for="i in 5" :key="i" class="s" :class="{ on: i <= (mv.rating || 0) }" @click="setRating(i)">★</span>
                </div>
                <span class="muted">{{ mv.rating ? mv.rating + ' 星' : '未评分' }}</span>
                <div class="spacer"></div>
                <span v-if="mv.play_count" class="badge">播放 {{ mv.play_count }} 次</span>
              </div>

              <!-- 标记 -->
              <div class="chip-list">
                <span v-if="mv.subtitle" class="badge ok">中文字幕</span>
                <span v-if="mv.uncensored" class="badge warn">无码</span>
                <span v-if="quality" class="badge accent">{{ quality }}</span>
                <span v-if="mv.vr" class="badge">VR</span>
                <span v-if="mv.leak" class="badge err">流出</span>
              </div>

              <!-- 关键信息 -->
              <dl class="dd-facts">
                <template v-if="mv.actresses && mv.actresses.length">
                  <dt>女优</dt>
                  <dd class="chip-list">
                    <button v-for="a in mv.actresses" :key="a" class="chip" @click="openActress(a)">{{ a }}</button>
                  </dd>
                </template>
                <template v-if="mv.genres && mv.genres.length">
                  <dt>类型</dt>
                  <dd class="chip-list">
                    <button v-for="g in mv.genres" :key="g" class="chip" @click="filterBy('genre', g)">{{ g }}</button>
                  </dd>
                </template>
                <template v-if="mv.studio"><dt>厂商</dt><dd><a @click="filterBy('studio', mv.studio)">{{ mv.studio }}</a></dd></template>
                <template v-if="mv.series"><dt>系列</dt><dd><a @click="filterBy('series', mv.series)">{{ mv.series }}</a></dd></template>
                <template v-if="mv.director"><dt>导演</dt><dd>{{ mv.director }}</dd></template>
                <template v-if="mv.release_date"><dt>发行</dt><dd>{{ fmtDate(mv.release_date) }}</dd></template>
                <template v-if="mv.runtime"><dt>时长</dt><dd>{{ mv.runtime }} 分钟</dd></template>
                <template v-if="mv.resolution"><dt>分辨率</dt><dd>{{ mv.resolution }}</dd></template>
                <dt>体积</dt><dd>{{ fmtSize(totalSize) }}<span v-if="mv.files && mv.files.length > 1" class="dim"> · {{ mv.files.length }} 个文件</span></dd>
                <template v-if="mv.added_at"><dt>入库</dt><dd>{{ fmtAgo(mv.added_at) }}</dd></template>
              </dl>
            </div>
          </div>

          <!-- 标签页 -->
          <div class="tabs dd-tabs">
            <button class="tab" :class="{ on: tab === 'info' }" @click="tab = 'info'">简介</button>
            <button class="tab" :class="{ on: tab === 'preview' }" @click="tab = 'preview'">预览图</button>
            <button class="tab" :class="{ on: tab === 'similar' }" @click="tab = 'similar'">相似推荐</button>
            <button class="tab" :class="{ on: tab === 'files' }" @click="tab = 'files'">文件</button>
            <div class="spacer"></div>
            <button class="btn tiny ghost" @click="doScrape" :disabled="loading">重新刮削</button>
            <button class="btn tiny ghost" @click="doNfo">导出 NFO</button>
          </div>

          <div class="dd-pane">
            <!-- 简介 -->
            <template v-if="tab === 'info'">
              <div v-if="!editing">
                <p v-if="mv.plot" class="plot">{{ mv.plot }}</p>
                <p v-else class="muted">暂无简介。</p>
                <div v-if="mv.note" class="note-box"><b>备注</b><p>{{ mv.note }}</p></div>
                <div v-if="mv.tags && mv.tags.length" class="chip-list mt">
                  <span v-for="t in mv.tags" :key="t" class="badge">{{ t }}</span>
                </div>
                <div v-if="aiReady" class="hstack mt wrap">
                  <button class="btn tiny ghost" :disabled="aiBusy" @click="doAiSynopsis">{{ aiBusy ? '生成中…' : 'AI 生成简介' }}</button>
                  <button class="btn tiny ghost" :disabled="aiBusy" @click="doAiTags">AI 补充标签</button>
                </div>
                <button class="btn tiny mt" @click="startEdit">编辑元数据</button>
              </div>

              <div v-else class="edit-form">
                <div class="field"><label>标题</label><input v-model="draft.title" /></div>
                <div class="two">
                  <div class="field"><label>番号</label><input v-model="draft.code" /></div>
                  <div class="field"><label>发行日期</label><input v-model="draft.release_date" placeholder="YYYY-MM-DD" /></div>
                </div>
                <div class="two">
                  <div class="field"><label>时长（分钟）</label><input v-model="draft.runtime" type="number" min="0" /></div>
                  <div class="field"><label>导演</label><input v-model="draft.director" /></div>
                </div>
                <div class="field"><label>简介</label><textarea v-model="draft.plot" rows="4"></textarea></div>
                <div class="field"><label>备注</label><textarea v-model="draft.note" rows="2"></textarea></div>
                <div class="hstack">
                  <button class="btn primary" @click="saveEdit">保存</button>
                  <button class="btn ghost" @click="editing = false">取消</button>
                </div>
              </div>
            </template>

            <!-- 预览图 -->
            <template v-else-if="tab === 'preview'">
              <div class="hstack mb">
                <button class="btn tiny" :disabled="pvLoading" @click="loadPreviews(true)">
                  {{ pvLoading ? '生成中…' : '生成预览图' }}
                </button>
                <span class="muted">需要配置 ffmpeg 路径</span>
              </div>
              <div v-if="previews.length" class="pv-grid">
                <img v-for="(u, i) in previews" :key="i" :src="u" alt="" @click="lightbox = u" />
              </div>
              <div v-else-if="!pvLoading" class="empty"><div class="icon">▤</div><div class="desc">还没有预览图</div></div>
            </template>

            <!-- 相似 -->
            <template v-else-if="tab === 'similar'">
              <div v-if="similar.length" class="sim-grid">
                <div v-for="s in similar" :key="s.id" class="sim" @click="state.currentId = s.id">
                  <img :src="coverThumbUrl(s.id, 220)" alt="" @error="coverFallback" />
                  <div class="sim-t ellipsis">{{ s.title || s.code }}</div>
                </div>
              </div>
              <div v-else class="empty"><div class="icon">≈</div><div class="desc">暂无相似影片</div></div>
            </template>

            <!-- 文件 -->
            <template v-else>
              <table class="ftable">
                <thead><tr><th>文件名</th><th>大小</th><th>状态</th></tr></thead>
                <tbody>
                  <tr v-for="f in mv.files" :key="f.id">
                    <td class="fname" :title="f.path">{{ f.filename }}</td>
                    <td class="tabular">{{ fmtSize(f.size) }}</td>
                    <td><span class="badge" :class="f.missing ? 'err' : 'ok'">{{ f.missing ? '丢失' : '正常' }}</span></td>
                  </tr>
                </tbody>
              </table>
              <div class="danger-zone">
                <b>危险操作</b>
                <div class="hstack">
                  <button class="btn tiny" @click="doDelete(false)">从库中移除</button>
                  <button class="btn tiny danger" @click="doDelete(true)">删除影片和文件</button>
                </div>
              </div>
            </template>
          </div>
        </div>
      </template>

      <div v-else-if="loading" class="dd-loading"><span class="spinner large"></span></div>
    </aside>

    <!-- 灯箱 -->
    <div v-if="lightbox" class="lightbox" @click="lightbox = ''">
      <img :src="lightbox" alt="" />
    </div>
  </Teleport>
</template>

<style scoped>
.dh-title { font-size: var(--fs-lg); font-weight: 600; flex: 1; min-width: 0; }

.dd-top { display: flex; gap: var(--sp-5); padding: var(--sp-5); }
.dd-cover { position: relative; width: 216px; flex: none; }
.dd-cover img {
  width: 100%; aspect-ratio: 2/3; object-fit: cover;
  border-radius: var(--r-md);
  background: var(--c-surface-2);
  cursor: zoom-in;
  box-shadow: var(--sh-2);
}
.cw-bar { position: absolute; left: 0; right: 0; bottom: 40px; height: 3px; background: rgba(0,0,0,.5); }
.cw-bar > i { display: block; height: 100%; background: var(--c-primary); }
.cov-acts { display: flex; gap: var(--sp-2); margin-top: var(--sp-2); }

.dd-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-3); }
.dd-actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.dd-rate { display: flex; align-items: center; gap: var(--sp-3); }

.coll-wrap { position: relative; }
.coll-pop {
  position: absolute; top: calc(100% + 6px); left: 0; z-index: 5;
  min-width: 170px; max-height: 220px; overflow-y: auto;
  background: var(--c-surface-2);
  border: 1px solid var(--c-line-strong);
  border-radius: var(--r-md);
  box-shadow: var(--sh-3);
  padding: var(--sp-1);
}
.cp-item { display: block; width: 100%; text-align: left; padding: var(--sp-2); border-radius: var(--r-sm); font-size: var(--fs-md); }
.cp-item:hover { background: var(--c-surface-3); }
.cp-empty { padding: var(--sp-3); font-size: var(--fs-sm); }

.dd-facts {
  display: grid;
  grid-template-columns: 62px 1fr;
  gap: var(--sp-2) var(--sp-3);
  margin: 0;
  font-size: var(--fs-md);
  align-items: start;
}
.dd-facts dt { color: var(--c-text-3); font-size: var(--fs-sm); padding-top: 2px; }
.dd-facts dd { margin: 0; min-width: 0; }
.dd-facts a { cursor: pointer; }

.dd-tabs { padding: 0 var(--sp-5); align-items: center; gap: var(--sp-2); }
.dd-pane { padding: var(--sp-4) var(--sp-5) var(--sp-8); }

.plot { line-height: 1.8; color: var(--c-text-2); white-space: pre-wrap; }
.note-box {
  margin-top: var(--sp-3); padding: var(--sp-3);
  background: var(--c-surface); border-radius: var(--r-md);
  border-left: 3px solid var(--c-warn);
  font-size: var(--fs-md);
}
.note-box p { color: var(--c-text-2); margin-top: 4px; }
.mt { margin-top: var(--sp-3); }
.mb { margin-bottom: var(--sp-3); }

.edit-form { display: flex; flex-direction: column; gap: var(--sp-3); max-width: 620px; }
.edit-form .two { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }

.pv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: var(--sp-3); }
.pv-grid img { width: 100%; border-radius: var(--r-sm); cursor: zoom-in; background: var(--c-surface-2); }

.sim-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(112px, 1fr)); gap: var(--sp-3); }
.sim { cursor: pointer; }
.sim img { width: 100%; aspect-ratio: 2/3; object-fit: cover; border-radius: var(--r-sm); background: var(--c-surface-2); transition: transform var(--t-base); }
.sim:hover img { transform: translateY(-2px); box-shadow: var(--sh-2); }
.sim-t { font-size: var(--fs-xs); color: var(--c-text-2); margin-top: 4px; }

.ftable { width: 100%; border-collapse: collapse; font-size: var(--fs-md); }
.ftable th, .ftable td { text-align: left; padding: var(--sp-2); border-bottom: 1px solid var(--c-line); }
.ftable th { color: var(--c-text-3); font-size: var(--fs-sm); font-weight: 500; }
.ftable .fname { max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.danger-zone {
  margin-top: var(--sp-6); padding: var(--sp-3);
  border: 1px solid var(--c-err-soft); border-radius: var(--r-md);
  display: flex; flex-direction: column; gap: var(--sp-2);
}
.danger-zone b { color: var(--c-err); font-size: var(--fs-sm); }

.dd-loading { flex: 1; display: grid; place-items: center; }

.lightbox {
  position: fixed; inset: 0; z-index: var(--z-toast);
  background: rgba(0,0,0,.88);
  display: grid; place-items: center;
  padding: var(--sp-6);
  cursor: zoom-out;
  animation: fade-in var(--t-base);
}
.lightbox img { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: var(--r-sm); }

@media (max-width: 760px) {
  .dd-top { flex-direction: column; }
  .dd-cover { width: 160px; }
  .edit-form .two { grid-template-columns: 1fr; }
}
</style>
