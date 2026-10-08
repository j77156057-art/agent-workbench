// 对话展示层的行为验收：解说截断豁免、流式中整轮摊开、思考面板自动展开、开关真的生效。
//
// 为什么需要单独一个脚本：这四件事只在「流式进行中」这个瞬时状态成立，而像素基线
// 抓的是结束态的夹具界面（对话台里根本没有消息），所以基线看不见这里的回归。
// 这里用假 SSE 把流卡在中途，逐项量 DOM —— 包括「摘要行实际排了几行」这种
// 只有渲染出来才知道的事实。
//
// 需要夹具：verify_scene_canvas.py --serve 8011（或设 DOCMIND_UI_TEST_URL）。不发真实 LLM 请求。
import { createRequire } from 'node:module'
import path from 'node:path'
import assert from 'node:assert/strict'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE, '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))
const base = process.env.DOCMIND_UI_TEST_URL || 'http://127.0.0.1:8011'

// 用户给的实测口径：每轮解说 71–165 字。两条都造出来，短的用来确认「本来就没被砍」，
// 长的用来确认「不再只剩开头」。纯中文（不含空白）以免和 traceSummary 的空白归一化打架。
const SENT = '先读状态机目录，再对照数值表确认伤害公式每一项来源，最后把不一致的地方列成清单交给用户确认，未确认前不动脚本文件。'
const ALL = SENT.repeat(6)
const LONG = ALL.slice(0, 165)
const SHORT = ALL.slice(0, 71)
const REASON = '先确认问题指向的是数值实现还是表现层，再决定检索范围；这里要排除缓存与热重载带来的干扰。'

const N_LONG = [...LONG].length
const N_SHORT = [...SHORT].length
assert.ok(N_LONG >= 160 && N_LONG <= 170, `LONG 必须落在实测区间上沿，实际 ${N_LONG} 字`)
assert.ok(N_SHORT >= 65 && N_SHORT <= 80, `SHORT 实际 ${N_SHORT} 字`)

const FRAMES = [
  { type: 'reasoning', text: REASON },
  { type: 'thought', text: LONG },
  { type: 'reflection', text: SHORT },
  // 卡住闸门之前先给一个 token：流式中的答案区靠 .cd-answer-live 标记，
  // 没有正文就没有这个节点，「仍在流式中」这条断言会假失败。
  { type: 'token', text: '初步结论：' },
]
const TAIL = [{ type: 'token', text: '两处不一致。' }, { type: 'done' }]

// 页面侧假 /api/chat：把 FRAMES 全推下去后卡在闸门上不放行，Node 侧就能在
// 「流式进行中」这个状态里量 DOM；放行后才补完答案并关流。
const fakeChat = () => `(() => {
  const native = window.fetch.bind(window)
  const frame = o => 'data: ' + JSON.stringify(o) + '\\n\\n'
  window.__chatDone = false
  window.fetch = async (input, init) => {
    if (typeof input !== 'string' || !input.startsWith('/api/chat')) return native(input, init)
    const enc = new TextEncoder()
    const stream = new ReadableStream({
      async start(controller) {
        for (const f of ${JSON.stringify(FRAMES)}) {
          controller.enqueue(enc.encode(frame(f)))
          await new Promise(r => setTimeout(r, 60))
        }
        await window.__gate()
        for (const f of ${JSON.stringify(TAIL)}) controller.enqueue(enc.encode(frame(f)))
        controller.close()
        window.__chatDone = true
      },
    })
    return new Response(stream, { headers: { 'content-type': 'text/event-stream' } })
  }
})()`

const DEFAULTS = { showTrace: true, reasonOpen: false, autoExpandTrace: true, maxRefs: 6, defaultThinking: false, defaultWeb: false }

let release = null
const browser = await chromium.launch({ channel: 'msedge', headless: true })
let failed = 0

function check(name, fn) {
  try { fn(); console.log(`PASS   ${name}`) }
  catch (e) { failed++; console.log(`FAIL   ${name}\n      ${String(e.message).split('\n')[0]}`) }
}

async function openDockAndAsk(page, question) {
  await page.click('button.wb-question-link[title*="AI 对话"]')
  const box = page.locator('.cd-dock textarea').first()
  await box.waitFor({ timeout: 15000 })
  await box.fill(question)
  // 对话台是 Ctrl+Enter 发送（回车只换行），点发送按钮最稳
  await page.click('button.cd-send')
}

