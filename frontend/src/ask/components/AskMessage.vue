<script setup lang="ts">
// 单条对话气泡：用户消息（含图片）/ 助手消息。
// 助手消息支持 SSE 流式渲染：深度思考折叠块、ReAct 轨迹（分析/动作/观察/复核）、
// Markdown 正文、文件行号引用 chips（点击打开只读源码弹窗）。
import { computed, defineAsyncComponent, onBeforeUnmount, ref, watch } from 'vue'
import { mdToHtml, extractFileRefs } from '../../workbench/markdown'
import { prefs } from '../askPrefs'
// 工作流卡片 104KB，只有触发工作流的消息才渲染：按需加载，问答首屏不背
const WorkflowCard = defineAsyncComponent(() => import('../../workbench/components/WorkflowCard.vue'))
import ArtifactCards from '../../workbench/components/ArtifactCards.vue'
import type { ChatMsg, ChatTraceItem, ChatArtifact } from '../../workbench/composables/chat-types'

const props = defineProps<{ msg: ChatMsg }>()
const emit = defineEmits<{
  (e: 'openFile', path: string, line: number): void
  (e: 'wfGate', open: boolean): void
  (e: 'wfActivity'): void
  (e: 'ask', value: string): void
}>()
// 选项框一经点选即锁定：后端把选择当"下一轮用户消息"处理，同一条 ask 重复点会
// 触发多次发送，所以本地记一次 answered、点后续按钮不再响应（自由文本另说）。
const askAnswered = ref(false)
function chooseAsk(value: string) {
  if (askAnswered.value) return
  askAnswered.value = true
  emit('ask', value)
}

const html = computed(() => mdToHtml(props.msg.text || ''))
const refs = computed(() => extractFileRefs(props.msg.text || ''))
const streaming = computed(() => props.msg.status === 'streaming')

// 展示偏好直接读 askPrefs 里那份共享 reactive —— 不再自己 ref(loadAskPrefs()) + 挂
// ASK_PREFS_EVENT 监听器。同一件事两套机制，漏挂或时序差就是「设置改了界面不跟」。

// 文中引用是**回答文本里提取的位置**，不是后端核验过的证据；默认折叠，
// 展开后先给偏好上限，再允许查看全部（截断必须数得出来、点得开）。
const refsOpen = ref(false)
const refsAll = ref(false)
const refsLimited = computed(() => prefs.maxRefs > 0 && refs.value.length > prefs.maxRefs)
const shownRefs = computed(() => {
  if (!refsOpen.value) return []
  return refsLimited.value && !refsAll.value ? refs.value.slice(0, prefs.maxRefs) : refs.value
})
const hiddenRefs = computed(() => refs.value.length - shownRefs.value.length)
function refName(path: string): string {
  return path.split('/').pop() || path
}
function refDir(path: string): string {
  const parts = path.split('/')
  return parts.length > 1 ? parts.slice(0, -1).join('/') : ''
}

const copyState = ref<'idle' | 'copied' | 'failed'>('idle')
let copyTimer = 0
async function copyAnswer() {
  if (!props.msg.text || streaming.value) return
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
    await navigator.clipboard.writeText(props.msg.text)
    copyState.value = 'copied'
  } catch {
    copyState.value = 'failed'
  }
  window.clearTimeout(copyTimer)
  copyTimer = window.setTimeout(() => { copyState.value = 'idle' }, 2200)
}
onBeforeUnmount(() => window.clearTimeout(copyTimer))

/** 点开一个文件产物：走问答页既有的「打开文件」通道。 */
function openArtifact(a: ChatArtifact) {
  if (!a?.path) return
  emit('openFile', a.path, 1)
}

