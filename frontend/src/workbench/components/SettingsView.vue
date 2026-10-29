<script setup lang="ts">
// 统一设置页：左侧分组导航（用量费用 / 网络搜索 / MCP / 智能体），右侧对应面板。
// 网络搜索与 URL 获取走 settingsApi 的专用端点 /api/web-settings（密钥不回显）；
// 不能复用 /api/config —— 只带 web_* 的局部请求会按默认 provider="mock" 切走模型。
// MCP 走 mcpApi；智能体为本地预设（localStorage），后续可升级为后端持久化。
import { reactive, ref, watch, computed, onUnmounted } from 'vue'
import {
  settingsApi, agentCapacityApi, memorySettingsApi, mcpApi, harnessApi,
  agentApi, modelResidencyApi,
  WEB_SEARCH_PROVIDERS, WEB_FETCH_PROVIDERS,
  type SettingsConfigInfo, type ModelConfigInfo, type ProviderOption, type McpServer,
  type McpPendingProposal,
  type AgentCapacityInfo,
  type AgentCapacityRanges,
  type McpCapabilityCandidate,
  type BudgetStatus, type TraceSummary,
  type McpAutoConnectCandidate, type McpAutoConnectConfig, type McpProbeRes, type McpDirectoryResult,
  type McpRegisterStatusRes, type McpRegisterTier, type McpRegisterState,
  type UserProfile, type MemoryItem, type LearnedRow,
  type ModelStatus,
} from '../api'
import Icon from './Icon.vue'
// 2026-10-19：这两个面板原挂在顶栏「更多」菜单（浮层形态），现以内嵌形态收进
// 设置页的「运行与引擎」分区。
import AgentPolicyPanel from './AgentPolicyPanel.vue'
import EngineConnectPopover from './EngineConnectPopover.vue'
import EngineSetupPanel from './EngineSetupPanel.vue'
import { appEvents } from '../eventBus'
import { costLabel, money, type PricingState } from '../composables/costLabel'
import { SKINS, loadSkin, setSkin, onSkinChange, type SkinKey } from '../skins'
import { loadMaterial, setMaterial, onMaterialChange, type Material } from '../glassPointer'
import { LAYOUTS, loadLayout, setLayout, onLayoutChange, type LayoutKey } from '../layoutMode'
// 对话展示偏好：与问答页 / 模型设置弹窗同一份（reactive 视图 + localStorage）。
// 这里不复制状态，直接绑到共享对象上，改完写存储并派发事件让两个入口同时刷新。
import { prefs as dp, saveAskPrefs } from '../../ask/askPrefs'

const props = defineProps<{ visible: boolean; initialTab?: 'usage' | 'search' | 'mcp' | 'agent' | 'model' | 'runtime' | 'appearance' }>()
const emit = defineEmits<{ (e: 'close'): void }>()

type Tab = 'usage' | 'search' | 'mcp' | 'agent' | 'model' | 'runtime' | 'appearance'
const tab = ref<Tab>('usage')
// 记录用户最近一次主动选择。设置面板的初始化请求是异步的，不能在请求完成后
// 用入口标签覆盖掉用户已经点选的页面。
const tabTouched = ref(false)

function selectTab(next: Tab) {
  tabTouched.value = true
  tab.value = next
}

// ---------------- 皮肤（视觉风格）----------------
// 纯前端：写 localStorage + 改 <html data-skin>，skins.css 里的 html[data-skin='x']
// 覆写 :root 令牌，浏览器立即重算全部 var()，无需刷新也不走后端。
const skin = ref<SkinKey>(loadSkin())

function chooseSkin(key: SkinKey) {
  // setSkin 内部会夹住非法值并返回实际生效的 key，用返回值回填避免 UI 显示与实际不一致
  skin.value = setSkin(key)
  const s = SKINS.find((x) => x.key === key)
  appEvents.emit('docmind:appearance-changed', { kind: 'skin', label: s?.name ?? key })
}

// 另一个入口（问答页 / 另一个窗口）改了皮肤时同步本组件的选中态
onSkinChange((k) => { skin.value = k })

// ---------------- 对话与运行展示偏好 ----------------
// 控件直接绑共享 dp（v-model 已经把新值写进这份 reactive），这里只负责落盘。
// 全仓只有这一份内存状态，所以不需要再广播什么 —— 见 askPrefs.ts 顶部注释。
function saveDisplayPrefs() {
  saveAskPrefs({ ...dp })
}
function setMaxRefs(e: Event) {
  dp.maxRefs = Number((e.target as HTMLSelectElement).value)
  saveDisplayPrefs()
}

// ---------------- 材质（玻璃 / 实体）----------------
// 材质与皮肤正交：皮肤管颜色，材质管表面。玻璃 = 半透明 + 背后模糊 + 高光描边。
const material = ref<Material>(loadMaterial())

function chooseMaterial(m: Material) {
  material.value = setMaterial(m)
  appEvents.emit('docmind:appearance-changed', {
    kind: 'material', label: m === 'glass' ? '玻璃' : '实体',
  })
}

onMaterialChange((m) => { material.value = m })

// 风格（造型语言）选择器已随造型收敛移除：造型固定为 brutal（零圆角 + 2px 墨描边
// + 硬阴影 + 孔版颗粒），令牌在 skins.css，data-style 仍由 styleMode.initStyle() 写入。

// ---------------- 布局（空间构成）----------------
// 皮肤管颜色、风格管造型、布局管空间：侧栏在哪、主区多宽、该居中还是铺满。
const layout = ref<LayoutKey>(loadLayout())

function chooseLayout(k: LayoutKey) {
  layout.value = setLayout(k)
  const l = LAYOUTS.find((x) => x.key === k)
  appEvents.emit('docmind:appearance-changed', { kind: 'layout', label: l?.name ?? k })
}

onLayoutChange((k) => { layout.value = k })

const loading = ref(false)
const errorMsg = ref('')
const savedMsg = ref('')

// ---------------- 长期记忆与用户画像 ----------------
const profileLoading = ref(false)
const profileSaving = ref(false)
const profileError = ref('')
const profileSaved = ref('')
const profile = ref<UserProfile>({ display_name: '', language: '', timezone: '', location: '', preferences: {}, goals: [], notes: '' })
const memoryItems = ref<MemoryItem[]>([])
const memoryLoading = ref(false)
async function loadMemoryProfile() {
  profileLoading.value = true; memoryLoading.value = true; profileError.value = ''
  try {
    const [p, m] = await Promise.all([agentApi.userProfile(), agentApi.memories('', 30)])
    if (p.profile) profile.value = { ...profile.value, ...p.profile }
    memoryItems.value = m.items || []
  } catch (e) { profileError.value = (e as { message?: string }).message || '读取长期记忆失败' }
  finally { profileLoading.value = false; memoryLoading.value = false }
}
async function saveMemoryProfile() {
  profileSaving.value = true; profileError.value = ''; profileSaved.value = ''
  try {
    const r = await agentApi.updateUserProfile({
      display_name: profile.value.display_name, language: profile.value.language,
      timezone: profile.value.timezone, location: profile.value.location,
      preferences: profile.value.preferences, goals: profile.value.goals, notes: profile.value.notes,
    })
    if (r.profile) profile.value = { ...profile.value, ...r.profile }
    profileSaved.value = '用户画像已保存'
  } catch (e) { profileError.value = (e as { message?: string }).message || '保存用户画像失败' }
  finally { profileSaving.value = false }
}
async function deleteMemory(id: string) {
  try { const r = await agentApi.deleteMemory(id); if (r.ok) memoryItems.value = memoryItems.value.filter(item => item.id !== id) }
  catch (e) { profileError.value = (e as { message?: string }).message || '删除记忆失败' }
}
const clearingLearned = ref(false)
async function clearLearned() {
  if (clearingLearned.value) return
  clearingLearned.value = true; profileError.value = ''; profileSaved.value = ''
  try {
    const r = await agentApi.clearLearnedProfile()
    if (r.ok === false) { profileError.value = r.error || '清空学习习惯失败'; return }
    if (r.profile) profile.value = { ...profile.value, ...r.profile }
    profileSaved.value = '已清空学到的使用习惯（不影响你手动填写的画像）'
  } catch (e) { profileError.value = (e as { message?: string }).message || '清空学习习惯失败' }
  finally { clearingLearned.value = false }
}
const learnedGroups = computed(() => {
  const l = profile.value.learned || {}
  const defs: Array<[string, LearnedRow[] | undefined]> = [
    ['回答风格', l.answer_style], ['常改文件', l.focus], ['常用能力', l.tasks],
  ]
  return defs
    .map(([label, items]) => ({ label, items: (items || []).filter(r => r && r.value) }))
    .filter(g => g.items.length)
})
function updateGoals(event: Event) {
  const value = (event.target as HTMLTextAreaElement | null)?.value || ''
  profile.value.goals = value.split('\n').map(v => v.trim()).filter(Boolean)
}

// ---------------- Token 用量 / 费用控制 ----------------
const usageLoading = ref(false)
const usageSaving = ref(false)
const usageError = ref('')
const usageSaved = ref('')
const budget = ref<BudgetStatus | null>(null)
const traceSummary = ref<TraceSummary | null>(null)
const budgetLimit = ref('0')
const perMinuteCalls = ref('0')
const perMinuteCost = ref('0')

const budgetRemaining = computed(() => {
  const limit = Number(budget.value?.global_limit || 0)
  if (limit <= 0) return null
  return Math.max(0, limit - Number(budget.value?.global_spent || 0))
})
const providerUsage = computed(() => Object.entries(traceSummary.value?.by_provider || {})
  .map(([provider, item]) => {
    const cacheRead = Number(item.cache_read_tokens || 0)
    const cacheWrite = Number(item.cache_creation_tokens || 0)
    const saved = Number(item.cache_saved_cny || 0)
    const parts = [`缓存命中 ${tokens(cacheRead)}`]
    if (cacheWrite > 0) parts.push(`写入 ${tokens(cacheWrite)}`)
    if (saved > 0) parts.push(`省 ¥${saved.toFixed(4)}`)
    if (cacheRead <= 0 && cacheWrite <= 0) parts.push('—')
    return {
      provider,
      ...item,
      ...providerMeta(provider),
      // 后端已判定好这个 0 是"免费/本地/未配置"，前端只翻成人话，不自己猜。
      cost: costLabel(item.cost_cny, (item as { pricing_state?: PricingState }).pricing_state),
      cache_read_tokens: cacheRead,
      cacheText: parts.join(' · '),
      cacheTitle: cacheRead > 0
        ? `输入 ${tokens(item.prompt_tokens)} 中有 ${tokens(cacheRead)} 命中缓存（${pctOf(cacheRead, Number(item.prompt_tokens || 0))}），按折扣价计费`
        : '这个供应商没有缓存命中记录',
    }
  })
  .sort((a, b) => b.tokens - a.tokens))

function pctOf(part: number, whole: number): string {
  if (!whole) return '0%'
  return `${((part / whole) * 100).toFixed(1)}%`
}

function providerMeta(provider: string): { label: string; kind: string; simulated: boolean } {
  const key = (provider || '').toLowerCase()
  if (key === 'mock') return { label: 'mock', kind: '离线估算', simulated: true }
  if (key === 'fake') return { label: 'fake', kind: '自动化测试', simulated: true }
  if (key === '?' || !key) return { label: '未知来源', kind: '无用量', simulated: true }
  if (key === 'flow') return { label: '内部工作流', kind: '无模型调用', simulated: true }
  if (key === 'ollama') return { label: 'Ollama', kind: '本地模型', simulated: false }
  if (key === 'llamacpp') return { label: 'llama.cpp', kind: '本地模型', simulated: false }
  return { label: provider, kind: '云端模型', simulated: false }
}

function tokens(value: number | null | undefined): string {
  return Number(value || 0).toLocaleString('zh-CN')
}

/**
 * 缓存命中的 token 数。
 *
 * 注意：`prompt_tokens` **已经包含**缓存命中部分（provider 报的 prompt_tokens 就是
 * 缓存后的总量），所以这里**不能**把它再加到总 Token 上——那会把账算重。
 * 正确的说法是"其中多少走了缓存折扣"，所以 UI 上写成"含缓存命中 N（M%）"。
 */
const cacheReadTokens = computed(() =>
  Number(traceSummary.value?.cache_read_tokens || 0))
const cacheWriteTokens = computed(() =>
  Number(traceSummary.value?.cache_creation_tokens || 0))

/** 缓存命中占输入的比例。分母用 prompt_tokens（含缓存），这才是"多少输入走了缓存"。 */
const cacheRatio = computed(() => {
  const prompt = Number(traceSummary.value?.prompt_tokens || 0)
  if (prompt <= 0 || !cacheReadTokens.value) return '0'
  return ((cacheReadTokens.value / prompt) * 100).toFixed(1)
})

/**
 * 缓存命中省下的钱。**后端算好的**，前端只负责显示。
 *
 * 不在前端算：单价在 `.docmind_pricing.json`，还有阶梯档（OpenRouter 长上下文会翻倍），
 * 前端拿不到也不该重复实现一遍——两处逻辑各写一遍，阶梯档一改就对不上，
 * 而且没人会发现（省的钱会比该收的钱还离谱）。
 */
const cacheSavedCny = computed(() =>
  Number(traceSummary.value?.cache_saved_cny || 0))

const cacheSavedNote = computed(() => {
  const saved = cacheSavedCny.value
  // 为 0 有两种含义：没命中缓存 / 没配单价算不出。只报有数的那个，
  // 拿 0 冒充"省了 0 元"就是把"没算"说成"算了"。
  if (saved <= 0) return ''
  return `其中缓存命中按折扣价计费，约省 ¥${saved.toFixed(4)}`
})

/** 缓存写入的 token。DeepSeek / Anthropic 的写入价**高于**全价（1.25x），所以不叫"省"。 */
const cacheWriteNote = computed(() => {
  const wrote = cacheWriteTokens.value
  if (wrote <= 0) return ''
  return `（另含缓存写入 ${tokens(wrote)}，写入价高于全价，不计入节省）`
})

async function loadUsage() {
  usageLoading.value = true
  usageError.value = ''
  try {
    const [b, t] = await Promise.all([harnessApi.budget(), harnessApi.trace(1)])
    budget.value = b.status
    traceSummary.value = t.summary
    budgetLimit.value = String(b.status.global_limit || 0)
    perMinuteCalls.value = String(b.status.per_minute_calls_limit || 0)
    perMinuteCost.value = String(b.status.per_minute_cost_limit || 0)
  } catch (e) {
    usageError.value = (e as { message?: string }).message || '读取用量失败'
  } finally {
    usageLoading.value = false
  }
}

async function saveUsage() {
  const values = [budgetLimit.value, perMinuteCalls.value, perMinuteCost.value].map(Number)
  if (values.some(v => !Number.isFinite(v) || v < 0)) {
    usageError.value = '预算和限流必须是不小于 0 的数字；0 表示不限。'
    return
  }
  usageSaving.value = true
  usageError.value = ''
  usageSaved.value = ''
  try {
    const r = await harnessApi.setUsageLimits(values[0], values[1], values[2])
    if (!r.ok) {
      usageError.value = r.error || '保存失败'
      return
    }
    usageSaved.value = '用量控制已保存'
    await loadUsage()
  } catch (e) {
    usageError.value = (e as { message?: string }).message || '保存失败'
  } finally {
    usageSaving.value = false
  }
}

/** 累计花费计数清零。2026-10-19 从 AI 运行台的「花费」tab 搬到这里 ——
 *  那个 tab 已删（费用展示与预算设置本来就都在这个分区），但「清零」这个动作
 *  在两页里没有第二处落点，删掉就等于功能消失，所以随它一起搬过来。 */
async function resetUsageSpent() {
  if (!window.confirm('确定把累计花费计数清零吗？（不影响模型与账本文件，只重置计数）')) return
  usageSaving.value = true
  usageError.value = ''
  usageSaved.value = ''
  try {
    const r = await harnessApi.resetBudget()
    if (!r.ok) {
      usageError.value = '清零失败'
      return
    }
    usageSaved.value = '累计计数已清零'
    await loadUsage()
  } catch (e) {
    usageError.value = (e as { message?: string }).message || '清零失败'
  } finally {
    usageSaving.value = false
  }
}

// ---------------- 网络搜索 / URL 获取 ----------------
const cfg = ref<SettingsConfigInfo & ModelConfigInfo | null>(null)
const wsProvider = ref<ProviderOption['value']>('builtin_auto')
const wsApiKey = ref('')
const wsApiUrl = ref('')
const wfProvider = ref<ProviderOption['value']>('builtin')
const wfApiKey = ref('')
const wfApiUrl = ref('')
const showWsKey = ref(false)
const showWfKey = ref(false)
const searchSaving = ref(false)

// ---------------- Agent 工作负载 / 显存感知 ----------------
// 六个字段的键名只在这张表里出现一次：上界、校验、报错文案、确认清单全部由它推导。
// 界面上不再有任何硬编码的容量数字 —— 数字的唯一来源是 GET /api/agent-capacity
// 的 ranges（后端 c457301 起下发）。以前是「前端抄一份 + 一条测试锁住两份相等」，
// 那是事后报警；消费载荷才是单源。
const CAPACITY_FIELDS = {
  maxAgents: 'max_agents',
  mainMaxSteps: 'main_max_steps',
  childMaxSteps: 'child_max_steps',
  maxNudges: 'max_nudges',
  maxFinalContinuations: 'max_final_continuations',
  taskTokenBudget: 'task_token_budget',
} as const
type CapacityField = keyof typeof CAPACITY_FIELDS
const CAPACITY_KEYS = Object.keys(CAPACITY_FIELDS) as CapacityField[]
const CAPACITY_LABEL: Record<CapacityField, string> = {
  maxAgents: 'Agent 数量', mainMaxSteps: '主 Agent 工具步数', childMaxSteps: '子代理工具步数',
  maxNudges: '纠偏次数', maxFinalContinuations: '续写次数', taskTokenBudget: '任务 Token 上限',
}

