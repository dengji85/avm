import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建为纯静态前端，相对路径部署，由后端 / 静态托管。
// dev（serve）用 /：base 为 './' 时 Vite 开发服务器访问根路径会 404，必须显式打开 /index.html；
// 打包（build）仍保留相对路径 './'，产物可放在任意子路径下。
export default defineConfig(({ command }) => ({
  base: command === 'serve' ? '/' : './',
  plugins: [vue()],
  build: {
    outDir: '../web_dist',
    emptyOutDir: true,
    assetsDir: 'assets',
    chunkSizeWarningLimit: 1500,
  },
  server: {
    proxy: {
      // 开发时把 /api 代理到后端，避免跨域（后端默认端口 8770）
      '/api': 'http://127.0.0.1:8770',
      '/covers': 'http://127.0.0.1:8770',
    },
  },
}))
