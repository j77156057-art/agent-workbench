// 把一轮对话的推理轨迹折成 Qoder 式的「工具调用卡片」。
//
// 为什么改：原来对话台把「分析摘要 / 执行动作 / 返回结果」逐条裸露成一排符号行
// （○ → •），信息密度高但读起来像日志。Qoder 的做法是**一个工具一张卡**：
// 工具名 + 状态灯 + 参数一行 + 结果收在卡里，点开才看全文。轨迹没丢，只是不再
// 抢最终答案的注意力。
//
// 这一层是**纯函数**：不碰 DOM、不持有状态，输入消息产出卡片列表。
// 组装口径与工具名映射都放在这里，便于 node 单测直接跑（frontend/tests/）。
import { flowOf } from './chat-types.ts'
import type { BrowserEvidence, ChatFlowItem, ChatTraceItem, FileDiffEntry } from './chat-types.ts'
import { actionToolOf, cleanNarration } from './traceDigest.ts'

/** 卡片状态：与轨迹行原有的 running/ok/warn/error 四态同名同义。 */
export type CardState = 'running' | 'ok' | 'warn' | 'error'

export type CardKind = 'think' | 'tool'

export interface ChatCard {
  /** 稳定 key（同一回合内不变，展开状态才不会在流式重渲染时丢掉）。 */
  key: string
  kind: CardKind
  /** 卡片标题：工具中文名，或「深度思考 / 分析 / 复核」。 */
  title: string
  /** 原始工具名（kind==='tool'），用于图标与归类。 */
  tool: string
  /** 参数摘要（折叠时显示的一行，如 `path=README.md`）。 */
  params: string
  state: CardState
  stateLabel: string
  /** 这一步耗时（ms）；observation 会把前一个 action 的耗时回填过来。 */
  elapsedMs?: number
  /** 展开后的正文：工具返回全文，或思考/旁白全文。 */
  body: string
  /** 折叠时的一行摘要（正文太长时的兜底）。 */
  summary: string
  /** 关联的 trace 下标（工具卡用它取证据与改动清单）。 */
  traceIndex?: number
  evidence?: BrowserEvidence
  file_diffs?: FileDiffEntry[]
}

/** 工具名 → 中文标题。未收录的直接用工具名本身，不编。 */
const TOOL_TITLE: Record<string, string> = {
  // 读
  read_file: '读取文件', list_dir: '浏览目录', dev_glob: '匹配文件',
  glob: '匹配文件', dev_asset_get: '取素材', dev_job_logs: '读后台日志',
  dev_read_external_file: '读取外部文件', dev_diagnostics: '诊断代码',
  // 找
  search_code: '检索代码', grep: '定位代码', search_knowledge: '检索知识库',
  read_doc: '查文档', dev_find_references: '查找引用', dev_semantic_locate: '语义定位',
  // 改
  apply_edit: '修改文件', create_file: '创建文件', write_file: '写入文件',
  delete_file: '删除文件', dev_write: '写入文件', dev_apply_edits: '批量修改',
  dev_patch: '应用补丁', dev_region_edit: '修改分区', dev_add_region: '新增分区',
  dev_apply_regions: '应用分区改动', dev_refactor: '重构', dev_migrate: '迁移文件',
  generate_data_file: '生成数据文件', init_regions: '初始化分区',
  // 跑
  run_command: '运行命令', python_exec: '执行代码', game_playtest: '运行校验',
  dev_testrun: '跑测试', dev_apptest: '整页自检', dev_headcheck: '提交自检',
  dev_serve: '启动服务', dev_media: '媒体夹具', dev_vue: '组件自检',
  // 网
  web_search: '网页搜索', web_fetch: '读取网页', web_research: '资料检索',
  dev_http_request: '外部请求',
  // 桌面 / 连接器
  dev_mcp_call: '调用连接器', dev_list_connector_tools: '查看连接器工具',
  dev_route_connector: '连接器路由', dev_desktop_capture: '截取桌面',
  dev_desktop_action: '桌面操作', dev_page_action: '网页操作',
  preview_project: '网页预览', dev_browser_action: '浏览器操作',
  // 任务
  delegate: '委派子代理', orchestrate: '编排任务', start_workflow: '启动工作流',
  // 游戏 / 引擎
  game_screenshot: '截取画面', engine_start: '启动引擎', engine_stop: '停止引擎',
  engine_logs: '读引擎日志', engine_reload: '重载引擎',
  // 版本
  dev_commit: '提交改动', dev_commit_all: '提交变更集', dev_git_diff: '查看改动',
  dev_git_log: '查看提交', dev_lanes: '分区认领', dev_verify_contracts: '校验契约',
  dev_undo_turn: '撤销回合', dev_handoff: '交接任务',
}

