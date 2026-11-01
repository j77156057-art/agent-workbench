// 健康面板「学习环漏斗」一行的真机回归（msedge headless）。
//
// 为什么要入库而不是留在 .tmp：这一行是"学习环断在哪一级"的唯一出口，改 chip、
// 改文案、改字号都会静默劣化它；没有可复跑的脚本，下次没人会再量一遍。
//
// 2026-11-01 起这行还多了一层责任：**每级的量纲**。七级里前四级数回合、五六数条目、
// 第七级数会话，且第七级的内容来自全局共享池 —— 旧版把「回执 3 → 采纳 0 → 回注 63」
// 读成一条链断了、又把跨单位的比较叫"倒挂"。所以脚本必须钉住：
//   · 单位角标在不在、对不对；
//   · 跨单位处显示的是「⇢ 单位 …」而不是同一排无差别 chip；
//   · 断点措辞与口径提示（caveats）分列两个 DOM 节点，不许混进同一句话。
//
// 用法（要先有服务在跑，默认 127.0.0.1:8000）：
//   node frontend/tests/memoryFunnel.browser.mjs
//   DOCMIND_BASE=http://127.0.0.1:8123 node frontend/tests/memoryFunnel.browser.mjs
//
// 四遍：① 真接口；② 缺字段倒挂；③ 七级全通；④ 真实项目形状（跨单位 + 存量/事件并列）。
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE || process.env.HOME,
    '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))

const BASE = process.env.DOCMIND_BASE || 'http://127.0.0.1:8000'
const STAGES = ['真实回合', '改了文件', '跑过测试', '拿到回执', '被采纳', '晋升共享', '共享层回注']
const KEYS = ['turns', 'with_changes', 'test_run', 'with_receipt', 'adopted', 'promoted', 'injected']
const UNITS = ['回合', '回合', '回合', '回合', '条目', '条目', '会话']
// 只有两处跨单位交接：回执(回合)→采纳(条目)、晋升(条目)→回注(会话)
const HANDOFFS = ['单位 回合→条目', '单位 条目→会话']

const results = []
function check(name, pass, detail = '') {
  results.push({ name, pass: !!pass, detail: String(detail).slice(0, 200) })
}

