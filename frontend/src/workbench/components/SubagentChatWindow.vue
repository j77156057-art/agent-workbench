<script setup lang="ts">
// 子 Agent 的独立对话窗（2026-11）。
//
// 诉求（用户原话）：子 agent 逻辑上和正常 agent 一样，直接复用对话框的推理逻辑，
// 点开卡片后**另弹一个单独的对话窗口**，不要卡片里的简化版。
//
// 所以这里不重写渲染：窗口里放的就是 ChatDock 本身，scope='replay'。
// replay 模式下 ChatDock 只做 history 回灌（rebuildFromTurns 同一套 trace 渲染、
// 工具行、最终答案、产物卡片），不碰全局会话/Web Lock/发送链路/折叠记忆。
import { ref, watch } from 'vue'
import ChatDock from './ChatDock.vue'

const props = defineProps<{
  open: boolean
  /** 子 agent 的真实会话 id（sub-<ticket>）；为空时只显示空态提示 */
  sessionId: string
  title?: string
  emptyHint?: string
  /** 归档来源：sessions=已归档逐轮记录；delegation=旧数据只有任务+结论 */
  source?: string
}>()
const emit = defineEmits<{ (e: 'close'): void }>()

const chip = ref('')
watch(() => [props.open, props.source] as const, () => {
  chip.value = props.source === 'sessions'
    ? '已归档逐轮记录'
    : props.source === 'delegation'
      ? '历史未归档（旧数据只有任务与结论）'
      : ''
}, { immediate: true })

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.open) emit('close')
}
watch(() => props.open, (open) => {
  if (open) window.addEventListener('keydown', onKey)
  else window.removeEventListener('keydown', onKey)
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="scw-mask" role="presentation" @click.self="emit('close')">
      <section class="scw-win" role="dialog" aria-modal="true" :aria-label="title || '子 Agent 对话'">
        <header class="scw-head">
          <span class="scw-mark" aria-hidden="true"><i /><i /><i /></span>
          <div class="scw-id">
            <strong>{{ title || '子 Agent 对话' }}</strong>
            <span class="scw-sub">
              只读回看 · {{ sessionId || '无会话' }}
              <em v-if="chip" class="scw-chip" :class="{ 'is-legacy': source !== 'sessions' }">{{ chip }}</em>
            </span>
          </div>
          <button class="scw-close" type="button" aria-label="关闭" title="关闭（Esc）" @click="emit('close')">×</button>
        </header>
        <div class="scw-body">
          <ChatDock
            scope="replay"
            :replay-session-id="sessionId"
            :replay-title="title || '子 Agent 对话'"
            :replay-empty-hint="emptyHint || '暂无已归档的逐轮记录'"
          />
        </div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.scw-mask {
  position: fixed;
  inset: 0;
  z-index: 900;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(9, 12, 18, .46);
  backdrop-filter: blur(2px);
}
.scw-win {
  width: min(920px, 92vw);
  height: min(760px, 86vh);
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg-raised);
  box-shadow: 0 24px 64px rgba(0, 0, 0, .34);
  overflow: hidden;
}
.scw-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-raised);
  flex: 0 0 auto;
}
.scw-mark {
  display: inline-flex;
  gap: 3px;
  align-items: center;
}
.scw-mark i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--accent, #4f8cff);
  opacity: .85;
}
.scw-id {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.scw-id strong {
  font-size: 13px;
  color: var(--fg);
}
.scw-sub {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--fg-muted, #8a93a3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.scw-chip {
  font-style: normal;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 999px;
  font-size: 10px;
  line-height: 16px;
  color: var(--fg-muted, #8a93a3);
}
.scw-chip.is-legacy {
  color: #c98a2e;
  border-color: rgba(201, 138, 46, .45);
}
.scw-close {
  margin-left: auto;
  width: 26px;
  height: 26px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: transparent;
  color: var(--fg);
  cursor: pointer;
  font-size: 15px;
  line-height: 1;
}
.scw-close:hover { background: var(--bg-hover, rgba(255,255,255,.06)); }
.scw-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
/* ChatDock 在 replay 下把自己摊满窗口 */
.scw-body :deep(.cd-dock) { border-top: 0; }
</style>
