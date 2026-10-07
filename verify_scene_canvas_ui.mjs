/**
 * 场景画布 UI 冒烟：用真实浏览器把「试玩器 → 场景画布」这条链路走一遍。
 *
 * 前置：
 *   1) 另开一个终端跑 `\.venv\Scripts\python.exe verify_scene_canvas.py --serve 8011`
 *      （它会造一个临时 Godot 工程并把 code_root 指过去，不污染你的日常实例）
 *   2) 安装 playwright-core（不下载浏览器，复用系统 Edge/Chrome）：
 *        cd %USERPROFILE%\.workbuddy\binaries\node\workspace && npm install playwright-core
 *
 * 运行（用托管 node）：
 *   set NODE_PATH=%USERPROFILE%\.workbuddy\binaries\node\workspace\node_modules
 *   node verify_scene_canvas_ui.mjs [http://127.0.0.1:8011]
 *
 * 为什么要有它：后端 verify_scene_canvas.py 打的是 HTTP 契约，量不到"画布到底画出来没有"。
 * 节点数、边数、撤销后 DOM 是否回退，这些只有真浏览器跑得出来。
 */
import { createRequire } from 'node:module'

// ESM 不认 NODE_PATH，而 playwright-core 是装在托管 node 工作区里的，
// 所以用 createRequire 走 CommonJS 解析（它认 NODE_PATH），再兜一个显式路径。
const require = createRequire(import.meta.url)
function loadPlaywright() {
  const candidates = [
    'playwright-core',
    `${process.env.USERPROFILE}\\.workbuddy\\binaries\\node\\workspace\\node_modules\\playwright-core`,
  ]
  for (const name of candidates) {
    try {
      return require(name)
    } catch {
      /* 试下一个 */
    }
  }
  throw new Error('未找到 playwright-core：请在 %USERPROFILE%\\.workbuddy\\binaries\\node\\workspace 下 npm install playwright-core')
}
const { chromium } = loadPlaywright()

const BASE = process.argv[2] || 'http://127.0.0.1:8011'
const SCENE = 'scenes/Main.tscn'
const pass = []
const fail = []

function check(label, ok, detail = '') {
  ;(ok ? pass : fail).push(label)
  console.log(`${ok ? '  ok  ' : '  FAIL'} ${label}${detail ? '   ' + detail : ''}`)
}

