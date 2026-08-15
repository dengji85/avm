<script setup>
import { ref } from 'vue'
import { t } from '../../i18n/index.js'
import { uploadAndMatchSubtitles, alignSubtitles } from '../../api.js'

const fileInput = ref(null)
const filenames = ref('')
const matching = ref(false)
const aligning = ref(false)
const result = ref(null)    // { matched, unmatched, total }
const alignResult = ref(null)
const selected = ref(new Set())
const copyMode = ref(false) // 是否复制（保留原文件）

const matchedList = ref([])
const unmatchedList = ref([])

function onPick(e) {
  const list = Array.from(e.target.files || [])
  if (!list.length) return
  filenames.value = list.map(f => f.name).join('、')
  result.value = null
  alignResult.value = null
  selected.value = new Set()
}

async function runMatch() {
  const list = Array.from(fileInput.value?.files || [])
  if (!list.length) return
  matching.value = true
  result.value = null
  alignResult.value = null
  try {
    const fd = new FormData()
    list.forEach(f => fd.append('files', f))
    const r = await uploadAndMatchSubtitles(fd)
    result.value = r
    matchedList.value = r.matched || []
    unmatchedList.value = r.unmatched || []
  } catch (e) {
    result.value = { error: e.message }
  } finally {
    matching.value = false
  }
}

function toggle(s) {
  if (selected.value.has(s.subtitle_path)) selected.value.delete(s.subtitle_path)
  else selected.value.add(s.subtitle_path)
}
const allChecked = ref(false)
function syncAll(v) {
  allChecked.value = v
  selected.value = new Set(v ? matchedList.value.map(s => s.subtitle_path) : [])
}

async function alignSelected() {
  const items = matchedList.value
    .filter(s => selected.value.has(s.subtitle_path))
    .map(s => ({ subtitle_path: s.subtitle_path, movie_id: s.movie_id }))
  if (!items.length) return
  aligning.value = true
  try {
    alignResult.value = await alignSubtitles({ items, copy: copyMode.value })
  } catch (e) {
    alignResult.value = { error: e.message }
  } finally {
    aligning.value = false
  }
}
</script>

<template>
  <div class="subtab">
    <p class="lead">{{ t('subtitle.lead') }}</p>

    <div class="pick">
      <input ref="fileInput" type="file" multiple accept=".srt,.ass,.ssa,.vtt,.sub,.smi,.txt"
             class="file" @change="onPick" />
      <span class="names" v-if="filenames">{{ filenames }}</span>
    </div>

    <div class="actions">
      <button class="btn primary" :disabled="!filenames || matching" @click="runMatch">
        {{ matching ? t('subtitle.matching') : t('subtitle.matchSelected') }}
      </button>
      <label class="ck"><input type="checkbox" v-model="copyMode" /> {{ t('subtitle.copyMode') }}</label>
    </div>

    <div v-if="result && result.error" class="err">{{ result.error }}</div>

    <template v-if="result && result.total !== undefined">
      <div class="sum">
        {{ t('subtitle.summary', { total: result.total, ok: matchedList.length, no: unmatchedList.length }) }}
      </div>

      <div class="block" v-if="matchedList.length">
        <div class="block-head">
          <label class="ck"><input type="checkbox" :checked="allChecked" @change="e => syncAll(e.target.checked)" /> {{ t('subtitle.selectAll') }}</label>
          <button class="btn" :disabled="!selected.size || aligning" @click="alignSelected">
            {{ aligning ? t('subtitle.aligning') : t('subtitle.align') }}
          </button>
        </div>
        <ul class="rows">
          <li v-for="s in matchedList" :key="s.subtitle_path" :class="{ on: selected.has(s.subtitle_path) }" @click="toggle(s)">
            <input type="checkbox" :checked="selected.has(s.subtitle_path)" @click.stop="toggle(s)" />
            <span class="code">{{ s.code }}</span>
            <span class="name">{{ s.subtitle_name }}</span>
            <span class="movie">→ {{ s.title }}</span>
            <span class="lang">{{ s.lang }}</span>
          </li>
        </ul>
      </div>

      <div class="block" v-if="unmatchedList.length">
        <div class="block-head muted">{{ t('subtitle.unmatchedTitle') }}</div>
        <ul class="rows dim">
          <li v-for="s in unmatchedList" :key="s.subtitle_path">
            <span class="name">{{ s.subtitle_name }}</span>
            <span class="lang">{{ s.lang }}</span>
            <span class="movie" v-if="s.code">番号: {{ s.code }}</span>
            <span class="movie" v-else>未能识别番号</span>
          </li>
        </ul>
      </div>

      <div v-if="alignResult && !alignResult.error" class="sum ok">
        {{ t('subtitle.aligned', { n: alignResult.results.filter(r => r.ok).length, total: alignResult.results.length }) }}
      </div>
      <div v-if="alignResult && alignResult.error" class="err">{{ alignResult.error }}</div>
    </template>
  </div>
</template>

<style scoped>
.subtab { display: flex; flex-direction: column; gap: var(--sp-3); }
.lead { font-size: var(--fs-sm); color: var(--c-text-3); margin: 0; }
.pick { display: flex; flex-direction: column; gap: 6px; }
.file { font-size: var(--fs-sm); }
.names { font-size: var(--fs-xs); color: var(--c-text-3); word-break: break-all; }
.actions { display: flex; align-items: center; gap: var(--sp-3); }
.ck { font-size: var(--fs-sm); color: var(--c-text-2); display: flex; align-items: center; gap: 4px; }
.btn { padding: 6px 14px; border-radius: 8px; border: 1px solid var(--c-border); background: var(--c-bg-2); color: var(--c-text); cursor: pointer; }
.btn.primary { background: var(--c-accent); color: #fff; border-color: var(--c-accent); }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.sum { font-size: var(--fs-sm); color: var(--c-text-3); }
.sum.ok { color: var(--c-ok); }
.block { border: 1px solid var(--c-border); border-radius: 10px; padding: var(--sp-2) var(--sp-3); }
.block-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--sp-2); }
.block-head.muted { color: var(--c-text-3); font-size: var(--fs-sm); }
.rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; max-height: 320px; overflow: auto; }
.rows li { display: flex; align-items: center; gap: 8px; padding: 4px 8px; border-radius: 6px; font-size: var(--fs-sm); cursor: pointer; }
.rows li.on { background: var(--c-bg-3); }
.rows.dim li { color: var(--c-text-3); cursor: default; }
.rows .code { font-weight: 600; color: var(--c-accent); min-width: 96px; }
.rows .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rows .movie { color: var(--c-text-3); font-size: var(--fs-xs); }
.rows .lang { color: var(--c-text-2); font-size: var(--fs-xs); min-width: 56px; text-align: right; }
.err { color: var(--c-err); font-size: var(--fs-sm); }

@media (max-width: 640px) {
  .actions { flex-wrap: wrap; }
  .rows li { flex-wrap: wrap; gap: 4px 8px; }
  .rows .code { min-width: auto; }
  .rows .name { flex-basis: 100%; order: -1; }
  .rows .lang { min-width: auto; text-align: left; }
}
</style>
