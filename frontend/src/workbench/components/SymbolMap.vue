<script setup lang="ts">
// P1：全局符号语义地图。顶栏按钮唤起；跨文件按分区/文件浏览全部符号，
// 支持名称/签名/文档/文件名实时搜索与种类过滤；点击符号打开文件并定位到行。
import { computed, nextTick, ref, watch } from 'vue'
import { fsApi } from '../api'
import type { SymbolMapResp, SymbolMapFile, SymbolInfo } from '../api'
import { useWorkbench } from '../composables/workbench'
import { regionColor, symbolKind } from '../theme'

const { symbolMapOpen, closeSymbolMap, jumpToLine } = useWorkbench()

const data = ref<SymbolMapResp | null>(null)
const loading = ref(false)
const error = ref('')
const query = ref('')
const activeKind = ref<string>('all')
const collapsed = ref<Set<string>>(new Set())
const searchEl = ref<HTMLInputElement | null>(null)

async function reload() {
  loading.value = true
  error.value = ''
  try {
    data.value = await fsApi.symbolMap()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

watch(symbolMapOpen, (open) => {
  if (open) {
    void reload()
    void nextTick(() => searchEl.value?.focus())
    window.addEventListener('keydown', onKey)
  } else {
    window.removeEventListener('keydown', onKey)
  }
})

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') closeSymbolMap()
}

interface KindChip { key: string; label: string; count: number }

const kindChips = computed<KindChip[]>(() => {
  const by = data.value?.stats.by_kind ?? {}
  const order = ['function', 'class', 'signal', 'enum', 'const', 'var', 'node', 'resource', 'section']
  const present = new Set([...Object.keys(by)])
  return [
    { key: 'all', label: '全部', count: data.value?.stats.symbols ?? 0 },
    ...order.filter((k) => present.has(k)).map((k) => ({
      key: k,
      label: symbolKind(k).label,
      count: by[k] ?? 0,
    })),
  ]
})

interface MatchFile { f: SymbolMapFile; syms: SymbolInfo[] }

function matchSymbol(f: SymbolMapFile, s: SymbolInfo, q: string): boolean {
  if (activeKind.value !== 'all' && s.kind !== activeKind.value) return false
  if (!q) return true
  const hay = `${s.name} ${s.signature} ${s.doc} ${s.detail} ${f.rel} ${f.class_name} ${f.extends} ${f.doc}`.toLowerCase()
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w))
}

const grouped = computed(() => {
  if (!data.value) return [] as Array<{ key: string; name: string; files: MatchFile[] }>
  const q = query.value.trim()
  const groups = new Map<string, { key: string; name: string; files: MatchFile[] }>()
  for (const f of data.value.files) {
    const syms = f.symbols.filter((s) => matchSymbol(f, s, q))
    if (q && syms.length === 0) continue
    if (!q && activeKind.value !== 'all' && syms.length === 0) continue
    const key = f.region || '_root'
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        name: f.region ? (f.region_name || f.region) : '未分区 / 根目录',
        files: [],
      })
    }
    groups.get(key)!.files.push({ f, syms })
  }
  return [...groups.values()].sort((a, b) => (a.key === '_root' ? -1 : a.name.localeCompare(b.name, 'zh-CN')))
})

const matchCount = computed(() =>
  grouped.value.reduce((n, g) => n + g.files.reduce((m, ff) => m + ff.syms.length, 0), 0),
)

function toggle(rel: string) {
  const next = new Set(collapsed.value)
  if (next.has(rel)) next.delete(rel)
  else next.add(rel)
  collapsed.value = next
}

async function go(f: SymbolMapFile, s: SymbolInfo) {
  closeSymbolMap()
  await jumpToLine(f.rel, s.start)
}

function kindOf(kind: string) {
  return symbolKind(kind)
}
</script>

