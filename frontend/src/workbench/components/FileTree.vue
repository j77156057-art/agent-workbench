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
  // 保留旧展开集合中仍存在的目录，再补开一级目录
  const all = new Set<string>()
  allDirPaths(nodes, all)
  const kept = new Set([...openPaths.value].filter((p) => all.has(p)))
  for (const n of nodes) if (n.type === 'dir') kept.add(n.path)
  openPaths.value = kept
}

watch(() => props.tree, (t) => reseed(t.nodes), { immediate: true })

// AI 引用定位：树节点必须真实存在才展开/闪烁（幻觉路径不做反应）
watch(treeReveal, (req) => {
  if (!req || !props.tree) return
  const node = findNode(props.tree.nodes, req.path)
  if (!node) return
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
        v-for="node in tree.nodes"
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
      <p v-if="tree.nodes.length === 0" class="ft-empty">
        代码库目录为空<br />右键此处新建文件或文件夹
      </p>
      <p v-if="tree.truncated" class="ft-truncated">
        文件过多，仅显示前 2000 个节点
      </p>
    </div>
  </aside>
</template>
