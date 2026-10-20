<script setup lang="ts">
// GLB 3D 预览：@google/model-viewer 本地打包，动态 import 拆成懒 chunk；
// 加载失败时降级为提示（文件仍可正常导入，不影响主流程）。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps<{ src: string; poster?: string; cameraControls?: boolean }>()

const status = ref<'loading' | 'ready' | 'error'>('loading')
let disposed = false

async function loadViewer() {
  status.value = 'loading'
  try {
    await import('@google/model-viewer')
    if (!disposed) status.value = 'ready'
  } catch {
    if (!disposed) status.value = 'error'
  }
}

onMounted(loadViewer)
watch(() => props.src, () => { /* src 切换由 model-viewer 自身处理，无需重载组件 */ })
onBeforeUnmount(() => { disposed = true })
</script>

<template>
  <div class="m3d-wrap">
    <model-viewer
      v-if="status === 'ready'"
      class="m3d-viewer"
      :src="src"
      :poster="poster || undefined"
      :camera-controls="cameraControls !== false"
      auto-rotate
      shadow-intensity="0.6"
      exposure="1"
      loading="lazy"
      alt="3D 模型预览"
    />
    <div v-else-if="status === 'loading'" class="m3d-fallback">
      <span class="m3d-spinner" aria-hidden="true" />
      <span>3D 预览组件加载中…</span>
    </div>
    <div v-else class="m3d-fallback m3d-error">
      <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
        <path d="M11 3 L20 18.5 H2 Z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>
        <path d="M11 9 V13.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
        <circle cx="11" cy="16" r="0.9" fill="currentColor"/>
      </svg>
      <span>3D 预览组件加载失败，文件仍可正常导入</span>
    </div>
  </div>
</template>

<style scoped>
.m3d-wrap {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 120px;
  background: #eef2f8;
  background-image:
    repeating-linear-gradient(0deg, rgba(35,52,84,.05) 0 1px, transparent 1px 16px),
    repeating-linear-gradient(90deg, rgba(35,52,84,.05) 0 1px, transparent 1px 16px);
  border-radius: 6px;
  overflow: hidden;
  box-shadow: inset 0 0 0 1px rgba(35,52,84,.04);
}
.m3d-viewer {
  display: block;
  width: 100%;
  height: 100%;
  min-height: 120px;
  --poster-color: transparent;
  background: transparent;
}
.m3d-fallback {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-faint);
  font-size: var(--fs-cap);
  background: rgba(238,242,248,.72);
  animation: m3d-fallback-in .18s ease-out both;
}
.m3d-error { color: #b06a2b; background: rgba(253,243,223,.82); }
.m3d-spinner {
  width: 18px;
  height: 18px;
  border: 2px solid color-mix(in srgb, var(--accent) 20%, transparent);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: m3d-spin .8s linear infinite;
}
@keyframes m3d-spin { to { transform: rotate(360deg); } }
@keyframes m3d-fallback-in { from { opacity: 0; } to { opacity: 1; } }
@media (prefers-reduced-motion: reduce) {
  .m3d-fallback, .m3d-spinner { animation: none; }
}
</style>
