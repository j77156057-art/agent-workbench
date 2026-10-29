// 问答页真实对话态验收：
// 1) 开发夹具挂载真实 AskMessage，回放长回答 / 运行中 / 失败 / 授权 / 工作流等状态；
// 2) 生产页面在 fetch 边界做分帧 SSE 桩，验证真实 AskApp + postSse + useChatStream。
// 不使用真实模型额度；报告里区分组件回放与页面级接口桩。
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs/promises'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE, '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))
const root = fileURLToPath(new URL('../../', import.meta.url))
const output = path.join(root, 'artifacts/shots')
const fixtureBase = process.env.DOCMIND_UI_FIXTURE_URL || 'http://127.0.0.1:5174/tests/ask-message.fixture.html'
const appBase = process.env.DOCMIND_UI_TEST_URL || 'http://127.0.0.1:8000'
const report = {
  fixture: [],
  app: [],
  interactions: [],
  contrast: [],
  errors: [],
  writes: [],
  mode: {
    fixture: '开发夹具，挂载真实 AskMessage',
    appChat: '生产页面 fetch 边界分帧 SSE 桩（不调用真实模型）',
    appHttp: 'Playwright 路由桩',
  },
}

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(fixtureBase).origin })
await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(appBase).origin })

const CONFIG = {
  ok: true,
  llm_provider: 'deepseek',
  llm_model: 'ui-fixture',
  embedding_provider: 'local',
  has_key: true,
  providers: ['deepseek'],
  provider_meta: {},
  custom_base_url: '',
  capability: { context_window: 32768, thinking: 'toggle', vision: 'native', video: 'none', cloud: true },
  ingested_files: ['战斗规则.md'],
  warnings: [],
}

const APP_ANSWER = `## 结论

请求由 \`session.js\` 取得会话 id，再通过 \`/api/chat\` 建立流式连接。

- 会话按页面域隔离。
- 工具结果以 observation 回传。
- 最终回答以 final 收束，done 表示传输结束。

相关实现位于 frontend/src/ask/components/AskMessage.vue:800 与 frontend/src/ask/AskApp.vue:835。`

const REF_ANSWER = `可以在这里查看源码：

- frontend/src/ask/components/AskMessage.vue:800
- frontend/src/ask/AskApp.vue:835
- frontend/src/ask/slow.ts:4
- frontend/src/ask/fast.ts:6`

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
  const pairs = []
  for (const el of document.querySelectorAll('.ask-main .msg, .ask-main .answer, .ask-main .process, .ask-main .refs, .ask-main .composer')) {
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

async function newFixture(width = 1440, height = 900) {
  const page = await context.newPage()
  await page.setViewportSize({ width, height })
  page.on('pageerror', e => report.errors.push(`fixture: ${e.message}`))
  // 只看真正的 /api/ 路径。Vite 的模块请求里也含 /src/workbench/api/，
  // 用 '**/api/**' 会把 WorkflowCard 依赖的源码模块当成后端接口拦掉。
  await page.route('**/*', route => {
    const req = route.request()
    const pathname = new URL(req.url()).pathname
    if (!pathname.startsWith('/api/')) return route.fallback()
    if (req.method() !== 'GET') {
      report.writes.push(req.url())
      return route.fulfill({ status: 409, contentType: 'application/json', body: '{"ok":false,"error":"fixture blocks writes"}' })
    }
    if (pathname.endsWith('/events')) {
      return route.fulfill({
        contentType: 'text/event-stream',
        body: 'data: ' + JSON.stringify({ kind: '__stream_done__' }) + '\n\n',
      })
    }
    if (pathname.endsWith('/evaluation')) {
      return route.fulfill({ contentType: 'application/json', body: '{"ok":true,"evaluation":null}' })
    }
    if (pathname === '/api/agent/workflow/wf-fixture-1') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          ok: true,
          workflow: {
            workflow_id: 'wf-fixture-1',
            status: 'planned',
            phase: '计划已生成',
            kind: 'generic',
            request: '界面夹具工作流',
            tasks: [],
            options: [],
            events: [],
            results: {},
          },
        }),
      })
    }
    return route.fallback()
  })
  await page.goto(fixtureBase + '?skin=zinc')
  await page.waitForFunction(() => window.__askFixture)
  await page.evaluate(() => document.fonts.ready)
  return page
}

