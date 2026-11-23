// DocMind 设计系统 · 人工评审项 A1–A6 量具
//
// 为什么要有这个脚本：docs/DESIGN-ACCEPTANCE.md §5 的 A1–A6 是"人工评审项"，
// 但"人工"不等于"凭感觉"——每一条都有可量化的判据。本脚本把判据变成数字，
// 并且用 --selftest 证明量具本身不是瞎的。
//
// 用法：
//   node tools/verify_design_acceptance_a.mjs              全量（4 皮肤 × 2 页面）
//   node tools/verify_design_acceptance_a.mjs --quick      只跑 zinc × 两页
//   node tools/verify_design_acceptance_a.mjs --selftest   注入已知缺陷，验证量具能抓到
//
// 依赖：playwright-core（复用 WorkBuddy 自带的那份，不新增 npm 包）+ 本机 Edge。
import { createRequire } from 'node:module'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import net from 'node:net'
import path from 'node:path'

const require = createRequire(import.meta.url)
// --help / -h：只打印用法并退出，供文档命令验证与人工查阅使用（不启动浏览器）。
if (process.argv.includes('--help') || process.argv.includes('-h')) {
  console.log(`用法：node tools/verify_design_acceptance_a.mjs [--quick | --rail | --selftest | --help]

  默认        四套皮肤 × 两页，跑 A1 字号层级 / A2 间距栅格 / A3 三态 /
              A4 对比度 / A5 键盘 / A6 溢出
  --quick     只跑 zinc 两页（约 1/4 时间，日常开发用）
  --rail      只跑轨道层次（导航栏与主区的明度关系）
  --selftest  量具自证：注入已知破坏，确认判据能抓到
  --help      打印本说明并退出

  产物：docs/screenshots/design-a/raw.json`);
  process.exit(0)
}

const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE || process.env.HOME || '', '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))

const QUICK = process.argv.includes('--quick')
const SELFTEST = process.argv.includes('--selftest')
const OUT_DIR = 'docs/screenshots/design-a'
const SKINS = QUICK || SELFTEST ? ['zinc'] : ['zinc', 'graphite', 'paper', 'seed', 'moyu', 'moyu-night']
const CASES = [
  { name: 'workbench', route: '/workbench', stable: '.ft-head-title' },
  { name: 'ask', route: '/', stable: '.composer textarea' },
]

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
  const py = ['.venv/Scripts/python.exe', '.venv/bin/python', 'python'].find(c => !c.includes('/') || fs.existsSync(c)) || 'python'
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

// ---------------- 页面内量具（真函数，交给 Playwright 序列化） ----------------

function M_A1() {
  const probe = document.createElement('div')
  probe.style.cssText = 'position:absolute;left:-9999px;visibility:hidden;'
  document.body.appendChild(probe)
  const readPx = (n) => { probe.style.fontSize = 'var(' + n + ')'; return parseFloat(getComputedStyle(probe).fontSize) }
  const displayPx = readPx('--fs-display')
  const bodyPx = readPx('--fs-body')
  probe.remove()

  const visible = (el) => {
    const r = el.getBoundingClientRect()
    if (r.width < 2 || r.height < 2) return false
    const cs = getComputedStyle(el)
    return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0.05
  }
  const ownText = (el) => {
    for (const n of el.childNodes) if (n.nodeType === 3 && n.nodeValue.trim().length > 1) return true
    return false
  }
  const rows = []
  for (const el of document.querySelectorAll('body *')) {
    if (!ownText(el) || !visible(el)) continue
    const r = el.getBoundingClientRect()
    if (r.bottom < 0 || r.top > innerHeight) continue
    rows.push({
      tag: el.tagName.toLowerCase(),
      cls: (el.getAttribute('class') || '').slice(0, 44),
      fs: parseFloat(getComputedStyle(el).fontSize),
      text: (el.textContent || '').trim().slice(0, 24),
    })
  }
  const display = rows.filter(r => r.fs >= displayPx - 0.6)
  const buckets = {}
  for (const r of rows) { const k = r.fs.toFixed(1); buckets[k] = (buckets[k] || 0) + 1 }
  const top = Object.entries(buckets).sort((a, b) => b[1] - a[1]).slice(0, 6)
    .map(([fs, n]) => ({ fs: +fs, n, pct: +(100 * n / rows.length).toFixed(1) }))
  let acc = 0, need = 0
  for (const b of top) { if (acc / rows.length >= 0.9) break; acc += b.n; need++ }
  return { displayPx, bodyPx, textEls: rows.length, displayCount: display.length,
           displaySamples: display.slice(0, 5), topBuckets: top, bucketsFor90pct: need }
}

