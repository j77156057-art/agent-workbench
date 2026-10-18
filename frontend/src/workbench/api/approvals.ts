// 人工确认轨 API：写工具待确认（/api/pending_edits）与 python_exec 三档分级同意
// （/api/pending_execs）。两者都是「工具已把操作登记为待确认、尚未落盘/落子进程」的
// 带外通道，前端轮询列表、点确认/拒绝，后端计数门控后真正执行。统一走 rawJson：
// 业务失败（如三档未点够次数）也当数据返回，便于读 message 展示「还需 N 次」。
import { rawJson } from './_base'

// ---- 写工具待确认 ----
export interface PendingEdit {
  id: string
  kind: string          // "apply_edit" | "create_file" | "delete_external_file"
  path: string
  diff: string
  size: number
}
export interface PendingEditsResp {
  pending: PendingEdit[]
  edit_confirm: boolean
}

// ---- python_exec 三档分级同意 ----
export interface PendingExec {
  id: string
  tier: number          // 2=一次确认（橙），3=连续三次确认（红）
  reason: string
  source: string        // "preexec" 执行前分类命中 | "runtime" 运行期安全钩升级
  confirms: number      // 已点击确认次数
  need: number          // 达标所需次数（tier2→1、tier3→3）
  code: string
}
export interface PendingExecsResp {
  pending: PendingExec[]
  exec_approval: boolean
}
export interface ExecDecisionResp {
  ok: boolean
  message: string
}

export const pendingApi = {
  edits(): Promise<PendingEditsResp> {
    return rawJson<PendingEditsResp>('/api/pending_edits')
  },
  confirmEdit(id: string): Promise<{ ok: boolean; message: string }> {
    return rawJson('/api/confirm_edit', { id })
  },
  rejectEdit(id: string): Promise<{ ok: boolean; message: string }> {
    return rawJson('/api/reject_edit', { id })
  },

  execs(): Promise<PendingExecsResp> {
    return rawJson<PendingExecsResp>('/api/pending_execs')
  },
  confirmExec(id: string): Promise<ExecDecisionResp> {
    return rawJson<ExecDecisionResp>('/api/confirm_exec', { id })
  },
  rejectExec(id: string): Promise<ExecDecisionResp> {
    return rawJson<ExecDecisionResp>('/api/reject_exec', { id })
  },
}