<template>
  <div v-if="symbolMapOpen" class="sm-overlay wb-modal-backdrop" @mousedown.self="closeSymbolMap">
    <section class="sm-panel wb-modal-shell" role="dialog" aria-modal="true" aria-label="符号语义地图">
      <header class="sm-top">
        <div class="sm-title">
          <svg width="15" height="15" viewBox="0 0 15 15" aria-hidden="true">
            <circle cx="4" cy="4" r="1.6" fill="none" stroke="#bc8cff" stroke-width="1.1" />
            <circle cx="11" cy="3.5" r="1.6" fill="none" stroke="#58a6ff" stroke-width="1.1" />
            <circle cx="9.5" cy="11.5" r="1.6" fill="none" stroke="#45c98c" stroke-width="1.1" />
            <path d="M5.3 4.8 L9.6 3.9 M5 5.2 L8.5 10.4 M10.2 5 L9.9 9.9" stroke="#3a4658" stroke-width="0.9" />
          </svg>
          <h2>符号语义地图</h2>
          <span v-if="data" class="sm-stats">
            {{ data.stats.files }} 个文件 · {{ data.stats.symbols }} 个符号
          </span>
        </div>
        <button class="sm-iconbtn" title="刷新" @click="reload">
          <svg width="13" height="13" viewBox="0 0 13 13"><path d="M11 6.5 A4.5 4.5 0 1 1 6.5 2 M11 1.4 V4.2 H8.2" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
        <button class="sm-iconbtn" title="关闭（Esc）" @click="closeSymbolMap">
          <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
        </button>
      </header>

      <div class="sm-toolbar">
        <div class="sm-search">
          <svg width="12" height="12" viewBox="0 0 12 12"><circle cx="5.2" cy="5.2" r="3.4" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M7.8 7.8 L10.5 10.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
          <input ref="searchEl" v-model="query" type="text" placeholder="搜索符号 / 签名 / 文档 / 文件名（空格分隔，多词同时命中）" />
          <kbd v-if="query" class="sm-clear" @click="query = ''">清空</kbd>
        </div>
        <div class="sm-chips">
          <button
            v-for="c in kindChips"
            :key="c.key"
            :class="['sm-chip', { active: activeKind === c.key }]"
            @click="activeKind = c.key"
          >
            {{ c.label }}<i>{{ c.count }}</i>
          </button>
        </div>
      </div>

      <div class="sm-content">
        <div v-if="loading" class="sm-state"><div class="cv-spinner" /><p>正在构建符号地图…</p></div>
        <div v-else-if="error" class="sm-state">
          <p class="sm-error">{{ error }}</p>
          <button class="sm-retry" @click="reload">重试</button>
        </div>
        <div v-else-if="!data || data.files.length === 0" class="sm-state">
          <p class="sm-faint">代码库中暂无可索引文件。</p>
        </div>
        <div v-else-if="matchCount === 0 && !grouped.length" class="sm-state">
          <p class="sm-faint">没有匹配「{{ query }}」的符号。</p>
        </div>

        <template v-else>
          <div v-for="g in grouped" :key="g.key" class="sm-group">
            <div class="sm-group-head">
              <i class="sm-group-dot" :style="{ background: regionColor(g.key === '_root' ? '' : g.key) }" />
              <span>{{ g.name }}</span>
              <em>{{ g.files.length }}</em>
            </div>

            <div v-for="mf in g.files" :key="mf.f.rel" class="sm-file"
                 :class="{ 'is-onlymatch': query && mf.syms.length > 0 }">
              <button class="sm-file-head" @click="toggle(mf.f.rel)">
                <svg class="sm-caret" :class="{ open: !collapsed.has(mf.f.rel) }" width="10" height="10" viewBox="0 0 10 10">
                  <path d="M3.5 2.5 L7 5 L3.5 7.5 Z" fill="currentColor" />
                </svg>
                <i v-if="mf.f.region" class="sm-region-tag"
                   :style="{ borderColor: regionColor(mf.f.region) }">
                  {{ mf.f.region_name }}
                </i>
                <span class="sm-file-name">{{ mf.f.rel }}</span>
                <span v-if="mf.f.class_name" class="sm-file-class">{{ mf.f.class_name }}</span>
                <span v-else-if="mf.f.extends" class="sm-file-class">extends {{ mf.f.extends }}</span>
                <span v-if="mf.f.doc" class="sm-file-doc" :title="mf.f.doc">{{ mf.f.doc }}</span>
                <em v-if="mf.syms.length" class="sm-file-count">{{ mf.syms.length }}</em>
              </button>

              <ul v-show="!collapsed.has(mf.f.rel) || query" class="sm-syms">
                <li v-for="s in mf.syms" :key="`${s.start}-${s.name}`">
                  <button class="sm-sym" @click="go(mf.f, s)" :title="s.doc || s.signature">
                    <i class="sm-sym-mark">{{ kindOf(s.kind).mark }}</i>
                    <span class="sm-sym-name">{{ s.name }}</span>
                    <span v-if="s.detail" class="sm-sym-detail">{{ s.detail }}</span>
                    <span v-else-if="s.kind === 'function' && s.signature" class="sm-sym-detail">{{ s.signature }}</span>
                    <span v-if="s.doc" class="sm-sym-doc">{{ s.doc }}</span>
                    <span class="sm-sym-ln">L{{ s.start }}<template v-if="s.end !== s.start">–{{ s.end }}</template></span>
                  </button>
                </li>
                <li v-if="!mf.syms.length && !query" class="sm-sym-empty">数据/配置文件，无符号结构</li>
              </ul>
            </div>
          </div>
        </template>
      </div>
    </section>
  </div>
