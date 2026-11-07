<script setup>
/**
 * 起步清单：空库时替代首页的四条空轨道。
 *
 * 所有完成状态都从真实数据推导（目录 / 影片数 / ffmpeg 就绪），不要求用户打卡：
 * 换设备、重跑向导、老用户清空库后都会自动复现，库里一有影片就自动消失。
 */
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { state, openExtInstall } from '../state.js'
import { getConfig, listMovies, extStatus } from '../api.js'
import { t } from '../i18n/index.js'
import { useTasks } from '../composables/useTasks.js'
import { setChecklistHidden, guideIntent } from '../composables/useGuide.js'

defineProps({ embedded: { type: Boolean, default: false } })
const { runScan } = useTasks()

const total = ref(0)
const paths = ref([])
const ffReady = ref(false)
const minSize = ref(100)
const showTrouble = ref(false)

/** 后端 0 结果时给的线索（见 /api/scan/status 的 hint） */
const hint = computed(() => state.task.scan.hint || null)

async function refresh() {
  try {
    const [cfg, lib, ext] = await Promise.all([
      getConfig().catch(() => null),
      listMovies({ page: 1, page_size: 1 }).catch(() => null),
      extStatus().catch(() => null),
    ])
    if (cfg && cfg.library) {
      paths.value = cfg.library.paths || []
      minSize.value = Number(cfg.library.min_size_mb ?? 100)
    }
    if (lib) total.value = Number(lib.total) || 0
    if (ext) {
      const ff = (ext.components || []).find((c) => c.id === 'ffmpeg')
      ffReady.value = !!ff && ff.state === 'ready'
    }
  } catch (e) { /* 离线时保留上一次结果 */ }
}

function goSettings(tab) {
  state.settingsTab = tab
  state.view = 'settings'
  state.guideOpen = false
}
function goLibrary() { goSettings('library') }
function doScan() {
  if (state.task.scan.running) return
  showTrouble.value = false
  runScan({})
}

const steps = computed(() => [
  {
    key: 'dir', title: t('guide.step1Title'), desc: t('guide.step1Desc'),
    done: paths.value.length > 0, action: t('guide.step1Action'), run: goLibrary,
  },
  {
    key: 'scan', title: t('guide.step2Title'), desc: t('guide.step2Desc'),
    done: total.value > 0,
    action: state.task.scan.running ? t('guide.step2Running') : t('guide.step2Action'),
    run: doScan,
  },
  {
    key: 'cover', title: t('guide.step3Title'), desc: t('guide.step3Desc'),
    done: ffReady.value, action: t('guide.step3Action'),
    run: () => { state.guideOpen = false; openExtInstall('ffmpeg') },
    extra: t('guide.step3Scrape'), extraRun: () => goSettings('scraper'),
  },
])
const doneCount = computed(() => steps.value.filter((s) => s.done).length)
const pctWidth = computed(() => `${Math.round(doneCount.value / 3 * 100)}%`)

const extras = computed(() => {
  const arr = [
    { key: 'phone', label: t('guide.playPhone'), desc: t('guide.playPhoneDesc'), view: 'settings', tab: 'remote' },
    { key: 'ai', label: t('guide.playAi'), desc: t('guide.playAiDesc'), view: 'ai', tab: '' },
    { key: 'sub', label: t('guide.playSub'), desc: t('guide.playSubDesc'), view: 'maintenance', tab: 'subtitle' },
    { key: 'list', label: t('guide.playList'), desc: t('guide.playListDesc'), view: 'collections', tab: '' },
  ]
  // 首启向导第 3 步选的目标决定哪个玩法排在最前面
  const first = { rich: 'ai', remote: 'phone', basic: 'list' }[guideIntent.value]
  const i = arr.findIndex((x) => x.key === first)
  if (i > 0) arr.unshift(arr.splice(i, 1)[0])
  return arr
})
const activeExtra = ref('')
function goExtra(e) {
  if (e.view === 'maintenance') state.maintTab = e.tab
  else if (e.tab) state.settingsTab = e.tab
  state.view = e.view
  state.guideOpen = false
}

