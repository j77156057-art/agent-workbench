// Plan 长跑 + 内部记忆定时检查的界面验收。
//
// 前置：frontend 里先 `npm run build`（工作台读的是构建产物 web/）。
// 用法：node verify_plan_ui.mjs
//
// 隔离：本脚本自己起一次性夹具（verify_scene_canvas.py --serve <随机端口>），
// 并把 DOCMIND_MEMORY_DIR 另指临时目录 —— Plan/记忆写盘只落夹具，绝不碰日常实例。
// Plan 的 start/stop/status 走 route 桩：这里验的是**前端契约**（头、体、状态渲染、
// 确认门），runner 的真实执行由 tests/test_plan_runner.py 用假 LLM 驱动真 Agent 验。
// 只有「内部记忆检查间隔」走真实端点往返（夹具的 STATE_FILE 也是隔离的）。
import { createRequire } from 'node:module'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import net from 'node:net'
import path from 'node:path'
import { spawn } from 'node:child_process'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE, '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))

const PY = ['.venv/Scripts/python.exe', '.venv/bin/python', 'python', 'py']
  .filter(c => !c.includes('/') || fs.existsSync(c))

function freePort() {
  return new Promise((res, rej) => {
    const s = net.createServer()
    s.on('error', rej)
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)) })
  })
}

async function startFixture() {
  const port = await freePort()
  const base = `http://127.0.0.1:${port}`
  const py = PY.find(c => c === 'python' || c === 'py' || fs.existsSync(c)) || 'python'
  const memoryDir = fs.mkdtempSync(path.join(os.tmpdir(), 'dm_plan_ui_mem_'))
  const child = spawn(py, ['verify_scene_canvas.py', '--serve', String(port)], {
    stdio: 'ignore',
    env: { ...process.env, DOCMIND_MEMORY_DIR: memoryDir, DOCMIND_MEMORY_V2: '1' },
  })
  const deadline = Date.now() + 60000
  while (Date.now() < deadline) {
    try {
      const r = await fetch(base + '/api/projects', { signal: AbortSignal.timeout(1500) })
      if (r.ok) return { base, child, memoryDir }
    } catch { /* 还没起来 */ }
    await new Promise(r => setTimeout(r, 400))
  }
  child.kill()
  throw new Error('夹具服务 60s 内没起来')
}

let passed = 0
const check = (name) => { passed++; console.log('PASS', name) }

const { base, child, memoryDir } = await startFixture()
const browser = await chromium.launch({ channel: 'msedge', headless: true })
const IDLE = { ok: true, active: false, state: null,
  taskbook: { items: [], pending: 0, done: 0, total: 0 }, log: [] }
const RUNNING = {
  ok: true, active: true,
  state: { id: 'plan-x', goal: '把登录模块补完并跑通验收', status: 'running',
           phase: 'execute', iteration: 4, next_review_at: '',
           last_final: '已完成第 2 项，正在跑测试。' },
  taskbook: { items: [
    { key: 'k1', text: '补齐登录校验', state: 'done' },
    { key: 'k2', text: '跑通验收用例', state: 'doing' },
    { key: 'k3', text: '更新文档', state: 'todo' }], pending: 2, done: 1, total: 3 },
  log: [{ kind: 'turn', text: '勾掉「补齐登录校验」，pytest 全绿',
          ts: '2026-10-31T02:00:00+00:00', actions: ['run_command: pytest -q', 'dev_plan: check'] },
    { kind: 'decision', text: '先修空输入再动样式', rationale: '测试最先失败',
      ts: '2026-10-31T01:50:00+00:00' }],
}
const stopCalls = []
const startCalls = []

