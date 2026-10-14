<script setup lang="ts">
// 单条对话气泡：用户消息（含图片）/ 助手消息。
// 助手消息支持 SSE 流式渲染：深度思考折叠块、ReAct 轨迹（分析/动作/观察/复核）、
// Markdown 正文、文件行号引用 chips（点击打开只读源码弹窗）。
import { computed, defineAsyncComponent, onBeforeUnmount, ref, watch } from 'vue'
import { mdToHtml, extractFileRefs } from '../../workbench/markdown'
import { prefs } from '../askPrefs'
// 工作流卡片 104KB，只有触发工作流的消息才渲染：按需加载，问答首屏不背
const WorkflowCard = defineAsyncComponent(() => import('../../workbench/components/WorkflowCard.vue'))
import type { ChatMsg, ChatTraceItem } from '../../workbench/composables/chat-types'

const props = defineProps<{ msg: ChatMsg }>()
const emit = defineEmits<{
  (e: 'openFile', path: string, line: number): void
  (e: 'wfGate', open: boolean): void
  (e: 'wfActivity'): void
}>()

const html = computed(() => mdToHtml(props.msg.text || ''))
const refs = computed(() => extractFileRefs(props.msg.text || ''))
const streaming = computed(() => props.msg.status === 'streaming')

// 展示偏好直接读 askPrefs 里那份共享 reactive —— 不再自己 ref(loadAskPrefs()) + 挂
// ASK_PREFS_EVENT 监听器。同一件事两套机制，漏挂或时序差就是「设置改了界面不跟」。

// 引用条数上限（0 = 不限制）：答案里可能引用几十处代码，默认只挑前几条展示。
// 但截断不能是静默的 —— 少掉的那几条要数得出来、点得开，否则用户以为答案只引用了这些。
const refsAll = ref(false)
const shownRefs = computed(() => {
  const all = refs.value
  const n = prefs.maxRefs
  return n > 0 && !refsAll.value ? all.slice(0, n) : all
})
const hiddenRefs = computed(() => refs.value.length - shownRefs.value.length)
const canCollapseRefs = computed(() => refsAll.value && prefs.maxRefs > 0 && refs.value.length > prefs.maxRefs)

// ---------------- 交错流（思考 ↔ 工具调用按发生顺序） ----------------
// 2026-10-14 重构：不再是「思考整块在上 + 过程流整块在下」的两段式，而是
// think → 调用 → 返回 → think → … 的一条故事线，答案之前全部可见、无边框。
// reasoning 的轮次切分发生在 useChatStream（动作事件即切点，思考随帧实时
// 生长）；历史回放/演示等绕开管线的路径没有 flow，由 flowOf 兜底按 trace
// 顺序合成（reasoning 只剩整块，当第一个 think——切点没留下来，不假装知道）。
import { flowOf } from '../../workbench/composables/chat-types'
const flowItems = computed(() => flowOf(props.msg))
/** 交错流是否有可见节点：思考类（reasoning 分段 / Thought）恒显示，
 *  动作/返回行随「显示推理轨迹」偏好。全不可见时不渲染容器，避免留一条
 *  空虚线在答案上方。 */
const hasFlow = computed(() => prefs.showTrace
  || flowItems.value.some((n) => n.kind === 'think' || n.kind === 'thought'))
function traceAt(node: { trace: number }): ChatTraceItem {
  return props.msg.trace[node.trace]
}

// 动作/返回行的全文展开：手动展开与手动收起各记一个集合——显式操作必须能
// 盖过「流式中最新一条自动摊开」的自动规则（只记展开的单一集合会让用户点
// 一条想收起来时点了没反应）。
const openRows = ref<Set<number>>(new Set())
const mutedRows = ref<Set<number>>(new Set())
function rowOpen(i: number) {
  if (openRows.value.has(i)) return true
  if (mutedRows.value.has(i)) return false
  return prefs.autoExpandTrace && streaming.value && i === flowItems.value.length - 1
}
function toggleRow(i: number) {
  const open = rowOpen(i)
  const nextOpen = new Set(openRows.value)
  const nextMute = new Set(mutedRows.value)
  nextOpen.delete(i)
  nextMute.delete(i)
  if (open) nextMute.add(i)
  else nextOpen.add(i)
  openRows.value = nextOpen
  mutedRows.value = nextMute
}

