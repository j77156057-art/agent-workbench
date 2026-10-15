// EDA 多模态验收（M3 结构化 + M6 合并门）API 客户端。
// 后端见 api_routes/eda.py：三条只读接口——解析报告、跑合并门、取 skill。
// 关键边界：/api/eda/verify 只「判 + 建议」，executes_rollback 恒为 false；
// 真实回滚须经 regions.rollback_changeset 的 require_approval 人工门，不在本层。
import { postJson, request } from './_base'

/** M3 EdaFact.to_dict()：结构化真值。timing_met/drc_clean/lvs_clean 为布尔判定量。 */
export interface EdaFact {
  ok: boolean
  tool: string
  kind: string
  metrics: Record<string, unknown>
  missing: string[]
  provenance: { field: string; excerpt: string }[]
  error: string | null
}

/** M1/M5 视觉比对结果（compare 的三态 + 块信息）。 */
export interface VisualResult {
  verdict: string
  passed?: boolean
  escalate_to_vlm?: boolean
  ssim?: number
  mean_abs_delta?: number
  blocks?: { count: number; total: number; ratio?: number; indices?: number[]; grid?: number }
  reason?: string
  [k: string]: unknown
}

/** POST /api/eda/visual-regression 请求：抓真实窗口 ROI → 与 golden 确定性 diff。
 *  后端见 api_routes/eda.py；本机无 EDA 工具时 source 取真实 GDI 截屏（被摄窗口是替身）。 */
export interface VisualRegressionReq {
  golden_key?: string
  view?: string | null            // layout/waveform/schematic/heatmap（EDA_VIEWS）
  bbox_norm?: number[] | null     // 显式 [x1,y1,x2,y2] 归一框，覆盖 view
  mode?: 'check' | 'save'         // check=比对 | save=存基线
  capture?: 'auto' | 'gdi' | 'x11' | 'vnc'  // 取像后端：auto 按平台（Win→gdi，其余→x11）
  source?: 'foreground' | 'embedded' | 'hwnd'  // 仅 capture=gdi 生效
  hwnd?: number | null            // capture=gdi & source=hwnd：目标窗口句柄
  window_id?: number | null       // capture=x11：X11 window id（缺省回退用 hwnd）
  vnc_host?: string | null        // capture=vnc：VNC 地址（默认 127.0.0.1）
  vnc_port?: number | null        // capture=vnc：VNC 端口（默认 5900）
  overwrite?: boolean             // save 模式是否覆盖已存在 golden
  out_size?: number
  ssim_threshold?: number
  delta_threshold?: number
  grid?: number
}

/** 视觉回归响应：check 模式带 verdict/块信息（同 VisualResult），save 模式带 saved/entry。
 *  escalate_to_vlm 为真时附 escalation.roi_data_url（局部 ROI 缩略图，供 VLM/人看）。 */
export interface VisualRegressionResp extends VisualResult {
  ok: boolean
  mode: 'check' | 'save'
  capture?: string               // 实际生效的取像后端：gdi/x11/vnc
  saved?: boolean
  golden_key?: string
  entry?: Record<string, unknown>
  hwnd?: number | null
  full_size?: number[]
  roi_sha256?: string
  escalation?: { roi_data_url?: string; question?: string; localized_blocks?: unknown }
  [k: string]: unknown
}

/** M6 合并门三态。state ∈ met/unmet/uncertain。 */
export interface MergedSignals {
  state: 'met' | 'unmet' | 'uncertain'
  met: boolean
  escalate_to_vlm: boolean
  needs_human: boolean
  vetoed: boolean
  reasons: Record<string, string[]>
  summary: string
}

export interface AcceptanceDecision {
  accept: boolean
  action: 'accept' | 'rollback' | 'escalate'
  state: 'met' | 'unmet' | 'uncertain'
  reason: string
}

export interface VerifyResp {
  ok: boolean
  structured_state: 'met' | 'unmet' | 'uncertain'
  visual_state: 'met' | 'unmet' | 'uncertain'
  merged: MergedSignals
  decision: AcceptanceDecision
  /** 恒 false：本接口只判不执行回滚。 */
  executes_rollback: boolean
  note: string
}

export interface VerifyReq {
  structured: Partial<EdaFact> | null
  visual?: VisualResult | null
  vlm_verified?: boolean | null
  critical?: string[] | null
}

export interface SkillResp {
  ok: boolean
  tool: string
  /** SKILL.md 正文；缺文件时后端优雅降级为空串。 */
  skill: string
}

export const edaApi = {
  /** POST /api/eda/parse-report：把工具报告文本解析成结构化事实。 */
  parseReport(tool: string, kind: string, text: string) {
    return postJson<EdaFact>('/api/eda/parse-report', { tool, kind, text })
  },
  /** POST /api/eda/verify：结构化 × 视觉 → 三态合并门 + 验收决策（只判不回滚）。 */
  verify(body: VerifyReq) {
    return postJson<VerifyResp>('/api/eda/verify', body)
  },
  /** POST /api/eda/visual-regression：抓真实窗口 ROI → SSIM+8×8 块哈希 vs golden → 算 verdict。
   *  mode='save' 存基线，mode='check' 比对；verdict 由像素得出，不再手选。 */
  visualRegression(body: VisualRegressionReq) {
    return postJson<VisualRegressionResp>('/api/eda/visual-regression', body)
  },
  /** GET /api/eda/skills?tool=：取该 EDA 工具的 skill 说明书。 */
  skills(tool: string) {
    return request<SkillResp>(`/api/eda/skills?tool=${encodeURIComponent(tool)}`)
  },
}
