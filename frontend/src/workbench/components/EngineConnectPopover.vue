<script setup lang="ts">
// 游戏引擎连接弹层（MCP 服务器：godot-ai stdio / unity / unreal HTTP + godot-ai 插件引导）。
// 自包含：监听事件总线的 docmind:open-engine 事件开关，Teleport 到 body，
// 折叠态/外部容器裁剪均不影响显示。父组件只负责派发事件，不持有任何状态。
//
// 2026-10-19（P1）：入口原在顶栏「更多」菜单，属配置类能力，已统一收进设置页的
// 「运行与引擎」分区。`embedded` 模式把弹层就地渲染在设置面板里
// （Teleport disabled + 去掉 fixed 定位与浮层阴影），不再遮罩、不再抢 z-index。
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { askConfirm, askAlert } from '../composables/workbench'
import { mcpApi } from '../api'
import type { McpServer } from '../api'
import { appEvents } from '../eventBus'

const props = defineProps<{ embedded?: boolean }>()

const open = ref(!!props.embedded)
const servers = ref<McpServer[]>([])
const probing = ref<string | null>(null)
const probeResults = ref<Record<string, { ok: boolean; tool_count?: number; error?: string }>>({})
const connected = ref<Record<string, boolean>>({})
const addon = ref<Awaited<ReturnType<typeof mcpApi.addonStatus>> | null>(null)
const installing = ref(false)
const showAddForm = ref(false)
const savingServer = ref(false)
const newServer = ref<{ key: string; label: string; transport: 'stdio' | 'http'; command: string; url: string; enabled: boolean }>({
  key: '', label: '', transport: 'stdio', command: '', url: '', enabled: true,
})

async function refreshEnginePop() {
  try {
    const [s, a, st] = await Promise.allSettled([mcpApi.servers(), mcpApi.addonStatus(), mcpApi.status()])
    if (s.status === 'fulfilled') servers.value = s.value.servers
    if (a.status === 'fulfilled') addon.value = a.value
    if (st.status === 'fulfilled' && st.value.ok) {
      const active = st.value.active || []
      const next: Record<string, boolean> = {}
      for (const k of active) next[k] = true
      connected.value = next
    }
  } catch {
    /* 弹层打开失败保持空态 */
  }
}

watch(open, (v) => {
  if (v) void refreshEnginePop()
})

function onDocKey(ev: KeyboardEvent) {
  if (ev.key === 'Escape' && open.value) open.value = false
}
let offOpenEngine = () => {}
onMounted(() => {
  offOpenEngine = appEvents.on('docmind:open-engine', () => { open.value = true })
  window.addEventListener('keydown', onDocKey)
})
onBeforeUnmount(() => {
  offOpenEngine()
  window.removeEventListener('keydown', onDocKey)
})

async function probe(key: string) {
  probing.value = key
  try {
    const r = await Promise.race([
      mcpApi.probe(key),
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('连接超时：请先在 Godot 编辑器中打开项目并启用 godot-ai 插件')), 8000)),
    ])
    const ok = !!r.ok && !(r as { error?: string }).error
    probeResults.value[key] = { ok, tool_count: r.tool_count, error: r.error }
    // stdio 为长驻会话，连接成功后保持「已连接」；HTTP 无状态，不持有会话
    if (ok && servers.value.find((x) => x.key === key)?.transport === 'stdio') connected.value[key] = true
  } catch (e) {
    probeResults.value[key] = { ok: false, error: (e as Error).message }
  } finally {
    probing.value = null
  }
}

async function installAddon() {
  const st = addon.value
  const ok = await askConfirm({
    title: st?.installed ? '重装 godot-ai 插件？' : '安装 godot-ai 插件',
    message: st?.installed
      ? '将从 GitHub Releases 重新下载并覆盖 addons/godot_ai/，并改写 project.godot 的插件启用项。'
      : '将从 GitHub Releases 下载最新版插件到 addons/godot_ai/，并在 project.godot 中启用它。',
    detail: '不会改动其它代码文件。安装后需打开/重启 Godot 编辑器一次，AI 对话台才能通过 MCP 连接引擎。',
    confirmText: st?.installed ? '覆盖安装' : '安装',
  })
  if (!ok) return
  installing.value = true
  try {
    const r = await mcpApi.addonInstall(!!st?.installed)
    await askAlert({
      title: r.ok ? 'godot-ai 安装完成' : 'godot-ai 安装失败',
      message: r.ok ? `插件 v${r.version} 已安装并启用。${r.next || ''}` : (r.error || '未知错误'),
    })
    if (r.ok) await refreshEnginePop()
  } catch (e) {
    await askAlert({ title: 'godot-ai 安装失败', message: (e as Error).message })
  } finally {
    installing.value = false
  }
}

