// 模型配置 / 能力画像 / 视觉理解（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { projectRequest, rawJson, request, withProject } from './_base'
import type { VisionAnomaly } from '../liveVisionAlerts'

// ---------------------------------------------------------------- 模型设置（/api/config）
export interface ProviderMeta {
  label: string
  base_url: string
  default_model: string
  needs_key: boolean
  cloud: boolean
}

export interface ModelCapability {
  context_window: number
  /** native=模型天生推理 / toggle=可开关 / none=不支持 / unknown=待确认 */
  thinking: 'native' | 'toggle' | 'none' | 'unknown' | string
  /** native=原生图片 / harness=Harness 视觉辅助 / none=不支持 / unknown=待确认 */
  vision: 'native' | 'harness' | 'none' | 'unknown' | string
  /** native=原生视频 / frames=Harness 抽帧 / none=不支持 / unknown=待确认 */
  video: 'native' | 'frames' | 'none' | 'unknown' | string
  cloud: boolean
}

/** 实时通道的只读观测（R5 时间线 + R10 指标）。**不含模式** —— 模式的唯一来源是
 *  `/api/vision/realtime-status` 与每次会话的 hello.ok.mode；这里再报一份会出现
 *  两个可能互相矛盾的 mode（后端 status_snapshot 的注释写明了这一点）。 */
export interface RealtimeMetricScope {
  histograms: Record<string, { count: number; window: number; min: number | null; max: number | null; p50: number | null; p95: number | null }>
  counters: Record<string, number>
  gauges: Record<string, number | null>
}
export interface RealtimeTimelineEntry {
  kind: string
  captured_at: number
  session_id?: string
  sequence?: number
  text?: string
  [k: string]: unknown
}
export interface RealtimeStatusResp {
  timeline: { project_id: string; count: number; dropped_other_project: number; entries: RealtimeTimelineEntry[]; active_sessions: number } | null
  timeline_projects: string[]
  metrics: { global: RealtimeMetricScope; sessions: Record<string, RealtimeMetricScope> }
}

export const visionApi = {
  /** 实时通道指标与时间线（项目级；project_id 为空时只有进程级视图）。 */
  realtimeChannelStatus(): Promise<RealtimeStatusResp> {
    return fetch('/api/vision/realtime/status', withProject()).then((res) => res.json() as Promise<RealtimeStatusResp>)
  },
  realtimeStatus(): Promise<{
    ok: boolean; mode?: 'sampled-frames' | 'native-realtime' | 'native' | 'unavailable' | string; mode_label?: string
    mode_reason?: string; native_provider?: string | null; native_available?: boolean
    native_reason?: string; limitations?: string[]; error?: string
  }> {
    return fetch('/api/vision/realtime-status', withProject()).then(async (res) => (await res.json()) as {
      ok: boolean; mode?: 'sampled-frames' | 'native-realtime' | 'native' | 'unavailable' | string; mode_label?: string
      mode_reason?: string; native_provider?: string | null; native_available?: boolean
      native_reason?: string; limitations?: string[]; error?: string
    })
  },
  locateClick(description: string, goal = ''): Promise<{
    ok: boolean; error?: string; proposal_id?: string; image?: string; bbox?: number[]
    point?: { x: number; y: number }; label?: string; evidence?: string; expires_in?: number
  }> {
    return fetch('/api/vision/locate-click', withProject({
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ description, goal }),
    })).then(res => res.json())
  },
  executeClick(proposalId: string): Promise<{
    ok: boolean; executed?: boolean; before?: string | null; after?: string | null; label?: string; error?: string
    verification?: { status: 'met' | 'unmet' | 'uncertain' | 'unavailable'; evidence: string; confidence?: number; next_target?: string }
  }> {
    return fetch('/api/vision/execute-click', withProject({
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proposal_id: proposalId, confirmed: true }),
    })).then(res => res.json())
  },
  analyzeVideo(file: File): Promise<{ ok: boolean; context?: string[]; info?: { frame_count?: number }; error?: string }> {
    const fd = new FormData()
    fd.append('file', file, file.name || 'clip.mp4')
    return fetch('/api/vision/video', withProject({ method: 'POST', body: fd }))
      .then(async (res) => (await res.json()) as { ok: boolean; context?: string[]; info?: { frame_count?: number }; error?: string })
  },
  analyzeLiveFrame(blob: Blob, previousObservation = '', signal?: AbortSignal, focused = false): Promise<{
    ok: boolean; accepted?: boolean; throttled?: boolean; retry_after?: number
    observations?: string[]; anomalies?: VisionAnomaly[]; audit?: { mode?: string; elapsed_ms?: number; error?: string }
    error?: string
  }> {
    const fd = new FormData()
    fd.append('file', blob, `live-frame-${Date.now()}.${blob.type === 'image/jpeg' ? 'jpg' : 'png'}`)
    if (previousObservation) fd.append('previous_observation', previousObservation.slice(-900))
    if (focused) fd.append('focused_region', '1')
    return fetch('/api/vision/frame', withProject({ method: 'POST', body: fd, signal }))
      .then(async (res) => (await res.json()) as {
        ok: boolean; accepted?: boolean; throttled?: boolean; retry_after?: number
        observations?: string[]; anomalies?: VisionAnomaly[]; audit?: { mode?: string; elapsed_ms?: number; error?: string }
        error?: string
      })
  },
  captureDesktopFrame(target: 'embedded' | 'foreground' = 'embedded', strictProject = false): Promise<{
    ok: boolean; image?: string; target?: string; captured_at?: string; error?: string
  }> {
    return fetch('/api/vision/desktop-frame', withProject({
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target, strict_project: strictProject }),
    })).then(async (res) => (await res.json()) as {
      ok: boolean; image?: string; target?: string; captured_at?: string; error?: string
    })
  },
}

