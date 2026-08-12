import { createApp } from 'vue'
import App from './App.vue'
import { t, setLang } from './i18n'

import './styles/tokens.css'
import './styles/base.css'
import './styles/ui.css'
import './styles/layout.css'
import './styles.css'

const app = createApp(App)
app.config.globalProperties.$t = t
app.config.globalProperties.$setLang = setLang
app.mount('#app')