function M_A2() {
  // 2026-11-20 与 tools/design_acceptance.py 的 r04 统一口径：
  // 偶数合规（2px 微调栅格），奇数只有 1px（细线补偿）合规。
  // 此前这里用硬编码集合 {2,6,10,14,18,22,28,30,36,44}，漏了 26/34/38/42/46 —— 
  // 那些合法偶数会被误判成违规，把 A2 的违规率算高。
  const isMain = (v) => v % 4 === 0
  const isFine = (v) => v % 2 === 0 && v % 4 !== 0
  const allowed = (v) => v === 1 || v % 2 === 0
  const PROPS = ['paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
                 'marginTop', 'marginBottom', 'marginLeft', 'marginRight', 'rowGap', 'columnGap']
  const seen = [], off = []
  let main = 0, fine = 0
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect()
    if (r.width < 2 || r.height < 2) continue
    const cs = getComputedStyle(el)
    for (const p of PROPS) {
      const v = Math.round(parseFloat(cs[p]) * 100) / 100
      // >80px 的值不是"设计间距"，而是 margin:auto 之类的布局计算结果
      // （getComputedStyle 会把 auto 解析成实际像素，如 505.28）。栅格只针对小区间。
      if (!isFinite(v) || v <= 0 || v > 80) continue
      seen.push(v)
      if (isMain(v)) main++
      else if (isFine(v)) fine++
      if (!allowed(v)) {
        off.push({ tag: el.tagName.toLowerCase(), cls: (el.getAttribute('class') || '').slice(0, 34), prop: p, v })
      }
    }
  }
  const hist = {}
  for (const v of seen) hist[v] = (hist[v] || 0) + 1
  const top = Object.entries(hist).sort((a, b) => b[1] - a[1]).slice(0, 10).map(([v, n]) => ({ v: +v, n }))
  const trio = seen.filter(v => [6, 8, 10, 14, 16, 18, 30, 32, 34].includes(v)).length
  return { decls: seen.length, offGrid: off.length,
           offPct: +(100 * off.length / Math.max(1, seen.length)).toFixed(1),
           mainPct: +(100 * main / Math.max(1, seen.length)).toFixed(1),
           finePct: +(100 * fine / Math.max(1, seen.length)).toFixed(1),
           topValues: top, trioPct: +(100 * trio / Math.max(1, seen.length)).toFixed(1),
           offSamples: off.slice(0, 10) }
}

function M_A4_COLLECT() {
  const visible = (el) => {
    const r = el.getBoundingClientRect()
    if (r.width < 8 || r.height < 8) return false
    const cs = getComputedStyle(el)
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) < 0.5) return false
    // WCAG 1.4.3 豁免失效控件（disabled / aria-disabled）。不算"设计缺陷"。
    if (el.disabled === true || el.getAttribute('aria-disabled') === 'true') return false
    // 祖先链上的 disabled 也豁免
    for (let p = el; p && p !== document.body; p = p.parentElement) {
      if (p.disabled === true || p.getAttribute?.('aria-disabled') === 'true') return false
    }
    return true
  }
  // 遮挡/裁切校验：rect 在视口内 ≠ 真的画出来了。
  // 实测 `.ov-recent-empty` 的 rect 合法但被父级裁掉，截图位置只有空白背景，
  // 不校验就会把"背景对背景"量成 1.48:1 的假缺陷。多点采样，命中须是本元素或后代。
  const painted = (el) => {
    const r = el.getBoundingClientRect()
    const pts = [[0.5, 0.5], [0.25, 0.5], [0.75, 0.5], [0.5, 0.3], [0.5, 0.7]]
    let hit = 0
    for (const [fx, fy] of pts) {
      const x = Math.min(innerWidth - 1, Math.max(0, r.left + r.width * fx))
      const y = Math.min(innerHeight - 1, Math.max(0, r.top + r.height * fy))
      const n = document.elementFromPoint(x, y)
      if (n && (n === el || el.contains(n) || n.contains(el))) hit++
    }
    return hit >= 2
  }
  const ownText = (el) => [...el.childNodes].some(n => n.nodeType === 3 && n.nodeValue.trim().length > 1)
  const out = []
  for (const el of document.querySelectorAll('body *')) {
    if (!ownText(el) || !visible(el)) continue
    const r = el.getBoundingClientRect()
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue
    const fs = parseFloat(getComputedStyle(el).fontSize)
    if (fs < 10) continue
    if (!painted(el)) continue
    out.push({
      sel: el.tagName.toLowerCase() + '.' + (el.getAttribute('class') || '').split(/\s+/)[0],
      text: (el.textContent || '').trim().slice(0, 18), fs,
      rect: { x: Math.max(0, r.left), y: Math.max(0, r.top),
              w: Math.min(r.width, innerWidth - Math.max(0, r.left)),
              h: Math.min(r.height, innerHeight - Math.max(0, r.top)) },
    })
  }
  out.sort((a, b) => (b.rect.w * b.rect.h) - (a.rect.w * a.rect.h))
  return out.slice(0, 40)
}

