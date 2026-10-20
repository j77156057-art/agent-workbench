<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { agentApi } from '../api'

/**
 * AI 运行策略面板。
 *
 * 2026-10-19（P1）：原入口在顶栏「更多」菜单里，是**配置类**能力，按产品方向
 * 统一收进设置页的「运行与引擎」分区。`embedded` 模式把这个浮层就地渲染在
 * 设置面板里（Teleport disabled + 去掉 fixed 定位与浮层阴影），不再遮罩、不再
 * 抢 z-index。同时因为它只在那个分区被打开时才挂载，**每 3 秒一次的
 * /api/agent 轮询也只在那时才发生**（原先是常驻挂载、整页开着就一直轮询）。
 */
const props = defineProps<{ embedded?: boolean }>()

const open = ref(!!props.embedded)
const route = ref('local')
const complexity = ref(0)
const auto = ref(false)
const connectors = ref<{ key: string; label: string; enabled: boolean }[]>([])
const externalPath = ref('')
const approval = ref(false)
const msg = ref('')
const approvals = ref<any[]>([])
const gateRequests = ref<any[]>([])
let gateTimer: ReturnType<typeof setInterval> | undefined

async function load() {
  try {
    const [routing, connectorResult, approvalResult, gateResult] = await Promise.all([
      agentApi.routing(), agentApi.connectors(), agentApi.approvals(), agentApi.approvalRequests(),
    ])
    auto.value = !!routing.auto_cloud_enabled
    connectors.value = connectorResult.connectors || []
    approvals.value = approvalResult.approvals || []
    gateRequests.value = gateResult.items || []
  } catch (error) { msg.value = '设置加载失败：' + (error as Error).message }
}

async function request() {
  if (!externalPath.value) { msg.value = '请填写目标文件路径'; return }
  try {
    if (!approval.value) {
      const result = await agentApi.requestApproval([externalPath.value], '外部文件修改申请')
      if (result.approval) approvals.value.push(result.approval)
      msg.value = '已创建审批请求'
      return
    }
    const result = await agentApi.permission({ path: externalPath.value, allow_external: true, approved: true })
    msg.value = result.recorded ? '已记录授权' : (result.reason || '失败')
  } catch (error) { msg.value = '授权请求失败：' + (error as Error).message }
}

async function decide(id: string, status: string) {
  try { await agentApi.decide(id, status); await load() }
  catch (error) { msg.value = '审批失败：' + (error as Error).message }
}

async function decideGate(id: string, approved: boolean) {
  try {
    const result = await agentApi.decideApprovalRequest(id, approved)
    if (!result.ok) throw new Error(result.error || '审批失败')
    await load()
  } catch (error) { msg.value = '工具动作审批失败：' + (error as Error).message }
}

function gateLabel(row: any) {
  const action = String(row.target || '').split(':')[1] || '动作'
  const labels: Record<string, string> = { click: '点击', type: '输入文字', drag: '拖拽', key: '按键', save: '保存' }
  return `${row.action === 'desktop_action' ? '桌面操作' : '工具动作'} · ${labels[action] || action}（参数已绑定）`
}

function onRoute(event: Event) {
  const detail = (event as CustomEvent).detail
  route.value = detail?.route || 'local'
  complexity.value = detail?.complexity || 0
}

onMounted(() => {
  load()
  gateTimer = setInterval(load, 3000)
  window.addEventListener('docmind-agent-route', onRoute)
})
onBeforeUnmount(() => {
  if (gateTimer) clearInterval(gateTimer)
  window.removeEventListener('docmind-agent-route', onRoute)
})

function show() { open.value = true }
defineExpose({ show })
</script>

