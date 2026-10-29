// 右侧预览侧栏（第三栏）生产页面验收。
//
// 与 product-ui.browser.mjs 同一套 harness（Edge headless + Playwright），但把
// 会变化的外部输入全部钉死：
//   · /api/chat          → 分帧 SSE：thought → action → observation(file_diffs) → final → done
//   · /api/fs/file       → 固定 40 行夹具（第 12 行是目标行）
//   · /api/dev/browser/* → 固定实况页与 1×1 JPEG 帧
// 其余 GET 放行到本地后端（项目树 / 项目列表需要真实数据才能通过 nodeExists 过滤引用）；
// 非 GET 一律拦截并记账，防止验收过程写后端。
//
// 退出码口径：planned 与 ran 必须严格相等，否则记失败并以非零码退出（防漏跑假绿）。
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs/promises'
import { FRAME_B64 } from './fixtures/right-rail-frame.mjs'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE, '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))
const root = fileURLToPath(new URL('../../', import.meta.url))
const output = path.join(root, 'artifacts/shots')
const base = process.env.DOCMIND_UI_TEST_URL || 'http://127.0.0.1:8000'
const FILE_PATH = 'frontend/src/workbench/App.vue'
const FILE_LINE = 12
const TARGET_TEXT = 'export const RIGHT_RAIL_TARGET = 12'
const FILE_CONTENT = Array.from({ length: 40 }, (_, i) =>
  i + 1 === FILE_LINE ? `${TARGET_TEXT} // 目标行高亮锚点` : `// 右栏验收夹具第 ${i + 1} 行`).join('\n')
const FINAL_TEXT = [
  `改动落在 ${FILE_PATH}:${FILE_LINE}，右栏可以只读查看。`,
  '',
  '· 本地预览样例',
  `${base}/play/right-rail-fixture`,
  '',
  '· 外部站点样例',
  'https://example.com/',
].join('\n')
const planned = [
  'ref-opens-file',
  'diff-in-rail',
  'resize-clamp-reset-key',
  'close-zero-trace-and-menu',
  'web-source-iframe',
  'mirror-frame-steps',
  'narrow-and-dock-fallback',
  'screenshots',
]
const ran = []
const report = { planned, ran, cases: [], interactions: [], errors: [], writes: [] }

await fs.mkdir(output, { recursive: true })
const browser = await chromium.launch({ channel: 'msedge', headless: true })

const sseFrame = (obj) => 'data: ' + JSON.stringify(obj) + '\n\n'
const SSE_BODY = [
  { type: 'thought', text: '先读入口文件，再用一次写文件工具演示改动。' },
  { type: 'action', text: `read_file({'path':'${FILE_PATH}'})` },
  { type: 'observation', text: '已读取 40 行。' },
  { type: 'action', text: `apply_edit({'path':'${FILE_PATH}'})` },
  {
    type: 'observation',
    text: '已写入文件（+2 −1）',
    file_diffs: [{
      path: FILE_PATH,
      kind: 'modify',
      old: '// 旧内容\n旧的说明行',
      new: `// 旧内容\n新的说明行\n${TARGET_TEXT}`,
      diff: `@@ -1,2 +1,3 @@\n // 旧内容\n-旧的说明行\n+新的说明行\n+${TARGET_TEXT}`,
    }],
  },
  { type: 'final', text: FINAL_TEXT, status: 'done' },
  { type: 'done' },
].map(sseFrame).join('')

