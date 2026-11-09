import { ref } from 'vue'

// 两个抽屉语义不同，但都从右侧展开：
// - memoryPanelOpen：某个 Agent 的上下文（本轮注入 / 守则 / 条目 / 合规）
// - agentTreeOpen：运行时任务编排（平台助手 → 子 Agent 的拓扑与审批）
// 共用一个互斥开关，避免两个抽屉叠在一起把主工作台挤没。
export const memoryPanelOpen = ref(false)
export const agentTreeOpen = ref(false)
// - agentWallOpen：Agent 卡片墙（一排卡片 → 对话 / 记忆 / 使用记录）
export const agentWallOpen = ref(false)

// 记忆面板当前看的是哪个 Agent 的上下文。平台助手（main）已挪到问答页，
// 工作台的入口只给子 Agent，不再把 main 当默认值。
export const memoryAgentId = ref('')
export const memoryAgentLabel = ref('')

/** 「记忆」入口的默认落点：Agent 卡片墙（只列子 Agent）；平台助手记忆在问答页。 */
export function openMemoryPanel() {
  openAgentWall()
}

/** 「记忆」入口的默认落点：先看 Agent 卡片墙，不直接假定是平台助手的记忆。 */
export function openAgentWall() {
  memoryPanelOpen.value = false
  agentTreeOpen.value = false
  agentWallOpen.value = true
}

export function closeAgentWall() {
  agentWallOpen.value = false
}

export function openAgentContext(agentId: string, label: string) {
  if (!agentId) return
  memoryAgentId.value = agentId
  memoryAgentLabel.value = label || agentId
  agentTreeOpen.value = false
  agentWallOpen.value = false
  memoryPanelOpen.value = true
}

export function closeMemoryPanel() {
  memoryPanelOpen.value = false
}

export function toggleAgentTree() {
  memoryPanelOpen.value = false
  agentWallOpen.value = false
  agentTreeOpen.value = !agentTreeOpen.value
}

export function closeAgentTree() {
  agentTreeOpen.value = false
}
