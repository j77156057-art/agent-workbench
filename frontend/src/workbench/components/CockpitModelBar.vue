<script setup lang="ts">
// 自主开发舱模型条：
// - 跟随全局：工作流（方案/规划/执行会话）与对话台共用全局模型；
// - 固定预设：工作流单独使用一套已保存的模型预设，与问答互不影响；
// - 自动选模：Harness 自行选择模型——方案生成用全局模型，规划/子任务按复杂度
//   自动升级到「模型设置-自动模式」里指定的云端强模型，失败重规划一律升级。
// 选择跨重启持久化、即时生效。预设的新增/Key 保存仍在对话台「模型设置」里完成。
import { computed, onMounted, ref } from 'vue'
import { modelApi, type CockpitMode, type CockpitModelInfo, type ModelPreset } from '../api'
import { useWorkbench } from '../composables/workbench'
import { appEvents } from '../eventBus'

const { setWorkspace } = useWorkbench()

const info = ref<CockpitModelInfo | null>(null)
const mode = ref<CockpitMode>('global')
const fixedPresetId = ref('')
const busy = ref(false)
const error = ref('')
const loaded = ref(false)

const PROVIDER_LABELS: Record<string, string> = {
  ollama: 'Ollama',
  llamacpp: 'llama.cpp',
  custom: '自定义端点',
  deepseek: 'DeepSeek',
  qwen: '通义千问',
  dashscope: '通义千问',
  kimi: 'Kimi',
  moonshot: 'Kimi',
  openai: 'OpenAI',
  zhipu: '智谱',
  doubao: '豆包',
}

const presets = computed<ModelPreset[]>(() => info.value?.presets || [])
const globalText = computed(() => {
  const c = info.value?.current
  return c ? `${c.provider} / ${c.model || '默认模型'}` : '全局模型'
})
const fixedPreset = computed(() =>
  presets.value.find(p => p.id === fixedPresetId.value) || null)
const autoInfo = computed(() => info.value?.auto || null)

const statusText = computed(() => {
  if (busy.value) return '切换中…'
  if (mode.value === 'fixed') {
    const p = fixedPreset.value
    return p
      ? `固定使用 ${p.label}（${p.provider} · ${p.model || '默认模型'}）`
      : '固定模式（预设已失效，请重新选择）'
  }
  if (mode.value === 'auto') {
    const a = autoInfo.value
    if (a?.available && a.preset) {
      return `自动选模：常规任务用全局（${globalText.value}），复杂阶段升级 ${a.preset.label}（${a.preset.provider} · ${a.preset.model || '默认模型'}）`
    }
    return a?.reason || '自动模式不可用'
  }
  return `跟随全局 · ${globalText.value}`
})

function providerLabel(p: ModelPreset): string {
  return PROVIDER_LABELS[p.provider] || p.provider
}
function presetText(p: ModelPreset): string {
  return `${p.label} · ${providerLabel(p)} / ${p.model || '默认模型'}${p.has_key ? '' : '（未存 Key）'}`
}

async function refresh() {
  const r = await modelApi.cockpitGet()
  info.value = r
  mode.value = r.mode || 'global'
  fixedPresetId.value = r.preset_id || ''
  return r
}

async function applyMode(next: CockpitMode, presetId = fixedPresetId.value) {
  if (busy.value) return
  // 切固定模式时必须有一个有效预设；没有预设就引导去模型设置
  if (next === 'fixed') {
    const target = presets.value.find(p => p.id === presetId) || presets.value[0]
    if (!target) {
      error.value = '还没有可用的模型预设，请先点「管理预设」在模型设置里创建。'
      return
    }
    presetId = target.id
  }
  const previousMode = mode.value
  const previousPreset = fixedPresetId.value
  busy.value = true
  error.value = ''
  mode.value = next
  if (next === 'fixed') fixedPresetId.value = presetId
  try {
    const r = await modelApi.cockpitSet(next, next === 'fixed' ? presetId : '')
    if (r.ok === false) throw new Error(r.error || '保存失败')
    info.value = r
    mode.value = r.mode || next
    fixedPresetId.value = r.preset_id || (next === 'fixed' ? presetId : fixedPresetId.value)
  } catch (e) {
    // 失败回滚界面选择并提示（例如自动模式还没配云端预设/Key）
    mode.value = previousMode
    fixedPresetId.value = previousPreset
    error.value = (e as Error).message || '保存失败'
  } finally {
    busy.value = false
  }
}

async function onPresetChange() {
  await applyMode('fixed', fixedPresetId.value)
}

async function managePresets() {
  // ChatDock 在开发舱工作区被卸载：先切回概览，等对话台挂载后再唤起模型设置弹窗
  setWorkspace('overview')
  await new Promise(r => setTimeout(r, 120))
  appEvents.emit('docmind:open-model-settings')
}

onMounted(() => {
  refresh()
    .catch(e => { error.value = (e as Error).message || '模型选择加载失败' })
    .finally(() => { loaded.value = true })
})
</script>

