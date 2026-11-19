<script setup lang="ts">
// 一张 Qoder 式的工具调用卡：图标 + 工具名 + 参数一行 + 状态灯 + 耗时；
// 点开才看返回全文、浏览器证据与文件改动。轨迹没丢，只是不再抢答案的注意力。
import { computed, ref, watch } from 'vue'
import { toolIconOf } from '../composables/chatCards.ts'
import type { ChatCard, CardIcon } from '../composables/chatCards.ts'
import type { BrowserEvidence, FileDiffEntry } from '../composables/chat-types.ts'

const props = withDefaults(defineProps<{
  card: ChatCard
  /** 初始展开与否（失败卡默认展开；流式中的最后一张卡默认展开）。 */
  defaultOpen?: boolean
  /** 浏览器证据里图片的取图地址。 */
  shotUrl?: (path: string) => string
}>(), { defaultOpen: false, shotUrl: undefined })

const emit = defineEmits<{
  (e: 'open-diff', entries: FileDiffEntry[]): void
  (e: 'open-evidence', evidence: BrowserEvidence): void
}>()

const open = ref(props.defaultOpen)
watch(() => props.defaultOpen, (v) => { open.value = v })

const ICONS: Record<CardIcon, string[]> = {
  read: ['M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z', 'M14 2v6h6', 'M16 13H8', 'M16 17H8'],
  search: ['M11 11m-8 0a8 8 0 1 0 16 0a8 8 0 1 0 -16 0', 'm21 21-4.3-4.3'],
  edit: ['M12 20h9', 'M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z'],
  run: ['M4 17l6-6-6-6', 'M12 19h8'],
  web: ['M12 12m-10 0a10 10 0 1 0 20 0a10 10 0 1 0 -20 0', 'M12 2a14.5 14.5 0 0 0 0 20a14.5 14.5 0 0 0 0-20', 'M2 12h20'],
  mcp: ['M9 3v6', 'M15 3v6', 'M6 9h12v4a6 6 0 0 1-12 0Z', 'M12 19v2'],
  task: ['M3 6h4', 'M3 12h4', 'M3 18h4', 'M11 6h10', 'M11 12h10', 'M11 18h10'],
  think: ['M12 3l1.9 4.6L18.5 9l-4.6 1.9L12 15.5l-1.9-4.6L5.5 9l4.6-1.4Z'],
}

const iconPaths = computed(() => ICONS[props.card.kind === 'think' ? 'think' : toolIconOf(props.card.tool)])

const shot = computed(() => {
  const ev = props.card.evidence
  if (!ev?.shot_after || !props.shotUrl) return ''
  return props.shotUrl(ev.shot_after)
})

function toggle() {
  open.value = !open.value
}
function effectLabel(ev: BrowserEvidence): string {
  if (!ev.ok) return '未生效'
  return ev.effect || '已生效'
}
</script>

<template>
  <article class="cd-card" :data-kind="card.kind" :data-state="card.state" :data-tool="card.tool || undefined">
    <button
      type="button"
      class="cd-card-head"
      :aria-expanded="open"
      @click="toggle"
    >
      <span class="cd-card-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path v-for="(d, i) in iconPaths" :key="i" :d="d" />
        </svg>
      </span>
      <span class="cd-card-name">{{ card.title }}</span>
      <span v-if="card.params" class="cd-card-params">{{ card.params }}</span>
      <img v-if="shot" class="cd-card-thumb" :src="shot" alt="动作后画面" loading="lazy" />
      <span class="cd-card-meta">
        <span class="cd-card-state">
          <i class="cd-card-dot" aria-hidden="true" />
          {{ card.stateLabel }}
        </span>
        <span v-if="card.elapsedMs" class="cd-card-time">{{ card.elapsedMs < 1000 ? card.elapsedMs + 'ms' : (card.elapsedMs / 1000).toFixed(1) + 's' }}</span>
        <span class="cd-card-chevron" aria-hidden="true">{{ open ? '▾' : '▸' }}</span>
      </span>
    </button>

    <div v-if="open" class="cd-card-body">
      <div v-if="card.evidence" class="cd-card-evidence">
        <div class="cd-card-evidence-meta">
          <span class="cd-card-evidence-effect" :data-ok="card.evidence.ok ? '1' : '0'">{{ effectLabel(card.evidence) }}</span>
          <a
            v-if="card.evidence.url"
            class="cd-card-evidence-url"
            :href="card.evidence.url"
            target="_blank"
            rel="noopener noreferrer"
          >{{ card.evidence.url }}</a>
          <button
            v-if="card.evidence.url"
            type="button"
            class="cd-card-evidence-rail"
            title="在内置浏览器（只读镜像）查看这个页面；侧栏不可用时在新标签打开"
            @click="emit('open-evidence', card.evidence)"
          >内置浏览器</button>
        </div>
        <div v-if="card.evidence.shot_before || card.evidence.shot_after" class="cd-card-shots">
          <figure v-if="card.evidence.shot_before && shotUrl">
            <img :src="shotUrl(card.evidence.shot_before)" alt="动作前画面" loading="lazy" />
            <figcaption>动作前</figcaption>
          </figure>
          <figure v-if="card.evidence.shot_after && shotUrl">
            <img :src="shotUrl(card.evidence.shot_after)" alt="动作后画面" loading="lazy" />
            <figcaption>动作后</figcaption>
          </figure>
        </div>
      </div>
      <pre v-if="card.body" class="cd-card-text">{{ card.body }}</pre>
      <button
        v-if="card.file_diffs?.length"
        type="button"
        class="cd-card-diffbtn"
        @click="emit('open-diff', card.file_diffs!)"
      >
        查看改动
        <span class="cd-card-diffbtn-count">{{ card.file_diffs!.length }} 个文件</span>
      </button>
    </div>
  </article>
