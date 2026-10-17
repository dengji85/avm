<script setup>
/**
 * 首次启动引导（4 步）：语言 → 媒体库目录 → 访问方式 → 完成。
 *
 * 设计要点：
 *  · **按环境换控件**：容器里看不到宿主机文件系统，目录浏览按钮直接隐藏，
 *    预填约定的挂载点 /media，并说明宿主机目录要在 compose 里挂载；
 *  · **第 3 步只问一个问题**（谁会用），用它决定完成页是否提示两步验证，
 *    不在向导里做认证设置 —— 用户此刻还没有"要保存凭据"的意识，
 *    在这里改认证最容易把人锁在门外；
 *  · 完成页给出访问地址、令牌位置与手机登录方式，用户照做即可。
 */
import { ref, reactive, computed, onMounted } from 'vue'
import { state } from '../state.js'
import { t, setLang, LANGUAGES, i18nState } from '../i18n/index.js'
import { fsList, fsMounts, getServerInfo, putConfig, setupDone } from '../api.js'
import { prettyHostPath, toast } from '../utils.js'
import { useTasks } from '../composables/useTasks.js'

const tasks = useTasks()

const step = ref(1)                 // 1=语言 2=目录 3=访问方式 4=完成
const paths = reactive([])
const newPath = ref('')
const saving = ref(false)
const starting = ref(false)

/* 环境：容器部署下目录选择器不可用，令牌取法也不同 */
const env = reactive({ inContainer: false, urls: [], loaded: false })

/* 容器里已挂载的目录（宿主机 → 容器），用来一键加入媒体库 */
const mounts = ref([])

onMounted(async () => {
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

const exposure = ref('private')     // private | public

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

async function finish(scan = true) {
  if (!paths.length) { toast(t('onboarding.addDirHint'), 'warn'); return }
  saving.value = true
  try {
    await putConfig({
      library: { paths: paths.slice() },
      // 只记录"这台服务会被谁访问"，用于后续提示（不直接改认证策略）
      server: { exposure: exposure.value },
    })
    await setupDone()
    state.config = state.config || {}
    state.config.library = Object.assign({}, state.config.library, { paths: paths.slice() })
    state.config.server = Object.assign({}, state.config.server, { setup_done: true, exposure: exposure.value })
    state.onboarded = true            // 先关遮罩，避免扫描时看不到任务中心
    if (scan) {
      starting.value = true
      await tasks.runScan({})
    }
  } catch (e) {
    toast(e.message, 'err')
  } finally {
    saving.value = false
    starting.value = false
  }
}

function skip() {
  // 跳过引导：立即关闭遮罩（本地优先），再尝试把 setup_done 写回后端
  state.config = state.config || {}
  state.config.server = Object.assign({}, state.config.server, { setup_done: true })
  state.onboarded = true
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
      <div class="ob-steps">
        <span :class="['ob-step', step >= 1 ? 'active' : '']">1 · {{ t('onboarding.lang') }}</span>
        <span class="ob-sep">›</span>
        <span :class="['ob-step', step >= 2 ? 'active' : '']">2 · {{ t('onboarding.dir') }}</span>
        <span class="ob-sep">›</span>
        <span :class="['ob-step', step >= 3 ? 'active' : '']">3 · {{ t('onboarding.access') }}</span>
        <span class="ob-sep">›</span>
        <span :class="['ob-step', step >= 4 ? 'active' : '']">4 · {{ t('onboarding.done') }}</span>
      </div>

      <h2 class="ob-title">{{ t('onboarding.title') }}</h2>
      <p class="ob-sub">{{ t('onboarding.subtitle') }}</p>

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

        <ul v-if="paths.length" class="ob-path-list">
          <li v-for="(p, i) in paths" :key="i">
            <span class="pi">📁</span>
            <span class="pp ellipsis" :title="p">{{ p }}</span>
            <button class="btn tiny ghost" @click="removePath(i)">{{ t('settings.remove') }}</button>
          </li>
        </ul>
        <p v-else class="muted">{{ env.inContainer ? t('onboarding.containerNoDir') : t('onboarding.noDirYet') }}</p>

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

      <!-- 步骤3：访问方式（只问一个问题） -->
      <div v-else-if="step === 3" class="ob-body">
        <p class="ob-tip">{{ t('onboarding.accessTitle') }}</p>
        <div class="ob-opts">
          <div :class="['ob-opt', exposure === 'private' ? 'sel' : '']" @click="exposure = 'private'">
            <div class="ob-opt-title">🏠 {{ t('onboarding.accessPrivate') }}</div>
            <div class="ob-opt-hint">{{ t('onboarding.accessPrivateHint') }}</div>
          </div>
          <div :class="['ob-opt', exposure === 'public' ? 'sel' : '']" @click="exposure = 'public'">
            <div class="ob-opt-title">🌐 {{ t('onboarding.accessPublic') }}</div>
            <div class="ob-opt-hint">{{ t('onboarding.accessPublicHint') }}</div>
          </div>
        </div>
        <p class="ob-tip ob-tip-muted">{{ t('onboarding.accessNote') }}</p>
      </div>

      <!-- 步骤4：完成 -->
      <div v-else class="ob-body">
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
      </div>

      <!-- 底部操作 -->
      <div class="ob-foot">
        <button v-if="step > 1" class="btn ghost" @click="step--">{{ t('common.back') }}</button>
        <span class="grow"></span>
        <button class="btn ghost" @click="skip">{{ t('onboarding.skip') }}</button>
        <button v-if="step < 3" class="btn primary" :disabled="!canNext" @click="step++">{{ t('common.next') }}</button>
        <button v-else-if="step === 3" class="btn primary" @click="step++">{{ t('common.next') }}</button>
        <button v-else class="btn primary" :disabled="!canNext || saving || starting" @click="finish(true)">
          {{ starting ? t('onboarding.scanning') : t('onboarding.startScan') }}
        </button>
        <button v-if="step === 4" class="btn ghost" :disabled="saving || starting" @click="finish(false)">
          {{ t('onboarding.scanLater') }}
        </button>
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
            class="ob-dir"
            @click="browse(d.path)"
          >📁 {{ d.name }}</div>
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
.ob-mask {
  position: fixed; inset: 0; z-index: 1000;
  background: var(--c-overlay);
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
.ob-path-list { list-style: none; margin: 0 0 10px; padding: 0; }
.ob-path-list li { display: flex; align-items: center; gap: var(--sp-2); padding: 6px 0; border-bottom: 1px dashed var(--c-line); }
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
.ob-browse-foot { display: flex; justify-content: flex-end; gap: var(--sp-2); padding: 10px 12px; border-top: 1px solid var(--c-line); }
</style>