<template>
  <div class="cmb">
    <span class="cmb-label">自主执行模型</span>
    <div class="cmb-seg" role="tablist">
      <button type="button" class="cmb-seg-opt" :class="{ 'cmb-seg-on': mode === 'global' }"
              :disabled="busy || !loaded" @click="applyMode('global')">跟随全局</button>
      <button type="button" class="cmb-seg-opt" :class="{ 'cmb-seg-on': mode === 'auto' }"
              :disabled="busy || !loaded" @click="applyMode('auto')">自动选模</button>
      <button type="button" class="cmb-seg-opt" :class="{ 'cmb-seg-on': mode === 'fixed' }"
              :disabled="busy || !loaded" @click="applyMode('fixed')">固定预设</button>
    </div>
    <select v-if="mode === 'fixed'" v-model="fixedPresetId" class="cmb-select"
            :disabled="busy || !loaded" @change="onPresetChange">
      <option v-for="p in presets" :key="p.id" :value="p.id">{{ presetText(p) }}</option>
    </select>
    <span class="cmb-effective"
          :class="{ 'cmb-on': mode === 'fixed', 'cmb-auto': mode === 'auto' && autoInfo?.available,
                    'cmb-warn': mode === 'auto' && !autoInfo?.available }"
          :title="mode === 'auto' && !autoInfo?.available ? (autoInfo?.reason || '') : ''">
      {{ statusText }}
    </span>
    <button type="button" class="cmb-manage" @click="managePresets">管理预设</button>
    <span v-if="mode === 'auto' && autoInfo && !autoInfo.available" class="cmb-hint">
      {{ autoInfo.reason }}
    </span>
    <span v-if="error" class="cmb-error" :title="error">{{ error }}</span>
  </div>
</template>

<style scoped>
.cmb {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 0 0 14px;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg-hover, color-mix(in srgb, var(--accent) 4%, transparent));
  font-size: var(--fs-sm);
  box-shadow: inset 3px 0 color-mix(in srgb, var(--accent) 42%, transparent), 0 3px 12px rgba(35,52,84,.05);
  transition: border-color .16s ease, box-shadow .16s ease, background .16s ease;
}
.cmb:focus-within { border-color: color-mix(in srgb, var(--accent) 45%, transparent); box-shadow: inset 3px 0 color-mix(in srgb, var(--accent) 70%, transparent), 0 0 0 3px color-mix(in srgb, var(--accent) 8%, transparent); }
.cmb-label { font-weight: 600; color: var(--text); white-space: nowrap; }
.cmb-seg {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: 7px;
  overflow: hidden;
  flex: 0 0 auto;
}
.cmb-seg-opt {
  border: 0;
  background: var(--bg-raised);
  color: var(--text-muted);
  padding: 5px 12px;
  font: inherit;
  font-size: var(--fs-sm);
  cursor: pointer;
  border-right: 1px solid var(--border);
  white-space: nowrap;
}
.cmb-seg-opt:last-child { border-right: 0; }
.cmb-seg-opt:hover:not(:disabled) { color: var(--text); background: var(--bg-hover); }
.cmb-seg-opt:disabled { opacity: .55; cursor: default; }
.cmb-seg-on { color: var(--accent-text); font-weight: 600; background: var(--bg-selected); }
.cmb-seg-opt:focus-visible, .cmb-select:focus-visible, .cmb-manage:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.cmb-seg-opt { transition: color .16s ease, background .16s ease, opacity .16s ease; }
.cmb-select {
  min-width: 220px;
  max-width: 380px;
  flex: 1 1 220px;
  border: 1px solid var(--border-graphic);
  border-radius: 7px;
  padding: 5px 8px;
  background: var(--bg-raised);
  color: var(--text);
  font: inherit;
  font-size: var(--fs-sm);
  transition: border-color .16s ease, box-shadow .16s ease, background .16s ease;
}
.cmb-select:hover:not(:disabled) { border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
.cmb-select:disabled { opacity: .55; cursor: default; }
.cmb-effective {
  color: var(--text-faint);
  font-size: var(--fs-cap);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 420px;
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: color-mix(in srgb, var(--bg-raised) 42%, transparent);
}
.cmb-on { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 28%, transparent); background: color-mix(in srgb, var(--accent) 8%, transparent); }
.cmb-auto { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 28%, transparent); background: color-mix(in srgb, var(--accent) 8%, transparent); }
.cmb-warn { color: var(--warning, #b8860b); border-color: rgba(184,134,11,.28); background: rgba(184,134,11,.08); }
.cmb-hint {
  flex-basis: 100%;
  color: var(--warning, #b8860b);
  font-size: var(--fs-cap);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cmb-manage {
  border: 1px solid var(--border);
  background: var(--bg-raised);
  color: var(--text-muted);
  border-radius: 7px;
  padding: 4px 10px;
  font: inherit;
  font-size: var(--fs-cap);
  cursor: pointer;
  white-space: nowrap;
  transition: color .16s ease, border-color .16s ease, background .16s ease, transform .16s ease;
}
.cmb-manage:hover { border-color: var(--accent); color: var(--accent); }
.cmb-manage:active { transform: translateY(1px); }
.cmb-error {
  flex-basis: 100%;
  color: var(--danger);
  font-size: var(--fs-cap);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 4px 8px;
  border-radius: 5px;
  background: rgba(192,58,64,.08);
  border: 1px solid rgba(192,58,64,.18);
}

@media (max-width: 760px) {
  .cmb { align-items: stretch; gap: 7px; padding: 9px 10px; }
  .cmb-label { flex-basis: 100%; }
  .cmb-seg { flex: 1 1 100%; }
  .cmb-seg-opt { flex: 1 1 0; padding-inline: 8px; }
  .cmb-select { min-width: 0; max-width: none; flex-basis: 100%; }
  .cmb-effective { flex: 1 1 100%; max-width: none; white-space: normal; line-height: 1.45; }
  .cmb-manage { margin-left: auto; }
  .cmb-hint, .cmb-error { white-space: normal; line-height: 1.45; }
}
@media (prefers-reduced-motion: reduce) {
  .cmb, .cmb-seg-opt, .cmb-select, .cmb-manage { transition: none; }
}
</style>