const capacityInfo = ref<AgentCapacityInfo | null>(null)
// 初值一律空串：真实值由 loadAgentCapacity() 填。以前这里写着 '8' / '24' / '50000'，
// 后端默认全改成 0（不限）之后它们就是过期复制 —— 加载失败时界面会亮着一排
// 谁都没同意的数字。空框用户看得见，比塞个错值诚实。
// ⚠ 类型必须是 string | number：v-model 绑在 <input type="number"> 上，Vue 写回来的是
// **数字**，把它声明成 string 会让 .trim() 在运行时抛 TypeError（vue-tsc 不报，因为
// 声明就是假的 —— 实测踩过：保存按钮点了没任何反应，异常只出现在 console 里）。
const capacityValues = reactive<Record<CapacityField, string | number>>({
  maxAgents: '', mainMaxSteps: '', childMaxSteps: '', maxNudges: '', maxFinalContinuations: '', taskTokenBudget: '',
})
/** 读某个容量框的当前值，统一成字符串。 */
function capRaw(field: CapacityField): string {
  const v = capacityValues[field]
  return v === null || v === undefined ? '' : String(v).trim()
}
function capNum(field: CapacityField): number {
  return Number(capRaw(field))
}
const localAutoLimit = ref(true)
const capacitySaving = ref(false)
const capacityError = ref('')
const capacitySaved = ref('')

// 长期记忆优化总开关：一个可点的 switch，切换即 POST /api/memory-settings 生效并落盘。
const memoryOptimized = ref(false)
const memorySaving = ref(false)
const memorySaved = ref('')

// 透明学习习惯开关：独立于总开关，默认关；切换即 POST /api/memory-settings 的 memory_learn。
const memoryLearn = ref(false)
const learnSaving = ref(false)
const learnSaved = ref('')

/** 上界只从载荷取。载荷没到（还在加载 / 老后端没有 ranges）时返回 0，
 *  调用方据此**不设 max** —— 回落到一个硬编码数字就等于把刚删掉的第二份拷贝请回来。 */
function capacityMax(field: CapacityField): number {
  return capacityInfo.value?.ranges?.[CAPACITY_FIELDS[field]] ?? 0
}

/** 下界也走载荷（`<字段>_min`），与上界同一个来源。
 *  只有 task_token_budget 有正数下界：0 是合法的「不限」，所以它不是 input 的
 *  min 属性（那会把 0 判成非法），只用于提前拦住「填了正数却小到跑不完一轮」。 */
function capacityFloor(field: CapacityField): number {
  const key = `${CAPACITY_FIELDS[field]}_min` as keyof AgentCapacityRanges
  return Number(capacityInfo.value?.ranges?.[key] ?? 0) || 0
}

function validateCapacity(): string {
  for (const field of CAPACITY_KEYS) {
    const raw = capRaw(field)
    const v = Number(raw)
    if (raw === '' || !Number.isInteger(v) || v < 0) return `${CAPACITY_LABEL[field]}需为非负整数；0 表示不设固定上限。`
    const hi = capacityMax(field)
    // 越界必须当场拒绝：后端 normalize_settings 越界时回落到默认值，而默认现在是
    // 0 = 不限 —— 用户填 5000 会被静默改成「不限」，行为与意图正好相反。
    if (hi > 0 && v > hi) return `${CAPACITY_LABEL[field]}不能大于服务端下发的上限 ${hi}。`
    const lo = capacityFloor(field)
    // 后端对 0 < 值 < 下界 是 raise → 400（不是夹取），所以这里拦的是「提交必然失败」，
    // 报错文案与后端一致：要么够大，要么填 0。
    if (lo > 0 && v > 0 && v < lo) return `${CAPACITY_LABEL[field]}至少要 ${lo} 才跑得完一轮问答（实测值）；想完全不限就填 0。`
  }
  return ''
}

const unlimitedRisk = '不设固定上限可能显著增加模型调用次数和 Token 消耗，也可能延长任务时间；关闭本地显存自动控制时还可能导致显存不足。确认吗？'
// 「哪些字段被确认过设为不限」记在集合里，blur 级不弹框：默认值现在全是 0，
// 六个 @change 各弹一次原生 confirm + 保存再弹一次 = 一轮七次模态，
// 而 pywebview 内嵌窗口里原生模态会阻塞 JS 线程。合并成保存时一次问清。
const unlimitedConfirmed = ref<Set<CapacityField>>(new Set())
const unlimitedPending = () => CAPACITY_KEYS.filter(f => capNum(f) === 0 && !unlimitedConfirmed.value.has(f))

async function loadAgentCapacity() {
  capacityError.value = ''
  try {
    const info = await agentCapacityApi.get()
    capacityInfo.value = info
    for (const field of CAPACITY_KEYS) capacityValues[field] = String(info.settings[CAPACITY_FIELDS[field]])
    localAutoLimit.value = info.settings.local_auto_limit
  } catch (e) {
    capacityError.value = (e as { message?: string }).message || '读取 Agent 容量设置失败'
  }
}

async function saveAgentCapacity() {
  const bad = validateCapacity()
  if (bad) { capacityError.value = bad; return }
  const pending = unlimitedPending()
  if (pending.length) {
    if (!window.confirm(`${pending.map(f => CAPACITY_LABEL[f]).join('、')} 将设为「不限」。\n${unlimitedRisk}`)) return
    unlimitedConfirmed.value = new Set([...unlimitedConfirmed.value, ...pending])
  }
  capacitySaving.value = true
  capacityError.value = ''
  capacitySaved.value = ''
  try {
    const result = await agentCapacityApi.save({
      max_agents: Number(capacityValues.maxAgents),
      main_max_steps: Number(capacityValues.mainMaxSteps),
      child_max_steps: Number(capacityValues.childMaxSteps),
      max_nudges: Number(capacityValues.maxNudges),
      max_final_continuations: Number(capacityValues.maxFinalContinuations),
      task_token_budget: Number(capacityValues.taskTokenBudget),
      local_auto_limit: localAutoLimit.value,
    })
    if (!result.ok) {
      capacityError.value = result.error || '保存失败'
      return
    }
    capacitySaved.value = 'Agent 容量设置已保存'
    unlimitedConfirmed.value = new Set()
    await loadAgentCapacity()
  } catch (e) {
    capacityError.value = (e as { message?: string }).message || '保存失败'
  } finally {
    capacitySaving.value = false
  }
}

const wsNeedsKey = computed(() => WEB_SEARCH_PROVIDERS.find(p => p.value === wsProvider.value)?.needs_key)
const wsNeedsUrl = computed(() => WEB_SEARCH_PROVIDERS.find(p => p.value === wsProvider.value)?.needs_url)
const wfNeedsKey = computed(() => WEB_FETCH_PROVIDERS.find(p => p.value === wfProvider.value)?.needs_key)
const wfNeedsUrl = computed(() => WEB_FETCH_PROVIDERS.find(p => p.value === wfProvider.value)?.needs_url)

// ---------------- MCP ----------------
const mcpServers = ref<McpServer[]>([])
const mcpProposals = ref<McpPendingProposal[]>([])
const mcpLoading = ref(false)
const mcpError = ref('')
// 凭证落盘成功后的短暂提示（MCP 面板内，几秒后自动消失）
const mcpNotice = ref('')
let mcpNoticeTimer: number | null = null
function notifyMcp(msg: string) {
  mcpNotice.value = msg
  if (mcpNoticeTimer !== null) window.clearTimeout(mcpNoticeTimer)
  mcpNoticeTimer = window.setTimeout(() => { mcpNotice.value = ''; mcpNoticeTimer = null }, 6000)
}
/**
 * 凭证落盘成功提示。stdio 与 http 都会在发起请求时解析 `@secret:` 引用
 * （http 侧经 `http_headers_for` + `_http_post(headers=)`，且仅 https+受信域才转发），
 * 故两种情况统一提示「可继续确认落盘」，不再区分。
 */
function notifyCredentialsSaved() {
  notifyMcp('凭证已写入本机钥匙串（secrets_store），可继续「确认添加并启用」。')
}
const mcpForm = ref({ key: '', label: '', transport: 'stdio' as 'stdio' | 'http', command: '', args: '', url: '' })
const mcpAdding = ref(false)
const mcpCapabilities = ref<{ active: Record<string, McpCapabilityCandidate>; pending: Record<string, McpCapabilityCandidate> }>({ active: {}, pending: {} })
const mcpDiscovering = ref('')
const mcpWebLearn = ref(true)
const mcpSearch = ref('')
const mcpSearching = ref(false)
const mcpDirectoryResults = ref<McpDirectoryResult[]>([])

// ---------------- P1：MCP 自动连接向导状态机 ----------------
type WizardStep = 1 | 2 | 3 | 4 | 5
type FillState = 'idle' | 'loading' | 'populated' | 'empty' | 'error' | 'edge' | 'registering'
interface DiagNode { step: number; label: string; state: 'done' | 'active' | 'fail' | 'warn' | 'idle'; sub?: string }
interface AcSource { domain: string; trust: 'official' | 'community' | 'unknown'; url: string }

const wizardStep = ref<WizardStep>(1)
const fillState = ref<FillState>('idle')
const acCandidates = ref<McpAutoConnectCandidate[]>([])
const acSelected = ref<McpAutoConnectCandidate | null>(null)
const acProbing = ref(false)
const acProbe = ref<McpProbeRes | null>(null)
const acConfirmOpen = ref(false)
const acConfirmBusy = ref(false)
const acConfirmError = ref('')
const acShowRaw = ref(false)

// 注册代管（C4）：L0 自动填充进度 / L1 停-继续 / L2 人工回填（默认与异常兜底路径）
const regOpen = ref(false)
const regTier = ref<McpRegisterTier>('L2')
const regTaskId = ref('')
const regUrl = ref('')
// L2 手动回填：一个候选可能引用多个 provider（如 @secret:slack + @secret:slack_team），
// 逐项填写、逐项非空校验，commit 时一次全量提交（漏一个 spawn 就解析失败）。
const regFields = ref<{ provider: string; value: string; required: boolean }[]>([])
/** 候选是否「本就带 @secret 引用」：带引用时按 provider 逐项填；否则回退单字段（旧行为）。 */
const regHasRefs = ref(false)
const regSecretKey = ref('')
const regError = ref('')
const regCommitting = ref(false)
// L0/L1 会话态：由 registerStatus 轮询回填
const regStatus = ref<McpRegisterState>('waiting_user')
const regUserPrompt = ref('')
const regStep = ref('')
// register/start 返回的降级原因/指引（L1/L2 展示，如「未收录该 provider…已降级 L2」）
const regNote = ref('')
const regResuming = ref(false)
let regPollTimer: number | null = null
let regPollTries = 0

// ---------------- 智能体（本地预设） ----------------
interface AgentPreset { id: string; name: string; model: string; system: string }
const agents = ref<AgentPreset[]>([])
const agentForm = ref({ name: '', model: '', system: '' })
const AGENT_KEY = 'docmind_agent_presets'

function loadAgents() {
  try {
    const raw = localStorage.getItem(AGENT_KEY)
    agents.value = raw ? (JSON.parse(raw) as AgentPreset[]) : []
  } catch { agents.value = [] }
}
function saveAgents() {
  try { localStorage.setItem(AGENT_KEY, JSON.stringify(agents.value)) } catch { /* ignore */ }
}
function addAgent() {
  const name = agentForm.value.name.trim()
  if (!name) return
  agents.value.push({
    id: `agent-${Date.now().toString(16)}`,
    name,
    model: agentForm.value.model.trim(),
    system: agentForm.value.system.trim(),
  })
  saveAgents()
  agentForm.value = { name: '', model: '', system: '' }
}
function removeAgent(id: string) {
  agents.value = agents.value.filter(a => a.id !== id)
  saveAgents()
}

async function loadConfig() {
  if (!props.visible) return
  loading.value = true
  errorMsg.value = ''
  try {
    const c = await settingsApi.get()
    cfg.value = c
    await loadAgentCapacity()
    wsProvider.value = c.web_search_provider
    wsApiKey.value = ''
    wsApiUrl.value = c.web_search_api_url || ''
    wfProvider.value = c.web_fetch_provider
    wfApiKey.value = ''
    wfApiUrl.value = c.web_fetch_api_url || ''
    memoryOptimized.value = !!c.memory_optimized
    memoryLearn.value = !!c.memory_learn
  } catch (e) {
    errorMsg.value = (e as { message?: string }).message || '读取配置失败'
  } finally {
    loading.value = false
  }
}

// 切换「长期记忆优化」总开关：翻当前值 → POST 专用端点 → 以服务端回读值为准。
async function toggleMemory() {
  if (memorySaving.value) return
  const want = !memoryOptimized.value
  memorySaving.value = true
  memorySaved.value = ''
  errorMsg.value = ''
  try {
    const res = await memorySettingsApi.save(want)
    if (res.ok === false) {
      errorMsg.value = res.error || '切换失败'
      return
    }
    // 以服务端为准回写，避免 UI 与实际 runtime 漂移
    memoryOptimized.value = !!res.memory_optimized
    memorySaved.value = memoryOptimized.value ? '已开启：每轮记忆将后台蒸馏精简、低价值轮次自动作废' : '已关闭：恢复逐字记忆（与改动前一致）'
  } catch (e) {
    errorMsg.value = (e as { message?: string }).message || '切换失败'
  } finally {
    memorySaving.value = false
  }
}

// 切换「学习习惯」独立开关：翻当前值 → POST 专用端点(只带 memory_learn) → 以服务端回读值为准。
async function toggleLearn() {
  if (learnSaving.value) return
  const want = !memoryLearn.value
  learnSaving.value = true
  learnSaved.value = ''
  errorMsg.value = ''
  try {
    const res = await memorySettingsApi.saveLearn(want)
    if (res.ok === false) {
      errorMsg.value = res.error || '切换失败'
      return
    }
    memoryLearn.value = !!res.memory_learn
    learnSaved.value = memoryLearn.value
      ? '已开启：只统计你的显式要求与实际改动，形成可删可清的建议级习惯，绝不推断个人信息'
      : '已关闭：不再统计，也不再向模型注入学到的习惯（与改动前一致）'
  } catch (e) {
    errorMsg.value = (e as { message?: string }).message || '切换失败'
  } finally {
    learnSaving.value = false
  }
}

async function saveSearch() {
  searchSaving.value = true
  errorMsg.value = ''
  savedMsg.value = ''
  try {
    const res = await settingsApi.save({
      web_search_provider: wsProvider.value as SettingsConfigInfo['web_search_provider'],
      web_search_api_key: wsApiKey.value.trim(),
      web_search_api_url: wsApiUrl.value.trim(),
      web_fetch_provider: wfProvider.value as SettingsConfigInfo['web_fetch_provider'],
      web_fetch_api_key: wfApiKey.value.trim(),
      web_fetch_api_url: wfApiUrl.value.trim(),
    })
    if (res.ok === false) {
      errorMsg.value = res.error || '保存失败'
      return
    }
    savedMsg.value = '已保存'
    if (res.warnings?.length) errorMsg.value = res.warnings.join('；')
  } catch (e) {
    errorMsg.value = (e as { message?: string }).message || '保存失败'
  } finally {
    searchSaving.value = false
  }
}

async function loadMcp() {
  mcpLoading.value = true
  mcpError.value = ''
  try {
    const [r, caps, props] = await Promise.all([mcpApi.servers(), mcpApi.capabilities(), mcpApi.pendingProposals()])
    mcpServers.value = r.servers || []
    mcpCapabilities.value = { active: caps.active || {}, pending: caps.pending || {} }
    mcpProposals.value = props.items || []
  } catch (e) {
    mcpError.value = (e as { message?: string }).message || '读取 MCP 失败'
  } finally {
    mcpLoading.value = false
  }
}

async function discoverMcp(key: string) {
  mcpDiscovering.value = key
  mcpError.value = ''
  try {
    const r = await mcpApi.discover(key, mcpWebLearn.value)
    if (!r.ok) { mcpError.value = r.error || '能力发现失败'; return }
    await loadMcp()
  } catch (e) { mcpError.value = (e as { message?: string }).message || '能力发现失败' }
  finally { mcpDiscovering.value = '' }
}

// 诊断节点：搜索→抓取→抽取→校验→试连→确认（Phase 2 六节点）
const AC_STEPS = ['搜索', '抓取', '抽取', '校验', '试连', '确认'] as const
function buildDiag(over: Partial<Record<number, DiagNode['state']>>, subs: Partial<Record<number, string>> = {}): DiagNode[] {
  return AC_STEPS.map((label, i) => ({ step: i + 1, label, state: over[i + 1] || 'idle', sub: subs[i + 1] }))
}
const acDiag = computed<DiagNode[]>(() => {
  if (fillState.value === 'loading') return buildDiag({ 1: 'active' })
  if (fillState.value === 'error') return buildDiag({ 1: 'done', 2: 'done', 3: 'fail' }, { 3: '命令不安全，已转手动填写' })
  if (fillState.value === 'empty') return buildDiag({ 1: 'done', 2: 'done', 3: 'done', 4: 'warn' }, { 4: '未找到可直接连的能力' })
  if (acCandidates.value.length === 0) return buildDiag({})
  const sel = acSelected.value
  const base: Partial<Record<number, DiagNode['state']>> = { 1: 'done', 2: 'done', 3: 'done' }
  if (sel) base[4] = sel.trust === 'source_untrusted' ? 'warn' : 'done'
  if (acProbing.value) base[5] = 'active'
  else if (acProbe.value) {
    base[5] = acProbe.value.probe_ok ? 'done' : 'fail'
    base[6] = acProbe.value.probe_ok ? 'active' : 'idle'
  }
  const subs: Partial<Record<number, string>> = {}
  if (sel && sel.trust === 'source_untrusted') subs[4] = '来源不可信，请在确认前人工核实'
  if (acProbe.value && !acProbe.value.probe_ok) subs[5] = acProbe.value.error || '连接失败'
  return buildDiag(base, subs)
})

function acStepState(n: WizardStep): 'done' | 'active' | 'idle' | 'fail' {
  if (n === 2 && fillState.value === 'error') return 'fail'
  if (wizardStep.value > n) return 'done'
  if (wizardStep.value === n) return 'active'
  return 'idle'
}

const acTrustTag = (c: McpAutoConnectCandidate | null) => {
  if (!c) return '未知来源'
  if (c.trust === 'source_untrusted') return '未知来源'
  if (c.trust_tier === 'community') return '社区 / 可信'
  return '官方'
}
/** 卡片标题优先用 MCP 服务名（registry 的 server_name / 精选索引 name），回退来源仓库域。 */
const acCardTitle = (c: McpAutoConnectCandidate | null) =>
  c ? (c.config.provenance?.server_name || c.config.provenance?.domain || 'server') : 'server'
