// L3 组合矩阵抽检：4 皮肤 × 2 布局 × 2 材质（问答页）+ 2 调色板 × 2 × 2（工作台）。
//
// 为什么是抽检而不是 40 组全跑：L3 的判定是「任取不得破相」，数学上等于全集，
// 但布局层里 fixed 与 focus 的差异最大（侧栏常驻 vs 移出视口 + 主区限宽），
// 其余 dock/split/grid 是同族变体。先抽两端，跑通后再铺开（--all 可跑全 5 种布局）。
//
// 每组判三件事：
//   ① 横向溢出：有没有元素把容器撑破（scrollWidth > clientWidth 且未声明滚动）
//   ② 文本对比度：色值解析（不用截图采样——11px 小字会被抗锯齿低估 1.0，见 §5.2）
//   ③ 轨道层次：侧栏与主区的亮度关系（深色下 0.02≤L≤0.08 且 |Δ|≥0.01；浅色下 Δ≥0.3）
//
// 用法：
//   node tools/verify_l3_matrix.mjs           抽检（2 布局）
//   node tools/verify_l3_matrix.mjs --all     全 5 种布局
//   node tools/verify_l3_matrix.mjs --shots   额外保存每组截图
import { createRequire } from 'node:module'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import net from 'node:net'
import path from 'node:path'

const require = createRequire(import.meta.url)
// --help / -h：只打印用法并退出，供文档命令验证与人工查阅使用（不启动浏览器）。
if (process.argv.includes('--help') || process.argv.includes('-h')) {
  console.log(`用法：node tools/verify_l3_matrix.mjs [--all] [--shots] [--help]

  默认      抽检 2 种布局：4 皮肤 × 2 布局 × 2 材质 + 工作台 2 调色板 × 2 × 2 = 24 组
  --all     全 5 种布局，共 60 组（L3 认证口径）
  --shots   额外保存每组截图到 docs/screenshots/design-l3/
  --help    打印本说明并退出

  每组判：横向溢出 / 文本对比度（色值解析）/ 轨道层次。任一组失败即退出码 1。
  产物：docs/screenshots/design-l3-matrix.json`);
  process.exit(0)
}

const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE || process.env.HOME || '', '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))

const ALL = process.argv.includes('--all')
const SHOTS = process.argv.includes('--shots')
const OUT = 'docs/screenshots/design-l3'

const SKINS = ['zinc', 'graphite', 'paper', 'seed']
const PALETTES = ['light', 'observatory']
const LAYOUTS = ALL ? ['fixed', 'focus', 'dock', 'split', 'grid'] : ['fixed', 'focus']
const MATERIALS = ['solid', 'glass']

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
  const child = spawn('.venv/Scripts/python.exe', ['verify_scene_canvas.py', '--serve', String(port)], { stdio: 'ignore' })
  const deadline = Date.now() + 60000
  while (Date.now() < deadline) {
    try { const r = await fetch(base + '/api/projects', { signal: AbortSignal.timeout(1500) }); if (r.ok) return { base, child } } catch {}
    await new Promise(r => setTimeout(r, 400))
  }
  child.kill()
  throw new Error('fixture timeout')
}

