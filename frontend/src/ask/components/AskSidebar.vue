<script setup lang="ts">
// AI 问答页左侧栏：品牌、当前项目（只读状态 + 跳转开发台）、资料文档上传、当前模型。
// 全部为只读/轻操作；代码建索引等重操作统一收口到代码工作台。
//
// 2026-10-19 瘦身（P1）：删掉「运行状态」整块（模型 / 显存占用 / 预加载 / 卸载）——
// 预加载与卸载显存模型是**运维级操作**，不是聊天主流程，已搬到工作台的
// 设置 → 模型 →「本机显存与模型驻留」。侧栏只留"当前模型"一行（要知道在拿哪个模型问），
// 连带问答页每 20 秒一次的 /api/model_status 轮询也一并省掉。
// 资料文档留在侧栏：上传资料是问答页的主流程动作，不该绕到设置里去。
import { computed, ref } from 'vue'
import type { ModelConfigInfo, ProjectInfo, SessionInfo } from '../../workbench/api'

const props = defineProps<{
  projects: ProjectInfo[]
  currentPid: string
  config: ModelConfigInfo | null
  demo: boolean
  ingestBusy: boolean
  ingestMsg: string
  sessions?: SessionInfo[]
  currentSessionId?: string
  sessionBusy?: boolean
}>()
const emit = defineEmits<{
  (e: 'ingest', file: File): void
  (e: 'openSettings'): void
  (e: 'newSession'): void
  (e: 'switchSession', id: string): void
  (e: 'deleteSession', id: string): void
}>()

const fileInput = ref<HTMLInputElement | null>(null)

const current = computed(() =>
  props.projects.find((p) => p.project_id === props.currentPid) || null)
const files = computed(() => props.config?.ingested_files || [])

function pickFile() {
  fileInput.value?.click()
}
function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0]
  if (f) emit('ingest', f)
  input.value = ''
}

function shortPath(p: string, n = 34): string {
  return p.length > n ? p.slice(0, n - 2) + '…' : p
}

