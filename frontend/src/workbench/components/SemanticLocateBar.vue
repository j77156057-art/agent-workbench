<script setup lang="ts">
// 阶段 1：顶栏「大白话定位框」。
// 输入「玩家数值 / 敌人 AI / 伤害计算」这类业务词，混合检索业务标签、文件名、
// 函数名与向量语义，下拉直接跳到对应文件/行；也能直接命中整个分区文件夹。
// 离线演示模式用 demo 数据，点击不跳转（没有真实文件树）。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useWorkbench } from '../composables/workbench'
import { demoMode, demoLocateFiles, demoLocateRegions } from '../composables/demo'
import { regionColor, regionTextColor, cat, catAlpha } from '../theme'
import type { LocateFileHit, LocateRegionHit } from '../api'

const {
  locateLoading, locateResult, runLocate, clearLocate,
  openLocateFile, openLocateRegion,
  tagMeta, tagLoading, tagError, refreshTags,
} = useWorkbench()

const q = ref('')
const open = ref(false)
const activeIdx = ref(0)
let debounce: number | undefined

const files = computed<LocateFileHit[]>(() =>
  demoMode.value ? (demoLocateFiles as LocateFileHit[]) : (locateResult.value?.files || []))
const regions = computed<LocateRegionHit[]>(() =>
  demoMode.value ? (demoLocateRegions as LocateRegionHit[]) : (locateResult.value?.regions || []))
const degraded = computed(() => (demoMode.value ? '' : locateResult.value?.degraded || ''))
const hasQuery = computed(() => q.value.trim().length > 0)
const showEmpty = computed(() =>
  hasQuery.value && !locateLoading.value && files.value.length === 0 && regions.value.length === 0)

function scheduleSearch() {
  window.clearTimeout(debounce)
  const text = q.value.trim()
  if (text.length < 2) {
    clearLocate()
    open.value = false
    return
  }
  openPop()
  debounce = window.setTimeout(() => {
    if (demoMode.value) return
    void runLocate(text)
  }, 280)
}

// 键盘导航：region 条目排在文件之前，统一压平成可选序列
const flatCount = computed(() => regions.value.length + files.value.length)
function onKeydown(ev: KeyboardEvent) {
  if (ev.key === 'ArrowDown') {
    ev.preventDefault()
    activeIdx.value = Math.min(activeIdx.value + 1, flatCount.value - 1)
  } else if (ev.key === 'ArrowUp') {
    ev.preventDefault()
    activeIdx.value = Math.max(activeIdx.value - 1, 0)
  } else if (ev.key === 'Enter') {
    ev.preventDefault()
    pick(activeIdx.value)
  } else if (ev.key === 'Escape') {
    open.value = false
  }
}

function pick(idx: number) {
  if (idx < regions.value.length) {
    const r = regions.value[idx]
    if (!demoMode.value) openLocateRegion(r.dir)
    open.value = false
    return
  }
  const fi = idx - regions.value.length
  const f = files.value[fi]
  if (f) {
    if (!demoMode.value) void openLocateFile(f)
    open.value = false
  }
}

function clearAll() {
  q.value = ''
  clearLocate()
  open.value = false
}

async function onRefreshTags() {
  if (demoMode.value) return
  const r = await refreshTags(60)
  if (r && hasQuery.value) void runLocate(q.value.trim())
}

watch(files, () => { activeIdx.value = 0 })

function onDocClick(ev: MouseEvent) {
  // 面板 teleport 到了 body，不再是 el 的后代 —— 只判 el 会把"点面板里的结果"当成点外面，
  // 面板当场消失（点了没反应）。两处都要判。
  const t = ev.target as Node
  if (!(el.value && el.value.contains(t)) && !(popRef.value && popRef.value.contains(t))) {
    open.value = false
  }
}
const el = ref<HTMLElement | null>(null)
const boxEl = ref<HTMLElement | null>(null)
const popRef = ref<HTMLElement | null>(null)

