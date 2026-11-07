<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { delegationApi, memoryPanelApi, orchestrationApi } from '../api'
import type { DelegationNode, DelegationTree, OrchestrationPlan, TaskAssistantReport } from '../api'
import { usePolling } from '../composables/polling'

const props = defineProps<{ sessionId: string; selectedId: string }>()
const emit = defineEmits<{
  (e: 'select', id: string, label: string): void
  (e: 'changed'): void
}>()

const data = ref<DelegationTree | null>(null)
const loading = ref(false)
const error = ref('')
const busy = ref('')
const decisionError = ref('')
const assistantGoal = ref('')
const lastGoal = ref('')
const assistantBusy = ref(false)
const assistantReport = ref<TaskAssistantReport | null>(null)
const chainPlan = ref<OrchestrationPlan | null>(null)
const planState = ref<'idle' | 'loading' | 'ok' | 'missing'>('idle')
const planError = ref('')

interface ContextCount {
  state: 'loading' | 'ok' | 'missing' | 'error'
  injected?: number
  memories?: number
  reason?: string
}
const contextInfo = ref<Record<string, ContextCount>>({})

const statusLabels: Record<string, string> = {
  active: '当前主 Agent', pending: '等待审批', running: '执行中', waiting_child: '等待下一层审批',
  completed: '已完成', failed: '失败', rejected: '已拒绝', unknown: '进程中断',
}
const permissionError = computed(() => /401|403|forbidden|unauthorized|无权限|权限不足/i.test(error.value))

async function load() {
  if (!props.sessionId) return
  loading.value = true
  error.value = ''
  try {
    const result = await delegationApi.tree(props.sessionId)
    if (result.session_id === props.sessionId) {
      data.value = result
      void loadContextCounts()
      void loadPlan()
    }
  } catch (e) {
    error.value = (e as Error)?.message || '读取 Agent 树失败'
  } finally {
    loading.value = false
  }
}
usePolling(load, 3000)
watch(() => props.sessionId, () => {
  data.value = null
  decisionError.value = ''
  assistantReport.value = null
  lastGoal.value = ''
  chainPlan.value = null
  planState.value = 'idle'
  planError.value = ''
  contextInfo.value = {}
  void load()
})

const flatNodes = computed(() => {
  const out: Array<DelegationNode & { level: number }> = []
  function visit(node: DelegationNode, level: number) {
    out.push({ ...node, level })
    ;(node.children || []).forEach(child => visit(child, level + 1))
  }
  if (data.value?.root) visit(data.value.root, 0)
  return out
})

function displayName(node: DelegationNode) {
  return node.id === 'main' ? '平台助手' : (node.role || node.id)
}

function parentLabel(node: DelegationNode) {
  if (node.parent_id === 'main') return '平台助手'
  return data.value?.nodes.find(item => item.id === node.parent_id)?.role || node.parent_id
}

/** 每个节点的上下文条数走 inspect（agent 级）；404 表示快照还在但实例已不在进程内。 */
async function loadContextCounts() {
  const ids = flatNodes.value.map(node => node.id)
  const pending = ids.filter(id => !contextInfo.value[id])
  if (!pending.length) return
  await Promise.all(pending.map(async id => {
    contextInfo.value[id] = { state: 'loading' }
    try {
      const result = await memoryPanelApi.inspect(props.sessionId, id, true)
      if (result.available === false) {
        contextInfo.value[id] = { state: 'missing', reason: result.error || '无活动实例' }
        return
      }
      contextInfo.value[id] = {
        state: 'ok',
        injected: result.injected?.length || 0,
        memories: result.memories?.length || 0,
      }
    } catch (e) {
      const message = (e as Error)?.message || '读取失败'
      contextInfo.value[id] = /不存在该 Agent 实例|404|not found/i.test(message)
        ? { state: 'missing', reason: '无活动实例' }
        : { state: 'error', reason: message }
    }
  }))
}

/** 任务助手计划落在 .docmind/orchestrations/<plan_id>.json；树节点带 plan_id 时取回持久报告，
 *  这样刷新后仍能拿到 report.merged（自然语言汇总）。 */
async function loadPlan() {
  const planId = String(data.value?.nodes.find(node => node.plan_id)?.plan_id || '')
  if (!planId || planId === String(chainPlan.value?.plan_id || '')) return
  planState.value = 'loading'
  planError.value = ''
  try {
    chainPlan.value = await orchestrationApi.get(planId)
    planState.value = 'ok'
  } catch (e) {
    chainPlan.value = null
    planState.value = 'missing'
    planError.value = (e as Error)?.message || '计划读取失败'
  }
}

function contextText(node: DelegationNode) {
  const info = contextInfo.value[node.id]
  if (!info || info.state === 'loading') return '读取中…'
  if (info.state === 'ok') return `注入 ${info.injected} / 库存 ${info.memories} 条`
  if (info.state === 'missing') return '不可读（无活动实例）'
  return `读取失败：${info.reason || '未知'}`
}

