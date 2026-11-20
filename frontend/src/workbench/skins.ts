// 皮肤（视觉风格）切换 —— 纯展示层，存 localStorage，不落后端。
//
// 为什么单独一个模块：工作台与问答页是两个独立入口（workbench.html / index.html），
// 但**皮肤必须是同一个**——用户在设置里选了「石墨夜航」，切到问答页也得是同一套，
// 否则两页风格分裂。单一 localStorage key + 启动时应用到 <html data-skin>，
// 天然跨入口一致。
//
// 关键实现细节：
//  ① 皮肤写在 <html> 的 data-skin 上，skins.css 用 html[data-skin='x'] 覆写 :root 令牌，
//     所以「切皮肤」= 改一个 dataset 值，浏览器立即重算全部 var()，无需刷新。
//  ② 必须在应用挂载**之前**应用（main.ts 里 createApp 之前），否则首帧会闪一下默认皮。
//     这里导出 applyStoredSkin() 供两个 main.ts 同步调用。
//  ③ 同一标签页内两个文档（工作台弹 pywebview 窗口、问答页）之间不触发 storage 事件，
//     所以额外派发 SKIN_CHANGE_EVENT，监听方直接改自己的 dataset。
//  ④ localStorage 在隐私模式 / 策略禁用时直接抛，读写一律吞异常回退默认，
//     绝不能让设置页或主界面因为存不下而白屏。

// 2026-10-07 收敛：8 套 × 5 种造型的组合矩阵（40+ 种，谁也调不过来）砍到
// 两套精调皮肤 × 1 种造型（brutal）。被砍皮肤的 localStorage 残值由
// isSkinKey 校验拦下，自动回默认 paper，不需要迁移。
//
// 2026-10-08 加回第三套 zinc：brutal 那两套的「杂」不在配色而在**造型**——
// 2px 墨黑描边 + 硬偏移阴影 + 孔版颗粒 + Archivo Black 标题，四件事同时压在每个
// 元素上。zinc 是同一套组件在「1px 灰细线 + 零阴影 + 6px 圆角 + 无纹理」下的样子。
// 为了让这套真的能成立，两页里 64 处「把 --ink 当描边色」+ 26 处「压根没写圆角」
// 的规则收编到了 --border / --edge-* / --r-* 上；这些替换在 paper / graphite 下
// 逐像素等价（--border 与 --ink 在那两套里同值，--r-sm 本就是 0），
// 由 verify_visual_regression.mjs 的「paper / graphite 用例 0 像素差」证明。
export type SkinKey = 'paper' | 'graphite' | 'zinc' | 'seed' | 'moyu' | 'moyu-night' | 'observatory'

export interface SkinDef {
  key: SkinKey
  name: string
  desc: string
  /** 卡片色板预览用的四个取样色：纸底 / 描边 / 强调 / 强调块上的字 */
  swatch: [string, string, string, string]
}

const KEY = 'docmind.skin'
/** 用户**主动**选过皮肤就记这个标记：之后不再做默认皮肤迁移。 */
const LOCK_KEY = 'docmind.skinLocked'
export const SKIN_CHANGE_EVENT = 'docmind:skin'

