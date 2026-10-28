// 仅测试用：挂载真实 AskMessage，复用问答页字体/样式/皮肤。
// 通过 window.__askFixture 推进消息状态，避免复制一套假消息 HTML。
import { createApp, h, reactive } from 'vue'
import AskMessage from '../src/ask/components/AskMessage.vue'
import '../src/fonts'
import '../src/ask/ask.css'
import '../src/ask/ask-riso.css'
import '../src/ask/ask-typo.css'
import '../src/ask/ask-a11y.css'
import '../src/shared/markdown.css'
import '../src/workbench/skins.css'
import '../src/workbench/skins-seed.css'
import '../src/workbench/glass.css'
import '../src/workbench/layout-modes.css'
import '../src/ask/ask-glass.css'
import { applySkin, isSkinKey } from '../src/workbench/skins'
import { prefs } from '../src/ask/askPrefs'
import type { ChatMsg, ChatTraceItem } from '../src/workbench/composables/chat-types'

const skin = new URLSearchParams(location.search).get('skin') || 'zinc'
applySkin(isSkinKey(skin) ? skin : 'zinc')

const state = reactive<{
  messages: ChatMsg[]
  lastOpenFile: { path: string; line: number } | null
  lastAsk: string
}>({
  messages: [],
  lastOpenFile: null,
  lastAsk: '',
})
let seq = 1

const LONG_ANSWER = `这个项目由两个前端入口和后端服务组成：问答页负责检索与对话，代码工作台负责浏览、编辑与验证。

## 请求如何进入模型

先用 \`session.js\` 取得当前标签页的会话 id，再通过 \`/api/chat\` 建立流式连接。服务端把检索结果、工具调用与模型输出拆成事件逐条下发。

- 会话按页面域隔离，问答页与工作台不会串话。
- 工具执行结果以 observation 事件回传，前端据此展示执行过程。
- 最终回答以 final 事件收束，done 表示传输结束。

## 需要注意的边界

1. 刷新页面时先从 \`/api/turn/active\` 恢复未结束的回合，避免同一条问题被重复执行。
2. 上下文接近窗口上限时会触发压缩，历史对话被摘要替代。
3. 写得再完整，也要保留"未验证"的诚实标记，不能把中间证据当成结论。

\`\`\`ts
export async function askGrounded(question: string, handlers: SseStreamHandlers) {
  const form = new FormData()
  form.append('question', question)
  form.append('session_id', getSessionId())
  return postSse('/api/chat', { method: 'POST', body: form }, handlers)
}
\`\`\`

相关实现位于 frontend/src/ask/AskApp.vue:835、frontend/src/ask/components/AskMessage.vue:208、frontend/src/ask/config.ts:10、frontend/src/workbench/config.ts:20、frontend/src/workbench/markdown.ts:40、agent_runtime/context_router.py:88、api_routes/projects.py:21 与 docs/ask-conversation-redesign-handoff.md:1。阅读时可以按"入口 → 会话 → 流式管线 → 消息渲染"的顺序推进。`

function message(role: ChatMsg['role'], text: string, extra: Partial<ChatMsg> = {}): ChatMsg {
  return {
    id: seq++,
    role,
    text,
    status: 'done',
    trace: [],
    reasoning: '',
    notices: [],
    plan: [],
    ...extra,
  }
}

function longAnswer(): ChatMsg[] {
  return [
    message('user', '这个项目的问答请求是怎么走到模型并返回的？请结合入口、会话和流式渲染说明。'),
    message('assistant', LONG_ANSWER, {
      startedAt: Date.now() - 24_000,
      finishedAt: Date.now(),
      trace: [
        { type: 'thought', text: '先从入口文件和会话脚本开始，确认请求链路，再看流式渲染。' },
        { type: 'action', text: 'read_file(frontend/src/ask/AskApp.vue)' },
        { type: 'observation', text: '读取到 1200 行，包含发送、恢复历史和滚动逻辑。', ok: true, elapsedMs: 320 },
        { type: 'action', text: 'search_code("postSse")' },
        { type: 'observation', text: '命中 frontend/src/workbench/api/sse.ts。', ok: true, elapsedMs: 180 },
        { type: 'reflection', text: '链路已确认：session.js → AskApp.send → postSse → useChatStream。' },
      ],
      reasoning: '需要区分传输层的 done 与业务层的 final，避免把断流当成正常完成。',
    }),
    message('user', '如果流式连接提前断开，界面会怎么提示？'),
    message('assistant', `连接提前断开时，已经收到的内容会保留，消息上显示失败说明，而不是伪装成完整回答。\n\n- 用户主动停止：显示"已停止"。\n- 服务在完成前断开：显示错误条，并保留部分正文。\n- 正常结束：必须收到 done 事件。`, {
      startedAt: Date.now() - 5000,
      finishedAt: Date.now(),
    }),
  ]
}

