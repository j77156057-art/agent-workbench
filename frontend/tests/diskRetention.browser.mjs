// 健康面板「磁盘保留期」一节的真机回归（msedge headless）。
//
// 这一节管的是**不可逆删除**，界面错一格就可能让用户以为设了自动清（其实没保存）、
// 或者以为白名单挡住了（其实没挡）。所以脚本钉的是接线，不是样式：
//   · 输入框的 min/max 必须等于 mock 载荷里的 `ranges`（后端下发边界的单源纪律 ——
//     载荷故意给 400/800，组件若把 3650 写死在模板里就会红）；
//   · 白名单里内置项没有删除按钮，用户自己加的有；点×发出的 POST 是真的少了一条；
//   · 「立即回收」先弹确认框，取消不发请求，确认才带 apply+confirm 双 true；
//   · 体积数字带单位，对比度≥4.5，1020px 不溢出。
//
// 用法（要先有服务在跑）：
//   .venv/Scripts/python.exe -m uvicorn api:app --host 127.0.0.1 --port 8123
//   DOCMIND_BASE=http://127.0.0.1:8123 node frontend/tests/diskRetention.browser.mjs
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'

/* global process, console, document, getComputedStyle, window */

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE || process.env.HOME,
    '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))

const BASE = process.env.DOCMIND_BASE || 'http://127.0.0.1:8000'
const GB = 1024 ** 3
const MB = 1024 ** 2
const results = []
function check(name, pass, detail = '') {
  results.push({ name, pass: !!pass, detail: String(detail).slice(0, 220) })
}

/** 载荷故意用**非默认**边界（400 / 800）：组件写死 3650 就必须被这一遍抓到。 */
function diskState(overrides = {}) {
  return {
    ok: true,
    enabled: true,
    state_root: 'C:/state/.docmind',
    project_root: 'D:/project',
    settings: {
      interval_days: 0,
      idle_days: 30,
      whitelist: ['release', 'installer', '*.zip', 'golden'],
    },
    ranges: {
      interval_days_min: 0, interval_days_max: 400,
      idle_days_min: 1, idle_days_max: 800,
      whitelist_max_entries: 64, max_items_per_run: 2000,
    },
    builtin_whitelist: ['release', 'installer', '*.zip'],
    schedule: { interval_days: 0, due: false, last_apply_ts: null,
      first_seen_ts: null, next_due_ts: null },
    last_run: { ts: 1764000000, trigger: 'scheduled', applied: false,
      freed_bytes: 0, freed_items: 0, failed_items: 0 },
    plan: {
      reclaimable_bytes: Math.round(4.01 * GB), reclaimable_items: 44,
      blocked_by_whitelist_bytes: Math.round(1.5 * GB), blocked_by_whitelist_items: 6,
      needs_review_bytes: 768 * MB, needs_review_items: 3, max_items_per_run: 2000,
    },
    ...overrides,
  }
}

function runResult(apply) {
  const freed = apply ? 2 * GB : 0
  return {
    ok: true, applied: !!apply, dry_run: !apply,
    plan: diskState().plan,
    items: [{ kind: 'project_stages', name: 'wf-old', bytes: 1024, reason: '闲置 100 天' }],
    blocked: [{ kind: 'project_stages', name: 'golden', bytes: 2048,
      whitelisted_by: 'golden', reason: '白名单：golden' }],
    result: { removed: [{ path: 'C:/state/.docmind/project_stages/wf-old', bytes: freed }],
      failed: [], skipped: [], freed_bytes: freed, freed_items: apply ? 1 : 0 },
    record: { ts: 1764000001, trigger: 'manual', applied: !!apply,
      freed_bytes: freed, freed_items: apply ? 1 : 0, failed_items: 0 },
    settings: diskState().settings,
  }
}

