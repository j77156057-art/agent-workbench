<script setup lang="ts">
// 右栏「文件」页签：只读源码 / 图片预览，定位到目标行。
// 与问答页 FileViewer 同一套口径（/api/fs/file、1500 行上限、请求序号防串台），
// 但这里是常驻栏而不是弹窗：换引用只换内容，不重建整栏。
// 「在编辑器中打开」把动作交回编辑器（打开标签 + 定位），右栏不提供写入口。
import { computed, nextTick, ref, watch } from 'vue'
import { useWorkbench } from '../composables/workbench'
import { fsApi } from '../api'

const { rightRailFile, jumpToLine, revealPath } = useWorkbench()

const MAX_LINES = 1500
const loading = ref(false)
const error = ref('')
const allLines = ref<string[]>([])
const previewKind = ref<'text' | 'binary' | 'image'>('text')
const previewUrl = ref('')
const previewNote = ref('')
const codeEl = ref<HTMLElement | null>(null)
/** 请求序号：快速连点多个引用时，只有最后一次请求的结果能落到界面上。 */
let requestSeq = 0

const lines = computed(() => allLines.value.slice(0, MAX_LINES))
const totalLines = computed(() => allLines.value.length)
const lineOutOfRange = computed(
  () => !loading.value && !error.value && rightRailFile.value.line > lines.value.length && totalLines.value > 0,
)

function reset() {
  allLines.value = []
  previewKind.value = 'text'
  previewUrl.value = ''
  previewNote.value = ''
  error.value = ''
}

async function load() {
  const path = rightRailFile.value.path
  if (!path) {
    requestSeq++
    reset()
    loading.value = false
    return
  }
  const ticket = ++requestSeq
  loading.value = true
  reset()
  try {
    const r = await fsApi.read(path)
    if (ticket !== requestSeq) return
    previewKind.value = r.preview_kind === 'image' || r.preview_kind === 'binary' ? r.preview_kind : 'text'
    previewUrl.value = r.preview_url || ''
    previewNote.value = r.preview_note || ''
    allLines.value = (r.content || '').split('\n')
    loading.value = false
    await nextTick()
    if (ticket !== requestSeq) return
    if (rightRailFile.value.line <= MAX_LINES) {
      codeEl.value?.querySelector('.rr-ln.hl')?.scrollIntoView({ block: 'center' })
    }
  } catch (e) {
    if (ticket !== requestSeq) return
    error.value = (e as Error).message || '加载失败'
  } finally {
    if (ticket === requestSeq) loading.value = false
  }
}

watch(() => [rightRailFile.value.path, rightRailFile.value.line], load, { immediate: true })

async function openInEditor() {
  const { path, line } = rightRailFile.value
  if (!path) return
  try {
    await jumpToLine(path, line || 1)
    revealPath(path)
  } catch {
    // 文件可能已被删/移走：让文件树去报错，不吞掉用户的操作意图
  }
}
</script>

<template>
  <div class="rr-file">
    <header class="rr-file-head">
      <span class="rr-file-path" :title="rightRailFile.path">
        {{ rightRailFile.path || '未选择文件' }}<template v-if="rightRailFile.path && rightRailFile.line > 1"> : {{ rightRailFile.line }}</template>
      </span>
      <button
        v-if="rightRailFile.path"
        type="button"
        class="rr-file-open"
        title="在编辑器中打开：会打开同名标签并定位到目标行（可编辑）"
        @click="openInEditor"
      >在编辑器中打开</button>
    </header>

    <div v-if="!rightRailFile.path" class="rr-note">
      点开对话里的文件引用、文件产物，或改动里的「只读查看」，这里会显示内容。
    </div>
    <div v-else-if="loading" class="rr-note">加载中…</div>
    <div v-else-if="error" class="rr-err">
      {{ error }}<br><br>
      该文件可能不在当前项目的索引范围内，可到代码工作区确认项目目录。
    </div>
    <template v-else>
      <div v-if="lineOutOfRange" class="rr-note rr-note-warn" role="status">
        目标行 {{ rightRailFile.line }} 超出当前加载上限（{{ MAX_LINES }} 行），未定位。
      </div>
      <div v-if="previewKind === 'image'" class="rr-file-image">
        <img v-if="previewUrl" :src="previewUrl" :alt="rightRailFile.path" />
        <p v-else class="rr-note">图片预览地址缺失。</p>
        <p v-if="previewNote" class="rr-note">{{ previewNote }}</p>
      </div>
      <div v-else-if="previewKind === 'binary'" class="rr-note">
        {{ previewNote || '二进制文件，无法以文本预览。' }}<br><br>
        可到代码工作区用对应工具打开它。
      </div>
      <div v-else ref="codeEl" class="rr-code">
        <div v-for="(ln, i) in lines" :key="i" class="rr-ln" :class="{ hl: i + 1 === rightRailFile.line }">
          <span class="rr-num">{{ i + 1 }}</span><span class="rr-src">{{ ln || ' ' }}</span>
        </div>
        <div v-if="totalLines > MAX_LINES" class="rr-note">
          （文件共 {{ totalLines }} 行，仅显示前 {{ MAX_LINES }} 行）
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.rr-file { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; }
.rr-file-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-raised);
}
.rr-file-path {
  flex: 1 1 auto;
  min-width: 0;
  font-family: var(--font-code);
  font-size: var(--fs-sm);
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rr-file-open {
  flex: 0 0 auto;
  padding: 3px 9px;
  font-size: var(--fs-cap);
  border: 1px solid var(--border-strong);
  border-radius: 5px;
  background: var(--bg-raised);
  color: var(--text-muted);
  cursor: pointer;
}
.rr-file-open:hover { background: var(--bg-hover); color: var(--text); }
.rr-file-image {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 10px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.rr-file-image img { max-width: 100%; height: auto; border: 1px solid var(--border); background: #fff; }
.rr-code {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 6px 0 14px;
  font-family: var(--font-code);
  font-size: var(--fs-sm);
  line-height: 1.6;
}
.rr-ln { display: flex; gap: 10px; padding: 0 12px; }
.rr-num {
  flex: 0 0 40px;
  text-align: right;
  color: var(--text-faint);
  user-select: none;
}
.rr-src { flex: 1; min-width: 0; color: var(--text); white-space: pre-wrap; word-break: break-word; }
.rr-ln.hl { background: color-mix(in srgb, var(--accent) 12%, transparent); box-shadow: inset 3px 0 0 var(--accent); }
.rr-note { padding: 10px 14px; color: var(--text-faint); font-size: var(--fs-sm); line-height: 1.7; }
.rr-note-warn { color: var(--amber); }
.rr-err { padding: 14px; color: var(--amber); font-size: var(--fs-md); line-height: 1.7; }
</style>
