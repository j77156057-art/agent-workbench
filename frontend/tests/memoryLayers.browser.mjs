// 记忆神经网络「store 语义分层 + 真实边」的真机回归（msedge headless）。
//
// 2026-11-04：环从“按重要度切三档”改成按 store 真实语义分层（rule/shared/legacy/
// project/hook），邻接从“同环相邻 / 内环同位”的合成边换成后端 edges 表的真实边。这个脚本钉住四件事：
//   1) 每个 store 的节点带预期 layer_key；四层环都在（数量可少，但不许伪造）；
//   2) 亮度必须用 canvas 取像素算相对亮度：rule>shared>legacy>project>hook 单调、注入仍比同层亮；
//   3) 图上每一条非注入边都能在 /api/agent/memory/inspect 的 edges 里找到，
//      且条数一致 —— 没有合成边；关系覆盖 N/总数 与图口径一致；
//   4) 无边节点显示“无关联边”，拖拽波及 0 个邻域；edges 为空时图仍可渲染。
//
// 用法（服务先起在 127.0.0.1:8000）：
//   node frontend/tests/memoryLayers.browser.mjs
import { createRequire } from 'node:module'
import fs from 'node:fs'
import path from 'node:path'

/* global process, console, document, getComputedStyle, window, fetch */

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE || process.env.HOME,
    '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))

const BASE = process.env.DOCMIND_BASE || 'http://127.0.0.1:8000'
// 真实快照最丰富的会话（injected 9：8 legacy + 1 shared；hook 在本会话无条目）。
// 首个 id 是 2026-11-04 真跑一轮 chat 后产生的快照；后面的 id 依次兜底。
const SESSION_RICH = process.env.DOCMIND_LAYER_SESSION
  ? [process.env.DOCMIND_LAYER_SESSION]
  : ['web-c354e15cc0e51b973887649b', 'web-5e3eecbb21b3423ba8d03267',
    'web-3f5b14cf6b814fa7897af79a', 'web-fbe442ffa59940199d73233e']
// 有会话钩子的会话（hook 环有真实节点）
const SESSION_HOOK = process.env.DOCMIND_HOOK_SESSION
  ? [process.env.DOCMIND_HOOK_SESSION]
  : ['web-32d380b1f11248139cc6b7da', 'web-07910a35553148ea858652f2',
    'web-e3cbd52264134b2db56d20ba', 'web-5a77280d534b4897a96e9d05']
const OUT = path.resolve(process.cwd(), '.screenshots/store-layers-20261104')

const results = []
const inspectUrls = []
function check(name, pass, detail = '') {
  results.push({ name, pass: !!pass, detail: String(detail).slice(0, 300) })
}

async function payload(session, project = '') {
  const url = `${BASE}/api/agent/memory/inspect?session_id=${encodeURIComponent(session)}&agent_id=main&soft=1`
  // 必须与页面发同一项目头：后端按 (session, project) 找活着的 agent 快照，
  // 头不一致会落到另一个 agent 上，看起来像“没有注入”。
  const res = await fetch(url, { headers: project ? { 'X-DocMind-Project': project } : {} })
  return res.json()
}

/**
 * 用真实会话打开记忆总览。
 *
 * 为什么不在挂载前写 sessionStorage：App 挂载时项目同步会自己 rotate 一次会话
 * （工作台的「切项目就换会话」规则），预写入的 id 会被它覆盖。所以先等挂载期的
 * 轮换停下来（连续 1.8s 不再变），再用 DocMindSession.set（acquire 不 rotate）
 * 落到目标会话，最后才打开面板——面板的 :session-id 在渲染时读的就是这个 id。
 */