const acSources = (c: McpAutoConnectCandidate | null): AcSource[] => {
  if (!c) return []
  const url = c.config.provenance?.url || ''
  const domain = c.config.provenance?.domain || ''
  if (!domain) return []
  const tier = c.trust_tier || (c.trust === 'source_untrusted' ? 'unknown' : 'official')
  return [{ domain, trust: tier, url }]
}
const acResolvedCmd = (c: McpAutoConnectCandidate | null) => c ? [c.config.command, ...(c.config.args || [])].filter(Boolean).join(' ') : ''
/**
 * 脱敏：`maskAll`（env：一律脱敏，沿用既有口径）或值**含** `@secret:` 引用。
 * 后者兼容**内嵌形态**（如 header `Bearer @secret:smithery_api_key`）——只要含引用就整体脱敏，
 * **绝不**把模板原文（连同 provider 名）显示出来。
 */
const acMaskEntries = (entries: Record<string, string> | undefined, maskAll = false): string => {
  const keys = Object.keys(entries || {})
  if (keys.length === 0) return '无'
  return keys.map(k => {
    const v = (entries || {})[k] ?? ''
    return (maskAll || v.includes('@secret:')) ? `${k}=已填写（脱敏）` : `${k}=${v}`
  }).join('  ')
}
const acEnvMasked = (c: McpAutoConnectCandidate | null) => (c ? acMaskEntries(c.config.env, true) : '无')
const acHeadersMasked = (c: McpAutoConnectCandidate | null) => (c ? acMaskEntries(c.config.headers, false) : '无')
/** http = 远程 MCP：连接信息在 url/headers，而非 stdio 的 command/args。 */
const isHttpTransport = (c: McpAutoConnectCandidate | null) => !!c && c.config.transport === 'http'
/**
 * 收集候选 env/headers 里**全部 distinct** `@secret:<provider>`（保序去重）。
 * 值可为**内嵌形态**（`Bearer @secret:smithery_api_key`），故用**非锚定**全局匹配：
 * `@secret:` 须位于串首或非标识符字符之后（避免 `foo@secret:` 这类标识符内部误匹配），
 * provider 名限定 `[A-Za-z0-9_.-]+`（与后端 provider 命名一致）。
 * 一个候选可引用多个 provider（如 slack → @secret:slack + @secret:slack_team）；
 * commit 必须一次全量覆盖，漏一个 spawn 时 resolve_secret_refs 就会抛错。
 * 这是「凭证最终写进哪个 secrets_store 键」的权威来源（优先于 provenance.domain）。
 */
const acSecretSpecs = (c: McpAutoConnectCandidate | null): { name: string; required: boolean }[] => {
  if (!c) return []
  if (c.secrets?.length) {
    return c.secrets.filter(s => s && s.name).map(s => ({ name: s.name, required: !!s.required }))
  }
  const re = /(?:^|[^A-Za-z0-9_.-])@secret:([A-Za-z0-9_.-]+)/g
  const out: { name: string; required: boolean }[] = []
  for (const bag of [c.config.env, c.config.headers]) {
    for (const v of Object.values(bag || {})) {
      for (const m of (v || '').matchAll(re)) {
        const p = m[1]
        if (p && !out.some(s => s.name === p)) out.push({ name: p, required: true })
      }
    }
  }
  return out
}
/** 必填 provider：可选密钥只作为候选元数据，不阻塞注册或连接。 */
const acSecretProviders = (c: McpAutoConnectCandidate | null): string[] =>
  acSecretSpecs(c).filter(s => s.required).map(s => s.name)
/** 主 provider：用于 `register/start` 的 adapter 选择，以及无引用回退时的 env 键名。 */
const acSecretProvider = (c: McpAutoConnectCandidate | null): string => acSecretProviders(c)[0] || ''
/** 候选是否「需要凭证」：env 或 headers 任一值**含** `@secret:`（锚定或内嵌皆可）。 */
const acNeedsSecret = (c: McpAutoConnectCandidate | null): boolean => acSecretProviders(c).length > 0
const acRawJson = (c: McpAutoConnectCandidate | null) =>
  c ? JSON.stringify({ key: candidateKey(c), ...c.config }, null, 2) : ''
const directorySourceLabel = (url: string): string => {
  try { return new URL(url).hostname.replace(/^www\./, '') || '官方来源' } catch { return '官方来源' }
}
const candidateKey = (c: McpAutoConnectCandidate | null): string => {
  if (!c) return 'server'
  const p = c.config.provenance || {}
  // 可读标识优先取包名/URL 尾段（server-github / mcp-server-git / docker），回退 command
  const args = (c.config.args || []).filter(a => a && !a.startsWith('-'))
  const pkg = args.find(a => a.includes('/') || a.includes('@')) || args[args.length - 1] || ''
  const tail = (pkg.split('/').pop() || '').replace(/^@/, '') || c.config.command || c.config.url || 'x'
  // 后端 save_server 的 key 白名单：仅字母数字 / 下划线 / 连字符，且 ≤40。
  // domain 含点（github.com）等非法字符，这里统一规整，避免「确认落盘」被拒。
  const slug = `${p.domain || 'server'}-${tail}`
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^[-_]+|[-_]+$/g, '')
    .slice(0, 40)
    .replace(/[-_]+$/g, '')
  return slug || 'server'
}

async function autoConnectSearch() {
  const query = mcpSearch.value.trim()
  if (query.length < 2) { mcpError.value = '请输入至少 2 个字符，例如 Godot、Unity、数据库。'; return }
  mcpSearching.value = true
  fillState.value = 'loading'
  wizardStep.value = 2
  mcpError.value = ''
  acCandidates.value = []
  mcpDirectoryResults.value = []
  acSelected.value = null
  acProbe.value = null
  try {
    const r = await mcpApi.autoConnectSearch(query, mcpWebLearn.value)
    acCandidates.value = r.candidates || []
    // 「没找到候选」是正常业务结果（ok:false + search_error 说明原因），不是传输失败。
    // 只有「确实没有候选」走 empty（友好引导手动）；有候选才进预览。
    const noCandidate = acCandidates.value.length === 0
    if (noCandidate) {
      fillState.value = 'empty'
      wizardStep.value = 2
      // search_error 是后端给出的可展示原因（未联网/无链接/读取失败/无命令）
      mcpError.value = r.search_error || r.error || ''
      try {
        const catalog = await mcpApi.searchCatalog(query, mcpWebLearn.value)
        mcpDirectoryResults.value = catalog.results || []
        mcpError.value = catalog.results?.length ? (catalog.search_error || '') : (mcpError.value || catalog.search_error || '')
      } catch { /* 保留自动连接原始诊断 */ }
    } else {
      fillState.value = 'populated'
      wizardStep.value = 3
      if (r.search_error) mcpError.value = r.search_error
    }
  } catch (e) {
    mcpError.value = (e as { message?: string }).message || '搜索失败'
    fillState.value = 'error'
  } finally { mcpSearching.value = false }
}