const CASES: Record<string, () => ChatMsg[]> = {
  'long-answer': longAnswer,
  running: () => [
    message('user', '梳理当前项目的目录结构、主要模块和入口文件。'),
    message('assistant', '', { status: 'streaming', startedAt: Date.now() }),
  ],
  'reason-only': () => [
    message('user', '先想一想这个项目的风险在哪里。'),
    message('assistant', '较大的风险是前后端协议分散在多个入口，修改时需要同时检查客户端与服务端。', { reasoning: '先定位入口，再检查错误处理与恢复逻辑。' }),
  ],
  history: () => [
    message('user', '解释一下历史回放。'),
    message('assistant', '历史回放由 trace 重建，缺少流式切点时按顺序合成。', {
      trace: [
        { type: 'reasoning', text: '先读取历史消息，再按事件顺序恢复。' },
        { type: 'thought', text: '已确认历史回放使用同一条渲染路径。' },
      ],
    }),
  ],
  failed: () => [
    message('user', '服务失败时会显示什么？'),
    message('assistant', '已收到的部分内容。', {
      status: 'error',
      error: '界面测试：服务暂不可用',
      errorKind: 'permanent',
    }),
  ],
  stopped: () => [
    message('user', '停止后还会继续跑吗？'),
    message('assistant', '', { status: 'stopped' }),
  ],
  degraded: () => [
    message('user', '给我一个完整结论。'),
    message('assistant', '目前只拿到了中间证据，还没有形成针对问题的完整结论。', { degraded: true }),
  ],
  warn: () => [
    message('user', '摘要覆盖了过程证据吗？'),
    message('assistant', '本回合的摘要对过程证据覆盖不足。', { consistency: 'warn', consistencyRatio: 0.42 }),
  ],
  verbatim: () => [
    message('user', '帮我归纳。'),
    message('assistant', '```ts\nexport const value = 1\n```\n以上是读取到的源码。', { consistencyReason: 'verbatim_dump' }),
  ],
  permission: () => [
    message('user', '帮我把这个文件改掉。'),
    message('assistant', '这次修改会写入项目文件，需要你的授权。', {
      ask: {
        kind: 'permission',
        question: '允许我修改 src/ask/AskApp.vue 吗？',
        options: [
          { label: '允许本次修改', value: '允许本次修改' },
          { label: '先只看方案', value: '先只看方案' },
        ],
        allow_free_text: true,
      },
    }),
  ],
  workflow: () => [
    message('user', '按计划执行这次改动。'),
    message('assistant', '已启动工作流，进度见下方卡片。', {
      trace: [
        { type: 'thought', text: '先确认改动范围，再启动工作流。' },
        { type: 'action', text: 'run_command("git status")' },
        { type: 'observation', text: '工作区干净。', ok: true, elapsedMs: 90 },
      ],
      workflow: { workflowId: 'wf-fixture-1', seed: { status: 'planned', phase: '计划已生成', kind: 'generic', request: '界面夹具工作流' } },
    }),
  ],
}

function current() {
  return state.messages[state.messages.length - 1]
}

const fixture = {
  setCase(name: string) {
    const make = CASES[name]
    if (!make) throw new Error(`unknown fixture case: ${name}`)
    state.messages = make()
    state.lastOpenFile = null
    state.lastAsk = ''
    return state.messages.map((m) => m.id)
  },
  appendEvent(ev: Record<string, unknown>) {
    const m = current()
    if (!m) return
    const type = String(ev.type || '')
    if (type === 'reasoning') {
      m.reasoning += String(ev.text || '')
      m.flow = [
        ...(m.flow || []),
        { kind: 'think' as const, text: String(ev.text || '') },
      ]
      return
    }
    if (type === 'token') {
      m.text += String(ev.text || '')
      return
    }
    if (type === 'final') {
      m.text = String(ev.text || m.text)
      m.status = 'done'
      m.finishedAt = Date.now()
      if (ev.degraded === true) m.degraded = true
      if (typeof ev.consistency === 'string') m.consistency = ev.consistency
      if (typeof ev.consistency_ratio === 'number') m.consistencyRatio = ev.consistency_ratio
      if (typeof ev.consistency_reason === 'string') m.consistencyReason = ev.consistency_reason
      if (ev.ask) m.ask = ev.ask as ChatMsg['ask']
      return
    }
    if (type === 'done') {
      m.status = 'done'
      m.finishedAt = Date.now()
      return
    }
    if (type === 'thought' || type === 'action' || type === 'observation' || type === 'reflection') {
      const item: ChatTraceItem = {
        type: type as ChatTraceItem['type'],
        text: String(ev.text || ''),
        at: Date.now(),
        ok: typeof ev.ok === 'boolean' ? ev.ok : undefined,
      }
      const idx = m.trace.length
      m.trace.push(item)
      m.flow = [
        ...(m.flow || []),
        { kind: type === 'action' ? ('action' as const) : type === 'observation' ? ('result' as const) : ('thought' as const), trace: idx },
      ]
      if (type === 'observation' && typeof ev.elapsed_ms === 'number') {
        for (let i = m.trace.length - 2; i >= 0; i--) {
          if (m.trace[i].type === 'action') { m.trace[i].elapsedMs = Number(ev.elapsed_ms); break }
        }
      }
    }
  },
  setText(text: string) {
    const m = current()
    if (m) m.text = text
  },
  finish(opts: Partial<ChatMsg> = {}) {
    const m = current()
    if (!m) return
    Object.assign(m, { status: 'done', finishedAt: Date.now() }, opts)
  },
  setPrefs(next: Partial<typeof prefs>) {
    Object.assign(prefs, next)
  },
  setSkin(key: string) {
    applySkin(isSkinKey(key) ? key : 'zinc')
  },
  state,
}

declare global {
  interface Window { __askFixture: typeof fixture }
}
window.__askFixture = fixture
fixture.setCase('long-answer')

const App = {
  setup() {
    return () => h('div', { class: 'ask-main' }, [
      h('div', { class: 'chat-scroll' }, [
        h('div', { class: 'chat-col' }, state.messages.map((m) => h(AskMessage, {
          key: m.id,
          msg: m,
          onOpenFile: (path: string, line: number) => { state.lastOpenFile = { path, line } },
          onAsk: (value: string) => { state.lastAsk = value },
          onWfGate: () => {},
          onWfActivity: () => {},
        }))),
      ]),
    ])
  },
}

createApp(App).mount('#app')