try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
  page.setDefaultTimeout(15000)
  const errors = []
  page.on('pageerror', e => errors.push(e.message))

  let statusPayload = IDLE
  await page.route('**/api/plan/status**', route => route.fulfill({ json: statusPayload }))
  await page.route('**/api/plan/log**', route => route.fulfill({ json: { ok: true, items: [] } }))
  await page.route('**/api/plan/start', async route => {
    startCalls.push({ body: JSON.parse(route.request().postData() || '{}'),
                      header: route.request().headers()['x-docmind-surface'] })
    statusPayload = RUNNING
    await route.fulfill({ json: RUNNING })
  })
  await page.route('**/api/plan/stop', async route => {
    stopCalls.push({ body: JSON.parse(route.request().postData() || '{}'),
                     header: route.request().headers()['x-docmind-surface'] })
    statusPayload = { ...RUNNING, active: false,
      state: { ...RUNNING.state, status: 'stopped', user_stop: true } }
    await route.fulfill({ json: statusPayload })
  })

  await page.goto(base + '/workbench')
  await page.waitForSelector('.cd-chip')

  // 1. 开发台对话台里有「长跑」入口，且带无障碍名
  const planChip = page.getByRole('button', { name: 'Plan 长跑' })
  await planChip.waitFor()
  check('开发台对话台有「长跑」入口')

  // 2. 问答页（面向外部用户）必须没有这个入口 —— 「只开放在开发台」的前端半边
  await page.goto(base + '/')
  const askChips = await page.locator('.composer button[aria-label="Plan 长跑"]').count()
  assert.equal(askChips, 0, '问答页出现了 Plan 入口，越权面被打开')
  check('问答页没有 Plan 入口')

  // 3. 面板结构
  await page.goto(base + '/workbench')
  await page.waitForSelector('.cd-chip')
  await planChip.click()
  await page.waitForSelector('.pp')
  const hintText = await page.locator('.pp-hint').innerText()
  assert.match(hintText, /只有用户可以停止/, '面板没讲清「只有用户能停」')
  assert.equal(await page.locator('.pp-section').count(), 2, '任务书 / 决策账本两块必须都在')
  const startBtn = page.locator('.pp-primary')
  assert.equal(await startBtn.isDisabled(), true, '空目标就该锁住启动')
  assert.equal(await page.locator('.pp-badge').innerText(), '未启动')
  check('面板渲染目标输入 / 复盘间隔 / 任务书 / 决策账本')

  // 4. 启动：目标 → 请求带 surface 头 → 面板切到运行中
  await page.locator('.pp-textarea').fill('把登录模块补完并跑通验收')
  assert.equal(await startBtn.isDisabled(), false, '填了目标后启动按钮应解锁')
  await page.locator('.pp-num').fill('45')
  await startBtn.click()
  await page.locator('.pp-active').waitFor()
  assert.equal(startCalls.length, 1)
  assert.equal(startCalls[0].header, 'workbench', 'Plan 请求没带 X-DocMind-Surface')
  assert.equal(startCalls[0].body.goal, '把登录模块补完并跑通验收')
  assert.equal(startCalls[0].body.review_interval_minutes, 45)
  check('启动 Plan 带上开发台标识与复盘间隔')

  // 5. 运行态：进度、轮次、任务书与决策账本都来自服务端载荷
  const statusText = await page.locator('.pp-status').innerText()
  assert.match(statusText, /第 4 轮/)
  assert.match(statusText, /执行中/)
  const sectionText = await page.locator('.pp-section').first().innerText()
  assert.match(sectionText, /1\/3 已完成/, '任务书完成度对不上')
  assert.match(sectionText, /补齐登录校验/)
  assert.match(sectionText, /剩余 2 项/)
  const ledgerText = await page.locator('.pp-section').nth(1).innerText()
  assert.match(ledgerText, /pytest 全绿/)
  assert.match(ledgerText, /先修空输入再动样式/)
  assert.match(ledgerText, /执行轮/, '账本种类没翻成中文')
  assert.match(ledgerText, /决策/)
  assert.equal(await page.locator('.pp-badge').innerText(), '运行中')
  await page.locator('.pp-log details summary').first().click()
  assert.match(await page.locator('.pp-pre').first().innerText(), /run_command/)
  await page.screenshot({ path: 'artifacts/shots/plan-panel-running.png' })
  check('运行态展示轮次 / 任务书进度 / 决策账本')

  // 6. 关闭面板不会停止 Plan
  await page.locator('.pp-close').click()
  await page.waitForSelector('.pp', { state: 'detached' })
  assert.equal(stopCalls.length, 0, '关闭面板被当成了停止')
  check('关闭面板 ≠ 停止 Plan')

  // 6b. 任务书全部划掉 → 定期复盘阶段，并显示下次复盘时间
  statusPayload = {
    ...RUNNING,
    state: { ...RUNNING.state, status: 'reviewing', phase: 'reviewing',
             next_review_at: new Date(Date.now() + 30 * 60000).toISOString() },
    taskbook: { items: [{ key: 'k1', text: '补齐登录校验', state: 'done' }],
                pending: 0, done: 1, total: 1 },
  }
  await planChip.click()
  await page.waitForSelector('.pp')
  assert.match(await page.locator('.pp-status').innerText(), /定期复盘/)
  assert.match(await page.locator('.pp-body').innerText(), /下次复盘/)
  assert.equal(await page.locator('.pp-badge').innerText(), '定期复盘中')
  assert.match(await page.locator('.pp-section').first().innerText(), /1\/1 已完成/)
  assert.equal(await page.locator('.pp-danger').count(), 1, '复盘阶段仍要留用户可停的入口')
  await page.locator('.pp-close').click()
  await page.waitForSelector('.pp', { state: 'detached' })
  check('任务书全部划掉后进入定期复盘，仍只有用户能停')
  statusPayload = RUNNING

  // 7. 停止：必须过确认门；取消不发帖，确认后带 confirm=true
  await planChip.click()
  await page.waitForSelector('.pp')
  await page.locator('.pp-danger').click()
  await page.waitForSelector('.dg-box')
  assert.match(await page.locator('.dg-msg').innerText(), /停止后模型不再执行/)
  await page.locator('.dg-btn', { hasText: '取消' }).click()
  await page.waitForSelector('.dg-box', { state: 'detached' })
  assert.equal(stopCalls.length, 0, '取消确认却发出了停止请求')
  check('停止 Plan 有二次确认，取消即作废')

  await page.locator('.pp-danger').click()
  await page.waitForSelector('.dg-box')
  await page.locator('.dg-primary').click()
  await page.waitForSelector('.pp-textarea')
  assert.equal(stopCalls.length, 1)
  assert.equal(stopCalls[0].header, 'workbench')
  assert.equal(stopCalls[0].body.confirm, true, '停止请求必须显式 confirm=true')
  check('用户确认后 Plan 才停止，面板回到可再次启动')

  // 面板与既有的记忆面板同款全高抽屉（top:0/right:0），会盖住顶栏：先关掉再走后面的顶栏入口。
  await page.locator('.pp-close').click()
  await page.waitForSelector('.pp', { state: 'detached' })

  // 8. 内部记忆检查间隔：真实端点往返（夹具 STATE_FILE 与记忆目录都是临时隔离的）
  const saved = await (await fetch(base + '/api/memory-settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ memory_check_interval_minutes: 12 }),
  })).json()
  assert.equal(saved.memory_check_interval_minutes, 12, '后端没有把间隔落盘')
  const reload = await (await fetch(base + '/api/memory-settings')).json()
  assert.equal(reload.memory_check_interval_minutes, 12, '重启后读不回（没有真正持久化）')
  check('检查间隔写盘并能读回（12 分钟）')

  await page.locator('button[aria-label="设置"]').click()
  await page.waitForSelector('.sv-nav')
  // 只看**当前可见**的那块：未激活的分区是 v-show 隐藏、仍在 DOM 里，用文本计数会
  // 数到隐藏面板（实测就是先踩了这个）。
  const readInterval = () => page.evaluate(() => {
    const label = [...document.querySelectorAll('.sv-label')]
      .filter(el => el.offsetParent !== null)
      .find(el => el.textContent.includes('内部记忆检查间隔'))
    if (!label) return 'no-visible-label'
    const box = label.nextElementSibling || label.parentElement
    const input = box && box.querySelector('input[type="number"]')
    return input ? input.value : 'no-input'
  })
  let seen = 'no-visible-label'
  for (const tab of ['模型', '运行与引擎', '用量与费用', '智能体', '外观']) {
    await page.locator('.sv-nav-item', { hasText: tab }).click()
    seen = await readInterval()
    if (seen !== 'no-visible-label') break
  }
  assert.notEqual(seen, 'no-visible-label', '设置页找不到「内部记忆检查间隔」控件')
  // 再等 load() 把服务端值写进控件：夹具冷启动时 /api/config 第一发实测要好几秒，
  // 只等标签出现会读到还没填值的初值。
  const settled = Date.now() + 30000
  while (seen !== '12' && Date.now() < settled) {
    await new Promise(r => setTimeout(r, 400))
    seen = await readInterval()
  }
  assert.equal(seen, '12', `设置界面回显的是 ${seen}，不是后端持久化的 12`)
  check('设置页回显后端持久化的检查间隔')

  // 9. 记忆健康面板：这一发不打桩，直接打夹具的真端点
  await page.evaluate(() => document.querySelector('.sv-mask')
    ?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
  await page.waitForSelector('.sv-mask', { state: 'detached' })
  await page.getByRole('button', { name: '上下文记忆' }).click()
  await page.waitForSelector('.mp')
  await page.locator('.mp button', { hasText: '健康' }).click()
  await page.waitForSelector('.mp-health-grid', { timeout: 10000 })
  const cards = await page.locator('.mp-health-card').count()
  assert.ok(cards >= 5, `健康卡片只有 ${cards} 张`)
  check('记忆健康面板用真实端点渲染')

  assert.deepEqual(errors, [], `页面脚本错误：${errors.join(' | ')}`)
  await page.goto(base + '/workbench')
  await page.waitForSelector('.cd-chip')
  await page.getByRole('button', { name: 'Plan 长跑' }).click()
  await page.waitForSelector('.pp')
  await page.locator('.pp-textarea').fill('把登录模块补完并跑通验收')
  await page.screenshot({ path: 'artifacts/shots/plan-panel.png', fullPage: false })
  console.log('截图：artifacts/shots/plan-panel.png')
} finally {
  await browser.close()
  child.kill()
  fs.rmSync(memoryDir, { recursive: true, force: true })
}

console.log(`\n通过 ${passed} 项`)