async function createPage({ skin = 'zinc', layout = null, width = 1440, height = 900 } = {}) {
  const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce' })
  page.on('pageerror', (error) => report.errors.push(error.message))
  page.on('dialog', (dialog) => dialog.accept())
  // 清库只在首屏做一次：刷新后保留右栏宽度，才能验持久化。
  await page.addInitScript(({ skin, layout }) => {
    try {
      if (sessionStorage.getItem('__rr_boot')) return
      sessionStorage.setItem('__rr_boot', '1')
      localStorage.clear()
      localStorage.setItem('docmind.skin', skin)
      localStorage.setItem('docmind.skinLocked', '1')
      if (layout) localStorage.setItem('docmind.layout', layout)
    } catch { /* 隐私模式：按默认继续 */ }
  }, { skin, layout })

  let chatCalls = 0
  await page.route('**/api/**', async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const pathname = url.pathname
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (pathname === '/api/chat') {
      chatCalls++
      return route.fulfill({ contentType: 'text/event-stream', body: SSE_BODY })
    }
    if (pathname === '/api/turn/active') return json({ ok: true, turns: [] })
    if (pathname === '/api/sessions') return json({ ok: true, items: [] })
    if (pathname.startsWith('/api/sessions/')) return json({ ok: true, session_id: pathname.split('/').at(-1), turns: [] })
    if (pathname === '/api/fs/file') {
      return json({
        ok: true,
        path: url.searchParams.get('path') || FILE_PATH,
        content: FILE_CONTENT,
        lang: 'vue',
        size: FILE_CONTENT.length,
        mtime: 1761696000,
        region: null,
        region_name: null,
        writable: true,
        preview_kind: 'text',
        preview_note: null,
        preview_url: null,
        tracked: true,
        dirty: false,
      })
    }
    if (pathname === '/api/dev/browser/pages') {
      return json({
        ok: true,
        pages: [{
          page: 'rr-live-1', url: 'https://example.com/', domain: 'example.com',
          actions: 2, last_action: 'click', last_step: 2, live_state: 'live', live_version: 7,
        }],
      })
    }
    if (pathname === '/api/dev/browser/live') {
      return json({
        ok: true, state: 'live', page: 'rr-live-1', domain: 'example.com',
        frame: FRAME_B64, frame_version: 7, url: 'https://example.com/', title: 'Example Domain',
        last_action: 'click', last_step: 2,
        steps: [
          { action: 'open', ok: true, effect: 'navigated', url: 'https://example.com/', at: 1 },
          { action: 'click', ok: true, effect: 'changed', url: 'https://example.com/', at: 2 },
        ],
      })
    }
    if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) {
      report.writes.push(pathname)
      return json({ ok: false, error: 'UI test blocks backend writes' }, 409)
    }
    return route.continue()
  })
  // iframe 夹具：本地样例与外部站点都给一份没有 XFO 的简单页面
  const frameHtml = (title, color) => `<!doctype html><meta charset="utf-8"><title>${title}</title>`
    + `<body style="margin:0;font:600 18px/1.5 system-ui;padding:24px;background:${color};color:#123">${title}</body>`
  await page.route('**/play/right-rail-fixture*', (route) =>
    route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: frameHtml('右栏本地预览夹具', '#eaf2ff') }))
  await page.route('https://example.com/**', (route) =>
    route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: frameHtml('Example Domain（路由桩）', '#fff7e6') }))
  return { page, calls: () => chatCalls }
}

async function bootWorkbench(page) {
  await page.goto(base + '/workbench')
  await page.locator('.ov-page-head').waitFor({ timeout: 20000 })
  await page.locator('.cd-input').waitFor()
  await page.evaluate(() => document.fonts.ready)
}

async function sendDemo(page) {
  const input = page.locator('.cd-input')
  await input.fill('看看这个文件的引用和改动')
  await page.locator('.cd-send').click()
  await page.locator('.cd-trace-head').waitFor({ timeout: 20000 })
  await page.locator('.cd-ref').first().waitFor({ timeout: 20000 })
}

async function railWidth(page) {
  return page.locator('.wb-rightrail').evaluate((el) => el.getBoundingClientRect().width)
}

/** 展开推理轨迹 → 展开分段 → 打开写文件观察 → 点「查看改动」。 */
async function openDiffFromTrace(page) {
  const head = page.locator('.cd-trace-head')
  if ((await head.getAttribute('aria-expanded')) !== 'true') await head.click()
  const seg = page.locator('.cd-flow-toggle').first()
  await seg.waitFor()
  if ((await seg.getAttribute('aria-expanded')) !== 'true') await seg.click()
  await page.locator('.cd-flow-body').waitFor()
  const row = page.locator('.cd-flow-row').filter({ hasText: '已写入文件' }).first()
  await row.waitFor()
  if ((await row.getAttribute('aria-expanded')) !== 'true') await row.click()
  const btn = page.locator('.cd-flow-diffbtn')
  await btn.waitFor()
  await btn.click()
}

