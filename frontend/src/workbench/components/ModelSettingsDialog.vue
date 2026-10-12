<script setup lang="ts">
// 模型设置弹窗：选择预设厂商或任意 OpenAI 兼容端点（base_url + Key + 模型名）。
// 保存后后端即时重建 Agent LLM 客户端；能力画像（思考/上下文窗口）回传父组件，
// 用于刷新输入栏的模型标签与「深度思考」开关状态。
import { ref, watch, computed, onMounted, onBeforeUnmount } from 'vue'
import { modelApi } from '../api'
import type { ModelConfigInfo, ContextLookupResult, ModelPreset } from '../api'
import { prefs as sharedPrefs, saveAskPrefs, type AskPrefs } from '../../ask/askPrefs'

const props = defineProps<{ visible: boolean; config: ModelConfigInfo | null }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'saved', info: ModelConfigInfo): void
}>()

const provider = ref('mock')
const model = ref('')
const apiKey = ref('')
const baseUrl = ref('')
const showKey = ref(false)
const saving = ref(false)
const errorMsg = ref('')
const warnings = ref<string[]>([])

// 上下文窗口：手填覆盖（空字符串=自动：Ollama 实时探测/内置画像）
const ctxWindow = ref('')
const ctxBusy = ref<'probe' | 'lookup' | ''>('')
const ctxMsg = ref('')
const ctxMsgOk = ref(false)
const lookupResult = ref<ContextLookupResult | null>(null)

// ---------------------------------------------------------------------------
// 知识库检索向量后端（可选功能）
//
// 为什么放在这里：此前 embedding_provider 在整个前端**只被读取、从不被设置**——
// 而 api.py 的报错文案却让用户「在⚙ 模型设置里把 embedding_provider 换成云端
// 服务」。那条建议指向一个不存在的控件，用户无处可去。后端（api.py 的
// ModelSaveReq.embedding_provider + set_embedding_provider + reset_collection）
// 早就完整支持，只有 UI 没接上。
//
// 措辞是刻意的：向量检索**只服务于上传文档**，代码检索走 grep/read_file，
// 与此无关。因此它被明确标成「可选」，不写成「必需」。
// ---------------------------------------------------------------------------
const embeddingProvider = ref<'ollama' | 'qwen' | 'local'>('ollama')
//: 与后端 embeddings.py EmbeddingClient 的分支一一对应。
//: 落不到这三者时后端会走 local 哈希词袋兜底（非语义），所以必须显式给出 local。
const EMBEDDING_OPTIONS = [
  {
    value: 'ollama',
    label: '本机 Ollama（需启动 Ollama 并拉取嵌入模型）',
    hint: '语义检索，效果最好。零 Key，但要求本机 Ollama 正在运行。',
  },
  {
    value: 'qwen',
    label: '云端嵌入（DashScope）',
    hint: '语义检索，效果最好，不占本机资源。',
  },
  {
    value: 'local',
    label: '不启用（本地哈希兜底）',
    hint: '零依赖、零配置。但只匹配字面词，不理解同义表达——知识库检索质量会明显下降。对话与代码检索不受影响。',
  },
] as const

const embeddingHint = computed(
  () => EMBEDDING_OPTIONS.find((o) => o.value === embeddingProvider.value)?.hint || '',
)
const embeddingNeedsOllama = computed(() => embeddingProvider.value === 'ollama')

// 云端嵌入会复用**对话模型的 API Key**（embeddings.py: api_key=get_runtime("llm_api_key")
// or os.getenv("DASHSCOPE_API_KEY")）。所以对话模型不是 qwen 时，上方那个 Key 是别家厂商的，
// 打 DashScope 会持续 401。必须在这里说破——否则这就是一个「选了才发现不能用」的控件，
// 跟之前那个指向不存在控件的报错文案是同一类问题。
const embeddingCloudKeyMismatch = computed(
  () => embeddingProvider.value === 'qwen' && provider.value !== 'qwen',
)

// AI 越界访问模式：safe=仅限项目内；high=允许受控越界读写（须配置白名单目录）
const accessMode = ref<'safe' | 'high'>('safe')
const thinkingCapability = ref('')
const visionCapability = ref('')
const videoCapability = ref('')

// 命名模型预设：保存多套云端/自定义配置，切换免重填参数与 Key
const presets = ref<ModelPreset[]>([])
const presetLabel = ref('')
const presetBusy = ref(false)
// 模型选择模式：fixed=固定用当前表单模型；auto=Harness 按任务复杂度自动选本地/云端
const llmMode = ref<'fixed' | 'auto'>('fixed')
const autoCloudPresetId = ref('')
const presetError = ref('')
// 弹窗由 v-if 按需创建，首次创建时 visible 已经是 true；回填期间禁止
// provider 监听把服务端保存的具体模型名改回厂商默认模型。
let hydratingConfig = false

const cloudPresets = computed<ModelPreset[]>(() =>
  presets.value.filter((p) => {
    const m = props.config?.provider_meta?.[p.provider]
    return m?.cloud || (p.provider === 'custom' && !!p.base_url)
  }),
)
const autoReady = computed(() => llmMode.value === 'fixed' || !!autoCloudPresetId.value)

const meta = computed(() => props.config?.provider_meta?.[provider.value])
const isCustom = computed(() => provider.value === 'custom')
const isOllama = computed(() => provider.value === 'ollama')
const ollamaModels = computed<string[]>(() =>
  isOllama.value ? (props.config?.ollama_status?.present_models ?? []) : [])
const modelPlaceholder = computed(() => meta.value?.default_model || '模型名称')
const ctxModelReady = computed(() => !!model.value.trim())

