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

// 引用上限那条用例要的素材：答案里点名的路径必须真的存在于夹具文件树
// （refsOf 会按文件树过滤掉幻觉路径），所以从 /api/fs/tree 现取，不写死 ——
// 夹具会被其它套件跑脏（多出一堆 regression_*/custom_* 文件），写死的路径说没就没。
const tree = await (await fetch(base + '/api/fs/tree')).json()
const FILES = []
;(function walk(node) {
  // 树是 nodes → 每层 children 的两套键名（顶层 nodes，子节点 children），
  // 且判文件要看 type === 'file'：用「有没有子节点」判会误收空目录。
  for (const c of node.children || node.nodes || []) {
    if (c.type === 'file' && c.path) FILES.push(c.path)
    walk(c)
  }
})(tree)
assert.ok(FILES.length >= 8, `夹具文件树只剩 ${FILES.length} 个文件，引用用例没法造截断`)
const REF_PATHS = FILES.slice(0, 8)
const REF_FRAMES = [{ type: 'token', text: REF_PATHS.map((p, i) => `- ${p}:${i + 1}`).join('\n\n') }]
const REF_TAIL = [{ type: 'done' }]

// 页面侧假 /api/chat：把 frames 全推下去后卡在闸门上不放行，Node 侧就能在
// 「流式进行中」这个状态里量 DOM；放行后才补完 tail 并关流。
const fakeChat = (frames, tail) => `(() => {
  const native = window.fetch.bind(window)
  const frame = o => 'data: ' + JSON.stringify(o) + '\\n\\n'
  window.__chatDone = false
  window.fetch = async (input, init) => {
    if (typeof input !== 'string' || !input.startsWith('/api/chat')) return native(input, init)
    const enc = new TextEncoder()
    const stream = new ReadableStream({
      async start(controller) {
        window.__held = false
        for (const f of ${JSON.stringify(frames)}) {
          controller.enqueue(enc.encode(frame(f)))
          await new Promise(r => setTimeout(r, 60))
        }
        // 闸门真的挂上了才算「流式中」：只等 DOM 会和这里抢时序（token 先渲染、
        // 下一拍才调用 __gate），Node 侧 finish() 就会拿到一个空的 resolver。
        window.__held = true
        await window.__gate()
        for (const f of ${JSON.stringify(tail)}) controller.enqueue(enc.encode(frame(f)))
        controller.close()
        window.__chatDone = true
      },
    })
    return new Response(stream, { headers: { 'content-type': 'text/event-stream' } })
  }
})()`

const DEFAULTS = { showTrace: true, reasonOpen: false, autoExpandTrace: true, maxRefs: 6, defaultThinking: false, defaultWeb: false }
// 引用截断用例专用：上限压到 3，保证 8 个真实引用一定被截（不依赖夹具文件数）
const PREFS_REF = { ...DEFAULTS, maxRefs: 3 }

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

