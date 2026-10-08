// 工作台外壳状态：Godot 式工作区视图（概览/代码/素材）与
// 画布/运行面板（弹窗态 runtimeOpen 与常驻态 runtimeResident 互斥）。
import { ref, type Ref } from 'vue'
import type { EditorTab } from './workbench-types'

export type WorkspaceView = 'overview' | 'code' | 'assets' | 'cockpit'
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
  }
}
