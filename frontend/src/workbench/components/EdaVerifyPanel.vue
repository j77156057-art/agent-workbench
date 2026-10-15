<script setup lang="ts">
// EDA 多模态验收面板（M3 结构化真值 × M1/M5 视觉信号 → M6 合并门）。
// 信条（docs/multimodal-verification-engine-eda-kickoff.md §0）：
// - 脚本拿事实、视觉补感知：结构化报告解析 + 视觉回归比对，两路信号交叉否决。
// - 三态而非二态：MET / UNMET / UNCERTAIN，任一否决即否决；拿不准绝不静默放行。
// - 视觉结论 verified:false 起步：本面板只呈现"判 + 建议"，绝不执行回滚
//   （后端 executes_rollback 恒 false；真实回滚须经 regions.rollback_changeset 人工审批门）。
// 组件自管状态，不进工作台全局；入口收在顶栏「更多 → 工具与运行」。
import { computed, ref } from 'vue'
import { edaApi, type EdaFact, type VerifyResp, type VisualResult } from '../api'

// 复用 SceneRuntimePanel 的「可嵌入」惯用法：popup（固定弹层，行为不变）/ docked（内联常驻主区）。
// 同一组件实例仅由父组件的 mode prop 切形态，绝不重建，故已解析的 fact / 门结果等状态在
// 弹窗↔停靠之间得以保留。docked 主体 teleport 进主区 host 槽（默认 #wb-eda-slot）。
const props = withDefaults(defineProps<{ mode?: 'popup' | 'docked'; host?: string }>(),
  { mode: 'popup', host: '#wb-eda-slot' })
const emit = defineEmits<{ dock: []; undock: [] }>()
const docked = computed(() => props.mode === 'docked')

const open = ref(false)
const busy = ref(false)
const error = ref('')

// 弹窗 → 停靠：先收掉弹窗，再请父组件切 docked（父置 edaResident=true）。
function dockNow() { open.value = false; emit('dock') }
// 停靠 → 弹出为浮窗：请父组件解除常驻，并立即以弹窗重新呈现。
function toPopup() { emit('undock'); open.value = true }
// 停靠 → 收起：解除常驻且不重开（再要打开走顶栏「更多」菜单）。
function collapse() { emit('undock') }

// ---- 结构化真值（M3）----
const tool = ref('vivado')
const kind = ref('timing')
const reportText = ref('')
const fact = ref<EdaFact | null>(null)

// ---- 视觉信号（M1/M5）----
const VERDICTS = [
  'identical', 'pass', 'localized', 'regression',
  'needs_golden', 'no_frame', 'capture_error',
]
const verdict = ref('localized')
const vlmVerified = ref(false)

// ---- 合并门结果（M6）----
const result = ref<VerifyResp | null>(null)

const stateMeta: Record<string, { label: string; cls: string; hint: string }> = {
  met: { label: 'MET · 通过', cls: 'ev-met', hint: '结构化 + 视觉双绿，放行。' },
  unmet: { label: 'UNMET · 否决', cls: 'ev-unmet', hint: '有信号明确否决，拒绝放行（建议回滚，但需人工审批）。' },
  uncertain: { label: 'UNCERTAIN · 拿不准', cls: 'ev-unc', hint: '未核实/缺证据，绝不静默放行，升级人工/下一段核实。' },
}
const stateInfo = computed(() => stateMeta[result.value?.merged.state || ''] || null)

async function parseReport() {
  error.value = ''
  busy.value = true
  try {
    fact.value = await edaApi.parseReport(tool.value, kind.value, reportText.value)
  } catch (e) {
    error.value = (e as Error).message || '解析失败'
    fact.value = null
  } finally {
    busy.value = false
  }
}

function currentVisual(): VisualResult {
  return { verdict: verdict.value, passed: verdict.value === 'pass' || verdict.value === 'identical' }
}

async function runGate() {
  error.value = ''
  busy.value = true
  try {
    result.value = await edaApi.verify({
      structured: fact.value,
      visual: currentVisual(),
      vlm_verified: vlmVerified.value ? true : null,
    })
  } catch (e) {
    error.value = (e as Error).message || '合并门调用失败'
    result.value = null
  } finally {
    busy.value = false
  }
}