// A4 第一层：截图采样（粗筛）。只用来发现"可疑"，不作为结论。
// 实测教训：11px 小字的笔画像素太少，取"最高对比度 8% 的中位数"实际取到的是
// 抗锯齿边缘，会系统性低估约 1.0（实测 4.08 的元素真值 5.24）。字越小低估越狠。
async function M_A4_MEASURE({ b64, targets }) {
  const img = new Image()
  img.src = 'data:image/png;base64,' + b64
  await img.decode()
  const cv = document.createElement('canvas')
  cv.width = img.width; cv.height = img.height
  const cx = cv.getContext('2d', { willReadFrequently: true })
  cx.drawImage(img, 0, 0)
  const sx = img.width / innerWidth, sy = img.height / innerHeight

  const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4) }
  const lum = (p) => 0.2126 * lin(p[0]) + 0.7152 * lin(p[1]) + 0.0722 * lin(p[2])
  const ratio = (a, b) => {
    const l1 = lum(a), l2 = lum(b), hi = Math.max(l1, l2), lo = Math.min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)
  }

  const results = []
  for (const t of targets) {
    const x = Math.round(t.rect.x * sx), y = Math.round(t.rect.y * sy)
    const w = Math.max(1, Math.round(t.rect.w * sx)), h = Math.max(1, Math.round(t.rect.h * sy))
    if (x < 0 || y < 0 || x + w > img.width || y + h > img.height) continue
    let data
    try { data = cx.getImageData(x, y, w, h).data } catch { continue }
    const hist = new Map()
    for (let i = 0; i < data.length; i += 4) {
      const k = (data[i] >> 3) + ',' + (data[i + 1] >> 3) + ',' + (data[i + 2] >> 3)
      const e = hist.get(k)
      if (e) e.n++
      else hist.set(k, { n: 1, c: [data[i], data[i + 1], data[i + 2]] })
    }
    if (!hist.size) continue
    const sorted = [...hist.values()].sort((a, b) => b.n - a.n)
    const bg = sorted[0].c
    const scored = [...hist.values()].map(e => ({ c: e.c, r: ratio(e.c, bg), n: e.n })).sort((a, b) => b.r - a.r)
    const topN = Math.max(1, Math.floor(scored.length * 0.08))
    const pick = scored.slice(0, topN)
    // 取前 8% 的中位像素作为文字色：避开抗锯齿边缘（最极端的那几个像素偏亮/偏暗）
    const mid = pick[Math.floor(pick.length / 2)]
    results.push({ ...t, bg, fg: mid.c, ratio: +mid.r.toFixed(2) })
  }
  return results
}

