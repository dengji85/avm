import { reactive, computed } from 'vue'
// zh-CN 是默认语言，也是其它语言缺 key 时的兜底字典，必须随主包加载。
// 其余三份字典按需 import：四份全静态塞进首屏会让主 chunk 多出约 450KB 源码。
import zhCN from './locales/zh-CN'

export const LANGUAGES = [
  { code: 'zh-CN', label: '简体中文' },
  { code: 'zh-TW', label: '繁體中文' },
  { code: 'ja', label: '日本語' },
  { code: 'en', label: 'English' },
]

const DICTS = { 'zh-CN': zhCN }
const LOADERS = {
  'zh-TW': () => import('./locales/zh-TW'),
  ja: () => import('./locales/ja'),
  en: () => import('./locales/en'),
}
const LOADING = {}
const PREF_KEY = 'avm.lang'
const VALID = new Set(LANGUAGES.map(l => l.code))

function detectInitial() {
  const saved = localStorage.getItem(PREF_KEY)
  if (saved && VALID.has(saved)) return saved
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

/** 按需载入语言字典；载入中/失败时 t() 会自动回退到 zh-CN，不会显示 key。 */
export function ensureDict(code) {
  if (DICTS[code]) return Promise.resolve(true)
  const loader = LOADERS[code]
  if (!loader) return Promise.resolve(false)
  if (!LOADING[code]) {
    LOADING[code] = loader()
      .then(m => { DICTS[code] = m.default || m; return true })
      .catch(() => false)
      .finally(() => { delete LOADING[code] })
  }
  return LOADING[code]
}

/** 启动时预载当前语言，避免非中文用户首屏闪一下中文。 */
export function initI18n() {
  document.documentElement.setAttribute('lang', i18nState.lang)
  return i18nState.lang === 'zh-CN' ? Promise.resolve() : ensureDict(i18nState.lang)
}

function lookup(dict, key) {
  if (!dict) return undefined
  if (dict[key] !== undefined) return dict[key]
  // 支持点号嵌套 key，如 'settings.title'
  return key.split('.').reduce((o, k) => (o == null ? undefined : o[k]), dict)
}

/**
 * 翻译函数。在模板中调用时会通过 i18nState.version 建立响应式依赖，
 * 切换语言后自动重渲染。保持同步：字典未就绪时回退 zh-CN，就绪后由 setLang
 * 递增 version 触发重渲染。
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

export async function setLang(code) {
  if (!VALID.has(code)) return
  // 先载入字典再切换，避免切过去后短暂显示中文；载入失败则保持 zh-CN 兜底
  if (code !== 'zh-CN') await ensureDict(code)
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
