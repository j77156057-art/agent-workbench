<script setup lang="ts">
/**
 * 实时对话面板（R13 的挂载点）。
 *
 * 这块面板是「实时视频流」这条线**第一次真正有入口**的地方：后端的 WS 网关
 * （`/api/vision/live-stream`）从 2026-09 就挂好了，协议/媒体/控制三层纯逻辑
 * 也都写完了，但没有任何一处 import 它们 —— 用户看到的是"后端有、前端没有"。
 *
 * 面板只做两件事：**显示状态** 与 **调动作**。协议细节全在
 * `useLiveSession` 里，它再往下调 `liveTransport` / `liveStreamControl` /
 * `liveAudioControl`。
 *
 * 设计约束（与本轮 UI 改造同口径）：
 * - 面板本体用 `--bg-raised` + 1px `--border`，不投影（层级靠遮罩与边框表达）；
 * - 圆角走皮肤令牌 `--r-md`（zinc 8px / 纸面 0），不在组件里写死；
 * - 状态色只用 `--accent` / `--green` / `--amber` / `--danger` 四个语义色。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import RealtimeModeBadge from './RealtimeModeBadge.vue'
import { useLiveSession, type LiveVideoSource } from '../composables/useLiveSession'
import { LIVE_PHASE_LABELS } from '../liveStreamControl'

const open = ref(false)
const videoRef = ref<HTMLVideoElement | null>(null)
const session = useLiveSession()
/** 面板自己的提示（与后端/会话状态分开：这里说的是"下一步怎么做"）。 */
const notice = ref('')
/** 下一次「开始」用哪种画面：摄像头，或系统屏幕/窗口共享。 */
const source = ref<LiveVideoSource>('camera')

const phaseLabel = computed(() => LIVE_PHASE_LABELS[session.phase.value] || '未开始')
const phaseTone = computed(() => {
  switch (session.phase.value) {
    case 'answering': return 'live'
    case 'viewing': return 'live'
    case 'connecting': return 'wait'
    case 'offline': return 'bad'
    case 'paused': return 'wait'
    default: return 'idle'
  }
})

const stats = computed(() => session.stats.value)
const nativeMode = computed(() => session.modeView.value?.tone === 'native')
const lastE2e = computed(() => {
  const value = stats.value.lastE2eMs
  return value === null ? '—' : `${Math.round(value)}ms`
})

/** 主按钮文案：开始 / 等待采集授权 / 暂停 / 继续。 */
const startLabel = computed(() => {
  if (session.active.value) return session.paused.value ? '继续' : '暂停'
  if (session.starting.value) return source.value === 'screen' ? '选择中…' : '启动中…'
  return '开始'
})
const videoHint = computed(() => {
  if (session.starting.value) {
    return source.value === 'screen'
      ? '等待你在系统窗口中选择要共享的屏幕或窗口…'
      : '正在启动摄像头…'
  }
  if (session.active.value) {
    return session.activeSource.value === 'screen'
      ? '正在共享屏幕：预览里的画面就是送给模型的内容。'
      : '摄像头画面正在实时送给模型。'
  }
  return source.value === 'screen'
    ? '点击「开始」后，选择要共享的屏幕或窗口，画面会实时送往本地实时模型。'
    : '点击「开始」后，摄像头画面会实时送往本地实时模型。'
})

// ---- 圈选聚焦：在预览上拖拽画框，上行的帧就是这块区域 ----
const selecting = ref(false)
const dragStart = ref<{ x: number; y: number } | null>(null)
const dragNow = ref<{ x: number; y: number } | null>(null)

/** 预览用 object-fit: contain，画面可能有信箱留白 —— 鼠标坐标要先去掉留白再归一。 */
function toNormalized(ev: MouseEvent): { x: number; y: number } | null {
  const el = videoRef.value
  if (!el || !el.videoWidth) return null
  const rect = el.getBoundingClientRect()
  const vw = el.videoWidth
  const vh = el.videoHeight
  const scale = Math.min(rect.width / vw, rect.height / vh)
  const dw = vw * scale
  const dh = vh * scale
  const offsetX = (rect.width - dw) / 2
  const offsetY = (rect.height - dh) / 2
  const x = (ev.clientX - rect.left - offsetX) / dw
  const y = (ev.clientY - rect.top - offsetY) / dh
  if (x < -0.02 || x > 1.02 || y < -0.02 || y > 1.02) return null
  return { x: Math.min(1, Math.max(0, x)), y: Math.min(1, Math.max(0, y)) }
}

