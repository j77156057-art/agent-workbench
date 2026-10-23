import { reactive } from 'vue'

// 对话「展示偏好」：这些都是纯展示层开关（推理轨迹显隐、思考默认是否展开、
// 流式中是否整轮摊开、答案下方文件引用最多几条），不需要落后端 /api/config
// （交叠热点 api.py 不动），存 localStorage 即可。
//
// 放在 ask/ 是历史原因，但它**不是问答页专用**：问答页（AskMessage）、工作台
// 设置弹窗（ModelSettingsDialog）、工作台对话台（ChatDock）读的是同一份。
// ⚠️ 2026-10-08 之前 ChatDock 完全没读它 —— 设置里勾了「显示推理轨迹」在开发台
// 不生效。新增偏好时必须同时确认两个入口都消费，否则又是一个假设置。
export type AskPrefs = {
  // ReAct 轨迹（分析/动作/观察/复核）是否跟着答案一起显示
  showTrace: boolean
  // 流式进行中是否自动摊开最新一条工具行（关=只显示摘要行，点开才看全文）
  autoExpandTrace: boolean
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
const THINKING_KEY = 'docmind.chatThinking'          // 旧布尔镜像（开/关）
const THINKING_LEVEL_KEY = 'docmind.chatThinkingLevel' // 新唯一真源（off/low/medium/high）
const WEB_KEY = 'docmind.chatWeb'

// 2026-10-08 删掉了原来的 ASK_PREFS_EVENT（'docmind:askprefs'）：偏好改成一份共享
// reactive 之后，唯一的监听者是本模块自己「事件 → 重读 localStorage」，而写路径
// saveAskPrefs 已经 Object.assign 过一遍 —— 自己派发给自己，纯空转。
// 真要跨标签页同步应该监听 window 的 storage 事件（skins.ts 的 onSkinChange 那样），
// 这个自定义事件从来没有覆盖过那个场景。别再往回加。

// 2026-10-14：删掉 reasonOpen（「深度思考在回答结束后保持展开」）——交错流里
// 思考节点全程外露、随帧生长，这个开关已无任何消费方，留着就是假设置。
export const ASK_PREFS_DEFAULT: AskPrefs = { showTrace: true, autoExpandTrace: true, maxRefs: 6, defaultThinking: false, defaultWeb: false }

// localStorage 在隐私模式/被策略禁用时会直接抛，读写都必须吞掉异常：
// 存不下顶多回退默认值，不能让问答页白屏。
export function loadAskPrefs(): AskPrefs {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return { ...ASK_PREFS_DEFAULT }
    const p = JSON.parse(raw) as Partial<AskPrefs>
    return {
      showTrace: typeof p.showTrace === 'boolean' ? p.showTrace : ASK_PREFS_DEFAULT.showTrace,
      autoExpandTrace: typeof p.autoExpandTrace === 'boolean' ? p.autoExpandTrace : ASK_PREFS_DEFAULT.autoExpandTrace,
      maxRefs: typeof p.maxRefs === 'number' && p.maxRefs >= 0 ? p.maxRefs : ASK_PREFS_DEFAULT.maxRefs,
      // 老用户的这两个开关可能早就在 localStorage 里（点过一次 chip 就写进去了），
      // 以存储里的真实值为准，别把人家已经开着的默认状态显示成关。
      // 思考：新等级键真源优先（非 off 即开），无等级键时回落旧布尔镜像。
      defaultThinking: readThinkingStored(),
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

// 思考默认（布尔视图）：新等级键真源优先 —— 非 'off' 即视为「开」（含 'native'）；
// 还没写过等级键的老用户回落到旧的 '1'/'0' 镜像。
function readThinkingStored(): boolean {
  try {
    const lv = window.localStorage.getItem(THINKING_LEVEL_KEY)
    if (lv != null) return lv !== 'off'
  } catch {
    /* 读不了就回落旧 key */
  }
  return readStored(THINKING_KEY)
}

export function saveAskPrefs(p: AskPrefs): void {
  // 先同步内存视图再写存储：存储可能因隐私模式/策略被拒，但本次会话内的生效
  // 不能依赖它成功（所以下面 catch 里不 return —— 界面已经跟着变了）。
  Object.assign(prefs, p)
  try {
    window.localStorage.setItem(KEY, JSON.stringify(p))
    // 必须镜像回 useModelConfig 那两个共享 key：读的时候走的是这两个 key（老用户点过 chip 就有值），
    // 只写 askPrefs 的话设置里勾了、芯片不翻、刷新后又变回关 —— 同一个状态两个数据源会打架。
    window.localStorage.setItem(THINKING_KEY, p.defaultThinking ? '1' : '0')
    // 等级键只在「开/关意图真的翻转」时才动，避免保存无关偏好把工作台选定的 low/high 顺带降级。
    const cur = window.localStorage.getItem(THINKING_LEVEL_KEY)
    const wantOn = p.defaultThinking
    if (cur == null) {
      window.localStorage.setItem(THINKING_LEVEL_KEY, wantOn ? 'medium' : 'off')
    } else if ((cur !== 'off') !== wantOn) {
      window.localStorage.setItem(THINKING_LEVEL_KEY, wantOn ? 'medium' : 'off')
    }
    window.localStorage.setItem(WEB_KEY, p.defaultWeb ? '1' : '0')
  } catch {
    /* 存不下不影响当前会话内的使用 */
  }
}

// 单一响应式视图：全仓只此一份内存状态。写路径只有 saveAskPrefs（落盘 + 同步这份），
// 读路径只有 import 这个对象 —— AskMessage / AskApp / ChatDock / useMessageRender /
// 两个设置面板都绑它。以前是「各自 ref(loadAskPrefs()) + 挂事件监听」的第二套机制，
// 同一状态两份副本，弹窗开着时另一边改过就读到旧的。
//
// ⚠️ 必须放在文件末尾：这里在模块求值期调用 loadAskPrefs()，而它读 ASK_PREFS_DEFAULT。
// 挪到上面去就是 TDZ 报错「Cannot access 'A' before initialization」，整站白屏
// （实测踩过：vite build 不报错、vue-tsc 不报错，只有真打开页面才炸）。
export const prefs: AskPrefs = reactive(loadAskPrefs())
