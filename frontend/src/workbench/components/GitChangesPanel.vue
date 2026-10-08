<script setup lang="ts">
// P4：仓库级「改动总览」（只读）。顶栏「更多 → 代码分析」唤起。
// 把文件树上那个小状态点（未跟踪/已改）汇总成一张跨文件清单：按仓库分组，
// 逐条列出路径 + 改动类型（冲突/修改/新增/删除/改名/未跟踪）。
// 刻意不做任何写操作（不提交、不暂存、不还原）——要还原请走 P3 的
// GitHistoryDialog（单文件历史/回滚）。这里点一条只做两件事：打开文件、关面板。
//
// 注意：各分区目录可能是独立 git 仓库（DocMind 区治），所以按仓库分组，
// 而不是假设全树只有一个 .git。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { fsApi } from '../api'
import type { GitChangesResp, GitChangeFile, GitChangeKind } from '../api'
import { useWorkbench } from '../composables/workbench'

const { changesOpen, closeChanges, openPath } = useWorkbench()

const data = ref<GitChangesResp | null>(null)
const loading = ref(false)
const error = ref('')
const filter = ref<GitChangeKind | 'all'>('all')
const collapsed = ref<Set<string>>(new Set())

async function reload() {
  if (loading.value) return   // 防重入：onMounted 与 watch 可能同帧各触发一次
  loading.value = true
  error.value = ''
  try {
    data.value = await fsApi.gitChanges()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

// 本组件是惰性挂载的（App.vue: v-if="everMounted.changes"），而 everMounted 由
// 一个 watch(changesOpen) 置位——挂载发生在 changesOpen 已经变 true **之后**。
// 所以首次挂载时 watch 的「变化」永远等不到（值早就是 true 了），不能只靠 watch 拉数据。
// 这里 onMounted 兜底：挂载那一刻若面板已打开，直接加载。
onMounted(() => {
  if (changesOpen.value) void reload()
  window.addEventListener('keydown', onKey)
})

watch(changesOpen, (open) => {
  if (open) void reload()
})

onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') closeChanges()
}

/** 过滤后仍非空的仓库（全空则整个仓库不显示）。 */
const repos = computed(() => {
  const all = data.value?.repos ?? []
  if (filter.value === 'all') return all
  return all
    .map((r) => ({ ...r, files: r.files.filter((f) => f.kind === filter.value) }))
    .filter((r) => r.files.length > 0)
})

/** 当前过滤下命中的文件总数。 */
const shown = computed(() => repos.value.reduce((n, r) => n + r.files.length, 0))

interface KindChip { key: GitChangeKind | 'all'; label: string; count: number }

/** 只列出真正出现过的类型，避免一排恒为 0 的筛选器。 */
const kindChips = computed<KindChip[]>(() => {
  const by = new Map<GitChangeKind, number>()
  for (const r of data.value?.repos ?? []) {
    for (const f of r.files) by.set(f.kind, (by.get(f.kind) ?? 0) + 1)
  }
  const order: GitChangeKind[] =
    ['conflict', 'modified', 'added', 'deleted', 'renamed', 'untracked']
  const chips: KindChip[] = [{ key: 'all', label: '全部', count: data.value?.total ?? 0 }]
  for (const k of order) {
    if (by.has(k)) {
      const sample = data.value?.repos.flatMap((r) => r.files).find((f) => f.kind === k)
      chips.push({ key: k, label: sample?.label ?? k, count: by.get(k) ?? 0 })
    }
  }
  return chips
})

// 颜色一律读皮肤令牌（--danger/--amber/--green 每套皮肤都各自解算过 AA 对比度）。
// 刻意不写 var(--red, #hex) 式兜底：兜底色在深色皮肤下会变成亮斑，
// 而且兜底命中时皮肤层是静默失效的（不报错、只是颜色不对）。
const KIND_COLOR: Record<GitChangeKind, string> = {
  conflict: 'var(--danger)',
  modified: 'var(--amber)',
  added: 'var(--green)',
  deleted: 'var(--danger)',
  renamed: 'var(--tool)',
  untracked: 'var(--text-faint)',
}

function toggle(repoKey: string) {
  const next = new Set(collapsed.value)
  if (next.has(repoKey)) next.delete(repoKey)
  else next.add(repoKey)
  collapsed.value = next
}

/** 只读动作：打开文件即关面板。改名条目打开新名（它才是磁盘上的现存文件）。 */
async function go(f: GitChangeFile) {
  closeChanges()
  await openPath(f.path_to || f.path)
}

function repoKey(dir: string) {
  return dir || '_root'
}
</script>

<template>
  <div v-if="changesOpen" class="gc-overlay wb-modal-backdrop" @mousedown.self="closeChanges">
    <section class="gc-panel wb-modal-shell" role="dialog" aria-modal="true" aria-label="改动总览">
      <header class="gc-top">
        <div class="gc-title">
          <svg width="15" height="15" viewBox="0 0 15 15" aria-hidden="true">
            <path d="M4.2 2.4 V12.6" stroke="#5d6b82" stroke-width="1.3" stroke-linecap="round" />
            <path d="M4.2 6.6 C4.2 8.6 10.8 7.2 10.8 9.4" fill="none" stroke="#5d6b82" stroke-width="1.3" stroke-linecap="round" />
            <circle cx="4.2" cy="2.4" r="1.7" fill="none" stroke="#2f6fed" stroke-width="1.2" />
            <circle cx="4.2" cy="12.6" r="1.7" fill="none" stroke="#2f6fed" stroke-width="1.2" />
            <circle cx="10.8" cy="9.4" r="1.7" fill="none" stroke="#d99a2b" stroke-width="1.2" />
          </svg>
          <h2>改动总览</h2>
          <span v-if="data" class="gc-stats">
            {{ data.total }} 处未提交改动
            <template v-if="data.repos.length"> · {{ data.repos.length }} 个仓库</template>
          </span>
          <span class="gc-ro">只读</span>
        </div>
        <button class="gc-iconbtn" title="刷新" @click="reload">
          <svg width="13" height="13" viewBox="0 0 13 13"><path d="M11 6.5 A4.5 4.5 0 1 1 6.5 2 M11 1.4 V4.2 H8.2" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
        <button class="gc-iconbtn" title="关闭（Esc）" @click="closeChanges">
          <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
        </button>
      </header>

      <div v-if="data && data.total > 0" class="gc-toolbar">
        <div class="gc-chips">
          <button
            v-for="c in kindChips"
            :key="c.key"
            :class="['gc-chip', { active: filter === c.key }]"
            @click="filter = c.key"
          >
            {{ c.label }}<i>{{ c.count }}</i>
          </button>
        </div>
      </div>

      <div class="gc-content">
        <div v-if="loading" class="gc-state"><div class="cv-spinner" /><p>正在扫描各仓库…</p></div>
        <div v-else-if="error" class="gc-state">
          <p class="gc-error">{{ error }}</p>
          <button class="gc-retry" @click="reload">重试</button>
        </div>
        <div v-else-if="!data || data.total === 0" class="gc-state">
          <p class="gc-clean">工作区干净，没有未提交的改动。</p>
        </div>
        <div v-else-if="shown === 0" class="gc-state">
          <p class="gc-faint">当前筛选下没有条目。</p>
        </div>

        <template v-else>
          <div v-for="r in repos" :key="repoKey(r.dir)" class="gc-repo">
            <button class="gc-repo-head" @click="toggle(repoKey(r.dir))">
              <svg class="gc-caret" :class="{ open: !collapsed.has(repoKey(r.dir)) }" width="10" height="10" viewBox="0 0 10 10">
                <path d="M3.5 2.5 L7 5 L3.5 7.5 Z" fill="currentColor" />
              </svg>
              <span class="gc-repo-name">{{ r.name }}</span>
              <span class="gc-branch" :title="`当前分支 ${r.branch}`">{{ r.branch }}</span>
              <em class="gc-repo-count">{{ r.files.length }}</em>
            </button>

            <p v-if="r.error" class="gc-repo-err">{{ r.error }}</p>

            <ul v-show="!collapsed.has(repoKey(r.dir))" class="gc-files">
              <li v-for="f in r.files" :key="`${f.path}|${f.xy}`">
                <button class="gc-file" :title="f.path_to ? `${f.path} → ${f.path_to}` : f.path" @click="go(f)">
                  <i class="gc-kind" :style="{ color: KIND_COLOR[f.kind], borderColor: KIND_COLOR[f.kind] }">{{ f.label }}</i>
                  <span class="gc-path">{{ f.path }}</span>
                  <template v-if="f.path_to">
                    <span class="gc-arrow">→</span>
                    <span class="gc-path gc-path-to">{{ f.path_to }}</span>
                  </template>
                </button>
              </li>
            </ul>
          </div>

          <p v-if="data.truncated" class="gc-note">
            改动过多，每仓库最多显示 500 条，仅列出前 500 条。
          </p>
          <p class="gc-note gc-note-muted">
            本面板只读。要还原某个文件，请右键文件 → 「历史版本 / 回滚」。
          </p>
        </template>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 背景 / 描边 / 圆角 / 阴影 / 遮罩色一律交给共享基类 .wb-modal-backdrop /
   .wb-modal-shell（style.css:1129+，走 --modal-* 令牌，已逐皮肤适配）。
   这里只声明基类不管的尺寸与几何 —— 不重复写颜色，避免硬编码盖过令牌。 */
.gc-overlay {
  position: fixed;
  inset: 0;
  z-index: 90;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 34px;
}
.gc-panel {
  width: min(880px, 96vw);
  height: min(680px, 88vh);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.gc-top {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}
.gc-title { display: flex; align-items: center; gap: 9px; flex: 1; min-width: 0; }
.gc-title h2 { margin: 0; font-size: 14px; font-weight: 600; }
.gc-stats { font-size: 12px; color: var(--text-faint); }
.gc-ro {
  font-size: 10px;
  color: var(--text-muted);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 5px;
  line-height: 15px;
  flex: 0 0 auto;
}
.gc-iconbtn {
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  width: 26px; height: 26px;
  border-radius: 5px;
  cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
}
.gc-iconbtn:hover { background: var(--bg-hover); color: var(--text); }
.gc-iconbtn:focus-visible, .gc-chip:focus-visible, .gc-retry:focus-visible,
.gc-repo-head:focus-visible, .gc-file:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.gc-iconbtn, .gc-chip, .gc-repo-head, .gc-file, .gc-retry { transition: color .16s ease, background .16s ease, border-color .16s ease; }
.gc-toolbar {
  flex: 0 0 auto;
  padding: 9px 16px;
  border-bottom: 1px solid var(--border);
}
.gc-chips { display: flex; gap: 6px; flex-wrap: wrap; }
.gc-chip {
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
.gc-chip i { font-style: normal; font-size: 10px; color: var(--text-faint); }
.gc-chip:hover { border-color: var(--border-strong); color: var(--text); }
/* 选中态用强调色软底：写死浅蓝在深色皮肤下会变成亮块（且会盖掉皮肤强调色）。 */
.gc-chip.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent-text, var(--accent)); }
.gc-chip.active i { color: var(--accent); }
.gc-content { flex: 1; overflow-y: auto; padding: 8px 12px 20px; }
.gc-state { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 70px 0; color: var(--text-faint); font-size: 12px; }
.gc-retry { border: 1px solid var(--border-strong); background: transparent; color: var(--text); border-radius: 5px; padding: 5px 14px; cursor: pointer; }
.gc-clean { color: var(--green); }
.gc-repo { margin: 8px 2px 0; }
.gc-repo-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: none;
  background: transparent;
  color: var(--text);
  cursor: pointer;
  border-radius: 6px;
  text-align: left;
}
.gc-repo-head:hover { background: var(--bg-hover); }
.gc-caret { color: var(--text-faint); transition: transform .12s; flex: 0 0 auto; }
.gc-caret.open { transform: rotate(90deg); }
.gc-repo-name { font-size: 12px; font-weight: 600; letter-spacing: .04em; color: var(--text-muted); flex: 0 0 auto; }
.gc-branch {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-faint);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 5px;
  line-height: 16px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.gc-repo-count { font-style: normal; font-size: 10px; color: var(--text-faint); margin-left: auto; flex: 0 0 auto; }
