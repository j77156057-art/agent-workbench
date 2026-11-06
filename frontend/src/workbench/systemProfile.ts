// 能力画像的前端判定：把后端静态画像与浏览器侧信号合并成最终 tier。
//
// 浏览器能读到的都是粗信号（核数、deviceMemory 档位、JS 堆上限、reduce-motion、
// WebGL 渲染器字符串），只作加权或「软件渲染」这种明确否决，绝不据此猜占用率。

export type CapabilityTier = 'high' | 'mid' | 'low'

export interface ProfileReason {
  kind: string
  detail: string
  value: string
  threshold: string
}

export interface ServerProfile {
  tier: CapabilityTier
  reasons: ProfileReason[]
  gpu?: {
    models?: string[]
    available?: boolean
    reason?: string
    software_render?: boolean
    discrete?: boolean
  }
}

export interface BrowserSignals {
  device_memory_gb: number | null
  js_heap_limit_mb: number | null
  prefers_reduced_motion: boolean
  has_raf: boolean
  webgl_renderer: string | null
  webgl_software: boolean
}

export interface MergedProfile {
  tier: CapabilityTier
  reasons: ProfileReason[]
}

const SOFTWARE_MARKERS = ['swiftshader', 'llvmpipe', 'softpipe', 'software rasterizer']
const WEAK_MARKERS = ['mali-400', 'mali 400', 'hd graphics']

function isWeakRenderer(name: string): boolean {
  const low = name.toLowerCase()
  if (SOFTWARE_MARKERS.some(marker => low.includes(marker))) return true
  if (low.includes('uhd') || low.includes('iris')) return false
  const match = low.match(/hd graphics\s*(\d{3,4})/)
  if (match) {
    const model = Number(match[1])
    if (Number.isFinite(model)) return model <= 4600
  }
  return WEAK_MARKERS.some(marker => low.includes(marker))
}

function looksDiscrete(name: string): boolean {
  const low = name.toLowerCase()
  return ['nvidia', 'geforce', 'quadro', 'rtx ', 'gtx ', 'radeon rx', 'radeon pro', 'intel arc']
    .some(marker => low.includes(marker))
}

/** 读取浏览器侧粗信号；无 window（node 测试）时全部为 null / false。 */
export function collectBrowserSignals(win: Window | null = typeof window !== 'undefined' ? window : null): BrowserSignals {
  const out: BrowserSignals = {
    device_memory_gb: null,
    js_heap_limit_mb: null,
    prefers_reduced_motion: false,
    has_raf: false,
    webgl_renderer: null,
    webgl_software: false,
  }
  if (!win) return out
  const nav = win.navigator as Navigator & { deviceMemory?: number }
  if (typeof nav.deviceMemory === 'number' && Number.isFinite(nav.deviceMemory)) {
    out.device_memory_gb = nav.deviceMemory
  }
  const perf = win.performance as Performance & { memory?: { jsHeapSizeLimit?: number } }
  if (perf.memory && typeof perf.memory.jsHeapSizeLimit === 'number') {
    out.js_heap_limit_mb = Math.round(perf.memory.jsHeapSizeLimit / (1024 * 1024))
  }
  try {
    out.prefers_reduced_motion = win.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    out.prefers_reduced_motion = false
  }
  out.has_raf = typeof win.requestAnimationFrame === 'function'
  try {
    const canvas = win.document.createElement('canvas')
    const gl = (canvas.getContext('webgl') || canvas.getContext('experimental-webgl')) as WebGLRenderingContext | null
    if (gl) {
      const ext = gl.getExtension('WEBGL_debug_renderer_info')
      const renderer = ext
        ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || '')
        : String(gl.getParameter(gl.RENDERER) || '')
      out.webgl_renderer = renderer || null
      out.webgl_software = !!renderer && SOFTWARE_MARKERS.some(marker => renderer.toLowerCase().includes(marker))
    }
  } catch {
    out.webgl_renderer = null
  }
  return out
}

/**
 * 合并画像。原则：
 * - 后端判 low 保持 low（前端不得「洗白」）；
 * - 浏览器只提供明确证据时才把 high/mid 拉低（软件渲染 / 已知弱卡）；
 * - deviceMemory、JS 堆、reduce-motion 只加权，不单独否决；
 * - 后端画像缺失（null）时返回 mid —— 未知不等于低端。
 */
export function judgeProfile(server: ServerProfile | null, browser: BrowserSignals): MergedProfile {
  const reasons: ProfileReason[] = [...(server?.reasons || [])]
  let tier: CapabilityTier = server?.tier || 'mid'
  let hardLow = tier === 'low'
  let weighted = 0

  const renderer = browser.webgl_renderer
  if (renderer && isWeakRenderer(renderer)) {
    hardLow = true
    reasons.push({
      kind: browser.webgl_software ? 'gpu_software_render' : 'gpu_weak_model',
      detail: '浏览器 WebGL 渲染器：' + renderer,
      value: renderer,
      threshold: browser.webgl_software
        ? '命中 SwiftShader / llvmpipe / Software'
        : 'Intel HD 4xxx 及更早 / Mali-400',
    })
  } else if (renderer && server?.gpu?.available && server.gpu.discrete === false && looksDiscrete(renderer)) {
    // 后端枚举与浏览器报告不一致时，以能明确说明来源的浏览器字符串为准（仅记录）。
    reasons.push({
      kind: 'gpu_renderer_disagree',
      detail: '浏览器报告独显而系统枚举为集显：' + renderer,
      value: renderer,
      threshold: '仅作信息，不参与降级',
    })
  }

  if (browser.device_memory_gb !== null && browser.device_memory_gb <= 4) {
    weighted += 2
    reasons.push({
      kind: 'device_memory',
      detail: '浏览器 deviceMemory 档位 ≤ 4GB',
      value: browser.device_memory_gb + 'GB',
      threshold: 'deviceMemory ≤ 4GB（加权 +2）',
    })
  }
  if (browser.js_heap_limit_mb !== null && browser.js_heap_limit_mb > 0 && browser.js_heap_limit_mb < 512) {
    weighted += 1
    reasons.push({
      kind: 'js_heap',
      detail: 'JS 堆上限偏小',
      value: browser.js_heap_limit_mb + 'MB',
      threshold: 'jsHeapSizeLimit < 512MB（加权 +1）',
    })
  }
  if (browser.prefers_reduced_motion) {
    weighted += 1
    reasons.push({
      kind: 'reduced_motion',
      detail: '系统已开启「减少动态效果」',
      value: 'on',
      threshold: 'prefers-reduced-motion（加权 +1）',
    })
  }
  if (!browser.has_raf) {
    weighted += 1
    reasons.push({
      kind: 'no_raf',
      detail: '浏览器缺少 requestAnimationFrame',
      value: 'missing',
      threshold: '无长帧观测能力（加权 +1）',
    })
  }

  if (hardLow) tier = 'low'
  else if (tier === 'high' && weighted >= 2) tier = 'mid'

  return { tier, reasons }
}
