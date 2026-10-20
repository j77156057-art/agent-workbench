// 容量边界的行为验收：界面上的每个数字都来自 GET /api/agent-capacity 的 ranges，
// 且「填了正数却跑不完一轮」在**发请求之前**就被拦下。
//
// 为什么单独一个脚本：后端下发 ranges 之后，「两边数字相等」这件事已经不是风险点
// （只有一份数字了），真正的风险变成了**界面有没有把载荷里的东西用起来**——
// 上界挂没挂到 input、下界有没有说清、下界违规是不是走的是本地拦截而不是
// 服务端 400。这三件事都只有渲染出来才看得见。
//
// 需要夹具：.venv/Scripts/python.exe verify_scene_canvas.py --serve 8011
// （或设 DOCMIND_UI_TEST_URL）。夹具隔离了 code_root 与 STATE_FILE，写设置不会碰到真实状态。
import { createRequire } from 'node:module'
import fs from 'node:fs'
import path from 'node:path'
import assert from 'node:assert/strict'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE, '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))
const base = process.env.DOCMIND_UI_TEST_URL || 'http://127.0.0.1:8011'
try {
  const r = await fetch(base + '/api/health', { signal: AbortSignal.timeout(4000) })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
} catch (e) {
  console.error(`夹具没起来：${base} 不可用（${e.message}）\n`
    + '  先跑：.venv/Scripts/python.exe verify_scene_canvas.py --serve 8011')
  process.exit(2)
}

const payload = await (await fetch(base + '/api/agent-capacity', { signal: AbortSignal.timeout(15000) })).json()
const RANGES = payload.ranges
assert.ok(RANGES && typeof RANGES === 'object', '夹具没有下发 ranges —— 这条脚本要验的东西不存在')
const FLOOR = Number(RANGES.task_token_budget_min)

// 界面上的字段名（label 文本前缀）→ 载荷键。用文本选而不是顺序选：面板里还有别的
// input，按 nth 匹配会在别人往中间插一项时静默错位。
const FIELD_BY_LABEL = [
  ['最多 Agent 数', 'max_agents'],
  ['主 Agent 工具步数', 'main_max_steps'],
  ['每个子 Agent 工具步数', 'child_max_steps'],
  ['任务 Token 上限', 'task_token_budget'],
  ['自动纠偏次数', 'max_nudges'],
  ['最终续写次数', 'max_final_continuations'],
]

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const jsErrors = []
page.on('pageerror', e => jsErrors.push('pageerror: ' + e.message))
// ⚠ @click 里 await 的 async 函数抛错**不会**进 pageerror（实测踩过：保存按钮点了没反应，
// 界面一片安静）。必须自己把 unhandledrejection 送到 Node 侧。
page.on('console', m => { if (m.type() === 'error') jsErrors.push('console: ' + m.text()) })
await page.addInitScript(() => {
  window.addEventListener('unhandledrejection', e => {
    console.error('unhandledrejection: ' + (e.reason && e.reason.message || e.reason))
  })
})
let posts = 0
page.on('request', r => { if (r.method() === 'POST' && r.url().endsWith('/api/agent-capacity')) posts++ })

let ran = 0
let failed = 0
let crashed = ''
const ok = (name, cond, detail = '') => {
  ran++
  if (!cond) failed++
  console.log(`${cond ? 'PASS' : 'FAIL'}   ${name}${detail ? '  ' + detail : ''}`)
}