export interface OllamaStatus {
  reachable: boolean
  needed_models?: string[]
  present_models?: string[]
  missing_models?: string[]
  guidance?: string
  /** 对话模型本身不可用 —— 真的没法聊天，横幅用告警语气。 */
  blocks_conversation?: boolean
  /** 仅向量检索不可用 —— 可降级，横幅用普通提示语气。 */
  search_degraded?: boolean
}

export interface ModelConfigInfo {
  ok?: boolean
  llm_provider: string
  llm_model: string
  embedding_provider: string
  has_key: boolean
  providers: string[]
  provider_meta: Record<string, ProviderMeta>
  custom_base_url: string
  capability: ModelCapability
  ollama_status?: OllamaStatus
  /** 用户手填的窗口覆盖；0/缺省 = 自动（实时探测或内置画像） */
  context_window_override?: number
  capability_override?: { thinking?: string; vision?: string; video?: string }
  /** 当前生效窗口来源：custom 手填 / probe Ollama 实时探测 / profile 内置画像 */
  context_source?: 'custom' | 'probe' | 'profile'
  /** AI 越界访问模式：safe=仅限项目内；high=允许受控越界读写（须配置白名单） */
  external_access_mode?: 'safe' | 'high'
  // 以下字段问答页（/）使用，后端 /api/config 始终返回；工作台类型里保留为可选
  /** 已入库资料文档文件名列表（/api/ingest 上传的 PDF/MD/TXT） */
  ingested_files?: string[]
  /** 当前代码库切片条数 */
  code_sources?: number
  /** 当前代码库根目录 */
  code_root?: string
  /** AI 改文件前是否需要人工确认 */
  edit_confirm?: boolean
  /** 模型选择模式：fixed=固定模型；auto=按任务复杂度自动在本地/云端间路由 */
  llm_mode?: 'fixed' | 'auto'
  /** 自动模式下复杂任务使用的云端预设 id */
  auto_cloud_preset_id?: string
  /** 已保存的命名模型预设（含当前项目是否已存 Key） */
  model_presets?: ModelPreset[]
}

/** 命名模型预设：一键切换整套云端/本地配置（Key 存项目密钥库，不回显） */
export interface ModelPreset {
  id: string
  label: string
  provider: string
  model: string
  base_url: string
  /** 0=自动探测/画像 */
  context_window: number
  created_at?: string
  updated_at?: string
  /** 当前项目下该预设是否已保存 API Key */
  has_key?: boolean
}

export interface SaveModelReq {
  provider: string
  model?: string
  api_key?: string
  base_url?: string
  /** >0 设置窗口覆盖；0 清除覆盖回到自动；undefined=不改动 */
  context_window?: number | null
  thinking_capability?: string
  vision_capability?: string
  video_capability?: string
  /** AI 越界访问模式：'safe' | 'high'；undefined=不改动 */
  external_access_mode?: 'safe' | 'high'
  /** 模型选择模式：'fixed' | 'auto'；undefined=不改动 */
  llm_mode?: 'fixed' | 'auto'
  /** 自动模式云端预设 id（'' 可清空） */
  auto_cloud_preset_id?: string
  /**
   * 知识库检索向量后端（可选功能）。undefined=不改动。
   *
   * 这个字段后端一直支持（api.py ModelSaveReq.embedding_provider），但前端此前
   * 从未发送过——报错的引导文案却让用户去 UI 里改它。写进类型是为了让「漏传」
   * 变成编译期可见，而不是运行时静默失效。
   */
  embedding_provider?: 'ollama' | 'qwen' | 'local'
}

