import { defineConfig, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'node:path'
import { existsSync, readFileSync, rmSync } from 'node:fs'

/**
 * 构建前清掉 web/assets。
 *
 * emptyOutDir 必须是 false（web/ 里有 trace.html 等非 Vite 托管文件，清了就没了），副作用是
 * 带哈希的旧产物会一直堆积——实测攒了 90 个文件，而冻结打包会把整个 web/ 拷进
 * 安装目录，等于白带一堆死文件。这里只删 assets 子目录：它纯粹是 Vite 产物，
 * 入口 HTML 每次构建都会重写引用，删掉安全。
 */
function cleanAssets(): Plugin {
  return {
    name: 'docmind-clean-assets',
    buildStart() {
      const dir = resolve(__dirname, '../web/assets')
      if (existsSync(dir)) rmSync(dir, { recursive: true, force: true })
    },
  }
}

// The production FastAPI server mounts the web directory at /static, while
// Vite exposes files from frontend/public at the root. Keep the shared session
// bootstrap URL identical in both environments so the Vue app can mount in
// dev as well as in the packaged desktop build.
function serveSessionScript(): Plugin {
  return {
    name: 'docmind-session-script',
    configureServer(server) {
      server.middlewares.use('/static/session.js', (_req, res) => {
        const file = resolve(__dirname, 'public/session.js')
        res.statusCode = 200
        res.setHeader('Content-Type', 'application/javascript; charset=utf-8')
        res.end(readFileSync(file))
      })
    },
  }
}

// DocMind 前端工程配置
// - 多页应用（MPA）：ask=AI 问答首页（/，产物 index.html）；workbench=代码工作台（/workbench）
// - 产物直接写入后端静态目录 ../web（PyInstaller 的 ("web","web") 会原样带走）
// - emptyOutDir 必须为 false：../web 里还有 trace.html 等非本工程托管的静态文件，构建时绝不能清空
export default defineConfig({
  plugins: [
    vue({ template: { compilerOptions: { isCustomElement: (tag: string) => tag === 'model-viewer' } } }),
    cleanAssets(),
    serveSessionScript(),
  ],
  build: {
    outDir: resolve(__dirname, '../web'),
    emptyOutDir: false,
    assetsDir: 'assets',
    rollupOptions: {
      input: {
        // 入口文件名决定产物名：index.html 构建为 ../web/index.html，由后端 / 路由返回
        ask: resolve(__dirname, 'index.html'),
        workbench: resolve(__dirname, 'workbench.html'),
      },
      output: {
        // 第三方依赖单独成块：业务代码高频改动，vendor 哈希稳定可被浏览器长期缓存。
        // CodeMirror 6 体积最大（约 2/3 bundle），独立成块避免与 Vue 混在一起。
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return
          if (id.includes('@codemirror') || id.includes('@lezer') || id.includes('codemirror')) {
            return 'vendor-codemirror'
          }
          // @vue-flow 必须显式排在 @vue 之前，否则会被下面的 @vue 规则捞进首屏的
          // vendor-vue（@vue-flow 的路径里也含 "@vue"）。这里返回 undefined = 不做切分，
          // 让它跟画布一起留在 SceneCanvas 异步 chunk：JS 与 CSS 同批加载，且首屏不受影响。
          if (id.includes('@vue-flow')) return
          // model-viewer 3D 预览全家桶（three / lit / gainmap / d3-*）只有素材中心 3D 预览
          // 用到，且仅被 ModelPreview.vue 里的 `await import('@google/model-viewer')` 引用，
          // 整体归入同一个懒 chunk。
          // 注意：不能只排除 @google/model-viewer 和 three——lit/gainmap/d3 若漏网落到下面
          // 的兜底 vendor-misc（首屏），three 会作为它们的公共依赖被 Rollup 一起提升回首屏，
          // 排除规则形同虚设。
          const norm = id.replaceAll('\\', '/')
          if (
            norm.includes('@google/model-viewer')
            || norm.includes('/node_modules/three/')
            || norm.includes('/node_modules/lit/')
            || norm.includes('/node_modules/lit-')
            || norm.includes('/node_modules/@lit/')
            || norm.includes('/node_modules/@monogrid/gainmap-js/')
            || norm.includes('/node_modules/promise-worker-transferable/')
            || /\/node_modules\/d3-[a-z-]+\//.test(norm)
          ) {
            return 'vendor-model-viewer'
          }
          if (id.includes('@vue') || id.includes('vue') || id.includes('@vitejs')) {
            return 'vendor-vue'
          }
          // html2canvas（含 css-line-break / text-segmentation）只在工作流预览截图时
          // 动态 import：若归入命名 vendor 块，Rollup 会把它提升成入口的静态依赖，
          // 208KB 白进首屏。返回 undefined 让它保留独立异步 chunk。
          if (
            norm.includes('/node_modules/html2canvas/')
            || norm.includes('/node_modules/css-line-break/')
            || norm.includes('/node_modules/text-segmentation/')
          ) {
            return
          }
          return 'vendor-misc'
        },
      },
    },
  },
  server: {
    port: 5173,
    // 默认只绑 IPv6 loopback，用 http://127.0.0.1:5173 打不开（必须写 localhost），
    // 排查时极易误判成"前端起不来"。绑 IPv4 loopback 即可修好，且不暴露局域网
    // （host: true 会绑 0.0.0.0，而 /api 代理指向有写能力的后端）。
    host: '127.0.0.1',
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