async function openHealthTab(page) {
  await page.goto(BASE + '/workbench.html', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('button[aria-label="上下文记忆"]', { timeout: 20000 })
  await page.click('button[aria-label="上下文记忆"]')
  await page.waitForSelector('button:has-text("健康")', { timeout: 10000 })
  await page.click('button:has-text("健康")')
  await page.waitForSelector('.mp-funnel', { timeout: 15000 })
}

function stage(key, index, count, extra = {}) {
  return {
    key, label: STAGES[index], count,
    unit: ['turn', 'turn', 'turn', 'turn', 'item', 'item', 'session'][index],
    unit_label: UNITS[index],
    scope: index === 6 ? 'project|global' : 'project',
    scope_label: index === 6 ? '本项目回合 × 全局共享池' : '本项目',
    measurable: index <= 3 ? (extra.measurable ?? count) : null,
    stock: index >= 4 && index <= 5 ? (extra.stock ?? count) : null,
    events: index === 6 ? (extra.events ?? count) : null,
  }
}

function healthPayload(variant) {
  const presets = {
    // ② 缺字段：test_run 整级没有判据字段，不许被读成断点
    inverted: {
      values: [10, 4, 0, 1, 1, 1, 0],
      unmeasurable: ['test_run'],
      missing: { test_run: 10 },
      stalled: '断在「真实回合 → 改了文件」（单位：回合）',
      caveats: ['后级多于前级：2 处判据字段缺失、这一级判不准；1 处是跨单位比较（量纲不同，不是倒挂）'],
    },
    // ③ 全通
    passed: {
      values: [10, 4, 2, 1, 1, 1, 1],
      unmeasurable: [],
      missing: {},
      stalled: '',
      caveats: [],
    },
    // ④ 真实项目形状：回合 21 / 改文件 0 / 测试 0 / 回执 3 / 采纳 0 / 晋升 0 / 回注 4 个会话（63 次事件）
    live: {
      values: [21, 0, 0, 3, 0, 0, 4],
      unmeasurable: [],
      missing: {},
      stalled: '断在「真实回合 → 改了文件」（单位：回合）',
      caveats: ['后级多于前级：1 处属正常形状（后级多于前级，例如跑验证不必改文件）；'
        + '1 处是跨单位比较（量纲不同，不是倒挂）'],
      extras: [{}, {}, {}, {}, { stock: 57 }, { stock: 1 }, { events: 63 }],
    },
  }
  const p = presets[variant]
  const values = p.values
  const counts = Object.fromEntries(KEYS.map((key, i) => [key, values[i]]))
  const handoffs = [
    { from: 'with_receipt', to: 'adopted', from_label: '拿到回执', to_label: '被采纳',
      comparable: false, reason: '单位 回合→条目' },
    { from: 'promoted', to: 'injected', from_label: '晋升共享', to_label: '共享层回注',
      comparable: false, reason: '单位 条目→会话，且作用域 本项目→本项目回合 × 全局共享池' },
  ]
  return {
    ok: true, project_id: 'prj-x', generated_at: 'now',
    funnel: {
      days: 30, counts,
      stalled_at: p.stalled,
      caveats: p.caveats,
      handoffs,
      unmeasurable: p.unmeasurable,
      missing_field: p.missing,
      skipped_breakpoints: p.unmeasurable.length
        ? [{ at: '改了文件 → 跑过测试', reason: '这一级没有一条记录带判据字段，判不了' }] : [],
      item_window_by: 'updated（采纳/晋升本身无时间戳，用更新时间作代理；0 条读不出时间仍计入）',
      inject_events: (p.extras && p.extras[6] && p.extras[6].events) || values[6],
      stages: KEYS.map((key, i) => stage(key, i, values[i], (p.extras || [])[i] || {})),
    },
    shared: { active: 1, pinned: 1, superseded: 0 },
    project: { total: 46, candidate: 45, adopted: 1, promoted: 1 },
    duplicates: { project: 0, shared: 0 }, stale: { superseded: 0, retention_days: 90 },
    index: { items: 99, vectors: 99, edges: 12, fts: true },
    context: { total_chars: 1200, layers: [] },
    metrics: { consolidate_calls: 3, errors: 0 },
    warnings: [{ code: 'no_promotion', message: '还没有候选通过晋升门' }],
  }
}

async function readPanel(page, tag, width, strict = true) {
  const info = await page.evaluate(() => {
    const box = document.querySelector('.mp-funnel')
    const block = box.closest('.mp-readonly')
    const lum = (rgb) => {
      const [r, g, b] = (rgb.match(/\d+/g) || [0, 0, 0]).map(Number)
      const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    }
    const ratio = (a, b) => {
      const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m)
      return (x + 0.05) / (y + 0.05)
    }
    const chips = [...document.querySelectorAll('.mp-funnel-step')]
    const units = [...document.querySelectorAll('.mp-funnel-unit')]
    const marks = [...document.querySelectorAll('.mp-funnel-handoff')]
    const stall = block.querySelector('.mp-funnel-stall')
    const caveats = [...block.querySelectorAll('.mp-funnel-caveat')]
    const metaLine = block.querySelector('.mp-funnel-meta-line')
    const bgOf = (el) => {
      let node = el
      while (node && node !== document.body) {
        const c = getComputedStyle(node).backgroundColor
        if (c && !c.includes('rgba(0, 0, 0, 0)')) return c
        node = node.parentElement
      }
      return getComputedStyle(document.body).backgroundColor
    }
    return {
      heading: block.querySelector('strong')?.innerText || '',
      steps: chips.map((el) => ({
        text: el.innerText.replace(/\s+/g, ' ').trim(),
        label: el.firstChild ? el.firstChild.textContent.trim() : '',
        zero: el.classList.contains('mp-funnel-zero'),
        unmeasured: el.classList.contains('mp-funnel-unmeasured'),
        fontSize: getComputedStyle(el).fontSize,
      })),
      unitTexts: units.map((el) => el.innerText.trim()),
      chipContrast: chips.map((el) => +ratio(getComputedStyle(el).color, bgOf(el)).toFixed(2)),
      markContrast: marks.length ? +ratio(getComputedStyle(marks[0]).color, bgOf(marks[0])).toFixed(2) : null,
      unitContrast: units.map((el) => +ratio(getComputedStyle(el).color,
        bgOf(el.closest('.mp-funnel-step') || el)).toFixed(2)),
      markTexts: marks.map((el) => el.innerText.replace(/\s+/g, ' ').trim()),
      markColor: marks.length ? getComputedStyle(marks[0]).color : '',
      stall: stall ? stall.innerText.trim() : '',
      stallColor: stall ? getComputedStyle(stall).color : '',
      caveats: caveats.map((el) => el.innerText.trim()),
      metaLine: metaLine ? metaLine.innerText.trim() : '',
      noteFontSize: (stall || block.querySelector('.mp-funnel-note'))
        ? getComputedStyle(stall || block.querySelector('.mp-funnel-note')).fontSize : '0px',
      overflowX: box.getBoundingClientRect().right > window.innerWidth,
      box: (({ width: w, height: h }) => `${Math.round(w)}x${Math.round(h)}`)(box.getBoundingClientRect()),
    }
  })
  check(`${tag} 七级 chip 都在`, info.steps.length === 7, `${info.steps.length} 个`)
  check(`${tag} 顺序与标签正确`,
    info.steps.map((s) => s.label).join('|') === STAGES.join('|'),
    info.steps.map((s) => s.text).join(' → '))
  if (strict) {
    // 单位角标与跨单位分隔是 2026-11-01 口径改造的一部分，只在 mock 载荷上硬要求：
    // 真接口跑的是当前**正在服务的那份代码**，服务没重启时它是旧版 —— 把版本耦合
    // 写成断言，只会让人习惯看红，那就等于没有守卫。
    check(`${tag} 每级都带单位角标`, info.unitTexts.join('|') === UNITS.join('|'),
      info.unitTexts.join('|'))
    check(`${tag} 跨单位处有分隔标记`, info.markTexts.length === HANDOFFS.length,
      info.markTexts.join(' | '))
    check(`${tag} 分隔标记说明单位不同`,
      HANDOFFS.every((text) => info.markTexts.some((m) => m.includes(text))), info.markTexts.join(' | '))
  } else if (info.unitTexts.length !== 7) {
    console.log(`ℹ ${tag}：服务侧尚未带单位口径（${info.unitTexts.length}/7），`
      + `结论行="${info.stall}" —— 重启服务后应转成 strict 断言`)
  }
  // 对比度：chip 文字两版都有，所以永远量；角标/分隔标记只在 strict 下要求存在
  const contrast = [...info.chipContrast,
    ...(strict ? [...info.unitContrast, ...(info.markContrast ? [info.markContrast] : [])] : [])]
  check(`${tag} 文字对底对比度≥4.5`,
    contrast.length > 0 && contrast.every((v) => v >= 4.5),
    `${contrast.slice(0, 5).join('/')}（共 ${contrast.length} 项）`)
  check(`${tag} 标题带窗口天数`, /近 \d+ 天/.test(info.heading), info.heading)
  check(`${tag} 不溢出视口（${width}px）`, !info.overflowX, info.box)
  check(`${tag} 字号≥12px`,
    info.steps.every((s) => parseFloat(s.fontSize) >= 12) && parseFloat(info.noteFontSize) >= 12,
    `chip ${info.steps[0]?.fontSize} / 提示 ${info.noteFontSize}`)
  return info
}

