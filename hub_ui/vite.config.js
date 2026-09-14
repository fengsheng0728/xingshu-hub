import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  // 产物由 FastAPI 托管于 /，hash 路由无需服务端 rewrite；相对基座保证任意挂载点可用
  base: './',
  build: {
    outDir: '../dashboard_dist',
    emptyOutDir: true,
  },
  server: {
    // 开发时代理到本地 Hub（仅 dev 用；产物运行时零外链）
    proxy: {
      '/api': 'http://127.0.0.1:3060',
      '/healthz': 'http://127.0.0.1:3060',
      '/readyz': 'http://127.0.0.1:3060',
      '/ws': { target: 'ws://127.0.0.1:3060', ws: true },
    },
  },
})