async function boot(prefs) {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
  page.setDefaultTimeout(20000)
  await page.exposeFunction('__gate', () => new Promise(res => { release = res }))
  await page.addInitScript(`localStorage.setItem("docmind.askPrefs", ${JSON.stringify(JSON.stringify(prefs))})`)
  await page.addInitScript(fakeChat())
  await page.goto(base + '/workbench', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.ft-row')
  return page
}

const snapshot = page => page.evaluate(() => {
  const rows = [...document.querySelectorAll('.cd-activity-item')]
  const lh = el => parseFloat(getComputedStyle(el).lineHeight) || 18
  return {
    rows: rows.length,
    openDetails: document.querySelectorAll('.cd-activity-detail').length,
    summaries: rows.map(r => {
      const el = r.querySelector('.cd-activity-summary')
      if (!el) return { text: '', lines: 0 }
      return { text: (el.textContent ?? '').trim(), lines: +(el.getBoundingClientRect().height / lh(el)).toFixed(2) }
    }),
    reasonBody: document.querySelector('.cd-reason-body')?.textContent?.trim() ?? null,
    traceBlock: !!document.querySelector('.cd-activity'),
    streaming: !!document.querySelector('.cd-answer-live'),
  }
})

async function holdMidStream(page) {
  await openDockAndAsk(page, '伤害公式哪里和设计文档不一致？')
  await page.waitForSelector('.cd-activity-item', { timeout: 15000 })
  await page.waitForTimeout(500)
  return snapshot(page)
}

async function finish(page) {
  const r = release; release = null
  r?.()
  await page.waitForFunction(() => window.__chatDone === true, null, { timeout: 10000 })
  await page.waitForTimeout(400)
  return snapshot(page)
}
/** 回合结束后整条时间线折在「本轮任务动作」段头后面，要看摘要得先把段展开。 */
async function openSegmentAndSnapshot(page) {
  await page.click('.cd-activity-segment')
  await page.waitForTimeout(300)
  return snapshot(page)
}

try {
  // ① 默认偏好：流式中整轮摊开 + 思考面板自动展开
  let page = await boot(DEFAULTS)
  let s = await holdMidStream(page)
  check('流式中：本轮所有步骤都摊开（旧行为只展开最新一条）', () => {
    assert.equal(s.streaming, true, '没抓到流式进行中的证据（.cd-answer-live）')
    // 正对照：偏好开着时时间线必须在（③ 里关掉后必须不在，两条一起才说明是偏好在起作用）
    assert.equal(s.traceBlock, true, 'showTrace 开着时时间线却没渲染，③ 的断言就失去意义')
    assert.equal(s.rows, 2, `步骤行数应为 2，实际 ${s.rows}`)
    assert.equal(s.openDetails, 2, `展开数应为 2，实际 ${s.openDetails}`)
  })
  check('思考过程在流式中自动展开（不再藏在折叠后面）', () => {
    assert.ok(s.reasonBody?.includes('先确认问题指向'), `思考正文没自动展开：${JSON.stringify(s.reasonBody?.slice(0, 20))}`)
  })
  s = await finish(page)
  check('回合结束回落：步骤不再自动摊开、思考按偏好折叠、时间线折回段头', () => {
    assert.equal(s.rows, 0, `结束后时间线仍摊着（${s.rows} 行）`)
    assert.equal(s.openDetails, 0, `结束后仍有 ${s.openDetails} 条展开`)
    assert.equal(s.reasonBody, null, '结束后思考面板仍开着（偏好为关）')
  })
  s = await openSegmentAndSnapshot(page)
  check('解说 165 字豁免生效：摘要行里是全文，没有省略号', () => {
    const long = s.summaries.find(x => x.text.length > 100)
    assert.ok(long, `没有找到长摘要行：${JSON.stringify(s.summaries.map(x => x.text.length))}`)
    assert.equal(long.text, LONG)
    assert.ok(s.summaries.some(x => x.text === SHORT), '71 字那条没有完整出现')
  })
  check('摘要行真的排成两行（CSS clamp 生效，不是 DOM 里有但看不见）', () => {
    const long = s.summaries.find(x => x.text.length > 100)
    assert.ok(long.lines >= 2, `长摘要只占了 ${long.lines} 行`)
    assert.ok(long.lines <= 3, `长摘要占了 ${long.lines} 行，两行 clamp 没生效`)
  })
  await page.close()

  // ② 关掉「整轮摊开」
  page = await boot({ ...DEFAULTS, autoExpandTrace: false })
  s = await holdMidStream(page)
  check('开关「流式中整轮摊开」关掉后确实不摊开（不是装饰）', () => {
    assert.equal(s.rows, 2, `步骤行数应为 2，实际 ${s.rows}`)
    assert.equal(s.openDetails, 0, `关掉后仍有 ${s.openDetails} 条自动展开`)
  })
  await finish(page)
  await page.close()

  // ③ 关掉「显示推理轨迹」——这条偏好此前对开发台完全无效
  page = await boot({ ...DEFAULTS, showTrace: false })
  await openDockAndAsk(page, '再来一轮')
  await page.waitForSelector('.cd-reason', { timeout: 15000 })
  await page.waitForTimeout(400)
  s = await snapshot(page)
  check('开关「显示推理轨迹步骤」在开发台真的生效（此前是假设置）', () => {
    assert.equal(s.traceBlock, false, '关掉后时间线仍在渲染')
    assert.ok(s.reasonBody, '轨迹关掉时思考面板应仍可用')
  })
  await finish(page)
  await page.close()

  // ④ 手动收起要能盖过自动展开（单一「展开」集合会点了没反应）
  page = await boot(DEFAULTS)
  s = await holdMidStream(page)
  await page.locator('.cd-activity-item .cd-activity-row').first().click()
  s = await snapshot(page)
  check('流式中手动收起一条：真收起来了，没被自动规则顶回去', () => {
    assert.equal(s.openDetails, 1, `收起后展开数应为 1，实际 ${s.openDetails}`)
    assert.equal(s.summaries[0].text, LONG, '收起的那条应回落到完整摘要行')
  })
  await finish(page)
  await page.close()
} finally {
  await browser.close()
}
console.log(`\n${failed ? failed + ' 项失败' : '全部通过'}：对话展示层 8 项断言`)
process.exit(failed ? 1 : 0)
