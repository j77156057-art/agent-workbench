/**
 * M2·③b 接线层的**纯状态机**：只负责"向来源取实时模式事实 + 失败兜底"，
 * **不 import vue、不碰 DOM/fetch、也不 import 任何其它 src 模块**，
 * 因而能被 Node 直接跑行为契约测试（realtimeModeDisplay.ts 的同一套离线可测约定）。
 *
 * 刻意不在此计算展示视图：纯映射（raw → view）留给 realtimeModeDisplay.describeRealtimeMode，
 * 由上层 vue 组合式函数把两者缝起来。这样 tracker 没有任何相对 import，
 * 既满足 vue-tsc 的 `moduleResolution: bundler`（不许 `.ts` 后缀），又能被裸 node --test 解析。
 *
 * 关键不变式（对齐开工文档 §5.4 / R13 的"降级要响亮"与 fail-closed）：
 * - 来源 `source()` 抛错、返回非对象、或带了 `error` 字段 → **一律退回未知态（raw=null）**，
 *   绝不保留上一次的 native 假象。宁可显示"模式未确认"（徽章仍亮、isDegraded=true），
 *   也不能静默停在"看起来是原生"。这是"绿了不代表对"的前端版。
 * - `busy` 只在一次 refresh 的飞行中为真，结束（成功/失败）必归 false，不卡态。
 * - `loaded` 记录"至少成功拿到过一次有效事实"，供上层决定是否提示"尚未连接"。
 */

/** 模式事实来源：REST 的 `() => visionApi.realtimeStatus()`，或未来 WS hello.ok 的缓存读取。 */
export type RealtimeStatusSource = () => Promise<unknown>

export interface RealtimeModeSnapshot {
  /** 最近一次**有效**的服务端事实；null 表示未知态（还没拿到 / 上一次拉取失败）。 */
  raw: Record<string, unknown> | null
  /** 是否有一次 refresh 正在飞行中。 */
  busy: boolean
  /** 最近一次失败的说明文案；成功时为空串。 */
  lastError: string
  /** 是否至少成功拿到过一次有效事实。 */
  loaded: boolean
}

export interface RealtimeModeTracker {
  refresh(): Promise<RealtimeModeSnapshot>
  reset(): void
  snapshot(): RealtimeModeSnapshot
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

/**
 * 造一个模式追踪器。`source` 由调用方注入——生产里是 `visionApi.realtimeStatus`，
 * 测试里塞个假 fetch 即可驱动"成功 / 抛错 / 返回 error 体 / 返回脏值"各分支。
 */
export function createRealtimeModeTracker(source: RealtimeStatusSource): RealtimeModeTracker {
  let raw: Record<string, unknown> | null = null
  let busy = false
  let lastError = ''
  let loaded = false

  const snapshot = (): RealtimeModeSnapshot => ({ raw, busy, lastError, loaded })

  return {
    snapshot,
    reset() {
      raw = null; busy = false; lastError = ''; loaded = false
    },
    async refresh() {
      busy = true
      try {
        const next = asRecord(await source())
        // fail-closed：拿不到对象、或后端明确回了 error 字段 → 归零到未知态，
        // 不保留上一次 native 假象。raw=null 时上层 describeRealtimeMode(null) 返回 unknown（徽章继续亮）。
        if (!next || next.error) {
          raw = null
          lastError = next && next.error ? String(next.error) : '实时模式读取失败（无有效响应）'
        } else {
          raw = next
          lastError = ''
          loaded = true
        }
      } catch (e) {
        raw = null
        lastError = e instanceof Error ? e.message : String(e)
      } finally {
        busy = false
      }
      return snapshot()
    },
  }
}
