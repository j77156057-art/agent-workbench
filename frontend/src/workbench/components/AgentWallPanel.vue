<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { agentsApi } from '../api'
import type { AgentCard, AgentConversationResp, AgentMemoryResp, AgentUsageResp } from '../api'
import { closeAgentWall, openAgentContext } from '../uiPanels'
import SubagentChatWindow from './SubagentChatWindow.vue'

const props = defineProps<{ visible: boolean; sessionId: string }>()

type TabKey = 'conversation' | 'memory' | 'usage'
const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'conversation', label: '对话记录' },
  { key: 'memory', label: '记忆' },
  { key: 'usage', label: '使用记录' },
]

const cards = ref<AgentCard[]>([])
const rosterError = ref('')
const loading = ref(false)
const selectedId = ref('main')
const tab = ref<TabKey>('conversation')

const conversation = ref<AgentConversationResp | null>(null)
const memory = ref<AgentMemoryResp | null>(null)
const usage = ref<AgentUsageResp | null>(null)
const detailLoading = ref(false)
const detailError = ref('')

// 子 agent 的独立对话窗：窗口里是 ChatDock（scope=replay），不是这里另写的渲染。
const chatOpen = ref(false)
const chatSessionId = ref('')
const chatTitle = ref('')
const chatEmptyHint = ref('')
const chatSource = ref('')

const selected = computed(() => cards.value.find(card => card.agent_id === selectedId.value) || null)
const memoryCount = computed(() => memory.value?.count ?? 0)
const conversationCount = computed(() => conversation.value?.messages?.length ?? 0)
const usageCount = computed(() => usage.value?.count ?? 0)

