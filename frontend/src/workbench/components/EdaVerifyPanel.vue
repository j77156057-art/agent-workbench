<script setup lang="ts">
// EDA 多模态验收面板（M3 结构化真值 × M1/M5 视觉信号 → M6 合并门）。
// 信条（docs/multimodal-verification-engine-eda-kickoff.md §0）：
// - 脚本拿事实、视觉补感知：结构化报告解析 + 视觉回归比对，两路信号交叉否决。
// - 三态而非二态：MET / UNMET / UNCERTAIN，任一否决即否决；拿不准绝不静默放行。
// - 视觉结论 verified:false 起步：本面板只呈现"判 + 建议"，绝不执行回滚
//   （后端 executes_rollback 恒 false；真实回滚须经 regions.rollback_changeset 人工审批门）。
// 组件自管状态，不进工作台全局；入口收在顶栏「更多 → 工具与运行」。
import { computed, ref } from 'vue'
import { edaApi, changesetApi, type EdaFact, type VerifyResp, type VisualResult, type VisualRegressionResp, type ReportGateReq } from '../api'
import { noProjectHint } from '../projectHint'
import { askConfirm } from '../composables/workbench'

// 复用 SceneRuntimePanel 的「可嵌入」惯用法：popup（固定弹层，行为不变）/ docked（内联常驻主区）。
// 同一组件实例仅由父组件的 mode prop 切形态，绝不重建，故已解析的 fact / 门结果等状态在
// 弹窗↔停靠之间得以保留。docked 主体 teleport 进主区 host 槽（默认 #wb-eda-slot）。
const props = withDefaults(defineProps<{ mode?: 'popup' | 'docked'; host?: string }>(),
  { mode: 'popup', host: '#wb-eda-slot' })
const emit = defineEmits<{ dock: []; undock: [] }>()
const docked = computed(() => props.mode === 'docked')

const open = ref(false)
const busy = ref(false)
const error = ref('')

// 弹窗 → 停靠：先收掉弹窗，再请父组件切 docked（父置 edaResident=true）。
function dockNow() { open.value = false; emit('dock') }
// 停靠 → 弹出为浮窗：请父组件解除常驻，并立即以弹窗重新呈现。
function toPopup() { emit('undock'); open.value = true }
// 停靠 → 收起：解除常驻且不重开（再要打开走顶栏「更多」菜单）。
function collapse() { emit('undock') }

// ---- 结构化真值（M3）----
const tool = ref('vivado')
const kind = ref('timing')
const reportText = ref('')
const fact = ref<EdaFact | null>(null)

// ---- 在线报告页：地址 + 取正文方式（2026-10-20 补参数）----
// 后端 report-gate 一直支持 `url` + `fetch='web_fetch'`（由后端 urllib 直抓），
// 但面板此前**只固定用** `fetch:'inline'` + 手贴正文 —— 于是"把报告页地址交给
// DocMind 自己抓"这条最自然的用法在前端根本不存在。
const reportUrl = ref('')
const fetchMode = ref<'inline' | 'web_fetch'>('inline')

// ---- 自定义判定量键（critical）----
// 后端 verify/report-gate 都支持 `critical`（显式指定纳入门禁的判定量键），
// 缺省才用 timing_met/drc_clean/lvs_clean。面板此前从不传 → 项目自己的
// 决定性指标（如 slack_met）永远进不了门。
const criticalKeys = ref('')

// ---- 显式 ROI（bbox_norm）----
// 后端支持显式 [x1,y1,x2,y2] 归一框覆盖 `view`；面板此前只传 view。
const bboxNorm = ref('')

function criticalList(): string[] | null {
  const parts = criticalKeys.value.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean)
  return parts.length ? parts : null
}
function bboxList(): number[] | null {
  const parts = bboxNorm.value.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean).map(Number)
  return parts.length === 4 && parts.every((n) => Number.isFinite(n)) ? parts : null
}

// ---- 适配说明（skill）：后端按工具切段返回 SKILL.md 正文 ----
const skillText = ref('')
const skillBusy = ref(false)
async function loadSkill() {
  skillBusy.value = true
  try {
    const r = await edaApi.skills(tool.value)
    skillText.value = r.ok ? (r.skill || '（该工具暂无适配说明）') : '（读取失败）'
  } catch (e) {
    skillText.value = `（读取失败：${(e as Error).message}）`
  } finally {
    skillBusy.value = false
  }
}

// ---- 真跑 EDA 工具脚本（POST /api/eda/run）----
// 这是"连接 EDA 工具"唯一的出口：后端本来就能跑 Vivado/KLayout/仿真器，
// 但此前既无 HTTP 端点也无前端入口。执行面由后端限定为登记在册的三个二进制，
// 且必须 confirm=true —— 所以这里**必须先弹确认**，绝不"点一下就跑"。
const scriptText = ref('')
const runBusy = ref(false)
const runMsg = ref('')
const runErr = ref('')
async function runScript() {
  if (!scriptText.value.trim()) { runErr.value = '脚本为空。'; return }
  const ok = await askConfirm({
    title: '运行 EDA 脚本？',
    message: `将调用 ${tool.value} 执行这段脚本，并以项目根为工作目录。`,
    detail: '工具以当前用户权限读写文件；脚本内容与执行结果都会记入本次面板。工具不可用时后端一律 fail-closed，不会返回空输出冒充"通过"。',
    confirmText: '运行',
  })
  if (!ok) return
  runBusy.value = true
  runMsg.value = ''
  runErr.value = ''
  try {
    const r = await edaApi.run(tool.value, kind.value, scriptText.value)
    if (!r.ok) {
      // 后端的失败码是人话之外的机器码，这里翻一下再显示（不猜、不美化）。
      const code = r.error || '未知错误'
      const explain = code.startsWith('tool_unavailable:')
        ? `本机没找到 ${code.slice('tool_unavailable:'.length)} 可执行文件 —— 先装好该工具再跑。`
        : code.startsWith('no_command_template:')
          ? '该工具没有配置执行模板。'
          : code
      runErr.value = `未拿到事实：${explain}${r.reason ? '（' + r.reason + '）' : ''}`
      fact.value = null
    } else {
      fact.value = r
      runMsg.value = `已跑完并解析：${r.tool}/${r.kind}`
    }
  } catch (e) {
    runErr.value = noProjectHint((e as Error).message) || '运行失败'
  } finally {
    runBusy.value = false
  }
}