// ---------------------------------------------------------------- 组件回放
{
  const page = await newFixture()
  await page.evaluate(() => window.__askFixture.setCase('long-answer'))
  await page.waitForTimeout(80)
  const before = await page.evaluate(() => ({
    processOpen: document.querySelectorAll('.process-body').length,
    processSummary: document.querySelector('.process-summary')?.textContent,
    status: [...document.querySelectorAll('.ai-status')].map(el => el.textContent),
    answerFont: getComputedStyle(document.querySelector('.answer')).fontSize,
    answerWidth: document.querySelector('.answer').getBoundingClientRect().width,
    refItems: document.querySelectorAll('.ref-item').length,
    copyButtons: document.querySelectorAll('.action-btn').length,
  }))
  assert.equal(before.processOpen, 0)
  assert.match(before.processSummary, /读取 1 个文件/)
  assert.deepEqual(before.status, ['已回复', '已回复'])
  // 2026-10-29：回答正文由 16px 降到 15px（用户反馈偏大），守卫跟着走。
  assert.equal(before.answerFont, '15px')
  assert(before.answerWidth >= 700)
  assert.equal(before.refItems, 0)
  assert.equal(before.copyButtons, 2)
  report.fixture.push({ case: 'long-answer', ...before })

  await page.locator('.process-toggle').first().click()
  await page.waitForTimeout(60)
  assert.equal(await page.locator('.process-body').count(), 1)
  assert((await page.locator('.narration').count()) >= 2)
  assert.equal(await page.locator('.flow-row').count(), 4)
  assert.equal(await page.locator('.process-toggle').count(), 1, 'process must not nest another level')

  await page.locator('.refs-toggle').first().click()
  await page.waitForTimeout(40)
  assert.equal(await page.locator('.ref-item').count(), 6)
  await page.locator('.refs-more').first().click()
  await page.waitForTimeout(40)
  assert.equal(await page.locator('.ref-item').count(), 9)
  await page.locator('.ref-item').first().click()
  const opened = await page.evaluate(() => window.__askFixture.state.lastOpenFile)
  assert.equal(opened.path, 'session.js')
  report.interactions.push('长回答：过程单层展开、引用 6→9 条、点击引用传递 path/line')

  await page.locator('.action-btn').first().click()
  await page.getByText('已复制到剪贴板').waitFor()
  const clipboard = await page.evaluate(() => navigator.clipboard.readText())
  const answerText = await page.evaluate(() => window.__askFixture.state.messages[1].text)
  // Windows 剪贴板会把换行规范化为 CRLF；比较语义内容，不因此假报失败。
  assert.equal(clipboard.replace(/\r\n/g, '\n'), answerText.replace(/\r\n/g, '\n'))
  assert(!clipboard.includes('深度思考'))
  await page.evaluate(() => {
    Object.defineProperty(navigator.clipboard, 'writeText', { value: () => Promise.reject(new Error('denied')), configurable: true })
  })
  await page.locator('.action-btn').first().click()
  await page.getByText('复制失败，请选择文本复制').waitFor()
  report.interactions.push('复制回答成功内容等于正文，失败有明确提示')

  await page.evaluate(() => window.__askFixture.setPrefs({ showTrace: false }))
  await page.waitForTimeout(40)
  const noTrace = await page.evaluate(() => ({
    summary: document.querySelector('.process-summary')?.textContent,
    rows: document.querySelectorAll('.flow-row').length,
    narration: document.querySelectorAll('.narration').length,
  }))
  assert.equal(noTrace.summary, '查看思考过程')
  assert.equal(noTrace.rows, 0)
  assert(noTrace.narration > 0)
  await page.evaluate(() => window.__askFixture.setPrefs({ showTrace: true, autoExpandTrace: true }))
  report.interactions.push('关闭推理轨迹后不显示工具行，但保留自然语言思考')

  for (const [name, status, note] of [
    ['failed', '回答失败', '失败'],
    ['stopped', '已停止', ''],
    ['degraded', '未完成', '未完成'],
    ['warn', '需核对', '需核对'],
    ['verbatim', '未综合结论', '未综合结论'],
  ]) {
    await page.evaluate((c) => window.__askFixture.setCase(c), name)
    await page.waitForTimeout(40)
    const got = await page.evaluate(() => ({
      status: document.querySelector('.ai-status')?.textContent,
      note: document.querySelector('.quality-note .quality-badge')?.textContent || '',
      text: document.querySelector('.answer')?.textContent || '',
    }))
    assert.equal(got.status, status, `${name} status`)
    assert.equal(got.note, note, `${name} note`)
    if (name === 'failed') assert(got.text.includes('已收到的部分内容'))
    if (name === 'warn') assert(got.note === '需核对')
    report.fixture.push({ case: name, status: got.status, note: got.note })
  }

  await page.evaluate(() => window.__askFixture.setCase('permission'))
  await page.waitForTimeout(40)
  assert.equal(await page.locator('.ai-status').textContent(), '等待授权')
  await page.locator('.ask-opt').first().click()
  await page.waitForTimeout(60)
  assert.equal(await page.evaluate(() => window.__askFixture.state.lastAsk), '允许本次修改')
  assert.equal(await page.locator('.ai-status').textContent(), '已提交选择')
  assert.equal(await page.locator('.ask-opt').first().isDisabled(), true)
  report.interactions.push('授权选项只提交一次，状态变为已提交选择')

  await page.evaluate(() => window.__askFixture.setCase('workflow'))
  await page.waitForTimeout(350)
  assert(await page.locator('.wf-card').count() > 0, 'workflow card should render')
  await page.locator('.process-toggle').first().click()
  await page.waitForTimeout(60)
  assert(await page.locator('.wf-card').count() > 0, 'workflow card must survive process toggle')
  report.interactions.push('工作流卡片不受执行过程折叠影响')

  await page.evaluate(() => window.__askFixture.setCase('running'))
  await page.waitForTimeout(40)
  assert.equal(await page.locator('.ai-status').textContent(), '正在等待模型或工具返回')
  await page.evaluate(() => window.__askFixture.appendEvent({ type: 'reasoning', text: '先确认入口与会话。' }))
  await page.evaluate(() => window.__askFixture.appendEvent({ type: 'action', text: 'read_file(frontend/src/ask/AskApp.vue)' }))
  await page.evaluate(() => window.__askFixture.appendEvent({ type: 'observation', text: '读取到入口发送逻辑。', ok: true }))
  await page.waitForTimeout(60)
  assert.equal(await page.locator('.process-body').count(), 0)
  assert.match(await page.locator('.process-summary').textContent(), /读取 1 个文件/)
  await page.locator('.process-toggle').click()
  await page.waitForTimeout(40)
  const autoOpen = await page.locator('.flow-detail').count()
  assert.equal(autoOpen, 1, 'autoExpandTrace should open the latest row while streaming')
  await page.locator('.flow-row').last().click()
  await page.evaluate(() => window.__askFixture.setPrefs({ autoExpandTrace: false }))
  await page.evaluate(() => window.__askFixture.appendEvent({ type: 'action', text: 'grep("postSse")' }))
  await page.evaluate(() => window.__askFixture.appendEvent({ type: 'observation', text: '命中 sse.ts。', ok: true }))
  await page.waitForTimeout(60)
  assert.equal(await page.locator('.flow-detail').count(), 0, 'manual collapse must survive later events')
  await page.evaluate(() => window.__askFixture.appendEvent({ type: 'token', text: '这是流式回答的开头。' }))
  await page.waitForTimeout(40)
  assert.equal(await page.locator('.ai-status').textContent(), '正在回答')
  assert(await page.locator('.answer').count() > 0)
  await page.evaluate(() => window.__askFixture.appendEvent({ type: 'final', text: '这是流式回答的最终内容。' }))
  await page.evaluate(() => window.__askFixture.appendEvent({ type: 'done' }))
  await page.waitForTimeout(40)
  assert.equal(await page.locator('.ai-status').textContent(), '已回复')
  assert.equal(await page.locator('.answer').textContent(), '这是流式回答的最终内容。')
  report.interactions.push('运行中：阶段状态、过程默认关闭、自动摊开最新行、手收后不被追加重开')

  await page.evaluate(() => window.__askFixture.setPrefs({ showTrace: true, autoExpandTrace: true }))
  await page.close()
}

