// 皮肤令牌对门禁 + 变异验证：证「填色/文字成对令牌」在每套皮下都达 AA，
// 并且**用变异证明这条门禁真的会红**（三套皮的预期各不相同）。
//
// 为什么需要它：视觉基线只覆盖默认视图，而皮肤泄漏大多发生在弹层和子页
// （实测 14 处白字压 --accent 时，五个基线用例全是 0 像素变化 —— 像素门禁
// 对这批改动是瞎的）。令牌对是这一类问题唯一能穷尽的判据层。
//
// 三套皮的预期**不同**，这才是有效对照：
//   paper    --green/--amber/--danger 的 ink 本就是白（不该红）；
//            --accent-ink 是 #000、--accent-2-ink 是 var(--ink) → 期望掉 2 对
//   graphite 四档状态色全是浅色，ink 一律 var(--bg)，且 --accent-ink 是 #000
//            → 期望掉 5 对
//   zinc     所有 *-ink 本来就是白 → 期望掉 0 对
//            （若这里也红，说明门禁在乱报而不是在判据）
//
// 需要夹具：.venv/Scripts/python.exe verify_scene_canvas.py --serve 8011
// （或设 DOCMIND_UI_TEST_URL）。本脚本只读，不写任何文件。
import { createRequire } from 'node:module'
import path from 'node:path'
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

const PAIRS = [['--accent', '--accent-ink'], ['--green', '--green-ink'], ['--amber', '--amber-ink'],
  ['--accent-2', '--accent-2-ink'], ['--danger', '--danger-ink'], ['--ink', '--bg']]
// 把每个 *-ink 强制改成 #fff 之后，各套皮应当有几对不达标
const EXPECT_WEAK_MUTATED = { paper: 2, graphite: 5, zinc: 0 }
const FLOOR = 4.5

const MEASURE = ps => {
  // 内联 var() 解析：作用域样式让外部探针拿不到类，而 var() 不需要类就能解出
  // 当前皮肤下的真实颜色。元素必须在文档里，否则 getComputedStyle 解不了 var()。
  const probe = document.createElement('span')
  document.body.appendChild(probe)
  const resolve = t => { probe.style.color = `var(${t})`; return getComputedStyle(probe).color }
  const parse = v => { const m = /rgba?\(([^)]+)\)/.exec(v); if (!m) return null
    const q = m[1].split(/[\s,\/]+/).filter(x => x !== '').map(Number)
    return { rgb: [q[0], q[1], q[2]], a: q[3] === undefined ? 1 : q[3] } }
  const lum = c => { const f = x => { x /= 255; return x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4 }
    return .2126 * f(c[0]) + .7152 * f(c[1]) + .0722 * f(c[2]) }
  const out = ps.map(([fill, ink]) => {
    const f = resolve(fill), i = resolve(ink)
    const fb = parse(f), ib = parse(i)
    if (!fb || !ib) return { 对: `${fill}/${ink}`, 值: null, 原因: '令牌没解析出颜色' }
    const L1 = lum(ib.rgb), L2 = lum(fb.rgb)
    return { 对: fill.replace(/^--/, ''), 值: +(((Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05)).toFixed(2)) }
  })
  probe.remove()
  return out
}

const browser = await chromium.launch({ channel: 'msedge', headless: true })
let ran = 0, failed = 0, crashed = ''
const ok = (name, cond, detail = '') => {
  ran++
  if (!cond) failed++
  console.log(`${cond ? 'PASS' : 'FAIL'}   ${name}${detail ? '  ' + detail : ''}`)
}

try {
  for (const skin of Object.keys(EXPECT_WEAK_MUTATED)) {
    for (const mutate of [false, true]) {
      let hits = 0
      const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
      await page.addInitScript(`localStorage.setItem("docmind.skin","${skin}")`)
      if (mutate) {
        // 生产构建里 skins.css 被打进哈希名 chunk —— 按文件名路由会一处都改不到，
        // 于是"变异前后数字完全相同"被当成通过。所以扫所有 CSS 并**数命中次数**。
        await page.route('**/*.css', async route => {
          const res = await route.fetch()
          const css = await res.text()
          const next = css.replace(
            /--([a-z0-9-]*?)-ink:\s*(#[0-9a-fA-F]{3,8}|var\(--bg\)|var\(--ink\)|var\(--accent-ink\))/g,
            (_m, k) => { hits++; return `--${k}-ink: #ffffff` })
          await route.fulfill({ response: res, body: next, headers: { ...res.headers(), 'content-type': 'text/css' } })
        })
      }
      await page.goto(base + '/workbench', { waitUntil: 'domcontentloaded', timeout: 60000 })
      // 等皮肤令牌真的落到 <html> 上再量：applyStoredSkin 在 mount 前跑，但首帧
      // 仍可能读到 default 档，那会让三套皮的数字看起来一样。
      await page.waitForSelector('.ft-row', { timeout: 60000 })
      await page.waitForFunction(s => document.documentElement.dataset.skin === s, skin, { timeout: 30000 })
      await page.waitForTimeout(400)
      const rows = await page.evaluate(MEASURE, PAIRS)
      const weak = rows.filter(r => r.值 === null || r.值 < FLOOR)
      const detail = rows.map(r => `${r.对}=${r.值 ?? '解析失败'}`).join('  ')
      if (!mutate) {
        ok(`${skin} 现状：${PAIRS.length} 对全部 ≥ ${FLOOR}:1`, weak.length === 0,
          weak.length ? `不达标：${weak.map(w => `${w.对}=${w.值}`).join(' ')}` : detail)
      } else {
        const expect = EXPECT_WEAK_MUTATED[skin]
        ok(`${skin} 变异（ink 全改白）：应掉 ${expect} 对`,
          hits > 0 && weak.length === expect,
          `命中 ${hits} 处改写 · 实掉 ${weak.length} 对 · ${detail}`)
        if (hits === 0) console.log('      ⚠ 一处都没改到 —— 这次变异是空跑，不能算验证')
      }
      await page.close()
    }
  }
} catch (e) {
  crashed = (e && e.message ? e.message : String(e)).split('\n')[0]
} finally {
  await browser.close()
  const verdict = crashed ? `中途抛错：${crashed}`
    : ran === 0 ? '一项都没判（判据恒真）'
      : failed ? `${failed} 项失败` : `${ran} 项全部通过`
  console.log(`\n皮肤令牌对：${verdict}`)
}
process.exit(crashed || failed || ran === 0 ? 1 : 0)
