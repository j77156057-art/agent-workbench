import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs/promises'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE || process.env.HOME, '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))
const root = fileURLToPath(new URL('../../', import.meta.url))
const output = path.join(root, 'artifacts/shots')
const base = process.env.DOCMIND_UI_TEST_URL || 'http://127.0.0.1:8000'
const report = { cases: [], interactions: [], errors: [], writes: [] }
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true,
  args: process.env.PLAYWRIGHT_NO_SANDBOX === '1' ? ['--no-sandbox', '--disable-dev-shm-usage'] : [] })

function measure() {
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = 1
  const ctx = canvas.getContext('2d', { willReadFrequently: true })
  const rgb = (color) => {
    ctx.clearRect(0, 0, 1, 1)
    ctx.fillStyle = color
    ctx.fillRect(0, 0, 1, 1)
    return [...ctx.getImageData(0, 0, 1, 1).data]
  }
  const over = (fg, bg) => fg.slice(0, 3).map((v, i) => v * fg[3] / 255 + bg[i] * (1 - fg[3] / 255)).concat(255)
  const background = (el) => {
    const parent = el.parentElement ? background(el.parentElement) : [255, 255, 255, 255]
    return over(rgb(getComputedStyle(el).backgroundColor), parent)
  }
  const light = (color) => color.slice(0, 3).map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4).reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0)
  const contrast = (fg, bg) => (Math.max(light(fg), light(bg)) + .05) / (Math.min(light(fg), light(bg)) + .05)
  const roots = '.ask-main, .ask-side, .ov, .ws-bar, .cd-empty, .cd-inputbar, .wb-topbar'
  const pairs = []
  for (const el of document.querySelectorAll(`${roots}`)) {
    for (const item of [el, ...el.querySelectorAll('*')]) {
      const box = item.getBoundingClientRect()
      const style = getComputedStyle(item)
      if (!box.width || !box.height || box.bottom <= 0 || box.top >= innerHeight || box.right <= 0 || box.left >= innerWidth || item.closest('[disabled], [inert], [aria-hidden="true"]') || style.visibility !== 'visible') continue
      const direct = [...item.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join('').trim()
      const placeholder = item instanceof HTMLTextAreaElement && !item.value && item.placeholder
      if (!direct && !placeholder) continue
      const actual = placeholder ? getComputedStyle(item, '::placeholder') : style
      const bg = background(item)
      const ratio = contrast(over(rgb(actual.color), bg), bg)
      const size = Number.parseFloat(style.fontSize)
      const threshold = size >= 24 || (size >= 18.66 && Number.parseFloat(style.fontWeight) >= 700) ? 3 : 4.5
      pairs.push({ text: (direct || placeholder).slice(0, 40), class: item.className, ratio: +ratio.toFixed(2), threshold })
    }
  }
  return { overflow: document.documentElement.scrollWidth > innerWidth, pairs: pairs.length, minimum: Math.min(...pairs.map(p => p.ratio)), failures: pairs.filter(p => p.ratio + .01 < p.threshold) }
}

// 工作台的颜色由**配色档**（palette，palette.ts）决定，不是 skins.ts 那套皮肤
// （skins.ts 只管问答页）。2026-11-20 起工作台默认是「墨屿纸感」，所以对比度
// 采样必须显式带上配色档 —— 否则工作台那四轮「换皮肤」量到的其实是同一档，
// 另外三档（浅色 / 观测舱 / 墨屿夜航）根本没被测到。
const WORKBENCH_PALETTES = ['light', 'observatory', 'moyu', 'moyu-night']