/** 心跳只展示后端真实派生字段；没有记录时明确写「无心跳记录」，不补 0 秒。 */
const heartbeatStateLabels: Record<string, string> = {
  alive: '正常', stale: '静默超阈值', dead: '失联', none: '无心跳记录',
  closed: '已结束', waiting: '等待审批',
}

function clockText(iso: string | null | undefined) {
  if (!iso) return '未记录'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return String(iso)
  return date.toLocaleTimeString('zh-CN', { hour12: false })
}

function secondsText(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  return `${Number(value).toFixed(1)}s`
}

function heartbeatText(node: DelegationNode) {
  const heartbeat = node.heartbeat
  if (!heartbeat || heartbeat.state === 'none' || heartbeat.last_seen === null) return '无心跳记录'
  const parts = [
    `最后活动 ${clockText(heartbeat.last_seen)}`,
    `静默 ${secondsText(heartbeat.silence_seconds)}`,
    `间隔 ${secondsText(heartbeat.interval_seconds)}`,
    `beat ${heartbeat.beats} 次`,
  ]
  if (heartbeat.progress) {
    parts.push(`进度 ${heartbeat.progress.steps ?? 0} 步 · ${heartbeat.progress.phase || '未标注阶段'}`)
  }
  if (heartbeat.progress_frozen) parts.push(`进度冻结 ${secondsText(heartbeat.progress_age_seconds)}`)
  parts.push(heartbeatStateLabels[heartbeat.state] || heartbeat.state)
  return parts.join(' · ')
}

function recentBeats(node: DelegationNode) {
  return (node.heartbeat?.recent_beats || []).slice(-4)
}

function beatDelta(beats: Array<{ epoch: number }>, index: number) {
  if (index <= 0) return ''
  const delta = beats[index].epoch - beats[index - 1].epoch
  return `+${delta.toFixed(1)}s`
}

const heartbeatConfig = computed(() => data.value?.heartbeat_config)
const heartbeatCounts = computed(() => {
  const counts = { alive: 0, stale: 0, dead: 0, none: 0 }
  for (const node of flatNodes.value) {
    const state = node.heartbeat?.state || 'none'
    if (state === 'alive' || state === 'stale' || state === 'dead') counts[state] += 1
    else if (state === 'none') counts.none += 1
  }
  return counts
})
const heartbeatSummary = computed(() => {
  const counts = heartbeatCounts.value
  const recorded = counts.alive + counts.stale + counts.dead
  return recorded
    ? `alive ${counts.alive} · stale ${counts.stale} · dead ${counts.dead}`
    : '当前无心跳记录'
})
const heartbeatThresholdText = computed(() => {
  const config = heartbeatConfig.value
  if (!config) return '阈值未提供'
  return `${config.interval_seconds}s × stale ${config.stale_factor} / dead ${config.dead_factor}`
})
const alerts = computed(() => data.value?.alerts || [])

function artifactItems(node: DelegationNode) {
  const result = (node.result || {}) as Record<string, unknown>
  const rows: string[] = []
  const push = (value: unknown) => {
    const text = String(value || '').trim()
    if (text && !rows.includes(text)) rows.push(text)
  }
  for (const item of (result.file_changes as Array<Record<string, unknown>> | undefined) || []) {
    push(item.path || item.label)
  }
  for (const item of (result.artifacts as Array<Record<string, unknown>> | undefined) || []) {
    push(item.path || item.uri || item.id || item.label)
  }
  return rows
}

const assistantCapabilities = computed(() => {
  const config = heartbeatConfig.value
  return [
    { label: '语义路由', detail: '任务助手 LLM 细化目标', state: '已实现' },
    { label: '派发', detail: 'delegation.request', state: '已实现' },
    { label: '汇总', detail: '_synth / report.merged', state: '已实现' },
    {
      label: '心跳监控',
      detail: config
        ? `delegation.beat 执行侧打点 · ${heartbeatSummary.value}`
        : '后端未提供 heartbeat_config',
      state: config ? '已实现' : '未实现',
    },
  ]
})

const chainNodes = computed(() => (data.value?.nodes || []).filter(node => node.id !== 'main'))
const chainMerged = computed(() => assistantReport.value?.merged || chainPlan.value?.report?.merged || '')
const chainPlanId = computed(() => chainPlan.value?.plan_id || String(data.value?.nodes.find(node => node.plan_id)?.plan_id || ''))

function openNode(node: DelegationNode) {
  emit('select', node.id, displayName(node))
}

async function runAssistant() {
  const goal = assistantGoal.value.trim()
  if (!goal || assistantBusy.value) return
  assistantBusy.value = true
  decisionError.value = ''
  assistantReport.value = null
  lastGoal.value = goal
  try {
    assistantReport.value = await delegationApi.taskAssist(props.sessionId, goal)
    assistantGoal.value = ''
    await load()
    emit('changed')
  } catch (e) {
    decisionError.value = (e as Error)?.message || '任务助手编排失败'
  } finally {
    assistantBusy.value = false
  }
}

