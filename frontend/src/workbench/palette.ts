// 工作台配色：浅色（默认）/ 深色观测舱（可选）。
//
// 2026-11-09：工作台此前把配色写死成深色观测舱（main.ts 与 workbench.html 内联脚本
// 都硬编码 data-skin='observatory'），导致首次打开就是黑的，且 localStorage 里的
// 旧偏好完全不起作用。现在恢复「浅色默认 + 深色可切」：
//   - light：<html> 不挂 data-skin，用 memory-inspector-tokens.css 的 :root 浅色档；
//   - observatory：挂 data-skin='observatory'，用同一文件的深色档。
// 只影响工作台；问答页仍用 skins.ts 的 docmind.skin（zinc/graphite/...），互不覆盖。

export type PaletteKey = 'light' | 'observatory'

const KEY = 'docmind.workbenchPalette'
export const PALETTE_CHANGE_EVENT = 'docmind:palette'

/** 首帧底色：必须与 memory-inspector-tokens.css 两档的 --mi-bg-deep 一致。 */
export const PALETTE_BG: Record<PaletteKey, string> = {
  light: 'oklch(0.952 0.004 245)',
  observatory: 'oklch(0.145 0.018 245)',
}

export function isPaletteKey(v: unknown): v is PaletteKey {
  return v === 'light' || v === 'observatory'
}

/** 读偏好：没存过 / 存了非法值都回落浅色。 */
export function loadPalette(): PaletteKey {
  try {
    const raw = window.localStorage.getItem(KEY)
    return isPaletteKey(raw) ? raw : 'light'
  } catch {
    return 'light'
  }
}

/** 只应用、不落盘（首帧与切换共用）。 */
export function applyPalette(key: PaletteKey): PaletteKey {
  const k = isPaletteKey(key) ? key : 'light'
  const el = document.documentElement
  if (k === 'observatory') el.setAttribute('data-skin', 'observatory')
  else el.removeAttribute('data-skin')
  el.style.colorScheme = k === 'observatory' ? 'dark' : 'light'
  el.style.background = PALETTE_BG[k]
  return k
}

/** 用户切换：应用 + 落盘 + 广播。存不下时本次仍生效。 */
export function setPalette(key: PaletteKey): PaletteKey {
  const k = applyPalette(key)
  try {
    window.localStorage.setItem(KEY, k)
  } catch {
    /* localStorage 不可用（隐私模式/配额）时只本次生效 */
  }
  window.dispatchEvent(new CustomEvent(PALETTE_CHANGE_EVENT, { detail: k }))
  return k
}

export function onPaletteChange(fn: (k: PaletteKey) => void): () => void {
  const handler = (e: Event) => {
    const detail = (e as CustomEvent).detail
    if (isPaletteKey(detail)) fn(detail)
  }
  window.addEventListener(PALETTE_CHANGE_EVENT, handler)
  return () => window.removeEventListener(PALETTE_CHANGE_EVENT, handler)
}