// ---- 三个预置用例：一眼演示双信号交叉否决 ----
type Preset = { name: string; tool: string; kind: string; report: string; verdict: string; vlm: boolean; blurb: string }
const PRESETS: Preset[] = [
  {
    name: '日志全绿 · 版图破图', tool: 'vivado', kind: 'timing', verdict: 'regression', vlm: false,
    blurb: '结构化 timing_met=True（时序守住），但把视觉 verdict 设为 regression（演示 macro 整片缺失）→ 一票否决 UNMET。真实回归由引擎产出 verdict，本面板只喂结论给门。',
    report: 'WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns)\n  0.412   0.000   0   4820   0.089   0.000\nsys_clk_i {0.000 3.333} 6.667 150.000',
  },
  {
    name: '均值达标 · 少一个 via', tool: 'vivado', kind: 'timing', verdict: 'localized', vlm: false,
    blurb: '演示：把视觉 verdict 设为 localized（模拟钝指标 SSIM/均值都达标、但区域块哈希抓到 1 个 via 没了）。localized 未核实 → UNCERTAIN，不静默放行。',
    report: 'WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns)\n  0.412   0.000   0   4820   0.089   0.000\nsys_clk_i {0.000 3.333} 6.667 150.000',
  },
  {
    name: '双绿 · 通过', tool: 'vivado', kind: 'timing', verdict: 'identical', vlm: false,
    blurb: '结构化绿 + 把视觉 verdict 设为 identical（模拟与 golden 同源）→ MET，accept。',
    report: 'WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns)\n  0.412   0.000   0   4820   0.089   0.000\nsys_clk_i {0.000 3.333} 6.667 150.000',
  },
]

async function loadPreset(p: Preset) {
  tool.value = p.tool; kind.value = p.kind; reportText.value = p.report
  verdict.value = p.verdict; vlmVerified.value = p.vlm
  error.value = ''
  busy.value = true
  try {
    fact.value = await edaApi.parseReport(p.tool, p.kind, p.report)
    result.value = await edaApi.verify({
      structured: fact.value, visual: currentVisual(), vlm_verified: p.vlm ? true : null,
    })
  } catch (e) {
    error.value = (e as Error).message || '预置用例执行失败'
  } finally {
    busy.value = false
  }
}

function show() { open.value = true }
defineExpose({ show })
</script>

