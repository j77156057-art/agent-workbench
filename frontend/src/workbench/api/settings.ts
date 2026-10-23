// 网络搜索供应商设置 / settingsApi（由原 workbench/api.ts 按域切分；调用方继续从 barrel ../api 引入）。
import { rawJson, request } from './_base'
import { ModelConfigInfo } from './model'

// ---------------------------------------------------------------- 网络搜索 / URL 获取 设置
export type WebSearchProvider =
  | 'builtin_auto' | 'ddg' | 'bing' | 'baidu' | 'exa' | 'tavily'
  | 'searxng' | 'zhipu' | 'bocha' | 'querit' | 'firecrawl' | 'parallel' | 'mcp_exa'
export type WebFetchProvider = 'builtin' | 'jina' | 'firecrawl' | 'custom'

export interface ProviderOption {
  value: string
  label: string
  /** 需要 API Key */
  needs_key?: boolean
  /** 需要自建实例地址（API URL） */
  needs_url?: boolean
  /** 一句话说明 */
  desc?: string
}

/** 设置页统一读取的配置（GET /api/config 的搜索相关字段）。 */
export interface SettingsConfigInfo {
  web_search_provider: WebSearchProvider
  web_search_api_url: string
  web_search_has_key: boolean
  /** Deprecated legacy API field; no model-native Web tool adapter is currently available. */
  web_search_prefer_builtin: boolean
  web_fetch_provider: WebFetchProvider
  web_fetch_api_url: string
  web_fetch_has_key: boolean
  /** 长期记忆优化总开关（异步蒸馏 + 重要度/语义混合召回）当前状态，默认关。 */
  memory_optimized: boolean
}

export interface SaveSettingsReq {
  web_search_provider?: WebSearchProvider
  web_search_api_key?: string
  web_search_api_url?: string
  web_search_prefer_builtin?: boolean
  web_fetch_provider?: WebFetchProvider
  web_fetch_api_key?: string
  web_fetch_api_url?: string
  /** 长期记忆优化总开关；不传=不变。 */
  memory_optimized?: boolean
}

export interface AgentCapacitySettings {
  max_agents: number
  main_max_steps: number
  child_max_steps: number
  max_nudges: number
  max_final_continuations: number
  task_token_budget: number
  local_auto_limit: boolean
}

/** 与 AgentCapacitySettings 同键，但值是每个字段的**上界**；
 *  另带 `<字段>_min` 形式的**正数下界**（目前只有 task_token_budget 有）。
 *  由 GET /api/agent-capacity 下发（api.py 的 ranges 字段），界面必须读它而不是
 *  再抄一份数字 —— 抄一份就多了「界面允许填、后端越界回落默认值」的漂移空间，
 *  而那个默认值现在是 0（不限），越界的后果与用户意图正好相反。 */
export type AgentCapacityRanges = { [K in keyof AgentCapacitySettings]-?: number } & {
  /** task_token_budget 填正数时的最小值；0（不限）不受此约束。老后端没有这个键。 */
  task_token_budget_min?: number
}

export interface AgentCapacityInfo {
  ok?: boolean
  settings: AgentCapacitySettings
  /** 老后端可能没有这个字段，所以可选；缺省时界面不设上界（见 bound()）。 */
  ranges?: Partial<AgentCapacityRanges>
  model: { provider: string; name: string; local_endpoint: boolean }
  resources: {
    available: boolean
    scope: 'local_host'
    total_mb: number
    used_mb: number
    free_mb: number
    gpus: Array<{ index: number; name: string | null; used_mb: number; total_mb: number; free_mb: number; utilization: number | null }>
    compute_apps: Array<{ pid: number; process_name: string | null; used_mb: number }>
  }
  loaded_models: {
    available: boolean
    provider: string
    models: Array<{ name: string; size_vram_bytes: number; size_bytes: number }>
  }
  effective_local_parallelism: number | null
  resource_note: string
}

