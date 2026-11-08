<script setup>
import { ref } from 'vue'
import { state } from '../../state.js'
import { importText } from '../../api.js'
import { toast } from '../../utils.js'

const text = ref('')
const scrape = ref(true)
const busy = ref(false)
const result = ref(null)

async function run() {
  if (!text.value.trim() || busy.value) return
  busy.value = true
  try {
    const r = await importText(text.value, scrape.value)
    result.value = r
    toast(r.added.length
      ? `新增 ${r.added.length} 部 · 已存在 ${r.existed.length} 部`
      : `未发现新番号（已存在 ${r.existed.length} 部）`, 'ok')
  } catch (e) {
    toast('导入失败：' + e.message, 'err')
  } finally {
    busy.value = false
  }
}

function openMovie(id) { state.currentId = id }
</script>

<template>
  <div>
    <p class="hint">{{ $t('maint.importDesc') }}</p>
    <textarea
      v-model="text" class="import-area" rows="10"
      :placeholder="$t('maint.importPlaceholder')"
    ></textarea>
    <div class="actions">
      <label class="check">
        <input type="checkbox" v-model="scrape" />{{ $t('maint.importScrape') }}
      </label>
      <button class="btn primary" :disabled="busy || !text.trim()" @click="run">
        {{ busy ? '…' : $t('maint.importBtn') }}
      </button>
    </div>

    <div v-if="result" class="result">
      <p v-if="!result.total" class="muted">{{ $t('maint.importNone') }}</p>
      <template v-else>
        <p class="muted">
          {{ $t('maint.importSummary', { n: result.added.length, m: result.existed.length }) }}
        </p>
        <ul class="list">
          <li v-for="a in result.added" :key="'a' + a.movie_id">
            <b>{{ a.code }}</b><span class="muted">{{ $t('maint.importNew') }}</span>
            <button class="btn tiny" @click="openMovie(a.movie_id)">{{ $t('maint.importOpen') }}</button>
          </li>
          <li v-for="e in result.existed" :key="'e' + e.movie_id">
            <b>{{ e.code }}</b><span class="muted">{{ $t('maint.importExisted') }}</span>
            <button class="btn tiny" @click="openMovie(e.movie_id)">{{ $t('maint.importOpen') }}</button>
          </li>
        </ul>
      </template>
    </div>
  </div>
</template>

<style scoped>
.hint { opacity: .65; font-size: 12px; margin: 0 0 8px; }
.import-area {
  width: 100%; box-sizing: border-box; padding: 10px;
  font: 13px/1.6 ui-monospace, Consolas, monospace;
  background: var(--bg-card, #161a22); color: var(--fg, #e8eaf0);
  border: 1px solid var(--border, #2a3140); border-radius: 8px; resize: vertical;
}
.actions { display: flex; align-items: center; gap: 14px; margin: 10px 0; }
.check { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.result { margin-top: 6px; }
.muted { opacity: .6; font-size: 12px; margin: 4px 0; }
.list { list-style: none; margin: 6px 0 0; padding: 0; display: grid; gap: 6px; }
.list li { display: flex; align-items: center; gap: 10px; font-size: 13px; }
</style>