const browser = await chromium.launch({ channel: 'msedge', headless: true })
try {
  const ctx1 = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page1 = await ctx1.newPage()
  await openHealthTab(page1)
  const real = await readPanel(page1, '真接口', 1440, false)
  check('真接口给出一句可读结论',
    /断在|交接处停在|还没有真实回合|各级都通了/.test(real.stall), real.stall)
  await ctx1.close()

  for (const [tag, variant, width] of [['缺字段', 'inverted', 1440],
    ['全通', 'passed', 1440], ['真实形状', 'live', 1020]]) {
    const ctx = await browser.newContext({ viewport: { width, height: 900 } })
    await ctx.route('**/api/memory/health', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(healthPayload(variant)),
    }))
    const page = await ctx.newPage()
    await openHealthTab(page)
    const info = await readPanel(page, tag, width)
    const [sr, sg] = (info.stallColor.match(/\d+/g) || [0, 0, 0]).map(Number)
    if (tag === '缺字段') {
      check('缺字段级标成「判不了」而非零值断点',
        info.steps.some((s) => s.unmeasured && s.label === '跑过测试'), '')
      check('缺字段态显示真断点（单位：回合）',
        info.stall.includes('断在「真实回合 → 改了文件」'), info.stall)
      check('口径提示与断点分列两个节点',
        info.caveats.length > 0 && !info.stall.includes('倒挂'),
        `caveats=${info.caveats.length} stall="${info.stall}"`)
      check('提示里说的是量具问题不是流量断点',
        info.caveats.join(' ').includes('判据字段缺失'), info.caveats.join(' '))
      check('断点用告警色（红>绿）', sr > sg, info.stallColor)
      check('窗口代理口径有脚注', info.metaLine.includes('updated'), info.metaLine)
    } else if (tag === '全通') {
      check('全通态不再报断点', info.stall === '', info.stall)
      check('全通态无零值 chip', info.steps.every((s) => !s.zero), '')
      check('全通态无口径提示', info.caveats.length === 0, info.caveats.join(' '))
    } else {
      check('真实形状：回执>改文件 被判成正常形状',
        info.caveats.join(' ').includes('正常形状'), info.caveats.join(' '))
      check('真实形状：条目级并列存量',
        info.steps[5].text.includes('存量 1') && info.steps[4].text.includes('存量 57'),
        info.steps[4].text + ' / ' + info.steps[5].text)
      check('真实形状：回注并列事件数',
        info.steps[6].text.includes('事件 63'), info.steps[6].text)
      check('真实形状：断点仍在回合段内',
        info.stall.includes('断在「真实回合 → 改了文件」'), info.stall)
    }
    await page.screenshot({
      path: path.join(process.env.DOCMIND_SHOT_DIR || os.tmpdir(), `memoryFunnel_${tag}.png`),
      clip: { x: Math.max(0, width - 540), y: 0, width: 540, height: 900 },
    })
    await ctx.close()
  }
} finally {
  await browser.close()
}

const failed = results.filter((r) => !r.pass)
for (const r of results) console.log(`${r.pass ? '✔' : '✗'} ${r.name}${r.detail ? ' — ' + r.detail : ''}`)
console.log(`\n${results.length - failed.length}/${results.length} 通过`)
process.exit(failed.length ? 1 : 0)
