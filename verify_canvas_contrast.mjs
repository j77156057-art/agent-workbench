// 画布非文字对比度探针：量「节点/连线/色块这些图形本身，在三套皮肤下看不看得见」。
//
// 为什么单独一个脚本：verify_skin_states.mjs 只测文字（WCAG 1.4.3），
// 而画布的身份是**颜色编码**的（场景/预制体/脚本/材质…各一种色），
// 归 1.4.11 管，门槛是 3:1。文字全 0 不代表画布看得见。
//
// 棘轮语义与状态表一致：只准变好不准变坏；图形数为 0 记 -1（未量到），
// 绝不记 0 —— 空画布量出来的"干净"是假账。
//   记账：node verify_canvas_contrast.mjs --record
//   看明细：SHOW=20 node verify_canvas_contrast.mjs
//
// 需要夹具：.venv/Scripts/python.exe verify_scene_canvas.py --serve 8013
import { createRequire } from 'node:module'
import fs from 'node:fs'
import path from 'node:path'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE, '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))
const base = process.env.DOCMIND_UI_TEST_URL || 'http://127.0.0.1:8011'
const BASELINE_FILE = 'docs/canvas-contrast-baseline.json'
const RECORD = process.argv.includes('--record')
const SHOW = Number(process.env.SHOW || 6)
const FLOOR = 3   // WCAG 1.4.11：非文字图形对象 3:1

// 每个画布：菜单里怎么点进去 + 量哪个容器 + 有没有真节点怎么确认。
// 容器取整个面板而不是只取 svg：图例色块就是画布配色的钥匙，且 Vue Flow 的
// 节点卡是 HTML（色相在它的左边框上），只看 svg 会漏掉。
const CANVASES = {
  '流程图': { menu: '流程图', root: '.fl-panel', need: '.vue-flow__node' },
  '关系图': { menu: '关系图', root: '.rg-panel', need: '.rg-legend' },
  'Unity 图': { menu: 'Unity 图', root: '.ug-panel', need: '.ug-legend', empty: '.ug-state p' },
  // 场景图不进用例：它在「运行游戏」面板里，入口 .sr-trigger 要真 Godot 工程才出现。
  // 留着一行永远 -1 的门禁等于教人无视门禁，所以先删掉，等夹具能起引擎再补。
}
const SKINS = ['paper', 'graphite', 'zinc']