// ---------------- 交错流（思考 ↔ 工具调用按发生顺序） ----------------
// 2026-10-14 重构：不再是「思考整块在上 + 过程流整块在下」的两段式，而是
// think → 调用 → 返回 → think → … 的一条故事线，答案之前全部可见、无边框。
// reasoning 的轮次切分发生在 useChatStream（动作事件即切点，思考随帧实时
// 生长）；历史回放/演示等绕开管线的路径没有 flow，由 flowOf 兜底按 trace
// 顺序合成（reasoning 只剩整块，当第一个 think——切点没留下来，不假装知道）。
import { traceSegments } from '../../workbench/composables/traceDigest'
import { getMessagePresentation } from '../messagePresentation'
// 按「发言 → 该段工具摘要」切段（与工作台 ChatDock 同一份实现）。
// 本轮只保留一个外层「执行过程」折叠；段内工具行仍可单独展开看全文。
const segments = computed(() => traceSegments(props.msg))
const processOpen = ref(false)
const hasProcess = computed(() =>
  segments.value.some((s) => s.narration.length || (prefs.showTrace && s.digest.toolTotal)),
)
const processGlyph = computed(() =>
  segments.value.some((s) => s.glyph === '✎') ? '✎' : '◷',
)
// 摘要聚合各段的计数（标签顺序按首次出现）；不是“成功次数”，只表示发生过什么。
const processSummary = computed(() => {
  if (!prefs.showTrace) return '查看思考过程'
  const counts = new Map<string, { count: number; unit: string }>()
  for (const seg of segments.value) {
    for (const part of seg.digest.parts) {
      const cur = counts.get(part.label) || { count: 0, unit: part.unit }
      cur.count += part.count
      counts.set(part.label, cur)
    }
  }
  const text = [...counts.entries()]
    .map(([label, v]) => `${label} ${v.count} ${v.unit}`)
    .join(' · ')
  return text || '思考过程'
})
const presentation = computed(() => getMessagePresentation(props.msg, askAnswered.value))
const statusLabel = computed(() =>
  streaming.value && !props.msg.text ? phaseLabel.value : presentation.value.label,
)
const qualityNote = computed(() => {
  const m = props.msg
  if (m.status === 'error') {
    return { label: '失败', text: m.error || '请求失败', tone: 'danger' }
  }
  if (m.status !== 'done') return null
  if (m.degraded) {
    return { label: '未完成', text: '本轮没有拿到可综合的完整答案，上面是兜底内容或中间证据。', tone: 'warning' }
  }
  if (m.consistencyReason === 'verbatim_dump') {
    return { label: '未综合结论', text: '回答以原始源码罗列为主，缺少针对问题的归纳结论。', tone: 'warning' }
  }
  if (m.consistency === 'warn') {
    return { label: '需核对', text: `摘要对过程证据的覆盖率约 ${Math.round((m.consistencyRatio || 0) * 100)}%，可能有细节未纳入。`, tone: 'warning' }
  }
  return null
})
function rowKind(ti: number): 'action' | 'result' {
  return props.msg.trace[ti]?.type === 'action' ? 'action' : 'result'
}
// 流式中最后一次动作/返回（自动摊开规则用）
const lastRowTrace = computed(() => {
  for (let i = props.msg.trace.length - 1; i >= 0; i--) {
    const t = props.msg.trace[i]?.type
    if (t === 'action' || t === 'observation') return i
  }
  return -1
})

