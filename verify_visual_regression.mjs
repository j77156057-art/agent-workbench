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
  { name: 'workbench-zinc', route: '/workbench', skin: 'zinc', stable: '.ft-head-title' },
  { name: 'ask', route: '/', skin: 'paper', stable: '.composer textarea' },
  { name: 'ask-zinc', route: '/', skin: 'zinc', stable: '.composer textarea' },
]

// 两个端点必须钉死，否则 ask 基线是抛硬币（实测，不是推测）：
//   · /api/model_status —— 读真后端：本机模型加载/释放之间显存会变（6.1 GB/2 个 ↔
//     6.5 GB/1 个，361 像素、最大通道差 231）。
//   · /api/config —— 两宗罪：① 冷启动第一发实测 >6 秒（页面没开时外部 GET 6s 超时，
//     页面开过之后同一发 6ms 返回），而本脚本只等 1500ms 就截图，所以侧栏的模型名和
//     文档列表有时进画面有时不进（这就是那 19% / 2.4% 的来历，跟排版无关）；
//     ② ingested_files 来自**全局** KB 存储，夹具只隔离了 code_root 和 STATE_FILE
//     （verify_scene_canvas.py:282-290），所以画的是这台机器上传过什么文档。
// 做法：config 先向夹具取一次真载荷（保住 providers / capability 等几十个真实字段），
// 只覆盖会漂的那三项 —— 不造假数据结构，只钉住不稳定值。
const PINNED_MODEL = 'qwen3.6:35b-a3b-agent25'
const MODEL_STATUS = {
  ok: true, reachable: true, needs_ollama: false, vram_gb: 6.1,
  loaded: [{ name: PINNED_MODEL, size_vram_gb: 6.1, size_gb: 6.1, expires_minutes: null, processor: 'ollama' }],
}
async function pinConfig(base) {
  const res = await fetch(base + '/api/config', { signal: AbortSignal.timeout(90000) })
  if (!res.ok) throw new Error(`/api/config 预热失败（HTTP ${res.status}）`)
  const live = await res.json()
  return {
    ...live,
    llm_provider: 'ollama',
    llm_model: PINNED_MODEL,
    ingested_files: ['战斗规则.md', '数值设计案.pdf'],
    warnings: [],
    // 顶部那条告警横幅读的是 config.ollama_status.guidance（AskApp.vue:152），
    // 而 guidance 是后端把「本机 Ollama 探测结果 + 要拉哪些模型」现场拼出来的字符串
    // （api.py:4573-4592）—— 里面直接印着真实模型名，机器状态一变横幅就变。
    // 保留横幅本身（warn 态的样式要留在视觉覆盖里），只把文案钉成一句不含机器信息的。
    ollama_status: {
      reachable: false, needed_models: ['pinned-model'], present_models: [], missing_models: ['pinned-model'],
      blocks_conversation: true, search_degraded: false,
      guidance: '未检测到本地模型服务（夹具钉死文案，不代表本机状态）。',
    },
  }
}

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

