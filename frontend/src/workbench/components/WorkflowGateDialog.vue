<script setup lang="ts">
// 工作流人工断点模态：方案选择 / 检索补充 / 计划审批与修改。
// 遮罩 pointer-events:none —— 弹窗背后的对话流仍可滚动查看，只有弹层本身拦截点击。
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import type { AcceptanceItem, WorkflowState } from '../api'

const props = defineProps<{
  state: WorkflowState
  busy: boolean
}>()
const emit = defineEmits<{
  (e: 'choose', choiceId: string, customText?: string): void
  (e: 'research-submit', findings: string): void
  (e: 'research-run'): void
  (e: 'approve'): void
  (e: 'reject'): void
  (e: 'revise', taskId: string, task: string, deps: string[]): void
  (e: 'revise-approve', approved: boolean): void
  (e: 'acceptance-save', items: AcceptanceItem[]): void
  (e: 'dismiss'): void
}>()

const customOpen = ref(false)
const customText = ref('')
const researchText = ref('')
const reviseOpen = ref(false)
const reviseTaskId = ref('')
const reviseTaskText = ref('')
const reviseTaskDeps = ref('')
const criteria = ref<AcceptanceItem[]>([])
const criteriaEditing = ref(false)
watch(() => props.state.acceptance_contract?.revision, () => {
  criteria.value = (props.state.acceptance_contract?.items || []).map(item => ({
    ...item, evidence: [...(item.evidence || [])],
  }))
  criteriaEditing.value = false
}, { immediate: true })
function addCriterion() {
  criteria.value.push({ id: `accept-user-${Date.now()}`, statement: '', method: '人工检查',
    evidence: [], required: true, user_approved: false })
}
function saveCriteria() {
  if (!criteria.value.length || criteria.value.some(item => !item.statement.trim())) return
  emit('acceptance-save', criteria.value.map(item => ({ ...item,
    evidence: item.evidence.filter(Boolean), user_approved: false })))
}

watch(() => props.state.status, () => {
  customOpen.value = false
  reviseOpen.value = false
})

/* ---- 键盘可达性：Esc 收起（等同 ×，可从卡片重新打开），Tab 焦点圈在弹窗内 ---- */
const boxRef = ref<HTMLElement | null>(null)
const FOCUSABLE = [
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')
function visibleFocusables(): HTMLElement[] {
  if (!boxRef.value) return []
  return [...boxRef.value.querySelectorAll<HTMLElement>(FOCUSABLE)]
    .filter(el => el.offsetParent !== null)
}
function restoreFocus() {
  nextTick(() => {
    const items = visibleFocusables()
    // 方案阶段优先聚焦「推荐」项，引导回车直达
    const preferred = isChoice.value
      ? boxRef.value?.querySelector<HTMLElement>('.wg-option:not(:disabled):has(.wg-tag)')
      : undefined
    ;(preferred || items[0] || boxRef.value)?.focus()
  })
}
function onBoxKeydown(ev: KeyboardEvent) {
  if (ev.key === 'Escape') {
    ev.preventDefault()
    emit('dismiss')
    return
  }
  if (ev.key !== 'Tab') return
  const items = visibleFocusables()
  if (!items.length) { ev.preventDefault(); return }
  const first = items[0]
  const last = items[items.length - 1]
  const active = document.activeElement as HTMLElement | null
  if (ev.shiftKey && (active === first || !boxRef.value?.contains(active))) {
    ev.preventDefault(); last.focus()
  } else if (!ev.shiftKey && active === last) {
    ev.preventDefault(); first.focus()
  }
}
onMounted(restoreFocus)
watch(() => props.state.status, restoreFocus)
watch([customOpen, reviseOpen], restoreFocus)

const isChoice = computed(() => ['awaiting_choice', 'generating_options'].includes(props.state.status))
const isResearch = computed(() => props.state.status === 'awaiting_research')
const isApproval = computed(() => ['awaiting_approval', 'planned'].includes(props.state.status))
const title = computed(() => {
  if (isChoice.value) return '请选择推进方案'
  if (isResearch.value) return '需要先补充联网资料'
  if (isApproval.value) return '请审核任务分工'
  return '工作流等待确认'
})
const editableTasks = computed(() =>
  (props.state.tasks || []).filter((t) => {
    const report = props.state.results || {}
    const results = (report.results as Record<string, Record<string, unknown>> | undefined) || report
    const r = results[String(t.id)]
    return !r || ['pending', 'blocked', 'failed'].includes(String(r.status || 'pending'))
  }))

function pickCustom() {
  if (!customOpen.value) { customOpen.value = true; return }
  const text = customText.value.trim()
  if (text) emit('choose', 'custom', text)
}
function submitRevise() {
  const id = reviseTaskId.value
  const text = reviseTaskText.value.trim()
  if (!id || !text) return
  const deps = reviseTaskDeps.value.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean)
  emit('revise', id, text, deps)
}
</script>

