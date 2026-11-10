// 侧栏/抽屉宽度拖拽：连续跟随指针、松手持久化、视口缩小时自动收窄。
// 问答页左栏、右侧记忆侧栏共用这一份 pointer 逻辑，避免三处各写一遍。
import { onBeforeUnmount, ref, type Ref } from 'vue'

export type ResizeEdge = 'left' | 'right'

export interface PanelWidthOptions {
  /** localStorage 键；每个侧栏独立记忆 */
  storageKey: string
  /** 允许的最小 / 最大宽度（像素） */
  min: number
  max: number
  /** 侧栏之外主区至少保留的宽度，用来算视口上限 */
  mainMin: number
  /** 手柄在侧栏右边缘（向右拖变宽）还是左边缘（向左拖变宽） */
  edge: ResizeEdge
}

/** 拖拽期间挂在 body 上的类名（layout-modes.css 里禁选中、禁 iframe 抢事件）。 */
const RESIZING_CLASS = 'ft-col-resizing'

export function usePanelWidth(
  el: Ref<HTMLElement | null> | (() => HTMLElement | null | undefined),
  opts: PanelWidthOptions,
) {
  const { storageKey, min, max, mainMin, edge } = opts
  const elRef = typeof el === 'function' ? el : () => el.value

  // 视口兜底：既不超过上限，也不把主区压到 mainMin 以下。
  const clamp = (value: number) => {
    const ceiling = Math.max(min, Math.min(max, window.innerWidth - mainMin))
    return Math.round(Math.max(min, Math.min(ceiling, value)))
  }

  // null = 用户没拖过，继续用样式表里的默认宽度。
  const width = ref<number | null>(null)
  try {
    const saved = Number(window.localStorage.getItem(storageKey))
    if (Number.isFinite(saved) && saved > 0) width.value = clamp(saved)
  } catch { /* 存储不可用时保持默认宽度 */ }

  const dragging = ref(false)
  let startX = 0
  let startW = 0
  let pointerId = -1

  function persist() {
    if (width.value == null) return
    try { window.localStorage.setItem(storageKey, String(width.value)) } catch { /* 仅本次会话生效 */ }
  }

  function detach() {
    dragging.value = false
    pointerId = -1
    document.body.classList.remove(RESIZING_CLASS)
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
    window.removeEventListener('pointercancel', onUp)
  }

  function onMove(event: PointerEvent) {
    if (!dragging.value || event.pointerId !== pointerId) return
    const delta = event.clientX - startX
    width.value = clamp(edge === 'right' ? startW + delta : startW - delta)
  }

  function onUp(event: PointerEvent) {
    if (!dragging.value || event.pointerId !== pointerId) return
    detach()
    persist()
  }

  function onPointerDown(event: PointerEvent) {
    if (event.button !== 0 || !elRef()) return
    event.preventDefault()
    dragging.value = true
    ;(event.currentTarget as HTMLElement | null)?.setPointerCapture?.(event.pointerId)
    startX = event.clientX
    startW = elRef()!.getBoundingClientRect().width
    pointerId = event.pointerId
    // 拖拽期间禁止选中文本；iframe 由 CSS 接管 pointer-events。
    document.body.classList.add(RESIZING_CLASS)
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
  }

  /** 方向键微调（与工作台文件树手柄一致，每次 16px）。 */
  function nudge(dx: number) {
    const base = width.value ?? elRef()?.getBoundingClientRect().width ?? min
    width.value = clamp(base + dx)
    persist()
  }

  /** 双击恢复样式表默认宽度。 */
  function reset() {
    width.value = null
    try { window.localStorage.removeItem(storageKey) } catch { /* ignore */ }
  }

  function onViewportResize() {
    if (width.value != null) width.value = clamp(width.value)
  }
  window.addEventListener('resize', onViewportResize)
  onBeforeUnmount(() => {
    detach()
    window.removeEventListener('resize', onViewportResize)
  })

  return { width, dragging, clamp, onPointerDown, nudge, reset }
}
