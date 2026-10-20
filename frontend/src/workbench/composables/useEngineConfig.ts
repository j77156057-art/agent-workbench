/**
 * 项目当前使用的游戏引擎（取自项目配置，不是写死的）。
 *
 * 为什么要它：引擎此前在前端被**硬编码成 godot**（`SceneRuntimePanel` 的
 * `start('godot')`、`TaskEnginePanel` 的 `start()`/`verify()` 默认值），
 * 而后端早就有 `/api/engine/config`（把引擎与可执行文件写进项目状态）。
 * 于是"选择引擎"这件事在前端**根本不存在**——用户改不了，也看不出当前是什么。
 *
 * 现在引擎选择在设置页「运行与引擎 → 引擎与项目」里做（`EngineSetupPanel.vue`），
 * 这里只负责把结果读出来给运行类界面用，并让文案跟着引擎名走（不再一律写"Godot"）。
 */
import { computed, ref } from 'vue'
import { engineApi } from '../api'

const LABELS: Record<string, string> = { godot: 'Godot', unity: 'Unity', unreal: 'Unreal' }

export function useEngineConfig() {
  /** 当前配置的引擎 id；读不到时回落到 godot（后端默认也是它）。 */
  const engine = ref('godot')
  const loaded = ref(false)

  async function load() {
    try {
      const cfg = await engineApi.config()
      if (cfg?.ok && cfg.engine) engine.value = cfg.engine
    } catch {
      /* 读不到就用回落的 godot，不假装知道 */
    } finally {
      loaded.value = true
    }
    return engine.value
  }

  const engineName = computed(() => LABELS[engine.value] || engine.value)

  return { engine, engineName, loaded, load }
}
