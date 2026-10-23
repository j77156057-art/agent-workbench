<script setup lang="ts">
// 代码编辑/预览：CodeMirror 6（oneDark）。
// 每个标签页持有独立 EditorState（保留各自 undo 历史），切换标签用 setState；
// 非活动标签的文本取 state.doc，保存时不依赖当前 DOM view。
// CodeMirror 全家桶（650KB）在首次打开文件时才动态加载（./codeEditor.ts），
// 工作台启动默认在概览页，CodeView 此阶段只渲染 OverviewView，不碰编辑器运行时。
import { onMounted, onBeforeUnmount, ref, watch, computed } from 'vue'
import type { EditorState } from '@codemirror/state'
import type { EditorView } from '@codemirror/view'
import type { EditorTab } from '../composables/workbench-types'
import { useWorkbench } from '../composables/workbench'
import { regionColor, regionTextColor, cat, catAlpha, formatMtime, gitState } from '../theme'
import { onSkinChange } from '../skins'
import OverviewView from './OverviewView.vue'

const props = defineProps<{ tab: EditorTab | null }>()

const {
  saveActive, closeTab, tabs, registerContentGetter, registerDocReplacer, setSelection,
} = useWorkbench()

/** 当前标签是图片预览：用 <img> 渲染，不挂载 CodeMirror。 */
const isImage = computed(() => props.tab?.previewKind === 'image')
/** 当前标签是二进制文件：显示只读信息卡，不挂载 CodeMirror（避免把假 hex 当正文）。 */
const isBinary = computed(() => props.tab?.previewKind === 'binary')

/** 图片加载失败标记：失败时给出「在新窗口打开」兜底，而不是一个空框。 */
const imgFailed = ref(false)
function onImgError() { imgFailed.value = true }
watch(() => props.tab?.previewUrl, () => { imgFailed.value = false })

const host = ref<HTMLElement | null>(null)
let view: EditorView | null = null
let viewPromise: Promise<EditorView> | null = null
let destroyed = false
const states = new Map<number, EditorState>()
/** 切肤订阅的退订函数（onSkinChange 返回）。 */
let offSkinChange: (() => void) | null = null

// ---- P2：把当前非空选区上报给选区 AI（含视口坐标，供浮条定位） ----
function publishSelection(v: EditorView, tab: EditorTab) {
  const main = v.state.selection.main
  const text = v.state.sliceDoc(main.from, main.to)
  if (main.empty || !text.trim()) {
    setSelection(null)
    return
  }
  const coords = v.coordsAtPos(main.to)
  if (!coords) {
    setSelection(null)
    return
  }
  setSelection({
    path: tab.path,
    name: tab.name,
    lang: tab.lang,
    writable: tab.writable,
    text,
    from: main.from,
    to: main.to,
    startLine: v.state.doc.lineAt(main.from).number,
    endLine: v.state.doc.lineAt(main.to).number,
    x: coords.right,
    y: coords.bottom,
  })
}

let rafPending = false
function scheduleRepublish() {
  if (rafPending) return
  rafPending = true
  requestAnimationFrame(() => {
    rafPending = false
    if (view && props.tab && !props.tab.loading && !props.tab.error) publishSelection(view, props.tab)
  })
}
function onEditorScrollOrResize() {
  scheduleRepublish()
}

/**
 * 懒加载 CodeMirror 运行时并创建单例 EditorView。
 * view 单例不变，切标签只替换其 state；桥接到 window.__docmind_cm 供选区 AI/jumpToLine 使用。
 */
function ensureView(): Promise<EditorView> {
  if (!viewPromise) {
    viewPromise = import('./codeEditor').then((m) => {
      const v = m.createEditorView(host.value!)
      if (destroyed) {
        // 极端竞态：块还没加载完组件就卸载了
        v.destroy()
        throw new Error('CodeView unmounted before editor ready')
      }
      view = v
      ;(window as unknown as { __docmind_cm?: EditorView }).__docmind_cm = v
      // 选区浮条是 fixed 定位：编辑器滚动或窗口缩放时刷新锚点坐标
      v.scrollDOM.addEventListener('scroll', onEditorScrollOrResize, { passive: true })
      window.addEventListener('resize', onEditorScrollOrResize)
      return v
    })
  }
  return viewPromise
}

async function ensureState(tab: EditorTab): Promise<EditorState> {
  let st = states.get(tab.id)
  if (!st) {
    const m = await import('./codeEditor')
    st = m.buildEditorState(tab, {
      saveActive: () => void saveActive(),
      tabs,
      commitState: (t, state) => states.set(t.id, state),
      publishSelection,
    })
    states.set(tab.id, st)
    registerContentGetter(tab.id, () => states.get(tab.id)!.doc.toString())
    registerDocReplacer(tab.id, (content) => replaceTabDoc(tab.id, content))
  }
  return st
}

