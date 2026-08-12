import { reactive, computed } from 'vue'
import zhCN from './locales/zh-CN'
import zhTW from './locales/zh-TW'
import ja from './locales/ja'
import en from './locales/en'

export const LANGUAGES = [
  { code: 'zh-CN', label: '简体中文' },
  { code: 'zh-TW', label: '繁體中文' },
  { code: 'ja', label: '日本語' },
  { code: 'en', label: 'English' },
]

const DICTS = { 'zh-CN': zhCN, 'zh-TW': zhTW, ja, en }
const PREF_KEY = 'avm.lang'

function detectInitial() {
  const saved = localStorage.getItem(PREF_KEY)
  if (saved && DICTS[saved]) return saved
  const nav = (navigator.language || 'zh-CN').toLowerCase()
  if (nav.startsWith('zh')) return nav.includes('tw') || nav.includes('hk') ? 'zh-TW' : 'zh-CN'
  if (nav.startsWith('ja')) return 'ja'
  if (nav.startsWith('en')) return 'en'
  return 'zh-CN'
}

export const i18nState = reactive({
  lang: detectInitial(),
  // version 用于触发依赖此值的模板重新渲染
  version: 0,
})

function lookup(dict, key) {
  if (!dict) return undefined
  if (dict[key] !== undefined) return dict[key]
  // 支持点号嵌套 key，如 'settings.title'
  return key.split('.').reduce((o, k) => (o == null ? undefined : o[k]), dict)
}

/**
 * 翻译函数。在模板中调用时会通过 i18nState.version 建立响应式依赖，
 * 切换语言后自动重渲染。
 */
export function t(key, arg2, arg3) {
  // 触碰 version 以建立响应式依赖
  void i18nState.version
  // 兼容两种调用：
  //   t('key', { params })            —— 第二参为对象，视为参数
  //   t('key', fallback, { params })  —— 第二参为字符串 fallback，第三参为参数
  //   t('key', fallback)              —— 仅有 fallback
  let fallback = key
  let params = undefined
  if (typeof arg2 === 'object' && arg2 !== null) {
    params = arg2
  } else {
    fallback = arg2 !== undefined ? arg2 : key
    params = arg3
  }
  const dict = DICTS[i18nState.lang] || DICTS['zh-CN']
  let val = lookup(dict, key)
  if (val === undefined && i18nState.lang !== 'zh-CN') val = lookup(DICTS['zh-CN'], key)
  if (val === undefined) val = fallback
  if (typeof val === 'string' && params && typeof params === 'object') {
    val = val.replace(/\{(\w+)\}/g, (m, k) => (params[k] !== undefined ? params[k] : m))
  }
  return val
}

export function setLang(code) {
  if (!DICTS[code]) return
  i18nState.lang = code
  i18nState.version++
  try { localStorage.setItem(PREF_KEY, code) } catch (e) {}
  document.documentElement.setAttribute('lang', code)
}

export function useI18n() {
  return {
    t,
    lang: computed(() => i18nState.lang),
    languages: LANGUAGES,
    setLang,
  }
}