async function openOverview(page, sessions, { strict = true } = {}) {
  const candidates = Array.isArray(sessions) ? sessions : [sessions]
  await page.goto(BASE + '/workbench.html', { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => window.DocMindSession && window.DocMindSession.ready, null, { timeout: 20000 })
  await page.waitForSelector('button[aria-label="上下文记忆"]', { timeout: 20000 })
  await page.evaluate(async () => {
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
    let last = window.DocMindSession.get()
    for (let stable = 0; stable < 12;) {
      await sleep(150)
      const now = window.DocMindSession.get()
      if (now === last) stable += 1
      else { last = now; stable = 0 }
    }
  })
  let chosen = ''
  for (const id of candidates) {
    const ok = await page.evaluate(value => window.DocMindSession.set(value), id)
    if (!ok) continue
    await page.waitForTimeout(250)
    if (await page.evaluate(() => window.DocMindSession.get()) === id) { chosen = id; break }
  }
  if (strict && !chosen) throw new Error(`没有可用的真实会话（都被占用）：${candidates.join(',')}`)
  const inspect = page.waitForResponse(
    resp => resp.url().includes('/memory/inspect') && (!chosen || resp.url().includes(chosen)),
    { timeout: 20000 },
  ).catch(() => null)
  await page.click('button[aria-label="上下文记忆"]')
  await page.waitForSelector('[data-memory-panel] .mo-stable-graph', { timeout: 25000 })
  await page.waitForSelector('[data-memory-panel] .mo-node', { timeout: 25000 })
  await inspect
  // 呼吸动画会改变 opacity；截图/取像素前关掉动画，避免同一节点在不同相位量出不同值。
  // 注意不能用 prefers-reduced-motion：那会同时关掉 BFS 波前与拖拽邻域。
  await page.addStyleTag({ content: '[data-memory-panel] .mo-node.used .mo-neuron-core { animation: none !important; }' })
  await page.waitForTimeout(1800)
  return chosen
}

// 用 canvas 把“背景 + 节点填充色 @ opacity”合成为 1px，再算 sRGB 相对亮度。
const measure = () => {
  const wrap = document.querySelector('[data-memory-panel] .mo-graph-wrap')
  const canvas = document.createElement('canvas')
  canvas.width = 1; canvas.height = 1
  const ctx = canvas.getContext('2d', { willReadFrequently: true })
  const lin = v => { const x = v / 255; return x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4 }
  const luminance = (fill, opacity) => {
    const bg = getComputedStyle(wrap).backgroundColor
    ctx.clearRect(0, 0, 1, 1)
    ctx.fillStyle = bg
    ctx.fillRect(0, 0, 1, 1)
    ctx.globalAlpha = Math.max(0, Math.min(1, opacity))
    ctx.fillStyle = fill
    ctx.fillRect(0, 0, 1, 1)
    const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
  }
  const rows = []
  for (const node of document.querySelectorAll('[data-memory-panel] .mo-node')) {
    const core = node.querySelector('.mo-neuron-core')
    const used = node.classList.contains('used')
    rows.push({
      key: node.getAttribute('data-node-key'), store: node.getAttribute('data-store'),
      layer: node.getAttribute('data-layer-key'), layerLabel: node.getAttribute('data-layer-label'), used,
      importance: Number(node.getAttribute('data-importance')), depth: Number(node.getAttribute('data-depth')),
      edgeCount: Number(node.getAttribute('data-edge-count')),
      opacity: Number(getComputedStyle(core).opacity), fill: getComputedStyle(core).fill,
      luminance: luminance(getComputedStyle(core).fill, Number(getComputedStyle(core).opacity)),
    })
  }
  const hubCore = document.querySelector('[data-memory-panel] .mo-hub-core')
  const svg = wrap.querySelector('.mo-stable-graph')
  rows.push({
    key: 'core', store: 'system', layer: document.querySelector('.mo-hub')?.getAttribute('data-hub-layer-key') || 'rule',
    layerLabel: document.querySelector('.mo-hub')?.getAttribute('data-hub-layer-label') || '常驻守则',
    used: true, importance: 1, depth: 0, edgeCount: 0,
    opacity: Number(getComputedStyle(hubCore).opacity), fill: getComputedStyle(hubCore).fill,
    luminance: luminance(getComputedStyle(hubCore).fill, Number(getComputedStyle(hubCore).opacity)),
  })
  // 旧公式重算（改前）：0.24 + importance*0.52 - depth*0.16，深度按旧重要度三环取。
  const oldDepth = imp => imp >= 0.72 ? 0.08 : imp >= 0.42 ? 0.42 : 0.74
  for (const row of rows) {
    row.oldOpacity = Math.max(0.12, 0.24 + row.importance * 0.52 - oldDepth(row.importance) * 0.16)
    row.oldLuminance = luminance(row.fill, row.oldOpacity)
  }
  return {
    rows,
    realEdges: Number(wrap.getAttribute('data-real-edges')),
    inTable: Number(wrap.getAttribute('data-edge-in-table')),
    unresolved: Number(wrap.getAttribute('data-edge-unresolved')),
    isolated: Number(wrap.getAttribute('data-isolated-nodes')),
    edgeNodes: Number(wrap.getAttribute('data-edge-nodes')),
    layerCounts: wrap.getAttribute('data-layer-counts'),
    coverage: wrap.getAttribute('data-edge-coverage'),
    calibrated: wrap.getAttribute('data-layer-calibrated'),
    reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
    motion: {
      degraded: svg?.dataset.motionDegraded, wave: svg?.dataset.motionWave,
      wavesSeeds: svg?.dataset.motionWaveSeeds, links: svg?.dataset.motionLinks,
      pulses: svg?.dataset.motionPulses, isWaving: svg?.classList.contains('is-waving'),
    },
  }
}

const median = values => {
  const sorted = [...values].sort((a, b) => a - b)
  return sorted.length ? sorted[Math.floor(sorted.length / 2)] : NaN
}

function byLayer(rows, layer, used) {
  return rows.filter(row => row.layer === layer && (used === undefined || row.used === used))
}

const browser = await chromium.launch({ channel: 'msedge', headless: true })
fs.mkdirSync(OUT, { recursive: true })
const consoleErrors = []
const brightness = {}
let richEdges = []

try {
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 })
  const page = await context.newPage()
  page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()) })
  page.on('pageerror', err => consoleErrors.push(String(err)))
  page.on('request', req => {
    const url = req.url()
    if (url.includes('/memory/inspect') || url.includes('/api/memory/context')) inspectUrls.push(url)
  })

  const richSession = await openOverview(page, SESSION_RICH)
  const project = await page.evaluate(() => localStorage.getItem('docmind_project_id') || '')
  const api = await payload(richSession, project)
  richEdges = api.edges || []
  const shot = await page.evaluate(measure)
  const headerText = await page.locator('[data-memory-panel] .mp-identity').innerText()
  check('面板确实用了指定真实会话', headerText.includes(richSession), headerText.replace(/\n/g, ' / '))
  check('inspect 请求带真实会话', inspectUrls.some(url => url.includes(richSession)),
    inspectUrls.find(url => url.includes('/memory/inspect')) || '(未捕获)')
  brightness.rich = shot

  // 1. 每个 store 的节点带预期 layer
  const expected = api.layer_config.store_map
  for (const store of Object.keys(expected)) {
    const nodes = shot.rows.filter(row => row.store === store)
    if (!nodes.length) continue
    const wrong = nodes.filter(row => row.layer !== expected[store])
    check(`store→layer: ${store}→${expected[store]}`, nodes.length > 0 && wrong.length === 0,
      `${nodes.length} 个节点，错层 ${wrong.length}`)
  }
  check('hub 是 rule（常驻守则）且不进环',
    shot.rows.some(row => row.key === 'core' && row.layer === 'rule'),
    `${shot.rows.find(row => row.key === 'core')?.layer} / ${shot.rows.find(row => row.key === 'core')?.layerLabel}`)
  check('四层环都画了（数量可为 0，但环存在）',
    await page.locator('[data-memory-panel] .mo-layer-ring').count() === 4,
    shot.layerCounts)

  // 2. 亮度单调（同注入状态可比）：rule=hub；shared/legacy/project 取未注入；hook 在钩子会话取
  const samples = {
    rule: [shot.rows.find(row => row.key === 'core')],
    shared: byLayer(shot.rows, 'shared', false).slice(0, 3),
    legacy: byLayer(shot.rows, 'legacy', false).slice(0, 3),
    project: byLayer(shot.rows, 'project', false).slice(0, 3),
  }
  const legacyUsed = byLayer(shot.rows, 'legacy', true)
  check('同层注入比未注入亮（legacy，真实注入 8 条）',
    legacyUsed.length > 0 && Math.min(...legacyUsed.map(r => r.luminance)) > Math.max(...byLayer(shot.rows, 'legacy', false).map(r => r.luminance)),
    `used 中位亮度 ${median(legacyUsed.map(r => r.luminance)).toFixed(4)} vs unused ${median(byLayer(shot.rows, 'legacy', false).map(r => r.luminance)).toFixed(4)}`)
  check('注入没有抹平层级：shared/project 注入不透明度仍 < 1',
    byLayer(shot.rows, 'shared', true).every(r => r.opacity < 1) && byLayer(shot.rows, 'project', true).every(r => r.opacity < 1),
    (byLayer(shot.rows, 'shared', true)[0]?.opacity || 'n/a') + ' / ' + (byLayer(shot.rows, 'project', true)[0]?.opacity || 'n/a'))
  const spread = ['shared', 'legacy', 'project'].map(layer => {
    const rows = byLayer(shot.rows, layer)
    return rows.length ? Math.max(...rows.map(r => r.opacity)) - Math.min(...rows.map(r => r.opacity)) : 0
  })
  check('噪声：同层内仍有亮度差（未被统一成 1）', spread.some(value => value > 0.01), spread.map(v => v.toFixed(3)).join('/'))

  // 3. 真实边条数与 API 完全一致，且不存在 API 外的边
  const apiEdges = new Set((api.edges || []).map(edge => `${edge.source}>${edge.target}`))
  const domEdges = await page.evaluate(() => [...document.querySelectorAll('[data-memory-panel] .mo-edge')]
    .filter(el => el.getAttribute('data-edge-injection') === '0')
    .map(el => `${el.getAttribute('data-edge-source')}>${el.getAttribute('data-edge-target')}`))
  check('真实边条数 = API edges 条数', domEdges.length === (api.edges || []).length,
    `DOM ${domEdges.length} / API ${(api.edges || []).length}（表内 ${shot.inTable}，端点缺失 ${shot.unresolved}）`)
  check('没有 API 之外的合成边', domEdges.every(key => apiEdges.has(key)),
    domEdges.filter(key => !apiEdges.has(key)).slice(0, 3).join(' | '))
  check('注入连线被单独标记，不算邻接',
    await page.locator('[data-memory-panel] .mo-edge[data-edge-injection="1"]').count() === (api.injected || []).length,
    `${await page.locator('[data-memory-panel] .mo-edge[data-edge-injection="1"]').count()} 条`)
  check('无关联节点数与 API 口径一致', shot.isolated === shot.rows.filter(r => r.key !== 'core' && r.edgeCount === 0).length,
    `${shot.isolated} / 图内 ${shot.rows.filter(r => r.key !== 'core' && r.edgeCount === 0).length}`)
  check('无边节点有虚线第二信号', await page.locator('[data-memory-panel] .mo-node.isolated .mo-isolated-mark').count() === shot.isolated,
    `${await page.locator('[data-memory-panel] .mo-node.isolated .mo-isolated-mark').count()} 个`)
  const coverageText = await page.locator('[data-memory-panel] [data-edge-summary]').innerText()
  check('关系覆盖 N/总数 与图口径一致（N=真实边，总数=节点）',
    shot.coverage === `${(api.edges || []).length}/${shot.rows.filter(row => row.key !== 'core').length}`
      && coverageText.includes('关系覆盖'),
    `data-edge-coverage=${shot.coverage} · ${coverageText.trim()}`)

  // 截图 01/02/03：先截未交互的五层全景与真实边（不含详情浮层）
  await page.locator('[data-memory-panel]').screenshot({ path: path.join(OUT, '01-five-layer-panorama.png') })
  await page.locator('[data-memory-panel] .mo-graph-wrap').screenshot({ path: path.join(OUT, '02-same-layer-injected-vs-not.png') })
  await clipEdgeRegion(page)
  // 关系覆盖率提示单独留证（caption 区域，文字与数值同框；此时无详情浮层遮挡）。
  await page.locator('[data-memory-panel] .mo-graph-caption').screenshot({ path: path.join(OUT, '06-relation-coverage.png') })

  // 4. 孤立节点：详情写“无关联边”，拖拽波及 0；激活波不会从它长出来
  const isolatedUsed = await page.$('[data-memory-panel] .mo-node.isolated.used')
  const isolatedNode = isolatedUsed || await page.$('[data-memory-panel] .mo-node.isolated')
  const isolatedKey = await isolatedNode.getAttribute('data-node-key')
  await isolatedNode.click()
  await page.waitForSelector('[data-memory-panel] .mo-detail', { timeout: 20000 })
  await waitStableMotion(page)
  const detailText = await page.locator('[data-memory-panel] .mo-detail').innerText()
  check('孤立节点详情显示“无关联边”', detailText.includes('无关联边'), detailText.split('\n').find(line => line.includes('无关联边')) || '')
  await page.locator('[data-memory-panel]').screenshot({ path: path.join(OUT, '05-isolated-node.png') })
  // 真实拓扑重算：等 reheat 后的稳定图再量坐标（d3 reheat 会重排，不能拿点击前的坐标）。
  await closeDetail(page)
  await waitStableMotion(page)
  const usedKeys = shot.rows.filter(row => row.used && row.key !== 'core').map(row => row.key)
  const metrics = await graphMetrics(page, api.edges || [], usedKeys)
  const hoodHub = metrics.neighborhoods.find(row => row.degree > 0) || null
  const isolatedAgain = await page.$(`[data-memory-panel] .mo-node[data-node-key="${isolatedKey}"]`)
  const isolatedDrag = await dragNode(page, isolatedAgain)
  check('拖拽孤立节点影响 0 个邻域', isolatedDrag.affected === 0, `affected=${isolatedDrag.affected}`)
  const isolatedHood = metrics.neighborhoods.find(row => row.key === isolatedKey)
  check('孤立节点在真实边上度数为 0（不是靠距离裁出来的）', isolatedHood?.degree === 0,
    `${isolatedKey} degree=${isolatedHood?.degree ?? 'n/a'}`)
  // 波前是全局注入种子在真实邻接上扩散；孤立节点自身只覆盖自己，不向外扩散。
  const wave = await page.evaluate(() => document.querySelector('[data-memory-panel] .mo-stable-graph')?.dataset.motionWave)
  if (shot.motion.degraded === '1') {
    check('弱机降级下不播种子波前（如实记录，不是伪造）', wave === undefined,
      `degraded=1 wave=${wave}`)
  } else {
    check('激活波前与真实邻接 BFS 一致（孤儿不扩散）',
      Number(wave) === metrics.expectedWave,
      `DOM wave=${wave} / 重算=${metrics.expectedWave}（种子 ${metrics.seeds}，孤立节点只覆盖自身）`)
  }

  await closeDetail(page)

  // 5. 拖拽 hub 节点：用真实邻接 + 坐标重算 2 跳邻域，和 DOM 的 dragAffected 对账
  let hubDrag = { affected: -1 }
  if (hoodHub) {
    const hubNode = await page.$(`[data-memory-panel] .mo-node[data-node-key="${hoodHub.key}"]`)
    if (hubNode) hubDrag = await dragNode(page, hubNode, path.join(OUT, '06-drag-neighborhood.png'))
  }
  check('拖拽 hub 的 2 跳邻域与真实邻接重算一致',
    !!hoodHub && hubDrag.affected === hoodHub.within && hubDrag.key === hoodHub.key,
    hoodHub
      ? `${hoodHub.key} degree=${hoodHub.degree} 2跳=${hoodHub.reached} 半径内=${hoodHub.within} 实测=${hubDrag.affected}（拖到 ${hubDrag.key}）`
      : '真实边里没有可拖的 hub')
  // 6. 钩子会话：hook 真实节点
  const hookSession = await openOverview(page, SESSION_HOOK)
  const hookApi = await payload(hookSession, project)
  const hookShot = await page.evaluate(measure)
  brightness.hook = hookShot
  check('hook 会话钩子在环里（真实条目，不补数）',
    hookShot.rows.some(row => row.layer === 'hook' && row.store === 'hook'),
    `hook 节点 ${hookShot.rows.filter(r => r.store === 'hook').length} 个 / ${hookShot.layerCounts}`)
  await page.locator('[data-memory-panel] .mo-graph-wrap').screenshot({ path: path.join(OUT, '04-hook-layer.png') })

  // 7. edges 为空时仍可渲染（接口拦截，仅测试用，不截图）
  const emptyContext = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 })
  const emptyPage = await emptyContext.newPage()
  emptyPage.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()) })
  const blank = { ...hookApi, edges: [], edge_stats: { ...hookApi.edge_stats, in_table: 0, resolved: 0, unresolved: 0 } }
  await emptyPage.route('**/api/agent/memory/inspect*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(blank),
  }))
  await openOverview(emptyPage, SESSION_HOOK, false)
  const emptyNodes = await emptyPage.locator('[data-memory-panel] .mo-node').count()
  const emptyText = await emptyPage.locator('[data-memory-panel] [data-edge-summary]').innerText()
  const emptyRealEdges = await emptyPage.locator('[data-memory-panel] .mo-graph-wrap').getAttribute('data-real-edges')
  check('edges 为空仍渲染节点', emptyNodes > 0, `${emptyNodes} 个节点`)
  check('edges 为空时文案说“真实边 0 条”', emptyText.includes('真实边 0 条') && emptyRealEdges === '0',
    `${emptyText.trim()} / data-real-edges=${emptyRealEdges}`)
  check('edges 为空时不画任何非注入边',
    await emptyPage.locator('[data-memory-panel] .mo-edge[data-edge-injection="0"]').count() === 0)
  await emptyContext.close()

  // 8. 采样亮度表（canvas 像素）+ 层级单调
  brightness.table = sampleTable(shot, hookShot)
  const table = brightness.table.filter(row => row.n > 0)
  const monotone = table.every((row, index) => index === 0
    || Number(table[index - 1].newMedian) > Number(row.newMedian))
  check('新亮度 rule>shared>legacy>project>hook 严格单调（canvas 像素中位亮度）', monotone,
    table.map(row => `${row.layer}:${row.newMedian}(n=${row.n})`).join(' > '))
  check('旧公式不保证层级单调（作为对照，不要求通过）',
    !table.every((row, index) => index === 0 || Number(table[index - 1].oldMedian) > Number(row.oldMedian)),
    table.map(row => `${row.layer}:${row.oldMedian}`).join(' / '))

  // 9. 宽度 / 溢出 / console
  const panelWidth = await page.locator('[data-memory-panel]').evaluate(el => ({
    rect: el.getBoundingClientRect().width, client: el.clientWidth,
  }))
  check('记忆总览面板宽度 720（未回退）', Math.round(panelWidth.rect) === 720,
    `rect=${panelWidth.rect}px client=${panelWidth.client}px（clientWidth 不含 1px 左边框）`)
  check('无横向溢出', await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    `${await page.evaluate(() => document.documentElement.scrollWidth)} ≤ 1600`)
  const tabMetrics = await page.evaluate(() => {
    const bar = document.querySelector('[data-memory-panel] .mp-tabs')
    return {
      count: document.querySelectorAll('[data-memory-panel] .mp-tab').length,
      scrollWidth: bar?.scrollWidth || 0,
      clientWidth: bar?.clientWidth || 0,
    }
  })
  check('记忆面板 8 个 tab 且横向溢出 0', tabMetrics.count === 8
    && tabMetrics.scrollWidth <= tabMetrics.clientWidth + 1,
    `${tabMetrics.count} 个 tab · scrollWidth=${tabMetrics.scrollWidth} clientWidth=${tabMetrics.clientWidth}`)

  // 面板互斥：记忆总览还开着时点 Agent 树，两个面板不能同时可见。
  // 记忆面板遮住顶栏入口，用户实际操作会先关面板；这里直接触发同一个 handler，
  // 断言互斥逻辑本身：Agent 树一打开，记忆面板必须已卸载。
  const treeEntry = page.locator('[data-agent-tree-entry]')
  if (await treeEntry.count()) {
    await page.evaluate(() => document.querySelector('[data-agent-tree-entry]')?.click())
    await page.waitForSelector('[data-agent-tree-panel]', { timeout: 10000 })
    await page.waitForSelector('[data-memory-panel]', { state: 'detached', timeout: 10000 }).catch(() => {})
    check('面板互斥：Agent 树打开时记忆面板已关闭',
      await page.locator('[data-memory-panel]').count() === 0
        && await page.locator('[data-agent-tree-panel]').count() === 1)
    const treeWidth = await page.locator('[data-agent-tree-panel]').evaluate(el => el.clientWidth)
    check('Agent 树抽屉宽度仍为 919', treeWidth === 919, `${treeWidth}px`)

    // 四态第二信号：状态不只靠颜色，还必须各有形状/描边/条纹/动画。
    const stateSessions = [
      { status: 'active', session: SESSION_RICH[0] },
      { status: 'running', session: 'hb-live-20261104-h' },
      { status: 'completed', session: 'hb-demo-20261104-a' },
      { status: 'failed', session: 'hb-stall-20261104-a' },
    ]
    const signals = []
    for (const item of stateSessions) {
      // AgentTreePanel 只在重新打开时按新 session 拉树；先关再开，避免沿用上一棵。
      await page.evaluate(session => window.DocMindSession.set(session), item.session)
      await page.evaluate(() => {
        const entry = document.querySelector('[data-agent-tree-entry]')
        if (entry?.getAttribute('aria-expanded') === 'true') entry.click()
      })
      await page.waitForSelector('[data-agent-tree-panel]', { state: 'detached', timeout: 5000 }).catch(() => {})
      await page.evaluate(() => document.querySelector('[data-agent-tree-entry]')?.click())
      await page.waitForSelector('[data-agent-tree-panel]', { timeout: 10000 })
      try {
        await page.waitForFunction(status => [...document.querySelectorAll('[data-agent-tree-panel] .at-node')]
          .some(node => node.classList.contains(`is-${status}`)), item.status, { timeout: 12000 })
      } catch {
        signals.push({ status: item.status, missing: true })
        continue
      }
      signals.push(await page.evaluate(status => {
        const node = [...document.querySelectorAll('[data-agent-tree-panel] .at-node')]
          .find(el => el.classList.contains(`is-${status}`))
        const mark = node?.querySelector('.at-status')
        const style = mark ? getComputedStyle(mark, '::before') : null
        return {
          status,
          borderStyle: style?.borderStyle || '',
          borderTopColor: style?.borderTopColor || '',
          transform: style?.transform || '',
          animationName: style?.animationName || '',
          backgroundImage: style?.backgroundImage || '',
          className: node?.className || '',
        }
      }, item.status))
    }
    const allPresent = signals.every(row => !row.missing)
    const hasSecondSignal = signals.every(row => row.missing || row.animationName !== 'none'
      || row.backgroundImage !== 'none' || row.transform !== 'none'
      || row.borderStyle !== 'none')
    const distinct = new Set(signals.map(row => [
      row.borderStyle, row.borderTopColor, row.transform, row.animationName, row.backgroundImage,
    ].join('|'))).size
    check('四态（active/running/completed/failed）第二信号都在且四种可区分',
      allPresent && hasSecondSignal && distinct === 4,
      signals.map(row => `${row.status}:${row.missing ? 'missing' : [
        row.borderStyle, row.transform, row.animationName, row.backgroundImage,
      ].filter(Boolean).join(',')}`).join(' | '))
  }
  check('console error 为 0', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | '))

  await context.close()
} finally {
  await browser.close()
}