/**
 * P3：git 回滚/历史恢复后整文档替换。
 * 注意 EditorState.update() 返回的是 Transaction（新状态在 .state 上），不是 State。
 * 活动标签直接 view.dispatch（listener 会自动回写 states）；
 * 非活动标签换 Map 里留存的 EditorState，切回去即为新内容。
 */
function replaceTabDoc(tabId: number, content: string) {
  const tab = tabs.value.find(t => t.id === tabId)
  if (tab) tab.draftContent = content
  const old = states.get(tabId)
  if (!old) return
  if (view && (view as unknown as { __tabId?: number }).__tabId === tabId && view.state === old) {
    view.dispatch({ changes: { from: 0, to: old.doc.length, insert: content } })
    return
  }
  states.set(
    tabId,
    old.update({ changes: { from: 0, to: old.doc.length, insert: content } }).state,
  )
}

// 连续 watch 触发只认最后一次：异步加载编辑器期间标签可能已再次切换
let syncToken = 0
async function syncView() {
  // 切换标签 / 进入加载或错误态：旧选区不再有效，隐藏选区浮条
  setSelection(null)
  if (!props.tab || props.tab.loading || props.tab.error) return
  // 图片/二进制：没有可编辑的文本，别去懒加载 CodeMirror，也不 setState
  if (isImage.value || isBinary.value) return
  const token = ++syncToken
  const v = await ensureView()
  const st = await ensureState(props.tab)
  if (destroyed || token !== syncToken) return
  // 切走前把旧标签的最终 state 落回 Map（listener 已逐次回写，这里兜底）
  const curId = (v as unknown as { __tabId?: number }).__tabId
  if (curId != null && curId !== props.tab.id && states.has(curId)) {
    states.set(curId, v.state)
  }
  ;(v as unknown as { __tabId?: number }).__tabId = props.tab.id
  v.setState(st)
}

onMounted(() => {
  // 无标签时只渲染概览，不触发编辑器块加载；首次有真实标签才异步建 view
  void syncView()
  // 切肤后换掉编辑器的语法高亮表（2026-10-20）。
  // 主体配色是 CSS 变量，会自动跟随；高亮表是 JS 对象，得逐个 state 重新 configure。
  // 每个标签各有一个 EditorState，只换当前这个的话，切到别的标签会露出旧皮肤的高亮。
  offSkinChange = onSkinChange(() => {
    void (async () => {
      const m = await import('./codeEditor')
      for (const [id, st] of states) {
        const ns = m.rethemeState(st)
        states.set(id, ns)
        if (view && view.state === st) view.setState(ns)
      }
    })()
  })
})

onBeforeUnmount(() => {
  destroyed = true
  offSkinChange?.()
  if (view) {
    view.scrollDOM.removeEventListener('scroll', onEditorScrollOrResize)
    window.removeEventListener('resize', onEditorScrollOrResize)
    setSelection(null)
    if ((window as unknown as { __docmind_cm?: unknown }).__docmind_cm === view) {
      delete (window as unknown as { __docmind_cm?: EditorView }).__docmind_cm
    }
    view.destroy()
    view = null
  }
})

// 切活动 tab / 加载完成 → 挂载对应 state
watch(() => [props.tab?.id, props.tab?.loading, props.tab?.error], () => void syncView())

/** CRLF 归一化比较（与 codeEditor 工厂内实现保持一致）。 */
function isDocDirty(docText: string, saved: string): boolean {
  const norm = (s: string) => s.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  return norm(docText) !== norm(saved)
}

// 保存成功后（savedContent 变化）重算 dirty
watch(() => props.tab?.savedContent, () => {
  if (!props.tab) return
  const st = states.get(props.tab.id)
  if (st) props.tab.dirty = isDocDirty(st.doc.toString(), props.tab.savedContent)
})

// 标签关闭后回收 state
watch(tabs, (list) => {
  const alive = new Set(list.map((t) => t.id))
  for (const id of [...states.keys()]) {
    if (!alive.has(id)) states.delete(id)
  }
}, { deep: false })

function accent(tab: EditorTab) {
  return regionColor(tab.region)
}
function accentText(tab: EditorTab) {
  return regionTextColor(tab.region)
}
function git(tab: EditorTab) {
  return gitState(tab.tracked, tab.gitDirty)
}
function segments(path: string) {
  return path.split('/')
}
function savedLabel(tab: EditorTab): string {
  if (tab.saving) return '保存中…'
  if (tab.dirty) return '● 未保存'
  if (tab.savedAt) return `已保存 ${new Date(tab.savedAt).toLocaleTimeString('zh-CN', { hour12: false })}`
  return ''
}

