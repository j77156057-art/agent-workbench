<script setup lang="ts">
// P3：分区可视化。上方为依赖方向 DAG（dep → 依赖方，按依赖深度分层），
// 下方为每区状态卡（目录/独立 git/分支/脏标记/文件数/依赖/导出/校验器），
// 顶部横幅展示契约校验结果（依赖无环、导出文件存在）。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useWorkbench, askConfirm } from '../composables/workbench'
import { appEvents } from '../eventBus'
import { withProject } from '../api'
import { regionColor, cat, catAlpha } from '../theme'
import type { RegionInfo } from '../api'

const {
  regionMapOpen, regionMap, openRegionMap, closeRegionMap, locateRegion,
  busyRegionKey, createRegion, fillRegionExports,
} = useWorkbench()

const selectedKey = ref<string | null>(null)
const adding = ref(false), regionKey = ref(''), regionName = ref(''), regionDir = ref(''), addError = ref(''), addBusy = ref(false)
async function addRegion() {
  addError.value = ''
  if (!/^[a-zA-Z][a-zA-Z0-9_-]*$/.test(regionKey.value)) { addError.value = '标识请使用英文字母开头，可含数字、下划线。'; return }
  if (regionMap.value.regions.some(r => r.key === regionKey.value)) { addError.value = '此分区标识已存在，请使用新的标识。'; return }
  const dir = regionDir.value.trim() || regionKey.value
  if (/^[\\/]|:/.test(dir) || dir.replace(/\\/g, '/').split('/').some(p => p === '..' || p === '.')) {
    addError.value = '目录必须是项目内的相对路径，不能包含 . 或 ..。'; return
  }
  if (!await askConfirm({ title: '新增项目分区', message: `新增「${regionName.value || regionKey.value}」，目录：${dir}。将更新分区配置、初始化目录及 Git。`, confirmText: '新增分区' })) return
  addBusy.value = true
  try {
    async function post(url: string, body: unknown) {
      const response = await fetch(url, withProject({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }))
      const result = await response.json()
      if (!response.ok || !result.ok) throw new Error(result.error || '分区操作失败')
      return result
    }
    await post('/api/approval', { action: 'apply_regions', target: '*', user: 'workbench-user', approved: true })
    await post('/api/add_region', { key: regionKey.value, dir, name: regionName.value })
    adding.value = false
    await openRegionMap()
    await useWorkbench().loadTree()
  } catch (e) { addError.value = (e as Error).message }
  finally { addBusy.value = false }
}
function askAiForRegions() {
  closeRegionMap()
  appEvents.emit('docmind:focus-chat', { q: '请读取当前项目结构和现有分区，提出适合此项目的分区调整方案。先展示依据、建议目录和影响范围，等待我审核再修改。' })
}

const byKey = computed(() => new Map(regionMap.value.regions.map((r) => [r.key, r])))

function depName(key: string): string {
  return byKey.value.get(key)?.name || key
}

// ---------------------------------------------------------------- DAG 分层布局
const NODE_W = 168
const NODE_H = 46
const GAP_X = 40
const GAP_Y = 16
const PAD = 10

interface LaidEdge { x1: number; y1: number; x2: number; y2: number }
interface DagLayout {
  pos: Map<string, { x: number; y: number }>
  edges: LaidEdge[]
  order: string[]
  width: number
  height: number
}