// A4 第二层（判定层）：解析真实色值算对比度。
// 无抗锯齿噪声 —— 把 CSS 颜色交给 canvas 填充后读回 RGB，oklch / color-mix 都会
// 被浏览器正确解析。只有"背景含渐变/图片"时才退回截图采样并标记 uncertain。
function M_A4_PRECISE(items) {
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
  const ratio = (a, b) => { const l1 = lum(a), l2 = lum(b), hi = Math.max(l1, l2), lo = Math.min(l1, l2); return (hi + 0.05) / (lo + 0.05) }
  const over = (fg, bg) => [0, 1, 2].map(i => Math.round(fg[i] * fg[3] + bg[i] * (1 - fg[3])))

  const find = (it) => {
    if (it.sel && !it.sel.endsWith('.')) {
      try {
        for (const el of document.querySelectorAll(it.sel)) {
          if ((el.textContent || '').trim().startsWith(it.text.slice(0, 6))) return el
        }
      } catch { /* 选择器非法（如 span.），走兜底 */ }
    }
    return [...document.querySelectorAll('body *')].find(el => {
      if (![...el.childNodes].some(n => n.nodeType === 3 && n.nodeValue.trim().length > 1)) return false
      if (!(el.textContent || '').trim().startsWith(it.text.slice(0, 6))) return false
      return Math.abs(parseFloat(getComputedStyle(el).fontSize) - it.fs) < 0.7
    }) || null
  }

  const out = []
  for (const it of items) {
    const el = find(it)
    if (!el) { out.push({ sel: it.sel, text: it.text, missing: true }); continue }
    const cs = getComputedStyle(el)
    let bg = null, hasImage = false
    for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
      const s = getComputedStyle(p)
      if (s.backgroundImage && s.backgroundImage !== 'none') hasImage = true
      const rgba = toRGBA(s.backgroundColor)
      if (rgba[3] >= 0.999) { bg = rgba.slice(0, 3); break }
    }
    const fgRaw = toRGBA(cs.color)
    const fg = fgRaw[3] < 0.999 ? over(fgRaw, bg || [255, 255, 255]) : fgRaw.slice(0, 3)
    out.push({
      sel: it.sel, text: (el.textContent || '').trim().slice(0, 18), fs: parseFloat(cs.fontSize),
      color: cs.color, bgColor: bg ? `rgb(${bg.join(',')})` : null,
      ratio: bg ? +ratio(fg, bg).toFixed(2) : null,
      uncertain: hasImage || !bg, method: 'computed-color',
    })
  }
  return out
}

function M_A6(inject) {
  const skipOverflow = (cs) => ['auto', 'scroll', 'hidden', 'clip'].includes(cs.overflowX)
  const scan = () => {
    const hits = []
    for (const el of document.querySelectorAll('body *')) {
      const cs = getComputedStyle(el)
      if (skipOverflow(cs)) continue
      if (el.clientWidth < 40) continue
      if (el.scrollWidth > el.clientWidth + 1) {
        hits.push({ tag: el.tagName.toLowerCase(), cls: (el.getAttribute('class') || '').slice(0, 40),
                    client: el.clientWidth, scroll: el.scrollWidth })
      }
    }
    return hits
  }
  const before = scan()
  if (!inject) return { before, after: [], injected: 0 }

  const picked = []
  for (const el of document.querySelectorAll('body *')) {
    if (picked.length >= 12) break
    const hasOwn = [...el.childNodes].some(n => n.nodeType === 3 && n.nodeValue.trim().length > 4)
    if (!hasOwn) continue
    const r = el.getBoundingClientRect()
    if (r.width < 80 || r.width > 900 || r.height < 8) continue
    picked.push(el)
  }
  const backups = picked.map(el => el.textContent)
  for (const el of picked) {
    const t = (el.textContent || '').trim()
    el.textContent = t + ' ' + t
  }
  const after = scan()
  picked.forEach((el, i) => { el.textContent = backups[i] })
  return { before, after, injected: picked.length }
}

// ---------------- 采集 ----------------

async function openCase(browser, base, c, skin) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await ctx.newPage()
  await page.addInitScript(`localStorage.setItem("docmind.skin","${skin}");localStorage.setItem("docmind.skinLocked","1")`)
  await page.goto(base + c.route, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(c.stable, { timeout: 25000 })
  await page.waitForTimeout(900)
  await page.addStyleTag({ content: '*,*::before,*::after{animation:none !important;transition:none !important}' })
  return { ctx, page }
}

async function captureB64(page) {
  const client = await page.context().newCDPSession(page)
  const { data } = await client.send('Page.captureScreenshot', { format: 'png' })
  await client.detach()
  return data
}