/* 下拉面板的位置（2026-10-20）。
   面板原本是 position:absolute 挂在顶栏里，而顶栏有 overflow-y:hidden
   （窄宽兜底要横向滚动）→ 面板被裁到只剩一条边，**两种材质下都看不见**；
   宽度也是按顶栏算的（实测 841px，而搜索框只有 380px）。
   改成 teleport 到 body + fixed 定位，坐标按搜索框的真实位置算。
   顺带躲开 fixed 定位的包含块问题：玻璃模式顶栏的 backdrop-filter
   会让祖先成为 fixed 的包含块（同「更多」菜单那个坑）。 */
const popStyle = ref<Record<string, string>>({})
function syncPopPosition() {
  // 按**可见的输入框**（.lb-box）定位，不是根节点 .lb ——
  // 根节点被 .wb-locate-slot 的 flex:1 1 auto 撑开成整条顶栏那么宽（实测 841px），
  // 面板跟着它就变成一大条，而不是搜索框那么宽。
  const box = boxEl.value
  if (!box) { popStyle.value = {}; return }
  const r = box.getBoundingClientRect()
  const w = Math.max(240, Math.round(r.width))
  const left = Math.max(8, Math.min(Math.round(r.left), window.innerWidth - w - 8))
  popStyle.value = { top: `${Math.round(r.bottom + 6)}px`, left: `${left}px`, width: `${w}px` }
}
function openPop() {
  syncPopPosition()
  open.value = true
}
watch(open, (v) => { if (v) syncPopPosition() })
window.addEventListener('resize', syncPopPosition)
window.addEventListener('scroll', syncPopPosition, true)

document.addEventListener('click', onDocClick)
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  window.clearTimeout(debounce)
  window.removeEventListener('resize', syncPopPosition)
  window.removeEventListener('scroll', syncPopPosition, true)
})
</script>