async function shoot(browser, base, c, config) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
  await page.addInitScript(`localStorage.setItem("docmind.skin","${c.skin}")`)
  // 两个桩都要计数：拦到 0 次就等于这个用例还在读真后端（端点改名 / 请求路径变了），
  // 那会让基线重新变成抛硬币，而且是静默的。
  const hit = { config: 0, status: 0 }
  await page.route('**/api/config*', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    hit.config++
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(config) })
  })
  await page.route('**/api/model_status*', async (route) => {
    hit.status++
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(MODEL_STATUS) })
  })
  await page.goto(base + c.route, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(c.stable, { timeout: 25000 })
  // 钉死之后不再靠固定时长赌时序：等钉进去的那个模型名真的出现在画面上。
  // 超时不抛（workbench 未必显示模型名），把「有没有出现」交给下面的断言材料。
  await page.waitForFunction(m => document.body.innerText.includes(m), PINNED_MODEL, { timeout: 8000 })
    .catch(() => {})
  await page.waitForTimeout(600)
  const hits = await page.evaluate(STABILIZE)
  await page.waitForTimeout(400)
  // 桩没生效就等于这个用例还在读真后端 —— 会天天误报，必须吵出来。
  const shown = await page.evaluate(() => document.body.innerText.includes('6.1 GB'))
  const modelShown = await page.evaluate((m) => document.body.innerText.includes(m), PINNED_MODEL)
  const buf = await page.screenshot({ type: 'png' })
  await page.close()
  // 把归一化命中数带出去：万一哪天路径不再出现在界面上（或选择器变了），
  // hits 掉到 0 就是"归一化失效"的信号，不能让它静默变成 no-op。
  return { buf, hits, hit, shown, modelShown }
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
  console.log(`夹具：${fixture.base}${reused ? '（复用外部服务，可能与基线不同源）' : '（本脚本新起，干净）'}`)
  const config = await pinConfig(fixture.base)
  console.log(`已钉住 /api/config 的易漂字段：模型=${config.llm_model} · 文档 ${config.ingested_files.length} 篇（其余字段用夹具真实值）\n`)
  for (const c of CASES) {
    const file = path.join(DIR, `${c.name}.png`)
    const { buf: shot, hits, hit, shown, modelShown } = await shoot(browser, fixture.base, c, config)
    // 工作台界面上必然出现随机夹具路径；一次都没抹平 = 归一化失效，要吵出来
    const warn = c.route === '/workbench' && hits === 0 ? '  ⚠ 未抹平任何随机路径，归一化可能失效' : ''
    // 命中次数常驻打印（不只出错时才说）：桩静默失效 = 基线重新开始赌机器状态，
    // 而这种失效只有出现在每一行输出里才看得见。
    const stubs = `数据桩 config×${hit.config}（钉死模型可见=${modelShown}）· model_status×${hit.status}（显存可见=${shown}）`
    const stubWarn = (!hit.config || !modelShown || (c.route === '/' && (!hit.status || !shown)))
      ? `  ⚠ 数据桩未生效 —— 这一例又开始读真后端了` : ''
    const info = `  抹平随机路径 ${hits} 处 · ${stubs}${warn}${stubWarn}`
    if (UPDATE || !fs.existsSync(file)) {
      fs.writeFileSync(file, shot)
      console.log(`BASELINE ${c.name}  ${(shot.length / 1024).toFixed(0)}KB  ${info}`
        + (UPDATE ? '' : '  （基线缺失，已生成）'))
      if (!UPDATE || stubWarn) failed++
      continue
    }
    const basePng = fs.readFileSync(file)
    const d = await diff(browser, basePng, shot)
    const bad = d.changed > MAX_CHANGED_ABS || d.pct > MAX_CHANGED_PCT || !!stubWarn
    const out = path.join(DIR, `diff-${c.name}.png`)
    const now = path.join(DIR, `now-${c.name}.png`)
    if (bad) {
      fs.writeFileSync(out, Buffer.from(d.diffPng, 'base64'))
      // 标红图只说"哪里变了"，不说"现在长什么样"——排查时每次都要重跑一遍才能看见
      // 当前渲染。失败路径上顺手存一张，成本是几百 KB。
      fs.writeFileSync(now, shot)
      failed++
    }
    console.log(`${bad ? 'FAIL' : 'PASS'}   ${c.name}  变化像素 ${d.changed}/${d.total} (${d.pct.toFixed(3)}%)  最大通道差 ${d.worst}${info}`
      + (bad ? `  → 标红图 ${out}\n      → 当前截图 ${now}` : ''))
  }
  console.log(`\n${CASES.length - failed}/${CASES.length} 视觉基线一致`
    + `（阈值：单像素通道差 >${CHANNEL_EPS} 的像素超过 ${MAX_CHANGED_ABS} 个，或占比 >${MAX_CHANGED_PCT}%）`)
} finally {
  await browser.close()
  if (fixture.child) fixture.child.kill()
}
process.exit(failed ? 1 : 0)
