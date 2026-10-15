/**
 * M2·③b 接线层（**只做接线，不假装挂载**）。
 *
 * 把后端"实时模式事实"接成一个响应式展示视图，供未来的实时座舱一行吃下：
 *   const mode = useRealtimeMode()
 *   <RealtimeModeBadge :payload="mode.raw.value" />
 *
 * 为什么现在只到组合式函数为止、不塞进任何现有面板（如实交代，别糊弄）：
 * 当前 build 里**没有任何用到实时流式通道的已挂载 surface**——v2 删了 AutonomousCockpit，
 * scope="cockpit" 的 ChatDock 只活在注释里没重建；SceneRuntimePanel 走的是"截图→VLM"的
 * 一次性链路，跟"原生实时 vs 兼容抽帧"这条流式通道不是一回事。硬把徽章挂到那些面板上会
 * **误导**（用户以为那个面板在用流式通道）。降级信号的正确落点，等座舱 WS 客户端重建那天，
 * 由它把 `hello.ok` 喂进同一个 `describeRealtimeMode` 即可——本组合式的取数换成 WS 缓存、
 * 显示逻辑一行都不用改。
 *
 * 数据源此刻用 REST `/api/vision/realtime-status`（连接前的配置事实，R13 的权威来源之一），
 * 好处是：真实可达、无需重建整条 WS 生命周期就能把接线与兜底跑通、可被单测驱动。
 */
import { computed, onMounted, onUnmounted, readonly, ref, type ComputedRef, type Ref } from 'vue'
import { visionApi } from '../api'
import { describeRealtimeMode, type RealtimeModeView } from '../realtimeModeDisplay'
import { createRealtimeModeTracker } from '../realtimeModeTracker'

export interface UseRealtimeMode {
  /** 归一后的展示视图（label/tone/isDegraded/hint/capabilities），响应 raw 变化。 */
  view: ComputedRef<RealtimeModeView>
  /** 最近一次有效服务端事实；null=未知态。直接绑给 RealtimeModeBadge 的 payload。 */
  raw: Ref<Record<string, unknown> | null>
  busy: Ref<boolean>
  lastError: Ref<string>
  loaded: Ref<boolean>
  /** 主动重取一次（挂载时自动调；座舱重建后可在 reconnect 时再调）。 */
  refresh(): Promise<void>
}

export function useRealtimeMode(): UseRealtimeMode {
  // 注入 REST 来源；未来接 WS 时只换这一行：`() => 从 ws hello.ok 缓存取`。
  const tracker = createRealtimeModeTracker(() => visionApi.realtimeStatus())

  const raw = ref<Record<string, unknown> | null>(null)
  const busy = ref(false)
  const lastError = ref('')
  const loaded = ref(false)

  const sync = () => {
    const s = tracker.snapshot()
    raw.value = s.raw
    busy.value = s.busy
    lastError.value = s.lastError
    loaded.value = s.loaded
  }

  // 纯映射只在 raw 变化时重算；tracker 负责"何时把 raw 置有效/置 null"。
  const view = computed(() => describeRealtimeMode(raw.value))

  async function refresh() {
    await tracker.refresh()
    sync()
  }

  // 换项目 = 后端解析结果可能变（不同 provider 配置），归零重取，避免旧项目模式残留。
  function onProjectChanged() {
    tracker.reset()
    sync()
    void refresh()
  }

  onMounted(() => {
    window.addEventListener('docmind:project-changed', onProjectChanged)
    void refresh()
  })
  onUnmounted(() => {
    window.removeEventListener('docmind:project-changed', onProjectChanged)
  })

  return {
    view: readonly(view) as ComputedRef<RealtimeModeView>,
    raw: readonly(raw) as Ref<Record<string, unknown> | null>,
    busy: readonly(busy) as Ref<boolean>,
    lastError: readonly(lastError) as Ref<string>,
    loaded: readonly(loaded) as Ref<boolean>,
    refresh,
  }
}