function useDirectoryOption(item: McpDirectoryResult, option: McpDirectoryResult['connection_options'][number]) {
  const config = option.config
  if (config?.transport && config.command !== undefined && config.url !== undefined) {
    const rawSpecs = option.secrets || (config.provenance as (McpAutoConnectConfig['provenance'] & {
      secret_specs?: { name: string; required: boolean }[]
    }) | undefined)?.secret_specs || []
    const candidate: McpAutoConnectCandidate = {
      config: {
        transport: config.transport,
        command: String(config.command || ''),
        args: Array.isArray(config.args) ? config.args.map(String) : [],
        url: String(config.url || ''),
        env: { ...(config.env || {}) } as Record<string, string>,
        headers: { ...(config.headers || {}) } as Record<string, string>,
        provenance: config.provenance || { url: '', domain: '', server_name: option.label || item.label },
        command_unresolved: Boolean(config.command_unresolved),
      },
      trust: 'trusted',
      trust_tier: option.trust_tier || 'community',
      secrets: rawSpecs.map(s => ({ name: s.name, required: !!s.required })),
      validation_errors: [],
    }
    acCandidates.value = [candidate]
    acSelected.value = candidate
    fillState.value = 'populated'
    wizardStep.value = 3
    mcpDirectoryResults.value = []
    mcpError.value = ''
    if (candidate.secrets?.some(s => s.required)) void openRegister(candidate)
    return
  }
  mcpForm.value = {
    key: item.template.key || item.id,
    label: item.label,
    transport: option.transport,
    command: item.template.command || '',
    args: item.template.args.join(' '),
    url: item.template.url || '',
  }
  mcpDirectoryResults.value = []
  mcpError.value = `${item.label}：请补充官方连接信息后测试连接。`
  document.querySelector<HTMLInputElement>('[data-mcp-manual-key]')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

async function probeCandidate(c: McpAutoConnectCandidate) {
  acSelected.value = c
  acProbing.value = true
  acProbe.value = null
  wizardStep.value = 4
  mcpError.value = ''
  try {
    const r = await mcpApi.probeCandidate(c.config)
    acProbe.value = r
    if (r.probe_ok) wizardStep.value = 5
  } catch (e) {
    acProbe.value = { ok: false, probe_ok: false, tools: [], error: (e as { message?: string }).message || '连接失败' }
  } finally { acProbing.value = false }
}

function openConfirm() { acConfirmOpen.value = true; acShowRaw.value = false; acConfirmError.value = '' }
function closeConfirm() { acConfirmOpen.value = false }

async function confirmConnect() {
  const c = acSelected.value
  if (!c) return
  acConfirmBusy.value = true
  acConfirmError.value = ''
  try {
    const r = await mcpApi.confirmConnect(candidateKey(c), c.config)
    if (!r.ok) { acConfirmError.value = r.error || '添加失败'; return }
    acConfirmOpen.value = false
    fillState.value = 'idle'
    acCandidates.value = []
    acSelected.value = null
    acProbe.value = null
    wizardStep.value = 1
    await loadMcp()
  } catch (e) {
    acConfirmError.value = (e as { message?: string }).message || '添加失败'
  } finally { acConfirmBusy.value = false }
}

// 注册代管（C4）：先问后端定档（L0/L1/L2）。L0=自动填充进度；L1=停-继续；L2=人工回填兜底。
const REG_POLL_MS = 1500
const REG_POLL_MAX = 160 // ≈4 分钟上限；超时兜底转 L2，绝不死等
const REG_L0_STEPS = ['opening', 'filling', 'submitting', 'capturing'] as const

const regStepText = computed(() => {
  const map: Record<string, string> = {
    // L0 自动步
    opening: '打开官方注册页',
    filling: '填写表单字段',
    submitting: '提交表单',
    capturing: '读取并保存凭证',
    // L1 停点步（后端 step 枚举）
    fill: '填写注册表单',
    challenge: '等待你完成人机验证',
    submit: '等待你点击提交/生成',
    capture: '读取并保存凭证',
    untrusted: '来源未受信，转为手动',
  }
  return map[regStep.value] || regStep.value
})
// 后端只回「步名」不给百分比：按序号推进；未知步名返回 0 → 走不确定进度条
const regProgress = computed(() => {
  const i = (REG_L0_STEPS as readonly string[]).indexOf(regStep.value)
  return i < 0 ? 0 : Math.round(((i + 1) / (REG_L0_STEPS.length + 1)) * 100)
})
type RegView = 'manual' | 'running' | 'waiting'
/** 弹窗视图：L2=人工回填；非 L2 且 running=自动进行中；其余（waiting_user）=停-继续。 */
const regView = computed<RegView>(() =>
  regTier.value === 'L2' ? 'manual' : regStatus.value === 'running' ? 'running' : 'waiting')

const REG_L1_FALLBACK = '页面需要你手动完成一步（如验证码 / 邮箱验证），完成后点下面的按钮，我会接着跑。'
/**
 * L1 主提示：**优先后端 `note`**（step 专属，如「已自动填表到提交前，请在浏览器中点『创建/生成』后回来点继续」），
 * 再退 `user_prompt`（adapter 通用语），最后兜底。后端 note 只随 start 回传，故整段会话保留。
 */
const regL1Prompt = computed(() => regNote.value || regUserPrompt.value || REG_L1_FALLBACK)
/** 次级提示：note 与 user_prompt 都有且不同（如续跑时后端给了新的 challenge 提示）→ 补一行，避免丢信息。 */
const regL1Extra = computed(() =>
  regNote.value && regUserPrompt.value && regUserPrompt.value !== regNote.value ? regUserPrompt.value : '')

/** L2 可提交：至少一项且**每一项都非空**（后端逐项写入，漏一个 spawn 会解析失败）。 */
const regCanCommit = computed(() =>
  regFields.value.length > 0 && regFields.value.every(f => !f.required || !!f.value.trim()))

function stopRegPoll() {
  if (regPollTimer !== null) { window.clearInterval(regPollTimer); regPollTimer = null }
}

function startRegPoll() {
  stopRegPoll()
  regPollTries = 0
  regPollTimer = window.setInterval(() => { void pollRegStatus() }, REG_POLL_MS)
}

function closeRegister() {
  stopRegPoll()
  regOpen.value = false
}

/** 转人工回填（L2）：自动态失败/超时/用户主动时的安全兜底，绝不把用户卡在死路。 */
function switchToManual() {
  stopRegPoll()
  regTier.value = 'L2'
  regStatus.value = 'waiting_user'
  regStep.value = ''
  regError.value = ''
}

/**
 * 把已存入钥匙串的凭证回写成连接器的 @secret 引用（L0/L1 自动、L2 手动共用）。
 * 若候选**本来就带** `@secret:<provider>` 引用（env/headers），引用已就绪 → 不再新增键，避免污染配置。
 */
function applySecretToCandidate() {
  const c = acSelected.value
  if (!c) return
  if (acSecretProvider(c)) return
  acSelected.value = {
    ...c,
    config: {
      ...c.config,
      env: { ...(c.config.env || {}), [regSecretKey.value.toUpperCase()]: `@secret:${regSecretKey.value}` },
    },
  }
}

/**
 * L0/L1 成功收尾：凭证由后端在捕获时写入 secrets_store，这里复用既有 commit 通道做**落盘确认**。
 * 后端契约：空入参 + 已捕获凭证 → {ok:true, stored:[provider]}；无捕获 → {ok:false}+error。
 * 故 ok:false / 抛错都**不得当作成功**——留在弹窗展示原因并转人工兜底（绝不吞错成成功）。
 */
async function finalizeAutoRegister() {
  stopRegPoll()
  let ok = false
  let errMsg = ''
  try {
    // 明文绝不回传前端：不带 credentials，仅触发后端落盘确认。
    const r = await mcpApi.registerCommit(regTaskId.value, {})
    ok = r.ok === true
    if (!ok) errMsg = r.error || '凭证落盘未确认'
  } catch (e) {
    errMsg = (e as { message?: string }).message || '凭证落盘确认失败'
  }
  if (!ok) {
    // 诚实兜底：不显示成功、不关弹窗；转人工回填并展示原因（保留 regError，不走会清错的 switchToManual）
    regTier.value = 'L2'
    regStatus.value = 'waiting_user'
    regStep.value = ''
    regError.value = `${errMsg}。请手动粘贴凭证后完成。`
    return
  }
  applySecretToCandidate()
  notifyCredentialsSaved()
  regOpen.value = false
}

function applyRegStatus(r: McpRegisterStatusRes) {
  if (r.tier) regTier.value = r.tier
  if (r.status) regStatus.value = r.status
  if (r.user_prompt) regUserPrompt.value = r.user_prompt
  regStep.value = r.step || ''
  if (r.url) regUrl.value = r.url
}

/** 轮询会话态：只在非 L2 且未到终态时进行；达上限或连续传输失败则兜底转 L2。 */
async function pollRegStatus() {
  if (!regOpen.value || regTier.value === 'L2') { stopRegPoll(); return }
  const current = regStatus.value
  if (current === 'done' || current === 'failed') { stopRegPoll(); return }
  if (regPollTries >= REG_POLL_MAX) {
    regError.value = '等待自动注册超时，已转为手动填写。'
    switchToManual()
    return
  }
  regPollTries++
  try {
    applyRegStatus(await mcpApi.registerStatus(regTaskId.value))
  } catch (e) {
    // 传输失败：先多试几次；达上限再兜底，避免一次网络抖动就打回手动
    if (regPollTries >= REG_POLL_MAX) {
      stopRegPoll()
      regError.value = (e as { message?: string }).message || '读取注册状态失败'
      switchToManual()
    }
    return
  }
  if (regStatus.value === 'done') { await finalizeAutoRegister(); return }
  if (regStatus.value === 'failed') {
    regError.value = '自动注册未成功，已转为手动填写。'
    switchToManual()
    return
  }
  // running 继续轮；waiting_user（L1 撞到验证码/2FA）停下，等用户点「继续」
  if (regStatus.value === 'waiting_user') stopRegPoll()
}

async function openRegister(c: McpAutoConnectCandidate) {
  stopRegPoll()
  acSelected.value = c
  // provider 名以候选已有的 `@secret:<provider>` 引用为准（写回/写钥匙串都用它）；
  // 无引用（命令未解析等着新建凭证）时才回退 provenance.domain —— 兼容既有行为。
  const specs = acSecretSpecs(c)
  regHasRefs.value = specs.length > 0
  regFields.value = (specs.length
    ? specs
    : [{ name: acSecretProvider(c) || c.config.provenance?.domain || 'provider', required: true }])
    .map(s => ({ provider: s.name, value: '', required: s.required }))
  regSecretKey.value = regFields.value[0]?.provider || 'provider'
  regError.value = ''
  regUserPrompt.value = ''
  regStep.value = ''
  regNote.value = ''
  regOpen.value = true
  regTier.value = 'L2'
  regStatus.value = 'waiting_user'
  regUrl.value = '' // 后端未记录该 provider 的出证链接时返回空串；不再回退 provenance.url（那是源码仓库，会跳错站）
  regTaskId.value = `ac-${Date.now().toString(16)}` // 后端不可达时的本地兜底 id
  // Registry 标注为可选的密钥不应触发自动注册浏览器流程；直接展示可选回填表单。
  if (specs.length && !specs.some(s => s.required)) return
  try {
    const r = await mcpApi.registerStart(candidateKey(c), c.config, regSecretKey.value)
    if (!r.ok) { regTier.value = 'L2'; return }
    regTier.value = r.tier
    regUrl.value = r.url || regUrl.value
    regNote.value = r.note || ''
    regTaskId.value = r.task_id || regTaskId.value
    if (r.tier === 'L0') { regStatus.value = 'running'; startRegPoll(); void pollRegStatus() }
    else if (r.tier === 'L1') { regStatus.value = 'waiting_user'; startRegPoll(); void pollRegStatus() }
    // L2：保留纯手动回填 UI，不轮询
  } catch {
    regTier.value = 'L2' // 后端不可达：诚实降级为 L2（人工回填兜底）
  }
}

/** L1「我已完成，继续」：续跑同一持久会话；撞到下一个挑战会再次 waiting_user。 */
async function resumeRegister() {
  if (regTier.value === 'L2') return
  regResuming.value = true
  regError.value = ''
  try {
    const r = await mcpApi.registerResume(regTaskId.value)
    if (r.tier) regTier.value = r.tier
    if (r.status) regStatus.value = r.status
    if (r.user_prompt) regUserPrompt.value = r.user_prompt
    if (r.step) regStep.value = r.step
    if (r.url) regUrl.value = r.url
    if (r.ok === false) {
      // 业务态失败（如无 live 会话）：保留提示，不当作传输错误
      regError.value = r.error || '续跑未成功，请重试或改用手动填写。'
      if (regStatus.value !== 'running') return
    }
    if (regStatus.value === 'done') { await finalizeAutoRegister(); return }
    if (regStatus.value === 'failed') {
      regError.value = r.error || '自动注册未成功，已转为手动填写。'
      switchToManual()
      return
    }
    if (regStatus.value === 'running') { startRegPoll(); void pollRegStatus() }
  } catch (e) {
    regError.value = (e as { message?: string }).message || '续跑请求失败'
  } finally {
    regResuming.value = false
  }
}

/**
 * L2：人工回填凭证 → 写钥匙串 → 回写 @secret 引用。
 * 一个候选可能引用多个 provider，**逐项非空**后**一次全量提交**（后端空值会跳过 → 漏项会导致 spawn 解析失败）。
 */
async function commitRegister() {
  if (!regFields.value.length) { regError.value = '没有需要填写的凭证'; return }
  if (regFields.value.some(f => f.required && !f.value.trim())) { regError.value = '请填写全部必填凭证后再提交'; return }
  const credentials: Record<string, string> = {}
  for (const f of regFields.value) {
    if (f.value.trim()) credentials[f.provider] = f.value.trim()
  }
  if (!Object.keys(credentials).length && regFields.value.every(f => !f.required)) {
    closeRegister()
    return
  }
  regCommitting.value = true
  regError.value = ''
  try {
    const r = await mcpApi.registerCommit(regTaskId.value, credentials)
    if (!r.ok) { regError.value = r.error || '凭证保存失败'; return }
    applySecretToCandidate()
    notifyCredentialsSaved()
    closeRegister()
  } catch (e) {
    regError.value = (e as { message?: string }).message || '凭证保存失败'
  } finally { regCommitting.value = false }
}

async function decideMcpCapability(key: string, approved: boolean) {
  mcpError.value = ''
  try {
    const r = await mcpApi.decideCapability(key, approved)
    if (!r.ok) { mcpError.value = r.error || '保存能力失败'; return }
    await loadMcp()
  } catch (e) { mcpError.value = (e as { message?: string }).message || '保存能力失败' }
}

async function addMcp() {
  const { key, label, transport, command, args, url } = mcpForm.value
  if (!key.trim()) return
  mcpAdding.value = true
  mcpError.value = ''
  try {
    const config: Record<string, unknown> = {
      label: label.trim() || key.trim(),
      transport,
      enabled: true,
    }
    if (transport === 'stdio') {
      config.command = command.trim()
      config.args = args.split(/\s+/).filter(Boolean)
    } else {
      config.url = url.trim()
    }
    const r = await mcpApi.save(key.trim(), config)
    if (!r.ok) {
      mcpError.value = r.error || '添加失败'
      return
    }
    mcpForm.value = { key: '', label: '', transport: 'stdio', command: '', args: '', url: '' }
    await loadMcp()
  } catch (e) {
    mcpError.value = (e as { message?: string }).message || '添加失败'
  } finally {
    mcpAdding.value = false
  }
}

/** 把提案的连接参数拼成一行可读命令，供确认卡片展示「要批准什么」。 */
function proposalCmd(p: McpPendingProposal): string {
  const c = p.config as Record<string, unknown>
  if (c.transport === 'http') return String(c.url || '')
  const args = Array.isArray(c.args) ? (c.args as unknown[]).map(String) : []
  return [String(c.command || ''), ...args].filter(Boolean).join(' ')
}

/**
 * 一键确认 Agent 经 dev_mcp_add 申请、卡在审批门的连接器。
 *
 * 用提案里**逐字相同**的 config 调 mcpApi.save → 后端 POST /api/mcp/servers 会
 * 现算 hash 匹配的审批并清理提案；此后 agent 用相同参数重试 dev_mcp_add 即放行。
 * 这正是修掉的死循环：此前队列里的 mcp_server「批准」被后端硬拒、设置页又无从
 * 复现 command/args，用户怎么点都不放行。
 */
const mcpConfirming = ref('')
async function confirmPendingProposal(p: McpPendingProposal) {
  if (mcpConfirming.value) return
  mcpConfirming.value = p.target
  mcpError.value = ''
  try {
    const r = await mcpApi.save(p.key, p.config)
    if (!r.ok) {
      mcpError.value = r.error || '确认失败'
      return
    }
    mcpNotice.value = `已确认连接器「${p.key}」，可让助手重试装配`
    await loadMcp()
  } catch (e) {
    mcpError.value = (e as { message?: string }).message || '确认失败'
  } finally {
    mcpConfirming.value = ''
  }
}

async function removeMcp(key: string) {
  mcpError.value = ''
  try {
    const r = await mcpApi.remove(key)
    if (!r.ok) { mcpError.value = r.error || '移除失败'; return }
    await loadMcp()
  } catch (e) {
    mcpError.value = (e as { message?: string }).message || '移除失败'
  }
}

// immediate：组件在首次打开设置时才由 App 异步挂载，挂载即 visible=true，需立即加载
watch(() => props.visible, async (v) => {
  if (!v) { stopRegPoll(); return } // 关掉设置即停轮询，绝不让定时器泄漏
  // 先应用入口标签，再加载远程配置；否则用户在加载期间点击其他标签，
  // 请求完成后的回调会把 tab 重置回 usage，表现为导航失效。
  tabTouched.value = false
  tab.value = props.initialTab || 'usage'
  savedMsg.value = ''
  usageSaved.value = ''
  await Promise.all([loadConfig(), loadMcp(), loadUsage(), loadMemoryProfile(), loadPower()])
  loadAgents()
}, { immediate: true })
watch(() => props.initialTab, (value) => {
  // 父组件只在从外部打开某个指定页面时更新 initialTab；用户已经在设置页
  // 内操作后，不能被一次无关的父级更新抢回入口页面。
  if (props.visible && value && !tabTouched.value) tab.value = value
})

// 注册弹窗关闭 → 立即停轮询
watch(regOpen, (open) => { if (!open) stopRegPoll() })

onUnmounted(() => {
  stopRegPoll()
  if (mcpNoticeTimer !== null) window.clearTimeout(mcpNoticeTimer)
})

function close() { emit('close') }
function openModelSettings() {
  // 先关闭统一设置壳，再打开对话台里的完整模型参数弹窗，避免两个遮罩叠加。
  emit('close')
  appEvents.emit('docmind:open-model-settings')
}

// ---------------- 本机显存与模型驻留 ----------------
// 2026-10-19 从问答页侧栏的「运行状态」卡片搬来。
// 原因：预加载/卸载显存模型是**运维级操作**，不是聊天主流程；问答页侧栏要留给
// 会话与项目。数据源与问答页原来那条完全一致（/api/model_status），不新增接口。
const power = ref<ModelStatus | null>(null)
const powerBusy = ref(false)
const powerMsg = ref('')

async function loadPower() {
  try {
    // 先 await 拿结果再赋值：immediate 监视器会在 setup 期间同步调用本函数，
    // 而 `power` 常量声明在监视器之后。若写成 `power.value = await ...`，JS 会
    // 在 await 之前就去解析左边的 `power` 引用，触发 TDZ「Cannot access before
    // initialization」。把赋值挪到 await 之后（此时 setup 已完成、常量已就绪）即可根治。
    const status = await modelResidencyApi.status()
    power.value = status
  } catch {
    power.value = null
  }
}

async function powerAction(action: 'on' | 'off') {
  powerBusy.value = true
  powerMsg.value = ''
  try {
    const r = await modelResidencyApi.power(action)
    powerMsg.value = r.ok
      ? (action === 'off'
          ? `已卸载 ${(r.unloaded || []).join('、') || '模型'}，下次对话自动重载`
          : `模型 ${(r.preloaded || []).join('、') || ''} 已预加载`.trim())
      : ('操作失败：' + (r.error || '未知错误'))
  } catch (e) {
    powerMsg.value = '请求失败：' + ((e as Error).message || '')
  } finally {
    powerBusy.value = false
    await loadPower()
  }
}
</script>

<template>
  <div v-if="visible" class="sv-mask wb-modal-backdrop" @mousedown.self="close">
    <div class="sv-box wb-modal-shell" role="dialog" aria-modal="true">
      <div class="sv-head wb-modal-head">
        <div class="sv-title-wrap">
          <h3 class="sv-title">设置</h3>
          <p class="sv-sub">工作区、模型与连接</p>
        </div>
        <button class="sv-x" @click="close" title="关闭">×</button>
      </div>
      <div class="sv-body">
        <!-- 左侧分组导航 -->
        <nav class="sv-nav">
          <button type="button" class="sv-nav-item" :class="{ on: tab === 'usage' }" :aria-current="tab === 'usage' ? 'page' : undefined" @click.stop="selectTab('usage')">用量与费用</button>
          <button type="button" class="sv-nav-item" :class="{ on: tab === 'search' }" :aria-current="tab === 'search' ? 'page' : undefined" @click.stop="selectTab('search')">网络搜索</button>
          <button type="button" class="sv-nav-item" :class="{ on: tab === 'mcp' }" :aria-current="tab === 'mcp' ? 'page' : undefined" @click.stop="selectTab('mcp')">MCP</button>
          <button type="button" class="sv-nav-item" :class="{ on: tab === 'agent' }" :aria-current="tab === 'agent' ? 'page' : undefined" @click.stop="selectTab('agent')">智能体</button>
          <button type="button" class="sv-nav-item" :class="{ on: tab === 'model' }" :aria-current="tab === 'model' ? 'page' : undefined" @click.stop="selectTab('model')">模型</button>
          <button type="button" class="sv-nav-item" :class="{ on: tab === 'runtime' }" :aria-current="tab === 'runtime' ? 'page' : undefined" @click.stop="selectTab('runtime')">运行与引擎</button>
          <button type="button" class="sv-nav-item" :class="{ on: tab === 'appearance' }" :aria-current="tab === 'appearance' ? 'page' : undefined" @click.stop="selectTab('appearance')">外观</button>
        </nav>

        <!-- 右侧内容 -->
        <div class="sv-content">
          <!-- Token 用量与费用 -->
          <section v-show="tab === 'usage'" class="sv-panel">
            <div class="sv-section-head">
              <div>
                <h4 class="sv-h4">Token 用量与费用</h4>
                <p class="sv-hint">统计来自逐轮运行账本，不保存对话正文。Token 表示上下文处理量，不等于费用；云端模型按配置单价估算，本地模型费用为 ¥0。</p>
              </div>
              <button class="sv-mini" :disabled="usageLoading" @click="loadUsage">刷新</button>
            </div>

            <div class="sv-meter-grid" aria-label="Token 用量概览">
              <div class="sv-meter">
                <span>输入 Token</span>
                <b>{{ tokens(traceSummary?.prompt_tokens) }}</b>
                <small v-if="cacheReadTokens" class="sv-meter-sub">含缓存命中 {{ tokens(cacheReadTokens) }}（{{ cacheRatio }}%）</small>
              </div>
              <div class="sv-meter">
                <span>输出 Token</span>
                <b>{{ tokens(traceSummary?.completion_tokens) }}</b>
              </div>
              <div class="sv-meter">
                <span>总 Token</span>
                <b>{{ tokens(traceSummary?.total_tokens) }}</b>
                <small v-if="cacheSavedNote" class="sv-meter-sub">{{ cacheSavedNote }}{{ cacheWriteNote }}</small>
              </div>
              <div class="sv-meter">
                <span>今日费用</span>
                <b>{{ money(budget?.day_spent) }}</b>
              </div>
              <div class="sv-meter">
                <span>累计费用</span>
                <b>{{ money(budget?.global_spent) }}</b>
              </div>
              <div class="sv-meter">
                <span>预算余额</span>
                <b>{{ budgetRemaining === null ? '不限' : money(budgetRemaining) }}</b>
              </div>
            </div>

            <div v-if="providerUsage.length" class="sv-provider-list">
              <div class="sv-provider-head"><span>模型供应商</span><span>类型</span><span>Token</span><span>缓存命中</span><span>费用</span></div>
              <div v-for="item in providerUsage" :key="item.provider" class="sv-provider-row" :class="{ simulated: item.simulated }">
                <b>{{ item.label }}</b>
                <span class="sv-kind">{{ item.kind }}</span>
                <span :title="`输入 ${tokens(item.prompt_tokens)} / 输出 ${tokens(item.completion_tokens)}`">{{ tokens(item.tokens) }}</span>
                <!-- 缓存命中**已包含在上面那列输入 Token 里**，所以这里不参与求和，
                     只标出其中有多少走了折扣价。 -->
                <span class="sv-cache" :class="{ on: item.cache_read_tokens > 0 }"
                      :title="item.cacheTitle">{{ item.cacheText }}</span>
                <span :title="item.pricing_state === 'unknown' ? '没有配置这个 provider 的单价，费用并未计入；用 dev_pricing(action: apply) 从官方目录检索写入' : ''">{{ item.cost }}</span>
              </div>
            </div>
            <p v-if="cacheSavedCny > 0" class="sv-note">
              缓存命中按折扣价（通常约为输入全价的 1/10）计费，累计<b>省下 ¥{{ cacheSavedCny.toFixed(4) }}</b>。
              没有这部分命中，同样的输入需要 ¥{{ (Number(traceSummary?.total_cost_cny || 0) + cacheSavedCny).toFixed(4) }}。
            </p>
            <p v-if="providerUsage.some(item => item.pricing_state === 'unknown' && item.tokens > 0)" class="sv-note">
              标为「未配置单价」的供应商费用<b>并未计入</b>——那个 ¥0 只代表「没算」，不代表免费。
              用 dev_pricing(action: apply) 从官方目录检索并写入单价。
            </p>
            <p v-if="providerUsage.some(item => item.simulated && item.tokens > 0)" class="sv-note">
              mock / fake 的 Token 是按文本估算的测试负载，用于验证上下文裁剪与性能，不代表 API 消耗，也不会计费。
            </p>
            <p v-else-if="!usageLoading" class="sv-hint">还没有模型调用记录。</p>

            <div class="sv-sep"></div>
            <h4 class="sv-h4">费用与调用上限</h4>
            <p class="sv-hint">达到累计预算后停止新的模型回合；每分钟限制用于抑制突发调用。所有输入填 0 表示不限。</p>
            <div class="sv-form-grid">
              <label class="sv-field">
                <span>累计预算上限（元）</span>
                <input v-model="budgetLimit" class="sv-input" type="number" min="0" step="0.01" inputmode="decimal" />
              </label>
              <label class="sv-field">
                <span>每分钟调用上限（次）</span>
                <input v-model="perMinuteCalls" class="sv-input" type="number" min="0" step="1" inputmode="numeric" />
              </label>
              <label class="sv-field">
                <span>每分钟费用上限（元）</span>
                <input v-model="perMinuteCost" class="sv-input" type="number" min="0" step="0.01" inputmode="decimal" />
              </label>
            </div>
            <p v-if="usageError" class="sv-err">{{ usageError }}</p>
            <p v-if="usageSaved" class="sv-ok">{{ usageSaved }}</p>
            <div class="sv-actions">
              <button class="sv-btn sv-primary" :disabled="usageSaving || usageLoading" @click="saveUsage">
                {{ usageSaving ? '保存中…' : '保存用量控制' }}
              </button>
              <button class="sv-btn" :disabled="usageSaving || usageLoading" @click="resetUsageSpent">计数清零</button>
            </div>
          </section>

          <!-- 网络搜索 -->
          <section v-show="tab === 'search'" class="sv-panel">
            <h4 class="sv-h4">网络搜索</h4>
            <p class="sv-hint">选择搜索服务商；内置（DDG / 百度 / Bing）无需 Key，API 类需填写 Key 或自建地址。</p>

            <label class="sv-label">搜索服务商</label>
            <select v-model="wsProvider" class="sv-input">
              <option v-for="p in WEB_SEARCH_PROVIDERS" :key="p.value" :value="p.value">{{ p.label }}</option>
            </select>
            <p v-if="wsNeedsKey || wsNeedsUrl" class="sv-hint">{{ WEB_SEARCH_PROVIDERS.find(p => p.value === wsProvider)?.desc }}</p>

            <template v-if="wsNeedsKey">
              <label class="sv-label">API Key
                <span class="sv-key-toggle" @click="showWsKey = !showWsKey">{{ showWsKey ? '隐藏' : '显示' }}</span>
              </label>
              <input v-model="wsApiKey" class="sv-input" :type="showWsKey ? 'text' : 'password'"
                     :placeholder="cfg?.web_search_has_key ? '已保存，留空表示不修改' : 'sk-...'" autocomplete="off" spellcheck="false" />
            </template>
            <template v-if="wsNeedsUrl">
              <label class="sv-label">API 地址（自建实例 / 网关）</label>
              <input v-model="wsApiUrl" class="sv-input" placeholder="https://your-instance" spellcheck="false" />
            </template>

            <div class="sv-sep"></div>
            <h4 class="sv-h4">URL 获取（网页正文）</h4>
            <label class="sv-label">URL 获取服务商</label>
            <select v-model="wfProvider" class="sv-input">
              <option v-for="p in WEB_FETCH_PROVIDERS" :key="p.value" :value="p.value">{{ p.label }}</option>
            </select>
            <template v-if="wfNeedsKey">
              <label class="sv-label">API Key
                <span class="sv-key-toggle" @click="showWfKey = !showWfKey">{{ showWfKey ? '隐藏' : '显示' }}</span>
              </label>
              <input v-model="wfApiKey" class="sv-input" :type="showWfKey ? 'text' : 'password'"
                     :placeholder="cfg?.web_fetch_has_key ? '已保存，留空表示不修改' : 'sk-...'" autocomplete="off" spellcheck="false" />
            </template>
            <template v-if="wfNeedsUrl">
              <label class="sv-label">API 地址</label>
              <input v-model="wfApiUrl" class="sv-input" placeholder="https://your-instance" spellcheck="false" />
            </template>

            <p v-if="errorMsg" class="sv-err">{{ errorMsg }}</p>
            <p v-if="savedMsg" class="sv-ok">{{ savedMsg }}</p>
            <div class="sv-actions">
              <button class="sv-btn sv-primary" :disabled="searchSaving || loading" @click="saveSearch">
                {{ searchSaving ? '保存中…' : '保存' }}
              </button>
            </div>
          </section>

          <!-- MCP -->
          <section v-show="tab === 'mcp'" class="sv-panel" data-wizard="mcp-autoconnect">
            <h4 class="sv-h4">MCP 自动连接</h4>
            <p class="sv-hint">描述你想要的能力，Agent 工作台 会读取官方文档、提取连接命令、试连并请你确认后写入本机。</p>
            <p v-if="mcpError" class="sv-err">{{ mcpError }}</p>
            <p v-if="mcpNotice" class="sv-ok" data-role="mcp-notice">{{ mcpNotice }}</p>

            <!-- C1：自动连接向导主流程（步骤条） -->
            <div class="sv-search-row">
              <input v-model="mcpSearch" class="sv-input" placeholder="搜索需要的能力，例如 Godot、Unity、数据库" @keyup.enter="autoConnectSearch" />
              <button class="sv-btn sv-primary" :disabled="mcpSearching" @click="autoConnectSearch">
                <Icon v-if="mcpSearching" name="loader" :size="16" class="dm-spin" />
                <template v-else><Icon name="zap" :size="16" /> 自动连接</template>
              </button>
            </div>
            <div class="sv-step-rail" data-role="wizard-rail">
              <button class="sv-step" :data-state="acStepState(1)" data-step="1"><Icon name="search" :size="16" />搜索</button>
              <button class="sv-step" :data-state="acStepState(2)" data-step="2"><Icon name="zap" :size="16" />自动填参</button>
              <button class="sv-step" :data-state="acStepState(3)" data-step="3"><Icon name="list" :size="16" />预览</button>
              <button class="sv-step" :data-state="acStepState(4)" data-step="4"><Icon name="activity" :size="16" />试连</button>
              <button class="sv-step" :data-state="acStepState(5)" data-step="5"><Icon name="shield-check" :size="16" />确认落盘</button>
            </div>

            <div class="sv-wizard-body" data-role="wizard-body">
              <!-- 加载中 -->
              <div v-if="fillState === 'loading'" class="sv-ac-state">
                <Icon name="loader" :size="20" class="dm-spin" />
                <span>正在读取官方文档并提取连接命令…</span>
              </div>
              <!-- 无候选 -->
              <div v-else-if="fillState === 'empty'" class="sv-ac-state">
                <Icon name="circle-alert" :size="20" />
                <span>暂时没有完整连接参数，下面列出这个领域可选择的 MCP 连接方式。</span>
              </div>
              <!-- 抽取失败 -->
              <div v-else-if="fillState === 'error'" class="sv-ac-state">
                <Icon name="circle-x" :size="20" />
                <span>文档里没找到可信任的命令，已转为手动填写（见下方手动添加）。</span>
              </div>

              <!-- C2：自动填参预览态（候选卡） -->
              <div v-for="c in acCandidates" :key="candidateKey(c)" class="sv-preview" data-role="mcp-preview"
                   :data-fill-state="acSelected === c ? 'registering' : (c.trust === 'source_untrusted' ? 'edge' : 'populated')"
                   v-show="fillState === 'populated' || (acSelected === c)">
                <div class="sv-preview-head">
                  <b>{{ acCardTitle(c) }}</b>
                  <span class="sv-cap-badge" :data-trust="c.trust_tier || (c.trust === 'source_untrusted' ? 'unknown' : 'official')">{{ acTrustTag(c) }}</span>
                </div>
                <!-- stdio：命令 / 参数 / 环境变量 -->
                <template v-if="!isHttpTransport(c)">
                  <label class="sv-label">将要运行的命令</label>
                  <pre class="sv-cmd" data-role="command-preview">{{ acResolvedCmd(c) }}</pre>
                  <label class="sv-label">参数</label>
                  <pre class="sv-cmd">{{ (c.config.args || []).join(' ') || '无' }}</pre>
                  <label class="sv-label">环境变量</label>
                  <pre class="sv-cmd" data-role="env-preview">{{ acEnvMasked(c) }}</pre>
                </template>
                <!-- http（远程 MCP）：连接地址 / 请求头 -->
                <template v-else>
                  <label class="sv-label">连接地址</label>
                  <pre class="sv-cmd" data-role="url-preview">{{ c.config.url || '无' }}</pre>
                  <label class="sv-label">请求头</label>
                  <pre class="sv-cmd" data-role="headers-preview">{{ acHeadersMasked(c) }}</pre>
                </template>

                <!-- C6：可信来源域徽标折叠态 -->
                <div class="sv-src-row" data-role="src-badges">
                  <span v-for="s in acSources(c)" :key="s.domain" class="sv-src-badge" :data-trust="s.trust">
                    <Icon name="globe" :size="16" />{{ s.domain }}
                    <span class="sv-src-tag" :data-tone="s.trust">{{ s.trust === 'official' ? '官方' : s.trust === 'community' ? '社区' : '未知' }}</span>
                  </span>
                  <a v-if="c.config.provenance?.url" class="sv-doc-link" :href="c.config.provenance.url" target="_blank" rel="noreferrer">
                    <Icon name="book-open" :size="16" />查看文档
                  </a>
                </div>

                <!-- C5：连接诊断图（试连时） -->
                <svg v-if="acSelected === c" class="mcp-diag" data-role="mcp-diag" viewBox="0 0 680 120" role="img" aria-label="连接诊断">
                  <line v-for="n in 5" :key="'seg'+n" class="mcp-seg"
                        :data-state="acDiag[n-1].state === 'done' || acDiag[n].state === 'done' ? 'done' : 'idle'"
                        :x1="70 + (n-1) * 110" y1="40" :x2="70 + n * 110" y2="40" />
                  <g v-for="(node, i) in acDiag" :key="'node'+i" class="mcp-node" :data-state="node.state" :data-step="node.step"
                     :transform="`translate(${40 + i * 110},40)`">
                    <circle class="mcp-ring" r="16" />
                    <text class="mcp-lbl" y="34">{{ node.label }}</text>
                    <text v-if="node.sub" class="mcp-sub" y="50">{{ node.sub }}</text>
                  </g>
                </svg>

                <div class="sv-row-actions">
                  <button class="sv-btn sv-primary" :disabled="acProbing" @click="probeCandidate(c)">
                    <Icon v-if="acProbing && acSelected === c" name="loader" :size="16" class="dm-spin" />
                    <template v-else>测试连接</template>
                  </button>
                  <button v-if="c.config.command_unresolved || acNeedsSecret(c) || acSecretSpecs(c).length" class="sv-mini" @click="openRegister(c)">
                    <Icon name="lock" :size="16" />{{ acNeedsSecret(c) || c.config.command_unresolved ? '填写凭证' : '填写可选凭证' }}
                  </button>
                  <button class="sv-mini" :disabled="acProbing" @click="acSelected === c ? (acSelected = null) : (acSelected = c)">
                    {{ acSelected === c ? '收起' : '编辑' }}
                  </button>
                </div>
                <p v-if="acSelected === c && acProbe && acProbe.probe_ok" class="sv-ok">
                  MCP 服务已连接，发现 {{ acProbe.tools?.length || 0 }} 个工具。
                </p>
                <p v-if="acSelected === c && acProbe?.readiness_message" class="sv-ac-state">
                  {{ acProbe.readiness_message }}
                </p>
                <button v-if="acSelected === c && acProbe && acProbe.probe_ok" class="sv-btn sv-primary sv-block" @click="openConfirm">
                  <Icon name="shield-check" :size="16" />确认添加并启用
                </button>
              </div>

              <div v-for="item in mcpDirectoryResults" :key="`directory-${item.id}`" class="sv-directory-card" data-role="mcp-directory-results">
                <div class="sv-directory-head">
                  <div>
                    <b>{{ item.label }}</b>
                    <p>{{ item.summary }}</p>
                  </div>
                  <span class="sv-cap-badge" data-trust="community">领域连接方式</span>
                </div>
                <div class="sv-directory-options">
                  <div v-for="option in item.connection_options" :key="`${item.id}-${option.id || option.transport}`" class="sv-directory-option">
                    <div class="sv-list-main">
                      <span class="sv-list-name">{{ option.label || option.transport.toUpperCase() }}</span>
                      <span class="sv-list-sub">{{ option.when }} · {{ option.value }}</span>
                    </div>
                    <button class="sv-mini" @click="useDirectoryOption(item, option)">选择此方式</button>
                  </div>
                </div>
                <ol class="sv-guide"><li v-for="step in item.setup_steps" :key="step">{{ step }}</li></ol>
                <div v-if="item.sources.length" class="sv-source-list">
                  <a v-for="source in item.sources.slice(0, 3)" :key="source" :href="source" target="_blank" rel="noreferrer">{{ directorySourceLabel(source) }}</a>
                </div>
              </div>
            </div>

            <!-- C3：写盘确认卡（最重要） -->
            <div v-if="acConfirmOpen" class="sv-mask wb-modal-backdrop" data-dialog="mcp-confirm" @click.self="closeConfirm">
              <div class="sv-dialog wb-modal-shell" style="width: 460px">
                <div class="sv-dialog-head wb-modal-head">
                  <Icon name="shield-check" :size="24" />
                  <div><h3 class="sv-title">确认添加这个连接器？</h3>
                  <p class="sv-hint">Agent 工作台 会把下面这条配置写入本机并立即启用，之后你可以在对话里调用它的工具。</p></div>
                </div>
                <div class="sv-dialog-body" v-if="acSelected">
                  <template v-if="!isHttpTransport(acSelected)">
                    <label class="sv-label">将要运行的命令</label>
                    <pre class="sv-cmd" data-role="command-preview">{{ acResolvedCmd(acSelected) }}</pre>
                    <p class="sv-list-sub">环境变量：{{ acEnvMasked(acSelected) }}</p>
                  </template>
                  <template v-else>
                    <label class="sv-label">连接地址</label>
                    <pre class="sv-cmd" data-role="url-preview">{{ acSelected.config.url || '无' }}</pre>
                    <p class="sv-list-sub">请求头：{{ acHeadersMasked(acSelected) }}</p>
                  </template>
                  <label class="sv-label">参数来源</label>
                  <div class="sv-src-row" data-role="src-badges">
                    <span v-for="s in acSources(acSelected)" :key="s.domain" class="sv-src-badge" :data-trust="s.trust">
                      <Icon name="globe" :size="16" />{{ s.domain }}
                      <span class="sv-src-tag" :data-tone="s.trust">{{ s.trust === 'official' ? '官方' : s.trust === 'community' ? '社区' : '未知' }}</span>
                    </span>
                  </div>
                  <p v-if="!isHttpTransport(acSelected)" class="sv-note" data-tone="neutral">这条命令会在本机启动一个进程来提供工具；它只在你主动调用时才运行，不会在后台自行动作。</p>
                  <p v-else class="sv-note" data-tone="neutral">这是一个远程 MCP 服务：只有你在对话里调用它的工具时才会向上面这个地址发起请求，不会在后台自行动作。</p>
                  <p class="sv-note" data-tone="warn" v-if="acSelected.trust === 'source_untrusted'">{{ isHttpTransport(acSelected) ? '该来源未被标记为官方渠道，请确认连接地址与公开文档一致后再启用。' : '该来源未被标记为官方渠道，请确认命令与公开文档一致后再启用。' }}</p>
                  <button class="sv-expand" data-action="toggle-config" @click="acShowRaw = !acShowRaw">
                    <Icon name="chevron-down" :size="16" :class="acShowRaw ? 'dm-rot' : ''" />我想先看看完整配置
                  </button>
                  <pre v-show="acShowRaw" class="sv-detail" data-role="raw-config">{{ acRawJson(acSelected) }}</pre>
                  <p v-if="acConfirmError" class="sv-err">{{ acConfirmError }}</p>
                </div>
                <div class="sv-dialog-actions">
                  <button class="sv-mini" data-action="cancel" :disabled="acConfirmBusy" @click="closeConfirm">取消</button>
                  <button class="sv-btn" data-action="add-only" :disabled="acConfirmBusy" @click="confirmConnect">仅添加不启用</button>
                  <button class="sv-btn sv-primary" data-action="confirm-enable" :disabled="acConfirmBusy" @click="confirmConnect">
                    {{ acConfirmBusy ? '写入中…' : '确认添加并启用' }}
                  </button>
                </div>
              </div>
            </div>

            <!-- C4：注册代管弹窗（L0 自动填充 / L1 停-继续 / L2 人工回填兜底） -->
            <div v-if="regOpen" class="sv-mask wb-modal-backdrop" data-dialog="mcp-register" :data-tier="regTier" :data-status="regStatus" @click.self="closeRegister">
              <div class="sv-dialog wb-modal-shell" style="width: 440px">
                <div class="sv-dialog-head wb-modal-head">
                  <Icon :name="regView === 'running' ? 'zap' : regView === 'waiting' ? 'user-check' : 'lock'" :size="24" />
                  <h3 class="sv-title">{{ regView === 'running' ? '自动填充注册信息' : regView === 'waiting' ? '需要你确认一步' : '这一步需要你手动完成' }}</h3>
                </div>
                <div class="sv-dialog-body">
                  <!-- 自动进行中（L0 填表 / L1 续跑）：进度态 -->
                  <template v-if="regView === 'running'">
                    <p class="sv-hint">Agent 工作台 正在官方页面按文档结构填入并提交，全程限定在可信域内完成；凭证不会进入对话上下文。</p>
                    <div class="sv-ac-state" data-role="l0-progress">
                      <Icon name="loader" :size="20" class="dm-spin" />
                      <span>自动填充中{{ regStep ? '：' + regStepText : '…' }}</span>
                    </div>
                    <div class="sv-progress" :data-determinate="regProgress > 0 ? '1' : '0'" role="progressbar" aria-label="自动填充进度">
                      <span :style="regProgress > 0 ? { width: regProgress + '%' } : {}"></span>
                    </div>
                    <p class="sv-note" data-tone="neutral">浏览器会保持打开；若页面弹出验证码或二次验证，会自动停下等你确认。</p>
                  </template>

                  <!-- 停-继续：优先显示后端 note（step 专属），否则 user_prompt，用户点一下继续 -->
                  <template v-else-if="regView === 'waiting'">
                    <p class="sv-hint" data-role="l1-prompt">{{ regL1Prompt }}</p>
                    <p v-if="regL1Extra" class="sv-note" data-tone="neutral" data-role="l1-prompt-extra">{{ regL1Extra }}</p>
                    <div class="sv-l1-wait" data-role="l1-wait">
                      <Icon name="user-check" :size="20" />
                      <span>浏览器窗口保持打开，Agent 工作台 会在你完成后继续。</span>
                    </div>
                    <a v-if="regUrl" class="sv-btn sv-primary sv-ext" :href="regUrl" target="_blank" rel="noreferrer">
                      <Icon name="external-link" :size="16" />在浏览器打开注册页
                    </a>
                    <p v-if="regError" class="sv-err">{{ regError }}</p>
                  </template>

                  <!-- 人工回填（默认与异常兜底路径） -->
                  <template v-else>
                    <p class="sv-hint">这个连接器无法自动配置——先在官网创建账号并拿到连接凭证（API Key），填好后我帮你写入并测试。</p>
                    <p v-if="regNote" class="sv-note" data-tone="neutral" data-role="reg-note">{{ regNote }}</p>
                    <ol class="sv-guide"><li>注册账号</li><li>创建凭证</li><li>复制 Key</li></ol>
                    <a v-if="regUrl" class="sv-btn sv-primary sv-ext" :href="regUrl" target="_blank" rel="noreferrer">
                      <Icon name="external-link" :size="16" />去官网创建凭证
                    </a>
                    <!-- 候选已带 @secret 引用：按 provider 逐项填写（可能多项，如 slack + slack_team） -->
                    <template v-if="regHasRefs">
                      <p class="sv-list-sub">该连接器引用了 {{ regFields.length }} 项凭证，必填项需填写，可选项可留空。</p>
                      <template v-for="f in regFields" :key="f.provider">
                        <label class="sv-label">凭证 · {{ f.provider }}（{{ f.required ? '必填' : '可选' }}）</label>
                        <div class="sv-input-wrap">
                          <Icon name="lock" :size="20" />
                          <input v-model="f.value" class="sv-input" type="password" placeholder="粘贴凭证" autocomplete="off" spellcheck="false" />
                        </div>
                      </template>
                    </template>
                    <!-- 无引用（命令未解析等）：沿用单字段旧行为 -->
                    <template v-else>
                      <label class="sv-label">API Key / 连接凭证</label>
                      <div class="sv-input-wrap">
                        <Icon name="lock" :size="20" />
                        <input v-model="regFields[0].value" class="sv-input" type="password" placeholder="粘贴 API Key" autocomplete="off" spellcheck="false" />
                      </div>
                    </template>
                    <p class="sv-note" data-tone="neutral">凭证会存进本机钥匙串（OS keychain），仅该连接器调用时使用，不会外传。</p>
                    <p v-if="regError" class="sv-err">{{ regError }}</p>
                  </template>
                </div>
                <div class="sv-dialog-actions">
                  <button class="sv-mini" :data-action="regView === 'manual' ? 'cancel' : 'manual'" @click="regView === 'manual' ? closeRegister() : switchToManual()">
                    {{ regView === 'manual' ? '取消' : '改用手动填写' }}
                  </button>
                  <button v-if="regView === 'manual'" class="sv-btn sv-primary" data-action="continue" :disabled="regCommitting || !regCanCommit" @click="commitRegister">
                    {{ regCommitting ? '处理中…' : '写入并测试连接' }}
                  </button>
                  <button v-else-if="regView === 'waiting'" class="sv-btn sv-primary" data-action="resume" :disabled="regResuming" @click="resumeRegister">
                    <Icon v-if="regResuming" name="loader" :size="16" class="dm-spin" />
                    <template v-else><Icon name="circle-check" :size="16" />我已完成，继续</template>
                  </button>
                  <span v-else class="sv-wait-inline"><Icon name="loader" :size="16" class="dm-spin" />自动进行中…</span>
                </div>
              </div>
            </div>

            <div v-if="mcpProposals.length" class="sv-pending-proposals" data-section="mcp-pending-proposals">
              <h4 class="sv-h4">待装配连接器（助手申请，需你确认）</h4>
              <p class="sv-hint">助手请求装配下列连接器。确认后会以**完全相同**的参数落盘并记录审批，助手随后重试即可放行——确认前它无法使用该连接器。</p>
              <ul class="sv-list">
                <li v-for="p in mcpProposals" :key="p.target" class="sv-list-item wb-card">
                  <div class="sv-list-main">
                    <span class="sv-list-name">{{ (p.config.label as string) || p.key }}</span>
                    <span class="sv-list-sub">{{ p.config.transport }} · <code>{{ proposalCmd(p) }}</code></span>
                  </div>
                  <div class="sv-row-actions">
                    <button class="sv-mini sv-approve" :disabled="mcpConfirming === p.target" @click="confirmPendingProposal(p)">{{ mcpConfirming === p.target ? '确认中…' : '确认装配' }}</button>
                  </div>
                </li>
              </ul>
            </div>

            <ul class="sv-list" v-if="mcpServers.length">
              <li v-for="s in mcpServers" :key="s.key" class="sv-list-item wb-card">
                <div class="sv-list-main">
                  <span class="sv-list-name">{{ s.label || s.key }}</span>
                  <span class="sv-list-sub">{{ s.transport }}{{ s.enabled ? ' · 已启用' : ' · 未启用' }} · {{ (mcpCapabilities.active[s.key]?.domain || s.engine || '未发现能力') }}</span>
                </div>
                <div class="sv-row-actions">
                  <button class="sv-mini" :disabled="mcpDiscovering === s.key || !s.enabled" @click="discoverMcp(s.key)">{{ mcpDiscovering === s.key ? '发现中…' : '发现能力' }}</button>
                  <button class="sv-mini" @click="removeMcp(s.key)">移除</button>
                </div>
              </li>
            </ul>
            <p v-else-if="!mcpLoading" class="sv-hint">暂无 MCP Server。</p>
            <label class="sv-toggle-row"><input v-model="mcpWebLearn" type="checkbox" />允许联网补充工具说明来源（只生成候选，不自动启用）</label>
            <div v-for="(candidate, key) in mcpCapabilities.pending" :key="`pending-${key}`" class="sv-cap-card">
              <div><b>{{ key }} · {{ candidate.domain.toUpperCase() }}</b><span class="sv-list-sub">候选能力：{{ candidate.capabilities.join('、') || '通用 MCP 工具' }} · 置信度 {{ Math.round(candidate.confidence * 100) }}%</span></div>
              <div class="sv-row-actions"><button class="sv-mini sv-approve" @click="decideMcpCapability(key, true)">批准路由</button><button class="sv-mini" @click="decideMcpCapability(key, false)">拒绝</button></div>
            </div>

            <div class="sv-sep"></div>
            <h4 class="sv-h4">手动添加连接器（高级）</h4>
            <p class="sv-hint">自动连接没覆盖时，可手动填写并添加；命令与参数需以官方文档为准。</p>
            <label class="sv-label">标识 key</label>
            <input v-model="mcpForm.key" class="sv-input" data-mcp-manual-key placeholder="my-server" spellcheck="false" />
            <label class="sv-label">名称</label>
            <input v-model="mcpForm.label" class="sv-input" placeholder="我的服务" spellcheck="false" />
            <label class="sv-label">传输方式</label>
            <select v-model="mcpForm.transport" class="sv-input">
              <option value="stdio">stdio</option>
              <option value="http">http</option>
            </select>
            <template v-if="mcpForm.transport === 'stdio'">
              <label class="sv-label">命令</label>
              <input v-model="mcpForm.command" class="sv-input" placeholder="uvx" spellcheck="false" />
              <label class="sv-label">参数（空格分隔）</label>
              <input v-model="mcpForm.args" class="sv-input" placeholder="mcp-server-godot" spellcheck="false" />
            </template>
            <template v-else>
              <label class="sv-label">URL</label>
              <input v-model="mcpForm.url" class="sv-input" placeholder="https://.../mcp" spellcheck="false" />
            </template>
            <div class="sv-actions">
              <button class="sv-btn sv-primary" :disabled="mcpAdding" @click="addMcp">
                {{ mcpAdding ? '添加中…' : '添加' }}
              </button>
            </div>
          </section>

          <!-- 智能体 -->
          <section v-show="tab === 'agent'" class="sv-panel">
            <h4 class="sv-h4">多 Agent 工作负载</h4>
            <p class="sv-hint">主 Agent 会按任务需要拆分工作；这里可以分别限制主/子 Agent 步数、自动纠偏、续写和整轮 Token 消耗。</p>
            <div class="sv-form-grid">
              <label class="sv-field">
                <span>最多 Agent 数（0 = 不设上限）</span>
                <input v-model="capacityValues.maxAgents" class="sv-input" type="number" min="0" :max="capacityMax('maxAgents') || undefined" step="1" />
              </label>
              <label class="sv-field">
                <span>主 Agent 工具步数（0 = 不设上限）</span>
                <input v-model="capacityValues.mainMaxSteps" class="sv-input" type="number" min="0" :max="capacityMax('mainMaxSteps') || undefined" step="1" />
              </label>
              <label class="sv-field">
                <span>每个子 Agent 工具步数（0 = 不设上限）</span>
                <input v-model="capacityValues.childMaxSteps" class="sv-input" type="number" min="0" :max="capacityMax('childMaxSteps') || undefined" step="1" />
              </label>
              <label class="sv-field">
                <span>任务 Token 上限（0 = 不限）</span>
                <input v-model="capacityValues.taskTokenBudget" class="sv-input" type="number" min="0" :max="capacityMax('taskTokenBudget') || undefined" step="1000" />
                <span v-if="capacityFloor('taskTokenBudget')" class="sv-field-sub">正数至少 {{ capacityFloor('taskTokenBudget') }}，再小连一轮问答都跑不完；完全不限请填 0</span>
              </label>
              <label class="sv-field">
                <span>自动纠偏次数（0 = 不限）</span>
                <input v-model="capacityValues.maxNudges" class="sv-input" type="number" min="0" :max="capacityMax('maxNudges') || undefined" step="1" />
              </label>
              <label class="sv-field">
                <span>最终续写次数（0 = 不限）</span>
                <input v-model="capacityValues.maxFinalContinuations" class="sv-input" type="number" min="0" :max="capacityMax('maxFinalContinuations') || undefined" step="1" />
              </label>
            </div>
            <label class="sv-toggle-row">
              <input v-model="localAutoLimit" type="checkbox" />
              <span>本地模型按当前显存余量自动控制并发</span>
            </label>
            <div v-if="capacityInfo" class="sv-note">
              <!-- 上限从哪来的，直接写在界面上：用户能看到它是服务端给的，不是页面自己定的 -->
              <div>服务端下发上限：{{ CAPACITY_KEYS.map(f => `${CAPACITY_LABEL[f]} ${capacityMax(f) || '不限'}`).join(' · ') }}</div>
              <!-- 下界单独一行：它只约束「填了正数」的情况，0 仍是合法的不限，
                   所以不能并进上面那句，也不能做成 input 的 min。 -->
              <div v-if="capacityFloor('taskTokenBudget')">
                服务端下发下界：{{ CAPACITY_LABEL.taskTokenBudget }} 填正数时不得小于
                {{ capacityFloor('taskTokenBudget') }}（低于此值一轮问答都跑不完，服务端会直接拒绝）
              </div>
              <div>{{ capacityInfo.model.provider }} / {{ capacityInfo.model.name || '默认模型' }} ·
                {{ capacityInfo.model.local_endpoint ? '本机推理端点' : '远程推理端点' }}</div>
              <div v-if="capacityInfo.resources.available">
                本机 NVIDIA 显存 {{ (capacityInfo.resources.used_mb / 1024).toFixed(1) }} /
                {{ (capacityInfo.resources.total_mb / 1024).toFixed(1) }} GB 已用 ·
                {{ capacityInfo.resources.gpus.length }} 张卡
                <span v-if="capacityInfo.effective_local_parallelism !== null">
                  · 当前估算并发上限 {{ capacityInfo.effective_local_parallelism }}
                </span>
              </div>
              <div v-else>未检测到可用的本机 NVIDIA 显存数据。</div>
              <div>{{ capacityInfo.resource_note }}</div>
              <div v-if="capacityInfo.loaded_models.available">
                Ollama 已加载模型显存：
                <span v-if="capacityInfo.loaded_models.models.length">
                  {{ capacityInfo.loaded_models.models.map(m => `${m.name} ${(m.size_vram_bytes / 1073741824).toFixed(1)} GB`).join(' · ') }}
                </span>
                <span v-else>当前没有已加载模型</span>
              </div>
              <div v-else-if="capacityInfo.model.local_endpoint">
                当前推理服务未提供可读取的模型级显存数据；显示的是本机显卡与进程占用。
              </div>
              <div v-for="gpu in capacityInfo.resources.gpus" :key="gpu.index">
                GPU {{ gpu.index }} · {{ gpu.name }} · {{ (gpu.used_mb / 1024).toFixed(1) }} /
                {{ (gpu.total_mb / 1024).toFixed(1) }} GB
              </div>
              <div v-for="app in capacityInfo.resources.compute_apps" :key="app.pid">
                进程 {{ app.process_name || '未知' }} · {{ (app.used_mb / 1024).toFixed(1) }} GB
              </div>
            </div>
            <p v-if="capacityError" class="sv-err">{{ capacityError }}</p>
            <p v-if="capacitySaved" class="sv-ok">{{ capacitySaved }}</p>
            <div class="sv-actions">
              <button class="sv-btn sv-primary" :disabled="capacitySaving" @click="saveAgentCapacity">
                {{ capacitySaving ? '保存中…' : '保存工作负载设置' }}
              </button>
            </div>
            <div class="sv-sep"></div>
            <h4 class="sv-h4">长期记忆优化</h4>
            <p class="sv-hint">开启后，每轮对话不再整段逐字写入长期记忆：系统会在后台用模型把它蒸馏成精简的可复用记忆，寒暄/纯命令这类低价值轮次自动作废，召回时再叠加重要度与语义相关度排序。关闭则完全恢复逐字记忆（与改动前一致）。切换即时生效并落盘，重启后仍保持。</p>
            <label class="sv-toggle-row">
              <input type="checkbox" :checked="memoryOptimized" :disabled="memorySaving" @change="toggleMemory" />
              <span>启用长期记忆优化（异步蒸馏 + 混合召回）</span>
            </label>
            <p v-if="memorySaving" class="sv-field-sub">正在切换…</p>
            <p v-else-if="memorySaved" class="sv-ok">{{ memorySaved }}</p>
            <div class="sv-sep"></div>
            <h4 class="sv-h4">学习习惯（透明统计）</h4>
            <p class="sv-hint">开启后，系统只从你能看见的信号里做纯计数——你明确提出的回答要求（如「简洁点」「用中文」）、你本轮实际改动的文件类型、常用到的能力——形成一份建议级的使用习惯，帮 Agent 更贴合你的偏好。它不调用模型、不联网、绝不推断任何隐藏个人信息；学到的内容永远只是参考，你手动填写的画像优先级更高，且随时可逐条查看或一键清空。关闭则完全停止统计、也不再注入，行为与改动前逐字一致。默认关闭，切换即时生效并落盘，重启后仍保持。</p>
            <label class="sv-toggle-row">
              <input type="checkbox" :checked="memoryLearn" :disabled="learnSaving" @change="toggleLearn" />
              <span>启用学习习惯透明统计（不推断个人信息）</span>
            </label>
            <p v-if="learnSaving" class="sv-field-sub">正在切换…</p>
            <p v-else-if="learnSaved" class="sv-ok">{{ learnSaved }}</p>
            <div class="sv-sep"></div>
            <h4 class="sv-h4">智能体（本地预设）</h4>
            <p class="sv-hint">保存常用 Agent 预设（名称 / 模型 / 提示词），便于在对话前快速切换。当前存于本机浏览器。</p>
            <ul class="sv-list" v-if="agents.length">
              <li v-for="a in agents" :key="a.id" class="sv-list-item wb-card">
                <div class="sv-list-main">
                  <span class="sv-list-name">{{ a.name }}</span>
                  <span class="sv-list-sub">{{ a.model || '默认模型' }} · {{ (a.system || '').slice(0, 24) }}{{ (a.system || '').length > 24 ? '…' : '' }}</span>
                </div>
                <button class="sv-mini" @click="removeAgent(a.id)">删除</button>
              </li>
            </ul>
            <p v-else class="sv-hint">还没有智能体预设。</p>

            <div class="sv-sep"></div>
            <h4 class="sv-h4">添加智能体</h4>
            <label class="sv-label">名称</label>
            <input v-model="agentForm.name" class="sv-input" placeholder="Cherry 小助手" spellcheck="false" />
            <label class="sv-label">模型（留空=默认）</label>
            <input v-model="agentForm.model" class="sv-input" placeholder="qwen-plus" spellcheck="false" />
            <label class="sv-label">提示词 / 角色设定</label>
            <textarea v-model="agentForm.system" class="sv-input sv-textarea" rows="3" placeholder="你是一个专注于……的助手"></textarea>
            <div class="sv-actions">
              <button class="sv-btn sv-primary" :disabled="!agentForm.name.trim()" @click="addAgent">添加</button>
            </div>

            <div class="sv-sep"></div>
            <div class="sv-section-head">
              <div>
                <h4 class="sv-h4">长期记忆与用户画像</h4>
                <p class="sv-hint">记忆按当前项目保存；画像只使用你明确填写或声明的内容，模型不会从项目文件推断个人信息。</p>
              </div>
              <button class="sv-mini" :disabled="profileLoading" @click="loadMemoryProfile">刷新</button>
            </div>
            <div v-if="profileLoading" class="sv-hint">读取中…</div>
            <div class="sv-form-grid">
              <label class="sv-field"><span>称呼</span><input v-model="profile.display_name" class="sv-input" placeholder="例如：小林" /></label>
              <label class="sv-field"><span>语言</span><input v-model="profile.language" class="sv-input" placeholder="例如：中文" /></label>
              <label class="sv-field"><span>时区</span><input v-model="profile.timezone" class="sv-input" placeholder="例如：Asia/Shanghai" /></label>
              <label class="sv-field"><span>所在地</span><input v-model="profile.location" class="sv-input" placeholder="例如：南宁" /></label>
            </div>
            <label class="sv-label">长期目标（每行一项）</label>
            <textarea :value="profile.goals.join('\n')" class="sv-input sv-textarea" rows="3" placeholder="例如：完成 Agent 工作台 游戏项目\n建立可复用的工作流" @input="updateGoals" />
            <label class="sv-label">补充偏好与备注</label>
            <textarea v-model="profile.notes" class="sv-input sv-textarea" rows="3" placeholder="只填写你希望 Agent 长期记住的内容" />
            <p v-if="profileError" class="sv-err">{{ profileError }}</p>
            <p v-if="profileSaved" class="sv-ok">{{ profileSaved }}</p>
            <div class="sv-actions"><button class="sv-btn sv-primary" :disabled="profileSaving" @click="saveMemoryProfile">{{ profileSaving ? '保存中…' : '保存画像' }}</button></div>
            <div v-if="memoryItems.length" class="sv-memory-list">
              <div class="sv-memory-head"><span>项目长期记忆</span><span>{{ memoryItems.length }} 条</span></div>
              <div v-for="item in memoryItems" :key="item.id" class="sv-memory-row">
                <div class="sv-list-main"><b>{{ item.title || item.kind }}</b><span class="sv-list-sub">{{ item.content }}</span></div>
                <button class="sv-mini" title="删除这条记忆" @click="deleteMemory(item.id)">删除</button>
              </div>
            </div>
            <p v-else-if="!memoryLoading" class="sv-hint">当前项目还没有长期记忆。</p>
            <div class="sv-sep"></div>
            <div class="sv-section-head">
              <div>
                <h4 class="sv-h4">系统学到的使用习惯</h4>
                <p class="sv-hint">下面是从你实际使用中做纯计数得到的习惯（非推断个人信息），仅供 Agent 参考、优先级低于你手动填写的画像。可随时逐条清空。</p>
              </div>
              <button class="sv-mini" :disabled="clearingLearned" @click="clearLearned">{{ clearingLearned ? '清空中…' : '一键清空' }}</button>
            </div>
            <p v-if="!learnedGroups.length" class="sv-hint">还没有学到任何习惯{{ memoryLearn ? '（开启后会在你正常使用的过程中逐步统计）' : '（需先开启上方「学习习惯透明统计」开关）' }}。</p>
            <div v-else class="sv-memory-list">
              <div v-for="g in learnedGroups" :key="g.label" class="sv-memory-row">
                <div class="sv-list-main">
                  <b>{{ g.label }}</b>
                  <span class="sv-list-sub">{{ g.items.map(r => `${r.value}×${r.count}`).join('、') }}</span>
                </div>
              </div>
            </div>
          </section>

          <!-- 模型参数统一入口；模型芯片只负责快速切换已保存预设。 -->
          <section v-show="tab === 'model'" class="sv-panel">
            <h4 class="sv-h4">模型与预设</h4>
            <p class="sv-hint">在这里添加模型、修改接口地址、API Key、上下文窗口和能力参数。保存后会立即切换，并在下次启动时自动恢复。</p>
            <div v-if="cfg" class="sv-model-current wb-card">
              <span class="sv-list-sub">当前模型</span>
              <b>{{ cfg.provider_meta?.[cfg.llm_provider]?.label || cfg.llm_provider }} · {{ cfg.llm_model || '默认模型' }}</b>
              <span class="sv-list-sub">已保存预设 {{ cfg.model_presets?.length || 0 }} 个 · {{ cfg.llm_mode === 'auto' ? '自动模式' : '固定模式' }}</span>
            </div>
            <button class="sv-btn sv-primary sv-model-open" :disabled="loading || !cfg" @click="openModelSettings">打开模型参数设置</button>

            <!-- 本机显存与模型驻留：2026-10-19 从问答页侧栏「运行状态」搬来。
                 预加载/卸载显存模型是运维级操作，放在设置里比挂在聊天侧栏合适。 -->
            <div class="sv-section-head" style="margin-top: 18px;">
              <div>
                <h4 class="sv-h4">本机显存与模型驻留</h4>
                <p class="sv-hint">本地模型在需要时才载入显存。预加载会让下一次回答更快，卸载立刻释放显存。</p>
              </div>
            </div>
            <div class="sv-note">
              <div>
                显存占用
                <b>{{ power ? power.vram_gb.toFixed(1) + ' GB' : '—' }}</b>
                <span v-if="power?.loaded?.length"> · {{ power.loaded.length }} 个模型驻留</span>
                <span v-else> · 无驻留模型</span>
              </div>
              <div v-if="power && !power.reachable">推理服务当前不可达，显存状态取不到。</div>
              <div v-if="power?.error" class="sv-err">{{ power.error }}</div>
            </div>
            <div class="sv-actions">
              <button
                class="sv-btn"
                :disabled="powerBusy || !power?.reachable"
                @click="powerAction(power?.loaded?.length ? 'off' : 'on')"
              >
                {{ powerBusy ? '处理中…' : (power?.loaded?.length ? '卸载显存模型' : '预加载显存模型') }}
              </button>
            </div>
            <p v-if="powerMsg" class="sv-ok">{{ powerMsg }}</p>
          </section>

          <!-- 运行与引擎：2026-10-19 从顶栏「更多」菜单迁入。
               这两块原本是两个浮层（AI 运行策略 / 游戏引擎连接），现在就地内嵌在
               设置面板里。用 v-if 挂载 —— 它们各自带轮询（策略面板每 3 秒拉一次
               待批动作），只在这个分区被打开时才有必要跑。 -->
          <section v-if="tab === 'runtime'" class="sv-panel">
            <div class="sv-section-head">
              <div>
                <h4 class="sv-h4">AI 运行策略</h4>
                <p class="sv-hint">AI 在哪运行、难题是否转云端，以及对项目外文件与工具动作的授权。</p>
              </div>
            </div>
            <AgentPolicyPanel v-if="tab === 'runtime'" embedded />
          </section>

          <section v-if="tab === 'runtime'" class="sv-panel sv-panel-runtime">
            <div class="sv-section-head">
              <div>
                <h4 class="sv-h4">引擎与项目</h4>
                <p class="sv-hint">探测项目用的引擎、选择引擎与可执行文件。此前引擎被写死成 Godot，Unity / Unreal 无法在此配置。</p>
              </div>
            </div>
            <EngineSetupPanel v-if="tab === 'runtime'" />
          </section>

          <section v-if="tab === 'runtime'" class="sv-panel sv-panel-runtime">
            <div class="sv-section-head">
              <div>
                <h4 class="sv-h4">游戏引擎连接</h4>
                <p class="sv-hint">连接后，AI 才能读取引擎中的场景、脚本和运行日志。</p>
              </div>
            </div>
            <EngineConnectPopover v-if="tab === 'runtime'" embedded />
          </section>

          <!-- 外观：皮肤（视觉风格） -->
          <section v-show="tab === 'appearance'" class="sv-panel">
            <div class="sv-section-head">
              <div>
                <h4 class="sv-h4">外观</h4>
                <p class="sv-hint">皮肤只改配色、圆角与阴影质感，不动布局与字号。选择后立即生效，问答页与工作台共用同一套。</p>
              </div>
            </div>

            <div class="sv-skins" role="radiogroup" aria-label="皮肤">
              <button
                v-for="s in SKINS"
                :key="s.key"
                type="button"
                class="sv-skin"
                :class="{ on: skin === s.key }"
                role="radio"
                :aria-checked="skin === s.key"
                @click="chooseSkin(s.key)"
              >
                <span class="sv-skin-swatch" aria-hidden="true">
                  <i v-for="(c, i) in s.swatch" :key="i" :style="{ background: c }" />
                </span>
                <span class="sv-skin-text">
                  <b>{{ s.name }}</b>
                  <small>{{ s.desc }}</small>
                </span>
                <span class="sv-skin-check" aria-hidden="true">
                  <Icon v-if="skin === s.key" name="circle-check" :size="16" />
                </span>
              </button>
            </div>

            <p class="sv-note" data-tone="neutral">
              全部 {{ SKINS.length }} 套配色都过了 WCAG AA 对比度校验（正文与实心块上的文字 ≥ 4.5:1，描边 ≥ 3:1）——
              深色皮肤下最容易塌的第三级灰字与状态色是逐套解算出来的，不是手调的。
            </p>

            <!-- 布局：空间构成 -->
            <div class="sv-section-head" style="margin-top: 18px;">
              <div>
                <h4 class="sv-h4">布局</h4>
                <p class="sv-hint">这是改动最大的一项：侧栏停在哪、主区多宽、概览分几栏。配色、造型、材质都不变。</p>
              </div>
            </div>

            <div class="sv-layouts" role="radiogroup" aria-label="布局">
              <button
                v-for="l in LAYOUTS"
                :key="l.key"
                type="button"
                class="sv-layout"
                :class="{ on: layout === l.key }"
                role="radio"
                :aria-checked="layout === l.key"
                @click="chooseLayout(l.key)"
              >
                <span class="sv-layout-demo" :data-layout-demo="l.key" aria-hidden="true">
                  <i class="rail" /><i class="main" />
                </span>
                <span class="sv-skin-text">
                  <b>{{ l.name }}</b>
                  <small>{{ l.desc }}</small>
                </span>
                <span class="sv-skin-check" aria-hidden="true">
                  <Icon v-if="layout === l.key" name="circle-check" :size="16" />
                </span>
              </button>
            </div>

            <!-- 风格：造型语言 —— 2026-10-07 收敛：造型固定为 brutal（零圆角 + 墨描边 + 硬阴影），
                 5 种造型与 8 套皮肤的 40+ 组合谁也调不过来，砍掉选择器只留精调的两套皮肤。 -->

            <!-- 材质：实体 / 玻璃 -->
            <div class="sv-section-head" style="margin-top: 18px;">
              <div>
                <h4 class="sv-h4">材质</h4>
                <p class="sv-hint">材质只改表面：半透明、背景模糊、高光描边。与配色、造型都正交。</p>
              </div>
            </div>

            <div class="sv-mats" role="radiogroup" aria-label="材质">
              <button
                type="button"
                class="sv-mat"
                :class="{ on: material === 'solid' }"
                role="radio"
                :aria-checked="material === 'solid'"
                @click="chooseMaterial('solid')"
              >
                <span class="sv-mat-demo" data-kind="solid" aria-hidden="true"><i /><i /><i /></span>
                <span class="sv-skin-text">
                  <b>实体</b>
                  <small>不透明面板，文字最清晰</small>
                </span>
                <span class="sv-skin-check" aria-hidden="true">
                  <Icon v-if="material === 'solid'" name="circle-check" :size="16" />
                </span>
              </button>
              <button
                type="button"
                class="sv-mat"
                :class="{ on: material === 'glass' }"
                role="radio"
                :aria-checked="material === 'glass'"
                @click="chooseMaterial('glass')"
              >
                <span class="sv-mat-demo" data-kind="glass" aria-hidden="true"><i /><i /><i /></span>
                <span class="sv-skin-text">
                  <b>玻璃</b>
                  <small>半透明 + 背景模糊 + 高光描边</small>
                </span>
                <span class="sv-skin-check" aria-hidden="true">
                  <Icon v-if="material === 'glass'" name="circle-check" :size="16" />
                </span>
              </button>
            </div>

            <p class="sv-note" data-tone="neutral">
              可调的三层互相独立：<b>皮肤</b>（{{ SKINS.map((s) => s.name).join(' / ') }}）·<b>材质</b>（实体/玻璃）·<b>布局</b>（5 种空间构成）。
              造型不再是一个独立开关：硬边还是细线，由所选皮肤自己带（素纸朱红 / 石墨夜航 = 零圆角 + 墨描边 + 硬阴影，
              锌白细线 = 1px 灰边 + 6px 圆角 + 零阴影），全部走 <code>skins.css</code> 的令牌。
              调形状参数改 <code>layout-modes.css</code>，改材质参数改 <code>glass.css</code>，改完刷新即生效，不需要重新构建。
            </p>

            <div class="sv-section-head" style="margin-top: 18px;">
              <div>
                <h4 class="sv-h4">对话与运行展示</h4>
                <p class="sv-hint">这一组管「流式输出时看得清看不清」。问答页与开发台对话台共用同一份，改完立即生效；只存本机，不写后端配置。</p>
              </div>
            </div>

            <label class="sv-toggle-row"><input v-model="dp.showTrace" type="checkbox" @change="saveDisplayPrefs" />显示推理轨迹步骤（分析 / 工具调用 / 返回结果 / 复核）</label>
            <label class="sv-toggle-row"><input v-model="dp.autoExpandTrace" type="checkbox" @change="saveDisplayPrefs" />输出过程中自动摊开最新一条工具行（关掉＝工具行只显示摘要，思考始终外露）</label>
            <label class="sv-pref-row">
              <span>答案下方文件引用最多显示</span>
              <select class="sv-input sv-pref-select" :value="dp.maxRefs" @change="setMaxRefs($event)">
                <option value="0">不限制</option>
                <option value="3">3 条</option>
                <option value="6">6 条</option>
                <option value="12">12 条</option>
              </select>
            </label>
          </section>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sv-mask {
  position: fixed; inset: 0; z-index: 2100;
  background: var(--scrim);
  display: flex; align-items: center; justify-content: center;
}
.sv-box {
  width: 760px; max-width: calc(100vw - 32px); height: 78vh; max-height: 720px;
  background: var(--bg, #fff); border: 1px solid var(--border);
  border-radius: 12px; box-shadow: 0 16px 48px rgba(15, 23, 42, .22);
  display: flex; flex-direction: column; overflow: hidden;
}
.sv-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid var(--border);
}
.sv-title { margin: 0; font-size: var(--fs-h3); font-weight: 600; }
.sv-x {
  border: none; background: transparent; font-size: var(--fs-xl); line-height: 1;
  color: var(--text-muted); cursor: pointer; padding: 0 6px;
}
.sv-x:hover { color: var(--text); }
.sv-body { display: flex; flex: 1; min-height: 0; }
.sv-nav {
  width: 168px; flex: none; border-right: 1px solid var(--border);
  padding: 10px 8px; display: flex; flex-direction: column; gap: 4px;
  background: var(--bg-selected);
  position: relative; z-index: 2; pointer-events: auto;
}
.sv-nav-item {
  text-align: left; padding: 9px 12px; border: 1px solid transparent; border-radius: 8px;
  position: relative; z-index: 1; width: 100%;
  background: transparent; color: var(--text); cursor: pointer; font-size: var(--fs-md);
  pointer-events: auto;
}
/* --bg-input 从来没在任何皮肤里定义过 —— 两处引用让导航项的 hover 与选中底
   一直是透明的，选中态只剩描边 + 字色。收回到确实存在的抬起面。 */
.sv-nav-item:hover { background: var(--bg-raised); }
.sv-nav-item.on { background: var(--bg-raised); border-color: var(--accent); color: var(--accent-text); font-weight: 600; }

/* 外观：皮肤卡片（色板 + 名称 + 说明 + 选中态）
   色板用 SKINS 里的 swatch 四色直接内联，**不读当前皮肤的 CSS 变量** ——
   否则所有卡片都会显示当前皮肤的颜色，就分不出"点了会变成什么样"了。 */
.sv-skins { display: grid; grid-template-columns: repeat(auto-fill, minmax(258px, 1fr)); gap: 10px; margin-top: 12px; }
.sv-skin {
  display: grid; grid-template-columns: 44px 1fr 18px; align-items: center; gap: 11px;
  padding: 11px 12px; text-align: left; cursor: pointer;
  background: var(--bg-raised); border: var(--edge-w-strong, 2px) solid var(--border);
  border-radius: var(--radius-md); color: var(--text);
  transition: background var(--motion-base) var(--ease-standard);
}
.sv-skin:hover { background: var(--bg-hover); }
.sv-skin.on { border-color: var(--accent); --btn-fill: var(--accent-soft, var(--bg-hover)); background: var(--accent-soft, var(--bg-hover)); }
.sv-skin:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.sv-skin-swatch { display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; gap: 2px; width: 44px; height: 44px; border: 1px solid var(--border); flex: none; }
.sv-skin-swatch i { display: block; }
.sv-skin-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.sv-skin-text b { font-size: var(--fs-md); font-weight: 700; color: var(--text); }
.sv-skin-text small { font-size: var(--fs-cap, 11px); line-height: 1.45; color: var(--text-muted); }
.sv-skin-check { display: grid; place-items: center; color: var(--accent); }
.sv-skin:not(.on) .sv-skin-check { color: transparent; }

/* 布局示意缩略图：**形状本身就是信息** —— 侧栏在哪、主区多宽、是否居中，
   五张必须长得不一样（不是同一张换配色）。i.rail=侧栏，i.main=主区。 */
.sv-layouts { display: grid; grid-template-columns: repeat(auto-fill, minmax(258px, 1fr)); gap: 10px; margin-top: 12px; }
.sv-layout {
  display: grid; grid-template-columns: 44px 1fr 18px; align-items: center; gap: 11px;
  padding: 11px 12px; text-align: left; cursor: pointer;
  background: var(--bg-raised); border: var(--edge-w-strong, 2px) solid var(--border);
  border-radius: var(--radius-md); color: var(--text);
  transition: background var(--motion-base) var(--ease-standard);
}
.sv-layout:hover { background: var(--bg-hover); }
.sv-layout.on { border-color: var(--accent); --btn-fill: var(--accent-soft, var(--bg-hover)); background: var(--accent-soft, var(--bg-hover)); }
.sv-layout:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.sv-layout:not(.on) .sv-skin-check { color: transparent; }
.sv-layout-demo {
  position: relative; width: 44px; height: 34px; flex: none;
  background: var(--bg-hover); border: 1px solid var(--border); overflow: hidden;
}
.sv-layout-demo i { position: absolute; display: block; background: var(--ink); }
.sv-layout-demo i.rail { background: var(--accent); }
.sv-layout-demo i.main { background: var(--ink); }
/* ① fixed：左窄栏 + 右满宽 */
.sv-layout-demo[data-layout-demo='fixed'] .rail { left: 0; top: 0; width: 12px; height: 100%; }
.sv-layout-demo[data-layout-demo='fixed'] .main { left: 15px; top: 0; right: 0; height: 100%; }
/* ② focus：左右留白（虚线示意）+ 居中一条 */
.sv-layout-demo[data-layout-demo='focus'] .rail { display: none; }
.sv-layout-demo[data-layout-demo='focus'] .main { left: 11px; right: 11px; top: 0; height: 100%; opacity: .9; }
/* ③ dock：上满宽 + 下抽屉 */
.sv-layout-demo[data-layout-demo='dock'] .rail { left: 0; bottom: 0; width: 100%; height: 10px; }
.sv-layout-demo[data-layout-demo='dock'] .main { left: 0; top: 0; right: 0; height: 20px; }
/* ④ split：左右对半，中间留缝 */
.sv-layout-demo[data-layout-demo='split'] .rail { left: 0; top: 0; width: 50%; height: 100%; }
.sv-layout-demo[data-layout-demo='split'] .main { left: 50%; top: 0; right: 0; height: 100%; opacity: .8; }
/* ⑤ grid：极窄栏（1/5）+ 满宽主区 */
.sv-layout-demo[data-layout-demo='grid'] .rail { left: 0; top: 0; width: 4px; height: 100%; }
.sv-layout-demo[data-layout-demo='grid'] .main { left: 7px; top: 0; right: 0; height: 100%; }

/* 风格示意缩略图（.sv-styles / .sv-style / .sv-style-demo）已随风格选择器移除：
   造型收敛为 brutal 一种，无需预览卡（2026-10-07）。 */

/* 材质：实体 / 玻璃 */
.sv-mats { display: grid; grid-template-columns: repeat(auto-fill, minmax(258px, 1fr)); gap: 10px; margin-top: 12px; }
.sv-mat {
  display: grid; grid-template-columns: 44px 1fr 18px; align-items: center; gap: 11px;
  padding: 11px 12px; text-align: left; cursor: pointer;
  background: var(--bg-raised); border: var(--edge-w-strong, 2px) solid var(--border);
  border-radius: var(--radius-md); color: var(--text);
  transition: background var(--motion-base) var(--ease-standard);
}
.sv-mat:hover { background: var(--bg-hover); }
.sv-mat.on { border-color: var(--accent); --btn-fill: var(--accent-soft, var(--bg-hover)); background: var(--accent-soft, var(--bg-hover)); }
.sv-mat:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.sv-mat:not(.on) .sv-skin-check { color: transparent; }
/* 材质示意缩略图：实体=不透明方块三层；玻璃=半透+模糊，三层透出彼此 */
.sv-mat-demo { position: relative; width: 44px; height: 44px; flex: none; overflow: hidden; border-radius: 6px; background: var(--bg-hover); }
.sv-mat-demo[data-kind='solid'] i { position: absolute; left: 6px; right: 6px; height: 6px; border-radius: 2px; background: var(--ink); }
.sv-mat-demo[data-kind='solid'] i:nth-child(1) { top: 7px; }
.sv-mat-demo[data-kind='solid'] i:nth-child(2) { top: 18px; opacity: .55; }
.sv-mat-demo[data-kind='solid'] i:nth-child(3) { top: 29px; opacity: .25; }
.sv-mat-demo[data-kind='glass'] { background: linear-gradient(135deg, var(--accent) 0%, var(--bg-hover) 60%); }
.sv-mat-demo[data-kind='glass'] i { position: absolute; left: 6px; right: 6px; height: 8px; border-radius: 4px; background: color-mix(in srgb, var(--bg-raised) 55%, transparent); box-shadow: inset 0 1px 0 rgba(255,255,255,.6); }
.sv-mat-demo[data-kind='glass'] i:nth-child(1) { top: 6px; }
.sv-mat-demo[data-kind='glass'] i:nth-child(2) { top: 17px; }
.sv-mat-demo[data-kind='glass'] i:nth-child(3) { top: 28px; }
.sv-content { flex: 1; min-width: 0; overflow-y: auto; padding: 16px 20px; }
.sv-panel { display: flex; flex-direction: column; gap: 4px; }
/* 「运行与引擎」分区里有两个内嵌面板，用一条分隔线把两块切开，
   否则「AI 运行策略」的尾巴直接顶着「游戏引擎连接」标题，层次读不出来。 */
.sv-panel-runtime { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--border); }
.sv-model-current { display: flex; flex-direction: column; gap: 5px; padding: 13px; margin-top: 8px; background: var(--bg); }
.sv-model-current b { font-size: var(--fs-body); color: var(--text); overflow-wrap: anywhere; }
.sv-model-open { align-self: flex-start; margin-top: 10px; }
.sv-section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.sv-section-head .sv-hint { margin-bottom: 0; }
.sv-h4 { margin: 0 0 6px; font-size: var(--fs-body); font-weight: 600; }
.sv-hint { font-size: var(--fs-sm); color: var(--text-faint); margin: 2px 0 6px; line-height: 1.5; }
.sv-label {
  font-size: var(--fs-sm); color: var(--text-muted); margin-top: 10px;
  display: flex; justify-content: space-between; align-items: center;
}
.sv-key-toggle { color: var(--accent); cursor: pointer; user-select: none; }
.sv-key-toggle:hover { text-decoration: underline; }
.sv-input {
  width: 100%; box-sizing: border-box; padding: 7px 10px; font-size: var(--fs-md);
  border: 1px solid var(--border-graphic); border-radius: 8px;
  background: var(--bg-raised); color: var(--text); outline: none;
}
.sv-input:focus { border-color: var(--accent); }
.sv-textarea { resize: vertical; font-family: inherit; }
.sv-meter-grid {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  margin: 12px 0 8px; border-block: 1px solid var(--border);
}
.sv-meter { min-width: 0; padding: 12px 10px; border-right: 1px solid var(--border); }
.sv-meter:nth-child(3n) { border-right: 0; }
.sv-meter:nth-child(n+4) { border-top: 1px solid var(--border); }
.sv-meter span { display: block; color: var(--text-faint); font-size: var(--fs-cap); }
.sv-meter b { display: block; margin-top: 3px; font-size: var(--fs-lead); font-weight: 650; overflow-wrap: anywhere; }
.sv-provider-list { margin-top: 8px; border-top: 1px solid var(--border); overflow-x: auto; }
.sv-provider-head, .sv-provider-row {
  display: grid; grid-template-columns: minmax(90px, 1fr) 84px 96px 132px 92px;
  gap: 10px; align-items: center; padding: 7px 4px; font-size: var(--fs-sm);
  border-bottom: 1px solid var(--border);
}
.sv-provider-head { color: var(--text-faint); }
.sv-provider-row span { text-align: right; color: var(--text-muted); }
.sv-provider-head span:not(:first-child) { text-align: right; }
.sv-provider-row .sv-kind { text-align: left; font-size: var(--fs-cap); }
/* 缓存命中：有命中才上色——满屏灰色「—」没有信息量，命中那行才该被看见。
   数字用等宽，避免长数字在 grid 里抖动。 */