async function decide(node: DelegationNode, approved: boolean) {
  if (busy.value) return
  busy.value = node.id
  decisionError.value = ''
  try {
    const result = await delegationApi.decide(props.sessionId, node.id, approved)
    if (!result.ok) decisionError.value = result.error || '审批处理失败'
    await load()
    emit('changed')
  } catch (e) {
    decisionError.value = (e as Error)?.message || '审批处理失败'
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <div class="at">
    <div v-if="loading && !data" class="at-state" role="status">
      <span class="at-state-mark" aria-hidden="true"></span><strong>正在读取任务拓扑</strong><p>同步平台助手与每个子 Agent 的层级、状态与上下文。</p>
    </div>
    <div v-else-if="error && !data" class="at-state" :class="{ 'is-permission': permissionError }" role="alert">
      <span class="at-state-mark" aria-hidden="true"></span><strong>{{ permissionError ? '没有查看任务树的权限' : '任务树暂时不可用' }}</strong>
      <p>{{ permissionError ? '当前身份不能读取此会话的 Agent 层级，请切换账号或联系会话所有者。' : error }}</p>
      <button type="button" @click="load">重新读取</button>
    </div>
    <div v-if="error && data" class="at-message" role="alert">{{ error }}<button type="button" @click="load">重试</button></div>
    <div v-if="decisionError" class="at-message" role="alert">{{ decisionError }}</div>

    <section class="at-assistant" aria-label="平台助手需求细化">
      <header><div><span>01</span><h3>平台助手 · 需求细化</h3></div><small>只细化与派发，不直接执行业务</small></header>
      <p class="at-description">平台助手有独立上下文，能力等同普通 Agent：先做语义路由、把目标细化成标准任务规格，再派给子 Agent 并汇总结果。</p>
      <textarea v-model="assistantGoal" rows="3" placeholder="描述目标、输入、产出物和边界…" :disabled="assistantBusy" />
      <button type="button" class="at-approve" :disabled="assistantBusy || !assistantGoal.trim()" @click="runAssistant">
        {{ assistantBusy ? '正在细化与分派…' : '细化并分派' }}
      </button>
      <div v-if="assistantReport" class="at-assistant-report">
        <p>{{ assistantReport.merged }}</p>
        <details v-for="task in assistantReport.tasks" :key="task.id">
          <summary>{{ task.goal || task.task }} · {{ statusLabels[assistantReport.results[task.id]?.status || ''] || assistantReport.results[task.id]?.status || '未执行' }}</summary>
          <dl>
            <dt>输入</dt><dd>{{ (task.input || []).join('；') }}</dd>
            <dt>产出物</dt><dd>{{ (task.outputs || []).join('；') }}</dd>
            <dt>边界</dt><dd>{{ (task.boundaries || []).join('；') }}</dd>
            <dt>验收</dt><dd>{{ (task.acceptance || []).join('；') }}</dd>
            <template v-if="assistantReport.results[task.id]?.failure_reason">
              <dt>失败原因</dt><dd>{{ assistantReport.results[task.id].failure_reason }}</dd>
              <dt>续跑点</dt><dd>{{ assistantReport.results[task.id].resume_point }}</dd>
            </template>
          </dl>
        </details>
      </div>
    </section>

    <section v-if="alerts.length" class="at-alerts" aria-label="平台助手已收到的失联报告">
      <header><div><span>!</span><h3>失联报告</h3></div><small>平台助手已收到 · 后端读路径落盘</small></header>
      <article v-for="alert in alerts" :key="`${alert.agent_id}-${alert.last_seen}`" class="at-alert" data-heartbeat-alert>
        <strong>{{ alert.agent_id }}</strong>
        <span v-if="alert.role"> · {{ alert.role }}</span>
        <p>平台助手已收到失联报告 · {{ clockText(alert.detected_at) }}</p>
        <small>静默 {{ secondsText(alert.silence_seconds) }} · 最后活动 {{ clockText(alert.last_seen) }} · 进度冻结 {{ alert.progress_frozen ? '是' : '否' }}</small>
        <small>接收端：{{ alert.receiver }} · 落盘：{{ alert.delivery }}</small>
      </article>
    </section>

    <section v-if="data?.pending.length" class="at-approvals" aria-label="待审批派发">
      <header><div><span>02</span><h3>待审批</h3></div><small>{{ data.pending.length }} 项</small></header>
      <article v-for="node in data.pending" :key="node.id" class="at-approval">
        <strong>{{ parentLabel(node) }} → {{ node.role }}</strong>
        <p>{{ node.task }}</p>
        <dl><dt>层级</dt><dd>第 {{ node.depth }} 层</dd><dt>预计影响</dt><dd>{{ node.impact?.estimated }}</dd></dl>
        <small v-if="node.impact?.tools?.length">工具：{{ node.impact.tools.join('、') }}</small>
        <p v-if="node.requires_limit_confirmation" class="at-limit">超过默认防护上限，需明确允许继续：{{ node.limit_reasons?.join('；') }}</p>
        <div class="at-actions">
          <button type="button" class="at-approve" :disabled="!!busy" @click="decide(node, true)">{{ busy === node.id ? '正在执行…' : node.requires_limit_confirmation ? '确认超限并执行' : '批准派发' }}</button>
          <button type="button" :disabled="!!busy" @click="decide(node, false)">拒绝</button>
        </div>
      </article>
    </section>

    <section class="at-tree" aria-label="平台助手与子 Agent 任务树">
      <header><div><span>{{ data?.pending.length ? '03' : '02' }}</span><h3>任务树</h3></div><small>点击任意节点 → 右栏切到该 Agent 上下文</small></header>
      <div v-if="!loading && !flatNodes.length && !error" class="at-state at-state-embedded">
        <span class="at-state-mark" aria-hidden="true"></span><strong>还没有 Agent 任务节点</strong><p>平台助手或任务助手创建第一条派发后，父子关系会显示在这里。</p>
      </div>

      <article
        v-for="node in flatNodes" :key="node.id" class="at-node at-card"
        :class="[`is-${node.status}`, `hb-${node.heartbeat?.state || 'none'}`, { selected: selectedId === node.id, 'at-root-card': node.id === 'main' }]"
        :data-agent-state="node.status"
        :data-heartbeat-state="node.heartbeat?.state || 'none'"
        :data-agent-id="node.id"
        :data-agent-role="node.id === 'main' ? 'platform-assistant' : node.role"
        :style="{ '--tree-level': node.level }"
        role="button"
        tabindex="0"
        @click="openNode(node)"
        @keydown.enter="openNode(node)"
        @keydown.space.prevent="openNode(node)"
      >
        <div class="at-node-head">
          <span class="at-branch" aria-hidden="true">{{ node.level ? '└' : '●' }}</span>
          <span class="at-copy">
            <strong>{{ displayName(node) }}</strong>
            <code class="at-id">{{ node.id }}</code>
            <span v-if="node.id === 'main'" class="at-independent" data-agent-independent>独立上下文 · 能力等同普通 Agent</span>
          </span>
          <span class="at-status">{{ statusLabels[node.status] || node.status }}</span>
          <span
            v-if="node.heartbeat && (node.heartbeat.state === 'stale' || node.heartbeat.state === 'dead')"
            class="at-hb-flag" :class="`is-${node.heartbeat.state}`"
            data-heartbeat-flag
          >{{ node.heartbeat.state === 'dead' ? '×' : '!' }} 静默 {{ secondsText(node.heartbeat.silence_seconds) }}</span>
        </div>

        <div v-if="node.id === 'main'" class="at-caps" data-agent-capabilities>
          <span v-for="cap in assistantCapabilities" :key="cap.label" class="at-cap" :class="cap.state === '已实现' ? 'is-on' : 'is-off'">
            <b>{{ cap.label }}</b>{{ cap.state }}<small>{{ cap.detail }}</small>
          </span>
        </div>

        <p class="at-task" data-agent-task>{{ node.task }}</p>

        <dl class="at-card-meta">
          <dt>心跳</dt><dd data-agent-heartbeat>{{ heartbeatText(node) }}</dd>
          <dt>上下文</dt><dd data-agent-context-count>{{ contextText(node) }}</dd>
          <dt>产物</dt><dd data-agent-artifacts>{{ artifactItems(node).length ? `${artifactItems(node).length} 个` : '无产物' }}</dd>
          <dt>创建</dt><dd>{{ node.created_at || '未记录' }}</dd>
        </dl>

        <div v-if="recentBeats(node).length" class="at-hb-series" data-heartbeat-series>
          <span class="at-hb-series-label">beat 时间序列</span>
          <span v-for="(beat, index) in recentBeats(node)" :key="beat.epoch" class="at-hb-beat">
            {{ clockText(beat.at) }}<small>{{ beat.phase }}<template v-if="beatDelta(recentBeats(node), index)"> · {{ beatDelta(recentBeats(node), index) }}</template></small>
          </span>
        </div>

        <p v-if="node.result?.conclusion" class="at-card-conclusion">结论：{{ node.result.conclusion }}</p>
        <p v-if="node.result?.error" class="at-card-error">错误：{{ node.result.error }}</p>
        <ul v-if="artifactItems(node).length" class="at-artifacts">
          <li v-for="item in artifactItems(node)" :key="item">{{ item }}</li>
        </ul>

        <details v-if="node.id === 'main'" class="at-chain" open @click.stop>
          <summary>编排链 <small>用户请求 → 细化派单 → 结构化结果 → 自然语言汇总</small></summary>
          <ol>
            <li>
              <span class="at-chain-num">01</span>
              <div>
                <strong>用户请求</strong>
                <p>{{ lastGoal || '未记录' }}</p>
                <small>来源：<code>POST /api/agent/task-assistant</code> 的 <code>body.goal</code>；仅本次调用，不落盘。</small>
              </div>
            </li>
            <li>
              <span class="at-chain-num">02</span>
              <div>
                <strong>细化后的派单 · {{ chainNodes.length }} 项</strong>
                <p v-if="!chainNodes.length">未记录</p>
                <ul v-else class="at-chain-list">
                  <li v-for="node in chainNodes" :key="node.id"><b>{{ displayName(node) }}</b> · {{ node.task }}</li>
                </ul>
                <small>来源：<code>GET /api/agent/delegations</code> 的 <code>nodes[].task</code>（持久化在 <code>.docmind/delegations/&lt;session&gt;.json</code>）。</small>
              </div>
            </li>
            <li>
              <span class="at-chain-num">03</span>
              <div>
                <strong>各 Agent 返回的结构化结果</strong>
                <p v-if="!chainNodes.length">未记录</p>
                <ul v-else class="at-chain-list">
                  <li v-for="node in chainNodes" :key="node.id">
                    <b>{{ displayName(node) }}</b> · {{ statusLabels[node.status] || node.status }}<template v-if="node.result?.conclusion"> · {{ node.result.conclusion }}</template><template v-else> · 无结论</template>
                  </li>
                </ul>
                <small>来源：<code>nodes[].result</code>（conclusion / artifacts / file_changes）；计划副本 <code>.docmind/orchestrations/&lt;plan_id&gt;.json → report.results</code>。</small>
              </div>
            </li>
            <li>
              <span class="at-chain-num">04</span>
              <div>
                <strong>给用户的自然语言汇总</strong>
                <p :class="{ 'at-chain-missing': !chainMerged }">{{ chainMerged || '未记录' }}</p>
                <small>
                  来源：<code>report.merged</code>（任务助手响应；持久副本 <code>.docmind/orchestrations/&lt;plan_id&gt;.json</code>）。
                  <template v-if="planState === 'missing'"> 当前节点无 plan_id 或计划文件不可读：{{ planError }}</template>
                  <template v-if="chainPlanId"> plan_id：<code>{{ chainPlanId }}</code></template>
                </small>
              </div>
            </li>
          </ol>
        </details>

        <div v-if="node.error || node.result?.resume_point" class="at-result">
          <strong>{{ displayName(node) }}：</strong>{{ node.error || node.result?.error }}<template v-if="node.result?.resume_point"> · 续跑点：{{ node.result.resume_point }}</template>
        </div>
      </article>
    </section>

    <footer v-if="data" class="at-limits">
      <strong>递归防护</strong>
      <p>默认软上限：深度 {{ data.limits.max_depth }} · 总分支 {{ data.limits.max_branches }} · 单父 {{ data.limits.max_children }}。超限不会静默递归，会在审批中要求再次确认。</p>
      <code>DOCMIND_DELEGATION_MAX_DEPTH / MAX_BRANCHES / MAX_CHILDREN</code>
      <div v-if="heartbeatConfig" class="at-hb-config" data-heartbeat-config>
        <strong>心跳阈值</strong>
        <p>间隔 {{ heartbeatConfig.interval_seconds }}s · stale {{ heartbeatConfig.stale_factor }}× · dead {{ heartbeatConfig.dead_factor }}× · 进度冻结 {{ heartbeatConfig.progress_freeze_factor }}× —— {{ heartbeatThresholdText }}</p>
        <small>{{ heartbeatConfig.calibration_note }}</small>
        <code>DOCMIND_HEARTBEAT_INTERVAL_S / HEARTBEAT_STALE_FACTOR / HEARTBEAT_DEAD_FACTOR / HEARTBEAT_PROGRESS_FREEZE_FACTOR</code>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.at { min-width: 0; color: var(--mi-text); font-family: var(--mi-font-ui); }
.at button, .at textarea, .at summary { font-family: inherit; }
.at button:focus-visible, .at textarea:focus-visible, .at summary:focus-visible, .at-node:focus-visible { outline: var(--mi-focus-width) solid var(--mi-accent); outline-offset: var(--mi-space-1); }
.at section { padding: var(--mi-space-6) 0; border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); }
.at section:first-of-type { padding-top: var(--mi-space-5); }
.at section > header { display: flex; align-items: center; justify-content: space-between; gap: var(--mi-space-4); margin-bottom: var(--mi-space-3); }
.at section > header > div { display: flex; align-items: baseline; gap: var(--mi-space-3); }
.at h3 { margin: 0; font-size: var(--mi-fs-section); line-height: var(--mi-lh-section); font-weight: var(--mi-weight-semibold); }
.at section > header > div > span { color: var(--mi-accent-dim); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); letter-spacing: var(--mi-tracking-label); }
.at section > header small { color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); text-align: right; }
.at-description { max-width: var(--mi-copy-width); margin: 0 0 var(--mi-space-4); color: var(--mi-text-muted); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.at-assistant textarea { box-sizing: border-box; width: 100%; padding: var(--mi-space-3); border: var(--mi-border-width) solid var(--mi-border-level-2); border-radius: var(--mi-radius-control); background: var(--mi-bg-deep); color: var(--mi-text); font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); resize: vertical; }
.at-assistant textarea::placeholder { color: var(--mi-text-muted); }
.at-approve, .at-actions button, .at-state button { min-height: var(--mi-control-height); padding: 0 var(--mi-space-4); border: var(--mi-border-width) solid var(--mi-border-level-3); border-radius: var(--mi-radius-control); background: var(--mi-surface-level-2); color: var(--mi-text); font-size: var(--mi-fs-secondary); cursor: pointer; }
.at-approve { border-color: var(--mi-accent); background: var(--mi-accent); color: var(--mi-accent-ink); }
.at-assistant > button { margin-top: var(--mi-space-3); }
.at button:disabled { opacity: var(--mi-opacity-disabled); cursor: default; }
.at-approve:hover:not(:disabled) { background: var(--mi-accent-strong); }
.at-assistant-report { margin-top: var(--mi-space-5); }
.at-assistant-report > p { max-width: var(--mi-copy-width); margin: 0 0 var(--mi-space-3); font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); white-space: pre-wrap; }
.at-assistant-report details { border-top: var(--mi-border-width) solid var(--mi-border-subtle); }
.at-assistant-report summary { padding: var(--mi-space-2) 0; color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); cursor: pointer; }
.at-assistant-report dl, .at-approval dl { display: grid; grid-template-columns: var(--mi-space-8) minmax(0, 1fr); gap: var(--mi-space-1) var(--mi-space-3); margin: 0 0 var(--mi-space-3); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.at-assistant-report dt, .at-approval dt { color: var(--mi-text-muted); }
.at-assistant-report dd, .at-approval dd { margin: 0; white-space: pre-wrap; }
.at-approval { padding: var(--mi-space-4) 0; border-top: var(--mi-border-width) solid var(--mi-border-subtle); }
.at-approval > strong { font-size: var(--mi-fs-body); font-weight: var(--mi-weight-medium); }
.at-approval > p { max-width: var(--mi-copy-width); margin: var(--mi-space-2) 0; font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); white-space: pre-wrap; }
.at-approval > small { display: block; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); }
.at-approval .at-limit { color: var(--mi-warning); }
.at-actions { display: flex; gap: var(--mi-space-2); margin-top: var(--mi-space-3); }

