<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { askConfirm } from '../composables/dialogs'
import { memoryPanelApi } from '../api'
import type {
  MemoryContextResult,
  MemoryPanelHook,
  MemoryPanelItem,
  MemoryPanelTaskItem,
} from '../api'

const props = defineProps<{ visible: boolean; sessionId: string }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const mode = ref<'next' | 'last'>('next')
const data = ref<MemoryContextResult | null>(null)
const active = ref('L0')
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')

const layers = computed(() => data.value?.layers || [])
const current = computed(() => layers.value.find(layer => layer.key === active.value) || layers.value[0] || null)
const totalChars = computed(() => data.value?.total_chars || 0)
const l2Items = computed(() => (current.value?.items || []) as MemoryPanelItem[])
const l4Items = computed(() => (current.value?.items || []) as MemoryPanelHook[])
const l3Items = computed(() => (current.value?.items || []) as MemoryPanelTaskItem[])
const l4Errors = computed(() => (current.value?.errors || []) as Array<Record<string, unknown>>)
const retrievableItems = computed(() => (current.value?.retrievable_items || []) as Array<Record<string, unknown>>)

const constitutionDraft = ref('')
const profileDraft = ref({
  display_name: '', language: '', timezone: '', location: '', notes: '',
})
const itemDrafts = ref<Record<string, string>>({})
const hookDrafts = ref<Record<string, string>>({})
const taskDrafts = ref<Record<string, string>>({})
const newHook = ref('')
const newHookKind = ref('fact')

const STATE_LABELS: Record<string, string> = {
  todo: '待办', doing: '进行中', done_pending_merge: '待验收', done: '已完成',
}

function stateLabel(state: unknown): string {
  return STATE_LABELS[String(state || '')] || String(state || '')
}

function refreshDrafts() {
  itemDrafts.value = {}
  hookDrafts.value = {}
  taskDrafts.value = {}
  for (const layer of layers.value) {
    if (layer.key === 'L0') constitutionDraft.value = layer.raw || ''
    if (layer.key === 'L1') {
      const profile = (layer.profile || {}) as Record<string, unknown>
      profileDraft.value = {
        display_name: String(profile.display_name || ''),
        language: String(profile.language || ''),
        timezone: String(profile.timezone || ''),
        location: String(profile.location || ''),
        notes: String(profile.notes || ''),
      }
    }
    if (layer.key === 'L2') {
      for (const item of (layer.items || []) as MemoryPanelItem[]) {
        if (item.id) itemDrafts.value[item.id] = String(item.body || '')
      }
    }
    if (layer.key === 'L3') {
      for (const item of (layer.items || []) as MemoryPanelTaskItem[]) {
        if (item.key) taskDrafts.value[item.key] = String(item.text || '')
      }
    }
    if (layer.key === 'L4') {
      for (const item of (layer.items || []) as MemoryPanelHook[]) {
        if (item.id) hookDrafts.value[item.id] = String(item.text || '')
      }
    }
  }
}

async function load(nextMode: 'next' | 'last' = mode.value) {
  if (!props.visible) return
  mode.value = nextMode
  loading.value = true
  error.value = ''
  notice.value = ''
  try {
    const result = await memoryPanelApi.context(props.sessionId, nextMode)
    data.value = result
    if (result.ok === false) error.value = result.error || '记忆 v2 未启用'
    if (result.layers?.length && !result.layers.some(layer => layer.key === active.value)) {
      active.value = result.layers[0].key
    }
    refreshDrafts()
  } catch (e) {
    error.value = (e as Error)?.message || '读取失败'
  } finally {
    loading.value = false
  }
}

watch(() => props.visible, visible => { if (visible) void load('next') })
watch(() => props.sessionId, () => { if (props.visible) void load(mode.value) })

async function run(action: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await action()
    const message = notice.value
    const failure = error.value
    await load(mode.value)
    notice.value = message
    error.value = failure
  } catch (e) {
    error.value = (e as Error)?.message || '保存失败'
  } finally {
    busy.value = false
  }
}

async function saveConstitution() {
  await run(async () => {
    await memoryPanelApi.saveConstitution(constitutionDraft.value, current.value?.version)
    notice.value = '宪法已保存，下一回合生效'
  })
}

async function saveProfile() {
  await run(async () => {
    await memoryPanelApi.saveProfile({ ...profileDraft.value })
    notice.value = '画像已保存，下一回合生效'
  })
}

