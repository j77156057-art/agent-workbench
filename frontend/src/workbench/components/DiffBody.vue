<script setup lang="ts">
// 统一 diff 渲染：文件头（路径 / 新建·修改 / 增删统计）+ 逐行对照。
// 抽自 FileDiffDialog —— 弹窗与右侧预览栏共用同一套渲染，避免两处口径漂移。
// 数据来自 observation 事件的 file_diffs（结构化字段，非正文二次解析）：
//   - 正常条目带 old/new 全文 → diffLines（与「AI 改写」同一套 LCS 引擎）逐行渲染；
//   - 超大文件后端只回摘要 diff、不下发全文（truncated=true）→ 回退纯文本摘要，
//     诚实告诉用户「内容过大只给摘要」，不假装能逐行对照。
import { computed } from 'vue'
import type { FileDiffEntry } from '../composables/chat-types'
import { diffLines } from '../diff'

const props = defineProps<{
  entries: FileDiffEntry[]
  /** 文件头右侧的动作按钮文案（如右栏的「只读查看」）；不传则不显示按钮。 */
  actionLabel?: string
}>()
const emit = defineEmits<{ (e: 'action', entry: FileDiffEntry): void }>()

interface RenderedFile {
  path: string
  kind: FileDiffEntry['kind']
  truncated: boolean
  rows: ReturnType<typeof diffLines>['rows']
  stats: ReturnType<typeof diffLines>['stats']
  summary: string
}

const files = computed<RenderedFile[]>(() =>
  (props.entries || []).map((e) => {
    const hasFull = typeof e.old === 'string' && typeof e.new === 'string'
    if (hasFull && !e.truncated) {
      const d = diffLines(e.old as string, e.new as string)
      return { path: e.path, kind: e.kind, truncated: false, rows: d.rows, stats: d.stats, summary: e.diff }
    }
    // 超大 / 无全文：只呈现后端给的摘要 diff 文本
    return { path: e.path, kind: e.kind, truncated: true, rows: [], stats: { adds: 0, dels: 0, same: 0, unchanged: false }, summary: e.diff }
  }),
)
</script>

<template>
  <div class="fd-body">
    <p v-if="!files.length" class="fd-empty">没有可显示的改动</p>
    <section v-for="(f, fi) in files" :key="fi" class="fd-file">
      <div class="fd-file-head">
        <span class="fd-file-badge" :data-kind="f.kind">{{ f.kind === 'create' ? '新建' : '修改' }}</span>
        <span class="fd-file-path" :title="f.path">{{ f.path }}</span>
        <span v-if="!f.truncated" class="fd-file-stat">
          <span class="fd-add">+{{ f.stats.adds }}</span>
          <span class="fd-del">−{{ f.stats.dels }}</span>
        </span>
        <button
          v-if="actionLabel"
          type="button"
          class="fd-file-action"
          :title="`在预览栏只读查看 ${f.path}`"
          @click="emit('action', entries[fi])"
        >{{ actionLabel }}</button>
      </div>

      <div v-if="f.truncated" class="fd-fallback">
        <div class="fd-fallback-note">文件过大，仅提供改动摘要（未下发完整前后内容）</div>
        <pre class="fd-fallback-diff">{{ f.summary || '（无差异摘要）' }}</pre>
      </div>
      <div v-else class="fd-diff">
        <div v-for="(r, ri) in f.rows" :key="ri" class="fd-row" :class="`fd-${r.kind}`">
          <span class="fd-gno">{{ r.oldNo ?? '' }}</span>
          <span class="fd-gno">{{ r.newNo ?? '' }}</span>
          <span class="fd-sign">{{ r.kind === 'same' ? '' : r.kind === 'del' ? '−' : '+' }}</span>
          <code class="fd-code">{{ r.text || ' ' }}</code>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.fd-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  background: #f6f8fb;
  padding: 4px 0 12px;
}
.fd-empty { margin: 14px 16px; color: var(--text-faint); font-size: var(--fs-sm); }
.fd-file { border-bottom: 1px solid var(--border); }
.fd-file:last-child { border-bottom: 0; }
.fd-file-head {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 16px;
  background: color-mix(in srgb, var(--text) 5%, var(--bg-raised));
  border-bottom: 1px solid var(--border);
  font-size: var(--fs-sm);
}
.fd-file-badge {
  flex: 0 0 auto;
  font-size: var(--fs-cap);
  padding: 0 6px;
  line-height: 17px;
  border-radius: 5px;
  border: 1px solid var(--border-strong);
  color: var(--text-muted);
}
.fd-file-badge[data-kind="create"] { color: var(--green); border-color: color-mix(in srgb, var(--green) 40%, var(--bg-raised)); }
.fd-file-path {
  flex: 1 1 auto;
  min-width: 0;
  font-family: var(--font-code);
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fd-file-stat { flex: 0 0 auto; display: inline-flex; gap: 8px; font-family: var(--font-code); font-weight: 600; }
.fd-file-action {
  flex: 0 0 auto;
  padding: 1px 8px;
  font-size: var(--fs-cap);
  border: 1px solid var(--border-strong);
  border-radius: 5px;
  background: var(--bg-raised);
  color: var(--text-muted);
  cursor: pointer;
}
.fd-file-action:hover { background: var(--bg-hover); color: var(--text); }
.fd-add { color: var(--green); }
.fd-del { color: var(--danger); }

.fd-diff {
  font-family: var(--font-code);
  font-size: var(--fs-sm);
  line-height: 1.65;
  padding: 6px 0;
}
.fd-row { display: flex; align-items: stretch; white-space: pre; }
.fd-gno {
  flex: 0 0 42px;
  text-align: right;
  padding: 0 8px;
  color: var(--text-muted);
  user-select: none;
  font-size: var(--fs-cap);
}
.fd-sign { flex: 0 0 18px; text-align: center; user-select: none; }
.fd-code { flex: 1 1 auto; padding-right: 16px; color: #243044; }
.fd-del { background: color-mix(in srgb, var(--danger) 12%, var(--bg-raised)); }
.fd-del .fd-sign, .fd-del .fd-code { color: var(--danger); }
.fd-add { background: color-mix(in srgb, var(--green) 12%, var(--bg-raised)); }
.fd-add .fd-sign, .fd-add .fd-code { color: var(--green); }

.fd-fallback { padding: 8px 16px; }
.fd-fallback-note { font-size: var(--fs-sm); color: var(--amber); margin-bottom: 6px; }
.fd-fallback-diff {
  margin: 0;
  font-family: var(--font-code);
  font-size: var(--fs-sm);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-muted);
}
</style>
