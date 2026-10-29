<script setup lang="ts">
// 左下角工作流成员抽屉：折叠条 + 展开面板。点击成员 → 定位到对话内该成员的实时轨迹。
// 仅在存在活跃工作流且已有成员时出现；终态后自动隐藏。
import { computed, ref } from 'vue'

interface Member { id: string; label: string; role: string; status: string; task: string }
const props = defineProps<{
  active: boolean
  members: Member[]
}>()
const emit = defineEmits<{ (e: 'focus', taskId: string): void }>()

const open = ref(false)
const visible = computed(() => props.active && props.members.length > 0)
const running = computed(() => props.members.some(m => m.status === 'running'))
const doneCount = computed(() => props.members.filter(m => m.status === 'ok').length)
function statusClass(s: string) {
  if (s === 'ok') return 'ok'
  if (s === 'failed' || s === 'blocked') return 'bad'
  if (s === 'running') return 'running'
  return 'pending'
}
function focus(id: string) {
  emit('focus', id)
}
</script>

<template>
  <div v-if="visible" class="wmd" :class="{ 'wmd-open': open }">
    <button class="wmd-strip" @click="open = !open">
      <span class="wmd-pulse" :class="{ on: running }" />
      <b>工作流团队</b>
      <em>{{ doneCount }}/{{ members.length }}</em>
      <span class="wmd-arrow" :class="{ open }">
        <svg width="8" height="8" viewBox="0 0 9 9"><path d="M2 1.5 L5.5 4.5 L2 7.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" /></svg>
      </span>
    </button>
    <div v-if="open" class="wmd-panel">
      <div class="wmd-head">
        <b>成员（{{ members.length }}）</b>
        <small>点击定位到对话轨迹</small>
      </div>
      <button
        v-for="m in members"
        :key="m.id"
        class="wmd-item"
        :class="`is-${statusClass(m.status)}`"
        @click="focus(m.id)"
      >
        <span class="wmd-dot" />
        <span class="wmd-main">
          <b>{{ m.label }} <small>{{ m.id }}</small></b>
          <span class="wmd-task">{{ m.task }}</span>
        </span>
        <em class="wmd-state">{{ ({ pending: '待执行', running: '执行中', ok: '完成', failed: '失败', blocked: '阻塞' } as Record<string, string>)[m.status] || m.status }}</em>
      </button>
    </div>
  </div>
</template>

<style scoped>
.wmd {
  position: fixed; left: 12px; bottom: 96px; z-index: 90;
  display: flex; flex-direction: column; align-items: flex-start; gap: 6px;
  max-width: min(340px, calc(100vw - 24px));
}
.wmd-strip {
  display: inline-flex; align-items: center; gap: 7px;
  height: 28px; padding: 0 11px;
  border: 1px solid var(--border-strong); border-radius: 99px;
  background: var(--bg-raised); box-shadow: 0 4px 14px rgba(35,52,84,.16);
  color: var(--text); font-size: var(--fs-sm); cursor: pointer; transition: border-color .16s ease, background .16s ease, transform .16s ease, box-shadow .16s ease;
}
.wmd-strip:hover { border-color: var(--accent); background: var(--bg-selected); transform: translateY(-1px); box-shadow: 0 6px 16px rgba(35,52,84,.18); }.wmd-strip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.wmd-strip b { font-weight: 600; }
.wmd-strip em { font-style: normal; color: var(--text-faint); font-size: var(--fs-cap); font-variant-numeric: tabular-nums; }
.wmd-arrow {
  display: inline-flex; align-items: center; color: var(--text-faint);
  transform: rotate(0deg); transform-origin: center;
  transition: transform .22s cubic-bezier(.32, .72, 0, 1);
  will-change: transform;
}
.wmd-arrow.open { transform: rotate(90deg); }
@media (prefers-reduced-motion: reduce) { .wmd-arrow { transition: none; } }
.wmd-pulse { width: 7px; height: 7px; border-radius: 50%; background: var(--text-faint); }
.wmd-pulse.on { background: var(--accent); box-shadow: 0 0 0 0 rgba(37,96,212,.45); animation: wmd-pulse 1.4s infinite; }
@keyframes wmd-pulse {
  0% { box-shadow: 0 0 0 0 rgba(37,96,212,.4); }
  70% { box-shadow: 0 0 0 7px rgba(37,96,212,0); }
  100% { box-shadow: 0 0 0 0 rgba(37,96,212,0); }
}
.wmd-panel {
  width: 310px; max-width: calc(100vw - 24px);
  max-height: 42vh; overflow-y: auto;
  border: 1px solid var(--border-strong); border-radius: 10px;
  background: var(--bg-raised); box-shadow: 0 14px 34px rgba(35,52,84,.22);
  padding: 8px; display: grid; gap: 3px;
  transform-origin: left bottom;
  animation: wmd-pop .18s cubic-bezier(.32, .72, 0, 1);
}
@keyframes wmd-pop {
  from { opacity: 0; transform: translateY(6px) scale(.97); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
@media (prefers-reduced-motion: reduce) { .wmd-panel { animation: none; } }
.wmd-head { display: flex; align-items: baseline; gap: 8px; padding: 2px 4px 6px; }
.wmd-head b { font-size: var(--fs-sm); color: var(--text); }
.wmd-head small { font-size: var(--fs-cap); color: var(--text-faint); }
.wmd-item {
  display: flex; align-items: center; gap: 8px;
  width: 100%; text-align: left; padding: 6px 7px;
  border: 0; border-radius: 7px; background: transparent; cursor: pointer; transition: background .15s ease, transform .15s ease;
}
.wmd-item:hover { background: var(--bg-hover); transform: translateX(2px); }.wmd-item:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.wmd-dot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto; background: var(--text-faint); }
.wmd-item.is-running .wmd-dot { background: var(--accent); }
.wmd-item.is-ok .wmd-dot { background: var(--green); }
.wmd-item.is-bad .wmd-dot { background: var(--danger); }
.wmd-main { flex: 1; min-width: 0; display: grid; gap: 1px; }
.wmd-main b { font-size: var(--fs-sm); color: var(--text); font-weight: 600; }
.wmd-main small { color: var(--text-faint); font-weight: 400; font-family: var(--font-mono); font-size: var(--fs-cap); }
.wmd-task { font-size: var(--fs-cap); color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wmd-state { font-style: normal; font-size: var(--fs-cap); color: var(--text-faint); flex: 0 0 auto; border: 1px solid currentColor; border-radius: 99px; padding: 1px 5px; }
.wmd-item.is-running .wmd-state { color: var(--accent); }
.wmd-item.is-ok .wmd-state { color: var(--green); }
.wmd-item.is-bad .wmd-state { color: var(--danger); }
@media (prefers-reduced-motion: reduce) { .wmd-strip, .wmd-item { transition: none; } }
</style>