function onSelectStart(ev: MouseEvent) {
  const point = toNormalized(ev)
  if (!point) return
  selecting.value = true
  dragStart.value = point
  dragNow.value = point
  ev.preventDefault()
}
function onSelectMove(ev: MouseEvent) {
  if (!selecting.value) return
  const point = toNormalized(ev)
  if (point) dragNow.value = point
}
function onSelectEnd() {
  if (!selecting.value) return
  selecting.value = false
  const a = dragStart.value
  const b = dragNow.value
  dragStart.value = null
  dragNow.value = null
  if (!a || !b) return
  const x = Math.min(a.x, b.x)
  const y = Math.min(a.y, b.y)
  const w = Math.abs(a.x - b.x)
  const h = Math.abs(a.y - b.y)
  // 太小多半是误点，不当成框选。
  session.focusRegion.value = w > 0.02 && h > 0.02 ? { x, y, w, h } : null
}

function clearFocus() {
  session.focusRegion.value = null
}

/** 选区在预览上的位置（百分比，用于画覆盖框）。 */
const focusBox = computed(() => {
  const r = session.focusRegion.value
  if (!r) return null
  return { left: `${r.x * 100}%`, top: `${r.y * 100}%`, width: `${r.w * 100}%`, height: `${r.h * 100}%` }
})
const dragBox = computed(() => {
  const a = dragStart.value
  const b = dragNow.value
  if (!a || !b) return null
  const x = Math.min(a.x, b.x)
  const y = Math.min(a.y, b.y)
  return { left: `${x * 100}%`, top: `${y * 100}%`, width: `${Math.abs(a.x - b.x) * 100}%`, height: `${Math.abs(a.y - b.y) * 100}%` }
})

// ---- 通道指标（R10）：首字延迟 / 队列深度 / 连接数等 ----
const metrics = computed(() => {
  const scope = session.channelStatus.value?.metrics?.global
  if (!scope) return null
  const hist = scope.histograms || {}
  const counters = scope.counters || {}
  const gauges = scope.gauges || {}
  const ms = (name: string): string => {
    const h = hist[name]
    if (!h || h.count === 0) return '—'
    const p50 = h.p50 === null ? '—' : Math.round(h.p50)
    const p95 = h.p95 === null ? '—' : Math.round(h.p95)
    return `${p50} / ${p95} ms`
  }
  const num = (bag: Record<string, number | null>, name: string): string => {
    const v = bag[name]
    return v === undefined || v === null ? '—' : String(v)
  }
  return {
    首字延迟: ms('first_token_ms'),
    首音延迟: ms('first_audio_ms'),
    观察延迟: ms('observation_latency_ms'),
    队列深度: num(gauges, 'queue_depth'),
    发帧: num(counters, 'frames_sent'),
    丢帧: num(counters, 'frames_dropped'),
    模型拒绝: num(counters, 'model_rejections'),
    连接数: num(counters, 'connections'),
  }
})

function show() {
  open.value = true
  void nextTick(() => session.bindVideo(videoRef.value))
  void session.refreshMode()
}
function hide() {
  session.stop()
  open.value = false
}
async function start() {
  // 上一轮的提示（例如麦克风没开成）不能盖住这一轮的新结果（例如“已取消屏幕共享”）。
  notice.value = ''
  session.bindVideo(videoRef.value)
  // 麦克风**是这条链路的必需输入**，不是可选项：实测（两轮对照）只发视频时
  // DashScope Omni 会稳定断流（stream_broken，零转写），带上音频就稳。
  // 所以「开始」= 画面 + 麦克风一起开，关麦用旁边的开关。
  await session.start({ video: true, audio: true, source: source.value })
  // 用户取消了屏幕/摄像头选择：start 没有建立会话，不要把取消提示覆盖成麦克风警告。
  if (!session.active.value) return
  if (!session.audioEnabled.value) {
    notice.value = '麦克风没开成：实时会话只发画面会被服务端断开（实测必断）。请允许麦克风权限后重试。'
  }
}
function toggle() {
  if (session.starting.value) return
  if (!session.active.value) { void start(); return }
  if (session.paused.value) session.resume()
  else session.pause()
}
function toggleMic() {
  if (session.audioEnabled.value) {
    session.disableAudio()
    // 实测：音频一停，服务端就会把会话关掉（只发视频必断）。
    notice.value = '麦克风已关：实时会话需要持续的音频输入，音频一停服务端就会断开这条会话。'
    return
  }
  void session.enableAudio()
  notice.value = ''
}

