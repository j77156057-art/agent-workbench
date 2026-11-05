<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { askConfirm, askPrompt } from '../composables/dialogs'
import { memoryPanelApi } from '../api'
import MemoryOverview from './MemoryOverview.vue'
import type {
  DiskRetentionRunResult,
  DiskRetentionState,
  MemoryContextResult,
  MemoryHealth,
  MemoryInspectResult,
  MemoryPanelHook,
  MemoryPanelItem,
  MemoryPanelTaskItem,
  MemoryPinnedEntry,
} from '../api'

const props = withDefaults(defineProps<{ visible: boolean; sessionId: string; agentId?: string; agentLabel?: string }>(), { agentId: 'main', agentLabel: '平台助手' })
const emit = defineEmits<{ (e: 'close'): void }>()

const mode = ref<'next' | 'last'>('next')
const data = ref<MemoryContextResult | null>(null)
const inspection = ref<MemoryInspectResult | null>(null)
const active = ref('overview')
const pinnedFacts = ref<MemoryPinnedEntry[]>([])
const pinnedError = ref('')
let loadSequence = 0
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const health = ref<MemoryHealth | null>(null)
const healthLoading = ref(false)
const healthBusy = ref(false)
const healthError = ref('')
const healthNotice = ref('')

const layers = computed(() => data.value?.layers || [])
const current = computed(() => layers.value.find(layer => layer.key === active.value) || layers.value[0] || null)
const totalChars = computed(() => data.value?.total_chars || 0)
const permissionError = computed(() => /401|403|forbidden|unauthorized|无权限|权限不足/i.test(error.value))
/** 子 Agent 的独立上下文只存在于进程内实例；快照仍在但实例不在时，必须给空状态，
 *  不能拿主 Agent 或上一个 Agent 的 inspection 冒充。 */
const agentScoped = computed(() => props.agentId !== 'main')
const agentUnavailable = computed(() => agentScoped.value && !loading.value
  && (inspection.value?.available === false || (!inspection.value && !!error.value)))
const agentUnavailableReason = computed(() => {
  if (inspection.value?.available === false) {
    return inspection.value.error
      || '该 Agent 在任务树里有快照，但当前会话没有活动的进程内实例；独立上下文不随快照持久化，重启后无法恢复。'
  }
  if (/不存在该 Agent 实例|404|not found/i.test(error.value)) {
    return '该 Agent 在任务树里有快照，但当前会话没有活动的进程内实例；独立上下文不随快照持久化，重启后无法恢复。'
  }
  return error.value || '该 Agent 的独立上下文读取失败。'
})
const missingVectors = computed(() => Math.max(
  0, Number(health.value?.index?.items || 0) - Number(health.value?.index?.vectors || 0),
))
const l2Items = computed(() => (current.value?.items || []) as MemoryPanelItem[])
const l4Items = computed(() => (current.value?.items || []) as MemoryPanelHook[])
const l3Items = computed(() => (current.value?.items || []) as MemoryPanelTaskItem[])
const l4Errors = computed(() => (current.value?.errors || []) as Array<Record<string, unknown>>)
const l4ProjectErrors = computed(() => (current.value?.project_errors || []) as Array<Record<string, unknown>>)
const retrievableItems = computed(() => (current.value?.retrievable_items || []) as Array<Record<string, unknown>>)

/** 漏斗的渲染序列：七级之间按后端标的 `handoffs` 插入「跨单位」分隔，
 *  免得把「3 个回合拿到回执」和「0 个条目被采纳」读成同一条链断了一格。 */
type FunnelStep =
  | { kind: 'step'; key: string; label: string; count: number; unit: string; note: string; unmeasurable: boolean }
  | { kind: 'handoff'; key: string; reason: string }

const funnelSteps = computed<FunnelStep[]>(() => {
  const funnel = health.value?.funnel
  const stages = funnel?.stages || []
  const unmeasurable = new Set((funnel?.unmeasurable || []).map(String))
  const handoffByFrom = new Map((funnel?.handoffs || []).map(item => [String(item.from), item]))
  const steps: FunnelStep[] = []
  stages.forEach((stage, index) => {
    const key = String(stage.key || '')
    const extras: string[] = []
    if (stage.measurable !== null && stage.measurable !== undefined && unmeasurable.has(key)) {
      extras.push('无判据')
    }
    if (stage.stock !== null && stage.stock !== undefined) extras.push(`存量 ${stage.stock}`)
    if (stage.events !== null && stage.events !== undefined) extras.push(`事件 ${stage.events}`)
    steps.push({
      kind: 'step',
      key,
      label: String(stage.label || key),
      count: Number(stage.count || 0),
      unit: String(stage.unit_label || ''),
      note: extras.join(' · '),
      unmeasurable: unmeasurable.has(key),
    })
    const hand = handoffByFrom.get(key)
    if (hand && index < stages.length - 1) {
      steps.push({ kind: 'handoff', key: `${key}-handoff`, reason: String(hand.reason || '单位不同') })
    }
  })
  return steps
})

const constitutionDraft = ref('')
const profileDraft = ref({
  display_name: '', language: '', timezone: '', location: '', notes: '',
})
const itemDrafts = ref<Record<string, string>>({})
const hookDrafts = ref<Record<string, string>>({})
const taskDrafts = ref<Record<string, string>>({})
const newHook = ref('')
const newHookKind = ref('fact')
const linkTargets = ref<Record<string, string>>({})
const linkTypes = ref<Record<string, string>>({})

const STATE_LABELS: Record<string, string> = {
  todo: '待办', doing: '进行中', done_pending_merge: '待验收', done: '已完成',
}

function stateLabel(state: unknown): string {
  return STATE_LABELS[String(state || '')] || String(state || '')
}

/** tab 计数必须带单位：否则 "472/600" 会被读成库存容量，实际是"本轮注入字数/上限"。 */
function layerCount(layer: { key: string; chars?: number; budget?: number | null }): string {
  const chars = Number(layer.chars || 0)
  if (layer.key === 'summary') {
    return layer.budget ? `${chars} 字 / 注入上限 ${Number(layer.budget)}` : `${chars} 字`
  }
  if (layer.budget) return `${chars}/${Number(layer.budget)} 字`
  return `${chars} 字`
}

/** 历史摘要的分子是磁盘存储字数，分母是注入侧"保头尾、丢中间"的硬截上限；口径必须说清。 */
function layerHint(layer: { key: string; budget?: number | null }): string {
  if (layer.key === 'summary') {
    const cap = Number(layer.budget || 0)
    if (!cap) {
      return '分子=存储的摘要字数；分母=注入硬截上限（由 token 预算派生）；超限时保留开头（约 40%）与结尾（约 60%），中间部分标注省略。'
    }
    return `分子=存储的摘要字数，分母=注入硬截上限 ${cap} 字；超限时保留开头（约 40%）与结尾（约 60%），中间部分标注省略。`
  }
  return '分子=本轮注入模型的字数，分母=本轮注入上限；不是存储容量'
}

function refreshDrafts() {
  itemDrafts.value = {}
  hookDrafts.value = {}
  taskDrafts.value = {}
  for (const layer of layers.value) {
    if (layer.key === 'rule') constitutionDraft.value = layer.raw || ''
    if (layer.key === 'profile') {
      const profile = (layer.profile || {}) as Record<string, unknown>
      profileDraft.value = {
        display_name: String(profile.display_name || ''),
        language: String(profile.language || ''),
        timezone: String(profile.timezone || ''),
        location: String(profile.location || ''),
        notes: String(profile.notes || ''),
      }
    }
    if (layer.key === 'shared') {
      linkTargets.value = {}
      linkTypes.value = {}
      for (const item of (layer.items || []) as MemoryPanelItem[]) {
        if (item.id) {
          itemDrafts.value[item.id] = String(item.body || '')
          linkTargets.value[item.id] = ''
          linkTypes.value[item.id] = 'refines'
        }
      }
    }
    if (layer.key === 'taskbook') {
      for (const item of (layer.items || []) as MemoryPanelTaskItem[]) {
        if (item.key) taskDrafts.value[item.key] = String(item.text || '')
      }
    }
    if (layer.key === 'hook') {
      for (const item of (layer.items || []) as MemoryPanelHook[]) {
        if (item.id) hookDrafts.value[item.id] = String(item.text || '')
      }
    }
  }
}