async function createPage(skin, width, height = 900, finalText = '这是隔离的界面测试回答，未调用真实模型。', palette = '') {
  const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce' })
  page.on('pageerror', error => report.errors.push(error.message))
  page.on('dialog', dialog => dialog.accept())
  await page.addInitScript(({ skin, palette }) => {
    localStorage.clear()
    sessionStorage.clear()
    localStorage.setItem('docmind.skin', skin)
    localStorage.setItem('docmind.skinLocked', '1')
    // 留空 = 用应用自己的默认档；给了才显式切。
    if (palette) localStorage.setItem('docmind.workbenchPalette', palette)
  }, { skin, palette })
  let chatCalls = 0
  let failChat = false
  const sessions = Array.from({ length: 18 }, (_, i) => ({ session_id: `ask-ui-fixture-${i}`, title: i === 0 ? '项目架构与入口分析' : `资料阅读记录 ${i}`, preview: '界面回归测试会话', updated_at: '2026-10-28T08:00:00Z' }))
  await page.route('**/api/**', async route => {
    const req = route.request()
    const pathname = new URL(req.url()).pathname
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (pathname === '/api/sessions') return json({ ok: true, items: sessions })
    if (pathname.startsWith('/api/sessions/')) {
      if (req.method() === 'DELETE') return json({ ok: true })
      return json({ ok: true, session_id: pathname.split('/').at(-1), turns: pathname.endsWith('ask-ui-fixture-0') ? [{ user: '这个项目的入口在哪里？', assistant: '项目由前端界面和后端服务组成。\n\n## 接下来\n先阅读入口文件，再检查主要模块。' }] : [] })
    }
    if (pathname === '/api/turn/active') return json({ ok: true, turns: [] })
    if (pathname === '/api/chat') {
      chatCalls++
      if (failChat) return json({ error: '界面测试：服务暂不可用' }, 503)
      return route.fulfill({
        contentType: 'text/event-stream',
        body: 'data: ' + JSON.stringify({ type: 'final', text: finalText, status: 'done' }) + '\n\n'
          + 'data: ' + JSON.stringify({ type: 'done' }) + '\n\n',
      })
    }
    if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) {
      report.writes.push(pathname)
      return json({ ok: false, error: 'UI test blocks backend writes' }, 409)
    }
    return route.continue()
  })
  return { page, calls: () => chatCalls, fail: () => { failChat = true } }
}