async function saveItem(item: MemoryPanelItem) {
  if (!item.id) return
  await run(async () => {
    await memoryPanelApi.patchItem(item.id!, { text: itemDrafts.value[item.id!] || '' })
    notice.value = '共享记忆已保存（回执清空，需重新验证）'
  })
}

async function togglePin(item: MemoryPanelItem) {
  if (!item.id) return
  await run(async () => {
    await memoryPanelApi.patchItem(item.id!, { pinned: !item.pinned })
    notice.value = item.pinned ? '已取消 pin' : '已 pin，下一回合注入'
  })
}

async function removeItem(item: MemoryPanelItem) {
  if (!item.id) return
  const confirmed = await askConfirm({
    title: '删除共享记忆',
    message: '删除后不再出现在任何项目，且无法撤销。',
    confirmText: '删除',
    danger: true,
  })
  if (!confirmed) return
  await run(async () => {
    await memoryPanelApi.deleteItem(item.id!)
    notice.value = '已删除'
  })
}

async function pinRetrievable(item: Record<string, unknown>) {
  if (!item.id) return
  await run(async () => {
    await memoryPanelApi.patchItem(String(item.id), { pinned: true })
    notice.value = '已 pin，下一回合注入'
  })
}

async function promoteCandidate(item: Record<string, unknown>) {
  if (!item.id) return
  await run(async () => {
    const result = await memoryPanelApi.promoteItem(String(item.id))
    if (result.promoted) {
      notice.value = result.pinned ? '已晋升并 pin' : '已晋升（重要度不足，暂不常驻）'
    } else {
      error.value = '未晋升：' + ((result.reasons || []).join('；') || '未满足晋升门')
    }
  })
}

async function saveHook(hook: MemoryPanelHook) {
  if (!hook.id) return
  await run(async () => {
    await memoryPanelApi.patchHook(hook.id!, {
      session_id: props.sessionId,
      text: hookDrafts.value[hook.id!] || '',
    })
    notice.value = '钩子已更新，下一回合生效'
  })
}

async function toggleHookPin(hook: MemoryPanelHook) {
  if (!hook.id) return
  await run(async () => {
    await memoryPanelApi.patchHook(hook.id!, { session_id: props.sessionId, pinned: !hook.pinned })
    notice.value = hook.pinned ? '已取消 pin' : '已 pin'
  })
}

async function removeHook(hook: MemoryPanelHook) {
  if (!hook.id) return
  const confirmed = await askConfirm({
    title: '删除钩子', message: '删除后本窗口不再提醒这条信息。', confirmText: '删除', danger: true,
  })
  if (!confirmed) return
  await run(async () => {
    await memoryPanelApi.deleteHook(hook.id!, props.sessionId)
    notice.value = '已删除'
  })
}

async function addHook() {
  const text = newHook.value.trim()
  if (!text) return
  await run(async () => {
    await memoryPanelApi.addHook({
      session_id: props.sessionId, kind: newHookKind.value, text, pinned: true,
    })
    newHook.value = ''
    notice.value = '钩子已添加'
  })
}

async function saveTask(item: MemoryPanelTaskItem) {
  if (!item.key) return
  await run(async () => {
    await memoryPanelApi.editTaskItem(item.key!, taskDrafts.value[item.key!] || '')
    notice.value = '任务条目已保存'
  })
}

async function completeTask(item: MemoryPanelTaskItem) {
  if (!item.key) return
  let override = false
  if (!(item.receipts || []).length) {
    const confirmed = await askConfirm({
      title: '缺少验收回执',
      message: '该条目没有通过的测试/验收回执。强制完成会记录为用户覆盖，之后仍可按回执核验。',
      confirmText: '仍然完成',
      danger: true,
    })
    if (!confirmed) return
    override = true
  }
  await run(async () => {
    await memoryPanelApi.completeTaskItem(item.key!, { override })
    notice.value = '任务状态已更新'
  })
}

async function reopenTask(item: MemoryPanelTaskItem) {
  if (!item.key) return
  await run(async () => {
    await memoryPanelApi.reopenTaskItem(item.key!)
    notice.value = '任务已重开'
  })
}

async function reindex() {
  await run(async () => {
    const result = await memoryPanelApi.reindex()
    notice.value = `索引已重建：${Number(result.items || 0)} 条`
  })
}
</script>

