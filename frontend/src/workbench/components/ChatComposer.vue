<script setup lang="ts">
// P0 对话输入条（薄封装）：只持有两个隐藏 file input 与 textarea 焦点；
// 附件列表/视频分析/发送判定等状态与逻辑全部留在 ChatDock 父级，
// 事件经显式 emit 上抛，避免父子双数据源。
import { ref, computed } from 'vue'
import type { ContextUsage } from '../api'

const props = defineProps<{
  modelValue: string
  sending: boolean
  demoMode: boolean
  videoBusy: boolean
  pendingImages: File[]
  attachmentError: string
  modelLabel: string
  visionLabel: string
  webOn: boolean
  thinkingSupported: boolean
  thinkingEffective: boolean
  thinkingNative: boolean
  thinkingMode: string
  thinkingTitle: string
  webTitle: string
  usage: ContextUsage | null
  usageTitle: string
  voiceActive: boolean
  voiceSupported: boolean
  /** 语音为什么不可用（开发舱内才有意义）；空串 = 不解释（普通对话台本来就没这个通道） */
  voiceHint?: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: string): void
  (e: 'submit'): void
  (e: 'stop'): void
  (e: 'images-picked', files: FileList | File[]): void
  (e: 'video-picked', file: File): void
  (e: 'remove-image', index: number): void
  (e: 'update:webOn', v: boolean): void
  (e: 'toggle-thinking'): void
  (e: 'open-model-switcher'): void
  (e: 'toggle-voice'): void
}>()

// BUG-8：与后端 _meaningless_input_clarification 同规则——纯空白/纯符号
// （无任何字母或数字，如"？""。。。"）的输入没有可检索意图。这里只做发送按钮的
// 视觉置灰（真正的 fail-closed 拦截在后端入口）；带附件时照常放行。
const canSendText = computed(() => /[\p{L}\p{N}]/u.test(props.modelValue || ''))

const inputEl = ref<HTMLTextAreaElement | null>(null)
const imageInput = ref<HTMLInputElement | null>(null)
const videoInput = ref<HTMLInputElement | null>(null)

function focus() { inputEl.value?.focus() }
defineExpose({ focus })

function onKeydown(ev: KeyboardEvent) {
  if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
    ev.preventDefault()
    emit('submit')
  }
}

function onImagePick(ev: Event) {
  const el = ev.target as HTMLInputElement
  if (el.files) emit('images-picked', el.files)
  el.value = ''
}
function onPaste(ev: ClipboardEvent) {
  const files = Array.from(ev.clipboardData?.files || []).filter(f => f.type.startsWith('image/'))
  if (files.length) { ev.preventDefault(); emit('images-picked', files) }
}
function onDrop(ev: DragEvent) {
  ev.preventDefault()
  if (ev.dataTransfer?.files?.length) emit('images-picked', ev.dataTransfer.files)
}
function onDragover(ev: DragEvent) { ev.preventDefault() }
function onVideoPick(ev: Event) {
  const el = ev.target as HTMLInputElement
  const file = el.files?.[0]
  el.value = ''
  if (file) emit('video-picked', file)
}
</script>