function resultOf(s: McpServer) {
  return probeResults.value[s.key]
}

async function closeServer(key: string) {
  try {
    await mcpApi.close(key)
  } catch {
    /* 断开失败保持原状 */
  } finally {
    connected.value[key] = false
  }
}

async function removeServer(key: string) {
  const ok = await askConfirm({
    title: '移除连接器',
    message: `确定移除「${key}」？内置预设会被禁用，自定义项会被删除。`,
    confirmText: '移除',
  })
  if (!ok) return
  try {
    await mcpApi.remove(key)
    connected.value[key] = false
    await refreshEnginePop()
  } catch {
    /* 移除失败保持原状 */
  }
}

async function addServer() {
  const ns = newServer.value
  if (!ns.key) return
  savingServer.value = true
  try {
    const cfg: Record<string, unknown> = { transport: ns.transport, enabled: ns.enabled }
    if (ns.label) cfg.label = ns.label
    if (ns.transport === 'stdio') cfg.command = ns.command
    else cfg.url = ns.url
    const r = await mcpApi.save(ns.key, cfg)
    if (!r.ok) {
      await askAlert({ title: '新增连接器失败', message: r.error || '未知错误' })
      return
    }
    showAddForm.value = false
    newServer.value = { key: '', label: '', transport: 'stdio', command: '', url: '', enabled: true }
    await refreshEnginePop()
  } catch (e) {
    await askAlert({ title: '新增连接器失败', message: (e as Error).message })
  } finally {
    savingServer.value = false
  }
}
function connectorGuide(s: McpServer) {
  if (s.key.includes('godot')) return '让 AI 读取场景、节点和运行日志；请先打开 Godot 项目。'
  if (s.key.includes('unity')) return '让 AI 查看 Unity 场景和组件；需先启动 Unity 并运行 MCP 插件。'
  if (s.key.includes('unreal')) return '让 AI 查询 Unreal 资产、Actor 和蓝图；需先启动 Editor Bridge。'
  return '让 AI 连接外部工具并调用其能力。'
}
</script>