/** 工具名 → 卡片图标归类（决定卡片左侧那个小图标）。 */
export type CardIcon = 'read' | 'search' | 'edit' | 'run' | 'web' | 'mcp' | 'task' | 'think'

const TOOL_ICON: Record<string, CardIcon> = {
  read_file: 'read', list_dir: 'read', dev_glob: 'read', glob: 'read',
  dev_asset_get: 'read', dev_job_logs: 'read', dev_read_external_file: 'read',
  dev_diagnostics: 'read',
  search_code: 'search', grep: 'search', search_knowledge: 'search', read_doc: 'search',
  dev_find_references: 'search', dev_semantic_locate: 'search',
  apply_edit: 'edit', create_file: 'edit', write_file: 'edit', delete_file: 'edit',
  dev_write: 'edit', dev_apply_edits: 'edit', dev_patch: 'edit', dev_region_edit: 'edit',
  dev_add_region: 'edit', dev_apply_regions: 'edit', dev_refactor: 'edit',
  dev_migrate: 'edit', generate_data_file: 'edit', init_regions: 'edit',
  run_command: 'run', python_exec: 'run', game_playtest: 'run', dev_testrun: 'run',
  dev_apptest: 'run', dev_headcheck: 'run', dev_serve: 'run', dev_media: 'run',
  dev_vue: 'run',
  web_search: 'web', web_fetch: 'web', web_research: 'web', dev_http_request: 'web',
  dev_mcp_call: 'mcp', dev_list_connector_tools: 'mcp', dev_route_connector: 'mcp',
  dev_desktop_capture: 'mcp', dev_desktop_action: 'mcp', dev_page_action: 'mcp',
  preview_project: 'mcp', dev_browser_action: 'mcp',
  delegate: 'task', orchestrate: 'task', start_workflow: 'task',
}

export function toolTitleOf(tool: string): string {
  if (!tool) return '执行动作'
  return TOOL_TITLE[tool] || tool
}

export function toolIconOf(tool: string): CardIcon {
  return TOOL_ICON[tool] || 'mcp'
}

/** 一次工具调用的解析结果。 */
export interface ToolCall {
  tool: string
  args: { key: string; value: string }[]
}

/** 顶层逗号切分：括号/方括号/引号里的逗号不算分隔符。 */
function splitTopLevel(raw: string): string[] {
  const out: string[] = []
  let depth = 0
  let quote = ''
  let buf = ''
  for (const ch of raw || '') {
    if (quote) {
      buf += ch
      if (ch === quote) quote = ''
      continue
    }
    if (ch === '"' || ch === "'" || ch === '`') { quote = ch; buf += ch; continue }
    if (ch === '(' || ch === '[' || ch === '{') { depth++; buf += ch; continue }
    if (ch === ')' || ch === ']' || ch === '}') { depth--; buf += ch; continue }
    if (ch === ',' && depth === 0) { out.push(buf); buf = ''; continue }
    buf += ch
  }
  if (buf.trim()) out.push(buf)
  return out
}