// 当前已保存模型的窗口来源标签
const sourceLabel = computed(() => {
  switch (props.config?.context_source) {
    case 'custom': return '窗口：手填'
    case 'probe': return '窗口：实时探测'
    default: return '窗口：内置画像'
  }
})

function fmtK(v: number): string {
  if (!v) return '0'
  if (v < 1024) return String(v)
  return v >= 1048576 ? `${(v / 1048576).toFixed(v % 1048576 ? 1 : 0)}M` : `${Math.round(v / 1024)}k`
}

const ctxWindowK = computed(() => {
  const raw = String(ctxWindow.value ?? '').trim()
  const n = Number(raw)
  return raw && n > 0 ? `≈ ${fmtK(n)}` : ''
})

// API responses from older backends may contain a number/object here; keep all
// validation paths stable even before the next dialog-open watcher runs.
function normalizedContextWindow(): string {
  const value = ctxWindow.value as unknown
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') {
    const candidate = (value as { value?: unknown; tokens?: unknown }).value
      ?? (value as { value?: unknown; tokens?: unknown }).tokens
    return candidate === null || candidate === undefined ? '' : String(candidate).trim()
  }
  return String(value).trim()
}

function hydrateConfig(config: ModelConfigInfo) {
    hydratingConfig = true
    errorMsg.value = ''
    warnings.value = []
    provider.value = config.llm_provider || 'mock'
    model.value = config.llm_model || ''
    apiKey.value = ''
    baseUrl.value = config.custom_base_url || ''
    ctxWindow.value = config.context_window_override ? String(config.context_window_override) : ''
    accessMode.value = config.external_access_mode || 'safe'
    thinkingCapability.value = config.capability?.thinking || 'unknown'
    visionCapability.value = config.capability?.vision || 'unknown'
    videoCapability.value = config.capability?.video || 'unknown'
    presets.value = [...(config.model_presets || [])]
    presetLabel.value = ''
    presetError.value = ''
    llmMode.value = config.llm_mode === 'auto' ? 'auto' : 'fixed'
    autoCloudPresetId.value = config.auto_cloud_preset_id || ''
    ctxBusy.value = ''
    ctxMsg.value = ''
    lookupResult.value = null
    // 收窄到受支持的三项。状态文件里若存了别的值（早期版本、手工编辑），
// 直接赋给<select> 会让下拉框显示空白且用户看不出当前是什么——那又是一个
// 「看起来配置了、实际没生效」。回落ollama 与后端默认值一致。
    embeddingProvider.value = (
      ['ollama', 'qwen', 'local'] as const
    ).includes(config.embedding_provider as 'ollama' | 'qwen' | 'local')
      ? (config.embedding_provider as 'ollama' | 'qwen' | 'local')
      : 'ollama'
    hydratingConfig = false
}

// v-if 首次挂载时 visible 已经为 true，所以必须 immediate；同时配置对象更新
// （例如从预设切换回来）时也重新回填，避免弹窗显示上一套模型。
watch(
  () => [props.visible, props.config] as const,
  ([visible, config]) => {
    if (visible && config) hydrateConfig(config)
  },
  { immediate: true },
)

// 切换厂商：模型名沿用该厂商默认（用户可改）；自定义端点清空 key 输入；
// 窗口覆盖按「厂商/模型」持久化，切厂商时重置为待填状态
watch(provider, (p) => {
  if (hydratingConfig || (p === props.config?.llm_provider && model.value === props.config?.llm_model)) return
  errorMsg.value = ''
  ctxMsg.value = ''
  lookupResult.value = null
  ctxWindow.value = ''
  thinkingCapability.value = 'unknown'
  visionCapability.value = 'unknown'
  videoCapability.value = 'unknown'
  const m = props.config?.provider_meta?.[p]
  if (p !== 'custom') model.value = m?.default_model || ''
})

function onMaskDown() {
  if (!saving.value) emit('close')
}

// Esc 关闭：组件只在父级 v-if 打开时挂载，所以监听随挂载/卸载同步进出，不会漏。
// 保存中不允许关窗（与点遮罩的行为一致），否则半成品配置会被误当取消吞掉。
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.visible && !saving.value) emit('close')
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

// 「展示偏好」：纯前端开关，不落后端 /api/config（那是 api.py 交叠热点）。
// 绑的是 askPrefs 那份共享 reactive（与设置页 / 问答页 / 对话台同一个对象），
// 这里不再自己 ref(loadAskPrefs()) 存一份 —— 同一状态两份内存副本，
// 一边改了另一边就是旧的（弹窗打开期间设置页改过就中招）。
const ap = sharedPrefs
function apSet(patch: Partial<AskPrefs>) {
  saveAskPrefs({ ...sharedPrefs, ...patch })
}

// Ollama /api/show 实时探测模型真实窗口：成功后仅填入输入框，保存才生效
async function probeContext() {
  const name = model.value.trim()
  if (!name || ctxBusy.value) return
  ctxBusy.value = 'probe'
  ctxMsg.value = ''
  try {
    const res = await modelApi.probeOllama(name)
    if (res.ok && res.context_window) {
      ctxWindow.value = String(res.context_window)
      ctxMsgOk.value = true
      ctxMsg.value = `实时探测成功：窗口 ${res.context_window.toLocaleString()} tokens（≈ ${fmtK(res.context_window)}），保存后生效。`
    } else {
      ctxMsgOk.value = false
      ctxMsg.value = res.error || '探测失败，可改用联网查询或手动填写。'
    }
  } catch (e) {
    ctxMsgOk.value = false
    ctxMsg.value = (e as { message?: string }).message || '探测失败，可改用联网查询或手动填写。'
  } finally {
    ctxBusy.value = ''
  }
}

