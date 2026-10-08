<script setup lang="ts">
// 阶段 3｜AI 工具流画布 —— trace 回看：
// 把每一轮问答（/api/trace 的一条记录）画成纵向流水线：
//   提问 → AI 决策 → 调用工具（检索/改码/分区/试玩/联网…）→ … → AI 组织回答 → 结果
// 每个节点显示真实元数据（耗时 / 输入输出规模 / 成败 / token / 花费），失败节点标红，
// 点节点看右侧详情。trace 按隐私设计只存元数据（不存提问正文与文件路径），这里不伪造跳转。
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import type { Node, Edge } from '@vue-flow/core'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import { harnessApi, type TraceItem, type TraceStep } from '../api'
import { demoMode, demoTraceItems } from '../composables/demo'
import { useWorkbench } from '../composables/workbench'
import { costLabel } from '../composables/costLabel'

const { flowOpen, closeFlow } = useWorkbench()
const { fitView } = useVueFlow()

// --------------------------------------------------------------- 动作元数据
// 后端真实工具名 → 中文类别/名称（与 tools.py TOOLS 注册表对齐；未知动作不编造，原样展示）
type CatKey = 'retrieve' | 'write' | 'region' | 'run' | 'web' | 'mcp' | 'asset' | 'bug' | 'task' | 'agent' | 'other'
const CAT_META: Record<CatKey, { text: string; glyph: string; color: string }> = {
  retrieve: { text: '检索', glyph: '检', color: '#2f6fed' },
  write: { text: '改码', glyph: '写', color: '#b3561f' },
  region: { text: '分区/Git', glyph: '区', color: '#7a4fd1' },
  run: { text: '运行校验', glyph: '运', color: '#128053' },
  web: { text: '联网', glyph: '网', color: '#0e8a8f' },
  mcp: { text: '引擎连接器', glyph: 'M', color: '#5a67d8' },
  asset: { text: '素材', glyph: '素', color: '#c4761a' },
  bug: { text: 'Bug', glyph: 'B', color: '#d23b42' },
  task: { text: '任务数值', glyph: '务', color: '#5a6778' },
  agent: { text: '多智能体', glyph: '派', color: '#9333ea' },
  other: { text: '其他', glyph: '?', color: '#8a94a6' },
}
const ACTION_META: Record<string, { cat: CatKey; label: string }> = {
  search_code: { cat: 'retrieve', label: '语义搜代码' },
  read_file: { cat: 'retrieve', label: '读取文件' },
  grep: { cat: 'retrieve', label: '关键字搜索' },
  list_dir: { cat: 'retrieve', label: '列出目录' },
  search_knowledge: { cat: 'retrieve', label: '知识库检索' },
  game_impact: { cat: 'retrieve', label: '影响面分析' },
  read_external_file: { cat: 'retrieve', label: '读取外部文件' },
  apply_edit: { cat: 'write', label: '应用代码修改' },
  create_file: { cat: 'write', label: '新建文件' },
  dev_region_edit: { cat: 'write', label: '分区内改码' },
  dev_refactor: { cat: 'write', label: '跨区重构' },
  create_external_file: { cat: 'write', label: '新建外部文件' },
  edit_external_file: { cat: 'write', label: '修改外部文件' },
  delete_external_file: { cat: 'write', label: '删除文件' },
  init_regions: { cat: 'region', label: '初始化分区' },
  dev_list_regions: { cat: 'region', label: '查看分区' },
  dev_region_read: { cat: 'region', label: '读取分区' },
  dev_region_verify: { cat: 'region', label: '校验分区改动' },
  dev_commit: { cat: 'region', label: '提交变更集' },
  dev_commit_all: { cat: 'region', label: '全部提交' },
  dev_verify_contracts: { cat: 'region', label: '契约校验' },
  dev_rebuild_index: { cat: 'region', label: '重建索引' },
  dev_list_changesets: { cat: 'region', label: '变更集列表' },
  dev_rollback_changeset: { cat: 'region', label: '回滚变更集' },
  dev_propose_regions: { cat: 'region', label: '生成分区方案' },
  dev_apply_regions: { cat: 'region', label: '应用分区' },
  dev_add_region: { cat: 'region', label: '新增分区' },
  dev_approve: { cat: 'region', label: '审批' },
  dev_approval_status: { cat: 'region', label: '审批状态' },
  run_command: { cat: 'run', label: '运行命令' },
  game_playtest: { cat: 'run', label: '试玩验证' },
  game_validate_data: { cat: 'run', label: '配置格式校验' },
  game_release_check: { cat: 'run', label: '发布前检查' },
  python_exec: { cat: 'run', label: '执行 Python' },
  web_research: { cat: 'web', label: '联网研究' },
  web_fetch: { cat: 'web', label: '读取网页' },
  web_search: { cat: 'web', label: '联网搜索' },
  web_subtitles: { cat: 'web', label: '读取视频字幕' },
  dev_http_request: { cat: 'web', label: 'HTTP 请求' },
  dev_mcp_call: { cat: 'mcp', label: '调用引擎连接器' },
  dev_list_connectors: { cat: 'mcp', label: '连接器列表' },
  dev_route_connector: { cat: 'mcp', label: '选择连接器' },
  dev_list_connector_tools: { cat: 'mcp', label: '连接器工具清单' },
  search_assets: { cat: 'asset', label: '搜索素材' },
  dev_asset_get: { cat: 'asset', label: '获取素材' },
  dev_asset_register: { cat: 'asset', label: '素材登记入库' },
  dev_capture_bug: { cat: 'bug', label: '抓取 Bug' },
  dev_list_bugs: { cat: 'bug', label: 'Bug 列表' },
  dev_update_bug: { cat: 'bug', label: '更新 Bug' },
  game_upsert_task: { cat: 'task', label: '开发任务' },
  game_simulate: { cat: 'task', label: '数值模拟' },
  calculate: { cat: 'task', label: '计算' },
  gen_video_prompt: { cat: 'task', label: '生成视频提示词' },
  delegate: { cat: 'agent', label: '委派子智能体' },
  orchestrate: { cat: 'agent', label: '编排多智能体' },
}
function actionMeta(action: string) {
  return ACTION_META[action] || { cat: 'other' as CatKey, label: action }
}

