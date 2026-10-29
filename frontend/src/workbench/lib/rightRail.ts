/**
 * 右侧预览栏（工作台第三栏）的尺寸与地址口径 —— 纯函数，无 Vue / DOM 依赖。
 *
 * ## 为什么抽出来
 *
 * 三件事判错的后果都很直接：
 *   - 宽度夹取错了：主区被挤到不可读，或拖到一半卡死在视口外；
 *   - 地址规整错了：`javascript:` 之类的地址被塞进 iframe / 新标签；
 *   - 可嵌入判定错了：对同源内容也弹「站点禁止嵌入」，或对外部站点假装能看。
 * 这些口径放这里，组件只负责摆位置；node 单测直接跑本模块。
 */

/** 默认 / 最小 / 最大宽度；主区至少保留 520px（对话正文仍可读）。 */
export const RIGHT_RAIL_DEFAULT_W = 460
export const RIGHT_RAIL_MIN_W = 340
export const RIGHT_RAIL_MAX_W = 720
export const RIGHT_RAIL_MAIN_MIN_W = 520
/** 低于这个视口宽度不启用第三栏（固定布局左栏 288 + 主区 520 已接近满载）。 */
export const RIGHT_RAIL_MIN_VIEWPORT = 1200

export interface RightRailClampOpts {
  innerWidth: number
  /** 左栏实际占宽（固定布局下是 288 或拖拽后的值）。 */
  leftWidth?: number
  /** 左栏已收起：不再占横向空间。 */
  leftCollapsed?: boolean
}

/**
 * 把期望宽度夹进 [MIN, MAX] 与「主区至少 520px」的视口上限。
 * 上限同时受左栏影响：左栏收得越窄，右栏允许拖得越宽。
 */
export function clampRightRailWidth(width: number, opts: RightRailClampOpts): number {
  const inner = Number(opts.innerWidth)
  if (!Number.isFinite(inner) || inner <= 0) return RIGHT_RAIL_DEFAULT_W
  const left = opts.leftCollapsed ? 0 : Math.max(0, Number(opts.leftWidth) || 0)
  const ceiling = Math.max(
    RIGHT_RAIL_MIN_W,
    Math.min(RIGHT_RAIL_MAX_W, inner - left - RIGHT_RAIL_MAIN_MIN_W),
  )
  const wanted = Number.isFinite(width) ? width : RIGHT_RAIL_DEFAULT_W
  return Math.round(Math.max(RIGHT_RAIL_MIN_W, Math.min(ceiling, wanted)))
}

/**
 * 当前是否具备第三栏的空间与布局条件。
 *
 * 只认 `fixed`（默认的固定三栏）：dock 是竖排、split 是对半分，
 * 强塞第三栏会破坏那套布局的空间语义。调用方可注入参数便于单测。
 */
export function canUseRightRail(opts?: { layout?: string | null; innerWidth?: number }): boolean {
  const layout = opts?.layout !== undefined
    ? opts.layout
    : (typeof document !== 'undefined' ? document.documentElement.getAttribute('data-layout') : 'fixed')
  const width = opts?.innerWidth !== undefined
    ? opts.innerWidth
    : (typeof window !== 'undefined' ? window.innerWidth : 0)
  return (layout || 'fixed') === 'fixed' && Number(width) >= RIGHT_RAIL_MIN_VIEWPORT
}

export interface NormalizedAddress {
  ok: boolean
  /** 规整后的地址；ok=false 时为空。 */
  url: string
  error?: string
}

const LOCAL_HOST = /^(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0|[\w-]+\.local)(:\d+)?(\/.*)?$/i

function isLocalHost(host: string): boolean {
  const h = host.replace(/^\[|\]$/g, '').toLowerCase()
  return h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '0.0.0.0' || h.endsWith('.local')
}

/**
 * 地址栏输入规整。
 *
 * 与浏览器地址栏同样的取舍：用户敲 `localhost:5199` 要能开，
 * 但 `javascript:` / `data:` / `file:` 一律拒绝（右栏不是特权宿主）。
 * 不带协议的普通域名默认 https，本机地址默认 http。
 *
 * 注意 `localhost:5199` 里的 `localhost:` 长得像协议名（RFC 3986），
 * 所以「协议」判定只看两种明确形态：`scheme://` 与非数字的 `scheme:`。
 * 冒号后是纯数字时按「主机:端口」处理。
 */
export function normalizeAddress(raw: string): NormalizedAddress {
  const text = String(raw || '').trim()
  if (!text) return { ok: false, url: '', error: '请输入网址' }

  // 同源相对路径：/play/...、/api/...
  if (text.startsWith('/') && !text.startsWith('//')) return { ok: true, url: text }
  // 协议相对：交给当前页面协议解析
  if (text.startsWith('//')) {
    const proto = typeof window !== 'undefined' ? window.location.protocol : 'http:'
    return { ok: true, url: proto + text }
  }

  // 明确的 scheme:// 形态
  const withSlashes = text.match(/^([a-z][a-z0-9+.-]*):\/\//i)
  if (withSlashes) {
    const name = withSlashes[1].toLowerCase()
    if (name === 'http' || name === 'https') return { ok: true, url: text }
    return { ok: false, url: '', error: `不支持 ${name}: 地址` }
  }
  // 其它「协议:非数字」形态：javascript:/data:/file:/mailto: 等
  if (/^[a-z][a-z0-9+.-]*:(?!\d)/i.test(text)) {
    return { ok: false, url: '', error: '只支持 http / https 网址' }
  }

  // 主机:端口（含 [::1]:8000）
  const hostPort = text.match(/^([a-z0-9.-]+|\[[0-9a-f:]+\]):(\d+)(?:[/?#].*)?$/i)
  if (hostPort) {
    return { ok: true, url: (isLocalHost(hostPort[1]) ? 'http://' : 'https://') + text }
  }

  if (LOCAL_HOST.test(text)) return { ok: true, url: 'http://' + text }
  if (/^[a-z0-9-]+(\.[a-z0-9-]+)+(\/.*)?$/i.test(text)) return { ok: true, url: 'https://' + text }
  return { ok: false, url: '', error: '这不是一个可识别的网址' }
}

export type PreviewKind = 'same-origin' | 'external' | 'unsupported'

/**
 * 这个地址该怎么预览：
 *   - same-origin：应用自己的路径（/play、/api/...），iframe 可信；
 *   - external：外部站点，iframe 可能被对方的 X-Frame-Options/CSP 拦；
 *   - unsupported：不要尝试。
 */
export function previewKindOfUrl(url: string, origin?: string): PreviewKind {
  const text = String(url || '').trim()
  if (!text) return 'unsupported'
  if (text.startsWith('/') && !text.startsWith('//')) return 'same-origin'
  if (/^https?:\/\//i.test(text)) {
    const base = origin ?? (typeof window !== 'undefined' ? window.location.origin : '')
    try {
      return base && new URL(text).origin === base ? 'same-origin' : 'external'
    } catch {
      return 'external'
    }
  }
  return 'unsupported'
}

/**
 * 需要加 sandbox 的预览：模型/项目生成的 workflow 预览产物。
 * 不授予 allow-same-origin —— 这类内容不是应用本身，不该拿应用源的权限。
 */
export function previewSandboxFor(url: string): string | undefined {
  return /\/preview\/artifact\//i.test(String(url || ''))
    ? 'allow-scripts allow-forms allow-popups'
    : undefined
}