async function boot(prefs, route = '/workbench', frames = FRAMES, tail = TAIL, extraInit = '') {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
  page.setDefaultTimeout(20000)
  await page.exposeFunction('__gate', () => new Promise(res => { release = res }))
  await page.addInitScript(`localStorage.setItem("docmind.askPrefs", ${JSON.stringify(JSON.stringify(prefs))})`)
  if (extraInit) await page.addInitScript(extraInit)
  await page.addInitScript(fakeChat(frames, tail))
  await page.goto(base + route, { waitUntil: 'domcontentloaded' })
  // 夹具是共享的，会被其它套件跑脏（几十个 custom_*/regression_* 目录），
  // 树首屏偶尔要 20 秒以上 —— 实测撞过一次，这里给足余量而不是让它变成假红。
  await page.waitForSelector(route === '/workbench' ? '.ft-row' : '.composer textarea', { timeout: 60000 })
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

/** 数引用：shown = 真实文件 chip 条数（不含 +N / 收起那颗），hidden = 「+N 处」里的 N。 */
const readRefs = (page, boxSel, chipSel, moreSel) => page.evaluate(([box, chip, more]) => {
  const el = document.querySelector(box)
  if (!el) return { shown: 0, moreLabel: null, hidden: 0 }
  const moreClass = more.split('.').pop()
  const chips = [...el.querySelectorAll(chip)].filter(x => !x.classList.contains(moreClass))
  const label = el.querySelector(more)?.textContent?.trim() ?? null
  const m = label ? /^\+(\d+) 处$/.exec(label) : null
  return { shown: chips.length, moreLabel: label, hidden: m ? Number(m[1]) : 0 }
}, [boxSel, chipSel, moreSel])

// 文字对比度：只看渲染结果（半透明底逐层合成），不看 CSS 里声明了什么。
// 引用 chip 以前用 var(--accent) 当文字色 —— 那是填色令牌，压在淡底上 paper 只有 2.71:1。
const textContrast = (page, sel) => page.evaluate((s) => {
  const lum = c => { const f = x => { x /= 255; return x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4 }
    return .2126 * f(c[0]) + .7152 * f(c[1]) + .0722 * f(c[2]) }
  const parse = v => {
    let m = v.match(/^color\(\s*srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+))?\s*\)/)
    if (m) return { rgb: [+m[1] * 255, +m[2] * 255, +m[3] * 255], a: m[4] === undefined ? 1 : +m[4] }
    m = v.match(/rgba?\(([^)]+)\)/)
    if (!m) return null
    const p = m[1].split(/[\s,\/]+/).filter(x => x !== '').map(Number)
    return { rgb: [p[0], p[1], p[2]], a: p[3] === undefined ? 1 : p[3] }
  }
  const el = document.querySelector(s)
  if (!el) return null
  const stack = []
  for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
    const c = parse(getComputedStyle(n).backgroundColor)
    if (c) { stack.push(c); if (c.a >= .999) break }
  }
  let bg = { rgb: [255, 255, 255], a: 1 }
  for (let i = stack.length - 1; i >= 0; i--) {
    const c = stack[i]
    bg = { rgb: [0, 1, 2].map(k => c.rgb[k] * c.a + bg.rgb[k] * (1 - c.a)), a: 1 }
  }
  const fg = parse(getComputedStyle(el).color)
  const eff = { rgb: [0, 1, 2].map(k => fg.rgb[k] * fg.a + bg.rgb[k] * (1 - fg.a)) }
  const L1 = lum(eff.rgb), L2 = lum(bg.rgb)
  return +(((Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05)).toFixed(2))
}, sel)

async function holdMidStream(page) {
  await openDockAndAsk(page, '伤害公式哪里和设计文档不一致？')
  await page.waitForSelector('.cd-activity-item', { timeout: 30000 })
  await page.waitForTimeout(500)
  return snapshot(page)
}

