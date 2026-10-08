// 视觉回归：把关键界面截成基线图，之后每次比对像素差。
//
// 为什么现在才做得动：字体原先走 CDN 且间歇不可达，同一份构建两次能截出两种墨量，
// 任何像素比对都会天天报假警。字体自托管（见 frontend/src/fonts.ts）之后渲染才确定。
//
// 依赖：无新增 npm 包 —— PNG 解码与逐像素比对交给浏览器 canvas 做（复用同一个浏览器）。
// 用法：
//   node verify_visual_regression.mjs              比对，发现差异则退出码 1
//   node verify_visual_regression.mjs --update     重新生成基线
//
// 本脚本【自己起一个一次性夹具服务】，不复用 8011。原因是其它 verify_*.mjs 会改写
// 夹具内容（新增分区、建 nested_/custom_ 文件、改文件数计数），一旦先跑过它们，
// 工作台界面就和基线对不上了 —— 实测这样会差 7.9% 像素。自己起干净夹具可以把
// 执行顺序从等式里拿掉。设 DOCMIND_UI_TEST_URL 可改为复用已有服务。
import { createRequire } from 'node:module'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import net from 'node:net'
import path from 'node:path'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE, '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))

const PY = ['.venv/Scripts/python.exe', '.venv/bin/python', 'python', 'py']
  .filter(c => !c.includes('/') || fs.existsSync(c))
const DIR = 'docs/screenshots/visual'
const UPDATE = process.argv.includes('--update')

function freePort() {
  return new Promise((res, rej) => {
    const s = net.createServer()
    s.on('error', rej)
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)) })
  })
}

async function startFixture() {
  const port = await freePort()
  const base = `http://127.0.0.1:${port}`
  const py = PY.find(c => c === 'python' || c === 'py' || fs.existsSync(c)) || 'python'
  const child = spawn(py, ['verify_scene_canvas.py', '--serve', String(port)], { stdio: 'ignore' })
  const deadline = Date.now() + 60000
  while (Date.now() < deadline) {
    try {
      const r = await fetch(base + '/api/projects', { signal: AbortSignal.timeout(1500) })
      if (r.ok) return { base, child }
    } catch { /* 还没起来 */ }
    await new Promise(r => setTimeout(r, 400))
  }
  child.kill()
  throw new Error('夹具服务 60s 内没起来')
}
// 阈值来自实测，不是拍的：
//   干净态噪声底（同一构建连跑，eps=4）最多 7 个像素，eps=2/8 为 0；
//   而一次"很小的真实改动"（把 paper 的 --accent 从 #ff674d 改成 #ff7755）
//   在 eps=4 下动了 2215 个像素（workbench）/ 30426 个（ask）。
//   信噪差约 300 倍，所以绝对下限取 200 像素：离噪声有 28 倍余量，又远小于信号。
// eps 取 4 而不是 16 —— 实测 eps=16 时那次品牌色改动被判成 0 像素，等于漏报。
const CHANNEL_EPS = 4
const MAX_CHANGED_ABS = 200
// 兜底：大面积但每像素都只变一点点的情况。
const MAX_CHANGED_PCT = 0.4
// 基线与截图机器相关（文字光栅化在 GPU / 软件渲染下会有差异）。换机器首次运行
// 若整片报红，用 --update 在新机器上重建基线，别去调阈值。

const CASES = [
  { name: 'workbench-paper', route: '/workbench', skin: 'paper', stable: '.ft-head-title' },
  { name: 'workbench-graphite', route: '/workbench', skin: 'graphite', stable: '.ft-head-title' },
  { name: 'ask', route: '/', skin: 'paper', stable: '.composer textarea' },
]

// 关掉动效与光标闪烁，否则同一状态两次截图就会差几像素。
//
// 还要抹掉一个非确定性来源：夹具服务用 tempfile.mkdtemp(prefix="docmind_verify_scene_")
// 造工程目录（verify_scene_canvas.py:61），每次启动后缀都不同，而这段路径会渲染进工作台
// 的「资源管理器」标题与项目路径行。不归一化的话服务一重启基线就必然误报。
const STABILIZE = () => {
  const s = document.createElement('style')
  s.textContent = '*,*::before,*::after{animation:none !important;transition:none !important;'
    + 'caret-color:transparent !important}'
  document.head.appendChild(s)
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  let hits = 0
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!/docmind_verify_scene_/.test(n.nodeValue)) continue
    n.nodeValue = n.nodeValue.replace(/docmind_verify_scene_[A-Za-z0-9_-]+/g, 'docmind_verify_scene_FIXED')
    hits++
  }
  return hits
}