export const WEB_SEARCH_PROVIDERS: ProviderOption[] = [
  { value: 'builtin_auto', label: '自动（内置 DDG / 百度 / Bing）', desc: '无需 Key，自动故障转移' },
  { value: 'ddg', label: 'DuckDuckGo', desc: '无需 Key' },
  { value: 'baidu', label: '百度', desc: '国内更稳，无需 Key' },
  { value: 'bing', label: 'Bing', desc: '无需 Key' },
  { value: 'exa', label: 'Exa', needs_key: true, desc: 'api.exa.ai，语义检索' },
  { value: 'tavily', label: 'Tavily', needs_key: true, desc: 'api.tavily.com' },
  { value: 'searxng', label: 'SearXNG', needs_url: true, desc: '自建实例' },
  { value: 'bocha', label: 'Bocha 博查', needs_key: true, desc: 'api.bochaai.com' },
  { value: 'firecrawl', label: 'Firecrawl', needs_key: true, desc: '搜索 + 抓取' },
  { value: 'zhipu', label: '智谱搜索', needs_url: true, desc: '通用 keyed POST' },
  { value: 'querit', label: 'Querit', needs_url: true, desc: '通用 keyed POST' },
  { value: 'parallel', label: 'Parallel', needs_url: true, desc: '通用 keyed POST' },
  { value: 'mcp_exa', label: 'ExaMCP', needs_url: true, desc: '经 MCP 接入的 Exa' },
]

export const WEB_FETCH_PROVIDERS: ProviderOption[] = [
  { value: 'builtin', label: '内置（直接抓 HTML）', desc: '无需 Key' },
  { value: 'jina', label: 'Jina', needs_key: true, desc: 'r.jina.ai' },
  { value: 'firecrawl', label: 'Firecrawl', needs_key: true, desc: 'v1/scrape' },
  { value: 'custom', label: '自定义服务', needs_url: true, desc: 'POST {url} 取 markdown' },
]


export const settingsApi = {
  /** 读取模型 + 搜索相关配置（复用 /api/config）。 */
  get(): Promise<SettingsConfigInfo & ModelConfigInfo> {
    return request('/api/config')
  },
  /** 保存网络搜索 / URL 获取设置（复用 /api/config 的保存通道）。 */
  save(req: SaveSettingsReq): Promise<SettingsConfigInfo & ModelConfigInfo & { ok?: boolean; error?: string; warnings?: string[] }> {
    return rawJson<SettingsConfigInfo & ModelConfigInfo & { ok?: boolean; error?: string; warnings?: string[] }>('/api/config', req)
  },
}

export const agentCapacityApi = {
  get(): Promise<AgentCapacityInfo> {
    return request('/api/agent-capacity')
  },
  save(settings: AgentCapacitySettings): Promise<{ ok?: boolean; settings?: AgentCapacitySettings; error?: string }> {
    return rawJson('/api/agent-capacity', settings)
  },
}

/** 长期记忆优化总开关：走专用 /api/memory-settings，刻意不复用 /api/config
 *  （后者局部保存会把 LLM provider 覆盖成 mock）。切换即生效 + 落盘，重启仍保持。 */
export interface MemorySettingsInfo {
  ok?: boolean
  memory_optimized?: boolean
  error?: string
}
export const memorySettingsApi = {
  get(): Promise<MemorySettingsInfo> {
    return request('/api/memory-settings')
  },
  save(memory_optimized: boolean): Promise<MemorySettingsInfo> {
    return rawJson('/api/memory-settings', { memory_optimized })
  },
}

// ================================================================ 阶段 4：边玩边改闭环
// Bug 归档（受控 bugs 分区）+ 变更集回滚/提交。全部经 request/rawJson（自动带项目头），
// 失败体（ok:false / 400 / 403）走 rawJson 原样返回，不抛错，便于面板分支展示。

/** POST /api/dev_capture_bug 的入参（与后端 BugReq 一一对应，7 个文本字段）。 */