function healthPayload() {
  const keys = ['turns', 'with_changes', 'test_run', 'with_receipt', 'adopted', 'promoted', 'injected']
  const labels = ['真实回合', '改了文件', '跑过测试', '拿到回执', '被采纳', '晋升共享', '共享层回注']
  const units = ['turn', 'turn', 'turn', 'turn', 'item', 'item', 'session']
  const unitLabels = ['回合', '回合', '回合', '回合', '条目', '条目', '会话']
  const values = [21, 0, 0, 3, 0, 0, 4]
  return {
    ok: true, project_id: 'prj-x',
    funnel: {
      days: 30, counts: Object.fromEntries(keys.map((k, i) => [k, values[i]])),
      stalled_at: '断在「真实回合 → 改了文件」（单位：回合）', caveats: [], handoffs: [],
      unmeasurable: [], missing_field: {}, skipped_breakpoints: [], item_window_by: 'updated',
      inject_events: 4,
      stages: keys.map((key, i) => ({ key, label: labels[i], count: values[i],
        unit: units[i], unit_label: unitLabels[i], scope: 'project', scope_label: '本项目' })),
    },
    shared: { active: 1, pinned: 1, superseded: 0 },
    project: { total: 46, candidate: 45 },
    duplicates: { project: 0, shared: 0 }, stale: { superseded: 0, retention_days: 90 },
    index: { items: 9, vectors: 9, edges: 0, fts: true },
    context: { total_chars: 100, layers: [] }, metrics: {}, warnings: [],
  }
}

async function openHealthTab(page) {
  await page.goto(BASE + '/workbench.html', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('button[aria-label="上下文记忆"]', { timeout: 20000 })
  await page.click('button[aria-label="上下文记忆"]')
  await page.waitForSelector('button:has-text("健康")', { timeout: 10000 })
  await page.click('button:has-text("健康")')
  await page.waitForSelector('.mp-disk', { timeout: 15000 })
}

async function readDisk(page) {
  return page.evaluate(() => {
    const block = document.querySelector('.mp-disk')
    // 同 memoryFunnel：oklch() 在 computed style 里原样返回，旧的数字正则会把
    // "oklch(0.74 0.018 230)" 误读为 rgb(0,74,0)。改用 canvas 解析真实 sRGB，
    // 半透明底按层合成。只修取色，不改阈值与令牌。
    const canvas = document.createElement('canvas')
    canvas.width = 1
    canvas.height = 1
    const ctx2d = canvas.getContext('2d', { willReadFrequently: true })
    const srgb = (color) => {
      ctx2d.clearRect(0, 0, 1, 1)
      ctx2d.fillStyle = '#000000'
      ctx2d.fillStyle = color
      ctx2d.fillRect(0, 0, 1, 1)
      const [r, g, b, a] = ctx2d.getImageData(0, 0, 1, 1).data
      return { r, g, b, a: a / 255 }
    }
    const flatten = (fg, bg) => ({
      r: Math.round(fg.r * fg.a + bg.r * (1 - fg.a)),
      g: Math.round(fg.g * fg.a + bg.g * (1 - fg.a)),
      b: Math.round(fg.b * fg.a + bg.b * (1 - fg.a)),
      a: 1,
    })
    const lum = (c) => {
      const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
      return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b)
    }
    const bgOf = (el) => {
      const layers = []
      let node = el
      while (node && node !== document.body) {
        const s = srgb(getComputedStyle(node).backgroundColor)
        if (s.a > 0) layers.push(s)
        if (s.a >= 1) break
        node = node.parentElement
      }
      if (!layers.length || layers[layers.length - 1].a < 1) {
        layers.push(srgb(getComputedStyle(document.body).backgroundColor))
      }
      let out = layers[layers.length - 1]
      for (let i = layers.length - 2; i >= 0; i--) out = flatten(layers[i], out)
      return out
    }
    const ratio = (fgColor, bg) => {
      const fg = srgb(fgColor)
      const solid = fg.a < 1 ? flatten(fg, bg) : fg
      const [x, y] = [lum(solid), lum(bg)].sort((m, n) => n - m)
      return +((x + 0.05) / (y + 0.05)).toFixed(2)
    }
    const text = (el) => (el ? el.innerText.replace(/\s+/g, ' ').trim() : '')
    const stats = [...block.querySelectorAll('.mp-disk-stats span')]
    const notes = [...block.querySelectorAll('.mp-disk-note')]
    const hints = [...block.querySelectorAll('.mp-disk-hint')]
    const chips = [...block.querySelectorAll('.mp-disk-chip')]
    const interval = block.querySelector('input[data-k="interval"]')
    const idle = block.querySelector('input[data-k="idle"]')
    const buttons = [...block.querySelectorAll('button')].map(text)
    const measured = [...stats, ...notes, ...hints, ...chips]
    return {
      heading: text(block.querySelector('strong')),
      actionHint: text(block.querySelector('.mp-actions .mp-hint')),
      stats: stats.map(text),
      note: text(notes[0]),
      hints: hints.map(text),
      chips: chips.map((el) => ({ text: text(el), builtin: el.classList.contains('mp-disk-chip-builtin'),
        removable: !!el.querySelector('.mp-disk-chip-x') })),
      interval: interval ? { min: interval.min, max: interval.max, value: interval.value } : null,
      idle: idle ? { min: idle.min, max: idle.max, value: idle.value } : null,
      orphans: !!block.querySelector('.mp-pin input'),
      buttons,
      contrast: measured.map((el) => ratio(getComputedStyle(el).color, bgOf(el))),
      fontSizes: measured.map((el) => parseFloat(getComputedStyle(el).fontSize)),
      overflowX: block.getBoundingClientRect().right > window.innerWidth,
      box: (({ width: w }) => `${Math.round(w)}px`)(block.getBoundingClientRect()),
    }
  })
}

