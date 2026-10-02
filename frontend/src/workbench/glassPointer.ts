// 玻璃拟态的光标跟随高光 + 视差 —— 只写 CSS 变量，不碰任何业务逻辑。
//
// 为什么不写在 CSS 里：CSS 拿不到光标位置。`--glass-x/--glass-y` 必须是
// 真实的像素坐标经归一化后的值，只能由 JS 写。这里用 rAF 节流 + 变量写入，
// 把每帧的成本压到「改两个 CSS 自定义属性」。
//
// 三重降级（缺一不可，否则要么掉帧要么晕）：
//  ① prefers-reduced-motion: reduce → 完全不启动，--glass-p 恒为 0
//  ② 浏览器不支持 backdrop-filter → 玻璃本身已降级成不透明，高光纯装饰也关掉
//  ③ 元素不在视口内 / 被 pointer-events 穿透 → 用 getBoundingClientRect 过滤
//
// 为什么用「进入即高亮 + 离开渐隐」而不是持续跟随：
// 持续跟随时鼠标划过整屏会一直有光斑在跳，读屏时很吵。进入高亮更接近苹果的做法。

const GLASS_SELECTOR = [
  '.wb-card', '.acp-panel', '.acp-stage-content',
  '.ov-group', '.rh-card',
].join(',')

const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)')

function supportsBackdrop(): boolean {
  return window.CSS?.supports?.('backdrop-filter', 'blur(1px)')
    || window.CSS?.supports?.('-webkit-backdrop-filter', 'blur(1px)')
    || false
}

/** 元素中心点在视口内才处理，省掉滚动时的无谓计算 */
function inView(r: DOMRect, pad = 120): boolean {
  return r.bottom > -pad && r.top < window.innerHeight + pad
    && r.right > -pad && r.left < window.innerWidth + pad
}

export type GlassPointerHandle = { stop(): void }

/**
 * 启动光标跟随。返回 stop() 用于组件卸载。
 * @param root 事件绑定的根节点，默认 document
 */
export function initGlassPointer(root: HTMLElement | Document = document): GlassPointerHandle {
  // 降级 ①：用户要求减少动效 → 不启动，CSS 里 --glass-p 本就是 0
  if (REDUCED.matches) return { stop() {} }
  // 降级 ②：没有 backdrop-filter 时玻璃已是不透明块，高光只会变成脏渍
  if (!supportsBackdrop()) return { stop() {} }

  let raf = 0
  let lastX = 0
  let lastY = 0
  let active: HTMLElement | null = null

  const write = (el: HTMLElement, x: number, y: number) => {
    const r = el.getBoundingClientRect()
    if (!r.width || !r.height) return
    // 归一化到 -1 ~ 1（0 = 元素中心）。CSS 里再用它算高光中心与视差位移。
    const nx = ((x - r.left) / r.width) * 2 - 1
    const ny = ((y - r.top) / r.height) * 2 - 1
    el.style.setProperty('--glass-x', nx.toFixed(3))
    el.style.setProperty('--glass-y', ny.toFixed(3))
  }

  const onMove = (e: MouseEvent) => {
    lastX = e.clientX
    lastY = e.clientY
    if (raf) return                       // 一帧最多写一次
    raf = requestAnimationFrame(() => {
      raf = 0
      const el = (e.target as Element | null)?.closest?.(GLASS_SELECTOR) as HTMLElement | null
      if (!el) { active = null; return }
      if (!inView(el.getBoundingClientRect())) return
      // 换了目标 → 旧的高光淡出（靠 CSS 的 opacity transition）
      active = el
      write(el, lastX, lastY)
    })
  }

  // 离开视口：清掉高光，避免下次进来时残留旧坐标
  const onLeave = () => {
    if (active) active.style.setProperty('--glass-p', '0')
    active = null
  }
  const onEnter = () => {
    if (active) active.style.setProperty('--glass-p', '1')
  }

  const node: HTMLElement = root instanceof Document
    ? (root.body ?? root.documentElement)
    : root

  node.addEventListener('mousemove', onMove, { passive: true })
  node.addEventListener('mouseleave', onLeave)
  node.addEventListener('mouseenter', onEnter)

  return {
    stop() {
      if (raf) cancelAnimationFrame(raf)
      node.removeEventListener('mousemove', onMove)
      node.removeEventListener('mouseleave', onLeave)
      node.removeEventListener('mouseenter', onEnter)
    },
  }
}

/**
 * 切换玻璃材质。玻璃是**材质**不是皮肤 —— 皮肤管颜色（skins.ts），
 * 玻璃管表面（这里），两者正交，可自由组合。
 */
const MATERIAL_KEY = 'docmind.material'
export const GLASS_CHANGE_EVENT = 'docmind:material'

export type Material = 'solid' | 'glass'
const VALID_MAT: Set<string> = new Set<Material>(['solid', 'glass'])

export function loadMaterial(): Material {
  try {
    const raw = window.localStorage.getItem(MATERIAL_KEY)
    return VALID_MAT.has(raw as string) ? (raw as Material) : 'solid'
  } catch {
    return 'solid'
  }
}

export function applyMaterial(m: Material): Material {
  const k = VALID_MAT.has(m) ? m : 'solid'
  const el = document.documentElement
  if (k === 'glass') el.setAttribute('data-material', 'glass')
  else el.removeAttribute('data-material')
  return k
}

export function setMaterial(m: Material): Material {
  const k = applyMaterial(m)
  try { window.localStorage.setItem(MATERIAL_KEY, k) } catch { /* 存不下则仅本次会话 */ }
  window.dispatchEvent(new CustomEvent(GLASS_CHANGE_EVENT, { detail: k }))
  return k
}

/** 启动时调用：必须在挂载前，否则首帧闪一下不透明再变玻璃 */
export function initMaterial(): Material {
  const m = applyMaterial(loadMaterial())
  initGlassPointer()
  return m
}

/** 订阅材质变化（另一个窗口/标签页切换时同步 UI），返回取消订阅函数 */
export function onMaterialChange(fn: (m: Material) => void): () => void {
  const onCustom = (e: Event) => {
    const d = (e as CustomEvent).detail
    if (VALID_MAT.has(d as string)) fn(d as Material)
  }
  const onStorage = (e: StorageEvent) => {
    if (e.key === MATERIAL_KEY && VALID_MAT.has(e.newValue ?? '')) {
      applyMaterial(e.newValue as Material)
      fn(e.newValue as Material)
    }
  }
  window.addEventListener(GLASS_CHANGE_EVENT, onCustom)
  window.addEventListener('storage', onStorage)
  return () => {
    window.removeEventListener(GLASS_CHANGE_EVENT, onCustom)
    window.removeEventListener('storage', onStorage)
  }
}
