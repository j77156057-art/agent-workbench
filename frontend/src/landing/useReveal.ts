// 粗野落地草案 · 交互组合式函数
// 把 showcase/scripts/main.js 的「入场序列 + 指标 count-up + 降级」原样迁到 Vue 生命周期里。
import { onMounted, onBeforeUnmount, type Ref } from 'vue'

const prefersReduced = (): boolean =>
  typeof window !== 'undefined' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches

/** 滚动进入视口时给 .reveal 加 .in（IntersectionObserver，带 reduced-motion 降级）。 */
export function useReveal(scope: Ref<HTMLElement | null>): void {
  let io: IntersectionObserver | null = null
  onMounted(() => {
    const root = scope.value
    if (!root) return
    const els = Array.from(root.querySelectorAll<HTMLElement>('.reveal'))
    if (prefersReduced() || !('IntersectionObserver' in window)) {
      els.forEach((el) => el.classList.add('in'))
      return
    }
    io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add('in')
            io?.unobserve(entry.target)
          }
        })
      },
      { threshold: 0.16, rootMargin: '0px 0px -8% 0px' },
    )
    els.forEach((el) => io!.observe(el))
  })
  onBeforeUnmount(() => io?.disconnect())
}

/** 指标数字进入视口时从 0 count-up 到 data-count（带 reduced-motion 与无 IO 降级）。 */
export function useCountUp(scope: Ref<HTMLElement | null>): void {
  let io: IntersectionObserver | null = null
  onMounted(() => {
    const root = scope.value
    if (!root) return
    const nums = Array.from(root.querySelectorAll<HTMLElement>('.metric .num[data-count]'))
    const reduced = prefersReduced()
    const fmt = (v: number) => Math.round(v).toLocaleString('en-US')
    const unit = (s: string) => (s ? `<span class="u">${s}</span>` : '')
    const run = (el: HTMLElement) => {
      const target = parseFloat(el.dataset.count || '0')
      const suffix = el.dataset.suffix || ''
      if (reduced) {
        el.innerHTML = fmt(target) + unit(suffix)
        return
      }
      const dur = 1100
      const start = performance.now()
      const tick = (now: number) => {
        const p = Math.min(1, (now - start) / dur)
        const eased = 1 - Math.pow(1 - p, 3)
        el.innerHTML = fmt(target * eased) + unit(suffix)
        if (p < 1) requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    }
    if (reduced || !('IntersectionObserver' in window)) {
      nums.forEach(run)
      return
    }
    io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            run(entry.target as HTMLElement)
            io?.unobserve(entry.target)
          }
        })
      },
      { threshold: 0.5 },
    )
    nums.forEach((el) => io!.observe(el))
  })
  onBeforeUnmount(() => io?.disconnect())
}