<template>
  <div class="ev">
    <button class="ev-trigger" :class="result ? stateInfo?.cls : ''" title="EDA 多模态验收：结构化报告 × 视觉回归 → 三态合并门" @click="open = !open">
      <svg width="12" height="12" viewBox="0 0 12 12">
        <rect x="1.2" y="1.2" width="9.6" height="9.6" rx="1" fill="none" stroke="currentColor" stroke-width="0.9" />
        <rect x="2.8" y="2.8" width="2.4" height="2.4" fill="currentColor" opacity="0.55" />
        <rect x="6.8" y="2.8" width="2.4" height="2.4" fill="currentColor" opacity="0.55" />
        <rect x="2.8" y="6.8" width="2.4" height="2.4" fill="currentColor" opacity="0.55" />
        <path d="M6.8 8 H9.2 M8 6.8 V9.2" stroke="currentColor" stroke-width="0.9" />
      </svg>
      <span class="ev-label">EDA 验收</span>
    </button>

    <!-- 遮罩：仅弹窗态（docked 无遮罩），teleport 到 body -->
    <Teleport to="body">
      <div v-if="!docked && open" class="ev-backdrop wb-modal-backdrop" @click="open = false" />
    </Teleport>

    <!-- 面板主体：popup→body 固定弹层；docked→主区 host 槽内联填充。
         同一实例仅切 teleport 目标（to），不重建，故 fact / 门结果状态得以保留。 -->
    <Teleport defer :to="docked ? host : 'body'">
      <div v-if="docked || open" class="ev-pop" :class="docked ? 'ev-pop-inline' : 'wb-modal-shell'"
           :role="docked ? 'region' : 'dialog'" :aria-modal="docked ? undefined : 'true'" aria-label="EDA 多模态验收">
        <div class="ev-head wb-modal-head">
          <b>EDA 多模态验收</b>
          <span class="ev-faint">结构化真值 × 视觉信号 → 三态合并门</span>
          <span class="ev-spacer" />
          <button v-if="!docked" class="ev-link" title="停靠到主区常驻（复用工作台嵌入能力）" @click="dockNow">⤓ 停靠</button>
          <button v-else class="ev-link" title="弹出为浮窗" @click="toPopup">⤒ 浮窗</button>
          <button class="ev-link" @click="docked ? collapse() : (open = false)">{{ docked ? '收起' : '关闭' }}</button>
        </div>

        <p class="ev-note">本面板<b>只判不执行回滚</b>（后端 <code>executes_rollback:false</code>）；真实回滚须经 <code>regions.rollback_changeset</code> 的人工审批门。视觉结论 <b>verified:false</b> 起步。</p>

        <!-- 预置用例 -->
        <div class="ev-presets">
          <span class="ev-sub">快速演示：</span>
          <button v-for="p in PRESETS" :key="p.name" class="ev-chip" :disabled="busy" :title="p.blurb" @click="loadPreset(p)">{{ p.name }}</button>
        </div>

        <p v-if="error" class="ev-err">{{ error }}</p>

        <div class="ev-grid">
          <!-- 左：结构化 -->
          <section class="ev-col">
            <div class="ev-col-h">① 结构化真值（M3 报告解析）</div>
            <div class="ev-row">
              <label>工具
                <select v-model="tool">
                  <option value="vivado">vivado</option>
                  <option value="klayout">klayout</option>
                  <option value="verdi">verdi</option>
                </select>
              </label>
              <label>kind
                <select v-model="kind">
                  <option value="timing">timing</option>
                  <option value="utilization">utilization</option>
                  <option value="drc">drc</option>
                  <option value="simulation">simulation</option>
                </select>
              </label>
            </div>
            <textarea v-model="reportText" class="ev-text" rows="5" placeholder="粘贴工具 report 原文（如 Vivado report_timing_summary）…" />
            <button class="ev-btn" :disabled="busy" @click="parseReport">解析成结构化事实</button>

            <div v-if="fact" class="ev-fact wb-card">
              <div class="ev-fact-h">
                <span :class="fact.ok ? 'ev-tag-ok' : 'ev-tag-bad'">{{ fact.ok ? 'ok' : 'fail' }}</span>
                <b>{{ fact.tool }}/{{ fact.kind }}</b>
              </div>
              <div v-for="(v, k) in fact.metrics" :key="k" class="ev-metric">
                <span class="ev-mk">{{ k }}</span><span class="ev-mv">{{ v }}</span>
              </div>
              <div v-if="fact.missing?.length" class="ev-miss">缺失：{{ fact.missing.join(', ') }}</div>
              <div v-if="fact.error" class="ev-err">错误：{{ fact.error }}</div>
              <details v-if="fact.provenance?.length" class="ev-prov">
                <summary>provenance（{{ fact.provenance.length }} 条，回指报告原行）</summary>
                <div v-for="(p, i) in fact.provenance" :key="i" class="ev-prov-row"><b>{{ p.field }}</b> ← {{ p.excerpt }}</div>
              </details>
            </div>
          </section>

          <!-- 右：视觉 + 门 -->
          <section class="ev-col">
            <div class="ev-col-h">② 视觉信号（verdict 由引擎产出，此处手选演示）</div>
            <div class="ev-row">
              <label>verdict
                <select v-model="verdict">
                  <option v-for="v in VERDICTS" :key="v" :value="v">{{ v }}</option>
                </select>
              </label>
              <label class="ev-check"><input type="checkbox" v-model="vlmVerified" /> 已人工核实通过（verified:true）</label>
            </div>
            <p class="ev-faint ev-hint">regression=硬否决；localized/needs_golden/no_frame/…=未核实→UNCERTAIN；pass/identical=通过。视觉结论默认 verified:false；仅经<b>人工核实</b>（VLM 只解释、不决策）才置 verified:true，门才可能据此解除不确定——决定权在人工核实，不在 VLM。</p>

            <button class="ev-btn ev-btn-gate" :disabled="busy" @click="runGate">跑合并门 →</button>

            <div v-if="result && stateInfo" class="ev-gate wb-card" :class="stateInfo.cls">
              <div class="ev-state">
                <span class="ev-state-badge">{{ stateInfo.label }}</span>
                <span class="ev-action">建议动作：{{ result.decision.action }}</span>
              </div>
              <p class="ev-state-hint">{{ stateInfo.hint }}</p>
              <div class="ev-signals">
                <span>结构化 <b>{{ result.structured_state }}</b></span>
                <span>视觉 <b>{{ result.visual_state }}</b></span>
                <span v-if="result.merged.vetoed" class="ev-veto">已否决</span>
                <span v-if="result.merged.escalate_to_vlm" class="ev-esc">需升级 VLM</span>
              </div>
              <ul class="ev-reasons">
                <li v-for="(rs, st) in result.merged.reasons" :key="st">
                  <span class="ev-rs-tag">{{ st }}</span>
                  <span v-for="(r, i) in rs" :key="i" class="ev-rs">{{ r }}</span>
                </li>
              </ul>
              <div class="ev-exec">{{ result.note }}</div>
            </div>
          </section>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.ev { position: relative; }
