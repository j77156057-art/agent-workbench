import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { mdToHtml, extractFileRefs, extractWebRefs } from '../markdown'
import type { FileRef } from '../markdown'
// 同 useChatStream：node --experimental-strip-types 直接跑这些模块，
// 相对导入必须带扩展名（tests/test_frontend_node_tests.py 的守卫链）。
import { flowOf } from './chat-types.ts'
import type { ChatFlowItem } from './chat-types.ts'
import { useWorkbench } from './workbench.ts'
import { prefs } from '../../ask/askPrefs'

/** 消息渲染所需的最小结构（ChatMsg 等具体消息类型按结构鸭子匹配） */
export interface RenderMessage {
  id: number
  text: string
  status: string
  trace: { type: string; text: string; at?: number; elapsedMs?: number }[]
  /** 深度思考整块（交错流切分信息只在 flow 里；无 flow 时兜底合成第一个 think） */
  reasoning?: string
  /** 思考与工具调用的交错流（流式期构建；历史/演示路径为空） */
  flow?: ChatFlowItem[]
  startedAt?: number
  lastActivityAt?: number
  finishedAt?: number
  workflow?: { workflowId: string; seed?: unknown }
}

/**
 * 对话消息的展示层：流式 markdown 节流渲染、成稿/引用缓存、trace 步骤
 * 标题/状态/计时、思考折叠。纯派生逻辑，不持有消息数组本身。
 */
