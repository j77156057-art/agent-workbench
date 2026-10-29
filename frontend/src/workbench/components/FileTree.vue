<script setup lang="ts">
// 文件树侧栏：头部（根目录名 + 新建 + 刷新）、递归树、节点截断提示。
// 展开状态本地维护：首次/刷新后默认展开全部一级目录，并保留用户已展开的路径。
// AI 答案引用点击时，展开目标的全部祖先目录并闪烁该节点。
import { ref, watch } from 'vue'
import type { TreeNode, TreeResp } from '../api'
import FileTreeNode from './FileTreeNode.vue'
import { computed } from 'vue'
import { useWorkbench } from '../composables/workbench'
import { demoMode, demoTagMap } from '../composables/demo'
import type { SemanticTagRecord } from '../api'

const props = defineProps<{
  tree: TreeResp
  selectedPath: string | null
  loading: boolean
  /** 固定布局下可收起：头部多一颗「收起资源管理器」按钮 */
  collapsible?: boolean
}>()

const emit = defineEmits<{
  (e: 'select', node: TreeNode): void
  (e: 'collapse'): void
}>()

const {
  loadTree, createAt, openNodeMenu, openRootMenu, treeReveal,
  tagMap, locatePaths,
} = useWorkbench()

// 离线演示态没有 /api/fs/semantic-tags，用内置示例标签渲染徽章
const effectiveTagMap = computed<Record<string, SemanticTagRecord>>(
  () => (demoMode.value ? (demoTagMap as Record<string, SemanticTagRecord>) : tagMap.value),
)

// 默认只展示源码与资料；隐藏目录、缓存、构建产物和会话记录不进资源管理器。
// 用户仍可用标题栏的眼睛按钮临时展开全部，偏好本地记忆。
const HIDDEN_NAMES = new Set([
  '__pycache__', 'node_modules', 'dist', 'build', 'coverage', 'venv',
  '.git', '.venv', '.pytest_cache', '.docmind_turn_traces', '.claude',
  '.trae', '.uv-cache', '.uv-cache-codex', '.next', '.cache',
])
const SHOW_HIDDEN_KEY = 'docmind.ftShowHidden'
const showHidden = ref(false)
try { showHidden.value = window.localStorage.getItem(SHOW_HIDDEN_KEY) === '1' } catch { /* 存不下就默认隐藏 */ }
watch(showHidden, (v) => {
  try { window.localStorage.setItem(SHOW_HIDDEN_KEY, v ? '1' : '0') } catch { /* 仅本次会话 */ }
})
function isNoise(node: TreeNode): boolean {
  return node.name.startsWith('.') || HIDDEN_NAMES.has(node.name)
}
function visibleNodes(nodes: TreeNode[]): TreeNode[] {
  if (showHidden.value) return nodes
  return nodes
    .filter((n) => !isNoise(n))
    .map((n) => n.type === 'dir' ? { ...n, children: visibleNodes(n.children) } : n)
}
const shownNodes = computed(() => visibleNodes(props.tree.nodes))
function hasNoiseSegment(path: string): boolean {
  return path.replace(/\\/g, '/').split('/').some((seg) => seg.startsWith('.') || HIDDEN_NAMES.has(seg))
}

const openPaths = ref<Set<string>>(new Set())
const flashPath = ref<string | null>(null)
const flashNonce = ref(0)
const createParent = computed(() => {
  const node = props.selectedPath ? findNode(props.tree.nodes, props.selectedPath) : null
  if (node?.type === 'dir') return node
  const path = node?.path.split('/').slice(0, -1).join('/')
  return path ? findNode(props.tree.nodes, path) : null
})

function allDirPaths(nodes: TreeNode[], acc: Set<string>) {
  for (const n of nodes) {
    if (n.type === 'dir') {
      acc.add(n.path)
      if (n.children.length) allDirPaths(n.children, acc)
    }
  }
}

function findNode(nodes: TreeNode[], path: string): TreeNode | null {
  for (const n of nodes) {
    if (n.path === path) return n
    if (n.type === 'dir') {
      const hit = findNode(n.children, path)
      if (hit) return hit
    }
  }
  return null
}

/** 目标路径的所有祖先目录（按路径分段推导，节点存在即可展开）。 */
function ancestorDirs(path: string): string[] {
  const parts = path.replace(/\\/g, '/').split('/').slice(0, -1)
  const out: string[] = []
  for (let i = 1; i <= parts.length; i++) out.push(parts.slice(0, i).join('/'))
  return out
}

function reseed(nodes: TreeNode[]) {
  // 默认不展开全部一级目录：那会把资源管理器变成一堵文件墙。
  // 只保留用户主动展开的目录，以及当前选中文件的祖先路径。
  const all = new Set<string>()
  allDirPaths(nodes, all)
  const kept = new Set([...openPaths.value].filter((p) => all.has(p)))
  if (props.selectedPath) {
    for (const d of ancestorDirs(props.selectedPath)) if (all.has(d)) kept.add(d)
  }
  openPaths.value = kept
}

watch(() => props.tree, (t) => reseed(t.nodes), { immediate: true })

// 从最近打开 / 搜索 / 外部入口选中文件时，把它所在的目录展开到可见位置。
watch(() => props.selectedPath, (path) => {
  if (!path || !props.tree) return
  if (hasNoiseSegment(path)) showHidden.value = true
  const next = new Set(openPaths.value)
  for (const d of ancestorDirs(path)) {
    if (findNode(props.tree.nodes, d)) next.add(d)
  }
  openPaths.value = next
})

