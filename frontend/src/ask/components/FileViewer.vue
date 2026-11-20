<script setup lang="ts">
// 源码查看弹窗：点击答案里的文件引用后，只读展示项目内文件内容并高亮目标行。
// 与旧手写页 / 开发台一致走 /api/fs/file；问答页不提供编辑能力。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { fsApi } from '../../workbench/api'
import { getCachedFile, rememberFile } from '../fileCache'

const props = defineProps<{ path: string; line: number }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const MAX_LINES = 1500
const loading = ref(true)
const error = ref('')
const lines = ref<string[]>([])
const totalLines = ref(0)
const codeEl = ref<HTMLElement | null>(null)
const modalEl = ref<HTMLElement | null>(null)
const closeBtn = ref<HTMLButtonElement | null>(null)
let requestSeq = 0
let returnFocus: HTMLElement | null = null

// 两种「没定位到」要分开说：文件本身没这一行 vs 文件被 1500 行上限截断。
// 早先合成一条「超出当前加载上限（1500 行）」，在 100 行的文件上引用第 835 行
// 也这么说 —— 用户会以为文件有 1500 行，实际是引用写错了。
const lineMissing = computed(() =>
  !loading.value && !error.value && totalLines.value > 0 && props.line > totalLines.value,
)
const lineTruncated = computed(() =>
  !loading.value && !error.value && !lineMissing.value && props.line > MAX_LINES,
)

// 弹窗点开 → Esc → 又点同一文件的另一条引用时，组件会被重建；没有缓存就要把整份
// 文件重读一遍（引用同一文件的两行很常见）。缓存放在独立模块（见 fileCache.ts：
// <script setup> 里的顶层状态会随组件卸载消失），并按 mtime 校验：
// 文件被 Agent 改过就自动重读，不会显示旧内容。
function applyContent(content: string) {
  const all = content.split('\n')
  totalLines.value = all.length
  lines.value = all.slice(0, MAX_LINES)
  loading.value = false
}

async function revealLoaded(ticket: number) {
  await nextTick()
  if (ticket !== requestSeq) return
  // 换文件时先回顶部；有目标行再由 scrollTarget 滚过去
  if (codeEl.value) codeEl.value.scrollTop = 0
  scrollTarget()
}

async function load() {
  const ticket = ++requestSeq
  error.value = ''
  const cached = getCachedFile(props.path)
  if (cached) {
    try {
      // 一次 stat（不读内容）确认文件没变；变了就落到下面重读
      const st = await fsApi.stat(props.path)
      if (ticket !== requestSeq) return
      if (st.exists && st.mtime === cached.mtime) {
        applyContent(cached.content)
        await revealLoaded(ticket)
        return
      }
    } catch {
      /* stat 失败按未命中处理：宁可多读一次，也不显示可能过期的内容 */
    }
  }
  loading.value = true
  lines.value = []
  totalLines.value = 0
  try {
    const r = await fsApi.read(props.path)
    if (ticket !== requestSeq) return
    const content = r.content || ''
    rememberFile(props.path, r.mtime, content)
    applyContent(content)
    await revealLoaded(ticket)
  } catch (e) {
    if (ticket !== requestSeq) return
    error.value = (e as Error).message || '加载失败'
  } finally {
    if (ticket === requestSeq) loading.value = false
  }
}

/** 把高亮行滚到可视区中间；行号不存在/被截断时没有 .hl，天然什么都不做。 */
function scrollTarget() {
  if (!props.line || props.line > MAX_LINES) return
  codeEl.value?.querySelector('.fv-ln.hl')?.scrollIntoView({ block: 'center' })
}

// 只有「换文件」才重新请求。同一文件换行（引用点同一文件的不同行很常见）只滚动，
// 不再把整份文件重拉一遍。
watch(() => props.path, load, { immediate: true })
watch(() => props.line, async () => {
  if (loading.value || error.value) return
  await nextTick()
  scrollTarget()
})

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('close')
    return
  }
  if (e.key !== 'Tab') return
  const root = modalEl.value
  if (!root) return
  const items = Array.from(root.querySelectorAll<HTMLElement>(
    'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
  )).filter((el) => el.getClientRects().length > 0)
  if (!items.length) return
  const first = items[0]
  const last = items[items.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}