// 注意：这个函数会被序列化成字符串塞进页面执行，所以**不能引用外层常量**
// （第一版引了 FLOOR，结果 12 项全部 ReferenceError，看着像"画布全空"）。
// dark=true 时另外统计"白岛"：深色皮肤里一块没跟皮肤的浅色表面。
// 状态棘轮看不见这个 —— HarnessPanel 收编前在 graphite 下就是一堆白色卡片，
// 而它的不达标文字只有 10 处。
const SWEEP = (rootSel, FLOOR, dark) => {
  const root = document.querySelector(rootSel)
  if (!root) return { err: '视口容器没出现' }
  const parse = v => {
    const m = /^rgba?\(([^)]+)\)/.exec(v || '')
    if (m) { const q = m[1].split(/[\s,\/]+/).filter(x => x !== '').map(Number)
      return { rgb: [q[0], q[1], q[2]], a: q[3] === undefined ? 1 : q[3] } }
    const h = /^#([0-9a-fA-F]{3,8})$/.exec((v || '').trim())
    if (h) { const s = h[1]
      const f = s.length === 3 || s.length === 4 ? s.split('').map(c => c + c).join('') : s.slice(0, 6)
      const a = s.length >= 8 ? parseInt(f ? s.slice(6, 8) : 'ff', 16) / 255 : 1
      return { rgb: [parseInt(f.slice(0, 2), 16), parseInt(f.slice(2, 4), 16), parseInt(f.slice(4, 6), 16)], a } }
    return null
  }
  const lum = c => { const f = x => { x /= 255; return x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4 }
    return .2126 * f(c[0]) + .7152 * f(c[1]) + .0722 * f(c[2]) }
  const over = (c, bg) => ({ rgb: [0, 1, 2].map(k => c.rgb[k] * c.a + bg.rgb[k] * (1 - c.a)), a: 1 })
  const bgOf = el => {
    const acc = []
    for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
      const gi = n.nodeType === 1 ? getComputedStyle(n).backgroundImage : 'none'
      if (gi && gi !== 'none') {   // 渐变底：取最浅那个色标，别把"透明"当真
        const stops = (gi.match(/rgba?\([^)]*\)|#[0-9a-fA-F]{3,8}\b/g) || []).map(parse).filter(Boolean)
        if (stops.length) { let best = null
          for (const s of stops) { const c = over(s, { rgb: [255, 255, 255], a: 1 })
            if (!best || lum(c.rgb) > lum(best.rgb)) best = c }
          return best } }
      const c = parse(getComputedStyle(n).backgroundColor)
      if (c) { if (c.a >= .999) return over(c, { rgb: [255, 255, 255], a: 1 })
        acc.push(c) }
    }
    let bg = { rgb: [255, 255, 255], a: 1 }
    for (let i = acc.length - 1; i >= 0; i--) bg = over(acc[i], bg)
    return bg
  }
  const ratio = (c, bg) => { const L1 = lum(c.rgb), L2 = lum(bg.rgb)
    return +(((Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05)).toFixed(2)) }

  // 表面令牌的实际值：填充色等于其中任何一个 = 这是"底板"（节点标签垫色），
  // 不是身份线索，1.4.11 管不着它。不排掉的话关系图会一直报 4 处 1.23:1 的假债。
  const SURF_NAMES = ['--bg', '--surface', '--bg-raised', '--bg-hover', '--bg-selected']
  const probe = document.createElement('div')
  document.body.appendChild(probe)
  const resolve = v => { probe.style.color = '#000'; probe.style.color = v || ''
    return parse(getComputedStyle(probe).color) }
  const rs = getComputedStyle(document.documentElement)
  const surfaces = SURF_NAMES.map(n => resolve(rs.getPropertyValue(n).trim())).filter(Boolean)
    .map(c => over(c, { rgb: [255, 255, 255], a: 1 }))
  const isPlate = c => surfaces.some(s => [0, 1, 2].every(k => Math.abs(s.rgb[k] - c.rgb[k]) <= 2))
  probe.remove()
  const marks = [], bad = [], invalid = [], islands = []
  for (const el of root.querySelectorAll('*')) {
    if (el.closest('defs')) continue
    const tag = el.tagName.toLowerCase()
    if (tag === 'text' || tag === 'tspan' || tag === 'title') continue   // 文字交给状态棘轮
    // 只量"自己着墨"的图元。g / svg / defs 的 fill 是从祖先继承来的容器属性，
    // 它自己不画东西 —— 第一版把 27 条 .vue-flow__edge 的 <g> 当成 27 个黑色图形，
    // 报出 1.38:1 的假账（真实情况是线用 stroke，箭头在 defs 里）。
    if (tag === 'g' || tag === 'svg' || tag === 'defs' || tag === 'marker'
      || tag === 'clippath' || tag === 'mask' || tag === 'filter' || tag === 'use') continue
    const cs = getComputedStyle(el)
    if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) < .05) continue
    let r = null
    try { r = el.getBoundingClientRect() } catch { /* detached */ }
    if (!r || !r.width || !r.height) continue
    // 内联样式里拼出来的非法颜色（regionColor 的 hsl + '55' 那类）会静默回落，
    // CSS.supports 能当场抓到
    if (el.hasAttribute('style')) for (const d of (el.getAttribute('style') || '').split(';')) {
      const i = d.indexOf(':')
      if (i < 0) continue
      const p = d.slice(0, i).trim(), v = d.slice(i + 1).trim()
      if (/(color|fill|stroke|background)/i.test(p) && v && !CSS.supports(p, v))
        invalid.push(`${tag}.${typeof el.className === 'string' ? el.className : ''} ${p}:${v}`)
    }
    const bg = bgOf(el)
    if (dark) {
      const own = parse(el.ownerSVGElement ? cs.fill : cs.backgroundColor)
      if (own && own.a > .9 && lum(own.rgb) > .55) {
        const r2 = el.getBoundingClientRect()
        if (r2.width * r2.height >= 400)   // 小色块不算岛
          islands.push(`${tag}.${typeof el.className === 'string' ? el.className.trim().split(/\s+/)[0] : ''} ${own.a >= .999 ? '' : '半'}底 ${Math.round(r2.width)}×${Math.round(r2.height)} 亮度 ${lum(own.rgb).toFixed(2)}`)
      }
    }
    const sw = parseFloat(cs.strokeWidth) || (cs.strokeWidth === 'thin' ? .5 : 0)
    const kind = el.ownerSVGElement ? 'svg' : 'html'
    const push = (what, col, note) => {
      const c = parse(col)
      if (!c || c.a === 0) return
      if (what === '填充' && isPlate(c)) return   // 底板不算身份图形
      const eff = over(c, bg)
      if (c.a < .999) return marks.push({ what, skip: true })   // 半透明不算身份色（是遮罩/阴影）
      marks.push({ what, col: col.trim(), bg: `rgb(${bg.rgb.map(Math.round).join(',')})`,
        ratio: ratio(eff, bg), kind, note,
        tag: tag + (typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\s+/)[0] : '') })
      const last = marks[marks.length - 1]
      if (last.ratio < FLOOR) bad.push(last)
    }
    if (kind === 'svg') {
      if (cs.stroke !== 'none' && sw >= 1) push('描边', cs.stroke, `${sw}px`)
      if (cs.fill !== 'none' && r.width * r.height >= 60) push('填充', cs.fill, `${Math.round(r.width)}×${Math.round(r.height)}`)
    } else {
      for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
        const w = parseFloat(cs['border' + side + 'Width']) || 0
        if (w >= 1.5) push('边框' + side.toLowerCase(), cs['border' + side + 'Color'], `${w}px`)
      }
    }
  }
  return { marks: marks.filter(m => !m.skip), bad, invalid, islands }
}