// 动作/返回行的全文展开：手动展开与手动收起各记一个集合——显式操作必须能
// 盖过「流式中最新一条自动摊开」的自动规则（只记展开的单一集合会让用户点
// 一条想收起来时点了没反应）。按 trace 下标记，跨段也唯一。
const openRows = ref<Set<number>>(new Set())
const mutedRows = ref<Set<number>>(new Set())
function rowOpen(i: number) {
  if (openRows.value.has(i)) return true
  if (mutedRows.value.has(i)) return false
  return prefs.autoExpandTrace && streaming.value && i === lastRowTrace.value
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
  <div class="msg" :class="msg.role" :data-message-id="msg.id">
    <div v-if="msg.role === 'user'" class="user-row">
      <div class="user-bubble">
        <div v-if="msg.images?.length" class="user-imgs">
          <img v-for="(src, i) in msg.images" :key="i" :src="src" alt="附件图片">
        </div>
        <div v-if="msg.text" class="user-text">{{ msg.text }}</div>
      </div>
    </div>

    <div v-else class="ai-row">
      <div class="ai-head">
        <span class="ai-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 6c-3-2-6-2-9-1v14c3-1 6-1 9 1 3-2 6-2 9-1V5c-3-1-6-1-9 1Z" />
            <path d="M12 6v14" />
          </svg>
        </span>
        <span class="ai-name">DocMind</span>
        <span class="ai-status" :class="`tone-${presentation.tone}`" role="status">{{ statusLabel }}</span>
        <span v-if="streaming" class="live-dots" aria-hidden="true"><i /><i /><i /></span>
        <span v-if="elapsedLabel" class="ai-time">{{ elapsedLabel }}</span>
      </div>
      <div class="ai-col">
        <!-- 执行过程：整轮只保留这一层折叠。展开后按原 segment 顺序阅读，
             单步工具行仍可各自展开；默认关闭，正文优先。 -->
        <div v-if="hasProcess" class="process">
          <button
            class="process-toggle"
            type="button"
            :aria-expanded="processOpen"
            :aria-controls="`process-${msg.id}`"
            :aria-label="`${processOpen ? '收起' : '展开'}执行过程：${processSummary}`"
            @click="processOpen = !processOpen"
          >
            <span class="process-glyph" aria-hidden="true">{{ processGlyph }}</span>
            <span class="process-label">{{ processOpen ? '收起执行过程' : '执行过程' }}</span>
            <span class="process-summary">{{ processSummary }}</span>
            <span class="chev" :class="{ open: processOpen }">⌄</span>
          </button>
          <div v-if="processOpen" :id="`process-${msg.id}`" class="process-body">
            <template v-for="(seg, si) in segments" :key="'seg' + si">
              <div v-for="(line, li) in seg.narration" :key="'n' + li" class="narration" :class="line.kind">
                <span v-if="line.kind === 'reasoning'" class="narration-tag">深度思考</span>
                <span class="narration-text">{{ line.text }}</span>
              </div>
              <template v-if="prefs.showTrace">
                <template v-for="(ti, ri) in seg.rows" :key="'r' + ri">
                  <button class="flow-row" :class="rowKind(ti)" type="button" @click="toggleRow(ti)">
                    <span class="flow-glyph" aria-hidden="true">{{ rowKind(ti) === 'action' ? '↗' : '↳' }}</span>
                    <span class="flow-title">{{ stepTitle(msg.trace[ti]) }}</span>
                    <span v-if="!rowOpen(ti)" class="flow-summary">{{ stepSummary(msg.trace[ti]) }}</span>
                    <span v-if="fmtElapsed(msg.trace[ti].elapsedMs)" class="flow-time">{{ fmtElapsed(msg.trace[ti].elapsedMs) }}</span>
                    <span class="chev" :class="{ open: rowOpen(ti) }">⌄</span>
                  </button>
                  <pre v-if="rowOpen(ti)" class="flow-detail">{{ msg.trace[ti].text.trim() }}</pre>
                </template>
              </template>
            </template>
            <p v-if="!segments.length" class="process-empty">这一轮没有可展开的过程记录。</p>
          </div>
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

        <!-- 正文常驻展开：过程收起时也不影响阅读；状态用一行说明，不压过答案。 -->
        <div class="ai-bubble">
          <div v-if="qualityNote" class="quality-note" :class="qualityNote.tone" role="status">
            <span class="quality-badge">{{ qualityNote.label }}</span>
            <span>{{ qualityNote.text }}</span>
          </div>
          <div v-if="msg.text" class="answer" :class="{ streaming }">
            <span v-html="html"></span>
          </div>
          <!-- 产物：本轮模型写下的文件、截到的图。直接摆在回答下面，
               不用再去路径里自己搜。图片给缩略图，文件给可点的名字。 -->
          <ArtifactCards :artifacts="msg.artifacts" :open="openArtifact" />
        </div>

        <!-- P0 结构化「提问 / 授权」出口：拿不准 / 歧义写 / 安全门拦下时弹可点选项框 -->
        <div v-if="msg.ask && !streaming" class="ask-box" :class="msg.ask.kind === 'permission' ? 'is-perm' : 'is-clarify'">
          <div class="ask-head">
            <span class="ask-badge">{{ msg.ask.kind === 'permission' ? '请求授权' : '请你确认' }}</span>
            <span class="ask-q">{{ msg.ask.question }}</span>
          </div>
          <div class="ask-opts">
            <button
              v-for="(o, i) in msg.ask.options" :key="i"
              type="button" class="ask-opt" :disabled="askAnswered"
              @click="chooseAsk(o.value)"
            ><span class="ask-opt-arrow" aria-hidden="true">→</span>{{ o.label }}</button>
          </div>
          <div v-if="askAnswered" class="ask-done">已选择，正在按你的答复继续…</div>
          <div v-else-if="msg.ask.allow_free_text" class="ask-freetext">
            也可以忽略上面的选项，直接在下方输入框补充你的答案。
          </div>
        </div>

        <!-- 文中引用：默认折叠；展开后按偏好显示前 N 条，可再看全部。 -->
        <div v-if="refs.length" class="refs">
          <button
            class="refs-toggle"
            type="button"
            :aria-expanded="refsOpen"
            :aria-controls="`refs-${msg.id}`"
            @click="refsOpen = !refsOpen"
          >
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6Zm0 0v6h6" />
            </svg>
            <span class="refs-label">文中引用</span>
            <span class="refs-count">{{ refs.length }}</span>
            <span class="chev" :class="{ open: refsOpen }">⌄</span>
          </button>
          <div v-if="refsOpen" :id="`refs-${msg.id}`" class="refs-list">
            <button
              v-for="(r, i) in shownRefs" :key="i"
              class="ref-item"
              :title="r.path + (r.line ? ':' + r.line : '')"
              @click="emit('openFile', r.path, r.line)"
            >
              <span class="ref-file">{{ refName(r.path) }}<span v-if="r.line" class="ref-line">:{{ r.line }}</span></span>
              <span v-if="refDir(r.path)" class="ref-dir">{{ refDir(r.path) }}</span>
            </button>
            <button v-if="hiddenRefs > 0" type="button" class="refs-more" @click="refsAll = true">
              显示全部 {{ refs.length }} 处
            </button>
            <button v-else-if="refsLimited" type="button" class="refs-more" @click="refsAll = false">
              只看前 {{ prefs.maxRefs }} 处
            </button>
          </div>
        </div>

        <div v-if="msg.text && !streaming" class="answer-actions">
          <button class="action-btn" type="button" @click="copyAnswer">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <rect x="9" y="9" width="11" height="12" rx="2" />
              <path d="M5 15V5a2 2 0 0 1 2-2h8" />
            </svg>
            复制回答
          </button>
          <span class="action-feedback" role="status" aria-live="polite">
            <template v-if="copyState === 'copied'">已复制到剪贴板</template>
            <template v-else-if="copyState === 'failed'">复制失败，请选择文本复制</template>
          </span>
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
/* 阅读优先：用户问题轻量靠右，助手回答占满整栏，不再让头像列吃掉正文宽度。 */
.msg { min-width: 0; }
.user-row { display: flex; justify-content: flex-end; animation: msg-in .18s ease both; }
.user-bubble {
  max-width: min(640px, 88%);
  padding: 12px 16px;
  background: var(--bg-hover);
  color: var(--text);
  border-radius: var(--r-lg);
}
.user-text { white-space: pre-wrap; word-break: break-word; font-family: var(--font-ui); font-size: var(--fs-h2); line-height: 1.75; }
.user-imgs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.user-imgs img { max-width: 180px; max-height: 180px; border-radius: var(--r-sm); }

.ai-row { min-width: 0; animation: msg-in .18s ease both; }
.ai-head { display: flex; align-items: center; gap: 8px; min-height: 24px; margin-bottom: 10px; }
.ai-mark {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: var(--r-sm);
  background: var(--accent-soft);
  color: var(--accent-text);
}
.ai-name { font-size: var(--fs-md); font-weight: 600; color: var(--text); }
.ai-time { margin-left: auto; font-family: var(--font-mono); font-size: var(--fs-cap); color: var(--text-faint); }

@keyframes msg-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) {
  .user-row, .ai-row { animation: none !important; }
  .live-dots i { animation: none !important; opacity: .6; }
  .answer.streaming::after { animation: none !important; opacity: .6; }
}

