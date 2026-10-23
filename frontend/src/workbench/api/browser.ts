// 内置浏览器留痕：实况采样与截图回放。
//
// 与 `dev_page_action` / `browser_action` 的 SSE 事件配套：observation 事件里
// 的 `browser_evidence` 只带相对路径，图片正文由这里的 shot 接口按文件回传，
// 所以会话关闭之后流程行里的前后缩略图依然打得开。
import { rawJson } from './_base'

export interface BrowserLiveStep {
  action?: string
  ok?: boolean
  effect?: string
  url?: string
  title?: string
  at?: number
  shot_before?: string
  shot_after?: string
}

export interface BrowserLivePage {
  page: string
  url?: string
  domain?: string
  actions?: number
  last_action?: string
  last_effect?: string
  last_ok?: boolean | null
  last_step?: number
  live_state?: string
  live_version?: number
  idle_seconds?: number
  age_seconds?: number
}

export interface BrowserLiveSnapshot {
  ok: boolean
  state?: string
  page?: string
  domain?: string
  available?: boolean
  error?: string
  /** base64 JPEG；为空表示还没收到帧 */
  frame?: string
  frame_version?: number
  captured_at?: number
  width?: number
  height?: number
  url?: string
  title?: string
  last_action?: string
  last_effect?: string
  last_ok?: boolean | null
  last_step?: number
  last_at?: number
  steps?: BrowserLiveStep[]
}

export const browserLiveApi = {
  /** 当前所有浏览会话（公网 + 回环）。 */
  pages: () => rawJson<{ ok: boolean; pages?: BrowserLivePage[] }>('/api/dev/browser/pages'),
  /** 某会话的最新帧与最近动作；会话不存在时返回 {ok:false, state:'closed'}。 */
  live: (page: string) =>
    rawJson<BrowserLiveSnapshot>('/api/dev/browser/live?page=' + encodeURIComponent(page)),
  /** 截图/缩略图地址（相对项目根，只允许 .docmind/page-action 下的图片）。 */
  shotUrl: (path: string) => '/api/dev/browser/shot?path=' + encodeURIComponent(path),
}