/**
 * 用「真实边 + 图上实际坐标」重算：
 *  - 每个节点的邻接度数，以及拖拽 2 跳邻域里落在 dragReach 半径内的节点数；
 *  - 注入种子在真实邻接上 BFS（≤waveMaxHops）能覆盖多少节点。
 * 口径对齐 memoryMotion.ts 的 collectAffected / applyWave，用来和 DOM 实测对账，
 * 避免「看着热闹」把跳数或阈值调大。
 */
async function graphMetrics(page, edges, usedKeys) {
  return page.evaluate(({ edges, usedKeys, hops, reach, maxNodes, waveHops }) => {
    const pos = new Map()
    for (const el of document.querySelectorAll('[data-memory-panel] .mo-node')) {
      const key = el.getAttribute('data-node-key')
      const core = el.querySelector('.mo-neuron-core')
      pos.set(key, [Number(core.getAttribute('cx')), Number(core.getAttribute('cy'))])
    }
    const adj = new Map()
    for (const edge of edges) {
      if (!pos.has(edge.source) || !pos.has(edge.target)) continue
      for (const [a, b] of [[edge.source, edge.target], [edge.target, edge.source]]) {
        const list = adj.get(a) || []
        if (!list.includes(b)) list.push(b)
        adj.set(a, list)
      }
    }
    const neighborhoods = [...pos.keys()].map(key => {
      const seen = new Set([key])
      let frontier = [key]
      for (let depth = 0; depth < hops; depth += 1) {
        const next = []
        for (const head of frontier) {
          for (const other of adj.get(head) || []) {
            if (other === 'core' || seen.has(other)) continue
            seen.add(other); next.push(other)
            if (seen.size >= maxNodes) break
          }
          if (seen.size >= maxNodes) break
        }
        frontier = next
        if (!frontier.length || seen.size >= maxNodes) break
      }
      seen.delete(key)
      const [ox, oy] = pos.get(key) || [0, 0]
      let within = 0
      for (const other of seen) {
        const [x, y] = pos.get(other) || [ox, oy]
        const distance = Math.hypot(x - ox, y - oy)
        if (distance >= 1 && distance <= reach) within += 1
      }
      return { key, degree: (adj.get(key) || []).length, reached: seen.size, within }
    }).sort((a, b) => b.within - a.within || b.degree - a.degree)
    // 波前：注入节点是种子，沿真实邻接扩散，跨不过 core，也不从孤立节点凭空长出来。
    const distance = new Map()
    const queue = []
    for (const key of usedKeys) if (pos.has(key)) { distance.set(key, 0); queue.push(key) }
    for (let head = 0; head < queue.length; head += 1) {
      const key = queue[head]
      const depth = distance.get(key) || 0
      if (depth >= waveHops) continue
      for (const other of adj.get(key) || []) {
        if (other === 'core' || distance.has(other)) continue
        distance.set(other, depth + 1)
        queue.push(other)
      }
    }
    return { neighborhoods, expectedWave: distance.size, seeds: usedKeys.length }
  }, { edges, usedKeys, hops: 2, reach: 190, maxNodes: 36, waveHops: 6 })
}