// 联网搜索模型公开窗口：结果只展示候选与出处，用户确认后点「采用」填入
async function lookupContext() {
  const name = model.value.trim()
  if (!name || ctxBusy.value) return
  ctxBusy.value = 'lookup'
  ctxMsg.value = ''
  lookupResult.value = null
  try {
    lookupResult.value = await modelApi.lookupModelContext(provider.value, name)
  } catch (e) {
    ctxMsgOk.value = false
    ctxMsg.value = (e as { message?: string }).message || '联网查询失败。'
  } finally {
    ctxBusy.value = ''
  }
}

function useCandidate(tokens: number) {
  ctxWindow.value = String(tokens)
}

// 把当前表单（含可选 Key）另存为命名预设；不切换当前模型
async function saveAsPreset() {
  errorMsg.value = ''
  presetError.value = ''
  const label = presetLabel.value.trim()
  if (!label) { presetError.value = '请先填写预设名称。'; return }
  if (isCustom.value && (!baseUrl.value.trim() || !model.value.trim())) {
    presetError.value = '自定义端点需要先填好接口地址与模型名称。'
    return
  }
  let ctxVal = 0
  const rawCtx = normalizedContextWindow()
  if (rawCtx) {
    ctxVal = Number(rawCtx)
    if (!Number.isInteger(ctxVal) || ctxVal < 1024 || ctxVal > 2_097_152) {
      presetError.value = '上下文窗口需为 1024 ~ 2097152 tokens 的整数。'
      return
    }
  }
  presetBusy.value = true
  try {
    const res = await modelApi.savePreset({
      label,
      provider: provider.value,
      model: model.value.trim(),
      base_url: isCustom.value ? baseUrl.value.trim() : '',
      context_window: ctxVal,
      // Key 留空则不随预设保存（激活时可再补；项目里已存的同厂商 Key 也可兜底）
      api_key: apiKey.value.trim() || undefined,
    })
    if (res.ok === false || !res.presets) {
      presetError.value = res.error || '预设保存失败'
      return
    }
    presets.value = res.presets
    presetLabel.value = ''
  } catch (e) {
    presetError.value = (e as { message?: string }).message || '预设保存失败'
  } finally {
    presetBusy.value = false
  }
}

async function removePreset(p: ModelPreset) {
  if (!window.confirm(`确定删除预设「${p.label}」吗？（不影响当前正在使用的模型）`)) return
  presetError.value = ''
  try {
    const res = await modelApi.deletePreset(p.id)
    if (res.ok === false || !res.presets) {
      presetError.value = res.error || '删除失败'
      return
    }
    presets.value = res.presets
    if (autoCloudPresetId.value === p.id) autoCloudPresetId.value = ''
  } catch (e) {
    presetError.value = (e as { message?: string }).message || '删除失败'
  }
}

// 一键激活预设：后端走与「保存并切换」完全相同的探活/落盘/换客户端流程
async function activatePreset(p: ModelPreset) {
  errorMsg.value = ''
  presetError.value = ''
  saving.value = true
  try {
    const res = await modelApi.activatePreset(p.id)
    if (res.ok === false || res.error) {
      errorMsg.value = res.error || '切换失败'
      return
    }
    presets.value = res.model_presets || presets.value
    emit('saved', res)
    emit('close')
  } catch (e) {
    errorMsg.value = (e as { message?: string }).message || '切换失败'
  } finally {
    saving.value = false
  }
}

function presetProviderLabel(p: ModelPreset): string {
  return props.config?.provider_meta?.[p.provider]?.label || p.provider
}

