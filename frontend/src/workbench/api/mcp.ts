// MCP 引擎桥 / Godot 插件 / 自动注册（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { postJson, rawJson, request } from './_base'

// ---------------------------------------------------------------- P0：MCP 引擎桥 + Godot 插件
export interface McpServer {
  key: string
  label?: string
  engine?: string
  transport: 'stdio' | 'http'
  command?: string
  args?: string[]
  url?: string
  enabled: boolean
  help?: string
  config_error?: string
}

export interface McpToolInfo {
  name: string
  description: string
  input_schema: Record<string, unknown>
}

export interface McpCapabilityCandidate {
  id: string
  server: string
  domain: string
  capabilities: string[]
  keywords: string[]
  best_for: string
  tool_mappings: Array<{ tool: string; capabilities: string[] }>
  ui_requirements: Array<{ tool: string; kind: string; fields: string[]; renderer: string }>
  sources: string[]
  confidence: number
  tool_count: number
  status: 'pending' | 'active' | 'rejected'
}

export interface McpDirectoryResult {
  id: string
  label: string
  summary: string
  capabilities: string[]
  connection_options: Array<{
    id?: string
    label?: string
    transport: 'stdio' | 'http'
    when: string
    value: string
    config?: Partial<McpAutoConnectConfig>
    trust_tier?: McpAutoConnectCandidate['trust_tier']
    secrets?: McpAutoConnectCandidate['secrets']
    register_provider?: string
  }>
  setup_steps: string[]
  template: { key: string; label: string; transport: 'stdio' | 'http'; command: string; args: string[]; url: string }
  sources: string[]
  source_status: 'web_sources' | 'offline_guide' | 'registry'
}

// P1：MCP 自动连接向导（设置 → MCP 链路重做）
export interface McpProvenance {
  url: string
  domain: string
  /** registry server 名 / 精选索引 name，前端卡片标题优先用 */
  server_name?: string
  /** registry 命名空间，用于信任分档（官方命名空间判定） */
  namespace?: string
  /** 标记来自官方 Registry */
  registry?: boolean
  /** 标记来自离线精选索引 */
  curated?: boolean
  note?: string
}
export interface McpAutoConnectConfig {
  transport: 'stdio' | 'http'
  command: string
  args: string[]
  url: string
  env: Record<string, string>
  headers: Record<string, string>
  provenance: McpProvenance
  command_unresolved: boolean
}
export interface McpAutoConnectCandidate {
  config: McpAutoConnectConfig
  trust: 'trusted' | 'source_untrusted'
  /** 候选引用的凭证；required=false 仅作可选预填，不阻塞提交。 */
  secrets?: { name: string; required: boolean }[]
  /** 显示用信任分档：official（官方发布）/ community（受信域第三方）/ unknown（来源不可信）。R8 自动填参闸门仍看 trust。 */
  trust_tier?: 'official' | 'community' | 'unknown'
  validation_errors: string[]
}
export interface McpAutoConnectSearchRes {
  ok: boolean
  candidates: McpAutoConnectCandidate[]
  search_error?: string
  error?: string
}
export interface McpProbeRes {
  ok: boolean
  probe_ok: boolean
  tools?: string[]
  error?: string
  ready?: boolean
  readiness_message?: string
}
/** L0/L1/L2 档位：L0=全自动填表取凭证；L1=遇验证码/2FA 暂停、用户点继续；L2=仅人工回填。 */
export type McpRegisterTier = 'L0' | 'L1' | 'L2'
/** 注册会话状态：running=自动进行中；waiting_user=等用户人工完成一步；done=成功；failed=失败。 */
export type McpRegisterState = 'running' | 'waiting_user' | 'done' | 'failed'

export interface McpRegisterStartRes {
  ok: boolean
  task_id?: string
  tier: McpRegisterTier
  url?: string
  note?: string
  error?: string
}
/** GET register/status 展平后的会话字典（后端 autoconnect_sessions.json 的一条）。 */
export interface McpRegisterStatusRes {
  ok: boolean
  task_id?: string
  tier?: McpRegisterTier
  status?: McpRegisterState
  url?: string
  context_dir?: string
  resume_token?: string
  created_at?: number
  /** 本次代管的 provider（与 secrets_store / commit 的 stored 同名）。 */
  provider?: string
  /** 给用户看的一步提示（如「请在浏览器里完成验证码后点继续」）。L1 展示。 */
  user_prompt?: string
  /** 当前自动步子（challenge/submit/untrusted/fill/capture/opening/filling/submitting/capturing）。 */
  step?: string
  error?: string
}
export interface McpRegisterResumeRes {
  ok: boolean
  task_id?: string
  tier?: McpRegisterTier
  status?: McpRegisterState
  url?: string
  user_prompt?: string
  step?: string
  /** 业务态说明（如「没有可续跑的会话」），与传输失败区分。 */
  error?: string
}
export interface McpRegisterCommitRes {
  ok: boolean
  stored?: string[]
  error?: string
}

export interface GodotAddonStatus {
  ok: boolean
  is_godot_project: boolean
  installed: boolean
  version: string
  enabled: boolean
  uvx: string
  uvx_available: boolean
  godot: string
  godot_available: boolean
  min_version: string
}

export interface McpPendingProposal {
  /** 审批绑定哈希 target（key:digest），确认卡片据此唯一标识一份提案 */
  target: string
  key: string
  /** 逐字连接配置，用户确认时原样 POST 回 /api/mcp/servers 以命中同一审批哈希 */
  config: Record<string, unknown>
  created_at?: string
}