/** 等稳定图重新挂上 motion（点节点会 reheat 力导向，稳定 SVG 会被骨架替换再重建）。 */
async function waitStableMotion(page) {
  await page.waitForFunction(() => {
    const svg = document.querySelector('[data-memory-panel] .mo-stable-graph')
    return !!svg && (svg.dataset.motionDegraded === '1' || !!svg.dataset.motionWave)
  }, null, { timeout: 30000 })
  await page.waitForTimeout(120)
}

async function closeDetail(page) {
  const close = page.locator('[data-memory-panel] button[aria-label="关闭记忆详情"]')
  if (await close.count()) await close.click()
  await page.waitForTimeout(120)
}

async function dragNode(page, locator, screenshot) {
  const core = await locator.$('.mo-neuron-core')
  const box = await (core || locator).boundingBox()
  const svg = page.locator('[data-memory-panel] .mo-stable-graph')
  if (!box) return { affected: 0 }
  const cx = box.x + box.width / 2
  const cy = box.y + box.height / 2
  await page.mouse.move(cx, cy)
  await page.mouse.down()
  await page.mouse.move(cx + 60, cy + 36, { steps: 8 })
  await page.waitForTimeout(250)
  if (screenshot) {
    // 拖拽邻域要看得见：以被拖节点为中心裁切，否则移动幅度被整图摊薄。
    const moved = await locator.boundingBox()
    const width = 520
    const height = 340
    const clip = {
      x: Math.max(0, (moved?.x ?? box.x) + (moved?.width ?? box.width) / 2 - width / 2),
      y: Math.max(0, (moved?.y ?? box.y) + (moved?.height ?? box.height) / 2 - height / 2),
      width, height,
    }
    const viewport = await page.evaluate(() => ({ width: innerWidth, height: innerHeight }))
    clip.width = Math.min(clip.width, viewport.width - clip.x)
    clip.height = Math.min(clip.height, viewport.height - clip.y)
    await page.screenshot({ path: screenshot, clip })
  }
  const raw = await svg.getAttribute('data-drag-affected')
  const key = await svg.getAttribute('data-drag-key')
  await page.mouse.up()
  await page.waitForTimeout(250)
  return { affected: raw === null ? -1 : Number(raw), key }
}