try {
  for (const skin of ['zinc', 'graphite', 'paper', 'seed']) {
    for (const width of [1440, 1024, 390]) {
      const { page } = await createPage(skin, width, width === 390 ? 844 : 900)
      await page.goto(base + '/')
      await page.locator('.composer textarea').waitFor()
      await page.locator('.sess-item').first().waitFor({ state: 'attached' })
      await page.evaluate(() => document.fonts.ready)
      const metrics = await page.evaluate(measure)
      report.cases.push({ page: 'ask', skin, width, ...metrics })
      await page.screenshot({ path: path.join(output, `product-ask-${skin}-${width}.png`), fullPage: true })
      assert.equal(metrics.overflow, false, `ask ${skin} ${width} overflow`)
      await page.close()
    }
  }
  const { page, calls, fail } = await createPage('zinc', 1440)
  await page.goto(base + '/')
  const input = page.locator('.composer textarea')
  await input.waitFor()
  const contrastMutation = await page.addStyleTag({ content: '.welcome-title { color: #ffffff !important; background: #ffffff !important; }' })
  // 注入 <style> 后浏览器不一定已经重算样式；等计算值真的变白再测，避免假阴性。
  await page.waitForFunction(() => {
    const el = document.querySelector('.welcome-title')
    if (!el) return false
    const s = getComputedStyle(el)
    return s.color === 'rgb(255, 255, 255)' && s.backgroundColor === 'rgb(255, 255, 255)'
  })
  const mutated = await page.evaluate(measure)
  assert(mutated.failures.some(item => item.class === 'welcome-title' && item.ratio === 1), 'Contrast guard did not detect white-on-white mutation')
  await contrastMutation.evaluate(el => el.remove())
  report.interactions.push('对比度检测器能识别白字白底注入缺陷，移除后恢复')
  await page.getByRole('button', { name: '读懂项目' }).click()
  assert.match(await input.inputValue(), /梳理当前项目/)
  assert.equal(calls(), 0)
  await input.dispatchEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true })
  assert.equal(calls(), 0)
  await page.getByRole('button', { name: '联网搜索', exact: true }).click()
  assert.equal(await page.getByRole('button', { name: '联网搜索', exact: true }).getAttribute('aria-pressed'), 'true')
  await page.locator('.send-btn').click()
  await page.getByText('这是隔离的界面测试回答，未调用真实模型。', { exact: true }).waitFor()
  // 成功回合按现契约隐藏「已回复」徽标；异常才显示。断言无危险态徽标残留。
  assert.equal(await page.locator('.ai-status.tone-danger').count(), 0)
  assert.equal(await page.getByText(/流式连接在服务完成前中断/).count(), 0)
  assert.equal(calls(), 1)
  assert.equal(await page.locator('.suggestions').count(), 0)
  await page.screenshot({ path: path.join(output, 'product-ask-answer.png') })
  await page.getByRole('button', { name: '新对话', exact: true }).click()
  await page.locator('.welcome-title').waitFor()
  await page.locator('.sess-main').filter({ hasText: '项目架构与入口分析' }).click()
  await page.getByText('这个项目的入口在哪里？', { exact: true }).waitFor()
  await page.getByRole('button', { name: '新对话', exact: true }).click()
  fail()
  await input.fill('错误态测试')
  await page.locator('.send-btn').click()
  await page.getByText(/服务暂不可用/).first().waitFor()
  report.interactions.push('建议预填不发送、中文输入法回车、联网开关、发送回答、历史切换、新对话、请求失败')
  await page.setViewportSize({ width: 390, height: 844 })
  const toggle = page.getByRole('button', { name: '打开导航', exact: true })
  await toggle.click()
  assert.equal(await page.locator('.ask-main').evaluate(el => el.inert), true)
  await page.keyboard.press('Escape')
  assert.equal(await toggle.evaluate(el => el === document.activeElement), true)
  await toggle.click()
  await page.keyboard.press('Shift+Tab')
  assert.equal(await page.locator('.ask-side').evaluate(el => el.contains(document.activeElement)), true)
  await page.getByRole('button', { name: '打开模型参数设置', exact: true }).click()
  assert.equal(await page.locator('.ask-main').evaluate(el => el.inert), false)
  assert.equal(await page.locator('.ask-side').isVisible(), false)
  await page.waitForFunction(() => document.activeElement?.closest('.ms-box'))
  report.interactions.push('移动导航打开、Escape与焦点归还、Tab焦点约束、抽屉进入模型设置')
  await page.reload()
  await toggle.click()
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.waitForFunction(() => !document.querySelector('.ask-main').inert)
  report.interactions.push('抽屉打开时切回桌面解除主区锁定')
  await page.close()

  // 工作台的颜色源是**配色档**（palette.ts），不是 skins.ts 那套皮肤 —— 工作台
  // 根本没读 docmind.skin，所以之前这一圈「换皮肤」量到的其实是同一档配色。
  // 2026-11-20 起默认是「墨屿纸感」，因此这里按配色档逐档采样，让四档
  // （浅色 / 观测舱 / 墨屿纸感 / 墨屿夜航）都真正过一遍对比度守卫。
  for (const palette of WORKBENCH_PALETTES) {
    const { page, calls } = await createPage('zinc', 1440, 900, undefined, palette)
    await page.goto(base + '/workbench')
    await page.locator('.ov-page-head').waitFor()
    await page.evaluate(() => document.fonts.ready)
    // 状态栏的降级态（.msb-chip.is-problem）只在**低功耗机器**上出现：CI runner
    // 是这种机器，本地是「高配」，所以本地根本跑不到那个状态 —— 不强制的话，
    // 这个状态在任何一台高配机器上都没被测到（2026-11-20 就是这样漏掉了夜航档
    // 砖橙当文字色、3.99:1 的对比度缺陷）。这里强制成降级态再采样。
    await page.evaluate(() => {
      const chip = document.querySelector('.msb-chip')
      if (!chip) return
      chip.classList.add('is-problem')
      const label = chip.querySelector('.msb-text')
      if (label) label.textContent = '低配 · 等待帧观测'
    })
    const metrics = await page.evaluate(measure)
    report.cases.push({ page: 'workbench', palette, width: 1440, ...metrics })
    await page.screenshot({ path: path.join(output, `product-workbench-${palette}.png`) })
    const action = page.getByRole('button', { name: '梳理项目结构', exact: true }).first()
    await action.click()
    await page.waitForFunction(() => document.querySelector('.cd-input')?.value.includes('梳理当前项目'))
    assert.equal(calls(), 0)
    const inputBox = await page.locator('.cd-input').boundingBox()
    assert(inputBox.y + inputBox.height <= 900, 'workbench composer clipped')
    await page.getByRole('button', { name: '更多', exact: true }).click()
    await page.locator('[data-wb-asset]').click()
    assert.equal(await page.locator('.wb-view-label').textContent(), '素材')
    assert.equal(await page.locator('[data-wb-asset]').getAttribute('aria-current'), 'page')
    const viewButton = page.locator('.wb-view-btn')
    await viewButton.click()
    await page.locator('[data-wb-view="overview"]').click()
    assert.equal(await page.locator('.wb-view-label').textContent(), '概览')
    report.interactions.push(`${palette} 工作台行动预填、输入框可见、素材从更多进入并回到概览`)
    await page.close()
  }
  // 尺寸回归固定用「墨屿纸感」这一档（当前默认），与上面的四档配色采样分工：
  // 这里量的是布局，palette 覆盖率由上面那圈负责。
  for (const [width, height] of [[1024, 768], [390, 844], [1440, 600]]) {
    const { page } = await createPage('zinc', width, height, undefined, 'moyu')
    await page.goto(base + '/workbench')
    await page.locator('.ov-page-head').waitFor()
    await page.evaluate(() => document.fonts.ready)
    const metrics = await page.evaluate(measure)
    assert.equal(metrics.overflow, false, `workbench ${width} overflow`)
    if (width <= 640) {
      const mainBox = await page.locator('.wb-main').boundingBox()
      assert(mainBox.width >= width - 48, 'Mobile workbench is compressed by file tree')
      const searchBox = await page.locator('.lb-input').boundingBox()
      const topbarBox = await page.locator('.wb-topbar').boundingBox()
      assert(searchBox.y >= 0 && searchBox.y + searchBox.height <= topbarBox.height, 'Mobile search is clipped by topbar')
      await page.getByRole('button', { name: '展开资源管理器', exact: true }).click()
      await page.locator('.ft-panel').waitFor()
      assert.equal(await page.locator('.wb-main').evaluate(el => el.inert), true)
      await page.keyboard.press('Escape')
      assert.equal(await page.locator('.ft-panel').isVisible(), false)
      assert.equal(await page.locator('.wb-main').evaluate(el => el.inert), false)
      report.interactions.push('移动工作台文件树抽屉、Escape关闭及主区宽度下限')
    }
    report.cases.push({ page: 'workbench', palette: 'moyu', width, height, ...metrics })
    await page.screenshot({ path: path.join(output, `product-workbench-${width}-${height}.png`) })
    await page.locator('.cd-size-btn').click()
    const box = await page.locator('.cd-input').boundingBox()
    assert(box.y + box.height <= height, `expanded composer clipped ${width}x${height}`)
    await page.locator('.cd-head-toggle').click()
    assert.equal(await page.locator('.cd-head-toggle').getAttribute('aria-expanded'), 'false')
    await page.locator('.cd-head-toggle').click()
    assert.equal(await page.locator('.cd-head-toggle').getAttribute('aria-expanded'), 'true')
    report.interactions.push(`${width}×${height} 工作台无横向溢出、展开后输入框可见、折叠恢复`)
    await page.close()
  }
  // 对话台底部锚定：短会话贴输入框；长会话可滚到第一条、上翻出现「回到底部」并能回底
  {
    const bodyMetrics = (page) => page.evaluate(() => {
      const body = document.querySelector('.cd-body')
      const msgs = [...document.querySelectorAll('.cd-msg')]
      const first = msgs[0].getBoundingClientRect()
      const last = msgs.at(-1).getBoundingClientRect()
      const box = body.getBoundingClientRect()
      return {
        expanded: !!document.querySelector('.cd-dock.cd-expanded'),
        bodyHeight: body.clientHeight,
        scrollHeight: body.scrollHeight,
        distance: body.scrollHeight - body.scrollTop - body.clientHeight,
        gapBelowLast: +(box.bottom - last.bottom).toFixed(1),
        firstVisible: first.top >= box.top - 1,
        jump: !!document.querySelector('.cd-jump-bottom'),
      }
    })
    const { page: short } = await createPage('zinc', 1440, 900, undefined, 'moyu')
    await short.goto(base + '/workbench')
    await short.locator('.cd-input').fill('短会话贴底验证')
    await short.locator('.cd-send').click()
    await short.waitForFunction(() => document.querySelector('.cd-msg-assistant .cd-answer')?.textContent.includes('隔离的界面测试回答'))
    await short.waitForTimeout(700)
    const shortM = await bodyMetrics(short)
    assert.equal(shortM.expanded, true, `asking must expand the dock: ${JSON.stringify(shortM)}`)
    assert(shortM.scrollHeight <= shortM.bodyHeight + 1, `short thread should not overflow: ${JSON.stringify(shortM)}`)
    assert(shortM.gapBelowLast <= 32, `short thread must sit on the composer: ${JSON.stringify(shortM)}`)
    assert.equal(shortM.jump, false, 'no jump control when everything is visible')
    await short.locator('.cd-body').hover()
    await short.mouse.wheel(0, -160)
    await short.waitForTimeout(150)
    assert.equal(await short.locator('.cd-jump-bottom').count(), 0, 'wheel on a short thread must not pretend to leave the bottom')
    await short.screenshot({ path: path.join(output, 'chatdock-bottom-anchored.png') })
    report.interactions.push('短会话贴底、无溢出时上滚不出现「回到底部」')
    await short.close()

    const longText = Array.from({ length: 40 }, (_, i) => `- 第 ${i + 1} 行：滚动回归的长回答，用来确认消息区可以正常溢出，且从顶上还能回到底部。`).join('\n')
    const { page: long } = await createPage('zinc', 1440, 900, longText, 'moyu')
    await long.goto(base + '/workbench')
    await long.locator('.cd-input').fill('长会话滚动验证')
    await long.locator('.cd-send').click()
    await long.waitForFunction(() => document.querySelector('.cd-msg-assistant .cd-answer')?.textContent.includes('第 40 行'))
    await long.waitForTimeout(900)
    const atBottom = await bodyMetrics(long)
    assert(atBottom.scrollHeight - atBottom.bodyHeight > 200, `long thread should overflow: ${JSON.stringify(atBottom)}`)
    assert(atBottom.distance < 80, `answer must stay glued to the bottom: ${JSON.stringify(atBottom)}`)
    assert.equal(atBottom.jump, false, 'no jump control while at bottom')
    await long.evaluate(() => { const el = document.querySelector('.cd-body'); el.scrollTop = 0; el.dispatchEvent(new Event('scroll')) })
    await long.waitForTimeout(150)
    const upTop = await bodyMetrics(long)
    assert.equal(upTop.firstVisible, true, `first message must be reachable at scrollTop 0: ${JSON.stringify(upTop)}`)
    assert.equal(upTop.jump, true, 'jump control appears after scrolling up')
    await long.screenshot({ path: path.join(output, 'chatdock-scrolled-up.png') })
    await long.locator('.cd-jump-bottom').click()
    await long.waitForTimeout(500)
    const back = await bodyMetrics(long)
    assert(back.distance < 8, `jump control must return to the newest message: ${JSON.stringify(back)}`)
    assert.equal(back.jump, false, 'jump control hides once back at bottom')
    report.interactions.push('长会话溢出后可到顶、上翻出现「回到底部」、点击回到最新')
    await long.close()
  }
  assert.equal(report.errors.length, 0, JSON.stringify(report.errors))
  assert.equal(report.writes.length, 0, `Unexpected writes: ${report.writes}`)
} finally {
  await fs.writeFile(path.join(output, 'product-ui-report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
  await browser.close()
}
assert.equal(report.cases.flatMap(c => c.failures).length, 0, 'Rendered text contrast failures')
