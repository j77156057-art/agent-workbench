/*
 * 临时校验配置（仅用于「改 CSS 但不 build」时拍活页面，用完即删）。
 * 与 vite.preview.config.ts 的差别：端口 5188 + /api 代理指到 8100（本次起的临时后端），
 * 其余（跳过 cleanAssets / cacheDir 挪到 D:/Temp 绕开 safe-delete shim）保持一致。
 * 本文件不参与正式构建，勿提交。
 */
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'node:path'
import { readFileSync } from 'node:fs'

export default defineConfig({
  cacheDir: 'D:/Temp/dm-vite-cache-2',
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
    port: 5188,
    proxy: {
      '/api': 'http://127.0.0.1:8100',
      '/ws': { target: 'ws://127.0.0.1:8100', ws: true },
    },
  },
})