async function save() {
  errorMsg.value = ''
  warnings.value = []
  if (!autoReady.value) {
    errorMsg.value = '自动模式需要先选择一个云端模型预设（可在上方把当前配置另存为预设）。'
    return
  }
  // 窗口范围前端先拦一道（与后端 1024~2097152 保持一致）
  let ctxVal = 0
  const rawCtx = normalizedContextWindow()
  if (rawCtx) {
    ctxVal = Number(rawCtx)
    if (!Number.isInteger(ctxVal) || ctxVal < 1024 || ctxVal > 2_097_152) {
      errorMsg.value = '上下文窗口需为 1024 ~ 2097152 tokens 的整数；不需要自定义时请清空输入框。'
      return
    }
  }
  saving.value = true
  try {
    const res = await modelApi.save({
      provider: provider.value,
      model: model.value.trim(),
      // 留空=不修改已保存的 Key，避免每次切换都要重输
      api_key: apiKey.value.trim(),
      base_url: isCustom.value ? baseUrl.value.trim() : '',
      // 空=0：清除覆盖回到自动（Ollama 实时探测/内置画像）
      context_window: ctxVal,
      thinking_capability: thinkingCapability.value,
      vision_capability: visionCapability.value,
      video_capability: videoCapability.value,
      // 越界访问模式：safe/high（与模型切换一并保存，幂等）
      external_access_mode: accessMode.value,
      // 固定 / 自动模式；自动时附带复杂任务所用云端预设
      llm_mode: llmMode.value,
      auto_cloud_preset_id: llmMode.value === 'auto' ? autoCloudPresetId.value : '',
      // 知识库检索后端（可选）。此前这个字段**从未被前端发送**——后端早就支持，
      // 但 UI 没有控件，导致报错文案让用户「去模型设置里改 embedding_provider」
      // 时无处可去。
      embedding_provider: embeddingProvider.value,
    })
    if (res.model_error) {
      // Ollama 探活失败：HTTP 200 + ok:false + model_error（切到 rawJson 后真正可达）
      errorMsg.value = `模型探活失败：${res.model_error}`
      return
    }
    if (res.ok === false) {
      // 其余业务失败（如自定义服务缺地址/模型名）读 error
      errorMsg.value = res.error || res.model_error || '保存失败'
      return
    }
    // 保存已生效；告警（如缺 Key）非致命，弹窗保持打开让用户看完再手动关
    warnings.value = res.warnings || []
    ctxWindow.value = res.context_window_override ? String(res.context_window_override) : ''
    accessMode.value = (res.external_access_mode as 'safe' | 'high') || accessMode.value
    llmMode.value = res.llm_mode === 'auto' ? 'auto' : 'fixed'
    autoCloudPresetId.value = res.auto_cloud_preset_id || ''
    presets.value = res.model_presets || presets.value
    emit('saved', res)
    if (!warnings.value.length) emit('close')
  } catch (e) {
    errorMsg.value = (e as { message?: string }).message || '保存失败'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <!-- ChatDock 有 transform 入场动画；弹窗必须脱离该 containing block，才能相对整个视口定位。 -->
  <Teleport to="body">
    <div v-if="visible" class="ms-mask wb-modal-backdrop" @mousedown.self="onMaskDown">
      <div class="ms-box wb-modal-shell" role="dialog" aria-modal="true">
      <h3 class="ms-title">
        <span class="ms-title-txt">模型设置</span>
        <button type="button" class="ms-x" title="关闭（Esc）" aria-label="关闭模型设置" @click="emit('close')">✕</button>
      </h3>

      <label class="ms-label">模型选择方式</label>
      <div class="ms-seg" role="radiogroup" aria-label="模型选择方式">
        <button type="button" class="ms-seg-opt" :class="{ 'ms-seg-on': llmMode === 'fixed' }"
                :disabled="saving" @click="llmMode = 'fixed'">
          <span class="ms-seg-name">固定模式</span>
          <span class="ms-seg-desc">始终使用下面选择的模型</span>
        </button>
        <button type="button" class="ms-seg-opt" :class="{ 'ms-seg-on': llmMode === 'auto' }"
                :disabled="saving" @click="llmMode = 'auto'">
          <span class="ms-seg-name">自动模式</span>
          <span class="ms-seg-desc">简单任务用本地模型，复杂任务自动升级云端</span>
        </button>
      </div>
      <div v-if="llmMode === 'auto'" class="ms-auto-row">
        <select v-model="autoCloudPresetId" class="ms-input" :disabled="saving">
          <option value="" disabled>选择复杂任务使用的云端模型预设…</option>
          <option v-for="p in cloudPresets" :key="p.id" :value="p.id">
            {{ p.label }}（{{ presetProviderLabel(p) }}）
          </option>
        </select>
        <p class="ms-hint" v-if="!cloudPresets.length">
          还没有云端预设：在下方填好云端厂商参数与名称，点「另存为预设」即可。
        </p>
      </div>

      <div class="ms-sep"></div>
      <label class="ms-label">
        已保存的模型预设
        <span class="ms-preset-count" v-if="presets.length">{{ presets.length }}</span>
      </label>
      <div v-if="presets.length" class="ms-preset-list">
        <div v-for="p in presets" :key="p.id" class="ms-preset-item">
          <div class="ms-preset-info">
            <span class="ms-preset-name">{{ p.label }}</span>
            <span class="ms-preset-meta">
              {{ presetProviderLabel(p) }} · {{ p.model || '默认模型' }}
              <span class="ms-preset-key" :class="{ 'ms-preset-key-off': !p.has_key }">
                {{ p.has_key ? 'Key 已存' : '未存 Key' }}
              </span>
              <span v-if="config?.llm_provider === p.provider && config?.llm_model === p.model"
                    class="ms-preset-current">使用中</span>
            </span>
          </div>
          <div class="ms-preset-actions">
            <button type="button" class="ms-mini" :disabled="saving" @click="activatePreset(p)">切换</button>
            <button type="button" class="ms-mini ms-mini-danger" :disabled="saving" @click="removePreset(p)">删除</button>
          </div>
        </div>
      </div>
      <p v-else class="ms-hint ms-preset-empty">还没有预设：填好下面的参数并命名，点「另存为预设」，之后切换云端模型无需重填。</p>
      <div class="ms-preset-save">
        <input
          v-model="presetLabel"
          class="ms-input ms-preset-name-input"
          placeholder="预设名称，如：通义千问-云端"
          maxlength="40"
          spellcheck="false"
          :disabled="presetBusy || saving"
          @keyup.enter="saveAsPreset"
        />
        <button type="button" class="ms-mini" :disabled="presetBusy || saving" @click="saveAsPreset">
          {{ presetBusy ? '保存中…' : '另存为预设' }}
        </button>
      </div>
      <p v-if="presetError" class="ms-hint ms-hint-err">{{ presetError }}</p>

      <div class="ms-sep"></div>
      <label class="ms-label">当前模型（固定模式 / 自动模式的本地模型）</label>
      <select v-model="provider" class="ms-input" :disabled="saving">
        <option v-for="p in props.config?.providers || []" :key="p" :value="p">
          {{ props.config?.provider_meta?.[p]?.label || p }}
        </option>
      </select>

      <template v-if="isCustom">
        <label class="ms-label">接口地址（OpenAI 兼容，到 /v1 一级）</label>
        <input
          v-model="baseUrl"
          class="ms-input"
          placeholder="https://your-host/v1"
          spellcheck="false"
          :disabled="saving"
        />
      </template>
      <div v-else-if="meta?.base_url" class="ms-endpoint" :title="meta.base_url">{{ meta.base_url }}</div>

      <label class="ms-label">模型名称</label>
      <input
        v-model="model"
        class="ms-input"
        :placeholder="modelPlaceholder"
        list="ms-ollama-models"
        spellcheck="false"
        :disabled="saving"
      />
      <datalist id="ms-ollama-models">
        <option v-for="m in ollamaModels" :key="m" :value="m" />
      </datalist>
      <div v-if="isOllama && props.config?.ollama_status && !props.config.ollama_status.reachable"
           class="ms-hint ms-hint-err">
        未检测到本机 Ollama 服务，保存前请先启动它（模型切换时会做一次真实探活）。
      </div>

      <label class="ms-label">上下文窗口（tokens）</label>
      <div class="ms-ctx-row">
        <input
          v-model="ctxWindow"
          class="ms-input ms-ctx-input"
          type="number" min="1024" max="2097152" step="1024"
          placeholder="留空=自动（Ollama 实时探测 / 内置画像）"
          spellcheck="false"
          :disabled="saving"
        />
        <span v-if="ctxWindowK" class="ms-ctx-k">{{ ctxWindowK }}</span>
      </div>
      <div class="ms-ctx-btns">
        <button v-if="isOllama" type="button" class="ms-mini"
                :disabled="!!ctxBusy || saving || !ctxModelReady"
                title="调用本机 Ollama /api/show 读取该模型的真实 context_length"
                @click="probeContext">
          {{ ctxBusy === 'probe' ? '探测中…' : '实时探测' }}
        </button>
        <button type="button" class="ms-mini"
                :disabled="!!ctxBusy || saving || !ctxModelReady"
                title="联网搜索该模型公开的上下文窗口，结果仅供参考，确认后再采用"
                @click="lookupContext">
          {{ ctxBusy === 'lookup' ? '查询中…' : '联网查询' }}
        </button>
        <span class="ms-ctx-note">未知模型可手动填写；手填值优先于探测与画像</span>
      </div>
      <p v-if="ctxMsg" class="ms-hint" :class="{ 'ms-hint-err': !ctxMsgOk }">{{ ctxMsg }}</p>
      <div v-if="lookupResult?.candidates?.length" class="ms-lookup">
        <p class="ms-lookup-title">联网识别到的候选窗口（点击数字采用，出处仅供参考）：</p>
        <div v-for="(c, i) in lookupResult.candidates" :key="c.tokens + '-' + i" class="ms-lookup-item">
          <button type="button" class="ms-lookup-num"
                  :class="{ 'ms-lookup-best': c.tokens === lookupResult.best }"
                  :disabled="saving" @click="useCandidate(c.tokens)">
            {{ c.tokens.toLocaleString() }} <span class="ms-lookup-k">≈{{ Math.round(c.tokens / 1024) }}k</span>
          </button>
          <span class="ms-lookup-ev" :title="c.evidence">{{ c.evidence }}</span>
        </div>
      </div>
      <p v-else-if="lookupResult && !lookupResult.ok" class="ms-hint ms-hint-err">{{ lookupResult.error }}</p>

      <div class="ms-sep"></div>

      <!--
        知识库检索（可选）。措辞刻意强调「可选」与「只影响上传文档」：
        代码检索走 grep / read_file，本来就不经过向量。把这一点写在控件旁边，
        是为了不让「没配向量模型」看起来像整套环境不完整。
      -->
      <label class="ms-label">
        知识库检索后端（可选 · 仅用于上传文档，不影响问答与代码检索）
      </label>
      <select v-model="embeddingProvider" class="ms-input" :disabled="saving">
        <option v-for="o in EMBEDDING_OPTIONS" :key="o.value" :value="o.value">
          {{ o.label }}
        </option>
      </select>
      <p class="ms-hint">{{ embeddingHint }}</p>
      <p v-if="embeddingCloudKeyMismatch" class="ms-hint ms-hint-err">
        当前对话模型是 {{ provider }}，它复用的是别家厂商的 Key，打DashScope 会认证失败。
        请先设置环境变量 DASHSCOPE_API_KEY 并重启服务，再使用此项。
      </p>
      <p v-if="embeddingNeedsOllama && props.config?.ollama_status && !props.config.ollama_status.reachable"
         class="ms-hint">
        当前未检测到本机 Ollama 服务。留在此项也能正常问答——只是知识库检索不可用，
        提问时会明确提示「本次不使用检索结果」。要真正启用检索，请启动 Ollama
        并拉取嵌入模型。
      </p>
      <p class="ms-hint">
        切换此项会清空已上传文档的向量并需要重新上传——因为不同后端的向量维度不通用。
      </p>

      <div class="ms-sep"></div>
      <label class="ms-label">能力确认</label>
      <div class="ms-cap-edit">
        <label>深度思考
          <select v-model="thinkingCapability" class="ms-select" :disabled="saving">
            <option value="unknown">未知 / 待确认</option>
            <option value="none">不支持</option>
            <option value="toggle">可开关</option>
            <option value="native">模型内置</option>
          </select>
        </label>
        <label>图片识别
          <select v-model="visionCapability" class="ms-select" :disabled="saving">
            <option value="unknown">未知 / 交给 Harness</option>
            <option value="none">不支持</option>
            <option value="harness">使用 Harness 视觉模型</option>
            <option value="native">模型原生支持</option>
          </select>
        </label>
        <label>视频识别
          <select v-model="videoCapability" class="ms-select" :disabled="saving">
            <option value="unknown">未知 / 待确认</option>
            <option value="none">不支持</option>
            <option value="frames">Harness 抽帧分析</option>
            <option value="native">模型原生支持</option>
          </select>
        </label>
      </div>
      <p class="ms-hint ms-cap-help">“未知 / 交给 Harness”不会把图片直接发送给当前模型；需配置 DOCMIND_VISION_MODEL 才会自动生成图片观察。</p>

      <div class="ms-sep"></div>
      <label class="ms-label">AI 越界访问权限</label>
      <div class="ms-seg" role="radiogroup" aria-label="越界访问模式">
        <button type="button" class="ms-seg-opt" :class="{ 'ms-seg-on': accessMode === 'safe' }"
                :disabled="saving" @click="accessMode = 'safe'">
          <span class="ms-seg-name">安全模式</span>
          <span class="ms-seg-desc">仅限当前项目内读写</span>
        </button>
        <button type="button" class="ms-seg-opt" :class="{ 'ms-seg-on ms-seg-high': accessMode === 'high' }"
                :disabled="saving" @click="accessMode = 'high'">
          <span class="ms-seg-name">高权限模式</span>
          <span class="ms-seg-desc">可越界读写其他项目</span>
        </button>
      </div>
      <p class="ms-hint ms-acm-note">
        高权限模式允许 AI 对 <code>DOCMIND_EXTERNAL_DIRS</code> 白名单内的其他项目进行增删改查；
        未配置该环境变量时保存会被拒绝。安全模式始终禁止任何项目外访问。
      </p>

      <template v-if="meta?.needs_key || isCustom">
        <label class="ms-label">
          API Key
          <span class="ms-key-toggle" @click="showKey = !showKey">{{ showKey ? '隐藏' : '显示' }}</span>
        </label>
        <input
          v-model="apiKey"
          class="ms-input"
          :type="showKey ? 'text' : 'password'"
          :placeholder="props.config?.has_key ? '已保存，留空表示不修改' : 'sk-...'"
          spellcheck="false"
          autocomplete="off"
          :disabled="saving"
        />
      </template>

      <p v-if="errorMsg" class="ms-hint ms-hint-err">{{ errorMsg }}</p>
      <p v-for="(w, i) in warnings" :key="i" class="ms-hint">{{ w }}</p>

      <div class="ms-cap" title="以下为当前已保存模型的能力，保存切换后自动刷新">
        <span class="ms-cap-cap-title">当前模型能力</span>
        <span class="ms-cap-tag">上下文 {{ Math.round((props.config?.capability?.context_window || 0) / 1024) }}k</span>
        <span class="ms-cap-tag">{{ sourceLabel }}</span>
        <span class="ms-cap-tag" v-if="props.config?.capability?.thinking === 'native'">深度思考·模型内置</span>
        <span class="ms-cap-tag" v-else-if="props.config?.capability?.thinking === 'toggle'">深度思考·可开关</span>
        <span class="ms-cap-tag ms-cap-off" v-else-if="props.config?.capability?.thinking === 'unknown'">深度思考·待确认</span>
        <span class="ms-cap-tag ms-cap-off" v-else>不支持深度思考</span>
        <span class="ms-cap-tag" v-if="props.config?.capability?.vision === 'native'">图片·模型原生</span>
        <span class="ms-cap-tag" v-else-if="props.config?.capability?.vision === 'harness'">图片·Harness 辅助</span>
        <span class="ms-cap-tag ms-cap-off" v-else>图片·待确认</span>
        <span class="ms-cap-tag" v-if="props.config?.capability?.video === 'native'">视频·模型原生</span>
        <span class="ms-cap-tag" v-else-if="props.config?.capability?.video === 'frames'">视频·Harness 抽帧</span>
        <span class="ms-cap-tag ms-cap-off" v-else>视频·待确认</span>
        <span class="ms-cap-tag" v-if="props.config?.capability?.cloud">云端</span>
        <span class="ms-cap-tag ms-cap-off" v-else>本地</span>
      </div>
      <p class="ms-tip">切换模型会清空当前对话上下文；Key 使用系统凭据加密存储，仅保存在本机项目目录。</p>

      <div class="ms-sep"></div>
      <label class="ms-label">问答偏好<span class="ms-label-note">纯前端偏好，点开关即生效，不需要点「保存并切换」</span></label>
      <div class="ms-prefs">
        <button type="button" class="ms-check" :class="{ 'ms-check-on': ap.defaultThinking }"
                :aria-pressed="ap.defaultThinking" @click="apSet({ defaultThinking: !ap.defaultThinking })">
          <span class="ms-box-tick" aria-hidden="true">✓</span><span>深度思考默认开启</span>
        </button>
        <button type="button" class="ms-check" :class="{ 'ms-check-on': ap.defaultWeb }"
                :aria-pressed="ap.defaultWeb" @click="apSet({ defaultWeb: !ap.defaultWeb })">
          <span class="ms-box-tick" aria-hidden="true">✓</span><span>联网搜索默认开启</span>
        </button>
        <button type="button" class="ms-check" :class="{ 'ms-check-on': ap.showTrace }"
                :aria-pressed="ap.showTrace" @click="apSet({ showTrace: !ap.showTrace })">
          <span class="ms-box-tick" aria-hidden="true">✓</span><span>显示推理轨迹步骤</span>
        </button>
        <button type="button" class="ms-check" :class="{ 'ms-check-on': ap.reasonOpen }"
                :aria-pressed="ap.reasonOpen" @click="apSet({ reasonOpen: !ap.reasonOpen })">
          <span class="ms-box-tick" aria-hidden="true">✓</span><span>深度思考默认展开</span>
        </button>
        <label class="ms-pref-row">
          <span>答案下方文件引用最多显示</span>
          <select class="ms-input ms-pref-select"
                  :value="ap.maxRefs"
                  @change="apSet({ maxRefs: Number(($event.target as HTMLSelectElement).value) })">
            <option value="0">不限制</option>
            <option value="3">3 条</option>
            <option value="6">6 条</option>
            <option value="12">12 条</option>
          </select>
        </label>
      </div>

      <div class="ms-actions">
        <button class="ms-btn" :disabled="saving" @click="emit('close')">{{ warnings.length ? '知道了' : '取消' }}</button>
        <button v-if="!warnings.length" class="ms-btn ms-primary"
                :disabled="saving || !autoReady || (isCustom && (!baseUrl.trim() || !model.trim()))"
                @click="save">
          {{ saving ? '保存中…' : '保存并切换' }}
        </button>
      </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.ms-mask {
  position: fixed; inset: 0; z-index: 2000; box-sizing: border-box;
  background: var(--scrim);
  display: flex; align-items: center; justify-content: center;
  overflow: auto; overscroll-behavior: contain;
  backdrop-filter: blur(2px); animation: ms-fade .18s ease-out both;
}
.ms-box {
  width: 460px; max-width: min(460px, calc(100vw - 24px));
  max-height: min(88vh, calc(100vh - 24px));
  box-sizing: border-box;
  background: var(--bg, #fff);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 12px 40px rgba(15, 23, 42, .18);
  padding: 18px 20px 16px;
  display: flex; flex-direction: column; gap: 6px; animation: ms-rise .2s cubic-bezier(.2,.8,.2,1) both; scrollbar-gutter: stable;
}
.ms-title { margin: 0 0 6px; font-size: 15px; font-weight: 600; display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.ms-x {
  flex: 0 0 auto; width: 26px; height: 26px; padding: 0; line-height: 1;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 13px; cursor: pointer;
  background: var(--bg-raised); color: var(--ink);
  border: 1px solid var(--border-strong); border-radius: 0;
}
.ms-x:hover { color: var(--accent); border-color: var(--accent); }

/* 问答偏好：纯展示开关（不落后端），沿用粗野风方角 + 响亮色勾选块 */
.ms-prefs { display: flex; flex-direction: column; gap: 8px; }
.ms-check {
  display: flex; align-items: center; gap: 8px; padding: 7px 10px;
  font-family: inherit; font-size: 13px; color: var(--text); text-align: left;
  background: var(--bg-raised); border: 1px solid var(--border); border-radius: 0; cursor: pointer;
}
.ms-check:hover { border-color: var(--border-strong); }
.ms-check-on { border-color: var(--accent); color: var(--accent-text); }
.ms-check .ms-box-tick {
  flex: 0 0 16px; width: 16px; height: 16px; padding: 0;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--border-strong); background: var(--bg);
  font-size: 11px; line-height: 1; color: transparent;
}
.ms-check-on .ms-box-tick { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
.ms-pref-row { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-muted); }
.ms-pref-select { width: 108px; }
.ms-label-note { color: var(--text-faint); font-size: 11px; font-weight: 400; }
.ms-label {
  font-size: 12px; color: var(--text-muted); margin-top: 8px;
  display: flex; justify-content: space-between; align-items: center;
}
.ms-key-toggle { color: var(--accent); cursor: pointer; user-select: none; }
.ms-key-toggle:hover { text-decoration: underline; }
.ms-input {
  width: 100%; box-sizing: border-box;
  padding: 7px 10px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--bg-input, #fff); color: var(--text);
  outline: none;
}
.ms-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(47,111,237,.1); }
.ms-endpoint {
  font-size: 11px; color: var(--text-faint);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  padding: 2px 2px 0;
}
.ms-hint { font-size: 12px; color: #b45309; margin: 6px 0 0; }
.ms-hint-err { color: #dc2626; }
/* 上下文窗口手填 + 探测/联网查询 */
.ms-ctx-row { display: flex; align-items: center; gap: 8px; }
.ms-ctx-input { flex: 1; }
.ms-ctx-k {
  font-size: 11px; color: var(--text-faint); white-space: nowrap;
  min-width: 42px; text-align: right;
}
.ms-ctx-btns { display: flex; align-items: center; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
.ms-mini {
  font-size: 12px; padding: 4px 12px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text);
  cursor: pointer; transition: color .15s ease, border-color .15s ease, background .15s ease, transform .15s ease, box-shadow .15s ease;
}
.ms-mini:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); background: var(--bg-selected); transform: translateY(-1px); box-shadow: 0 3px 8px rgba(35,52,84,.08); }
.ms-mini:disabled { opacity: .5; cursor: default; }
.ms-ctx-note { font-size: 11px; color: var(--text-faint); }
.ms-lookup {
  margin-top: 6px; border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 10px; display: flex; flex-direction: column; gap: 6px;
  background: var(--bg-selected);
}
.ms-lookup-title { margin: 0; font-size: 11px; color: var(--text-muted); }
.ms-lookup-item { display: flex; align-items: baseline; gap: 8px; }
.ms-lookup-num {
  flex: none; font-size: 12px; cursor: pointer;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text);
  padding: 3px 10px;
}
.ms-lookup-num:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.ms-lookup-num:disabled { opacity: .5; cursor: default; }
.ms-lookup-best { border-color: var(--accent); color: var(--accent); font-weight: 600; }
.ms-lookup-k { font-weight: 400; opacity: .7; }
.ms-lookup-ev {
  font-size: 11px; color: var(--text-faint); line-height: 1.4;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0;
}
.ms-cap { display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; align-items: center; }
.ms-cap-edit { display: grid; grid-template-columns: 1fr; gap: 6px; }
.ms-cap-edit label { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 12px; color: var(--text-muted); }
.ms-select { min-width: 190px; padding: 5px 7px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-input, #fff); color: var(--text); font-size: 12px; }
.ms-cap-help { margin-top: 2px; line-height: 1.45; }
.ms-cap-cap-title { font-size: 11px; color: var(--text-faint); margin-right: 2px; }
.ms-cap-tag {
  font-size: 11px; color: var(--text-muted);
  border: 1px solid var(--border); border-radius: 999px;
  padding: 2px 9px; background: var(--bg-selected); transition: border-color .15s ease, transform .15s ease;
}
.ms-cap-tag:hover { border-color: var(--accent); transform: translateY(-1px); }
.ms-cap-off { opacity: .65; }
.ms-tip { font-size: 11px; color: var(--text-faint); margin: 8px 0 0; line-height: 1.5; }
/* 越界访问模式分段选择 */
.ms-sep { height: 1px; background: var(--border); margin: 12px 0 2px; }
.ms-seg { display: flex; gap: 8px; }
.ms-seg-opt {
  flex: 1; display: flex; flex-direction: column; gap: 2px;
  padding: 9px 12px; border: 1px solid var(--border); border-radius: 9px;
  background: var(--bg); color: var(--text); cursor: pointer; text-align: left; transition: border-color .15s ease, background .15s ease, transform .15s ease, box-shadow .15s ease;
}
.ms-seg-opt:hover:not(:disabled) { border-color: var(--accent); transform: translateY(-1px); box-shadow: 0 3px 9px rgba(35,52,84,.07); }.ms-seg-opt:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.ms-seg-on { border-color: var(--accent); background: var(--bg-selected); }
.ms-seg-high.ms-seg-on { border-color: #d97706; background: #fff7ed; }
.ms-seg-name { font-size: 13px; font-weight: 600; }
.ms-seg-desc { font-size: 11px; color: var(--text-faint); }
.ms-acm-note { color: var(--text-faint); margin-top: 6px; line-height: 1.5; }
.ms-acm-note code { background: var(--bg-selected); padding: 0 4px; border-radius: 4px; font-size: 11px; }
/* 自动模式云端预设选择 */
.ms-auto-row { margin-top: 6px; display: flex; flex-direction: column; gap: 4px; }
/* 命名模型预设列表 / 另存行 */
.ms-preset-count {
  display: inline-block; min-width: 18px; text-align: center;
  background: var(--bg-selected); border-radius: 999px;
  font-size: 11px; color: var(--text-muted); padding: 0 6px;
}
.ms-preset-list { display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
.ms-preset-item {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  border: 1px solid var(--border); border-radius: 8px; padding: 7px 10px;
  background: var(--bg); transition: border-color .15s ease, background .15s ease, transform .15s ease;
}
.ms-preset-item:hover { border-color: var(--border-strong); background: var(--bg-selected); transform: translateX(2px); }
.ms-preset-info { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.ms-preset-name { font-size: 13px; font-weight: 600; }
.ms-preset-meta { font-size: 11px; color: var(--text-faint); display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.ms-preset-key {
  border: 1px solid var(--border); border-radius: 999px; padding: 0 7px;
  color: #047857; background: #ecfdf5;
}
.ms-preset-key-off { color: #b45309; background: #fffbeb; }
.ms-preset-current { color: var(--accent); }
.ms-preset-actions { display: flex; gap: 6px; flex: none; }
.ms-mini-danger:hover:not(:disabled) { border-color: #dc2626; color: #dc2626; }
.ms-preset-empty { margin-top: 4px; }
.ms-preset-save { display: flex; gap: 8px; margin-top: 8px; }
.ms-preset-name-input { flex: 1; }
.ms-box { overflow-y: auto; overscroll-behavior: contain; }
.ms-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.ms-btn {
  padding: 7px 16px; font-size: 13px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text);
  cursor: pointer; transition: color .15s ease, border-color .15s ease, background .15s ease, transform .15s ease, box-shadow .15s ease;
}
.ms-btn:hover:not(:disabled) { border-color: var(--accent); transform: translateY(-1px); box-shadow: 0 3px 9px rgba(35,52,84,.08); }.ms-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.ms-primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.ms-primary:disabled { opacity: .5; cursor: default; }
@keyframes ms-fade { from { opacity: 0; } to { opacity: 1; } }
@keyframes ms-rise { from { opacity: 0; transform: translateY(7px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
@media (max-width: 560px) { .ms-mask { align-items: flex-end; }.ms-box { width: 100%; max-width: none; max-height: calc(100vh - 12px); border-radius: 12px 12px 0 0; padding: 15px 14px 13px; }.ms-seg { flex-direction: column; }.ms-cap-edit label { align-items: stretch; flex-direction: column; gap: 4px; }.ms-select { min-width: 0; width: 100%; }.ms-actions { position: sticky; bottom: -13px; padding-top: 10px; padding-bottom: 2px; background: linear-gradient(180deg, transparent, var(--bg) 26%); } }
@media (prefers-reduced-motion: reduce) { .ms-mask, .ms-box, .ms-mini, .ms-cap-tag, .ms-seg-opt, .ms-preset-item, .ms-btn { animation: none; transition: none; } }
</style>
