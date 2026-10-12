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
  ['--accent-2', '--accent-2-ink'], ['--danger', '--danger-ink'], ['--ink', '--bg'],
  // --accent-text 是「压在表面上的强调字」，它自己也得在每个面上过 4.5 ——
  // paper 原来取 color-mix(accent 62%, ink)，压在 --bg-hover 上只有 4.34，
  // 而这条当时不存在，所以没人发现。加进来之后它必须逐面判。
  ['--accent-text', '--bg'], ['--accent-text', '--bg-raised'], ['--accent-text', '--bg-hover']]
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

/** 死令牌引用：用了某个 var(--x) 但没有任何一张表定义过 --x。
 *  为什么需要：`--accent-text / --bg-input` 这种「我以为存在的契约」根本测不准 ——
 *  实测 --bg-input 从未被任何皮肤定义过，两处引用让设置页导航的 hover 与选中底
 *  一直是透明的。静态扫引用比人工配对可靠。 */
const DEAD_TOKENS = () => {
  const defined = new Set(), used = new Map()
  let skipped = 0
  for (const sheet of document.styleSheets) {
    let rules = null
    try { rules = sheet.cssRules } catch { skipped++; continue }
    const walk = list => { for (const r of list) {
      if (r.cssRules && r.cssRules.length) { walk(r.cssRules); continue }
      const txt = r.cssText || ''
      for (const m of txt.matchAll(/(--[\w-]+)\s*:/g)) defined.add(m[1])
      for (const m of txt.matchAll(/var\(\s*(--[\w-]+)/g))
        if (!used.has(m[1])) used.set(m[1], (r.selectorText || '?').slice(0, 44))
    } }
    if (rules) walk(rules)
  }
  for (const el of document.querySelectorAll('[style]'))
    for (const m of (el.getAttribute('style') || '').matchAll(/var\(\s*(--[\w-]+)/g))
      if (!used.has(m[1])) used.set(m[1], 'inline style')
  return { dead: [...used.entries()].filter(([t]) => !defined.has(t))
      .map(([t, where]) => `${t} ← ${where}`),
    定义: defined.size, 引用: used.size, 读不到的表: skipped }
}

/** 硬投影方向不变量：阴影是遮挡，必须比它落的底**更暗**。
 *  为什么单独一条：graphite 的弹层阴影以前取的是 var(--ink)（浅色），于是深色皮肤里
 *  每个模态带一圈 8px 白边 —— 像素基线看不见弹层、文字棘轮只读 color，
 *  这条债当时没有任何门禁能报出来。 */
const SHADOWS = () => {
  const probe = document.createElement('span')
  document.body.appendChild(probe)
  const resolve = v => { probe.style.color = '#000'; probe.style.color = v; return getComputedStyle(probe).color }
  const parse = v => { const m = /^rgba?\(([^)]+)\)/.exec(v || ''); if (!m) return null
    const q = m[1].split(/[\s,\/]+/).filter(x => x !== '').map(Number)
    return { rgb: [q[0], q[1], q[2]], a: q[3] === undefined ? 1 : q[3] } }
  const lum = c => { const f = x => { x /= 255; return x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4 }
    return .2126 * f(c[0]) + .7152 * f(c[1]) + .0722 * f(c[2]) }
  const rs = getComputedStyle(document.documentElement)
  const bg = parse(resolve((rs.getPropertyValue('--bg') || '#fff').trim()))
  const bad = []
  for (const t of ['--modal-shadow', '--shadow-pop', '--shadow-soft',
    '--sh-hard', '--sh-hard-sm', '--sh-hard-lg']) {
    const raw = (rs.getPropertyValue(t) || '').trim()
    if (!raw || raw === 'none') continue
    for (const part of raw.split(/,(?![^(]*\))/)) {
      const colM = part.match(/rgba?\([^)]*\)|#[0-9a-fA-F]{3,8}|var\([^)]*\)/)
      if (!colM) continue
      const nums = part.replace(colM[0], ' ').trim().split(/\s+/).map(parseFloat).filter(n => !isNaN(n))
      if ((nums[2] || 0) > 0 || !((nums[0] || 0) || (nums[1] || 0))) continue   // 只判硬投影
      const c = parse(resolve(colM[0]))
      if (c && lum(c.rgb) > lum(bg.rgb))
        bad.push(`${t} = ${colM[0]} 比 --bg 还亮（${lum(c.rgb).toFixed(2)} > ${lum(bg.rgb).toFixed(2)}）`)
    }
  }
  probe.remove()
  return bad
}

// 已知未修的死引用：4 个从未在任何皮肤里定义的令牌，共 36 处引用。
// 列在这里不是放过它们，是让门禁只对**新增**的死引用变红 —— 一上来就红的门禁会被无视。
// 每条都写了"该由谁定"：--amber-deep 要逐皮解算（深色皮下它不能是同一个棕），
// 其余三个是纯别名，定一次即可。
const KNOWN_DEAD_TOKENS = {
  '--font-sans': '21 处引用（问答页为主）。从未定义 → 字体回落继承值。该定成 var(--font-ui)',
  '--text-dim': '7 处引用（对话台/开发舱/连接弹层）。从未定义 → 字色回落继承值，'
    + '所以这些行"碰巧"看得清，但没有任何一处跟着皮肤走',
  '--amber-deep': '7 处引用（脏状态徽标/状态点）。从未定义 → 回落继承色。'
    + '定值要逐皮肤解算：深色皮下不能沿用浅色皮那个棕',
  '--font-base': '1 处引用（SceneRuntimePanel .pb-tl-empty）。从未定义',
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
        const sh = await page.evaluate(SHADOWS)
        ok(`${skin} 硬投影都比自己落的底暗`, sh.length === 0, sh.join(' · '))
        // 变异：把模态阴影改回"取 --ink"。只有深色皮肤应当变红 ——
        // paper / zinc 的 --ink 本来就比底暗，它们不红才证明这条判据在判方向而不是在报"浅色"。
        await page.evaluate(() => document.documentElement.style
          .setProperty('--modal-shadow', '8px 8px 0 var(--ink)'))
        const shM = await page.evaluate(SHADOWS)
        const shouldRed = skin === 'graphite'
        ok(`${skin} 变异（模态阴影改回取 --ink）：${shouldRed ? '应当红' : '不该红'}`,
          shM.length > 0 === shouldRed, shouldRed ? shM.join(' · ') : '未报（正确）')
        await page.evaluate(() => document.documentElement.style.removeProperty('--modal-shadow'))
        const dt = await page.evaluate(DEAD_TOKENS)
        const names = dt.dead.map(d => d.split(' ')[0])
        const fresh = names.filter(n => !(n in KNOWN_DEAD_TOKENS))
        ok(`${skin} 没有新增未定义的令牌引用`, fresh.length === 0 && dt.读不到的表 === 0,
          `定义 ${dt.定义} / 引用 ${dt.引用} · 命中已知清单 ${names.length - fresh.length} 条`
          + (fresh.length ? ` · 新增死引用：${fresh.join(' ')}` : '')
          + (dt.读不到的表 ? ` · 有 ${dt.读不到的表} 张表读不到，判据不完整` : ''))
        const gone = Object.keys(KNOWN_DEAD_TOKENS).filter(t => !names.includes(t))
        if (gone.length) console.log(`      ⚠ 已知清单里的 ${gone.join(' ')} 这次没出现 —— 修好了就把那条删掉`)
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