// ---------------------------------------------------------------- 主题 / 尺寸矩阵与对比度
{
  const page = await newFixture()
  for (const skin of ['zinc', 'graphite', 'paper', 'seed']) {
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 900 })
      await page.evaluate((s) => {
        window.__askFixture.setSkin(s)
        window.__askFixture.setCase('long-answer')
        window.__askFixture.setPrefs({ showTrace: true, autoExpandTrace: true })
      }, skin)
      await page.waitForTimeout(80)
      const metrics = await page.evaluate(measure)
      report.contrast.push({ page: 'fixture', skin, width, ...metrics })
      await page.mouse.move(1, 1)
      await page.screenshot({ path: path.join(output, `ask-conversation-after-${skin}-${width}.png`), fullPage: true })
      assert.equal(metrics.overflow, false, `fixture ${skin} ${width} overflow`)
      assert.equal(metrics.failures.length, 0, `fixture ${skin} ${width} contrast: ${JSON.stringify(metrics.failures)}`)
      await page.locator('.process-toggle').first().click()
      await page.waitForTimeout(50)
      await page.screenshot({ path: path.join(output, `ask-conversation-after-process-${skin}-${width}.png`), fullPage: true })
      await page.locator('.process-toggle').first().click()
    }
  }
  await page.close()
}