.ai-col { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 12px; }
.ai-bubble { background: transparent; border: 0; padding: 0; width: 100%; max-width: 100%; }

/* 回答正文是主视觉：16px / 1.8，标题逐级放大；不改共享 markdown 层，避免影响工作台。 */
.answer {
  word-break: break-word;
  font-family: var(--font-ui);
  font-size: var(--fs-h2);
  line-height: 1.8;
  letter-spacing: 0;
  color: var(--text);
}
.answer :deep(.md-p) { margin: 0 0 12px; }
.answer :deep(.md-p:last-child) { margin-bottom: 0; }
.answer :deep(.md-h) { font-weight: 600; line-height: 1.35; color: var(--ink); }
.answer :deep(.md-h1) { margin: 28px 0 12px; font-size: 24px; }
.answer :deep(.md-h2) { margin: 26px 0 10px; font-size: 20px; }
.answer :deep(.md-h3), .answer :deep(.md-h4) { margin: 22px 0 8px; font-size: 17px; }
.answer :deep(.md-h:first-child) { margin-top: 0; }
.answer :deep(.md-ul) { margin: 0 0 12px; padding-left: 1.4em; }
.answer :deep(.md-ul li) { margin: 4px 0; }
.answer :deep(.md-ic) {
  font-size: 14px;
  background: color-mix(in srgb, var(--accent) 7%, transparent);
  border-color: color-mix(in srgb, var(--accent) 12%, transparent);
  border-radius: var(--r-xs);
  padding: 1px 5px;
}
.answer :deep(.md-pre) { margin: 16px 0; border-radius: var(--r-md); }
.answer :deep(.md-pre code) { font-size: 13px; line-height: 1.7; }
.answer :deep(.md-link) { color: var(--accent-text); border-bottom: 1px solid color-mix(in srgb, var(--accent) 35%, transparent); }
.answer :deep(.md-link:hover) { border-bottom-color: var(--accent-text); }

