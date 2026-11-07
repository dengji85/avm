<script setup>
/**
 * 首次启动引导（3 步 + 结果确认）：语言 → 媒体库目录 → 想先做成什么 → 结果。
 *
 * 设计要点：
 *  · **按环境换控件**：容器里看不到宿主机文件系统，目录浏览按钮直接隐藏，
 *    预填约定的挂载点 /media，并说明宿主机目录要在 compose 里挂载；
 *  · **第 3 步只问意图**，用它决定后续提示，不在向导里做认证设置 ——
 *    用户此刻还没有"要保存凭据"的意识，在这里改认证最容易把人锁在门外；
 *  · **不再「点完就撒手」**：结果页会等扫描跑完，明确告诉用户找到几部影片；
 *    0 结果时留在向导里给出可点的排查项，而不是把人丢进空白首页；
 *  · 「稍后设置」仍由首页起步清单接管，不会出现跳过后再也找不到入口。
 */
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { state } from '../state.js'
import { t, setLang, LANGUAGES, i18nState } from '../i18n/index.js'
import { fsList, fsMounts, getServerInfo, putConfig, setupDone, extStatus, listMovies } from '../api.js'
import { prettyHostPath, toast } from '../utils.js'
import { useTasks } from '../composables/useTasks.js'
import { setIntent } from '../composables/useGuide.js'
import { openExtInstall } from '../state.js'

const tasks = useTasks()

const step = ref(1)                 // 1=语言 2=目录 3=目标 4=结果确认
const phase = ref('ready')          // ready | scanning | found | empty
const foundCount = ref(0)
const paths = reactive([])
const newPath = ref('')
const saving = ref(false)
const starting = ref(false)

/* 环境：容器部署下目录选择器不可用，令牌取法也不同 */
const env = reactive({ inContainer: false, urls: [], loaded: false })

/* 容器里已挂载的目录（宿主机 → 容器），用来一键加入媒体库 */
const mounts = ref([])

/* 基础组件检测：ffmpeg 缺失时完成页提示 + 完成后带「去安装」的常驻 toast。
 * 预览缩略图 / 视频补封面 / 浏览器播放转封装 / 内嵌字幕探测都依赖 ffmpeg，
 * 打包版不内置（体积考虑），首次使用最容易在这里卡住。 */
const ffMissing = ref(false)
const extChecking = ref(false)

async function checkExt() {
  extChecking.value = true
  try {
    const r = await extStatus()
    const ff = (r.components || []).find((c) => c.id === 'ffmpeg')
    ffMissing.value = !ff || ff.state !== 'ready'
  } catch (e) { ffMissing.value = false }
  finally { extChecking.value = false }
}

onMounted(async () => {
  checkExt()
  try {
    const info = await getServerInfo()
    env.inContainer = !!info.in_container
    // 只展示手机真能连上的地址（回环地址对手机没意义）
    env.urls = (info.access_urls || []).filter((u) => !/(^|[/:])(127\.0\.0\.1|localhost|0\.0\.0\.0)(:|\/|$)/i.test(u))
  } catch (e) { /* 未登录 / 离线：按桌面形态处理 */ }
  finally { env.loaded = true }

  if (env.inContainer) {
    try {
      const m = await fsMounts()
      mounts.value = (m.items || []).filter((x) => x.target && x.target !== '/data')
      // 只有一个挂载点时直接预填，多个时让用户自己点（避免误选）
      if (mounts.value.length === 1) newPath.value = mounts.value[0].target
      else if (!mounts.value.length) newPath.value = '/media'
    } catch (e) {
      newPath.value = '/media'
    }
  }
})

/* 宿主机路径美化用 utils 里的 prettyHostPath（设置页共用同一实现） */

/* 第 3 步问的是「想先做成什么」，用意图决定后续提示；认证策略不在这里改。 */
const intent = ref('basic')          // basic | rich | remote
const exposure = computed(() => intent.value === 'remote' ? 'public' : 'private')
const intents = computed(() => [
  { key: 'basic', icon: '▶', title: t('guide.intentBasic'), hint: t('guide.intentBasicHint') },
  { key: 'rich', icon: '▦', title: t('guide.intentRich'), hint: t('guide.intentRichHint') },
  { key: 'remote', icon: '⌁', title: t('guide.intentRemote'), hint: t('guide.intentRemoteHint') },
])