async function load(nextMode: 'next' | 'last' = mode.value, prefetch = false) {
  if (!prefetch && !props.visible) return
  const sequence = ++loadSequence
  const sessionId = props.sessionId
  const agentId = props.agentId
  mode.value = nextMode
  loading.value = true
  error.value = ''
  notice.value = ''
  performance.mark('memory:load:start', { detail: { sequence, prefetch } })
  try {
    const [inspectResult, result, pinnedResult] = await Promise.all([
      memoryPanelApi.inspect(sessionId, agentId, agentId !== 'main'),
      memoryPanelApi.context(sessionId, nextMode),
      memoryPanelApi.pinned(sessionId, agentId).catch((e: unknown) => ({
        ok: false,
        error: (e as Error)?.message || '固定事实读取失败',
        entries: [] as MemoryPinnedEntry[],
      })),
    ])
    if (sequence !== loadSequence || sessionId !== props.sessionId || agentId !== props.agentId) return
    performance.mark('memory:data:commit:start', { detail: { sequence, prefetch } })
    inspection.value = inspectResult
    data.value = result
    pinnedFacts.value = Array.isArray(pinnedResult.entries) ? pinnedResult.entries : []
    pinnedError.value = pinnedResult.ok === false ? (pinnedResult.error || '固定事实读取失败') : ''
    if (result.ok === false && active.value !== 'overview') error.value = result.error || '记忆 v2 未启用'
    if (!['health', 'overview', 'pinned'].includes(active.value) && result.layers?.length &&
        !result.layers.some(layer => layer.key === active.value)) {
      active.value = result.layers[0].key
    }
    refreshDrafts()
    performance.mark('memory:data:commit:end', { detail: { sequence, prefetch } })
  } catch (e) {
    if (sequence !== loadSequence) return
    error.value = (e as Error)?.message || '读取失败'
  } finally {
    if (sequence === loadSequence) {
      loading.value = false
      performance.mark('memory:load:end', { detail: { sequence, prefetch } })
    }
  }
}

async function loadHealth() {
  if (!props.visible) return
  healthLoading.value = true
  healthError.value = ''
  try {
    health.value = await memoryPanelApi.health()
  } catch (e) {
    healthError.value = (e as Error)?.message || '健康数据读取失败'
  } finally {
    healthLoading.value = false
  }
}

function openHealth() {
  active.value = 'health'
  void loadHealth()
  void loadDisk()
}

async function maintainHealth(apply: boolean) {
  if (healthBusy.value) return
  if (apply) {
    const confirmed = await askConfirm({
      title: '执行记忆维护',
      message: '会合并近似候选、把重复共享条目标为失效，并归档超过保留期的失效条目；正文会先复制到 archive/memory，索引随后重建。',
      confirmText: '执行维护',
      danger: true,
    })
    if (!confirmed) return
  }
  healthBusy.value = true
  healthError.value = ''
  healthNotice.value = ''
  try {
    const result = await memoryPanelApi.maintain({
      apply,
      confirm: apply,
      retention_days: Number(health.value?.stale?.retention_days || 90),
    })
    const candidateMerged = Number((result.candidates as Record<string, unknown>)?.merged || 0)
    const sharedMerged = Number((result.shared as Record<string, unknown>)?.duplicates || 0)
    const gcArchived = Number((result.gc as Record<string, unknown>)?.archived || 0)
    healthNotice.value = apply
      ? `维护完成：候选合并 ${candidateMerged}，共享去重 ${sharedMerged}，归档 ${gcArchived}`
      : `预览：候选合并 ${candidateMerged}，共享去重 ${sharedMerged}，可归档 ${gcArchived}`
    await loadHealth()
    if (apply) await load(mode.value)
  } catch (e) {
    healthError.value = (e as Error)?.message || '维护失败'
  } finally {
    healthBusy.value = false
  }
}

// ---- 磁盘保留期：间隔 / 过期天数 / 白名单都归用户，动手要确认 ----
const disk = ref<DiskRetentionState | null>(null)
const diskBusy = ref(false)
const diskError = ref('')
const diskNotice = ref('')
// 表单值与已保存值分开：用户改到一半就能看出「还没保存」，保存后以后端回读为准。
const diskInterval = ref(0)
const diskIdle = ref(30)
const diskOrphans = ref(false)
const diskNewEntry = ref('')

const diskWhitelist = computed(() => disk.value?.settings?.whitelist || [])
const diskDirty = computed(() => !!disk.value && (
  diskInterval.value !== disk.value.settings.interval_days
  || diskIdle.value !== disk.value.settings.idle_days))
/** 记录里的 trigger 是后端键名，界面翻成中文：用户要看得懂那一次是谁触发的。 */
const diskTriggerLabel = computed(() => {
  const map: Record<string, string> = {
    manual: '手动', scheduled: '定时体检', cli: '命令行',
  }
  return map[String(disk.value?.last_run?.trigger || '')] || '未知来源'
})
/** 到点状态三种说法，别把「还没跑过体检」写成「未到点」——那会让人以为已经在计时。 */
const diskScheduleNote = computed(() => {
  const sched = disk.value?.schedule
  if (!sched) return ''
  if (!sched.interval_days) return '（0 = 不自动清，只能手动点）'
  if (!sched.first_seen_ts) return '（还没跑过体检：首轮只记基线，不会当场删）'
  if (sched.due) return '（当前已到期，下次体检就会清）'
  return sched.next_due_ts ? `（下次不早于 ${fmtDiskTime(sched.next_due_ts)}）` : ''
})

/** 白名单条目的删除按钮只对用户自己加的开放：内置项（release / installer / *.zip）
 *  移掉就等于把发布物交给自动删除，后端也挡不住"用户以为自己在移除"这种误操作。 */
function diskIsBuiltin(entry: string): boolean {
  return (disk.value?.builtin_whitelist || []).includes(entry)
}