// 面板关闭时必须停掉采集与连接（摄像头指示灯不能一直亮着）。
watch(open, (value) => { if (!value) session.stop() })
onBeforeUnmount(() => session.stop())

defineExpose({ show, hide })
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="lsp-mask" @click.self="hide">
      <section class="lsp" role="dialog" aria-label="实时对话">
        <header class="lsp-head">
          <h2 class="lsp-title">实时对话</h2>
          <span class="lsp-phase" :data-tone="phaseTone">{{ phaseLabel }}</span>
          <RealtimeModeBadge :payload="session.modePayload.value" />
          <button type="button" class="lsp-x" aria-label="关闭" @click="hide">×</button>
        </header>

        <div class="lsp-body">
          <div class="lsp-video">
            <div
              class="lsp-stage"
              :class="{ 'lsp-stage--selecting': selecting }"
              @mousedown="onSelectStart" @mousemove="onSelectMove" @mouseup="onSelectEnd" @mouseleave="onSelectEnd"
            >
              <video ref="videoRef" class="lsp-video-el" autoplay playsinline muted />
              <span v-if="focusBox" class="lsp-focus" :style="focusBox" />
              <span v-if="dragBox" class="lsp-focus lsp-focus--live" :style="dragBox" />
              <p v-if="!session.active.value" class="lsp-video-hint">{{ videoHint }}</p>
            </div>
            <div class="lsp-focus-row">
              <span class="lsp-dim">
                {{ session.focusRegion.value ? '已聚焦选区：只把这一块送给模型' : '在画面上拖拽可框选聚焦（只把这一块送给模型）' }}
              </span>
              <button v-if="session.focusRegion.value" class="lsp-btn lsp-btn--tiny" @click="clearFocus">清除聚焦</button>
            </div>
            <dl class="lsp-stats">
              <div><dt>已发帧</dt><dd>{{ stats.framesSent }}</dd></div>
              <div><dt>观察</dt><dd>{{ stats.observations }}</dd></div>
              <div><dt>端到端</dt><dd>{{ lastE2e }}</dd></div>
              <div><dt>音频片</dt><dd>{{ stats.chunksSent }}</dd></div>
            </dl>
            <dl v-if="metrics" class="lsp-metrics">
              <div v-for="(value, key) in metrics" :key="key"><dt>{{ key }}</dt><dd>{{ value }}</dd></div>
            </dl>
          </div>

          <div class="lsp-side">
            <section class="lsp-sec">
              <h3 class="lsp-sec-title">对话</h3>
              <ul v-if="session.captions.value.length" class="lsp-captions">
                <li
                  v-for="(turn, index) in session.captions.value" :key="index"
                  class="lsp-caption" :data-role="turn.role"
                >
                  <span class="lsp-caption-role">{{ turn.role === 'user' ? '你' : '模型' }}</span>
                  <span class="lsp-caption-text" :data-done="turn.done ? '1' : '0'">{{ turn.text }}</span>
                </li>
              </ul>
              <p v-else class="lsp-empty">还没有对话。开启麦克风后说的话会出现在这里。</p>
            </section>

            <section class="lsp-sec lsp-sec--grow">
              <h3 class="lsp-sec-title">画面观察</h3>
              <ul v-if="session.observations.value.length" class="lsp-obs">
                <li v-for="(item, index) in session.observations.value.slice().reverse()" :key="index" class="lsp-obs-item">
                  <span class="lsp-obs-time">{{ new Date(item.at).toLocaleTimeString() }}</span>
                  <span class="lsp-obs-text">{{ item.throttled ? '（服务端限流，本帧未分析）' : (item.text || '（无内容）') }}</span>
                </li>
              </ul>
              <p v-else-if="nativeMode" class="lsp-empty">
                原生实时模式下画面由实时模型直接理解，结果体现在上方的回复里；
                这一栏用于降级（抽帧）模式下后端旁路的逐帧观察。
              </p>
              <p v-else class="lsp-empty">还没有画面观察。</p>
            </section>
          </div>
        </div>

        <p v-if="session.error.value" class="lsp-msg lsp-msg--bad">{{ session.error.value }} <button class="lsp-retry" @click="start">重试</button></p>
        <p v-else-if="notice" class="lsp-msg lsp-msg--warn">{{ notice }}</p>
        <p v-else-if="session.notice.value" class="lsp-msg">{{ session.notice.value }}</p>
        <p v-if="session.modeView.value?.isDegraded && session.modeView.value.hint" class="lsp-msg lsp-msg--warn">
          {{ session.modeView.value.hint }}
        </p>

        <footer class="lsp-foot">
          <div class="lsp-source" role="group" aria-label="画面来源">
            <button
              type="button" class="lsp-source-btn" :class="{ 'lsp-source-btn--on': source === 'camera' }"
              :aria-pressed="source === 'camera'" :disabled="session.active.value || session.starting.value"
              @click="source = 'camera'"
            >摄像头</button>
            <button
              type="button" class="lsp-source-btn" :class="{ 'lsp-source-btn--on': source === 'screen' }"
              :aria-pressed="source === 'screen'" :disabled="session.active.value || session.starting.value"
              @click="source = 'screen'"
            >屏幕</button>
          </div>
          <button
            type="button" class="lsp-btn lsp-btn--primary" :disabled="session.starting.value"
            @click="toggle"
          >{{ startLabel }}</button>
          <button
            type="button" class="lsp-btn" :class="{ 'lsp-btn--on': session.audioEnabled.value }"
            :disabled="!session.active.value" @click="toggleMic"
          >{{ session.audioEnabled.value ? '关麦' : '开麦' }}</button>
          <button type="button" class="lsp-btn" :disabled="!session.active.value" @click="session.cancel()">打断</button>
          <button
            type="button" class="lsp-btn" :disabled="!session.active.value && !session.starting.value"
            @click="session.stop()"
          >{{ session.starting.value ? '取消' : '停止' }}</button>
          <span class="lsp-foot-note">
            实时会话按项目隔离；未配置原生实时模型时后端会自动降级为抽帧模式。
          </span>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.lsp-mask {
  position: fixed; inset: 0; z-index: 120;
  background: var(--scrim);
  display: flex; align-items: center; justify-content: center;
  padding: 24px;
}
.lsp {
  width: min(1040px, 100%); height: min(680px, 100%);
  display: flex; flex-direction: column;
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  overflow: hidden;
}
.lsp-head {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
}
.lsp-title { margin: 0; font-size: var(--fs-lead); font-weight: 600; }
.lsp-phase {
  font-size: var(--fs-sm); color: var(--text-muted);
  padding: 1px 8px; border-radius: var(--r-full, 999px);
  border: 1px solid var(--border);
}
.lsp-phase[data-tone='live'] { color: var(--green); border-color: color-mix(in srgb, var(--green) 40%, transparent); }
.lsp-phase[data-tone='wait'] { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 40%, transparent); }
.lsp-phase[data-tone='bad'] { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 40%, transparent); }
.lsp-x {
  margin-left: auto; width: 26px; height: 26px; border: none; background: transparent;
  color: var(--text-muted); font-size: var(--fs-lead); line-height: 1; cursor: pointer; border-radius: var(--r-sm);
}
.lsp-x:hover { background: var(--bg-hover); color: var(--text); }
.lsp-body { flex: 1 1 auto; min-height: 0; display: grid; grid-template-columns: 1.15fr 1fr; }
.lsp-video {
  position: relative; display: flex; flex-direction: column; gap: 10px;
  padding: 12px; border-right: 1px solid var(--border); min-width: 0;
}
.lsp-video-el {
  width: 100%; flex: 1 1 auto; min-height: 0;
  background: var(--bg); border: 1px solid var(--border); border-radius: var(--r-sm);
  object-fit: contain;
}
/* 画面容器：承载选区框（圈选聚焦）。 */
.lsp-stage { position: relative; flex: 1 1 auto; min-height: 0; display: flex; }
.lsp-stage--selecting { cursor: crosshair; }
.lsp-focus {
  position: absolute; pointer-events: none;
  border: 1px solid var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  border-radius: 2px;
}
.lsp-focus--live { border-style: dashed; }
.lsp-focus-row { display: flex; align-items: center; gap: 8px; }
.lsp-dim { font-size: var(--fs-cap); color: var(--text-faint); }
.lsp-btn--tiny { height: 22px; padding: 0 8px; font-size: var(--fs-cap); }
.lsp-metrics {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 2px 12px; margin: 0;
  border-top: 1px solid var(--border); padding-top: 6px;
}
.lsp-metrics div { display: flex; align-items: baseline; gap: 5px; min-width: 0; }
.lsp-metrics dt { font-size: var(--fs-cap); color: var(--text-faint); }
.lsp-metrics dd { margin: 0; font-size: var(--fs-cap); color: var(--text-muted); font-variant-numeric: tabular-nums; }
.lsp-video-hint {
  position: absolute; inset: 12px 12px auto; margin: 0;
  padding: 8px 10px; font-size: var(--fs-sm); color: var(--text-muted);
  background: color-mix(in srgb, var(--bg-raised) 88%, transparent);
  border: 1px solid var(--border); border-radius: var(--r-sm);
}
.lsp-stats { display: flex; gap: 14px; margin: 0; }
.lsp-stats div { display: flex; align-items: baseline; gap: 5px; }
.lsp-stats dt { font-size: var(--fs-cap); color: var(--text-faint); }
.lsp-stats dd { margin: 0; font-size: var(--fs-sm); color: var(--text); font-variant-numeric: tabular-nums; }
.lsp-side { display: flex; flex-direction: column; min-height: 0; }
.lsp-sec { padding: 10px 12px; border-bottom: 1px solid var(--border); min-height: 0; display: flex; flex-direction: column; }
.lsp-sec--grow { flex: 1 1 auto; border-bottom: none; }
.lsp-sec-title { margin: 0 0 6px; font-size: var(--fs-cap); font-weight: 600; color: var(--text-faint); }
.lsp-captions, .lsp-obs { list-style: none; margin: 0; padding: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
.lsp-caption { display: flex; gap: 8px; font-size: var(--fs-sm); line-height: 1.55; }
.lsp-caption-role { flex: 0 0 auto; color: var(--text-faint); font-size: var(--fs-cap); padding-top: 2px; }
.lsp-caption-text { color: var(--text); white-space: pre-wrap; }
.lsp-caption[data-role='user'] .lsp-caption-text { color: var(--accent-text); }
.lsp-caption-text[data-done='0'] { color: var(--text-muted); }
.lsp-obs-item { display: flex; gap: 8px; font-size: var(--fs-sm); line-height: 1.5; }
.lsp-obs-time { flex: 0 0 auto; color: var(--text-faint); font-size: var(--fs-cap); font-variant-numeric: tabular-nums; padding-top: 2px; }
.lsp-obs-text { color: var(--text-muted); white-space: pre-wrap; }
.lsp-empty { margin: 0; font-size: var(--fs-sm); color: var(--text-faint); }
.lsp-msg {
  margin: 0; padding: 7px 12px; font-size: var(--fs-sm);
  border-top: 1px solid var(--border); color: var(--text-muted);
}
.lsp-msg--bad { color: var(--danger); }
.lsp-msg--warn { color: var(--amber); }
.lsp-retry {
  margin-left: 6px; height: 20px; padding: 0 8px; font-size: var(--fs-cap);
  border: 1px solid currentColor; border-radius: var(--r-sm);
  background: transparent; color: inherit; cursor: pointer;
}
.lsp-msg--warn { color: var(--amber); }
.lsp-foot {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px; border-top: 1px solid var(--border);
}
.lsp-btn {
  height: 28px; padding: 0 12px; border-radius: var(--r-sm);
  border: 1px solid var(--border); background: var(--bg-raised); color: var(--text);
  font-size: var(--fs-sm); cursor: pointer;
}
.lsp-btn:hover:not(:disabled) { border-color: var(--border-strong); color: var(--accent); }
.lsp-btn:disabled { opacity: .5; cursor: default; }
.lsp-btn--primary { --btn-fill: var(--accent); background: var(--accent); color: var(--accent-ink); border-color: var(--accent); font-weight: 600; }
.lsp-btn--primary:hover:not(:disabled) { color: var(--accent-ink); filter: brightness(1.04); }
.lsp-btn--on { border-color: color-mix(in srgb, var(--green) 45%, transparent); color: var(--green); }
/* 画面来源分段选择：只在会话开始前可点。 */
.lsp-source {
  display: inline-flex; flex: 0 0 auto;
  border: 1px solid var(--border); border-radius: var(--r-sm); overflow: hidden;
}
.lsp-source-btn {
  height: 26px; padding: 0 9px; border: none; background: transparent;
  color: var(--text-muted); font-size: var(--fs-sm); cursor: pointer;
}
.lsp-source-btn + .lsp-source-btn { border-left: 1px solid var(--border); }
.lsp-source-btn:hover:not(:disabled) { color: var(--text); background: var(--bg-hover); }
.lsp-source-btn--on {
  color: var(--accent-text); font-weight: 600;
  background: color-mix(in srgb, var(--accent) 14%, transparent);
}
.lsp-source-btn:disabled { opacity: .55; cursor: default; }
.lsp-foot-note { margin-left: auto; font-size: var(--fs-cap); color: var(--text-faint); }
</style>