// 页面内量测：全部用色值解析，不截图取样
function M_MEASURE() {
  const cv = document.createElement('canvas')
  cv.width = cv.height = 1
  const cx = cv.getContext('2d', { willReadFrequently: true })
  const toRGBA = (css) => {
    cx.clearRect(0, 0, 1, 1)
    cx.fillStyle = 'rgba(0,0,0,0)'
    cx.fillStyle = css
    cx.fillRect(0, 0, 1, 1)
    const d = cx.getImageData(0, 0, 1, 1).data
    return [d[0], d[1], d[2], d[3] / 255]
  }
  const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4) }
  const lum = (p) => 0.2126 * lin(p[0]) + 0.7152 * lin(p[1]) + 0.0722 * lin(p[2])
  const cr = (a, b) => { const l1 = lum(a), l2 = lum(b), hi = Math.max(l1, l2), lo = Math.min(l1, l2); return (hi + 0.05) / (lo + 0.05) }
  const over = (fg, bg) => [0, 1, 2].map(i => Math.round(fg[i] * fg[3] + bg[i] * (1 - fg[3])))

  // ① 横向溢出
  const overflow = []
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el)
    if (['auto', 'scroll', 'hidden', 'clip'].includes(cs.overflowX)) continue
    if (el.clientWidth < 40 || el.clientHeight < 8) continue
    if (el.scrollWidth > el.clientWidth + 1) {
      overflow.push({ cls: (el.getAttribute('class') || el.tagName).slice(0, 36), client: el.clientWidth, scroll: el.scrollWidth })
    }
  }

  // ② 文本对比度：取视口内面积最大的 24 个有文本元素
  const items = []
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect()
    if (r.width < 8 || r.height < 8) continue
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.nodeValue.trim().length > 1)) continue
    const cs = getComputedStyle(el)
    if (cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.5) continue
    if (el.closest('[disabled],[aria-disabled="true"]')) continue
    const pts = [[0.5, 0.5], [0.3, 0.5], [0.7, 0.5]]
    let hit = 0
    for (const [fx, fy] of pts) {
      const n = document.elementFromPoint(
        Math.min(innerWidth - 1, Math.max(0, r.left + r.width * fx)),
        Math.min(innerHeight - 1, Math.max(0, r.top + r.height * fy)))
      if (n && (n === el || el.contains(n) || n.contains(el))) hit++
    }
    if (hit < 2) continue
    items.push({ el, area: r.width * r.height })
  }
  items.sort((a, b) => b.area - a.area)
  const contrast = []
  for (const { el } of items.slice(0, 24)) {
    const cs = getComputedStyle(el)
    let bg = null, hasImg = false
    for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
      const s = getComputedStyle(p)
      if (s.backgroundImage && s.backgroundImage !== 'none') hasImg = true
      const rgba = toRGBA(s.backgroundColor)
      if (rgba[3] >= 0.999) { bg = rgba.slice(0, 3); break }
    }
    if (!bg || hasImg) continue
    const fgRaw = toRGBA(cs.color)
    const fg = fgRaw[3] < 0.999 ? over(fgRaw, bg) : fgRaw.slice(0, 3)
    contrast.push({
      cls: (el.getAttribute('class') || el.tagName).slice(0, 30),
      text: (el.textContent || '').trim().slice(0, 14),
      ratio: +cr(fg, bg).toFixed(2),
    })
  }

  // ③ 轨道层次
  // glass 材质下容器的 backgroundColor 是半透明（rgba(...,0.x) 甚至 alpha=0），
  // 直接取 getComputedStyle 会得到亮度 0，把"侧栏 vs 主区"算成极端差值 ——
  // 实测一开始 10 组 glass 全都误报"Δ≈-0.03"。必须向上找到第一个不透明祖先再算。
  const effBg = (el) => {
    for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
      const rgba = toRGBA(getComputedStyle(p).backgroundColor)
      if (rgba[3] >= 0.999) return rgba.slice(0, 3)
    }
    const root = toRGBA(getComputedStyle(document.documentElement).backgroundColor)
    return root[3] >= 0.999 ? root.slice(0, 3) : [255, 255, 255]
  }
  const rail = document.querySelector('.ask-side') || document.querySelector('.ft-panel')
  const main = document.querySelector('main') || document.querySelector('.ask-main') || document.body
  let railInfo = null
  if (rail) {
    const dark = getComputedStyle(document.documentElement).getPropertyValue('--skin-dark').trim() === '1'
    railInfo = {
      railL: +lum(effBg(rail)).toFixed(3),
      mainL: +lum(effBg(main)).toFixed(3),
      dark,
    }
    railInfo.delta = +(railInfo.mainL - railInfo.railL).toFixed(3)
  }

  return {
    overflow, contrast, rail: railInfo,
    layout: document.documentElement.getAttribute('data-layout'),
    material: document.documentElement.getAttribute('data-material') || 'solid',
    skin: document.documentElement.getAttribute('data-skin'),
  }
}

