<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { agentApi, getProjectId, withProject } from '../api'
import type { VisualFeedbackRecord, WorkflowPreviewArtifact, WorkflowState } from '../api'
import { blobDataUrl } from '../previewFeedback'
import type { PreviewFeedbackRequest } from '../previewFeedback'
import { appEvents } from '../eventBus'
import { capturePreviewFrame, refreshPreviewFrame } from '../workflowPreviewCapture'

const props = defineProps<{ workflow: WorkflowState }>()
const emit = defineEmits<{ (e: 'activity'): void }>()
const projectId = getProjectId()
const workflowId = props.workflow.workflow_id
const artifacts = computed(() => props.workflow.preview?.artifacts || [])
const interactive = (item: WorkflowPreviewArtifact) => item.kind === 'interactive' || item.renderer === 'interactive'
const kindLabel: Record<string, string> = {
  code: '代码', text: '文本', diff: '差异', image: '图片', audio: '音频',
  video: '视频', interactive: '实时画面', model: '模型', structured: '结构化数据',
  binary: '文件', unknown: '证据',
}
const feedbackStatusLabel: Record<string, string> = {
  pending: '待发送', processing: 'AI 处理中', awaiting_review: '等待检查效果',
  failed: '处理未完成', accepted: '效果已确认', needs_changes: '需要继续修改',
  sent: '历史反馈，发送状态未确认',
}

const error = ref('')
const width = ref(100)
const height = ref(360)
const reload = ref<Record<string, number>>({})
const feedbackOpenId = ref('')
const feedbackText = ref('')
const feedbackRegion = ref<VisualFeedbackRecord['region']>(null)
const regionOwner = ref('')
const annotationId = ref('')
const sending = ref(false)
const snapshotBusyId = ref('')
const history = ref<VisualFeedbackRecord[]>([])
const frames = new Map<string, HTMLIFrameElement>()
const captureErrors = new Map<string, string>()
const statusQueues = new Map<string, Promise<void>>()
const pendingUpdates = new Set<string>()
let destroyed = false
let resizeStart: { x: number; y: number; width: number; height: number; containerWidth: number } | null = null
let regionStart: { id: string; rect: DOMRect; x: number; y: number } | null = null

