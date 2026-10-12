<script setup lang="ts">
// 会话历史弹层（薄封装）：列表渲染与展示格式化内聚于此，
// 数据加载、会话切换、新建/删除/清空等副作用全部留在父组件，通过 emit 上抛。
import type { SessionInfo } from '../api'

defineProps<{
  open: boolean
  items: SessionInfo[]
  busy: boolean
  error: string
  currentId: string
  /** 是否允许「清空当前对话内容」（当前有消息，或 demo 模式） */
  canClear: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'refresh'): void
  (e: 'create'): void
  (e: 'select', sessionId: string): void
  (e: 'delete', sessionId: string): void
  (e: 'clear'): void
}>()

function sessionTitle(item: SessionInfo): string {
  return (item.title || item.preview || '').trim() || '未命名对话'
}
function sessionTime(value: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const delta = Math.max(0, Date.now() - date.getTime())
  if (delta < 60_000) return '刚刚'
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`
  return date.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' })
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="cd-pop-mask wb-modal-backdrop" @click="emit('close')" />
    <div v-if="open" class="cd-session-pop wb-modal-shell" role="dialog" aria-modal="true" aria-label="会话历史" @click.stop>
      <div class="cd-session-head">
        <div>
          <div class="cd-pop-title">会话历史</div>
          <div class="cd-session-subtitle">当前项目的对话会单独保存，切换后会恢复完整问答。</div>
        </div>
        <button class="cd-mini" :disabled="busy" title="刷新会话列表" @click="emit('refresh')">刷新</button>
      </div>
      <button class="cd-session-new" :disabled="busy" @click="emit('create')">
        <span class="cd-session-new-icon">＋</span>
        <span><b>新建对话</b><small>开启一段空白会话，不影响历史记录</small></span>
      </button>
      <div v-if="error" class="cd-session-error">{{ error }}</div>
      <div v-if="!items.length && !error" class="cd-session-empty">还没有已保存的对话</div>
      <div v-for="item in items" :key="item.session_id" class="cd-session-item" :class="{ active: item.session_id === currentId }">
        <button class="cd-session-main" :disabled="busy" @click="emit('select', item.session_id)">
          <span class="cd-session-current" aria-hidden="true">{{ item.session_id === currentId ? '●' : '○' }}</span>
          <span class="cd-session-copy">
            <strong>{{ sessionTitle(item) }}</strong>
            <small>{{ item.turns }} 轮<span v-if="sessionTime(item.updated_at)"> · {{ sessionTime(item.updated_at) }}</span></small>
          </span>
        </button>
        <button
          class="cd-session-delete"
          :title="item.session_id === currentId ? '清空当前会话' : '删除这条对话历史'"
          :disabled="busy"
          @click.stop="emit('delete', item.session_id)"
        >×</button>
      </div>
      <button
        class="cd-session-clear"
        :disabled="busy || !canClear"
        title="清空当前对话的全部消息（会话记录本身保留）"
        @click="emit('clear')"
      >清空当前对话内容</button>
    </div>
  </Teleport>
</template>

<style scoped>
.cd-pop-mask {
  position: fixed;
  inset: 0;
  z-index: 59;
  background: var(--scrim-soft);
  backdrop-filter: blur(1px);
  animation: sh-fade-in .16s ease-out both;
}
.cd-pop-title { font-size: 12px; font-weight: 600; color: var(--text); margin-bottom: 8px; }
.cd-mini {
  height: 21px; padding: 0 9px; font-size: 11px;
  border: 1px solid var(--border-strong); border-radius: 5px;
  background: transparent; color: var(--text-muted); cursor: pointer;
}
.cd-mini:hover:not(:disabled) { color: var(--text); border-color: var(--accent); }
.cd-mini:disabled { opacity: .45; cursor: default; }
.cd-mini:focus-visible,
.cd-session-new:focus-visible,
.cd-session-main:focus-visible,
.cd-session-delete:focus-visible,
.cd-session-clear:focus-visible { outline: 2px solid #2f6fed88; outline-offset: 2px; }
.cd-mini, .cd-session-new, .cd-session-main, .cd-session-delete, .cd-session-clear { transition: color .16s ease, border-color .16s ease, background .16s ease, transform .16s ease, opacity .16s ease; }
.cd-session-pop {
  position: fixed;
  right: 10px; bottom: 64px;
  width: 360px; max-width: calc(100vw - 24px);
  max-height: min(62vh, 430px); overflow-y: auto;
  background: var(--bg-raised); border: 1px solid var(--border-strong);
  border-radius: 8px; box-shadow: 0 14px 38px rgba(35,52,84,.2);
  padding: 10px; z-index: 60;
  animation: sh-pop-in .2s cubic-bezier(.2,.8,.2,1) both;
}
.cd-session-head { display: flex; align-items: flex-start; gap: 8px; justify-content: space-between; }
.cd-session-subtitle { color: var(--text-faint); font-size: 10px; line-height: 1.45; margin-top: -4px; }
.cd-session-new {
  width: 100%; display: flex; align-items: center; gap: 9px; margin: 8px 0;
  padding: 8px 9px; border: 1px solid #c8dcfa; border-radius: 6px;
  background: #f3f8ff; color: var(--accent); text-align: left; cursor: pointer;
}
.cd-session-new:hover { background: var(--bg-selected); border-color: var(--accent); }
.cd-session-new:disabled { opacity: .55; cursor: default; }
.cd-session-new-icon { width: 20px; height: 20px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid currentColor; border-radius: 50%; font-size: 16px; line-height: 1; }
.cd-session-new b, .cd-session-new small { display: block; }
.cd-session-new b { font-size: 12px; }
.cd-session-new small { margin-top: 2px; color: var(--text-muted); font-size: 10px; }
.cd-session-clear {
  width: 100%;
  margin-top: 8px;
  padding: 8px 9px 4px;
  border: 0;
  border-top: 1px solid var(--border);
  background: transparent;
  color: var(--danger, #dc4c52);
  font-size: 11px;
  text-align: center;
  cursor: pointer;
}
.cd-session-clear:hover:not(:disabled) { background: rgba(220,76,82,.08); }
.cd-session-clear:disabled { opacity: .45; cursor: default; }
.cd-session-error { padding: 6px 8px; color: var(--danger); background: #fff4f4; border: 1px solid #f2caca; border-radius: 5px; font-size: 10px; }
.cd-session-empty { padding: 12px 5px; color: var(--text-faint); font-size: 11px; text-align: center; }
.cd-session-item { display: flex; align-items: stretch; border-top: 1px solid var(--border); }
.cd-session-item.active { background: var(--bg-selected); }
.cd-session-main { flex: 1; min-width: 0; display: flex; gap: 7px; align-items: center; padding: 8px 5px; border: 0; background: transparent; color: var(--text); text-align: left; cursor: pointer; }
.cd-session-main:hover { background: var(--bg-hover); }
.cd-session-main:disabled { opacity: .6; cursor: default; }
.cd-session-current { flex: 0 0 14px; color: var(--text-faint); font-size: 10px; }
.cd-session-item.active .cd-session-current { color: var(--accent); }
.cd-session-copy { min-width: 0; display: block; }
.cd-session-copy strong, .cd-session-copy small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cd-session-copy strong { font-size: 12px; font-weight: 600; }
.cd-session-copy small { margin-top: 2px; color: var(--text-faint); font-size: 10px; }
.cd-session-delete { align-self: center; margin-right: 5px; width: 22px; height: 22px; border: 0; background: transparent; color: var(--text-faint); cursor: pointer; font-size: 16px; }
.cd-session-delete:hover { color: var(--danger); }
.cd-session-delete:disabled { cursor: default; opacity: .5; }
@keyframes sh-fade-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes sh-pop-in { from { opacity: 0; transform: translateY(8px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
@media (max-width: 560px) {
  .cd-session-pop { left: 8px; right: 8px; bottom: 8px; width: auto; max-width: none; max-height: calc(100vh - 16px); border-radius: 10px; }
}
@media (prefers-reduced-motion: reduce) {
  .cd-pop-mask, .cd-session-pop, .cd-mini, .cd-session-new, .cd-session-main, .cd-session-delete, .cd-session-clear { animation: none; transition: none; }
}
</style>