<template>
  <footer class="cd-inputbar">
    <div class="cd-tools">
      <input ref="imageInput" class="cd-file-input" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple @change="onImagePick" />
      <input ref="videoInput" class="cd-file-input" type="file" accept="video/mp4,video/quicktime,video/webm,video/x-matroska,video/avi,image/gif" @change="onVideoPick" />
      <button class="cd-chip cd-attach" :disabled="demoMode || sending" :title="visionLabel" @click="imageInput?.click()">
        <span aria-hidden="true">▧</span><span>图片</span>
      </button>
      <button class="cd-chip cd-attach" :disabled="demoMode || sending || videoBusy" title="视频会由 Harness 抽帧并生成时间轴观察" @click="videoInput?.click()">
        <span aria-hidden="true">▹</span><span>{{ videoBusy ? '分析视频…' : '视频' }}</span>
      </button>
      <button v-if="voiceSupported" class="cd-chip cd-attach" :class="{ 'cd-chip-on': voiceActive }" :aria-pressed="voiceActive" title="语音协作：发言时段会匹配当前项目画面；无同步画面时明确说明" @click="emit('toggle-voice')">
        <span aria-hidden="true">{{ voiceActive ? '◼' : '◉' }}</span><span>{{ voiceActive ? '听取中' : '语音' }}</span>
      </button>
      <!-- 能力缺失时不能只是让按钮消失：用户分不清「这里没有语音」和「语音坏了」。
           禁用态 + tooltip 点名缺的是哪一项、怎么补。普通对话台不传 hint，仍然不渲染。 -->
      <button v-else-if="voiceHint" type="button" class="cd-chip cd-attach" disabled :title="voiceHint">
        <span aria-hidden="true">◌</span><span>语音不可用</span>
      </button>
      <span v-if="pendingImages.length" class="cd-attachments">
        <span v-for="(img, i) in pendingImages" :key="img.name + i" class="cd-attachment">
          {{ img.name || '图片' }}
          <button type="button" title="移除图片" @click="emit('remove-image', i)">×</button>
        </span>
      </span>
      <span v-if="attachmentError" class="cd-attachment-error">{{ attachmentError }}</span>
      <button
        class="cd-chip cd-chip-model"
        :disabled="demoMode"
        :title="demoMode ? '离线演示模式无需配置模型' : '切换已保存模型；添加模型和修改参数请到设置'"
        @click="emit('open-model-switcher')"
      >
        <svg width="12" height="12" viewBox="0 0 12 12">
          <rect x="2.6" y="2.6" width="6.8" height="6.8" rx="1" fill="none" stroke="currentColor" stroke-width="1"/>
          <path d="M4.6 0.9v1.7M7.4 0.9v1.7M4.6 9.4v1.7M7.4 9.4v1.7M0.9 4.6h1.7M0.9 7.4h1.7M9.4 4.6h1.7M9.4 7.4h1.7" stroke="currentColor" stroke-width="1" stroke-linecap="round"/>
        </svg>
        <span class="cd-chip-text">{{ modelLabel }}</span>
        <svg width="9" height="9" viewBox="0 0 9 9"><path d="M2 3.2 L4.5 5.7 L7 3.2" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>

      <button
        class="cd-chip"
        :class="{ 'cd-chip-on': webOn }"
        :aria-pressed="webOn"
        :title="webTitle"
        @click="emit('update:webOn', !webOn)"
      >
        <svg width="12" height="12" viewBox="0 0 12 12">
          <circle cx="6" cy="6" r="4.7" fill="none" stroke="currentColor" stroke-width="1"/>
          <path d="M1.3 6h9.4" fill="none" stroke="currentColor" stroke-width="1"/>
          <path d="M6 1.3c1.5 1.3 2.3 2.9 2.3 4.7S7.5 9.4 6 10.7C4.5 9.4 3.7 7.8 3.7 6S4.5 2.6 6 1.3z" fill="none" stroke="currentColor" stroke-width="1"/>
        </svg>
        <span>联网搜索</span>
        <span class="cd-switch" :class="{ on: webOn }"><i /></span>
      </button>

      <button
        v-if="thinkingSupported"
        class="cd-chip"
        :class="{ 'cd-chip-on': thinkingEffective, 'cd-chip-native': thinkingNative }"
        :aria-pressed="thinkingEffective"
        :disabled="thinkingNative"
        :title="thinkingTitle"
        @click="emit('toggle-thinking')"
      >
        <svg width="12" height="12" viewBox="0 0 12 12">
          <path d="M6 1 L7.1 4.9 L11 6 L7.1 7.1 L6 11 L4.9 7.1 L1 6 L4.9 4.9 Z" fill="none" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/>
        </svg>
        <span>深度思考</span>
        <span v-if="thinkingNative" class="cd-chip-badge">内置</span>
        <span v-else class="cd-switch" :class="{ on: thinkingEffective }"><i /></span>
      </button>
      <span v-else class="cd-chip cd-chip-off" :title="thinkingTitle">
        <svg width="12" height="12" viewBox="0 0 12 12">
          <path d="M6 1 L7.1 4.9 L11 6 L7.1 7.1 L6 11 L4.9 7.1 L1 6 L4.9 4.9 Z" fill="none" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/>
        </svg>
        <span>深度思考</span>
        <span class="cd-chip-badge">{{ thinkingMode === 'unknown' ? '待确认' : '不支持' }}</span>
      </span>

      <!-- 上下文窗口占用：按当前模型真实窗口估算，65% 转黄、80%（压缩触发线）转红 -->
      <span
        v-if="usage"
        class="cd-chip cd-ctx cd-ctx-push"
        :class="'cd-ctx-' + usage.level"
        :title="usageTitle"
      >
        <svg width="12" height="12" viewBox="0 0 12 12">
          <path d="M1.5 9.5a4.5 4.5 0 0 1 9 0" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round"/>
          <path d="M3.3 9.5a2.7 2.7 0 0 1 5.4 0" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round"/>
          <circle cx="6" cy="9.5" r=".7" fill="currentColor"/>
        </svg>
        <span>上下文 {{ usage.percent }}%</span>
        <span class="cd-ctx-bar"><i :style="{ width: usage.percent + '%' }" /></span>
      </span>
    </div>
    <div class="cd-input-row">
      <textarea
        ref="inputEl"
        :value="modelValue"
        class="cd-input"
        rows="2"
        placeholder="提问：角色数值在哪 / 解释这段逻辑 / 这个报错怎么改…（Ctrl+Enter 发送）"
        @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
        @keydown="onKeydown"
        @paste="onPaste"
        @drop="onDrop"
        @dragover="onDragover"
      />
      <button v-if="sending" class="cd-send cd-stop" title="停止当前请求" aria-label="停止当前请求" @click="emit('stop')">停止</button>
      <!-- 2026-10-20：aria-busy —— 全站 451 个按钮此前 0 个有 aria-busy，
           屏幕阅读器无法知道"正在发送"。 -->
      <button class="cd-send" :aria-busy="sending" :disabled="(!canSendText && !pendingImages.length)" :title="canSendText || pendingImages.length ? '发送消息（Ctrl+Enter）；运行中发送＝打断当前轮并重定向' : '请描述你的问题（至少包含文字或数字）'" @click="emit('submit')">发送</button>
    </div>
  </footer>
