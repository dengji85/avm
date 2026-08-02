<script setup>
import { ref, onMounted } from 'vue'
import { state } from '../state.js'
import { getHealthCheck, reparseCodes, startScrape } from '../api.js'
import { toast } from '../utils.js'

const health = ref(null)
const loading = ref(false)
const fixing = ref(false)

const GROUPS = [
  ['missing_files', '缺失文件', '数据库有记录但磁盘文件不存在', 'err'],
  ['split_incomplete', '分片不完整', '多集影片缺少部分分片', 'warn'],
  ['missing_cover', '缺封面', '尚未获取到封面图', 'warn'],
  ['placeholder_cover', '占位图封面', '封面为无效的占位图（如 DMM 无封面图）', 'warn'],
  ['unrecognized', '未识别番号', '未能从文件名解析出番号', 'warn'],
  ['duplicates', '疑似重复', '文件大小完全相同', 'accent'],
]

async function run() {
  loading.value = true
  try { health.value = await getHealthCheck(); }
  catch (e) { toast(e.message, 'err') }
  finally { loading.value = false }
}

async function doReparse() {
  fixing.value = true
  try {
    const r = await reparseCodes({ only_missing: true })
    toast(`重解析：${r.fixed} 部识别成功，${r.failed} 部无法识别`, 'ok')
    await run()
  } catch (e) { toast(e.message, 'err') }
  finally { fixing.value = false }
}

async function scrapeMissing() {
  fixing.value = true
  try { await startScrape({ missing_only: true }); toast('已开始刮削缺失元数据', 'ok') }
  catch (e) { toast(e.message, 'err') }
  finally { fixing.value = false }
}

function openMovie(id) { state.currentId = id }

onMounted(run)
</script>

<template>
  <div class="view view-health">
    <header class="view-head">
      <div>
        <h1>数据健康</h1>
        <p class="muted">扫描库内数据质量问题：缺失文件、占位图、未识别番号、重复等。</p>
      </div>
      <div class="hstack">
        <button class="btn" :disabled="loading" @click="run">{{ loading ? '体检中…' : '重新体检' }}</button>
        <button class="btn primary" :disabled="fixing" @click="doReparse">重解析番号</button>
        <button class="btn" :disabled="fixing" @click="scrapeMissing">刮削缺失</button>
      </div>
    </header>

    <div v-if="!health" class="empty muted">点击「重新体检」开始扫描…</div>

    <div v-else class="cards">
      <div v-for="g in GROUPS" :key="g[0]" class="card" :class="g[3]">
        <div class="c-head">
          <span class="dot"></span>
          <b>{{ g[1] }}</b>
          <span class="cnt" :class="g[3]">{{ (health.counts[g[0]] || 0) }}</span>
        </div>
        <div class="c-sub muted">{{ g[2] }}</div>
        <ul v-if="(health[g[0]] || []).length" class="list">
          <li v-for="item in health[g[0]]" :key="item.id" @click="openMovie(item.id)">
            <span class="code">{{ item.code || '—' }}</span>
            <span class="ttl ellipsis">{{ item.title || item.folder || item.name }}</span>
          </li>
        </ul>
        <div v-else class="ok-note">✓ 无此项问题</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.view-health { padding: var(--sp-4); }
.view-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--sp-3); margin-bottom: var(--sp-4); flex-wrap: wrap; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: var(--sp-3); }
.card { background: var(--c-surface); border: 1px solid var(--c-line); border-radius: var(--r-md); padding: var(--sp-3); }
.card.err { border-color: color-mix(in srgb, var(--c-danger) 40%, var(--c-line)); }
.card.warn { border-color: color-mix(in srgb, var(--c-gold) 40%, var(--c-line)); }
.card.accent { border-color: color-mix(in srgb, var(--c-accent) 40%, var(--c-line)); }
.c-head { display: flex; align-items: center; gap: var(--sp-2); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--c-text-3); }
.card.err .dot { background: var(--c-danger); }
.card.warn .dot { background: var(--c-gold); }
.card.accent .dot { background: var(--c-accent); }
.cnt { margin-left: auto; font-size: var(--fs-lg); font-variant-numeric: tabular-nums; font-weight: 600; }
.cnt.err { color: var(--c-danger); }
.cnt.warn { color: var(--c-gold); }
.cnt.accent { color: var(--c-accent); }
.c-sub { font-size: var(--fs-xs); margin: var(--sp-1) 0 var(--sp-2); }
.list { list-style: none; margin: 0; padding: 0; max-height: 220px; overflow: auto; display: flex; flex-direction: column; gap: 2px; }
.list li { display: flex; gap: var(--sp-2); padding: 4px 6px; border-radius: var(--r-sm); cursor: pointer; font-size: var(--fs-sm); }
.list li:hover { background: var(--c-surface-2); }
.code { color: var(--c-accent); flex: none; font-variant-numeric: tabular-nums; }
.ok-note { color: var(--c-success, #3fb950); font-size: var(--fs-sm); }
</style>