// ---------------------------------------------------------------- 生产页面：fetch 分帧 SSE
function installAskTransport() {
  const originalFetch = window.fetch.bind(window)
  const calls = []
  window.__askTransport = {
    calls,
    emit(id, event) {
      const call = calls.find(c => c.id === id)
      if (!call || !call.alive) return false
      call.controller.enqueue(new TextEncoder().encode('data: ' + JSON.stringify(event) + '\n\n'))
      return true
    },
    finish(id, text) {
      this.emit(id, { type: 'final', text, status: 'done' })
      this.emit(id, { type: 'done' })
      const call = calls.find(c => c.id === id)
      if (call && call.alive) { call.alive = false; call.controller.close() }
    },
    disconnect(id) {
      const call = calls.find(c => c.id === id)
      if (call && call.alive) { call.alive = false; call.controller.close() }
    },
  }
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input.url
    const method = String((init && init.method) || (typeof input === 'object' && input.method) || 'GET').toUpperCase()
    if (url.endsWith('/api/chat') && method === 'POST') {
      const id = calls.length + 1
      const controller = new ReadableStream({
        start(c) {
          const call = { id, controller: c, alive: true }
          calls.push(call)
          const signal = init && init.signal
          if (signal) signal.addEventListener('abort', () => {
            if (!call.alive) return
            call.alive = false
            try { c.error(new DOMException('Aborted', 'AbortError')) } catch { /* already closed */ }
          }, { once: true })
        },
      })
      return Promise.resolve(new Response(controller, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }))
    }
    return originalFetch(input, init)
  }
}