</template>

<style scoped>
/* 输入区 */
.cd-inputbar { display: flex; flex-direction: column; gap: 6px; padding: 7px 12px 9px; }
.cd-input-row { display: flex; gap: 8px; align-items: flex-end; }
.cd-input {
  flex: 1; resize: none;
  background: var(--bg); color: var(--text);
  border: 1px solid var(--border-graphic); border-radius: 7px;
  padding: 7px 10px; font-size: var(--fs-sm); font-family: inherit;
  outline: none; max-height: 110px;
}
.cd-input:focus { border-color: var(--accent); }
/* 不给 ::placeholder 上色的话，Chromium 用 UA 默认 #757575 —— 实测三套皮 3.73 /
   3.85 / 4.49:1，全部不过 AA。占位符是输入框里唯一的说明文字，不能靠浏览器默认色。 */
.cd-input::placeholder { color: var(--text-muted); opacity: 1; }
.cd-send {
  height: 30px; padding: 0 16px; border-radius: 7px;
  border: 1px solid var(--accent-deep); background: linear-gradient(180deg, var(--accent-loud), var(--accent));
  color: #fff; font-size: var(--fs-sm); font-weight: 600; cursor: pointer;
}
.cd-send:disabled { opacity: .4; cursor: default; }
.cd-stop { background: transparent; color: var(--danger); border-color: var(--danger); }

/* 工具行：模型芯片 / 联网开关 / 深度思考开关 */
.cd-tools { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.cd-chip {
  display: inline-flex; align-items: center; gap: 5px;
  height: 23px; padding: 0 8px;
  border: 1px solid var(--edge-ink-soft, var(--border)); border-radius: 12px;
  background: transparent; color: var(--text-muted);
  font-size: var(--fs-cap); line-height: 1; cursor: pointer;
  max-width: 60%;
}
.cd-chip:hover:not(:disabled):not(.cd-chip-off) { border-color: var(--border-strong); color: var(--text); background: var(--bg-hover); }
/* 禁用态此前没有任何视觉差别（只有 .cd-send:disabled 有），图片/视频按钮在演示模式
   下看着和可点一样。语音不可用那颗更需要看得出来是「不能点」。
   .55 而不是 .4：禁用态虽豁免 AA，但这颗的文案本身就是给人读的。 */
.cd-chip:disabled { opacity: .55; cursor: not-allowed; }
.cd-chip:disabled { cursor: default; }
.cd-chip-text {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: var(--font-mono);
}
.cd-chip-model { padding: 0 7px 0 8px; }
.cd-chip-on {
  color: var(--accent); border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 7%, transparent);
}
.cd-chip-native { color: var(--tool); border-color: var(--tool); background: color-mix(in srgb, var(--tool) 8%, transparent); }
.cd-chip-off { opacity: .6; cursor: default; }
.cd-chip-badge { font-size: var(--fs-cap); color: var(--text-faint); }
.cd-chip-native .cd-chip-badge { color: var(--tool); }