export function useMessageRender<T extends RenderMessage>(isActive: () => boolean) {
  const { nodeExists, jumpToLine, revealPath } = useWorkbench()

  // ------------------------------------------------- 流式 markdown 节流
  // token 仍逐帧进 m.text（便宜），markdown 重渲染按消息限流到 ~8 次/秒——
  // 这是人眼「格式实时跟随」与「不逐 token 重建 DOM」之间的平衡点。
  const LIVE_MD_INTERVAL = 125
  const liveHtml = reactive(new Map<number, string>())
  const liveMdTimers = new Map<number, ReturnType<typeof setTimeout>>()
  let liveMdLastAt = new Map<number, number>()
  function renderLiveMd(turn: T) {
    liveMdTimers.delete(turn.id)
    liveMdLastAt.set(turn.id, performance.now())
    liveHtml.set(turn.id, mdToHtml(turn.text || ''))
  }
  function scheduleLiveMd(turn: T) {
    if (liveMdTimers.has(turn.id)) return
    // 首个 token 帧立即成型，避免节流窗口内 fallback 每帧解析
    if (!liveMdLastAt.has(turn.id)) { renderLiveMd(turn); return }
    const last = liveMdLastAt.get(turn.id) ?? 0
    const wait = Math.max(0, LIVE_MD_INTERVAL - (performance.now() - last))
    liveMdTimers.set(turn.id, setTimeout(() => renderLiveMd(turn), wait))
  }
  /** 回合结束（done/stop/error/final 覆盖）：立即排一次待渲染并交还完成态渲染。 */
  function finishLiveMd(id: number, renderNow: () => string) {
    const timer = liveMdTimers.get(id)
    if (timer) { clearTimeout(timer); liveMdTimers.delete(id) }
    liveMdLastAt.delete(id)
    liveHtml.delete(id)
    // 触发 answerHtml 缓存写入，完成态 v-html 与最后一帧不会出现格式回退
    renderNow()
  }
  function streamHtmlOf(msg: T): string {
    return liveHtml.get(msg.id) ?? mdToHtml(msg.text || '')
  }

  // ------------------------------------------------- 成稿/引用缓存
  // 模板每秒会因计时刷新重渲染；markdown 与引用提取都按 (消息 id, 文本) 缓存，
  // 文本不变时不做重复解析（流式期间走纯文本层，根本不进 markdown 解析）。
  const answerHtmlCache = new Map<number, { src: string; html: string }>()
  function answerHtml(msg: T): string {
    const hit = answerHtmlCache.get(msg.id)
    if (hit && hit.src === msg.text) return hit.html
    const html = mdToHtml(msg.text || '')
    answerHtmlCache.set(msg.id, { src: msg.text, html })
    return html
  }
  const refsCache = new Map<number, { src: string; refs: FileRef[] }>()
  function refsOf(msg: T): FileRef[] {
    // 只对真实存在于文件树中的路径生成卡片（过滤幻觉引用）
    const hit = refsCache.get(msg.id)
    if (hit && hit.src === msg.text) return hit.refs
    const refs = extractFileRefs(msg.text).filter((r) => nodeExists(r.path))
    refsCache.set(msg.id, { src: msg.text, refs })
    return refs
  }
  const webRefsCache = new Map<number, { src: string; refs: ReturnType<typeof extractWebRefs> }>()
  function webRefsOf(msg: T) {
    const hit = webRefsCache.get(msg.id)
    if (hit && hit.src === msg.text) return hit.refs
    const refs = extractWebRefs(msg.text)
    webRefsCache.set(msg.id, { src: msg.text, refs })
    return refs
  }
  async function openRef(r: FileRef) {
    await jumpToLine(r.path, r.line || 1)
    revealPath(r.path)
  }

  // ------------------------------------------------- trace 步骤展示
  const TRACE_LABEL: Record<string, string> = {
    thought: '分析摘要', action: '执行动作', observation: '返回结果', reflection: '执行提示',
  }
  const TRACE_GLYPH: Record<string, string> = {
    thought: '◌', action: '↗', observation: '✓', reflection: '↻',
  }
  const nowTick = ref(Date.now())
  let elapsedTimer: number | null = null
  // 计时钟只在「有活动回合」时走：空闲/全部结束后不再每 500ms 触发响应式刷新。
  const elapsedTicking = computed(isActive)
  watch(elapsedTicking, (on) => {
    if (on && elapsedTimer === null) {
      nowTick.value = Date.now()
      elapsedTimer = window.setInterval(() => { nowTick.value = Date.now() }, 500)
    } else if (!on && elapsedTimer !== null) {
      window.clearInterval(elapsedTimer)
      elapsedTimer = null
      nowTick.value = Date.now()
    }
  })

  /**
   * 步骤行的展开状态：手动展开与手动收起各记一个集合。
   * 为什么要两个：显式操作必须能盖过自动规则——只记「展开」的单一集合会让用户
   * 点一条想收起来时点了没反应（自动规则仍然判它该开着）。
   *
   * 2026-10-14：交错流常态可见后，自动规则从「流式中整轮摊开」收成「只摊开
   * 最新一条」——思考节点已全程外露，每条动作行再全文本展开会把流式画面撑爆；
   * 历史行想读全文点开即可。
   */
  const activityPinned = ref<Set<string>>(new Set())
  const activityMuted = ref<Set<string>>(new Set())
  function activityKey(id: number, index: number) { return `${id}:${index}` }
  function activityIsOpen(id: number, index: number, msg: T) {
    const key = activityKey(id, index)
    if (activityPinned.value.has(key)) return true
    if (activityMuted.value.has(key)) return false
    return prefs.autoExpandTrace && msg.status === 'streaming' && index === msg.trace.length - 1
  }
  function toggleActivity(id: number, index: number, msg: T) {
    const key = activityKey(id, index)
    const open = activityIsOpen(id, index, msg)
    const nextPin = new Set(activityPinned.value)
    const nextMute = new Set(activityMuted.value)
    nextPin.delete(key)
    nextMute.delete(key)
    if (open) nextMute.add(key)
    else nextPin.add(key)
    activityPinned.value = nextPin
    activityMuted.value = nextMute
  }
  function traceTitle(item: { type: string; text: string }) {
    const raw = item.text.trim()
    const tool = raw.match(/^([\w.-]+)\s*\(/)?.[1]
    if (item.type === 'action' && tool) {
      const names: Record<string, string> = {
        search_code: '检索代码', search_knowledge: '检索知识库', read_file: '读取文件',
        grep: '定位代码', run_command: '运行命令', game_playtest: '运行校验',
        web_search: '网页搜索', web_fetch: '读取网页', dev_mcp_call: '调用 MCP',
        dev_list_connector_tools: '查看 MCP 工具', apply_edit: '修改文件', create_file: '创建文件',
        delegate: '委派 Subagent', orchestrate: '编排任务',
      }
      return names[tool] || tool
    }
    return TRACE_LABEL[item.type] || item.type
  }
  /**
   * 折叠行的摘要文案。
   * 以前在这里砍到 89 字 + '…'。实测每轮解说的长度是 71–165 字，也就是说**正常长度
   * 的解说也会被砍**，而砍完这一刀 CSS 那边还有一层 ellipsis —— 两层截断叠在一起，
   * 用户看到的就是「只剩开头」。现在 JS 不砍了，长度交给 CSS 的两行 line-clamp：
   * 一行放得下多少字取决于对话台宽度（1440 视口下摘要列约 1030px ≈ 86 个中文字），
   * 只有 CSS 这一层能跟着宽度走。全文始终在展开的事件详情里。
   */
  function traceSummary(item: { type: string; text: string }) {
    const raw = item.text.trim().replace(/\s+/g, ' ')
    if (item.type === 'action') {
      const open = raw.indexOf('(')
      if (open > 0) return raw.slice(open).replace(/\s*\[已拦截.*$/, '')
    }
    return raw
  }
  function traceState(msg: T, item: { type: string; text: string }, index: number): 'running' | 'ok' | 'warn' | 'error' {
    const raw = item.text
    if (/重复调用|重复空转/.test(raw)) return 'warn'
    if (/失败|错误|超时|拦截|阻断|未执行|不可用|exception/i.test(raw)) return 'error'
    if (item.type === 'reflection' && /重试|换思路|重新规划|注意/i.test(raw)) return 'warn'
    if (msg.status === 'streaming' && index === msg.trace.length - 1) return 'running'
    return 'ok'
  }
  function traceStateLabel(state: ReturnType<typeof traceState>, item?: { text: string }) {
    if (state === 'warn' && item && /重复调用|重复空转/.test(item.text)) return '已拦截'
    return state === 'running' ? '进行中' : state === 'error' ? '失败' : state === 'warn' ? '需关注' : '完成'
  }
  function traceElapsed(item: { elapsedMs?: number }) {
    if (!item.elapsedMs) return ''
    if (item.elapsedMs < 1000) return `${item.elapsedMs}ms`
    return `${(item.elapsedMs / 1000).toFixed(1)}s`
  }
  function elapsedLabel(msg: T) {
    if (!msg.startedAt) return ''
    const end = msg.finishedAt || nowTick.value
    const seconds = Math.max(0, (end - msg.startedAt) / 1000)
    return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`
  }
  function slowResponseLabel(msg: T) {
    if (msg.status !== 'streaming' || !msg.lastActivityAt) return ''
    const quiet = Math.max(0, nowTick.value - msg.lastActivityAt)
    if (quiet < 15000) return ''
    return `已经 ${Math.round(quiet / 1000)} 秒没有收到新进展；模型可能仍在推理或执行工具。现场会保留，必要时可停止后继续。`
  }

  /** 清空对话时重置全部渲染缓存与待渲染计时器 */
  function resetRenderState() {
    answerHtmlCache.clear()
    refsCache.clear()
    webRefsCache.clear()
    liveMdTimers.forEach(t => clearTimeout(t))
    liveMdTimers.clear()
    liveMdLastAt.clear()
    liveHtml.clear()
    activityPinned.value = new Set()
    activityMuted.value = new Set()
  }

  // 组件卸载时清掉计时器，避免离开对话台后仍触发响应式刷新
  onBeforeUnmount(() => {
    if (elapsedTimer !== null) { window.clearInterval(elapsedTimer); elapsedTimer = null }
    liveMdTimers.forEach(t => clearTimeout(t))
    liveMdTimers.clear()
  })

  return {
    liveHtml,
    scheduleLiveMd,
    finishLiveMd,
    streamHtmlOf,
    answerHtml,
    refsOf,
    webRefsOf,
    openRef,
    TRACE_GLYPH,
    toggleActivity,
    activityIsOpen,
    traceTitle,
    traceSummary,
    traceState,
    traceStateLabel,
    traceElapsed,
    elapsedLabel,
    slowResponseLabel,
    resetRenderState,
  }
}