function fmtTime(value?: string): string {
  if (!value) return '未记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function fmtDuration(ms: number | null | undefined): string {
  if (typeof ms !== 'number' || ms < 0) return '未记录'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`
}

function typeLabel(type: string): string {
  return { search: '搜索', skill: 'Skill', mcp: 'MCP', tool: '工具' }[type] || type
}

function agentLabel(id?: string): string {
  if (!id || id === 'main') return '平台助手'
  return id.length > 11 ? `${id.slice(0, 11)}…` : id
}

function statusClass(status?: string): string {
  if (!status) return ''
  if (/失败|失败|error|failed/i.test(status)) return 'is-bad'
  if (/成功|ok|完成|active|运行中/i.test(status)) return 'is-good'
  return ''
}

async function loadRoster() {
  loading.value = true
  rosterError.value = ''
  try {
    const data = await agentsApi.roster(props.sessionId)
    cards.value = Array.isArray(data.items) ? data.items : []
    if (!cards.value.some(card => card.agent_id === selectedId.value)) {
      selectedId.value = cards.value[0]?.agent_id || 'main'
    }
  } catch (e) {
    rosterError.value = (e as Error).message || 'Agent 列表加载失败'
    cards.value = []
  } finally {
    loading.value = false
  }
}

async function loadDetail() {
  if (!props.visible) return
  detailLoading.value = true
  detailError.value = ''
  try {
    const tasks: Promise<unknown>[] = []
    conversation.value = null
    memory.value = null
    usage.value = null
    tasks.push(agentsApi.conversation(selectedId.value, props.sessionId)
      .then(data => { conversation.value = data }).catch(e => { detailError.value = (e as Error).message }))
    tasks.push(agentsApi.memory(selectedId.value, props.sessionId)
      .then(data => { memory.value = data }).catch(e => { detailError.value = (e as Error).message }))
    tasks.push(agentsApi.usage(selectedId.value, props.sessionId)
      .then(data => { usage.value = data }).catch(e => { detailError.value = (e as Error).message }))
    await Promise.all(tasks)
  } finally {
    detailLoading.value = false
  }
}

function selectCard(card: AgentCard) {
  if (selectedId.value === card.agent_id) return
  selectedId.value = card.agent_id
  tab.value = 'conversation'
  void loadDetail()
}

// 点开子 agent 卡片 → 独立对话窗（复用 ChatDock 回放）。
// 归档来源决定空态措辞：旧数据没有 sub- 会话，如实说「历史未归档」，不补造对话。
function openChat(card: AgentCard) {
  if (!card || card.is_main) return
  chatSessionId.value = card.chat_session_id || ''
  chatTitle.value = card.name || card.agent_id
  chatSource.value = card.archive_source || ''
  chatEmptyHint.value = card.archive_source === 'sessions'
    ? '该子 agent 还没有落库的逐轮记录。'
    : '历史未归档：该子 agent 运行于旧版本，逐轮记录没有落库；这里只能回看任务与结论。'
  chatOpen.value = true
}

function onCardClick(card: AgentCard) {
  selectCard(card)
  // 主 agent 保持原有行为（下方详情面板）；子 agent 另弹独立对话窗。
  if (!card.is_main) openChat(card)
}

function openFullContext() {
  const card = selected.value
  openAgentContext(card?.agent_id || selectedId.value, card?.name || '平台助手')
}

watch(() => props.visible, visible => {
  if (visible) {
    void loadRoster().then(() => loadDetail())
  }
}, { immediate: true })

watch(() => props.sessionId, () => { if (props.visible) void loadRoster().then(() => loadDetail()) })
</script>

<template>
  <Transition name="aw-panel">
    <section v-if="visible" class="aw" role="dialog" aria-label="Agent 工作台">
      <header class="aw-head">
        <div class="aw-identity">
          <span class="aw-mark" aria-hidden="true"><i /><i /><i /></span>
          <div>
            <strong>Agent 工作台</strong>
            <span>卡片墙 · 会话 {{ sessionId || '未记录' }}</span>
          </div>
        </div>
        <button class="aw-close" type="button" aria-label="关闭" @click="closeAgentWall">×</button>
      </header>

      <div v-if="rosterError" class="aw-error">{{ rosterError }}</div>

      <div class="aw-cards" :class="{ 'is-loading': loading }">
        <button
          v-for="card in cards"
          :key="card.agent_id"
          type="button"
          class="aw-card"
          :class="{ on: card.agent_id === selectedId }"
          :title="card.agent_id"
          @click="onCardClick(card)"
        >
          <span class="aw-card-top">
            <span class="aw-card-name">{{ card.name }}</span>
            <span class="aw-chip" :class="statusClass(card.status)">{{ card.status_label || card.status }}</span>
          </span>
          <span class="aw-card-role">{{ card.role_label || card.role }}{{ card.is_main ? ' · 主 agent' : ' · 委派 agent' }}</span>
          <span class="aw-card-meta">
            <span>消息 {{ card.message_count }}</span>
            <span v-if="!card.is_main" class="aw-archive" :class="{ 'is-legacy': card.archive_source !== 'sessions' }">
              {{ card.archive_source === 'sessions' ? '已归档' : '未归档' }}
            </span>
            <span>最近 {{ fmtTime(card.last_active) }}</span>
          </span>
        </button>
        <div v-if="!loading && !cards.length && !rosterError" class="aw-empty">还没有任何 Agent 记录。</div>
      </div>

      <div v-if="selected" class="aw-detail">
        <div class="aw-detail-head">
          <div>
            <strong>{{ selected.name }}</strong>
            <span>{{ selected.status_label || selected.status }} · 会话 {{ selected.session_id || '未记录' }}</span>
          </div>
          <button v-if="selected.is_main" type="button" class="aw-link" @click="openFullContext">打开完整上下文面板</button>
          <template v-else>
            <button type="button" class="aw-link" @click="openChat(selected)">打开独立对话窗口</button>
            <span class="aw-source">记忆已按 agent 隔离，六 Tab 上下文面板仅对平台助手开放</span>
          </template>
        </div>

        <div class="aw-tabs">
          <button
            v-for="item in TABS"
            :key="item.key"
            type="button"
            class="aw-tab"
            :class="{ on: tab === item.key }"
            @click="tab = item.key"
          >
            {{ item.label }}
            <span v-if="item.key === 'conversation'">{{ conversationCount }}</span>
            <span v-else-if="item.key === 'memory'">{{ memoryCount }}</span>
            <span v-else>{{ usageCount }}</span>
          </button>
        </div>

        <div class="aw-body">
          <p v-if="detailError" class="aw-error">{{ detailError }}</p>
          <p v-else-if="detailLoading" class="aw-hint">正在读取持久化数据…</p>

          <template v-if="!detailLoading">
            <!-- 对话记录 -->
            <template v-if="tab === 'conversation'">
              <div v-if="conversation?.messages?.length" class="aw-thread">
                <div
                  v-for="(message, index) in conversation.messages"
                  :key="`${message.turn_id || index}-${message.role}`"
                  class="aw-msg"
                  :class="message.role === 'user' ? 'is-user' : 'is-assistant'"
                >
                  <div class="aw-msg-meta">
                    <span>{{ message.role === 'user' ? (message.kind === 'delegated_task' ? '委派任务' : '用户') : (message.kind === 'conclusion' ? '结论' : '平台助手') }}</span>
                    <span>{{ fmtTime(message.ts) }}</span>
                  </div>
                  <div class="aw-msg-text">{{ message.text }}</div>
                </div>
              </div>
              <div v-else class="aw-empty">
                <strong>没有可展示的对话</strong>
                <span>{{ conversation?.note || '服务端没有该 Agent 的对话记录。' }}</span>
                <span v-if="conversation?.source" class="aw-source">数据源：{{ conversation.source }}</span>
              </div>
            </template>

            <!-- 记忆 -->
            <template v-else-if="tab === 'memory'">
              <div class="aw-summary">
                <span>总计 <strong>{{ memory?.count ?? 0 }}</strong></span>
                <span>活跃 <strong>{{ memory?.active_count ?? 0 }}</strong></span>
                <span>历史未分类 <strong>{{ memory?.legacy_unclassified_count ?? 0 }}</strong></span>
                <span>归属维度 <code>{{ memory?.source || 'agent_id' }}</code></span>
              </div>
              <div v-if="memory?.items?.length" class="aw-mem-list">
                <article v-for="row in memory.items" :key="row.id" class="aw-mem">
                  <div class="aw-mem-head">
                    <strong>{{ row.title || row.id }}</strong>
                    <span v-if="row.legacy_unclassified" class="aw-badge">历史未分类</span>
                    <span class="aw-mem-kind">{{ row.kind }}</span>
                  </div>
                  <p v-if="row.content" class="aw-mem-body">{{ row.content }}</p>
                  <div class="aw-mem-foot">
                    <span>{{ row.status }}</span>
                    <span>{{ fmtTime(row.updated) }}</span>
                    <span>{{ row.source || '未记录' }}</span>
                  </div>
                </article>
              </div>
              <div v-else class="aw-empty">
                <strong>该 Agent 暂无独立记忆</strong>
                <span>记忆库按 <code>agent_id</code> 过滤，没有归属到 {{ selectedId }} 的条目。</span>
                <span class="aw-source">{{ memory?.note }}</span>
              </div>
              <p v-if="memory?.note" class="aw-note">{{ memory.note }}</p>
            </template>

            <!-- 使用记录 -->
            <template v-else>
              <div class="aw-summary">
                <span>搜索 <strong>{{ usage?.counts?.search ?? 0 }}</strong></span>
                <span>Skill <strong>{{ usage?.counts?.skill ?? 0 }}</strong></span>
                <span>MCP <strong>{{ usage?.counts?.mcp ?? 0 }}</strong></span>
                <span>工具 <strong>{{ usage?.counts?.tool ?? 0 }}</strong></span>
              </div>
              <div v-if="usage?.items?.length" class="aw-usage">
                <div class="aw-usage-head">
                  <span>类型</span><span>名称 / 入参</span><span>Agent</span><span>状态</span><span>耗时</span><span>时间</span>
                </div>
                <div v-for="(row, index) in usage.items" :key="`${row.ts}-${row.name}-${index}`" class="aw-usage-row">
                  <span class="aw-usage-type">{{ typeLabel(row.type) }}</span>
                  <span class="aw-usage-name">
                    <strong>{{ row.name || '未记录' }}</strong>
                    <em>{{ row.summary || '入参未记录' }}</em>
                  </span>
                  <span class="aw-usage-agent" :title="row.agent_id || ''">{{ agentLabel(row.agent_id) }}</span>
                  <span :class="statusClass(row.status)">{{ row.status || '未记录' }}</span>
                  <span>{{ fmtDuration(row.duration_ms) }}</span>
                  <span>{{ fmtTime(row.ts) }}</span>
                </div>
              </div>
              <div v-else class="aw-empty">
                <strong>没有工具调用记录</strong>
                <span>该 Agent 在此会话内没有可归集的搜索 / Skill / MCP / 工具调用。</span>
              </div>
              <p v-if="usage?.note" class="aw-note">{{ usage.note }}</p>
              <p v-if="usage?.source" class="aw-note">数据源：{{ usage.source }}</p>
            </template>
          </template>
        </div>
      </div>
    </section>
  </Transition>

  <!-- 子 agent 独立对话窗：弹在卡片墙之上，内容是真的 ChatDock 回放 -->
  <SubagentChatWindow
    :open="chatOpen"
    :session-id="chatSessionId"
    :title="chatTitle"
    :empty-hint="chatEmptyHint"
    :source="chatSource"
    @close="chatOpen = false"
  />
</template>

<style scoped>
.aw { position: fixed; inset: 0 0 0 auto; z-index: var(--mi-z-panel); width: min(720px, 100vw); display: flex; flex-direction: column; overflow: hidden; border-left: var(--mi-border-width) solid var(--mi-border); background: var(--mi-bg); box-shadow: var(--mi-shadow-panel); color: var(--mi-text); color-scheme: dark; font-family: var(--mi-font-ui); font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); isolation: isolate; }
.aw *, .aw *::before, .aw *::after { box-sizing: border-box; }
.aw-panel-leave-active { transition: transform var(--mi-duration-slow) var(--mi-ease-enter), opacity var(--mi-duration-base) var(--mi-ease-standard); }
.aw-panel-leave-to { transform: translateX(100%); opacity: 0; }
.aw-head { display: flex; align-items: center; gap: var(--mi-space-4); min-height: var(--mi-header-height); padding: 0 var(--mi-space-5); border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); background: var(--mi-bg); }
.aw-identity { display: flex; align-items: center; gap: var(--mi-space-3); flex: 1; min-width: 0; }
.aw-identity > div { display: flex; flex-direction: column; min-width: 0; }
.aw-identity strong { font-size: var(--mi-fs-title); line-height: var(--mi-lh-title); font-weight: var(--mi-weight-semibold); }
.aw-identity span:last-child { overflow: hidden; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); text-overflow: ellipsis; white-space: nowrap; }
.aw-mark { position: relative; flex: 0 0 var(--mi-space-6); width: var(--mi-space-6); height: var(--mi-space-6); }
.aw-mark i { position: absolute; inset: 0; border: var(--mi-border-width) solid var(--mi-border-strong); border-radius: var(--mi-radius-lg); }
.aw-mark i:nth-child(2) { inset: var(--mi-space-2); border-color: var(--mi-accent-dim); }
.aw-mark i:nth-child(3) { inset: var(--mi-space-3); border: 0; background: var(--mi-accent); box-shadow: var(--mi-glow-node); }
.aw-close { flex: 0 0 var(--mi-icon-button); width: var(--mi-icon-button); height: var(--mi-icon-button); padding: 0; border: var(--mi-border-width) solid transparent; border-radius: var(--mi-radius-control); background: transparent; color: var(--mi-text-secondary); font-size: var(--mi-fs-title); line-height: 1; cursor: pointer; }
.aw-close:hover { border-color: var(--mi-border); background: var(--mi-hover); color: var(--mi-text); }
.aw-cards { display: flex; gap: var(--mi-space-3); flex: 0 0 auto; overflow-x: auto; padding: var(--mi-space-4) var(--mi-space-5); border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); scrollbar-color: var(--mi-border-strong) transparent; }
.aw-cards.is-loading { opacity: .55; }
.aw-card { display: flex; flex-direction: column; gap: var(--mi-space-1); flex: 0 0 210px; min-height: 92px; padding: var(--mi-space-3); border: var(--mi-border-width) solid var(--mi-border); border-radius: var(--mi-radius-control); background: var(--mi-surface); color: var(--mi-text); text-align: left; cursor: pointer; transition: border-color var(--mi-duration-base) var(--mi-ease-standard), background var(--mi-duration-base) var(--mi-ease-standard); }
.aw-card:hover { border-color: var(--mi-border-strong); background: var(--mi-hover); }
.aw-card.on { border-color: var(--mi-accent); background: var(--mi-surface-level-3); box-shadow: var(--mi-shadow-level-2); }
.aw-card-top { display: flex; align-items: center; justify-content: space-between; gap: var(--mi-space-2); }
.aw-card-name { overflow: hidden; font-weight: var(--mi-weight-semibold); font-size: var(--mi-fs-secondary); text-overflow: ellipsis; white-space: nowrap; }
.aw-card-role { color: var(--mi-text-muted); font-size: var(--mi-fs-caption); }
.aw-card-meta { display: flex; flex-direction: column; gap: 2px; margin-top: auto; color: var(--mi-text-muted); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); font-variant-numeric: tabular-nums; }
.aw-archive { color: var(--mi-text-muted); }
.aw-archive.is-legacy { color: #c98a2e; }
.aw-chip { flex: 0 0 auto; padding: 0 var(--mi-space-2); border: var(--mi-border-width) solid var(--mi-border); border-radius: var(--mi-radius-lg); color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.aw-chip.is-good, .is-good { color: var(--mi-success); }
.aw-chip.is-bad, .is-bad { color: var(--mi-danger); }
.aw-detail { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
.aw-detail-head { display: flex; align-items: center; justify-content: space-between; gap: var(--mi-space-3); padding: var(--mi-space-3) var(--mi-space-5) 0; }
.aw-detail-head > div { display: flex; flex-direction: column; min-width: 0; }
.aw-detail-head strong { font-size: var(--mi-fs-title); line-height: var(--mi-lh-title); }
.aw-detail-head span { overflow: hidden; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); text-overflow: ellipsis; white-space: nowrap; }
.aw-link { flex: 0 0 auto; padding: 0 var(--mi-space-3); min-height: var(--mi-control-height-sm); border: var(--mi-border-width) solid var(--mi-border-level-3); border-radius: var(--mi-radius-control); background: var(--mi-surface-level-3); color: var(--mi-text); font-size: var(--mi-fs-caption); cursor: pointer; }
.aw-link:hover { border-color: var(--mi-accent-dim); }
.aw-tabs { display: flex; flex-wrap: wrap; gap: var(--mi-space-2) var(--mi-space-5); padding: var(--mi-space-2) var(--mi-space-5) 0; border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); }
.aw-tab { position: relative; min-height: var(--mi-space-7); padding: 0; border: 0; background: transparent; color: var(--mi-text-muted); font-size: var(--mi-fs-secondary); cursor: pointer; }
.aw-tab::after { content: ''; position: absolute; right: 0; bottom: 0; left: 0; height: var(--mi-focus-width); background: var(--mi-accent); transform: scaleX(0); transform-origin: left; transition: transform var(--mi-duration-base) var(--mi-ease-standard); }
.aw-tab.on { color: var(--mi-text); }
.aw-tab.on::after { transform: scaleX(1); }
.aw-tab span { margin-left: var(--mi-space-1); color: var(--mi-text-muted); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); font-variant-numeric: tabular-nums; }
.aw-body { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: var(--mi-space-4) var(--mi-space-5) var(--mi-space-6); scrollbar-color: var(--mi-border-strong) transparent; }
.aw-error { margin: var(--mi-space-3) var(--mi-space-5) 0; padding: var(--mi-space-2) var(--mi-space-3); border-left: var(--mi-focus-width) solid var(--mi-danger); background: var(--mi-surface); color: var(--mi-danger); font-size: var(--mi-fs-secondary); }
.aw-hint { color: var(--mi-text-muted); font-size: var(--mi-fs-secondary); }
.aw-empty { display: flex; flex-direction: column; gap: var(--mi-space-2); padding: var(--mi-space-6) 0; color: var(--mi-text-muted); font-size: var(--mi-fs-secondary); }
.aw-empty strong { color: var(--mi-text); font-size: var(--mi-fs-body); }
.aw-source { font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); }
.aw-thread { display: flex; flex-direction: column; gap: var(--mi-space-3); }
.aw-msg { padding: var(--mi-space-3); border: var(--mi-border-width) solid var(--mi-border); border-left: var(--mi-focus-width) solid var(--mi-border-strong); border-radius: var(--mi-radius-control); background: var(--mi-surface); }
.aw-msg.is-user { border-left-color: var(--mi-accent-dim); }
.aw-msg.is-assistant { border-left-color: var(--mi-success); }
.aw-msg-meta { display: flex; justify-content: space-between; gap: var(--mi-space-2); color: var(--mi-text-muted); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); margin-bottom: var(--mi-space-2); }
.aw-msg-text { white-space: pre-wrap; word-break: break-word; color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.aw-summary { display: flex; flex-wrap: wrap; gap: var(--mi-space-4); margin-bottom: var(--mi-space-4); color: var(--mi-text-muted); font-size: var(--mi-fs-caption); }
.aw-summary strong { color: var(--mi-text); font-family: var(--mi-font-mono); font-variant-numeric: tabular-nums; }
.aw-summary code { color: var(--mi-accent); font-family: var(--mi-font-mono); }
.aw-mem-list { display: flex; flex-direction: column; gap: var(--mi-space-2); }
.aw-mem { padding: var(--mi-space-3); border: var(--mi-border-width) solid var(--mi-border); border-radius: var(--mi-radius-control); background: var(--mi-surface); }
.aw-mem-head { display: flex; align-items: center; gap: var(--mi-space-2); }
.aw-mem-head strong { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--mi-fs-secondary); }
.aw-badge { padding: 0 var(--mi-space-2); border: var(--mi-border-width) solid var(--mi-border-level-3); border-radius: var(--mi-radius-lg); color: var(--mi-text-muted); font-size: var(--mi-fs-caption); }
.aw-mem-kind { color: var(--mi-text-muted); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); }
.aw-mem-body { margin: var(--mi-space-2) 0 0; color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); word-break: break-word; }
.aw-mem-foot { display: flex; gap: var(--mi-space-3); margin-top: var(--mi-space-2); color: var(--mi-text-muted); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); }
.aw-usage { display: flex; flex-direction: column; border-top: var(--mi-border-width) solid var(--mi-border-subtle); }
.aw-usage-head, .aw-usage-row { display: grid; grid-template-columns: 52px minmax(0, 1fr) 88px 56px 72px 116px; gap: var(--mi-space-2); align-items: center; padding: var(--mi-space-2) 0; border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); }
.aw-usage-head { color: var(--mi-text-muted); font-size: var(--mi-fs-caption); }
.aw-usage-row { font-size: var(--mi-fs-caption); font-variant-numeric: tabular-nums; }
.aw-usage-type { color: var(--mi-accent); }
.aw-usage-name { display: flex; flex-direction: column; min-width: 0; }
.aw-usage-name strong { overflow: hidden; color: var(--mi-text); font-size: var(--mi-fs-secondary); text-overflow: ellipsis; white-space: nowrap; }
.aw-usage-name em { overflow: hidden; color: var(--mi-text-muted); font-style: normal; text-overflow: ellipsis; white-space: nowrap; }
.aw-usage-agent { overflow: hidden; color: var(--mi-text-secondary); font-family: var(--mi-font-mono); text-overflow: ellipsis; white-space: nowrap; }
.aw-note { margin: var(--mi-space-3) 0 0; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
</style>