/**
 * 解析 action 正文：`tool(key: value, key2: value2)` 或多行的 `tool(key: value\nkey2: value2)`。
 *
 * 工具参数的真实约定是**一行一个 key: value**（`run_command(cmd: …\nbackground: true)`），
 * 所以先按行切；同一行里写了多个参数时才按逗号切，且只在后半段**也**是 key: value
 * 时才切——否则那个逗号是值的一部分（`cmd: python -c "a, b"`）。
 *
 * 动作正文里常混着守卫标记（`[已拦截…]`），先剥掉再解析——否则参数摘要会把
 * 拦截说明当成参数值显示出来。
 */
export function parseCall(raw: string): ToolCall {
  const text = (raw || '').replace(/\s*\[已拦截[^\]]*\]\s*$/g, '').trim()
  const m = /^([\w.-]+)\s*\(([\s\S]*)\)\s*$/.exec(text)
  if (!m) return { tool: actionToolOf(text), args: [] }
  const args: { key: string; value: string }[] = []
  for (const line of m[2].split(/\r?\n/)) {
    const seg = line.trim()
    if (!seg) continue
    for (const piece of splitTopLevel(seg)) {
      const p = piece.trim()
      if (!p) continue
      const km = /^\s*([\w.-]+)\s*[:：]\s*([\s\S]*)$/.exec(p)
      if (km) {
        args.push({ key: km[1], value: km[2].trim() })
        continue
      }
      // 不是 key: value ——它是上一个参数的延续（值里带逗号）。
      if (args.length) {
        const last = args[args.length - 1]
        last.value = (last.value ? last.value + ', ' : '') + p
        continue
      }
      args.push({ key: '', value: p })
    }
  }
  return { tool: m[1], args }
}

function ellipsis(text: string, max: number): string {
  const t = (text || '').replace(/\s+/g, ' ').trim()
  if (t.length <= max) return t
  return t.slice(0, max).trimEnd() + '…'
}

/** 参数摘要：一行、最多 3 个参数，其余折叠成 +N。 */
export function paramsSummary(call: ToolCall, max = 3): string {
  if (!call.args.length) return ''
  const shown = call.args.slice(0, max).map((a) => (a.key ? `${a.key}=${ellipsis(a.value, 48)}` : ellipsis(a.value, 48)))
  const rest = call.args.length - shown.length
  return shown.join(' · ') + (rest > 0 ? ` · +${rest}` : '')
}

/**
 * 工具这一步的状态。
 *
 * 口径与轨迹行原来的判定一致：**以工具的真实成败为准**，绝不从返回正文里
 * grep 失败词——read_file 读到的源码天然含「错误/失败」，历史上把一次成功的
 * 读取整排标红。后端随 observation 下发结构化 ok，有则直接采信。
 */
export function cardStateOf(
  observation: ChatTraceItem | undefined,
  streaming: boolean,
): { state: CardState; label: string } {
  const raw = observation?.text || ''
  if (/重复调用|重复空转/.test(raw)) return { state: 'warn', label: '已拦截' }
  if (observation) {
    if (observation.ok === true) return { state: 'ok', label: '完成' }
    if (observation.ok === false) return { state: 'error', label: '失败' }
    // 没有 ok（旧后端 / 未执行的安全护栏）时，只认回包**开头**的失败签名。
    const failed = /^\s*(\[[^\]]*\]|参数缺失|工具执行失败|工具调用已|试做区|幂等保护|搜索结果相关性不足)/
      .test(raw)
    return failed ? { state: 'error', label: '失败' } : { state: 'ok', label: '完成' }
  }
  // 还没有观察结果：流式中就是「进行中」，回合已结束却没回包按完成算
  // （后端在 observation 缺失时也会走 done，标红是虚惊）。
  return streaming ? { state: 'running', label: '进行中' } : { state: 'ok', label: '完成' }
}