.sv-provider-row .sv-cache { font-size: var(--fs-cap); font-variant-numeric: tabular-nums; color: var(--text-muted); }
.sv-provider-row .sv-cache.on { color: var(--accent-text); }
.sv-meter-sub { display: block; margin-top: 4px; font-size: var(--fs-cap); line-height: 1.45; color: var(--text-faint); }
.sv-provider-row.simulated { background: color-mix(in srgb, var(--bg-selected) 55%, transparent); }
.sv-note { margin: 8px 0 0; padding-left: 9px; border-left: 2px solid var(--accent); color: var(--text-muted); font-size: var(--fs-cap); line-height: 1.55; }
.sv-form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 12px; }
.sv-field { display: flex; flex-direction: column; gap: 5px; margin-top: 7px; font-size: var(--fs-sm); color: var(--text-muted); }
/* 字段内的服务端约束说明（如 Token 下界）：跟标签同色、比标签小一号 */
.sv-field-sub { font-size: var(--fs-cap); line-height: 1.45; color: var(--text-muted); }
.sv-toggle-row { display: flex; align-items: center; gap: 8px; margin-top: 12px; font-size: var(--fs-md); color: var(--text); cursor: pointer; }
.sv-toggle-row input { width: 16px; height: 16px; }
/* 一行一个参数的偏好行（复选框那种放不下 select，单独一档） */
.sv-pref-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 12px; font-size: var(--fs-md); color: var(--text); }
.sv-pref-select { width: auto; flex: 0 0 auto; }
.sv-sep { height: 1px; background: var(--border); margin: 16px 0 4px; }
.sv-err { font-size: var(--fs-sm); color: var(--danger); margin: 8px 0 0; }
.sv-ok { font-size: var(--fs-sm); color: var(--green); margin: 8px 0 0; }
.sv-actions { display: flex; justify-content: flex-end; margin-top: 14px; }
.sv-btn {
  padding: 7px 18px; font-size: var(--fs-md); border-radius: 8px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text); cursor: pointer;
}
.sv-btn:hover { border-color: var(--accent); }
.sv-primary { --btn-fill: var(--accent); background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.sv-primary:disabled { opacity: .5; cursor: default; }
.sv-list { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.sv-list-item {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 9px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg);
}
.sv-row-actions { display: flex; align-items: center; gap: 6px; flex: none; }
.sv-cap-card { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 8px; padding: 9px 12px; border: 1px solid var(--accent); border-radius: 8px; background: color-mix(in srgb, var(--accent) 7%, var(--bg)); font-size: var(--fs-sm); }
.sv-cap-card > div:first-child { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.sv-approve { color: var(--accent); }
.sv-search-row { display: grid; grid-template-columns: 1fr auto; gap: 8px; margin: 10px 0; }
.sv-directory-card { margin: 8px 0; padding: 11px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg); font-size: var(--fs-sm); }
.sv-directory-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
.sv-directory-head p { margin: 3px 0 0; color: var(--text-muted); }
.sv-directory-card ol { margin: 8px 0; padding-left: 20px; color: var(--text-muted); line-height: 1.65; }
.sv-directory-options { display: flex; flex-direction: column; gap: 6px; margin: 9px 0; }
.sv-directory-option { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-raised); }
.sv-source-list { display: flex; flex-direction: column; gap: 3px; overflow-wrap: anywhere; }
.sv-source-list a { color: var(--accent); }
.sv-list-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.sv-list-name { font-size: var(--fs-md); font-weight: 600; }
.sv-list-sub { font-size: var(--fs-cap); color: var(--text-faint); }
.sv-memory-list { margin-top: 12px; border-top: 1px solid var(--border); }
.sv-memory-head { display: flex; justify-content: space-between; padding: 9px 0 5px; color: var(--text-faint); font-size: var(--fs-cap); }
.sv-memory-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; padding: 9px 0; border-bottom: 1px solid var(--border); }
.sv-memory-row .sv-list-sub { line-height: 1.45; overflow-wrap: anywhere; }
.sv-mini {
  flex: none; font-size: var(--fs-sm); padding: 4px 12px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text); cursor: pointer;
}
.sv-mini:hover { border-color: var(--accent); color: var(--accent); }
.sv-mini:disabled { opacity: .5; cursor: default; }

