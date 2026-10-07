// Requires verify_scene_canvas.py --serve 8011. All mutations mocked; no LLM calls.
import { createRequire } from 'node:module'
import path from 'node:path'
import assert from 'node:assert/strict'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE, '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))
const base = process.env.DOCMIND_UI_TEST_URL || 'http://127.0.0.1:8011'
const registry = await (await fetch(base + '/api/projects')).json()
const fixture = registry.projects.find(p => p.project_id === registry.current)
assert.equal(fixture.name, 'UI regression fixture')
const tree = await (await fetch(base + '/api/fs/tree')).json()
const second = { ...fixture, project_id: 'prj-race-second', name: 'Second fixture', root: fixture.root + '_mockB' }
const browser = await chromium.launch({ channel: 'msedge', headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
  page.setDefaultTimeout(15000)
  page.on('dialog', d => d.accept())
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  await page.route('**/api/sessions/*', r => r.fulfill({ json: { turns: [] } }))
  await page.goto(base + '/workbench')
  await page.locator('.ft-name[title="behaviors/player.gd"]').click()
  await page.waitForFunction(() => window.__docmind_cm?.state.doc.length > 0)
  let releaseRead
  const held = new Promise(resolve => { releaseRead = resolve })
  let startedRead
  const readStarted = new Promise(resolve => { startedRead = resolve })
  await page.route('**/api/fs/file?*', async r => {
    const response = await r.fetch()
    startedRead()
    await held
    await r.fulfill({ response })
  })
  await page.reload()
  await readStarted
  await page.waitForFunction(() => window.__docmind_cm?.state.doc.length > 0)
  await page.evaluate(() => {
    const v = window.__docmind_cm
    v.dispatch({ changes: { from: v.state.doc.length, insert: '\n# EDIT-WHILE-RESTORING' } })
  })
  releaseRead()
  await page.waitForTimeout(300)
  assert.match(await page.evaluate(() => window.__docmind_cm.state.doc.toString()), /EDIT-WHILE-RESTORING/)
  console.log('PASS delayed disk read does not overwrite new editor input')
  await page.close()

  const chat = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
  chat.setDefaultTimeout(15000)
  chat.on('dialog', d => d.accept())
  chat.on('pageerror', e => errors.push(e.message))
  await chat.addInitScript(() => {
    const nativeFetch = window.fetch.bind(window)
    window.fetch = async (input, init) => {
      if (input !== '/api/chat') return nativeFetch(input, init)
      window.__chatRequests = (window.__chatRequests || 0) + 1
      const stream = new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode('data: {"type":"token","text":"PARTIAL-OLD-PROJECT"}\n\n'))
        init?.signal?.addEventListener('abort', () => controller.error(new DOMException('Aborted', 'AbortError')), { once: true })
      } })
      return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })
    }
  })
  let current = fixture.project_id
  await chat.route('**/api/projects', r => r.fulfill({ json: { ok: true, current, projects: [fixture, second] } }))
  await chat.route('**/api/projects/*/activate', r => { current = r.request().url().includes(second.project_id) ? second.project_id : fixture.project_id; return r.fulfill({ json: { ok: true } }) })
  await chat.route('**/api/fs/tree', r => r.fulfill({ json: { ...tree, code_root: r.request().headers()['x-docmind-project'] === second.project_id ? second.root : fixture.root } }))
  await chat.route('**/api/sessions/*', r => r.fulfill({ json: { turns: [] } }))
  await chat.route('**/api/tasks', r => r.fulfill({ json: r.request().method() === 'POST'
    ? { ok: true, task: { id: 'task-A' } }
    : { ok: true, tasks: [{ id: 'history', title: r.request().headers()['x-docmind-project'] === second.project_id ? 'HISTORY-B' : 'HISTORY-A', status: 'open' }] } }))
  await chat.goto(base + '/workbench')
  await chat.waitForSelector('.ft-row')
  // te/ap/gp 触发钮已收进隐藏宿主，面板改由顶栏「更多」菜单唤起
  await chat.locator('.wb-menu-trigger').click()
  await chat.locator('.wb-menu-item', { hasText: '任务与生成' }).click()
  await chat.getByText('HISTORY-A · open').waitFor()
  await chat.getByPlaceholder('任务目标，例如：修改玩家受击逻辑').fill('TASK-A')
  await chat.getByPlaceholder('分区，例如 behaviors').fill('behaviors')
  await chat.getByRole('button', { name: '保存任务', exact: true }).click()
  await chat.getByText('任务已保存 task-A').waitFor()
  const saved = await chat.evaluate(pid => JSON.parse(localStorage.getItem('docmind.activeTask:' + pid)), fixture.project_id)
  assert.equal(saved.id, 'task-A')
  await chat.locator('.te-backdrop').click({ position: { x: 5, y: 200 } })
  await chat.locator('.cd-input').fill('OLD-PROJECT-QUESTION')
  await chat.locator('.cd-input').press('Control+Enter')
  await chat.getByText('PARTIAL-OLD-PROJECT', { exact: true }).waitFor()
  await chat.locator('.wb-proj-btn').click()
  await chat.locator('.wb-proj-item').filter({ hasText: 'Second fixture' }).click()
  await chat.waitForFunction(() => !document.querySelector('.cd-stop'))
  assert.equal(await chat.locator('.cd-msg').count(), 0)
  await chat.locator('.wb-menu-trigger').click()
  await chat.locator('.wb-menu-item', { hasText: '任务与生成' }).click()
  await chat.getByText('HISTORY-B · open').waitFor()
  assert.equal(await chat.getByPlaceholder('分区，例如 behaviors').inputValue(), '')
  await chat.locator('.te-backdrop').click({ position: { x: 5, y: 200 } })
  let savePayload
  await chat.route('**/api/fs/save', r => { savePayload = r.request().postDataJSON(); return r.fulfill({ json: { ok: true, mtime: 123, reindex_warnings: [] } }) })
  await chat.locator('.ft-name[title="behaviors/player.gd"]').click()
  await chat.waitForFunction(() => window.__docmind_cm?.state.doc.length > 0)
  await chat.locator('.cm-content').click()
  await chat.locator('.cm-content').press('Control+s')
  await chat.waitForTimeout(300)
  assert.equal(savePayload.task_id, '')
  console.log('PASS switching project aborts old stream, resets task panel and omits old task scope from save')
  await chat.locator('.wb-proj-btn').click()
  await chat.locator('.wb-proj-item').filter({ hasText: 'UI regression fixture' }).click()
  await chat.getByText('PARTIAL-OLD-PROJECT', { exact: true }).waitFor()
  await chat.getByText('上次回答因离开页面', { exact: false }).waitFor()
  assert.equal(await chat.evaluate(() => window.__chatRequests), 1)
  console.log('PASS interrupted response restored with warning and no automatic tool retry')
  // Homepage stream guard: Enter twice must not issue concurrent requests.
  await chat.goto(base + '/')
  // 问答页没有 id="sendBtn"/id="q"（历史上也没有过）。这里的等待只为"首页就绪"，
  // 不能等 .send-btn 可用 —— 它的 :disabled="!canSend" 要求草稿非空，空首页永远等不到。
  // 真正的并发断言在后面的请求计数里。
  await chat.waitForSelector('.composer textarea')
  await chat.locator('.composer textarea').fill('HOME-QUESTION')
  await chat.locator('.composer textarea').press('Enter')
  await chat.locator('.composer textarea').fill('DUPLICATE')
  await chat.locator('.composer textarea').press('Enter')
  assert.equal(await chat.evaluate(() => window.__chatRequests), 1)
  await chat.goto(base + '/workbench')
  await chat.getByText('HOME-QUESTION', { exact: true }).waitFor()
  await chat.getByText('上次回答因离开页面', { exact: false }).waitFor()
  console.log('PASS homepage prevents concurrent sends and shares interrupted draft with workbench')
  // 运行游戏触发钮由 SceneRuntimePanel teleport 进菜单槽位，菜单收起时不可见
  await chat.locator('.wb-menu-trigger').click()
  await chat.locator('.wb-menu-slot-item .sr-trigger').click()
  await chat.locator('.pb-tabs').getByRole('button', { name: '场景画布' }).click()
  await chat.locator('.pb-path').fill('scenes/Main.tscn')
  await chat.getByRole('button', { name: '加载场景', exact: true }).click()
  await chat.waitForSelector('.vue-flow__node-sceneNode')
  await chat.locator('.sc-node-name').filter({ hasText: /^Player$/ }).click()
  await chat.getByRole('button', { name: '聚焦选中', exact: true }).click()
  await chat.waitForFunction(() => {
    const node = [...document.querySelectorAll('.sc-node')].find(el => el.querySelector('.sc-node-name')?.textContent === 'Player')?.getBoundingClientRect()
    const canvas = document.querySelector('.sc-canvas')?.getBoundingClientRect()
    return node && canvas && Math.abs(node.x + node.width / 2 - canvas.x - canvas.width / 2) < 3
  })
  // Focusing intentionally moves distant file cards outside the viewport.
  // Return to the full graph through the same visible control a user would use.
  await chat.locator('.sc-canvas .vue-flow__controls-fitview').click()
  await chat.locator('.sc-file').filter({ hasText: 'behaviors/player.gd' }).dblclick()
  await chat.locator('.pb-x').click()
  await chat.waitForFunction(() => window.__docmind_cm?.state.doc.toString().includes('extends'))
  console.log('PASS scene node focus and file-card double click open the actual editor')
  assert.deepEqual(errors, [])
  console.log('PASS no browser runtime errors in race scenarios')
} finally { await browser.close() }