async function newAppPage(width = 1440, height = 900) {
  const page = await context.newPage()
  await page.setViewportSize({ width, height })
  page.on('pageerror', e => report.errors.push(`app: ${e.message}`))
  await page.addInitScript(installAskTransport)
  await page.route('**/api/**', async route => {
    const req = route.request()
    const url = new URL(req.url())
    const p = url.pathname
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (p === '/api/config') return json(CONFIG)
    if (p === '/api/sessions') return json({ ok: true, items: [] })
    if (p.startsWith('/api/sessions/')) return json({ ok: true, session_id: p.split('/').pop(), turns: [] })
    if (p === '/api/turn/active') return json({ ok: true, turns: [] })
    if (p === '/api/agent/workflow/active') return json({ ok: true, workflow: null })
    if (p === '/api/context') return json({ ok: true, used_tokens: 120, context_window: 32768, prompt_budget: 30000, percent: 0.4, level: 'ok' })
    if (p === '/api/fs/file') {
      const target = url.searchParams.get('path') || ''
      if (target.endsWith('slow.ts')) {
        await new Promise(r => setTimeout(r, 300))
        return json({ content: Array.from({ length: 40 }, (_, i) => `slow line ${i + 1}`).join('\n') })
      }
      if (target.endsWith('AskApp.vue')) {
        return json({ content: Array.from({ length: 100 }, (_, i) => `AskApp line ${i + 1}`).join('\n') })
      }
      return json({ content: Array.from({ length: 1200 }, (_, i) => `line ${i + 1}`).join('\n') })
    }
    if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) {
      report.writes.push(p)
      return json({ ok: false, error: 'UI test blocks backend writes' }, 409)
    }
    return route.continue()
  })
  await page.goto(appBase + '/')
  await page.locator('.composer textarea').waitFor()
  return page
}

async function send(page, text) {
  const count = await page.evaluate(() => window.__askTransport.calls.length)
  await page.locator('.composer textarea').fill(text)
  await page.locator('.send-btn:not(.stop)').click()
  await page.waitForFunction(n => window.__askTransport.calls.length > n, count)
  return count + 1
}

// 首屏工作台构图：桌面双栏 + 右侧上下文；窄屏收起右栏，输入框必须留在首屏。
{
  for (const [width, height] of [[1440, 900], [1024, 768], [390, 844]]) {
    const page = await newAppPage(width, height)
    await page.locator('.home-layout').waitFor()
    const composer = await page.locator('.composer textarea').boundingBox()
    assert(composer.y + composer.height <= height, `home composer clipped ${width}x${height}`)
    assert.equal(await page.locator('.home-layout').count(), 1)
    if (width > 920) {
      assert.equal(await page.locator('.home-rail').isVisible(), true, `home rail hidden at ${width}`)
      assert.equal(await page.locator('.workspace-resources').isVisible(), false, `sidebar context duplicated at ${width}`)
      assert.equal(await page.locator('.rail-upload').isVisible(), true)
    } else {
      assert.equal(await page.locator('.home-rail').isVisible(), false, `home rail should collapse at ${width}`)
    }
    await page.mouse.move(1, 1)
    await page.screenshot({ path: path.join(output, `ask-home-final-${width}.png`) })
    report.interactions.push(`${width} 首屏工作台布局与输入框可见性`)
    await page.close()
  }
}

