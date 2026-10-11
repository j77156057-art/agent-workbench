<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { agentApi } from '../api'
import type { PreviewAdapterInfo, WorkflowAcceptanceReport, WorkflowState } from '../api'

const props = defineProps<{ workflow: WorkflowState }>()
const emit = defineEmits<{ (e: 'activity'): void }>()
const adapters = ref<PreviewAdapterInfo[]>([])
const configured = ref<string[]>([])
const report = ref<WorkflowAcceptanceReport | null>(null)
const selected = ref<string[]>([])
const loading = ref(false)
const error = ref('')
const rollbackBusy = ref(false)
const decisionBusy = ref('')

const changedFiles = computed(() => report.value?.preview?.changes?.files || props.workflow.preview?.changes?.files || [])
const hasExecution = computed(() => !!(props.workflow.steps || typeof props.workflow.review?.ok === 'boolean' || props.workflow.timeline?.some(event => event.kind === 'execute_start')))
const evaluation = computed(() => hasExecution.value ? report.value?.evaluation : null)

async function load() {
  loading.value = true
  error.value = ''
  const [catalog, acceptance] = await Promise.allSettled([
    agentApi.previewAdapters(),
    hasExecution.value ? agentApi.workflowAcceptanceReport(props.workflow.workflow_id) : Promise.resolve({ ok: true, report: null }),
  ])
  if (catalog.status === 'fulfilled' && catalog.value.ok) {
    adapters.value = catalog.value.adapters || []
    const profileAdapters = props.workflow.project_profile?.preview_adapters || []
    configured.value = profileAdapters.length ? [...profileAdapters] : adapters.value.filter(item => item.available && !item.requires_connector).map(item => item.id)
  }
  if (acceptance.status === 'fulfilled' && acceptance.value.ok) report.value = acceptance.value.report || null
  if ((catalog.status === 'rejected' || (catalog.status === 'fulfilled' && !catalog.value.ok)) &&
      (acceptance.status === 'rejected' || (acceptance.status === 'fulfilled' && !acceptance.value.ok))) {
    error.value = '适配器或验收报告暂时无法读取'
  }
  loading.value = false
}

function toggleAdapter(id: string) {
  configured.value = configured.value.includes(id)
    ? configured.value.filter(item => item !== id)
    : [...configured.value, id]
}

function approveMessage(item: PreviewAdapterInfo) {
  const hash = item.source_sha256 ? `\n代码哈希：${item.source_sha256.slice(0, 16)}…` : ''
  return `确认激活适配器“${item.label || item.id}”？${hash}\n请先查看下方源码和差异。`
}

async function saveConfig() {
  const result = await agentApi.configurePreviewAdapters(configured.value)
  if (!result.ok) error.value = result.error || '适配器配置保存失败'
  else emit('activity')
}

async function decideAdapter(item: PreviewAdapterInfo, approved: boolean) {
  if (decisionBusy.value || !item.generated || item.status !== 'pending') return
  if (approved && !window.confirm(approveMessage(item))) return
  decisionBusy.value = item.id
  error.value = ''
  try {
    const result = await agentApi.decidePreviewAdapter(item.id, approved, item.source_sha256 || '')
    if (!result.ok) throw new Error(result.error || '适配器审批失败')
    await load()
    emit('activity')
  } catch (cause) {
    error.value = (cause as Error).message || '适配器审批失败'
  } finally {
    decisionBusy.value = ''
  }
}

function togglePath(path: string) {
  selected.value = selected.value.includes(path)
    ? selected.value.filter(item => item !== path)
    : [...selected.value, path]
}

async function rollback() {
  if (rollbackBusy.value || !selected.value.length || !window.confirm('只恢复选中的文件？此操作需要用户确认。')) return
  rollbackBusy.value = true
  error.value = ''
  try {
    const result = await agentApi.workflowProjectRollback(props.workflow.workflow_id, true, selected.value)
    if (!result.ok) throw new Error(result.error || '局部回滚失败')
    selected.value = []
    emit('activity')
    await load()
  } catch (cause) {
    error.value = (cause as Error).message || '局部回滚失败'
  } finally { rollbackBusy.value = false }
}

watch(() => props.workflow.workflow_id, () => { selected.value = []; void load() })
watch(hasExecution, (started, previous) => { if (started && !previous) void load() })
onMounted(() => { void load() })
</script>

