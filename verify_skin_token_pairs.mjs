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
// 把每个 *-ink 强制改成 #fff 之后，各套皮应当有几对不达标。
// paper / graphite / zinc 是固定皮肤，这三个数是**独立真值**（改皮肤配色要同步改这里）。
// seed 不行 —— 它的明暗与色相都是 tools/design_seed.py 按种子现算的：
//   深色种子：四个状态色都是浅色，ink 必须深 → 全改白打掉 5 对
//   浅色种子：accent 够深（生成器保证白字达标）→ 全改白打掉 0 对
// 所以 seed 的期望值用下面 WHITE_BREAKERS 解析算，其余三套用真值表并交叉校验。
const EXPECT_WEAK_MUTATED = { paper: 2, graphite: 5, zinc: 0, seed: 5 }
const FLOOR = 4.5

/** 解析算：哪几对的「填色」承不住白字 —— 把 ink 换成白字时这些对会破。
 *  这是**用皮肤自己的取值算**的，不是跑完变异再回头数（那样期望值就成了变异结果的
 *  同义反复，变异验证就白做了）。三套固定皮拿它和真值表对一次，方法本身才算被验证过。 */
const WHITE_BREAKERS = () => {
  const probe = document.createElement('span')
  document.body.appendChild(probe)
  const parse = v => { const m = /rgba?\(([^)]+)\)/.exec(v); if (!m) return null
    const q = m[1].split(/[\s,\/]+/).filter(x => x !== '').map(Number)
    return { rgb: [q[0], q[1], q[2]], a: q[3] === undefined ? 1 : q[3] } }
  const lum = c => { const f = x => { x /= 255; return x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4 }
    return .2126 * f(c[0]) + .7152 * f(c[1]) + .0722 * f(c[2]) }
  const ratio = (a, b) => { const L1 = lum(a.rgb), L2 = lum(b.rgb)
    return (Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05) }
  const resolve = t => { probe.style.color = '#000'; probe.style.color = `var(${t})`
    return parse(getComputedStyle(probe).color) }
  const white = { rgb: [255, 255, 255] }
  let n = 0
  // 4.5 内联：整个函数会被序列化成字符串塞进页面执行，引用不了外层的 FLOOR 常量
  for (const t of ['--accent', '--green', '--amber', '--accent-2', '--danger']) {
    const f = resolve(t)
    if (f && ratio(f, white) < 4.5) n++
  }
  probe.remove()
  return n
}

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

/** 遮罩方向不变量：遮罩必须让底**沉下去**，而且要有可感幅度。
 *  这条存在的原因：全仓 23 处遮罩以前各写各的"海军蓝"，压在 paper 上是 −53% 亮度，
 *  压在 graphite（#16181d）上却是 **+34% 变亮** —— 深色皮肤里打开弹层，
 *  背景发灰发白而不是变暗，而像素基线没有一条用例开着弹层，所以没人发现。 */
const SCRIMS = () => {
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
  for (const t of ['--scrim', '--scrim-soft']) {
    const raw = (rs.getPropertyValue(t) || '').trim()
    if (!raw) { bad.push(`${t} 没定义`); continue }
    const c = parse(resolve(raw))
    if (!c) { bad.push(`${t} 解不出颜色：${raw}`); continue }
    const over = [0, 1, 2].map(k => c.rgb[k] * c.a + bg.rgb[k] * (1 - c.a))
    const drop = (lum(over) / lum(bg.rgb) - 1) * 100
    if (drop > -8) bad.push(`${t} 在 --bg 上${drop > 0 ? '反而把底提亮' : '只压暗'} ${Math.abs(drop).toFixed(0)}%（要求至少压暗 8%）`)
  }
  probe.remove()
  return bad
}

// 已知未修的死引用清单。列在这里不是放过它们，是让门禁只对**新增**的死引用变红 ——
// 一上来就红的门禁会被无视。每条都写了"该由谁定"。
//
// 2026-10-20：清单已清空 —— 原来这 4 条都定掉了：
//   --font-sans  问答页 19 处（landing 页有一套，问答页这套 :root 漏了）→ ask.css 别名到 --font-ui
//   --text-dim   对话台/连接弹层 5 处 → style.css 别名到 --text-muted
//   --font-base  场景运行台空态 1 处 → style.css 别名到 --font-ui
//   --amber-deep 脏状态徽标等 7 处 → style.css 别名到 --amber（惰性替换，随皮肤走）
// 现在任何新的死引用都会直接让门禁变红。
const KNOWN_DEAD_TOKENS = {
}

/** 控件边界档：--border-graphic 对 --bg-raised 必须 ≥ 3:1。
 *  这是 WCAG 1.4.11 真正管的判据 —— 1.4.11 要求 UI 控件的**视觉边界**
 *  （输入框、复选框、下拉）与相邻底色有 3:1 对比度；而纯装饰的图形对象
 *  （容器外框、列表分隔线）明确豁免（原文：purely decorative are exempt）。
 *  仓库里原来把"描边 3:1"当成了不可退让的审美下限，是把标准用错了地方 ——
 *  主流产品全都低于 3:1：Primer border.default 1.45:1、Linear 5%~8% 白 1.15~1.26。
 *  改成三档后，3:1 落到该落的地方：容器/分隔淡到 1.35:1，控件边界守住 3:1。
 *
 *  注意：这个函数会被序列化进页面执行，不能引用外层常量 —— 3 只能内联。 */
