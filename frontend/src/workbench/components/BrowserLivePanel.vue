<!--
  内置浏览器实况小窗。

  无头浏览器最大的问题是"看不见"：模型说它点了、读了，用户只能看到一行行
  文字流水。这个面板把 CDP 推流的最新画面、当前地址和最近几步动作摆在
  对话旁边 —— 模型操作网页时，用户能同时看到它在看什么、点到了哪。
  收起后只留一个胶囊，随时点开，不挡代码与对话。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { useBrowserLive } from '../composables/browserLive'
import { browserLiveApi } from '../api/browser'

const {
  pages, snapshot, frameSrc, frameVersion, lastError, collapsed, visible,
  activeMeta, title, lastAction, liveState, recentSteps,
  startBrowserWatch, stopBrowserWatch, collapseBrowserWatch, expandBrowserWatch,
} = useBrowserLive()

onMounted(() => startBrowserWatch())
onBeforeUnmount(() => stopBrowserWatch())

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
const pageLabel = computed(() => activeMeta.value?.page || '')
const domainLabel = computed(() => (activeMeta.value?.domain === 'public' ? '公网' : '本机'))
const urlLabel = computed(() => title.value || '正在打开网页')
const lastActionLabel = computed(() => {
  const act = lastAction.value
  if (!act) return '等待第一步动作'
  const effect = snapshot.value?.last_effect || ''
  return (ACTION_LABEL[act] || act) + (effect ? ' · ' + (EFFECT_LABEL[effect] || effect) : '')
})

function effectLabel(effect?: string): string {
  if (!effect) return ''
  return EFFECT_LABEL[effect] || effect
}
function actionLabel(action?: string): string {
  if (!action) return ''
  return ACTION_LABEL[action] || action
}
function clock(at?: number): string {
  if (!at) return ''
  try {
    return new Date(at * 1000).toLocaleTimeString('zh-CN', { hour12: false })
  } catch {
    return ''
  }
}
function shotUrl(path?: string): string {
  return path ? browserLiveApi.shotUrl(path) : ''
}
</script>

<template>
  <Teleport to="body">
    <!-- 收起态：一枚胶囊，提示"浏览器还在跑"，点开即回放实时画面。 -->
    <button
      v-if="visible && collapsed"
      type="button"
      class="blp-pill"
      data-testid="browser-live-pill"
      :title="urlLabel"
      @click="expandBrowserWatch"
    >
      <span class="blp-dot" :data-tone="stateTone" />
      <span class="blp-pill-label">浏览器实况</span>
      <span class="blp-pill-title">{{ urlLabel }}</span>
    </button>

    <section
      v-else-if="visible"
      class="blp"
      data-testid="browser-live-panel"
      aria-label="浏览器实况"
    >
      <header class="blp-head">
        <span class="blp-dot" :data-tone="stateTone" />
        <span class="blp-head-title">浏览器实况</span>
        <span class="blp-chip">{{ domainLabel }}</span>
        <span class="blp-state">{{ stateLabel }}</span>
        <button type="button" class="blp-icon" aria-label="收起" title="收起" @click="collapseBrowserWatch">—</button>
      </header>

      <div class="blp-stage" data-testid="browser-live-stage">
        <img
          v-if="frameSrc"
          :key="frameVersion"
          class="blp-frame"
          :src="frameSrc"
          alt="浏览器当前画面"
          data-testid="browser-live-frame"
        />
        <div v-else class="blp-wait">
          <span class="blp-spinner" aria-hidden="true" />
          <p>{{ lastError || '正在等待浏览器画面…' }}</p>
          <small>页面刚打开时通常要 1–2 秒才有第一帧。</small>
        </div>
        <span v-if="pageLabel" class="blp-page">会话 {{ pageLabel }}</span>
      </div>

      <div class="blp-url" :title="urlLabel" data-testid="browser-live-title">{{ urlLabel }}</div>
      <div class="blp-now">
        <span class="blp-now-label">最近动作</span>
        <span class="blp-now-value">{{ lastActionLabel }}</span>
      </div>

      <ol v-if="recentSteps.length" class="blp-steps" data-testid="browser-live-steps">
        <li v-for="(step, i) in recentSteps" :key="`${step.action}-${step.at}-${i}`" class="blp-step">
          <img v-if="shotUrl(step.shot_after)" class="blp-step-shot" :src="shotUrl(step.shot_after)" alt="" loading="lazy" />
          <span v-else class="blp-step-shot blp-step-shot--empty" aria-hidden="true" />
          <span class="blp-step-body">
            <span class="blp-step-line">
              <b>{{ actionLabel(step.action) }}</b>
              <span v-if="step.effect" class="blp-step-effect" :data-ok="step.ok ? '1' : '0'">{{ effectLabel(step.effect) }}</span>
              <span class="blp-step-time">{{ clock(step.at) }}</span>
            </span>
            <span class="blp-step-url">{{ step.title || step.url }}</span>
          </span>
        </li>
      </ol>
      <p v-else class="blp-empty">这台浏览器还没有执行过动作。</p>

      <footer class="blp-foot">
        <span>{{ pages.length }} 个浏览会话</span>
        <span class="blp-foot-note">画面来自无头浏览器实时推流</span>
      </footer>
    </section>
  </Teleport>
