import { defineConfig, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createHash } from 'node:crypto'
import { join, relative, resolve, sep } from 'node:path'
import {
  existsSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs'

/**
 * 构建前清掉 web/assets。
 *
 * emptyOutDir 必须是 false（web/ 里有 trace.html 等非 Vite 托管文件，清了就没了），副作用是
 * 带哈希的旧产物会一直堆积——实测攒了 90 个文件，而冻结打包会把整个 web/ 拷进
 * 安装目录，等于白带一堆死文件。这里只删 assets 子目录：它纯粹是 Vite 产物，
 * 入口 HTML 每次构建都会重写引用，删掉安全。
 */
// fontsource 的 css 同时列 woff2+woff 双 src，vite 会双份产出；
// 现代浏览器全支持 woff2，woff 是安装包死重（实测 4.57MB）——构建期剥离 woff src。
function woff2Only(): Plugin {
  return {
    name: 'docmind-woff2-only',
    enforce: 'pre',
    transform(code, id) {
      if (!id.includes('@fontsource') || !id.endsWith('.css')) return null
      return code.replace(/,\s*url\([^)]*\.woff\)\s*format\(['"]woff['"]\)/g, '')
    },
  }
}

function cleanAssets(): Plugin {
  return {
    name: 'docmind-clean-assets',
    // 只在构建时清旧哈希产物：buildStart 在 dev 启动也会跑，若不限制 apply，
    // 起一次 dev server 就会删掉后端正在服务的生产构建（实测 web/assets 被清空）。
    apply: 'build',
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

// Agent 工作台 前端工程配置
// - 多页应用（MPA）：ask=AI 问答首页（/，产物 index.html）；workbench=代码工作台（/workbench）
// - 产物直接写入后端静态目录 ../web（PyInstaller 的 ("web","web") 会原样带走）
// - emptyOutDir 必须为 false：../web 里还有 trace.html 等非本工程托管的静态文件，构建时绝不能清空


/**
 * 构建来源清单：把「这次构建用的 frontend/src 每一个文件的哈希」写进
 * web/build-manifest.json。
 *
 * 为什么不能靠时间戳：产物与源码的对应关系**只能用内容核对**。git checkout
 * 会把所有文件的 mtime 刷成同一时刻，于是「产物比源码新」这条判据在 CI 上
 * 只是构建顺序的副产品；而在本地「改了 src 忘了 build」又可能被 mtime 偶然
 * 放过（文件内容没真改，只有 mtime 变了）。哈希不受这两种干扰。
 *
 * 清单本身是**构建产物**（web/ 在 .gitignore 里），所以它只存在于「构建过的那
 * 台机器上」—— 而这正是它该在的地方：要核对的正是「这台机器上的产物」对不对
 * 得上「这台机器上的源码」。跨机器比较没有意义，也不该做。
 */
function buildManifest(): Plugin {
  return {
    name: 'docmind-build-manifest',
    apply: 'build',
    closeBundle() {
      const srcRoot = resolve(__dirname, 'src')
      const files: Record<string, string> = {}
      const walk = (dir: string) => {
        for (const name of readdirSync(dir).sort()) {
          const full = join(dir, name)
          if (statSync(full).isDirectory()) {
            walk(full)
            continue
          }
          const rel = relative(srcRoot, full).split(sep).join('/')
          files[rel] = createHash('sha256').update(readFileSync(full)).digest('hex')
        }
      }
      walk(srcRoot)
      const payload = {
        version: 1,
        source_root: 'frontend/src',
        source_count: Object.keys(files).length,
        sources: files,
      }
      writeFileSync(resolve(__dirname, '../web/build-manifest.json'),
        JSON.stringify(payload, null, 1) + '\n')
    },
  }
}

export default defineConfig({
  plugins: [
    vue({ template: { compilerOptions: { isCustomElement: (tag: string) => tag === 'model-viewer' } } }),
    woff2Only(),
    cleanAssets(),
    buildManifest(),
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
      // 后端端口跟 desktop.py 一样认 DOCMIND_PORT：本机 8000 常被另一个项目占着，
      // 换端口起 Agent 工作台 时，dev 模式的 /api 也必须跟着指到同一个后端，
      // 否则前端调的是**另一个应用**的 API。
      //
      // ws: true —— 实时对话走 WebSocket（/api/vision/live-stream），
      // 不开这个开关，dev 下浏览器会把 WS 请求直接发给 vite，握手必然失败。
      // changeOrigin + headers.origin —— 后端对 WS/HTTP 都做**同源**校验
      // （比对 Origin 与 Host），两个头必须一起改写到代理目标，不能只改 Origin。
      '/api': {
        target: `http://127.0.0.1:${process.env.DOCMIND_PORT || 8000}`,
        changeOrigin: true,
        ws: true,
        headers: { origin: `http://127.0.0.1:${process.env.DOCMIND_PORT || 8000}` },
      },
    },
  },
})
