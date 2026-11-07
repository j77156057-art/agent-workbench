import { ref } from 'vue'

// 两个抽屉语义不同，但都从右侧展开：
// - memoryPanelOpen：某个 Agent 的上下文（本轮注入 / 守则 / 条目 / 合规）
// - agentTreeOpen：运行时任务编排（平台助手 → 子 Agent 的拓扑与审批）
// 共用一个互斥开关，避免两个抽屉叠在一起把主工作台挤没。
export const memoryPanelOpen = ref(false)
export const agentTreeOpen = ref(false)
// - agentWallOpen：Agent 卡片墙（一排卡片 → 对话 / 记忆 / 使用记录）
export const agentWallOpen = ref(false)

// 记忆面板当前看的是哪个 Agent 的上下文。点 Agent 树节点会把这里切成该节点，
// 但始终是同一个工作台、同一个项目、同一个会话，不新开窗口。
export const memoryAgentId = ref('main')
export const memoryAgentLabel = ref('平台助手')

export function openMemoryPanel() {
  memoryAgentId.value = 'main'
  memoryAgentLabel.value = '平台助手'
  agentTreeOpen.value = false
  agentWallOpen.value = false
  memoryPanelOpen.value = true
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
  memoryAgentId.value = agentId || 'main'
  memoryAgentLabel.value = label || (agentId === 'main' ? '平台助手' : agentId)
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