// --------------------------------------------------------------- 状态
const traces = ref<TraceItem[]>([])
const loading = ref(false)
const errorMsg = ref('')
const selectedTurnId = ref('')
const selectedNodeId = ref<string | null>(null)
const filterMode = ref<'all' | 'issue'>('all')
const filterText = ref('')

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    if (demoMode.value) {
      traces.value = [...demoTraceItems]
    } else {
      const r = await harnessApi.trace(60)
      traces.value = r.items || []
    }
    if (!selectedTurnId.value || !traces.value.some(t => t.turn_id === selectedTurnId.value)) {
      selectedTurnId.value = traces.value.length ? traces.value[traces.value.length - 1].turn_id : ''
    }
    selectedNodeId.value = null
    void refreshFit()
  } catch (e) {
    errorMsg.value = (e as Error).message || '操作记录加载失败'
  } finally {
    loading.value = false
  }
}
// immediate：组件在首次打开时才由 App 异步挂载，挂载即 open=true，需立即加载
watch(flowOpen, (v) => { if (v) void load() }, { immediate: true })

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && flowOpen.value) closeFlow()
}
window.addEventListener('keydown', onKey)
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

// --------------------------------------------------------------- 回合列表
const turnsNewFirst = computed(() => [...traces.value].reverse())
const filteredTurns = computed(() => {
  const q = filterText.value.trim().toLowerCase()
  return turnsNewFirst.value.filter((t) => {
    if (filterMode.value === 'issue' && !isIssueTurn(t)) return false
    if (!q) return true
    const hay = [t.provider, t.model, t.outcome, t.session_id, ...(t.steps || []).map(s => s.action)].join(' ').toLowerCase()
    return hay.includes(q)
  })
})
function isIssueTurn(t: TraceItem) {
  return !!(t.aborted || t.error || (t.steps || []).some(s => s.ok === false))
}
function turnStatus(t: TraceItem) {
  if (t.aborted) return { text: '停止', cls: 'stop' }
  if (t.error) return { text: '出错', cls: 'err' }
  if (t.outcome === 'completed') return { text: '完成', cls: 'ok' }
  return { text: t.outcome || '完成', cls: 'warn' }
}
function fmtTime(iso: string) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
function fmtMs(ms: number) {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function money(v: number) {
  // 同 SettingsView：0 元时不猜「免费」，只回金额；解释交给带 pricing_state 的
  // 供应商表。逐轮 trace 里没有那个字段，硬猜就是假信息。
  return costLabel(v)
}
function selectTurn(id: string) {
  if (selectedTurnId.value === id) return
  selectedTurnId.value = id
  selectedNodeId.value = null
  void refreshFit()
}
async function refreshFit() {
  await nextTick()
  await nextTick()
  try { fitView({ padding: 0.25, duration: 200 }) } catch { /* 节点为空时忽略 */ }
}

const selectedTurn = computed(() => traces.value.find(t => t.turn_id === selectedTurnId.value) || null)

// --------------------------------------------------------------- 构图
type FlNodeData = {
  kind: 'start' | 'think' | 'tool' | 'end'
  turn: TraceItem
  step?: TraceStep
  index?: number
  total?: number
}
const W_TOOL = 300, W_LLM = 200, W_TERM = 300
const H_START = 96, H_LLM = 52, H_TOOL = 104, H_END = 108, GAP = 26

const graphModel = computed<{ nodes: Node<FlNodeData>[]; edges: Edge[] }>(() => {
  const t = selectedTurn.value
  if (!t) return { nodes: [], edges: [] }
  const nodes: Node<FlNodeData>[] = []
  const seq: string[] = []
  let y = 0
  const push = (id: string, type: string, w: number, h: number, data: FlNodeData) => {
    nodes.push({ id, type, position: { x: -w / 2, y }, data, draggable: false, selectable: false })
    seq.push(id)
    y += h + GAP
  }
  push('start', 'fl-start', W_TERM, H_START, { kind: 'start', turn: t })
  const steps = t.steps || []
  const hasLLM = (t.llm_calls || 0) > 0
  steps.forEach((s, i) => {
    if (hasLLM) push(`think${i}`, 'fl-think', W_LLM, H_LLM, { kind: 'think', turn: t, index: i + 1, total: steps.length + 1 })
    push(`step${i}`, 'fl-tool', W_TOOL, H_TOOL, { kind: 'tool', turn: t, step: s, index: i, total: steps.length })
  })
  if (hasLLM) push('thinkFinal', 'fl-think', W_LLM, H_LLM, { kind: 'think', turn: t, index: steps.length + 1, total: steps.length + 1 })
  push('end', 'fl-end', W_TERM, H_END, { kind: 'end', turn: t })

  const edgeColor = (id: string) => {
    if (id.startsWith('step')) {
      const i = Number(id.slice(4))
      return steps[i] && steps[i].ok === false ? '#e0525a' : undefined
    }
    if (id === 'end') return t.error ? '#e0525a' : t.aborted ? '#e0a13a' : undefined
    return undefined
  }
  const edges: Edge[] = []
  for (let i = 1; i < seq.length; i++) {
    const c = edgeColor(seq[i])
    edges.push({
      id: `e${i}`, source: seq[i - 1], target: seq[i],
      type: 'smoothstep', style: c ? { stroke: c, strokeWidth: 2 } : undefined,
    })
  }
  return { nodes, edges }
})

const nodes = computed(() => graphModel.value.nodes)
const edges = computed(() => graphModel.value.edges)

function onNodeClick(p: { node: Node }) {
  selectedNodeId.value = p.node.id
}
const selectedDetail = computed<FlNodeData | null>(() => {
  if (!selectedNodeId.value) return null
  return graphModel.value.nodes.find(n => n.id === selectedNodeId.value)?.data ?? null
})

// 节点卡内展示用小工具
function stepMeta(s: TraceStep) { return actionMeta(s.action) }
function endStatus(t: TraceItem) {
  if (t.aborted) return { text: '手动停止', cls: 'stop', glyph: '⏸', desc: '这一轮在完成前被你手动停止。' }
  if (t.error) return { text: '出错中断', cls: 'err', glyph: '!', desc: '执行过程中发生错误，未生成最终回答。' }
  if (t.outcome === 'max_steps') return { text: '步数用尽', cls: 'warn', glyph: '⌛', desc: '工具调用达到上限后被迫收尾。' }
  if (t.outcome === 'completed') return { text: '完成回答', cls: 'ok', glyph: '✓', desc: 'AI 已基于工具结果给出最终回答。' }
  return { text: t.outcome || '完成', cls: 'warn', glyph: '?', desc: `结局标记：${t.outcome || '无'}` }
}
function providerLabel(p: string) {
  if (!p || p === '?') return '测试/离线'
  return p
}
</script>

<template>
  <div v-if="flowOpen" class="fl-overlay wb-modal-backdrop" @mousedown.self="closeFlow">
    <div class="fl-panel wb-modal-shell" role="dialog" aria-modal="true" aria-label="AI 工作流">
      <!-- 头部 -->
      <div class="fl-head">
        <div class="fl-title">
          <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
            <circle cx="3.5" cy="3" r="1.6" fill="none" stroke="#2f6fed" stroke-width="1.1" />
            <rect x="1.8" y="7.4" width="3.4" height="2.4" rx="0.6" fill="none" stroke="#b3561f" stroke-width="1.1" />
            <circle cx="12.5" cy="12.5" r="1.6" fill="none" stroke="#128053" stroke-width="1.1" />
            <path d="M3.5 4.6 V7.4 M3.5 9.8 C3.5 11.4 6 11 8 11.6 C10 12.2 11 11.4 11.2 10.9" fill="none" stroke="#93a0b5" stroke-width="1" stroke-dasharray="2 2" />
          </svg>
          <b>AI 工作流</b>
          <span>每一轮问答的操作流水线：提问 → 思考 → 调工具 → 回答（耗时 / 成败 / token 全记录）</span>
        </div>
        <div class="fl-head-actions">
          <button class="fl-iconbtn" title="重新加载" @click="load">
            <svg width="13" height="13" viewBox="0 0 13 13"><path d="M11 2.8 V5.4 H8.4 M2.2 7.2 A4.6 4.6 0 1 0 3 4.1 L11 5.4" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <button class="fl-iconbtn" title="关闭（Esc）" @click="closeFlow">×</button>
        </div>
      </div>

      <div v-if="demoMode" class="fl-demo">示例演示模式：以下为演示流水线。真实使用时，你在 AI 助手里的每轮问答都会自动记录并画成图。</div>
      <p v-if="errorMsg" class="fl-err">{{ errorMsg }} <button class="fl-link" @click="load">重试</button></p>

      <div class="fl-body">
        <!-- 左：回合列表 -->
        <aside class="fl-turns">
          <div class="fl-filter">
            <div class="fl-seg">
              <button :class="{ on: filterMode === 'all' }" @click="filterMode = 'all'">全部 {{ traces.length }}</button>
              <button :class="{ on: filterMode === 'issue' }" @click="filterMode = 'issue'">异常</button>
            </div>
            <input v-model="filterText" placeholder="筛选模型 / 工具名" />
          </div>
          <div v-if="loading" class="fl-hint">加载中…</div>
          <div v-else-if="!filteredTurns.length" class="fl-hint">
            还没有操作记录。<br />到 AI 助手问一个问题或让它改代码，这里会自动画出流水线。
          </div>
          <div v-else class="fl-turn-list">
            <button
              v-for="t in filteredTurns"
              :key="t.turn_id"
              class="fl-turn"
              :class="{ on: t.turn_id === selectedTurnId }"
              @click="selectTurn(t.turn_id)"
            >
              <div class="fl-turn-row">
                <span class="fl-turn-time">{{ fmtTime(t.ts) }}</span>
                <span :class="['fl-dot', turnStatus(t).cls]" :title="turnStatus(t).text" />
              </div>
              <div class="fl-turn-model">{{ providerLabel(t.provider) }}<template v-if="t.model"> · {{ t.model }}</template></div>
              <div class="fl-turn-meta">
                <span>{{ (t.steps || []).length }} 步操作</span>
                <span>{{ t.total_tokens.toLocaleString() }} tok</span>
                <span>{{ fmtMs(t.elapsed_ms) }}</span>
              </div>
              <div v-if="isIssueTurn(t)" class="fl-turn-issue">
                {{ t.aborted ? '已停止' : t.error ? '出错' : '含失败步骤' }}
              </div>
            </button>
          </div>
        </aside>

        <!-- 中：流水线画布 -->
        <section class="fl-canvas-wrap">
          <div v-if="selectedTurn" class="fl-canvas-head">
            <span class="fl-chip model">{{ providerLabel(selectedTurn.provider) }}<template v-if="selectedTurn.model"> · {{ selectedTurn.model }}</template></span>
            <span class="fl-chip">{{ (selectedTurn.steps || []).length }} 个工具调用</span>
            <span class="fl-chip">{{ selectedTurn.llm_calls }} 次模型思考 · {{ fmtMs(selectedTurn.llm_ms) }}</span>
            <span class="fl-chip">{{ selectedTurn.total_tokens.toLocaleString() }} token（入 {{ selectedTurn.prompt_tokens.toLocaleString() }} / 出 {{ selectedTurn.completion_tokens.toLocaleString() }}）</span>
            <span v-if="selectedTurn.task_budget?.limit" class="fl-chip" :class="{ cache: selectedTurn.task_budget.exhausted }">
              任务预算 {{ (selectedTurn.task_budget.total_tokens || 0).toLocaleString() }} / {{ selectedTurn.task_budget.limit.toLocaleString() }}
            </span>
            <span v-if="selectedTurn.cache_read_tokens" class="fl-chip cache">缓存命中 {{ selectedTurn.cache_read_tokens.toLocaleString() }}</span>
            <span class="fl-chip">花费 {{ money(selectedTurn.cost_cny) }}</span>
            <span class="fl-chip">总耗时 {{ fmtMs(selectedTurn.elapsed_ms) }}</span>
          </div>
          <div v-if="selectedTurn" class="fl-canvas">
            <VueFlow
              :nodes="nodes"
              :edges="edges"
              :min-zoom="0.25"
              :max-zoom="1.6"
              :pan-on-scroll="false"
              :zoom-on-scroll="true"
              :fit-view-on-init="true"
              @node-click="onNodeClick"
            >
              <Background :gap="18" :size="1.2" pattern-color="#cfd8e6" />
              <Controls position="bottom-right" :show-interactive="false" />

              <!-- 起点：提问 -->
              <template #node-fl-start="{ data, id }">
                <div :class="['fl-node', 'fl-n-start', { sel: selectedNodeId === id }]">
                  <div class="fl-n-head"><span class="fl-n-glyph start">问</span><b>用户提问</b></div>
                  <div class="fl-n-sub">问题 {{ data.turn.question_chars }} 字 · 上下文 {{ data.turn.messages_count }} 条消息</div>
                  <div class="fl-n-sub faint">会话 {{ data.turn.session_id }} · 出于隐私不记录问题原文</div>
                </div>
              </template>

              <!-- AI 思考（决策点） -->
              <template #node-fl-think="{ data, id }">
                <div :class="['fl-node', 'fl-n-think', { sel: selectedNodeId === id }]">
                  <span class="fl-think-dot" />
                  <b>AI 思考{{ data.total && data.total > 1 ? ` ${data.index}/${data.total}` : '' }}</b>
                  <span class="fl-n-sub">分析结果，决定下一步工具</span>
                </div>
              </template>

              <!-- 工具步骤 -->
              <template #node-fl-tool="{ data, id }">
                <div :class="['fl-node', 'fl-n-tool', { fail: data.step && data.step.ok === false, sel: selectedNodeId === id }]">
                  <div class="fl-n-head">
                    <span
                      class="fl-n-glyph"
                      :style="{ background: (CAT_META[stepMeta(data.step!).cat].color) + '1f', color: CAT_META[stepMeta(data.step!).cat].color, borderColor: CAT_META[stepMeta(data.step!).cat].color + '55' }"
                    >{{ CAT_META[stepMeta(data.step!).cat].glyph }}</span>
                    <b>{{ stepMeta(data.step!).label }}</b>
                    <span class="fl-n-idx">{{ (data.index ?? 0) + 1 }}/{{ data.total }}</span>
                    <span v-if="data.step && data.step.ok === false" class="fl-n-fail">失败</span>
                  </div>
                  <div class="fl-n-sub mono">{{ data.step!.action }}</div>
                  <div class="fl-n-stats">
                    <span>入 {{ data.step!.arg_chars ?? 0 }} 字</span>
                    <span>出 {{ data.step!.obs_chars ?? 0 }} 字</span>
                    <span>{{ fmtMs(data.step!.latency_ms || 0) }}</span>
                    <span :class="['fl-n-ok', data.step!.ok === false ? 'bad' : 'good']">{{ data.step!.ok === false ? '未成功' : '成功' }}</span>
                  </div>
                </div>
              </template>

              <!-- 终点：结果 -->
              <template #node-fl-end="{ data, id }">
                <div :class="['fl-node', 'fl-n-end', endStatus(data.turn).cls, { sel: selectedNodeId === id }]">
                  <div class="fl-n-head">
                    <span class="fl-n-glyph end" :class="endStatus(data.turn).cls">{{ endStatus(data.turn).glyph }}</span>
                    <b>{{ endStatus(data.turn).text }}</b>
                  </div>
                  <div class="fl-n-sub">{{ endStatus(data.turn).desc }}</div>
                  <div class="fl-n-sub faint">回答 {{ data.turn.final_chars }} 字 · finish: {{ data.turn.finish_reason || data.turn.outcome }}</div>
                </div>
              </template>
            </VueFlow>
          </div>
          <div v-else class="fl-canvas fl-empty-canvas">
            <span>选择左侧一轮记录查看流水线</span>
          </div>
        </section>

        <!-- 右：节点详情 -->
        <aside class="fl-detail">
          <template v-if="selectedDetail">
            <h4>节点详情</h4>
            <template v-if="selectedDetail.kind === 'start'">
              <p class="fl-d-line"><label>类型</label><span>用户提问（回合起点）</span></p>
              <p class="fl-d-line"><label>时间</label><span>{{ fmtTime(selectedDetail.turn.ts) }}</span></p>
              <p class="fl-d-line"><label>会话</label><span class="mono">{{ selectedDetail.turn.session_id }}</span></p>
              <p class="fl-d-line"><label>模型</label><span>{{ providerLabel(selectedDetail.turn.provider) }} · {{ selectedDetail.turn.model || '—' }}</span></p>
              <p class="fl-d-line"><label>问题长度</label><span>{{ selectedDetail.turn.question_chars }} 字</span></p>
              <p class="fl-d-line"><label>上下文</label><span>{{ selectedDetail.turn.messages_count }} 条消息</span></p>
            </template>
            <template v-else-if="selectedDetail.kind === 'think'">
              <p class="fl-d-line"><label>类型</label><span>模型决策点</span></p>
              <p class="fl-d-plain">每次调用工具前后，模型都会基于已有观测决定下一步：选哪个工具、传什么参数。</p>
              <p class="fl-d-line"><label>本回合模型调用</label><span>{{ selectedDetail.turn.llm_calls }} 次 · 共 {{ fmtMs(selectedDetail.turn.llm_ms) }}</span></p>
              <p class="fl-d-line"><label>本回合 token</label><span>入 {{ selectedDetail.turn.prompt_tokens.toLocaleString() }} / 出 {{ selectedDetail.turn.completion_tokens.toLocaleString() }}</span></p>
            </template>
            <template v-else-if="selectedDetail.kind === 'tool' && selectedDetail.step">
              <p class="fl-d-line"><label>工具</label><span>{{ stepMeta(selectedDetail.step).label }}</span></p>
              <p class="fl-d-line"><label>分类</label><span>{{ CAT_META[stepMeta(selectedDetail.step).cat].text }}</span></p>
              <p class="fl-d-line"><label>动作名</label><span class="mono">{{ selectedDetail.step.action }}</span></p>
              <p class="fl-d-line"><label>步骤序号</label><span>第 {{ (selectedDetail.index ?? 0) + 1 }} / {{ selectedDetail.turn.steps?.length || 0 }} 步</span></p>
              <p class="fl-d-line"><label>参数规模</label><span>{{ selectedDetail.step.arg_chars ?? 0 }} 字符（不记录原文）</span></p>
              <p class="fl-d-line"><label>返回规模</label><span>{{ selectedDetail.step.obs_chars ?? 0 }} 字符（不记录原文）</span></p>
              <p class="fl-d-line"><label>耗时</label><span>{{ fmtMs(selectedDetail.step.latency_ms || 0) }}</span></p>
              <p class="fl-d-line"><label>结果</label>
                <span :class="selectedDetail.step.ok === false ? 'fl-bad' : 'fl-good'">{{ selectedDetail.step.ok === false ? '未成功（AI 会看到失败原因并调整）' : '成功' }}</span>
              </p>
            </template>
            <template v-else-if="selectedDetail.kind === 'end'">
              <p class="fl-d-line"><label>结局</label><span>{{ endStatus(selectedDetail.turn).text }}</span></p>
              <p class="fl-d-line"><label>finish_reason</label><span class="mono">{{ selectedDetail.turn.finish_reason || '—' }}</span></p>
              <p class="fl-d-line"><label>回答长度</label><span>{{ selectedDetail.turn.final_chars }} 字</span></p>
              <p class="fl-d-line"><label>总耗时</label><span>{{ fmtMs(selectedDetail.turn.elapsed_ms) }}</span></p>
              <p class="fl-d-line"><label>token</label><span>入 {{ selectedDetail.turn.prompt_tokens.toLocaleString() }} / 出 {{ selectedDetail.turn.completion_tokens.toLocaleString() }}</span></p>
              <p v-if="selectedDetail.turn.cache_read_tokens" class="fl-d-line"><label>缓存命中</label><span>{{ selectedDetail.turn.cache_read_tokens.toLocaleString() }} token（按折扣价计费，已避免全价重复计）</span></p>
              <p class="fl-d-line"><label>花费</label><span>{{ money(selectedDetail.turn.cost_cny) }}（本地模型为 ¥0）</span></p>
              <p v-if="selectedDetail.turn.error" class="fl-d-line"><label>错误</label><span class="fl-bad">{{ selectedDetail.turn.error }}</span></p>
            </template>
            <p class="fl-d-privacy">trace 只记录操作元数据（动作名 / 字数 / 耗时 / 成败），不记录问题、代码与文件路径原文。</p>
          </template>
          <div v-else class="fl-detail-empty">
            点击流水线中的任意节点<br />查看这一步的详细数据
          </div>
        </aside>
      </div>
    </div>
  </div>
</template>

<style scoped>
.fl-overlay {
  position: fixed; inset: 0; z-index: 90;
  background: rgba(38, 52, 77, 0.38);
  backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center;
  padding: 22px;
  animation: fl-fade .18s ease-out both;
}
.fl-panel {
  width: min(1340px, 98vw);
  height: min(880px, 94vh);
  background: #f4f6fa;
  border: 1px solid #d3dcea;
  border-radius: 14px;
  box-shadow: 0 22px 60px rgba(31, 45, 72, .28);
  display: flex; flex-direction: column; overflow: hidden; animation: fl-rise .22s cubic-bezier(.2,.8,.2,1) both;
}
.fl-head {
  flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; background: #fff; border-bottom: 1px solid #e2e8f1;
}
.fl-title { display: flex; align-items: center; gap: 8px; min-width: 0; }
.fl-title b { font-size: 14px; color: #1b2433; white-space: nowrap; }
.fl-title span { font-size: 12px; color: #93a0b5; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fl-head-actions { display: flex; gap: 6px; flex: 0 0 auto; }
.fl-iconbtn {
  width: 28px; height: 28px; border: 1px solid #d3dcea; background: #fff; border-radius: 7px;
  color: #5a6778; cursor: pointer; font-size: 16px; line-height: 1;
  display: inline-flex; align-items: center; justify-content: center; transition: color .15s ease, border-color .15s ease, background .15s ease, transform .15s ease;
}
.fl-iconbtn:hover { border-color: #9fb0c6; background: #f5f8fc; color: #23304a; transform: translateY(-1px); }.fl-iconbtn:focus-visible { outline: 2px solid #2f6fed; outline-offset: 1px; }
.fl-demo {
  flex: 0 0 auto; margin: 10px 16px 0; padding: 7px 11px; border-radius: 8px;
  font-size: 12px; line-height: 1.6; color: #8a5a16; background: #fdf2e0; border: 1px solid #f0d29a;
}
.fl-err { flex: 0 0 auto; margin: 10px 16px 0; font-size: 12px; color: #d23b42; }
.fl-link { border: none; background: none; color: #2f6fed; cursor: pointer; font: inherit; padding: 0; }
.fl-body { flex: 1 1 auto; min-height: 0; display: flex; }

/* 左：回合列表 */
.fl-turns {
  flex: 0 0 248px; display: flex; flex-direction: column; min-height: 0;
  background: #fff; border-right: 1px solid #e2e8f1;
}
.fl-filter { padding: 10px; border-bottom: 1px solid #eef1f6; display: flex; flex-direction: column; gap: 8px; }
.fl-seg { display: flex; background: #f3f6fb; border-radius: 8px; padding: 3px; gap: 2px; }
.fl-seg button {
  flex: 1; border: none; background: transparent; font: inherit; font-size: 12px; font-weight: 600;
  color: #5a6778; padding: 5px 0; border-radius: 6px; cursor: pointer;
}
.fl-seg button.on { background: #fff; color: #2f6fed; box-shadow: 0 1px 4px rgba(35,52,84,.12); }
.fl-filter input {
  padding: 6px 9px; border: 1px solid #d3dcea; border-radius: 7px; font: inherit; font-size: 12px;
  color: #23304a; background: #f7f9fc; outline: none;
}
.fl-filter input:focus { border-color: #2f6fed; background: #fff; }
.fl-turn-list { flex: 1 1 auto; overflow: auto; padding: 8px; display: flex; flex-direction: column; gap: 7px; }
.fl-hint { padding: 22px 14px; font-size: 12px; line-height: 1.8; color: #93a0b5; text-align: center; }
.fl-turn {
  text-align: left; border: 1px solid #e2e8f1; background: #fafbfd; border-radius: 10px;
  padding: 8px 10px; cursor: pointer; font: inherit; display: flex; flex-direction: column; gap: 3px; transition: border-color .15s ease, background .15s ease, transform .15s ease, box-shadow .15s ease;
}
.fl-turn:hover { border-color: #b9c9e2; background: #f6f9ff; transform: translateX(2px); box-shadow: 0 3px 9px rgba(35,52,84,.08); }.fl-turn:focus-visible { outline: 2px solid #2f6fed; outline-offset: 1px; }
.fl-turn.on { border-color: #8fb2f2; background: #f0f6ff; box-shadow: 0 0 0 2px rgba(47,111,237,.10); }
.fl-turn-row { display: flex; align-items: center; justify-content: space-between; }
.fl-turn-time { font-size: 10px; color: #7a869a; font-variant-numeric: tabular-nums; }
.fl-dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; }
.fl-dot.ok { background: #2fbf7e; }
.fl-dot.err { background: #e0525a; }
.fl-dot.stop { background: #e0a13a; }
.fl-dot.warn { background: #e0a13a; }
.fl-turn-model { font-size: 12px; font-weight: 600; color: #46536a; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fl-turn-meta { display: flex; gap: 8px; font-size: 10px; color: #93a0b5; font-variant-numeric: tabular-nums; }
.fl-turn-issue { font-size: 10px; font-weight: 700; color: #c0434a; }

/* 中：画布 */
.fl-canvas-wrap { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; min-height: 0; }
.fl-canvas-head {
  flex: 0 0 auto; padding: 8px 12px; display: flex; flex-wrap: wrap; gap: 6px;
  background: #eef2f8; border-bottom: 1px solid #e2e8f1;
}
.fl-chip {
  font-size: 10px; color: #46536a; background: #fff; border: 1px solid #dbe2ee;
  border-radius: 999px; padding: 2px 9px; font-variant-numeric: tabular-nums; white-space: nowrap; transition: border-color .15s ease, background .15s ease;
}
.fl-chip.model { color: #7a4fd1; border-color: #d9c9f5; background: #f8f4ff; font-weight: 600; }
.fl-chip.cache { color: var(--green); border-color: #bfe6d2; background: #eefaf3; font-weight: 600; }
.fl-canvas { flex: 1 1 auto; min-height: 0; position: relative; }
.fl-empty-canvas { display: flex; align-items: center; justify-content: center; color: #93a0b5; font-size: 13px; }

/* Vue Flow 自定义节点 */
.fl-node {
  background: #fff; border: 1.5px solid #d3dcea; border-radius: 12px;
  padding: 10px 12px; box-shadow: 0 3px 10px rgba(31, 45, 72, .08); transition: box-shadow .16s ease, transform .16s ease, border-color .16s ease;
}
.fl-node:hover { transform: translateY(-1px); box-shadow: 0 8px 18px rgba(31,45,72,.14); }
.fl-n-head { display: flex; align-items: center; gap: 8px; }
.fl-n-head b { font-size: 12px; color: #1b2433; }
.fl-n-glyph {
  width: 22px; height: 22px; border-radius: 7px; border: 1px solid; flex: 0 0 auto;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 700;
}
.fl-n-glyph.start { background: #eaf1fe; color: #2f6fed; border-color: #c8dcfa; }
.fl-n-glyph.end.ok { background: #e6f7ee; color: #128053; border-color: #b6e6c8; }
.fl-n-glyph.end.err { background: #fdecec; color: #d23b42; border-color: #f3c4c7; }
.fl-n-glyph.end.stop, .fl-n-glyph.end.warn { background: #fdf2e0; color: #b3741a; border-color: #f0d8a8; }
.fl-n-idx { margin-left: auto; font-size: 10px; color: #93a0b5; font-variant-numeric: tabular-nums; }
.fl-n-fail {
  font-size: 10px; font-weight: 700; color: #fff; background: #e0525a;
  border-radius: 999px; padding: 1px 8px;
}
.fl-n-sub { margin-top: 5px; font-size: 10px; color: #5a6778; line-height: 1.5; }
.fl-n-sub.faint { color: #9aa5b8; }
.fl-n-sub.mono { font-family: var(--font-mono, ui-monospace, Consolas, monospace); font-size: 10px; color: #7a869a; }
.fl-n-stats { margin-top: 7px; display: flex; flex-wrap: wrap; gap: 5px; }
.fl-n-stats > span {
  font-size: 10px; color: #5a6778; background: #f3f6fb; border: 1px solid #e2e8f1;
  border-radius: 5px; padding: 1px 7px; font-variant-numeric: tabular-nums;
}
.fl-n-ok.good { color: #128053 !important; background: #e6f7ee !important; border-color: #b6e6c8 !important; }
.fl-n-ok.bad { color: #b32d33 !important; background: #fdecec !important; border-color: #f3c4c7 !important; font-weight: 700; }
.fl-n-tool.fail { border-color: #ef9ba0; box-shadow: 0 3px 12px rgba(224,82,90,.18); }
.fl-n-think {
  display: flex; align-items: center; gap: 8px; padding: 8px 12px;
  border-style: dashed; border-color: #b9c9e2; background: #f7faff;
}
.fl-n-think b { font-size: 12px; color: #3a5684; }
.fl-n-think .fl-n-sub { margin-top: 0; }
.fl-think-dot {
  width: 8px; height: 8px; border-radius: 50%; background: #6d93d8; flex: 0 0 auto;
  box-shadow: 0 0 0 4px rgba(109,147,216,.18);
}
.fl-n-start { border-color: #b9cdf2; background: linear-gradient(180deg, #f8fbff, #f2f7ff); }
.fl-n-end.ok { border-color: #9ed8b5; background: linear-gradient(180deg, #f7fef9, #effaf3); }
.fl-n-end.err { border-color: #ef9ba0; background: linear-gradient(180deg, #fff7f7, #fdeeee); }
.fl-n-end.stop, .fl-n-end.warn { border-color: #ecc98f; background: linear-gradient(180deg, #fffdf7, #fdf6e7); }
.vue-flow__node { cursor: pointer; }
.fl-node.sel { outline: 2px solid rgba(47,111,237,.55); outline-offset: 2px; }

/* 右：详情 */
.fl-detail {
  flex: 0 0 268px; background: #fff; border-left: 1px solid #e2e8f1;
  padding: 14px 14px 18px; overflow: auto;
}
.fl-detail h4 { margin: 0 0 12px; font-size: 13px; color: #1b2433; }
.fl-d-line { display: flex; gap: 8px; margin: 0 0 10px; font-size: 12px; line-height: 1.6; }
.fl-d-line label { flex: 0 0 62px; color: #93a0b5; }
.fl-d-line span { flex: 1 1 auto; color: #334055; word-break: break-all; }
.fl-d-plain { margin: 0 0 10px; font-size: 11px; line-height: 1.7; color: #7a869a; }
.fl-d-privacy { margin-top: 14px; font-size: 10px; line-height: 1.7; color: #a2adbf; border-top: 1px dashed #e2e8f1; padding-top: 10px; }
.fl-good { color: #128053; }
.fl-bad { color: #d23b42; }
.fl-detail-empty { color: #93a0b5; font-size: 12px; line-height: 1.9; text-align: center; margin-top: 80px; }
.mono { font-family: var(--font-mono, ui-monospace, Consolas, monospace); }
@keyframes fl-fade { from { opacity: 0; } to { opacity: 1; } }
@keyframes fl-rise { from { opacity: 0; transform: translateY(8px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }

@media (max-width: 1080px) {
  .fl-turns { flex-basis: 208px; }
  .fl-detail { flex-basis: 228px; }
}
@media (max-width: 860px) {
  .fl-overlay { padding: 0; }
  .fl-panel { width: 100vw; height: 100vh; border-radius: 0; }
  .fl-detail { display: none; }
  .fl-turns { flex-basis: 176px; }
  .fl-title span { display: none; }
}
@media (max-width: 560px) { .fl-head { padding: 10px 11px; }.fl-turns { flex-basis: 154px; }.fl-turn-list { padding: 6px; }.fl-canvas-head { max-height: 90px; overflow: auto; padding: 7px 9px; }.fl-n-sub.faint { display: none; } }
@media (prefers-reduced-motion: reduce) { .fl-overlay, .fl-panel, .fl-iconbtn, .fl-turn, .fl-chip, .fl-node { animation: none; transition: none; } }
</style>
