/**
 * M2·③：把实时模式的**服务端事实**归一成前端可显示的形状，"降级不再隐形"的第一块砖。
 *
 * 背景（docs/multimodal-verification-engine-eda-kickoff.md §5.4、R13）：
 * 后端在两个地方把模式讲清楚了——
 *   - REST `/api/vision/realtime-status`（连接前，`describe()+resolve()`）
 *   - WS `hello.ok`（连接后，`SessionBridge.hello_fields()` 追加 mode/degraded_to/reason/
 *     provider/provider_capabilities）
 * 但前端两处都没读，所以"画质掉回抽帧"对用户完全隐形——这是开工文档明列的坑。
 *
 * 这个模块只做**纯展示归一**：输入两个形状任一（REST 或 WS），输出一个可以直接绑到
 * 徽章组件的 view model。它不 fetch、不订阅、不改状态，所以能被 Node 单测直接跑（无 DOM、
 * 无 Vite）。真正的 WS/REST 客户端接线是下一步——但**一旦接上，显示层就是零成本**：
 * 组件吃这个 helper 就再也不会漏掉降级说明。
 */
export type RealtimeMode =
  | 'native-realtime'    // 原生实时（hello.ok 与 REST 的正式值）
  | 'native'             // REST 里的简写别名（model.ts 声明过）
  | 'sampled-frames'     // 兼容抽帧降级
  | 'unavailable'        // 完全没视觉能力
  | ''                   // 未确认

export interface RealtimeModeView {
  /** 展示在主徽章上的短标签。 */
  label: string
  /** 语气：native=绿、degraded=琥珀、unavailable=灰、unknown=灰。 */
  tone: 'native' | 'degraded' | 'unavailable' | 'unknown'
  /** 是否算"降级中"——决定是否显示醒目底色。 */
  isDegraded: boolean
  /** 悬停/展开时的解释文本；无解释时回落到 label。 */
  hint: string
  /** 后端提供的能力列表（provider_capabilities/capabilities）；未知时为空。 */
  capabilities: string[]
}

interface MaybeString { toString?: () => string }

function s(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  const ms = v as MaybeString
  return ms && typeof ms.toString === 'function' ? ms.toString() : ''
}

function list(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(s).filter(Boolean)
  return []
}

/**
 * 把 REST `realtimeStatus()` 返回或 WS `hello.ok` 事件原样丢进来，出一个统一的展示视图。
 * 两个来源字段名略不同，这里都吃：
 *   REST: { mode, mode_label, mode_reason, native_available, native_reason, limitations }
 *   WS:   { mode, degraded_to, reason, provider, provider_capabilities }
 * `mode` 认 'native-realtime' / 'native' / 'sampled-frames' / 'unavailable'，其余归 unknown。
 * **不猜**：没字段就当 unknown；native 但 degraded_to 非空 → 仍按降级处理，不装没事。
 */
export function describeRealtimeMode(raw: Record<string, unknown> | null | undefined): RealtimeModeView {
  const info = (raw || {}) as Record<string, unknown>
  const modeRaw = s(info.mode).trim()
  const degradedTo = s(info.degraded_to)
  const nativeAvailableFlag = info.native_available === undefined ? null : Boolean(info.native_available)
  // REST 用 native_available，WS 用 degraded_to==null 判原生；任一说不原生 → 不原生。
  let native = modeRaw === 'native-realtime' || modeRaw === 'native'
  if (nativeAvailableFlag === false) native = false
  if (degradedTo) native = false

  const unavailable = modeRaw === 'unavailable'
    || degradedTo === 'unavailable'
    || (nativeAvailableFlag === false && !degradedTo && modeRaw !== 'sampled-frames' && !native)

  const caps = list(info.provider_capabilities ?? info.capabilities ?? info.limitations)

  if (unavailable) {
    return {
      label: '视觉不可用',
      tone: 'unavailable',
      isDegraded: true,
      hint: s(info.mode_reason) || s(info.native_reason) || s(info.reason) || '实时视觉与抽帧兜底都不可用，只看代码/日志。',
      capabilities: caps,
    }
  }
  if (native) {
    return {
      label: '原生实时',
      tone: 'native',
      isDegraded: false,
      hint: s(info.mode_reason) || s(info.reason) || '已连上原生实时模型，音视频双向通道开启。',
      capabilities: caps,
    }
  }
  if (modeRaw === 'sampled-frames' || degradedTo === 'sampled-frames' || nativeAvailableFlag === false) {
    return {
      label: '兼容抽帧（已降级）',
      tone: 'degraded',
      isDegraded: true,
      hint: s(info.mode_reason) || s(info.reason) || s(info.native_reason)
        || '原生实时不可用，已回退到"最新帧 + 单轮问答"模式；口播、打断、连续观察都拿不到。',
      capabilities: caps,
    }
  }
  return {
    label: '模式未确认',
    tone: 'unknown',
    isDegraded: true,
    hint: '还没拿到 hello.ok / realtime-status；连接前先按未知处理，绝不假装原生。',
    capabilities: caps,
  }
}
