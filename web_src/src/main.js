import { createApp } from 'vue'
import App from './App.vue'
import { t, setLang, initI18n } from './i18n'
import { applySiteTitleFromCache } from './utils.js'

import './styles/tokens.css'
import './styles/base.css'
import './styles/ui.css'
import './styles/layout.css'
import './styles/assistant.css'
import './styles.css'

// 挂载前用缓存的后缀同步定标题，避免刷新时闪过 index.html 的默认标题
applySiteTitleFromCache()

const app = createApp(App)
app.config.globalProperties.$t = t
app.config.globalProperties.$setLang = setLang
// 非中文用户先预载对应字典再挂载，避免首屏闪一下中文；失败也会挂载（由 t() 兜底）
initI18n().finally(() => app.mount('#app'))

/* PWA：注册 Service Worker（仅 http/https 下生效；file:// 或调试时跳过） */
if ('serviceWorker' in navigator && /^https?:$/.test(location.protocol)) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('./sw.js').catch(() => {})
  })
}