const CONTROL_BORDER = () => {
  const probe = document.createElement('span')
  document.body.appendChild(probe)
  const parse = v => { const m = /rgba?\(([^)]+)\)/.exec(v); if (!m) return null
    const q = m[1].split(/[\s,\/]+/).filter(x => x !== '').map(Number)
    return { rgb: [q[0], q[1], q[2]], a: q[3] === undefined ? 1 : q[3] } }
  const lum = c => { const f = x => { x /= 255; return x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4 }
    return .2126 * f(c[0]) + .7152 * f(c[1]) + .0722 * f(c[2]) }
  const resolve = t => { probe.style.color = '#000'; probe.style.color = `var(${t})`
    return parse(getComputedStyle(probe).color) }
  const f = resolve('--border-graphic'), b = resolve('--bg-raised')
  probe.remove()
  if (!f || !b) return null
  const L1 = lum(f.rgb), L2 = lum(b.rgb)
  return +(((Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05)).toFixed(2))
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
    let whiteBreakers = 0
    for (const mutate of [false, true]) {
      let hits = 0
      const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
      // 2026-10-20：补写锁定标记。
// skins.ts 现在把「默认皮肤」从 paper 迁到 zinc，迁移判据是"存的是旧默认 paper 且
// 从没主动选过"（localStorage 没有 docmind.skinLocked）。门禁是**显式指定**要测哪套皮肤，
// 必须同时写锁定标记，否则 paper 用例会被迁移成 zinc，waitForFunction 干等到超时。
await page.addInitScript(`localStorage.setItem("docmind.skin","${skin}");localStorage.setItem("docmind.skinLocked","1")`)
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
        // 控件边界档：1.4.11 的 3:1 真正落在这里（容器/分隔线是装饰，豁免）。
        const cb = await page.evaluate(CONTROL_BORDER)
        ok(`${skin} 控件边界 --border-graphic 对 --bg-raised ≥ 3:1`, cb !== null && cb >= 3,
          cb === null ? '令牌没解析出颜色' : `${cb}:1`)
        // 解析期望值：哪几对的填色承不住白字。三套固定皮拿它和真值表对一次 ——
        // 对得上，说明这个解析方法本身可信；seed 是生成皮肤，只能用它。
        whiteBreakers = await page.evaluate(WHITE_BREAKERS)
        if (skin !== 'seed') {
          ok(`${skin} 解析期望值与固定真值一致（${EXPECT_WEAK_MUTATED[skin]}）`,
            whiteBreakers === EXPECT_WEAK_MUTATED[skin],
            `解析算得 ${whiteBreakers} · 真值 ${EXPECT_WEAK_MUTATED[skin]}`)
        } else {
          console.log(`      · seed 皮肤的期望值按其自身色值解析算得 ${whiteBreakers}（明暗由种子决定，不写死）`)
        }
        const sh = await page.evaluate(SHADOWS)
        ok(`${skin} 硬投影都比自己落的底暗`, sh.length === 0, sh.join(' · '))
        // 变异：把模态阴影改回"取 --ink"。只有深色皮肤应当变红 ——
        // paper / zinc 的 --ink 本来就比底暗，它们不红才证明这条判据在判方向而不是在报"浅色"。
        await page.evaluate(() => document.documentElement.style
          .setProperty('--modal-shadow', '8px 8px 0 var(--ink)'))
        const shM = await page.evaluate(SHADOWS)
        // 是否该红：取决于**这套皮肤是不是深色**（深色皮肤里 --ink 是浅色，
        // 拿它当投影色会在深底上提亮）。原先写死 `skin === 'graphite'`，
        // 种子皮肤（也是深色）会被误判成"浅底"→ 期望值反了 → 门禁误报。
        // 改成读皮肤块自己声明的 --skin-dark：新增深色皮肤不用改这里。
        const isDark = await page.evaluate(() =>
          getComputedStyle(document.documentElement).getPropertyValue('--skin-dark').trim() === '1')
        const shouldRed = isDark
        ok(`${skin} 变异（模态阴影改回取 --ink）：${shouldRed ? '应当红' : '不该红'}`,
          shM.length > 0 === shouldRed, shouldRed ? shM.join(' · ') : '未报（正确）')
        await page.evaluate(() => document.documentElement.style.removeProperty('--modal-shadow'))
        const sc = await page.evaluate(SCRIMS)
        ok(`${skin} 遮罩把底压暗了`, sc.length === 0, sc.join(' · '))
        // 变异：把遮罩换回原来那族"海军蓝"。同样只该在 graphite 红 ——
        // 蓝压在浅底上仍然变暗（所以浅皮不红是对的），压在深底上会提亮。
        await page.evaluate(() => document.documentElement.style
          .setProperty('--scrim', 'rgba(27, 39, 61, .34)'))
        const scM = await page.evaluate(SCRIMS)
        // 遮罩变异同理：海军蓝压在深底上会提亮 → 深色皮肤该红，浅色皮不红。
    const scRed = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue('--skin-dark').trim() === '1')
        ok(`${skin} 变异（遮罩换回海军蓝）：${scRed ? '应当红' : '不该红'}`,
          scM.length > 0 === scRed, scRed ? scM.join(' · ') : '未报（正确）')
        await page.evaluate(() => document.documentElement.style.removeProperty('--scrim'))
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
        // 期望值：固定皮用真值表（已在非变异轮交叉校验过解析算法），种子用解析算出的值。
        const expect = whiteBreakers
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