async function launch(browserType) {
  const candidates = [
    { channel: 'msedge' },
    { channel: 'chrome' },
    { executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' },
    { executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' },
  ]
  for (const option of candidates) {
    try {
      return await browserType.launch({ headless: true, ...option })
    } catch {
      /* 试下一个 */
    }
  }
  throw new Error('找不到可用的 Chromium 系浏览器（Edge/Chrome），无法做 UI 冒烟')
}

const counts = (page) => page.evaluate(() => ({
  sceneNodes: document.querySelectorAll('.vue-flow__node-sceneNode').length,
  fileCards: document.querySelectorAll('.vue-flow__node-sceneFile').length,
  edges: document.querySelectorAll('.vue-flow__edge').length,
  names: [...document.querySelectorAll('.vue-flow__node-sceneNode .sc-node-name')].map((n) => n.textContent),
  message: document.querySelector('.sc-msg')?.textContent ?? '',
}))

const browser = await launch(chromium)
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
  const errors = []
  page.on('pageerror', (e) => errors.push(String(e)))
  // 本机访问 fonts.googleapis.com 会间歇性 ERR_CONNECTION_RESET，那是沙箱网络
  // 状况而不是应用缺陷。资源加载失败的 console 消息带 location().url，按来源判定：
  // 同源失败照报，第三方 CDN 才忽略；拿不到 URL 时保守计入。
  const sameOrigin = u => { try { return new URL(u).origin === new URL(BASE).origin } catch { return true } }
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    const text = m.text()
    if (/Failed to load resource/.test(text)) {
      const u = m.location()?.url || ''
      if (!u || sameOrigin(u)) errors.push(`${text} ${u}`)
      return
    }
    errors.push(text)
  })
  page.on('requestfailed', (r) => { if (sameOrigin(r.url())) errors.push(`FAILED ${r.url()}`) })
  page.on('response', (r) => { if (r.status() >= 400 && sameOrigin(r.url())) errors.push(`HTTP ${r.status()} ${r.url()}`) })

  await page.goto(`${BASE}/workbench`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.wb-shell', { timeout: 20000 })
  check('工作台外壳渲染', true)

  // 运行游戏触发钮由 SceneRuntimePanel teleport 进顶栏「更多」菜单槽位，
  // 菜单收起时在 DOM 里但不可见，必须先开菜单再点槽内那一个。
  await page.click('.wb-menu-trigger')
  await page.click('.wb-menu-slot-item .sr-trigger')
  await page.waitForSelector('.pb-pop', { timeout: 10000 })
  check('试玩器弹出', true)

  // 试玩 tab：原生引擎的「嵌入工作台」开关在浏览器模式下必须降级（禁用 + 提示），
  // 而不是让用户点了之后拿到一个含糊的失败。
  const embedUi = await page.evaluate(() => ({
    toggle: !!document.querySelector('.pb-check input'),
    disabled: document.querySelector('.pb-check input')?.disabled ?? null,
    checked: document.querySelector('.pb-check input')?.checked ?? null,
    hint: document.querySelector('.pb-hintline')?.textContent?.trim() ?? '',
    hasNativeBtn: [...document.querySelectorAll('.pb-btn')].some((b) => b.textContent.includes('桌面窗口启动')),
  }))
  check('试玩 tab 有「嵌入工作台」开关', embedUi.toggle === true, embedUi)
  check('浏览器模式下开关被禁用（原生窗口嵌不进浏览器）', embedUi.disabled === true, embedUi.disabled)
  check('浏览器模式下给出降级提示', embedUi.hint.includes('浏览器模式'), embedUi.hint.slice(0, 40))
  check('「桌面窗口启动」按钮仍在（独立窗口可用）', embedUi.hasNativeBtn === true)

  await page.click('.pb-tabs button:has-text("场景画布")')
  await page.waitForSelector('input.pb-path', { timeout: 10000 })
  check('存在「场景画布」tab', true)

  await page.fill('input.pb-path', SCENE)
  await page.click('button.pb-btn.primary:has-text("加载场景")')
  await page.waitForSelector('.vue-flow__node-sceneNode', { timeout: 20000 })

  let state = await counts(page)
  check('场景节点全部画出（6）', state.sceneNodes === 6, `实际 ${state.sceneNodes}`)
  // 默认只画脚本卡 + 被实例化场景卡；资源引用（贴图等）要显式打开，避免真实工程里
  // 上百个 ext_resource 把场景层级淹没
  check('外部引用卡默认画出脚本+实例化（3）', state.fileCards === 3, `实际 ${state.fileCards}`)
  check('三类边共 8 条', state.edges === 8, `实际 ${state.edges}`)
  check('节点名正确', ['Main', 'Player', 'Sprite', 'Gun', 'Enemy', 'Bullet']
    .every((n) => state.names.includes(n)), state.names.join(','))
  check('加载提示正确', state.message.includes('已加载 6 节点'), state.message)

  // 层级树 + 选中检查器
  await page.click('.vue-flow__node-sceneNode:has-text("Player") .sc-node')
  await page.waitForSelector('.sc-side-head b', { timeout: 5000 })
  const inspector = await page.evaluate(() => ({
    title: document.querySelector('.sc-side-head b')?.textContent,
    props: [...document.querySelectorAll('.sc-prop-name')].map((n) => n.textContent),
    props2: [...document.querySelectorAll('.sc-prop input')].map((n) => n.value),
  }))
  check('点节点后检查器显示该节点', inspector.title === 'Player', inspector.title)
  check('检查器列出 position/script', inspector.props.includes('position') && inspector.props.includes('script'),
    inspector.props.join(','))
  check('position 值正确', inspector.props2.includes('Vector2(320, 240)'), inspector.props2.join('|'))

  // 新增子节点（用明确的定位器：:has(h4:text()) 这种组合在 Playwright 里不稳）
  const addSection = page.locator('.sc-sec', { has: page.locator('h4', { hasText: '新增子节点' }) })
  await addSection.locator('input').nth(0).fill('Area2D')
  await addSection.locator('input').nth(1).fill('UITest')
  await addSection.locator('button').click()
  try {
    await page.waitForFunction(
      () => [...document.querySelectorAll('.sc-node-name')].some((n) => n.textContent === 'UITest'),
      null, { timeout: 15000 },
    )
  } catch (e) {
    const dump = await page.evaluate(() => ({
      message: document.querySelector('.sc-msg')?.textContent ?? '(无)',
      inputs: [...document.querySelectorAll('.sc-side input')].map((n) => n.value),
      names: [...document.querySelectorAll('.sc-node-name')].map((n) => n.textContent),
    }))
    check('新增子节点后画布出现新卡', false, JSON.stringify(dump))
    throw e
  }
  state = await counts(page)
  check('新增子节点后画布出现新卡', state.sceneNodes === 7, `实际 ${state.sceneNodes}`)

  // 撤销
  await page.click('button.sc-btn:has-text("撤销")')
  await page.waitForFunction(() => document.querySelectorAll('.vue-flow__node-sceneNode').length === 6,
    null, { timeout: 15000 })
  state = await counts(page)
  check('撤销后画布回到 6 个节点', state.sceneNodes === 6, `实际 ${state.sceneNodes}`)

  // 重做
  await page.click('button.sc-btn:has-text("重做")')
  await page.waitForFunction(() => document.querySelectorAll('.vue-flow__node-sceneNode').length === 7,
    null, { timeout: 15000 })
  check('重做后回到 7 个节点', (await counts(page)).sceneNodes === 7)
  await page.click('button.sc-btn:has-text("撤销")')
  await page.waitForFunction(() => document.querySelectorAll('.vue-flow__node-sceneNode').length === 6,
    null, { timeout: 15000 })

  // 空间布局：按场景坐标落点
  await page.click('.sc-seg button:has-text("空间布局")')
  await page.waitForTimeout(600)
  const spatial = await page.evaluate(() => {
    const target = [...document.querySelectorAll('.vue-flow__node-sceneNode')]
      .find((el) => el.textContent.includes('Enemy'))
    const player = [...document.querySelectorAll('.vue-flow__node-sceneNode')]
      .find((el) => el.textContent.includes('Player'))
    const box = (el) => {
      const r = el.querySelector('.sc-node').getBoundingClientRect()
      return { x: Math.round(r.x), y: Math.round(r.y) }
    }
    return { enemy: box(target), player: box(player) }
  })
  // 场景坐标：Player(320,240)、Enemy(760,180) —— Godot 与画布 Y 轴同向（都向下），
  // 所以 Enemy 既在 Player 右边、也应该在它上面
  check('空间布局按场景坐标排布（Enemy 在 Player 右上方）',
    spatial.enemy.x > spatial.player.x && spatial.enemy.y < spatial.player.y,
    JSON.stringify(spatial))
  check('空间布局落点比例与场景一致',
    Math.abs((spatial.enemy.x - spatial.player.x) / (760 - 320) - (spatial.player.y - spatial.enemy.y) / (240 - 180)) < 0.08,
    JSON.stringify(spatial))

  await page.screenshot({ path: 'docs/screenshots/scene-canvas.png' })

  // 资源引用开关
  await page.check('input[type=checkbox] >> nth=2')
  await page.waitForTimeout(400)
  check('打开「资源引用」后多出贴图卡', (await counts(page)).fileCards === 4,
    `实际 ${(await counts(page)).fileCards}`)
  await page.uncheck('input[type=checkbox] >> nth=2')
  await page.waitForTimeout(300)

  // 边开关
  const beforeHide = (await counts(page)).edges
  await page.uncheck('input[type=checkbox] >> nth=1')
  await page.waitForTimeout(400)
  check('关掉「实例化边」后边数减少', (await counts(page)).edges < beforeHide,
    `${beforeHide} -> ${(await counts(page)).edges}`)
  await page.check('input[type=checkbox] >> nth=1')

  // 运行时时间线 tab
  // 先清空：否则上一次跑的事件会累积，事件点数量断言就不稳
  await page.evaluate(async (base) => {
    await fetch(`${base}/api/runtime/clear`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope: 'all' }),
    })
  }, BASE)
  await page.evaluate(async (base) => {
    await fetch(`${base}/api/runtime/events`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        events: [
          { type: 'damage', data: { hp: 80, session: 'ui' } },
          { type: 'heal', data: { hp: 100, session: 'ui' } },
          { type: 'death', data: { hp: 0, session: 'ui' } },
        ],
      }),
    })
  }, BASE)
  await page.click('.pb-tabs button:has-text("运行时时间线")')
  await page.waitForSelector('.rt-wrap', { timeout: 15000 })
  await page.click('button.rt-btn:has-text("刷新")')
  await page.waitForSelector('.rt-mark', { timeout: 15000 })
  const timeline = await page.evaluate(() => ({
    tracks: [...document.querySelectorAll('.rt-label b')].map((n) => n.textContent),
    marks: document.querySelectorAll('.rt-mark').length,
    pills: [...document.querySelectorAll('.rt-pill')].map((n) => n.textContent.trim()),
  }))
  check('时间线渲染出三条轨道', ['damage', 'heal', 'death'].every((t) => timeline.tracks.includes(t)),
    timeline.tracks.join(','))
  check('时间线画出事件点', timeline.marks === 3, `实际 ${timeline.marks}`)
  check('类型筛选 pill 带计数', timeline.pills.some((p) => p.startsWith('damage')), timeline.pills.join('|'))

  await page.click('.rt-pill:has-text("death")')
  await page.waitForTimeout(400)
  const filtered = await page.evaluate(() => document.querySelectorAll('.rt-mark').length)
  check('按类型筛选后只剩 death', filtered === 1, `实际 ${filtered}`)

  await page.screenshot({ path: 'docs/screenshots/runtime-timeline.png' })
  console.log('\n截图：docs/screenshots/scene-canvas.png 与 docs/screenshots/runtime-timeline.png')

  check('无 JS 运行时错误与失败请求', errors.length === 0, errors.join(' | '))
} finally {
  await browser.close()
}

console.log('\n' + '='.repeat(46))
console.log(`通过 ${pass.length} 项，失败 ${fail.length} 项`)
fail.forEach((f) => console.log('  - ' + f))
process.exit(fail.length ? 1 : 0)