async function runCase(browser, base, c, skin) {
  const { ctx, page } = await openCase(browser, base, c, skin)
  const out = { case: c.name, skin }
  out.A1 = await page.evaluate(M_A1)
  out.A2 = await page.evaluate(M_A2)

  const targets = await page.evaluate(M_A4_COLLECT)
  if (targets.length) {
    const b64 = await captureB64(page)
    const coarse = await page.evaluate(M_A4_MEASURE, { b64, targets })
    out.A4 = coarse.map(x => ({ ...x, method: 'screenshot' }))

    // 对临界/可疑值做精确复测：截图法在小字号上会低估，不能直接用来判"不达标"
    const weak = out.A4.filter(x => x.ratio < 5.5)
    if (weak.length) {
      const precise = await page.evaluate(M_A4_PRECISE,
        weak.map(w => ({ sel: w.sel, text: w.text, fs: w.fs })))
      const map = new Map(precise.filter(p => !p.missing && p.ratio != null).map(p => [p.text + '|' + p.fs, p]))
      out.A4 = out.A4.map(x => {
        const p = map.get(x.text + '|' + x.fs)
        if (!p) return x
        return { ...x, ratio: p.ratio, color: p.color, bgColor: p.bgColor,
                 method: p.uncertain ? 'screenshot(uncertain-bg)' : 'computed-color' }
      })
      out.A4_preciseCount = precise.filter(p => !p.missing).length
    }
  } else out.A4 = []

  out.A6 = await page.evaluate(M_A6, true)
  await ctx.close()
  return out
}

async function runInteraction(browser, base, c, skin) {
  const { ctx, page } = await openCase(browser, base, c, skin)
  const out = { case: c.name, skin }

  out.normal = await page.evaluate(() => ({
    hasEmpty: !!document.querySelector('[class*="empty"],[class*="no-data"],[class*="nodata"]'),
    hasSkeleton: !!document.querySelector('[class*="skeleton"],[aria-busy="true"]'),
    hasErrorNode: !!document.querySelector('[class*="error"],[class*="retry"]'),
    bodyTextLen: document.body.innerText.trim().length,
  }))

  await page.route('**/api/**', route => route.fulfill({
    status: 502, contentType: 'application/json', body: '{"ok":false,"error":"simulated"}',
  }))
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  out.degraded = await page.evaluate(() => {
    const txt = document.body.innerText.trim()
    return {
      bodyTextLen: txt.length,
      blank: txt.length < 40,
      mentionsError: /失败|错误|重试|无法|error|retry/i.test(txt),
      snippet: txt.slice(0, 120),
    }
  })
  await page.unroute('**/api/**')
  await ctx.close()
  return out
}