function judge(m) {
  const fail = []
  if (m.overflow.length) fail.push(`溢出 ${m.overflow.length}`)
  const bad = m.contrast.filter(c => c.ratio < 4.5)
  if (bad.length) fail.push(`对比度 ${bad.length} 处 <4.5（最低 ${Math.min(...bad.map(b => b.ratio))}）`)
  if (m.rail) {
    const { railL, mainL, delta, dark } = m.rail
    if (dark) {
      if (!(railL >= 0.02 && railL <= 0.08)) fail.push(`深色侧栏 L=${railL} 越界`)
      else if (Math.abs(delta) < 0.01) fail.push(`深色层次 Δ=${delta}`)
    } else if (delta < 0.3) fail.push(`浅色侧栏 Δ=${delta} <0.3`)
  }
  return fail
}

const CASES = []
for (const skin of SKINS) for (const layout of LAYOUTS) for (const material of MATERIALS)
  CASES.push({ page: 'ask', route: '/', stable: '.composer textarea', skin, palette: null, layout, material })
for (const palette of PALETTES) for (const layout of LAYOUTS) for (const material of MATERIALS)
  CASES.push({ page: 'workbench', route: '/workbench', stable: '.wb-body', skin: null, palette, layout, material })

const fixture = await startFixture()
const browser = await chromium.launch({ channel: 'msedge', headless: true })
if (SHOTS) fs.mkdirSync(OUT, { recursive: true })
const rows = []

try {
  for (const c of CASES) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    const page = await ctx.newPage()
    const init = [
      c.skin ? `localStorage.setItem("docmind.skin","${c.skin}");localStorage.setItem("docmind.skinLocked","1")` : '',
      c.palette ? `localStorage.setItem("docmind.workbenchPalette","${c.palette}")` : '',
      `localStorage.setItem("docmind.layout","${c.layout}")`,
      `localStorage.setItem("docmind.material","${c.material}")`,
    ].filter(Boolean).join(';')
    await page.addInitScript(init)
    await page.goto(fixture.base + c.route, { waitUntil: 'domcontentloaded' })
    await page.waitForSelector(c.stable, { timeout: 25000 })
    await page.waitForTimeout(1400)
    await page.addStyleTag({ content: '*,*::before,*::after{animation:none !important;transition:none !important}' })
    const m = await page.evaluate(M_MEASURE)
    const fail = judge(m)
    const label = `${c.page}/${c.skin || c.palette}/${c.layout}/${c.material}`
    rows.push({ label, ...m, fail })
    if (SHOTS) {
      const client = await page.context().newCDPSession(page)
      const { data } = await client.send('Page.captureScreenshot', { format: 'png' })
      await client.detach()
      fs.writeFileSync(path.join(OUT, label.replace(/[\/]/g, '_') + '.png'), Buffer.from(data, 'base64'))
    }
    const mark = fail.length ? '✗' : '✓'
    const minC = m.contrast.length ? Math.min(...m.contrast.map(x => x.ratio)) : null
    console.log(`${mark} ${label.padEnd(42)} 溢出 ${String(m.overflow.length).padEnd(2)} 最低对比 ${String(minC).padEnd(6)} ${fail.length ? fail.join(' / ') : ''}`)
    await ctx.close()
  }

  const bad = rows.filter(r => r.fail.length)
  console.log(`\n=== ${rows.length} 组，破相 ${bad.length} 组 ===`)
  if (bad.length) {
    for (const b of bad) {
      console.log(`\n${b.label}`)
      if (b.overflow.length) console.log('  溢出:', b.overflow.slice(0, 3))
      const cb = b.contrast.filter(c => c.ratio < 4.5).slice(0, 3)
      if (cb.length) console.log('  低对比:', cb)
    }
  }
  fs.writeFileSync('docs/screenshots/design-l3-matrix.json', JSON.stringify(rows, null, 2), 'utf-8')
  process.exitCode = bad.length ? 1 : 0
} finally {
  await browser.close()
  fixture.child.kill()
}