const TRACE_LABEL: Record<string, string> = {
  thought: '分析', action: '工具', observation: '观察', reflection: '复核',
}
const TOOL_NAMES: Record<string, string> = {
  search_code: '检索代码', search_knowledge: '检索知识库', read_file: '读取文件',
  grep: '定位代码', run_command: '运行命令', game_playtest: '运行校验',
  web_search: '网页搜索', web_fetch: '读取网页', dev_mcp_call: '调用 MCP',
  apply_edit: '修改文件', create_file: '创建文件',
  delegate: '委派子任务', orchestrate: '编排任务',
}
function oneLine(s: string, n = 80): string {
  const t = s.trim().replace(/\s+/g, ' ')
  return t.length > n ? t.slice(0, n) + '…' : t
}
function stepTitle(item: ChatTraceItem): string {
  const raw = item.text.trim()
  if (item.type === 'action') {
    const tool = /^([\w.-]+)\s*\(/.exec(raw)?.[1]
    if (tool) return TOOL_NAMES[tool] || tool
  }
  return TRACE_LABEL[item.type] || item.type
}
function stepSummary(item: ChatTraceItem): string {
  // 单条轨迹缺 text 曾让整条消息渲染抛错挂掉（页面级 pageerror），这里做兜底
  const raw = (item.text ?? '').trim()
  if (item.type === 'action') {
    const tool = /^([\w.-]+)\s*\(([^)]*)\)/.exec(raw)
    if (tool) return oneLine(tool[2].replace(/^['"]|['"]$/g, '') || tool[1], 72)
  }
  if (item.type === 'observation') {
    const len = raw.replace(/\s+/g, '').length
    return len > 0 ? `返回 ${len} 个字符` : '返回结果'
  }
  // 解说类（分析/复核）实测 71–165 字，砍到 72 等于「只剩开头」；全文交给 CSS
  // 两行 clamp 收，长度跟着容器宽度走。工具行仍压成一行参数，观察行仍只报字符数
  // ——那两类是技术串和噪声，摊开反而更杂。
  return raw.replace(/\s+/g, ' ')
}
function fmtElapsed(ms?: number): string {
  if (!ms) return ''
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}

// 流式阶段指示：把「取证在跑」做成看得见的状态行 + 秒表（对齐工作台的确定感）
const now = ref(Date.now())
let phaseTimer = 0
watch(streaming, (on) => {
  if (on) {
    now.value = Date.now()
    if (!phaseTimer) phaseTimer = window.setInterval(() => { now.value = Date.now() }, 1000)
  } else if (phaseTimer) {
    clearInterval(phaseTimer)
    phaseTimer = 0
  }
}, { immediate: true })
onBeforeUnmount(() => { if (phaseTimer) clearInterval(phaseTimer) })

const elapsedLabel = computed(() => {
  if (!props.msg.startedAt) return ''
  const end = props.msg.finishedAt || now.value
  const s = Math.max(0, Math.round((end - props.msg.startedAt) / 1000))
  return s >= 1 ? `${s}s` : ''
})

const phaseLabel = computed(() => {
  const t = props.msg
  if (t.workflow?.workflowId) return '工作流进行中，在下方卡片查看进度与审批'
  const last = [...t.trace].reverse().find((x) => x.text.trim())
  if (last) {
    if (last.type === 'thought') return '正在分析问题'
    if (last.type === 'action') return '正在调用工具：' + stepTitle(last)
    if (last.type === 'observation') return '正在阅读结果、组织回答'
    if (last.type === 'reflection') return '正在复核思路'
  }
  if (t.reasoning) return '正在深度思考'
  return '正在等待模型或工具返回'
})
</script>

<template>
  <div class="msg" :class="msg.role">
    <div v-if="msg.role === 'user'" class="msg-row user-row">
      <div class="bubble user-bubble">
        <div v-if="msg.images?.length" class="user-imgs">
          <img v-for="(src, i) in msg.images" :key="i" :src="src" alt="附件图片">
        </div>
        <div v-if="msg.text" class="user-text">{{ msg.text }}</div>
      </div>
    </div>

    <div v-else class="msg-row ai-row">
      <div class="ai-avatar" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="16" height="16"><path fill="#fff" d="M12 2l2.4 5.6L20 9l-4 4 1 6-5-3-5 3 1-6-4-4 5.6-1.4z"/></svg>
      </div>
      <div class="ai-col">
        <!-- 2026-10-14：交错流——思考（reasoning 分段 / ReAct Thought）与工具
             调用按发生顺序排成一条故事线：think → 调用 → 返回 → think → …
             答案之前全部可见、无边框（容器只有左侧一条虚线时间线）。
             点动作/返回行看该事件全文；流式中最新一行自动摊开（偏好可关）。 -->
        <div v-if="hasFlow" class="flow">
          <template v-for="(node, i) in flowItems" :key="i">
            <div v-if="node.kind === 'think' || node.kind === 'thought'" class="flow-think">
              <span class="flow-dot" :class="{ on: streaming && i === flowItems.length - 1 }"></span>
              <div class="flow-think-body">{{ node.kind === 'think' ? node.text : traceAt(node).text.trim() }}</div>
            </div>
            <template v-else-if="prefs.showTrace">
              <button class="flow-row" :class="node.kind" @click="toggleRow(i)">
                <span class="flow-glyph">{{ node.kind === 'action' ? '↗' : '↳' }}</span>
                <span class="flow-title">{{ stepTitle(traceAt(node)) }}</span>
                <span v-if="!rowOpen(i)" class="flow-summary">{{ stepSummary(traceAt(node)) }}</span>
                <span v-if="fmtElapsed(traceAt(node).elapsedMs)" class="flow-time">{{ fmtElapsed(traceAt(node).elapsedMs) }}</span>
                <span class="chev" :class="{ open: rowOpen(i) }">⌄</span>
              </button>
              <pre v-if="rowOpen(i)" class="flow-detail">{{ traceAt(node).text.trim() }}</pre>
            </template>
          </template>
        </div>

        <!-- 工作流卡片：start_workflow 的动作与观察由卡片承载，不进交错流 -->
        <WorkflowCard
          v-if="msg.workflow?.workflowId"
          :key="msg.workflow.workflowId"
          :workflow-id="msg.workflow.workflowId"
          :seed="msg.workflow.seed"
          @activity="emit('wfActivity')"
          @gate="(open: boolean) => emit('wfGate', open)"
        />

        <!-- 正文（永远在最下、常驻展开、不会因过程流回缩而消失） -->
        <div class="bubble ai-bubble">
          <div v-if="!msg.text && streaming" class="typing" :title="`已等待 ${elapsedLabel || '0s'}`">
            <span class="typing-phase">{{ phaseLabel }}</span>
            <span v-if="elapsedLabel" class="typing-time">{{ elapsedLabel }}</span>
            <span class="td"></span><span class="td"></span><span class="td"></span>
          </div>
          <div v-else-if="msg.text" class="answer" :class="{ streaming }" v-html="html"></div>
          <div v-if="msg.status === 'stopped' && msg.text" class="flag stopped">已停止</div>
          <div v-if="msg.status === 'error'" class="flag error">{{ msg.error || '请求失败' }}</div>
        </div>

        <!-- 文件引用（条数上限由展示偏好控制；被截断的条数必须数得出来、点得开） -->
        <div v-if="shownRefs.length" class="refs">
          <button
            v-for="(r, i) in shownRefs" :key="i"
            class="ref-chip"
            :title="r.path + (r.line ? ':' + r.line : '')"
            @click="emit('openFile', r.path, r.line)"
          >
            <svg viewBox="0 0 24 24" width="13" height="13"><path fill="currentColor" d="M6 2h8l4 4v16H6V2zm7 1.5V7h3.5L13 3.5zM8 11h8v1.5H8V11zm0 3.5h8V16H8v-1.5zm0 3.5h5v1.5H8V18z"/></svg>
            <span class="ref-path">{{ r.path }}</span><span v-if="r.line" class="ref-line">:{{ r.line }}</span>
          </button>
          <button v-if="hiddenRefs > 0" type="button" class="ref-chip ref-more"
                  :title="'还有 ' + hiddenRefs + ' 处引用未显示'"
                  @click="refsAll = true">+{{ hiddenRefs }} 处</button>
          <button v-else-if="canCollapseRefs" type="button" class="ref-chip ref-more"
                  @click="refsAll = false">收起</button>
        </div>

        <!-- 运行期提示（transient notice）：只在流式中显示，不进历史 -->
        <div v-if="msg.progress && streaming" class="notice">{{ msg.progress }}</div>
        <!-- 通知 -->
        <div v-for="(n, i) in msg.notices" :key="'n' + i" class="notice">{{ n }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ===== DocMind 问答气泡 · 高级粗野风 ===== */
.msg-row { display: flex; gap: 12px; animation: msg-in .22s ease both; }
.user-row { justify-content: flex-end; }
@keyframes msg-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) {
  .msg-row { animation: none !important; }
  .td, .flow-dot.on { animation: none !important; }
  .td { opacity: .5; }
  .answer.streaming::after { animation: none !important; opacity: .6; }
}

.bubble { border-radius: 0; padding: 12px 16px; max-width: 100%; }
.user-bubble {
  max-width: min(560px, 82%);
  background: var(--accent);
  color: #fff;
  border: var(--edge-w-strong) solid var(--edge-ink);
  border-radius: var(--r-sm);
  box-shadow: var(--sh-hard-sm);
}
.user-text { white-space: pre-wrap; word-break: break-word; font-family: var(--font-sans); }
.user-imgs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.user-imgs img { max-width: 180px; max-height: 180px; border: var(--edge-w-strong) solid var(--edge-ink);
border-radius: var(--r-sm); }

.ai-avatar {
  flex: 0 0 32px;
  width: 32px;
  height: 32px;
  border: var(--edge-w-heavy) solid var(--edge-ink);
  border-radius: 0;
  background: var(--ink);
  display: flex;
  align-items: center;
  justify-content: center;
  margin-top: 2px;
}
.ai-col { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
/* 2026-10-14：无边框 chat——去 background / border / box-shadow / padding，让答案
   就是文字流；max-width 仍按容器 100% 跑，不挤压但也不再"卡在一个框里"。 */
.ai-bubble {
  background: transparent;
  border: none;
  border-radius: 0;
  box-shadow: none;
  padding: 0;
  width: 100%;
  max-width: 100%;
}
.answer { word-break: break-word; font-family: var(--font-sans); line-height: 1.65; }
.answer :deep(.md-link) { color: var(--accent); border-bottom: 2px solid transparent; }
.answer :deep(.md-link:hover) { border-bottom-color: var(--accent); }

.typing { color: var(--text-muted); font-size: 13px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-family: var(--font-sans); }
.typing-phase { color: var(--text); font-weight: 600; }
.typing-time {
  font-family: var(--font-mono); font-size: 12px; color: var(--text-muted);
  background: var(--bg-hover); border: 1px solid var(--border); padding: 1px 8px;
}
.td {
  width: 6px; height: 6px; background: var(--accent-2);
  display: inline-block; animation: td-blink 1.4s ease-in-out infinite;
}
.td ~ .td { animation-delay: .18s; }
.td ~ .td ~ .td { animation-delay: .36s; }
@keyframes td-blink { 0%, 100% { opacity: .2; } 50% { opacity: 1; } }

/* 流式正文末端插入粗野风方块光标，传达「生成中」的实时感（仅 streaming 时显示） */
.answer.streaming::after {
  content: '';
  display: inline-block;
  width: 2px;
  height: 1.05em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--ink);
  animation: ai-caret 1s steps(1, end) infinite;
}
@keyframes ai-caret { 0%, 50% { opacity: 1; } 50.01%, 100% { opacity: 0; } }

.flag { margin-top: 8px; font-family: var(--font-mono); font-size: 12px; }
.flag.stopped { color: var(--text-faint); }
.flag.error { color: var(--danger); }

/* 交错流（2026-10-14）：思考 ↔ 工具调用按发生顺序的一条故事线。
   无边框——容器只有左侧一条虚线时间线；节点全是文字行。思考（reasoning
   分段 / ReAct Thought）实时外露随帧生长，动作/返回行一行摘要、点开看全文。
   替代了旧的「reason 独立块 + 可折叠过程流」两段式。 */
.flow {
  display: flex;
  flex-direction: column;
  margin: 2px 0 6px 3px;
  padding-left: 12px;
  border-left: 1px dashed var(--border);
}
.flow-think { display: flex; gap: 8px; padding: 2px 0; }
.flow-dot {
  flex: 0 0 6px; width: 6px; height: 6px; margin-top: 6px;
  border-radius: 50%; background: var(--text-faint);
}
.flow-dot.on { background: var(--accent-2); animation: reason-pulse 1.1s infinite; }
@keyframes reason-pulse { 0%, 100% { opacity: .35; } 50% { opacity: 1; } }
.flow-think-body {
  min-width: 0; flex: 1;
  font-family: var(--font-sans);
  font-size: 12.5px; line-height: 1.6;
  color: var(--text-muted);
  white-space: pre-wrap; overflow-wrap: anywhere;
  max-height: 220px; overflow: auto;
}

/* 动作/返回行：一行摘要（工具名 + 参数/结果），点开看该事件全文 */
.flow-row {
  width: 100%;
  display: flex;
  align-items: baseline;
  gap: 7px;
  padding: 3px 0;
  border: none;
  background: none;
  cursor: pointer;
  font-family: var(--font-sans);
  font-size: 12px;
  text-align: left;
  color: var(--text-muted);
}
.flow-row:hover { color: var(--text); }
.flow-glyph { flex: 0 0 13px; color: var(--text-faint); font-size: 12px; }
.flow-title { flex: 0 0 auto; color: var(--text); font-weight: 650; }
.flow-summary {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted);
}
/* 返回行缩进挂在对应动作行下，读作「这次调用的结果」 */
.flow-row.result { padding-left: 20px; color: var(--text-faint); font-size: 11.5px; }
.flow-row.result .flow-summary { color: var(--text-faint); }
.flow-time { flex: 0 0 auto; color: var(--text-faint); font-size: 10.5px; font-family: var(--font-mono); }
.flow-detail {
  margin: 0 0 4px 20px;
  padding: 3px 0 3px 8px;
  border-left: 2px solid var(--border);
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-muted);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 240px;
  overflow: auto;
}

/* 文件引用 chips */
.refs { display: flex; flex-wrap: wrap; gap: 6px; }
.ref-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 320px;
  border: var(--edge-w-strong) solid var(--edge-ink);
  border-radius: var(--r-sm);
  background: var(--bg-raised);
  padding: 3px 9px;
  font-family: var(--font-mono);
  font-size: 12px;
  /* 填色令牌不能当文字色：var(--accent) 压在 --bg-raised 上 paper 皮肤实测 2.71:1。
     仓库约定是纸底强调字走 --accent-text（graphite 下同样已按 4.5:1 解算）。 */
  color: var(--accent-text);
  cursor: pointer;
  transition: background 130ms, color 130ms, border-color 130ms;
}
.ref-chip:hover { background: var(--accent); color: var(--accent-ink); border-color: var(--border-strong); }
.ref-more { font-weight: 700; letter-spacing: .02em; }
.ref-path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ref-line { color: var(--text-muted); font-family: var(--font-mono); }
.ref-chip:hover .ref-line { color: var(--accent-ink); }

.notice {
  font-family: var(--font-sans);
  font-size: 12px;
  color: var(--text-muted);
  background: var(--bg-hover);
  border: 2px solid var(--border);
  padding: 6px 10px;
}

.chev { color: var(--text-faint); transition: transform .15s; }
.chev.open { transform: rotate(180deg); }
</style>