export const SKINS: SkinDef[] = [
  { key: 'zinc', name: '锌白细线', desc: '冷白底 + 1px 灰细线 + 6px 圆角 + 零阴影，青绿强调。日常默认', swatch: ['#fcfcfd', '#7d7d86', '#0f766e', '#ffffff'] },
  { key: 'graphite', name: '石墨夜航', desc: '深炭底 + 电光青，硬边 + 近黑阴影。夜间低亮环境', swatch: ['#16181d', '#e8e6e0', '#4dd0e1', '#000000'] },
  { key: 'paper', name: '素纸朱红', desc: '暖骨白纸 + 朱红，零圆角 + 硬阴影。印刷海报风，按需选用', swatch: ['#ece7db', '#14110f', '#ff674d', '#000000'] },
  // 「观测舱」是工作台原来的深色调（memory-inspector-tokens.css 的深色家族）。
  // 2026-11-20 两页合并到同一个皮肤键之后，它也进了问答页的皮肤表 ——
  // 否则工作台切「观测舱」时，问答页没有对应的皮肤可跟，两页又得分家。
  { key: 'observatory', name: '观测舱', desc: '深蓝灰底 + 电光青，面板与画布同源。夜间低亮环境', swatch: ['#0a1118', '#3b4757', '#5accf0', '#0a1118'] },
  // 「种子」是一套**由随机种子推导出来**的皮肤：色相/明度/圆角/描边/阴影/字号/间距
  // 全部来自 tools/design_seed.py 对该种子的确定性派生（色值按对比度反解，保证过 AA）。
  // 换观感就跑 `python tools/design_seed.py --new-seed` 重新生成，别手改这个文件。
  { key: 'seed', name: '种子 344284156', desc: '由种子推导：玫瑰红强调 + 14px 圆角 + 紧凑密度 + 浅底', swatch: ['#f9f8f8', '#28201f', '#904759', '#ffffff'] },
  // 「墨屿」两套（2026-11-20）：暖白纸底 + 砖橙强调 + 衬线阅读体，与深墨夜航。
  // 令牌在独立的 skins-moyu.css 里（skins.css 正被另一条线改动，新皮肤另起文件
  // 免得撞车）；阅读体用系统衬线栈，不引字体资源。
  { key: 'moyu', name: '墨屿纸感', desc: '暖白纸底 + 砖橙强调 + 衬线正文，圆角明显', swatch: ['#f6f1e8', '#cdbfa9', '#b4532a', '#fffdf8'] },
  { key: 'moyu-night', name: '墨屿夜航', desc: '深墨底 + 砖橙强调 + 衬线正文，夜间低亮环境', swatch: ['#121110', '#443f39', '#c2623a', '#17150f'] },
]

// 默认皮肤：
//   2026-10-20 从 paper（brutalist：墨黑粗边 + 硬偏移阴影 + 孔版纹理）
//   换成 zinc（1px 细线 + 圆角 + 零阴影 + 青绿强调）—— 前者更像工程样机，
//   后者才是能拿上线给普通用户的默认观感。
//   2026-11-20 再换成 moyu（墨屿纸感）：工作台的配色档先一步做成了墨屿，
//   而问答页还在 zinc —— 两页默认值分家，用户来回切一次就看出是两套产品。
//   两页现在共用这一个键（见下方「唯一真源」），默认自然一致。
export const DEFAULT_SKIN: SkinKey = 'moyu'

const VALID = new Set<string>(SKINS.map((s) => s.key))
/** 旧默认皮肤：曾被大量用户默认选中（从没主动选过），迁移到新默认时要换掉。
 *  2026-11-20：zinc 也成了旧默认（它当过一个月的默认），一并列入。 */
const LEGACY_DEFAULTS: SkinKey[] = ['paper', 'zinc']
/** 2026-11-20 之前工作台单独用的配色键（docmind.workbenchPalette）。
 *  两页合并到 docmind.skin 之后，这个键只作一次性迁移来源。 */
const LEGACY_PALETTE_KEY = 'docmind.workbenchPalette'
/** 旧配色键的档位 → 皮肤键。两页合并后皮肤键是唯一真源。 */
const LEGACY_PALETTE_TO_SKIN: Record<string, SkinKey> = {
  light: 'zinc',
  observatory: 'observatory',
  moyu: 'moyu',
  'moyu-night': 'moyu-night',
}

export function isSkinKey(v: unknown): v is SkinKey {
  return typeof v === 'string' && VALID.has(v)
}

/** 读当前皮肤；读不到/被禁用/值非法一律回默认（不抛）
 *
 *  2026-10-20 加一次性迁移：旧默认皮肤是 paper（brutalist），当时换成 zinc。
 *  凡是**从没主动选过**皮肤的机器（localStorage 里是旧默认，且没有锁定标记），
 *  自动换到新默认 —— 否则它们会一直停在旧观感上。
 *  主动选过的用户（选的时候会打锁定标记）不受影响。
 *  2026-11-20：新默认是 moyu（墨屿纸感），zinc 也列入旧默认。 */