/* 迷你开关 */
.cd-switch {
  position: relative; flex: 0 0 auto;
  width: 22px; height: 12px; border-radius: 7px;
  background: var(--border-strong); transition: background .15s;
}
.cd-switch i {
  position: absolute; top: 1.5px; left: 2px;
  width: 9px; height: 9px; border-radius: 50%;
  background: var(--bg-raised); box-shadow: 0 1px 2px rgba(0,0,0,.25);
  transition: left .15s;
}
.cd-switch.on { --btn-fill: var(--accent); background: var(--accent); }
.cd-chip-native .cd-switch.on { --btn-fill: var(--tool); background: var(--tool); }
.cd-switch.on i { left: 11px; }

/* 上下文窗口用量指示 */
.cd-ctx-push { margin-left: auto; cursor: default; max-width: none; }
.cd-ctx:hover { border-color: var(--border); color: var(--text-muted); }
.cd-ctx-bar {
  position: relative; flex: 0 0 auto;
  width: 34px; height: 4px; border-radius: 2px;
  background: var(--border-strong); overflow: hidden;
}
.cd-ctx-bar i {
  position: absolute; inset: 0 auto 0 0;
  border-radius: 2px; background: currentColor;
  transition: width .25s ease;
}
.cd-ctx-ok { color: var(--text-faint); }
.cd-ctx-high {
  color: var(--amber); border-color: color-mix(in srgb, var(--amber) 45%, transparent);
  background: color-mix(in srgb, var(--amber) 8%, transparent);
}
.cd-ctx-warn {
  color: var(--danger); border-color: color-mix(in srgb, var(--danger) 50%, transparent);
  background: color-mix(in srgb, var(--danger) 9%, transparent);
}

.cd-file-input { display: none; }
.cd-attach { cursor: pointer; }
.cd-attachments { display: inline-flex; gap: 4px; flex-wrap: wrap; max-width: 360px; }
.cd-attachment { display: inline-flex; align-items: center; gap: 3px; max-width: 150px; padding: 2px 5px; border: 1px solid var(--border); border-radius: 5px; color: var(--text-muted); font-size: var(--fs-cap); }
.cd-attachment button { border: 0; background: transparent; color: var(--text-faint); cursor: pointer; padding: 0 1px; }
.cd-attachment-error { color: var(--danger); font-size: var(--fs-cap); }

/* 输入工具行的层次与微动效 */
.cd-tools { padding: 1px 2px 0; }
.cd-inputbar { border-top: 1px solid color-mix(in srgb, var(--border) 84%, white); }
.cd-attachments { animation: cd-attachment-in .2s var(--ease-spring) both; }
.cd-attachment { background: color-mix(in srgb, var(--bg-raised) 66%, transparent); transition: border-color .16s, background .16s, transform .16s var(--ease-spring); }
.cd-attachment:hover { border-color: var(--accent); background: var(--bg-raised); transform: translateY(-1px); }
.cd-send:active:not(:disabled) { transform: translateY(0); }
@keyframes cd-attachment-in {
  from { opacity: 0; transform: translateY(3px) scale(.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
@media (max-width: 560px) {
  .cd-inputbar { padding: 6px 8px 8px; }
  .cd-tools { gap: 5px; }
  .cd-chip { max-width: calc(100vw - 24px); }
  .cd-chip-model { max-width: min(62vw, 240px); }
  .cd-input-row { gap: 6px; }
  .cd-input { min-width: 0; font-size: var(--fs-sm); }
  .cd-send { padding-inline: 12px; }
}
</style>
