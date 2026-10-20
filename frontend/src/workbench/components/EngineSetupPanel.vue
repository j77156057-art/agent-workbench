<script setup lang="ts">
/**
 * 引擎与项目（设置页「运行与引擎」分区）。
 *
 * 这一块补的是引擎线上**最要命的一个缺口**：后端早就有「探测项目是哪个引擎 →
 * 选引擎 → 探测可执行文件 → 写入项目配置」这条链（`/api/engine/scan` ·
 * `/api/engine/config` · `/api/engine/prepare`），前端却一个调用点都没有 ——
 * 结果引擎永远是**硬编码 godot**（`SceneRuntimePanel.vue:307`），
 * Unity / Unreal 只有一张 GUID 图，没有任何启动或索引入口。
 *
 * 定位：这是**配置类**能力（和连接器同性质），所以放在设置页而不是顶栏浮层；
 * 而"启动/嵌入/停止/热重载"是运行类能力，仍归运行槽（SceneRuntimePanel），
 * 但它启动的引擎改从本面板配置的结果读，不再写死。
 */
import { computed, onMounted, ref } from 'vue'
import { engineApi, type EngineCatalogEntry } from '../api'
import { noProjectHint } from '../projectHint'
import { askConfirm } from '../composables/workbench'

const catalog = ref<EngineCatalogEntry[]>([])
const detected = ref<{ engine: string; path: string }[]>([])
const engine = ref('')
const executable = ref('')
const saving = ref(false)
const preparing = ref(false)
const scanning = ref(false)
const error = ref('')
const okMsg = ref('')
const skill = ref('')
const skillOpen = ref(false)

const ENGINE_LABELS: Record<string, string> = { godot: 'Godot', unity: 'Unity', unreal: 'Unreal' }
const label = (id: string) => ENGINE_LABELS[id] || id

const currentName = computed(() => label(engine.value) || '未选择')
const detectedEngines = computed(() => [...new Set(detected.value.map((p) => p.engine))])

function fillExecutable(id: string) {
  const item = catalog.value.find((x) => x.id === id)
  executable.value = item ? item.executable : ''
}

async function load() {
  error.value = ''
  okMsg.value = ''
  try {
    const [cat, cfg] = await Promise.all([
      engineApi.catalog().catch(() => null),
      engineApi.config().catch(() => null),
    ])
    if (cat?.ok && Array.isArray(cat.engines)) catalog.value = cat.engines
    if (cfg?.ok && cfg.engine) {
      engine.value = cfg.engine
      executable.value = cfg.executable || ''
    } else if (catalog.value.length) {
      // 还没有配置：默认选第一个（Godot），但**不假装**它是探测结果。
      engine.value = catalog.value[0].id
      fillExecutable(engine.value)
    }
  } catch (e) {
    error.value = noProjectHint(`读取引擎配置失败：${(e as Error).message}`)
  }
  await scan()
}

async function scan() {
  scanning.value = true
  try {
    const r = await engineApi.scan().catch(() => null)
    detected.value = r?.ok && Array.isArray(r.projects) ? r.projects : []
  } catch {
    detected.value = []
  } finally {
    scanning.value = false
  }
}