.gc-repo-err { margin: 2px 0 4px 26px; font-size: 11px; color: var(--danger); }
.gc-files { list-style: none; margin: 0; padding: 0 0 4px 22px; }
.gc-file {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 8px;
  border: none;
  background: transparent;
  color: var(--text);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  border-radius: 4px;
}
.gc-file:hover { background: var(--bg-selected); }
.gc-kind {
  font-style: normal;
  font-size: 10px;
  border: 1px solid;
  border-radius: 4px;
  padding: 0 5px;
  line-height: 15px;
  flex: 0 0 auto;
}
.gc-path {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.gc-path-to { color: var(--text-muted); }
.gc-arrow { color: var(--text-faint); flex: 0 0 auto; }
.gc-note { margin: 10px 6px 0; font-size: 11px; color: var(--amber); }
.gc-note-muted { color: var(--text-faint); }
/* 入场动画由基类 .wb-modal-backdrop / .wb-modal-shell 提供（wb-modal-fade/rise），
   这里不再自带 keyframes，避免与基类 animation 抢同一属性。 */
@media (max-width: 700px) {
  .gc-overlay { padding: 14px; }
  .gc-panel { width: 100%; height: min(900px, 94vh); }
  .gc-top { padding: 10px 12px; }
  .gc-stats { display: none; }
}
@media (max-width: 520px) {
  .gc-overlay { padding: 0; align-items: stretch; }
  .gc-panel { height: 100%; }
  .gc-branch { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .gc-iconbtn, .gc-chip, .gc-repo-head, .gc-file, .gc-retry, .gc-caret { animation: none; transition: none; }
}
</style>