<template>
  <section class="adapter-panel">
    <div class="adapter-head"><b>适配器与验收</b><small>当前项目范围</small><span v-if="loading">读取中…</span></div>
    <p v-if="error" class="adapter-error">{{ error }}</p>
    <div class="adapter-grid">
      <div v-for="item in adapters" :key="item.id" class="adapter-item">
        <input v-if="!item.requires_connector && (!item.generated || item.status === 'active')" type="checkbox" :checked="configured.includes(item.id)" @change="toggleAdapter(item.id)" />
        <div><b>{{ item.label || item.id }}</b><small>{{ item.evidence }}</small><small v-if="item.generated && item.runtime">代码：隔离 {{ item.runtime }} / {{ item.entrypoint || 'adapt' }}()</small><small v-if="item.generated && item.refresh_tool">刷新：{{ item.refresh_tool }}</small><small v-if="item.generated && item.validation?.length">验收：{{ item.validation.join('；') }}</small></div>
        <span :class="item.available ? 'on' : 'off'">{{ item.generated ? (item.status === 'pending' ? '待确认' : item.status === 'rejected' ? '已拒绝' : '已激活') : (item.available ? '可用' : '未连接') }}</span>
        <div v-if="item.generated && item.status === 'pending'" class="adapter-actions">
          <button type="button" :disabled="!!decisionBusy" @click="decideAdapter(item, true)">{{ decisionBusy === item.id ? '处理中…' : '激活' }}</button>
          <button type="button" :disabled="!!decisionBusy" @click="decideAdapter(item, false)">拒绝</button>
        </div>
        <details v-if="item.generated && item.source_preview" class="adapter-code">
          <summary>{{ item.code_diff ? '查看待审批代码与差异' : '查看适配器代码' }}</summary>
          <small v-if="item.source_sha256">SHA-256：{{ item.source_sha256 }}</small>
          <pre v-if="item.code_diff" class="adapter-diff">{{ item.code_diff }}<span v-if="item.code_diff_truncated">\n…差异已截断</span></pre>
          <pre>{{ item.source_preview }}<span v-if="item.source_truncated">\n…源码已截断</span></pre>
        </details>
      </div>
    </div>
    <button type="button" class="adapter-save" @click="saveConfig">保存项目预览配置</button>
    <div v-if="evaluation" class="acceptance-report">
      <div class="report-head"><b>真实验收报告</b><strong :class="evaluation.passed ? 'ok' : 'bad'">{{ Math.round((evaluation.score || 0) * 100) }}%</strong></div>
      <small>{{ evaluation.passed ? '检查通过' : '仍有检查未通过' }} · {{ evaluation.task_count || 0 }} 个任务 · {{ evaluation.steps || 0 }} 步</small>
      <details v-if="evaluation.checks?.length"><summary>查看检查项</summary><p v-for="(check, index) in evaluation.checks" :key="index" :class="check.ok ? 'ok' : 'bad'">{{ check.ok ? '✓' : '!' }} {{ check.name }}{{ check.detail ? '：' + check.detail : '' }}</p></details>
    </div>
    <div v-if="changedFiles.length" class="rollback-box">
      <div class="report-head"><b>按文件回滚</b><small>只恢复选中的快照文件</small></div>
      <label v-for="file in changedFiles" :key="file.path" class="rollback-file">
        <input type="checkbox" :checked="selected.includes(file.path || '')" :disabled="!file.path" @change="file.path && togglePath(file.path)" />
        <span>{{ file.path }}</span><small>{{ file.kind || 'modified' }}</small>
      </label>
      <button type="button" :disabled="rollbackBusy || !selected.length" @click="rollback">{{ rollbackBusy ? '恢复中…' : `恢复选中文件（${selected.length}）` }}</button>
    </div>
  </section>
</template>

<style scoped>
.adapter-panel { display: grid; gap: 8px; padding: 10px; border: 1px solid var(--border); border-radius: 9px; background: linear-gradient(180deg, var(--bg-hover), var(--bg-raised)); font-size: 11px; }
.adapter-head, .report-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.adapter-head small, .adapter-head span, .report-head small, .adapter-item small { color: var(--text-faint); }
.adapter-error { margin: 0; color: var(--danger); }
.adapter-grid { display: grid; gap: 5px; }
.adapter-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-raised); transition: border-color .16s ease, background .16s ease, box-shadow .16s ease; }
.adapter-item:hover { border-color: var(--border-strong); background: var(--bg-selected); box-shadow: 0 3px 10px rgba(35,52,84,.06); }
.adapter-item div { display: grid; gap: 2px; min-width: 0; }.adapter-item small { overflow-wrap: anywhere; }
.adapter-code { grid-column: 1 / -1; width: 100%; color: var(--text-muted); }.adapter-code summary { cursor: pointer; color: var(--accent); }.adapter-code small { display: block; margin-top: 4px; }.adapter-code pre { max-height: 180px; overflow: auto; margin: 5px 0 0; padding: 6px; white-space: pre-wrap; font: 10px/1.45 var(--font-mono); color: var(--text); background: var(--bg); border: 1px solid var(--border); border-radius: 5px; }.adapter-code .adapter-diff { color: var(--text); border-color: var(--accent); }
.adapter-item > span { flex: 0 0 auto; font-size: 10px; border: 1px solid currentColor; border-radius: 99px; padding: 1px 6px; }.on, .ok { color: var(--green); background: rgba(52,168,112,.05); }.off, .bad { color: var(--danger); background: rgba(214,78,78,.05); }
.acceptance-report, .rollback-box { display: grid; gap: 5px; border-top: 1px solid var(--border); padding-top: 8px; }
.acceptance-report > small { color: var(--text-muted); }.acceptance-report details { border-top: 1px solid var(--border); padding-top: 5px; }.acceptance-report p { margin: 4px 0 0; }
.report-head strong { font-size: 18px; font-variant-numeric: tabular-nums; }.rollback-file { display: flex; align-items: center; gap: 6px; padding: 4px 5px; border-radius: 5px; transition: background .15s ease; }.rollback-file:hover { background: var(--bg-selected); }.rollback-file span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.rollback-file small { color: var(--text-faint); }
.rollback-box button { justify-self: start; border: 1px solid var(--accent); border-radius: 6px; padding: 5px 8px; color: var(--accent-ink); background: var(--accent); cursor: pointer; font: inherit; transition: transform .15s ease, box-shadow .15s ease, opacity .15s ease; }.rollback-box button:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 3px 8px rgba(37,96,212,.18); }.rollback-box button:disabled { opacity: .5; cursor: default; }
.adapter-save { justify-self: start; border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px; color: var(--text); background: var(--bg-raised); cursor: pointer; font: inherit; transition: color .15s ease, border-color .15s ease, transform .15s ease; }.adapter-save:hover { color: var(--accent); border-color: var(--accent); transform: translateY(-1px); }
@media (prefers-reduced-motion: reduce) { .adapter-item, .rollback-file, .rollback-box button, .adapter-save { transition: none; } }
</style>