/* 节点卡片：不再把角色/任务/状态挤在一行；长任务与产物路径换行阅读。 */
.at-node { display: block; width: 100%; padding: var(--mi-space-4) var(--mi-space-3); padding-left: calc(var(--mi-space-3) + var(--tree-level) * var(--mi-space-5)); border: 0; border-left: var(--mi-state-signal-width) solid transparent; border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); background: transparent; color: var(--mi-text); text-align: left; cursor: pointer; transition: background var(--mi-duration-fast) var(--mi-ease-standard); }
.at-node:hover { background: var(--mi-hover); }
.at-node.selected { background: var(--mi-surface-level-3); box-shadow: inset var(--mi-focus-width) 0 0 var(--mi-accent), var(--mi-shadow-level-2); }
.at-root-card { margin-bottom: var(--mi-space-3); border: var(--mi-border-width) solid var(--mi-border-level-2); border-left-width: var(--mi-state-signal-width); border-radius: var(--mi-radius-lg); background: var(--mi-surface); }
.at-node-head { display: flex; align-items: flex-start; gap: var(--mi-space-3); }
.at-branch { flex: 0 0 var(--mi-tree-branch-width); color: var(--mi-text-muted); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); }
.at-node.is-active .at-branch, .at-node.is-running .at-branch { color: var(--mi-accent); }
.at-copy { display: flex; flex: 1; min-width: 0; flex-wrap: wrap; align-items: baseline; gap: var(--mi-space-2); }
.at-copy strong { font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); font-weight: var(--mi-weight-semibold); white-space: nowrap; }
.at-id { overflow-wrap: anywhere; color: var(--mi-text-muted); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); }
.at-independent { flex-basis: 100%; color: var(--mi-accent-dim); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.at-status { display: inline-flex; align-items: center; gap: var(--mi-state-mark-gap); flex: 0 0 auto; color: var(--mi-text-secondary); font-size: var(--mi-fs-caption); white-space: nowrap; }
.at-status::before { content: ''; flex: 0 0 var(--mi-state-mark-size); width: var(--mi-state-mark-size); height: var(--mi-state-mark-size); box-sizing: border-box; }
/* 四态第二信号：颜色之外再给形态（虚线 / 进度弧 / 对勾 / 斜纹），
   色弱与灰度截图下也能分辨四种状态。 */
.at-node.is-active .at-status::before { border: var(--mi-border-width) solid var(--mi-accent); border-radius: var(--mi-radius-pill); }
.at-node.is-pending, .at-node.is-waiting_child { border-left-style: dashed; border-left-color: var(--mi-state-pending-edge); }
.at-node.is-pending .at-status, .at-node.is-waiting_child .at-status { color: var(--mi-warning); }
.at-node.is-pending .at-status::before, .at-node.is-waiting_child .at-status::before { border: var(--mi-border-width) dashed var(--mi-warning); border-radius: var(--mi-radius-xs); }
.at-node.is-running { border-left-color: var(--mi-accent); }
.at-node.is-running .at-status::before { border: var(--mi-border-width) solid transparent; border-top-color: var(--mi-accent); border-right-color: var(--mi-accent); border-radius: var(--mi-radius-pill); animation: at-status-turn var(--mi-state-run-duration) linear infinite; }
.at-node.is-completed { border-left-color: var(--mi-success); }
.at-node.is-completed .at-status { color: var(--mi-success); }
.at-node.is-completed .at-status::before { height: calc(var(--mi-state-mark-size) * 0.5); border-left: var(--mi-border-width) solid var(--mi-success); border-bottom: var(--mi-border-width) solid var(--mi-success); transform: rotate(-45deg); }
.at-node.is-failed, .at-node.is-rejected { border-left-color: var(--mi-danger); }
.at-node.is-failed .at-status, .at-node.is-rejected .at-status { color: var(--mi-danger); }
.at-node.is-failed .at-status::before, .at-node.is-rejected .at-status::before { border: var(--mi-border-width) solid var(--mi-danger); background: var(--mi-state-failed-stripe); }

/* 心跳第二信号：只加 outline 与徽标，绝不改四态的形状/边框语义。 */
.at-node.hb-stale { outline: var(--mi-border-width) dashed var(--mi-warning); outline-offset: -3px; }
.at-node.hb-dead { outline: calc(var(--mi-border-width) * 2) solid var(--mi-danger); outline-offset: -3px; }
.at-hb-flag { display: inline-flex; align-items: center; gap: var(--mi-space-1); flex: 0 0 auto; padding: 0 var(--mi-space-2); border: var(--mi-border-width) dashed var(--mi-warning); border-radius: var(--mi-radius-pill); color: var(--mi-warning); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); white-space: nowrap; }
.at-hb-flag::before { content: '!'; display: inline-grid; place-items: center; width: var(--mi-state-mark-size); height: var(--mi-state-mark-size); border: var(--mi-border-width) solid currentColor; border-radius: var(--mi-radius-pill); font-family: var(--mi-font-mono); }
.at-hb-flag.is-dead { border-style: solid; border-width: calc(var(--mi-border-width) * 2); color: var(--mi-danger); }
.at-hb-flag.is-dead::before { content: '×'; border-radius: var(--mi-radius-xs); }

