// 模型配置与联网/思考开关：工作台对话台与问答首页共用。
// - modelConfig 拉取/保存后的本地状态；
// - webOn / thinkingLevel 持久化在本机 localStorage（两入口共享同一份偏好）；
// - 思考能力三态：native（恒开不可点）/ toggle（用户可调档）/ none（不支持）；
//   toggle 家族按后端下发的 thinking_levels 在 off/low/medium/high 间循环。
import { computed, ref, watch } from 'vue'
import { modelApi } from '../api'
import type { ModelConfigInfo } from '../api'
import { demoMode } from './demo'

export interface UseModelConfigOptions {
  /** 模型设置保存成功后的附加副作用（如清空多轮消息、刷新驻留状态） */
  onSaved?: (info: ModelConfigInfo) => void
}

export function useModelConfig(options: UseModelConfigOptions = {}) {
  const modelConfig = ref<ModelConfigInfo | null>(null)
  const settingsOpen = ref(false)
  // 联网默认关（代码问答不外联），选择持久化在本机
  const webOn = ref(window.localStorage.getItem('docmind.chatWeb') === '1')
  watch(webOn, (v) => window.localStorage.setItem('docmind.chatWeb', v ? '1' : '0'))
  // 深度思考等级：off/low/medium/high（native 模型走单态 'native'，none 无控件）。
  // 这是思考状态的唯一来源；旧的布尔偏好首次读时迁移过来。
  const storedLevel = window.localStorage.getItem('docmind.chatThinkingLevel')
  const legacyBool = window.localStorage.getItem('docmind.chatThinking')
  const initialLevel = storedLevel
    ?? (legacyBool === '1' ? 'medium' : legacyBool === '0' ? 'off' : 'off')
  const thinkingLevel = ref<string>(initialLevel)
  // 落盘新 key（唯一真源）+ 同步镜像旧布尔 key 的「开/关」意图，
  // 避免 askPrefs 那个「默认深度思考」布尔与本等级各存各的、打架。
  watch(thinkingLevel, (v) => {
    try {
      window.localStorage.setItem('docmind.chatThinkingLevel', v)
      window.localStorage.setItem('docmind.chatThinking', v === 'off' ? '0' : '1')
    } catch { /* 隐私模式存不下不影响当前会话 */ }
  })

  const modelLabel = computed(() => {
    if (demoMode.value) return '离线演示模型'
    const c = modelConfig.value
    if (!c) return '模型加载中…'
    const name = c.provider_meta?.[c.llm_provider]?.label || c.llm_provider
    return `${name} · ${c.llm_model || '默认模型'}`
  })
  const thinkingMode = computed(() => modelConfig.value?.capability?.thinking || 'none')
  const thinkingSupported = computed(() => thinkingMode.value === 'native' || thinkingMode.value === 'toggle')
  // native 思考模型（reasoner 类）开关恒开且不可点；toggle 家族才允许用户切
  const thinkingNative = computed(() => thinkingMode.value === 'native')
  // 可选档位：优先用后端下发的 thinking_levels；缺字段时按三态画像兜底
  // （native→单态不可关，toggle→等价旧开/关两态，none→空数组=不显示控件）。
  const thinkingLevels = computed<string[]>(() => {
    const lv = modelConfig.value?.capability?.thinking_levels
    if (Array.isArray(lv) && lv.length) return lv
    if (thinkingNative.value) return ['native']
    if (thinkingMode.value === 'toggle') return ['off', 'medium']
    return []
  })
  // 是否可在多档间循环（≥2 档且非 native）；binary 模型也满足（off↔medium）
  const thinkingAdjustable = computed(() => thinkingLevels.value.length >= 2 && !thinkingNative.value)
  // 布尔兼容层：AskApp 等旧消费方仍看 thinkingOn；等级 'off' 即 false
  const thinkingOn = computed(() => thinkingLevel.value !== 'off')
  const thinkingEffective = computed(() =>
    thinkingNative.value || (thinkingSupported.value && thinkingLevel.value !== 'off'))
  const visionMode = computed(() => modelConfig.value?.capability?.vision || 'unknown')
  const visionSupported = computed(() => visionMode.value !== 'none')
  const visionLabel = computed(() => {
    if (visionMode.value === 'native') return '当前模型直接识图'
    if (visionMode.value === 'harness') return 'Harness 视觉辅助'
    if (visionMode.value === 'none') return '当前模型不支持识图'
    return '识图能力待确认，可由 Harness 辅助'
  })

  function setThinkingLevel(v: string) {
    thinkingLevel.value = v
  }
  // 芯片点击：在可选档位里循环 off→low→medium→high→off（binary 模型即 off↔medium）。
  // native 不可点（外部靠 thinkingNative 禁用），none 无控件。
  function cycleThinking() {
    if (thinkingNative.value) return
    const allowed = thinkingLevels.value
    if (allowed.length < 2) return
    const idx = allowed.indexOf(thinkingLevel.value)
    thinkingLevel.value = allowed[((idx < 0 ? -1 : idx) + 1) % allowed.length]
  }
  // 布尔兼容开关：AskApp 等旧 UI 仍用「开/关」语义；开时取一个代表档（优先 medium）。
  function toggleThinking() {
    if (thinkingNative.value || !thinkingSupported.value) return
    if (thinkingLevel.value === 'off') {
      const allowed = thinkingLevels.value
      thinkingLevel.value = allowed.includes('medium')
        ? 'medium'
        : (allowed.find((x) => x !== 'off') || 'medium')
    } else {
      thinkingLevel.value = 'off'
    }
  }
  // 切模型后可选档位会变：把当前等级收敛进允许集合，保留「是否想过」的意图。
  watch(thinkingLevels, (allowed) => {
    if (!allowed.length || allowed[0] === 'native') return
    if (!allowed.includes(thinkingLevel.value)) {
      const wasOn = thinkingLevel.value !== 'off'
      thinkingLevel.value = wasOn
        ? (allowed.includes('medium') ? 'medium' : (allowed.find((x) => x !== 'off') || allowed[allowed.length - 1]))
        : 'off'
    }
  })

  async function loadModelConfig() {
    if (demoMode.value) return
    try {
      modelConfig.value = await modelApi.get()
    } catch {
      /* 配置加载失败不阻塞聊天，芯片显示兜底文案 */
    }
  }
  function openSettings() {
    if (demoMode.value) return
    void loadModelConfig().then(() => { settingsOpen.value = true })
  }
  function onModelSaved(info: ModelConfigInfo) {
    modelConfig.value = info
    options.onSaved?.(info)
    // 是否关闭弹窗由弹窗自身决定（有告警时停留，让用户看到告警内容）
  }

  return {
    modelConfig, settingsOpen, webOn,
    thinkingLevel, thinkingLevels, thinkingAdjustable, thinkingOn,
    modelLabel, thinkingMode, thinkingSupported, thinkingNative, thinkingEffective,
    visionMode, visionSupported, visionLabel,
    cycleThinking, toggleThinking, setThinkingLevel,
    loadModelConfig, openSettings, onModelSaved,
  }
}