const layout = computed<DagLayout>(() => {
  const regs = regionMap.value.regions
  const present = new Set(regs.map((r) => r.key))
  const depthCache = new Map<string, number>()
  const depthOf = (k: string, stack: Set<string>): number => {
    const cached = depthCache.get(k)
    if (cached != null) return cached
    if (stack.has(k)) return 0 // 环保护（契约会报错）
    const r = byKey.value.get(k)
    const deps = (r?.depends_on || []).filter((d) => present.has(d))
    const d = deps.length
      ? 1 + Math.max(...deps.map((x) => depthOf(x, new Set(stack).add(k))))
      : 0
    depthCache.set(k, d)
    return d
  }

  const cols: string[][] = []
  for (const r of regs) {
    // 初始栈必须为空：环检测发生在递归下钻时（栈里放的是祖先），
    // 若把节点自身放入初始栈，守卫会立即命中导致所有节点深度恒为 0。
    const d = depthOf(r.key, new Set())
    ;(cols[d] ||= []).push(r.key)
  }
  cols.forEach((keys) => keys.sort())

  const pos = new Map<string, { x: number; y: number }>()
  let maxRows = 0
  cols.forEach((keys, ci) => {
    maxRows = Math.max(maxRows, keys.length)
    keys.forEach((k, ri) => {
      pos.set(k, { x: PAD + ci * (NODE_W + GAP_X), y: PAD + ri * (NODE_H + GAP_Y) })
    })
  })

  const edges: LaidEdge[] = []
  for (const r of regs) {
    for (const dep of r.depends_on || []) {
      const a = pos.get(dep)
      const b = pos.get(r.key)
      if (a && b) edges.push({ x1: a.x + NODE_W, y1: a.y + NODE_H / 2, x2: b.x, y2: b.y + NODE_H / 2 })
    }
  }

  const width = PAD * 2 + Math.max(1, cols.length) * NODE_W + Math.max(0, cols.length - 1) * GAP_X
  const height = PAD * 2 + Math.max(1, maxRows) * NODE_H + Math.max(0, maxRows - 1) * GAP_Y
  return { pos, edges, order: cols.flat(), width, height }
})

// 卡片排序与 DAG 分层同语义：先按依赖深度（列），同列按 key 字典序
const orderedRegions = computed<RegionInfo[]>(() =>
  layout.value.order.map((k) => byKey.value.get(k)).filter((r): r is RegionInfo => !!r),
)

function nodePos(key: string) {
  return layout.value.pos.get(key) || { x: 0, y: 0 }
}
function edgePath(e: LaidEdge): string {
  const mx = (e.x1 + e.x2) / 2
  return `M ${e.x1} ${e.y1} C ${mx} ${e.y1}, ${mx} ${e.y2}, ${e.x2 - 2} ${e.y2}`
}

