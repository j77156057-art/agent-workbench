// 工作台外壳状态：Godot 式工作区视图（概览/代码/素材）与
// 画布/运行面板（弹窗态 runtimeOpen 与常驻态 runtimeResident 互斥）。
import { ref, type Ref } from 'vue'
import type { EditorTab } from './workbench-types'

// 工作区视图只有三个。
// 'cockpit'（开发舱）曾是这个联合的第四项，但 AutonomousCockpit 组件在 v2 重构 E 阶段
// 已删除、主区不再挂载它，标签却留在 WorkspaceTabs 里 —— 点进去主区高度塌成 0，整屏空白。
// 2026-10-19 把标签与该联合成员一并删掉。
// ⚠️ 这不等于取消开发舱：docs/cockpit-v2-redesign.md 记着新舱要重建（只保留「实时对话视频流 +
// 窗口嵌入」），届时按新形态接回，别把这里的删除读成「不做新舱」。
export type WorkspaceView = 'overview' | 'code' | 'assets'
export type RuntimePanelTab = 'play' | 'scene' | 'timeline'

interface RuntimeDeps {
  tabs: Ref<EditorTab[]>
}
let deps!: RuntimeDeps
export function initRuntimeDeps(d: RuntimeDeps) { deps = d }

const workspace = ref<WorkspaceView>('overview')

function setWorkspace(v: WorkspaceView) {
  // 没有打开的文件时，代码工作区无内容可看，忽略切换
  if (v === 'code' && !deps.tabs.value.length) return
  workspace.value = v
}

const runtimeOpen = ref(false)
const runtimeTab = ref<RuntimePanelTab>('play')
/** 常驻态：true = 面板停靠在主区（docked），false = 传统弹窗（popup）。与 runtimeOpen 互斥。 */
const runtimeResident = ref(false)

function openRuntime(t: RuntimePanelTab) {
  runtimeTab.value = t
  runtimeResident.value = false   // 弹窗与常驻互斥，避免弹层叠在常驻面板上
  runtimeOpen.value = true
}
/** 把运行面板常驻进主区（docked），并切到指定 tab。 */
function openRuntimeResident(t: RuntimePanelTab) {
  runtimeTab.value = t
  runtimeOpen.value = false
  runtimeResident.value = true
}
function closeRuntimeResident() {
  runtimeResident.value = false
}
function closeRuntime() {
  runtimeOpen.value = false
  runtimeResident.value = false
}

// ---------------------------------------------------------------- 分析浮层开关
// 符号地图 / 关系图 / Unity 图 / 流程：纯布尔弹层态，无外部依赖。
const symbolMapOpen = ref(false)
function openSymbolMap() { symbolMapOpen.value = true }
function closeSymbolMap() { symbolMapOpen.value = false }

const relationGraphOpen = ref(false)
function openRelationGraph() { relationGraphOpen.value = true }
function closeRelationGraph() { relationGraphOpen.value = false }

const unityGraphOpen = ref(false)
function openUnityGraph() { unityGraphOpen.value = true }
function closeUnityGraph() { unityGraphOpen.value = false }

const flowOpen = ref(false)
function openFlow() { flowOpen.value = true }
function closeFlow() { flowOpen.value = false }

// P4：改动总览（只读）——跨文件列出所有未提交改动，是文件树状态点的汇总视图。
const changesOpen = ref(false)
function openChanges() { changesOpen.value = true }
function closeChanges() { changesOpen.value = false }

// 后台任务抽屉：AI 用 run_command(background: true) 起的长命令，从这里持续看进度与日志。
const jobsOpen = ref(false)
function openJobs() { jobsOpen.value = true }
function closeJobs() { jobsOpen.value = false }

export function useRuntime() {
  return {
    workspace, setWorkspace,
    runtimeOpen, runtimeTab, runtimeResident,
    openRuntime, openRuntimeResident, closeRuntimeResident, closeRuntime,
    symbolMapOpen, openSymbolMap, closeSymbolMap,
    relationGraphOpen, openRelationGraph, closeRelationGraph,
    unityGraphOpen, openUnityGraph, closeUnityGraph,
    flowOpen, openFlow, closeFlow,
    changesOpen, openChanges, closeChanges,
    jobsOpen, openJobs, closeJobs,
  }
}
