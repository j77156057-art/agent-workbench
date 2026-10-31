// 健康面板「学习环漏斗」一行的真机回归（msedge headless）。
//
// 为什么要入库而不是留在 .tmp：这一行是"学习环断在哪一级"的唯一出口，改 chip、
// 改文案、改字号都会静默劣化它；没有可复跑的脚本，下次没人会再量一遍。
//
// 用法（要先有服务在跑，默认 127.0.0.1:8000）：
//   node frontend/tests/memoryFunnel.browser.mjs
//   DOCMIND_BASE=http://127.0.0.1:8123 node frontend/tests/memoryFunnel.browser.mjs
//
// 三遍：① 真接口（当前项目多半是空漏斗）；②③ route mock 灌「有数据+倒挂」与
// 「七级全通」两种载荷 —— 断点提示、颜色、数字、溢出都在真 DOM 上量。
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE || process.env.HOME,
    '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))

const BASE = process.env.DOCMIND_BASE || 'http://127.0.0.1:8000'
const STAGES = ['真实回合', '改了文件', '跑过测试', '拿到回执', '被采纳', '晋升共享', '共享层回注']

const results = []
function check(name, pass, detail = '') {
  results.push({ name, pass: !!pass, detail: String(detail).slice(0, 170) })
}

async function openHealthTab(page) {
  await page.goto(BASE + '/workbench.html', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('button[aria-label="上下文记忆"]', { timeout: 20000 })
  await page.click('button[aria-label="上下文记忆"]')
  await page.waitForSelector('button:has-text("健康")', { timeout: 10000 })
  await page.click('button:has-text("健康")')
  await page.waitForSelector('.mp-funnel', { timeout: 15000 })
}

async function readPanel(page, tag) {
  const info = await page.evaluate(() => {
    const box = document.querySelector('.mp-funnel')
    const block = box.closest('.mp-readonly')
    const note = block.querySelector('.mp-funnel-note')
    return {
      heading: block.querySelector('strong')?.innerText || '',
      steps: [...document.querySelectorAll('.mp-funnel-step')].map((el) => ({
        text: el.innerText.replace(/\s+/g, ' ').trim(),
        zero: el.classList.contains('mp-funnel-zero'),
        fontSize: getComputedStyle(el).fontSize,
      })),
      line: note ? note.innerText.trim() : '',
      lineColor: note ? getComputedStyle(note).color : '',
      noteFontSize: note ? getComputedStyle(note).fontSize : '0px',
      overflowX: box.getBoundingClientRect().right > window.innerWidth,
      box: (({ width, height }) => `${Math.round(width)}x${Math.round(height)}`)(
        box.getBoundingClientRect()),
    }
  })
  check(`${tag} 七级都在`, info.steps.length === 7, `${info.steps.length} 个 chip`)
  check(`${tag} 顺序与标签正确`,
    info.steps.map((s) => s.text.split(' ')[0]).join('|') === STAGES.join('|'),
    info.steps.map((s) => s.text).join(' → '))
  check(`${tag} 标题带窗口天数`, /近 \d+ 天/.test(info.heading), info.heading)
  check(`${tag} 不溢出视口`, !info.overflowX, info.box)
  // 漏斗是要读数字的：11px（--fs-cap）实测不达标，这里钉住 12px 下限
  check(`${tag} 字号≥12px`,
    info.steps.every((s) => parseFloat(s.fontSize) >= 12) && parseFloat(info.noteFontSize) >= 12,
    `chip ${info.steps[0]?.fontSize} / 提示 ${info.noteFontSize}`)
  return info
}

const KEYS = ['turns', 'with_changes', 'test_run', 'with_receipt', 'adopted', 'promoted', 'injected']

function healthPayload({ inverted }) {
  const values = [10, 4, 2, 1, 1, 1, inverted ? 0 : 1]
  const counts = Object.fromEntries(KEYS.map((key, i) => [key, values[i]]))
  return {
    ok: true, project_id: 'prj-x', generated_at: 'now',
    funnel: {
      days: 30, counts,
      stalled_at: inverted
        ? '断在「晋升共享 → 共享层回注」（注意：有 1 处倒挂，早期回合缺字段，别按断点解读）'
        : '',
      inversions: inverted ? [{ from: 'with_changes', to: 'with_receipt', message: 'x' }] : [],
      stages: KEYS.map((key, i) => ({ key, label: STAGES[i], count: values[i] })),
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

const browser = await chromium.launch({ channel: 'msedge', headless: true })
try {
  const ctx1 = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page1 = await ctx1.newPage()
  await openHealthTab(page1)
  const real = await readPanel(page1, '真接口')
  check('空漏斗报「还没有真实回合」', /还没有真实回合|七级全通/.test(real.line), real.line)
  await ctx1.close()

  for (const [tag, payload] of [['倒挂', healthPayload({ inverted: true })],
    ['全通', healthPayload({ inverted: false })]]) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    await ctx.route('**/api/memory/health', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(payload),
    }))
    const page = await ctx.newPage()
    await openHealthTab(page)
    const info = await readPanel(page, tag)
    const [lr, lg] = (info.lineColor.match(/\d+/g) || []).map(Number)
    if (tag === '倒挂') {
      check('倒挂态显示断点提示', /断在/.test(info.line), info.line)
      check('倒挂态带缺字段警告', /倒挂/.test(info.line), info.line)
      check('倒挂态提示用告警色（红>绿）', lr > lg, info.lineColor)
      check('倒挂态末级零值标色', info.steps[6].zero && !info.steps[0].zero, '')
    } else {
      check('全通态不再报断点', /七级全通/.test(info.line), info.line)
      check('全通态提示用通过色（绿>红）', lg > lr, info.lineColor)
      check('全通态无零值 chip', info.steps.every((s) => !s.zero), '')
    }
    // 截图默认落系统临时目录：写进仓库根会污染工作树（第一版就是这么漏的）
    await page.screenshot({
      path: path.join(process.env.DOCMIND_SHOT_DIR || os.tmpdir(), `memoryFunnel_${tag}.png`),
      clip: { x: 900, y: 0, width: 540, height: 900 },
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
