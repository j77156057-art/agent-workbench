<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { engineApi, taskApi, comfyApi, getProjectId, getActiveTask, setActiveTask } from '../api'
import { usePolling } from '../composables/polling'
import { useWorkbench, askConfirm } from '../composables/workbench'
import { useEngineConfig } from '../composables/useEngineConfig'
const { jumpToLine } = useWorkbench()
// 引擎取自项目配置（设置页「引擎与项目」），不再写死 Godot。
const engineCfg = useEngineConfig()
const panelProject = getProjectId()
let disposed = false
const activeTask = getActiveTask(panelProject)
const open = ref(false), title = ref(''), region = ref(activeTask.region), files = ref(''), result = ref(''), impact = ref<string[]>([]), taskId = ref(activeTask.id), tasks = ref<Record<string, unknown>[]>([])
const running = ref(false), busy = ref(false), busyAction = ref(''), logs = ref<string[]>([]), errors = ref<{path:string;line:number;message:string}[]>([]), comfyUrl = ref('http://127.0.0.1:8188'), comfyState = ref('未检测'), workflow = ref(''), comfyResult = ref(''), promptId = ref(''), outputs = ref<{filename?:string;subfolder?:string;type?:string;preview_url?:string;mime?:string;asset_kind?:string;preview_supported?:boolean}[]>([]), comfyTemplates = ref<{id:string;name:string;model:string;kind:string;workflow?:string;schema?:Record<string,string>}[]>([]), comfySelected = ref<{schema?:Record<string,string>}|null>(null), comfyParams = ref<Record<string,unknown>>({prompt:'',width:512,height:512,frames:16,steps:8,seed:42,filename_prefix:'docmind'}) , comfyHistory = ref<string[]>([]), comfyPage = ref(1), comfyTotal = ref(0)
const unrealState = ref('未连接'), unrealAssets = ref<string[]>([]), unrealActors = ref<{name:string;class:string}[]>([])
async function loadTasks() { if (disposed || panelProject !== getProjectId()) return; try { tasks.value = (await taskApi.list()).tasks.slice(-5).reverse() } catch (e) { result.value = '任务列表加载失败：' + (e as Error).message } }
async function refresh() { if (disposed || panelProject !== getProjectId()) return; try { running.value = (await engineApi.status()).running; const r = await engineApi.logs(); logs.value = r.lines.slice(-8); errors.value = r.errors } catch(e) { result.value='引擎状态读取失败：'+(e as Error).message } }
async function analyze() {
  busy.value = true; busyAction.value = 'analyze'; result.value = ''
  try { const r = await taskApi.impact({ title: title.value, region: region.value, files: files.value.split(/[,\n]/).map(x => x.trim()).filter(Boolean), allowed_paths: region.value ? [region.value] : [] }); if (!r.ok) { scopeErrors.value = r.scope?.errors || []; result.value = r.scope?.errors?.length ? `影响范围算不了：任务范围有 ${scopeErrors.value.length} 处越界` : (r.error || '影响范围分析失败'); return } scopeErrors.value = []; impact.value = r.files || []; result.value = `影响文件 ${impact.value.length} 个` } catch (e) { result.value = (e as Error).message } finally { busy.value = false; busyAction.value = '' }
}
async function saveTask() { busy.value = true; busyAction.value = 'save'; try { const r = await taskApi.create({ title: title.value, region: region.value, files: files.value.split(/[,\n]/).map(x => x.trim()).filter(Boolean), allowed_paths: region.value ? [region.value] : [], status: 'open' }); taskId.value = String(r.task?.id || ''); if (taskId.value) { setActiveTask({ id: taskId.value, region: region.value, allowedPaths: region.value ? [region.value] : [] }, panelProject) } result.value = r.ok ? `任务已保存 ${taskId.value}` : (r.error || '保存失败'); await loadTasks() } catch (e) { result.value = (e as Error).message } finally { busy.value = false; busyAction.value = '' } }
async function createBranch() { busy.value = true; busyAction.value = 'branch'; try { const r=await taskApi.branch({id:taskId.value,title:title.value}); result.value=r.ok?`已切换分支 ${r.branch}`:(r.error||'创建分支失败') } catch(e){ result.value=(e as Error).message } finally { busy.value = false; busyAction.value = '' } }
// ---- 任务范围 / 快照 / 回滚（这三个端点的客户端早就有，调用点一直是零）----
const scopeErrors = ref<{ path: string; error: string }[]>([])
const taskSnapshot = ref<Record<string, unknown> | null>(null)