<template>
  <aside v-if="visible" class="mp" role="complementary" aria-label="上下文记忆">
    <header class="mp-head">
      <strong>上下文记忆</strong>
      <div class="mp-modes" role="group" aria-label="注入视图">
        <button type="button" :class="{ on: mode === 'next' }" @click="load('next')">下一回合</button>
        <button type="button" :class="{ on: mode === 'last' }" @click="load('last')">上一回合</button>
      </div>
      <button class="mp-close" type="button" aria-label="关闭上下文记忆" @click="emit('close')">×</button>
    </header>

    <p v-if="loading" class="mp-hint">读取中…</p>
    <p v-if="error" class="mp-error">{{ error }}</p>
    <p v-else-if="notice" class="mp-ok">{{ notice }}</p>

    <div class="mp-tabs" role="tablist">
      <button
        v-for="layer in layers"
        :key="layer.key"
        type="button"
        role="tab"
        :aria-selected="active === layer.key"
        class="mp-tab"
        :class="{ on: active === layer.key }"
        @click="active = layer.key"
      >
        {{ layer.title }}
        <span>{{ layer.chars }}<template v-if="layer.budget">/{{ layer.budget }}</template></span>
      </button>
    </div>

    <div v-if="current" class="mp-body">
      <template v-if="current.kind === 'markdown'">
        <p class="mp-meta">
          v{{ current.version }} ·
          {{ current.hash_ok ? '哈希一致' : '手改/哈希不一致（已记审计）' }}
        </p>
        <textarea
          v-model="constitutionDraft"
          class="mp-textarea"
          spellcheck="false"
          :rows="14"
          :disabled="!current.editable || mode === 'last'"
        />
        <div class="mp-actions">
          <span class="mp-hint">保存后版本 +1；下一回合生效</span>
          <button class="mp-btn" type="button" :disabled="busy || !current.editable || mode === 'last'" @click="saveConstitution">保存宪法</button>
        </div>
      </template>

      <template v-else-if="current.kind === 'profile'">
        <label class="mp-field">称呼<input v-model="profileDraft.display_name" class="mp-input" /></label>
        <label class="mp-field">语言<input v-model="profileDraft.language" class="mp-input" placeholder="中文 / 英文" /></label>
        <label class="mp-field">时区<input v-model="profileDraft.timezone" class="mp-input" placeholder="Asia/Shanghai" /></label>
        <label class="mp-field">所在地<input v-model="profileDraft.location" class="mp-input" /></label>
        <label class="mp-field">备注<textarea v-model="profileDraft.notes" class="mp-textarea" :rows="3" /></label>
        <div class="mp-actions">
          <span class="mp-hint">学到的习惯只读；画像与习惯互不覆盖</span>
          <button class="mp-btn" type="button" :disabled="busy || mode === 'last'" @click="saveProfile">保存画像</button>
        </div>
        <pre class="mp-pre">{{ JSON.stringify(current.learned || {}, null, 2) }}</pre>
      </template>

      <template v-else-if="current.kind === 'items'">
        <p v-if="!l2Items.length" class="mp-hint">
          没有 pin 的共享记忆。可召回条目 {{ retrievableItems.length }} 条，可在下方检索或晋升。
        </p>
        <div v-for="item in l2Items" :key="String(item.id)" class="mp-item">
          <div class="mp-item-head">
            <span>{{ item.kind === 'lesson' ? '教训' : '事实' }} · 置信 {{ Number(item.confidence || 0).toFixed(2) }} · 关联 {{ (item.edges || []).length }}</span>
            <label class="mp-pin"><input type="checkbox" :checked="!!item.pinned" @change="togglePin(item)" /> pin</label>
          </div>
          <textarea v-model="itemDrafts[String(item.id)]" class="mp-textarea" :rows="4" :disabled="mode === 'last'" />
          <div class="mp-actions">
            <button class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="saveItem(item)">保存</button>
            <button class="mp-mini mp-danger" type="button" :disabled="busy || mode === 'last'" @click="removeItem(item)">删除</button>
          </div>
        </div>
        <details v-if="retrievableItems.length" class="mp-retrievable">
          <summary>可召回但未注入（{{ retrievableItems.length }}）</summary>
          <div v-for="item in retrievableItems" :key="String(item.id)" class="mp-item">
            <div class="mp-item-head">
              <span>{{ item.scope === 'global' ? '共享' : '项目候选' }} · {{ item.kind === 'lesson' ? '教训' : '事实' }} · 置信 {{ Number(item.confidence || 0).toFixed(2) }}</span>
              <span v-if="item.edges" class="mp-hint">关联 {{ item.edges }}</span>
            </div>
            <p class="mp-body-text">{{ item.body }}</p>
            <div class="mp-actions">
              <button v-if="item.scope === 'global'" class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="pinRetrievable(item)">pin</button>
              <button v-else class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="promoteCandidate(item)">晋升</button>
            </div>
          </div>
        </details>
      </template>

      <template v-else-if="current.kind === 'taskbook'">
        <p v-if="!l3Items.length" class="mp-hint">当前没有任务书。长任务（计划 ≥3 项）会自动建书。</p>
        <div v-for="item in l3Items" :key="String(item.key)" class="mp-item">
          <div class="mp-item-head">
            <span>{{ stateLabel(item.state) }}</span>
            <span v-if="item.user_override" class="mp-warn">用户强制完成</span>
          </div>
          <textarea v-model="taskDrafts[String(item.key)]" class="mp-textarea" :rows="2" :disabled="mode === 'last'" />
          <div class="mp-actions">
            <button class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="saveTask(item)">保存</button>
            <button class="mp-mini" type="button" :disabled="busy || mode === 'last' || item.state === 'done'" @click="completeTask(item)">完成</button>
            <button class="mp-mini" type="button" :disabled="busy || mode === 'last' || item.state !== 'done'" @click="reopenTask(item)">重开</button>
          </div>
        </div>
        <p class="mp-hint">「完成」需要测试/验收回执；没有回执时只能按用户覆盖记录。</p>
      </template>

      <template v-else-if="current.kind === 'hooks'">
        <div v-for="hook in l4Items" :key="String(hook.id)" class="mp-item">
          <div class="mp-item-head">
            <span>{{ hook.kind }} · {{ hook.verified ? '已验证' : '未验证' }}</span>
            <label class="mp-pin"><input type="checkbox" :checked="!!hook.pinned" @change="toggleHookPin(hook)" /> pin</label>
          </div>
          <textarea v-model="hookDrafts[String(hook.id)]" class="mp-textarea" :rows="2" :disabled="mode === 'last'" />
          <div class="mp-actions">
            <button class="mp-mini" type="button" :disabled="busy || mode === 'last'" @click="saveHook(hook)">保存</button>
            <button class="mp-mini mp-danger" type="button" :disabled="busy || mode === 'last'" @click="removeHook(hook)">删除</button>
          </div>
        </div>
        <div v-if="mode !== 'last'" class="mp-add">
          <select v-model="newHookKind" class="mp-input mp-select" aria-label="钩子类型">
            <option value="fact">事实</option>
            <option value="user_agreement">约定</option>
            <option value="decision">决策</option>
          </select>
          <input v-model="newHook" class="mp-input" placeholder="新增钩子（压缩后仍保留）" />
          <button class="mp-btn" type="button" :disabled="busy || !newHook.trim()" @click="addHook">添加</button>
        </div>
        <div v-if="l4Errors.length" class="mp-readonly">
          <strong>易错点（只读）</strong>
          <pre class="mp-pre">{{ l4Errors.map(e => `- ${e.label}: ${e.cause}（×${e.occurrences}）`).join('\n') }}</pre>
        </div>
        <div v-if="current.next" class="mp-readonly">
          <strong>上一回合下一步（只读）</strong>
          <p>{{ current.next }}</p>
        </div>
      </template>

      <template v-else>
        <pre class="mp-pre">{{ current.text || '（空）' }}</pre>
      </template>
    </div>

    <footer class="mp-foot">
      <span>合计 {{ totalChars }} 字符</span>
      <button class="mp-mini" type="button" :disabled="busy" @click="reindex">重建索引</button>
    </footer>
  </aside>