// ---- 视觉信号（M1/M5）----
// 两种取源：live=真取像回归（抓真实窗口 ROI → 真算 SSIM+块哈希 → verdict 由像素得出，
// 不再手选）；manual=手选 verdict 演示（无 EDA 工具/无窗口时仍能演三态否决）。默认 live。
const visualMode = ref<'live' | 'manual'>('live')

// live 取像回归入参（对齐后端 VisualRegressionReq）。
const vrCapture = ref<'auto' | 'gdi' | 'x11' | 'vnc'>('auto')  // 取像后端（平台可切换）
const vrSource = ref<'foreground' | 'embedded' | 'hwnd'>('foreground')  // 仅 gdi 生效
const vrHwnd = ref('')                 // gdi + source=hwnd：窗口句柄（字符串，提交时转 int）
const vrWinId = ref('')                // x11：X11 window id
const vrVncHost = ref('127.0.0.1')     // vnc：服务地址
const vrVncPort = ref('5900')          // vnc：端口
const goldenKey = ref('layout_default')
const vrView = ref<'layout' | 'waveform' | 'schematic' | 'heatmap'>('layout')
const vrBusy = ref(false)
const vrMsg = ref('')                  // 存基线成功/失败提示
const vrError = ref('')                // 取像/比对错误
const liveResult = ref<VisualRegressionResp | null>(null)   // check 算出的三态
const liveThumb = ref('')              // escalation.roi_data_url（有变化时才有）

// manual 兜底：本机没窗口/没 golden 时，仍可手选 verdict 演示合并门否决逻辑。
const VERDICTS = [
  'identical', 'pass', 'localized', 'regression',
  'needs_golden', 'no_frame', 'capture_error',
]
const verdict = ref('localized')
const vlmVerified = ref(false)

// ---- 合并门结果（M6）----
const result = ref<VerifyResp | null>(null)
// 在线整链（/api/eda/report-gate）溯源标注：取页后端 · 视觉通道来源 · 异常，供审计。
const reportNote = ref('')

// ---- 门→人工审批→执行回滚 桥线（本面板仍不自动 revert，仅把 gate 的 rollback 建议
//      经后端登记成审批请求；下面按钮点下去才走 regions.rollback_changeset 的 require_approval 门）----
// 待绑定的真实变更集 id：留空 → 门只判不登记审批（行为同以前）。
const changesetId = ref('')
// 最近一次在线门返回的审批桥状态：requested=后端已登记审批请求；changesetId=实际绑定的 id。
const gateApproval = ref<{ requested: boolean; changesetId: string } | null>(null)
// 点「执行回滚」后的调用状态与回执（未经审批会被后端 403 拦下，如实回显）。
const rollbackBusy = ref(false)
const rollbackMsg = ref('')
const rollbackErr = ref('')

const stateMeta: Record<string, { label: string; cls: string; hint: string }> = {
  met: { label: 'MET · 通过', cls: 'ev-met', hint: '结构化 + 视觉双绿，放行。' },
  unmet: { label: 'UNMET · 否决', cls: 'ev-unmet', hint: '有信号明确否决，拒绝放行（建议回滚，但需人工审批）。' },
  uncertain: { label: 'UNCERTAIN · 拿不准', cls: 'ev-unc', hint: '未核实/缺证据，绝不静默放行，升级人工/下一段核实。' },
}
const stateInfo = computed(() => stateMeta[result.value?.merged.state || ''] || null)

// 只有「在线整链跑过 + 门判 rollback + 后端确实登记了审批请求 + 绑了真实 cs id」才亮回滚按钮。
// 手选/分步门（runGate/loadPreset）不登记审批，故不出现此按钮——绝不给"看起来能一键回滚"的假象。
const showRollback = computed(() =>
  !!gateApproval.value?.requested &&
  !!gateApproval.value?.changesetId &&
  result.value?.decision.action === 'rollback')

// 数值展示：SSIM/均值差等可能是 undefined（缺 golden/取像失败）→ 给 '—' 而非崩。
function fmtNum(v: number | undefined | null): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '—'
  return (Math.round(v * 1000) / 1000).toString()
}

// live verdict → 卡片配色：regression 否决(红)；pass/identical 通过(绿)；其余未核实(琥珀)。
const liveVerdictCls = computed(() => {
  const v = liveResult.value?.verdict
  if (!v) return ''
  if (v === 'regression') return 'ev-unmet'
  if (v === 'pass' || v === 'identical') return 'ev-met'
  return 'ev-unc'
})

async function parseReport() {
  error.value = ''
  busy.value = true
  try {
    fact.value = await edaApi.parseReport(tool.value, kind.value, reportText.value)
  } catch (e) {
    error.value = noProjectHint((e as Error).message) || '解析失败'
    fact.value = null
  } finally {
    busy.value = false
  }
}

function currentVisual(): VisualResult {
  // live 模式且已跑出比对结果 → 用像素算出的 verdict（真回归）；否则回退手选演示。
  if (visualMode.value === 'live' && liveResult.value) {
    return liveResult.value
  }
  return { verdict: verdict.value, passed: verdict.value === 'pass' || verdict.value === 'identical' }
}

function _vrBody(mode: 'save' | 'check'): Parameters<typeof edaApi.visualRegression>[0] {
  const body: Parameters<typeof edaApi.visualRegression>[0] = {
    mode, capture: vrCapture.value, golden_key: goldenKey.value.trim() || 'layout_default',
    view: vrView.value,
  }
  // 显式 ROI 覆盖 view（后端 bbox_norm 优先于 view）。
  const box = bboxList()
  if (box) body.bbox_norm = box
  const cap = vrCapture.value
  if (cap === 'gdi' || cap === 'auto') {
    // auto 在 Windows 后端即 gdi，仍带上 source/hwnd 以便本机直接用。
    body.source = vrSource.value
    if (vrSource.value === 'hwnd') {
      const h = parseInt(vrHwnd.value.trim(), 10)
      if (!Number.isNaN(h)) body.hwnd = h
    }
  }
  if (cap === 'x11') {
    const w = parseInt(vrWinId.value.trim(), 10)
    if (!Number.isNaN(w)) body.window_id = w
  }
  if (cap === 'vnc') {
    body.vnc_host = vrVncHost.value.trim() || '127.0.0.1'
    const p = parseInt(String(vrVncPort.value).trim(), 10)
    if (!Number.isNaN(p)) body.vnc_port = p
  }
  return body
}