const troubleItems = computed(() => [
  { title: t('guide.troubleDir'), desc: t('guide.troubleDirDesc') },
  { title: t('guide.troubleSize'), desc: t('guide.troubleSizeDesc') },
  { title: t('guide.troubleExt'), desc: t('guide.troubleExtDesc') },
  { title: t('guide.troubleKeyword'), desc: t('guide.troubleKeywordDesc') },
])

/* 扫描刚结束时刷新数据；仍是 0 结果就直接展开排查清单，不用用户再找入口 */
watch(() => state.task.scan.running, async (running, was) => {
  if (!was || running) return
  await refresh()
  if (total.value === 0) showTrouble.value = true
})

onMounted(() => {
  refresh()
  window.addEventListener('avm-refresh', refresh)
})
onBeforeUnmount(() => window.removeEventListener('avm-refresh', refresh))
</script>

<template>
  <div class="gs">
    <div class="gs-head">
      <div class="gs-head-txt">
        <h2>{{ $t('guide.welcome') }}</h2>
        <p>{{ $t('guide.welcomeSub') }}</p>
      </div>
      <div class="gs-head-side">
        <div class="gs-progress">{{ $t('guide.progress', { n: doneCount, total: 3 }) }}</div>
        <div class="gs-bar"><i :style="{ width: pctWidth }"></i></div>
        <button v-if="!embedded" class="btn tiny ghost" @click="setChecklistHidden(true)">{{ $t('guide.hide') }}</button>
      </div>
    </div>

    <div class="gs-cards">
      <div v-for="(s, i) in steps" :key="s.key" :class="['gs-card', s.done ? 'done' : '']">
        <div class="gs-card-top">
          <span class="gs-num">{{ s.done ? '✓' : i + 1 }}</span>
          <span :class="['gs-state', s.done ? 'ok' : '']">
            {{ s.done ? $t('guide.statusDone') : $t('guide.statusTodo') }}
          </span>
        </div>
        <h3>{{ s.title }}</h3>
        <p>{{ s.desc }}</p>
        <div class="gs-actions">
          <button class="btn tiny primary" :disabled="s.key === 'scan' && state.task.scan.running" @click="s.run">{{ s.action }}</button>
          <button v-if="s.extra" class="btn tiny ghost" @click="s.extraRun">{{ s.extra }}</button>
        </div>
      </div>

      <div class="gs-card optional">
        <div class="gs-card-top">
          <span class="gs-num">＋</span>
          <span class="gs-state opt">{{ $t('guide.statusOptional') }}</span>
        </div>
        <h3>{{ $t('guide.step4Title') }}</h3>
        <p>{{ $t('guide.step4Desc') }}</p>
        <div class="gs-chips">
          <button
            v-for="e in extras"
            :key="e.key"
            :class="['btn', 'tiny', activeExtra === e.key ? 'primary' : 'ghost']"
            @click="activeExtra = activeExtra === e.key ? '' : e.key"
          >{{ e.label }}</button>
        </div>
        <div v-if="activeExtra" class="gs-extra">
          <p>{{ (extras.find((x) => x.key === activeExtra) || {}).desc }}</p>
          <button class="btn tiny" @click="goExtra(extras.find((x) => x.key === activeExtra))">{{ $t('common.next') }} →</button>
        </div>
      </div>
    </div>

    <div v-if="showTrouble" class="gs-trouble">
      <b>{{ $t('guide.noResultTitle') }}</b>
      <p>{{ $t('guide.noResultDesc') }}</p>
      <p v-if="hint" class="gs-hint">
        {{ $t('guide.troubleSize') }}: {{ minSize }} MB
        <span v-if="hint.checked_paths && hint.checked_paths.length"> · {{ hint.checked_paths.join(' / ') }}</span>
      </p>
      <ul>
        <li v-for="(x, i) in troubleItems" :key="i">
          <b>{{ x.title }}</b><span>{{ x.desc }}</span>
        </li>
      </ul>
      <div class="gs-actions">
        <button class="btn tiny" @click="goLibrary">{{ $t('guide.troubleAction') }}</button>
        <button class="btn tiny primary" :disabled="state.task.scan.running" @click="doScan">{{ $t('guide.troubleRescan') }}</button>
      </div>
    </div>
    <button v-else-if="!total" class="gs-trouble-link" @click="showTrouble = true">{{ $t('guide.trouble') }}</button>
  </div>