export function stateLabelOf(state: CardState): string {
  return state === 'running' ? '进行中' : state === 'error' ? '失败' : state === 'warn' ? '需关注' : '完成'
}

/**
 * 把一条消息折成卡片列表。
 *
 * 规则：
 *  · think 节点（模型原生思考）→ 「深度思考」卡；
 *  · thought / reflection → 「分析」/「复核」卡；
 *  · action → 工具卡，紧随其后的 observation 作为这张卡的**结果**收进去
 *    （Qoder 的工具卡就是这样：结果不是独立一行，是这张卡的输出）；
 *  · 落单的 observation（没有对应 action）自己成一张卡。
 */
export function cardsOf(msg: {
  trace: ChatTraceItem[]
  reasoning?: string
  flow?: ChatFlowItem[]
  status?: string
}): ChatCard[] {
  const trace = msg.trace || []
  const streaming = msg.status === 'streaming'
  const cards: ChatCard[] = []
  let pendingTool: ChatCard | null = null

  const push = (card: ChatCard) => {
    cards.push(card)
    return card
  }

  for (const node of flowOf(msg)) {
    if (node.kind === 'think') {
      const text = cleanNarration(node.text, 12000, true)
      if (!text) continue
      push({
        key: `think-${cards.length}`,
        kind: 'think',
        title: '深度思考',
        tool: '',
        params: '',
        state: 'ok',
        stateLabel: '已完成',
        body: text,
        summary: ellipsis(text, 60),
      })
      pendingTool = null
      continue
    }
    const item = trace[node.trace]
    if (!item) continue

    if (node.kind === 'thought' || item.type === 'reflection') {
      const text = cleanNarration(item.text || '')
      if (!text) continue
      push({
        key: `speak-${cards.length}`,
        kind: 'think',
        title: item.type === 'reflection' ? '复核' : '分析',
        tool: '',
        params: '',
        state: 'ok',
        stateLabel: '已完成',
        body: text,
        summary: ellipsis(text, 60),
      })
      pendingTool = null
      continue
    }

    if (item.type === 'action') {
      const call = parseCall(item.text)
      const card = push({
        key: `tool-${node.trace}`,
        kind: 'tool',
        title: toolTitleOf(call.tool),
        tool: call.tool,
        params: paramsSummary(call),
        state: 'running',
        stateLabel: '进行中',
        body: '',
        summary: '',
        traceIndex: node.trace,
      })
      pendingTool = card
      continue
    }

    // observation：并入上一张工具卡；没有主就自己成卡。
    const { state, label } = cardStateOf(item, streaming)
    if (pendingTool && pendingTool.kind === 'tool') {
      pendingTool.body = item.text || ''
      pendingTool.summary = ellipsis(item.text, 80)
      pendingTool.state = state
      pendingTool.stateLabel = label
      pendingTool.elapsedMs = item.elapsedMs
      pendingTool.evidence = item.evidence
      pendingTool.file_diffs = item.file_diffs
      pendingTool = null
      continue
    }
    push({
      key: `obs-${node.trace}`,
      kind: 'tool',
      title: '返回结果',
      tool: '',
      params: '',
      state,
      stateLabel: label,
      elapsedMs: item.elapsedMs,
      body: item.text || '',
      summary: ellipsis(item.text, 80),
      traceIndex: node.trace,
      evidence: item.evidence,
      file_diffs: item.file_diffs,
    })
  }
  return cards
}

/** 本轮概览（折叠头那一行）：工具数 + 失败数 + 耗时。 */
export function cardsHeadline(cards: ChatCard[]): string {
  const tools = cards.filter((c) => c.kind === 'tool').length
  const failed = cards.filter((c) => c.state === 'error').length
  const parts: string[] = []
  if (tools) parts.push(`${tools} 步工具调用`)
  if (failed) parts.push(`${failed} 步失败`)
  if (!parts.length) parts.push('思考过程')
  return parts.join(' · ')
}