async function clipEdgeRegion(page) {
  const box = await page.evaluate(() => {
    const svg = document.querySelector('[data-memory-panel] .mo-stable-graph')
    const wrap = document.querySelector('[data-memory-panel] .mo-graph-wrap')
    if (!svg || !wrap) return null
    const cores = new Map([...svg.querySelectorAll('.mo-node')].map(el => [
      el.getAttribute('data-node-key'), el.querySelector('.mo-neuron-core').getBoundingClientRect(),
    ]))
    const pairs = [...svg.querySelectorAll('.mo-edge[data-edge-injection="0"]')]
      .map(el => [el.getAttribute('data-edge-source'), el.getAttribute('data-edge-target')])
      .filter(([a, b]) => cores.has(a) && cores.has(b))
    // 取最大真实连通分量来截图：孤立边分散在整圈时，整图裁切会把边摊薄到看不见。
    const parent = new Map([...cores.keys()].map(key => [key, key]))
    const find = key => {
      const up = parent.get(key)
      if (up === key) return key
      const root = find(up)
      parent.set(key, root)
      return root
    }
    for (const [a, b] of pairs) parent.set(find(a), find(b))
    const groups = new Map()
    for (const key of parent.keys()) {
      const root = find(key)
      const list = groups.get(root) || []
      list.push(key)
      groups.set(root, list)
    }
    const best = [...groups.values()].sort((a, b) => b.length - a.length)[0]
    if (!best) return null
    const rects = best.map(key => cores.get(key))
    const frame = wrap.getBoundingClientRect()
    const pad = 70
    const x = Math.max(frame.left, Math.min(...rects.map(r => r.left)) - pad)
    const y = Math.max(frame.top, Math.min(...rects.map(r => r.top)) - pad)
    const right = Math.min(frame.right, Math.max(...rects.map(r => r.right)) + pad)
    const bottom = Math.min(frame.bottom, Math.max(...rects.map(r => r.bottom)) + pad)
    return { x, y, width: Math.max(120, right - x), height: Math.max(90, bottom - y) }
  })
  if (box) {
    const viewport = await page.evaluate(() => ({ width: innerWidth, height: innerHeight }))
    box.width = Math.min(box.width, viewport.width - box.x)
    box.height = Math.min(box.height, viewport.height - box.y)
    await page.screenshot({ path: path.join(OUT, '03-real-edges-cluster.png'), clip: box })
  }
}