const browser = await chromium.launch({ channel: 'msedge', headless: true })
try {
  // ---- ① 完整交互（mock 载荷）----
  {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    const posts = { settings: [], run: [] }
    let current = diskState()
    await ctx.route('**/api/memory/health', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(healthPayload()) }))
    await ctx.route('**/api/disk-retention/settings', async (route) => {
      const body = route.request().postDataJSON()
      posts.settings.push(body)
      current = { ...current, settings: { ...current.settings, ...body } }
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify(current) })
    })
    await ctx.route('**/api/disk-retention/run', async (route) => {
      const body = route.request().postDataJSON()
      posts.run.push(body)
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify(runResult(body.apply)) })
    })
    await ctx.route('**/api/disk-retention', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(current) }))
    const page = await ctx.newPage()
    await openHealthTab(page)
    const info = await readDisk(page)

    check('磁盘小节有中文标题', info.heading === '磁盘保留期', info.heading)
    check('三个量并列：可回收 / 白名单挡下 / 待人工',
      info.stats.length === 3 && /可回收 4\.0 GB \/ 44 项/.test(info.stats[0])
      && /白名单挡下 1\.5 GB \/ 6 项/.test(info.stats[1])
      && /待人工 768 MB \/ 3 项/.test(info.stats[2]),
      info.stats.join(' | '))
    check('计划来源写明时间与触发方（定时→中文）',
      /定时体检/.test(info.note) && !/未知来源/.test(info.note), info.note)
    check('间隔输入框的 min/max 来自后端载荷',
      info.interval && info.interval.min === '0' && info.interval.max === '400',
      JSON.stringify(info.interval))
    check('过期天数输入框的 min/max 来自后端载荷',
      info.idle && info.idle.min === '1' && info.idle.max === '800',
      JSON.stringify(info.idle))
    check('间隔为 0 时说明「不自动清」', info.hints.join(' ').includes('0 = 不自动清'),
      info.hints.join(' / '))
    check('自动清的时间口径说实话（体检每天一次，可能晚一天）',
      info.hints.join(' ').includes('晚一天'), info.hints.join(' / '))
    check('内置白名单没有删除按钮', info.chips.filter((c) => c.builtin).every((c) => !c.removable),
      JSON.stringify(info.chips.filter((c) => c.builtin)))
    check('用户加的白名单可以移除',
      info.chips.some((c) => c.text.startsWith('golden') && c.removable),
      JSON.stringify(info.chips))
    check('单次上限与白名单上限来自载荷而非写死',
      info.hints.join(' ').includes('上限 64 条') && info.actionHint.includes('2000 项'),
      `${info.hints.join(' / ')} || ${info.actionHint}`)

    // 移除 golden → POST 的白名单真的少一条，且内置项还在
    await page.click('.mp-disk-chip:not(.mp-disk-chip-builtin) .mp-disk-chip-x')
    await page.waitForTimeout(400)
    check('点×后发出的白名单去掉了该条且保留内置项',
      posts.settings.length === 1
      && JSON.stringify(posts.settings[0].whitelist) === JSON.stringify(['release', 'installer', '*.zip']),
      JSON.stringify(posts.settings))

    // 加入新条目
    await page.fill('input[data-k="whitelist"]', 'DocMind_p9_*')
    await page.click('button:has-text("加入")')
    await page.waitForTimeout(400)
    check('「加入」把新条目并进现有名单（不是替换）',
      posts.settings.length === 2
      && posts.settings[1].whitelist.includes('release')
      && posts.settings[1].whitelist.includes('DocMind_p9_*'),
      JSON.stringify(posts.settings[1]))

    // 保存两格天数
    await page.fill('input[data-k="interval"]', '7')
    await page.fill('input[data-k="idle"]', '21')
    check('改过未保存时给出「还没保存」提示',
      (await page.locator('.mp-disk .mp-actions .mp-hint').innerText()).includes('还没保存'), '')
    await page.click('button:has-text("保存设置")')
    await page.waitForTimeout(400)
    check('保存设置提交两格天数',
      JSON.stringify(posts.settings[2]) === JSON.stringify({ interval_days: 7, idle_days: 21 }),
      JSON.stringify(posts.settings[2]))

    // 重新扫描 = 干跑，不弹确认框
    await page.click('button:has-text("重新扫描")')
    await page.waitForTimeout(500)
    check('重新扫描走干跑（apply=false 且不弹确认）',
      posts.run.length === 1 && posts.run[0].apply === false
      && !(await page.locator('.dg-box').count()),
      JSON.stringify(posts.run))
    check('干跑结论是可回收量而不是已删除',
      (await page.locator('.mp-disk .mp-ok').innerText()).includes('可回收'), '')

    // 立即回收：取消 → 不发请求
    await page.click('button:has-text("立即回收")')
    await page.waitForSelector('.dg-box', { timeout: 5000 })
    const dialogText = await page.locator('.dg-box').innerText()
    check('真删除先弹确认框并写明不可撤销',
      /立即回收磁盘/.test(dialogText) && /不可撤销/.test(dialogText), dialogText.replace(/\s+/g, ' '))
    check('确认按钮是危险样式（danger 类）',
      !!(await page.locator('.dg-box .dg-danger').count()))
    await page.click('.dg-box button:has-text("取消")')
    await page.waitForTimeout(300)
    check('取消确认框后不发删除请求', posts.run.length === 1, JSON.stringify(posts.run))

    // 确认 → apply 与 confirm 同时为 true
    await page.click('button:has-text("立即回收")')
    await page.waitForSelector('.dg-box', { timeout: 5000 })
    await page.click('.dg-box button:has-text("删除并释放空间")')
    await page.waitForTimeout(600)
    check('确认后发出 apply+confirm 双 true',
      posts.run.length === 2 && posts.run[1].apply === true && posts.run[1].confirm === true,
      JSON.stringify(posts.run[1]))
    check('回收完成后回显释放量',
      (await page.locator('.mp-disk .mp-ok').innerText()).includes('已回收 2.0 GB'), '')
    check('勾选项「认不出状态的副本」默认不勾', info.orphans, String(info.orphans))

    const bad = info.contrast.filter((v) => v < 4.5)
    check('本节文字对底对比度≥4.5', info.contrast.length >= 6 && bad.length === 0,
      `${info.contrast.join('/')}（共 ${info.contrast.length} 项）`)
    const small = info.fontSizes.filter((v) => v < 12)
    check('本节字号≥12px', small.length === 0, info.fontSizes.join('/'))
    check('1440px 不溢出', !info.overflowX, info.box)
    await page.screenshot({
      path: path.join(process.env.DOCMIND_SHOT_DIR || os.tmpdir(), 'diskRetention_1440.png'),
      clip: { x: 900, y: 0, width: 540, height: 900 },
    })
    await ctx.close()
  }

  // ---- ② 1020px + 无计划记录 + 状态目录不存在 + 到点三态 ----
  for (const [tag, state] of [
    ['窄屏', diskState()],
    ['无记录', diskState({ last_run: {}, plan: {} })],
    ['无状态目录', diskState({ enabled: false })],
    ['已到期', diskState({
      settings: { interval_days: 30, idle_days: 30, whitelist: ['release', 'installer', '*.zip'] },
      schedule: { interval_days: 30, due: true, last_apply_ts: 1760000000,
        first_seen_ts: 1760000000, next_due_ts: 1762592000 },
    })],
    ['等基线', diskState({
      settings: { interval_days: 30, idle_days: 30, whitelist: ['release', 'installer', '*.zip'] },
      schedule: { interval_days: 30, due: false, last_apply_ts: null,
        first_seen_ts: null, next_due_ts: null },
    })],
  ]) {
    const width = tag === '窄屏' ? 1020 : 1440
    const ctx = await browser.newContext({ viewport: { width, height: 900 } })
    await ctx.route('**/api/memory/health', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(healthPayload()) }))
    await ctx.route('**/api/disk-retention', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(state) }))
    const page = await ctx.newPage()
    await openHealthTab(page)
    const info = await readDisk(page)
    check(`${tag} 不溢出视口（${width}px）`, !info.overflowX, info.box)
    if (tag === '无记录') {
      check('没有扫描记录时不谎报数字，改说去扫描',
        info.note.includes('还没有扫描记录') && info.stats[0].includes('0 B'), info.note)
      check('没有记录时不显示「未知来源」这种半句话', !/未知来源/.test(info.note), info.note)
    }
    if (tag === '无状态目录') {
      check('状态目录不存在时给出可读说明且收起控件',
        info.buttons.length === 0 && !info.interval, JSON.stringify(info.buttons))
    }
    if (tag === '已到期') {
      check('到点时明说「下次体检就会清」',
        info.hints.join(' ').includes('已到期'), info.hints.join(' / '))
      check('间隔默认档在输入框里显示 30 天', info.interval.value === '30',
        JSON.stringify(info.interval))
    }
    if (tag === '等基线') {
      check('首轮只记基线，不许写成「未到点」骗人已在计时',
        info.hints.join(' ').includes('首轮只记基线')
        && !info.hints.join(' ').includes('未到点'), info.hints.join(' / '))
    }
    await ctx.close()
  }

  // ---- ③ 真接口（服务需带本轮代码重启）----
  {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    const page = await ctx.newPage()
    let apiStatus = 0
    page.on('response', (res) => {
      if (res.url().endsWith('/api/disk-retention')) apiStatus = res.status()
    })
    await openHealthTab(page)
    const info = await readDisk(page)
    check('真接口返回 200', apiStatus === 200, `HTTP ${apiStatus}`)
    check('真接口下小节渲染出输入框与按钮',
      !!info.interval && info.buttons.includes('立即回收'), JSON.stringify(info.buttons))
    check('真接口的边界与后端常量一致（3650 天）',
      info.interval && info.interval.max === '3650' && info.idle.max === '3650',
      JSON.stringify({ interval: info.interval, idle: info.idle }))
    check('真接口的默认间隔是 30 天（按时间回收，不是关闭）',
      info.interval && info.interval.value === '30', JSON.stringify(info.interval))
    await ctx.close()
  }
} finally {
  await browser.close()
}

const failed = results.filter((r) => !r.pass)
for (const r of results) console.log(`${r.pass ? '✔' : '✗'} ${r.name}${r.detail ? ' — ' + r.detail : ''}`)
console.log(`\n${results.length - failed.length}/${results.length} 通过`)
process.exit(failed.length ? 1 : 0)