try {
  await page.goto(base + '/workbench', { waitUntil: 'load', timeout: 45000 })
  await page.click('.wb-settings-btn')
  await page.getByRole('button', { name: '智能体', exact: true }).first().click()
  // 等到**载荷真的落地**再量。这里不能等 input：容量 input 一直是渲染着的，只是
  // max 还没挂上（capacityMax 在载荷到之前返回 0 → 不设 max）。实测第一版就是这么
  // 误判成「界面没消费 ranges」——六个 max 全 null，而 6 秒后它们全是对的。
  // .sv-note 是 v-if="capacityInfo"，所以它出现 = GET 回来并赋值了。
  // 夹具首针 /api/agent-capacity 要探显存与模型状态，冷启动实测 5–6 秒。
  await page.waitForSelector('section.sv-panel:visible .sv-note', { timeout: 90000 })

  // 每个字段读回 { label, max, min, step, sub }（sub = 字段下方的约束说明）
  const fields = await page.$$eval('label.sv-field', els => els.map(el => {
    const inp = el.querySelector('input[type=number]')
    if (!inp) return null
    const spans = [...el.querySelectorAll('span')]
    return {
      label: (spans[0]?.textContent || '').trim(),
      max: inp.getAttribute('max'),
      min: inp.getAttribute('min'),
      sub: (spans[1]?.textContent || '').trim(),
    }
  }).filter(Boolean))

  const find = key => {
    const prefix = FIELD_BY_LABEL.find(([, k]) => k === key)?.[0]
    return fields.find(f => f.label.startsWith(prefix))
  }
  for (const [, key] of FIELD_BY_LABEL) {
    const f = find(key)
    ok(`${key}：input.max === 载荷的 ${key}`, !!f && f.max === String(RANGES[key]),
      `界面 ${f?.max} / 载荷 ${RANGES[key]}`)
  }

  // 下界：0 是合法的「不限」，所以 min 属性必须留在 0 —— 把 min 做成下界会让填 0 变非法。
  const tok = find('task_token_budget')
  ok('task_token_budget：min 仍是 0（填 0 = 不限必须能输进去）', !!tok && tok.min === '0', `min=${tok?.min}`)
  if (!Number.isFinite(FLOOR) || FLOOR <= 0) {
    console.log('SKIP   载荷没有 task_token_budget_min —— 老后端，下界三项不判')
  } else {
    ok('task_token_budget：字段下方说明了「至少 FLOOR」', tok?.sub.includes(String(FLOOR)),
      `实测文案：${tok?.sub || '（无说明）'}`)
    const note = await page.locator('section.sv-panel:visible .sv-note').first().innerText()
    ok('下界也进了「服务端下发」那块说明', note.includes(String(FLOOR)))
    // 单源守卫：脚本里的数字全来自载荷；界面源码里若出现这个字面量，就是有人又抄了一份。
    const vue = fs.readFileSync(path.resolve('frontend/src/workbench/components/SettingsView.vue'), 'utf8')
    ok('SettingsView.vue 里没有把下界写成字面量', !vue.includes(String(FLOOR)),
      `FLOOR=${FLOOR}`)

    // 行为：填 floor-1 必须**本地**拦下（一次 POST 都不发）。后端对 0<v<floor 是
    // raise → 400，所以放过去只会变成一句服务端报错，用户等一圈才知道。
    // 各面板都是 v-show（隐藏了但仍在 DOM 里），所以选 input / 报错一律限定在当前可见面板。
    const panel = page.locator('section.sv-panel:visible')
    const others = { '最多 Agent 数': '2', '主 Agent 工具步数': '8', '每个子 Agent 工具步数': '8', '自动纠偏次数': '2', '最终续写次数': '2' }
    for (const [text, val] of Object.entries(others)) {
      await panel.locator('label.sv-field').filter({ hasText: text }).first().locator('input').fill(val)
    }
    const tokInput = panel.locator('label.sv-field').filter({ hasText: '任务 Token 上限' }).first().locator('input')
    await tokInput.fill(String(FLOOR - 1))
    const before = posts
    await page.getByRole('button', { name: '保存工作负载设置' }).click()
    await page.waitForTimeout(400)
    const err = await panel.locator('.sv-err').first().innerText().catch(() => '')
    ok(`填 ${FLOOR - 1} 当场报错并点名下界`, err.includes(String(FLOOR)), `报错：${err.slice(0, 60) || '（空）'}`)
    ok('填低于下界时没有发出 POST', posts === before, `POST ${posts - before} 次`)

    // 边界值本身必须放行：off-by-one 把 floor 也拦掉是另一种难查的坏法。
    await tokInput.fill(String(FLOOR))
    await page.getByRole('button', { name: '保存工作负载设置' }).click()
    await panel.locator('.sv-ok, .sv-err').first().waitFor({ timeout: 15000 }).catch(() => {})
    const done = await panel.locator('.sv-ok').first().innerText().catch(() => '')
    const err2 = await panel.locator('.sv-err').first().innerText().catch(() => '')
    ok(`填 ${FLOOR} 放行并保存成功`, posts > before && done.includes('已保存'),
      `POST ${posts - before} 次 · ${done || err2}`)
  }
  ok('全程没有 JS 报错 / 未处理的 Promise 拒绝', jsErrors.length === 0, jsErrors.slice(0, 2).join(' | '))

  // 变异验证：把载荷里的下界**换成另一个数**再看界面。
  // 上面几条只证明「界面显示的等于当前载荷」，而 18906 恰好也可能被我抄成字面量
  // （两边同值时相等是恒真的）。改一个值还跟着变，才证明是读的不是抄的。
  if (Number.isFinite(FLOOR)) {
    const FAKE = 42000
    const page2 = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    await page2.route('**/api/agent-capacity', async route => {
      const res = await route.fetch()
      const body = await res.json()
      body.ranges.task_token_budget_min = FAKE
      await route.fulfill({ response: res, body: JSON.stringify(body), headers: { ...res.headers(), 'content-type': 'application/json' } })
    })
    await page2.goto(base + '/workbench', { waitUntil: 'load', timeout: 45000 })
    await page2.click('.wb-settings-btn')
    await page2.getByRole('button', { name: '智能体', exact: true }).first().click()
    await page2.waitForSelector('section.sv-panel:visible .sv-note', { timeout: 90000 })
    const sub = await page2.locator('section.sv-panel:visible label.sv-field')
      .filter({ hasText: '任务 Token 上限' }).first().locator('.sv-field-sub').innerText().catch(() => '')
    ok('把载荷下界改成 42000 后界面跟着变（证明不是写死的）', sub.includes(String(FAKE)), `实测文案：${sub}`)
    const tokInput2 = page2.locator('section.sv-panel:visible label.sv-field')
      .filter({ hasText: '任务 Token 上限' }).first().locator('input')
    await tokInput2.fill(String(FAKE - 1))
    await page2.getByRole('button', { name: '保存工作负载设置' }).click()
    await page2.waitForTimeout(400)
    const err2 = await page2.locator('section.sv-panel:visible .sv-err').first().innerText().catch(() => '')
    ok('拦截线跟着载荷走：41999 被拦、报错写的是 42000', err2.includes(String(FAKE)), `报错：${err2.slice(0, 60)}`)
    await page2.close()
  }

  // 对比度：新加的那行是 11px 小字，挂在 --text-muted 上。三套皮肤各量一次，
  // 因为同一个 token 在三种底色上的比值差得很远（graphite 是深底）。
  for (const skin of ['paper', 'graphite', 'zinc']) {
    const ps = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    await ps.addInitScript(`localStorage.setItem("docmind.skin","${skin}");localStorage.setItem("docmind.skinLocked","1")`)
    await ps.goto(base + '/workbench', { waitUntil: 'load', timeout: 45000 })
    await ps.click('.wb-settings-btn')
    await ps.getByRole('button', { name: '智能体', exact: true }).first().click()
    await ps.waitForSelector('section.sv-panel:visible .sv-field-sub', { timeout: 90000 })
    const ratio = await ps.evaluate(() => {
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
      // 页面侧没有 :visible 这个伪类（那是 Playwright 的），这里用普通选择器：
      // .sv-field-sub 全文件只有这一处。
      const el = document.querySelector('.sv-field-sub')
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
    })
    // 11px 属于普通字号正文，AA 门槛 4.5:1
    ok(`${skin}：下界说明文字对比度 ≥ 4.5:1`, ratio >= 4.5, `实测 ${ratio}:1`)
    await ps.close()
  }
} catch (e) {
  // ⚠ 抛出的一整套都必须算失败。第一版就是这么红的：waitForSelector 超时后
  // 一项都没判，而汇总只看 failed===0，于是打印「全部通过」。
  crashed = (e && e.message ? e.message : String(e)).split('\n')[0]
} finally {
  await browser.close()
  const verdict = crashed ? `中途抛错：${crashed}`
    : ran === 0 ? '一项都没判（选择器失效，判据恒真）'
      : failed ? `${failed} 项失败` : `${ran} 项全部通过`
  console.log(`\n容量边界：${verdict}`)
}
process.exit(crashed || failed || ran === 0 ? 1 : 0)
