<script setup lang="ts">
// 右栏「网页」页签：地址栏 + 真实 iframe + 只读镜像（CDP 实况帧）。
//
// 两种预览方式的边界必须说清楚：
//   · 网页：同源/本机内容用真 iframe（后端没有 X-Frame-Options/CSP，站内页面可信）；
//     外部站点能不能嵌入取决于对方的响应头，纯前端探测不到 —— 所以常驻一行提示，
//     空白时给「只读镜像」和「新标签」两条退路，不假装成功。
//   · 只读镜像：复用 BrowserLivePanel 同一套轮询（browserLive.ts），显示最新帧、
//     当前地址与最近 3 步动作。它只是"看"，没有任何操作入口。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useWorkbench } from '../composables/workbench'
import { useBrowserLive } from '../composables/browserLive'
import { normalizeAddress, previewKindOfUrl, previewSandboxFor } from '../lib/rightRail'

const { rightRailBrowser, openRightRailBrowser } = useWorkbench()
const {
  pages, activePage, frameSrc, frameVersion, lastError, collapsed,
  activeMeta, title, lastAction, liveState, recentSteps,
  startBrowserWatch, stopBrowserWatch, expandBrowserWatch,
} = useBrowserLive()

const typed = ref(rightRailBrowser.value.url)
const addrError = ref('')
watch(() => rightRailBrowser.value.url, (u) => {
  typed.value = u
  addrError.value = ''
})

const url = computed(() => rightRailBrowser.value.url)
const mode = computed(() => rightRailBrowser.value.mode)
const kind = computed(() => previewKindOfUrl(url.value))
const isExternal = computed(() => kind.value === 'external')
const sandbox = computed(() => previewSandboxFor(url.value))

// ---- 地址栏 ----
function go() {
  const r = normalizeAddress(typed.value)
  if (!r.ok) {
    addrError.value = r.error || '地址不可用'
    return
  }
  addrError.value = ''
  // 手输地址意味着想看真实网页：切回「网页」页签
  openRightRailBrowser(r.url, rightRailBrowser.value.page, 'page')
}
function openTab() {
  const r = normalizeAddress(url.value || typed.value)
  if (!r.ok) {
    addrError.value = r.error || '地址不可用'
    return
  }
  window.open(r.url, '_blank', 'noopener')
}
function setMode(next: 'page' | 'mirror') {
  if (mode.value === next) return
  openRightRailBrowser(url.value, rightRailBrowser.value.page, next)
}

// ---- 只读镜像 ----
// 与浮窗共用 collapsed / watchers：进入镜像时展开并订阅，离开时若原本是收起的就还原。
// 浮窗在右栏打开期间被 App 隐藏（v-if），轮询由这里接管，关闭右栏后浮窗自动恢复。
let watching = false
let restoredCollapse = false
function ensureMirrorWatch() {
  if (watching) return
  watching = true
  restoredCollapse = collapsed.value
  if (collapsed.value) expandBrowserWatch()
  startBrowserWatch()
}
function releaseMirrorWatch() {
  if (!watching) return
  watching = false
  stopBrowserWatch()
  // 原本收起的浮窗不应因为看过右栏镜像就被强行展开
  if (restoredCollapse) collapsed.value = true
  restoredCollapse = false
}
watch(mode, (m) => {
  if (m === 'mirror') ensureMirrorWatch()
  else releaseMirrorWatch()
}, { immediate: true })
onBeforeUnmount(releaseMirrorWatch)

// 打开时钉住指定会话（浏览器证据带 page）；没指定就按 URL 匹配当前实况页。
// 用户手动在 select 里换页后不再自动改，避免轮询把选择顶回去。
watch([() => rightRailBrowser.value.page, () => rightRailBrowser.value.url, pages], () => {
  const want = rightRailBrowser.value.page
  if (want && pages.value.some((p) => p.page === want)) {
    if (activePage.value !== want) activePage.value = want
    return
  }
  const target = rightRailBrowser.value.url
  if (!target) return
  if (pages.value.some((p) => p.page === activePage.value)) return
  const hit = pages.value.find((p) => p.url && (p.url.startsWith(target) || target.startsWith(p.url)))
  if (hit) activePage.value = hit.page
})

