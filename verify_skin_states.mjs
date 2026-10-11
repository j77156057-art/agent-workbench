// 状态化 AA 棘轮：把「打开子页/弹层再穷举扫对比度」固化成门禁。
//
// 为什么不是普通门禁：前端还有 ~330 处写死的表面色分布在 70 个文件里，一次改不完；
// 一个"上线即红"的门禁会被无视，所以这里记一份基线计数，**只准变好不准变坏**：
//   · 某个状态的不达标数超过基线 → FAIL（说明新代码引入了皮肤泄漏）
//   · 低于基线 → PASS 并提示把数字降下来（防止基线悄悄变松）
//   · 出现基线里没有的新状态 → FAIL
// 重新记账：node verify_skin_states.mjs --record
//
// 为什么必须"打开状态"扫：默认视图的像素基线看不见这些组件 —— 素材中心 58 处
// AA 不达标（graphite）时，五个视觉基线用例全是 0 像素变化。
//
// 需要夹具：.venv/Scripts/python.exe verify_scene_canvas.py --serve 8011
import { createRequire } from 'node:module'
import fs from 'node:fs'
import path from 'node:path'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE, '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))
const base = process.env.DOCMIND_UI_TEST_URL || 'http://127.0.0.1:8011'
const BASELINE_FILE = 'docs/skin-states-baseline.json'
const RECORD = process.argv.includes('--record')

try {
  const r = await fetch(base + '/api/health', { signal: AbortSignal.timeout(4000) })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
} catch (e) {
  console.error(`夹具没起来：${base} 不可用（${e.message}）\n`
    + '  先跑：.venv/Scripts/python.exe verify_scene_canvas.py --serve 8011')
  process.exit(2)
}

// 每个状态都要真的走到，否则量的是空气。走到之后如果元素没出现，探针会抛错 ——
// 这比"静默扫了默认视图然后报干净"好。
// 「更多」下拉里能直达的面板。每个都必须等到自己的根节点出现 ——
// 等不到就抛错、记 -1，绝不记成"扫了默认视图的 0 处"。
async function openMore (p, item, appear) {
  await p.click('.wb-menu-trigger')
  await p.locator('.wb-menu-item').filter({ hasText: item }).first().click()
  await p.waitForSelector(appear, { timeout: 30000 })
}
const STATES = {
  async ask () {},
  async workbench (p) { await p.waitForSelector('.ft-row', { timeout: 60000 }) },
  async settings (p) {
    await p.click('.wb-settings-btn')
    await p.waitForSelector('.sv-nav-item.on', { timeout: 30000 })
    // 用量页有缓存行、智能体页有表单，两个都走一遍再扫
    await p.locator('.sv-nav-item').filter({ hasText: '智能体' }).first().click()
    await p.waitForTimeout(600)
  },
  async assets (p) {
    await p.waitForSelector('.ft-row', { timeout: 60000 })
    await p.locator('button').filter({ hasText: '素材' }).first().click()
    await p.waitForTimeout(2500)
  },
  async cockpit (p) {
    await p.click('.wb-brand')
    await p.keyboard.press('Alt+4')
    await p.waitForSelector('#wb-cockpit-chat-slot .cd-inputbar', { timeout: 60000 })
  },
  async harness (p) { await openMore(p, 'AI 运行台', '.hp-backdrop') },
  async gpu (p) { await openMore(p, 'GPU 监控', '.gp-backdrop') },
  // .te-panel 是常驻包裹层且不可见（Playwright 默认等"可见"，等它必超时）；
  // 真正的面板根是点开后才出现的 .te-pop。
  async tasks (p) { await openMore(p, '任务与生成', '.te-pop') },
  async flow (p) { await openMore(p, '流程图', '.fl-panel') },
  async changes (p) { await openMore(p, '改动总览', '.gc-panel') },
  async relation (p) { await openMore(p, '关系图', '.rg-panel') },
  async unity (p) { await openMore(p, 'Unity 图', '.ug-panel') },
  async symbolmap (p) { await openMore(p, '代码地图', '.sm-panel') },
}
const ROUTES = {
  '/workbench': ['workbench', 'settings', 'assets', 'cockpit',
    'harness', 'gpu', 'tasks', 'flow', 'changes', 'relation', 'unity', 'symbolmap'],
  '/': ['ask'],
}
const SKINS = ['paper', 'graphite', 'zinc']