</template>

<style scoped>
.blp,
.blp-pill {
  position: fixed;
  right: 16px;
  top: 64px;
  z-index: 58;
  font-family: var(--font-ui);
}
.blp {
  width: 340px;
  max-height: calc(100vh - 120px);
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  color: var(--text);
  overflow: hidden;
}
.blp-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  max-width: 340px;
  padding: 7px 12px;
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text);
  cursor: pointer;
  font-size: 12px;
}
.blp-pill:hover { border-color: var(--accent); }
.blp-pill-label { font-weight: 600; white-space: nowrap; }
.blp-pill-title {
  max-width: 190px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-faint);
}
.blp-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.blp-head-title { font-size: 12px; font-weight: 600; }
.blp-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--text-faint); flex: 0 0 auto;
}
.blp-dot[data-tone="live"] { background: var(--green); }
.blp-dot[data-tone="wait"] { background: var(--amber); }
.blp-dot[data-tone="bad"] { background: var(--danger); }
.blp-chip {
  padding: 0 5px;
  border: 1px solid var(--border);
  border-radius: 3px;
  color: var(--text-faint);
  font: 9.5px/1.5 var(--font-ui);
}
.blp-state { margin-left: auto; color: var(--text-faint); font-size: 10.5px; }
.blp-icon {
  border: 0; background: transparent; color: var(--text-faint);
  cursor: pointer; font-size: 13px; line-height: 1; padding: 2px 4px;
}
.blp-icon:hover { color: var(--text); }

.blp-stage {
  position: relative;
  min-height: 170px;
  max-height: 260px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: color-mix(in srgb, var(--bg-raised) 70%, var(--text-faint) 10%);
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}
.blp-frame { width: 100%; height: auto; display: block; max-height: 260px; object-fit: contain; }
.blp-wait {
  padding: 18px;
  text-align: center;
  color: var(--text-faint);
  font-size: 11px;
}
.blp-wait p { margin: 8px 0 4px; color: var(--text); }
.blp-wait small { font-size: 10px; }
.blp-spinner {
  display: inline-block; width: 14px; height: 14px;
  border: 2px solid var(--border); border-top-color: var(--accent);
  border-radius: 50%; animation: blp-spin .8s linear infinite;
}
.blp-page {
  position: absolute; left: 6px; bottom: 5px;
  padding: 1px 5px;
  border-radius: 3px;
  background: color-mix(in srgb, #000 55%, transparent);
  color: #fff; font: 9.5px var(--font-mono);
}
.blp-url {
  font: 11px/1.45 var(--font-mono);
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.blp-now {
  display: flex; gap: 8px; align-items: baseline;
  font-size: 11px;
}
.blp-now-label { color: var(--text-faint); flex: 0 0 auto; }
.blp-now-value { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.blp-steps {
  list-style: none; margin: 0; padding: 6px 0 0;
  border-top: 1px solid var(--border);
  overflow-y: auto;
  display: flex; flex-direction: column; gap: 6px;
}
.blp-step { display: flex; gap: 7px; align-items: flex-start; }
.blp-step-shot {
  width: 52px; height: 34px; flex: 0 0 auto;
  object-fit: cover; border: 1px solid var(--border); border-radius: 3px;
  background: var(--bg-hover);
}
.blp-step-shot--empty { display: block; }
.blp-step-body { min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.blp-step-line { display: flex; gap: 6px; align-items: baseline; font-size: 10.5px; }
.blp-step-line b { font-weight: 600; }
.blp-step-effect { color: var(--text-faint); }
.blp-step-effect[data-ok="0"] { color: var(--danger); }
.blp-step-time { margin-left: auto; color: var(--text-faint); font: 9.5px var(--font-mono); }
.blp-step-url {
  font: 9.5px/1.4 var(--font-mono);
  color: var(--text-faint);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.blp-empty { margin: 0; color: var(--text-faint); font-size: 10.5px; }
.blp-foot {
  display: flex; justify-content: space-between; gap: 8px;
  padding-top: 7px;
  border-top: 1px solid var(--border);
  color: var(--text-faint);
  font-size: 9.5px;
}
.blp-foot-note { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@keyframes blp-spin { to { transform: rotate(360deg); } }
</style>