// 存基线：在"当前即通过态"抓一帧 ROI 存 golden（无损 PNG），后续 check 与之比对。
async function saveBaseline() {
  vrError.value = ''; vrMsg.value = ''; vrBusy.value = true
  try {
    const r = await edaApi.visualRegression(_vrBody('save'))
    if (r.saved) vrMsg.value = `已存基线「${r.golden_key || goldenKey.value}」（golden 落盘）。`
    else vrError.value = r.reason || `未存（${r.verdict}）。`
  } catch (e) {
    vrError.value = (e as Error).message || '存基线失败'
  } finally {
    vrBusy.value = false
  }
}

// 跑视觉回归：抓当前窗口 ROI → vs golden 真算 SSIM + 8×8 块哈希 → 得出 verdict。
async function runRegression() {
  vrError.value = ''; vrMsg.value = ''; vrBusy.value = true
  liveResult.value = null; liveThumb.value = ''
  try {
    const r = await edaApi.visualRegression(_vrBody('check'))
    liveResult.value = r
    if (r.escalation?.roi_data_url) liveThumb.value = r.escalation.roi_data_url
    if (r.verdict === 'no_frame' || r.verdict === 'capture_error' || r.verdict === 'no_source') {
      vrError.value = r.reason || `取像失败：${r.verdict}`
    } else if (r.verdict === 'needs_golden') {
      vrMsg.value = '该 golden_key 还没有基线，先「存基线」再跑回归。'
    }
  } catch (e) {
    vrError.value = (e as Error).message || '视觉回归调用失败'
  } finally {
    vrBusy.value = false
  }
}

async function runGate() {
  error.value = ''
  busy.value = true
  gateApproval.value = null      // 分步门不登记审批：清掉回滚桥状态
  rollbackMsg.value = ''; rollbackErr.value = ''
  try {
    result.value = await edaApi.verify({
      structured: fact.value,
      visual: currentVisual(),
      vlm_verified: vlmVerified.value ? true : null,
      critical: criticalList(),
    })
  } catch (e) {
    error.value = noProjectHint((e as Error).message) || '合并门调用失败'
    result.value = null
  } finally {
    busy.value = false
  }
}

// 在线整链：一次跑完「正文(inline) → M3 解析 →（live 时后端现算 M5 视觉回归）→ M6 合并门」。
// 与分步「解析 → 跑回归 → 跑合并门」等价，但走 /api/eda/report-gate 单一只读入口，
// 视觉通道带 golden_key+ROI+capture 时由后端现算 diff，喂进同一个门；仍只判不执行回滚。
async function runReportGate() {
  error.value = ''
  busy.value = true
  reportNote.value = ''
  gateApproval.value = null      // 每次在线整链重跑：清掉上一轮回滚桥状态
  rollbackMsg.value = ''; rollbackErr.value = ''
  try {
    const body: ReportGateReq = {
      tool: tool.value, kind: kind.value,
      // 取正文方式：inline=用下面的正文框；web_fetch=由后端直抓下面的地址。
      fetch: fetchMode.value,
      url: fetchMode.value === 'web_fetch' ? (reportUrl.value.trim() || null) : null,
      content: fetchMode.value === 'inline' ? reportText.value : null,
      vlm_verified: vlmVerified.value ? true : null,
      critical: criticalList(),
      changeset_id: changesetId.value.trim() || null,
    }
    if (visualMode.value === 'live' && goldenKey.value.trim()) {
      // 复用取像参数装配（_vrBody 的 mode 对本接口无意义，只取其取像字段）。
      const vr = _vrBody('check')
      body.golden_key = vr.golden_key ?? goldenKey.value.trim()
      body.view = vr.view ?? null
      if (vr.bbox_norm) body.bbox_norm = vr.bbox_norm
      body.capture = vr.capture ?? 'auto'
      if (vr.source) body.source = vr.source
      if (vr.hwnd != null) body.hwnd = vr.hwnd
      if (vr.window_id != null) body.window_id = vr.window_id
      if (vr.vnc_host) body.vnc_host = vr.vnc_host
      if (vr.vnc_port != null) body.vnc_port = vr.vnc_port
    } else {
      // manual 兜底：把已算/手选的 verdict 直接透传给视觉通道。
      body.visual = currentVisual()
    }
    const r = await edaApi.reportGate(body)
    // 嵌套 gate → 现有 result（VerifyResp 形状），复用三态卡片渲染，不动模板主体。
    result.value = {
      ok: r.ok,
      structured_state: r.gate.structured_state,
      visual_state: r.gate.visual_state,
      merged: r.gate.merged,
      decision: r.gate.decision,
      executes_rollback: r.executes_rollback,
      note: r.note,
    }
    // 审批桥：后端在门判 rollback 且绑了 cs id 时登记人工审批请求，回此两字段。
    gateApproval.value = { requested: !!r.approval_requested, changesetId: r.changeset_id || '' }
    if (r.fact) fact.value = r.fact
    reportNote.value =
      `取页 ${r.source} · 视觉 ${r.visual_source}` +
      (r.visual_error ? ` · ${r.visual_error}` : '') +
      (r.fetch_failed ? ' · 正文取不到（fail-closed，未造事实）' : '')
    if (!r.ok && r.error === 'no_report_text') {
      error.value = '在线正文取不到 → 判不确定，交人工/下一段核实（未喂解析器、未造假）。'
    }
  } catch (e) {
    error.value = noProjectHint((e as Error).message) || '在线报告门调用失败'
    result.value = null
  } finally {
    busy.value = false
  }
}

