// 轨迹「按段聚合活动摘要」——一段发言之后跑了什么，就在这段下面挂一行
// 「编辑 N 个文件 · 执行 N 条命令 · 读取 N 个文件 · 检索 N 次 · 联网 N 次 · 调用 N 次 ·
// 编排 N 次 · 思考 N 次」。工作台 ChatDock 与问答页 AskMessage 共用同一份切段与计数口径，
// 避免两边各写一份飘走。这里只关心「分段 + 归类计数」，具体单步中文名仍由各处的
// stepTitle/traceTitle 负责。
import { flowOf } from './chat-types.ts'
import type { ChatFlowItem, ChatTraceItem } from './chat-types'

export type DigestCat =
  | 'edit'    // 改文件 / 写文件
  | 'run'     // 跑命令 / 执行代码 / 跑测试
  | 'read'    // 读文件 / 看日志 / 取产物 / 列目录
  | 'search'  // 检索代码 / grep / glob
  | 'docs'    // 查资料 / 知识库
  | 'web'     // 联网搜索 / 抓网页
  | 'mcp'     // 调外部连接器 / MCP / 桌面自动化
  | 'task'    // 委派 / 编排 / 起工作流
  | 'call'    // 未知工具兜底（保证总数守恒）

// 展示顺序 + 中文量词。最像「干活」的（编辑/执行）放前面，思考垫后。
const CAT_LABEL: Record<DigestCat, [string, string]> = {
  edit: ['编辑', '个文件'],
  run: ['执行', '条命令'],
  read: ['读取', '个文件'],
  search: ['检索', '次'],
  docs: ['查资料', '次'],
  web: ['联网', '次'],
  mcp: ['调用', '次'],
  task: ['编排', '次'],
  call: ['调用', '次'],
}
const CAT_ORDER: DigestCat[] = ['edit', 'run', 'read', 'search', 'docs', 'web', 'mcp', 'task', 'call']

// 工具名 → 归类。尽量覆盖后端 tools.py 里的真实工具名；未知工具一律并入「调用」，
// 保证总数守恒、不静默丢计数。
const TOOL_CAT: Record<string, DigestCat> = {
  // 编辑 / 写文件
  apply_edit: 'edit', create_file: 'edit', write_file: 'edit', edit_file: 'edit',
  dev_write: 'edit', dev_apply_edits: 'edit', dev_patch: 'edit', dev_move: 'edit',
  dev_refactor: 'edit', dev_region_edit: 'edit', dev_add_region: 'edit',
  dev_apply_regions: 'edit', init_regions: 'edit', generate_data_file: 'edit',
  // 执行 / 跑命令 / 跑测试
  run_command: 'run', python_exec: 'run', game_playtest: 'run',
  dev_testrun: 'run', dev_apptest: 'run', dev_headcheck: 'run',
  // 读取 / 看日志 / 取产物 / 列目录
  read_file: 'read', dev_asset_get: 'read', dev_job_logs: 'read', list_dir: 'read',
  // 检索代码
  search_code: 'search', grep: 'search', glob: 'search',
  // 查资料 / 知识库
  search_knowledge: 'docs', read_doc: 'docs',
  // 联网
  web_search: 'web', web_fetch: 'web', web_research: 'web', dev_http_request: 'web',
  // 外部连接器 / MCP / 桌面自动化
  dev_mcp_call: 'mcp', dev_mcp_add: 'mcp', dev_mcp_probe: 'mcp', dev_mcp_discover: 'mcp',
  dev_mcp_decide: 'mcp', dev_mcp_remove: 'mcp', dev_mcp_search: 'mcp',
  dev_mcp_discover_from_need: 'mcp', dev_list_connectors: 'mcp', dev_list_connector_tools: 'mcp',
  dev_route_connector: 'mcp', dev_desktop_capture: 'mcp', dev_desktop_action: 'mcp',
  dev_page_action: 'mcp', dev_vue: 'mcp', dev_serve: 'mcp', dev_media: 'mcp',
  // 委派 / 编排 / 起工作流
  delegate: 'task', orchestrate: 'task', start_workflow: 'task',
}

