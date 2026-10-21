<script setup lang="ts">
// 产物卡：本轮模型生成的文件、截取的图。
//
// 为什么单独一个组件：工作台对话台与问答首页要显示同一批产物，
// 而两边外层的样式体系不同（workbench 的 token vs ask 的 brutal 描边）。
// 把"怎么判定、怎么排、怎么点"的口径收在 lib/artifactView.ts（纯函数，可单测），
// 这里只负责摆位置。
import { computed } from 'vue'
import type { ChatArtifact } from '../composables/chat-types.ts'
import {
  isImageArtifact,
  artifactRawUrl,
  artifactShortName,
  artifactCaption,
  visibleArtifacts,
} from '../lib/artifactView.ts'

const props = defineProps<{
  artifacts?: ChatArtifact[]
  /** 点文件产物时的回调（外层决定怎么打开：跳到工作台 / 弹只读预览） */
  open?: (a: ChatArtifact) => void
}>()

const shown = computed(() => visibleArtifacts(props.artifacts))
</script>

<template>
  <div v-if="shown.length" class="ac-row">
    <template v-for="(a, i) in shown" :key="a.id || a.path || i">
      <!-- 图片：直接看图。点开走原图（新标签），不用去路径里翻。 -->
      <a
        v-if="isImageArtifact(a) && artifactRawUrl(a)"
        class="ac-img"
        :href="artifactRawUrl(a)"
        target="_blank"
        rel="noopener"
        :title="a.summary || a.label || a.path"
      >
        <img :src="artifactRawUrl(a)" :alt="a.label || artifactShortName(a)" loading="lazy" />
        <span class="ac-cap">{{ a.label || artifactShortName(a) }}</span>
      </a>
      <!-- 其余产物（文件/改动/代码）：点开由外层决定（跳工作台 / 弹预览） -->
      <button
        v-else
        type="button"
        class="ac-file"
        :title="a.summary || a.path || a.label"
        @click="open?.(a)"
      >
        <svg class="ac-ico" viewBox="0 0 24 24" width="13" height="13" aria-hidden="true">
          <path fill="currentColor" d="M6 2h8l4 4v16H6V2zm7 1.5V7h3.5L13 3.5zM8 11h8v1.5H8V11zm0 3.5h8V16H8v-1.5zm0 3.5h5v1.5H8V18z" />
        </svg>
        <span class="ac-name">{{ artifactShortName(a) }}</span>
        <span v-if="artifactCaption(a)" class="ac-sum">{{ artifactCaption(a) }}</span>
      </button>
    </template>
  </div>
</template>

<style scoped>
/* 产物区：贴在回答正文下面的一行卡片。
   图片给缩略图（能直接看见），文件给名字 + 一句话说明（能点开）。 */
.ac-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}
.ac-img {
  display: flex;
  flex-direction: column;
  gap: 3px;
  max-width: 180px;
  text-decoration: none;
}
.ac-img img {
  width: 180px;
  height: 118px;
  object-fit: cover;
  border-radius: var(--r-sm, 6px);
  border: 1px solid var(--border, var(--border-strong));
  background: var(--bg, var(--bg-raised));
  display: block;
}
.ac-img:hover img { filter: brightness(1.06); }
.ac-cap {
  font-size: var(--fs-cap, 11px);
  color: var(--text-faint, var(--text-muted));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ac-file {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 320px;
  padding: 4px 9px;
  border: 1px solid var(--border, var(--border-strong));
  border-radius: var(--r-sm, 6px);
  background: var(--bg, var(--bg-raised));
  color: var(--text-muted, var(--text));
  font-size: var(--fs-cap, 11px);
  cursor: pointer;
  text-align: left;
}
.ac-file:hover { border-color: var(--border-strong); color: var(--text); }
.ac-ico { flex: 0 0 auto; opacity: .75; }
.ac-name {
  font-family: var(--font-mono, monospace);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 160px;
}
.ac-sum {
  color: var(--text-faint, var(--text-muted));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 150px;
}
</style>
