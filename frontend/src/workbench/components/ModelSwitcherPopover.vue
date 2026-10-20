<script setup lang="ts">
// 对话栏模型芯片的轻量切换器：只负责在已保存模型之间切换。
// 新增模型、修改接口、Key、能力等完整参数统一进入「设置 → 模型」。
import { computed, ref } from 'vue'
import { modelApi } from '../api'
import type { ModelConfigInfo, ModelPreset } from '../api'

const props = defineProps<{ visible: boolean; config: ModelConfigInfo | null; loadError?: string }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'manage'): void
  (e: 'saved', info: ModelConfigInfo): void
}>()

const busyId = ref('')
const error = ref('')

const presets = computed(() => props.config?.model_presets || [])
const currentText = computed(() => {
  const c = props.config
  if (!c) return '模型加载中…'
  const label = c.provider_meta?.[c.llm_provider]?.label || c.llm_provider
  return `${label} · ${c.llm_model || '默认模型'}`
})
const isCurrent = (p: ModelPreset) =>
  p.provider === props.config?.llm_provider && p.model === props.config?.llm_model

async function activate(p: ModelPreset) {
  if (busyId.value) return
  busyId.value = p.id
  error.value = ''
  try {
    const result = await modelApi.activatePreset(p.id)
    if (result.error || result.ok === false) {
      error.value = result.error || '切换失败'
      return
    }
    emit('saved', result)
    emit('close')
  } catch (e) {
    error.value = (e as { message?: string }).message || '切换失败'
  } finally {
    busyId.value = ''
  }
}

function providerLabel(p: ModelPreset) {
  return props.config?.provider_meta?.[p.provider]?.label || p.provider
}
</script>

<template>
  <div v-if="visible" class="msp-wrap">
    <button class="msp-backdrop" aria-label="关闭模型切换器" @click="emit('close')" />
    <div class="msp-pop wb-modal-shell" role="dialog" aria-modal="true" aria-label="切换模型">
      <header class="msp-head">
        <div>
          <b>切换模型</b>
          <small>当前：{{ currentText }}</small>
        </div>
        <button class="msp-close" title="关闭" @click="emit('close')">×</button>
      </header>

      <div v-if="presets.length" class="msp-list" role="listbox" aria-label="已保存模型">
        <button
          v-for="p in presets"
          :key="p.id"
          class="msp-item"
          :class="{ current: isCurrent(p) }"
          :disabled="!!busyId || isCurrent(p)"
          role="option"
          :aria-selected="isCurrent(p)"
          @click="activate(p)"
        >
          <span class="msp-item-main">
            <strong>{{ p.label }}</strong>
            <small>{{ providerLabel(p) }} · {{ p.model || '默认模型' }}</small>
          </span>
          <span v-if="busyId === p.id" class="msp-state">切换中…</span>
          <span v-else-if="isCurrent(p)" class="msp-state current-state">使用中</span>
          <span v-else class="msp-state">切换</span>
        </button>
      </div>
      <p v-else class="msp-empty">还没有保存的模型预设。</p>
      <p v-if="error || loadError" class="msp-error">{{ error || loadError }}</p>

      <footer class="msp-foot">
        <span>添加模型、修改参数和保存 Key</span>
        <button class="msp-manage" @click="emit('manage')">打开设置</button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.msp-wrap { position: fixed; inset: 0; z-index: 2050; pointer-events: none; }
.msp-backdrop { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; background: transparent; pointer-events: auto; }
.msp-pop { position: absolute; right: 14px; bottom: 78px; width: min(360px, calc(100vw - 28px)); padding: 12px; pointer-events: auto; }
.msp-head { display: flex; justify-content: space-between; gap: 10px; padding-bottom: 9px; border-bottom: 1px solid var(--border); }
.msp-head b, .msp-head small { display: block; }
.msp-head b { font-size: var(--fs-md); }
.msp-head small { margin-top: 3px; color: var(--text-faint); font-size: var(--fs-cap); overflow-wrap: anywhere; }
.msp-close { border: 0; background: transparent; color: var(--text-muted); font-size: var(--fs-lead); cursor: pointer; border-radius: 5px; }
.msp-close:hover { background: var(--bg-hover); color: var(--text); }
.msp-list { display: grid; gap: 6px; max-height: 260px; overflow: auto; padding: 9px 0; }
.msp-item { display: flex; align-items: center; gap: 8px; width: 100%; padding: 8px 9px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg); color: var(--text); text-align: left; cursor: pointer; transition: border-color .14s, background .14s, transform .14s; }
.msp-item:hover:not(:disabled) { border-color: var(--accent); background: var(--bg-selected); transform: translateY(-1px); }
.msp-item.current { border-color: var(--accent); background: var(--bg-selected); }
.msp-item-main { min-width: 0; flex: 1; }
.msp-item-main strong, .msp-item-main small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.msp-item-main strong { font-size: var(--fs-sm); }
.msp-item-main small { margin-top: 2px; color: var(--text-faint); font-size: var(--fs-cap); }
.msp-state { flex: none; color: var(--accent); font-size: var(--fs-cap); }
.current-state { font-weight: 600; }
.msp-empty { margin: 14px 0; color: var(--text-faint); font-size: var(--fs-sm); }
.msp-error { margin: 0 0 8px; color: var(--danger); font-size: var(--fs-cap); }
.msp-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding-top: 9px; border-top: 1px solid var(--border); color: var(--text-faint); font-size: var(--fs-cap); }
.msp-manage { flex: none; padding: 5px 9px; border: 1px solid var(--accent); border-radius: 6px; background: var(--accent); color: var(--accent-ink); cursor: pointer; font-size: var(--fs-cap); }
@media (max-width: 560px) { .msp-pop { right: 8px; bottom: 70px; width: calc(100vw - 16px); } }
</style>
