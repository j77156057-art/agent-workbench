// 产物新鲜度门禁：构建产物必须比所有前端源文件都新。
//
// 为什么需要它：2026-10-20 出现过一次"我改了、构建也报了 ✓、但产物里的规则是旧的"。
// 原因是**并发写者也在构建同一个 web/ 输出目录**（vite 的 emptyOutDir: false，
// 双方共用 ../web），谁后跑谁覆盖 —— 我的构建被对方的产物盖掉了，于是：
//   · 源码里有新规则
//   · web/assets 里的 bundle 里没有
//   · 用户打开应用看到的是旧样子
// 而所有门禁都是"对着服务出来的页面量"的，量到的也是旧产物，于是**一片绿**，
// 没人发现改动根本没生效。
//
// 这道门禁把"产物比源码旧"直接报红，并指出最旧的那个产物文件。
//
// 用法：node verify_product_fresh.mjs
import fs from 'node:fs'
import path from 'node:path'

const SRC_DIRS = ['frontend/src']
const SRC_FILES = ['frontend/index.html', 'frontend/workbench.html']
const OUT_DIR = 'web'

let newestSrc = { file: '', mtime: 0 }
const scan = (p) => {
  let st
  try { st = fs.statSync(p) } catch { return }
  if (st.isDirectory()) {
    for (const e of fs.readdirSync(p)) scan(path.join(p, e))
  } else if (st.mtimeMs > newestSrc.mtime) {
    newestSrc = { file: p, mtime: st.mtimeMs }
  }
}
for (const d of SRC_DIRS) scan(d)
for (const f of SRC_FILES) scan(f)

// 只看 **vite 产出的那部分**：index.html / workbench.html / assets/。
// web/ 里还有 favicon.ico、trace.html、session.js 这些 vite 不管的文件
// （配置里 emptyOutDir: false 就是为了保住它们），拿它们比新鲜度会永远报旧。
let oldestOut = { file: '', mtime: Infinity }
const scanOut = (p) => {
  let st
  try { st = fs.statSync(p) } catch { return }
  if (st.isDirectory()) {
    for (const e of fs.readdirSync(p)) scanOut(path.join(p, e))
  } else if (st.mtimeMs < oldestOut.mtime) {
    oldestOut = { file: p, mtime: st.mtimeMs }
  }
}
for (const f of ['index.html', 'workbench.html']) scanOut(path.join(OUT_DIR, f))
scanOut(path.join(OUT_DIR, 'assets'))

const fmt = (ms) => new Date(ms).toLocaleString('zh-CN', { hour12: false })
console.log(`最新源文件：${newestSrc.file}  ${fmt(newestSrc.mtime)}`)
console.log(`最旧产物文件：${oldestOut.file}  ${fmt(oldestOut.mtime)}`)

if (!newestSrc.file || !oldestOut.file) {
  console.log('\n没有可比对的文件（源或产物为空）—— 跳过')
  process.exit(0)
}
if (oldestOut.mtime < newestSrc.mtime) {
  console.log(`\nFAIL 产物比源码旧 ${Math.round((newestSrc.mtime - oldestOut.mtime) / 60000)} 分钟 —— ` +
    '你看到的应用是旧样子，先重新构建（cd frontend && npm run build）')
  process.exit(1)
}
console.log('\nPASS 产物不旧于源码')
