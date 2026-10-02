<script setup lang="ts">
// AI 运行台：把 harness 后端能力（成本预算 / 会话 / 逐轮轨迹 / 技能与钩子）
// 做成普通人看得懂的面板。静态预览（无后端）时自动使用演示数据。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  harnessApi, setSessionId, getSessionId, startNewSession, fmtTraceError,
  type BudgetStatus, type BudgetCheck, type SessionInfo,
  type TraceItem, type TraceSummary, type SkillInfo,
  agentApi, type LangSmithStatus,
} from '../api'
import {
  demoMode,
  demoBudget, demoSessions, demoTraceItems, demoTraceSummary, demoSkills, demoHooks,
} from '../composables/demo'
import { useWorkbench } from '../composables/workbench'
import { appEvents } from '../eventBus'
import { costLabel } from '../composables/costLabel'

const { openFlow } = useWorkbench()

const open = ref(false)
const tab = ref<'cost' | 'sessions' | 'trace' | 'skills'>('cost')
const loading = ref(false)
const error = ref('')

const budget = ref<BudgetStatus | null>(null)
const budgetCheck = ref<BudgetCheck | null>(null)
const sessions = ref<SessionInfo[]>([])
/** 当前标签页的会话 id：用于在列表里高亮「当前会话」。 */
const currentId = ref(getSessionId())
const traces = ref<TraceItem[]>([])
const traceSummary = ref<TraceSummary | null>(null)
const skills = ref<SkillInfo[]>([])
const skillsDir = ref('')
const hooksCounts = ref<Record<string, number>>({ pre_tool: 0, post_tool: 0, pre_turn: 0, post_turn: 0 })
const hooksWorkflowCounts = ref<Record<string, number>>({})
const hookKinds = ref<string[]>([])
const hookBreakpoints = ref<Record<string, { enabled?: boolean; block?: boolean; match?: string; reason?: string }>>({})
const breakpointKind = ref('before_tool')
const breakpointMatch = ref('')
const breakpointReason = ref('')
const breakpointBlock = ref(true)
const hooksTotal = computed(() => Object.values(hooksCounts.value).reduce((a, b) => a + b, 0))

const limitInput = ref('')
const actionMsg = ref('')
const langsmith = ref<LangSmithStatus | null>(null)

const TABS = [
  { key: 'cost', label: '花费' },
  { key: 'sessions', label: '对话' },
  { key: 'trace', label: '操作记录' },
  { key: 'skills', label: '技能与扩展' },
] as const

/**
 * 每轮的花费。0 元时带上「这个 0 是什么意思」——免费 / 本地 / **未配置单价**
 * 三者数字一样但含义完全不同，缺了这层用户会以为系统在乱算账。
 * 逐轮 trace 里没有 pricing_state 字段（那是按 provider 聚合的），
 * 所以这里退到纯金额，由设置页的供应商表承担解释。
 */