async function save() {
  if (!engine.value) return
  saving.value = true
  error.value = ''
  okMsg.value = ''
  try {
    const r = await engineApi.config(engine.value, executable.value)
    if (!r.ok) { error.value = r.error || '保存失败'; return }
    okMsg.value = `已保存：${currentName.value}（${r.executable}）`
  } catch (e) {
    error.value = noProjectHint(`保存失败：${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

async function prepare() {
  if (!engine.value) return
  preparing.value = true
  error.value = ''
  okMsg.value = ''
  try {
    const r = await engineApi.prepare(engine.value, executable.value)
    if (!r.ok || r.ready === false) {
      // 下载链接在失败体里（`download`），缺了它用户只知道"没找到"却不知道去哪装。
      error.value = r.error || '未找到引擎可执行文件。'
      if (r.download) error.value += ` 请先安装：${r.download}`
      return
    }
    okMsg.value = `${currentName.value} 已就绪，项目配置已写入。`
    await scan()
  } catch (e) {
    error.value = noProjectHint(`准备失败：${(e as Error).message}`)
  } finally {
    preparing.value = false
  }
}

async function toggleSkill() {
  skillOpen.value = !skillOpen.value
  if (!skillOpen.value || skill.value) return
  try {
    const r = await engineApi.skill(engine.value)
    skill.value = r.ok ? (r.skill || '（该引擎暂无适配说明）') : '（读取失败）'
  } catch {
    skill.value = '（读取失败）'
  }
}

onMounted(load)

// ---- 引擎工具：后端早就有的这批能力，此前前端一个调用点都没有 ----
const toolBusy = ref('')
const toolMsg = ref('')
const toolDetail = ref<Record<string, unknown> | null>(null)
const checkPath = ref('')
const diagText = ref('')
const playtestCmd = ref('')
const perfCmd = ref('')

async function runTool(name: string, fn: () => Promise<Record<string, unknown>>) {
  toolBusy.value = name
  toolMsg.value = ''
  toolDetail.value = null
  try {
    const r = await fn()
    toolDetail.value = r
    toolMsg.value = r?.ok === false ? String(r.error || '失败') : `${name}完成`
  } catch (e) {
    toolMsg.value = noProjectHint(`${name}失败：${(e as Error).message}`)
  } finally {
    toolBusy.value = ''
  }
}

/** 资产索引只支持 Unity / Unreal（后端只对这两家建索引），Godot 走的是场景画布那条路。 */
async function inspectAssets() {
  if (engine.value !== 'unity' && engine.value !== 'unreal') {
    toolBusy.value = ''
    toolDetail.value = null
    toolMsg.value = `资产索引只支持 Unity / Unreal（当前是 ${currentName.value}）——Godot 的场景结构走工作台的场景画布。`
    return
  }
  await runTool('索引资产', () => engineApi.inspect(engine.value))
}

async function installUnrealBridge() {
  const ok = await askConfirm({
    title: '安装 Unreal 桥接脚本？',
    message: '会在项目里写入 Unreal Editor Bridge 的 Python 脚本（需在编辑器启用 Python Script Plugin）。',
    detail: '只写这一个文件，不改其它内容。',
    confirmText: '安装',
  })
  if (!ok) return
  await runTool('安装 Unreal 桥', () => engineApi.installUnrealBridge())
}

const toolRows = computed(() => {
  const d = toolDetail.value
  if (!d) return []
  const out: { label: string; value: string }[] = []
  const push = (label: string, value: unknown) => {
    if (value === undefined || value === null) return
    const text = Array.isArray(value) ? `${value.length} 项` : String(value)
    out.push({ label, value: text })
  }
  push('清单', d.manifests)
  push('场景', d.scenes)
  push('预制体', d.prefabs)
  push('资产', d.assets)
  push('符号', d.symbols)
  push('蓝图', d.blueprints)
  push('关卡', d.levels)
  const diags = d.diagnostics
  if (Array.isArray(diags) && diags.length) {
    for (const item of diags.slice(0, 20)) {
      const it = item as { path?: string; line?: number; severity?: string; kind?: string; message?: string }
      out.push({
        label: it.severity || it.kind || '诊断',
        value: `${it.path || ''}${it.line ? ':' + it.line : ''} ${it.message || ''}`.trim(),
      })
    }
  }
  if (d.output) out.push({ label: '输出', value: String(d.output).slice(0, 400) })
  if (d.metric) out.push({ label: '口径', value: String(d.metric) })
  if (d.duration !== undefined && d.duration !== null) out.push({ label: '耗时', value: `${d.duration}s` })
  // path 在「脚本校验」里是**被检查的文件**，在「装探针/装桥」里才是**写入的文件**，
  // 所以这里只能叫「路径」——叫「写入」会把"检查了哪个文件"说成"写了哪个文件"。
  if (d.path) out.push({ label: '路径', value: String(d.path) })
  if (d.note) out.push({ label: '说明', value: String(d.note) })
  return out
})
</script>

<template>
  <div class="esp">
    <div class="esp-row esp-row--detect">
      <span class="esp-label">项目探测</span>
      <span v-if="scanning" class="esp-dim">扫描中…</span>
      <span v-else-if="!detected.length" class="esp-dim">未在项目里找到引擎工程文件</span>
      <span v-else class="esp-tags">
        <span v-for="p in detected" :key="p.path" class="esp-tag">
          {{ label(p.engine) }}<span class="esp-tag-path">{{ p.path }}</span>
        </span>
      </span>
      <button class="esp-btn" :disabled="scanning" @click="scan">重新扫描</button>
    </div>

    <div class="esp-row">
      <span class="esp-label">引擎</span>
      <span class="esp-opts" role="radiogroup" aria-label="游戏引擎">
        <label v-for="e in catalog" :key="e.id" class="esp-opt" :class="{ 'esp-opt--on': engine === e.id }">
          <input
            type="radio" name="esp-engine" :value="e.id" :checked="engine === e.id"
            @change="engine = e.id; fillExecutable(e.id); skill = ''"
          >
          <span>{{ e.name }}</span>
          <span v-if="detectedEngines.includes(e.id)" class="esp-opt-hit">本项目</span>
        </label>
      </span>
    </div>

    <div class="esp-row">
      <span class="esp-label">可执行文件</span>
      <input v-model="executable" class="esp-input" placeholder="godot / Unity.exe / UnrealEditor.exe" spellcheck="false">
      <button class="esp-btn" :disabled="saving || !engine" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
      <button class="esp-btn" :disabled="preparing || !engine" @click="prepare">{{ preparing ? '准备中…' : '准备引擎' }}</button>
      <button class="esp-btn esp-btn--ghost" @click="toggleSkill">适配说明</button>
    </div>

    <p v-if="error" class="esp-msg esp-msg--bad">{{ error }}</p>
    <p v-else-if="okMsg" class="esp-msg esp-msg--ok">{{ okMsg }}</p>
    <pre v-if="skillOpen" class="esp-skill">{{ skill || '读取中…' }}</pre>

    <!-- 引擎工具：后端早就有的这批能力，此前前端一个调用点都没有 -->
    <div class="esp-tools">
      <div class="esp-tools-head">
        <span>引擎工具</span>
        <span class="esp-dim">索引资产、脚本校验、诊断解析、探针与桥接、玩测</span>
      </div>
      <div class="esp-tools-row">
        <button class="esp-btn" :disabled="toolBusy !== ''" @click="inspectAssets">
          {{ toolBusy === '索引资产' ? '索引中…' : '索引资产' }}
        </button>
        <input v-model="checkPath" class="esp-input esp-grow" placeholder="要校验的 .gd 路径（相对项目根）" spellcheck="false">
        <button class="esp-btn" :disabled="toolBusy !== '' || !checkPath.trim()" @click="runTool('脚本校验', () => engineApi.check(checkPath.trim(), executable))">
          {{ toolBusy === '脚本校验' ? '校验中…' : '脚本校验' }}
        </button>
      </div>
      <div class="esp-tools-row">
        <textarea v-model="diagText" class="esp-input esp-grow esp-textarea" rows="2" placeholder="Unreal/MSVC 编译输出（可粘贴多行），解析成诊断" spellcheck="false" />
        <button class="esp-btn" :disabled="toolBusy !== '' || !diagText.trim()" @click="runTool('诊断解析', () => engineApi.diagnostics(diagText, 'unreal'))">
          {{ toolBusy === '诊断解析' ? '解析中…' : '诊断解析' }}
        </button>
      </div>
      <div class="esp-tools-row">
        <button class="esp-btn" :disabled="toolBusy !== ''" @click="runTool('装运行时探针', () => engineApi.installRuntimeProbe())">
          {{ toolBusy === '装运行时探针' ? '安装中…' : '装运行时探针' }}
        </button>
        <button class="esp-btn" :disabled="toolBusy !== ''" @click="installUnrealBridge">安装 Unreal 桥</button>
        <input v-model="playtestCmd" class="esp-input esp-grow" placeholder="玩测命令（留空=自动探测测试框架）" spellcheck="false">
        <button class="esp-btn" :disabled="toolBusy !== ''" @click="runTool('玩测', () => engineApi.playtest(playtestCmd.trim()))">
          {{ toolBusy === '玩测' ? '运行中…' : '跑玩测' }}
        </button>
      </div>
      <div class="esp-tools-row">
        <input v-model="perfCmd" class="esp-input esp-grow" placeholder="要剖析的 .py 路径（相对项目根，如 tools/x.py）" spellcheck="false">
        <button class="esp-btn" :disabled="toolBusy !== '' || !perfCmd.trim()" @click="runTool('性能剖析', () => engineApi.performance(perfCmd.trim()))">
          {{ toolBusy === '性能剖析' ? '剖析中…' : '性能剖析' }}
        </button>
      </div>
      <p v-if="toolMsg" class="esp-msg" :class="{ 'esp-msg--bad': toolDetail?.ok === false }">{{ toolMsg }}</p>
      <dl v-if="toolRows.length" class="esp-toolrows">
        <div v-for="(row, i) in toolRows" :key="i"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></div>
      </dl>
    </div>
  </div>
</template>

<style scoped>
.esp { display: flex; flex-direction: column; gap: 8px; }
.esp-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.esp-label { flex: 0 0 68px; font-size: var(--fs-cap); color: var(--text-faint); }
.esp-dim { font-size: var(--fs-sm); color: var(--text-faint); }
.esp-tags { display: flex; gap: 6px; flex-wrap: wrap; }
.esp-tag {
  display: inline-flex; align-items: baseline; gap: 6px;
  font-size: var(--fs-cap); color: var(--text);
  padding: 1px 7px; border: 1px solid var(--border); border-radius: var(--r-sm);
}
.esp-tag-path { font-family: var(--font-mono); color: var(--text-faint); }
.esp-opts { display: flex; gap: 10px; flex-wrap: wrap; }
.esp-opt { display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-sm); color: var(--text-muted); cursor: pointer; }
.esp-opt--on { color: var(--accent-text); }
.esp-opt input { accent-color: var(--accent); }
.esp-opt-hit {
  font-size: var(--fs-cap); color: var(--green);
  padding: 0 5px; border-radius: var(--r-sm);
  background: color-mix(in srgb, var(--green) 12%, transparent);
}
.esp-input {
  flex: 1 1 220px; min-width: 0; height: 26px; padding: 0 8px;
  background: var(--bg); color: var(--text);
  border: 1px solid var(--border-graphic); border-radius: var(--r-sm);
  font-family: var(--font-mono); font-size: var(--fs-sm); outline: none;
}
.esp-input:focus { border-color: var(--accent); }
.esp-textarea { resize: vertical; font-family: var(--font-mono); line-height: 1.5; padding: 4px 7px; }
.esp-btn {
  height: 26px; padding: 0 11px; font-size: var(--fs-sm);
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-raised); color: var(--text); cursor: pointer;
}
.esp-btn:hover:not(:disabled) { border-color: var(--border-strong); color: var(--accent); }
.esp-btn:disabled { opacity: .5; cursor: default; }
.esp-btn--ghost { background: transparent; color: var(--text-muted); }
.esp-msg { margin: 0; font-size: var(--fs-sm); }
.esp-msg--bad { color: var(--danger); }
.esp-msg--ok { color: var(--green); }
.esp-dim { font-size: var(--fs-cap); color: var(--text-faint); }
.esp-tools { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 6px; }
.esp-tools-head { display: flex; align-items: baseline; gap: 8px; font-size: var(--fs-sm); font-weight: 600; color: var(--text); }
.esp-tools-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.esp-toolrows { margin: 0; display: flex; flex-direction: column; gap: 2px; }
.esp-toolrows div { display: flex; gap: 8px; align-items: baseline; min-width: 0; }
.esp-toolrows dt { flex: 0 0 68px; font-size: var(--fs-cap); color: var(--text-faint); }
.esp-toolrows dd {
  margin: 0; font-size: var(--fs-cap); color: var(--text-muted); font-family: var(--font-mono);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.esp-skill {
  margin: 0; max-height: 220px; overflow: auto;
  padding: 8px 10px; font-size: var(--fs-cap); line-height: 1.55;
  font-family: var(--font-mono); color: var(--text-muted);
  background: var(--bg); border: 1px solid var(--border); border-radius: var(--r-sm);
  white-space: pre-wrap; word-break: break-word;
}
</style>