// AI 引用定位：树节点必须真实存在才展开/闪烁（幻觉路径不做反应）
watch(treeReveal, (req) => {
  if (!req || !props.tree) return
  const node = findNode(props.tree.nodes, req.path)
  if (!node) return
  if (hasNoiseSegment(req.path)) showHidden.value = true
  const next = new Set(openPaths.value)
  for (const d of ancestorDirs(req.path)) next.add(d)
  openPaths.value = next
  flashPath.value = req.path
  flashNonce.value = req.nonce
})

function toggle(path: string) {
  const next = new Set(openPaths.value)
  if (next.has(path)) next.delete(path)
  else next.add(path)
  openPaths.value = next
}

// 阶段 1：大白话定位结果到达后，自动展开所有命中文件的祖先目录
watch(locatePaths, (paths) => {
  if (!paths.size || !props.tree) return
  const next = new Set(openPaths.value)
  for (const p of paths) {
    if (!findNode(props.tree.nodes, p)) continue
    for (const d of ancestorDirs(p)) next.add(d)
  }
  openPaths.value = next
})

function rootLabel(p: string): string {
  const parts = p.replace(/\\/g, '/').replace(/\/+$/, '').split('/')
  return parts[parts.length - 1] || p
}
</script>

<template>
  <aside class="ft-panel">
    <header class="ft-head">
      <div class="ft-head-title">
        <span class="ft-head-label">资源管理器</span>
        <span class="ft-head-root" :title="tree.code_root">{{ rootLabel(tree.code_root) }}</span>
      </div>
      <div class="ft-head-actions">
        <button class="ft-iconbtn" :title="`新建文件：${createParent?.path || '根目录'}`" @click="createAt(createParent, 'file')">
          <svg width="13" height="13" viewBox="0 0 13 13">
            <path d="M7 1.5 H11.5 V11.5 H1.5 V6.2" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/>
            <path d="M6.7 4.6 V9.2 M4.4 6.9 H9" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
          </svg>
        </button>
        <button class="ft-iconbtn" :title="`新建文件夹：${createParent?.path || '根目录'}`" @click="createAt(createParent, 'folder')">
          <svg width="14" height="13" viewBox="0 0 14 13">
            <path d="M1 2.8 Q1 2.1 1.7 2.1 H5.2 L6.3 3.3 H12.3 Q13 3.3 13 4 V10.2 Q13 10.9 12.3 10.9 H1.7 Q1 10.9 1 10.2 Z" fill="none" stroke="currentColor" stroke-width="1"/>
            <path d="M7 5.6 V9.4 M5.1 7.5 H8.9" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
          </svg>
        </button>
        <button
          class="ft-iconbtn"
          :class="{ 'ft-iconbtn-on': showHidden }"
          :aria-pressed="showHidden"
          :title="showHidden ? '隐藏生成文件与隐藏目录' : '显示生成文件与隐藏目录'"
          :aria-label="showHidden ? '隐藏生成文件与隐藏目录' : '显示生成文件与隐藏目录'"
          @click="showHidden = !showHidden"
        >
          <svg v-if="showHidden" width="14" height="13" viewBox="0 0 14 13" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round">
            <path d="M1.2 6.5 C3 3.3 5 1.8 7 1.8 S11 3.3 12.8 6.5 C11 9.7 9 11.2 7 11.2 S3 9.7 1.2 6.5Z" />
            <circle cx="7" cy="6.5" r="1.9" />
          </svg>
          <svg v-else width="14" height="13" viewBox="0 0 14 13" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round">
            <path d="M1.2 6.5 C3 3.3 5 1.8 7 1.8 S11 3.3 12.8 6.5 C11 9.7 9 11.2 7 11.2 S3 9.7 1.2 6.5Z" />
            <path d="M2 11 L12 2" />
          </svg>
        </button>
        <button class="ft-iconbtn" :disabled="loading" title="重新加载文件树" @click="loadTree()">
          <svg width="13" height="13" viewBox="0 0 13 13" :class="{ spinning: loading }">
            <path d="M11.2 6.6 A4.7 4.7 0 1 1 6.5 1.8 A4.7 4.7 0 0 1 10.6 3.6 M10.8 1.6 V3.9 H8.5"
                  fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <button
          v-if="collapsible"
          class="ft-iconbtn ft-collapse-btn"
          title="收起资源管理器"
          aria-label="收起资源管理器"
          @click="emit('collapse')"
        >
          <svg width="13" height="13" viewBox="0 0 13 13">
            <rect x="1.4" y="1.8" width="10.2" height="9.4" rx="1" fill="none" stroke="currentColor" stroke-width="1" />
            <path d="M5.2 1.8 V11.2" stroke="currentColor" stroke-width="1" />
            <path d="M10 5 L7.8 6.5 L10 8" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
      </div>
    </header>

    <div class="ft-body" @contextmenu.prevent="openRootMenu($event)">
      <FileTreeNode
        v-for="node in shownNodes"
        :key="node.path"
        :node="node"
        :depth="0"
        :selected-path="selectedPath"
        :open-paths="openPaths"
        :flash-path="flashPath"
        :flash-nonce="flashNonce"
        :tag-map="effectiveTagMap"
        :located-paths="locatePaths"
        @toggle="toggle"
        @select="emit('select', $event)"
        @contextmenu="(ev, n) => openNodeMenu(ev, n)"
      />
      <p v-if="shownNodes.length === 0" class="ft-empty">
        {{ tree.nodes.length ? '当前只显示源码与资料；点标题栏眼睛图标可查看隐藏/生成项' : '代码库目录为空，右键此处新建文件或文件夹' }}
      </p>
      <p v-if="tree.truncated" class="ft-truncated">
        文件过多，仅显示前 2000 个节点
      </p>
    </div>
  </aside>
</template>
