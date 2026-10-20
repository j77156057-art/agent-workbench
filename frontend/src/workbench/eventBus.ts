// 跨层/跨入口全局事件总线（问答页与工作台共用）。
// 取代散落在各处的裸 window.dispatchEvent(new CustomEvent('docmind:xxx'))：
// 事件名与 detail 结构集中在 AppEventMap 声明，派发/监听都有编译期校验。
// 底层仍走 window CustomEvent——天然跨 MPA 入口、宿主页面也可感知，
// 但业务代码只允许经 appEvents 收发。
import type { PreviewFeedbackRequest } from './previewFeedback'

/** 聚焦对话台时可携带的意图：预置问题或强制按当前项目重建会话上下文 */
export interface FocusChatDetail {
  q?: string
  /** 仅在草稿为空时预填，不覆盖用户已输入内容 */
  qIfEmpty?: string
  /** 聚焦哪个对话台：普通工作台（default）或自主开发舱（cockpit） */
  target?: 'default' | 'cockpit'
  reload?: boolean
}

/** 工作流团队抽屉 → 卡片内成员定位 */
export interface WfFocusMemberDetail {
  workflowId: string
  taskId: string
}

export interface AppEventMap {
  /** WorkflowPreview 实时预览反馈 → ChatDock 代发一条带图消息 */
  'docmind:send-chat': PreviewFeedbackRequest
  /** 各入口（概览/分区图/预览/工具菜单）请求聚焦底部对话台 */
  'docmind:focus-chat': FocusChatDetail
  /** 自主开发舱「管理预设」→ 切回概览并打开对话台模型设置弹窗 */
  'docmind:open-model-settings': void
  /** api 层检测到当前项目 id 变化 → ChatDock 重建会话上下文 */
  'docmind:project-context-changed': void
  /** ChatDock 团队抽屉 → 指定 WorkflowCard 滚动定位成员任务 */
  'docmind:wf-focus-member': WfFocusMemberDetail
  /** 设置页外观 → 切换皮肤/风格/布局/材质后派发，用于顶部提示条告知结果 */
  'docmind:appearance-changed': { kind: 'skin' | 'style' | 'layout' | 'material'; label: string }
  /** 开发舱持续视觉：最近一帧及当前观察会话的变化记录。 */
  'docmind:live-vision-frame': {
    projectId: string
    image: Blob | null
    /** 帧采集时间（epoch ms），语音发言时段匹配依赖它。 */
    capturedAt?: number
    /** 用户圈选区域的裁剪帧；未圈选时为 null。 */
    focusImage?: Blob | null
    observation?: string
    timeline?: { at: string; observation: string }[]
  }
}

export type AppEventName = keyof AppEventMap
type AppEventHandler<K extends AppEventName> = (detail: AppEventMap[K]) => void

interface ListenerEntry {
  handler: AppEventHandler<AppEventName>
  listener: EventListener
}

// 同一 handler 的 on/off 必须复用包装后的 EventListener，用 name 分组登记
const registry = new Map<AppEventName, Set<ListenerEntry>>()

export const appEvents = {
  emit<K extends AppEventName>(name: K, detail?: AppEventMap[K]): void {
    window.dispatchEvent(new CustomEvent(name, { detail }))
  },

  on<K extends AppEventName>(name: K, handler: AppEventHandler<K>): () => void {
    const listener = (ev: Event) => {
      handler((ev as CustomEvent<AppEventMap[K]>).detail)
    }
    const entry: ListenerEntry = {
      handler: handler as AppEventHandler<AppEventName>,
      listener,
    }
    let set = registry.get(name)
    if (!set) {
      set = new Set()
      registry.set(name, set)
    }
    set.add(entry)
    window.addEventListener(name, listener)
    // 返回取消订阅函数：组合式 API 里直接在 onBeforeUnmount 调用即可
    return () => this.off(name, handler)
  },

  off<K extends AppEventName>(name: K, handler: AppEventHandler<K>): void {
    const set = registry.get(name)
    if (!set) return
    for (const entry of set) {
      if (entry.handler === (handler as AppEventHandler<AppEventName>)) {
        window.removeEventListener(name, entry.listener)
        set.delete(entry)
      }
    }
  },
}
