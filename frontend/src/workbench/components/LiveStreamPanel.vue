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
import { useLiveSession } from '../composables/useLiveSession'
import { LIVE_PHASE_LABELS } from '../liveStreamControl'

const open = ref(false)
const videoRef = ref<HTMLVideoElement | null>(null)
const session = useLiveSession()

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
  session.bindVideo(videoRef.value)
  await session.start({ video: true, audio: false })
}
function toggle() {
  if (!session.active.value) { void start(); return }
  if (session.paused.value) session.resume()
  else session.pause()
}
function toggleMic() {
  if (session.audioEnabled.value) { session.disableAudio(); return }
  void session.enableAudio()
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
            <video ref="videoRef" class="lsp-video-el" autoplay playsinline muted />
            <p v-if="!session.active.value" class="lsp-video-hint">
              点击「开始」后，摄像头画面会实时送往本地实时模型。
            </p>
            <dl class="lsp-stats">
              <div><dt>已发帧</dt><dd>{{ stats.framesSent }}</dd></div>
              <div><dt>观察</dt><dd>{{ stats.observations }}</dd></div>
              <div><dt>端到端</dt><dd>{{ lastE2e }}</dd></div>
              <div><dt>音频片</dt><dd>{{ stats.chunksSent }}</dd></div>
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

        <p v-if="session.error.value" class="lsp-msg lsp-msg--bad">{{ session.error.value }}</p>
        <p v-else-if="session.notice.value" class="lsp-msg">{{ session.notice.value }}</p>
        <p v-if="session.modeView.value?.isDegraded && session.modeView.value.hint" class="lsp-msg lsp-msg--warn">
          {{ session.modeView.value.hint }}
        </p>

        <footer class="lsp-foot">
          <button type="button" class="lsp-btn lsp-btn--primary" @click="toggle">
            {{ session.active.value ? (session.paused.value ? '继续' : '暂停') : '开始' }}
          </button>
          <button
            type="button" class="lsp-btn" :class="{ 'lsp-btn--on': session.audioEnabled.value }"
            :disabled="!session.active.value" @click="toggleMic"
          >{{ session.audioEnabled.value ? '关麦' : '开麦' }}</button>
          <button type="button" class="lsp-btn" :disabled="!session.active.value" @click="session.cancel()">打断</button>
          <button type="button" class="lsp-btn" :disabled="!session.active.value" @click="session.stop()">停止</button>
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
  color: var(--text-muted); font-size: 18px; line-height: 1; cursor: pointer; border-radius: var(--r-sm);
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
.lsp-foot-note { margin-left: auto; font-size: var(--fs-cap); color: var(--text-faint); }
</style>