/* 语言：直接切换全局语言，无需等待保存 */
function chooseLang(code) { setLang(code) }

/* 目录浏览器（仅桌面可用：容器内看到的是容器文件系统，选不到宿主机目录） */
const browser = reactive({ open: false, path: '', dirs: [], loading: false })
async function browse(p) {
  try {
    browser.loading = true
    const d = await fsList(p || '')
    browser.path = d.path || ''
    browser.dirs = d.dirs || []
    browser.open = true
  } catch (e) { toast(e.message, 'err') }
  finally { browser.loading = false }
}
function pickDir() {
  if (browser.path) newPath.value = browser.path
  browser.open = false
}
function addPath() {
  const v = newPath.value.trim()
  if (!v) return
  if (paths.includes(v)) {
    if (!env.inContainer) toast(t('settings.dirExists'), 'err')
    return
  }
  paths.push(v)
  // 清空输入框：容器里常常挂了多个目录（/media、/media2、/mnt/nas），
  // 停留在上一次的值会让人以为"只能加一个"。
  newPath.value = ''
}

/** 点一下已挂载目录即加入（容器里最常用的路径） */
function addMount(target) {
  const v = String(target || '').trim()
  if (!v || paths.includes(v)) return
  paths.push(v)
  if (newPath.value.trim() === v) newPath.value = ''
}

function removePath(i) { paths.splice(i, 1) }

const canNext = computed(() => {
  if (step.value === 1) return true
  if (step.value === 2) return paths.length > 0
  return true
})

const scanPct = computed(() => {
  const s = state.task.scan
  return s.total > 0 ? `${Math.min(100, Math.round(s.done / s.total * 100))}%` : '0%'
})
const scanHint = computed(() => state.task.scan.hint || null)
const troubleItems = computed(() => [
  { title: t('guide.troubleDir'), desc: t('guide.troubleDirDesc') },
  { title: t('guide.troubleSize'), desc: t('guide.troubleSizeDesc') },
  { title: t('guide.troubleExt'), desc: t('guide.troubleExtDesc') },
  { title: t('guide.troubleKeyword'), desc: t('guide.troubleKeywordDesc') },
])

/** 保存基础配置。每次点「保存并开始扫描」都重存一次，
 *  这样 0 结果后「返回修改目录」改过的路径不会被旧值覆盖。 */
async function saveConfig() {
  await putConfig({
    library: { paths: paths.slice() },
    // 只记录"这台服务会被谁访问"，用于后续提示（不直接改认证策略）
    server: { exposure: exposure.value },
  })
  await setupDone()
  state.config = state.config || {}
  state.config.library = Object.assign({}, state.config.library, { paths: paths.slice() })
  state.config.server = Object.assign({}, state.config.server, { setup_done: true, exposure: exposure.value })
  setIntent(intent.value)
}

/** 等后台扫描跑完；进度状态由 App.vue 的 useTasks 统一轮询维护。 */
function waitScanDone(timeoutMs = 10 * 60 * 1000) {
  return new Promise((resolve) => {
    const startedAt = Date.now()
    let stopped = false
    const stop = watch(() => state.task.scan.running, (running) => {
      if (!running) cleanup()
    })
    const timer = setInterval(() => {
      if (Date.now() - startedAt > timeoutMs) cleanup()
    }, 1000)
    function cleanup() {
      if (stopped) return
      stopped = true
      stop()
      clearInterval(timer)
      resolve()
    }
  })
}

async function runAndCheck() {
  phase.value = 'scanning'
  starting.value = true
  const ok = await tasks.runScan({})
  if (!ok) { phase.value = 'ready'; starting.value = false; return }
  await waitScanDone()
  starting.value = false
  try {
    const r = await listMovies({ page: 1, page_size: 1 })
    foundCount.value = Number(r.total) || 0
  } catch (e) { foundCount.value = 0 }
  phase.value = foundCount.value > 0 ? 'found' : 'empty'
  window.dispatchEvent(new CustomEvent('avm-refresh'))
}