// 「执行回滚」：只有门已把 rollback 建议登记成人工审批请求（showRollback 为真）才出现此按钮。
// 点下去走 changesetApi.rollback → regions.rollback_changeset：内部仍过 require_approval 人工门，
// 未获批会被后端 403 拦下（approval_required），本函数如实回显"待审批"，绝不假装回滚成功。
async function rollbackNow() {
  const cs = gateApproval.value?.changesetId || ''
  if (!cs) { rollbackErr.value = '未绑定变更集 id，无法回滚。'; return }
  if (rollbackBusy.value) return
  rollbackBusy.value = true
  rollbackMsg.value = ''; rollbackErr.value = ''
  try {
    const r = await changesetApi.rollback(cs) as {
      ok?: boolean; blocked?: boolean; approval_required?: boolean; message?: string; error?: string
    }
    if (r && (r.blocked || r.approval_required)) {
      rollbackErr.value = `回滚被人工审批门拦下（待审批）：${r.message || r.error || '需先审批 rollback_changeset'}`
    } else if (!r.ok) {
      rollbackErr.value = `回滚失败：${r.error || '未知错误'}`
    } else {
      rollbackMsg.value = `已执行回滚（cs：${cs}）——生成新提交撤销，历史未破坏。`
    }
  } catch (e) {
    rollbackErr.value = (e as Error).message || '回滚调用失败'
  } finally {
    rollbackBusy.value = false
  }
}

// ---- 三个预置用例：一眼演示双信号交叉否决 ----
type Preset = { name: string; tool: string; kind: string; report: string; verdict: string; vlm: boolean; blurb: string }
const PRESETS: Preset[] = [
  {
    name: '日志全绿 · 版图破图', tool: 'vivado', kind: 'timing', verdict: 'regression', vlm: false,
    blurb: '结构化 timing_met=True（时序守住），但把视觉 verdict 设为 regression（演示 macro 整片缺失）→ 一票否决 UNMET。真实回归由引擎产出 verdict，本面板只喂结论给门。',
    report: 'WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns)\n  0.412   0.000   0   4820   0.089   0.000\nsys_clk_i {0.000 3.333} 6.667 150.000',
  },
  {
    name: '均值达标 · 少一个 via', tool: 'vivado', kind: 'timing', verdict: 'localized', vlm: false,
    blurb: '演示：把视觉 verdict 设为 localized（模拟钝指标 SSIM/均值都达标、但区域块哈希抓到 1 个 via 没了）。localized 未核实 → UNCERTAIN，不静默放行。',
    report: 'WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns)\n  0.412   0.000   0   4820   0.089   0.000\nsys_clk_i {0.000 3.333} 6.667 150.000',
  },
  {
    name: '双绿 · 通过', tool: 'vivado', kind: 'timing', verdict: 'identical', vlm: false,
    blurb: '结构化绿 + 把视觉 verdict 设为 identical（模拟与 golden 同源）→ MET，accept。',
    report: 'WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns)\n  0.412   0.000   0   4820   0.089   0.000\nsys_clk_i {0.000 3.333} 6.667 150.000',
  },
]

async function loadPreset(p: Preset) {
  tool.value = p.tool; kind.value = p.kind; reportText.value = p.report
  // 预置是"手选 verdict 喂门"的演示：切到 manual 并清掉 live 结果，确保喂的就是该 verdict。
  visualMode.value = 'manual'; liveResult.value = null; liveThumb.value = ''
  verdict.value = p.verdict; vlmVerified.value = p.vlm
  error.value = ''
  gateApproval.value = null      // 预置走分步门，不登记审批
  rollbackMsg.value = ''; rollbackErr.value = ''
  busy.value = true
  try {
    fact.value = await edaApi.parseReport(p.tool, p.kind, p.report)
    result.value = await edaApi.verify({
      structured: fact.value, visual: currentVisual(), vlm_verified: p.vlm ? true : null,
    })
  } catch (e) {
    error.value = (e as Error).message || '预置用例执行失败'
  } finally {
    busy.value = false
  }
}

function show() { open.value = true }
defineExpose({ show })
</script>