async function shoot(browser, base, c) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
  await page.addInitScript(`localStorage.setItem("docmind.skin","${c.skin}")`)
  await page.goto(base + c.route, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(c.stable, { timeout: 25000 })
  await page.waitForTimeout(1500)
  const hits = await page.evaluate(STABILIZE)
  await page.waitForTimeout(400)
  const buf = await page.screenshot({ type: 'png' })
  await page.close()
  // 把归一化命中数带出去：万一哪天路径不再出现在界面上（或选择器变了），
  // hits 掉到 0 就是"归一化失效"的信号，不能让它静默变成 no-op。
  return { buf, hits }
}

// 交给浏览器解码 PNG 并逐像素比：省掉一个 npm 依赖。
async function diff(browser, aBuf, bBuf) {
  const page = await browser.newPage()
  await page.goto('about:blank')
  const out = await page.evaluate(async ([a, b, eps]) => {
    const load = src => new Promise((res, rej) => {
      const im = new Image()
      im.onload = () => res(im)
      im.onerror = rej
      im.src = 'data:image/png;base64,' + src
    })
    const [ia, ib] = await Promise.all([load(a), load(b)])
    const w = Math.max(ia.width, ib.width), h = Math.max(ia.height, ib.height)
    const cv = document.createElement('canvas')
    cv.width = w; cv.height = h
    const x = cv.getContext('2d', { willReadFrequently: true })
    x.fillStyle = '#fff'; x.fillRect(0, 0, w, h)
    x.drawImage(ia, 0, 0)
    const da = x.getImageData(0, 0, w, h).data
    x.clearRect(0, 0, w, h); x.fillStyle = '#fff'; x.fillRect(0, 0, w, h)
    x.drawImage(ib, 0, 0)
    const db = x.getImageData(0, 0, w, h).data
    x.clearRect(0, 0, w, h); x.fillStyle = '#fff'; x.fillRect(0, 0, w, h)
    x.drawImage(ia, 0, 0)
    const mark = x.getImageData(0, 0, w, h)
    let changed = 0, worst = 0
    for (let i = 0; i < da.length; i += 4) {
      const d = Math.max(Math.abs(da[i] - db[i]), Math.abs(da[i + 1] - db[i + 1]), Math.abs(da[i + 2] - db[i + 2]))
      if (d > worst) worst = d
      if (d > eps) {
        changed++
        mark.data[i] = 255; mark.data[i + 1] = 0; mark.data[i + 2] = 0   // 标红
      }
    }
    x.putImageData(mark, 0, 0)
    const total = w * h
    return {
      w, h, changed, total, pct: (100 * changed) / total, worst,
      diffPng: cv.toDataURL('image/png').split(',')[1],
    }
  }, [aBuf.toString('base64'), bBuf.toString('base64'), CHANNEL_EPS])
  await page.close()
  return out
}

fs.mkdirSync(DIR, { recursive: true })
const browser = await chromium.launch({ channel: 'msedge', headless: true })
const reused = !!process.env.DOCMIND_UI_TEST_URL
const fixture = reused ? { base: process.env.DOCMIND_UI_TEST_URL, child: null } : await startFixture()
let failed = 0
try {
  console.log(`夹具：${fixture.base}${reused ? '（复用外部服务，可能与基线不同源）' : '（本脚本新起，干净）'}\n`)
  for (const c of CASES) {
    const file = path.join(DIR, `${c.name}.png`)
    const { buf: shot, hits } = await shoot(browser, fixture.base, c)
    // 工作台界面上必然出现随机夹具路径；一次都没抹平 = 归一化失效，要吵出来
    const warn = c.route === '/workbench' && hits === 0 ? '  ⚠ 未抹平任何随机路径，归一化可能失效' : ''
    const info = `  抹平随机路径 ${hits} 处${warn}`
    if (UPDATE || !fs.existsSync(file)) {
      fs.writeFileSync(file, shot)
      console.log(`BASELINE ${c.name}  ${(shot.length / 1024).toFixed(0)}KB  ${info}`
        + (UPDATE ? '' : '  （基线缺失，已生成）'))
      if (!UPDATE) failed++
      continue
    }
    const basePng = fs.readFileSync(file)
    const d = await diff(browser, basePng, shot)
    const bad = d.changed > MAX_CHANGED_ABS || d.pct > MAX_CHANGED_PCT
    const out = path.join(DIR, `diff-${c.name}.png`)
    if (bad) {
      fs.writeFileSync(out, Buffer.from(d.diffPng, 'base64'))
      failed++
    }
    console.log(`${bad ? 'FAIL' : 'PASS'}   ${c.name}  变化像素 ${d.changed}/${d.total} (${d.pct.toFixed(3)}%)  最大通道差 ${d.worst}`
      + (bad ? `  → 标红图 ${out}` : ''))
  }
  console.log(`\n${CASES.length - failed}/${CASES.length} 视觉基线一致`
    + `（阈值：单像素通道差 >${CHANNEL_EPS} 的像素超过 ${MAX_CHANGED_ABS} 个，或占比 >${MAX_CHANGED_PCT}%）`)
} finally {
  await browser.close()
  if (fixture.child) fixture.child.kill()
}
process.exit(failed ? 1 : 0)
