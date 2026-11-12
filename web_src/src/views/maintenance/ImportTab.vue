<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
import { state } from '../../state.js'
import {
  importText, importLinks, probeStart, probeState, probeCancel,
} from '../../api.js'
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

/* ---------------- 片名/番号 + 播放地址 ---------------- */

const linkText = ref('')
const linkBusy = ref(false)
const linkResult = ref(null)
const probe = ref(null)
let probeTimer = null

const URL_RE = /https?:\/\/[^\s,，|｜]+/i

function host(url) {
  try { return new URL(url).host } catch { return '' }
}

function isHls(url) {
  const path = String(url || '').split('?')[0].toLowerCase()
  return path.endsWith('.m3u8') || path.includes('/hls/')
}

const preview = computed(() => {
  const rows = []
  linkText.value.split('\n').forEach((raw, i) => {
    const s = raw.trim()
    if (!s || s.startsWith('#')) return
    const m = s.match(URL_RE)
    const url = m ? m[0].replace(/[.,;:)\]}"']+$/, '') : ''
    const name = (m
      ? (s.slice(0, m.index) + ' ' + s.slice(m.index + m[0].length))
      : s).replace(/[\t,，|｜]+/g, ' ').trim()
    rows.push({
      line: i + 1, name, url, hls: isHls(url),
      ok: !!url && !!name && /^https?:\/\/[^/\s]+/i.test(url),
    })
  })
  return rows
})
const badCount = computed(() => preview.value.filter((r) => !r.ok).length)

const STATUS_KEY = {
  created: 'maint.importLinksNew',
  source_added: 'maint.importLinksSource',
  duplicate: 'maint.importLinksDup',
  failed: 'maint.importLinksFail',
}
const statusKey = (s) => STATUS_KEY[s] || 'maint.importLinksFail'

function stopProbeTimer() {
  if (probeTimer) { clearInterval(probeTimer); probeTimer = null }
}

async function startProbe() {
  try { probe.value = await probeStart({}) } catch { return }
  if (!probe.value || !probe.value.running) return
  stopProbeTimer()
  probeTimer = setInterval(async () => {
    try { probe.value = await probeState() } catch { return }
    if (!probe.value || !probe.value.running) stopProbeTimer()
  }, 1500)
}

async function cancelProbe() {
  try { probe.value = await probeCancel() } catch { /* 已经结束就算了 */ }
}

async function runLinks() {
  if (!linkText.value.trim() || linkBusy.value) return
  linkBusy.value = true
  try {
    const r = await importLinks(linkText.value, scrape.value)
    linkResult.value = r
    const s = r.summary || {}
    toast(`新建 ${s.created || 0} · 新来源 ${s.source_added || 0} · 重复 ${s.duplicate || 0} · 失败 ${s.failed || 0}`, 'ok')
    if ((s.created || 0) + (s.source_added || 0) > 0) startProbe()
  } catch (e) {
    toast('导入失败：' + e.message, 'err')
  } finally {
    linkBusy.value = false
  }
}

onBeforeUnmount(stopProbeTimer)
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

    <hr class="sep" />

    <h4 class="sub-title">{{ $t('maint.importLinksTitle') }}</h4>
    <p class="hint">{{ $t('maint.importLinksDesc') }}</p>
    <textarea
      v-model="linkText" class="import-area" rows="6"
      :placeholder="$t('maint.importLinksPlaceholder')"
    ></textarea>
    <div class="actions">
      <span v-if="preview.length" class="muted">
        {{ $t('maint.importLinksPreview', { n: preview.length }) }}
        <template v-if="badCount"> · {{ $t('maint.importLinksPreviewBad', { n: badCount }) }}</template>
      </span>
      <button class="btn primary" :disabled="linkBusy || !preview.length" @click="runLinks">
        {{ linkBusy ? '…' : $t('maint.importLinksBtn') }}
      </button>
    </div>

    <ul v-if="preview.length" class="list preview">
      <li v-for="p in preview.slice(0, 30)" :key="'p' + p.line" :class="{ bad: !p.ok }">
        <span class="ln">{{ p.line }}</span>
        <b>{{ p.name || '—' }}</b>
        <span class="muted">{{ p.url ? host(p.url) : $t('maint.importLinksNoUrl') }}</span>
        <span class="tag">
          {{ p.ok ? (p.hls ? $t('maint.importLinksHls') : $t('maint.importLinksPage'))
                  : $t('maint.importLinksBad') }}
        </span>
      </li>
      <li v-if="preview.length > 30" class="muted">…</li>
    </ul>

    <div v-if="linkResult" class="result">
      <p class="muted">
        {{ $t('maint.importLinksSummary', {
          c: linkResult.summary.created, s: linkResult.summary.source_added,
          d: linkResult.summary.duplicate, f: linkResult.summary.failed,
        }) }}
      </p>
      <table class="tbl">
        <thead>
          <tr>
            <th>#</th>
            <th>{{ $t('maint.importLinksColName') }}</th>
            <th>{{ $t('maint.importLinksColSite') }}</th>
            <th>{{ $t('maint.importLinksColResult') }}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="it in linkResult.items" :key="'r' + it.line">
            <td>{{ it.line }}</td>
            <td>{{ it.code || it.text }}</td>
            <td class="muted">{{ it.site || '—' }}</td>
            <td>
              <span class="tag" :class="it.status">{{ $t(statusKey(it.status)) }}</span>
              <span v-if="it.message" class="muted"> {{ it.message }}</span>
            </td>
            <td>
              <button v-if="it.movie_id" class="btn tiny" @click="openMovie(it.movie_id)">
                {{ $t('maint.importOpen') }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="probe" class="probe">
        <span class="muted">
          {{ $t('maint.importLinksProbe', { done: probe.done, total: probe.total }) }}
          <template v-if="probe.message"> · {{ probe.message }}</template>
        </span>
        <button v-if="probe.running" class="btn tiny" @click="cancelProbe">
          {{ $t('maint.importLinksCancel') }}
        </button>
      </div>
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
.sep { border: 0; border-top: 1px solid var(--border, #2a3140); margin: 22px 0 16px; }
.sub-title { margin: 0 0 6px; font-size: 14px; }
.preview { max-height: 220px; overflow: auto; }
.preview li.bad b { opacity: .55; }
.ln { min-width: 22px; opacity: .45; font-size: 12px; text-align: right; }
.tag {
  font-size: 11px; padding: 1px 6px; border-radius: 6px;
  background: var(--bg-hover, #222a36); color: var(--fg, #e8eaf0);
}
.tag.created { background: rgba(64, 160, 110, .22); }
.tag.source_added { background: rgba(92, 140, 255, .22); }
.tag.duplicate { opacity: .6; }
.tag.failed { background: rgba(220, 90, 90, .22); }
.tbl { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 6px; }
.tbl th, .tbl td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border, #2a3140); }
.tbl th { opacity: .55; font-weight: 500; }
.probe { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
</style>