/* ================= P1：MCP 自动连接向导 ================= */
.sv-step-rail { display: flex; gap: 6px; margin: 10px 0; flex-wrap: wrap; }
.sv-step {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 11px; font-size: var(--fs-sm); border-radius: var(--radius-pill);
  border: 1px solid var(--border); background: var(--bg); color: var(--text-muted); cursor: default;
}
.sv-step .dm-icon { display: inline-flex; }
.sv-step[data-state="done"] { border-color: var(--diag-done); color: var(--diag-done); }
.sv-step[data-state="active"] { border-color: var(--diag-active); color: var(--diag-active); background: var(--bg-selected); font-weight: 600; }
.sv-step[data-state="fail"] { border-color: var(--diag-fail); color: var(--diag-fail); }
.sv-step[data-state="idle"] { opacity: .75; }

.sv-ac-state { display: flex; align-items: center; gap: 8px; margin: 10px 0; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-hover); font-size: var(--fs-sm); color: var(--text-muted); }
.sv-ac-state .dm-icon { color: var(--text-faint); flex: none; }

.sv-preview { margin: 10px 0; padding: 12px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-raised); }
.sv-preview-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.sv-preview-head b { font-size: var(--fs-md); }
.sv-cap-badge { font-size: var(--fs-cap); padding: 2px 9px; border-radius: var(--radius-pill); }
.sv-cap-badge[data-trust="official"] { color: var(--trust-verified-fg); background: var(--trust-verified-bg); }
.sv-cap-badge[data-trust="community"] { color: var(--trust-community-fg); background: var(--trust-community-bg); }
.sv-cap-badge[data-trust="unknown"] { color: var(--trust-unknown-fg); background: var(--trust-unknown-bg); }

