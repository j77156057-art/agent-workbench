// 工作台配色 —— 现在只是 skins.ts 的**薄适配层**。
//
// 为什么不再自己存一份：2026-11-20 之前，工作台读 docmind.workbenchPalette、
// 问答页读 docmind.skin，两个 localStorage 键各记各的 —— 工作台把默认做成了
// 「墨屿纸感」，问答页还停在「锌白细线」，两页来回切一次就是两个产品。
// 现在只有一个键：docmind.skin（skins.ts 是唯一真源），两页必然一致。
//
// 这一层保留的意义：
//   ① 设置页的「配色」四档（浅色 / 观测舱 / 墨屿纸感 / 墨屿夜航）是**皮肤表的
//      一个子集** —— 用户在设置里看到的是四档，而不是皮肤表里全部八套；
//   ② 首帧底色表（PALETTE_BG）留在这里；
//   ③ onPaletteChange 继续按「配色档」广播，CodeView / SettingsView 的接口不变。

import { applySkin, loadSkin, onSkinChange, setSkin, type SkinKey } from './skins'

export type PaletteKey = 'light' | 'observatory' | 'moyu' | 'moyu-night'
export const PALETTE_CHANGE_EVENT = 'docmind:palette'

/** 配色档 → 皮肤键。浅色档落在 zinc（冷白细线）：两页同一套冷白，不另开一套。 */
const PALETTE_TO_SKIN: Record<PaletteKey, SkinKey> = {
  light: 'zinc',
  observatory: 'observatory',
  moyu: 'moyu',
  'moyu-night': 'moyu-night',
}

/** 皮肤键 → 配色档。皮肤表里多出来的几套（石墨 / 素纸 / 种子）落到最接近的一档。 */
const SKIN_TO_PALETTE: Record<SkinKey, PaletteKey> = {
  zinc: 'light',
  observatory: 'observatory',
  moyu: 'moyu',
  'moyu-night': 'moyu-night',
  graphite: 'observatory',
  paper: 'light',
  seed: 'light',
}

/** 首帧底色：必须与 memory-inspector-tokens.css / skins-moyu.css 的底色一致。 */
export const PALETTE_BG: Record<PaletteKey, string> = {
  light: 'oklch(0.952 0.004 245)',
  observatory: 'oklch(0.145 0.018 245)',
  // 「墨屿」两档（2026-11-20）：暖白纸底 / 深墨夜航。色值与 skins-moyu.css 里
  // 的 --mi-* 覆写一致，首帧铺的底色必须和它对得上，否则刷新会闪一下冷白。
  moyu: 'oklch(0.945 0.011 78)',
  'moyu-night': 'oklch(0.142 0.008 60)',
}

function isPaletteKey(v: unknown): v is PaletteKey {
  return v === 'light' || v === 'observatory' || v === 'moyu' || v === 'moyu-night'
}

/** 读当前配色档：真源是皮肤键，这里只做「皮肤 → 配色档」的投影。 */
export function loadPalette(): PaletteKey {
  return SKIN_TO_PALETTE[loadSkin()]
}

/** 只应用、不落盘（首帧与切换共用）。 */
export function applyPalette(key: PaletteKey): PaletteKey {
  const k = isPaletteKey(key) ? key : 'light'
  applySkin(PALETTE_TO_SKIN[k])
  document.documentElement.style.background = PALETTE_BG[k]
  return k
}

/** 用户切换：应用 + 落盘 + 广播。存的是**皮肤键**（唯一真源），问答页随之变化。 */
export function setPalette(key: PaletteKey): PaletteKey {
  const k = applyPalette(key)
  setSkin(PALETTE_TO_SKIN[k])
  window.dispatchEvent(new CustomEvent(PALETTE_CHANGE_EVENT, { detail: k }))
  return k
}

/** 订阅配色变化，返回取消订阅函数（Vue 的 onUnmounted 里调用）。 */
export function onPaletteChange(fn: (k: PaletteKey) => void): () => void {
  return onSkinChange((skin) => {
    const k = SKIN_TO_PALETTE[skin]
    document.documentElement.style.background = PALETTE_BG[k]
    fn(k)
  })
}