</script>

<template>
  <section class="cv">
    <!-- 文件头：面包屑 + 元信息 -->
    <header v-if="tab && !tab.loading && !tab.error" class="cv-head">
      <div class="cv-crumbs">
        <template v-for="(seg, i) in segments(tab.path)" :key="i">
          <span v-if="i" class="cv-sep">/</span>
          <span :class="['cv-seg', { 'cv-seg-region': i === 0 && tab.region, 'cv-seg-last': i === segments(tab.path).length - 1 }]">
            {{ seg }}
          </span>
        </template>
      </div>
      <div class="cv-meta">
        <span v-if="tab.regionName" class="cv-badge cv-region-badge"
              :style="{ color: accentText(tab), borderColor: cat(accent(tab)), background: catAlpha(accent(tab), 8) }">
          {{ tab.regionName }}
        </span>
        <span class="cv-badge cv-lang">{{ tab.lang }}</span>
        <!-- 不在任何 git 仓库（或本机无 git）时不显示徽标，避免每个标签都挂「无 git」噪声 -->
        <span v-if="git(tab).dot !== 'none'" class="cv-badge" :class="`cv-git-${git(tab).dot}`" :title="git(tab).title">
          {{ git(tab).dot === 'untracked' ? '未跟踪' : git(tab).dot === 'dirty' ? '已修改' : '已跟踪' }}
        </span>
        <span v-if="!tab.writable" class="cv-badge cv-readonly">{{ tab.previewKind ? '只读预览' : '只读保护' }}</span>
        <span :class="['cv-savestate', { 'is-dirty': tab.dirty, 'is-saving': tab.saving }]">{{ savedLabel(tab) }}</span>
        <span class="cv-faint">{{ formatMtime(tab.mtime) }}</span>
      </div>
    </header>

    <div v-if="tab && !tab.loading && !tab.error && tab.previewKind" class="cv-previewbar">
      <strong>{{ tab.previewKind === 'image' ? '图片预览' : tab.previewKind === 'binary' ? '二进制文件（只读信息）' : '文本文件预览' }}</strong>
      <span>{{ tab.previewNote }}</span>
    </div>

    <!-- 加载态 -->
    <div v-if="tab?.loading" class="cv-state">
      <div class="cv-spinner" />
      <p>正在读取 {{ tab.name }}…</p>
    </div>

    <!-- 打开失败 -->
    <div v-else-if="tab?.error" class="cv-state cv-error">
      <svg width="34" height="34" viewBox="0 0 34 34">
        <circle cx="17" cy="17" r="15" fill="none" stroke="#ff6b6b55" stroke-width="1.5" />
        <path d="M17 9 V19" stroke="#ff6b6b" stroke-width="2" stroke-linecap="round" />
        <circle cx="17" cy="24" r="1.3" fill="#ff6b6b" />
      </svg>
      <p class="cv-error-title">无法打开文件 · HTTP {{ tab.errorStatus }}</p>
      <p class="cv-error-msg">{{ tab.error }}</p>
      <button class="cv-close-btn" @click="closeTab(tab.id)">关闭标签</button>
    </div>

    <!-- 概览驾驶舱（未进入代码工作区时） -->
    <OverviewView v-else-if="!tab" />

    <!-- 图片：直接渲染原图（/api/fs/raw），不再显示假 hex -->
    <div v-if="tab && !tab.loading && !tab.error && isImage" class="cv-imgwrap">
      <img class="cv-img" :src="tab.previewUrl || ''" :alt="tab.name" @error="onImgError" />
      <p v-if="imgFailed" class="cv-imgfail">
        图片加载失败，可尝试<a :href="tab.previewUrl || ''" target="_blank" rel="noopener">在新窗口打开</a>。
      </p>
    </div>

    <!-- 二进制：只读信息卡（类型/大小/SHA），不挂编辑器 -->
    <div v-else-if="tab && !tab.loading && !tab.error && isBinary" class="cv-binary">
      <div class="cv-binary-icon" aria-hidden="true">
        <svg width="46" height="46" viewBox="0 0 46 46" fill="none">
          <path d="M9 6h20l8 8v26H9z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" />
          <path d="M29 6v8h8" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" />
        </svg>
      </div>
      <pre class="cv-binary-body">{{ tab.savedContent }}</pre>
    </div>

    <div v-show="tab && !tab.loading && !tab.error && !isImage && !isBinary" ref="host" class="cv-host" />

    <!-- reindex 警告（不阻断） -->
    <div v-if="tab && !tab.loading && !tab.error && tab.reindexWarn" class="cv-warnbar" :title="tab.reindexWarn">
      {{ tab.reindexWarn }}
    </div>
  </section>
</template>