async function finish(scan = true) {
  if (!paths.length) { toast(t('onboarding.addDirHint'), 'warn'); return }
  saving.value = true
  try {
    await saveConfig()
    step.value = 4
    if (ffMissing.value) {
      // 常驻 toast + 「去安装」：引导结束后一步跳到扩展组件页
      toast(t('onboarding.extMissing'), 'warn', 0,
        { label: t('detail.goInstall'), run: () => openExtInstall('ffmpeg') })
    }
    if (!scan) { enterApp(); return }
    await runAndCheck()
  } catch (e) {
    toast(e.message, 'err')
    phase.value = 'ready'
  } finally {
    saving.value = false
  }
}

function enterGallery() { state.onboarded = true; state.view = 'gallery' }
function enterApp() { state.onboarded = true; state.view = 'home' }
function backToDir() { step.value = 2; phase.value = 'ready' }
function retry() { runAndCheck() }

async function skip() {
  // 「稍后设置」：仍写 setup_done 避免每次刷新都弹向导；空库首页会自动
  // 用起步清单接管，不会出现点了跳过就再也找不到入口。
  state.config = state.config || {}
  state.config.server = Object.assign({}, state.config.server, { setup_done: true })
  state.onboarded = true
  // 已经选过目录就顺手保存，别让用户白选一遍
  if (paths.length) {
    try {
      await putConfig({ library: { paths: paths.slice() }, server: { exposure: exposure.value } })
      state.config.library = Object.assign({}, state.config.library, { paths: paths.slice() })
    } catch (e) { toast(e.message, 'err') }
  }
  setupDone().catch(() => { /* 后端不可达时忽略，下次启动会重新引导 */ })
}

/* 容器里查看令牌的命令（服务名让用户自行替换，避免写死 avm） */
const tokenCmd = 'docker compose exec <服务名> cat /data/access_token.txt'
async function copyCmd() {
  try { await navigator.clipboard.writeText(tokenCmd); toast(t('settings.copied'), 'ok') }
  catch (e) { toast(t('settings.copyFailed'), 'err') }
}
</script>