async function runKeyboard(browser, base, c, skin) {
  const { ctx, page } = await openCase(browser, base, c, skin)
  const out = { case: c.name, skin, stops: [], lost: 0, lostAt: [], covered: 0 }
  await page.evaluate(() => document.body.focus())

  const STEPS = 45
  // Tab 走完一圈会把焦点交回浏览器 UI（activeElement 变 body），这是正常行为，
  // 不是缺陷。只有"中段停靠"才是真断点。末尾阈值取 78%。
  const TAIL = Math.floor(STEPS * 0.78)

  for (let i = 0; i < STEPS; i++) {
    await page.keyboard.press('Tab')
    const info = await page.evaluate(() => {
      const el = document.activeElement
      if (!el || el === document.body || el === document.documentElement) return { lost: true }
      const r = el.getBoundingClientRect()
      const cx = Math.min(innerWidth - 1, Math.max(0, r.left + r.width / 2))
      const cy = Math.min(innerHeight - 1, Math.max(0, r.top + r.height / 2))
      const hit = document.elementFromPoint(cx, cy)
      const visible = r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight
      const covered = hit && !el.contains(hit) && !hit.contains(el)
      return { lost: false, tag: el.tagName.toLowerCase(),
               cls: (el.getAttribute('class') || '').slice(0, 34),
               top: Math.round(r.top), left: Math.round(r.left), visible,
               coveredBy: covered ? (hit.getAttribute('class') || hit.tagName).slice(0, 30) : null }
    })
    if (info.lost) { out.lost++; out.lostAt.push(i); continue }
    if (!info.visible) out.lost++
    if (info.coveredBy) out.covered++
    out.stops.push({ i, ...info })
  }

  // 区分三种"焦点消失"：
  //   ① 走完一圈交回浏览器 UI，再 Tab 从头开始（next.top 显著小于 prev.top）→ 正常
  //   ② 真正的中段断裂（下一个停靠还在原位附近甚至更靠下）→ 缺陷
  //   ③ 焦点落在视口外/零尺寸元素 → 缺陷
  // 用位置比例判断会误判：ask 页一圈只有 19 个停靠，#19 落在 42% 处，看着像"中段"，
  // 实际是循环边界。判据要抓行为，不能抓序号。
  const breaks = []
  for (const i of out.lostAt) {
    const prev = out.stops.filter(s => s.i < i).pop()
    const next = out.stops.find(s => s.i > i)
    if (!prev || !next) continue
    if (next.top < prev.top - 100) continue      // 回到顶部 = 一圈结束
    breaks.push({ at: i, prev: prev.cls, next: next.cls, prevTop: prev.top, nextTop: next.top })
  }
  out.breaks = breaks
  out.cycles = out.lostAt.length - breaks.length

  // 逆序跳同样排除"循环回到顶部"这一类
  let backward = 0
  const backwardDetail = []
  for (let i = 1; i < out.stops.length; i++) {
    const a = out.stops[i - 1], b = out.stops[i]
    if (b.top < a.top - 40) {
      backward++
      backwardDetail.push(`${a.cls}(${a.top}) -> ${b.cls}(${b.top})`)
    }
  }
  out.backwardJumps = backward
  out.backwardDetail = backwardDetail.slice(0, 6)
  await ctx.close()
  return out
}

// ---------------- 轨道层次（导航栏 vs 主区的明度关系）----------------
// 为什么单列：这不是 A1–A6 里的任何一条，但它是"配色一致性"最容易静默回归的地方。
// 2026-11-19 实测两次踩到：深色皮肤下侧栏要么亮得发光（L=0.726），要么与主区糊成
// 一片（0.003 vs 0.005）。两条根因不同（漏选择器 / 复用 bg-deep），靠的是同一套
// 明度关系判据。判据：深色下侧栏 0.02 ≤ L ≤ 0.08 且 |ΔL| ≥ 0.01；
// 浅色下侧栏必须明显深于主区（Δ ≥ 0.3）。
function M_RAIL() {
  const cv = document.createElement('canvas')
  cv.width = cv.height = 1
  const cx = cv.getContext('2d', { willReadFrequently: true })
  const rgb = (css) => {
    cx.clearRect(0, 0, 1, 1)
    cx.fillStyle = 'rgba(0,0,0,0)'
    cx.fillStyle = css
    cx.fillRect(0, 0, 1, 1)
    const d = cx.getImageData(0, 0, 1, 1).data
    return [d[0], d[1], d[2], d[3] / 255]
  }
  const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4) }
  const lum = (p) => 0.2126 * lin(p[0]) + 0.7152 * lin(p[1]) + 0.0722 * lin(p[2])

  const rail = document.querySelector('.ask-side') || document.querySelector('.ft-panel')
  const main = document.querySelector('main') || document.querySelector('.ask-main') || document.body
  if (!rail) return { missing: true }
  const rg = getComputedStyle(rail).backgroundColor
  const mg = getComputedStyle(main).backgroundColor
  const dark = getComputedStyle(document.documentElement).getPropertyValue('--skin-dark').trim() === '1'
  return { railBg: rg, mainBg: mg, railL: +lum(rgb(rg)).toFixed(3),
           mainL: +lum(rgb(mg)).toFixed(3), dark }
}