function selectNode(key: string) {
  selectedKey.value = key
  document.getElementById(`rm-card-${key}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
}

function verifyText(r: RegionInfo): string {
  const v = (r.verify || '').trim()
  if (!v) return '未配置校验'
  if (v === 'builtin:py') return 'Python 语法检查'
  if (v === 'builtin:json') return 'JSON/TOML 校验'
  return v
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') closeRegionMap()
}
watch(regionMapOpen, (open) => {
  if (open) {
    selectedKey.value = null
    window.addEventListener('keydown', onKey)
  } else window.removeEventListener('keydown', onKey)
  // immediate：组件在首次打开时才由 App 异步挂载，挂载即 open=true，需立即绑键
}, { immediate: true })
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div v-if="regionMapOpen" class="rm-overlay wb-modal-backdrop" @mousedown.self="closeRegionMap">
    <section class="rm-panel wb-modal-shell" role="dialog" aria-modal="true" aria-label="分区可视化">
      <header class="rm-top">
        <div class="rm-title">
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
            <rect x="1.2" y="2" width="4.6" height="3.4" rx="0.8" fill="none" style="stroke: var(--viz-class)" stroke-width="1" />
            <rect x="8.4" y="2" width="4.6" height="3.4" rx="0.8" fill="none" style="stroke: var(--viz-const)" stroke-width="1" />
            <rect x="4.8" y="9.2" width="4.6" height="3.4" rx="0.8" fill="none" style="stroke: var(--viz-fn)" stroke-width="1" />
            <path d="M5.8 3.7 L8.4 3.7 M3.5 5.4 L6 9.2 M10.7 5.4 L8.2 9.2" style="stroke: var(--viz-link)" stroke-width="0.9" />
          </svg>
          <h2>分区可视化</h2>
          <span class="rm-root" :title="regionMap.codeRoot">{{ regionMap.codeRoot }}</span>
        </div>
        <div class="rm-top-actions">
          <button class="rm-refresh" @click="adding = !adding">新增分区</button>
          <button class="rm-refresh" @click="askAiForRegions">让 AI 规划分区</button>
          <button class="rm-refresh" :disabled="regionMap.loading" @click="void openRegionMap()">
            <svg width="12" height="12" viewBox="0 0 12 12" :class="{ spinning: regionMap.loading }">
              <path d="M10.2 6 A4.2 4.2 0 1 1 6 1.8 A4.2 4.2 0 0 1 9.6 3.4 M9.6 1.4 V3.4 H7.6"
                    fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            刷新
          </button>
          <button class="rm-iconbtn" title="关闭（Esc）" @click="closeRegionMap">
            <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /></svg>
          </button>
        </div>
      </header>

      <div v-if="regionMap.loading && !regionMap.regions.length" class="rm-state"><div class="cv-spinner" /><p>正在读取分区状态…</p></div>
      <div v-else-if="regionMap.error" class="rm-state"><p class="rm-error">{{ regionMap.error }}</p></div>

      <div v-else class="rm-body">
        <form v-if="adding" class="rm-add" @submit.prevent="addRegion">
          <label>分区名称<input v-model="regionName" placeholder="例如：任务系统" /></label>
          <label>唯一标识<input v-model="regionKey" placeholder="quests" required /></label>
          <label>项目内目录<input v-model="regionDir" placeholder="默认使用标识" /></label>
          <button :disabled="addBusy" class="rm-refresh">{{ addBusy ? '创建中…' : '确认新增' }}</button>
          <p v-if="addError" role="alert">{{ addError }}</p>
        </form>
        <!-- 契约校验横幅 -->
        <div class="rm-contract" :class="regionMap.contracts?.ok ? 'ok' : 'bad'">
          <template v-if="regionMap.contracts">
            <svg v-if="regionMap.contracts.ok" width="13" height="13" viewBox="0 0 13 13">
              <circle cx="6.5" cy="6.5" r="5.4" fill="none" style="stroke: var(--green)" stroke-width="1" />
              <path d="M4 6.6 L5.8 8.3 L9 4.8" fill="none" style="stroke: var(--green)" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            <svg v-else width="13" height="13" viewBox="0 0 13 13">
              <circle cx="6.5" cy="6.5" r="5.4" fill="none" style="stroke: var(--danger)" stroke-width="1" />
              <path d="M6.5 3.4 V7.2" style="stroke: var(--danger)" stroke-width="1.2" stroke-linecap="round" />
              <circle cx="6.5" cy="9.2" r="0.7" style="fill: var(--danger)" />
            </svg>
            <div class="rm-contract-text">
              <strong>{{ regionMap.contracts.ok ? '契约校验通过' : `契约校验发现 ${regionMap.contracts.errors.length} 个问题` }}</strong>
              <ul v-if="!regionMap.contracts.ok">
                <li v-for="(err, i) in regionMap.contracts.errors" :key="i">{{ err }}</li>
              </ul>
            </div>
          </template>
          <span v-else class="rm-contract-off">契约校验不可用：{{ regionMap.contractsError }}</span>
        </div>

        <!-- 依赖方向 DAG -->
        <section class="rm-section">
          <h3>依赖方向 <em>箭头由被依赖分区指向依赖方（只允许单向依赖）</em></h3>
          <div class="rm-dag-scroll">
            <svg :width="layout.width" :height="layout.height" class="rm-dag">
              <defs>
                <marker id="rm-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M0 0 L8 4 L0 8 Z" style="fill: var(--viz-dag-edge)" />
                </marker>
              </defs>
              <path
                v-for="(e, i) in layout.edges" :key="i"
                :d="edgePath(e)" fill="none" style="stroke: var(--viz-dag-edge)" stroke-width="1.1"
                marker-end="url(#rm-arrow)"
              />
              <g
                v-for="r in regionMap.regions" :key="r.key"
                :transform="`translate(${nodePos(r.key).x},${nodePos(r.key).y})`"
                class="rm-node"
                :class="{ missing: !r.exists, selected: selectedKey === r.key }"
                @click="selectNode(r.key)"
              >
                <rect
                  :width="NODE_W" :height="NODE_H" rx="7"
                  :style="{ stroke: cat(regionColor(r.key)), fill: catAlpha(regionColor(r.key), 8) }"
                  stroke-width="1.1"
                />
                <circle cx="11" cy="15" r="3.4" :fill="regionColor(r.key)" />
                <text x="20" y="19" class="rm-node-name">{{ r.name }}</text>
                <text x="11" y="36" class="rm-node-dir">{{ r.dir }}/</text>
              </g>
            </svg>
          </div>
        </section>

        <!-- 分区状态卡 -->
        <section class="rm-section">
          <h3>分区状态 <em>点击卡片定位目录 · 未创建可生成 · 缺导出可补齐</em></h3>
          <div class="rm-cards">
            <article
              v-for="r in orderedRegions" :key="r.key"
              :id="`rm-card-${r.key}`"
              class="rm-card"
              :class="{ missing: !r.exists, selected: selectedKey === r.key }"
              @click="r.exists && locateRegion(r.dir)"
              :title="r.exists ? `在文件树中定位 ${r.dir}/` : '该分区目录尚未创建'"
            >
              <span class="rm-card-bar" :style="{ background: regionColor(r.key) }" />
              <div class="rm-card-main">
                <div class="rm-card-head">
                  <span class="rm-card-name">
                    <i class="rm-card-dot" :style="{ background: regionColor(r.key) }" />{{ r.name }}
                  </span>
                  <code class="rm-card-dir">{{ r.dir }}/</code>
                </div>
                <p class="rm-card-desc">{{ r.desc || '（无描述）' }}</p>
                <div class="rm-card-badges">
                  <span v-if="!r.exists" class="rm-badge rm-badge-missing">目录未创建</span>
                  <template v-else>
                    <span class="rm-badge rm-badge-files">{{ r.files }} 个文件</span>
                    <span v-if="r.git" class="rm-badge rm-badge-git">
                      git · {{ r.branch || '?' }}
                    </span>
                    <span v-else class="rm-badge rm-badge-nogit">无独立仓库</span>
                    <span v-if="r.missing_exports.length" class="rm-badge rm-badge-missing-export">
                      缺 {{ r.missing_exports.length }} 个导出
                    </span>
                    <span v-if="r.dirty" class="rm-badge rm-badge-dirty">有未提交改动</span>
                  </template>
                </div>
                <div class="rm-card-meta">
                  <span class="rm-meta-label">依赖</span>
                  <span v-if="!r.depends_on.length" class="rm-mata-faint">无（基础分区）</span>
                  <span v-for="dep in r.depends_on" :key="dep" class="rm-chip">
                    <i :style="{ background: regionColor(dep) }" />{{ depName(dep) }}
                  </span>
                </div>
                <div class="rm-card-meta">
                  <span class="rm-meta-label">导出</span>
                  <span v-if="!r.exports.length" class="rm-mata-faint">无对外接口文件</span>
                  <template v-for="ex in r.exports" :key="ex">
                    <code
                      class="rm-export"
                      :class="{ missing: r.missing_exports.includes(ex) }"
                      :title="r.missing_exports.includes(ex) ? '该导出文件尚不存在，可一键补齐' : ''"
                    >{{ ex }}</code>
                  </template>
                </div>
                <div class="rm-card-meta">
                  <span class="rm-meta-label">校验</span>
                  <span class="rm-mata-faint">{{ verifyText(r) }}</span>
                </div>
                <div v-if="!r.exists" class="rm-card-actions">
                  <button
                    class="rm-create-btn"
                    :disabled="busyRegionKey !== null"
                    @click.stop="void createRegion(r)"
                  >
                    <span v-if="busyRegionKey === r.key" class="rm-btn-spin" />
                    <svg v-else width="11" height="11" viewBox="0 0 11 11" aria-hidden="true">
                      <path d="M5.5 1.5 V9.5 M1.5 5.5 H9.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
                    </svg>
                    {{ busyRegionKey === r.key ? '创建中…' : '创建目录与文件' }}
                  </button>
                </div>
                <div v-else-if="r.missing_exports.length" class="rm-card-actions">
                  <button
                    class="rm-fill-btn"
                    :disabled="busyRegionKey !== null"
                    @click.stop="void fillRegionExports(r)"
                  >
                    <span v-if="busyRegionKey === r.key" class="rm-btn-spin rm-btn-spin-amber" />
                    <svg v-else width="11" height="11" viewBox="0 0 11 11" aria-hidden="true">
                      <path d="M5.5 1.5 V9.5 M1.5 5.5 H9.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
                    </svg>
                    {{ busyRegionKey === r.key ? '补齐中…' : `补齐导出桩（${r.missing_exports.length}）` }}
                  </button>
                </div>
              </div>
            </article>
          </div>
        </section>
      </div>
    </section>
  </div>
</template>

<style scoped>
.rm-add { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; padding: 14px; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 16px; }
.rm-add label { display: grid; gap: 6px; font-size: var(--fs-sm); }
.rm-add input { padding: 8px; border: 1px solid var(--border); border-radius: 5px; }
.rm-add p { width: 100%; color: var(--danger); }
.rm-overlay {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  z-index: 92;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 30px;
  animation: rm-overlay-in .18s ease-out both;
}
.rm-panel {
  width: min(1020px, 96vw);
  height: min(700px, 90vh);
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  box-shadow: 0 24px 70px rgba(35, 52, 84, 0.16);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: rm-panel-in .22s cubic-bezier(.2,.8,.2,1) both;
}
.rm-top {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}
.rm-title { display: flex; align-items: center; gap: 9px; flex: 1; min-width: 0; }
.rm-title h2 { margin: 0; font-size: var(--fs-body); font-weight: 600; }
.rm-root {
  font-size: var(--fs-cap);
  color: var(--text-faint);
  font-family: var(--font-code);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rm-top-actions { display: flex; gap: 6px; align-items: center; flex: 0 0 auto; }
.rm-refresh {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: var(--fs-sm); padding: 4px 10px; border-radius: 6px;
  border: 1px solid var(--border-strong); background: transparent; color: var(--text-muted); cursor: pointer;
}
.rm-refresh:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); }
.rm-refresh:disabled { opacity: 0.5; cursor: default; }
.rm-refresh { transition: color .16s ease, border-color .16s ease, background .16s ease, transform .16s ease; }
.rm-refresh:active:not(:disabled) { transform: translateY(1px); }
.rm-iconbtn {
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  width: 26px; height: 26px;
  border-radius: 5px;
  cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
}
.rm-iconbtn:hover { background: var(--bg-hover); color: var(--text); }
.rm-iconbtn:focus-visible,
.rm-refresh:focus-visible,
.rm-add input:focus-visible,
.rm-create-btn:focus-visible,
.rm-fill-btn:focus-visible {
  outline: 2px solid var(--accent-deep);
  outline-offset: 2px;
}
.spinning { animation: rm-spin 1s linear infinite; }
@keyframes rm-spin { to { transform: rotate(360deg); } }

.rm-body { flex: 1 1 auto; overflow-y: auto; padding: 14px 16px 22px; }

.rm-contract {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  border-radius: 8px;
  padding: 9px 12px;
  font-size: var(--fs-sm);
  margin-bottom: 16px;
}
/* 契约横幅：底 / 边 / 字 / 左侧色条走同一族语义色。
   语义色每套皮肤各有一份（纸底深、深底浅），所以这一条横幅在四套皮肤上都成立。 */
.rm-contract.ok { background: color-mix(in srgb, var(--green) 10%, var(--bg-raised)); border: 1px solid color-mix(in srgb, var(--green) 38%, transparent); color: var(--green); box-shadow: inset 3px 0 var(--green); }
.rm-contract.bad { background: color-mix(in srgb, var(--danger) 8%, var(--bg-raised)); border: 1px solid color-mix(in srgb, var(--danger) 38%, transparent); color: var(--danger); box-shadow: inset 3px 0 var(--danger); }
.rm-contract svg { flex: 0 0 auto; margin-top: 1px; }
.rm-contract-text { min-width: 0; }
.rm-contract-text strong { font-size: var(--fs-sm); }
.rm-contract-text ul { margin: 5px 0 0; padding-left: 18px; line-height: 1.7; }
.rm-contract-off { color: var(--text-faint); font-size: var(--fs-sm); }

.rm-section { margin-bottom: 18px; }
.rm-section h3 {
  margin: 0 0 9px;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text);
}
.rm-section h3 em {
  font-style: normal;
  font-weight: 400;
  font-size: var(--fs-cap);
  color: var(--text-faint);
  margin-left: 8px;
}

.rm-dag-scroll {
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-raised);
  padding: 4px;
}
/* 画布按实际列宽收缩并居中；列数多到溢出时 margin:auto 退化为 0，可横向滚动 */
.rm-dag { display: block; margin: 0 auto; }
.rm-node { cursor: pointer; }
.rm-node rect { transition: stroke-width .14s ease, filter .14s ease, opacity .14s ease; }
.rm-node:hover rect, .rm-node.selected rect { stroke-width: 2; }
.rm-node:hover rect { filter: brightness(1.04); }
.rm-node.missing rect { stroke-dasharray: 4 3; fill-opacity: 0.35; }
/* DAG 节点文字原先写死深色（#222b38 / #5a6778），那是"只在纸底上成立"的假设：
   面板底是 --bg-raised，深色皮肤下深字压深底 = 直接隐形。改走文字三档。 */
.rm-node-name {
  fill: var(--text);
  font-size: var(--fs-sm);
  font-weight: 600;
  font-family: inherit;
}
.rm-node-dir {
  fill: var(--text-muted);
  font-size: var(--fs-cap);
  font-family: var(--font-code);
}
.rm-node.missing .rm-node-name { fill: var(--text-muted); }

.rm-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(310px, 1fr));
  gap: 10px;
}
.rm-card {
  position: relative;
  display: flex;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg-raised);
  overflow: hidden;
  cursor: pointer;
  transition: border-color .16s ease, background .16s ease, box-shadow .16s ease, transform .16s ease;
}
.rm-card:hover { border-color: var(--border-strong); background: var(--bg-hover); transform: translateY(-1px); box-shadow: 0 5px 14px rgba(35,52,84,.08); }
.rm-card.selected { border-color: color-mix(in srgb, var(--accent) 47%, transparent); box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 27%, transparent) inset; }
.rm-card.missing { cursor: default; opacity: 0.72; }
.rm-card-bar { flex: 0 0 3px; }
.rm-card-main { flex: 1 1 auto; min-width: 0; padding: 9px 11px 10px; }
.rm-card-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 3px; }
.rm-card-name { font-size: var(--fs-sm); font-weight: 600; color: var(--text); display: inline-flex; align-items: center; gap: 6px; }
.rm-card-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.rm-card-dir {
  font-family: var(--font-code);
  font-size: var(--fs-cap);
  color: var(--text-muted);
}
.rm-card-desc {
  margin: 0 0 7px;
  font-size: var(--fs-sm);
  line-height: 1.55;
  color: var(--text-muted);
}
.rm-card-badges { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 7px; }
.rm-badge {
  font-size: var(--fs-cap);
  line-height: 17px;
  padding: 0 7px;
  border-radius: 9px;
  border: 1px solid var(--border-strong);
  color: var(--text-muted);
}
.rm-badge-files { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 32%, var(--bg-raised)); background: color-mix(in srgb, var(--accent) 7%, transparent); }
.rm-badge-git { color: var(--green); border-color: color-mix(in srgb, var(--green) 38%, transparent); background: color-mix(in srgb, var(--green) 7%, transparent); }
.rm-badge-nogit { color: var(--amber); }
.rm-badge-dirty { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 38%, transparent); background: color-mix(in srgb, var(--amber) 7%, transparent); }
.rm-badge-missing { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 38%, transparent); background: color-mix(in srgb, var(--danger) 8%, transparent); }
.rm-card-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px 7px;
  font-size: var(--fs-cap);
  margin-top: 4px;
}
.rm-meta-label {
  flex: 0 0 auto;
  font-size: var(--fs-cap);
  color: var(--text-faint);
  width: 30px;
}
.rm-mata-faint { color: var(--text-faint); }
.rm-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: var(--fs-cap);
  color: var(--text-muted);
  background: var(--bg-hover);
  border: 1px solid var(--border-strong);
  border-radius: 9px;
  padding: 1px 8px 1px 7px;
}
.rm-chip i { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.rm-export {
  font-family: var(--font-code);
  font-size: var(--fs-cap);
  color: var(--amber);
  background: var(--bg-hover);
  border-radius: 4px;
  padding: 0 5px;
}

.rm-card-actions { display: flex; justify-content: flex-end; margin-top: 9px; }
.rm-create-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: var(--fs-cap);
  line-height: 1;
  padding: 6px 11px;
  border-radius: 6px;
  border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--bg-raised));
  background: color-mix(in srgb, var(--accent) 7%, transparent);
  color: var(--accent);
  cursor: pointer;
}
.rm-create-btn:hover:not(:disabled) { background: color-mix(in srgb, var(--accent) 13%, transparent); border-color: var(--accent-deep); }
.rm-create-btn:disabled { opacity: 0.55; cursor: default; }
.rm-create-btn, .rm-fill-btn { transition: background .16s ease, border-color .16s ease, transform .16s ease; }
.rm-create-btn:active:not(:disabled), .rm-fill-btn:active:not(:disabled) { transform: translateY(1px); }
.rm-fill-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: var(--fs-cap);
  line-height: 1;
  padding: 6px 11px;
  border-radius: 6px;
  border: 1px solid color-mix(in srgb, var(--amber) 38%, transparent);
  background: color-mix(in srgb, var(--amber) 12%, var(--bg-raised));
  color: var(--amber);
  cursor: pointer;
}
.rm-fill-btn:hover:not(:disabled) { background: color-mix(in srgb, var(--amber) 15%, var(--bg-raised)); border-color: color-mix(in srgb, var(--amber) 38%, transparent); }
.rm-fill-btn:disabled { opacity: 0.55; cursor: default; }
.rm-btn-spin {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  border: 1.5px solid color-mix(in srgb, var(--accent) 33%, transparent);
  border-top-color: var(--accent);
  animation: rm-spin 0.7s linear infinite;
}
.rm-btn-spin-amber { border-color: color-mix(in srgb, var(--amber) 33%, transparent); border-top-color: var(--amber); }
.rm-badge-missing-export { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 38%, transparent); background: color-mix(in srgb, var(--amber) 7%, transparent); }
.rm-export.missing {
  color: var(--danger);
  background: color-mix(in srgb, var(--danger) 12%, var(--bg-raised));
  border: 1px dashed color-mix(in srgb, var(--danger) 38%, transparent);
  padding: 0 5px;
}

.rm-state {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  font-size: var(--fs-sm);
  color: var(--text-faint);
}
.rm-error { color: var(--danger); }

@keyframes rm-overlay-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes rm-panel-in { from { opacity: 0; transform: translateY(10px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }

@media (max-width: 760px) {
  .rm-overlay { padding: 14px; }
  .rm-panel { width: 100%; height: min(900px, 94vh); }
  .rm-top { align-items: flex-start; }
  .rm-top-actions { flex-wrap: wrap; justify-content: flex-end; }
  .rm-body { padding: 12px 12px 18px; }
  .rm-cards { grid-template-columns: 1fr; }
  .rm-add { align-items: stretch; }
  .rm-add label { flex: 1 1 160px; }
}
@media (max-width: 520px) {
  .rm-overlay { padding: 0; align-items: stretch; }
  .rm-panel { height: 100%; border-radius: 0; border-left: 0; border-right: 0; }
  .rm-top { padding: 10px 12px; }
  .rm-title { min-width: 0; }
  .rm-root { display: none; }
  .rm-top-actions { gap: 4px; }
  .rm-top-actions .rm-refresh { padding: 4px 7px; font-size: var(--fs-cap); }
  .rm-contract { font-size: var(--fs-sm); }
  .rm-card-main { padding: 8px 9px 9px; }
}
@media (prefers-reduced-motion: reduce) {
  .rm-overlay, .rm-panel, .rm-refresh, .rm-node rect, .rm-card, .rm-create-btn, .rm-fill-btn { animation: none; transition: none; }
}
</style>