export const mcpApi = {
  servers(): Promise<{ ok: boolean; servers: McpServer[] }> {
    return request('/api/mcp/servers')
  },
  pendingProposals(): Promise<{ ok: boolean; items: McpPendingProposal[]; error?: string }> {
    return request('/api/mcp/pending-proposals')
  },
  probe(key: string): Promise<{ ok: boolean; tool_count?: number; tools?: string[]; error?: string }> {
    return postJson('/api/mcp/probe', { key })
  },
  capabilities(): Promise<{ ok: boolean; active: Record<string, McpCapabilityCandidate>; pending: Record<string, McpCapabilityCandidate>; error?: string }> {
    return request('/api/mcp/capabilities')
  },
  searchCatalog(query: string, webEnabled = true): Promise<{ ok: boolean; results: McpDirectoryResult[]; search_error?: string; error?: string }> {
    return postJson('/api/mcp/catalog/search', { query, web_enabled: webEnabled })
  },
  discover(key: string, webEnabled = false): Promise<{ ok: boolean; candidate?: McpCapabilityCandidate; error?: string }> {
    return postJson('/api/mcp/discover', { key, web_enabled: webEnabled })
  },
  decideCapability(key: string, approved: boolean): Promise<{ ok: boolean; approved?: boolean; capability?: McpCapabilityCandidate; error?: string }> {
    return postJson('/api/mcp/capabilities/decision', { key, approved })
  },
  tools(key: string): Promise<{ ok: boolean; tools: McpToolInfo[]; count?: number; error?: string }> {
    return request(`/api/mcp/tools?key=${encodeURIComponent(key)}`)
  },
  call(key: string, name: string, args: Record<string, unknown>):
      Promise<{ ok: boolean; text?: string; is_error?: boolean; error?: string }> {
    return postJson('/api/mcp/call', { key, name, arguments: args })
  },
  save(key: string, config: Record<string, unknown>):
      Promise<{ ok: boolean; servers?: McpServer[]; error?: string }> {
    return postJson('/api/mcp/servers', { key, config })
  },
  remove(key: string): Promise<{ ok: boolean; servers?: McpServer[]; error?: string }> {
    return postJson('/api/mcp/servers/remove', { key })
  },
  close(key: string): Promise<{ ok: boolean; closed?: boolean; error?: string }> {
    return postJson('/api/mcp/close', { key })
  },
  status(): Promise<{ ok: boolean; active?: string[]; error?: string }> {
    return request('/api/mcp/status')
  },
  // ---- P1：MCP 自动连接 ----
  // search / probe / vision 的「没找到候选」属于业务数据（ok:false + 可选 error），
  // 不是传输失败。若走会抛错的 request 通道，HTTP 200 会被误报成「请求失败（HTTP 200）」，
  // 所以这里统一走 rawJson（不抛业务错，只有网络不可达/非 JSON 才抛）。
  async autoConnectSearch(query: string, webEnabled = true, timeoutMs = 60000): Promise<McpAutoConnectSearchRes> {
    // 联网搜索+多页抓取可能较慢；加硬超时，避免后端卡住时向导无限转圈。
    const ctrl = new AbortController()
    const timer = window.setTimeout(() => ctrl.abort(), timeoutMs)
    try {
      return await rawJson('/api/mcp/autoconnect/search', { query, web_enabled: webEnabled }, ctrl.signal)
    } catch (e) {
      if (ctrl.signal.aborted) {
        return { ok: false, candidates: [], search_error: `联网搜索超时（>${Math.round(timeoutMs / 1000)}s），请关闭“允许联网”后重试或手动添加。` }
      }
      throw e
    } finally {
      window.clearTimeout(timer)
    }
  },
  probeCandidate(config: McpAutoConnectConfig): Promise<McpProbeRes> {
    return rawJson('/api/mcp/autoconnect/probe', { config })
  },
  confirmConnect(key: string, config: McpAutoConnectConfig):
      Promise<{ ok: boolean; servers?: McpServer[]; error?: string }> {
    return postJson('/api/mcp/autoconnect/confirm', { key, config, user_ack: true })
  },
  registerStart(key: string, config: McpAutoConnectConfig, provider: string): Promise<McpRegisterStartRes> {
    return postJson('/api/mcp/autoconnect/register/start', { key, config, provider })
  },
  registerStatus(taskId: string): Promise<McpRegisterStatusRes> {
    return request(`/api/mcp/autoconnect/register/status?task_id=${encodeURIComponent(taskId)}`)
  },
  /**
   * L1「我已完成，继续」：让后端在同一持久会话上续跑（撞到下一个挑战会再次 waiting_user）。
   *
   * 走 rawJson：响应里的 `status`（waiting_user/done/failed）与 `user_prompt` 是**业务态**，
   * 即便后端以 `ok:false` 返回（例如「没有可续跑的 live 会话」）也要读出来渲染，
   * 不能被通用 request 当成传输失败抛掉（与上面 autoConnectSearch/probe 同一通道约定）。
   */
  registerResume(taskId: string): Promise<McpRegisterResumeRes> {
    return rawJson('/api/mcp/autoconnect/register/resume', { task_id: taskId })
  },
  registerCommit(taskId: string, credentials: Record<string, string>): Promise<McpRegisterCommitRes> {
    return postJson('/api/mcp/autoconnect/register/commit', { task_id: taskId, credentials })
  },
  visionExtract(imageBase64: string): Promise<McpAutoConnectSearchRes> {
    return rawJson('/api/mcp/autoconnect/vision/extract', { image_base64: imageBase64 })
  },
  addonStatus(): Promise<GodotAddonStatus> {
    return request('/api/engine/addon/status')
  },
  addonInstall(force = false): Promise<{ ok: boolean; version?: string; error?: string; next?: string }> {
    return postJson('/api/engine/addon/install', { confirm: true, force })
  },
}