async function checkRailLayer(browser, base) {
  const cases = [
    ...['zinc', 'graphite', 'paper', 'seed'].map(s => ({ label: `ask/${s}`, route: '/', stable: '.composer textarea', skin: s })),
    { label: 'workbench/light', route: '/workbench', stable: '.ft-head-title', palette: 'light' },
    { label: 'workbench/observatory', route: '/workbench', stable: '.ft-head-title', palette: 'observatory' },
  ]
  const rows = []
  for (const c of cases) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    const page = await ctx.newPage()
    const init = []
    if (c.skin) init.push(`localStorage.setItem("docmind.skin","${c.skin}");localStorage.setItem("docmind.skinLocked","1")`)
    if (c.palette) init.push(`localStorage.setItem("docmind.workbenchPalette","${c.palette}")`)
    await page.addInitScript(init.join(';'))
    await page.goto(base + c.route, { waitUntil: 'domcontentloaded' })
    await page.waitForSelector(c.stable, { timeout: 25000 })
    await page.waitForTimeout(900)
    const m = await page.evaluate(M_RAIL)
    await ctx.close()
    if (m.missing) { rows.push({ ...c, verdict: 'no-rail' }); continue }
    const d = +(m.mainL - m.railL).toFixed(3)
    let verdict
    if (m.dark) {
      const okL = m.railL >= 0.02 && m.railL <= 0.08
      const okD = Math.abs(d) >= 0.01
      verdict = okL && okD ? 'ok' : (!okL ? (m.railL > 0.08 ? 'rail-too-bright' : 'rail-too-dark') : 'no-layer')
    } else {
      verdict = d >= 0.3 ? 'ok' : 'rail-not-darker'
    }
    rows.push({ label: c.label, railBg: m.railBg, mainBg: m.mainBg,
                railL: m.railL, mainL: m.mainL, delta: d, dark: m.dark, verdict })
  }
  return rows
}

// ---------------- 自证 ----------------
async function selftest(browser, base) {
  const { ctx, page } = await openCase(browser, base, CASES[0], 'zinc')
  const results = []

  const a1before = await page.evaluate(M_A1)
  await page.evaluate(() => {
    const d = document.createElement('div')
    d.id = '__a1inject'
    d.textContent = '注入的主角标题'
    d.style.cssText = 'font-size:calc(var(--fs-display) * 2);position:relative;z-index:1'
    document.body.prepend(d)
  })
  const a1after = await page.evaluate(M_A1)
  results.push({ rule: 'A1', ok: a1after.displayCount > a1before.displayCount,
                 detail: `display 计数 ${a1before.displayCount} → ${a1after.displayCount}` })
  await page.evaluate(() => document.getElementById('__a1inject')?.remove())

  const a2before = await page.evaluate(M_A2)
  await page.evaluate(() => {
    const d = document.createElement('div')
    d.id = '__a2inject'
    d.textContent = 'x'
    d.style.cssText = 'padding:13px;display:block;height:40px;width:200px'
    document.body.prepend(d)
  })
  const a2after = await page.evaluate(M_A2)
  results.push({ rule: 'A2', ok: a2after.offGrid > a2before.offGrid,
                 detail: `离栅格计数 ${a2before.offGrid} → ${a2after.offGrid}` })
  await page.evaluate(() => document.getElementById('__a2inject')?.remove())

  await page.evaluate(() => {
    const mk = (id, fg, bg, txt) => {
      const d = document.createElement('div')
      d.id = id
      d.textContent = txt
      d.style.cssText = 'font-size:18px;padding:12px 16px;width:260px;position:relative;z-index:1;'
        + 'color:' + fg + ';background:' + bg
      document.body.prepend(d)
    }
    mk('__a4bad', '#777777', '#ffffff', '低对比度样例文本')
    mk('__a4good', '#000000', '#ffffff', '高对比度样例文本')
  })
  const t4 = await page.evaluate(M_A4_COLLECT)
  const want = t4.filter(t => /低对比度|高对比度/.test(t.text))
  const b64 = await captureB64(page)
  const m4 = await page.evaluate(M_A4_MEASURE, { b64, targets: want })
  const bad = m4.find(x => /低对比度/.test(x.text))
  const good = m4.find(x => /高对比度/.test(x.text))
  results.push({ rule: 'A4', ok: !!bad && !!good && bad.ratio < 5 && good.ratio > 15,
                 detail: `低对比度实测 ${bad ? bad.ratio : 'N/A'}（期望≈4.48）；高对比度 ${good ? good.ratio : 'N/A'}（期望≈21）` })
  await page.evaluate(() => {
    document.getElementById('__a4bad')?.remove()
    document.getElementById('__a4good')?.remove()
  })

  const a6before = await page.evaluate(M_A6, false)
  const a6after = await page.evaluate(() => {
    const d = document.createElement('div')
    d.id = '__a6inject'
    d.style.cssText = 'width:200px;overflow:visible;white-space:nowrap;font-size:14px'
    d.textContent = 'A'.repeat(70)
    document.body.prepend(d)
    const hits = []
    for (const el of document.querySelectorAll('body *')) {
      const cs = getComputedStyle(el)
      if (['auto', 'scroll', 'hidden', 'clip'].includes(cs.overflowX)) continue
      if (el.clientWidth < 40) continue
      if (el.scrollWidth > el.clientWidth + 1) hits.push({ cls: (el.getAttribute('class') || '').slice(0, 30) })
    }
    d.remove()
    return hits
  })
  results.push({ rule: 'A6', ok: a6after.length > a6before.before.length,
                 detail: `溢出命中 ${a6before.before.length} → ${a6after.length}` })

  await page.evaluate(() => document.body.focus())
  await page.keyboard.press('Tab')
  const s1 = await page.evaluate(() => (document.activeElement?.getAttribute('class') || '') + '|' + document.activeElement?.tagName)
  await page.keyboard.press('Tab')
  const s2 = await page.evaluate(() => (document.activeElement?.getAttribute('class') || '') + '|' + document.activeElement?.tagName)
  results.push({ rule: 'A5', ok: !!s1 && !!s2 && s1 !== s2, detail: `Tab 停靠：${s1} → ${s2}` })

  await ctx.close()
  return results
}

