// 视觉回归：把关键界面截成基线图，之后每次比对像素差。
//
// 为什么现在才做得动：字体原先走 CDN 且间歇不可达，同一份构建两次能截出两种墨量，
// 任何像素比对都会天天报假警。字体自托管（见 frontend/src/fonts.ts）之后渲染才确定。
//
// 依赖：无新增 npm 包 —— PNG 解码与逐像素比对交给浏览器 canvas 做（复用同一个浏览器）。
// 用法：
//   node verify_visual_regression.mjs              比对，发现差异则退出码 1
//   node verify_visual_regression.mjs --update     重新生成基线
// 需要 verify_scene_canvas.py --serve 8011 提供有数据的夹具。
import { createRequire } from 'node:module'
import fs from 'node:fs'
import path from 'node:path'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || path.join(process.env.USERPROFILE, '.workbuddy/binaries/node/workspace/node_modules/playwright-core'))

const BASE = process.env.DOCMIND_UI_TEST_URL || 'http://127.0.0.1:8011'
const DIR = 'docs/screenshots/visual'
const UPDATE = process.argv.includes('--update')
// 阈值来自实测，不是拍的：
//   干净态噪声底（同一构建连跑，eps=4）最多 7 个像素，eps=2/8 为 0；
//   而一次"很小的真实改动"（把 paper 的 --accent 从 #ff674d 改成 #ff7755）
//   在 eps=4 下动了 2215 个像素（workbench）/ 30426 个（ask）。
//   信噪差约 300 倍，所以绝对下限取 200 像素：离噪声有 28 倍余量，又远小于信号。
// eps 取 4 而不是 16 —— 实测 eps=16 时那次品牌色改动被判成 0 像素，等于漏报。
const CHANNEL_EPS = 4
const MAX_CHANGED_ABS = 200
// 兜底：大面积但每像素都只变一点点的情况。
const MAX_CHANGED_PCT = 0.4
// 基线与截图机器相关（文字光栅化在 GPU / 软件渲染下会有差异）。换机器首次运行
// 若整片报红，用 --update 在新机器上重建基线，别去调阈值。

const CASES = [
  { name: 'workbench-paper', route: '/workbench', skin: 'paper', stable: '.ft-head-title' },
  { name: 'workbench-graphite', route: '/workbench', skin: 'graphite', stable: '.ft-head-title' },
  { name: 'ask', route: '/', skin: 'paper', stable: '.composer textarea' },
]

// 关掉动效与光标闪烁，否则同一状态两次截图就会差几像素。
const STABILIZE = () => {
  const s = document.createElement('style')
  s.textContent = '*,*::before,*::after{animation:none !important;transition:none !important;'
    + 'caret-color:transparent !important}'
  document.head.appendChild(s)
}

async function shoot(browser, c) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
  await page.addInitScript(`localStorage.setItem("docmind.skin","${c.skin}")`)
  await page.goto(BASE + c.route, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(c.stable, { timeout: 25000 })
  await page.waitForTimeout(1500)
  await page.evaluate(STABILIZE)
  await page.waitForTimeout(400)
  const buf = await page.screenshot({ type: 'png' })
  await page.close()
  return buf
}

// 交给浏览器解码 PNG 并逐像素比：省掉一个 npm 依赖。
async function diff(browser, aBuf, bBuf) {
  const page = await browser.newPage()
  await page.goto('about:blank')
  const out = await page.evaluate(async ([a, b, eps]) => {
    const load = src => new Promise((res, rej) => {
      const im = new Image()
      im.onload = () => res(im)
      im.onerror = rej
      im.src = 'data:image/png;base64,' + src
    })
    const [ia, ib] = await Promise.all([load(a), load(b)])
    const w = Math.max(ia.width, ib.width), h = Math.max(ia.height, ib.height)
    const cv = document.createElement('canvas')
    cv.width = w; cv.height = h
    const x = cv.getContext('2d', { willReadFrequently: true })
    x.fillStyle = '#fff'; x.fillRect(0, 0, w, h)
    x.drawImage(ia, 0, 0)
    const da = x.getImageData(0, 0, w, h).data
    x.clearRect(0, 0, w, h); x.fillStyle = '#fff'; x.fillRect(0, 0, w, h)
    x.drawImage(ib, 0, 0)
    const db = x.getImageData(0, 0, w, h).data
    x.clearRect(0, 0, w, h); x.fillStyle = '#fff'; x.fillRect(0, 0, w, h)
    x.drawImage(ia, 0, 0)
    const mark = x.getImageData(0, 0, w, h)
    let changed = 0, worst = 0
    for (let i = 0; i < da.length; i += 4) {
      const d = Math.max(Math.abs(da[i] - db[i]), Math.abs(da[i + 1] - db[i + 1]), Math.abs(da[i + 2] - db[i + 2]))
      if (d > worst) worst = d
      if (d > eps) {
        changed++
        mark.data[i] = 255; mark.data[i + 1] = 0; mark.data[i + 2] = 0   // 标红
      }
    }
    x.putImageData(mark, 0, 0)
    const total = w * h
    return {
      w, h, changed, total, pct: (100 * changed) / total, worst,
      diffPng: cv.toDataURL('image/png').split(',')[1],
    }
  }, [aBuf.toString('base64'), bBuf.toString('base64'), CHANNEL_EPS])
  await page.close()
  return out
}

fs.mkdirSync(DIR, { recursive: true })
const browser = await chromium.launch({ channel: 'msedge', headless: true })
let failed = 0
try {
  for (const c of CASES) {
    const file = path.join(DIR, `${c.name}.png`)
    const shot = await shoot(browser, c)
    if (UPDATE || !fs.existsSync(file)) {
      fs.writeFileSync(file, shot)
      console.log(`BASELINE ${c.name}  ${(shot.length / 1024).toFixed(0)}KB` + (UPDATE ? '' : '  （基线缺失，已生成）'))
      if (!UPDATE) failed++
      continue
    }
    const base = fs.readFileSync(file)
    const d = await diff(browser, base, shot)
    const bad = d.changed > MAX_CHANGED_ABS || d.pct > MAX_CHANGED_PCT
    const out = path.join(DIR, `diff-${c.name}.png`)
    if (bad) {
      fs.writeFileSync(out, Buffer.from(d.diffPng, 'base64'))
      failed++
    }
    console.log(`${bad ? 'FAIL' : 'PASS'}   ${c.name}  变化像素 ${d.changed}/${d.total} (${d.pct.toFixed(3)}%)  最大通道差 ${d.worst}`
      + (bad ? `  → 标红图 ${out}` : ''))
  }
  console.log(`\n${CASES.length - failed}/${CASES.length} 视觉基线一致`
    + `（阈值：单像素通道差 >${CHANNEL_EPS} 的像素超过 ${MAX_CHANGED_ABS} 个，或占比 >${MAX_CHANGED_PCT}%）`)
} finally {
  await browser.close()
}
process.exit(failed ? 1 : 0)