.ev-trigger {
  display: inline-flex; align-items: center; gap: 5px;
  background: transparent; color: var(--text-muted);
  border: 1px solid var(--border-strong); border-radius: 5px;
  padding: 5px 9px; cursor: pointer; font-size: 11px; white-space: nowrap;
  transition: color .16s ease, border-color .16s ease, background .16s ease;
}
.ev-trigger:hover { color: var(--text); background: var(--bg-hover); }
.ev-trigger.ev-met { border-color: rgba(58, 150, 90, .5); color: var(--green, #3a965a); }
.ev-trigger.ev-unmet { border-color: rgba(224, 72, 79, .5); color: var(--danger); }
.ev-trigger.ev-unc { border-color: color-mix(in srgb, var(--amber) 45%, transparent); color: var(--amber); }

.ev-backdrop { position: fixed; inset: 0; z-index: 299; background: var(--scrim-soft); backdrop-filter: blur(1px); }
.ev-pop {
  position: fixed; right: 16px; top: 64px; width: 640px; max-width: calc(100vw - 32px);
  max-height: calc(100vh - 80px); overflow-y: auto; padding: 12px 14px;
  background: var(--bg-raised); border: 1px solid var(--border-strong); border-radius: 8px;
  z-index: 300; font-size: 12px; line-height: 1.6; box-shadow: 0 12px 32px rgba(35,52,84,.16);
}
/* 常驻（docked）：被 teleport 进主区 host 槽，去掉固定弹层的定位/尺寸/阴影，内联填充父容器。
   animation: none 必须——弹层入场动画的 to 帧会钉住 transform，静态 transform:none 压不住它。 */
.ev-pop-inline {
  position: static; left: auto; top: auto; transform: none; animation: none;
  width: 100%; height: 100%; max-width: none; max-height: none; flex: 1;
  border: 0; border-radius: 0; box-shadow: none; overflow-y: auto; z-index: auto;
}
.ev-head { display: flex; align-items: center; gap: 8px; }
.ev-head b { color: var(--text); font-size: 13px; }
.ev-spacer { flex: 1; }
.ev-link { background: none; border: none; color: var(--accent-text); cursor: pointer; font-size: 11px; }
.ev-note { margin: 8px 0 0; padding: 6px 8px; border-radius: 5px; background: rgba(47,111,237,.10); color: var(--accent-text); font-size: 11px; }
.ev-note code { background: rgba(0,0,0,.06); padding: 0 3px; border-radius: 3px; }
.ev-presets { margin: 10px 0 0; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.ev-sub { color: var(--text-muted); font-size: 11px; }
.ev-chip { background: transparent; border: 1px solid var(--border-strong); color: var(--text-muted); border-radius: 12px; font-size: 10px; padding: 2px 9px; cursor: pointer; }
.ev-chip:hover:not(:disabled) { color: var(--text); border-color: var(--accent); }
.ev-err { margin: 8px 0 0; color: var(--danger); font-size: 11px; }
.ev-faint { color: var(--text-muted); font-size: 11px; }

.ev-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }
.ev-col { border: 1px solid var(--border-strong); border-radius: 6px; padding: 8px 9px; }
.ev-col-h { font-size: 12px; color: var(--text); font-weight: 600; margin-bottom: 6px; }
.ev-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
.ev-row label { display: inline-flex; align-items: center; gap: 4px; color: var(--text-muted); font-size: 11px; }
.ev-row select { background: var(--bg); border: 1px solid var(--border-strong); border-radius: 4px; color: var(--text); padding: 2px 4px; font-size: 11px; }
.ev-check { cursor: pointer; }
.ev-text { width: 100%; box-sizing: border-box; background: var(--bg); border: 1px solid var(--border-strong); border-radius: 4px; color: var(--text); padding: 5px 6px; font-size: 11px; font-family: var(--mono, monospace); resize: vertical; }
.ev-btn { margin-top: 6px; background: transparent; border: 1px solid var(--accent); color: var(--accent-text); border-radius: 5px; font-size: 11px; padding: 4px 10px; cursor: pointer; }
.ev-btn:hover:not(:disabled) { background: var(--bg-hover); }
.ev-btn-gate { border-color: var(--amber); color: var(--amber); }
.ev-hint { margin: 6px 0 0; }
button:disabled { opacity: .5; cursor: default; }

.ev-fact { margin-top: 8px; padding: 6px 8px; border: 1px solid var(--border-strong); border-radius: 5px; }
.ev-fact-h { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.ev-tag-ok { background: rgba(58,150,90,.14); color: var(--green, #3a965a); border-radius: 8px; padding: 0 6px; font-size: 10px; }
.ev-tag-bad { background: rgba(224,72,79,.14); color: var(--danger); border-radius: 8px; padding: 0 6px; font-size: 10px; }
.ev-metric { display: flex; justify-content: space-between; font-size: 11px; }
.ev-mk { color: var(--text-muted); }
.ev-mv { color: var(--text); font-family: var(--mono, monospace); }
.ev-miss { color: var(--amber); font-size: 11px; margin-top: 3px; }
.ev-prov { margin-top: 4px; font-size: 10px; color: var(--text-muted); }
.ev-prov summary { cursor: pointer; }
.ev-prov-row { margin: 2px 0 0 8px; word-break: break-all; }

.ev-gate { margin-top: 8px; padding: 8px 9px; border-radius: 6px; border: 1px solid var(--border-strong); }
.ev-gate.ev-met { border-color: rgba(58,150,90,.5); }
.ev-gate.ev-unmet { border-color: rgba(224,72,79,.5); }
.ev-gate.ev-unc { border-color: color-mix(in srgb, var(--amber) 50%, transparent); }
.ev-state { display: flex; align-items: center; gap: 8px; }
.ev-state-badge { font-weight: 700; font-size: 13px; }
.ev-met .ev-state-badge { color: var(--green, #3a965a); }
.ev-unmet .ev-state-badge { color: var(--danger); }
.ev-unc .ev-state-badge { color: var(--amber); }
.ev-action { color: var(--text-muted); font-size: 11px; }
.ev-state-hint { margin: 4px 0 6px; font-size: 11px; color: var(--text-muted); }
.ev-signals { display: flex; flex-wrap: wrap; gap: 8px; font-size: 11px; color: var(--text-muted); }
.ev-signals b { color: var(--text); }
.ev-veto { color: var(--danger); }
.ev-esc { color: var(--amber); }
.ev-reasons { list-style: none; margin: 6px 0 0; padding: 0; }
.ev-reasons li { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 3px; }
.ev-rs-tag { background: rgba(0,0,0,.06); border-radius: 8px; padding: 0 6px; font-size: 10px; color: var(--text-muted); }
.ev-rs { font-size: 11px; color: var(--text); }
.ev-exec { margin-top: 8px; padding-top: 6px; border-top: 1px dashed var(--border-strong); font-size: 10px; color: var(--text-muted); }

@media (max-width: 720px) { .ev-grid { grid-template-columns: 1fr; } .ev-pop { left: 8px; right: 8px; width: auto; } }
</style>