onMounted(async () => {
  returnFocus = document.activeElement as HTMLElement | null
  await nextTick()
  closeBtn.value?.focus()
  window.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  if (returnFocus?.isConnected) returnFocus.focus()
})
</script>

<template>
  <div class="ask-overlay" @click.self="emit('close')">
    <div ref="modalEl" class="ask-modal fv-modal" role="dialog" aria-modal="true" aria-labelledby="fv-title">
      <div class="fv-head">
        <div class="fv-titles">
          <h2 id="fv-title">源码查看</h2>
          <div class="fv-path">{{ path }}<template v-if="line"> : {{ line }}</template></div>
        </div>
        <button ref="closeBtn" class="fv-x" type="button" title="关闭" aria-label="关闭源码查看" @click="emit('close')">×</button>
      </div>
      <div v-if="loading" class="fv-note">加载中…</div>
      <div v-else-if="error" class="fv-err">
        {{ error }}<br><br>
        该文件可能不在当前项目的索引范围内，可到代码工作台确认项目目录。
      </div>
      <template v-else>
        <div v-if="lineMissing" class="fv-note fv-note-warn" role="status">
          目标行 {{ line }} 不存在：该文件共 {{ totalLines }} 行，引用里的行号可能写错了。
        </div>
        <div v-else-if="lineTruncated" class="fv-note fv-note-warn" role="status">
          目标行 {{ line }} 超出当前加载上限（{{ MAX_LINES }} 行），未定位；文件共 {{ totalLines }} 行。
        </div>
        <pre ref="codeEl" class="fv-code"><div
          v-for="(ln, i) in lines"
          :key="i"
          class="fv-ln"
          :class="{ hl: i + 1 === line }"
        ><span class="fv-num">{{ i + 1 }}</span><span class="fv-src">{{ ln || ' ' }}</span></div>
<div v-if="totalLines > MAX_LINES" class="fv-note">（文件共 {{ totalLines }} 行，仅显示前 {{ MAX_LINES }} 行）</div></pre>
      </template>
    </div>
  </div>
</template>

<style scoped>
.fv-modal { width: min(880px, 94vw); }
.fv-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 14px 18px 10px;
  border-bottom: 1px solid var(--border);
}
.fv-titles h2 { margin: 0; font-size: var(--fs-h3); font-weight: 600; }
.fv-path {
  margin-top: 2px;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  color: var(--text-muted);
  word-break: break-all;
}
.fv-x {
  border: none;
  background: none;
  font-size: var(--fs-h1);
  line-height: 1;
  color: var(--text-faint);
  cursor: pointer;
  padding: 2px 6px;
  border-radius: var(--r-sm);
}
.fv-x:hover { background: var(--bg-hover); color: var(--text); }
.fv-code {
  margin: 0;
  padding: 8px 0;
  overflow: auto;
  max-height: 70vh;
  background: var(--bg-raised);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: 1.6;
}
.fv-ln { display: flex; gap: 12px; padding: 0 14px; }
.fv-num {
  flex: 0 0 44px;
  text-align: right;
  color: var(--text-faint);
  user-select: none;
}
.fv-src { flex: 1; min-width: 0; color: var(--text); white-space: pre-wrap; word-break: break-word; }
.fv-ln.hl { background: color-mix(in srgb, var(--accent) 12%, transparent); box-shadow: inset 3px 0 0 var(--accent); }
.fv-note { padding: 10px 16px; color: var(--text-faint); font-size: var(--fs-sm); }
.fv-note-warn { color: var(--amber); }
.fv-err { padding: 16px 18px; color: var(--amber); font-size: var(--fs-md); line-height: 1.7; }
</style>