async function finish(page) {
  // 夹具是共享的，被其它套件跑脏后首屏和流式管线都可能慢到几十秒（实测撞过一次），
  // 这些等待给足余量，否则偶发超时会变成假红。
  await page.waitForFunction(() => window.__held === true, null, { timeout: 30000 })
  const r = release; release = null
  // 闸门还没被页面调用就 finish() 是用例写错了，直接说清楚，别留一个 10 秒超时
  assert.ok(r, 'finish() 时闸门还没被页面调用（发送没发生，或等错了选择器）')
  r()
  await page.waitForFunction(() => window.__chatDone === true, null, { timeout: 30000 })
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
  await page.waitForSelector('.cd-reason', { timeout: 30000 })
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

  // ⑤ 引用条数上限：截断必须数得出来、点得开（两页都是以前静默吞掉）
  page = await boot(PREFS_REF, '/workbench', REF_FRAMES, REF_TAIL)
  await openDockAndAsk(page, '把所有相关文件列出来')
  // 先等到「流式中」这个证据再放行：抢在页面调用 __gate 之前 finish() 会去 release
  // 一个还不存在的 resolver，闸门就永远卡不住。
  await page.waitForSelector('.cd-answer-live', { timeout: 30000 })
  await finish(page)
  const dockChip = await textContrast(page, '.cd-refs .cd-ref')
  const dockMore = await textContrast(page, '.cd-refs .cd-ref-more')
  let before = await readRefs(page, '.cd-refs', '.cd-ref', '.cd-ref-more')
  check('开发台引用超上限时露出「+N 处」，不是静默少几条', () => {
    assert.equal(before.shown, PREFS_REF.maxRefs, `默认应只显 ${PREFS_REF.maxRefs} 条，实际 ${before.shown}`)
    assert.match(before.moreLabel || '', /^\+\d+ 处$/, `没看到 +N 按钮：${JSON.stringify(before.moreLabel)}`)
    assert.ok(before.hidden >= 1, `+N 里的 N 应当 ≥1，读到 ${before.moreLabel}`)
  })
  check('开发台引用 chip 文字达 AA（曾经用填色令牌当文字色）', () => {
    assert.ok(dockChip >= 4.5, `引用 chip 文字对比度只有 ${dockChip}:1`)
    assert.ok(dockMore >= 4.5, `+N 按钮文字对比度只有 ${dockMore}:1`)
  })
  await page.click('.cd-refs .cd-ref-more')
  let after = await readRefs(page, '.cd-refs', '.cd-ref', '.cd-ref-more')
  check('点开「+N 处」补齐剩下的，并给出收起入口', () => {
    assert.equal(after.shown, PREFS_REF.maxRefs + before.hidden,
      `展开后应 ${PREFS_REF.maxRefs + before.hidden} 条，实际 ${after.shown}`)
    assert.equal(after.moreLabel, '收起', '展开后没有收起入口')
  })
  await page.close()

  page = await boot(PREFS_REF, '/', REF_FRAMES, REF_TAIL)
  await page.locator('.composer textarea').fill('把所有相关文件列出来')
  await page.click('button.send-btn')
  await page.waitForSelector('.refs', { timeout: 30000 })
  await page.waitForTimeout(400)
  before = await readRefs(page, '.refs', '.ref-chip', '.ref-more')
  const askChip = await textContrast(page, '.refs .ref-chip')
  const askMore = await textContrast(page, '.refs .ref-more')
  check('问答页同样露出 +N 并可展开', () => {
    assert.equal(before.shown, PREFS_REF.maxRefs, `默认应只显 ${PREFS_REF.maxRefs} 条，实际 ${before.shown}`)
    assert.match(before.moreLabel || '', /^\+\d+ 处$/, `问答页没看到 +N：${JSON.stringify(before.moreLabel)}`)
  })
  check('问答页引用 chip 文字达 AA', () => {
    assert.ok(askChip >= 4.5, `引用 chip 文字对比度只有 ${askChip}:1`)
    assert.ok(askMore >= 4.5, `+N 按钮文字对比度只有 ${askMore}:1`)
  })
  await page.click('.refs .ref-more')
  after = await readRefs(page, '.refs', '.ref-chip', '.ref-more')
  check('问答页展开后条数对得上', () => {
    assert.equal(after.shown, PREFS_REF.maxRefs + before.hidden,
      `展开后应 ${PREFS_REF.maxRefs + before.hidden} 条，实际 ${after.shown}`)
  })
  await page.close()

  // ⑥ 语音能力缺失时不能只是让按钮消失：要说出缺的是哪一条通路
  const NO_SPEECH = `(() => {
    delete window.SpeechRecognition
    delete window.webkitSpeechRecognition
    const native = window.fetch.bind(window)
    window.fetch = (input, init) => (typeof input === 'string' && input.startsWith('/api/voice/status'))
      ? Promise.resolve(new Response(JSON.stringify({ browser_fallback: false, stt_configured: false, tts_configured: false }),
          { headers: { 'content-type': 'application/json' } }))
      : native(input, init)
  })()`
  page = await boot(DEFAULTS, '/workbench', FRAMES, TAIL, NO_SPEECH)
  // 切到开发舱用工作区快捷键 Alt+4（App.vue 的 setWorkspace('cockpit')），
  // 比按文案点标签稳 —— 「开发舱」三个字在界面上出现多处。裸按 4 不生效，
  // onWorkspaceHotkey 第一行就要求 e.altKey。
  await page.click('.wb-brand')
  await page.keyboard.press('Alt+4')
  await page.waitForSelector('#wb-cockpit-chat-slot .cd-inputbar', { timeout: 30000 })
  const voice = await page.evaluate(() => {
    const btn = [...document.querySelectorAll('#wb-cockpit-chat-slot .cd-attach')]
      .find(b => (b.textContent || '').includes('语音'))
    return btn ? { text: btn.textContent.trim(), disabled: btn.disabled, title: btn.title } : null
  })
  check('开发舱里语音不可用时给出禁用按钮 + 点名缺哪条通路（不再静默消失）', () => {
    assert.ok(voice, '语音按钮整个不见了 —— 用户无从知道是坏了还是这里本来没有')
    assert.equal(voice.disabled, true, '不可用的语音按钮必须是禁用态')
    assert.match(voice.text, /语音不可用/, `按钮文案应说明状态，读到 ${JSON.stringify(voice.text)}`)
    assert.ok(voice.title.includes('SpeechRecognition'), `tooltip 没点名浏览器通路：${voice.title}`)
    assert.ok(voice.title.includes('STT'), `tooltip 没点名后端通路：${voice.title}`)
  })
  await page.close()
} finally {
  await browser.close()
}
console.log(`\n${failed ? failed + ' 项失败' : '全部通过'}：对话展示层 15 项断言`)
process.exit(failed ? 1 : 0)