async function openMoreMenu(page) {
  await page.locator('button[aria-label="更多"]').click()
  await page.locator('.wb-more-menu').waitFor({ state: 'visible' })
}

async function clickPreviewRailMenuItem(page) {
  const item = page.locator('.wb-more-menu .wb-menu-item').filter({ hasText: /预览侧栏/ }).first()
  await item.waitFor({ state: 'attached' })
  await page.waitForFunction(() => {
    const hit = [...document.querySelectorAll('.wb-more-menu .wb-menu-item')].find((el) => /预览侧栏/.test(el.textContent || ''))
    return !!hit && !(hit instanceof HTMLButtonElement && hit.disabled)
  })
  await item.click()
  await page.locator('.wb-rightrail').waitFor({ timeout: 5000 })
}

try {
  // ── 主用例：1440×900、fixed 布局 ─────────────────────────────────────────
  const { page, calls } = await createPage()
  await bootWorkbench(page)
  await sendDemo(page)
  assert.equal(calls(), 1, 'chat stub should be called exactly once')

  // 1) 文中引用 → 右栏文件页签 + 目标行高亮
  await page.locator('.cd-ref').first().click()
  await page.locator('.wb-rightrail').waitFor()
  assert.equal((await page.locator('.rr-tab.on').textContent())?.trim(), '文件')
  await page.locator('.rr-file-path').waitFor()
  assert.match(await page.locator('.rr-file-path').textContent(), new RegExp(FILE_PATH.replace(/\./g, '\\.')))
  await page.locator('.rr-ln.hl').waitFor()
  assert.equal((await page.locator('.rr-ln.hl .rr-num').textContent())?.trim(), String(FILE_LINE))
  assert.match(await page.locator('.rr-ln.hl .rr-src').textContent(), /RIGHT_RAIL_TARGET/)
  await page.screenshot({ path: path.join(output, 'right-rail-file.png') })
  // 「在编辑器中打开」→ 编辑器标签出现
  await page.locator('.rr-file-open').click()
  const tab = page.locator('.et-tab').filter({ hasText: 'App.vue' }).first()
  await tab.waitFor({ timeout: 10000 })
  assert.match(await page.locator('.et-name').first().textContent(), /App\.vue/)
  report.interactions.push('引用 → 右栏文件页签（第 12 行高亮）→ 在编辑器中打开生成标签')
  ran.push('ref-opens-file')

  // 2) 推理轨迹里的「查看改动」→ 右栏改动页签，弹窗不再出现
  await openDiffFromTrace(page)
  await page.locator('.wb-rightrail').waitFor()
  assert.equal((await page.locator('.rr-tab.on').textContent())?.trim(), '改动')
  assert.equal(await page.locator('.fd-overlay').count(), 0, '改动弹窗不应再弹出')
  await page.locator('.rr-diff .fd-file').first().waitFor()
  assert.equal(await page.locator('.rr-diff .fd-file').count(), 1)
  assert((await page.locator('.rr-diff .fd-row.fd-add').count()) > 0, 'diff 应有新增行')
  assert((await page.locator('.rr-diff .fd-row.fd-del').count()) > 0, 'diff 应有删除行')
  await page.screenshot({ path: path.join(output, 'right-rail-diff.png') })
  report.interactions.push('查看改动 → 右栏改动页签渲染文件头与增删行，弹窗未弹出')
  ran.push('diff-in-rail')

  // 3) 联网来源 → 网页页签 iframe；外部站点提示可见
  const details = page.locator('.cd-web-sources')
  await details.waitFor()
  if (!(await details.evaluate((el) => el.open))) await details.locator('summary').click()
  const railButtons = page.locator('.cd-web-source-rail')
  assert.equal(await railButtons.count(), 2, '应有两处联网来源（本地 + 外部）')
  await railButtons.nth(0).click()
  await page.locator('.rr-frame').waitFor()
  assert.equal(await page.locator('.rr-frame').getAttribute('src'), `${base}/play/right-rail-fixture`)
  assert.equal(await page.locator('.rr-tip').filter({ hasText: '外部站点' }).count(), 0)
  await page.screenshot({ path: path.join(output, 'right-rail-browser.png') })
  await railButtons.nth(1).click()
  await page.waitForFunction((expected) => document.querySelector('.rr-frame')?.getAttribute('src') === expected, 'https://example.com/')
  await page.locator('.rr-tip').filter({ hasText: '外部站点' }).waitFor()
  report.interactions.push('联网来源「侧栏」→ 网页 iframe；外部站点显示嵌入限制提示')
  ran.push('web-source-iframe')

  // 4) 只读镜像：实况帧 + 最近动作
  await page.locator('.rr-mode button').filter({ hasText: '只读镜像' }).click()
  const mirror = page.locator('[data-testid="right-rail-mirror-frame"]')
  await mirror.waitFor({ timeout: 10000 })
  assert.match(await mirror.getAttribute('src'), /^data:image\/jpeg;base64,/)
  await page.locator('[data-testid="right-rail-mirror-steps"] li').first().waitFor()
  assert((await page.locator('[data-testid="right-rail-mirror-steps"] li').count()) >= 1)
  assert.match(await page.locator('.rr-mirror-act').textContent(), /点击/)
  await page.screenshot({ path: path.join(output, 'right-rail-mirror.png') })
  report.interactions.push('只读镜像：data:image/jpeg 实况帧与最近动作可见')
  ran.push('mirror-frame-steps')

  // 5) 拖动夹取 / 双击复位 / 键盘微调 / 刷新后宽度保留
  const resizer = page.locator('.rr-resizer')
  const box = await resizer.boundingBox()
  assert(box, '缺少右栏拖拽条')
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width / 2 - 400, box.y + box.height / 2, { steps: 10 })
  await page.mouse.up()
  const expectedCeil = await page.evaluate(() => {
    const left = document.querySelector('.ft-panel')?.getBoundingClientRect().width || 0
    return Math.round(innerWidth - left - 3 - 3 - 520)
  })
  const wide = await railWidth(page)
  assert(Math.abs(wide - expectedCeil) <= 2, `拖动应被主区 520px 下限夹住：${wide} vs ${expectedCeil}`)
  const mainBox = await page.locator('.wb-main').boundingBox()
  assert(mainBox && mainBox.width >= 519, `主区宽度应 ≥520：${mainBox?.width}`)
  await page.screenshot({ path: path.join(output, 'right-rail-resized.png') })

  const box2 = await resizer.boundingBox()
  await page.mouse.move(box2.x + box2.width / 2, box2.y + box2.height / 2)
  await page.mouse.down()
  await page.mouse.move(box2.x + box2.width / 2 + 500, box2.y + box2.height / 2, { steps: 10 })
  await page.mouse.up()
  assert(Math.abs((await railWidth(page)) - 340) <= 2, '向右拖动应被 340px 最小宽度夹住')
  await resizer.dblclick()
  assert(Math.abs((await railWidth(page)) - 460) <= 2, '双击应复位到 460px')
  await resizer.focus()
  await page.keyboard.press('ArrowLeft')
  assert(Math.abs((await railWidth(page)) - 476) <= 2, '← 应加宽 16px')
  await page.keyboard.press('ArrowRight')
  assert(Math.abs((await railWidth(page)) - 460) <= 2, '→ 应收窄 16px')
  await page.keyboard.press('ArrowLeft')
  assert.equal(await page.evaluate(() => localStorage.getItem('docmind.rightRailWidth')), '476')
  report.interactions.push('拖动夹取（626/340）→ 双击 460 → 键盘 ±16 → 宽度持久化 476')
  ran.push('resize-clamp-reset-key')

  // 6) 关闭零痕迹 + 「更多 → 预览侧栏」重开
  await page.locator('.rr-close').click()
  await page.locator('.wb-rightrail').waitFor({ state: 'detached' })
  assert.equal(await page.locator('.wb-rightrail').count(), 0)
  assert.equal(await page.locator('.rr-resizer').count(), 0)
  assert.equal(await page.locator('.wb-body').evaluate((el) => el.querySelectorAll('.wb-rightrail, .rr-resizer').length), 0)
  await openMoreMenu(page)
  await clickPreviewRailMenuItem(page)
  assert(Math.abs((await railWidth(page)) - 476) <= 2, '重开应沿用已保存宽度')
  // 刷新后仍保留（会话打开态不持久化，需再次从菜单打开）。
  // 注意不能等 .ov-page-head：刷新会恢复上次的工作区视图，可能是代码页而非概览页。
  await page.reload()
  await page.locator('.wb-main').waitFor()
  await openMoreMenu(page)
  await clickPreviewRailMenuItem(page)
  assert(Math.abs((await railWidth(page)) - 476) <= 2, '刷新后宽度应保留')
  report.interactions.push('关闭后 DOM 无任何占位，更多菜单可重开，刷新后宽度仍为 476')
  ran.push('close-zero-trace-and-menu')
  await page.close()

  // 7) 窄屏（1000）与 dock 布局：入口回落到编辑器，右栏不出现
  for (const [label, opts] of [['narrow', { width: 1000 }], ['dock', { width: 1440, layout: 'dock' }]]) {
    const { page: fallback } = await createPage(opts)
    await bootWorkbench(fallback)
    await sendDemo(fallback)
    await fallback.locator('.cd-ref').first().click()
    await fallback.locator('.et-tab').filter({ hasText: 'App.vue' }).first().waitFor({ timeout: 10000 })
    assert.equal(await fallback.locator('.wb-rightrail').count(), 0, `${label} 不应出现右栏`)
    assert.equal(await fallback.locator('.rr-resizer').count(), 0, `${label} 不应出现拖拽条`)
    // 降级路径：查看改动仍走原弹窗（DiffBody 抽取后的回归覆盖）
    await openDiffFromTrace(fallback)
    await fallback.locator('.fd-overlay').waitFor({ timeout: 10000 })
    assert.equal(await fallback.locator('.fd-overlay .fd-file').count(), 1, `${label} 弹窗应渲染改动`)
    assert((await fallback.locator('.fd-overlay .fd-row.fd-del').count()) > 0, `${label} 弹窗应有删除行`)
    await fallback.screenshot({ path: path.join(output, `right-rail-dialog-${label}.png`) })
    await fallback.locator('.fd-close').click()
    await fallback.locator('.fd-overlay').waitFor({ state: 'detached' })
    assert.equal(await fallback.locator('.wb-rightrail').count(), 0)
    report.cases.push({ case: label, width: opts.width, layout: opts.layout || 'fixed', rail: 0, dialog: 1 })
    report.interactions.push(`${label} 布局：引用回落编辑器，查看改动回落弹窗，无右栏/无拖拽条`)
    await fallback.close()
  }
  ran.push('narrow-and-dock-fallback')

  ran.push('screenshots')
  assert.equal(report.errors.length, 0, JSON.stringify(report.errors))
  assert.equal(report.writes.length, 0, `Unexpected writes: ${report.writes}`)
} finally {
  const missing = planned.filter((id) => !ran.includes(id))
  report.missing = missing
  report.ok = missing.length === 0 && report.errors.length === 0
  await fs.writeFile(path.join(output, 'right-rail-report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
  await browser.close()
  if (!report.ok) process.exitCode = 1
}
const missing = planned.filter((id) => !ran.includes(id))
assert.deepEqual(missing, [], `未执行的验收项：${missing.join(', ')}`)
assert.deepEqual([...ran].sort(), [...planned].sort(), 'planned 与 ran 必须一一对应（只比较集合，不比较执行顺序）')