</template>

<style scoped>
/* 卡片视觉语言沿用对话台既有令牌（--border / --r-sm / --fs-cap / --green…），
   不引入新的一套色板：卡片是轨迹行的新皮肤，不是新主题。 */
.cd-card {
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  background: color-mix(in srgb, var(--bg) 55%, transparent);
  overflow: hidden;
}
.cd-card + .cd-card { margin-top: 4px; }
.cd-card[data-state="error"] { border-color: color-mix(in srgb, var(--danger) 34%, var(--border)); }
.cd-card[data-state="running"] { border-color: color-mix(in srgb, var(--accent) 30%, var(--border)); }

.cd-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 30px;
  padding: 5px 8px;
  border: 0;
  background: transparent;
  font-family: inherit;
  text-align: left;
  color: var(--text-muted);
  cursor: pointer;
}
.cd-card-head:hover { background: var(--bg-hover); color: var(--text); }
.cd-card-head:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

.cd-card-icon { flex: none; display: inline-flex; width: 14px; height: 14px; color: var(--text-faint); }
.cd-card-icon svg { width: 14px; height: 14px; }
.cd-card-name { flex: none; color: var(--text); font-size: var(--fs-sm); font-weight: 600; }
.cd-card-params {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-faint);
  font-family: var(--font-mono);
  font-size: var(--fs-cap);
}
.cd-card-thumb { flex: none; width: 28px; height: 18px; object-fit: cover; border-radius: 4px; border: 1px solid var(--border); }

.cd-card-meta { flex: none; display: inline-flex; align-items: center; gap: 8px; }
.cd-card-state { display: inline-flex; align-items: center; gap: 4px; font-size: var(--fs-cap); }
.cd-card-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.cd-card-time { color: var(--text-faint); font-size: var(--fs-cap); font-variant-numeric: tabular-nums; }
.cd-card-chevron { color: var(--text-faint); font-size: 10px; }

.cd-card[data-state="ok"] .cd-card-state { color: var(--green); }
.cd-card[data-state="warn"] .cd-card-state { color: var(--amber); }
.cd-card[data-state="error"] .cd-card-state { color: var(--danger); }
.cd-card[data-state="running"] .cd-card-state { color: var(--accent); }
.cd-card[data-state="running"] .cd-card-dot { animation: cd-card-pulse 1.2s ease-in-out infinite; }
@keyframes cd-card-pulse { 0%, 100% { opacity: .35; } 50% { opacity: 1; } }

.cd-card-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 2px 10px 10px 30px;
}
.cd-card-text {
  margin: 0;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  padding: 8px 10px;
  border-radius: var(--r-xs);
  background: color-mix(in srgb, var(--bg) 65%, transparent);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-cap);
  line-height: 1.55;
}

.cd-card-evidence { display: flex; flex-direction: column; gap: 6px; }
.cd-card-evidence-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.cd-card-evidence-effect {
  padding: 1px 6px;
  border-radius: var(--r-xs);
  font-size: var(--fs-cap);
  color: var(--green);
  background: color-mix(in srgb, var(--green) 10%, transparent);
}
.cd-card-evidence-effect[data-ok="0"] { color: var(--danger); background: color-mix(in srgb, var(--danger) 10%, transparent); }
.cd-card-evidence-url {
  color: var(--accent-text);
  font-size: var(--fs-cap);
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 320px;
}
.cd-card-evidence-url:hover { text-decoration: underline; }
.cd-card-evidence-rail {
  border: 1px solid var(--border);
  border-radius: var(--r-xs);
  background: transparent;
  color: var(--text-muted);
  font-family: inherit;
  font-size: var(--fs-cap);
  padding: 1px 6px;
  cursor: pointer;
}
.cd-card-evidence-rail:hover { background: var(--bg-hover); color: var(--text); }

.cd-card-shots { display: flex; gap: 8px; flex-wrap: wrap; }
.cd-card-shots figure { margin: 0; display: flex; flex-direction: column; gap: 3px; }
.cd-card-shots img { width: 180px; border-radius: var(--r-xs); border: 1px solid var(--border); }
.cd-card-shots figcaption { color: var(--text-faint); font-size: var(--fs-cap); }

.cd-card-diffbtn {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: var(--r-xs);
  background: transparent;
  color: var(--text-muted);
  font-family: inherit;
  font-size: var(--fs-cap);
  cursor: pointer;
}
.cd-card-diffbtn:hover { background: var(--bg-hover); color: var(--text); }
.cd-card-diffbtn-count { color: var(--text-faint); }
</style>
