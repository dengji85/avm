import { reactive, ref, computed, watch } from 'vue'

/**
 * 上手引导的轻量状态层。
 *
 * 客观进度（影片数、目录、组件是否就绪）一律从接口实时推导，不落在这里；
 * 这里只记「哪条提示看过 / 用户把清单收起来了 / 设置是否只看基础项」。
 * 存 localStorage 而不是后端：换浏览器是另一台设备，理应重新给一次提示。
 */
const KEY = 'avm.guide.v1'

function load() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '{}')
    return {
      done: raw.done && typeof raw.done === 'object' ? raw.done : {},
      dismissed: raw.dismissed && typeof raw.dismissed === 'object' ? raw.dismissed : {},
      checklistHidden: !!raw.checklistHidden,
      basicOnly: !!raw.basicOnly,
      intent: ['basic', 'rich', 'remote'].includes(raw.intent) ? raw.intent : 'basic',
    }
  } catch (e) {
    return { done: {}, dismissed: {}, checklistHidden: false, basicOnly: false, intent: 'basic' }
  }
}

export const guideState = reactive(load())

watch(guideState, (v) => {
  try { localStorage.setItem(KEY, JSON.stringify(v)) } catch (e) { /* 隐私模式下忽略 */ }
}, { deep: true })

export const checklistHidden = computed(() => guideState.checklistHidden)
export const basicOnly = computed(() => guideState.basicOnly)
/** 首启向导第 3 步选的目标，用于给起步清单里的进阶玩法排序 */
export const guideIntent = computed(() => guideState.intent || 'basic')

export function setChecklistHidden(v) { guideState.checklistHidden = !!v }
export function setBasicOnly(v) { guideState.basicOnly = !!v }
export function setIntent(v) { guideState.intent = v }

export function isDone(k) { return !!guideState.done[k] }
export function markDone(k) { guideState.done[k] = true }
export function isDismissed(k) { return !!guideState.dismissed[k] }
export function dismiss(k) { guideState.dismissed[k] = true }

/** 当前正在显示的一次性页面提示（同一时刻只显示一条） */
export const activeTip = ref('')
export function showTip(k) {
  if (isDismissed(k) || isDone(k)) return false
  activeTip.value = k
  return true
}
export function closeTip(k) {
  dismiss(k)
  if (activeTip.value === k) activeTip.value = ''
}

/** 重跑首启向导时清掉「已看过」的标记，让场景提示重新生效 */
export function resetGuide() {
  guideState.done = {}
  guideState.dismissed = {}
  guideState.checklistHidden = false
  guideState.intent = 'basic'
  activeTip.value = ''
}