<template>
  <div class="ev">
    <button class="ev-trigger" :class="result ? stateInfo?.cls : ''" title="EDA 多模态验收：结构化报告 × 视觉回归 → 三态合并门" @click="open = !open">
      <svg width="12" height="12" viewBox="0 0 12 12">
        <rect x="1.2" y="1.2" width="9.6" height="9.6" rx="1" fill="none" stroke="currentColor" stroke-width="0.9" />
        <rect x="2.8" y="2.8" width="2.4" height="2.4" fill="currentColor" opacity="0.55" />
        <rect x="6.8" y="2.8" width="2.4" height="2.4" fill="currentColor" opacity="0.55" />
        <rect x="2.8" y="6.8" width="2.4" height="2.4" fill="currentColor" opacity="0.55" />
        <path d="M6.8 8 H9.2 M8 6.8 V9.2" stroke="currentColor" stroke-width="0.9" />
      </svg>
      <span class="ev-label">EDA 验收</span>
    </button>

    <!-- 遮罩：仅弹窗态（docked 无遮罩），teleport 到 body -->
    <Teleport to="body">
      <div v-if="!docked && open" class="ev-backdrop wb-modal-backdrop" @click="open = false" />
    </Teleport>

    <!-- 面板主体：popup→body 固定弹层；docked→主区 host 槽内联填充。
         同一实例仅切 teleport 目标（to），不重建，故 fact / 门结果状态得以保留。 -->
    <Teleport defer :to="docked ? host : 'body'">
      <div v-if="docked || open" class="ev-pop" :class="docked ? 'ev-pop-inline' : 'wb-modal-shell'"
           :role="docked ? 'region' : 'dialog'" :aria-modal="docked ? undefined : 'true'" aria-label="EDA 多模态验收">
        <div class="ev-head wb-modal-head">
          <b>EDA 多模态验收</b>
          <span class="ev-faint">结构化真值 × 视觉信号 → 三态合并门</span>
          <span class="ev-spacer" />
          <button v-if="!docked" class="ev-link" title="停靠到主区常驻（复用工作台嵌入能力）" @click="dockNow">⤓ 停靠</button>
          <button v-else class="ev-link" title="弹出为浮窗" @click="toPopup">⤒ 浮窗</button>
          <button class="ev-link" @click="docked ? collapse() : (open = false)">{{ docked ? '收起' : '关闭' }}</button>
        </div>

        <p class="ev-note">本面板<b>自身不自动回滚</b>（后端 <code>executes_rollback:false</code>）。绑定<b>变更集 id</b> 后跑在线门，若判 rollback，会经 <code>regions.rollback_changeset</code> 的<em>人工审批门</em>登记一条审批请求；只有审批通过、再点「执行回滚」才真正 revert。视觉结论 <b>verified:false</b> 起步。</p>

        <!-- 预置用例 -->
        <div class="ev-presets">
          <span class="ev-sub">快速演示：</span>
          <button v-for="p in PRESETS" :key="p.name" class="ev-chip" :disabled="busy" :title="p.blurb" @click="loadPreset(p)">{{ p.name }}</button>
        </div>

        <!-- 2026-10-20：错误行补「重试」。此前只有裸错误串，用户只能关面板重开。 -->
        <p v-if="error" class="ev-err">{{ error }} <button class="ev-retry" :disabled="busy" @click="runReportGate">重试</button></p>

        <div class="ev-grid">
          <!-- 左：结构化 -->
          <section class="ev-col">
            <div class="ev-col-h">① 结构化真值（M3 报告解析）</div>
            <div class="ev-row">
              <label>工具
                <select v-model="tool">
                  <option value="vivado">vivado</option>
                  <option value="klayout">klayout</option>
                  <option value="verdi">verdi</option>
                </select>
              </label>
              <label>kind
                <select v-model="kind">
                  <option value="timing">timing</option>
                  <option value="utilization">utilization</option>
                  <option value="drc">drc</option>
                  <option value="simulation">simulation</option>
                </select>
              </label>
            </div>
            <div class="ev-row">
              <label>取正文
                <select v-model="fetchMode">
                  <option value="inline">手贴正文</option>
                  <option value="web_fetch">由后端抓取报告页</option>
                </select>
              </label>
              <label v-if="fetchMode === 'web_fetch'" class="ev-grow">报告地址
                <input v-model="reportUrl" class="ev-input ev-grow" type="text" placeholder="https://…/report.html" spellcheck="false" />
              </label>
            </div>
            <textarea v-if="fetchMode === 'inline'" v-model="reportText" class="ev-text" rows="5" placeholder="粘贴工具 report 原文（如 Vivado report_timing_summary）…" />
            <div class="ev-row">
              <label class="ev-grow">判定量键 critical（逗号分隔；留空用默认）
                <input v-model="criticalKeys" class="ev-input ev-grow" type="text" placeholder="timing_met, drc_clean" spellcheck="false" />
              </label>
              <button class="ev-btn" :disabled="skillBusy" @click="loadSkill">{{ skillBusy ? '读取中…' : '适配说明' }}</button>
            </div>
            <pre v-if="skillText" class="ev-skill">{{ skillText }}</pre>
            <button class="ev-btn" :disabled="busy" @click="parseReport">解析成结构化事实</button>

            <!-- 连接 EDA 工具：真跑脚本（后端只认登记在册的三个二进制，且要显式确认） -->
            <details class="ev-run">
              <summary>连接 EDA 工具（真跑脚本）</summary>
              <textarea v-model="scriptText" class="ev-text" rows="4" placeholder="工具脚本正文（Vivado 为 TCL，如 report_timing_summary）…" />
              <button class="ev-btn" :disabled="runBusy" @click="runScript">{{ runBusy ? '运行中…' : '运行并解析' }}</button>
              <p v-if="runMsg" class="ev-ok">{{ runMsg }}</p>
              <p v-if="runErr" class="ev-err">{{ runErr }}</p>
            </details>

            <div v-if="fact" class="ev-fact wb-card">
              <div class="ev-fact-h">
                <span :class="fact.ok ? 'ev-tag-ok' : 'ev-tag-bad'">{{ fact.ok ? 'ok' : 'fail' }}</span>
                <b>{{ fact.tool }}/{{ fact.kind }}</b>
              </div>
              <div v-for="(v, k) in fact.metrics" :key="k" class="ev-metric">
                <span class="ev-mk">{{ k }}</span><span class="ev-mv">{{ v }}</span>
              </div>
              <div v-if="fact.missing?.length" class="ev-miss">缺失：{{ fact.missing.join(', ') }}</div>
              <div v-if="fact.error" class="ev-err">错误：{{ fact.error }}</div>
              <details v-if="fact.provenance?.length" class="ev-prov">
                <summary>provenance（{{ fact.provenance.length }} 条，回指报告原行）</summary>
                <div v-for="(p, i) in fact.provenance" :key="i" class="ev-prov-row"><b>{{ p.field }}</b> ← {{ p.excerpt }}</div>
              </details>
            </div>
          </section>

          <!-- 右：视觉 + 门 -->
          <section class="ev-col">
            <div class="ev-col-h">② 视觉信号（live=真取像回归 · verdict 由像素算，非手选）</div>
            <div class="ev-row">
              <label class="ev-radio"><input type="radio" value="live" v-model="visualMode" /> 真取像回归</label>
              <label class="ev-radio"><input type="radio" value="manual" v-model="visualMode" /> 手选演示</label>
            </div>

            <!-- live：抓真实窗口 ROI → vs golden 真算 SSIM + 8×8 块哈希 → 得出 verdict -->
            <div v-if="visualMode === 'live'" class="ev-vr">
              <div class="ev-row">
                <label>取像后端
                  <select v-model="vrCapture">
                    <option value="auto">auto（按平台）</option>
                    <option value="gdi">gdi · Windows 截屏</option>
                    <option value="x11">x11 · Linux/X11</option>
                    <option value="vnc">vnc · 远端 framebuffer</option>
                  </select>
                </label>
              </div>
              <div v-if="vrCapture === 'gdi' || vrCapture === 'auto'" class="ev-row">
                <label>取像源
                  <select v-model="vrSource">
                    <option value="foreground">前台窗口</option>
                    <option value="embedded">嵌入引擎窗</option>
                    <option value="hwnd">指定 HWND</option>
                  </select>
                </label>
                <label v-if="vrSource === 'hwnd'">HWND
                  <input v-model="vrHwnd" class="ev-input" type="text" size="9" placeholder="如 196876" />
                </label>
              </div>
              <div v-else-if="vrCapture === 'x11'" class="ev-row">
                <label>X11 window id
                  <input v-model="vrWinId" class="ev-input" type="text" size="12" placeholder="如 0x1a 或 26" />
                </label>
                <span class="ev-faint">走 M4 ImageMagick import</span>
              </div>
              <div v-else class="ev-row">
                <label>VNC host
                  <input v-model="vrVncHost" class="ev-input" type="text" size="12" placeholder="127.0.0.1" />
                </label>
                <label>port
                  <input v-model="vrVncPort" class="ev-input" type="text" size="6" placeholder="5900" />
                </label>
              </div>
              <div class="ev-row">
                <label>golden_key
                  <input v-model="goldenKey" class="ev-input" type="text" size="14" placeholder="layout_default" />
                </label>
                <label>view
                  <select v-model="vrView">
                    <option value="layout">layout</option>
                    <option value="waveform">waveform</option>
                    <option value="schematic">schematic</option>
                    <option value="heatmap">heatmap</option>
                  </select>
                </label>
                <label>ROI 归一框 x1,y1,x2,y2（留空按 view）
                  <input v-model="bboxNorm" class="ev-input" type="text" size="16" placeholder="0.1,0.1,0.6,0.4" spellcheck="false" />
                </label>
              </div>
              <div class="ev-row">
                <button class="ev-btn" :disabled="vrBusy" title="把当前 ROI 存成 golden 基线（无损 PNG）" @click="saveBaseline">存基线</button>
                <button class="ev-btn ev-btn-run" :disabled="vrBusy" title="抓当前 ROI 与 golden 做确定性 diff" @click="runRegression">跑视觉回归 →</button>
              </div>
              <p v-if="vrBusy" class="ev-faint">取像比对中…</p>
              <p v-if="vrMsg" class="ev-vr-msg">{{ vrMsg }}</p>
              <p v-if="vrError" class="ev-err">{{ vrError }}</p>

              <div v-if="liveResult" class="ev-vr-res wb-card" :class="liveVerdictCls">
                <div class="ev-vr-verdict">
                  <b>{{ liveResult.verdict }}</b>
                  <span class="ev-vr-verified">verified:false</span>
                </div>
                <div class="ev-vr-metrics">
                  <span>SSIM <b>{{ fmtNum(liveResult.ssim) }}</b></span>
                  <span>均值差 <b>{{ fmtNum(liveResult.mean_abs_delta) }}</b></span>
                  <span>变化块 <b>{{ liveResult.blocks?.count ?? 0 }}/{{ liveResult.blocks?.total ?? 0 }}</b></span>
                </div>
                <div v-if="liveResult.golden_key" class="ev-vr-key">
                  <span v-if="liveResult.capture">后端 {{ liveResult.capture }} · </span>golden_key：{{ liveResult.golden_key }}<span v-if="liveResult.hwnd"> · hwnd {{ liveResult.hwnd }}</span><span v-if="liveResult.full_size"> · 帧 {{ liveResult.full_size[0] }}×{{ liveResult.full_size[1] }}</span>
                </div>
                <img v-if="liveThumb" :src="liveThumb" class="ev-thumb" alt="变化 ROI 局部图" />
                <p v-if="liveResult.reason" class="ev-faint">{{ liveResult.reason }}</p>
              </div>
            </div>

            <!-- manual：手选 verdict 兜底演示（无窗口/无 golden 时仍能演三态否决） -->
            <div v-else class="ev-row">
              <label>verdict
                <select v-model="verdict">
                  <option v-for="v in VERDICTS" :key="v" :value="v">{{ v }}</option>
                </select>
              </label>
            </div>
            <label class="ev-check"><input type="checkbox" v-model="vlmVerified" /> 已人工核实通过（verified:true）</label>

            <p class="ev-faint ev-hint">live：verdict 由真实截屏 + 确定性像素比对得出（本机无 EDA 工具，被摄窗口是<b>替身</b>，比对链路是真算的）。regression=硬否决；localized/needs_golden/no_frame/…=未核实→UNCERTAIN；pass/identical=通过。视觉结论默认 verified:false；仅经<b>人工核实</b>（VLM 只解释、不决策）才置 verified:true，门才可能据此解除不确定——决定权在人工核实，不在 VLM。</p>

            <div class="ev-row">
              <label>变更集 id（回滚桥）
                <input v-model="changesetId" class="ev-input" type="text" size="16" placeholder="绑定后 rollback 会登记人工审批；留空=只判" />
              </label>
            </div>
            <!-- 2026-10-20：两个主门按钮 busy 时给出文案（此前只变灰，
                 一次整链跑几十秒，用户不知道在跑什么）。回滚按钮本来就有文案。 -->
            <button class="ev-btn ev-btn-gate" :aria-busy="busy" :disabled="busy" @click="runGate">{{ busy ? '跑合并门中…' : '跑合并门 →' }}</button>
            <button class="ev-btn ev-btn-gate" :aria-busy="busy" :disabled="busy" title="一次跑完 正文→M3 解析→（live 现算 M5 视觉回归）→M6 门；只读、不执行回滚" @click="runReportGate">{{ busy ? '整链执行中…' : '在线报告门 · 整链跑一次 →' }}</button>

            <div v-if="result && stateInfo" class="ev-gate wb-card" :class="stateInfo.cls">
              <div class="ev-state">
                <span class="ev-state-badge">{{ stateInfo.label }}</span>
                <span class="ev-action">建议动作：{{ result.decision.action }}</span>
              </div>
              <p class="ev-state-hint">{{ stateInfo.hint }}</p>
              <div class="ev-signals">
                <span>结构化 <b>{{ result.structured_state }}</b></span>
                <span>视觉 <b>{{ result.visual_state }}</b></span>
                <span v-if="result.merged.vetoed" class="ev-veto">已否决</span>
                <span v-if="result.merged.escalate_to_vlm" class="ev-esc">需升级 VLM</span>
              </div>
              <ul class="ev-reasons">
                <li v-for="(rs, st) in result.merged.reasons" :key="st">
                  <span class="ev-rs-tag">{{ st }}</span>
                  <span v-for="(r, i) in rs" :key="i" class="ev-rs">{{ r }}</span>
                </li>
              </ul>
              <div class="ev-exec">{{ result.note }}</div>
              <div v-if="reportNote" class="ev-faint ev-prov-line">在线整链溯源：{{ reportNote }}</div>

              <div v-if="showRollback" class="ev-rollback">
                <div class="ev-rollback-h">
                  <span class="ev-rollback-badge">已请求人工回滚审批 · cs:{{ gateApproval?.changesetId }}</span>
                </div>
                <p class="ev-faint">门判 rollback，已在审批队列登记一条 <code>rollback_changeset</code> 请求（本面板未自动 revert）。<b>须人工审批通过后</b>，下方「执行回滚」才会真正生成撤销提交；未获批将被后端 403 拦下。</p>
                <button class="ev-btn ev-btn-rollback" :disabled="rollbackBusy" @click="rollbackNow">{{ rollbackBusy ? '回滚执行中…' : '执行回滚 →' }}</button>
                <p v-if="rollbackMsg" class="ev-vr-msg">{{ rollbackMsg }}</p>
                <p v-if="rollbackErr" class="ev-err">{{ rollbackErr }}</p>
              </div>
            </div>
          </section>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.ev { position: relative; }
