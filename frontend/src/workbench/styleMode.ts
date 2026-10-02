// 风格层（造型语言）切换 —— 与皮肤（配色）、材质（实体/玻璃）三者正交。

const STYLE_KEY = 'docmind.style'
export const STYLE_CHANGE_EVENT = 'docmind:style'

export type StyleKey = 'brutal' | 'soft' | 'outline' | 'neon' | 'flat'

export interface StyleDef {
  key: StyleKey
  name: string
  desc: string
  /** 预览示意图用的六维签名：圆角 / 描边 / 阴影 / 字体 / 纹理 / 字重 */
  signature: { radius: string; edge: string; shadow: string; font: string; texture: string; weight: string }
}

export const STYLES: StyleDef[] = [
  {
    key: 'brutal', name: '粗野硬边', desc: '当前默认。零圆角 + 2px 纯黑描边 + 错位硬阴影 + 孔版颗粒',
    signature: { radius: '0', edge: '2px', shadow: '硬', font: '无衬线', texture: '颗粒', weight: '800' },
  },
  {
    key: 'soft', name: '柔化大圆角', desc: '10–20px 大圆角 + 半透明细边 + 极淡软阴影，macOS 式轻盈卡片',
    signature: { radius: '10–20', edge: '1px 半透', shadow: '极淡', font: '无衬线', texture: '无', weight: '700' },
  },
  {
    key: 'outline', name: '等宽线框', desc: '全等宽字体 + 强调色 1px 线框 + 零阴影，像终端 / 原理图',
    signature: { radius: '0', edge: '1px 强调', shadow: '无', font: '等宽', texture: '无', weight: '700' },
  },
  {
    key: 'neon', name: '霓虹发光', desc: '双层辉光描边 + 加重字重 + 拉开字距，CRT / 赛博终端',
    signature: { radius: '3–12', edge: '2px 辉光', shadow: '双层辉光', font: '无衬线', texture: '弱', weight: '800' },
  },
  {
    key: 'flat', name: '极扁平', desc: '无描边 + 无阴影，靠纯色块与留白分层，Swiss / Unovis 平涂',
    signature: { radius: '0', edge: '无', shadow: '无', font: '无衬线', texture: '无', weight: '700' },
  },
]

export const DEFAULT_STYLE: StyleKey = 'brutal'
const VALID: Set<string> = new Set<StyleKey>(STYLES.map((s) => s.key))

export function isStyleKey(v: unknown): v is StyleKey {
  return typeof v === 'string' && VALID.has(v)
}

export function loadStyle(): StyleKey {
  try {
    const raw = window.localStorage.getItem(STYLE_KEY)
    return isStyleKey(raw) ? raw : DEFAULT_STYLE
  } catch {
    return DEFAULT_STYLE
  }
}

/** 写到 <html data-style>。默认 brutal 也显式写上，避免「残留上一个风格」。 */
export function applyStyle(key: StyleKey): StyleKey {
  const k = isStyleKey(key) ? key : DEFAULT_STYLE
  document.documentElement.setAttribute('data-style', k)
  return k
}

export function saveStyle(key: StyleKey): void {
  try {
    window.localStorage.setItem(STYLE_KEY, key)
  } catch {
    /* 存不下则仅本次会话生效 */
  }
}

export function setStyle(key: StyleKey): StyleKey {
  const k = applyStyle(key)
  saveStyle(k)
  window.dispatchEvent(new CustomEvent(STYLE_CHANGE_EVENT, { detail: k }))
  return k
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

export function onStyleChange(fn: (k: StyleKey) => void): () => void {
  const onCustom = (e: Event) => {
    const d = (e as CustomEvent).detail
    if (isStyleKey(d)) fn(d)
  }
  const onStorage = (e: StorageEvent) => {
    if (e.key === STYLE_KEY && isStyleKey(e.newValue)) {
      applyStyle(e.newValue as StyleKey)
      fn(e.newValue as StyleKey)
    }
  }
  window.addEventListener(STYLE_CHANGE_EVENT, onCustom)
  window.addEventListener('storage', onStorage)
  return () => {
    window.removeEventListener(STYLE_CHANGE_EVENT, onCustom)
    window.removeEventListener('storage', onStorage)
  }
}