.at-hb-series { display: flex; flex-wrap: wrap; align-items: center; gap: var(--mi-space-2); margin-top: var(--mi-space-3); padding: var(--mi-space-2) var(--mi-space-3); border-left: var(--mi-focus-width) solid var(--mi-accent-dim); background: var(--mi-bg-deep); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.at-hb-series-label { color: var(--mi-text-muted); }
.at-hb-beat { display: inline-flex; flex-direction: column; gap: 1px; padding: 1px var(--mi-space-2); border: var(--mi-border-width) solid var(--mi-border-subtle); border-radius: var(--mi-radius-xs); font-family: var(--mi-font-mono); color: var(--mi-text); }
.at-hb-beat small { color: var(--mi-text-muted); font-family: var(--mi-font-mono); }

.at-alerts .at-alert { padding: var(--mi-space-3) 0; border-top: var(--mi-border-width) solid var(--mi-border-subtle); }
.at-alert strong { color: var(--mi-danger); font-size: var(--mi-fs-body); }
.at-alert p { margin: var(--mi-space-1) 0; color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.at-alert small { display: block; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); overflow-wrap: anywhere; }
.at-hb-config { margin-top: var(--mi-space-4); padding-top: var(--mi-space-3); border-top: var(--mi-border-width) solid var(--mi-border-subtle); }
.at-hb-config small { display: block; margin: var(--mi-space-1) 0 var(--mi-space-2); color: var(--mi-warning); font-size: var(--mi-fs-caption); }

.at-caps { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--mi-space-2); margin-top: var(--mi-space-3); }
.at-cap { display: flex; flex-direction: column; gap: var(--mi-space-1); padding: var(--mi-space-2); border: var(--mi-border-width) solid var(--mi-border-subtle); border-radius: var(--mi-radius-control); background: var(--mi-bg-deep); color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.at-cap b { color: var(--mi-text); font-size: var(--mi-fs-secondary); font-weight: var(--mi-weight-medium); }
.at-cap small { overflow-wrap: anywhere; }
.at-cap.is-on { border-color: var(--mi-accent-dim); }
.at-cap.is-off { border-color: var(--mi-warning); }
.at-cap.is-off b { color: var(--mi-warning); }

.at-task { margin: var(--mi-space-3) 0 0; color: var(--mi-text-secondary); font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); white-space: pre-wrap; overflow-wrap: anywhere; }
.at-card-meta { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: var(--mi-space-1) var(--mi-space-3); margin: var(--mi-space-3) 0 0; font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.at-card-meta dt { color: var(--mi-text-muted); }
.at-card-meta dd { margin: 0; overflow-wrap: anywhere; color: var(--mi-text-secondary); }
.at-card-conclusion { margin: var(--mi-space-2) 0 0; padding-left: var(--mi-space-3); border-left: var(--mi-focus-width) solid var(--mi-success); color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); white-space: pre-wrap; overflow-wrap: anywhere; }
.at-card-error { margin: var(--mi-space-2) 0 0; padding-left: var(--mi-space-3); border-left: var(--mi-focus-width) solid var(--mi-danger); color: var(--mi-danger); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); white-space: pre-wrap; overflow-wrap: anywhere; }
.at-artifacts { margin: var(--mi-space-2) 0 0; padding-left: var(--mi-space-5); color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.at-artifacts li { overflow-wrap: anywhere; }

.at-chain { margin-top: var(--mi-space-4); border-top: var(--mi-border-width) solid var(--mi-border-subtle); }
.at-chain > summary { padding: var(--mi-space-2) 0; color: var(--mi-text); font-size: var(--mi-fs-secondary); cursor: pointer; }
.at-chain > summary small { margin-left: var(--mi-space-2); color: var(--mi-text-muted); font-size: var(--mi-fs-caption); }
.at-chain > ol { display: grid; gap: var(--mi-space-3); margin: var(--mi-space-3) 0 0; padding: 0; list-style: none; }
.at-chain > ol > li { display: grid; grid-template-columns: var(--mi-space-7) minmax(0, 1fr); gap: var(--mi-space-3); }
.at-chain-num { color: var(--mi-accent-dim); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); }
.at-chain strong { font-size: var(--mi-fs-secondary); font-weight: var(--mi-weight-medium); }
.at-chain p { margin: var(--mi-space-1) 0; color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); white-space: pre-wrap; overflow-wrap: anywhere; }
.at-chain small { display: block; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); overflow-wrap: anywhere; }
.at-chain code { font-family: var(--mi-font-mono); }
.at-chain-missing { color: var(--mi-warning) !important; }
.at-chain-list { margin: var(--mi-space-1) 0; padding-left: var(--mi-space-4); color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.at-chain-list li { overflow-wrap: anywhere; }

.at-result { margin: var(--mi-space-3) 0; color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.at-limits { padding: var(--mi-space-5) 0; color: var(--mi-text-muted); }
.at-limits strong { color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); font-weight: var(--mi-weight-medium); }
.at-limits p { max-width: var(--mi-copy-width); margin: var(--mi-space-2) 0; font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.at-limits code { display: block; overflow-wrap: anywhere; color: var(--mi-text-muted); font: var(--mi-fs-caption)/var(--mi-lh-caption) var(--mi-font-mono); }
.at-message { display: flex; justify-content: space-between; gap: var(--mi-space-3); margin-top: var(--mi-space-3); padding-left: var(--mi-space-3); border-left: var(--mi-focus-width) solid var(--mi-danger); color: var(--mi-danger); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.at-message button { padding: 0; border: 0; background: transparent; color: var(--mi-text); cursor: pointer; }
.at-state { display: flex; flex-direction: column; align-items: flex-start; justify-content: center; min-height: var(--mi-pre-height); padding: var(--mi-space-6); border-block: var(--mi-border-width) solid var(--mi-border-subtle); background: var(--mi-bg-deep); }
.at-state-mark { width: var(--mi-space-6); height: var(--mi-space-6); margin-bottom: var(--mi-space-4); border: var(--mi-border-width) solid var(--mi-accent-dim); border-radius: var(--mi-radius-lg); box-shadow: inset 0 0 0 var(--mi-space-2) var(--mi-bg-deep), inset 0 0 0 var(--mi-space-3) var(--mi-accent); }
.at-state.is-permission .at-state-mark { border-color: var(--mi-warning); box-shadow: inset 0 0 0 var(--mi-space-2) var(--mi-bg-deep), inset 0 0 0 var(--mi-space-3) var(--mi-warning); }
.at-state strong { font-size: var(--mi-fs-title); line-height: var(--mi-lh-title); font-weight: var(--mi-weight-medium); }
.at-state p { max-width: var(--mi-copy-width); margin: var(--mi-space-2) 0 0; color: var(--mi-text-secondary); font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); }
.at-state button { margin-top: var(--mi-space-4); }
.at-state-embedded { min-height: var(--mi-graph-height-compact); margin-top: var(--mi-space-4); }
@keyframes at-status-turn { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  .at, .at * { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
}
</style>
