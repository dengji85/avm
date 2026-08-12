import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建为纯静态前端，相对路径部署，由后端 / 静态托管。
export default defineConfig({
  base: './',
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
})