<template>
  <div class="ap" :class="{ 'ap-embedded': embedded }">
    <button v-if="!embedded" class="ap-trigger" title="AI 在哪运行：本地显卡 / 云端自动切换，以及对外操作授权" @click="open = !open">
      <svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true"><circle cx="6.5" cy="6.5" r="2.05" stroke="currentColor" stroke-width="1.05"/><path d="M6.5 1.3 V2.7 M6.5 10.3 V11.7 M1.3 6.5 H2.7 M10.3 6.5 H11.7 M2.8 2.8 L3.8 3.8 M9.2 9.2 L10.2 10.2 M10.2 2.8 L9.2 3.8 M3.8 9.2 L2.8 2.8" stroke="currentColor" stroke-width="1.05" stroke-linecap="round"/></svg>
      <span class="ap-label">AI 设置 · {{ route === 'local' ? '本地' : route }}</span>
    </button>
    <!-- embedded 时 Teleport 失效，内容就地渲染在设置面板里（不遮罩、不抢 z-index） -->
    <Teleport to="body" :disabled="embedded">
      <div v-if="open && !embedded" class="ap-backdrop wb-modal-backdrop" @click="open = false"></div>
      <div
        v-if="open"
        class="ap-pop"
        :class="embedded ? 'ap-inline' : 'wb-modal-shell'"
        :role="embedded ? 'region' : 'dialog'"
        :aria-modal="embedded ? undefined : 'true'"
        aria-label="AI 设置"
      >
        <b>AI 在哪运行</b>
        <div>当前：{{ route === 'local' ? '本地显卡（免费）' : route }} · 本轮复杂度 {{ complexity }}</div>
        <div>难题自动转云端：{{ auto ? '已启用' : '已关闭' }}</div>
        <b v-if="gateRequests.length" class="ap-heading">待处理的工具动作</b>
        <div v-for="gate in gateRequests" :key="gate.id" class="ap-approval">
          <div>{{ gateLabel(gate) }}</div><small>请求 {{ gate.id }} · 风险 {{ gate.risk || 'L2' }}</small>
          <button @click="decideGate(gate.id, true)">批准</button><button @click="decideGate(gate.id, false)">拒绝</button>
        </div>
        <b class="ap-heading">外接工具</b>
        <div v-if="!connectors.length" class="ap-empty">暂无外接工具（游戏引擎等在下面的「游戏引擎连接」里连）</div>
        <div v-for="connector in connectors" :key="connector.key">{{ connector.label || connector.key }} · {{ connector.enabled ? '可用' : '未启用' }}</div>
        <b class="ap-heading">改项目外文件的授权</b>
        <input v-model="externalPath" placeholder="项目文件夹之外的路径" />
        <label><input v-model="approval" type="checkbox" /> 我已确认，允许修改</label><button @click="request">提交授权</button><small>{{ msg }}</small>
        <div v-for="item in approvals" :key="item.id" class="ap-approval">
          <div>{{ item.id }} · {{ item.status === 'pending' ? '待批准' : item.status }}</div><div>{{ item.summary }}</div>
          <pre v-if="item.diff">{{ item.diff }}</pre><button v-if="item.status === 'pending'" @click="decide(item.id, 'approved')">批准</button><button v-if="item.status === 'pending'" @click="decide(item.id, 'rejected')">拒绝</button>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.ap{position:relative}.ap-trigger{display:inline-flex;align-items:center;gap:5px;background:transparent;color:var(--text-muted);border:1px solid var(--border-strong);border-radius:5px;padding:5px 9px;cursor:pointer;white-space:nowrap;transition:color .16s ease,border-color .16s ease,background .16s ease,transform .16s ease}.ap-trigger:hover{color:var(--text);border-color:var(--accent);background:var(--bg-hover)}.ap-trigger:active{transform:translateY(1px)}.ap-trigger:focus-visible,.ap-pop button:focus-visible,.ap-pop input:focus-visible,.ap-pop label:focus-within{outline: 2px solid var(--accent-deep);outline-offset:2px}.ap-icon{font-size: var(--fs-sm)}.ap-backdrop{position:fixed;inset:0;z-index:299;background: var(--scrim-soft);backdrop-filter:blur(1px);animation:ap-fade-in .16s ease-out both}.ap-pop{position:fixed;right:16px;top:64px;max-height:calc(100vh - 90px);max-width:calc(100vw - 48px);overflow:auto;width:min(360px,calc(100vw - 48px));padding:13px 14px;background:var(--bg-raised);border:1px solid var(--border-strong);border-radius:10px;box-shadow:0 16px 42px rgba(35,52,84,.2);z-index:300;font-size: var(--fs-cap);line-height:1.8;animation:ap-pop-in .2s cubic-bezier(.2,.8,.2,1) both}.ap-pop b{display:block;color:var(--text)}.ap-heading{margin-top:8px;padding-top:7px;border-top:1px solid var(--border)}.ap-empty{color:var(--text-faint);padding:4px 0}.ap-pop input[type=text],.ap-pop input:not([type]){width:100%;box-sizing:border-box;margin:3px 0;padding:6px 8px;background:var(--bg);border:1px solid var(--border);border-radius:5px;color:var(--text);font:inherit;transition:border-color .16s ease,box-shadow .16s ease}.ap-pop input:focus{border-color:color-mix(in srgb, var(--accent) 53%, transparent);box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 20%, transparent);outline:none}.ap-pop label{display:flex;gap:5px;align-items:center;color:var(--text-muted)}.ap-pop button{margin-top:5px;background:linear-gradient(180deg, var(--accent-loud), var(--accent));border:1px solid var(--accent-deep);color:#fff;border-radius:5px;padding:4px 11px;font-size: var(--fs-cap);cursor:pointer;transition:filter .16s ease,transform .16s ease,opacity .16s ease}.ap-pop button:hover:not(:disabled){filter:brightness(1.06)}.ap-pop button:active:not(:disabled){transform:translateY(1px)}.ap-pop small{display:block;color:var(--green);margin-top:3px}.ap-approval{margin-top:7px;padding:7px 8px;background:var(--bg-hover);border:1px solid var(--border);border-radius:6px;transition:border-color .16s ease,box-shadow .16s ease}.ap-approval:hover{border-color:var(--border-strong);box-shadow:0 3px 10px rgba(35,52,84,.07)}.ap-approval pre{max-height:90px;overflow:auto;margin:4px 0;font:10px var(--font-mono)}.ap-approval button{margin-right:6px}
/* 内嵌档（设置页「运行与引擎」分区）：去掉浮层定位、阴影、入场动画与边框，
   变成设置面板里的一段普通内容。不再遮罩，也不抢 z-index。
   注意：模板里内嵌时**不挂 .wb-modal-shell**（那套用 !important 定背景/边框/阴影），
   所以这里不必再跟 !important 对冲。 */
.ap-inline{position:static;width:auto;max-width:none;max-height:none;overflow:visible;padding:0;background:none;border:0;border-radius:0;box-shadow:none;z-index:auto;animation:none;font-size: var(--fs-sm)}
.ap-inline .ap-heading{margin-top:14px}
.ap-embedded .ap-trigger{display:none}
@keyframes ap-fade-in{from{opacity:0}to{opacity:1}}@keyframes ap-pop-in{from{opacity:0;transform:translateY(-6px) scale(.985)}to{opacity:1;transform:translateY(0) scale(1)}}
@media (max-width:560px){.ap-pop{left:8px;right:8px;top:8px;bottom:8px;width:auto;max-width:none;max-height:none;border-radius:10px}.ap-label{max-width:120px;overflow:hidden;text-overflow:ellipsis}}
@media (prefers-reduced-motion:reduce){.ap-trigger,.ap-backdrop,.ap-pop,.ap-pop button,.ap-approval{animation:none!important;transition:none!important}}
</style>