function current(): boolean {
  return !destroyed && props.workflow.workflow_id === workflowId && getProjectId() === projectId
}
function previewSource(item: WorkflowPreviewArtifact): string {
  const uri = String(item.uri || '').trim()
  if (uri) {
    try {
      const url = new URL(uri.startsWith('/') || /^https?:\/\//i.test(uri) ? uri : '/' + uri, window.location.origin)
      if (url.protocol === 'http:' || url.protocol === 'https:') return url.href
    } catch { /* use the registered project artifact below */ }
  }
  return item.id && item.path
    ? '/api/agent/workflow/' + encodeURIComponent(workflowId) + '/preview/artifact/' + encodeURIComponent(item.id)
    : ''
}
function imageSource(item: WorkflowPreviewArtifact): string {
  const source = previewSource(item)
  if (!source) return ''
  const url = new URL(source, window.location.origin)
  url.searchParams.set('preview_revision', String(reload.value[item.id] || 0))
  return url.href
}
function snapshotUrl(item: VisualFeedbackRecord, phase: 'before' | 'after'): string {
  const url = agentApi.workflowFeedbackSnapshotUrl(workflowId, item.id, phase, projectId)
  const version = item.snapshots?.[phase]?.captured_at
  return version ? url + '&v=' + encodeURIComponent(version) : url
}
function upsert(item: VisualFeedbackRecord) {
  history.value = [item, ...history.value.filter(row => row.id !== item.id)]
    .sort((a, b) => b.sent_at.localeCompare(a.sent_at)).slice(0, 20)
  emit('activity')
}
watch(() => props.workflow.visual_feedback, rows => {
  const server = (rows || []).filter(row => row && row.id)
  const keep = history.value.filter(row => pendingUpdates.has(row.id) || !server.some(item => item.id === row.id))
  history.value = [...server.filter(row => !pendingUpdates.has(row.id)), ...keep]
    .sort((a, b) => b.sent_at.localeCompare(a.sent_at)).slice(0, 20)
}, { immediate: true })

function setFrame(id: string | undefined, value: unknown) {
  if (!id) return
  if (value instanceof HTMLIFrameElement) frames.set(id, value)
  else frames.delete(id)
}
function reloadFrame(id: string | undefined) {
  if (id) reload.value = { ...reload.value, [id]: (reload.value[id] || 0) + 1 }
}
function fullscreenFrame(id: string | undefined) {
  const frame = id ? frames.get(id) : null
  const host = frame?.closest<HTMLElement>('.vp-live')
  if (!host?.requestFullscreen) { error.value = '当前环境不支持全屏预览'; return }
  void host.requestFullscreen().catch(() => { error.value = '当前环境未允许全屏预览' })
}
function startResize(event: PointerEvent) {
  const handle = event.currentTarget as HTMLElement
  handle.setPointerCapture(event.pointerId)
  resizeStart = {
    x: event.clientX, y: event.clientY, width: width.value, height: height.value,
    containerWidth: handle.closest<HTMLElement>('.vp-live')?.clientWidth || 300,
  }
  window.addEventListener('pointermove', resizeFrame)
  window.addEventListener('pointerup', stopResize, { once: true })
  window.addEventListener('pointercancel', stopResize, { once: true })
}
function resizeFrame(event: PointerEvent) {
  if (!resizeStart) return
  width.value = Math.max(45, Math.min(100, resizeStart.width + (event.clientX - resizeStart.x) * 100 / resizeStart.containerWidth))
  height.value = Math.max(220, Math.min(900, resizeStart.height + event.clientY - resizeStart.y))
}
function stopResize() {
  resizeStart = null
  window.removeEventListener('pointermove', resizeFrame)
  window.removeEventListener('pointerup', stopResize)
  window.removeEventListener('pointercancel', stopResize)
}
function toggleAnnotation(id: string) {
  const active = annotationId.value !== id
  annotationId.value = active ? id : ''
  regionOwner.value = id
  feedbackRegion.value = null
  regionStart = null
}
function beginAnnotation(event: PointerEvent, id: string) {
  const layer = event.currentTarget as HTMLElement
  const rect = layer.getBoundingClientRect()
  regionStart = { id, rect, x: Math.max(rect.left, Math.min(rect.right, event.clientX)), y: Math.max(rect.top, Math.min(rect.bottom, event.clientY)) }
  regionOwner.value = id
  feedbackRegion.value = null
  layer.setPointerCapture(event.pointerId)
}
function moveAnnotation(event: PointerEvent, id: string) {
  if (!regionStart || regionStart.id !== id) return
  const { rect, x, y } = regionStart
  const endX = Math.max(rect.left, Math.min(rect.right, event.clientX))
  const endY = Math.max(rect.top, Math.min(rect.bottom, event.clientY))
  feedbackRegion.value = {
    x: Math.round((Math.min(x, endX) - rect.left) / rect.width * 1000) / 10,
    y: Math.round((Math.min(y, endY) - rect.top) / rect.height * 1000) / 10,
    width: Math.round(Math.abs(endX - x) / rect.width * 1000) / 10,
    height: Math.round(Math.abs(endY - y) / rect.height * 1000) / 10,
  }
}
function endAnnotation(cancel = false) {
  regionStart = null
  annotationId.value = ''
  if (cancel) feedbackRegion.value = null
  else if (feedbackRegion.value?.width && feedbackRegion.value.height) feedbackOpenId.value = regionOwner.value
}
function regionStyle() {
  const region = feedbackRegion.value
  return region ? {
    left: region.x + '%', top: region.y + '%',
    width: region.width + '%', height: region.height + '%',
  } : {}
}
function toggleFeedback(id: string) {
  feedbackOpenId.value = feedbackOpenId.value === id ? '' : id
}

async function captureArtifact(item: WorkflowPreviewArtifact): Promise<Blob | null> {
  try {
    let blob: Blob
    if (interactive(item)) {
      const frame = frames.get(item.id)
      if (!frame) throw new Error('预览尚未显示，请稍后重试')
      blob = await capturePreviewFrame(frame)
    } else if (item.kind === 'image') {
      const source = imageSource(item)
      if (!source) throw new Error('图片预览没有可读取的地址')
      if (new URL(source).origin !== window.location.origin) {
        throw new Error('跨域图片暂不能自动附图，请使用文字反馈')
      }
      const response = await fetch(source, withProject({}, projectId))
      if (!response.ok) throw new Error('图片预览读取失败')
      blob = await response.blob()
      if (!['image/png', 'image/jpeg', 'image/webp'].includes(blob.type) || blob.size > 8 * 1024 * 1024) {
        throw new Error('图片类型或大小不支持自动附图')
      }
    } else throw new Error('此类预览暂不能自动截图')
    captureErrors.delete(item.id)
    return blob
  } catch (cause) {
    captureErrors.set(item.id, (cause as Error).message || '当前画面无法截图')
    return null
  }
}

async function saveStatus(item: VisualFeedbackRecord, status: string, detail = ''): Promise<void> {
  const previous = statusQueues.get(item.id) || Promise.resolve()
  const queued = previous.catch(() => {}).then(async () => {
    const result = await agentApi.workflowFeedbackStatus(workflowId, item.id, status, detail, projectId)
    if (!result.ok || !result.feedback) throw new Error(result.error || '反馈状态保存失败')
    if (current()) upsert(result.feedback)
  })
  statusQueues.set(item.id, queued)
  try { await queued }
  finally { if (statusQueues.get(item.id) === queued) statusQueues.delete(item.id) }
}
async function captureAfter(item: VisualFeedbackRecord, refresh = false): Promise<string | null> {
  if (!current()) return '项目或工作流已切换'
  if (snapshotBusyId.value) return '另一个截图正在保存，请稍后重试'
  snapshotBusyId.value = item.id
  try {
    const artifact = artifacts.value.find(row => row.id === item.artifact_id)
    if (!artifact) throw new Error('原预览已不在当前工作流中')
    if (refresh && interactive(artifact)) {
      const frame = frames.get(artifact.id)
      if (!frame) throw new Error('预览尚未显示，请展开画面后重试')
      await refreshPreviewFrame(frame)
    } else if (refresh && !interactive(artifact)) {
      reloadFrame(artifact.id)
      await nextTick()
    }
    if (!current()) throw new Error('项目或工作流已切换')
    const image = await captureArtifact(artifact)
    if (!image) throw new Error(captureErrors.get(artifact.id) || '当前画面无法截图')
    if (!current()) throw new Error('项目或工作流已切换')
    const result = await agentApi.workflowFeedbackSnapshot(workflowId, item.id, 'after', await blobDataUrl(image), projectId)
    if (!result.ok || !result.feedback) throw new Error(result.error || '复验截图保存失败')
    if (current()) upsert(result.feedback)
    return null
  } catch (cause) {
    const message = (cause as Error).message || '复验截图失败'
    if (!refresh && current()) error.value = message
    return message
  } finally { snapshotBusyId.value = '' }
}
function dispatchFeedback(item: VisualFeedbackRecord, prompt: string, images: Blob[]) {
  let reviewStarted = false
  const onStatus: PreviewFeedbackRequest['onStatus'] = (status, detail) => {
    if (status === 'awaiting_review') {
      if (reviewStarted) return
      reviewStarted = true
      void (async () => {
        try {
          await saveStatus(item, 'processing', 'AI 回复已结束，正在刷新预览并记录效果')
          const failure = await captureAfter(item, true)
          await saveStatus(item, 'awaiting_review', failure
            ? 'AI 回复已结束，自动记录未完成：' + failure + '。请检查后手动记录。'
            : '已刷新预览并保存复验画面，请对照检查效果')
        } catch (cause) {
          if (current()) error.value = (cause as Error).message || '反馈状态更新失败'
        } finally { pendingUpdates.delete(item.id) }
      })()
      return
    }
    void saveStatus(item, status, detail || '').catch(cause => {
      if (current()) error.value = (cause as Error).message || '反馈状态更新失败'
    }).finally(() => {
      if (status !== 'processing') pendingUpdates.delete(item.id)
    })
  }
  const request: PreviewFeedbackRequest = { prompt, images, projectId, uiContext: 'web_preview_feedback', onStatus }
  appEvents.emit('docmind:send-chat', request)
  appEvents.emit('docmind:focus-chat', {})
}
async function sendFeedback(artifact: WorkflowPreviewArtifact) {
  const note = feedbackText.value.trim()
  if (!note || sending.value || !current()) return
  sending.value = true
  error.value = ''
  const region = regionOwner.value === artifact.id && feedbackRegion.value?.width && feedbackRegion.value.height
    ? { ...feedbackRegion.value } : null
  const promptParts = [
    '请继续修改当前项目。用户针对项目预览「' + artifact.label + '」提出反馈：',
    note,
    region ? '标注区域（相对画面百分比）：左 ' + region.x + '%、上 ' + region.y
      + '%、宽 ' + region.width + '%、高 ' + region.height + '%。请优先检查该区域。' : '',
    '请先检查当前项目状态，完成修改并验证实际效果。工作流：' + workflowId,
  ]
  let savedRecord: VisualFeedbackRecord | null = null
  try {
    const image = await captureArtifact(artifact)
    if (!image) promptParts.push('本次反馈未附截图：' + (captureErrors.get(artifact.id) || '当前画面无法截图')
      + '。请用项目工具检查，不要声称已看过画面。')
    if (!current()) return
    const recordId = String(Date.now()) + '-' + Math.random().toString(36).slice(2, 7)
    const saved = await agentApi.workflowVisualFeedback(workflowId, {
      id: recordId, artifact_id: artifact.id, label: artifact.label, note, region,
      screenshot: !!image, sent_at: new Date().toISOString(),
    }, projectId)
    if (!saved.ok || !saved.feedback) throw new Error(saved.error || '反馈保存失败')
    let record = saved.feedback
    savedRecord = record
    if (current()) upsert(record)
    if (image) {
      const snapshot = await agentApi.workflowFeedbackSnapshot(workflowId, record.id, 'before', await blobDataUrl(image), projectId)
      if (!snapshot.ok || !snapshot.feedback) throw new Error(snapshot.error || '修改前截图保存失败')
      record = snapshot.feedback
      savedRecord = record
    }
    if (!current()) return
    pendingUpdates.add(record.id)
    upsert(record)
    dispatchFeedback(record, promptParts.filter(Boolean).join('\n'), image ? [image] : [])
    feedbackText.value = ''
    feedbackOpenId.value = ''
    feedbackRegion.value = null
  } catch (cause) {
    if (current()) {
      const message = '反馈尚未发送：' + ((cause as Error).message || '保存失败')
      error.value = message
      if (savedRecord) {
        try { await saveStatus(savedRecord, 'pending', message) }
        catch { /* 本地历史仍保留待发送反馈 */ }
      }
    }
  } finally { sending.value = false }
}
async function retryFeedback(item: VisualFeedbackRecord) {
  if (sending.value || !current()) return
  sending.value = true
  error.value = ''
  try {
    const images: Blob[] = []
    if (item.snapshots?.before) {
      const response = await fetch(snapshotUrl(item, 'before'), withProject({}, projectId))
      if (!response.ok) throw new Error('修改前截图读取失败')
      images.push(await response.blob())
    }
    if (!current()) return
    pendingUpdates.add(item.id)
    dispatchFeedback(item, [
      '请根据当前项目的视觉反馈继续修改并验证：', item.note,
      '工作流：' + workflowId, '预览：' + item.label,
      item.region ? '标注区域百分比：' + JSON.stringify(item.region) : '',
      images.length ? '' : '本次重试没有附图，请用项目工具检查实际画面，不要声称已看过截图。',
      '先检查当前项目状态；之前执行的操作不会自动撤销，请避免重复修改。',
    ].filter(Boolean).join('\n'), images)
  } catch (cause) {
    if (current()) error.value = (cause as Error).message || '反馈重试失败'
  } finally { sending.value = false }
}
async function decideFeedback(item: VisualFeedbackRecord, accepted: boolean) {
  if (!current()) return
  try {
    await saveStatus(item, accepted ? 'accepted' : 'needs_changes')
    if (!accepted && item.artifact_id) {
      feedbackOpenId.value = item.artifact_id
      feedbackText.value = '继续改进：' + item.note
    }
  } catch (cause) {
    if (current()) error.value = (cause as Error).message || '反馈决定保存失败'
  }
}
onBeforeUnmount(() => {
  destroyed = true
  stopResize()
  frames.clear()
})
</script>

<template>
  <section v-if="workflow.preview || history.length" class="vp-root">
    <div class="vp-head"><b>项目预览与视觉反馈</b><small>按实际画面检查模型的修改</small></div>
    <p v-if="error" class="vp-error">{{ error }}</p>
    <div v-if="workflow.preview?.changes?.total" class="vp-changes">
      <b>{{ workflow.preview.changes.total }} 个文件有变化</b>
      <span>新增 {{ workflow.preview.changes.counts?.added || 0 }}</span>
      <span>修改 {{ workflow.preview.changes.counts?.modified || 0 }}</span>
      <span>删除 {{ workflow.preview.changes.counts?.deleted || 0 }}</span>
    </div>
    <div v-if="artifacts.length" class="vp-artifacts">
      <template v-for="artifact in artifacts" :key="artifact.id">
        <section v-if="interactive(artifact)" class="vp-artifact">
          <div class="vp-artifact-head"><b>实时画面</b><span>{{ artifact.label }}</span></div>
          <p v-if="artifact.summary">{{ artifact.summary }}</p>
          <div v-if="previewSource(artifact)" class="vp-live">
            <div class="vp-toolbar">
              <span class="vp-live-dot" /><b>可操作预览</b><small>拖动右下角调整大小</small>
              <button type="button" @click="reloadFrame(artifact.id)">刷新</button>
              <button type="button" @click="fullscreenFrame(artifact.id)">全屏</button>
              <button type="button" @click="toggleFeedback(artifact.id)">反馈给 AI</button>
              <button type="button" @click="toggleAnnotation(artifact.id)">{{ annotationId === artifact.id ? '取消标注' : '标注区域' }}</button>
            </div>
            <div class="vp-viewport" :style="{ width: width + '%', height: height + 'px' }">
              <iframe :key="artifact.id + '-' + (reload[artifact.id] || 0)" :ref="element => setFrame(artifact.id, element)"
                :src="previewSource(artifact)" :title="artifact.label" allow="fullscreen" />
              <div v-if="annotationId === artifact.id" class="vp-annotation"
                @pointerdown="beginAnnotation($event, artifact.id)"
                @pointermove="moveAnnotation($event, artifact.id)"
                @pointerup="endAnnotation(false)" @pointercancel="endAnnotation(true)" />
              <div v-if="regionOwner === artifact.id && feedbackRegion?.width" class="vp-region" :style="regionStyle()" />
              <button type="button" class="vp-resize" title="拖动调整预览大小" @pointerdown.stop.prevent="startResize" />
            </div>
            <div v-if="feedbackOpenId === artifact.id" class="vp-feedback-pop">
              <div class="vp-pop-head"><b>告诉 AI 需要怎么改</b><button type="button" @click="toggleFeedback(artifact.id)">×</button></div>
              <textarea v-model="feedbackText" rows="3" placeholder="例如：按钮太靠右，移到画面下方并增大字号…" @keydown.ctrl.enter="sendFeedback(artifact)" />
              <div class="vp-pop-foot"><small>Ctrl+Enter 发送</small><button type="button" :disabled="!feedbackText.trim() || sending" @click="sendFeedback(artifact)">{{ sending ? '保存中…' : '发送并修改' }}</button></div>
            </div>
          </div>
          <small v-else class="vp-muted">预览地址尚未生成</small>
        </section>
        <details v-else class="vp-artifact">
          <summary class="vp-artifact-head"><b>{{ kindLabel[artifact.kind] || artifact.kind }}</b><span>{{ artifact.label }}</span></summary>
          <p v-if="artifact.summary">{{ artifact.summary }}</p>
          <img v-if="artifact.kind === 'image' && imageSource(artifact)" :src="imageSource(artifact)" :alt="artifact.label" />
          <video v-else-if="artifact.kind === 'video' && previewSource(artifact)" :src="previewSource(artifact)" controls />
          <audio v-else-if="artifact.kind === 'audio' && previewSource(artifact)" :src="previewSource(artifact)" controls />
          <button v-if="artifact.kind === 'image' && previewSource(artifact)" type="button" class="vp-image-feedback-btn" @click="toggleFeedback(artifact.id)">反馈这张图</button>
          <div v-if="feedbackOpenId === artifact.id" class="vp-image-feedback">
            <textarea v-model="feedbackText" rows="3" placeholder="描述希望修改的画面…" @keydown.ctrl.enter="sendFeedback(artifact)" />
            <button type="button" :disabled="!feedbackText.trim() || sending" @click="sendFeedback(artifact)">发送并修改</button>
          </div>
          <pre v-if="artifact.before || artifact.after">{{ artifact.before ? '变更前：\n' + artifact.before + '\n\n' : '' }}{{ artifact.after ? '变更后：\n' + artifact.after : '' }}</pre>
          <small v-if="artifact.path">路径：{{ artifact.path }}</small>
          <small v-if="artifact.change_summary">变更：{{ artifact.change_summary.before_lines || 0 }} 行 → {{ artifact.change_summary.after_lines || 0 }} 行</small>
          <small v-if="artifact.evidence?.length">证据：{{ artifact.evidence.join('、') }}</small>
        </details>
      </template>
    </div>
    <p v-else class="vp-muted">任务完成后，模型产生的画面、文件和验证证据会显示在这里。</p>
    <div v-if="history.length" class="vp-history">
      <b>反馈与效果对比</b>
      <details v-for="(item, index) in history" :key="item.id" class="vp-history-item" :open="index === 0">
        <summary><b>{{ item.label }}</b><span :class="`vp-status-${item.status || 'pending'}`">{{ feedbackStatusLabel[item.status || 'pending'] || item.status }}</span></summary>
        <p>{{ item.note }}</p>
        <small>{{ new Date(item.sent_at).toLocaleString('zh-CN') }} · {{ item.snapshots?.before ? '附带截图' : '文字反馈' }}{{ item.region ? ' · 已标注区域' : '' }}</small>
        <p v-if="item.detail" class="vp-muted">{{ item.detail }}</p>
        <div v-if="item.snapshots?.before || item.snapshots?.after" class="vp-snapshots">
          <figure><figcaption>修改前</figcaption><img v-if="item.snapshots?.before" :src="snapshotUrl(item, 'before')" alt="反馈发送时的画面" /><small v-else>未取得截图</small></figure>
          <figure><figcaption>复验画面</figcaption><img v-if="item.snapshots?.after" :src="snapshotUrl(item, 'after')" alt="模型回复后的画面" /><small v-else>模型回复后会尝试自动记录，也可手动记录</small></figure>
        </div>
        <div class="vp-history-actions">
          <button v-if="['pending', 'failed', 'needs_changes'].includes(item.status || 'pending')" type="button" :disabled="sending" @click="retryFeedback(item)">重新发送</button>
          <button type="button" :disabled="!!snapshotBusyId || !item.artifact_id || item.status === 'processing'" @click="captureAfter(item)">{{ snapshotBusyId === item.id ? '保存截图中…' : '记录当前效果' }}</button>
          <button v-if="item.status === 'awaiting_review' || item.snapshots?.after" type="button" @click="decideFeedback(item, true)">效果满意</button>
          <button v-if="item.status === 'awaiting_review' || item.snapshots?.after" type="button" @click="decideFeedback(item, false)">继续修改</button>
        </div>
      </details>
    </div>
  </section>
</template>

<style scoped>
.vp-root { display: grid; gap: 9px; min-width: 0; padding: 10px; border: 1px solid var(--border); border-radius: 9px; font-size: var(--fs-cap); background: linear-gradient(180deg, var(--bg-hover), var(--bg-raised)); box-shadow: 0 5px 16px rgba(35,52,84,.045); animation: vp-rise .24s ease-out both; }
.vp-head, .vp-artifact-head { display: flex; align-items: baseline; gap: 7px; min-width: 0; }
.vp-head b { font-size: var(--fs-sm); color: var(--text); }
.vp-head small, .vp-muted { color: var(--text-faint); }
.vp-error { margin: 0; padding: 7px 9px; border: 1px solid rgba(214,78,78,.25); border-radius: 7px; color: var(--danger); background: rgba(214,78,78,.05); }
.vp-changes { display: flex; flex-wrap: wrap; gap: 7px; color: var(--text-muted); align-items: center; }.vp-changes span, .vp-changes b { padding: 2px 7px; border: 1px solid var(--border); border-radius: 99px; background: var(--bg-raised); }.vp-changes b { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 28%, transparent); }
.vp-artifacts, .vp-history { display: grid; gap: 7px; min-width: 0; }
.vp-artifact { min-width: 0; padding: 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-hover); transition: border-color .16s ease, box-shadow .18s ease, transform .18s ease; }.vp-artifact:hover { border-color: var(--border-strong); box-shadow: 0 4px 14px rgba(35,52,84,.07); transform: translateY(-1px); }
.vp-artifact-head { cursor: pointer; }
.vp-artifact-head b { color: var(--accent); }
.vp-artifact-head span { color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vp-artifact p { margin: 6px 0; color: var(--text-muted); }
.vp-artifact > img, .vp-artifact video { display: block; max-width: 100%; max-height: 260px; margin-top: 7px; border-radius: 5px; }
.vp-artifact audio { display: block; width: 100%; margin-top: 7px; }
.vp-artifact pre { max-height: 220px; overflow: auto; padding: 8px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text-muted); background: var(--bg-raised); }
.vp-artifact > small { display: block; margin-top: 5px; color: var(--text-faint); overflow-wrap: anywhere; }
.vp-live { position: relative; min-width: 0; border: 1px solid var(--border); border-radius: 7px; background: #101521; }
.vp-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; padding: 6px; color: #dbe5f5; }
.vp-live-dot { width: 7px; height: 7px; border-radius: 50%; background: #43d17a; box-shadow: 0 0 8px #43d17a; animation: vp-pulse 1.5s ease-in-out infinite; }
.vp-toolbar small { color: #93a4bd; margin-right: auto; }
.vp-root button { border: 1px solid var(--border); border-radius: 6px; padding: 4px 7px; color: var(--text); background: var(--bg-raised); cursor: pointer; font: inherit; transition: color .15s ease, border-color .15s ease, background .15s ease, transform .15s ease, box-shadow .15s ease; }
.vp-root button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); transform: translateY(-1px); box-shadow: 0 3px 8px rgba(35,52,84,.08); }.vp-root button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.vp-root button:disabled { opacity: .5; cursor: default; }
.vp-toolbar button { border-color: #52698b; color: #dbe5f5; background: #26344b; }
.vp-viewport { position: relative; max-width: 100%; min-height: 220px; }
.vp-viewport iframe { display: block; width: 100%; height: 100%; border: 0; background: #000; border-radius: 0 0 7px 7px; }
.vp-annotation { position: absolute; inset: 0; cursor: crosshair; background: rgba(24,39,64,.22); touch-action: none; }
.vp-region { position: absolute; border: 2px solid #55a8ff; background: rgba(85,168,255,.18); pointer-events: none; box-sizing: border-box; }
.vp-resize { position: absolute; right: 0; bottom: 0; z-index: 2; width: 20px; height: 20px; padding: 0 !important; border: 0 !important; cursor: nwse-resize !important; touch-action: none; background: linear-gradient(135deg, transparent 45%, #8fa4c4 46%, #8fa4c4 53%, transparent 54%, transparent 64%, #8fa4c4 65%, #8fa4c4 72%, transparent 73%) !important; }
.vp-live:fullscreen { width: 100vw; height: 100vh; overflow: auto; }
.vp-live:fullscreen .vp-viewport { width: 100% !important; height: calc(100vh - 45px) !important; }
.vp-feedback-pop { position: absolute; z-index: 4; top: 38px; right: 7px; width: min(300px, calc(100% - 14px)); box-sizing: border-box; padding: 9px; border: 1px solid var(--border-strong); border-radius: 8px; color: var(--text); background: var(--bg-raised); box-shadow: 0 12px 32px rgba(0,0,0,.28); animation: vp-pop .18s ease-out both; }
.vp-pop-head, .vp-pop-foot { display: flex; align-items: center; justify-content: space-between; gap: 7px; }
.vp-pop-head button { border: 0; font-size: var(--fs-h2); }
.vp-feedback-pop textarea, .vp-image-feedback textarea { box-sizing: border-box; width: 100%; margin: 8px 0; padding: 7px; border: 1px solid var(--border); border-radius: 6px; color: var(--text); background: var(--bg-raised); font: inherit; resize: vertical; }
.vp-pop-foot small { color: var(--text-faint); }
.vp-pop-foot button, .vp-image-feedback button { border-color: var(--accent); color: var(--accent-ink); --btn-fill: var(--accent); background: var(--accent); }
.vp-image-feedback-btn { margin-top: 7px; }
.vp-image-feedback { display: grid; justify-items: end; }
.vp-history { border-top: 1px solid var(--border); padding-top: 8px; }
.vp-history > b { font-size: var(--fs-sm); }
.vp-history-item { padding: 7px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-hover); transition: border-color .16s ease, background .16s ease; }.vp-history-item:hover { border-color: var(--border-strong); background: var(--bg-selected); }
.vp-history-item summary { display: flex; justify-content: space-between; gap: 8px; cursor: pointer; }
.vp-history-item summary span { color: var(--accent); border: 1px solid currentColor; border-radius: 99px; padding: 1px 6px; font-size: var(--fs-cap); white-space: nowrap; }.vp-status-processing { color: var(--accent) !important; }.vp-status-awaiting_review { color: var(--amber) !important; }.vp-status-accepted { color: var(--green) !important; }.vp-status-failed, .vp-status-needs_changes { color: var(--danger) !important; }.vp-status-pending { color: var(--text-faint) !important; }
.vp-history-item p { margin: 5px 0; color: var(--text-muted); line-height: 1.45; }
.vp-history-item small { color: var(--text-faint); }
.vp-history-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.vp-snapshots { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 7px; margin-top: 8px; }
.vp-snapshots figure { min-width: 0; margin: 0; padding: 6px; border: 1px solid var(--border); border-radius: 6px; }
.vp-snapshots figcaption { margin-bottom: 5px; color: var(--text-muted); }
.vp-snapshots img { display: block; width: 100%; border-radius: 4px; transition: transform .18s ease; }.vp-snapshots img:hover { transform: scale(1.015); }
@keyframes vp-rise { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
@keyframes vp-pop { from { opacity: 0; transform: translateY(-4px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
@keyframes vp-pulse { 0%, 100% { opacity: .65; transform: scale(.85); } 50% { opacity: 1; transform: scale(1); } }
@media (max-width: 600px) { .vp-snapshots { grid-template-columns: 1fr; } .vp-head { flex-wrap: wrap; } .vp-toolbar small { flex-basis: 100%; margin-right: 0; } .vp-toolbar button { flex: 1 1 auto; } }
@media (prefers-reduced-motion: reduce) { .vp-root, .vp-artifact, .vp-root button, .vp-history-item, .vp-snapshots img { transition: none; } .vp-root, .vp-feedback-pop, .vp-live-dot { animation: none; } }
</style>
