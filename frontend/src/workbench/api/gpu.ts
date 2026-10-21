// GPU 监控 / 模型驻留 / 分区契约（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { FsApiError, postJson, request, withProject } from './_base'

export interface GpuHolder {
  owner: string
  purpose: string
  held_for_seconds: number | null
  lease_ttl: number
}
export interface GpuQueueItem {
  owner: string
  purpose: string
  /** 指定卡序号；null=自动挑最空的卡 */
  gpu: number | null
  waiting_gpu: boolean
}
export interface GpuInfo {
  index: number
  name: string | null
  used_mb: number | null
  total_mb: number | null
  free_mb: number | null
  utilization: number | null
  temperature_c: number | null
  holder: GpuHolder | null
  queue: GpuQueueItem[]
}
export interface GpuSamplePoint {
  t: number
  gpus: { index: number; used_mb: number; total_mb: number; utilization: number }[]
}
export interface GpuStatus {
  ok?: boolean
  mode: 'serial' | 'parallel' | 'multi'
  coordinating: boolean
  active: string | null
  purpose: string
  held_for_seconds: number | null
  holders: (GpuHolder & { gpu: number })[]
  queue: GpuQueueItem[]
  queue_length: number
  memory: { used_mb: number; total_mb: number; free_mb: number; utilization: number } | null
  gpus: GpuInfo[]
  samples: GpuSamplePoint[]
  ollama_idle: { unload_seconds: number; last_activity_ago: number | null; last_unload_ago: number | null }
  hooks: string[]
  processes?: { pid:number; owner:string; gpu:number|null; purpose:string; status:string; last_heartbeat:number }[]
  compute_apps?: { pid:number; process_name:string; used_mb:number }[]
  recovery_events?: Record<string, unknown>[]
}

/** 本地推理槽（本地模型串行闸）诊断：GET /api/gpu/local-slots */
export interface LocalSlotHolder {
  /** 用途@线程名，例如 "subagent:coder@docmind-chat-agent" */
  tag: string | null
  held_seconds: number
}
export interface LocalSlots {
  ok?: boolean
  /** 等槽上限（秒）；0 = 无限等待 */
  wait_timeout_seconds: number
  /** 超时被放行的累计次数（>0 说明确实发生过"槽位被长期占用"） */
  wait_exceeded: number
  /** "provider/model" → 当前持有者 */
  holders: Record<string, LocalSlotHolder>
}

export const gpuApi = {
  status() { return request<GpuStatus>('/api/gpu/status') },
  localSlots() { return request<LocalSlots>('/api/gpu/local-slots') },
  setSlotTimeout(seconds: number) {
    return postJson<LocalSlots & { ok: boolean }>('/api/gpu/local-slots/wait-timeout', { seconds })
  },
  cancel(owner: string) { return postJson<{ ok: boolean; canceled?: number; error?: string }>('/api/gpu/cancel', { owner }) },
  forceRelease(owner?: string) { return postJson<{ ok: boolean; released?: string; error?: string }>('/api/gpu/force-release', { owner: owner ?? '' }) },
  configure(idleUnloadSeconds?: number, pollInterval?: number) {
    return postJson<GpuStatus & { ok: boolean }>('/api/gpu/configure', {
      idle_unload_seconds: idleUnloadSeconds,
      poll_interval: pollInterval,
    })
  },
  environment(deviceIndex = -1) {
    return request<{ ok: boolean; environment: Record<string, string> }>(`/api/gpu/environment?device_index=${deviceIndex}`)
  },
}

/** 本地模型驻留（显存占用）：GET /api/model_status + POST /api/model_power。
 *  本地大模型默认长期驻留显存，切模型 / 跑 ComfyUI 生图前可一键卸载腾显存。 */
export interface ModelResident {
  name: string
  size_vram_gb: number
  size_gb: number
  expires_minutes: number | null
  processor: string
}
export interface ModelStatus {
  reachable: boolean
  loaded: ModelResident[]
  vram_gb: number
  needs_ollama: boolean
  needed_models?: string[]
  error?: string
}
export interface ModelPowerResult {
  ok: boolean
  action?: string
  unloaded?: string[]
  preloaded?: string[]
  skipped?: { name: string; size_gb: number; free_gb: number }[]
  loaded?: ModelResident[]
  vram_gb?: number
  error?: string
}
export const modelResidencyApi = {
  status() { return request<ModelStatus>('/api/model_status') },
  /** off=立即卸载全部驻留模型（释放显存，下次对话自动重载）；on=预加载当前配置所需模型 */
  power(action: 'off' | 'on') {
    return postJson<ModelPowerResult>('/api/model_power', { action })
  },
}

/** P3：分区状态（GET /api/regions → regions.list_regions） */
export interface RegionInfo {
  key: string
  name: string
  dir: string
  desc: string
  access: string
  depends_on: string[]
  exports: string[]
  /** 目录已存在但缺失的导出文件（空=契约完整或目录尚未创建） */
  missing_exports: string[]
  verify: string
  exists: boolean
  /** 该分区目录是否为独立 git 仓库 */
  git: boolean
  branch: string
  dirty: boolean
  files: number
}
export interface RegionsResp {
  code_root: string
  regions: RegionInfo[]
}
export interface ContractsResp {
  ok: boolean
  errors: string[]
  /** key → 依赖的 key 列表（DAG） */
  graph: Record<string, string[]>
}

export const regionsApi = {
  list(): Promise<RegionsResp> {
    return request<RegionsResp>('/api/regions')
  },
  /**
   * 注意：契约校验失败是正常业务结果（HTTP 200 + {ok:false,errors}），
   * 不能走通用 request（它会把 body.ok===false 当错误抛出）。
   */
  async contracts(): Promise<ContractsResp> {
    let res: Response
    try {
      res = await fetch('/api/verify_contracts', withProject())
    } catch {
      throw new FsApiError(0, '无法连接本地服务（127.0.0.1:8000），请确认 Agent 工作台 已启动。')
    }
    let body: ContractsResp | null = null
    try {
      body = (await res.json()) as ContractsResp
    } catch {
      /* 非 JSON */
    }
    if (!res.ok) {
      throw new FsApiError(res.status, body?.errors?.join('；') || `请求失败（HTTP ${res.status}）`)
    }
    return {
      ok: !!body?.ok,
      errors: body?.errors || [],
      graph: body?.graph || {},
    }
  },
  /** 一键创建缺失分区（目录 + 导出接口桩/README） */
  createRegion(key: string): Promise<CreateRegionResp> {
    return postJson<CreateRegionResp>('/api/regions/create', { key })
  },
  /** 为已存在但缺导出文件的分区补齐导出桩 */
  fillExports(key: string): Promise<FillExportsResp> {
    return postJson<FillExportsResp>('/api/regions/fill_exports', { key })
  },
}

/** POST /api/regions/create 响应：创建结果 + 刷新后的全量分区状态 */
export interface CreateRegionResp {
  ok: boolean
  key: string
  name: string
  dir: string
  created_dir: boolean
  created_files: string[]
  git_warning: string
  regions: RegionInfo[]
}

/** POST /api/regions/fill_exports 响应 */
export interface FillExportsResp {
  ok: boolean
  key: string
  name: string
  dir: string
  created_files: string[]
  regions: RegionInfo[]
}
