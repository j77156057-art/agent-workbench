<script setup lang="ts">
// P1：当前文件符号大纲。路径切换/保存后拉取 /api/fs/symbols，点击跳转对应行。
import { computed, ref, watch } from 'vue'
import { fsApi } from '../api'
import type { SymbolsResp, SymbolInfo } from '../api'
import { useWorkbench } from '../composables/workbench'
import { symbolKind } from '../theme'

const { activeTab, jumpToLine } = useWorkbench()

const env = ref<SymbolsResp | null>(null)
const loading = ref(false)
const collapsed = ref(false)
let reqSeq = 0

async function reload(path: string | null) {
  if (!path) {
    env.value = null
    return
  }
  const seq = ++reqSeq
  loading.value = true
  try {
    const data = await fsApi.symbols(path)
    if (seq === reqSeq) env.value = data
  } catch {
    if (seq === reqSeq) env.value = null
  } finally {
    if (seq === reqSeq) loading.value = false
  }
}

watch(
  () => [activeTab.value?.path, activeTab.value?.savedAt] as const,
  ([p]) => void reload(p ?? null),
  { immediate: true },
)

const symbols = computed<SymbolInfo[]>(() => env.value?.symbols ?? [])
const hasStructure = computed(() => !!env.value && (env.value.symbols.length > 0
  || !!env.value.class_name || !!env.value.extends || !!env.value.doc))

function meta(kind: string) {
  return symbolKind(kind)
}

function go(s: SymbolInfo) {
  if (activeTab.value) void jumpToLine(activeTab.value.path, s.start)
}
</script>

<template>
  <aside v-if="activeTab" :class="['so-panel', { 'is-collapsed': collapsed }]">
    <header class="so-head">
      <template v-if="!collapsed">
        <span class="so-title">符号大纲</span>
        <span v-if="symbols.length" class="so-count">{{ symbols.length }}</span>
        <button class="so-collapse" title="收起面板" @click="collapsed = true">
          <svg width="11" height="11" viewBox="0 0 11 11"><path d="M7 2 L4 5.5 L7 9" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
      </template>
      <button v-else class="so-expand" title="展开符号大纲" @click="collapsed = false">
        <svg width="11" height="11" viewBox="0 0 11 11"><path d="M4 2 L7 5.5 L4 9" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
    </header>

    <div v-if="!collapsed" class="so-body">
      <div v-if="loading && !env" class="so-hint">正在解析…</div>

      <template v-else-if="hasStructure">
        <div v-if="env?.class_name || env?.extends || env?.doc" class="so-classcard">
          <div v-if="env?.class_name" class="so-class-name">
            <i class="so-mark" :style="{ color: meta('class').color }">C</i>
            {{ env.class_name }}
          </div>
          <div v-if="env?.extends" class="so-extends">
            extends <code>{{ env.extends }}</code>
          </div>
          <div v-if="env?.doc" class="so-doc" :title="env.doc">{{ env.doc }}</div>
        </div>

        <ul class="so-list">
          <li v-for="s in symbols" :key="`${s.start}-${s.name}`">
            <button
              :class="['so-item', `so-kind-${s.kind}`]"
              :style="{ paddingLeft: (s.parent ? 26 : 10) + 'px' }"
              :title="s.doc || s.signature"
              @click="go(s)"
            >
              <i class="so-mark" :style="{ color: meta(s.kind).color }">{{ meta(s.kind).mark }}</i>
              <span class="so-name">{{ s.name }}</span>
              <span class="so-ln">{{ s.start }}</span>
            </button>
          </li>
        </ul>
      </template>

      <div v-else class="so-hint">
        {{ loading ? '正在解析…' : '该文件类型暂无符号结构（数据/配置文件）' }}
      </div>
    </div>
  </aside>
</template>

<style scoped>
.so-panel {
  flex: 0 0 208px;
  width: 208px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-raised);
  border-left: 1px solid var(--border);
  overflow: hidden;
}
.so-panel.is-collapsed {
  flex-basis: 28px;
  width: 28px;
}
.so-head {
  height: 34px;
  flex: 0 0 34px;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 8px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-raised);
}
.so-title {
  font-size: var(--fs-sm);
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--text-muted);
}
.so-count {
  font-size: var(--fs-cap);
  color: var(--text-faint);
  background: var(--bg-hover);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0 6px;
  line-height: 15px;
}
.so-collapse, .so-expand {
  margin-left: auto;
  border: none;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  padding: 4px;
  border-radius: 4px;
  display: inline-flex;
}
.so-expand { margin: 0 auto; }
.so-collapse:hover, .so-expand:hover { color: var(--text); background: var(--bg-hover); }
.so-collapse:focus-visible, .so-expand:focus-visible, .so-item:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: -2px; }
.so-collapse, .so-expand, .so-item { transition: color .16s ease, background .16s ease, transform .16s ease; }
.so-body { flex: 1; overflow-y: auto; padding: 6px 0 12px; }
.so-hint {
  padding: 12px 10px;
  font-size: var(--fs-sm);
  line-height: 1.6;
  color: var(--text-faint);
}
.so-classcard {
  margin: 4px 8px 8px;
  padding: 7px 9px;
  border: 1px solid var(--border);
  border-left: 2px solid color-mix(in srgb, var(--accent) 47%, transparent);
  border-radius: 5px;
  background: color-mix(in srgb, var(--accent) 6%, transparent);
}
.so-class-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--viz-inherit-t);
}
.so-extends {
  margin-top: 3px;
  font-size: var(--fs-cap);
  color: var(--text-muted);
  font-family: var(--font-mono);
}
.so-extends code { color: var(--accent); }
.so-doc {
  margin-top: 4px;
  font-size: var(--fs-cap);
  line-height: 1.5;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.so-list { list-style: none; margin: 0; padding: 0; }
.so-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 3px 10px;
  border: none;
  background: transparent;
  color: var(--text);
  font-size: var(--fs-sm);
  text-align: left;
  cursor: pointer;
  line-height: 1.5;
}
.so-item:hover { background: var(--bg-hover); }
.so-item:hover .so-ln { color: var(--text-muted); }
.so-item:active { transform: translateX(1px); }
.so-mark {
  font-style: normal;
  font-family: var(--font-mono);
  font-size: var(--fs-cap);
  font-weight: 700;
  width: 13px;
  text-align: center;
  flex: 0 0 13px;
}
.so-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
}
.so-kind-group .so-name { color: var(--text-faint); font-size: var(--fs-cap); }
.so-ln {
  font-family: var(--font-mono);
  font-size: var(--fs-cap);
  color: var(--text-faint);
  flex: 0 0 auto;
}
@media (max-width: 760px) {
  .so-panel { flex-basis: 180px; width: 180px; }
  .so-panel.is-collapsed { flex-basis: 28px; width: 28px; }
}
@media (prefers-reduced-motion: reduce) {
  .so-collapse, .so-expand, .so-item { transition: none; }
}
</style>
