/** Internal handoff: the receiver acknowledges every request, including blocks. */
export type FeedbackDeliveryStatus = 'processing' | 'awaiting_review' | 'pending' | 'failed'

/** /api/chat 的 ui_context 取值：
 *  - web_preview_feedback：用户本人的截图反馈（消息体仍是用户的话，仅追加内部提示）；
 *  - app_interface_inspect：应用自己发起的「浏览当前界面」，整条 prompt 都是内部指令，
 *    不作为用户消息展示，由后端转入 system_context；
 *  - desktop_visual_review：用户确认桌面点击后的视觉复验反馈，携带 workflowId/feedbackId 供后端对账。 */
export type ChatUiContext = 'web_preview_feedback' | 'app_interface_inspect' | 'cockpit_live_vision'
  | 'desktop_visual_review' | 'cockpit_voice_turn' | 'verify_realtime_alert'

export interface PreviewFeedbackRequest {
  prompt: string
  /** App-authored guidance is sent separately from the user's visible message. */
  uiContext?: ChatUiContext
  projectId: string
  target?: 'default' | 'cockpit'
  images?: Blob[]
  /** 桌面复验反馈所属工作流与反馈编号，仅 uiContext=desktop_visual_review 时携带。 */
  workflowId?: string
  feedbackId?: string
  /** 反馈发送失败时由调用方自行重试，聊天层不弹默认重试交互。 */
  autoRetry?: boolean
  onStatus?: (status: FeedbackDeliveryStatus, detail?: string) => void
  /** Only a completed answer, never reasoning tokens or an interrupted partial. */
  onResult?: (text: string) => void
}

export function blobDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error('无法读取截图'))
    reader.readAsDataURL(blob)
  })
}
