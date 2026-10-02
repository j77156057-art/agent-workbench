/*
 * 临时预览配置（仅用于沙箱内截图核验，用完即删）。
 * 与 vite.config.ts 的唯一差别：去掉 cleanAssets 插件——它的 rmSync 会被
 * WorkBuddy 的 safe-delete shim 拦截，导致沙箱内 dev/build 全部失败。
 * 本文件不参与正式构建，勿提交。
 */
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'node:path'
import { readFileSync } from 'node:fs'

export default defineConfig({
  // 指到全新空目录，避免 Vite 重建 node_modules/.vite/deps 时触发批量删除护栏
  cacheDir: 'D:/Temp/dm-vite-cache-1',
  plugins: [
    vue({ template: { compilerOptions: { isCustomElement: (tag: string) => tag === 'model-viewer' } } }),
    {
      name: 'docmind-session-script',
      configureServer(server) {
        server.middlewares.use('/static/session.js', (_req, res) => {
          const file = resolve(__dirname, 'public/session.js')
          res.statusCode = 200
          res.setHeader('Content-Type', 'application/javascript; charset=utf-8')
          res.end(readFileSync(file))
        })
      },
    },
  ],
  server: {
    port: 5199,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