{
  const page = await newAppPage()
  const first = await send(page, '这个项目的问答请求怎么走到模型？')
  await page.waitForFunction(() => document.querySelector('.ai-status')?.textContent === '正在等待模型或工具返回')
  await page.evaluate(id => window.__askTransport.emit(id, { type: 'reasoning', text: '先确认入口与会话。' }), first)
  await page.evaluate(id => window.__askTransport.emit(id, { type: 'action', text: 'read_file(frontend/src/ask/AskApp.vue)' }), first)
  await page.evaluate(id => window.__askTransport.emit(id, { type: 'observation', text: '读取到入口发送逻辑。', ok: true }), first)
  await page.waitForFunction(() => document.querySelector('.process-summary')?.textContent?.includes('读取 1 个文件'))
  assert.equal(await page.locator('.process-body').count(), 0)
  await page.evaluate(id => window.__askTransport.emit(id, { type: 'token', text: '这是流式回答的开头。' }), first)
  await page.waitForFunction(() => document.querySelector('.ai-status')?.textContent === '正在回答')
  await page.evaluate(({ id, text }) => window.__askTransport.finish(id, text), { id: first, text: APP_ANSWER })
  await page.waitForFunction(() => document.querySelector('.ai-status')?.textContent === '已回复')
  assert((await page.locator('.answer').textContent()).includes('请求由'))
  assert.equal(await page.getByText(/流式连接在服务完成前中断/).count(), 0)
  await page.waitForFunction(() => !document.querySelector('.send-btn.stop'))
  report.app.push({ case: 'stream-success', status: '已回复' })

  await page.locator('.process-toggle').click()
  assert((await page.locator('.narration').count()) >= 1)
  assert((await page.locator('.flow-row').count()) >= 2)
  report.interactions.push('生产页面：分阶段状态、final+done 成功收束、过程展开')

  await page.locator('.refs-toggle').click()
  // APP_ANSWER 里除两处显式路径外，行内 `session.js` 也会被识别为引用。
  assert.equal(await page.locator('.ref-item').count(), 3)
  await page.locator('.ref-item').filter({ hasText: 'AskMessage.vue' }).click()
  await page.locator('.fv-modal').waitFor()
  await page.waitForFunction(() => document.querySelector('.fv-ln.hl')?.textContent?.includes('line 800'))
  assert((await page.locator('.fv-code').evaluate(el => el.scrollTop)) > 0, 'target line must scroll into view')
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('fv-x')), true)
  await page.keyboard.press('Shift+Tab')
  assert.equal(await page.evaluate(() => document.activeElement?.closest('.fv-modal') !== null), true)
  await page.keyboard.press('Escape')
  await page.waitForFunction(() => !document.querySelector('.fv-modal'))
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('ref-item')), true)
  report.interactions.push('源码弹层：目标行高亮、焦点进入与闭环、Esc 关闭后焦点归还引用')

  await page.locator('.ref-item').filter({ hasText: 'AskApp.vue' }).click()
  await page.locator('.fv-modal').waitFor()
  await page.getByText(/超出当前加载上限/).waitFor()
  await page.keyboard.press('Escape')
  await page.waitForFunction(() => !document.querySelector('.fv-modal'))
  report.interactions.push('目标行超出加载上限时明确提示，不伪装定位成功')

  await page.locator('.refs-toggle').click()
  await page.close()
}

