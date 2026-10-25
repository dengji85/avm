import { computed, reactive, ref, watch } from 'vue'
import { state } from '../state.js'
import { getMovie } from '../api.js'
import { toast, confirmDialog } from '../utils.js'
import { useAiAssistant } from './useAiAssistant.js'

/**
 * AI 助手的全局单例状态。
 *
 * 之前工作台长在详情页里，只有打开详情才能用；现在它是一块可以浮在任意页面
 * 之上的窗口，所以状态必须脱离任何单个组件：
 * - 影片对象只保留一份（详情页与浮窗共用同一个对象，AI 写回后两处同时更新）；
 * - 打开 / 关闭 / 视图切换 / 卡片折叠 都放在这里，任何页面调用 openWorkspace()
 *   即可弹出，不需要先打开详情页。
 */

/** 当前上下文影片（与详情页共用同一个对象） */
const movie = ref(null)

/** 后端 tags 可能是数组或逗号分隔字符串，统一规整成数组 */
function normalize(m) {
  if (!m) return null
  let tags = m.tags
  if (typeof tags === 'string') tags = tags ? tags.split(',').map((s) => s.trim()).filter(Boolean) : []
  else if (!Array.isArray(tags)) tags = []
  m.tags = tags
  return m
}

const ai = useAiAssistant({
  movieId: computed(() => state.currentId),
  movie,
  toast,
  confirm: confirmDialog,
  onChange: () => refreshMovie(),
})

export const aiWorkspace = reactive({
  open: false,
  view: 'tasks',
  // 卡片折叠状态：默认只展开「剧情简介」，其余一眼看到摘要即可点开
  cards: { plot: true, tags: false, subs: false, ask: false, similar: false },
  ai,
  movie,
  openWorkspace,
  close,
  toggleView,
  showPrompts,
  seekToRef,
  syncMovie,
  refreshMovie,
  ensureStatus,
  ensureAsr,
  gotoMovie,
})

/** 重新拉取当前影片；已在手上的对象就地更新，保证详情页与浮窗同步 */
async function refreshMovie() {
  const id = state.currentId
  if (!id) {
    movie.value = null
    return null
  }
  try {
    const m = normalize(await getMovie(id))
    if (!m) return null
    if (movie.value && movie.value.id === m.id) Object.assign(movie.value, m)
    else movie.value = m
    ai.loadTracks()
    return movie.value
  } catch (e) {
    /* 失败就让卡片里已有的错误提示接管，不额外打扰 */
    return null
  }
}

/** 详情页已经拿到完整数据：直接共享同一个对象，避免重复请求与两份状态 */
function syncMovie(m) {
  if (!m || !m.id) return
  normalize(m)
  movie.value = m
}

let statusProbed = false
/**
 * 入口按钮要按「AI 是否启用」高亮，首次用到时探测一次即可。
 * 探测失败（例如登录前、后端刚重启）不锁死，允许下次再试。
 */
async function ensureStatus() {
  if (statusProbed) return
  statusProbed = true
  await ai.refreshStatus()
  if (!ai.ready) statusProbed = false
}

let asrProbed = false
/** 语音提取能力（组件/模型是否就位）是全局的，探测一次即可；详情页据此决定按钮能否点 */
function ensureAsr() {
  if (asrProbed) return
  asrProbed = true
  ai.loadAsrStatus()
}

/** 打开工作台：m 可传详情页已加载的影片对象，省一次请求 */
function openWorkspace(m) {
  if (m) syncMovie(m)
  aiWorkspace.open = true
  aiWorkspace.cards.plot = true
  statusProbed = true
  ai.refreshStatus()
  hydrate()
}

function close() {
  aiWorkspace.open = false
  // 关闭后不再后台轮询对白提取进度；下次打开会重新读一次状态
  ai.stopDialogPoll()
}

/** 打开工作台要用的数据：字幕轨、问答历史、ASR 状态、相似推荐 */
async function hydrate() {
  if (!state.currentId) return
  if (!movie.value || movie.value.id !== state.currentId) await refreshMovie()
  else ai.loadTracks()
  ai.loadAskHistory()
  ai.loadAsrStatus()
  ai.loadDialog(ai.dialog.show ? 20000 : 0)
  if (!ai.similar.items.length) ai.loadSimilar()
}

function toggleView(v) {
  aiWorkspace.view = v || (aiWorkspace.view === 'tasks' ? 'prompts' : 'tasks')
  if (aiWorkspace.view === 'prompts' && !ai.prompts.items.length) ai.loadPrompts()
}

/** 从「调用详情 → 改提示词」直接进提示词视图并定位到那一项 */
function showPrompts(id) {
  aiWorkspace.view = 'prompts'
  const pick = () => ai.startEditPrompt(ai.prompts.items.find((x) => x.id === id) || null)
  if (!ai.prompts.items.length) ai.loadPrompts().then(pick)
  else pick()
}

/** 问答里的时间戳：交给详情页的内嵌播放器跳过去 */
function seekToRef(timeText) {
  const seconds = ai.timeToSeconds(timeText)
  if (!Number.isFinite(seconds) || seconds <= 0 || !state.currentId) return
  state.aiSeek = { id: state.currentId, seconds, n: Date.now() }
}

/** 相似推荐点进去：只换上下文，浮窗留在原地 */
function gotoMovie(id) {
  if (!id || id === state.currentId) return
  state.currentId = id
}

// 切换影片：清掉上一部的结果，浮窗开着就跟过去加载新片
watch(() => state.currentId, (id) => {
  ai.reset()
  movie.value = null
  if (!id) {
    close()
    return
  }
  // 字幕轨 / 语音对白是详情页快捷 AI（写简介 / 问剧情）的前置条件，无论浮窗开没开
  // 都要探测，否则详情页会误判成「这部片没有字幕」，也看不到已经提取过的对白。
  ensureAsr()
  ai.loadTracks()
  ai.loadDialog(0)
  if (aiWorkspace.open) hydrate()
})