function sessTitle(s: SessionInfo): string {
  return (s.title || s.preview || '').trim() || '未命名对话'
}
function sessTime(v?: string): string {
  if (!v) return ''
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return ''
  const delta = Math.max(0, Date.now() - d.getTime())
  if (delta < 60_000) return '刚刚'
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`
  return d.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' })
}
</script>

<template>
  <aside class="ask-side">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="17" height="17"><path fill="#fff" d="M12 2l2.4 5.6L20 9l-4 4 1 6-5-3-5 3 1-6-4-4 5.6-1.4z"/></svg>
      </div>
      <div class="brand-text">
        <div class="brand-name">Agent Workbench</div>
        <div class="brand-sub">AI 问答 · 读代码答问题</div>
      </div>
    </div>

    <button class="btn block new-chat" @click="emit('newSession')">＋ 新对话</button>

    <div class="ask-middle">
      <section class="card">
        <div class="card-title">会话历史</div>
      <div class="sess-list">
        <div
          v-for="s in (sessions || [])"
          :key="s.session_id"
          class="sess-item"
          :class="{ active: s.session_id === currentSessionId }"
        >
          <button class="sess-main" :disabled="sessionBusy" @click="emit('switchSession', s.session_id)">
            <span class="ah-dot">{{ s.session_id === currentSessionId ? '●' : '○' }}</span>
            <span class="sess-copy">
              <strong>{{ sessTitle(s) }}</strong>
              <small v-if="sessTime(s.updated_at)">{{ sessTime(s.updated_at) }}</small>
            </span>
          </button>
          <button class="sess-del" title="删除这条问答历史" :disabled="sessionBusy" @click.stop="emit('deleteSession', s.session_id)">×</button>
        </div>
        <div v-if="!(sessions || []).length" class="sess-empty">还没有已保存的问答</div>
      </div>
    </section>

    <!-- 当前项目：只读展示。选择 / 切换 / 建索引等重操作统一收口到代码工作台。
         当前模型也并在这一块 —— 模型名要看得见（知道在拿哪个模型问），
         但显存与驻留这类运维信息已搬到工作台设置，这里不占地方。 -->
    <section class="card">
      <div class="card-title">当前项目</div>
      <div v-if="demo" class="project-name">示例游戏项目</div>
      <div v-else-if="current" class="project-name">{{ current.name }}</div>
      <div v-else class="project-name muted">未选择项目</div>
      <div v-if="current?.root" class="project-root" :title="current.root">{{ shortPath(current.root) }}</div>
      <p class="project-hint">
        问答页无需绑定项目，可直接提问。需要建索引、分区开发或切换项目，请到代码工作台。
      </p>
      <a class="card-link" href="/workbench">打开代码工作台，管理项目与索引 →</a>
      <div class="model-line">
        <span class="model-k">模型</span>
        <b :title="config?.llm_model">{{ config?.llm_model || '未配置' }}</b>
        <button class="gear" title="模型参数设置" @click="emit('openSettings')">
          <svg viewBox="0 0 24 24" width="15" height="15"><path fill="currentColor" d="M19.4 13a7.8 7.8 0 000-2l2-1.6-2-3.4-2.4 1a7.6 7.6 0 00-1.7-1l-.4-2.6H10.1l-.4 2.6a7.6 7.6 0 00-1.7 1l-2.4-1-2 3.4L3.6 11a7.8 7.8 0 000 2l-2 1.6 2 3.4 2.4-1c.5.4 1.1.7 1.7 1l.4 2.6h3.8l.4-2.6c.6-.3 1.2-.6 1.7-1l2.4 1 2-3.4-2-1.6zM12 15.5A3.5 3.5 0 1112 8.5a3.5 3.5 0 010 7z"/></svg>
        </button>
      </div>
    </section>

    <!-- 资料文档：上传是问答页的主流程动作，留在侧栏；
         2026-10-19 把整宽的「上传资料文档」大按钮收成标题行里的小按钮，卡片矮了一截。 -->
    <section class="card">
      <div class="card-title">
        资料文档
        <span class="card-count">{{ demo ? 3 : files.length }}</span>
        <button
          class="btn sm add-doc"
          :disabled="demo || ingestBusy"
          :title="ingestBusy ? '正在摄取…' : '上传资料文档（PDF / Markdown / 文本）'"
          @click="pickFile"
        >{{ ingestBusy ? '摄取中…' : '＋ 上传' }}</button>
      </div>
      <input ref="fileInput" type="file" accept=".pdf,.md,.txt" hidden @change="onFileChange">
      <div v-if="ingestMsg" class="ingest-msg" :class="{ err: ingestMsg.includes('失败') }">{{ ingestMsg }}</div>
      <div class="file-list">
        <template v-if="demo">
          <div v-for="n in ['数值设计案.pdf', '战斗规则.md', '常见问题.txt']" :key="n" class="file-item">
            <span class="file-ic">{{ n.split('.').pop()!.slice(0, 3).toUpperCase() }}</span>
            <span class="file-nm">{{ n }}</span>
          </div>
        </template>
        <template v-else>
          <div v-for="n in files" :key="n" class="file-item">
            <span class="file-ic">{{ (n.split('.').pop() || '?').slice(0, 3).toUpperCase() }}</span>
            <span class="file-nm" :title="n">{{ n }}</span>
          </div>
          <div v-if="!files.length" class="file-empty">尚未上传文档</div>
        </template>
      </div>
    </section>
    </div>
  </aside>
</template>

<style scoped>
/* ===== Agent 工作台 问答页侧栏 · 高级粗野风 ===== */
.ask-side {
  flex: 0 0 296px;
  width: 296px;
  height: 100vh;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 12px 12px 14px;
  background: var(--bg-raised);
  border-right: var(--edge-w-heavy) solid var(--edge-ink);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
/* 侧栏整体作为单一滚动列：所有卡片保持自然高度（flex 不收缩、不中途裁切），
   内容超出视口时整栏平滑滚动。短视口下不会出现卡片被压扁/互相重叠。 */
.ask-middle { display: contents; }
.brand { flex: 0 0 auto; }
.new-chat { flex: 0 0 auto; }

.brand { display: flex; align-items: center; gap: 10px; padding: 0 2px 4px; }
.brand-mark {
  width: 30px; height: 30px;
  border: var(--edge-w-heavy) solid var(--edge-ink);
  border-radius: var(--r-sm);
  background: var(--accent);
  display: flex; align-items: center; justify-content: center;
  box-shadow: var(--sh-hard-sm);
}
.brand-name { font-family: var(--font-display); font-size: var(--fs-lead); font-weight: 900; letter-spacing: .01em; color: var(--ink); }
.brand-sub { font-family: var(--font-mono); font-size: var(--fs-cap); color: var(--text-muted); letter-spacing: .04em; }

.card {
  flex: 0 0 auto;
  border: var(--edge-w-strong) solid var(--edge-ink);
  border-radius: var(--r-sm);
  background: var(--bg-raised);
  box-shadow: var(--sh-hard-sm);
  padding: 9px;
}
.card-title {
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--text-muted);
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 5px;
}
.card-count {
  margin-left: auto;
  background: var(--ink);
  color: var(--bg);
  font-family: var(--font-mono);
  font-size: var(--fs-cap);
  padding: 1px 7px;
  line-height: 18px;
}
.card-link {
  display: block;
  margin-top: 8px;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  color: var(--accent-text);
  text-decoration: none;
  border-bottom: 2px solid transparent;
}
.card-link:hover { border-bottom-color: var(--accent); }

.project-name { font-family: var(--font-display); font-size: var(--fs-h3); font-weight: 900; word-break: break-all; color: var(--ink); }
.project-name.muted { color: var(--text-muted); }
.project-hint {
  margin: 6px 0 0;
  font-family: var(--font-sans);
  font-size: var(--fs-sm);
  line-height: 1.55;
  color: var(--text-muted);
}
.project-root {
  margin-top: 6px;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.ingest-msg { margin-top: 6px; font-family: var(--font-sans); font-size: var(--fs-sm); color: var(--green); word-break: break-all; }
.ingest-msg.err { color: var(--danger); }
.file-list { margin-top: 6px; display: flex; flex-direction: column; gap: 6px; max-height: 78px; overflow-y: auto; }
.file-item { display: flex; align-items: center; gap: 8px; font-family: var(--font-sans); font-size: var(--fs-md); }
.file-ic {
  flex: 0 0 auto;
  font-family: var(--font-mono);
  font-size: var(--fs-cap);
  font-weight: 700;
  color: #fff;
  background: var(--ink);
  border: var(--edge-w-strong) solid var(--edge-ink);
  border-radius: var(--r-sm);
  padding: 1px 5px;
}
.file-nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-empty { font-family: var(--font-mono); font-size: var(--fs-sm); color: var(--text-faint); padding: 2px 0; }

.new-chat { width: 100%; margin-bottom: 0; }

.sess-list { display: flex; flex-direction: column; gap: 6px; max-height: 108px; overflow-y: auto; }
.sess-item {
  display: flex;
  align-items: stretch;
  gap: 6px;
  border: var(--edge-w-strong) solid var(--edge-ink);
  border-radius: var(--r-sm);
  background: var(--bg);
  box-shadow: var(--sh-hard-sm);
}
.sess-item.active { background: var(--accent); }
.sess-item.active .sess-main strong,
.sess-item.active .sess-main small { color: var(--accent-ink); }
.sess-item.active .ah-dot { color: var(--accent-ink); }
.sess-main {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  gap: 9px;
  text-align: left;
  background: none;
  border: none;
  padding: 9px 10px;
  cursor: pointer;
  color: var(--text);
  min-width: 0;
}
.sess-main:disabled { cursor: default; opacity: .6; }
.ah-dot { flex: 0 0 auto; font-size: var(--fs-cap); color: var(--text-faint); }
.sess-copy { display: flex; flex-direction: column; min-width: 0; gap: 2px; }
.sess-copy strong {
  font-family: var(--font-sans);
  font-size: var(--fs-md);
  font-weight: 700;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sess-copy small { font-family: var(--font-mono); font-size: var(--fs-cap); color: var(--text-muted); }
.sess-del {
  flex: 0 0 auto;
  width: 30px;
  border: none;
  border-left: var(--edge-w-strong) solid var(--edge-ink);
  background: none;
  color: var(--text-muted);
  font-size: var(--fs-h2);
  line-height: 1;
  cursor: pointer;
  transition: background 130ms, color 130ms;
}
.sess-del:hover:not(:disabled) { background: var(--danger); color: var(--danger-ink); }
.sess-del:disabled { opacity: .4; cursor: default; }
.sess-empty { font-family: var(--font-mono); font-size: var(--fs-sm); color: var(--text-faint); padding: 4px 2px; }

/* 资料文档：标题行右侧的小上传按钮（2026-10-19 取代整宽大按钮） */
.add-doc {
  flex: 0 0 auto;
  padding: 2px 8px;
  font-size: var(--fs-cap);
  box-shadow: none;
  transform: none;
}
.add-doc:hover:not(:disabled) { transform: translate(-1px, -1px); box-shadow: var(--sh-hard-sm); }

.model-line { display: flex; gap: 10px; font-family: var(--font-sans); font-size: var(--fs-md); padding: 1px 0; }
.model-k { flex: 0 0 34px; color: var(--text-faint); font-family: var(--font-mono); font-size: var(--fs-cap); text-transform: uppercase; }
.model-line b { font-weight: 600; word-break: break-all; }
.gear {
  margin-left: auto;
  border: var(--edge-w-strong, 2px) solid transparent;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 3px;
  display: flex;
  transition: background 130ms, color 130ms, border-color 130ms;
}
.gear:hover { background: var(--bg-hover); color: var(--accent); border-color: var(--border-strong); }

.btn {
  border: var(--edge-w-strong) solid var(--edge-ink);
  border-radius: var(--r-sm);
  --btn-fill: var(--accent);
  background: var(--accent);
  color: var(--accent-ink);
  font-family: var(--font-mono);
  font-weight: 700;
  padding: 6px 12px;
  font-size: var(--fs-md);
  cursor: pointer;
  box-shadow: var(--sh-hard-sm);
  transition: transform 130ms cubic-bezier(.2,.8,.2,1), box-shadow 130ms, background 130ms, color 130ms;
}
.btn:hover { transform: translate(-2px, -2px); box-shadow: var(--sh-hard); }
.btn:disabled { opacity: .55; cursor: default; }
.btn.ghost { --btn-fill: initial; background: var(--bg-raised); color: var(--text); }
.btn.ghost:hover { transform: translate(-2px, -2px); --btn-fill: var(--ink); background: var(--ink); color: var(--bg); }
.btn.block { width: 100%; }
.btn.sm { padding: 5px 10px; font-size: var(--fs-sm); }
</style>