<template>
  <!-- embedded 时 Teleport 失效，内容就地渲染在设置面板里（不遮罩、不抢 z-index） -->
  <Teleport to="body" :disabled="embedded">
    <div v-if="open && !embedded" class="cd-pop-mask wb-modal-backdrop" @click="open = false" />
    <div
      v-if="open"
      class="cd-pop"
      :class="embedded ? 'cd-inline' : 'wb-modal-shell'"
      :role="embedded ? 'region' : 'dialog'"
      :aria-modal="embedded ? undefined : 'true'"
      aria-label="连接游戏引擎"
      @click.stop
    >
      <div class="cd-pop-title" v-if="!embedded">连接游戏引擎</div>
      <!-- 内嵌时标题与副标题都由设置页的分区头提供，这里不再重复一遍 -->
      <div class="cd-pop-subtitle" v-if="!embedded">连接后，AI 才能读取引擎中的场景、脚本和运行日志。</div>
      <div v-if="addon && addon.is_godot_project" class="cd-addon">
        <div class="cd-addon-row">
          <span class="cd-dot" :class="addon.installed ? 'cd-dot-ok' : 'cd-dot-off'" />
          godot-ai 插件
          <span class="cd-addon-ver">{{ addon.installed ? 'v' + addon.version : '未安装' }}</span>
          <span v-if="addon.installed && addon.enabled" class="cd-tag cd-tag-ok">已启用</span>
          <span v-else-if="addon.installed" class="cd-tag cd-tag-warn">待在 Godot 中启用</span>
          <span class="cd-spacer" />
          <button class="cd-mini" :disabled="installing || !addon.uvx_available" @click="installAddon">
            {{ installing ? '安装中…' : (addon.installed ? '重装' : '安装') }}
          </button>
        </div>
        <div class="cd-preflight">
          <span :class="addon.godot_available ? 'cd-ok-text' : 'cd-err-text'">
            {{ addon.godot_available ? 'Godot 已发现' : 'Godot 未配置' }}
          </span>
          <span>·</span>
          <span :class="addon.uvx_available ? 'cd-ok-text' : 'cd-err-text'">
            {{ addon.uvx_available ? 'uvx 可用' : 'uvx 未安装' }}
          </span>
          <span class="cd-preflight-path" v-if="addon.godot">{{ addon.godot }}</span>
        </div>
        <div v-if="!addon.uvx_available" class="cd-warn-text">
          需先安装 uv（提供 uvx）：docs.astral.sh/uv，装完重启服务。
        </div>
      </div>
      <div v-for="s in servers" :key="s.key" class="cd-server">
        <div class="cd-server-row">
          <span class="cd-dot" :class="connected[s.key] ? 'cd-dot-ok' : (!s.enabled ? 'cd-dot-off' : 'cd-dot-idle')" />
          <span class="cd-server-name">{{ s.label || s.key }}</span>
          <span class="cd-server-meta">{{ s.transport === 'stdio' ? '本机插件' : '本机服务' }}</span>
          <span class="cd-spacer" />
          <button class="cd-mini" :disabled="probing === s.key || !s.enabled" @click="probe(s.key)">
            {{ probing === s.key ? '连接中…' : (connected[s.key] ? '重连' : '连接') }}
          </button>
          <button class="cd-mini cd-mini-stop" :disabled="!connected[s.key] || s.transport !== 'stdio'"
                  title="断开 stdio 长驻会话（HTTP 无状态服务无需断开）" @click="closeServer(s.key)">断开</button>
        </div>
        <div class="cd-server-guide">{{ connectorGuide(s) }}</div>
        <div v-if="connected[s.key]" class="cd-server-result">
          <span class="cd-ok-text">已连接{{ resultOf(s)?.tool_count != null ? ' · ' + resultOf(s)?.tool_count + ' 个工具可用' : '' }}</span>
        </div>
        <div v-else-if="resultOf(s)" class="cd-server-result">
          <span v-if="resultOf(s)?.ok" class="cd-ok-text">已断开（上次连接成功）</span>
          <span v-else class="cd-err-text" :title="resultOf(s)?.error">{{ resultOf(s)?.error || '连接失败（确认对应引擎/插件已运行）' }}</span>
        </div>
        <div v-if="s.help && !resultOf(s) && !connected[s.key]" class="cd-server-help">{{ s.help }}</div>
        <div class="cd-server-actions">
          <span class="cd-spacer" />
          <button class="cd-mini cd-mini-danger" @click="removeServer(s.key)">移除</button>
        </div>
      </div>

      <button class="cd-mini cd-add-toggle" @click="showAddForm = !showAddForm">+ 新增连接器</button>
      <div v-if="showAddForm" class="cd-add-form">
        <div class="cd-add-row">
          <input v-model="newServer.key" class="cd-input-sm" placeholder="key（字母数字_-，≤40）" />
          <input v-model="newServer.label" class="cd-input-sm" placeholder="显示名（可选）" />
        </div>
        <div class="cd-add-row">
          <select v-model="newServer.transport" class="cd-input-sm">
            <option value="stdio">stdio</option>
            <option value="http">http</option>
          </select>
          <label class="cd-add-chk"><input type="checkbox" v-model="newServer.enabled" /> 启用</label>
        </div>
        <div class="cd-add-row">
          <input v-if="newServer.transport === 'stdio'" v-model="newServer.command" class="cd-input-sm" placeholder="命令（如 uvx）" />
          <input v-else v-model="newServer.url" class="cd-input-sm" placeholder="url（http://…）" />
        </div>
        <div class="cd-add-actions">
          <button class="cd-mini" :disabled="savingServer || !newServer.key" @click="addServer">保存</button>
          <button class="cd-mini" @click="showAddForm = false">取消</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.cd-pop-mask {
  position: fixed;
  inset: 0;
  z-index: 59;
  background: var(--scrim-soft);
  backdrop-filter: blur(1px);
  animation: cd-mask-in .16s ease-out both;
}
.cd-pop {
  position: fixed;
  left: 10px; bottom: 64px;
  width: 460px; max-width: calc(100vw - 24px);
  max-height: 60vh; overflow-y: auto;
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  box-shadow: 0 14px 38px rgba(35,52,84,.2);
  padding: 10px 12px;
  z-index: 60;
  animation: cd-pop-in .2s cubic-bezier(.2,.8,.2,1) both;
}
.cd-pop-title { font-size: var(--fs-sm); font-weight: 600; color: var(--text); margin-bottom: 8px; }
/* 内嵌档（设置页「运行与引擎」分区）：去掉浮层定位、阴影、入场动画与边框。
   同理，模板里内嵌时不挂 .wb-modal-shell（它用 !important 定背景/边框/阴影）。 */
.cd-inline { position: static; left: auto; bottom: auto; width: auto; max-width: none; max-height: none; overflow: visible; padding: 0; background: none; border: 0; border-radius: 0; box-shadow: none; z-index: auto; animation: none; }
.cd-spacer { flex: 1; }
.cd-mini {
  height: 21px; padding: 0 9px; font-size: var(--fs-cap);
  border: 1px solid var(--border-strong); border-radius: 5px;
  background: transparent; color: var(--text-muted); cursor: pointer;
}
.cd-mini:hover:not(:disabled) { color: var(--text); border-color: var(--accent); }
.cd-mini:disabled { opacity: .45; cursor: default; }
.cd-mini-danger:hover:not(:disabled) { color: var(--danger); border-color: var(--danger); }
.cd-mini { transition: color .16s ease, border-color .16s ease, background .16s ease, transform .16s ease; }
.cd-mini:active:not(:disabled) { transform: translateY(1px); }
.cd-mini:focus-visible,
.cd-input-sm:focus-visible,
.cd-add-chk input:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--accent) 60%, transparent);
  outline-offset: 2px;
}

