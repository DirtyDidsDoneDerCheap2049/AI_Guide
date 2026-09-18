import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发代理目标：浏览器端永远只请求同源的 /api 与 /_AMapService，由 Vite dev server 转发到后端，
// 因此 src/ 里不会出现任何主机名、IP 或固定域名（生产环境由 Caddy 反向代理）。
// 这个默认值只存在于构建配置中，不会进入浏览器产物。
const DEV_API_TARGET = process.env.VITE_DEV_API_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      // SSE 也走这条代理：http-proxy 默认流式转发，不缓冲响应体。
      '/api': {
        target: DEV_API_TARGET,
        changeOrigin: true,
      },
      // 高德 JS 安全代理：前端把 serviceHost 指向同源 /_AMapService，
      // 开发环境必须把它也转发到后端（后端在转发时追加 JS 安全密钥）。
      '/_AMapService': {
        target: DEV_API_TARGET,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    // 产物中不保留 sourcemap，避免把源码结构带进镜像。
    sourcemap: false,
  },
})