<template>
  <div ref="el" class="lb">
    <div ref="boxEl" class="lb-box" :class="{ 'lb-open': open }">
      <svg class="lb-icon" width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
        <circle cx="6.2" cy="6.2" r="4.4" fill="none" stroke="currentColor" stroke-width="1.4" />
        <path d="M9.6 9.6 L12.5 12.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
      </svg>
      <input
        v-model="q"
        class="lb-input"
        type="text"
        placeholder="用大白话找代码：玩家数值、敌人AI、伤害计算…"
        @input="scheduleSearch"
        @keydown="onKeydown"
        @focus="hasQuery ? openPop() : (open = false)"
      />
      <span v-if="locateLoading || tagLoading" class="lb-spin" />
      <button v-if="q" class="lb-clear" title="清空" @click="clearAll">×</button>
    </div>

    <!-- 下拉结果：teleport 到 body（2026-10-20）。
         原本它是 position:absolute 挂在顶栏里，而顶栏 overflow-y:hidden
         （窄宽兜底要横向滚动）→ 面板被裁到只剩一条边，两种材质下都看不见；
         宽度也按错的包含块算（实测 841px，搜索框只有 300~380px）。
         teleport + fixed 后坐标与宽度都按搜索框真实位置算，
         也不再受祖先 backdrop-filter 建立包含块的影响。 -->
    <Teleport to="body">
      <div ref="popRef" v-if="open" class="lb-pop" :style="popStyle">
      <div class="lb-pop-head">
        <span v-if="demoMode" class="lb-demo-note">示例结果 · 连接本地项目后可直接点击跳转</span>
        <span v-else class="lb-count">
          命中 {{ locateResult?.total ?? files.length }} 个文件
          <template v-if="regions.length"> · {{ regions.length }} 个分区</template>
        </span>
        <span class="lb-spacer" />
        <button
          v-if="!demoMode"
          class="lb-tagbtn"
          :disabled="tagLoading"
          :title="tagMeta.pending > 0 ? `还有 ${tagMeta.pending} 个文件未标注，点我继续` : '让本地模型给文件打中文业务标签（失败会自动用关键词规则）'"
          @click="onRefreshTags"
        >
          {{ tagLoading ? '标注中…' : tagMeta.pending > 0 ? `AI 补标注（${tagMeta.pending}）` : 'AI 智能标注' }}
        </button>
      </div>

      <div v-if="tagError" class="lb-warn">⚠️ {{ tagError }}</div>
      <div v-if="degraded" class="lb-warn">⚠️ {{ degraded }}</div>

      <div class="lb-list">
        <!-- 分区命中 -->
        <button
          v-for="(r, i) in regions"
          :key="'r-' + r.key"
          class="lb-item lb-item-region"
          :class="{ 'lb-active': activeIdx === i }"
          @click="pick(i)"
          @mouseenter="activeIdx = i"
        >
          <span class="lb-region-rail" :style="{ background: regionColor(r.key) }" />
          <span class="lb-item-main">
            <span class="lb-item-title">
              <span class="lb-folder-glyph">📁</span>{{ r.name }}
              <em class="lb-region-dir">{{ r.dir }}/</em>
            </span>
            <span class="lb-item-sub">{{ r.desc || '分区文件夹' }}</span>
          </span>
          <span class="lb-kind-tag">分区</span>
        </button>

        <!-- 文件命中 -->
        <button
          v-for="(f, i) in files"
          :key="f.path"
          class="lb-item"
          :class="{ 'lb-active': activeIdx === i + regions.length }"
          @click="pick(i + regions.length)"
          @mouseenter="activeIdx = i + regions.length"
        >
          <span class="lb-item-main">
            <span class="lb-item-title">
              <span class="lb-file-name">{{ f.name }}</span>
              <span v-if="f.symbol" class="lb-symbol">{{ f.symbol }}()<template v-if="f.line">:{{ f.line }}</template></span>
              <span v-if="f.region_name" class="lb-region-chip" :style="{ color: regionTextColor(f.region), borderColor: cat(regionColor(f.region)), background: catAlpha(regionColor(f.region), 8) }">{{ f.region_name }}</span>
            </span>
            <span class="lb-item-sub">
              <span class="lb-path">{{ f.path }}</span>
              <span v-if="f.summary" class="lb-summary">· {{ f.summary }}</span>
            </span>
            <span v-if="f.tags?.length" class="lb-tags">
              <i v-for="t in f.tags.slice(0, 3)" :key="t">#{{ t }}</i>
            </span>
          </span>
          <span class="lb-reasons">
            <i v-for="r in f.reasons.slice(0, 2)" :key="r">{{ r }}</i>
          </span>
        </button>

        <div v-if="showEmpty" class="lb-empty">
          没找到相关代码。换个说法试试，或点右上角「AI 智能标注」让模型先读一遍项目。
        </div>
      </div>

      <div class="lb-pop-foot">
        <span>↑↓ 选择</span><span>Enter 跳转</span><span>Esc 关闭</span>
      </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.lb { position: relative; width: 340px; }
.lb-box {
  display: flex; align-items: center; gap: 7px;
  height: 32px; padding: 0 10px;
  background: var(--bg-raised);
  border: 1px solid var(--border-graphic);
  border-radius: 8px;
  transition: border-color .15s, box-shadow .15s;
}
.lb-box.lb-open, .lb-box:focus-within {
  border-color: #9dbdf4;
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 12%, transparent);
}
.lb-icon { color: var(--text-faint); flex: 0 0 auto; }
.lb-input {
  flex: 1; min-width: 0; border: 0; outline: none; background: transparent;
  font-size: var(--fs-sm); color: var(--text); font-family: var(--font-ui);
}
.lb-input::placeholder { color: var(--text-faint); }
.lb-clear {
  border: 0; background: transparent; color: var(--text-faint);
  font-size: var(--fs-h3); line-height: 1; cursor: pointer; padding: 0 2px;
}
.lb-clear:hover { color: var(--text); }
.lb-spin {
  width: 12px; height: 12px; border-radius: 50%; flex: 0 0 auto;
  border: 2px solid #cfd9ea; border-top-color: var(--accent);
  animation: lbspin .7s linear infinite;
}
@keyframes lbspin { to { transform: rotate(360deg); } }