<template>
  <!-- Teleport 到 body：卡片自身 overflow:hidden、对话台折叠态 33px 裁剪都可能
       吞掉 position:fixed 遮罩（与会话历史弹层同策略），必须脱离裁剪容器。 -->
  <Teleport to="body">
  <div class="wg-mask wb-modal-backdrop">
    <div
      ref="boxRef"
      class="wg-box wb-modal-shell"
      :class="{ 'wg-busy-on': busy }"
      role="dialog"
      aria-modal="true"
      :aria-label="title"
      :aria-busy="busy"
      tabindex="-1"
      @keydown="onBoxKeydown"
    >
      <header class="wg-head">
        <h3>{{ title }}</h3>
        <span v-if="busy" class="wg-busy" aria-live="polite">
          <span class="wg-busy-dot" />正在提交，可收起窗口等待
        </span>
        <button class="wg-x" title="收起弹窗，选择会在后台继续，可随时从卡片重新打开" @click="emit('dismiss')">×</button>
      </header>

      <!-- 方案选择 -->
      <template v-if="isChoice">
        <p class="wg-goal">{{ state.request || '完整推进这个开发目标' }}</p>
        <p v-if="state.status === 'generating_options'" class="wg-hint">
          <span class="wg-hint-dot" />AI 仍在精炼方案，也可以直接点选一个方案立即开始
        </p>
        <div class="wg-options">
          <button
            v-for="option in (state.options || [])"
            :key="option.id"
            class="wg-option"
            :disabled="busy"
            :title="state.status === 'generating_options' ? '方案仍在精炼，可直接选择当前方案' : undefined"
            @click="emit('choose', option.id)"
          >
            <b>
              {{ option.title }}
              <em v-if="option.recommended" class="wg-tag">推荐</em>
              <em v-if="option.requires_web" class="wg-tag wg-tag-web">需联网</em>
            </b>
            <span>{{ option.summary }}</span>
          </button>
        </div>
        <div v-if="customOpen" class="wg-custom">
          <textarea v-model="customText" rows="3" placeholder="描述你真正想要的效果、限制或参考作品…" />
          <div class="wg-row">
            <button class="wg-btn" :disabled="busy" @click="customOpen = false">返回</button>
            <button class="wg-btn wg-primary" :disabled="busy || !customText.trim()" @click="pickCustom">提交自定义目标</button>
          </div>
        </div>
        <button v-else class="wg-btn wg-ghost" :disabled="busy" @click="pickCustom">我自己描述目标</button>
      </template>

      <!-- 联网检索补充 -->
      <template v-else-if="isResearch">
        <p class="wg-goal">{{ state.request }}</p>
        <textarea v-model="researchText" rows="4" placeholder="粘贴联网检索摘要或来源链接…" />
        <div class="wg-row">
          <button class="wg-btn" :disabled="busy" @click="emit('research-run')">自动检索</button>
          <button class="wg-btn wg-primary" :disabled="busy || !researchText.trim()" @click="emit('research-submit', researchText)">提交检索结果</button>
        </div>
      </template>

      <!-- 任务审批 / 修改 -->
      <template v-else-if="isApproval">
        <div v-if="state.pending_tasks?.length" class="wg-pending">
          <b>待审核的任务 DAG 修改</b>
          <span v-for="task in state.pending_tasks" :key="String(task.id)" class="wg-pending-item">
            {{ task.id }} · {{ task.task }}
          </span>
          <div class="wg-row">
            <button class="wg-btn wg-danger" :disabled="busy" @click="emit('revise-approve', false)">拒绝修改</button>
            <button class="wg-btn wg-primary" :disabled="busy" @click="emit('revise-approve', true)">批准重排</button>
          </div>
        </div>
        <template v-else>
          <div class="wg-tasks">
            <div v-for="task in (state.tasks || [])" :key="String(task.id)" class="wg-task">
              <b>{{ task.id }}</b>
              <div class="wg-task-main">
                <span class="wg-task-role">{{ task.role }}<template v-if="task.persona"> · {{ task.persona }}</template></span>
                <span>{{ task.task }}</span>
                <small v-if="Array.isArray(task.depends_on) && task.depends_on.length">依赖：{{ task.depends_on.join(', ') }}</small>
              </div>
            </div>
          </div>
          <section class="wg-acceptance">
            <div class="wg-acceptance-head">
              <b>验收条件</b>
              <button class="wg-btn" :disabled="busy" @click="criteriaEditing = !criteriaEditing">
                {{ criteriaEditing ? '收起编辑' : '编辑条件' }}
              </button>
            </div>
            <p>模型根据任务提出条件。请检查完成效果和证据，再决定是否通过最终验收。</p>
            <div v-for="(item, index) in criteria" :key="item.id" class="wg-criterion">
              <template v-if="criteriaEditing">
                <label>条件 {{ index + 1 }}<textarea v-model="item.statement" rows="2" maxlength="1000" /></label>
                <label>检查方法<input v-model="item.method" maxlength="200" /></label>
                <label>预期证据<input :value="item.evidence.join('，')" @input="item.evidence = ($event.target as HTMLInputElement).value.split(/[,，]/).map(s => s.trim()).filter(Boolean)" /></label>
                <label class="wg-check"><input v-model="item.required" type="checkbox" />必须满足</label>
                <button class="wg-btn wg-danger" :disabled="criteria.length <= 1" @click="criteria.splice(index, 1)">删除</button>
              </template>
              <template v-else><b>{{ item.required ? '必须' : '可选' }}</b><span>{{ item.statement }}</span><small>{{ item.method }} · {{ item.evidence.join('、') || '待补证据' }}</small></template>
            </div>
            <div v-if="criteriaEditing" class="wg-row">
              <button class="wg-btn" @click="addCriterion">增加条件</button>
              <button class="wg-btn wg-primary" :disabled="busy || !criteria.length || criteria.some(i => !i.statement.trim()) || !criteria.some(i => i.required)" @click="saveCriteria">保存验收条件</button>
            </div>
          </section>
          <div v-if="reviseOpen" class="wg-revise">
            <select v-model="reviseTaskId">
              <option disabled value="">选择未执行任务</option>
              <option v-for="task in editableTasks" :key="String(task.id)" :value="String(task.id)">
                {{ task.id }} · {{ String(task.task).slice(0, 40) }}
              </option>
            </select>
            <textarea v-model="reviseTaskText" rows="2" placeholder="新的任务目标与验收标准" />
            <input v-model="reviseTaskDeps" placeholder="依赖任务 ID，逗号分隔；没有可留空" />
            <div class="wg-row">
              <button class="wg-btn" :disabled="busy" @click="reviseOpen = false">取消</button>
              <button class="wg-btn" :disabled="busy || !reviseTaskId || !reviseTaskText.trim()" @click="submitRevise">提交修改</button>
            </div>
            <small>修改只会生成待审核版本，不会直接覆盖正在执行的任务。</small>
          </div>
          <div class="wg-row wg-foot">
            <button class="wg-btn wg-ghost" :disabled="busy || !state.tasks?.length" @click="reviseOpen = !reviseOpen">修改任务</button>
            <span class="wg-spacer" />
            <button class="wg-btn" :disabled="busy" @click="emit('reject')">暂不执行</button>
            <button class="wg-btn wg-primary" :disabled="busy || criteriaEditing" @click="emit('approve')">
              {{ state.status === 'planned' ? '确认执行' : '批准并开始执行' }}
            </button>
          </div>
        </template>
      </template>
    </div>
  </div>
  </Teleport>