<template>
  <div class="ob-mask">
    <div class="ob-card">
      <!-- 进度指示 -->
      <div v-if="step < 4" class="ob-steps">
        <span :class="['ob-step', step >= 1 ? 'active' : '']">1 · {{ t('onboarding.lang') }}</span>
        <span class="ob-sep">›</span>
        <span :class="['ob-step', step >= 2 ? 'active' : '']">2 · {{ t('onboarding.dir') }}</span>
        <span class="ob-sep">›</span>
        <span :class="['ob-step', step >= 3 ? 'active' : '']">3 · {{ t('guide.intentTitle') }}</span>
      </div>
      <div v-else class="ob-steps">
        <span class="ob-step active">✓ {{ t('onboarding.done') }}</span>
      </div>

      <h2 class="ob-title">{{ step === 4 ? t('guide.readyTitle') : t('onboarding.title') }}</h2>
      <p v-if="step < 4" class="ob-sub">{{ t('onboarding.subtitle') }}</p>
      <p v-else class="ob-sub">{{ t('guide.welcomeSub') }}</p>

      <!-- 步骤1：语言 -->
      <div v-if="step === 1" class="ob-body">
        <div class="ob-lang-grid">
          <button
            v-for="l in LANGUAGES"
            :key="l.code"
            :class="['ob-lang', i18nState.lang === l.code ? 'sel' : '']"
            @click="chooseLang(l.code)"
          >{{ l.label }}</button>
        </div>
      </div>

      <!-- 步骤2：媒体库目录 -->
      <div v-else-if="step === 2" class="ob-body">
        <p class="ob-tip">{{ t('onboarding.dirTip') }}</p>
        <p v-if="env.inContainer" class="ob-tip ob-tip-warn">{{ t('onboarding.containerDirTip') }}</p>

        <!-- 容器：把已挂载的目录列出来，点一下即可加入（支持多个） -->
        <div v-if="env.inContainer && mounts.length" class="ob-mounts">
          <span class="ob-mounts-title">{{ t('onboarding.detectedMounts') }}</span>
          <div class="ob-mount-chips">
            <button
              v-for="m in mounts"
              :key="m.target"
              :class="['btn', 'tiny', paths.includes(m.target) ? 'primary' : 'ghost']"
              @click="addMount(m.target)"
            >
              📁 {{ m.target }}
              <span v-if="m.root && m.root !== '/' && prettyHostPath(m.root) !== m.target" class="ob-mount-src">
                ← {{ prettyHostPath(m.root) }}
              </span>
            </button>
          </div>
        </div>

        <!-- 已添加的目录：始终显示（含数量），避免加完看不到结果 -->
        <div class="ob-added">
          <div class="ob-added-head">
            <span class="ob-added-title">{{ t('onboarding.addedDirs', { n: paths.length }) }}</span>
            <span v-if="paths.length" class="ob-added-hint">{{ t('onboarding.addedDirsHint') }}</span>
          </div>
          <ul v-if="paths.length" class="ob-path-list">
            <li v-for="(p, i) in paths" :key="i">
              <span class="pi">📁</span>
              <span class="pp ellipsis" :title="p">{{ p }}</span>
              <button class="btn tiny ghost" @click="removePath(i)">{{ t('settings.remove') }}</button>
            </li>
          </ul>
          <p v-else class="ob-added-empty">{{ env.inContainer ? t('onboarding.containerNoDir') : t('onboarding.noDirYet') }}</p>
        </div>

        <div class="hstack">
          <input
            v-model="newPath"
            :placeholder="env.inContainer ? t('onboarding.containerPathPh') : t('settings.scanDirPh')"
            @keydown.enter="addPath"
          />
          <!-- 容器内隐藏目录浏览：看到的是容器文件系统，选不到宿主机目录，只会误导 -->
          <button v-if="!env.inContainer" class="btn" @click="browse('')" :disabled="browser.loading">{{ t('settings.browseDir') }}</button>
          <button class="btn primary" @click="addPath">{{ t('common.add') }}</button>
        </div>
        <p v-if="env.inContainer" class="ob-tip ob-tip-muted">{{ t('onboarding.multiDirHint') }}</p>
      </div>

      <!-- 步骤3：只问「想先做成哪件事」，用它决定后续提示，不动认证设置 -->
      <div v-else-if="step === 3" class="ob-body">
        <p class="ob-tip">{{ t('guide.intentTitle') }}</p>
        <div class="ob-opts">
          <div
            v-for="o in intents"
            :key="o.key"
            :class="['ob-opt', intent === o.key ? 'sel' : '']"
            @click="intent = o.key"
          >
            <div class="ob-opt-title">{{ o.icon }} {{ o.title }}</div>
            <div class="ob-opt-hint">{{ o.hint }}</div>
          </div>
        </div>
        <p class="ob-tip ob-tip-muted">{{ t('guide.intentNote') }}</p>
      </div>

      <!-- 结果确认：不再只是信息清单，会等扫描真正出结果 -->
      <div v-else class="ob-body">
        <template v-if="phase === 'ready'">
          <p class="ob-tip">{{ t('onboarding.doneTitle') }}</p>

          <div class="ob-kv">
            <div class="ob-k">{{ t('onboarding.doneAddr') }}</div>
            <div class="ob-v mono">
              <div v-if="env.urls.length">
                <div v-for="u in env.urls" :key="u">{{ u }}</div>
              </div>
              <div v-else class="muted">{{ t('onboarding.doneAddrLocal') }}</div>
            </div>
          </div>

          <div class="ob-kv">
            <div class="ob-k">{{ t('onboarding.doneToken') }}</div>
            <div class="ob-v">
              <template v-if="env.inContainer">
                {{ t('onboarding.doneTokenDocker') }}
                <div class="hstack" style="margin-top:6px">
                  <code class="mono ob-cmd">{{ tokenCmd }}</code>
                  <button class="btn tiny ghost" @click="copyCmd">{{ t('settings.copy') }}</button>
                </div>
              </template>
              <template v-else>{{ t('onboarding.doneTokenDesk') }}</template>
            </div>
          </div>

          <div class="ob-kv">
            <div class="ob-k">{{ t('onboarding.donePhone') }}</div>
            <div class="ob-v">{{ t('onboarding.donePhoneHint') }}</div>
          </div>

          <p v-if="exposure === 'public'" class="ob-tip ob-tip-warn">{{ t('onboarding.twofaWarn') }}</p>
          <p v-if="ffMissing && !extChecking" class="ob-tip ob-tip-warn">{{ t('onboarding.extMissing') }}</p>
          <p v-else-if="extChecking" class="ob-tip muted">{{ t('onboarding.extChecking') }}</p>
        </template>

        <template v-else-if="phase === 'scanning'">
          <p class="ob-tip">{{ t('guide.scanning') }}</p>
          <div class="ob-scan">
            <div class="ob-scan-num">{{ state.task.scan.done }} / {{ state.task.scan.total }}</div>
            <div class="ob-scan-bar"><i :style="{ width: scanPct }"></i></div>
            <p class="muted ellipsis">{{ state.task.scan.message }}</p>
          </div>
        </template>

        <template v-else-if="phase === 'found'">
          <div class="ob-result">
            <div class="ob-result-ic">✓</div>
            <h3>{{ t('guide.foundTitle', { n: foundCount }) }}</h3>
            <p>{{ t('guide.foundDesc') }}</p>
          </div>
        </template>

        <template v-else>
          <p class="ob-tip ob-tip-warn">{{ t('guide.noResultTitle') }}</p>
          <p class="ob-tip">{{ t('guide.noResultDesc') }}</p>
          <ul class="ob-trouble">
            <li v-for="(x, i) in troubleItems" :key="i">
              <b>{{ x.title }}</b><span>{{ x.desc }}</span>
            </li>
          </ul>
          <p v-if="scanHint" class="ob-tip ob-tip-muted">
            {{ t('guide.troubleDir') }}: {{ (scanHint.checked_paths || []).join(' / ') }} · {{ scanHint.min_size_mb }} MB
          </p>
        </template>
      </div>

      <!-- 底部操作 -->
      <div class="ob-foot">
        <button v-if="step > 1 && step < 4" class="btn ghost" @click="step--">{{ t('common.back') }}</button>
        <span class="grow"></span>
        <button v-if="step < 4" class="btn ghost" @click="skip">{{ t('guide.laterBtn') }}</button>

        <button v-if="step < 3" class="btn primary" :disabled="!canNext" @click="step++">{{ t('common.next') }}</button>
        <button v-else-if="step === 3" class="btn primary" @click="step++">{{ t('common.next') }}</button>

        <template v-else-if="phase === 'ready'">
          <button class="btn primary" :disabled="saving || starting" @click="finish(true)">{{ t('guide.saveAndScan') }}</button>
          <button class="btn ghost" :disabled="saving" @click="finish(false)">{{ t('onboarding.scanLater') }}</button>
        </template>
        <template v-else-if="phase === 'scanning'">
          <button class="btn ghost" @click="enterApp">{{ t('guide.enterApp') }}</button>
        </template>
        <template v-else-if="phase === 'found'">
          <button class="btn primary" @click="enterGallery">{{ t('guide.enterGallery') }}</button>
        </template>
        <template v-else>
          <button class="btn ghost" @click="backToDir">{{ t('guide.backToDir') }}</button>
          <button class="btn" :disabled="starting" @click="retry">{{ t('guide.retryScan') }}</button>
          <button class="btn primary" @click="enterApp">{{ t('guide.enterApp') }}</button>
        </template>
      </div>
    </div>

    <!-- 目录浏览器（仅桌面） -->
    <div v-if="browser.open" class="ob-browse-mask" @click.self="browser.open = false">
      <div class="ob-browse">
        <div class="ob-browse-head">
          <button class="btn tiny ghost" @click="browse(browser.path ? browser.path.replace(/[\\/][^\\/]+$/, '') : '')">↑ {{ t('onboarding.rootDir') }}</button>
          <span class="mono ellipsis">{{ browser.path || '/' }}</span>
          <button class="btn tiny ghost" @click="browser.open = false">✕</button>
        </div>
        <div v-if="!browser.dirs.length" class="muted pad">{{ t('onboarding.noSubDir') }}</div>
        <div v-else class="ob-browse-body">
          <div
            v-for="d in browser.dirs"
            :key="d.path"
            :class="['ob-dir', paths.includes(d.path) ? 'added' : '']"
            @click="browse(d.path)"
          >
            <span class="ellipsis">📁 {{ d.name }}</span>
            <span v-if="paths.includes(d.path)" class="ob-dir-tag">{{ t('onboarding.alreadyAdded') }}</span>
          </div>
        </div>
        <div class="ob-browse-foot">
          <button class="btn" @click="pickDir" :disabled="!browser.path">{{ t('common.select') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 注意：本文件曾使用 --bg-panel / --text / --muted / --border / --accent 等
   旧变量名，它们已在 tokens.css 里被 --c-* 取代、不再有定义，于是全部
   fallback 到深色兜底色。浅色主题下表现为「深色卡片 + 继承来的深色文字」，
   已添加的路径完全看不清。现统一改用现行 design token。 */
/* 引导页跟随应用主题（默认暗色，见 index.html 的 data-theme 与 state.js） */
.ob-mask {
  position: fixed; inset: 0; z-index: 1000;
  background: var(--c-overlay);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  display: flex; align-items: center; justify-content: center;
  padding: 16px;
}
.ob-card {
  width: min(560px, 100%);
  max-height: 90vh; overflow: auto;
  background: var(--c-surface);
  color: var(--c-text);
  border: 1px solid var(--c-line);
  border-radius: var(--r-lg);
  padding: 22px 24px 18px;
  box-shadow: var(--sh-4);
}
.ob-steps { display: flex; align-items: center; gap: var(--sp-2); font-size: var(--fs-md); color: var(--c-text-2); margin-bottom: 14px; flex-wrap: wrap; }
.ob-step.active { color: var(--c-primary); font-weight: 600; }
.ob-sep { opacity: .5; }
.ob-title { margin: 0 0 4px; font-size: var(--fs-xl); color: var(--c-text); }
.ob-sub { margin: 0 0 18px; font-size: var(--fs-md); color: var(--c-text-2); }
.ob-body { min-height: 180px; }
.ob-lang-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.ob-lang {
  padding: 14px; border-radius: var(--r-md); cursor: pointer;
  background: var(--c-surface-2); color: var(--c-text);
  border: 1px solid var(--c-line); font-size: var(--fs-lg); text-align: center;
}
.ob-lang.sel { border-color: var(--c-primary); box-shadow: 0 0 0 3px var(--c-primary-soft); color: var(--c-primary); }
.ob-tip { font-size: var(--fs-md); color: var(--c-text-2); margin: 0 0 10px; }
.ob-tip-warn { color: var(--c-warn, #c08a2e); }
.ob-tip-muted { color: var(--c-text-2); font-size: var(--fs-sm); }
/* 已添加目录：标题 + 数量 + 列条（多了可滚动） */
.ob-added { margin: 4px 0 10px; }
.ob-added-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; }
.ob-added-title { font-size: var(--fs-md); font-weight: 600; color: var(--c-text); }
.ob-added-hint { font-size: var(--fs-sm); color: var(--c-text-2); }
.ob-added-empty { margin: 0; padding: 10px 0; font-size: var(--fs-sm); color: var(--c-text-2); }
.ob-path-list {
  list-style: none; margin: 0; padding: 0;
  max-height: 176px; overflow-y: auto;
  border: 1px solid var(--c-line); border-radius: var(--r-sm);
  background: var(--c-surface-2);
}
.ob-path-list li {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: 8px 10px; border-bottom: 1px dashed var(--c-line);
}
.ob-path-list li:last-child { border-bottom: 0; }
/* 路径文字显式定色：此前未设 color，浅色主题下会继承到深色正文色而看不见 */
.ob-path-list .pp { flex: 1; min-width: 0; color: var(--c-text); font-size: var(--fs-md); }
.ob-foot { display: flex; align-items: center; gap: 10px; margin-top: 18px; flex-wrap: wrap; }
.ob-foot .grow { flex: 1; }

/* 访问方式：两张可选卡片 */
.ob-opts { display: flex; flex-direction: column; gap: var(--sp-2, 8px); }
.ob-opt {
  padding: 12px 14px; border: 1px solid var(--c-line); border-radius: var(--r-md, 10px);
  cursor: pointer; transition: border-color .15s, background .15s;
}
.ob-opt:hover { border-color: var(--c-primary); }
.ob-opt.sel { border-color: var(--c-primary); box-shadow: 0 0 0 3px var(--c-primary-soft); }
.ob-opt-title { font-weight: 600; }
.ob-opt-hint { color: var(--c-text-2); font-size: var(--fs-sm); margin-top: 2px; }

/* 容器：已挂载目录 */
.ob-mounts { margin-bottom: 12px; }
.ob-mounts-title { display: block; font-size: var(--fs-sm); color: var(--c-text-2); margin-bottom: 6px; }
.ob-mount-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.ob-mount-src { color: var(--c-text-2); font-size: 11px; margin-left: 4px; }

/* 完成页信息行 */
.ob-kv { display: flex; gap: 10px; padding: 7px 0; border-bottom: 1px dashed var(--c-line); }
.ob-kv:last-of-type { border-bottom: 0; }
.ob-k { flex: 0 0 72px; color: var(--c-text-2); font-size: var(--fs-sm); }
.ob-v { flex: 1; min-width: 0; word-break: break-all; color: var(--c-text); }
.ob-cmd { padding: 4px 8px; background: var(--c-surface-2); border-radius: 6px; font-size: 12px; }

.hstack { display: flex; gap: var(--sp-2); }
.hstack input {
  flex: 1; min-width: 0; padding: 9px 10px; border-radius: var(--r-sm);
  background: var(--c-surface-2); color: var(--c-text);
  border: 1px solid var(--c-line);
}
.btn {
  padding: 9px 14px; border-radius: var(--r-sm); cursor: pointer;
  background: var(--c-surface-2); color: var(--c-text);
  border: 1px solid var(--c-line); white-space: nowrap;
}
.btn.primary { background: var(--c-primary); color: #fff; border-color: transparent; }
.btn.primary:hover { background: var(--c-primary-h); }
.btn.ghost { background: transparent; }
.btn.tiny { padding: 4px 8px; font-size: var(--fs-sm); }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.muted { color: var(--c-text-2); }
.pad { padding: var(--sp-4); }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.ob-browse-mask {
  position: fixed; inset: 0; z-index: 1100;
  background: var(--c-overlay); display: flex; align-items: center; justify-content: center; padding: 16px;
}
.ob-browse {
  width: min(480px, 100%); background: var(--c-surface); color: var(--c-text);
  border: 1px solid var(--c-line); border-radius: var(--r-md); overflow: hidden;
}
.ob-browse-head { display: flex; align-items: center; gap: var(--sp-2); padding: 10px 12px; border-bottom: 1px solid var(--c-line); }
.ob-browse-head .ellipsis { flex: 1; min-width: 0; color: var(--c-text); }
.ob-browse-body { max-height: 320px; overflow: auto; padding: 6px 0; }
.ob-dir { display: flex; align-items: center; gap: var(--sp-2); padding: 9px 14px; cursor: pointer; color: var(--c-text); }
.ob-dir:hover { background: var(--c-surface-3); }
.ob-dir.added { opacity: .72; }
.ob-dir-tag {
  margin-left: auto; flex: 0 0 auto;
  padding: 1px 6px; border-radius: 999px;
  font-size: 11px; color: var(--c-primary);
  background: var(--c-primary-soft);
}
.ob-browse-foot { display: flex; justify-content: flex-end; gap: var(--sp-2); padding: 10px 12px; border-top: 1px solid var(--c-line); }

/* 结果确认页：扫描进度 / 找到 N 部 / 0 结果排查 */
.ob-scan { padding: 8px 0; }
.ob-scan-num { font-size: var(--fs-lg); font-weight: 600; text-align: center; color: var(--c-text); margin-bottom: 8px; }
.ob-scan-bar { height: 8px; border-radius: 999px; background: var(--c-surface-2); overflow: hidden; }
.ob-scan-bar i { display: block; height: 100%; background: var(--c-primary); border-radius: 999px; transition: width .3s; }
.ob-scan p { margin: 8px 0 0; font-size: var(--fs-sm); }
.ob-result { text-align: center; padding: 10px 0 6px; }
.ob-result-ic {
  width: 48px; height: 48px; margin: 0 auto 10px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: var(--c-primary); color: #fff; font-size: 24px;
}
.ob-result h3 { margin: 0 0 6px; font-size: var(--fs-lg); color: var(--c-text); }
.ob-result p { margin: 0; font-size: var(--fs-md); color: var(--c-text-2); }
.ob-trouble {
  list-style: none; margin: 0 0 10px; padding: 0;
  max-height: 210px; overflow-y: auto; display: grid; gap: 8px;
}
.ob-trouble li {
  display: flex; flex-direction: column; gap: 2px;
  padding: 8px 10px; border: 1px solid var(--c-line); border-radius: var(--r-sm);
  background: var(--c-surface-2); font-size: var(--fs-sm);
}
.ob-trouble li b { color: var(--c-text); }
.ob-trouble li span { color: var(--c-text-2); line-height: 1.5; }
</style>