.lb-pop {
  position: fixed; z-index: 600;
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: var(--shadow-pop);
  overflow: hidden;
}
.lb-pop-head {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; border-bottom: 1px solid var(--border);
  font-size: var(--fs-sm); color: var(--text-muted);
}
.lb-spacer { flex: 1; }
.lb-count { white-space: nowrap; }
.lb-demo-note { color: var(--amber); }
.lb-tagbtn {
  border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--bg-raised)); --btn-fill: color-mix(in srgb, var(--accent) 12%, var(--bg-raised)); background: color-mix(in srgb, var(--accent) 12%, var(--bg-raised)); color: var(--accent);
  font-size: var(--fs-cap); font-weight: 600; padding: 3px 9px; border-radius: 6px;
  cursor: pointer; white-space: nowrap; font-family: var(--font-ui);
}
.lb-tagbtn:hover:not(:disabled) { --btn-fill: #e0ecfd; background: #e0ecfd; }
.lb-tagbtn:disabled { opacity: .6; cursor: wait; }
.lb-warn {
  padding: 6px 10px; font-size: var(--fs-cap); color: var(--amber);
  background: #fdf6ea; border-bottom: 1px solid #f0e2c4;
}
.lb-list { max-height: 380px; overflow-y: auto; padding: 4px; }
.lb-item {
  display: flex; align-items: stretch; gap: 8px; width: 100%;
  text-align: left; padding: 7px 9px; border: 0; border-radius: 7px;
  background: transparent; cursor: pointer; font-family: var(--font-ui);
  position: relative;
}
.lb-item.lb-active { background: var(--bg-selected); }
.lb-region-rail { width: 3px; border-radius: 2px; flex: 0 0 3px; }
.lb-item-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.lb-item-title { display: flex; align-items: center; gap: 7px; font-size: var(--fs-sm); color: var(--text); }
.lb-folder-glyph { font-size: var(--fs-md); }
.lb-file-name { font-weight: 600; }
.lb-symbol {
  font-family: var(--font-mono); font-size: var(--fs-cap); color: var(--accent);
  background: color-mix(in srgb, var(--accent) 8%, transparent); border-radius: 4px; padding: 0 5px;
}
.lb-region-dir { font-style: normal; color: var(--text-faint); font-size: var(--fs-sm); }
.lb-region-chip {
  font-size: var(--fs-cap); font-style: normal; font-weight: 600;
  border: 1px solid; border-radius: 5px; padding: 0 5px; white-space: nowrap;
}
.lb-item-sub {
  display: flex; gap: 5px; font-size: var(--fs-cap); color: var(--text-faint);
  min-width: 0;
}
.lb-path { font-family: var(--font-mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.lb-summary { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lb-tags { display: flex; gap: 5px; flex-wrap: wrap; }
.lb-tags i {
  font-style: normal; font-size: var(--fs-cap); color: var(--accent-2);
  background: rgba(122, 90, 248, .09); border-radius: 4px; padding: 0 5px;
}
.lb-kind-tag {
  align-self: center; flex: 0 0 auto;
  font-size: var(--fs-cap); color: var(--text-muted);
  border: 1px solid var(--border); border-radius: 5px; padding: 1px 6px;
}
.lb-reasons {
  display: flex; flex-direction: column; gap: 3px; align-items: flex-end;
  align-self: center; flex: 0 0 auto;
}
.lb-reasons i {
  font-style: normal; font-size: var(--fs-cap); color: var(--text-faint);
  background: var(--bg-hover); border-radius: 4px; padding: 1px 5px; white-space: nowrap;
}
.lb-empty { padding: 22px 14px; text-align: center; font-size: var(--fs-sm); color: var(--text-faint); line-height: 1.7; }
.lb-pop-foot {
  display: flex; gap: 12px; padding: 6px 10px;
  border-top: 1px solid var(--border);
  font-size: var(--fs-cap); color: var(--text-faint);
  background: var(--bg-hover);
}
</style>