export function loadSkin(): SkinKey {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (isSkinKey(raw)) {
      // 从没主动选过的机器（没有锁定标记）跟着新默认走，不留在旧观感上。
      if (LEGACY_DEFAULTS.includes(raw) && !window.localStorage.getItem(LOCK_KEY)) {
        window.localStorage.setItem(KEY, DEFAULT_SKIN)
        return DEFAULT_SKIN
      }
      return raw
    }
    // 2026-11-20 之前工作台单独用 docmind.workbenchPalette 存配色，
    // 问答页用 docmind.skin —— 两个键各记各的，两页随时可能分家。
    // 这里把旧配色键一次性并到皮肤键：用户选过的配色不丢，之后只有一个键。
    const legacy = window.localStorage.getItem(LEGACY_PALETTE_KEY)
    const mapped = legacy ? LEGACY_PALETTE_TO_SKIN[legacy] : undefined
    if (mapped) {
      window.localStorage.setItem(KEY, mapped)
      return mapped
    }
    return DEFAULT_SKIN
  } catch {
    return DEFAULT_SKIN
  }
}

export function saveSkin(key: SkinKey): void {
  try {
    window.localStorage.setItem(KEY, key)
    // 用户主动选了就锁定：之后不再被默认皮肤迁移动
    window.localStorage.setItem(LOCK_KEY, '1')
    writeSkinBg(key)
  } catch {
    /* 存不下就只在本次会话生效，不打断切换 */
  }
}

/** 皮肤底色缓存键：给首帧内联脚本用。
 *  为什么需要它：html 里的内联脚本要在 CSS 之前就把底色铺上（否则深色皮肤首帧白闪），
 *  但底色是每套皮肤各自的，html 里写死一套会跟换皮肤脱节。切皮肤时把底色一起存下来，
 *  首帧直接读 —— 换任何皮肤（包括由种子生成的）都不会闪错。 */
const BG_KEY = 'docmind.skinBg'

function writeSkinBg(key: SkinKey): void {
  try {
    const def = SKINS.find((s) => s.key === key)
    if (def) window.localStorage.setItem(BG_KEY, def.swatch[0])
  } catch { /* 存不下就算了，首帧退回默认底色 */ }
}

/** 把皮肤写到 <html data-skin>；非法 key 一律回落默认，不留"半失效"状态 */
export function applySkin(key: SkinKey): void {
  const k = isSkinKey(key) ? key : DEFAULT_SKIN
  document.documentElement.setAttribute('data-skin', k)
  // 同步 color-scheme：深色皮肤让原生滚动条/表单控件也跟着走暗色。
  // 注意不能只认 'graphite' —— 种子皮肤可能是深色（--skin-dark: 1），
  // 这里读皮肤块自己声明的 --skin-dark，新增深色皮肤不用改这里。
  const dark = getComputedStyle(document.documentElement).getPropertyValue('--skin-dark').trim() === '1'
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light'
  writeSkinBg(k)
}

/**
 * 启动时调用：读存储 → 写 <html>。必须在 createApp().mount() 之前，
 * 否则首帧会用默认皮渲染再切，用户看到一次闪白。
 */
export function applyStoredSkin(): SkinKey {
  const k = loadSkin()
  applySkin(k)
  return k
}

/**
 * 切换皮肤：写存储 + 应用 + 广播。
 * 返回实际生效的 key（非法值会被夹到默认）。
 */
export function setSkin(key: SkinKey): SkinKey {
  const k = isSkinKey(key) ? key : DEFAULT_SKIN
  saveSkin(k)
  applySkin(k)
  window.dispatchEvent(new CustomEvent(SKIN_CHANGE_EVENT, { detail: k }))
  return k
}

/** 订阅皮肤变化，返回取消订阅函数（Vue 的 onUnmounted 里调用） */
export function onSkinChange(fn: (key: SkinKey) => void): () => void {
  const handler = (e: Event) => {
    const d = (e as CustomEvent).detail
    if (isSkinKey(d)) fn(d)
  }
  window.addEventListener(SKIN_CHANGE_EVENT, handler)
  // 另一个文档（如问答页）改了皮肤，storage 事件在本标签页也会触发
  const onStorage = (e: StorageEvent) => {
    if (e.key === KEY && isSkinKey(e.newValue)) {
      applySkin(e.newValue as SkinKey)
      fn(e.newValue as SkinKey)
    }
  }
  window.addEventListener('storage', onStorage)
  return () => {
    window.removeEventListener(SKIN_CHANGE_EVENT, handler)
    window.removeEventListener('storage', onStorage)
  }
}