</template>

<style scoped>
.mp {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 400;
  width: min(460px, 94vw);
  display: flex;
  flex-direction: column;
  background: var(--bg-raised);
  border-left: 1px solid var(--border-strong);
  box-shadow: -12px 0 34px rgba(35, 52, 84, .16);
  color: var(--text);
  font-size: var(--fs-sm);
}
.mp-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}
.mp-head strong { font-size: var(--fs-md); }
.mp-modes { display: inline-flex; gap: 2px; margin-left: auto; }
.mp-modes button {
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  font-size: var(--fs-cap);
  padding: 3px 8px;
  border-radius: 5px;
  cursor: pointer;
}
.mp-modes button.on { color: var(--text); border-color: var(--border); background: var(--bg-hover); }
.mp-close {
  border: 0;
  background: transparent;
  color: var(--text-muted);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  padding: 0 4px;
}
.mp-close:hover { color: var(--text); }
.mp-hint { margin: 8px 14px; color: var(--text-muted); font-size: var(--fs-cap); }
.mp-ok { margin: 8px 14px; color: var(--green); font-size: var(--fs-cap); }
.mp-error { margin: 8px 14px; color: var(--danger); font-size: var(--fs-cap); }
.mp-tabs {
  display: flex;
  gap: 4px;
  overflow-x: auto;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border);
}
.mp-tab {
  flex: 0 0 auto;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  border-radius: 6px;
  padding: 4px 8px;
  font-size: var(--fs-cap);
  cursor: pointer;
}
.mp-tab span { color: var(--text-faint); margin-left: 4px; }
.mp-tab.on { color: var(--text); border-color: var(--border); background: var(--bg-hover); }
.mp-body { flex: 1 1 auto; overflow-y: auto; padding: 10px 14px 14px; }
.mp-meta { color: var(--text-muted); font-size: var(--fs-cap); margin: 0 0 6px; }
.mp-textarea, .mp-input {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  font: inherit;
  padding: 6px 8px;
  resize: vertical;
}
.mp-textarea { line-height: 1.6; margin-bottom: 6px; font-size: var(--fs-cap); }
.mp-input { margin-bottom: 6px; }
.mp-select { width: 92px; flex: 0 0 auto; }
.mp-field { display: block; margin-bottom: 6px; color: var(--text-muted); font-size: var(--fs-cap); }
.mp-field .mp-input, .mp-field .mp-textarea { margin-top: 3px; color: var(--text); }
.mp-actions { display: flex; align-items: center; gap: 8px; margin-top: 2px; }
.mp-actions .mp-hint { margin: 0; }
.mp-actions .mp-btn, .mp-actions .mp-mini { margin-left: auto; }
.mp-actions .mp-mini + .mp-mini { margin-left: 0; }
.mp-btn {
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--bg-hover);
  color: var(--text);
  padding: 4px 10px;
  font-size: var(--fs-cap);
  cursor: pointer;
}
.mp-btn:disabled, .mp-mini:disabled { opacity: .45; cursor: default; }
.mp-mini {
  border: 1px solid var(--border);
  border-radius: 5px;
  background: transparent;
  color: var(--text-muted);
  padding: 2px 8px;
  font-size: var(--fs-cap);
  cursor: pointer;
}
.mp-mini:hover:not(:disabled) { color: var(--text); border-color: var(--border-strong); }
.mp-danger { color: var(--danger); }
.mp-item {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px;
  margin-bottom: 8px;
  background: color-mix(in srgb, var(--bg) 60%, transparent);
}
.mp-item-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: var(--text-muted);
  font-size: var(--fs-cap);
  margin-bottom: 5px;
}
.mp-pin { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; }
.mp-pin input { accent-color: var(--accent); }
.mp-warn { color: var(--amber); font-size: var(--fs-cap); }
.mp-retrievable { margin-top: 10px; border-top: 1px dashed var(--border); padding-top: 8px; }
.mp-retrievable summary { cursor: pointer; color: var(--text-muted); font-size: var(--fs-cap); margin-bottom: 6px; }
.mp-body-text { margin: 0 0 6px; color: var(--text-muted); font-size: var(--fs-cap); line-height: 1.6; white-space: pre-wrap; }
.mp-add { display: flex; gap: 6px; align-items: flex-start; margin: 8px 0; }
.mp-add .mp-input { margin-bottom: 0; flex: 1 1 auto; width: auto; min-width: 0; }
.mp-add .mp-btn { flex: 0 0 auto; white-space: nowrap; }
.mp-readonly { margin-top: 10px; color: var(--text-muted); font-size: var(--fs-cap); }
.mp-pre {
  white-space: pre-wrap;
  word-break: break-word;
  margin: 4px 0 0;
  padding: 8px;
  border: 1px dashed var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text-muted);
  font: 11px/1.6 var(--font-mono);
  max-height: 220px;
  overflow: auto;
}
.mp-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 14px;
  border-top: 1px solid var(--border);
  color: var(--text-faint);
  font-size: var(--fs-cap);
}
.mp :deep(button:focus-visible),
.mp :deep(textarea:focus-visible),
.mp :deep(input:focus-visible),
.mp :deep(select:focus-visible) {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
</style>
