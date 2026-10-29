import { nextTick, ref } from 'vue'

/**
 * 对话流自动滚动：仅当用户已贴底时跟随输出；用户上滚看历史时暂停，
 * 回到底部（或主动发送）再恢复。高频事件（子代理步骤/流式帧）统一
 * 按 rAF 合批，避免每次 nextTick + 强制布局。
 */
export function useAutoScroll() {
  const scroller = ref<HTMLElement | null>(null)
  const stickToBottom = ref(true)
  let suppressScrollEvent = false
  let followRaf = 0

  function isNearBottom(el: HTMLElement) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  function onScroll() {
    if (suppressScrollEvent) return
    const el = scroller.value
    if (el) stickToBottom.value = isNearBottom(el)
  }

  function onWheel(ev: WheelEvent) {
    // 用户向上滚轮 = 明确要查看历史：保持流式输出，但不再把视口拽回底部
    if (ev.deltaY >= 0) return
    // 没有可滚动内容时上滚不该算「离开底部」，否则短会话会一直挂着「回到底部」入口
    const el = scroller.value
    if (el && el.scrollHeight - el.clientHeight > 8) stickToBottom.value = false
  }

  function followBottom() {
    const el = scroller.value
    if (el && stickToBottom.value) {
      suppressScrollEvent = true
      el.scrollTop = el.scrollHeight
      // 滚动事件在本帧内派发，下一帧解除即可；比每 token 排一个 setTimeout(0) 便宜
      requestAnimationFrame(() => { suppressScrollEvent = false })
    }
  }

  function scrollToBottom() { followBottom() }

  /** 卡片高频活动（子代理每步 emit）按帧合批跟随一次 */
  function scheduleFollow() {
    if (!followRaf) followRaf = requestAnimationFrame(() => {
      followRaf = 0
      queueMicrotask(followBottom)
    })
  }

  function resumeAutoScroll() {
    stickToBottom.value = true
    void nextTick(scrollToBottom)
  }

  return {
    scroller,
    stickToBottom,
    onScroll,
    onWheel,
    followBottom,
    scrollToBottom,
    scheduleFollow,
    resumeAutoScroll,
  }
}