.sv-cmd {
  margin: 4px 0 2px; padding: 7px 10px; font-family: var(--font-mono); font-size: var(--fs-sm); line-height: 1.5;
  background: var(--bg-hover); border: 1px solid var(--border); border-radius: var(--radius-md);
  color: var(--text); white-space: pre-wrap; word-break: break-all; max-height: 160px; overflow: auto;
}

.sv-src-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin: 6px 0; }
.sv-src-badge { display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-cap); padding: 2px 9px; border-radius: var(--radius-pill); border: 1px solid var(--border); color: var(--text-muted); }
.sv-src-badge .dm-icon { color: var(--text-faint); }
.sv-src-badge[data-trust="official"] { color: var(--trust-verified-fg); background: var(--trust-verified-bg); border-color: transparent; }
.sv-src-badge[data-trust="community"] { color: var(--trust-community-fg); background: var(--trust-community-bg); border-color: transparent; }
.sv-src-badge[data-trust="unknown"] { color: var(--trust-unknown-fg); background: var(--trust-unknown-bg); }
.sv-src-tag { font-size: var(--fs-cap); opacity: .9; }
.sv-doc-link { display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-cap); color: var(--accent); text-decoration: none; }
.sv-doc-link:hover { text-decoration: underline; }

/* C5：诊断图 */
.mcp-diag { width: 100%; height: auto; margin: 10px 0; display: block; }
.mcp-seg { stroke-width: 2; }
.mcp-seg[data-state="done"] { stroke: var(--diag-seg-done); }
.mcp-seg[data-state="idle"] { stroke: var(--diag-seg-idle); }
.mcp-ring { fill: var(--bg-raised); stroke-width: 2; }
.mcp-node[data-state="done"] .mcp-ring { stroke: var(--diag-done); }
.mcp-node[data-state="active"] .mcp-ring { stroke: var(--diag-active); }
.mcp-node[data-state="fail"] .mcp-ring { stroke: var(--diag-fail); }
.mcp-node[data-state="warn"] .mcp-ring { stroke: var(--diag-warn); }
.mcp-node[data-state="idle"] .mcp-ring { stroke: var(--diag-idle); }
.mcp-lbl { font: 510 var(--fs-sm) var(--font-ui); fill: var(--text); text-anchor: middle; }
.mcp-sub { font: 400 var(--fs-cap) var(--font-ui); fill: var(--text-muted); text-anchor: middle; }