// ---------------- 主流程 ----------------
const fixture = await startFixture()
const browser = await chromium.launch({ channel: 'msedge', headless: true })
fs.mkdirSync(OUT_DIR, { recursive: true })

try {
  if (process.argv.includes('--rail')) {
    const rows = await checkRailLayer(browser, fixture.base)
    console.log('\n=== 轨道层次（导航栏 vs 主区）===')
    let allOk = true
    for (const r of rows) {
      if (r.verdict === 'no-rail') { console.log(`  ?  ${r.label}  未找到轨道元素`); continue }
      if (r.verdict !== 'ok') allOk = false
      const mark = r.verdict === 'ok' ? '✓' : '✗'
      console.log(`  ${mark} ${r.label.padEnd(22)} rail L=${String(r.railL).padEnd(6)} main L=${String(r.mainL).padEnd(6)} Δ=${String(r.delta).padEnd(7)} ${r.dark ? '深色' : '浅色'}  ${r.verdict === 'ok' ? '' : r.verdict}`)
    }
    console.log(allOk ? '\n层次关系全部正常' : '\n存在层次关系错误')
    process.exitCode = allOk ? 0 : 1
  } else if (SELFTEST) {
    const res = await selftest(browser, fixture.base)
    console.log('\n=== 量具自证（注入已知缺陷，必须全部抓到）===')
    let allOk = true
    for (const r of res) {
      if (!r.ok) allOk = false
      console.log(`${r.ok ? '✓' : '✗'} ${r.rule}  ${r.detail}`)
    }
    console.log(allOk ? '\n量具可信：5 条自证全部命中' : '\n量具不可信：存在漏检，结论不能用')
    process.exitCode = allOk ? 0 : 1
  } else {
    const report = { generatedAt: new Date().toISOString(), skins: SKINS, cases: [], interaction: [], keyboard: [] }
    for (const skin of SKINS) {
      for (const c of CASES) {
        const r = await runCase(browser, fixture.base, c, skin)
        report.cases.push(r)
        console.log(`[采集] ${skin}/${c.name}  文本 ${r.A1.textEls}  display ${r.A1.displayCount}  对比样本 ${r.A4.length}`)
      }
    }
    for (const c of CASES) {
      report.interaction.push(await runInteraction(browser, fixture.base, c, 'zinc'))
      report.keyboard.push(await runKeyboard(browser, fixture.base, c, 'zinc'))
    }
    report.railLayer = await checkRailLayer(browser, fixture.base)
    const file = path.join(OUT_DIR, 'raw.json')
    fs.writeFileSync(file, JSON.stringify(report, null, 2), 'utf-8')
    console.log(`\n原始数据：${file}`)
  }
} finally {
  await browser.close()
  fixture.child.kill()
}