function money(v: number | null | undefined): string {
  return costLabel(v)
}
function ago(iso: string): string {
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return iso
  const d = Date.now() - t
  const m = Math.floor(d / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  return new Date(iso).toLocaleDateString('zh-CN')
}
function fmtTime(iso: string): string {
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return iso
  return t.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
function fmtMs(ms: number): string {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}
function outcomeTag(item: TraceItem): { text: string; cls: string } {
  if (item.aborted) return { text: '已停止', cls: 'hp-tag-stop' }
  if (item.error) return { text: '出错', cls: 'hp-tag-err' }
  if (item.outcome === 'completed') return { text: '完成', cls: 'hp-tag-ok' }
  return { text: item.outcome || '完成', cls: 'hp-tag-ok' }
}
function toolNames(item: TraceItem): string[] {
  return (item.steps || []).map((s) => s.action)
}
function providerLabel(p: string): string {
  if (!p) return '测试/离线'
  if (p === '?') return '未知'
  return p
}
function langsmithText(): string {
  if (!langsmith.value) return '未读取'
  if (langsmith.value.enabled) {
    const pending = Number(langsmith.value.pending_exports || 0)
    return `已启用 · ${langsmith.value.project || 'default'}${pending ? ` · 待发送 ${pending}` : ''}`
  }
  if (!langsmith.value.installed) return 'SDK 未安装'
  if (!langsmith.value.key_configured) return '未配置密钥'
  return '未启用'
}
/** 未启用时给用户的可执行指引（环境变量配置后重启服务生效）。 */
const langsmithGuide = computed(() => {
  const ls = langsmith.value
  if (!ls || ls.enabled) return ''
  if (!ls.installed) return '先在当前 Python 环境安装 langsmith 包，再配置环境变量后重启服务。'
  if (!ls.key_configured) {
    return '设置环境变量 LANGCHAIN_API_KEY=你的密钥（可选 LANGCHAIN_PROJECT 指定项目名），再把 DOCMIND_LANGSMITH=1 后重启服务。'
  }
  return '密钥已就绪：设置 DOCMIND_LANGSMITH=1（或 LANGCHAIN_TRACING_V2=true）后重启服务即可开启轨迹上报。'
})
// 项目页需要组织/工作区 id，无法本地拼出，直接给 Smith 入口
const langsmithSmithUrl = computed(() => 'https://smith.langchain.com/')
function skillHistoryText(skill: SkillInfo): string {
  return (skill.history_versions || []).map(item => item.version || '—').join('、')
}
async function loadAll() {
  loading.value = true
  error.value = ''
  currentId.value = getSessionId()   // 每次刷新列表时同步「当前会话」标记
  try {
    if (demoMode.value) {
      budget.value = demoBudget.status
      budgetCheck.value = demoBudget.check
      sessions.value = [...demoSessions]
      traces.value = [...demoTraceItems]
      traceSummary.value = demoTraceSummary as unknown as TraceSummary
      skills.value = [...demoSkills.items]
      skillsDir.value = demoSkills.skills_dir
      hooksCounts.value = { ...demoHooks.counts }
      langsmith.value = { ok: true, installed: true, key_configured: true, enabled: false, realtime: false, project: 'demo' }
      return
    }
    const [b, s, t, sk, hk, smith] = await Promise.all([
      harnessApi.budget(),
      harnessApi.sessions(),
      harnessApi.trace(30),
      harnessApi.skills(),
      harnessApi.hooks(),
      agentApi.langsmith().catch(() => null),
    ])
    budget.value = b.status
    budgetCheck.value = b.check
    sessions.value = s.items || []
    traces.value = t.items || []
    traceSummary.value = t.summary
    skills.value = sk.items || []
    skillsDir.value = sk.skills_dir
    hooksCounts.value = hk.counts || hooksCounts.value
    hooksWorkflowCounts.value = hk.workflow_counts || hooksWorkflowCounts.value
    hookKinds.value = hk.workflow_kinds || hookKinds.value
    hookBreakpoints.value = hk.breakpoints || hookBreakpoints.value
    if (smith) langsmith.value = smith
  } catch (e) {
    error.value = (e as Error).message || '数据获取失败'
  } finally {
    loading.value = false
  }
}

watch(open, (v) => { if (v) void loadAll() })

// 项目切换：会话按项目隔离，面板开着时必须重拉（否则仍显示旧项目的会话列表）。
function onProjectChanged() {
  currentId.value = getSessionId()
  if (open.value) void loadAll()
}
onMounted(() => {
  window.addEventListener('docmind:project-changed', onProjectChanged)
})
onBeforeUnmount(() => {
  window.removeEventListener('docmind:project-changed', onProjectChanged)
  open.value = false
})
async function saveLimit() {
  actionMsg.value = ''
  const v = Number(limitInput.value)
  if (!Number.isFinite(v) || v < 0) { actionMsg.value = '请输入不小于 0 的数字（0 = 不限预算）'; return }
  if (demoMode.value) { actionMsg.value = '演示模式不能修改预算'; return }
  const r = await harnessApi.setBudgetLimit(v)
  if (r.ok && r.check) { budgetCheck.value = r.check; actionMsg.value = '预算已保存' }
  else actionMsg.value = r.error || '保存失败'
  if (r.ok) void loadAll()
}
async function resetSpent() {
  if (demoMode.value) { actionMsg.value = '演示模式不能清零'; return }
  if (!window.confirm('确定把累计花费计数清零吗？（不影响模型与账本文件，只重置计数）')) return
  const r = await harnessApi.resetBudget()
  if (r.ok) actionMsg.value = '累计计数已清零'
  void loadAll()
}
async function removeSession(id: string) {
  if (demoMode.value) {
    sessions.value = sessions.value.filter((x) => x.session_id !== id)
    actionMsg.value = '演示数据已移除（刷新后恢复）'
    return
  }
  if (!window.confirm(`删除会话「${id}」的历史记录？此操作不可恢复。`)) return
  const r = await harnessApi.deleteSession(id)
  if (!r.ok) return
  sessions.value = sessions.value.filter((x) => x.session_id !== id)
  // 删掉的正是当前会话：自动切到一段全新会话并让助手刷新（否则会停在已删除的空 id 上）。
  if (id === currentId.value) {
    currentId.value = await startNewSession()
    open.value = false
    appEvents.emit('docmind:focus-chat', { reload: true })
  }
}
/** 继续某段历史对话：把该 session_id 切为当前会话并让 AI 助手回灌其历史。 */
async function continueSession(id: string) {
  if (demoMode.value) { actionMsg.value = '演示模式不能续聊'; return }
  if (!await setSessionId(id)) { actionMsg.value = '该会话正在另一个页面使用，请先关闭那个页面。'; return }
  currentId.value = id
  open.value = false
  appEvents.emit('docmind:focus-chat', { reload: true })
}
/** 开一段全新对话：切到全新 session id（新 id 无历史），让 AI 助手显示空态。 */
async function newSession() {
  if (demoMode.value) { actionMsg.value = '演示模式不能新建会话'; return }
  currentId.value = await startNewSession()
  open.value = false
  appEvents.emit('docmind:focus-chat', { reload: true })
}
async function clearTraces() {
  if (demoMode.value) { traces.value = []; actionMsg.value = '演示记录已清空（刷新后恢复）'; return }
  if (!window.confirm('清空全部操作记录（trace 账本）？')) return
  await harnessApi.clearTrace()
  void loadAll()
}
async function reloadSkills() {
  if (demoMode.value) { actionMsg.value = '演示模式无需重载'; return }
  const r = await harnessApi.reloadSkills()
  actionMsg.value = r.ok ? `已重载，发现 ${r.count ?? 0} 个技能` : '重载失败'
  void loadAll()
}
async function rollbackSkill(skill: SkillInfo) {
  if (demoMode.value) { actionMsg.value = '演示模式不能回滚技能'; return }
  if (skill.source !== 'user') return
  if (!window.confirm(`回滚技能「${skill.name}」到上一版本？`)) return
  const result = await harnessApi.rollbackSkill(skill.name)
  actionMsg.value = result.approval_required ? '回滚需要审批' : result.ok ? (result.restored_previous ? '已恢复上一版本' : '已移除该技能') : (result.error || '回滚失败')
  if (result.ok) void loadAll()
}
async function reloadHooks() {
  if (demoMode.value) { actionMsg.value = '演示模式无需重载'; return }
  const r = await harnessApi.reloadHooks()
  actionMsg.value = r.ok ? '钩子已重载' : '重载失败'
  void loadAll()
}
async function saveHookBreakpoint() {
  try {
    const r = await harnessApi.setHookBreakpoint({
      kind: breakpointKind.value, enabled: true, block: breakpointBlock.value,
      match: breakpointMatch.value, reason: breakpointReason.value,
    })
    actionMsg.value = r.ok ? `已保存断点：${breakpointKind.value}` : (r.error || '断点保存失败')
    if (r.ok) void loadAll()
  } catch (e) { actionMsg.value = (e as Error).message || '断点保存失败' }
}
async function removeHookBreakpoint(kind: string) {
  try {
    const r = await harnessApi.removeHookBreakpoint(kind)
    actionMsg.value = r.ok ? `已移除断点：${kind}` : (r.error || '断点移除失败')
    if (r.ok) void loadAll()
  } catch (e) { actionMsg.value = (e as Error).message || '断点移除失败' }
}

/** 供顶栏「工具」菜单外部唤起。 */
function show() { open.value = true }
defineExpose({ show })
</script>

<template>
  <div class="hp">
    <button class="hp-trigger" :title="'AI 运行台：花费 / 历史对话 / 每步操作记录 / 技能扩展'" @click="open = !open">
      <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
        <path d="M1 6.5 H3.2 L4.6 2 L6.8 11 L8.2 6.5 H12" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span class="hp-label">AI 运行台</span>
    </button>

    <template v-if="open">
      <Teleport to="body">
        <div class="hp-backdrop wb-modal-backdrop" @click="open = false" />
        <div class="hp-pop wb-modal-shell" role="dialog" aria-modal="true" aria-label="AI 运行台">
        <div class="hp-head wb-modal-head">
          <div>
            <b>AI 运行台</b>
            <p>看 AI 每次回答花了多少、操作了什么、装了哪些扩展</p>
          </div>
          <button class="hp-x" title="关闭" @click="open = false">×</button>
        </div>

        <div v-if="demoMode" class="hp-demo">
          示例演示模式：以下是演示数据。在本地启动 DocMind 桌面端后，这里会显示你的真实记录。
        </div>
        <p v-if="error" class="hp-err">{{ error }} <button class="hp-link" @click="loadAll">重试</button></p>
        <p v-if="actionMsg" class="hp-actionmsg">{{ actionMsg }}</p>

        <div class="hp-tabs">
          <button v-for="t in TABS" :key="t.key" :class="{ active: tab === t.key }" @click="tab = t.key">{{ t.label }}</button>
        </div>

        <!-- ---------------- 花费 ---------------- -->
        <div v-if="tab === 'cost'" class="hp-pane">
          <div class="hp-statgrid">
            <div class="hp-stat wb-card">
              <span>今日 AI 花费</span>
              <b>{{ money(budget?.day_spent) }}</b>
              <em>{{ budget?.day || '' }}</em>
            </div>
            <div class="hp-stat wb-card">
              <span>累计花费</span>
              <b>{{ money(budget?.global_spent) }}</b>
              <em>按模型官方单价折算</em>
            </div>
            <div class="hp-stat wb-card">
              <span>预算上限</span>
              <b v-if="(budget?.global_limit || 0) > 0">{{ money(budget?.global_limit) }}</b>
              <b v-else class="hp-none">不限</b>
              <em>超过预算 AI 会自动停下</em>
            </div>
          </div>
          <p class="hp-plain">
            本地模型（Ollama / llama.cpp）在你自己的显卡上运行，花费永远为 ¥0；
            使用云端 API 时，这里按官方单价累计费用，防止 AI 跑超支。
          </p>
          <div class="hp-row">
            <input v-model="limitInput" placeholder="输入累计预算上限（元），0 表示不限" />
            <button class="hp-btn primary" @click="saveLimit">保存预算</button>
            <button class="hp-btn" @click="resetSpent">计数清零</button>
          </div>
          <div v-if="budget && (budget.per_minute_calls_limit > 0 || budget.per_minute_cost_limit > 0)" class="hp-rateline">
            每分钟限流：{{ budget.per_minute_calls_limit || '不限' }} 次
            <template v-if="budget.per_minute_cost_limit > 0"> / {{ money(budget.per_minute_cost_limit) }}</template>
          </div>
        </div>

        <!-- ---------------- 对话 ---------------- -->
        <div v-else-if="tab === 'sessions'" class="hp-pane">
          <div class="hp-sesshead">
            <p class="hp-plain">每段对话都会单独保存，互不串台。删除后该段问答历史不可恢复。</p>
            <button class="hp-btn sm" title="开始一段全新对话（当前会话保留在列表里，可随时「继续」）" @click="newSession">+ 新会话</button>
          </div>
          <div v-if="!sessions.length" class="hp-empty">还没有保存的对话。到下方「AI 助手」问一个问题试试。</div>
          <div v-else class="hp-list">
            <div v-for="s in sessions" :key="s.session_id" class="hp-item wb-card" :class="{ on: s.session_id === currentId }">
              <div class="hp-item-main">
                <b>{{ s.session_id }}<span v-if="s.session_id === currentId" class="hp-cur">当前</span></b>
                <span>{{ s.turns }} 轮问答<template v-if="s.has_summary"> · 已生成早期摘要</template></span>
                <em>{{ ago(s.updated_at) }}（{{ fmtTime(s.updated_at) }}）</em>
              </div>
              <button class="hp-btn sm" title="切到这段对话并载入其历史" @click="continueSession(s.session_id)">继续</button>
              <button class="hp-btn danger sm" title="删除这段对话" @click="removeSession(s.session_id)">删除</button>
            </div>
          </div>
        </div>

        <!-- ---------------- 轨迹 ---------------- -->
        <div v-else-if="tab === 'trace'" class="hp-pane">
          <div v-if="traceSummary" class="hp-sumline">
            共 <b>{{ traceSummary.turns }}</b> 轮回答 ·
            累计 <b>{{ traceSummary.total_tokens.toLocaleString() }}</b> token ·
            平均每轮 <b>{{ fmtMs(traceSummary.avg_elapsed_ms) }}</b> ·
            错误 <b :class="{ bad: traceSummary.errors > 0 }">{{ traceSummary.errors }}</b> ·
            中止 <b>{{ traceSummary.aborted }}</b>
          </div>
          <p class="hp-plain">AI 每轮回答调用了哪些工具、用了多少 token、结果如何，都记在这里（不记录对话正文，只记操作元数据）。</p>
          <!-- LangSmith 轨迹：只显示状态与开启指引，不在应用内做在线配置 -->
          <div v-if="langsmith" class="hp-ls" :class="{ on: langsmith.enabled }">
            <div class="hp-ls-head">
              <b>LangSmith 轨迹</b>
              <span class="hp-ls-badge" :class="langsmith.enabled ? 'ok' : 'off'">{{ langsmith.enabled ? '已开启' : '未开启' }}</span>
              <span class="hp-ls-text">{{ langsmithText() }}</span>
            </div>
            <template v-if="langsmith.enabled">
              <p class="hp-ls-line">
                项目 <b>{{ langsmith.project || 'default' }}</b> · {{ langsmith.realtime ? '实时上报' : '后台批量上报' }}
                <template v-if="Number(langsmith.pending_exports || 0)"> · 待发送 {{ langsmith.pending_exports }}</template>
                ，在对话框里每个工作流卡片的事件与子代理步骤可与此处的运行轨迹对应。
              </p>
              <p class="hp-ls-line" v-for="(failure, i) in (langsmith.recent_export_errors || []).slice(-3)" :key="i" :title="failure.error">
                <span class="hp-ls-err">上报异常</span> {{ failure.name || 'run' }}：{{ failure.error || '未知错误' }}
              </p>
              <a class="hp-linkline" :href="langsmithSmithUrl" target="_blank" rel="noreferrer">前往 LangSmith 查看运行轨迹 ↗</a>
            </template>
            <p v-else class="hp-ls-guide">{{ langsmithGuide }}</p>
          </div>
          <div v-if="!traces.length" class="hp-empty">暂无操作记录。</div>
          <div v-else class="hp-list">
            <div v-for="it in traces" :key="it.turn_id" class="hp-trace wb-card">
              <div class="hp-trace-head">
                <span class="hp-time">{{ fmtTime(it.ts) }}</span>
                <span class="hp-model">{{ providerLabel(it.provider) }}<template v-if="it.model"> · {{ it.model }}</template></span>
                <span :class="['hp-tag', 'wb-status-chip', outcomeTag(it).cls]">{{ outcomeTag(it).text }}</span>
                <span class="hp-spacer" />
                <span class="hp-nums">{{ it.total_tokens.toLocaleString() }} token · {{ money(it.cost_cny) }} · {{ fmtMs(it.elapsed_ms) }}<template v-if="it.cache_read_tokens"> · <span class="hp-cache">缓存命中 {{ it.cache_read_tokens.toLocaleString() }}</span></template></span>
              </div>
              <div v-if="toolNames(it).length" class="hp-tools">
                <i v-for="(a, i) in toolNames(it)" :key="i">{{ a }}</i>
              </div>
              <div v-else-if="it.error" class="hp-traceerr">{{ fmtTraceError(it.error) }}</div>
            </div>
          </div>
          <div class="hp-footrow">
            <a class="hp-linkline" href="/trace.html" target="_blank" rel="noreferrer">打开完整轨迹页 ↗</a>
            <button class="hp-btn sm" title="把每轮问答画成「提问→思考→调工具→回答」节点流水线" @click="open = false; openFlow()">流程图查看</button>
            <button class="hp-btn sm" @click="clearTraces">清空记录</button>
          </div>
        </div>

        <!-- ---------------- 技能与扩展 ---------------- -->
        <div v-else-if="tab === 'skills'" class="hp-pane">
          <div class="hp-subhead">
            <b>技能</b>
            <button class="hp-btn sm" @click="reloadSkills">重新扫描</button>
          </div>
          <p class="hp-plain">技能是写给 AI 的「专项操作手册」：遇到对应任务时它会先读手册再动手。</p>
          <div v-if="!skills.length" class="hp-empty">
            还没有安装技能。把 SKILL.md 放进 .docmind/skills/ 目录后点「重新扫描」即可。
          </div>
          <div v-else class="hp-list">
            <div v-for="sk in skills" :key="sk.path" class="hp-skill wb-card">
              <b>{{ sk.name }}</b>
              <span>{{ sk.description }}</span>
              <em>适用：{{ sk.when_to_use }}</em>
              <small>版本 {{ sk.version || '—' }} · 使用 {{ sk.stats?.uses || 0 }} 次 · 成功 {{ sk.stats?.successes || 0 }} · 失败 {{ sk.stats?.failures || 0 }}</small>
              <small v-if="sk.history_versions?.length" class="hp-skill-history">历史版本：{{ skillHistoryText(sk) }}</small>
              <button v-if="sk.source === 'user'" class="hp-btn sm hp-skill-rollback" @click="rollbackSkill(sk)">回滚</button>
            </div>
          </div>

          <div class="hp-subhead" style="margin-top:14px">
            <b>自动钩子（高级）</b>
            <button class="hp-btn sm" @click="reloadHooks">重新扫描</button>
          </div>
          <p class="hp-plain">
            钩子是在「AI 每次调工具 / 每轮回答」前后自动运行的 Python 脚本，
            当前已启用 <b>{{ hooksTotal }}</b> 个
            （调工具前 {{ hooksCounts.pre_tool || 0 }} · 调工具后 {{ hooksCounts.post_tool || 0 }} ·
            每轮前 {{ hooksCounts.pre_turn || 0 }} · 每轮后 {{ hooksCounts.post_turn || 0 }}）。
            钩子与本服务同权限运行，只应放置你自己信任的脚本。
          </p>
          <div class="hp-hook-breakpoint">
            <div class="hp-subhead"><b>安全断点</b><small>仅允许预定义生命周期，不执行用户输入的代码</small></div>
            <div class="hp-row">
              <select v-model="breakpointKind">
                <option v-for="kind in hookKinds" :key="kind" :value="kind">{{ kind }}</option>
              </select>
              <input v-model="breakpointMatch" placeholder="匹配内容（可留空，匹配全部）" />
            </div>
            <div class="hp-row">
              <input v-model="breakpointReason" placeholder="暂停时展示给用户的原因" />
              <label class="hp-check"><input v-model="breakpointBlock" type="checkbox" /> 命中即暂停</label>
              <button class="hp-btn primary" @click="saveHookBreakpoint">保存断点</button>
            </div>
            <div v-if="Object.keys(hookBreakpoints).length" class="hp-hook-list">
              <div v-for="(item, kind) in hookBreakpoints" :key="kind" class="hp-hook-item">
                <span><b>{{ kind }}</b> · {{ item.match || '全部' }} · {{ item.block ? '阻断' : '仅记录' }}</span>
                <button class="hp-btn sm danger" @click="removeHookBreakpoint(String(kind))">移除</button>
              </div>
            </div>
          </div>
        </div>

        <!-- ---------------- 开发工作流已并入对话框：工作流卡片、子代理实时轨迹与人工审核都在聊天中展示 ---------------- -->
        </div>
      </Teleport>
    </template>
  </div>
</template>

<style scoped>
.hp { position: relative; }
.hp-trigger {
  display: inline-flex; align-items: center; gap: 6px;
  height: 28px; padding: 0 11px;
  border: 1px solid #c8dcfa;
  background: linear-gradient(180deg, #f3f8ff, #eaf1fe);
  color: #2f6fed;
  border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer;
  white-space: nowrap;
}
.hp-trigger:hover { border-color: #2f6fed; filter: brightness(1.02); }
.hp-backdrop { position: fixed; inset: 0; z-index: 40; }
.hp-pop {
  position: fixed; right: 16px; top: 54px; z-index: 1001;
  width: 480px; max-width: calc(100vw - 32px);
  max-height: calc(100vh - 70px); overflow: auto;
  background: #fff; border: 1px solid #dde3ee; border-radius: 14px;
  box-shadow: 0 18px 50px rgba(35, 52, 84, .22);
  padding: 15px 16px 14px;
}
.hp-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.hp-head b { font-size: 14.5px; color: #1b2433; }
.hp-head p { margin: 3px 0 0; font-size: 11.5px; color: #98a3b4; }
.hp-x { border: none; background: transparent; font-size: 18px; line-height: 1; color: #98a3b4; cursor: pointer; padding: 2px 6px; border-radius: 6px; }
.hp-x:hover { background: #f3f6fb; color: #222b38; }
.hp-demo { margin-top: 10px; font-size: 11.5px; line-height: 1.6; color: #8a5a16; background: #fdf2e0; border: 1px solid #f0d29a; border-radius: 8px; padding: 7px 10px; }
.hp-err { margin: 8px 0 0; font-size: 12px; color: #d23b42; }
.hp-actionmsg { margin: 8px 0 0; font-size: 11.5px; color: #1c9e66; }
.hp-tabs { display: flex; gap: 4px; margin: 12px 0 10px; background: #f3f6fb; padding: 3px; border-radius: 9px; }
.hp-tabs button {
  flex: 1; border: none; background: transparent; cursor: pointer;
  font-size: 12px; font-weight: 600; color: #5a6778; padding: 6px 0; border-radius: 7px;
}
.hp-tabs button.active { background: #fff; color: #2f6fed; box-shadow: 0 1px 4px rgba(35,52,84,.12); }
.hp-pane { font-size: 12px; }
.hp-statgrid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 9px; }
.hp-stat { background: #f7f9fc; border: 1px solid #e4e9f2; border-radius: 11px; padding: 10px 11px; display: flex; flex-direction: column; gap: 3px; }
.hp-stat span { font-size: 10.5px; color: #98a3b4; }
.hp-stat b { font-size: 17px; color: #1b2433; font-variant-numeric: tabular-nums; }
.hp-stat b.hp-none { color: #1c9e66; font-size: 14px; }
.hp-stat em { font-style: normal; font-size: 10px; color: #a7b2c2; line-height: 1.4; }
.hp-plain { margin: 10px 0 0; font-size: 11.5px; line-height: 1.75; color: #5a6778; }
.hp-row { display: flex; gap: 7px; margin-top: 10px; }
.hp-row input { flex: 1; min-width: 0; padding: 7px 10px; border: 1px solid #c4cedd; border-radius: 7px; font: inherit; color: #222b38; background: #f6f8fb; outline: none; }
.hp-row select { min-width: 170px; padding: 7px 9px; border: 1px solid #c4cedd; border-radius: 7px; font: inherit; color: #222b38; background: #f6f8fb; outline: none; }
.hp-row input:focus { border-color: #2f6fed; background: #fff; box-shadow: 0 0 0 3px rgba(47,111,237,.12); }
.hp-btn { border: 1px solid #c4cedd; background: #fff; color: #334055; border-radius: 7px; padding: 7px 12px; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap; }
.hp-btn:hover { border-color: #9fb0c6; background: #f7f9fc; }
.hp-btn.primary { background: linear-gradient(180deg,#3b7ef2,#2f6fed); border-color: #2560d4; color: #fff; }
.hp-btn.primary:hover { filter: brightness(1.06); }
.hp-btn.danger { color: #d23b42; border-color: #efc4c6; }
.hp-btn.danger:hover { background: #fdecec; border-color: #e0484f; }
.hp-btn.sm { padding: 4px 10px; font-size: 11px; }
.hp-link { border: none; background: none; color: #2f6fed; cursor: pointer; font: inherit; padding: 0; }
.hp-rateline { margin-top: 9px; font-size: 11px; color: #5a6778; }
.hp-empty { margin-top: 10px; padding: 16px; text-align: center; font-size: 12px; color: #98a3b4; background: #f7f9fc; border: 1px dashed #d5dce7; border-radius: 10px; line-height: 1.7; }
.hp-list { margin-top: 9px; display: flex; flex-direction: column; gap: 7px; max-height: 380px; overflow: auto; }
.hp-item { display: flex; align-items: center; gap: 8px; background: #fafbfd; border: 1px solid #e4e9f2; border-radius: 10px; padding: 8px 11px; }
.hp-item-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
.hp-item-main b { font-size: 12px; color: #1b2433; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hp-item-main span { font-size: 11px; color: #5a6778; }
.hp-item-main em { font-style: normal; font-size: 10px; color: #a7b2c2; }
.hp-item.on { border-color: #9fc0f5; background: #f2f7ff; box-shadow: 0 0 0 2px rgba(47,111,237,.10); }
.hp-cur { margin-left: 6px; font-size: 9.5px; font-weight: 700; color: #2f6fed; background: #eaf1fe; border: 1px solid #c8dcfa; border-radius: 999px; padding: 0 6px; }
.hp-sesshead { display: flex; align-items: center; gap: 10px; }
.hp-sesshead .hp-plain { flex: 1; margin: 0; }
.hp-sumline { font-size: 11.5px; color: #5a6778; background: #f7f9fc; border: 1px solid #e4e9f2; border-radius: 9px; padding: 8px 10px; line-height: 1.8; }
.hp-sumline b { color: #1b2433; }
.hp-sumline b.bad { color: #d23b42; }
.hp-trace { background: #fafbfd; border: 1px solid #e4e9f2; border-radius: 10px; padding: 8px 11px; }
.hp-trace-head { display: flex; align-items: center; gap: 8px; font-size: 11px; }
.hp-time { color: #5a6778; font-variant-numeric: tabular-nums; }
.hp-model { color: #7a4fd1; font-weight: 600; }
.hp-spacer { flex: 1; }
.hp-nums { color: #98a3b4; font-variant-numeric: tabular-nums; }
.hp-cache { color: var(--green); font-weight: 600; }
.hp-tag { font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 999px; }
.hp-tag-ok { color: #128053; background: #e6f7ee; }
.hp-tag-err { color: #b32d33; background: #fdecec; }
.hp-tag-stop { color: #8a5a16; background: #fdf2e0; }
.hp-tools { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.hp-tools i { font-style: normal; font-family: var(--font-mono); font-size: 10px; color: #2f6fed; background: #eaf1fe; border: 1px solid #c8dcfa; border-radius: 5px; padding: 1px 7px; }
.hp-traceerr { margin-top: 5px; font-size: 11px; color: #b32d33; }
.hp-footrow { display: flex; align-items: center; justify-content: space-between; margin-top: 10px; }
.hp-linkline { font-size: 12px; color: #2f6fed; text-decoration: none; }
.hp-linkline:hover { text-decoration: underline; }
.hp-subhead { display: flex; align-items: center; justify-content: space-between; }
.hp-subhead b { font-size: 12.5px; color: #1b2433; }
.hp-skill { background: #fafbfd; border: 1px solid #e4e9f2; border-radius: 10px; padding: 9px 11px; display: flex; flex-direction: column; gap: 3px; }
.hp-skill b { font-size: 12px; color: #1b2433; }
.hp-skill span { font-size: 11.5px; color: #5a6778; line-height: 1.6; }
.hp-skill em { font-style: normal; font-size: 10.5px; color: #8a4fd1; }
.hp-skill small { font-size: 10px; color: #7b8796; }
.hp-skill-history { color: #5374a5 !important; }
.hp-skill-rollback { align-self: flex-start; margin-top: 4px; }
.hp-hook-breakpoint { margin-top:10px; padding:8px 9px; border:1px solid #e4e9f2; border-radius:8px; background:#fbfcfe; }
.hp-hook-breakpoint .hp-subhead { margin:0 0 6px; }
.hp-hook-breakpoint .hp-subhead small { color:#98a3b4; font-size:9px; font-weight:400; margin-left:6px; }
.hp-hook-breakpoint select { min-width:125px; }
.hp-check { display:inline-flex; align-items:center; gap:4px; color:#5a6778; font-size:10px; white-space:nowrap; }
.hp-check input { margin:0; }
.hp-hook-list { display:flex; flex-direction:column; gap:4px; margin-top:7px; }
.hp-hook-item { display:flex; align-items:center; justify-content:space-between; gap:6px; padding:5px 6px; border-top:1px solid #edf0f5; color:#5a6778; font-size:10px; }
.hp-hook-item b { color:#7a4fd1; }
/* LangSmith 状态卡：状态徽标 + 开启指引，不在应用内做在线配置 */
.hp-ls { margin:8px 0 10px; padding:9px 10px; border:1px solid #e4e9f2; border-radius:9px; background:#fbfcfe; }
.hp-ls.on { border-color:#bfe6d2; background:#f5fcf8; }
.hp-ls-head { display:flex; align-items:center; gap:7px; }
.hp-ls-head b { font-size:11.5px; }
.hp-ls-badge { display:inline-flex; align-items:center; height:17px; padding:0 7px; border-radius:9px; font-size:9.5px; font-style:normal; font-weight:700; }
.hp-ls-badge.ok { color:#128053; background:#ddf5e7; }
.hp-ls-badge.off { color:#8a6216; background:#fcefd6; }
.hp-ls-text { color:#8a95a6; font-size:10px; }
.hp-ls-line { margin:6px 0 0; color:#5a6778; font-size:10.5px; line-height:1.6; }
.hp-ls-guide { margin:6px 0 0; color:#6b5530; font-size:10.5px; line-height:1.7; }
.hp-ls-err { display:inline-block; margin-right:4px; padding:0 5px; border-radius:4px; background:#fdecec; color:#b32d33; font-size:9.5px; font-style:normal; }
.hp-ls .hp-linkline { display:inline-block; margin-top:7px; }
</style>