const SWEEP = () => {
  const parse = v => { const m = /rgba?\(([^)]+)\)/.exec(v); if (!m) return null
    const q = m[1].split(/[\s,\/]+/).filter(x => x !== '').map(Number)
    return { rgb: [q[0], q[1], q[2]], a: q[3] === undefined ? 1 : q[3] } }
  const lum = c => { const f = x => { x /= 255; return x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4 }
    return .2126 * f(c[0]) + .7152 * f(c[1]) + .0722 * f(c[2]) }
  const bgOf = el => { const acc = []
    for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
      const c = parse(getComputedStyle(n).backgroundColor); if (c) { acc.push(c); if (c.a >= .999) break } }
    let bg = { rgb: [255, 255, 255], a: 1 }
    for (let i = acc.length - 1; i >= 0; i--) { const c = acc[i]
      bg = { rgb: [0, 1, 2].map(k => c.rgb[k] * c.a + bg.rgb[k] * (1 - c.a)), a: 1 } }
    return bg }
  const bad = []
  for (const el of document.querySelectorAll('body *')) {
    // 只判"自己直接写着文字"的元素；容器交给它的子元素自己算
    if (![...el.childNodes].some(n => n.nodeType === 3 && (n.textContent || '').trim())) continue
    // WCAG 1.4.3 豁免 inactive 控件；大字号门槛降到 3:1
    if (el.disabled || el.closest('[disabled]') || el.getAttribute('aria-disabled') === 'true') continue
    const cs = getComputedStyle(el)
    if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) continue
    const r = el.getBoundingClientRect(); if (!r.width || !r.height) continue
    const fg = parse(cs.color); if (!fg) continue
    const bg = bgOf(el)
    const eff = { rgb: [0, 1, 2].map(k => fg.rgb[k] * fg.a + bg.rgb[k] * (1 - fg.a)) }
    const L1 = lum(eff.rgb), L2 = lum(bg.rgb)
    const ratio = +(((Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05)).toFixed(2))
    const px = parseFloat(cs.fontSize) || 16
    const need = (px >= 24 || (px >= 18.66 && Number(cs.fontWeight) >= 700)) ? 3 : 4.5
    if (ratio < need) bad.push({ 元素: el.tagName.toLowerCase()
      + (typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\s+/)[0] : ''),
      比值: ratio, 字: cs.color, 底: `rgb(${bg.rgb.map(Math.round).join(',')})`,
      字号: px, 文本: (el.textContent || '').trim().slice(0, 14) })
  }
  return bad
}

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const counts = {}, samples = {}
let crashed = ''
try {
  // 每个状态独立开页：复用同一个页面时，设置弹窗是模态的，后面的「素材」标签和
  // Alt+4 都会被它挡住 —— 第一版就这么把 8 个状态全记成了 -1。
  for (const [route, names] of Object.entries(ROUTES)) for (const skin of SKINS) for (const name of names) {
    const key = `${route}|${name}|${skin}`
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
    try {
      await page.addInitScript(`localStorage.setItem("docmind.skin","${skin}")`)
      await page.goto(base + route, { waitUntil: 'domcontentloaded', timeout: 60000 })
      await page.waitForFunction(s => document.documentElement.dataset.skin === s, skin, { timeout: 30000 })
      await page.waitForSelector(route === '/' ? '.composer textarea' : '.ft-row', { timeout: 60000 })
      await STATES[name](page)
      await page.waitForTimeout(900)
      const bad = await page.evaluate(SWEEP)
      counts[key] = bad.length
      if (bad.length) samples[key] = bad.slice(0, 3)
    } catch (e) {
      counts[key] = -1   // 状态没走到：记成 -1，绝不记成 0
      samples[key] = [{ 元素: '探针失败', 比值: 0, 文本: String(e.message).split('\n')[0].slice(0, 44) }]
    } finally { await page.close() }
  }
} catch (e) { crashed = String(e && e.message || e).split('\n')[0] }
finally { await browser.close() }

if (crashed) { console.error('中途抛错：' + crashed); process.exit(1) }

if (RECORD) {
  fs.writeFileSync(BASELINE_FILE, JSON.stringify(counts, null, 2) + '\n', 'utf8')
  console.log(`已记账 ${Object.keys(counts).length} 个状态 × ${BASELINE_FILE}`)
  for (const [k, v] of Object.entries(counts)) {
    console.log(`  ${String(v).padStart(3)}  ${k}`)
    for (const s of (samples[k] || [])) console.log(`        ${s.元素} ${s.比值}:1  ${s.字} on ${s.底} 「${s.文本}」`)
  }
  process.exit(0)
}

if (!fs.existsSync(BASELINE_FILE)) {
  console.error(`没有基线文件 ${BASELINE_FILE}\n  先跑：node verify_skin_states.mjs --record`)
  process.exit(2)
}
const BASE = JSON.parse(fs.readFileSync(BASELINE_FILE, 'utf8'))
let failed = 0, checked = 0
for (const [key, now] of Object.entries(counts)) {
  const allowed = BASE[key]
  checked++
  if (allowed === undefined) { failed++; console.log(`FAIL   ${key}  基线里没有这个状态（新增状态要先 --record）`); continue }
  if (now < 0 || allowed < 0) {
    failed++; console.log(`FAIL   ${key}  探针没走通（现在 ${now} / 基线 ${allowed}）`)
    continue
  }
  if (now > allowed) {
    failed++
    console.log(`FAIL   ${key}  不达标 ${now} 处 > 基线 ${allowed} 处  ← 新引入的皮肤泄漏`)
    for (const s of (samples[key] || [])) console.log(`         ${s.元素} ${s.比值}:1  ${s.字} on ${s.底} 「${s.文本}」`)
  } else if (now < allowed) {
    console.log(`PASS↓  ${key}  不达标 ${now} 处 < 基线 ${allowed} 处  → 变好了，记得 --record 把基线降下来`)
  } else {
    console.log(`PASS   ${key}  ${now} 处（与基线一致）`)
  }
}
console.log(`\n状态 AA 棘轮：${checked} 个状态，${failed ? `${failed} 项失败` : '无退步'}`)
process.exit(failed ? 1 : 0)