export interface SavePresetReq {
  id?: string
  label: string
  provider: string
  model?: string
  base_url?: string
  context_window?: number | null
  api_key?: string
}

/** 开发舱模型模式：global=跟随全局；fixed=固定预设；auto=Harness 按复杂度自选 */
export type CockpitMode = 'global' | 'fixed' | 'auto'

/** auto 模式的强模型层（自动模式云端预设）可用性 */
export interface CockpitAutoInfo {
  available: boolean
  reason: string
  preset: { id: string; label: string; provider: string; model: string } | null
}

/** 自主开发舱模型选择状态 */
export interface CockpitModelInfo {
  ok?: boolean
  mode: CockpitMode
  /** 已保存的固定预设（global/auto 下仅用于切回 fixed 时回显） */
  preset_id: string
  presets: ModelPreset[]
  /** 当前全局对话模型（未指定专用预设时工作流也用它） */
  current: { provider: string; model: string }
  /** fixed 模式实际生效的预设；global/auto 时为 null */
  effective: { id: string; label: string; provider: string; model: string } | null
  /** auto 模式强模型层信息 */
  auto: CockpitAutoInfo
  error?: string
}

/** 联网识别出的候选窗口（附带出处片段，由用户判断后采用） */
export interface ContextCandidate {
  tokens: number
  evidence: string
}

export interface ContextLookupResult {
  ok: boolean
  error?: string
  query?: string
  best?: number
  candidates?: ContextCandidate[]
}

export interface OllamaProbeResult {
  ok: boolean
  error?: string
  model?: string
  context_window?: number
}

export const modelApi = {
  get(): Promise<ModelConfigInfo> {
    return request('/api/config')
  },
  save(req: SaveModelReq): Promise<ModelConfigInfo & { warnings?: string[]; model_error?: string; error?: string }> {
    // 后端配置保存失败是 HTTP 200 + {ok:false, model_error?}：必须走 rawJson 原样返回失败体，
    // 否则通用 request 会把真实原因吞成「请求失败（HTTP 200）」，model_error 分支永不触发。
    return rawJson<ModelConfigInfo & { warnings?: string[]; model_error?: string; error?: string }>('/api/config', req)
  },
  /** 实时探测本机已安装 Ollama 模型的真实上下文窗口 */
  probeOllama(model: string): Promise<OllamaProbeResult> {
    return rawJson<OllamaProbeResult>(`/api/ollama/probe?model=${encodeURIComponent(model)}`)
  },
  /** 联网搜索模型公开的上下文窗口（返回候选列表，不自动写配置） */
  lookupModelContext(provider: string, model: string): Promise<ContextLookupResult> {
    return rawJson<ContextLookupResult>('/api/model_context_lookup', { provider, model })
  },
  /** 列出命名模型预设 */
  listPresets(): Promise<{ ok: boolean; presets: ModelPreset[] }> {
    return request('/api/model_presets')
  },
  /** 新增/更新预设（api_key 可选，随预设存入当前项目密钥库） */
  savePreset(req: SavePresetReq): Promise<{ ok: boolean; presets?: ModelPreset[]; error?: string }> {
    return rawJson('/api/model_presets', req)
  },
  /** 删除预设 */
  deletePreset(id: string): Promise<{ ok: boolean; presets?: ModelPreset[]; error?: string }> {
    return projectRequest(`/api/model_presets/${encodeURIComponent(id)}`, { method: 'DELETE' })
  },
  /** 一键激活预设（等同在弹窗里填好整套参数后点保存并切换） */
  activatePreset(id: string): Promise<ModelConfigInfo & { error?: string }> {
    return rawJson(`/api/model_presets/${encodeURIComponent(id)}/activate`, {})
  },
  /** 开发舱模型：读取模式、固定预设与自动模式强模型层状态 */
  cockpitGet(): Promise<CockpitModelInfo> {
    return rawJson<CockpitModelInfo>('/api/agent/cockpit-model')
  },
  /** 开发舱模型：设置模式（global/fixed/auto）；fixed 需带预设 id */
  cockpitSet(mode: CockpitMode, presetId = ''): Promise<CockpitModelInfo> {
    return rawJson<CockpitModelInfo>('/api/agent/cockpit-model', { mode, preset_id: presetId })
  },
}