</template>

<style scoped>
.gs { padding: 8px 0 20px; }
.gs-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: var(--sp-4, 16px);
  flex-wrap: wrap; margin-bottom: 16px;
}
.gs-head-txt h2 { margin: 0 0 4px; font-size: var(--fs-xl); color: var(--c-text); }
.gs-head-txt p { margin: 0; color: var(--c-text-2); font-size: var(--fs-md); }
.gs-head-side { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.gs-progress { font-size: var(--fs-sm); color: var(--c-text-2); white-space: nowrap; }
.gs-bar { width: 110px; height: 6px; border-radius: 999px; background: var(--c-surface-2); overflow: hidden; }
.gs-bar i { display: block; height: 100%; background: var(--c-primary); border-radius: 999px; transition: width .25s; }

.gs-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.gs-card {
  display: flex; flex-direction: column;
  padding: 14px; border: 1px solid var(--c-line); border-radius: var(--r-md, 10px);
  background: var(--c-surface-2);
}
.gs-card.done { border-color: var(--c-primary); background: var(--c-primary-soft); }
.gs-card.optional { border-style: dashed; }
.gs-card-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.gs-num {
  display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; border-radius: 50%;
  background: var(--c-surface); color: var(--c-text); font-size: var(--fs-sm); font-weight: 600;
}
.gs-card.done .gs-num { background: var(--c-primary); color: #fff; }
.gs-state { font-size: var(--fs-sm); color: var(--c-text-2); }
.gs-state.ok { color: var(--c-primary); }
.gs-state.opt { opacity: .8; }
.gs-card h3 { margin: 0 0 6px; font-size: var(--fs-md); color: var(--c-text); }
.gs-card p { margin: 0 0 12px; font-size: var(--fs-sm); color: var(--c-text-2); line-height: 1.5; }
.gs-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: auto; }
.gs-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: auto; }
.gs-extra { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--c-line); }
.gs-extra p { margin: 0 0 8px; }
.gs-trouble {
  margin-top: 14px; padding: 14px;
  border: 1px solid var(--c-warn, #c08a2e); border-radius: var(--r-md, 10px);
  background: var(--c-surface-2); color: var(--c-text);
}
.gs-trouble > b { font-size: var(--fs-md); }
.gs-trouble > p { margin: 4px 0 10px; font-size: var(--fs-sm); color: var(--c-text-2); }
.gs-hint { font-size: var(--fs-sm); color: var(--c-text-2); word-break: break-all; }
.gs-trouble ul { list-style: none; margin: 0 0 12px; padding: 0; display: grid; gap: 8px; }
.gs-trouble li { display: flex; flex-direction: column; gap: 2px; font-size: var(--fs-sm); }
.gs-trouble li b { color: var(--c-text); }
.gs-trouble li span { color: var(--c-text-2); line-height: 1.5; }
.gs-trouble-link {
  margin-top: 12px; padding: 0; border: 0; background: none; cursor: pointer;
  color: var(--c-primary); font-size: var(--fs-sm); text-decoration: underline;
}
.btn.tiny { padding: 4px 9px; font-size: var(--fs-sm); border-radius: var(--r-sm, 6px); }
.btn.primary { background: var(--c-primary); color: #fff; border: 1px solid transparent; }
.btn.ghost { background: transparent; color: var(--c-text); border: 1px solid var(--c-line); }
.btn:disabled { opacity: .5; cursor: not-allowed; }
</style>