try {
  const h = await fetch(base + '/api/health', { signal: AbortSignal.timeout(4000) })
  if (!h.ok) throw new Error(`HTTP ${h.status}`)
} catch (e) {
  console.error(`夹具没起来：${base} 不可用（${e.message}）\n  先跑：.venv/Scripts/python.exe verify_scene_canvas.py --serve 8013`)
  process.exit(2)
}

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const counts = {}, detail = {}
let crashed = ''
try {
  for (const skin of SKINS) for (const [name, c] of Object.entries(CANVASES)) {
    const key = `${name}|${skin}`
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
    try {
      await page.addInitScript(`localStorage.setItem("docmind.skin","${skin}")`)
      await page.goto(base + '/workbench', { waitUntil: 'domcontentloaded', timeout: 60000 })
      await page.waitForFunction(s => document.documentElement.dataset.skin === s, skin, { timeout: 30000 })
      await page.waitForSelector('.ft-row', { timeout: 60000 })
      if (c.menu) {
        await page.click('.wb-menu-trigger')
        await page.locator('.wb-menu-item').filter({ hasText: c.menu }).first().click()
      } else if (await page.$(c.trigger)) await page.click(c.trigger)
      else throw new Error(`入口不在页面上（${c.trigger} 需要真引擎工程才出现）`)
      await page.waitForSelector(c.root, { timeout: 30000 })
      // need 只是"有没有数据"的探针，等不到不算失败：让它落到下面的
      // 图形数=0 分支，报出面板自己写的原因（比 Playwright 的超时文案有用）
      if (c.need) await page.waitForSelector(c.need, { timeout: 12000 }).catch(() => {})
      await page.waitForTimeout(1200)
      const res = await page.evaluate(`(${SWEEP.toString()})(${JSON.stringify(c.root)}, ${FLOOR}, ${skin === 'graphite'})`)
      if (res.err) throw new Error(res.err)
      // 有容器但一个图形都没量到 = 画布是空的，这种"0 处不达标"不能记账
      if (!res.marks.length) {
        const why = c.empty ? await page.$eval(c.empty, e => (e.textContent || '').trim().slice(0, 46)).catch(() => '') : ''
        throw new Error(why ? `没数据：${why}` : '画布内没有可量图形（多半是没数据）')
      }
      counts[key] = res.bad.length
      const groups = new Map()
      for (const b of res.bad) {
        const g = `${b.col} ${b.what} → ${b.bg}`
        const e = groups.get(g) || { n: 0, 比值: b.ratio, 例: b.tag + ' ' + b.note }
        e.n++; e.比值 = Math.min(e.比值, b.ratio); groups.set(g, e)
      }
      detail[key] = {
        图形: res.marks.length,
        最差: [...groups].sort((a, b) => a[1].比值 - b[1].比值).slice(0, SHOW)
          .map(([g, v]) => `${g}  ${v.比值}:1  ×${v.n}  例:${v.例}`),
        非法内联色: [...new Set(res.invalid)].slice(0, SHOW),
        白岛: [...new Set(res.islands)].slice(0, SHOW),
      }
    } catch (e) {
      counts[key] = -1
      detail[key] = { 图形: 0, 最差: [], 非法内联色: [], 白岛: [], 原因: String(e.message).split('\n')[0].slice(0, 60) }
    } finally { await page.close() }
  }
} catch (e) { crashed = String(e && e.message || e).split('\n')[0] }
finally { await browser.close() }
if (crashed) { console.error('中途抛错：' + crashed); process.exit(1) }