function taskPayload() {
  return {
    id: taskId.value, title: title.value, region: region.value,
    files: files.value.split(/[,\n]/).map(x => x.trim()).filter(Boolean),
    allowed_paths: region.value ? [region.value] : [],
  }
}
async function validateScope() {
  busy.value = true; busyAction.value = 'validate'; scopeErrors.value = []
  try {
    const r = await taskApi.validate(taskPayload())
    // validate 直接返回 TaskScope 本体：errors 就在顶层。
    scopeErrors.value = r.errors || []
    result.value = r.ok
      ? `任务范围校验通过（分区 ${r.region || '未限定'}，允许路径 ${(r.allowed_paths || []).length} 条）`
      : `任务范围有 ${scopeErrors.value.length} 处越界`
  } catch (e) { result.value = (e as Error).message } finally { busy.value = false; busyAction.value = '' }
}
async function saveSnapshot() {
  busy.value = true; busyAction.value = 'snapshot'
  try {
    const r = await taskApi.snapshot(taskPayload())
    if (!r.ok) {
      // 422：范围越界。后端把具体越界项放在 body.scope.errors 里，别把它吞了。
      scopeErrors.value = r.scope?.errors || []
      result.value = `存快照失败：任务范围有 ${scopeErrors.value.length} 处越界`
      return
    }
    taskSnapshot.value = (r.snapshot || null) as Record<string, unknown> | null
    scopeErrors.value = []
    const n = Object.keys((r.snapshot?.files || {}) as object).length
    result.value = `已存快照（${n} 个文件，可用于回滚）`
  } catch (e) { result.value = (e as Error).message } finally { busy.value = false; busyAction.value = '' }
}
async function revertSnapshot() {
  if (!taskSnapshot.value) { result.value = '还没有快照，先点「存快照」。'; return }
  const ok = await askConfirm({
    title: '回滚到快照？',
    message: '会覆盖（或删除）这个任务里的文件，回到快照时的内容。',
    detail: '这是破坏性操作：快照之后做的改动会丢失。',
    confirmText: '回滚',
    danger: true,
  })
  if (!ok) return
  busy.value = true; busyAction.value = 'revert'
  try {
    const r = await taskApi.revert({ ...taskPayload(), snapshot: taskSnapshot.value })
    if (!r.ok) {
      scopeErrors.value = r.scope?.errors || []
      result.value = r.error || `回滚失败：任务范围有 ${scopeErrors.value.length} 处越界`
      return
    }
    const n = (r.results || []).filter(x => x.ok).length
    scopeErrors.value = []
    result.value = `已回滚 ${n} 个文件`
  } catch (e) { result.value = (e as Error).message } finally { busy.value = false; busyAction.value = '' }
}
async function verifyTask() { busy.value = true; busyAction.value = 'verify'; try { const r = await taskApi.verify({ id: taskId.value, title: title.value, region: region.value, files: files.value.split(/[,\n]/).map(x => x.trim()).filter(Boolean), allowed_paths: region.value ? [region.value] : [] }); result.value = r.ok ? '任务验证通过' : '任务验证失败：请查看检查结果' } catch (e) { result.value = (e as Error).message } finally { busy.value = false; busyAction.value = '' } }
async function toggleEngine() { busy.value = true; busyAction.value = 'engine'; try { await engineCfg.load(); const r = running.value ? await engineApi.stop() : await engineApi.start(engineCfg.engine.value); running.value = 'running' in r ? !!r.running : false; await refresh() } catch (e) { result.value = (e as Error).message } finally { busy.value = false; busyAction.value = '' } }
async function reloadEngine() { busy.value = true; busyAction.value = 'reload'; try { const r = await engineApi.reload(); result.value = r.ok ? `${engineCfg.engineName.value} 已热重载` : (r.error || '热重载失败'); running.value = !!r.running; await refresh() } catch (e) { result.value = (e as Error).message } finally { busy.value = false; busyAction.value = '' } }
async function verifyEngine() { busy.value = true; busyAction.value = 'engine'; try { await engineCfg.load(); const r = await engineApi.verify(engineCfg.engine.value); result.value = r.ok ? `${engineCfg.engineName.value} 校验通过` : (r.error || `${engineCfg.engineName.value} 校验失败`) } catch (e) { result.value = (e as Error).message } finally { busy.value = false; busyAction.value = '' } }
async function loadComfyHistory(page = comfyPage.value) { if (disposed || panelProject !== getProjectId()) return; try { const h=await comfyApi.jobs(page,20); comfyPage.value=page; comfyTotal.value=h.total||0; const ids=(h.items||[]).map(x=>String(x.prompt_id||'' )).filter(Boolean); comfyHistory.value=ids } catch (e) { comfyResult.value='历史加载失败：'+(e as Error).message } }
onMounted(loadTasks); onMounted(async () => { try { comfyTemplates.value = (await comfyApi.templates()).templates } catch {}; await loadComfyHistory() })
// 引擎状态/日志轮询：仅弹窗打开且页面可见时进行（旧实现无论弹窗开关恒定 3s 轮询）
usePolling(refresh, 3000, { active: open })
// ComfyUI 任务进度：弹窗打开且确有在途生成时才轮询
usePolling(() => { void pollComfy() }, 4000, {
  active: computed(() => open.value && !!promptId.value),
})
onBeforeUnmount(() => { disposed = true })
async function checkComfy() { try { const r = await comfyApi.status(comfyUrl.value); comfyState.value = r.available ? '可用' : '不可用' } catch { comfyState.value = '不可用' } }
async function startComfy() { comfyResult.value = '正在启动 ComfyUI…'; try { const r=await comfyApi.start(); comfyResult.value=r.ok ? `ComfyUI 已启动（PID ${r.pid||'?' }）` : (r.error||'启动失败'); await checkComfy() } catch(e) { comfyResult.value=(e as Error).message } }
async function stopComfy() { try { const r=await comfyApi.stop(); comfyResult.value=r.ok ? 'ComfyUI 已停止' : (r.error||'停止失败'); await checkComfy() } catch(e) { comfyResult.value=(e as Error).message } }
async function queueComfy() { try { const w = JSON.parse(workflow.value); const r = await comfyApi.queue(w, comfyUrl.value); promptId.value = String(r.response?.prompt_id || ''); if (promptId.value) { comfyHistory.value = [promptId.value, ...comfyHistory.value.filter(x => x !== promptId.value)].slice(0, 10); void loadComfyHistory() }; comfyResult.value = r.ok ? `已提交 ${promptId.value}` : (r.error || '提交失败') } catch { comfyResult.value = 'Workflow JSON 无效' } }
async function pollComfy() { if (!promptId.value || disposed || panelProject !== getProjectId()) return; const queried = promptId.value; try { const r = await comfyApi.history(promptId.value, comfyUrl.value); if (disposed || panelProject !== getProjectId() || queried !== promptId.value) return; outputs.value = r.outputs || []; const p = r.progress; comfyResult.value = r.done ? `生成完成（${outputs.value.length} 个结果）` : `生成中 ${p ? `${p.percent}%（${p.executed_nodes}/${p.total_nodes} 节点）` : ''}` } catch(e) { comfyResult.value='查询失败：'+(e as Error).message } }
async function cancelComfy() { if (!promptId.value) return; const r = await comfyApi.cancel(promptId.value, comfyUrl.value); comfyResult.value = r.ok ? `已取消 ${promptId.value}（租约${r.lease_released ? '已释放' : '未持有'}）` : (r.error || '取消失败') }
async function retryComfy() { if (!promptId.value) return; const r = await comfyApi.retry(promptId.value, comfyUrl.value); if (r.ok) { promptId.value=String(r.response?.prompt_id||''); comfyResult.value=`已重试（第 ${r.retry_count||1} 次）` } else comfyResult.value=r.error||'重试失败' }
async function selectComfyHistory(id: string) { promptId.value = id; await pollComfy() }
async function refreshUnreal() { try { const s = await engineApi.unrealBridgeStatus(); unrealState.value = s.available ? '已连接' : '未连接'; if (s.available) { const [a,b] = await Promise.all([engineApi.unrealAssets(), engineApi.unrealActors()]); unrealAssets.value = a.assets || []; unrealActors.value = b.actors || [] } } catch { unrealState.value = '未连接' } }
// 蓝图 / Actor 详情：客户端早就有，调用点一直是零 —— 列表里点一下就能看。
const unrealDetail = ref('')
const unrealDetailBusy = ref(false)
async function showBlueprint(path: string) {
  unrealDetailBusy.value = true
  unrealDetail.value = ''
  try {
    const r = await engineApi.unrealBlueprint(path)
    if (!r.ok || !r.available) { unrealDetail.value = r.error || '读取失败（Unreal bridge 未连接？）'; return }
    const nodes = r.nodes?.length ? `，节点 ${r.nodes.length}` : ''
    const vars = r.variables?.length ? `，变量 ${r.variables.length}` : ''
    unrealDetail.value = `蓝图 ${path}${nodes}${vars}\n${(r.blueprint || '').slice(0, 600)}`
  } catch (e) { unrealDetail.value = '读取失败：' + (e as Error).message } finally { unrealDetailBusy.value = false }
}
async function showActor(name: string) {
  unrealDetailBusy.value = true
  unrealDetail.value = ''
  try {
    const r = await engineApi.unrealActor(name)
    if (!r.ok || !r.available) { unrealDetail.value = r.error || '读取失败（Unreal bridge 未连接？）'; return }
    const comps = r.components?.length ? `，组件 ${r.components.length}` : ''
    const props = r.properties?.length ? `，属性 ${r.properties.length}` : ''
    unrealDetail.value = `Actor ${name}${comps}${props}\n${(r.actor || '').slice(0, 600)}`
  } catch (e) { unrealDetail.value = '读取失败：' + (e as Error).message } finally { unrealDetailBusy.value = false }
}
async function importOutput(o: Record<string, unknown>) { const v=await comfyApi.validateProvenance(o); if (!v.ok) { comfyResult.value=`来源信息无效：${(v.errors||[]).join('、')}`; return }; const x = await comfyApi.import(promptId.value, o, comfyUrl.value); comfyResult.value = x.ok ? (v.review_required ? `已导入 ${x.path}（许可证待人工审核）` : `已导入 ${x.path}`) : (x.error || '导入失败') }
async function loadComfyTemplate(id: string) { const meta = comfyTemplates.value.find(x => x.id === id); comfySelected.value = meta || null; const r = await comfyApi.template(id); if (r.ok && r.workflow) { workflow.value = JSON.stringify(r.workflow, null, 2); comfyResult.value = `已加载模板（${r.format || 'api'}）` } else comfyResult.value = r.error || '模板加载失败' }
async function applyComfyParams() { try { const r=await comfyApi.apply(JSON.parse(workflow.value), comfyParams.value); if (r.ok && r.workflow) { workflow.value=JSON.stringify(r.workflow,null,2); comfyResult.value='参数已应用并完成节点校验' } else comfyResult.value=r.error||'参数应用失败' } catch { comfyResult.value='Workflow JSON 无效' } }
async function showFailure(action: () => Promise<unknown>) { try { await action() } catch(e) { comfyResult.value='操作失败：'+(e as Error).message } }
/** 供顶栏「工具」菜单外部唤起（自带触发按钮已隐藏）。 */
function show() { open.value = true }
defineExpose({ show })
</script>
<template>
  <div class="te-panel">
    <button class="te-trigger" title="任务与生成：登记修改任务、启动 Godot、AI 画图/视频（ComfyUI）、连接 Unreal" @click="open = !open"><svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true"><rect x="1.5" y="2.2" width="6.2" height="8.6" rx="1" stroke="currentColor" stroke-width="0.95"/><path d="M9.2 3.2 H11.5 M10.35 2.05 V4.35" stroke="currentColor" stroke-width="0.95" stroke-linecap="round"/><path d="M3.4 5.2 H5.8 M3.4 7.2 H5.8" stroke="currentColor" stroke-width="0.85" stroke-linecap="round"/></svg><span class="te-label">任务与生成</span></button>
      <Teleport to="body"><div v-if="open" class="te-backdrop wb-modal-backdrop" @click="open=false"></div><div v-if="open" class="te-pop wb-modal-shell" role="dialog" aria-modal="true" aria-label="任务与生成">
      <div class="te-head"><b>修改任务（高级）</b><button @click="open=false">×</button></div>
      <div v-if="tasks.length" class="te-tasks"><span v-for="t in tasks" :key="String(t.id)">{{ t.title }} · {{ t.status }}</span></div>
      <input v-model="title" placeholder="任务目标，例如：修改玩家受击逻辑" />
      <input v-model="region" placeholder="分区，例如 behaviors" />
      <textarea v-model="files" placeholder="文件路径，每行或逗号分隔" />
      <button class="te-action" :aria-busy="busyAction === 'save'" :disabled="busy" @click="saveTask">{{ busyAction === 'save' ? '保存中…' : '保存任务' }}</button><button class="te-action" :aria-busy="busyAction === 'branch'" :disabled="busy" @click="createBranch">{{ busyAction === 'branch' ? '创建中…' : '任务分支' }}</button><button class="te-action" :aria-busy="busyAction === 'analyze'" :disabled="busy" @click="analyze">{{ busyAction === 'analyze' ? '分析中…' : '分析影响范围' }}</button><button class="te-action" :aria-busy="busyAction === 'verify'" :disabled="busy" @click="verifyTask">{{ busyAction === 'verify' ? '验证中…' : '执行验证' }}</button>
      <button class="te-action" :aria-busy="busyAction === 'validate'" :disabled="busy" @click="validateScope">{{ busyAction === 'validate' ? '校验中…' : '校验范围' }}</button><button class="te-action" :aria-busy="busyAction === 'snapshot'" :disabled="busy" @click="saveSnapshot">{{ busyAction === 'snapshot' ? '存中…' : '存快照' }}</button><button class="te-action te-action-warn" :aria-busy="busyAction === 'revert'" :disabled="busy || !taskSnapshot" @click="revertSnapshot">{{ busyAction === 'revert' ? '回滚中…' : '回滚快照' }}</button>
      <p v-if="result" class="te-result">{{ result }}</p>
      <ul v-if="scopeErrors.length" class="te-list te-list-warn"><li v-for="e in scopeErrors" :key="e.path">{{ e.path }} · {{ e.error }}</li></ul>
      <ul v-if="impact.length" class="te-list"><li v-for="p in impact" :key="p">{{ p }}</li></ul>
      <div class="te-engine"><span :class="{ live: running }" /> Godot {{ running ? '运行中' : '未运行' }} <button :disabled="busy" @click="verifyEngine">{{ busyAction === 'engine' ? '校验中…' : '校验' }}</button><button v-if="running" :disabled="busy" @click="reloadEngine">热重载</button><button :disabled="busy" @click="toggleEngine">{{ busyAction === 'engine' ? (running ? '停止中…' : '启动中…') : (running ? '停止' : '启动') }}</button></div>
      <div class="te-unreal"><b>Unreal 连接：{{ unrealState }}</b><button @click="refreshUnreal">刷新</button><small>Blueprint {{ unrealAssets.length }} · Actor {{ unrealActors.length }}</small></div>
      <div v-if="unrealAssets.length || unrealActors.length" class="te-unreal-list"><button v-for="a in unrealAssets.slice(0,8)" :key="a" class="te-unreal-item" :disabled="unrealDetailBusy" @click="showBlueprint(a)">BP · {{ a }}</button><button v-for="a in unrealActors.slice(0,8)" :key="a.name" class="te-unreal-item" :disabled="unrealDetailBusy" @click="showActor(a.name)">Actor · {{ a.name }} ({{ a.class }})</button></div>
      <pre v-if="unrealDetail" class="te-unreal-detail">{{ unrealDetail }}</pre>
      <pre v-if="logs.length" class="te-logs">{{ logs.join('\n') }}</pre>
      <button v-for="e in errors" :key="`${e.path}:${e.line}`" class="te-error" @click="jumpToLine(e.path, e.line)">{{ e.path }}:{{ e.line }} · {{ e.message }}</button>
      <div class="te-comfy"><b>AI 画图 / 视频（ComfyUI）</b><button @click="startComfy">启动</button><button @click="stopComfy">停止</button><button @click="loadComfyHistory()">刷新历史</button><input v-model="comfyUrl" @change="checkComfy" /><div class="te-templates"><button v-for="t in comfyTemplates" :key="t.id" @click="showFailure(() => loadComfyTemplate(t.id))">{{ t.name }}</button></div><div v-if="comfySelected?.schema" class="te-params"><label v-for="(_,key) in comfySelected.schema" :key="key">{{ key }}<input v-model="comfyParams[key]" /></label><button @click="applyComfyParams">应用参数</button></div><div class="te-history-pager"><button :disabled="comfyPage <= 1" @click="loadComfyHistory(comfyPage - 1)">上一页</button><span>{{ comfyPage }} / {{ Math.max(1, Math.ceil(comfyTotal / 20)) }}</span><button :disabled="comfyPage * 20 >= comfyTotal" @click="loadComfyHistory(comfyPage + 1)">下一页</button></div><div v-if="comfyHistory.length" class="te-history"><button v-for="id in comfyHistory" :key="id" @click="selectComfyHistory(id)">{{ id.slice(0,8) }}</button></div><textarea v-model="workflow" placeholder="粘贴 workflow JSON" /><button @click="queueComfy">提交生成</button><button v-if="promptId" @click="pollComfy">查询结果</button><button v-if="promptId" class="te-cancel" @click="showFailure(cancelComfy)">取消生成</button><button v-if="promptId" @click="showFailure(retryComfy)">失败重试</button><span>{{ comfyState }} {{ comfyResult }}</span></div>
      <div v-if="outputs.length" class="te-outputs"><div v-for="o in outputs" :key="o.filename" class="te-output"><img v-if="o.mime?.startsWith('image/')" :src="o.preview_url" :alt="o.filename" /><audio v-else-if="o.mime?.startsWith('audio/')" :src="o.preview_url" controls /><video v-else-if="o.mime?.startsWith('video/')" :src="o.preview_url" controls /><div v-else-if="o.asset_kind === '3d'" class="te-3d-placeholder">3D 资源<br/><small>格式识别成功，当前提供安全导入元数据</small></div><div v-else class="te-unknown-output">{{ o.mime || '未知格式' }}<br/><small>可下载或导入</small></div><span>{{ o.filename }}</span><button @click="showFailure(() => importOutput(o))">导入</button></div></div>
    </div></Teleport>
  </div>
