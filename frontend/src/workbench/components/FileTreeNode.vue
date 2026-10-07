<script setup lang="ts">
// 递归树节点：文件夹（chevron + 文件夹 glyph）/ 文件（类型 chip + git 状态点）。
// 分区根目录（depth=0 且带 region）使用分区色描边与中文区名。
import { computed, ref, watch } from 'vue'
import type { TreeNode, SemanticTagRecord } from '../api'
import { regionColor, regionTextColor, fileChip, gitState } from '../theme'

const props = defineProps<{
  node: TreeNode
  depth: number
  selectedPath: string | null
  openPaths: Set<string>
  flashPath?: string | null
  flashNonce?: number
  /** 阶段 1：全项目业务标签表（节点按自身路径取） */
  tagMap?: Record<string, SemanticTagRecord>
  /** 阶段 1：被大白话定位命中的文件集合（持续高亮） */
  locatedPaths?: Set<string>
}>()

const emit = defineEmits<{
  (e: 'toggle', path: string): void
  (e: 'select', node: TreeNode): void
  (e: 'contextmenu', ev: MouseEvent, node: TreeNode): void
}>()

const isRegionRoot = computed(
  () => props.depth === 0 && props.node.type === 'dir' && !!props.node.region,
)
const accent = computed(() => regionColor(props.node.region))
const accentText = computed(() => regionTextColor(props.node.region))
const chip = computed(() => fileChip(props.node.name))
const git = computed(() => gitState(props.node.tracked, props.node.dirty))
const isOpen = computed(() => props.openPaths.has(props.node.path))
const isSelected = computed(() => props.selectedPath === props.node.path)
const isFlashing = ref(false)
const indent = computed(() => 8 + props.depth * 13)

// 阶段 1：业务标签徽章（首个标签可见，其余进 title；文件改动后 stale 标签弱化）
const tag = computed<SemanticTagRecord | null>(() => props.tagMap?.[props.node.path] || null)
const located = computed(() => props.locatedPaths?.has(props.node.path) ?? false)
const firstTag = computed(() => tag.value?.tags?.[0] || '')
const tagTitle = computed(() => {
  const t = tag.value
  if (!t) return ''
  const lines = t.tags.map((x) => `#${x}`)
  if (t.summary) lines.push(t.summary)
  if (t.stale) lines.push('（文件已改动，标签待刷新）')
  if (t.origin === 'manual') lines.push('人工标签')
  return lines.join('\n')
})

// AI 引用定位：闪烁 + 滚动到可见区域（nonce 变化重新触发动画）
const rowEl = ref<HTMLElement | null>(null)
watch(
  () => props.flashNonce,
  (n) => {
    if (n && props.flashPath === props.node.path) {
      isFlashing.value = false
      requestAnimationFrame(() => {
        isFlashing.value = true
        rowEl.value?.scrollIntoView({ block: 'nearest' })
        window.setTimeout(() => { isFlashing.value = false }, 1800)
      })
    }
  },
)

function onClick() {
  if (props.node.type === 'dir') emit('toggle', props.node.path)
  emit('select', props.node)
}
</script>

<template>
  <div
    ref="rowEl"
    class="ft-row"
    :class="[
      `ft-${node.type}`,
      { 'ft-region-root': isRegionRoot, 'ft-selected': isSelected, 'ft-dir-open': isOpen,
        'ft-flash': isFlashing, 'ft-located': located },
    ]"
    :style="{ paddingLeft: indent + 'px' }"
    @click="onClick"
    @contextmenu.prevent.stop="emit('contextmenu', $event, node)"
  >
    <span
      v-if="isRegionRoot"
      class="ft-rail"
      :style="{ background: accent }"
      aria-hidden="true"
    />
    <span class="ft-chevron" :class="{ rotated: isOpen }" aria-hidden="true">
      <svg v-if="node.type === 'dir' && node.children.length" width="8" height="8" viewBox="0 0 8 8">
        <path d="M2 1 L5 4 L2 7" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </span>

    <!-- 文件夹 glyph -->
    <svg v-if="node.type === 'dir'" class="ft-folder" :style="isRegionRoot ? { color: accent } : null" width="15" height="14" viewBox="0 0 15 14" aria-hidden="true">
      <path d="M1 3.2 Q1 2.4 1.8 2.4 H5.6 L6.9 3.7 H13.2 Q14 3.7 14 4.5 V10.8 Q14 11.6 13.2 11.6 H1.8 Q1 11.6 1 10.8 Z"
            fill="currentColor" fill-opacity="0.22" stroke="currentColor" stroke-width="1" />
    </svg>

    <!-- 文件类型 chip -->
    <span v-else class="ft-chip" :class="`ft-chip-${chip.kind}`">{{ chip.label }}</span>

    <span class="ft-name" :title="node.path">{{ node.name }}</span>

    <!-- 分区根：中文区名 -->
    <span v-if="isRegionRoot && node.region_name" class="ft-region-tag" :style="{ color: accentText }">
      {{ node.region_name }}
    </span>

    <template v-else>
      <!-- 阶段 1：业务标签徽章（首个可见，悬浮看全部；stale 弱化） -->
      <span
        v-if="node.type === 'file' && firstTag"
        class="ft-tagbadge"
        :class="{ 'ft-tag-stale': tag?.stale, 'ft-tag-manual': tag?.origin === 'manual' }"
        :title="tagTitle"
      >#{{ firstTag }}</span>

      <!-- 文件 git 状态点 -->
      <span
        v-if="node.type === 'file' && git.dot !== 'none' && git.dot !== 'clean'"
        class="ft-gitdot"
        :class="`ft-git-${git.dot}`"
        :title="git.title"
      />
    </template>
  </div>

  <template v-if="node.type === 'dir' && isOpen">
    <FileTreeNode
      v-for="child in node.children"
      :key="child.path"
      :node="child"
      :depth="depth + 1"
      :selected-path="selectedPath"
      :open-paths="openPaths"
      :flash-path="flashPath"
      :flash-nonce="flashNonce"
      :tag-map="tagMap"
      :located-paths="locatedPaths"
      @toggle="emit('toggle', $event)"
      @select="emit('select', $event)"
      @contextmenu="(ev, n) => emit('contextmenu', ev, n)"
    />
  </template>
</template>