.ai-status { font-size: var(--fs-sm); color: var(--text-muted); }
.ai-status.tone-accent { color: var(--accent-text); }
.ai-status.tone-warning { color: var(--amber); }
.ai-status.tone-danger { color: var(--danger); }
.live-dots { display: inline-flex; gap: 3px; }
.live-dots i { width: 4px; height: 4px; border-radius: 50%; background: var(--accent-text); animation: live-blink 1.2s ease-in-out infinite; }
.live-dots i:nth-child(2) { animation-delay: .15s; }
.live-dots i:nth-child(3) { animation-delay: .3s; }
@keyframes live-blink { 0%, 100% { opacity: .2; } 50% { opacity: 1; } }

/* 流式正文末端光标 */
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

.quality-note {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 0 0 14px;
  padding: 8px 10px;
  border-left: 2px solid var(--border-strong);
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
  background: var(--bg-raised);
  color: var(--text-muted);
  font-size: var(--fs-md);
  line-height: 1.6;
}
.quality-note.warning { border-left-color: var(--amber); }
.quality-note.danger { border-left-color: var(--danger); color: var(--danger); }
.quality-badge { flex: none; font-weight: 600; color: var(--text); }
.quality-note.warning .quality-badge { color: var(--amber); }
.quality-note.danger .quality-badge { color: var(--danger); }

