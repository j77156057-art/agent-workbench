// 系统资源快照（GET /api/system/resources）。采样在服务端后台线程里做，
// 浏览器只读结果；GPU 不可用时 available=false + reason，占用率保持 null。
import { request } from './_base'

export interface SystemCpu {
  overall_percent: number | null
  process_percent: number | null
  physical_cores: number | null
  logical_cores: number | null
  freq_mhz: number | null
  freq_max_mhz: number | null
  freq_known?: boolean
  warming_up?: boolean
  sampled_at?: string
}

export interface SystemMemory {
  total_bytes: number | null
  used_bytes: number | null
  available_bytes: number | null
  percent: number | null
  error?: string
}

export interface SystemDisk {
  path: string
  total_bytes: number | null
  used_bytes: number | null
  free_bytes: number | null
  percent: number | null
  error?: string
}

export interface SystemGpu {
  available: boolean
  reason: string
  source: string | null
  index?: number | null
  name: string | null
  utilization_percent: number | null
  memory_used_mb: number | null
  memory_total_mb: number | null
  driver_version?: string | null
  sampled_at?: string | null
}

export interface ProfileReason {
  kind: string
  detail: string
  value: string
  threshold: string
}

export interface SystemServerProfile {
  tier: 'high' | 'mid' | 'low'
  reasons: ProfileReason[]
  cpu?: Record<string, unknown>
  gpu?: {
    models?: string[]
    available?: boolean
    reason?: string
    software_render?: boolean
    discrete?: boolean
  }
  system?: Record<string, unknown>
  memory_total_gb?: number | null
}

export interface SystemResources {
  cpu: SystemCpu
  memory: SystemMemory
  disk: SystemDisk
  gpu: SystemGpu
  profile: SystemServerProfile | null
  sampled_at: string
}

export const systemResourcesApi = {
  resources() { return request<SystemResources>('/api/system/resources') },
}