function fmtBytes(value?: number): string {
  const n = Number(value || 0)
  if (n <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let index = 0
  let size = n
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size >= 100 || index === 0 ? Math.round(size) : size.toFixed(1)} ${units[index]}`
}

function fmtDiskTime(ts?: number | null): string {
  if (!ts) return '从未'
  const date = new Date(ts * 1000)
  return Number.isNaN(date.getTime()) ? '未知时间' : date.toLocaleString()
}

function applyDiskState(state: DiskRetentionState) {
  disk.value = state
  diskInterval.value = state.settings.interval_days
  diskIdle.value = state.settings.idle_days
}

async function loadDisk() {
  try {
    applyDiskState(await memoryPanelApi.diskRetention())
  } catch (e) {
    diskError.value = (e as Error)?.message || '磁盘保留期读取失败'
  }
}

async function saveDiskSettings() {
  if (diskBusy.value) return
  diskBusy.value = true
  diskError.value = ''
  diskNotice.value = ''
  try {
    applyDiskState(await memoryPanelApi.saveDiskRetention({
      interval_days: diskInterval.value,
      idle_days: diskIdle.value,
    }))
    diskNotice.value = disk.value?.settings.interval_days
      ? `已保存：每 ${disk.value.settings.interval_days} 天回收一次，闲置满 ${disk.value.settings.idle_days} 天的副本进回收`
      : `已保存：不自动回收（间隔 0 天），只能手动点「立即回收」`
  } catch (e) {
    diskError.value = (e as Error)?.message || '设置保存失败'
  } finally {
    diskBusy.value = false
  }
}

async function patchDiskWhitelist(next: string[]) {
  if (diskBusy.value) return
  diskBusy.value = true
  diskError.value = ''
  try {
    applyDiskState(await memoryPanelApi.saveDiskRetention({ whitelist: next }))
    diskNotice.value = '白名单已保存'
  } catch (e) {
    diskError.value = (e as Error)?.message || '白名单保存失败'
  } finally {
    diskBusy.value = false
  }
}

function addDiskWhitelist() {
  const entry = diskNewEntry.value.trim()
  if (!entry || diskWhitelist.value.includes(entry)) return
  diskNewEntry.value = ''
  void patchDiskWhitelist([...diskWhitelist.value, entry])
}

function removeDiskWhitelist(entry: string) {
  if (diskIsBuiltin(entry)) return
  void patchDiskWhitelist(diskWhitelist.value.filter(item => item !== entry))
}

async function runDisk(apply: boolean) {
  if (diskBusy.value) return
  if (apply) {
    const confirmed = await askConfirm({
      title: '立即回收磁盘',
      message: `会永久删除闲置超过 ${diskIdle.value} 天的旧副本（试做区 / 项目快照 / dist 构建副本）。`
        + '未跑完的工作流、最近的试做区、白名单里的名字都不动。删除不可撤销。',
      confirmText: '删除并释放空间',
      danger: true,
    })
    if (!confirmed) return
  }
  diskBusy.value = true
  diskError.value = ''
  diskNotice.value = ''
  try {
    const out: DiskRetentionRunResult = await memoryPanelApi.runDiskRetention({
      apply,
      confirm: apply,
      include_orphans: diskOrphans.value,
    })
    const plan = out.plan || {}
    const failed = Number(out.result?.failed?.length || 0)
    diskNotice.value = apply
      ? `已回收 ${fmtBytes(out.result?.freed_bytes)}（${out.result?.freed_items ?? 0} 项）`
        + `${failed ? ` · ${failed} 项失败` : ''}`
      : `可回收 ${fmtBytes(plan.reclaimable_bytes)} / ${plan.reclaimable_items ?? 0} 项`
        + ` · 白名单挡下 ${fmtBytes(plan.blocked_by_whitelist_bytes)}`
        + ` · 待人工 ${fmtBytes(plan.needs_review_bytes)} / ${plan.needs_review_items ?? 0} 项`
    await loadDisk()
  } catch (e) {
    diskError.value = (e as Error)?.message || (apply ? '回收失败' : '扫描失败')
  } finally {
    diskBusy.value = false
  }
}

onMounted(() => {
  // 首次打开前预取快照；打开时仍刷新，但先用已缓存数据呈现骨架与力导向。
  void load('next', true)
})

watch(() => props.visible, visible => {
  if (!visible) return
  void load('next')
  if (active.value === 'health') {
    void loadHealth()
    void loadDisk()
  }
})
watch(() => [props.sessionId, props.agentId], () => {
  loadSequence += 1
  data.value = null
  inspection.value = null
  pinnedFacts.value = []
  pinnedError.value = ''
  active.value = 'overview'
  if (props.visible) void load(mode.value)
})

async function run(action: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await action()
    const message = notice.value
    const failure = error.value
    await load(mode.value)
    notice.value = message
    error.value = failure
  } catch (e) {
    error.value = (e as Error)?.message || '保存失败'
  } finally {
    busy.value = false
  }
}

function pinnedActorLabel(actor?: string): string {
  if (actor === 'user') return '用户'
  if (actor === 'migration') return '迁移'
  return '模型'
}

async function deletePinnedFact(entry: MemoryPinnedEntry) {
  if (busy.value || mode.value === 'last') return
  const confirmed = await askConfirm({
    title: '删除固定事实',
    message: '删除后该条不再注入；同一事实的后续自动提取会被墓碑阻挡。',
    confirmText: '删除',
    danger: true,
  })
  if (!confirmed) return
  await run(async () => {
    await memoryPanelApi.deletePinned(entry.id, props.sessionId, props.agentId)
    notice.value = '已删除固定事实'
  })
}

async function saveConstitution() {
  await run(async () => {
    await memoryPanelApi.saveConstitution(constitutionDraft.value, current.value?.version)
    notice.value = '宪法已保存，下一回合生效'
  })
}

async function saveProfile() {
  await run(async () => {
    await memoryPanelApi.saveProfile({ ...profileDraft.value })
    notice.value = '画像已保存，下一回合生效'
  })
}

async function saveItem(item: MemoryPanelItem) {
  if (!item.id) return
  await run(async () => {
    await memoryPanelApi.patchItem(item.id!, { text: itemDrafts.value[item.id!] || '' })
    notice.value = '共享记忆已保存（回执清空，需重新验证）'
  })
}

async function togglePin(item: MemoryPanelItem) {
  if (!item.id) return
  await run(async () => {
    await memoryPanelApi.patchItem(item.id!, { pinned: !item.pinned })
    notice.value = item.pinned ? '已取消 pin' : '已 pin，下一回合注入'
  })
}

async function removeItem(item: MemoryPanelItem) {
  if (!item.id) return
  const confirmed = await askConfirm({
    title: '删除共享记忆',
    message: '删除后不再出现在任何项目，且无法撤销。',
    confirmText: '删除',
    danger: true,
  })
  if (!confirmed) return
  await run(async () => {
    await memoryPanelApi.deleteItem(item.id!)
    notice.value = '已删除'
  })
}

async function invalidateItem(item: MemoryPanelItem) {
  if (!item.id) return
  const target = await askPrompt({
    title: '标记共享记忆失效',
    message: '输入取代它的共享条目 id。旧条目会保留审计记录，但不再参与召回或注入。',
    defaultValue: '',
    placeholder: 'mem-...',
  })
  if (!target?.trim()) return
  await run(async () => {
    await memoryPanelApi.supersedeItem(item.id!, target.trim(), true)
    notice.value = '已标记失效；新条目仍可继续检索'
  })
}

async function addLink(item: MemoryPanelItem) {
  if (!item.id) return
  const target = (linkTargets.value[item.id] || '').trim()
  if (!target) return
  await run(async () => {
    await memoryPanelApi.linkItem(item.id!, target, linkTypes.value[item.id!] || 'refines')
    notice.value = '关联已添加'
  })
}

async function removeLink(item: MemoryPanelItem, edge: { type?: string; to?: string }) {
  if (!item.id || !edge.to || !edge.type) return
  await run(async () => {
    await memoryPanelApi.unlinkItem(item.id!, edge.to!, edge.type!)
    notice.value = '关联已解除'
  })
}

async function pinRetrievable(item: Record<string, unknown>) {
  if (!item.id) return
  await run(async () => {
    await memoryPanelApi.patchItem(String(item.id), { pinned: true })
    notice.value = '已 pin，下一回合注入'
  })
}

async function adoptCandidate(item: Record<string, unknown>) {
  if (!item.id) return
  await run(async () => {
    const result = await memoryPanelApi.adoptItem(String(item.id))
    if (result.promoted) {
      notice.value = result.pinned ? '已采纳并 pin' : '已采纳并晋升（重要度不足，暂不常驻）'
    } else {
      error.value = '未晋升：' + ((result.reasons || []).join('；') || '未满足晋升门')
    }
  })
}

async function promoteCandidate(item: Record<string, unknown>) {
  if (!item.id) return
  await run(async () => {
    const result = await memoryPanelApi.promoteItem(String(item.id))
    if (result.promoted) {
      notice.value = result.pinned ? '已晋升并 pin' : '已晋升（重要度不足，暂不常驻）'
    } else {
      error.value = '未晋升：' + ((result.reasons || []).join('；') || '未满足晋升门')
    }
  })
}

async function saveHook(hook: MemoryPanelHook) {
  if (!hook.id) return
  await run(async () => {
    await memoryPanelApi.patchHook(hook.id!, {
      session_id: props.sessionId,
      text: hookDrafts.value[hook.id!] || '',
    })
    notice.value = '钩子已更新，下一回合生效'
  })
}

async function toggleHookPin(hook: MemoryPanelHook) {
  if (!hook.id) return
  await run(async () => {
    await memoryPanelApi.patchHook(hook.id!, { session_id: props.sessionId, pinned: !hook.pinned })
    notice.value = hook.pinned ? '已取消 pin' : '已 pin'
  })
}

async function removeHook(hook: MemoryPanelHook) {
  if (!hook.id) return
  const confirmed = await askConfirm({
    title: '删除钩子', message: '删除后本窗口不再提醒这条信息。', confirmText: '删除', danger: true,
  })
  if (!confirmed) return
  await run(async () => {
    await memoryPanelApi.deleteHook(hook.id!, props.sessionId)
    notice.value = '已删除'
  })
}

async function addHook() {
  const text = newHook.value.trim()
  if (!text) return
  await run(async () => {
    await memoryPanelApi.addHook({
      session_id: props.sessionId, kind: newHookKind.value, text, pinned: true,
    })
    newHook.value = ''
    notice.value = '钩子已添加'
  })
}

async function saveTask(item: MemoryPanelTaskItem) {
  if (!item.key) return
  await run(async () => {
    await memoryPanelApi.editTaskItem(item.key!, taskDrafts.value[item.key!] || '')
    notice.value = '任务条目已保存'
  })
}

async function completeTask(item: MemoryPanelTaskItem) {
  if (!item.key) return
  let override = false
  if (!(item.receipts || []).length) {
    const confirmed = await askConfirm({
      title: '缺少验收回执',
      message: '该条目没有通过的测试/验收回执。强制完成会记录为用户覆盖，之后仍可按回执核验。',
      confirmText: '仍然完成',
      danger: true,
    })
    if (!confirmed) return
    override = true
  }
  await run(async () => {
    await memoryPanelApi.completeTaskItem(item.key!, { override })
    notice.value = '任务状态已更新'
  })
}

async function reopenTask(item: MemoryPanelTaskItem) {
  if (!item.key) return
  await run(async () => {
    await memoryPanelApi.reopenTaskItem(item.key!)
    notice.value = '任务已重开'
  })
}

async function reindex() {
  await run(async () => {
    const result = await memoryPanelApi.reindex()
    notice.value = `索引已重建：${Number(result.items || 0)} 条`
  })
}
</script>

<template>
  <Transition name="mp-panel">
    <aside v-if="visible" class="mp" :class="{ 'is-overview': active === 'overview' }" :data-agent-id="agentId" data-memory-panel role="complementary" aria-label="上下文记忆">
    <header class="mp-head">
      <div class="mp-identity">
        <span class="mp-mark" aria-hidden="true"><i></i><i></i><i></i></span>
        <div><strong>{{ agentLabel }} 的上下文 · 独立</strong><span>{{ agentId }} · {{ sessionId || '未选择会话' }}</span></div>
      </div>
      <span v-if="active === 'overview'" class="mp-observing"><i></i>观测中</span>
      <div v-else class="mp-modes" role="group" aria-label="注入视图">
        <button type="button" :class="{ on: mode === 'next' }" @click="load('next')">下一回合</button>
        <button type="button" :class="{ on: mode === 'last' }" @click="load('last')">上一回合</button>
      </div>
      <button class="mp-close" type="button" aria-label="关闭上下文记忆" @click="emit('close')">×</button>
    </header>

    <div v-if="!agentUnavailable && loading && active !== 'overview'" class="mp-inline-status" role="status"><i></i>正在读取当前层级的真实数据</div>
    <div v-if="!agentUnavailable && error && active !== 'overview'" class="mp-message is-error" role="alert"><span>{{ error }}</span><button type="button" @click="load(mode)">重试</button></div>
    <div v-else-if="!agentUnavailable && notice" class="mp-message is-ok" role="status">{{ notice }}</div>

    <div v-if="!agentUnavailable" class="mp-tabs" role="tablist">
      <button type="button" role="tab" :aria-selected="active === 'overview'" class="mp-tab" :class="{ on: active === 'overview' }" @click="active = 'overview'">
        记忆总览 <span title="分子=本轮实际注入的记忆条数，分母=该 Agent 记忆库存总条数；单位是条，不是字数/容量">{{ inspection?.injected.length || 0 }}/{{ inspection?.memories.length || 0 }} 条</span>
      </button>
      <button
        v-for="layer in layers"
        :key="layer.key"
        type="button"
        role="tab"
        :aria-selected="active === layer.key"
        class="mp-tab"
        :class="{ on: active === layer.key }"
        :title="layerHint(layer)"
        @click="active = layer.key"
      >
        {{ layer.title }}
        <span>{{ layerCount(layer) }}</span>
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="active === 'pinned'"
        class="mp-tab"
        :class="{ on: active === 'pinned' }"
        title="会话内固定事实：只增不删，只有用户能删除"
        @click="active = 'pinned'"
      >
        固定事实
        <span>{{ pinnedFacts.length }} 条</span>
      </button>
    </div>

    <div v-if="agentScoped && !agentUnavailable" class="mp-scope-note" data-agent-scope-note>
      <strong>Agent 私有视图</strong>
      <p>记忆总览来自 <code>/api/agent/memory/inspect?agent_id={{ agentId }}</code>，是该 Agent 实例的注入、守则与库存；其余层级 tab 来自会话级 <code>/api/memory/context</code>（后端不接受 agent_id），不是该 Agent 私有数据。</p>
    </div>

    <div v-if="agentUnavailable" class="mp-body mp-agent-empty" data-agent-context-empty>
      <span class="mp-agent-empty-mark" aria-hidden="true"></span>
      <strong>{{ agentLabel }} 的独立上下文不可读</strong>
      <p>{{ agentUnavailableReason }}</p>
      <small>数据来源：<code>GET /api/agent/memory/inspect?agent_id={{ agentId }}</code>。该接口只解析进程内活动 Agent 实例；<code>.docmind/delegations</code> 快照不包含上下文正文。</small>
      <button type="button" @click="load(mode)">重新读取</button>
    </div>

    <div v-else-if="active === 'overview'" class="mp-body">
      <MemoryOverview
        :data="inspection"
        :loading="loading && !inspection"
        :error="error"
        :permission="permissionError"
        @retry="load(mode)"
      />
    </div>

    <div v-else-if="active === 'health'" class="mp-body">
      <p v-if="healthLoading" class="mp-hint">读取中…</p>
      <p v-if="healthError" class="mp-error">{{ healthError }}</p>
      <p v-else-if="healthNotice" class="mp-ok">{{ healthNotice }}</p>
      <template v-if="health">
        <div class="mp-health-grid">
          <div class="mp-health-card">
            <strong>{{ health.shared?.active ?? 0 }}</strong><span>共享 active</span>
          </div>
          <div class="mp-health-card">
            <strong>{{ health.shared?.pinned ?? 0 }}</strong><span>共享 pin</span>
          </div>
          <div class="mp-health-card">
            <strong>{{ health.project?.candidate ?? 0 }}</strong><span>项目候选</span>
          </div>
          <div class="mp-health-card">
            <strong>{{ health.project?.promoted ?? 0 }}</strong><span>已晋升</span>
          </div>
          <div class="mp-health-card">
            <strong>{{ health.shared?.superseded ?? 0 }}</strong><span>共享失效</span>
          </div>
          <div class="mp-health-card" :class="{ 'mp-health-warn': (health.duplicates?.project || 0) + (health.duplicates?.shared || 0) > 0 }">
            <strong>{{ (health.duplicates?.project || 0) + (health.duplicates?.shared || 0) }}</strong><span>重复项</span>
          </div>
          <div class="mp-health-card" :class="{ 'mp-health-warn': missingVectors > 0 }">
            <strong>{{ missingVectors }}</strong><span>缺向量</span>
          </div>
          <div class="mp-health-card" :class="{ 'mp-health-warn': (health.stale?.superseded || 0) > 0 }">
            <strong>{{ health.stale?.superseded ?? 0 }}</strong><span>待归档失效</span>
          </div>
        </div>

        <div v-if="health.warnings?.length" class="mp-health-warnings">
          <strong>需要处理</strong>
          <p v-for="warning in health.warnings" :key="warning.code" class="mp-warn">
            · {{ warning.message }}
          </p>
        </div>

        <div class="mp-readonly">
          <strong>学习环漏斗（近 {{ health.funnel?.days ?? 30 }} 天）</strong>
          <div class="mp-funnel">
            <template v-for="step in funnelSteps" :key="step.key">
              <span
                v-if="step.kind === 'step'"
                class="mp-funnel-step"
                :class="{ 'mp-funnel-zero': !step.count, 'mp-funnel-unmeasured': step.unmeasurable }"
              >{{ step.label }} <strong>{{ step.count }}</strong
                ><em v-if="step.unit" class="mp-funnel-unit">{{ step.unit }}</em
                ><em v-if="step.note" class="mp-funnel-meta">{{ step.note }}</em></span>
              <span v-else class="mp-funnel-handoff" :title="step.reason">⇢ {{ step.reason }}</span>
            </template>
          </div>
          <p v-if="health.funnel?.stalled_at" class="mp-funnel-note mp-funnel-stall">· {{ health.funnel.stalled_at }}</p>
          <p v-else-if="health.funnel" class="mp-funnel-note">· 各级都通了：晋升出去的共享记忆已经回到提示词</p>
          <p v-for="(note, index) in health.funnel?.caveats || []" :key="`caveat-${index}`" class="mp-funnel-note mp-funnel-caveat">· {{ note }}</p>
          <p v-if="health.funnel?.item_window_by" class="mp-funnel-note mp-funnel-meta-line">· 采纳/晋升的窗口：{{ health.funnel.item_window_by }}</p>
        </div>

        <div class="mp-readonly">
          <strong>上下文预算</strong>
          <div v-for="layer in health.context?.layers || []" :key="String(layer.key)" class="mp-health-row">
            <span>{{ layer.title }}</span>
            <span>{{ layer.chars }}<template v-if="layer.budget">/{{ layer.budget }}</template></span>
          </div>
        </div>

        <div class="mp-readonly">
          <strong>近 7 天固化</strong>
          <p>
            {{ health.metrics?.consolidate_calls ?? 0 }} 次调用 ·
            跳过 {{ health.metrics?.skipped ?? 0 }} ·
            错误 {{ health.metrics?.errors ?? 0 }} ·
            p50 {{ health.metrics?.p50_ms ?? 0 }}ms
          </p>
          <p v-if="health.receipts" class="mp-receipts">
            近 {{ health.receipts.days ?? 30 }} 天验证回执 {{ health.receipts.total ?? 0 }} 条
            （通过 {{ health.receipts.pass ?? 0 }} / 未通过 {{ health.receipts.fail ?? 0 }}）——
            采纳/晋升要的是「通过」的回执
          </p>
        </div>

        <div class="mp-readonly mp-disk">
          <strong>磁盘保留期</strong>
          <p v-if="diskError" class="mp-error">{{ diskError }}</p>
          <p v-else-if="diskNotice" class="mp-ok">{{ diskNotice }}</p>
          <p v-if="disk && !disk.enabled" class="mp-hint">状态目录还不存在：没有需要回收的副本。</p>
          <template v-if="disk?.enabled">
            <div class="mp-disk-stats">
              <span>可回收 <strong>{{ fmtBytes(disk.plan?.reclaimable_bytes) }}</strong>
                / {{ disk.plan?.reclaimable_items ?? 0 }} 项</span>
              <span>白名单挡下 <strong>{{ fmtBytes(disk.plan?.blocked_by_whitelist_bytes) }}</strong>
                / {{ disk.plan?.blocked_by_whitelist_items ?? 0 }} 项</span>
              <span>待人工 <strong>{{ fmtBytes(disk.plan?.needs_review_bytes) }}</strong>
                / {{ disk.plan?.needs_review_items ?? 0 }} 项</span>
            </div>
            <p v-if="!disk.last_run?.ts" class="mp-disk-note">
              还没有扫描记录：点「重新扫描」算一次计划（只列清单，不删任何东西）。
            </p>
            <p v-else class="mp-disk-note">
              数字来自 {{ fmtDiskTime(disk.last_run.ts) }} 的{{ disk.last_run?.applied ? '回收' : '扫描' }}
              （{{ diskTriggerLabel }}）；上次释放 {{ fmtBytes(disk.last_run?.freed_bytes) }}
              / {{ disk.last_run?.freed_items ?? 0 }} 项<template v-if="disk.last_run?.failed_items">
                · <span class="mp-warn">{{ disk.last_run.failed_items }} 项失败</span></template>
            </p>
            <label class="mp-field">每隔多少天自动清一次
              <input
                v-model.number="diskInterval"
                class="mp-input"
                data-k="interval"
                type="number"
                :min="disk.ranges.interval_days_min"
                :max="disk.ranges.interval_days_max"
                :disabled="diskBusy"
              />
              <span class="mp-disk-hint">
                0 = 不自动清，只能自己点「立即回收」；正数（默认 30 天）到点由后台体检执行
                —— 体检本身每天跑一次，真正动手可能比你设的日期晚一天{{
                  diskScheduleNote }}
              </span>
            </label>
            <label class="mp-field">闲置多少天才算过期
              <input
                v-model.number="diskIdle"
                class="mp-input"
                data-k="idle"
                type="number"
                :min="disk.ranges.idle_days_min"
                :max="disk.ranges.idle_days_max"
                :disabled="diskBusy"
              />
              <span class="mp-disk-hint">试做区、项目快照、dist 构建副本都按这一档判</span>
            </label>
            <div class="mp-disk-whitelist">
              <span class="mp-disk-wl-title">白名单（这些名字永不清）</span>
              <div class="mp-disk-chips">
                <span
                  v-for="entry in diskWhitelist"
                  :key="entry"
                  class="mp-disk-chip"
                  :class="{ 'mp-disk-chip-builtin': diskIsBuiltin(entry) }"
                >{{ entry }}<button
                  v-if="!diskIsBuiltin(entry)"
                  type="button"
                  class="mp-disk-chip-x"
                  :disabled="diskBusy"
                  aria-label="移出白名单"
                  @click="removeDiskWhitelist(entry)"
                >×</button></span>
              </div>
              <div class="mp-disk-wl-add">
                <input
                  v-model="diskNewEntry"
                  class="mp-input"
                  data-k="whitelist"
                  placeholder="加一条：目录名或前缀，如 DocMind_p0_*"
                  :disabled="diskBusy"
                  @keyup.enter="addDiskWhitelist"
                />
                <button class="mp-mini" type="button" :disabled="diskBusy || !diskNewEntry.trim()" @click="addDiskWhitelist">加入</button>
              </div>
              <p class="mp-disk-hint">内置 {{ disk.builtin_whitelist.length }} 条（发布物 / 安装包 / zip）不可移除；当前上限 {{ disk.ranges.whitelist_max_entries }} 条。</p>
            </div>
            <label class="mp-pin"><input v-model="diskOrphans" type="checkbox" :disabled="diskBusy" /> 把认不出状态的副本也算进回收</label>
            <div class="mp-actions">
              <span class="mp-hint">
                <template v-if="diskBusy">正在扫描整盘副本，要十几秒…</template>
                <template v-else-if="diskDirty">设置改动还没保存</template>
                <template v-else>单次最多回收 {{ disk.ranges.max_items_per_run }} 项</template>
              </span>
              <button class="mp-mini" type="button" :disabled="diskBusy" @click="saveDiskSettings">保存设置</button>
              <button class="mp-mini" type="button" :disabled="diskBusy" @click="runDisk(false)">重新扫描</button>
              <button class="mp-btn mp-danger" type="button" :disabled="diskBusy" @click="runDisk(true)">立即回收</button>
            </div>
          </template>
        </div>

        <div class="mp-actions">
          <span class="mp-hint">维护会先归档再移出工作区，可 dry-run</span>
          <button class="mp-mini" type="button" :disabled="healthBusy" @click="maintainHealth(false)">预览维护</button>
          <button class="mp-btn" type="button" :disabled="healthBusy" @click="maintainHealth(true)">执行维护</button>
        </div>
      </template>
    </div>

    <div v-else-if="active === 'pinned'" class="mp-body" data-memory-pinned>
      <p v-if="pinnedError" class="mp-warn" role="alert">{{ pinnedError }}</p>
      <p class="mp-hint">固定事实只增不删，只有用户可以删除；被删除的条目会记墓碑，后续自动提取同一事实会被丢弃。</p>
      <p v-if="!pinnedFacts.length" class="mp-body-text">暂无固定事实</p>
      <div v-for="entry in pinnedFacts" :key="entry.id" class="mp-item">
        <div class="mp-item-head">
          <span>{{ pinnedActorLabel(entry.actor) }}</span>
          <button
            class="mp-mini mp-danger"
            type="button"
            :disabled="busy || mode === 'last'"
            :aria-label="`删除固定事实：${entry.text}`"
            @click="deletePinnedFact(entry)"
          >删除</button>
        </div>
        <p class="mp-body-text">{{ entry.text }}</p>
        <div class="mp-item-head">
          <span>来源：{{ entry.source || '未标注' }}</span>
          <span>时间：{{ entry.added_at || '未记录' }}</span>
        </div>
      </div>
    </div>

    <div v-else-if="current" class="mp-body">
      <template v-if="current.kind === 'markdown'">
        <p class="mp-meta">
          v{{ current.version }} ·
          {{ current.hash_ok ? '哈希一致' : '手改/哈希不一致（已记审计）' }} ·
          <span v-if="current.gates_ok">条款闸门齐全</span>
          <span v-else class="mp-warn">未绑定闸门：{{ (current.missing_gates || []).join('、') }}</span>
        </p>
        <textarea
          v-model="constitutionDraft"
          class="mp-textarea"
          spellcheck="false"
          :rows="14"
          :disabled="!current.editable || mode === 'last'"
        />
        <div class="mp-actions">
          <span class="mp-hint">保存后版本 +1；下一回合生效</span>
          <button class="mp-btn" type="button" :disabled="busy || !current.editable || mode === 'last'" @click="saveConstitution">保存宪法</button>
        </div>
      </template>

      <template v-else-if="current.kind === 'profile'">
        <label class="mp-field">称呼<input v-model="profileDraft.display_name" class="mp-input" /></label>
        <label class="mp-field">语言<input v-model="profileDraft.language" class="mp-input" placeholder="中文 / 英文" /></label>
        <label class="mp-field">时区<input v-model="profileDraft.timezone" class="mp-input" placeholder="Asia/Shanghai" /></label>
        <label class="mp-field">所在地<input v-model="profileDraft.location" class="mp-input" /></label>
        <label class="mp-field">备注<textarea v-model="profileDraft.notes" class="mp-textarea" :rows="3" /></label>
        <div class="mp-actions">
          <span class="mp-hint">学到的习惯只读；画像与习惯互不覆盖</span>
          <button class="mp-btn" type="button" :disabled="busy || mode === 'last'" @click="saveProfile">保存画像</button>
        </div>
        <pre class="mp-pre">{{ JSON.stringify(current.learned || {}, null, 2) }}</pre>
      </template>

      <template v-else-if="current.kind === 'items'">
        <p v-if="!l2Items.length" class="mp-hint">
          没有 pin 的共享记忆。可召回条目 {{ retrievableItems.length }} 条，可在下方检索或采纳。
        </p>
        <div v-for="item in l2Items" :key="String(item.id)" class="mp-item">
          <div class="mp-item-head">
            <span>{{ item.kind === 'lesson' ? '教训' : '事实' }} · 置信 {{ Number(item.confidence || 0).toFixed(2) }} · 关联 {{ (item.edges || []).length }}</span>
            <label class="mp-pin"><input type="checkbox" :checked="!!item.pinned" @change="togglePin(item)" /> pin</label>
          </div>
          <textarea v-model="itemDrafts[String(item.id)]" class="mp-textarea" :rows="4" :disabled="mode === 'last'" />
          <div class="mp-actions">
            <button class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="saveItem(item)">保存</button>
            <button class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="invalidateItem(item)">失效</button>
            <button class="mp-mini mp-danger" type="button" :disabled="busy || mode === 'last'" @click="removeItem(item)">删除</button>
          </div>
          <div v-if="item.edges?.length" class="mp-links">
            <div v-for="edge in item.edges" :key="`${edge.type}:${edge.to}`" class="mp-link">
              <span>{{ edge.type }} → {{ edge.to }}</span>
              <button class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="removeLink(item, edge)">解除</button>
            </div>
          </div>
          <div v-if="mode !== 'last'" class="mp-add mp-link-add">
            <select v-model="linkTypes[String(item.id)]" class="mp-input mp-select" aria-label="关联类型">
              <option value="refines">refines</option>
              <option value="supersedes">supersedes</option>
              <option value="derived_from">derived_from</option>
              <option value="used_in">used_in</option>
              <option value="contradicts">contradicts</option>
            </select>
            <input v-model="linkTargets[String(item.id)]" class="mp-input" placeholder="目标条目 id" />
            <button class="mp-mini" type="button" :disabled="busy || !linkTargets[String(item.id)]" @click="addLink(item)">关联</button>
          </div>
        </div>
        <details v-if="retrievableItems.length" class="mp-retrievable">
          <summary>可召回但未注入（{{ retrievableItems.length }}）</summary>
          <div v-for="item in retrievableItems" :key="String(item.id)" class="mp-item">
            <div class="mp-item-head">
              <span>{{ item.scope === 'global' ? '共享' : '项目候选' }} · {{ item.kind === 'lesson' ? '教训' : '事实' }} · 置信 {{ Number(item.confidence || 0).toFixed(2) }}</span>
              <span v-if="item.edges" class="mp-hint">关联 {{ item.edges }}</span>
            </div>
            <p class="mp-body-text">{{ item.body }}</p>
            <div class="mp-actions">
              <button v-if="item.scope === 'global'" class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="pinRetrievable(item)">pin</button>
              <template v-else>
                <button class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="adoptCandidate(item)">采纳</button>
                <button class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="promoteCandidate(item)">晋升</button>
              </template>
            </div>
          </div>
        </details>
      </template>

      <template v-else-if="current.kind === 'taskbook'">
        <p v-if="!l3Items.length" class="mp-hint">当前没有任务书。长任务（计划 ≥3 项）会自动建书。</p>
        <div v-for="item in l3Items" :key="String(item.key)" class="mp-item">
          <div class="mp-item-head">
            <span>{{ stateLabel(item.state) }}</span>
            <span v-if="item.user_override" class="mp-warn">用户强制完成</span>
          </div>
          <textarea v-model="taskDrafts[String(item.key)]" class="mp-textarea" :rows="2" :disabled="mode === 'last'" />
          <div class="mp-actions">
            <button class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="saveTask(item)">保存</button>
            <button class="mp-mini" type="button" :disabled="busy || mode === 'last' || item.state === 'done'" @click="completeTask(item)">完成</button>
            <button class="mp-mini" type="button" :disabled="busy || mode === 'last' || item.state !== 'done'" @click="reopenTask(item)">重开</button>
          </div>
        </div>
        <p class="mp-hint">「完成」需要测试/验收回执；没有回执时只能按用户覆盖记录。</p>
      </template>

      <template v-else-if="current.kind === 'hooks'">
        <div v-for="hook in l4Items" :key="String(hook.id)" class="mp-item">
          <div class="mp-item-head">
            <span>{{ hook.kind }} · {{ hook.verified ? '已验证' : '未验证' }}</span>
            <label class="mp-pin"><input type="checkbox" :checked="!!hook.pinned" @change="toggleHookPin(hook)" /> pin</label>
          </div>
          <textarea v-model="hookDrafts[String(hook.id)]" class="mp-textarea" :rows="2" :disabled="mode === 'last'" />
          <div class="mp-actions">
            <button class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="saveHook(hook)">保存</button>
            <button class="mp-mini mp-danger" type="button" :disabled="busy || mode === 'last'" @click="removeHook(hook)">删除</button>
          </div>
        </div>
        <div v-if="mode !== 'last'" class="mp-add">
          <select v-model="newHookKind" class="mp-input mp-select" aria-label="钩子类型">
            <option value="fact">事实</option>
            <option value="user_agreement">约定</option>
            <option value="decision">决策</option>
          </select>
          <input v-model="newHook" class="mp-input" placeholder="新增钩子（压缩后仍保留）" />
          <button class="mp-btn" type="button" :disabled="busy || !newHook.trim()" @click="addHook">添加</button>
        </div>
        <div v-if="l4Errors.length" class="mp-readonly">
          <strong>本窗口易错点（只读）</strong>
          <pre class="mp-pre">{{ l4Errors.map(e => `- ${e.label}: ${e.cause}（×${e.occurrences}）`).join('\n') }}</pre>
        </div>
        <div v-if="l4ProjectErrors.length" class="mp-readonly">
          <strong>跨会话易错点（只读）</strong>
          <pre class="mp-pre">{{ l4ProjectErrors.map(e => `- ${e.label}: ${e.cause}（×${e.occurrences}）`).join('\n') }}</pre>
        </div>
        <div v-if="current.next" class="mp-readonly">
          <strong>上一回合下一步（只读）</strong>
          <p>{{ current.next }}</p>
        </div>
      </template>

      <template v-else>
        <pre class="mp-pre">{{ current.text || '（空）' }}</pre>
      </template>
    </div>

    <footer class="mp-foot">
      <span>合计 {{ totalChars }} 字符</span>
      <div class="mp-foot-actions">
        <button
          class="mp-mini"
          :class="{ on: active === 'health' }"
          type="button"
          :aria-pressed="active === 'health'"
          @click="openHealth"
        >健康</button>
        <button class="mp-mini" type="button" :disabled="busy" @click="reindex">重建索引</button>
      </div>
    </footer>
    </aside>
  </Transition>
</template>

<style scoped>
.mp { position: fixed; inset: 0 0 0 auto; z-index: var(--mi-z-panel); width: min(var(--mi-panel-width-base), 100vw); display: flex; flex-direction: column; overflow: hidden; border-left: var(--mi-border-width) solid var(--mi-border); background: var(--mi-bg); box-shadow: var(--mi-shadow-panel); color: var(--mi-text); color-scheme: dark; font-family: var(--mi-font-ui); font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); isolation: isolate; }
.mp *, .mp *::before, .mp *::after { box-sizing: border-box; }
.mp.is-overview { width: min(var(--mi-panel-width), 100vw); }
.mp-panel-leave-active { transition: transform var(--mi-duration-slow) var(--mi-ease-enter), opacity var(--mi-duration-base) var(--mi-ease-standard); }
.mp-panel-leave-to { transform: translateX(100%); opacity: 0; }
.mp-head { display: flex; align-items: center; gap: var(--mi-space-4); min-height: var(--mi-header-height); padding: 0 var(--mi-space-5); border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); background: var(--mi-bg); }
.mp-identity { display: flex; align-items: center; gap: var(--mi-space-3); flex: 1; min-width: 0; }
.mp-identity > div { display: flex; flex-direction: column; min-width: 0; }
.mp-identity strong { font-size: var(--mi-fs-title); line-height: var(--mi-lh-title); font-weight: var(--mi-weight-semibold); }
.mp-identity span:last-child { overflow: hidden; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); text-overflow: ellipsis; white-space: nowrap; }
.mp-mark { position: relative; flex: 0 0 var(--mi-space-6); width: var(--mi-space-6); height: var(--mi-space-6); }
.mp-mark i { position: absolute; inset: 0; border: var(--mi-border-width) solid var(--mi-border-strong); border-radius: var(--mi-radius-lg); }
.mp-mark i:nth-child(2) { inset: var(--mi-space-2); border-color: var(--mi-accent-dim); }
.mp-mark i:nth-child(3) { inset: var(--mi-space-3); border: 0; background: var(--mi-accent); box-shadow: var(--mi-glow-node); }
.mp-observing { display: inline-flex; align-items: center; gap: var(--mi-space-2); color: var(--mi-accent); font-size: var(--mi-fs-caption); white-space: nowrap; }
.mp-observing i { width: var(--mi-status-dot); height: var(--mi-status-dot); border-radius: var(--mi-radius-lg); background: var(--mi-accent); box-shadow: var(--mi-glow-node); }
.mp-modes { display: inline-flex; gap: var(--mi-space-1); margin-left: auto; }
.mp-modes button { min-height: var(--mi-control-height-sm); padding: 0 var(--mi-space-3); border: var(--mi-border-width) solid transparent; border-radius: var(--mi-radius-control); background: transparent; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); cursor: pointer; }
.mp-modes button.on { border-color: var(--mi-border-level-3); background: var(--mi-surface-level-3); color: var(--mi-text); box-shadow: var(--mi-shadow-level-2); }
.mp-close { flex: 0 0 var(--mi-icon-button); width: var(--mi-icon-button); height: var(--mi-icon-button); padding: 0; border: var(--mi-border-width) solid transparent; border-radius: var(--mi-radius-control); background: transparent; color: var(--mi-text-secondary); font-size: var(--mi-fs-title); line-height: 1; cursor: pointer; }
.mp-close:hover { border-color: var(--mi-border); background: var(--mi-hover); color: var(--mi-text); }
.mp-inline-status { display: flex; align-items: center; gap: var(--mi-space-2); margin: var(--mi-space-3) var(--mi-space-5) 0; color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); }
.mp-inline-status i { width: var(--mi-status-dot); height: var(--mi-status-dot); border-radius: var(--mi-radius-lg); background: var(--mi-accent); animation: mp-status var(--mi-duration-flow) var(--mi-ease-standard) infinite; }
.mp-message { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--mi-space-3); margin: var(--mi-space-3) var(--mi-space-5) 0; padding: var(--mi-space-2) var(--mi-space-3); border-left: var(--mi-focus-width) solid var(--mi-border-strong); background: var(--mi-surface); color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.mp-message.is-error { border-left-color: var(--mi-danger); color: var(--mi-danger); }
.mp-message.is-ok { border-left-color: var(--mi-success); color: var(--mi-success); }
.mp-message button { flex: 0 0 auto; padding: 0; border: 0; background: transparent; color: var(--mi-text); cursor: pointer; }
/* 八个 tab 在 719px 可视宽度里一行放不下：换行（而不是隐藏滚动条），
   让人一眼看见「还有内容」，不把会话钩子/历史摘要藏到视口外。 */
.mp-tabs { display: flex; flex: 0 0 auto; flex-wrap: wrap; align-content: flex-start; gap: var(--mi-space-2) var(--mi-space-5); min-height: var(--mi-space-7); padding: 0 var(--mi-space-5); border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); }
.mp-tab { position: relative; flex: 0 0 auto; min-height: var(--mi-space-7); padding: 0; border: 0; background: transparent; color: var(--mi-text-muted); font-size: var(--mi-fs-secondary); white-space: nowrap; cursor: pointer; }
.mp-tab::after { content: ''; position: absolute; right: 0; bottom: 0; left: 0; height: var(--mi-focus-width); background: var(--mi-accent); transform: scaleX(0); transform-origin: left; transition: transform var(--mi-duration-base) var(--mi-ease-standard); }
.mp-tab.on { color: var(--mi-text); }
.mp-tab.on::after { transform: scaleX(1); }
.mp-tab span { margin-left: var(--mi-space-1); color: var(--mi-text-muted); font-family: var(--mi-font-mono); font-size: var(--mi-fs-caption); font-variant-numeric: tabular-nums; }
.mp-body { flex: 1 1 auto; min-width: 0; overflow-x: hidden; overflow-y: auto; padding: var(--mi-space-5) var(--mi-space-5); scrollbar-color: var(--mi-border-strong) transparent; }
.mp.is-overview .mp-body { padding-top: 0; }
/* 子 Agent 的上下文只对活动实例可读：给明确空状态，不拿别人的 inspection 顶替。 */
.mp-scope-note { margin: var(--mi-space-3) var(--mi-space-5) 0; padding: var(--mi-space-2) var(--mi-space-3); border-left: var(--mi-focus-width) solid var(--mi-accent-dim); background: var(--mi-surface); }
.mp-scope-note strong { color: var(--mi-text-secondary); font-size: var(--mi-fs-caption); font-weight: var(--mi-weight-medium); }
.mp-scope-note p { margin: var(--mi-space-1) 0 0; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.mp-scope-note code, .mp-agent-empty code { overflow-wrap: anywhere; font-family: var(--mi-font-mono); }
.mp-agent-empty { display: flex; flex-direction: column; align-items: flex-start; justify-content: center; gap: var(--mi-space-2); }
.mp-agent-empty-mark { width: var(--mi-space-6); height: var(--mi-space-6); margin-bottom: var(--mi-space-2); border: var(--mi-border-width) dashed var(--mi-border-strong); border-radius: var(--mi-radius-lg); }
.mp-agent-empty strong { font-size: var(--mi-fs-title); line-height: var(--mi-lh-title); font-weight: var(--mi-weight-medium); }
.mp-agent-empty p { max-width: var(--mi-copy-width); margin: 0; color: var(--mi-text-secondary); font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); }
.mp-agent-empty small { max-width: var(--mi-copy-width); color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.mp-agent-empty button { min-height: var(--mi-control-height); margin-top: var(--mi-space-2); padding: 0 var(--mi-space-4); border: var(--mi-border-width) solid var(--mi-border-level-3); border-radius: var(--mi-radius-control); background: var(--mi-surface-level-2); color: var(--mi-text); font-family: inherit; font-size: var(--mi-fs-secondary); cursor: pointer; }
.mp-hint, .mp-ok, .mp-error { margin: var(--mi-space-2) 0; color: var(--mi-text-muted); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.mp-ok { color: var(--mi-success); }
.mp-error { color: var(--mi-danger); }
.mp-meta { margin: 0 0 var(--mi-space-3); color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.mp-textarea, .mp-input { width: 100%; border: var(--mi-border-width) solid var(--mi-border-level-2); border-radius: var(--mi-radius-control); background: var(--mi-bg-deep); color: var(--mi-text); font: var(--mi-fs-body)/var(--mi-lh-body) var(--mi-font-ui); padding: var(--mi-space-3); resize: vertical; }
.mp-textarea { margin-bottom: var(--mi-space-2); }
.mp-input { min-height: var(--mi-control-height); margin-bottom: var(--mi-space-2); padding-block: var(--mi-space-1); }
.mp-input::placeholder, .mp-textarea::placeholder { color: var(--mi-text-muted); }
.mp-select { flex: 0 0 var(--mi-select-width); width: var(--mi-select-width); }
.mp-field { display: block; margin-bottom: var(--mi-space-3); color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); }
.mp-field .mp-input, .mp-field .mp-textarea { margin-top: var(--mi-space-1); color: var(--mi-text); }
.mp-actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: var(--mi-space-2); margin-top: var(--mi-space-2); }
.mp-actions .mp-hint { margin: 0 auto 0 0; }
.mp-btn, .mp-mini { min-height: var(--mi-control-height); padding: 0 var(--mi-space-4); border: var(--mi-border-width) solid var(--mi-border-level-3); border-radius: var(--mi-radius-control); background: var(--mi-surface-level-2); color: var(--mi-text); font-size: var(--mi-fs-secondary); cursor: pointer; transition: background var(--mi-duration-fast) var(--mi-ease-standard), border-color var(--mi-duration-fast) var(--mi-ease-standard); }
.mp-btn { border-color: var(--mi-accent); background: var(--mi-accent); color: var(--mi-accent-ink); }
.mp-mini { min-height: var(--mi-control-height-sm); padding-inline: var(--mi-space-3); color: var(--mi-text-secondary); font-size: var(--mi-fs-caption); }
.mp-btn:disabled, .mp-mini:disabled { opacity: var(--mi-opacity-disabled); cursor: default; }
.mp-btn:hover:not(:disabled) { background: var(--mi-accent-strong); }
.mp-mini:hover:not(:disabled) { border-color: var(--mi-accent-dim); background: var(--mi-hover); color: var(--mi-text); }
.mp-danger { border-color: var(--mi-danger); color: var(--mi-danger); }
.mp-item { padding: var(--mi-space-5) 0; border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); }
.mp-item-head { display: flex; align-items: center; justify-content: space-between; gap: var(--mi-space-3); margin-bottom: var(--mi-space-3); color: var(--mi-text-muted); font-size: var(--mi-fs-caption); }
.mp-pin { display: inline-flex; align-items: center; gap: var(--mi-space-1); cursor: pointer; }
.mp-pin input { accent-color: var(--mi-accent); }
.mp-warn { color: var(--mi-warning); font-size: var(--mi-fs-secondary); }
.mp-funnel { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 var(--mi-space-5); }
.mp-funnel-step { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: baseline; gap: var(--mi-space-2); padding: var(--mi-space-2) 0; border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); }
.mp-funnel-step strong { color: var(--mi-text); font-family: var(--mi-font-mono); font-variant-numeric: tabular-nums; }
.mp-funnel-zero strong { color: var(--mi-warning); }
.mp-funnel-unit, .mp-funnel-meta { color: var(--mi-text-muted); font-style: normal; font-size: var(--mi-fs-caption); }
.mp-funnel-meta { grid-column: 1 / -1; }
.mp-funnel-handoff { grid-column: 1 / -1; padding: var(--mi-space-2) 0; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); }
.mp-funnel-unmeasured { border-bottom-style: dashed; }
.mp-funnel-note { margin: var(--mi-space-2) 0 0; color: var(--mi-success); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.mp-funnel-stall { color: var(--mi-warning); }
.mp-funnel-caveat, .mp-funnel-meta-line { color: var(--mi-text-muted); }
.mp-links { margin-top: var(--mi-space-3); padding-top: var(--mi-space-2); border-top: var(--mi-border-width) solid var(--mi-border-subtle); }
.mp-link { display: flex; align-items: center; justify-content: space-between; gap: var(--mi-space-3); padding: var(--mi-space-1) 0; color: var(--mi-text-muted); font-size: var(--mi-fs-caption); }
.mp-link-add { margin-top: var(--mi-space-3); }
.mp-link-add .mp-input, .mp-add .mp-input { margin-bottom: 0; }
.mp-retrievable { margin-top: var(--mi-space-5); padding-top: var(--mi-space-4); border-top: var(--mi-border-width) solid var(--mi-border-subtle); }
.mp-retrievable summary { margin-bottom: var(--mi-space-2); color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); cursor: pointer; }
.mp-body-text { max-width: var(--mi-copy-width); margin: 0 0 var(--mi-space-3); color: var(--mi-text-secondary); font-size: var(--mi-fs-body); line-height: var(--mi-lh-body); white-space: pre-wrap; }
.mp-add { display: flex; flex-wrap: wrap; gap: var(--mi-space-2); align-items: flex-start; margin: var(--mi-space-3) 0; }
.mp-add .mp-input { flex: 1 1 auto; width: auto; min-width: calc(var(--mi-space-8) * 2); }
.mp-add .mp-btn { flex: 0 0 auto; white-space: nowrap; }
.mp-readonly { margin-top: var(--mi-space-5); padding: var(--mi-space-4); border: var(--mi-border-width) solid var(--mi-border-level-2); border-radius: var(--mi-radius-card); background: var(--mi-surface-level-2); box-shadow: var(--mi-shadow-level-2); color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); }
.mp-readonly > strong { display: block; margin-bottom: var(--mi-space-3); color: var(--mi-text); font-size: var(--mi-fs-section); line-height: var(--mi-lh-section); font-weight: var(--mi-weight-semibold); }
.mp-receipts { color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); }
.mp-disk-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--mi-space-3); margin: 0 0 var(--mi-space-4); font-size: var(--mi-fs-secondary); }
.mp-disk-stats span { padding-bottom: var(--mi-space-2); border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); }
.mp-disk-stats strong { display: block; color: var(--mi-text); font-family: var(--mi-font-mono); font-variant-numeric: tabular-nums; }
.mp-disk-note { margin: var(--mi-space-2) 0; color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); line-height: var(--mi-lh-secondary); }
.mp-disk-hint { display: block; margin-top: var(--mi-space-1); color: var(--mi-text-muted); font-size: var(--mi-fs-caption); line-height: var(--mi-lh-caption); }
.mp-disk .mp-field .mp-input { max-width: calc(var(--mi-space-8) * 2); }
.mp-disk-whitelist { margin: var(--mi-space-4) 0; }
.mp-disk-wl-title { color: var(--mi-text); font-size: var(--mi-fs-secondary); }
.mp-disk-chips { display: flex; flex-direction: column; margin: var(--mi-space-2) 0; }
.mp-disk-chip { display: flex; align-items: center; justify-content: space-between; gap: var(--mi-space-3); padding: var(--mi-space-2) 0; border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); color: var(--mi-text-secondary); font-size: var(--mi-fs-secondary); }
.mp-disk-chip-builtin { border-bottom-style: dashed; }
.mp-disk-chip-x { width: var(--mi-control-height-sm); height: var(--mi-control-height-sm); padding: 0; border: 0; background: transparent; color: var(--mi-text-muted); cursor: pointer; }
.mp-disk-chip-x:hover:not(:disabled) { color: var(--mi-danger); }
.mp-disk-wl-add { display: flex; gap: var(--mi-space-2); align-items: flex-start; }
.mp-disk-wl-add .mp-input { flex: 1; margin-bottom: 0; }
/* 浏览器契约（frontend/tests/memoryFunnel.browser.mjs、diskRetention.browser.mjs）：
   健康漏斗与磁盘审计这两块是"要逐项读数"的密集审计区，字号不得低于 12px。
   2026-11-04 全局字阶收窄后，这里显式保底，避免契约测试与可读性一起退化。 */
.mp-funnel-step, .mp-funnel-note,
.mp-disk-stats, .mp-disk-note, .mp-disk-hint,
.mp-disk-chip, .mp-disk-wl-title {
  font-size: 12px;
  line-height: 18px;
}
.mp-health-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: var(--mi-space-5); border-block: var(--mi-border-width) solid var(--mi-border-subtle); }
.mp-health-card { display: flex; flex-direction: column; gap: var(--mi-space-1); padding: var(--mi-space-4) var(--mi-space-3); border-left: var(--mi-border-width) solid var(--mi-border-subtle); }
.mp-health-card:nth-child(4n + 1) { border-left: 0; }
.mp-health-card strong { color: var(--mi-text); font-family: var(--mi-font-mono); font-size: var(--mi-fs-title); line-height: var(--mi-lh-title); font-variant-numeric: tabular-nums; }
.mp-health-card span { color: var(--mi-text-muted); font-size: var(--mi-fs-caption); }
.mp-health-warn strong { color: var(--mi-warning); }
.mp-health-warnings { margin: var(--mi-space-4) 0; padding-left: var(--mi-space-3); border-left: var(--mi-focus-width) solid var(--mi-warning); }
.mp-health-warnings > strong { color: var(--mi-warning); }
.mp-health-warnings p { margin: var(--mi-space-1) 0 0; }
.mp-health-row { display: flex; justify-content: space-between; gap: var(--mi-space-4); padding: var(--mi-space-2) 0; border-bottom: var(--mi-border-width) solid var(--mi-border-subtle); font-variant-numeric: tabular-nums; }
.mp-health-row:last-child { border-bottom: 0; }
.mp-pre { max-width: var(--mi-copy-width); max-height: var(--mi-pre-height); margin: var(--mi-space-2) 0 0; padding: var(--mi-space-3); overflow: auto; border: var(--mi-border-width) solid var(--mi-border-subtle); border-radius: var(--mi-radius-sm); background: var(--mi-bg-deep); color: var(--mi-text-secondary); font: var(--mi-fs-caption)/var(--mi-lh-caption) var(--mi-font-mono); white-space: pre-wrap; word-break: break-word; }
.mp-foot { display: flex; align-items: center; justify-content: space-between; gap: var(--mi-space-3); min-height: var(--mi-space-7); padding: var(--mi-space-2) var(--mi-space-5); border-top: var(--mi-border-width) solid var(--mi-border-subtle); background: var(--mi-bg-deep); color: var(--mi-text-muted); font-size: var(--mi-fs-caption); }
.mp-foot > span { font-variant-numeric: tabular-nums; }
.mp-foot-actions { display: flex; gap: var(--mi-space-2); }
.mp-foot .mp-mini.on { border-color: var(--mi-accent-dim); background: var(--mi-hover); color: var(--mi-text); }
.mp :deep(button:focus-visible), .mp :deep(textarea:focus-visible), .mp :deep(input:focus-visible), .mp :deep(select:focus-visible), .mp :deep(summary:focus-visible) { outline: var(--mi-focus-width) solid var(--mi-accent); outline-offset: var(--mi-space-1); }
@keyframes mp-status { 0%, 100% { opacity: var(--mi-opacity-skeleton); } 50% { opacity: 1; } }
@media (max-width: 1280px) { .mp.is-overview { width: min(var(--mi-panel-width-md), 100vw); } }
@media (max-width: 1100px) {
  .mp.is-overview { width: min(var(--mi-panel-width-sm), 100vw); }
  .mp-head, .mp-tabs { padding-inline: var(--mi-space-4); }
  .mp-body { padding-inline: var(--mi-space-4); }
  .mp-foot { padding-inline: var(--mi-space-4); }
  .mp-health-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .mp-health-card:nth-child(odd) { border-left: 0; }
  .mp-health-card:nth-child(even) { border-left: var(--mi-border-width) solid var(--mi-border-subtle); }
}
@media (max-width: 720px) {
  .mp, .mp.is-overview { width: 100vw; }
  .mp-observing { display: none; }
  .mp-funnel, .mp-disk-stats { grid-template-columns: 1fr; }
  .mp-identity span:last-child { max-width: var(--mi-copy-width); }
}
@media (prefers-reduced-motion: reduce) {
  .mp, .mp *, .mp *::before, .mp *::after, .mp :deep(*), .mp :deep(*)::before, .mp :deep(*)::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
  .mp-panel-enter-from, .mp-panel-leave-to { transform: none; }
}
</style>