/* 执行过程：整轮唯一的折叠层；展开后按时间顺序阅读，单步仍可各自展开。 */
.process { border: 1px solid var(--border); border-radius: var(--r-md); background: var(--bg-raised); overflow: hidden; }
.process-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 40px;
  padding: 8px 12px;
  border: 0;
  background: none;
  color: var(--text-muted);
  font-family: var(--font-ui);
  font-size: var(--fs-md);
  text-align: left;
  cursor: pointer;
  transition: background 150ms;
}
.process-toggle:hover { background: var(--bg-hover); }
/* hover 底色比 surface 深一档，弱化文字必须同步往 ink 收，否则纸面皮肤跌到 4.34:1。 */
.process-toggle:hover .process-summary,
.process-toggle:hover .chev,
.refs-toggle:hover .chev,
.flow-row:hover .flow-summary,
.flow-row:hover .flow-time,
.flow-row:hover .chev,
.ref-item:hover .ref-dir,
.ref-item:hover .ref-line {
  color: color-mix(in srgb, var(--ink) 72%, var(--bg-hover));
}
.process-glyph { flex: none; color: var(--text-faint); }
.process-label { flex: none; font-weight: 500; color: var(--text); }
.process-summary { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-sm); }
.process-body { max-height: 320px; overflow-y: auto; padding: 2px 12px 10px; border-top: 1px solid var(--border); }
.process-empty { margin: 10px 0; color: var(--text-muted); font-size: var(--fs-sm); }

.narration { padding: 8px 0; font-size: var(--fs-md); line-height: 1.7; color: var(--text-muted); white-space: pre-wrap; word-break: break-word; }
.narration.reasoning {
  margin: 6px 0;
  padding: 8px 10px;
  border-left: 2px solid var(--border-strong);
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
  background: var(--surface);
  color: var(--text);
}
.narration-tag { display: block; margin-bottom: 2px; color: var(--accent-text); font-size: var(--fs-cap); font-weight: 600; }

.flow-row {
  display: flex;
  align-items: baseline;
  gap: 7px;
  width: 100%;
  padding: 6px 0;
  border: 0;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
  background: none;
  color: var(--text-muted);
  font-family: var(--font-ui);
  font-size: var(--fs-md);
  text-align: left;
  cursor: pointer;
}
.flow-row:hover { color: var(--text); }
.flow-glyph { flex: 0 0 13px; color: var(--text-faint); }
.flow-title { flex: none; color: var(--text); font-weight: 600; }
.flow-summary { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.flow-row.result { padding-left: 20px; color: var(--text-muted); }
.flow-row.result .flow-summary { color: var(--text-muted); }
.flow-time { flex: none; color: var(--text-faint); font-size: var(--fs-cap); font-family: var(--font-mono); }
.flow-detail {
  margin: 0 0 6px 20px;
  padding: 6px 0 6px 10px;
  border-left: 2px solid var(--border);
  font-family: var(--font-mono);
  font-size: var(--fs-cap);
  line-height: 1.65;
  color: var(--text-muted);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 240px;
  overflow: auto;
}
@media (max-width: 600px) {
  .process-toggle { align-items: flex-start; }
  .process-summary {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    white-space: normal;
    line-height: 1.5;
  }
}

/* 文中引用：折叠摘要 + 可展开的文件列表 */
.refs { border-top: 1px solid var(--border); padding-top: 4px; }
.refs-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 40px;
  padding: 6px 2px;
  border: 0;
  background: none;
  color: var(--text-muted);
  font-family: var(--font-ui);
  font-size: var(--fs-md);
  text-align: left;
  cursor: pointer;
}
.refs-toggle:hover { color: var(--text); }
.refs-label { font-weight: 500; color: var(--text); }
.refs-count {
  min-width: 20px;
  padding: 0 6px;
  border-radius: var(--r-full);
  background: var(--bg-hover);
  color: color-mix(in srgb, var(--ink) 72%, var(--bg-hover));
  font-size: var(--fs-cap);
  text-align: center;
}
.refs-toggle .chev { margin-left: auto; }
.refs-list { display: flex; flex-direction: column; padding-bottom: 4px; }
.ref-item {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-height: 44px;
  padding: 8px 4px;
  border: 0;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
  background: none;
  color: var(--text);
  font-family: var(--font-ui);
  font-size: var(--fs-md);
  text-align: left;
  cursor: pointer;
}
.ref-item:hover { background: var(--bg-hover); }
.ref-file {
  flex: none;
  max-width: 58%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  color: var(--accent-text);
}
.ref-line { color: var(--text-muted); }
.ref-dir { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: right; color: var(--text-muted); font-size: var(--fs-sm); }
.refs-more {
  align-self: flex-start;
  min-height: 36px;
  margin-top: 4px;
  padding: 4px 8px;
  border: 0;
  border-radius: var(--r-sm);
  background: none;
  color: var(--accent-text);
  font-family: var(--font-ui);
  font-size: var(--fs-sm);
  cursor: pointer;
}
.refs-more:hover { background: var(--bg-hover); }