if (RECORD) {
  fs.writeFileSync(BASELINE_FILE, JSON.stringify(counts, null, 2) + '\n', 'utf8')
  console.log(`已记账 ${Object.keys(counts).length} 个画布×皮肤 × ${BASELINE_FILE}\n`)
  for (const [k, v] of Object.entries(counts)) {
    console.log(`  ${String(v).padStart(3)}  ${k}   图形 ${detail[k].图形} 个${detail[k].原因 ? '  ⚠ ' + detail[k].原因 : ''}`)
    for (const s of detail[k].最差) console.log('        ' + s)
    for (const s of detail[k].非法内联色) console.log('        非法内联色：' + s)
    for (const s of (detail[k].白岛 || [])) console.log('        白岛：' + s)
  }
  process.exit(0)
}
if (!fs.existsSync(BASELINE_FILE)) { console.error(`没有基线，先 --record`); process.exit(2) }
const BASE = JSON.parse(fs.readFileSync(BASELINE_FILE, 'utf8'))
let failed = 0, checked = 0
for (const [key, now] of Object.entries(counts)) {
  checked++
  const allowed = BASE[key]
  if (allowed === undefined) { failed++; console.log(`FAIL   ${key}  基线里没有这个画布`); continue }
  if (now < 0 || allowed < 0) { failed++; console.log(`FAIL   ${key}  没量到（现在 ${now} / 基线 ${allowed}）`); continue }
  if (now > allowed) { failed++; console.log(`FAIL   ${key}  不可见图形 ${now} 个 > 基线 ${allowed} 个`)
    for (const s of detail[key].最差) console.log('         ' + s)
  } else console.log(`PASS   ${key}  不可见图形 ${now} 个（基线 ${allowed}）· 共 ${detail[key].图形} 个图形`)
}
console.log(`\n画布 3:1 探针：${checked} 项，${failed ? `${failed} 项失败` : '无退步'}`)
process.exit(failed ? 1 : 0)
