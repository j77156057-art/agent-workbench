// API 基础设施：统一错误类型、项目上下文注入与请求出口。
// 各业务域文件只负责端点定义，通用能力统一从这里引入。
import { appEvents } from '../eventBus'

/** 业务/HTTP 错误；status=0 表示网络层失败（服务未启动） */
export class FsApiError extends Error {
  status: number
  extra: Record<string, unknown>

  constructor(status: number, message: string, extra: Record<string, unknown> = {}) {
    super(message)
    this.name = 'FsApiError'
    this.status = status
    this.extra = extra
  }
}

// ---------------------------------------------------------------- P3/P4 项目上下文
// 全请求出口统一注入 `X-DocMind-Project`，把每个请求绑定到「当前项目」。
// 空串 = 未选择项目 → **不注入**（后端缺省回落当前项目，保持生命线）。持久化到
// localStorage 以便刷新后保持选中；后端切换项目后由 UI 回写对齐。
const PROJECT_ID_KEY = 'docmind_project_id'

/** 当前项目 id（持久化于 localStorage）；空串表示未选择。 */
export function getProjectId(): string {
  try {
    return localStorage.getItem(PROJECT_ID_KEY) || ''
  } catch {
    return '' // 隐私模式 / 无 localStorage：视作未选择
  }
}

/** 设置当前项目 id（空串 = 清除选择）；持久化以便刷新后保持。 */
export function setProjectId(pid: string): void {
  const previous = getProjectId()
  try {
    if (pid) localStorage.setItem(PROJECT_ID_KEY, pid)
    else localStorage.removeItem(PROJECT_ID_KEY)
  } catch {
    /* 写入失败（隐私模式等）不影响主流程 */
  }
  if (previous !== pid) appEvents.emit('docmind:project-context-changed')
}

export interface ActiveTask { id: string; region: string; allowedPaths: string[] }
export function getActiveTask(project = getProjectId()): ActiveTask {
  try {
    const task = JSON.parse(localStorage.getItem('docmind.activeTask:' + project) || 'null')
    if (task && typeof task.id === 'string' && typeof task.region === 'string' &&
        Array.isArray(task.allowedPaths) && task.allowedPaths.every((p: unknown) => typeof p === 'string')) return task
  } catch { /* Invalid or legacy unscoped records must not grant scope to another project. */ }
  return { id: '', region: '', allowedPaths: [] }
}
export function setActiveTask(task: ActiveTask, project = getProjectId()) {
  localStorage.setItem('docmind.activeTask:' + project, JSON.stringify(task))
}

/**
 * 在 init.headers 基础上**合并** `X-DocMind-Project`。
 *
 * - 不改动既有头（尤其 `Content-Type`：FormData 场景必须由浏览器自带 multipart boundary）；
 * - `getProjectId()` 为空时**不注入**，原样返回（生命线：后端回落当前项目）；
 * - 用 `new Headers(...)` 统一处理 headers 为 `undefined` / 普通对象 / `Headers` 实例三种形态。
 */
export function withProject(init?: RequestInit, projectId = getProjectId()): RequestInit {
  const base: RequestInit = init ? { ...init } : {}
  const pid = projectId
  if (!pid) return base
  const merged = new Headers(base.headers as HeadersInit | undefined)
  merged.set('X-DocMind-Project', pid)
  base.headers = merged
  return base
}

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, withProject(init))
  } catch {
    throw new FsApiError(0, '无法连接本地服务（127.0.0.1:8000），请确认 Agent 工作台 已启动。')
  }
  let body: any = null
  try {
    body = await res.json()
  } catch {
    /* 非 JSON 响应走下面的状态码分支 */
  }
  if (!res.ok || body?.ok === false) {
    const { ok: _ok, error: _e, ...extra } = body || {}
    throw new FsApiError(
      res.status,
      body?.error || body?.detail || `请求失败（HTTP ${res.status}）`,
      extra as Record<string, unknown>,
    )
  }
  return body as T
}

export async function postJson<T>(url: string, payload: unknown): Promise<T> {
  return request<T>(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/**
 * 业务失败也当数据返回（不抛错）。
 *
 * 通用 request 会把 `ok:false` 抛成异常，但场景编辑必须读到失败体里的
 * stale / rolled_back / warnings 才能正确分支，所以这里单独给一条原始通道；
 * 只有网络不可达或响应不是 JSON 才抛错。
 */
export async function rawJson<T>(url: string, payload?: unknown, signal?: AbortSignal, projectId = getProjectId()): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, withProject(payload === undefined
      ? { method: 'GET', signal }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), signal }, projectId))
  } catch {
    throw new FsApiError(0, '无法连接本地服务（127.0.0.1:8000），请确认 Agent 工作台 已启动。')
  }
  try {
    return (await res.json()) as T
  } catch {
    throw new FsApiError(res.status, `响应不是合法 JSON（HTTP ${res.status}）`)
  }
}

/**
 * 项目 CRUD 的原始通道：**不**把 `ok:false` 抛错，便于 UI 读失败体里的 `error`
 * （例如「目录不存在」「项目名称不能为空」）；只有网络不可达或响应非 JSON 才抛错。
 * 全部经 `withProject` 注入项目头。
 */
export async function projectRequest<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, withProject(init))
  } catch {
    throw new FsApiError(0, '无法连接本地服务（127.0.0.1:8000），请确认 Agent 工作台 已启动。')
  }
  try {
    return (await res.json()) as T
  } catch {
    throw new FsApiError(res.status, `响应不是合法 JSON（HTTP ${res.status}）`)
  }
}