.answer-actions { display: flex; align-items: center; gap: 10px; min-height: 32px; }
.action-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  padding: 4px 8px;
  border: 0;
  border-radius: var(--r-sm);
  background: none;
  color: var(--text-muted);
  font-family: var(--font-ui);
  font-size: var(--fs-sm);
  cursor: pointer;
}
.action-btn:hover { background: var(--bg-hover); color: var(--text); }
.action-feedback { min-height: 18px; color: var(--text-muted); font-size: var(--fs-sm); }

/* ===== P0 提问 / 授权 选项框 ===== */
.ask-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: var(--edge-w-strong) solid var(--edge-ink);
  border-radius: var(--r-sm);
  background: var(--bg-raised);
  box-shadow: var(--sh-hard-sm);
  padding: 10px 12px;
}
.ask-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ask-badge {
  font-family: var(--font-mono);
  font-size: var(--fs-cap);
  font-weight: 700;
  letter-spacing: .03em;
  padding: 1px 7px;
  border: 1px solid var(--edge-ink);
  border-radius: 4px;
}
.ask-box.is-clarify .ask-badge { color: var(--accent-text); background: var(--bg-hover); }
.ask-box.is-perm .ask-badge { color: var(--amber); background: color-mix(in srgb, var(--amber) 12%, transparent); border-color: color-mix(in srgb, var(--amber) 50%, transparent); }
.ask-q { font-family: var(--font-sans); font-weight: 650; color: var(--text); }
.ask-opts { display: flex; flex-wrap: wrap; gap: 8px; }
.ask-opt {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: var(--edge-w-strong) solid var(--edge-ink);
  border-radius: var(--r-sm);
  background: var(--bg-raised);
  color: var(--text);
  padding: 6px 12px;
  font-family: var(--font-sans);
  font-size: var(--fs-md);
  cursor: pointer;
  transition: background 130ms, color 130ms, border-color 130ms, transform 90ms;
}
.ask-opt:hover:not(:disabled) { --btn-fill: var(--accent); background: var(--accent); color: var(--accent-ink); border-color: var(--border-strong); }
.ask-opt:active:not(:disabled) { transform: translateY(1px); }
.ask-opt:disabled { opacity: .45; cursor: default; }
.ask-opt-arrow { color: var(--accent-2); font-weight: 700; }
.ask-opt:hover:not(:disabled) .ask-opt-arrow { color: var(--accent-ink); }
.ask-box.is-perm .ask-opt:hover:not(:disabled) { --btn-fill: var(--amber); background: var(--amber); color: #fff; border-color: var(--amber); }
.ask-box.is-perm .ask-opt:hover:not(:disabled) .ask-opt-arrow { color: #fff; }
.ask-done { font-family: var(--font-mono); font-size: var(--fs-sm); color: var(--text-faint); }
.ask-freetext { font-family: var(--font-sans); font-size: var(--fs-sm); color: var(--text-muted); }

.notice {
  font-family: var(--font-sans);
  font-size: var(--fs-sm);
  color: var(--text-muted);
  background: var(--bg-hover);
  border: var(--edge-w-strong, 2px) solid var(--border);
  padding: 6px 10px;
}

.chev { color: var(--text-faint); transition: transform .15s; }
.chev.open { transform: rotate(180deg); }
</style>
