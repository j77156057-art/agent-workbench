// 风格层（造型语言）—— 2026-10-07 收敛：5 种造型（brutal/soft/outline/neon/flat）
// 砍到只留 brutal，本模块只负责把 data-style='brutal' 写到 <html> 上。
// ⚠️ 2026-10-08：「造型」并没有消失，只是换了归属 —— 硬边还是细线现在由**皮肤**带
// （skins.css 的 --r-* / --edge-* / --sh-* / --style-* 逐套覆写，zinc 就是细线档）。
// 所以别再假设 data-style 与 data-skin 正交；仍然正交的是材质（实体/玻璃）。
//
// 保留 data-style 机制而非彻底删除：切皮肤/材质的链路已经依赖它存在
// （CSS 里有 html[data-style] 接驳规则），且日后若要再加造型，机制还在。

const STYLE_KEY = 'docmind.style'

export type StyleKey = 'brutal'

export const DEFAULT_STYLE: StyleKey = 'brutal'

export function isStyleKey(v: unknown): v is StyleKey {
  return v === 'brutal'
}

/** 读存储的旧值；被砍掉的造型（soft/outline/neon/flat）清掉残值，回落 brutal */
export function loadStyle(): StyleKey {
  try {
    const raw = window.localStorage.getItem(STYLE_KEY)
    if (isStyleKey(raw)) return raw
    if (raw !== null) window.localStorage.removeItem(STYLE_KEY)
    return DEFAULT_STYLE
  } catch {
    return DEFAULT_STYLE
  }
}

/** 写到 <html data-style>。显式写上，避免「残留上一个风格」。 */
export function applyStyle(key: StyleKey): StyleKey {
  document.documentElement.setAttribute('data-style', key)
  return key
}

/**
 * 启动时调用：必须在挂载前，否则首帧闪默认造型。
 *
 * ⚠️ 不在这里调 initGlassPointer() —— initMaterial() 也会调，
 * 两者都由 main.ts 顺序调用，重复注册 mousemove 会让高光更新两次（浪费 + 抖动）。
 * 光标跟随是「材质」的能力，归 glassPointer 管。
 */
export function initStyle(): StyleKey {
  return applyStyle(loadStyle())
}