.cd-pop-subtitle { font-size: var(--fs-cap); color: var(--text-dim); line-height: 1.5; margin-bottom: 8px; }

.cd-addon {
  border: 1px solid var(--border); border-radius: 7px;
  padding: 8px 10px; margin-bottom: 10px; background: var(--bg-hover);
}

.cd-addon-row, .cd-server-row { display: flex; align-items: center; gap: 7px; font-size: var(--fs-sm); color: var(--text); }

.cd-addon-ver { color: var(--text-muted); font-size: var(--fs-cap); }

.cd-preflight { display: flex; gap: 6px; align-items: center; margin-top: 6px; font-size: var(--fs-cap); color: var(--text-faint); flex-wrap: wrap; }

.cd-preflight-path { font-family: var(--font-mono); font-size: var(--fs-cap); color: var(--text-faint); }

.cd-tag { font-size: var(--fs-cap); padding: 1px 6px; border-radius: 8px; }

.cd-tag-ok { color: var(--green); background: rgba(69,201,140,.12); }

.cd-tag-warn { color: var(--amber); background: rgba(227,168,58,.12); }

.cd-ok-text { color: var(--green); }

.cd-err-text { color: var(--danger); }

.cd-warn-text { font-size: var(--fs-cap); color: var(--amber); margin-top: 5px; }

.cd-mini-stop { border-color: var(--border-strong); }

.cd-mini-stop:hover:not(:disabled) { color: var(--danger); border-color: var(--danger); }


.cd-server-actions { display: flex; align-items: center; padding-top: 5px; }

.cd-add-toggle { margin-top: 6px; }

.cd-add-form { margin-top: 6px; padding: 8px; border: 1px dashed var(--border); border-radius: 6px; display: flex; flex-direction: column; gap: 6px; }

.cd-add-row { display: flex; gap: 6px; align-items: center; }

.cd-input-sm {
  flex: 1; min-width: 0; height: 24px; padding: 0 7px;
  background: var(--bg); color: var(--text);
  border: 1px solid var(--border-graphic); border-radius: 5px;
  font-size: var(--fs-cap); font-family: inherit; outline: none;
}

.cd-input-sm:focus { border-color: var(--accent); }

.cd-add-chk { font-size: var(--fs-cap); color: var(--text-muted); display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }

.cd-add-actions { display: flex; gap: 6px; justify-content: flex-end; }


.cd-server { padding: 8px 5px; border-top: 1px dashed var(--border); border-radius: 6px; transition: background .16s ease; }
.cd-server:hover { background: var(--bg-hover); }

.cd-server-name { font-size: var(--fs-sm); }

.cd-server-meta { font-family: var(--font-mono); font-size: var(--fs-cap); color: var(--text-faint); }

.cd-server-result { margin-top: 4px; font-size: var(--fs-cap); word-break: break-all; }

.cd-server-help { margin-top: 3px; font-size: var(--fs-cap); color: var(--text-faint); }

.cd-server-guide { margin: 4px 0 0 20px; font-size: var(--fs-cap); color: var(--text-dim); line-height: 1.45; }

.cd-dot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 7px; }

.cd-dot-ok { background: var(--green); box-shadow: 0 0 6px rgba(69,201,140,.6); }

.cd-dot-idle { background: var(--amber); }

.cd-dot-off { background: var(--text-faint); }

@keyframes cd-mask-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes cd-pop-in { from { opacity: 0; transform: translateY(8px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }

@media (max-width: 620px) {
  .cd-pop { left: 8px; right: 8px; bottom: 8px; width: auto; max-width: none; max-height: calc(100vh - 16px); border-radius: 10px; }
  .cd-addon-row, .cd-server-row { flex-wrap: wrap; }
  .cd-server-meta { margin-right: auto; }
  .cd-server-row .cd-spacer { display: none; }
  .cd-server-row .cd-mini { margin-left: auto; }
  .cd-add-row { flex-direction: column; align-items: stretch; }
  .cd-add-chk { align-self: flex-start; }
}
@media (prefers-reduced-motion: reduce) {
  .cd-pop-mask, .cd-pop, .cd-mini, .cd-server { animation: none; transition: none; }
}
</style>
