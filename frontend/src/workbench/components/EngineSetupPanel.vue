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
.esp-skill {
  margin: 0; max-height: 220px; overflow: auto;
  padding: 8px 10px; font-size: var(--fs-cap); line-height: 1.55;
  font-family: var(--font-mono); color: var(--text-muted);
  background: var(--bg); border: 1px solid var(--border); border-radius: var(--r-sm);
  white-space: pre-wrap; word-break: break-word;
}
</style>