</template>

<style scoped>
/* 遮罩不拦截：背后对话可继续滚动；仅居中弹层接收点击 */
.wg-mask {
  position: fixed; inset: 0; z-index: 1100;
  display: flex; align-items: center; justify-content: center;
  background: rgba(38, 52, 77, .30);
  backdrop-filter: blur(1.5px);
  pointer-events: none;
  animation: wg-fade .12s ease-out;
}
.wg-box {
  pointer-events: auto;
  outline: none;
  width: 560px; max-width: calc(100vw - 48px);
  max-height: min(78vh, 720px); overflow-y: auto;
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow-pop);
  padding: 18px 20px 16px;
  animation: wg-rise .18s cubic-bezier(.2,.8,.2,1);
  scrollbar-gutter: stable;
}
.wg-box:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
@keyframes wg-fade { from { opacity: 0; } to { opacity: 1; } }
@keyframes wg-rise {
  from { opacity: 0; transform: translateY(8px) scale(.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
.wg-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.wg-head h3 { margin: 0; font-size: 15px; font-weight: 700; color: var(--text); }
.wg-busy {
  display: inline-flex; align-items: center; gap: 6px;
  margin-left: auto;
  font-size: 12px; font-weight: 500; color: var(--accent);
}
.wg-busy-dot {
  width: 9px; height: 9px; border-radius: 50%;
  border: 2px solid var(--accent); border-top-color: transparent;
  animation: wg-busy-spin .7s linear infinite;
}
@keyframes wg-busy-spin { to { transform: rotate(360deg); } }
.wg-busy-on { opacity: .85; }
.wg-x {
  border: 0; background: transparent; color: var(--text-faint);
  font-size: 18px; line-height: 1; cursor: pointer; padding: 2px 6px; border-radius: 6px; transition: color .15s ease, background .15s ease, transform .15s ease;
}
.wg-x:hover:not(:disabled) { color: var(--text); background: var(--bg-hover); transform: rotate(90deg); }
.wg-x:disabled { cursor: default; }
.wg-goal {
  margin: 10px 0 12px; padding: 9px 11px;
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--bg-surface, var(--bg-hover));
  font-size: 12px; line-height: 1.6; color: var(--text);
}
.wg-hint {
  margin: -4px 0 10px; display: flex; align-items: center; gap: 7px;
  font-size: 12px; color: var(--accent);
}
.wg-hint-dot {
  width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto;
  border: 2px solid var(--accent); border-top-color: transparent;
  animation: wg-busy-spin .7s linear infinite;
}
.wg-options { display: grid; gap: 8px; }
.wg-option {
  display: block; text-align: left; width: 100%;
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg-raised); padding: 10px 12px; cursor: pointer;
  transition: border-color .16s ease, background .16s ease, transform .16s ease, box-shadow .16s ease;
}
.wg-option:hover:not(:disabled) { border-color: var(--accent); background: var(--bg-selected); transform: translateY(-1px); box-shadow: 0 4px 12px rgba(35,52,84,.08); }
.wg-option:active:not(:disabled) { transform: scale(.995); }
.wg-option:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.wg-option:has(.wg-tag) { border-color: rgba(47,111,237,.4); background: color-mix(in srgb, var(--accent) 4%, var(--bg-raised)); }
.wg-option:disabled { opacity: .6; cursor: default; }
.wg-option b { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text); }
.wg-option span { display: block; margin-top: 4px; font-size: 12px; line-height: 1.55; color: var(--text-muted); }
.wg-tag {
  font-style: normal; font-size: 10px; font-weight: 600;
  padding: 1px 6px; border-radius: 99px;
  color: #fff; background: var(--accent);
}
.wg-tag-web { background: var(--amber); }
.wg-custom { margin-top: 10px; display: grid; gap: 8px; }
.wg-tasks { margin: 10px 0; display: grid; gap: 7px; max-height: 38vh; overflow-y: auto; }
.wg-task { display: flex; gap: 10px; padding: 9px 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg-surface, var(--bg-hover)); transition: border-color .15s ease, background .15s ease, transform .15s ease; }.wg-task:hover { border-color: var(--border-strong); background: var(--bg-selected); transform: translateX(2px); }
.wg-task b { flex: 0 0 auto; font-size: 11px; color: var(--accent); font-family: ui-monospace, monospace; padding-top: 1px; }
.wg-task-main { min-width: 0; display: grid; gap: 2px; font-size: 12px; line-height: 1.5; }
.wg-task-role { font-size: 11px; color: var(--text-faint); font-weight: 600; }
.wg-task-main small { color: var(--text-faint); font-size: 10px; }
.wg-pending { display: grid; gap: 7px; margin: 8px 0; padding: 10px; border: 1px dashed var(--amber); border-radius: 9px; background: rgba(214,158,46,.07); }
.wg-pending-item { font-size: 12px; color: var(--text-muted); }
.wg-revise { display: grid; gap: 7px; margin: 10px 0; padding: 10px; border: 1px solid var(--border); border-radius: 9px; }
.wg-revise small { color: var(--text-faint); font-size: 10px; }
textarea, input, select {
  width: 100%; box-sizing: border-box;
  border: 1px solid var(--border); border-radius: 7px;
  background: var(--bg-raised); color: var(--text);
  font: inherit; font-size: 12px; padding: 7px 9px; resize: vertical;
}
textarea:focus, input:focus, select:focus { outline: none; border-color: var(--accent); }
.wg-row { display: flex; gap: 8px; justify-content: flex-end; margin-top: 4px; }
.wg-foot { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); background: linear-gradient(180deg, transparent, var(--bg-raised) 28%); position: sticky; bottom: -16px; padding-bottom: 2px; }
.wg-spacer { flex: 1; }
.wg-btn {
  height: 30px; padding: 0 14px; border-radius: 7px;
  border: 1px solid var(--border-strong); background: var(--bg-raised);
  color: var(--text); font-size: 12px; cursor: pointer; transition: color .15s ease, border-color .15s ease, background .15s ease, transform .15s ease, box-shadow .15s ease;
}
.wg-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); transform: translateY(-1px); box-shadow: 0 3px 8px rgba(35,52,84,.08); }
.wg-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.wg-btn:disabled { opacity: .55; cursor: default; }
.wg-primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
.wg-primary:hover:not(:disabled) { background: var(--accent-strong, var(--accent)); color: #fff; }
.wg-danger { border-color: var(--danger); color: var(--danger); }
.wg-danger:hover:not(:disabled) { background: var(--danger); color: #fff; }
.wg-ghost { border-style: dashed; }
.wg-acceptance { display: grid; gap: 7px; padding: 10px; border: 1px solid var(--border); border-radius: 9px; }
.wg-acceptance-head { display: flex; align-items: center; justify-content: space-between; }
.wg-acceptance-head b { font-size: 12px; }
.wg-acceptance p { margin: 0; color: var(--text-muted); font-size: 11px; }
.wg-criterion { display: grid; gap: 4px; padding: 7px 0; border-top: 1px solid var(--border); font-size: 12px; }
.wg-criterion small { color: var(--text-faint); }
.wg-criterion label { display: grid; gap: 4px; }
.wg-criterion .wg-check { display: flex; align-items: center; }
.wg-check input { width: auto; }
@media (max-width: 620px) {
  .wg-mask { align-items: flex-end; }
  .wg-box { width: 100%; max-width: none; max-height: 88vh; border-radius: 14px 14px 0 0; padding: 15px 14px 12px; }
  .wg-row { flex-wrap: wrap; }
  .wg-row .wg-btn { flex: 1 1 auto; }
  .wg-foot { bottom: -12px; }
}
@media (prefers-reduced-motion: reduce) {
  .wg-box, .wg-mask, .wg-option, .wg-task, .wg-x, .wg-btn { transition: none; animation: none; }
  .wg-busy-dot, .wg-hint-dot { animation: none; }
}
</style>
