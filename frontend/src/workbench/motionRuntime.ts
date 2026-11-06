// 运行时画像状态：一次拉取 /api/system/resources，合并浏览器信号后供 UI 与动效共用。
// 采样与判定都只在启动时做一次（资源数值只在弹层里展示，不参与逐帧决策）。
import { reactive } from 'vue'
import { systemResourcesApi, type SystemResources } from './api'
import {
  collectBrowserSignals,
  judgeProfile,
  type BrowserSignals,
  type CapabilityTier,
  type ProfileReason,
} from './systemProfile'

export interface MotionRuntimeState {
  resources: SystemResources | null
  browser: BrowserSignals
  tier: CapabilityTier
  reasons: ProfileReason[]
  loading: boolean
  loaded: boolean
  error: string
  /** 动效层实时状态（由 status bar 从 DOM 观测字段回填） */
  degraded: boolean
  overrunPercent: number
  refreshMs: number
}

export const motionRuntime = reactive<MotionRuntimeState>({
  resources: null,
  browser: collectBrowserSignals(),
  tier: 'mid',
  reasons: [],
  loading: false,
  loaded: false,
  error: '',
  degraded: false,
  overrunPercent: 0,
  refreshMs: 16.7,
})

let pending: Promise<void> | null = null

/** 幂等加载；失败时保持 mid（未知不等于低端），错误只用于提示，不参与降级。 */
export function loadMotionProfile(): Promise<void> {
  if (pending) return pending
  motionRuntime.loading = true
  pending = systemResourcesApi.resources()
    .then(resources => {
      motionRuntime.resources = resources
      motionRuntime.browser = collectBrowserSignals()
      const merged = judgeProfile(resources.profile, motionRuntime.browser)
      motionRuntime.tier = merged.tier
      motionRuntime.reasons = merged.reasons
      motionRuntime.loaded = true
    })
    .catch((error: unknown) => {
      motionRuntime.error = error instanceof Error ? error.message : String(error)
      motionRuntime.tier = 'mid'
      motionRuntime.reasons = [{
        kind: 'profile_unavailable',
        detail: '系统资源接口不可用，按中等能力运行：' + motionRuntime.error,
        value: 'unavailable',
        threshold: '未知不等于低端',
      }]
      motionRuntime.loaded = true
    })
    .finally(() => {
      motionRuntime.loading = false
    })
  return pending
}