/* C3 / C4：确认卡 / 注册卡（复用 .sv-mask） */
.sv-dialog {
  background: var(--bg-raised); border: 1px solid var(--border);
  border-radius: var(--radius-xl); box-shadow: var(--shadow-pop);
  display: flex; flex-direction: column; max-width: calc(100vw - 32px); max-height: 86vh; overflow: hidden;
}
.sv-dialog-head { display: flex; align-items: flex-start; gap: 10px; padding: 14px 16px; border-bottom: 1px solid var(--border); }
.sv-dialog-head .dm-icon { color: var(--accent); flex: none; margin-top: 1px; }
.sv-dialog-head h3 { margin: 0; font-size: var(--fs-h3); font-weight: 600; }
.sv-dialog-body { padding: 14px 16px; overflow-y: auto; }
.sv-dialog-actions { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--border); }
.sv-dialog-actions .sv-btn, .sv-dialog-actions .sv-mini { margin-left: 0; }
.sv-expand { display: inline-flex; align-items: center; gap: 5px; margin-top: 10px; font-size: var(--fs-sm); color: var(--accent); background: transparent; border: none; cursor: pointer; padding: 0; }
.sv-detail { margin: 8px 0 0; padding: 8px 10px; font-family: var(--font-mono); font-size: var(--fs-cap); background: var(--bg-hover); border: 1px solid var(--border); border-radius: var(--radius-md); white-space: pre-wrap; word-break: break-all; max-height: 180px; overflow: auto; }
.sv-note { margin: 8px 0 0; padding: 8px 10px; border-radius: var(--radius-md); font-size: var(--fs-cap); line-height: 1.55; }
.sv-note[data-tone="neutral"] { background: var(--bg-hover); color: var(--text-muted); border-left: 2px solid var(--border-strong); }
.sv-note[data-tone="warn"] { background: var(--trust-community-bg); color: var(--trust-community-fg); border-left: 2px solid var(--amber); }
.sv-guide { margin: 8px 0; padding-left: 20px; color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.7; }
.sv-input-wrap { display: flex; align-items: center; gap: 8px; border: 1px solid var(--border-graphic); border-radius: var(--radius-md); background: var(--bg-raised); padding: 0 10px; }
.sv-input-wrap:focus-within { border-color: var(--accent); }
.sv-input-wrap .dm-icon { color: var(--text-faint); flex: none; }
.sv-input-wrap .sv-input { border: none; background: transparent; padding-left: 0; padding-right: 0; }
.sv-input-wrap .sv-input:focus { border-color: transparent; }
.sv-ext { display: inline-flex; align-items: center; gap: 6px; margin: 4px 0; text-decoration: none; }
.sv-block { width: 100%; margin-top: 10px; justify-content: center; }

/* C4：L0 自动填充进度 / L1 停-继续 */
.sv-progress { height: 3px; margin: 2px 0; border-radius: var(--radius-pill); background: var(--bg-hover); overflow: hidden; }
.sv-progress span { display: block; height: 100%; width: 38%; border-radius: var(--radius-pill); background: var(--accent); animation: dm-indeterminate 1.2s var(--ease-standard) infinite; }
.sv-progress[data-determinate="1"] span { animation: none; }
@keyframes dm-indeterminate { from { transform: translateX(-110%); } to { transform: translateX(320%); } }
.sv-l1-wait { display: flex; align-items: center; gap: 8px; margin: 8px 0 2px; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-hover); font-size: var(--fs-sm); color: var(--text-muted); line-height: 1.5; }
.sv-l1-wait .dm-icon { color: var(--accent); flex: none; }
.sv-wait-inline { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-sm); color: var(--text-muted); }

.dm-spin { animation: dm-spin 1s linear infinite; transform-origin: center; }
.dm-rot { transition: transform var(--motion-base) var(--ease-standard); transform: rotate(180deg); }
@keyframes dm-spin { to { transform: rotate(360deg); } }

@media (prefers-reduced-motion: reduce) {
  .dm-spin, .sv-progress span { animation: none; }
}

/* 产品化设置面板：暖灰导航 + 白色工作面 + 卡片化指标，减少后台表格感。 */
.sv-box { width: min(1040px, calc(100vw - 32px)); height: min(820px, calc(100vh - 48px)); }
.sv-head { padding: 18px 24px 14px; align-items: center; }
.sv-title-wrap { display: flex; flex-direction: column; gap: 3px; }
.sv-title { font-size: var(--fs-xl); font-weight: 600; letter-spacing: -.01em; }
.sv-sub { margin: 0; color: var(--text-muted); font-size: var(--fs-sm); }
.sv-x {
  width: 32px; height: 32px; display: grid; place-items: center;
  border: 1px solid transparent; border-radius: var(--r-sm); font-size: var(--fs-xl);
}
.sv-x:hover { background: var(--bg-hover); border-color: var(--border); }
.sv-nav { width: 200px; padding: 16px 12px; gap: 2px; background: var(--bg-hover); }
.sv-nav-item {
  padding: 10px 12px; border-radius: var(--r-sm); font-size: var(--fs-body);
  color: var(--text-muted);
}
.sv-nav-item:hover { background: var(--bg-hover); color: var(--text); }
.sv-nav-item.on {
  background: var(--surface); color: var(--accent-text); border-color: var(--border);
  box-shadow: 0 1px 2px rgba(17, 17, 22, .06);
}
.sv-content { padding: 24px 28px 28px; background: var(--surface); }
.sv-panel { gap: 8px; }
.sv-h4 { margin-bottom: 8px; font-size: var(--fs-lead); }
.sv-hint { color: var(--text-muted); font-size: var(--fs-md); line-height: 1.7; }
.sv-meter-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px; margin: 16px 0 12px; border: 0;
}
.sv-meter,
.sv-meter:nth-child(n) {
  padding: 16px; border: 1px solid var(--border);
  border-radius: var(--r-md); background: var(--bg);
}
.sv-meter span { color: var(--text-muted); }
.sv-meter b { margin-top: 6px; font-size: calc(var(--fs-h1) + 2px); font-weight: 600; letter-spacing: -.02em; }
.sv-provider-list {
  margin-top: 16px; border: 1px solid var(--border); border-radius: var(--r-md);
  overflow: hidden; background: var(--surface); overflow-x: auto;
}
.sv-provider-head { padding: 10px 14px; background: var(--bg); color: var(--text-muted); }
.sv-provider-row { padding: 11px 14px; border-bottom: 1px solid var(--border); }
.sv-provider-row:last-child { border-bottom: 0; }
.sv-provider-row:hover { background: var(--bg); }
.sv-label { margin-top: 16px; font-size: var(--fs-md); font-weight: 600; color: var(--text); }
.sv-input { min-height: 38px; border-radius: var(--r-sm); background: var(--surface); }
.sv-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.sv-input::placeholder { color: var(--text-muted); opacity: 1; }
.sv-btn { min-height: 36px; padding: 7px 16px; border-radius: var(--r-sm); }
.sv-primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.sv-mini { min-height: 30px; padding: 4px 10px; border-radius: var(--r-sm); }
.sv-form-grid { gap: 14px 16px; }

@media (max-width: 680px) {
  .sv-box { height: calc(100vh - 20px); max-width: calc(100vw - 20px); }
  .sv-nav { width: 128px; }
  .sv-meter-grid, .sv-form-grid { grid-template-columns: 1fr 1fr; }
  .sv-meter:nth-child(3n) { border-right: 1px solid var(--border); }
  .sv-meter:nth-child(2n) { border-right: 0; }
  .sv-meter:nth-child(n+3) { border-top: 1px solid var(--border); }
  .sv-provider-head, .sv-provider-row { grid-template-columns: minmax(70px, 1fr) 66px 76px 120px 64px; gap: 6px; }
}
</style>
