<script setup lang="ts">
/**
 * M2·③ 实时模式徽章——把 `describeRealtimeMode` 的 view 直接绑到 UI 上。
 *
 * 用法：接 REST `visionApi.realtimeStatus()` 或 WS `hello.ok`，把整个 payload 丢给
 * `:payload` prop 就行；组件内部再做归一，两种拼法字段名不同也吃得住。
 * 关键信条（docs/multimodal-verification-engine-eda-kickoff.md §5.4）：
 * 降级必须响亮——native 是淡绿 pill、degraded/unavailable/unknown 一律带底色 + 短标签，
 * 用户扫一眼就知道"当前到底是不是原生实时"，不再靠翻后端日志。
 *
 * 现阶段的接线范围只到"helper + 组件 + 单测"。真正的挂载点等 WS 客户端接好（R13 另一半），
 * 到时候一行 `<RealtimeModeBadge :payload="liveModeSnapshot" />` 就落地，不再改这块逻辑。
 */
import { computed } from 'vue'
import { describeRealtimeMode } from '../realtimeModeDisplay'

const props = defineProps<{
  payload: Record<string, unknown> | null | undefined
  compact?: boolean
}>()

const view = computed(() => describeRealtimeMode(props.payload))
</script>

<template>
  <span
    class="rmb"
    :class="[`rmb--${view.tone}`, props.compact ? 'rmb--compact' : '']"
    :title="view.hint"
    :aria-label="`实时视觉模式：${view.label}`"
    data-testid="realtime-mode-badge"
    :data-tone="view.tone"
    :data-degraded="view.isDegraded ? 'true' : 'false'"
  >
    <span class="rmb-dot" aria-hidden="true" />
    <span class="rmb-label">{{ view.label }}</span>
  </span>
</template>

<style scoped>
.rmb {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px 2px 8px;
  border-radius: 999px;
  font-size: var(--fs-sm);
  line-height: 1.6;
  border: 1px solid transparent;
  white-space: nowrap;
  user-select: none;
}
.rmb-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: currentColor;
  opacity: 0.9;
}
.rmb--compact { padding: 0 6px; font-size: var(--fs-cap); }
.rmb--native       { color: var(--green); background: color-mix(in srgb, var(--green) 12%, var(--bg-raised)); border-color: color-mix(in srgb, var(--green) 38%, transparent); }
.rmb--degraded     { color: var(--amber); background: color-mix(in srgb, var(--amber) 12%, var(--bg-raised)); border-color: color-mix(in srgb, var(--amber) 38%, transparent); }
.rmb--unavailable  { color: var(--danger); background: color-mix(in srgb, var(--danger) 12%, var(--bg-raised)); border-color: color-mix(in srgb, var(--danger) 38%, transparent); }
.rmb--unknown      { color: var(--text-muted); background: color-mix(in srgb, var(--text-muted) 10%, var(--bg-raised)); border-color: color-mix(in srgb, var(--text-muted) 30%, transparent); }
</style>
