/** npm run build first; python verify_scene_canvas.py --serve 8011 in another terminal.
 * Refuses to write against anything except that disposable fixture. No model calls.
 * PLAYWRIGHT_MODULE may point to an installed playwright/playwright-core package.
 */
import { createRequire } from 'node:module'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE, '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))
const base = process.env.DOCMIND_UI_TEST_URL || 'http://127.0.0.1:8011'
const tree = await (await fetch(base + '/api/fs/tree')).json()
assert.match(path.basename(tree.code_root), /^docmind_verify_scene_/)
assert.equal((await (await fetch(base + '/api/projects')).json()).projects[0].name, 'UI regression fixture')
const browser = await chromium.launch({ channel: 'msedge', headless: true })
let passed = 0
const check = (name) => { passed++; console.log('PASS', name) }
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
  page.setDefaultTimeout(10000)
  page.on('dialog', d => d.accept())
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  const turn = { user: '回归验证问题', assistant: '回归验证回答' }
  let empty = false
  await page.route('**/api/sessions/*', route => route.fulfill({ json: { turns: empty ? [] : [turn] } }))
  await page.goto(base + '/workbench')
  await page.waitForSelector('.ft-row')
  await page.getByText(turn.assistant, { exact: true }).waitFor()
  await page.locator('.ft-name[title="behaviors/player.gd"]').click()
  await page.waitForFunction(() => window.__docmind_cm?.state.doc.toString().includes('extends'))
  const disk = fs.readFileSync(path.join(tree.code_root, 'behaviors/player.gd'), 'utf8')
  await page.evaluate(() => {
    const v = window.__docmind_cm
    v.dispatch({ changes: { from: v.state.doc.length, insert: '\n# UNSAVED-REGRESSION' } })
  })
  await page.locator('.cd-input').fill('未发送的工作台问题')
  // 2026-10-19：顶栏右侧的「问答页」<a> 已并入品牌（点 DocMind 回问答页），
  // 顶栏右侧只剩「设置 / 更多」。原来这里靠 .wb-question-link 消歧，现在用 .wb-brand-home。
  await page.locator('a.wb-brand-home').click()
  await page.getByText(turn.assistant, { exact: true }).waitFor()
  // 问答页输入框没有 id（历史上也没有过 id="q"），用 composer 容器定位
  const askInput = page.locator('.composer textarea')
  await askInput.fill('未发送的首页问题')
  await page.goto(base + '/workbench')
  await page.waitForFunction(() => window.__docmind_cm?.state.doc.toString().includes('UNSAVED-REGRESSION'))
  assert.equal(await page.locator('.cd-input').inputValue(), '未发送的工作台问题')
  assert.equal(fs.readFileSync(path.join(tree.code_root, 'behaviors/player.gd'), 'utf8'), disk)
  await page.getByText(turn.assistant, { exact: true }).waitFor()
  check('page roundtrip preserves history, editor draft and chat draft without disk writes')
  await page.goto(base + '/')
  await page.getByText(turn.assistant, { exact: true }).waitFor()
  assert.equal(await page.locator('.composer textarea').inputValue(), '未发送的首页问题')
  await page.goto(base + '/workbench')
  await page.waitForSelector('.ft-row')
  check('Q&A draft and history survive repeated navigation')

  for (const width of [1600, 1280]) {
    await page.setViewportSize({ width, height: 1000 })
    // 2026-10-19：「AI 运行设置」不再是顶栏「更多」里的浮层面板，已内嵌进
    // 设置页的「运行与引擎」分区（.ap-inline，static 定位、无遮罩）；
    // te/gp 仍是「更多」菜单唤起的浮层。两条路径分别验。
    for (const [menuItem, prefix] of [['任务与生成', 'te'], ['GPU 监控', 'gp']]) {
      await page.locator('.wb-menu-trigger').click()
      await page.locator('.wb-menu-item', { hasText: menuItem }).click()
      const panel = page.locator('.' + prefix + '-pop')
      await panel.waitFor()
      assert.equal(await panel.evaluate(el => {
        const r = el.getBoundingClientRect()
        return r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight && el.contains(document.elementFromPoint(r.left + 20, r.top + 20))
      }), true, `${prefix}: not clipped or covered`)
      await page.locator('.' + prefix + '-backdrop').click({ position: { x: 5, y: 200 } })
      await panel.waitFor({ state: 'hidden' })
    }
    // 内嵌档：设置 → 运行与引擎 → AI 运行策略。要求它就地在设置面板里、
    // 完整可见可点，且**没有**残留遮罩（内嵌面板不该再遮住设置页）。
    await page.getByRole('button', { name: '设置' }).first().click()
    await page.locator('.sv-nav-item').filter({ hasText: '运行与引擎' }).first().click()
    const inline = page.locator('.ap-inline')
    await inline.waitFor()
    assert.equal(await inline.evaluate(el => {
      const r = el.getBoundingClientRect()
      return r.top >= 0 && r.left >= 0 && r.right <= innerWidth && r.bottom <= innerHeight
    }), true, 'ap-inline: fully inside the viewport')
    assert.equal(await page.locator('.ap-backdrop, .cd-pop-mask').count(), 0, 'embedded panels must not leave a backdrop behind')
    await page.locator('.sv-nav-item').filter({ hasText: '外观' }).first().click()
    // 关设置：点它自己的 × （.sv-x），别用角色名 —— 弹层里还有别的 title="关闭" 按钮
    await page.locator('.sv-x').first().click()
    await page.locator('.sv-mask').waitFor({ state: 'hidden' })
  }
  check('AI settings / task / GPU panels are visible and clickable at 1600 and 1280px')
  await page.setViewportSize({ width: 1600, height: 1000 })
  await page.locator('.ft-name[title="behaviors"]').click()
  for (const [kind, name] of [['文件', 'regression_' + Date.now() + '.gd'], ['文件夹', 'nested_' + Date.now()]]) {
    await page.getByTitle('新建' + kind + '：behaviors', { exact: true }).click()
    await page.locator('.dg-input').fill(name)
    const created = page.waitForResponse(r => r.url().includes('/api/fs/create') && r.request().method() === 'POST')
    await page.locator('.dg-primary').click()
    assert.equal((await created).status(), 200)
    await page.waitForFunction(() => !document.querySelector('.dg-mask'))
    assert(fs.existsSync(path.join(tree.code_root, 'behaviors', name)))
    assert(!fs.existsSync(path.join(tree.code_root, name)))
  }
  check('new file and folder use selected directory or selected file parent')
  await page.locator('.wb-region-pill').click()
  await page.getByRole('button', { name: '新增分区', exact: true }).click()
  await page.getByLabel('分区名称', { exact: true }).fill('自定义回归区')
  const key = 'custom_' + Date.now()
  await page.getByLabel('唯一标识').fill(key)
  await page.getByLabel('项目内目录').fill('../outside')
  await page.getByRole('button', { name: '确认新增' }).click()
  await page.getByRole('alert').filter({ hasText: '相对路径' }).waitFor()
  await page.getByLabel('项目内目录').fill(key)
  await page.getByRole('button', { name: '确认新增' }).click()
  await page.locator('.dg-actions').getByRole('button', { name: '取消' }).click()
  assert(!fs.existsSync(path.join(tree.code_root, key)))
  await page.getByRole('button', { name: '确认新增' }).click()
  await page.locator('.dg-actions').getByRole('button', { name: '新增分区' }).click()
  await page.locator('.rm-add').waitFor({ state: 'hidden' })
  assert(fs.existsSync(path.join(tree.code_root, key)))
  await page.getByRole('button', { name: '让 AI 规划分区' }).click()
  assert.match(await page.locator('.cd-input').inputValue(), /分区调整方案/)
  check('custom region validation / cancel / create / AI planning entry')
  const fixtureEvents = [
    { id: 'historical', type: 'old_event', timestamp: '2020-01-01T00:00:00Z', source: 'test', data: {} },
    { id: 'undated', type: 'undated_event', source: 'test', data: { hp: 90 } },
  ]
  await page.route('**/api/runtime/events*', route => route.fulfill({ json: { events: fixtureEvents } }))
  // 运行游戏触发钮由 SceneRuntimePanel teleport 进顶栏「更多」菜单槽位（App.vue #wb-sr-slot），
  // 菜单收起时该按钮在 DOM 里但不可见，必须先开菜单再点槽内的那一个。
  await page.locator('.wb-menu-trigger').click()
  await page.locator('.wb-menu-slot-item .sr-trigger').click()
  // .pb-tl-empty 同时是时间线和 Bug 两处空态的类名，必须限定容器
  await page.locator('.pb-tl .pb-tl-empty').waitFor()
  assert.equal(await page.locator('.pb-ev').count(), 0)
  await page.evaluate(() => window.postMessage({ source: 'docmind-runtime', eid: 'fake', type: 'fake' }, location.origin))
  await page.getByLabel('历史事件').check()
  assert.equal(await page.locator('.pb-ev').count(), 2)
  await page.locator('.pb-tabs').getByRole('button', { name: '运行时时间线' }).click()
  await page.getByText('1 条事件缺少有效时间戳', { exact: false }).waitFor()
  assert.equal(await page.locator('.rt-mark').count(), 1)
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出 JSON' }).click()
  const exported = JSON.parse(fs.readFileSync(await (await downloadPromise).path(), 'utf8'))
  assert.equal(exported.events.find(e => e.id === 'undated').timestamp, undefined)
  await page.locator('.pb-x').click()
  check('runtime hides old events, rejects unrelated messages and never invents timestamps')
  empty = true
  await page.evaluate(() => { sessionStorage.clear() })
  await page.goto(base + '/workbench')
  await page.waitForSelector('.ft-row')
  await page.waitForTimeout(200)
  assert.equal(await page.locator('.cd-msg').count(), 0)
  check('fresh session remains empty instead of auto-selecting old history')
  assert.deepEqual(errors, [])
  check('no browser runtime errors')
  const offline = await browser.newPage()
  await offline.route('**/api/**', route => route.fulfill({ status: 503, json: { error: 'offline-test' } }))
  await offline.goto(base + '/workbench')
  await offline.waitForTimeout(500)
  assert.equal(await offline.locator('.wb-demo-banner').count(), 0)
  assert.equal(await offline.locator('.wb-demo-side').count(), 0)
  await offline.goto(base + '/')
  assert.equal(await offline.locator('#demoBanner').isVisible(), false)
  check('backend failure never substitutes demonstration data')
} finally { await browser.close() }
console.log(`${passed} browser regression groups passed`)
