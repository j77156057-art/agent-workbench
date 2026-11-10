import { ref } from 'vue'

// 右侧只有一个记忆侧栏，内部用横向小标签切换两个面板：
// - wall：Agent 卡片墙（一排卡片 → 对话 / 记忆 / 使用记录）
// - memory：某个 Agent 的上下文（本轮注入 / 守则 / 条目 / 合规）
// 两个面板常驻挂载（v-show），切标签不重置各自的滚动位置与内部状态；
// 关闭侧栏才整体卸载。
export const rightSidebarOpen = ref(false)
export const rightSidebarTab = ref<'wall' | 'memory'>('wall')

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
  rightSidebarTab.value = 'wall'
  rightSidebarOpen.value = true
}

export function closeAgentWall() {
  closeRightSidebar()
}

export function openAgentContext(agentId: string, label: string) {
  if (!agentId) return
  memoryAgentId.value = agentId
  memoryAgentLabel.value = label || agentId
  rightSidebarTab.value = 'memory'
  rightSidebarOpen.value = true
}

export function closeMemoryPanel() {
  closeRightSidebar()
}

export function closeRightSidebar() {
  rightSidebarOpen.value = false
}