/** 从 action 步骤正文里抠出工具名：形如 `run_command(...)`。 */
export function actionToolOf(raw: string): string {
  return /^([\w.-]+)\s*\(/.exec((raw || '').trim())?.[1] || ''
}

// 模型有时会把「即将写进文件的代码」整段复述进 reasoning 流，两种形态：
//  · 写文件工具：「……接下来创建 models.py。 new_text: """完整源码……」
//  · 改用 python_exec 写：「……绕开收尾问题。 from pathlib import Path code = '''完整源码'''」
// 于是本应简短的解说被撑成几千字代码块，源码泄漏到可见的推理轨迹里。代码只应
// 落盘、在工具摘要行里以「编辑 N 个文件」体现，不该摊在解说上。
// reasoning 流里的换行常被压成空格，不能按行锚定；改为在整段里找「最早的代码
// 起点」下标截断，只留前面的自然语言旁白。关键词两侧要求 ASCII 词边界，避免误伤
// 中文旁白里偶现的 import/def 等字样。
const NAR_CODE_STARTS: RegExp[] = [
  /\b(?:new_text|old_text|file_text)\s*[:：]/,        // 写文件工具参数
  /'''|"""/,                                            // 三引号串（含 docstring）
  /\bfrom\s+[A-Za-z_][\w.]*\s+import\b/,              // from x import y
  /\b(?:import|def|class|return)\s+[A-Za-z_(]/,       // import x / def f / class C
  /[A-Za-z_]\w*\s*=\s*(?:r?'''|r?""")/,               // code = '''…
  /@[\w.]+\(/,                                          // 装饰器调用
]
const NAR_MAX = 600
export function cleanNarration(raw: string): string {
  let t = (raw || '').trim()
  let cut = -1
  for (const re of NAR_CODE_STARTS) {
    const m = re.exec(t)
    if (m && (cut < 0 || m.index < cut)) cut = m.index
  }
  if (cut > 0) t = t.slice(0, cut)
  else if (cut === 0) t = ''
  t = t.replace(/\n{3,}/g, '\n\n').trim()
  if (t.length > NAR_MAX) t = t.slice(0, NAR_MAX).trimEnd() + ' …'
  return t
}

export interface TraceDigest {
  parts: { label: string; count: number; unit: string }[]
  /** 「编辑 1 个文件 · 执行 1 条命令 · 思考 2 次」 */
  text: string
  /** 工具调用总数（action 步数）；为 0 的段不显示摘要行 */
  toolTotal: number
  /** 思考步数（thought + reflection） */
  think: number
}

/** 把一组工具名 + 思考次数聚合成活动摘要。 */
export function digestFromTools(tools: string[] | undefined | null, think = 0): TraceDigest {
  const counts = new Map<DigestCat, number>()
  for (const tool of tools || []) {
    const cat = (tool && TOOL_CAT[tool]) || 'call'
    counts.set(cat, (counts.get(cat) || 0) + 1)
  }
  const parts = CAT_ORDER
    .filter((c) => counts.get(c))
    .map((c) => ({ label: CAT_LABEL[c][0], count: counts.get(c)!, unit: CAT_LABEL[c][1] }))
  if (think) parts.push({ label: '思考', count: think, unit: '次' })
  const text = parts.map((p) => `${p.label} ${p.count} ${p.unit}`).join(' · ')
  return { parts, text, toolTotal: (tools || []).length, think }
}

/** 一段「发言 + 它之后的工具」：解说在前，活动摘要紧跟其后，再进入下一段。 */
export interface TraceSegment {
  /** 本段的解说段落（仅 ReAct Thought / 复核；原始 reasoning 不再进解说） */
  narration: string[]
  /** 本段动作/返回在 msg.trace 里的下标（展开看单步明细用） */
  rows: number[]
  /** 本段的活动摘要（只统计本段的工具与思考，不外溢到别的段） */
  digest: TraceDigest
  /** 摘要行图标：动过文件用 ✎，否则用 ◷ */
  glyph: '✎' | '◷'
}

/**
 * 按发言段落把交错流切段。
 *
 * 对话效果是「解说 → 摘要 → 解说 → 摘要」：一段发言之后跑了哪些工具，就在这段
 * 下面挂一行灰色摘要；下一次发言即开新段。可见解说只取简洁的 ReAct Thought / 复核；
 * 模型原生 reasoning_content（think 节点）不再摊出来——思考型模型会把即将写进文件
 * 的代码也打在思考流里，摊出来既长又泄漏源码，这里只把它计入「思考 N 次」。
 * 观察（observation）当工具返回看。
 */
export function traceSegments(msg: {
  trace: ChatTraceItem[]
  reasoning?: string
  flow?: ChatFlowItem[]
}): TraceSegment[] {
  const trace = msg.trace || []
  const segments: TraceSegment[] = []
  let narration: string[] = []
  let rows: number[] = []
  let tools: string[] = []
  let think = 0

  const flush = () => {
    if (!narration.length && !rows.length) return
    const digest = digestFromTools(tools, think)
    const edited = digest.parts.some((p) => p.label === CAT_LABEL.edit[0])
    segments.push({ narration, rows, digest, glyph: edited ? '✎' : '◷' })
    narration = []
    rows = []
    tools = []
    think = 0
  }

  for (const node of flowOf(msg)) {
    // 原始 reasoning_content（think 节点）不再摊成可见解说：思考型模型会把即将
    // 写进文件的代码也打在这条流里，解说因此被撑成几千字代码块。这里只把它计入
    // 「思考 N 次」，可见旁白一律交给简洁的 ReAct Thought / 复核。
    if (node.kind === 'think') { think++; continue }
    const item = trace[node.trace]
    const spoken = node.kind === 'thought' || item?.type === 'reflection'
    if (spoken) {
      // 这一段已经跑过工具：先结段，再让这句话成为下一段的开头
      if (rows.length) flush()
      think++
      const clean = cleanNarration(item?.text || '')
      if (clean) narration.push(clean)
      continue
    }
    if (!item) continue
    // 动作/观察都属于「这段发言之后干了什么」
    rows.push(node.trace)
    if (item.type === 'action') tools.push(actionToolOf(item.text))
  }
  flush()
  return segments
}