</template>
<style scoped>
.te-panel{position:relative}.te-trigger{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--border-strong);background:transparent;color:var(--text-muted);border-radius:5px;padding:5px 9px;cursor:pointer;white-space:nowrap;transition:color .16s ease,border-color .16s ease,background .16s ease,transform .16s ease}.te-trigger:hover{color:var(--text);border-color:var(--accent);background:var(--bg-hover)}.te-trigger:active{transform:translateY(1px)}.te-trigger:focus-visible,.te-head button:focus-visible,.te-pop button:focus-visible,.te-pop input:focus-visible,.te-pop textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.te-backdrop{position:fixed;inset:0;z-index:299;background: var(--scrim-soft);backdrop-filter:blur(1px);animation:te-fade-in .16s ease-out both}.te-pop{position:fixed;right:16px;top:64px;max-height:calc(100vh - 90px);max-width:calc(100vw - 48px);overflow:auto;width:min(420px,calc(100vw - 48px));padding:14px;background:var(--bg-raised);border:1px solid var(--border-strong);border-radius:9px;box-shadow:0 16px 42px rgba(35,52,84,.2);z-index:300;animation:te-pop-in .2s cubic-bezier(.2,.8,.2,1) both}.te-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--border)}.te-head b{font-size: var(--fs-sm)}.te-head button{background:none;border:0;color:var(--text-muted);font-size: var(--fs-lead);line-height:1;cursor:pointer;border-radius:4px}.te-pop input,.te-pop textarea{width:100%;margin:4px 0;padding:7px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:5px;font:inherit;transition:border-color .16s ease,box-shadow .16s ease}.te-pop input:focus,.te-pop textarea:focus{border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb, var(--accent) 12%, transparent);outline:none}.te-pop textarea{height:64px;resize:vertical}.te-action,.te-engine button{--btn-fill:var(--accent);background:var(--accent);border:1px solid var(--accent-deep);color:var(--accent-ink);border-radius:5px;padding:5px 8px;cursor:pointer;transition:filter .16s ease,transform .16s ease,opacity .16s ease}.te-action:hover:not(:disabled),.te-engine button:hover:not(:disabled),.te-comfy button:hover:not(:disabled),.te-output button:hover:not(:disabled){filter:brightness(1.06)}.te-action:active:not(:disabled),.te-engine button:active:not(:disabled),.te-comfy button:active:not(:disabled),.te-output button:active:not(:disabled){transform:translateY(1px)}.te-action:disabled,.te-engine button:disabled,.te-comfy button:disabled{opacity:.55;cursor:default}.te-result{color:var(--green);font-size: var(--fs-cap);padding:6px 8px;border-radius:5px;background:color-mix(in srgb, var(--green) 8%, transparent);border:1px solid color-mix(in srgb, var(--green) 25%, transparent)}.te-list{max-height:100px;overflow:auto;padding-left:18px;font:var(--fs-cap) var(--font-mono);color:var(--text-muted);background:var(--bg);border-radius:5px;padding-top:5px;padding-bottom:5px}.te-engine{margin-top:10px;padding-top:9px;border-top:1px solid var(--border);display:flex;align-items:center;gap:7px;color:var(--text-muted);font-size: var(--fs-cap)}.te-engine button{margin-left:auto}.te-engine span{width:7px;height:7px;border-radius:50%;background:var(--border)}.te-engine span.live{background:var(--green);box-shadow:0 0 7px var(--green);animation:te-pulse 1.8s ease-in-out infinite}
.te-comfy{margin-top:10px;padding-top:9px;border-top:1px solid var(--border);font-size: var(--fs-cap);color:var(--text-muted)}.te-comfy span{display:block;margin-top:4px;color:var(--green)}
.te-comfy button{--btn-fill:var(--accent);background:var(--accent);border:1px solid var(--accent-deep);color:var(--accent-ink);border-radius:4px;padding:4px 8px;cursor:pointer;margin:2px 4px 2px 0;font-size: var(--fs-cap)}.te-comfy button.te-cancel{--btn-fill:var(--danger);background:var(--danger);border-color:var(--danger);color:var(--danger-ink)}
.te-templates,.te-history{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0}.te-templates button,.te-history button{margin:0;padding:2px 7px;font-size: var(--fs-cap)}
.te-params{display:grid;grid-template-columns:1fr 1fr;gap:4px;margin:5px 0}.te-params label{font-size: var(--fs-cap);color:var(--text-muted)}.te-params input{margin:2px 0!important;padding:3px!important;font-size: var(--fs-cap)}.te-params button{grid-column:1/-1}
.te-outputs{margin-top:8px;display:grid;grid-template-columns:repeat(auto-fill,minmax(125px,1fr));gap:7px;max-height:240px;overflow:auto}.te-output{display:flex;flex-direction:column;align-items:stretch;gap:4px;padding:6px;background:var(--bg);border:1px solid var(--border);border-radius:6px;font-size: var(--fs-cap);transition:border-color .16s ease,box-shadow .16s ease}.te-output:hover{border-color:var(--border-strong);box-shadow:0 4px 12px rgba(35,52,84,.08)}.te-output img{width:100%;height:86px;object-fit:cover;border-radius:4px}.te-output audio,.te-output video{width:100%;max-height:86px}.te-output span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--font-mono)}.te-output button{--btn-fill:var(--accent);background:var(--accent);border:1px solid var(--accent-deep);color:var(--accent-ink);border-radius:5px;padding:3px 7px;cursor:pointer;transition:filter .16s ease,transform .16s ease}
.te-unreal{margin-top:8px;padding-top:8px;border-top:1px solid var(--border);font-size: var(--fs-cap);color:var(--text-muted);display:flex;gap:6px;align-items:center}.te-unreal small{margin-left:2px}.te-unreal button{margin-left:auto;--btn-fill:var(--accent);background:var(--accent);border:1px solid var(--accent-deep);color:var(--accent-ink);border-radius:5px;padding:4px 7px;cursor:pointer;transition:filter .16s ease,transform .16s ease}.te-unreal-list{max-height:110px;overflow:auto;margin-top:4px;padding:6px;background:var(--bg);border:1px solid var(--border);border-radius:5px;font:10px var(--font-mono);color:var(--text-muted)}
/* 回滚是破坏性操作 → 用琥珀色，和旁边的普通动作区分开（--btn-fill 顺带照顾玻璃材质）。 */
.te-action-warn{--btn-fill:var(--amber);background:var(--amber);border-color:var(--amber);color:var(--amber-ink)}
.te-list-warn li{color:var(--amber)}
@keyframes te-fade-in{from{opacity:0}to{opacity:1}}@keyframes te-pop-in{from{opacity:0;transform:translateY(-6px) scale(.985)}to{opacity:1;transform:translateY(0) scale(1)}}@keyframes te-pulse{0%,100%{box-shadow:0 0 0 0 color-mix(in srgb, var(--green) 35%, transparent)}50%{box-shadow:0 0 0 5px transparent}}
@media (max-width:640px){.te-pop{right:8px;top:8px;bottom:8px;width:auto;max-width:none;max-height:none;border-radius:10px}.te-label{display:none}.te-engine,.te-unreal{flex-wrap:wrap}.te-engine button,.te-unreal button{margin-left:auto}}
@media (prefers-reduced-motion:reduce){.te-trigger,.te-backdrop,.te-pop,.te-engine span.live,.te-output,.te-action,.te-engine button,.te-comfy button,.te-output button{animation:none!important;transition:none!important}}
</style>
