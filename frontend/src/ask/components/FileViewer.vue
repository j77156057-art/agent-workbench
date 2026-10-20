<script setup lang="ts">
// 源码查看弹窗：点击答案里的文件引用后，只读展示项目内文件内容并高亮目标行。
// 与旧手写页 / 开发台一致走 /api/fs/file；问答页不提供编辑能力。
import { ref, watch, nextTick } from 'vue'
import { fsApi } from '../../workbench/api'

const props = defineProps<{ path: string; line: number }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const MAX_LINES = 1500
const loading = ref(true)
const error = ref('')
const lines = ref<string[]>([])
const totalLines = ref(0)
const codeEl = ref<HTMLElement | null>(null)

async function load() {
  loading.value = true
  error.value = ''
  lines.value = []
  try {
    const r = await fsApi.read(props.path)
    const all = (r.content || '').split('\n')
    totalLines.value = all.length
    lines.value = all.slice(0, MAX_LINES)
    await nextTick()
    if (props.line && codeEl.value) {
      codeEl.value.querySelector('.fv-ln.hl')?.scrollIntoView({ block: 'center' })
    }
  } catch (e) {
    error.value = (e as Error).message || '加载失败'
  } finally {
    loading.value = false
  }
}

watch(() => props.path, load, { immediate: true })
</script>

<template>
  <div class="ask-overlay" @click.self="emit('close')">
    <div class="ask-modal fv-modal">
      <div class="fv-head">
        <div class="fv-titles">
          <h2>源码查看</h2>
          <div class="fv-path">{{ path }}<template v-if="line"> : {{ line }}</template></div>
        </div>
        <button class="fv-x" title="关闭" @click="emit('close')">×</button>
      </div>
      <div v-if="loading" class="fv-note">加载中…</div>
      <div v-else-if="error" class="fv-err">
        {{ error }}<br><br>
        该文件可能不在当前项目的索引范围内，可到代码工作台确认项目目录。
      </div>
      <pre v-else ref="codeEl" class="fv-code"><div
        v-for="(ln, i) in lines"
        :key="i"
        class="fv-ln"
        :class="{ hl: i + 1 === line }"
      ><span class="fv-num">{{ i + 1 }}</span><span class="fv-src">{{ ln || ' ' }}</span></div>
<div v-if="totalLines > MAX_LINES" class="fv-note">（文件共 {{ totalLines }} 行，仅显示前 {{ MAX_LINES }} 行）</div></pre>
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
  border-radius: 6px;
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
.fv-err { padding: 16px 18px; color: var(--amber); font-size: var(--fs-md); line-height: 1.7; }
</style>