.ev-trigger {
  display: inline-flex; align-items: center; gap: 5px;
  background: transparent; color: var(--text-muted);
  border: 1px solid var(--border-strong); border-radius: 5px;
  padding: 5px 9px; cursor: pointer; font-size: var(--fs-cap); white-space: nowrap;
  transition: color .16s ease, border-color .16s ease, background .16s ease;
}
.ev-trigger:hover { color: var(--text); background: var(--bg-hover); }
.ev-trigger.ev-met { border-color: rgba(58, 150, 90, .5); color: var(--green, #3a965a); }
.ev-trigger.ev-unmet { border-color: rgba(224, 72, 79, .5); color: var(--danger); }
.ev-trigger.ev-unc { border-color: color-mix(in srgb, var(--amber) 45%, transparent); color: var(--amber); }

.ev-backdrop { position: fixed; inset: 0; z-index: 299; background: var(--scrim-soft); backdrop-filter: blur(1px); }
.ev-pop {
  position: fixed; right: 16px; top: 64px; width: 640px; max-width: calc(100vw - 32px);
  max-height: calc(100vh - 80px); overflow-y: auto; padding: 12px 14px;
  background: var(--bg-raised); border: 1px solid var(--border-strong); border-radius: 8px;
  z-index: 300; font-size: var(--fs-sm); line-height: 1.6; box-shadow: 0 12px 32px rgba(35,52,84,.16);
}
/* 常驻（docked）：被 teleport 进主区 host 槽，去掉固定弹层的定位/尺寸/阴影，内联填充父容器。
   animation: none 必须——弹层入场动画的 to 帧会钉住 transform，静态 transform:none 压不住它。 */
.ev-pop-inline {
  position: static; left: auto; top: auto; transform: none; animation: none;
  width: 100%; height: 100%; max-width: none; max-height: none; flex: 1;
  border: 0; border-radius: 0; box-shadow: none; overflow-y: auto; z-index: auto;
}
.ev-head { display: flex; align-items: center; gap: 8px; }
.ev-head b { color: var(--text); font-size: var(--fs-md); }
.ev-spacer { flex: 1; }
.ev-link { background: none; border: none; color: var(--accent-text); cursor: pointer; font-size: var(--fs-cap); }
.ev-note { margin: 8px 0 0; padding: 6px 8px; border-radius: 5px; background: color-mix(in srgb, var(--accent) 10%, transparent); color: var(--accent-text); font-size: var(--fs-cap); }
.ev-note code { background: rgba(0,0,0,.06); padding: 0 3px; border-radius: 3px; }
.ev-presets { margin: 10px 0 0; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.ev-sub { color: var(--text-muted); font-size: var(--fs-cap); }
.ev-chip { background: transparent; border: 1px solid var(--border-strong); color: var(--text-muted); border-radius: 12px; font-size: var(--fs-cap); padding: 2px 9px; cursor: pointer; }
.ev-chip:hover:not(:disabled) { color: var(--text); border-color: var(--accent); }
.ev-err { margin: 8px 0 0; color: var(--danger); font-size: var(--fs-cap); }
.ev-retry { margin-left: 6px; border: 1px solid var(--border-strong); border-radius: 4px; background: transparent; color: var(--text-muted); font-size: var(--fs-cap); padding: 1px 7px; cursor: pointer; }
.ev-retry:hover:not(:disabled) { color: var(--accent-text); border-color: var(--accent); }
.ev-faint { color: var(--text-muted); font-size: var(--fs-cap); }

.ev-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }
.ev-col { border: 1px solid var(--border-strong); border-radius: 6px; padding: 8px 9px; }
.ev-col-h { font-size: var(--fs-sm); color: var(--text); font-weight: 600; margin-bottom: 6px; }
.ev-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
.ev-row label { display: inline-flex; align-items: center; gap: 4px; color: var(--text-muted); font-size: var(--fs-cap); }
.ev-row select { background: var(--bg); border: 1px solid var(--border-strong); border-radius: 4px; color: var(--text); padding: 2px 4px; font-size: var(--fs-cap); }
.ev-check { cursor: pointer; }
.ev-text { width: 100%; box-sizing: border-box; background: var(--bg); border: 1px solid var(--border-graphic); border-radius: 4px; color: var(--text); padding: 5px 6px; font-size: var(--fs-cap); font-family: var(--font-mono); resize: vertical; }
.ev-btn { margin-top: 6px; background: transparent; border: 1px solid var(--accent); color: var(--accent-text); border-radius: 5px; font-size: var(--fs-cap); padding: 4px 10px; cursor: pointer; }
.ev-btn:hover:not(:disabled) { background: var(--bg-hover); }
.ev-btn-gate { border-color: var(--amber); color: var(--amber); }
.ev-hint { margin: 6px 0 0; }
button:disabled { opacity: .5; cursor: default; }

.ev-fact { margin-top: 8px; padding: 6px 8px; border: 1px solid var(--border-strong); border-radius: 5px; }
.ev-fact-h { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.ev-tag-ok { background: rgba(58,150,90,.14); color: var(--green, #3a965a); border-radius: 8px; padding: 0 6px; font-size: var(--fs-cap); }
.ev-tag-bad { background: rgba(224,72,79,.14); color: var(--danger); border-radius: 8px; padding: 0 6px; font-size: var(--fs-cap); }
.ev-metric { display: flex; justify-content: space-between; font-size: var(--fs-cap); }
.ev-mk { color: var(--text-muted); }
.ev-mv { color: var(--text); font-family: var(--font-mono); }
.ev-miss { color: var(--amber); font-size: var(--fs-cap); margin-top: 3px; }
.ev-prov { margin-top: 4px; font-size: var(--fs-cap); color: var(--text-muted); }
.ev-prov summary { cursor: pointer; }
.ev-prov-row { margin: 2px 0 0 8px; word-break: break-all; }

.ev-gate { margin-top: 8px; padding: 8px 9px; border-radius: 6px; border: 1px solid var(--border-strong); }
.ev-gate.ev-met { border-color: rgba(58,150,90,.5); }
.ev-gate.ev-unmet { border-color: rgba(224,72,79,.5); }
.ev-gate.ev-unc { border-color: color-mix(in srgb, var(--amber) 50%, transparent); }
.ev-state { display: flex; align-items: center; gap: 8px; }
.ev-state-badge { font-weight: 700; font-size: var(--fs-md); }
.ev-met .ev-state-badge { color: var(--green, #3a965a); }
.ev-unmet .ev-state-badge { color: var(--danger); }
.ev-unc .ev-state-badge { color: var(--amber); }
.ev-action { color: var(--text-muted); font-size: var(--fs-cap); }
.ev-state-hint { margin: 4px 0 6px; font-size: var(--fs-cap); color: var(--text-muted); }
.ev-signals { display: flex; flex-wrap: wrap; gap: 8px; font-size: var(--fs-cap); color: var(--text-muted); }
.ev-signals b { color: var(--text); }
.ev-veto { color: var(--danger); }
.ev-esc { color: var(--amber); }
.ev-reasons { list-style: none; margin: 6px 0 0; padding: 0; }
.ev-reasons li { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 3px; }
.ev-rs-tag { background: rgba(0,0,0,.06); border-radius: 8px; padding: 0 6px; font-size: var(--fs-cap); color: var(--text-muted); }
.ev-rs { font-size: var(--fs-cap); color: var(--text); }
.ev-exec { margin-top: 8px; padding-top: 6px; border-top: 1px dashed var(--border-strong); font-size: var(--fs-cap); color: var(--text-muted); }
.ev-prov-line { margin-top: 4px; }

.ev-rollback { margin-top: 8px; padding-top: 7px; border-top: 1px dashed var(--border-strong); }
.ev-rollback-h { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.ev-rollback-badge { background: rgba(224,72,79,.14); color: var(--danger); border-radius: 8px; padding: 1px 8px; font-size: var(--fs-cap); font-weight: 600; }
.ev-rollback p code { background: rgba(0,0,0,.06); padding: 0 3px; border-radius: 3px; }
.ev-btn-rollback { border-color: var(--danger); color: var(--danger); }
.ev-btn-rollback:hover:not(:disabled) { background: rgba(224,72,79,.10); }

.ev-radio { cursor: pointer; user-select: none; }
.ev-input { background: var(--bg); border: 1px solid var(--border-graphic); border-radius: 4px; color: var(--text); padding: 2px 4px; font-size: var(--fs-cap); font-family: var(--font-mono); }
/* ev-grow：让输入框吃掉整行剩余宽度（报告地址 / 判定量键这类长文本）。 */
.ev-grow { flex: 1 1 180px; min-width: 0; }
.ev-ok { margin: 6px 0 0; color: var(--green); font-size: var(--fs-cap); }
/* skill 文本：等宽小字，限高可滚。 */
.ev-skill {
  margin: 6px 0 0; max-height: 220px; overflow: auto; padding: 6px 8px;
  background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
  color: var(--text-muted); font-family: var(--font-mono); font-size: var(--fs-cap);
  line-height: 1.5; white-space: pre-wrap; word-break: break-word;
}
/* 真跑脚本：默认收起（执行类能力不该和只读解析并列在第一屏）。 */
.ev-run { margin-top: 8px; }
.ev-run > summary { cursor: pointer; font-size: var(--fs-cap); color: var(--text-muted); }
.ev-run > summary:hover { color: var(--accent-text); }
.ev-vr { margin-top: 2px; }
.ev-btn-run { border-color: var(--accent); color: var(--accent-text); }
.ev-vr-msg { margin: 4px 0 0; color: var(--green, #3a965a); font-size: var(--fs-cap); }
.ev-vr-res { margin-top: 8px; padding: 7px 8px; border: 1px solid var(--border-strong); border-radius: 6px; }
.ev-vr-verdict { display: flex; align-items: center; gap: 6px; }
.ev-vr-verdict b { font-size: var(--fs-md); color: var(--text); }
.ev-met .ev-vr-verdict b { color: var(--green, #3a965a); }
.ev-unmet .ev-vr-verdict b { color: var(--danger); }
.ev-unc .ev-vr-verdict b { color: var(--amber); }
.ev-vr-verified { margin-left: auto; background: rgba(224,140,50,.14); color: var(--amber); border-radius: 8px; padding: 0 6px; font-size: var(--fs-cap); }
.ev-vr-metrics { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 5px; font-size: var(--fs-cap); color: var(--text-muted); }
.ev-vr-metrics b { color: var(--text); font-family: var(--font-mono); }
.ev-vr-key { margin-top: 4px; font-size: var(--fs-cap); color: var(--text-muted); font-family: var(--font-mono); }
.ev-thumb { display: block; margin-top: 6px; width: 128px; height: 128px; image-rendering: pixelated; border: 1px solid var(--border-strong); border-radius: 4px; }

.ev-vr-res.ev-met { border-color: rgba(58,150,90,.5); }
.ev-vr-res.ev-unmet { border-color: rgba(224,72,79,.5); }
.ev-vr-res.ev-unc { border-color: color-mix(in srgb, var(--amber) 50%, transparent); }

@media (max-width: 720px) { .ev-grid { grid-template-columns: 1fr; } .ev-pop { left: 8px; right: 8px; width: auto; } }
</style>