function sampleTable(shot, hookShot) {
  const rows = []
  const pick = (layer, list) => {
    const values = list.slice(0, 3)
    rows.push({
      layer,
      n: values.length,
      used: values.filter(v => v.used).length,
      newOpacity: values.map(v => v.opacity.toFixed(3)),
      oldOpacity: values.map(v => v.oldOpacity.toFixed(3)),
      newLum: values.map(v => v.luminance.toFixed(4)),
      oldLum: values.map(v => v.oldLuminance.toFixed(4)),
      newMedian: median(values.map(v => v.luminance)).toFixed(4),
      oldMedian: median(values.map(v => v.oldLuminance)).toFixed(4),
    })
  }
  pick('rule', shot.rows.filter(r => r.layer === 'rule'))
  pick('shared', byLayer(shot.rows, 'shared'))
  pick('legacy', byLayer(shot.rows, 'legacy', false))
  pick('project', byLayer(shot.rows, 'project', false))
  pick('hook', hookShot.rows.filter(r => r.layer === 'hook'))
  return rows
}

function topologySummary(rows, edges) {
  const keys = rows.filter(row => row.key !== 'core').map(row => row.key)
  const adj = new Map(keys.map(key => [key, new Set()]))
  let real = 0
  for (const edge of edges) {
    if (!adj.has(edge.source) || !adj.has(edge.target)) continue
    real += 1
    adj.get(edge.source).add(edge.target)
    adj.get(edge.target).add(edge.source)
  }
  const degrees = keys.map(key => adj.get(key).size)
  const isolated = degrees.filter(value => value === 0).length
  const seen = new Set()
  const components = []
  for (const key of keys) {
    if (seen.has(key) || adj.get(key).size === 0) continue
    const stack = [key]
    seen.add(key)
    let size = 0
    while (stack.length) {
      const head = stack.pop()
      size += 1
      for (const other of adj.get(head)) {
        if (seen.has(other)) continue
        seen.add(other)
        stack.push(other)
      }
    }
    components.push(size)
  }
  components.sort((a, b) => b - a)
  return {
    nodes: keys.length, realEdges: real, isolated,
    avgDegree: keys.length ? real * 2 / keys.length : 0,
    maxComponent: components[0] || 0, components,
    maxDegree: degrees.length ? Math.max(...degrees) : 0,
  }
}

const passed = results.filter(row => row.pass).length
const topology = topologySummary(brightness.rich?.rows || [], richEdges)
console.log(JSON.stringify({ passed, total: results.length, results, consoleErrors, brightness: { table: brightness.table, motion: brightness.rich?.motion }, topology }, null, 2))
if (passed !== results.length) process.exitCode = 1