</template>

<style scoped>
.sm-overlay {
  position: fixed;
  inset: 0;
  background: rgba(38, 52, 77, 0.38);
  backdrop-filter: blur(2px);
  z-index: 90;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 34px;
  animation: sm-overlay-in .18s ease-out both;
}
.sm-panel {
  width: min(1040px, 96vw);
  height: min(720px, 88vh);
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  box-shadow: 0 24px 70px rgba(35, 52, 84, 0.16);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: sm-panel-in .22s cubic-bezier(.2,.8,.2,1) both;
}
.sm-top {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}
.sm-title { display: flex; align-items: center; gap: 9px; flex: 1; }
.sm-title h2 { margin: 0; font-size: 14px; font-weight: 600; }
.sm-stats { font-size: 12px; color: var(--text-faint); }
.sm-iconbtn {
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  width: 26px; height: 26px;
  border-radius: 5px;
  cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
}
.sm-iconbtn:hover { background: var(--bg-hover); color: var(--text); }
.sm-iconbtn:focus-visible, .sm-chip:focus-visible, .sm-clear:focus-visible, .sm-retry:focus-visible, .sm-file-head:focus-visible, .sm-sym:focus-visible { outline: 2px solid #2f6fed88; outline-offset: 2px; }
.sm-iconbtn, .sm-chip, .sm-file-head, .sm-sym, .sm-retry { transition: color .16s ease, background .16s ease, border-color .16s ease, transform .16s ease; }
.sm-toolbar {
  flex: 0 0 auto;
  padding: 10px 16px 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-bottom: 1px solid var(--border);
}
.sm-search {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  padding: 0 10px;
  height: 32px;
  color: var(--text-faint);
}
.sm-search:focus-within { border-color: #2f6fed66; box-shadow: 0 0 0 2px #2f6fed18; }
.sm-search input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text);
  font-size: 12px;
  font-family: inherit;
}
.sm-clear {
  font-style: normal;
  font-size: 10px;
  color: var(--text-muted);
  cursor: pointer;
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 6px;
}
.sm-clear:hover { color: var(--text); }
.sm-chips { display: flex; gap: 6px; flex-wrap: wrap; }
.sm-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  font-size: 12px;
  padding: 2px 9px;
  border-radius: 11px;
  cursor: pointer;
}
.sm-chip i { font-style: normal; font-size: 10px; color: var(--text-faint); }
.sm-chip:hover { border-color: var(--border-strong); color: var(--text); }
.sm-chip.active { background: #e4edfc; border-color: #2560d4; color: #1b3c73; }
.sm-chip.active i { color: #2560d4; }
.sm-content { flex: 1; overflow-y: auto; padding: 8px 10px 20px; }
.sm-state { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 70px 0; color: var(--text-faint); font-size: 12px; }
.sm-retry { border: 1px solid var(--border-strong); background: transparent; color: var(--text); border-radius: 5px; padding: 5px 14px; cursor: pointer; }
.sm-group { margin: 10px 6px 0; }
.sm-group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  padding: 4px 6px;
  position: sticky;
  top: 0;
  background: linear-gradient(180deg, var(--bg-raised) 70%, transparent);
  z-index: 1;
}
.sm-group-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.sm-group-head em { font-style: normal; font-size: 10px; color: var(--text-faint); }
.sm-file { margin: 2px 0 2px 14px; border-left: 1px solid var(--border); padding-left: 4px; }
.sm-file-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  border: none;
  background: transparent;
  color: var(--text);
  cursor: pointer;
  border-radius: 0 5px 5px 0;
  text-align: left;
}
.sm-file-head:hover { background: var(--bg-hover); }
.sm-caret { color: var(--text-faint); transition: transform 0.12s; flex: 0 0 auto; }
.sm-caret.open { transform: rotate(90deg); }
.sm-region-tag {
  font-style: normal;
  font-size: 10px;
  border: 1px solid;
  color: var(--text);
  border-radius: 4px;
  padding: 0 5px;
  line-height: 15px;
  flex: 0 0 auto;
}
.sm-file-name { font-family: var(--font-mono); font-size: 12px; color: var(--text); flex: 0 0 auto; }
.sm-file-class { font-family: var(--font-mono); font-size: 11px; color: var(--accent-text); flex: 0 0 auto; }
.sm-file-doc {
  font-size: 11px;
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.sm-file-count { font-style: normal; font-size: 10px; color: var(--text-faint); margin-left: auto; flex: 0 0 auto; }
.sm-syms { list-style: none; margin: 0; padding: 0 0 4px 26px; }
.sm-sym {
  width: 100%;
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 2.5px 8px;
  border: none;
  background: transparent;
  color: var(--text);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  border-radius: 4px;
}
.sm-sym:hover { background: var(--bg-selected); }
.sm-sym:active, .sm-file-head:active { transform: translateX(1px); }
.sm-sym-mark {
  font-style: normal;
  font-family: var(--font-mono);
  font-weight: 700;
  font-size: 10px;
  /* 类别色相原来直接当 10px 字色（paper 实测 2.04:1）。字形本身就是类别记号
     （R / ▣ / x / f），色相只是第二线索，所以字回归中性，色相留给分区标签的描边。 */
  color: var(--text);
  width: 12px;
  flex: 0 0 12px;
  align-self: center;
  text-align: center;
}
.sm-sym-name { font-family: var(--font-mono); color: var(--text); flex: 0 0 auto; align-self: center; }
.sm-sym-detail {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.sm-sym-doc {
  font-size: 11px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 60px;
  flex: 1 1 auto;
}
.sm-sym-ln { font-family: var(--font-mono); font-size: 10px; color: var(--text-faint); margin-left: auto; flex: 0 0 auto; align-self: center; }
.sm-sym:hover .sm-sym-ln { color: var(--accent); }
.sm-sym-empty { font-size: 11px; color: var(--text-faint); padding: 2px 8px 4px; list-style: none; }
@keyframes sm-overlay-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes sm-panel-in { from { opacity: 0; transform: translateY(10px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
@media (max-width: 700px) {
  .sm-overlay { padding: 14px; }
  .sm-panel { width: 100%; height: min(900px, 94vh); }
  .sm-top { padding: 10px 12px; }
  .sm-stats { display: none; }
  .sm-toolbar { padding: 8px 12px; }
  .sm-chips { overflow-x: auto; flex-wrap: nowrap; padding-bottom: 2px; }
}
@media (max-width: 520px) {
  .sm-overlay { padding: 0; align-items: stretch; }
  .sm-panel { height: 100%; border-radius: 0; border-left: 0; border-right: 0; }
  .sm-content { padding-inline: 6px; }
  .sm-file-doc { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .sm-overlay, .sm-panel, .sm-iconbtn, .sm-chip, .sm-file-head, .sm-sym, .sm-retry, .sm-caret { animation: none; transition: none; }
}
</style>
