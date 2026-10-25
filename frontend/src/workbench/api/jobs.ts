// 后台命令任务（run_command background: true）的 HTTP 出口。
// 列表只带状态/大小，不带日志；详情才回当前日志窗口（头尾各 4000 字符）。
import { postJson, request } from './_base'

export type JobState = 'starting' | 'running' | 'done' | 'timeout' | 'failed' | 'cancelled'

export interface BackgroundJob {
  job_id: string
  state: JobState | string
  exit_code: number | null
  /** 秒；运行中为"到现在为止"，结束后为总耗时 */
  elapsed: number
  /** 启动时声明的超时预算（秒） */
  timeout: number
  /** 子进程真实输出总量（可能大于日志窗口） */
  total_chars: number
  /** 缓冲只留头尾，true 表示中段已被省略 */
  truncated: boolean
  command: string
  /** 任务的工作目录（项目内路径；面板据此做项目过滤） */
  cwd?: string
  error: string
  offset?: number
  logs?: string
  next_offset?: number
}

export interface BackgroundJobList {
  ok: boolean
  scope: 'project' | 'all'
  root: string
  jobs: BackgroundJob[]
  running: number
  /** 当前项目之外还有多少个近期任务（切项目后才可见） */
  hidden: number
  /** 同时运行的任务上限 */
  capacity: number
}

export const jobsApi = {
  list(scope: 'project' | 'all' = 'project') {
    return request<BackgroundJobList>(`/api/jobs?scope=${scope}`)
  },
  detail(jobId: string, offset = 0) {
    return request<BackgroundJob>(`/api/jobs/${encodeURIComponent(jobId)}?offset=${offset}`)
  },
  cancel(jobId: string) {
    return postJson<BackgroundJob>(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {})
  },
}
