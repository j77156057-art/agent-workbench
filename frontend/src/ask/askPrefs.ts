// 问答页「展示偏好」：这些都是纯展示层开关（推理轨迹显隐、思考默认是否展开、
// 答案下方文件引用最多几条），不需要落后端 /api/config（交叠热点 api.py 不动），
// 存 localStorage 即可 —— 问答页与设置弹窗共用这一份，改完即时生效。
export type AskPrefs = {
  // ReAct 轨迹（分析/动作/观察/复核）是否跟着答案一起显示
  showTrace: boolean
  // 深度思考块在答案生成完毕后是否保持展开（关=自动折叠，避免长推理轨迹铺开）
  reasonOpen: boolean
  // 答案下方文件引用 chips 最多显示几条（0 = 不限制）
  maxRefs: number
  // 输入区「深度思考」chip 的默认状态
  defaultThinking: boolean
  // 输入区「联网搜索」chip 的默认状态
  defaultWeb: boolean
}

const KEY = 'docmind.askPrefs'
// 这两个开关本来就有存储（useModelConfig 写的 docmind.chatThinking / chatWeb，问答页与工作台对话台共享），
// 设置只是补一个入口 —— 不另造 key，避免同一个状态两个数据源打架。
const THINKING_KEY = 'docmind.chatThinking'
const WEB_KEY = 'docmind.chatWeb'

// 设置弹窗改完偏好后派发这个事件，让已渲染的旧消息立刻跟着变（同标签页不触发 storage 事件）
export const ASK_PREFS_EVENT = 'docmind:askprefs'

export const ASK_PREFS_DEFAULT: AskPrefs = { showTrace: true, reasonOpen: false, maxRefs: 6, defaultThinking: false, defaultWeb: false }

// localStorage 在隐私模式/被策略禁用时会直接抛，读写都必须吞掉异常：
// 存不下顶多回退默认值，不能让问答页白屏。
export function loadAskPrefs(): AskPrefs {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return { ...ASK_PREFS_DEFAULT }
    const p = JSON.parse(raw) as Partial<AskPrefs>
    return {
      showTrace: typeof p.showTrace === 'boolean' ? p.showTrace : ASK_PREFS_DEFAULT.showTrace,
      reasonOpen: typeof p.reasonOpen === 'boolean' ? p.reasonOpen : ASK_PREFS_DEFAULT.reasonOpen,
      maxRefs: typeof p.maxRefs === 'number' && p.maxRefs >= 0 ? p.maxRefs : ASK_PREFS_DEFAULT.maxRefs,
      // 老用户的这两个开关可能早就在 localStorage 里（点过一次 chip 就写进去了），
      // 以存储里的真实值为准，别把人家已经开着的默认状态显示成关。
      defaultThinking: readStored(THINKING_KEY),
      defaultWeb: readStored(WEB_KEY),
    }
  } catch {
    return { ...ASK_PREFS_DEFAULT }
  }
}

function readStored(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === '1'
  } catch {
    return false
  }
}

export function saveAskPrefs(p: AskPrefs): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(p))
    // 必须镜像回 useModelConfig 那两个共享 key：读的时候走的是这两个 key（老用户点过 chip 就有值），
    // 只写 askPrefs 的话设置里勾了、芯片不翻、刷新后又变回关 —— 同一个状态两个数据源会打架。
    window.localStorage.setItem(THINKING_KEY, p.defaultThinking ? '1' : '0')
    window.localStorage.setItem(WEB_KEY, p.defaultWeb ? '1' : '0')
  } catch {
    /* 存不下不影响当前会话内的使用 */
  }
}
