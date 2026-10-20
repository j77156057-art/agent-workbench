<script setup lang="ts">
// AI 问答页的会话历史弹层（仅 ask- 域会话）。
// 与工作台 SessionHistoryPopover 同构：渲染内聚于此，数据加载/切换/删除由父组件处理。
import type { SessionInfo } from '../../workbench/api'

defineProps<{
  open: boolean
  items: SessionInfo[]
  busy: boolean
  error: string
  currentId: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'refresh'): void
  (e: 'create'): void
  (e: 'select', sessionId: string): void
  (e: 'delete', sessionId: string): void
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
    <div v-if="open" class="ah-pop-mask" @click="emit('close')" />
    <div v-if="open" class="ah-pop" @click.stop>
      <div class="ah-head">
        <div>
          <div class="ah-title">问答历史</div>
          <div class="ah-subtitle">当前项目的问答会自动保存，点开即可恢复完整对话。</div>
        </div>
        <button class="ah-mini" :disabled="busy" title="刷新历史列表" @click="emit('refresh')">刷新</button>
      </div>
      <button class="ah-new" :disabled="busy" @click="emit('create')">
        <span class="ah-new-icon">＋</span>
        <span><b>新建问答</b><small>开始一段空白对话，不影响历史记录</small></span>
      </button>
      <div v-if="error" class="ah-error">{{ error }}</div>
      <div v-if="!items.length && !error" class="ah-empty">还没有已完成并保存的问答</div>
      <div v-for="item in items" :key="item.session_id"
           class="ah-item" :class="{ active: item.session_id === currentId }">
        <button class="ah-main" :disabled="busy" @click="emit('select', item.session_id)">
          <span class="ah-dot">{{ item.session_id === currentId ? '●' : '○' }}</span>
          <span class="ah-copy">
            <strong>{{ sessionTitle(item) }}</strong>
            <small>{{ item.turns }} 轮<span v-if="sessionTime(item.updated_at)"> · {{ sessionTime(item.updated_at) }}</span></small>
          </span>
        </button>
        <button class="ah-delete"
                :title="item.session_id === currentId ? '清空当前问答' : '删除这条问答历史'"
                :disabled="busy"
                @click.stop="emit('delete', item.session_id)">×</button>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.ah-pop-mask { position: fixed; inset: 0; z-index: 59; }
.ah-pop {
  position: fixed; top: 58px; right: 24px;
  width: 360px; max-width: calc(100vw - 24px);
  max-height: min(70vh, 520px); overflow-y: auto;
  background: var(--bg-raised, #fff);
  border: 1px solid var(--border-strong, #d9dee8);
  border-radius: 10px;
  box-shadow: 0 14px 38px rgba(35, 52, 84, .2);
  padding: 12px; z-index: 60;
}
.ah-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.ah-title { font-size: var(--fs-md); font-weight: 600; color: var(--text); }
.ah-subtitle { color: var(--text-faint, #8a93a5); font-size: var(--fs-cap); line-height: 1.45; margin-top: 2px; }
.ah-mini {
  height: 24px; padding: 0 10px; font-size: var(--fs-cap); flex: none;
  border: 1px solid var(--border-strong, #d9dee8); border-radius: 6px;
  background: transparent; color: var(--text-muted, #667085); cursor: pointer;
}
.ah-mini:hover:not(:disabled) { color: var(--accent, var(--accent)); border-color: var(--accent, var(--accent)); }
.ah-mini:disabled { opacity: .45; cursor: default; }
.ah-new {
  width: 100%; display: flex; align-items: center; gap: 9px; margin: 10px 0 4px;
  padding: 9px 10px; border: 1px solid #c8dcfa; border-radius: 8px;
  background: #f3f8ff; color: var(--accent, var(--accent));
  text-align: left; cursor: pointer;
}
.ah-new:hover { background: var(--bg-selected, #eaf2ff); }
.ah-new:disabled { opacity: .55; cursor: default; }
.ah-new-icon {
  width: 20px; height: 20px; display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid currentColor; border-radius: 50%; font-size: var(--fs-h2); line-height: 1;
}
.ah-new b, .ah-new small { display: block; }
.ah-new b { font-size: var(--fs-sm); }
.ah-new small { margin-top: 2px; color: var(--text-muted, #667085); font-size: var(--fs-cap); }
.ah-error {
  padding: 6px 8px; margin-top: 6px;
  color: var(--danger, #dc4c52); background: #fff4f4;
  border: 1px solid #f2caca; border-radius: 6px; font-size: var(--fs-cap);
}
.ah-empty { padding: 14px 5px; color: var(--text-faint, #8a93a5); font-size: var(--fs-sm); text-align: center; }
.ah-item { display: flex; align-items: stretch; border-top: 1px solid var(--border, #e4e8ef); }
.ah-item.active { background: var(--bg-selected, #f1f5fb); }
.ah-main {
  flex: 1; min-width: 0; display: flex; gap: 8px; align-items: center;
  padding: 9px 5px; border: 0; background: transparent;
  color: var(--text); text-align: left; cursor: pointer;
}
.ah-main:hover { background: var(--bg-hover, #f5f8fd); }
.ah-main:disabled { opacity: .6; cursor: default; }
.ah-dot { flex: 0 0 14px; color: var(--text-faint, #8a93a5); font-size: var(--fs-cap); }
.ah-item.active .ah-dot { color: var(--accent, var(--accent)); }
.ah-copy { min-width: 0; display: block; }
.ah-copy strong, .ah-copy small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ah-copy strong { font-size: var(--fs-sm); font-weight: 600; }
.ah-copy small { margin-top: 2px; color: var(--text-faint, #8a93a5); font-size: var(--fs-cap); }
.ah-delete {
  align-self: center; margin-right: 5px; width: 24px; height: 24px;
  border: 0; background: transparent; color: var(--text-faint, #8a93a5);
  cursor: pointer; font-size: var(--fs-lead); line-height: 1;
}
.ah-delete:hover { color: var(--danger, #dc4c52); }
.ah-delete:disabled { cursor: default; opacity: .5; }
</style>
