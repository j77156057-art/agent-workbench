// 布局模式切换 —— 换**空间构成本身**，与配色/造型/材质三层完全正交。
//
// 为什么第四层：前三层换的全是「表面」（颜色、描边、圆角、字体），
// 但空间构成一直是同一副骨架（.wb-body{flex} + .ft-panel{flex:0 0 288px}），
// 也就是「左 288px 侧栏 + 右主区」永远不变。
// 用户说「风格大改」指的就是这个 —— 侧栏该在哪、主区该多宽、该居中还是铺满。
//
// 四层总览：
//   data-skin      配色（含造型令牌） skins.css        key: docmind.skin
//   data-style     造型语言           skins.css 令牌   key: docmind.style（已收敛为 brutal 一种）
//   data-material  材质               glass.css        key: docmind.material
//   data-layout    空间构成           layout-modes.css key: docmind.layout  ← 本文件
//
// 切换不需要刷新：改 <html data-layout> 后浏览器立即重排。
// 纯 CSS 实现，不改任何 DOM 结构与业务逻辑。

const LAYOUT_KEY = 'docmind.layout'
export const LAYOUT_CHANGE_EVENT = 'docmind:layout'

export type LayoutKey = 'fixed' | 'focus' | 'dock' | 'split' | 'grid'

export interface LayoutDef {
  key: LayoutKey
  name: string
  desc: string
  /** 空间签名：侧栏在哪 / 主区多宽 / 概览几列 */
  sig: { rail: string; main: string; cols: string }
}

export const LAYOUTS: LayoutDef[] = [
  {
    key: 'fixed', name: '固定三栏', desc: '当前默认。侧栏 288px 常驻左，主区铺满右侧。常规 IDE 骨架',
    sig: { rail: '左 288px', main: '铺满', cols: '2 栏' },
  },
  {
    key: 'focus', name: '单列专注', desc: '侧栏移出视口，主区居中限宽 860px，行距放大。长时间读文档 / 写提示词',
    sig: { rail: '移出视口', main: '居中 860', cols: '单列' },
  },
  {
    key: 'dock', name: '底部抽屉', desc: '文件树从左栏变成底部横向抽屉，主区在上。横向全宽、纵向分层',
    sig: { rail: '底部 240', main: '铺满', cols: '4 栏' },
  },
  {
    key: 'split', name: '对半分屏', desc: '侧栏与主区各占一半，中间粗分隔线。适合文件与内容对照',
    sig: { rail: '左 50%', main: '右 50%', cols: '单列' },
  },
  {
    key: 'grid', name: '仪表盘网格', desc: '侧栏收成 68px 图标条，主区高密度，概览铺成三列卡片流',
    sig: { rail: '左 68px', main: '铺满', cols: '3 栏' },
  },
]

export const DEFAULT_LAYOUT: LayoutKey = 'fixed'
const VALID: Set<string> = new Set<LayoutKey>(LAYOUTS.map((l) => l.key))

export function isLayoutKey(v: unknown): v is LayoutKey {
  return typeof v === 'string' && VALID.has(v)
}

export function loadLayout(): LayoutKey {
  try {
    const raw = window.localStorage.getItem(LAYOUT_KEY)
    return isLayoutKey(raw) ? raw : DEFAULT_LAYOUT
  } catch {
    return DEFAULT_LAYOUT
  }
}

export function applyLayout(key: LayoutKey): LayoutKey {
  const k = isLayoutKey(key) ? key : DEFAULT_LAYOUT
  document.documentElement.setAttribute('data-layout', k)
  return k
}

export function setLayout(key: LayoutKey): LayoutKey {
  const k = applyLayout(key)
  try {
    window.localStorage.setItem(LAYOUT_KEY, k)
  } catch {
    /* 存不下则仅本次会话生效 */
  }
  window.dispatchEvent(new CustomEvent(LAYOUT_CHANGE_EVENT, { detail: k }))
  return k
}

/** 启动时调用：必须在挂载前，否则首帧按默认骨架渲染完才重排（会闪一下） */
export function initLayout(): LayoutKey {
  return applyLayout(loadLayout())
}

export function onLayoutChange(fn: (k: LayoutKey) => void): () => void {
  const onCustom = (e: Event) => {
    const d = (e as CustomEvent).detail
    if (isLayoutKey(d)) fn(d)
  }
  const onStorage = (e: StorageEvent) => {
    if (e.key === LAYOUT_KEY && isLayoutKey(e.newValue)) {
      applyLayout(e.newValue as LayoutKey)
      fn(e.newValue as LayoutKey)
    }
  }
  window.addEventListener(LAYOUT_CHANGE_EVENT, onCustom)
  window.addEventListener('storage', onStorage)
  return () => {
    window.removeEventListener(LAYOUT_CHANGE_EVENT, onCustom)
    window.removeEventListener('storage', onStorage)
  }
}