// 滚动 / 停止 / 断流 / 引用竞态
{
  const page = await newAppPage()
  let id = await send(page, '给我一份较长的说明。')
  const longText = Array.from({ length: 26 }, (_, i) => `第 ${i + 1} 段：这是一段用于撑开滚动区域的长内容。`).join('\n\n')
  await page.evaluate(({ id, text }) => window.__askTransport.finish(id, text), { id, text: longText })
  await page.waitForFunction(() => document.querySelector('.ai-status')?.textContent === '已回复')

  id = await send(page, '再来一段更长的。')
  await page.evaluate(id => {
    window.__askTransport.emit(id, { type: 'token', text: Array.from({ length: 30 }, (_, i) => `流式段落 ${i + 1}：继续写入内容以验证不抢滚动。`).join('\n\n') })
  }, id)
  await page.waitForTimeout(80)
  await page.evaluate(() => { const el = document.querySelector('.chat-scroll'); el.scrollTop = 0 })
  await page.waitForTimeout(60)
  const top = await page.evaluate(() => document.querySelector('.chat-scroll').scrollTop)
  await page.evaluate(id => window.__askTransport.emit(id, { type: 'token', text: '\n\n追加内容不应拉走视口。' }), id)
  await page.waitForTimeout(80)
  const after = await page.evaluate(() => document.querySelector('.chat-scroll').scrollTop)
  assert(Math.abs(after - top) <= 2, `scroll jumped ${top} -> ${after}`)
  await page.locator('.jump-latest').click()
  await page.waitForTimeout(80)
  const bottom = await page.evaluate(() => { const el = document.querySelector('.chat-scroll'); return el.scrollHeight - el.scrollTop - el.clientHeight })
  assert(bottom < 90, `jump to latest did not reach bottom: ${bottom}`)
  report.interactions.push('用户上滚后追加内容不抢视口，回到最新回答可恢复跟随')

  await page.locator('.send-btn.stop').click()
  await page.waitForFunction(() => [...document.querySelectorAll('.ai-status')].at(-1)?.textContent === '已停止')
  assert.equal(await page.locator('.send-btn.stop').count(), 0)
  await page.evaluate(id => window.__askTransport.emit(id, { type: 'token', text: '停止后不应出现。' }), id)
  await page.waitForTimeout(60)
  assert(!(await page.locator('.answer').last().textContent()).includes('停止后不应出现'))
  report.interactions.push('停止后状态为已停止，旧流不再写正文')

  id = await send(page, '测试断流。')
  await page.evaluate(id => window.__askTransport.disconnect(id), id)
  await page.getByText(/流式连接在服务完成前中断/).waitFor()
  await page.waitForFunction(() => [...document.querySelectorAll('.ai-status')].at(-1)?.textContent === '回答失败')
  report.interactions.push('缺少 done 的断流被标为回答失败，而非假装完成')
  await page.close()
}

// 引用竞态：慢文件关闭后打开另一个文件，不能被旧请求覆盖
{
  const page = await newAppPage()
  const id = await send(page, '给我几个文件位置。')
  await page.evaluate(({ id, text }) => window.__askTransport.finish(id, text), { id, text: REF_ANSWER })
  await page.waitForFunction(() => document.querySelector('.ai-status')?.textContent === '已回复')
  await page.locator('.refs-toggle').click()
  const rows = page.locator('.ref-item')
  await rows.filter({ hasText: 'slow.ts' }).click()
  await page.locator('.fv-modal').waitFor()
  await page.keyboard.press('Escape')
  await page.waitForFunction(() => !document.querySelector('.fv-modal'))
  await rows.filter({ hasText: 'fast.ts' }).click()
  await page.locator('.fv-modal').waitFor()
  await page.waitForFunction(() => document.querySelector('.fv-path')?.textContent?.includes('fast.ts'))
  await page.waitForTimeout(400)
  assert((await page.locator('.fv-path').textContent()).includes('fast.ts'))
  assert(!(await page.locator('.fv-path').textContent()).includes('slow.ts'))
  await page.keyboard.press('Escape')
  report.interactions.push('慢请求返回后不覆盖后打开的文件')
  await page.close()
}

// 生产页面 390 与对比度
{
  const page = await newAppPage(390, 844)
  const id = await send(page, '移动端长回答。')
  await page.evaluate(({ id, text }) => window.__askTransport.finish(id, text), { id, text: APP_ANSWER })
  await page.waitForFunction(() => document.querySelector('.ai-status')?.textContent === '已回复')
  await page.evaluate(() => document.fonts.ready)
  const metrics = await page.evaluate(measure)
  report.contrast.push({ page: 'app', skin: 'zinc', width: 390, ...metrics })
  assert.equal(metrics.overflow, false)
  assert.equal(metrics.failures.length, 0, JSON.stringify(metrics.failures))
  await page.mouse.move(1, 1)
  await page.screenshot({ path: path.join(output, 'ask-conversation-after-app-390.png'), fullPage: true })
  await page.close()
}

assert.equal(report.errors.length, 0, JSON.stringify(report.errors))
assert.equal(report.writes.length, 0, `Unexpected writes: ${report.writes}`)
await fs.writeFile(path.join(output, 'ask-conversation-report.json'), JSON.stringify(report, null, 2))
console.log(JSON.stringify(report, null, 2))
await browser.close()