const ACTION_LABEL: Record<string, string> = {
  open: '打开', click: '点击', type: '输入', press: '按键',
  wait: '等待', read: '读取', locate: '定位', close: '关闭',
}
const EFFECT_LABEL: Record<string, string> = {
  changed: '页面有变化', navigated: '跳转', reloaded: '刷新', typed: '已输入',
  pixels: '画面变化', error_seen: '页面报错', no_change: '没有变化',
}
const STATE_LABEL: Record<string, string> = {
  live: '实时', starting: '连接中', reconnecting: '重连中',
  error: '画面中断', stopped: '已停止', unavailable: '无推流',
}
const stateLabel = computed(() => STATE_LABEL[liveState.value] || liveState.value || '连接中')
const stateTone = computed(() => {
  if (liveState.value === 'live') return 'live'
  if (liveState.value === 'error') return 'bad'
  return 'wait'
})
const lastActionLabel = computed(() => {
  const act = lastAction.value
  if (!act) return '等待第一步动作'
  const effect = activeMeta.value?.last_effect || ''
  return (ACTION_LABEL[act] || act) + (effect ? ' · ' + (EFFECT_LABEL[effect] || effect) : '')
})
function actionLabel(action?: string): string {
  return action ? (ACTION_LABEL[action] || action) : ''
}
function effectLabel(effect?: string): string {
  return effect ? (EFFECT_LABEL[effect] || effect) : ''
}
</script>

<template>
  <div class="rr-browser">
    <div class="rr-addr-row">
      <input
        v-model="typed"
        class="rr-addr"
        type="text"
        spellcheck="false"
        placeholder="网址：localhost:5199、example.com 或 /play/…（Enter 打开）"
        aria-label="预览地址"
        @keydown.enter.prevent="go"
      />
      <button type="button" class="rr-btn" title="打开这个地址" @click="go">打开</button>
      <button type="button" class="rr-btn" title="在新标签页打开（外部站点被禁止嵌入时用这个）" @click="openTab">新标签</button>
    </div>
    <p v-if="addrError" class="rr-addr-err" role="alert">{{ addrError }}</p>

    <div class="rr-mode" role="tablist" aria-label="预览方式">
      <button type="button" role="tab" :aria-selected="mode === 'page'" :class="{ on: mode === 'page' }" @click="setMode('page')">网页</button>
      <button type="button" role="tab" :aria-selected="mode === 'mirror'" :class="{ on: mode === 'mirror' }" @click="setMode('mirror')">只读镜像</button>
      <span class="rr-mode-hint">{{ mode === 'page' ? '真实嵌入' : '实况帧回放 · 只读' }}</span>
    </div>

    <template v-if="mode === 'page'">
      <div v-if="!url" class="rr-note">
        输入一个网址，或从对话里的联网来源 / 浏览器证据点「侧栏」打开。
      </div>
      <template v-else>
        <iframe
          v-if="kind !== 'unsupported'"
          class="rr-frame"
          :src="url"
          :sandbox="sandbox"
          referrerpolicy="no-referrer"
          title="网页预览"
        />
        <div v-else class="rr-note rr-note-warn">
          这个地址不能在这里预览（只支持 http / https 与站内路径）。
        </div>
        <p v-if="isExternal" class="rr-tip">
          外部站点能否嵌入取决于对方的 X-Frame-Options / CSP；若这里空白，请切「只读镜像」看实况，或点「新标签」直接打开。
        </p>
        <p v-else class="rr-tip">
          站内页面直接嵌入；若页面要求登录或有跳转限制，可点「新标签」打开。
        </p>
      </template>
    </template>

    <template v-else>
      <div class="rr-mirror-bar">
        <select v-if="pages.length > 1" v-model="activePage" class="rr-mirror-pick" aria-label="选择浏览器会话">
          <option v-for="p in pages" :key="p.page" :value="p.page">{{ p.url || p.page }}</option>
        </select>
        <span class="rr-mirror-state" :data-tone="stateTone">{{ stateLabel }}</span>
      </div>
      <div class="rr-mirror-stage">
        <img
          v-if="frameSrc"
          :key="frameVersion"
          class="rr-mirror-frame"
          :src="frameSrc"
          alt="浏览器当前画面"
          data-testid="right-rail-mirror-frame"
        />
        <div v-else class="rr-mirror-wait">
          <p>{{ !pages.length ? '没有正在运行的浏览器实况。' : (lastError || '正在等待浏览器画面…') }}</p>
          <small v-if="pages.length">页面刚打开时通常要 1–2 秒才有第一帧。</small>
          <small v-else>模型开始操作网页后，这里会出现实时画面与最近动作。</small>
        </div>
      </div>
      <div class="rr-mirror-meta">
        <div class="rr-mirror-url" :title="title">{{ title }}</div>
        <div class="rr-mirror-act">最近动作：{{ lastActionLabel }}</div>
      </div>
      <ol v-if="recentSteps.length" class="rr-mirror-steps" data-testid="right-rail-mirror-steps">
        <li v-for="(s, i) in recentSteps.slice(0, 3)" :key="`${s.action}-${s.at}-${i}`">
          <span class="rr-mirror-dot" :data-ok="s.ok === false ? 'bad' : 'ok'" />
          <span class="rr-mirror-step-act">{{ actionLabel(s.action) }}</span>
          <span v-if="s.effect" class="rr-mirror-step-effect">{{ effectLabel(s.effect) }}</span>
          <span v-if="s.url" class="rr-mirror-step-url" :title="s.url">{{ s.url }}</span>
        </li>
      </ol>
    </template>
  </div>
