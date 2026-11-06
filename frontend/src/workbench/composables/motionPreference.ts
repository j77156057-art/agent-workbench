import { onBeforeUnmount, ref } from 'vue'

export const reducedMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')

export function useReducedMotion() {
  const reduced = ref(reducedMotionQuery.matches)
  const sync = () => { reduced.value = reducedMotionQuery.matches }
  reducedMotionQuery.addEventListener('change', sync)
  onBeforeUnmount(() => reducedMotionQuery.removeEventListener('change', sync))
  return reduced
}

export function preferredScrollBehavior(): ScrollBehavior {
  return reducedMotionQuery.matches ? 'auto' : 'smooth'
}