</template>

<style scoped>
.rr-browser { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; }
.rr-addr-row { flex: 0 0 auto; display: flex; gap: 6px; padding: 8px 10px 4px; }
.rr-addr {
  flex: 1 1 auto;
  min-width: 0;
  padding: 5px 9px;
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: var(--fs-sm);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
}
.rr-addr:focus-visible { outline: 2px solid var(--accent-deep); outline-offset: 1px; }
.rr-btn {
  flex: 0 0 auto;
  padding: 4px 10px;
  font-size: var(--fs-sm);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--bg-raised);
  color: var(--text-muted);
  cursor: pointer;
}
.rr-btn:hover { background: var(--bg-hover); color: var(--text); }
.rr-addr-err { margin: 0 10px; color: var(--amber); font-size: var(--fs-sm); }

.rr-mode { flex: 0 0 auto; display: flex; align-items: center; gap: 4px; padding: 4px 10px 8px; }
.rr-mode button {
  padding: 2px 10px;
  font-size: var(--fs-cap);
  border: 1px solid transparent;
  border-radius: 999px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}
.rr-mode button:hover { color: var(--text); background: var(--bg-hover); }
.rr-mode button.on { color: var(--text); border-color: var(--border-strong); background: var(--bg-raised); }
.rr-mode-hint { margin-left: auto; font-size: var(--fs-cap); color: var(--text-faint); }

.rr-frame { flex: 1 1 auto; min-height: 0; width: 100%; border: 0; background: #fff; }
.rr-tip { flex: 0 0 auto; margin: 0; padding: 7px 12px; font-size: var(--fs-cap); color: var(--text-faint); line-height: 1.6; border-top: 1px solid var(--border); }
.rr-note { padding: 10px 14px; color: var(--text-faint); font-size: var(--fs-sm); line-height: 1.7; }
.rr-note-warn { color: var(--amber); }

.rr-mirror-bar { flex: 0 0 auto; display: flex; align-items: center; gap: 8px; padding: 0 10px 6px; }
.rr-mirror-pick { max-width: 100%; font-size: var(--fs-cap); padding: 2px 6px; border-radius: 5px; border: 1px solid var(--border-strong); background: var(--bg); color: var(--text); }
.rr-mirror-state { margin-left: auto; font-size: var(--fs-cap); color: var(--text-faint); }
.rr-mirror-state[data-tone="live"] { color: var(--green); }
.rr-mirror-state[data-tone="bad"] { color: var(--danger); }
.rr-mirror-stage {
  flex: 1 1 auto;
  min-height: 160px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #14171c;
  overflow: hidden;
}
.rr-mirror-frame { max-width: 100%; max-height: 100%; object-fit: contain; }
.rr-mirror-wait { padding: 16px; text-align: center; color: #c6ccd6; font-size: var(--fs-sm); line-height: 1.7; }
.rr-mirror-wait p { margin: 0 0 6px; }
.rr-mirror-wait small { color: #8b929d; }
.rr-mirror-meta { flex: 0 0 auto; padding: 7px 12px 4px; border-top: 1px solid var(--border); }
.rr-mirror-url { font-size: var(--fs-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rr-mirror-act { margin-top: 2px; font-size: var(--fs-cap); color: var(--text-faint); }
.rr-mirror-steps { flex: 0 0 auto; margin: 0; padding: 4px 12px 10px; list-style: none; }
.rr-mirror-steps li { display: flex; align-items: center; gap: 6px; padding: 3px 0; font-size: var(--fs-cap); color: var(--text-muted); min-width: 0; }
.rr-mirror-dot { flex: 0 0 auto; width: 6px; height: 6px; border-radius: 50%; background: var(--green); }
.rr-mirror-dot[data-ok="bad"] { background: var(--danger); }
.rr-mirror-step-act { flex: 0 0 auto; font-weight: 600; }
.rr-mirror-step-effect { flex: 0 0 auto; color: var(--text-faint); }
.rr-mirror-step-url { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-faint); }
</style>
