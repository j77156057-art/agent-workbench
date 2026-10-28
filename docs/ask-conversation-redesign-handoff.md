# 问答页真实对话态重塑 · 执行交接

状态：已按本文实施并通过验收（2026-10-28）。实施记录见文末第 10 节。编写日期：2026-10-28。

## 0. 直接交给接手模型的指令

> 在 `D:\WorkBuddy\rag-agent` 实施本文件的问答页改造，先完成D中的测试夹具与before截图，再按 A → B → C 串行实现，最后跑完D验收；不要重新设计方向。先确认当前工作树，保留已有未提交改动；只修改本文白名单内文件，不改后端，不动工作台，不提交或推送。核心交付是「有真实回答、有执行过程、有引用」的对话界面，不是欢迎页。每阶段先通过明确验收再继续；最终必须构建并在浏览器中检查生产页面，提供同一夹具的前后截图、行为断言和对比度结果。不要用通过单测代替视觉验收，不要用最终回答一次性回包代替流式状态测试。

## 1. 这次到底改什么

上一轮改动偏重侧栏和空白欢迎态，用户认为问答页基本没变。本轮保留这些改动，把重心移到每天真正使用的消息区域。

目标风格：成熟的阅读型 AI 助手。回答像排版清楚的文档，执行过程是辅助信息，文件位置可追溯，输入操作稳定。借鉴成熟产品的层级和交互，不复制品牌、不添加营销插画。

三个必须看得出的变化：

1. **完成的回答不再被大段思考与工具记录压在下面。** 一条紧凑的「执行过程」展开入口之后就能读到正文。
2. **助手回答有明确的阅读层级。** 身份行、正文标题、段落、代码、回答操作相互区分，不再都是一档小字。
3. **文中引用不再散成一堆路径按钮。** 收成独立的来源区域，展开后清楚显示文件、目录、行号，仍能打开只读源码。

这不是重写聊天系统，也不是新增聊天产品功能。禁止增加登录、路由框架、全局状态库、模型供应商、知识库接口、新主题、评分点赞、分享、分支对话等。

## 2. 执行基线与文件边界

### 必须先知道

- 规划时 HEAD 为 `607d90f`，但**实现基线是当前未提交工作树，不是 HEAD**。上一轮前端改动仍未提交，直接从 HEAD 重写会丢掉它们。
- 当前还有 `golden/` 下其他工作的未跟踪文件。不要删除、修改或顺手暂存。
- 只允许前端修改；发现后端问题时记录事实，不改 Python 文件。
- 行号是本计划编写时的位置；执行时同时按函数名、类名定位。
- 不新增依赖，不改 Vite 端口或代理，不更新现有视觉基线来掩盖失败。

### 允许修改

| 文件 | 明确职责 |
|---|---|
| [AskMessage.vue](../frontend/src/ask/components/AskMessage.vue) | 主战场：消息结构、过程展开、正文、引用、状态、复制 |
| [AskApp.vue](../frontend/src/ask/AskApp.vue) | 只改非空对话布局、输入区、回到底部、源码弹层焦点交接 |
| [FileViewer.vue](../frontend/src/ask/components/FileViewer.vue) | 来源点击后的只读查看、行号定位、键盘关闭与竞态 |
| [ask-a11y.css](../frontend/src/ask/ask-a11y.css) | 删除与组件新规则冲突的具体覆盖，保留焦点环、减少动效 |
| [product-ui.browser.mjs](../frontend/tests/product-ui.browser.mjs) | 既有页面级回归；只补测试，不删已有验收 |

允许新增四个小文件，除此以外不新建生产模块：

- `frontend/src/ask/messagePresentation.ts`：消息状态展示的纯函数，不保存状态、不发请求。
- `frontend/tests/messagePresentation.test.mjs`：上述纯函数的 Node 单测，沿用现有直接导入 TS 的运行方式。
- `frontend/tests/ask-message.fixture.html`：仅开发测试使用，挂载真实 AskMessage，不进入生产构建入口。
- `frontend/tests/ask-conversation.browser.mjs`：本轮消息态专项浏览器测试。

### 只读复用，不修改

- [chat-types.ts:136](../frontend/src/workbench/composables/chat-types.ts#L136)：真实 `ChatMsg` 字段，状态仅 `streaming / done / stopped / error`，没有伪造的 `success` 状态。
- [traceDigest.ts:170](../frontend/src/workbench/composables/traceDigest.ts#L170)：`traceSegments` 的分段、清洗和工具计数。
- [askPrefs.ts:11](../frontend/src/ask/askPrefs.ts#L11)：共享偏好 `showTrace / autoExpandTrace / maxRefs`。
- [markdown.ts:40](../frontend/src/workbench/markdown.ts#L40)：现有转义后的 Markdown 渲染器；104 行起 `extractFileRefs`。
- [shared/markdown.css](../frontend/src/shared/markdown.css)、[ask-typo.css:83](../frontend/src/ask/ask-typo.css#L83)、[skins.css](../frontend/src/workbench/skins.css)：共享样式与字体、颜色令牌。
- `useChatStream.ts`、`WorkflowCard.vue`、`ArtifactCards.vue`、所有 `workbench/api/` 文件、`ChatDock.vue`：本轮不改。

## 3. 页面结构定稿

非空对话态：

```text
保留现有侧栏                         保留现有顶栏
─────────────────────────────────────────────────
                              用户问题，浅底气泡

DocMind                              真实状态 / 耗时
▸ 执行过程 · 3 次工具调用                    [展开]

回答正文：标题 → 段落 → 列表 → 代码
正文始终展开，不放进过程容器，不套大卡片

[产物：有才显示，沿用原组件]
▸ 文中引用 · 6 处                           [展开]
[复制回答]   已停止 / 未完成等必要说明

[澄清、授权或工作流卡片：独立呈现，不能被折叠藏住]
─────────────────────────────────────────────────
                      [有新内容时：回到最新回答]
[附件预览]
[输入框                                      ]
[图片] [深度思考] [联网搜索] [增强提问] [停止/发送]
```

实际 DOM 顺序：身份行 → 过程摘要及可展开内容 → 工作流卡片（若有）→ 状态提示与回答正文 → 产物 → 澄清/授权选项（若有）→ 文中引用 → 复制操作 → 通知。

工作流和需要用户处理的卡片优先于普通阅读节奏：保留既有位置与回调，不移动进过程或引用的折叠容器；不为强行符合示意图而重建实例。

### 定量设计约束

| 项目 | 值 |
|---|---|
| 对话外栏最大宽度 | 沿用 840px，桌面水平内边距 32px，手机 16–20px |
| 助手结构 | 身份图标与名称在同一行；移除左侧常驻 32px 头像列，让正文获得整栏宽度 |
| 正文 | `var(--font-ui)`，16px，行高 1.8，正常字距 |
| 正文标题 | `.md-h1` 24px、`.md-h2` 20px、`.md-h3/.md-h4` 17px，字重 600 |
| 段落、列表 | 段落间距 12px；标题前 24px、后 10px；列表项间距 6px |
| 代码 | 行内 14px；代码块 13px / 1.7，长行在代码块内部横向滚动 |
| 辅助信息 | 12–13px，使用 UI 字体；只有路径、行号、计时用等宽字体 |
| 用户气泡 | 浅中性色，最大 `min(640px, 88%)`，内边距 12px 16px |
| 消息间距 | 32px；一条消息内部按 8 / 12 / 16px 分组 |
| 过程摘要 | 一行 40px 左右；手机允许两行，总高度不超过 64px |
| 过程明细 | 展开后最长 320px，内部可滚动；原始单步输出最长 240px |
| 引用 | 折叠摘要至少 40px；展开后的文件行至少 44px |
| 交互目标 | 桌面不低于 32px；手机主要按钮不低于 44px |
| 正文限制 | 不设置 `max-height`、行数截断、隐藏溢出；不能用缩字号解决窄屏 |

颜色只使用现有语义令牌。正文 `--text`，辅助 `--text-muted`，强调文字 `--accent-text`，强调填充 `--accent` 配 `--accent-ink`，错误 `--danger`，警告 `--amber`。

不把 `--accent` 当小字颜色；不把白字配给会换肤的 `--ink`；不新增手写渐变。圆角使用 `--r-*`，保留四套皮肤各自的造型，不强迫纸面皮肤也圆角化。

## 4. A 阶段：先重排消息主体

改 `AskMessage.vue`、`AskApp.vue` 的非空布局和必要的 `ask-a11y.css` 规则。

1. 把 219–223 行的常驻头像列改为助手身份行。用简洁书页 SVG + `DocMind`，不再使用硬编码白色五角星。
2. 给消息添加稳定的测试定位，例如 `data-message-id`；不要用数组顺序作为跨会话标识。
3. 正文使用本计划字阶。把覆盖写在 AskMessage 的 `.answer :deep(.md-*)` 作用域内，**不要改共享 Markdown 样式或全站字号**。
4. 继续调用 `mdToHtml`；不拼接未转义的模型 HTML，不新增 Markdown 插件。
5. 当前渲染器只支持段落、标题、列表、围栏代码和少量行内语法，不支持表格。本轮不扩展表格解析器，也不伪称支持了表格。
6. 样式落在原组件内，移除 `ask-a11y.css:17–23` 对用户气泡的高优先级覆盖，并把需要保留的样式并入组件；保留其全局焦点与减少动效规则。
7. 不修改欢迎页文案、卡片、侧栏、皮肤默认值；截图必须是至少两轮有内容的对话。

阶段验收：1440px 下正文计算字号 16px，长段落易读；390px 下正文至少 300px 宽，无横向裁切；工作台正文计算样式与本阶段前一致。

## 5. B 阶段：执行过程与状态

### 过程折叠规则

- 每条助手消息只新增一个本地 `processOpen` 状态，默认关闭。外层用按钮配 `aria-expanded / aria-controls`。
- 现有 `traceSegments(msg)` 是唯一分段源，**不要再从 `msg.reasoning` 拼一份过程，不改 trace/flow 数组**。
- 外层关闭时：只保留紧凑摘要。流式没有正文时仍显示当前处理阶段；已经收到正文时正文立即可见。
- 摘要工具数是所有可见 segment 的 `digest.toolTotal` 之和，不用 trace 长度。工具调用次数不是成功次数，不写“成功读取 N 个文件”。
- 展开后：按原 segment 顺序显示 narration 和工具行；段落分组保留，但删除原来的 `openSegs / segOpen / toggleSeg` 二次折叠，避免“整体→段落→单步”三层套娃。单步 `rowOpen/toggleRow` 留下。
- reasoning 在展开过程内只显示一次；不能既在 narration 渲染，又在详情复制一份。保留 `traceSegments` 已有源码转储清洗。
- `prefs.showTrace=false` 时不显示工具行和工具计数；已有自然语言思考仍能在过程内查看，不修改共享偏好语义。
- `prefs.autoExpandTrace` 只控制**已经打开的过程**内最新一条工具记录。保留 `mutedRows`：用户主动关闭的行，不得被下一次 token 更新重新打开。
- 用户展开/关闭后，流式结束不擅自翻转外层状态；换成另一条消息 ID 时重置本地展开状态。
- 观察条目的失败标记只认已有 `ok === false`；`ok` 缺失是“未提供状态”，不能因为正文含“失败”二字就判失败，也不能默认成功。
- `WorkflowCard` 的 key、seed、`wfGate/wfActivity` 转发和惰性导入全部保留。不能因为折叠过程卸载工作流卡片。

### 状态纯函数

新增 `messagePresentation.ts`，导出 `getMessagePresentation(msg, askAnswered = false)`；输入只读，不依赖 DOM、Vue、计时器或网络。返回 `{ label: string, tone: 'neutral' | 'accent' | 'warning' | 'danger' }`，不给业务字段赋值。无正文流式分支在纯函数里返回“正在处理”；组件显示时使用已有 `phaseLabel` 替代这个通用标签，计时也继续留在组件。

按以下优先级判定，不猜测任务是否验收通过：

| 条件 | 展示 |
|---|---|
| `status === error` | 回答失败；保留 `error/errorKind` 说明与已有部分正文 |
| `status === stopped` | 已停止；即使没有正文也要显示 |
| `status === streaming` | 有正文：正在回答；没有正文：沿用真实 `phaseLabel` |
| 非流式且 `ask` 存在、尚未选择 | permission：等待授权；其余：等待补充 |
| 非流式且已选择 ask 选项 | 已提交选择；不表示后续任务完成 |
| `degraded === true` | 未完成；已有正文继续展示，不能显示“完整完成” |
| `consistencyReason === verbatim_dump` | 未综合结论；保留说明 |
| `consistency === warn` | 需核对；保留覆盖率信息，但不要让解释占据正文主视觉 |
| 普通 `done` | 已回复；不是“已验证”“已执行成功” |

工作流真实状态由原 `WorkflowCard` 显示；消息 `done` 不能推出工作流已经结束。耗时只用已有 `startedAt/finishedAt`；缺失则不显示，禁止补假耗时、进度百分比。

阶段验收：同一条 3 次工具调用、长思考、已有最终正文的夹具，默认过程高度≤64px；一键展开能看到全部既有分段，顺序正确、文本不重复；停止/失败/降级都不被误标成成功。

## 6. C 阶段：引用、操作与输入区

### 文中引用

1. 继续用 `extractFileRefs(msg.text)`，保持 path+line 去重和当前结果顺序。**这些是回答文本里提取的位置，不是后端核验过的证据**，统一叫“文中引用”，不叫“已验证来源”。
2. 默认折叠，摘要显示总数。展开后每行展示“文件名 + 行号”“上级目录”；同名文件必须能靠目录区分，完整路径保留在 title 中。
3. 沿用 `prefs.maxRefs`：默认 6，0 代表不限制；“展开全部 N 处 / 收起”必须真实改变列表，不能静默截断。
4. 点文件仍调用 `emit('openFile', r.path, r.line)`；不新建取文件接口，不静默改当前项目，不在解析失败时虚构目录或行号。
5. 产物继续交给 `ArtifactCards`，不要把产物混作引用。

### 复制回答

- 只加一项“复制回答”，放在正文后的轻量操作行；没有文本或仍在生成时不显示。
- 复制 `msg.text` 原文，不复制 HTML、思考、工具回包或文件路径列表。
- `await navigator.clipboard.writeText(...)` 成功后才显示“已复制”；拒绝或 API 不可用时显示“复制失败，请选择文本复制”。反馈使用 `role=status`，不能吞错后假报成功。
- 反馈定时器在组件卸载或消息切换时清理。不加点赞、分享、自动重试按钮。

### 输入与滚动

- 只调整 `.ask-main:not(.is-empty)` 范围：输入 textarea 最小高度 56px、最大160px，整块输入区与正文居中对齐。
- 保留图片上传/粘贴/拖拽、原生思考禁用态、联网、增强提问、停止与发送。窄屏可以换行，不能 `display:none` 隐藏能力。
- 保留 Enter 发送、Shift+Enter 换行、`isComposing` 防中文候选确认误发送，以及运行中发送的中断重定向机制。
- 利用现有 `stick` 和 80px 底部阈值：离开底部且已有消息时显示“回到最新回答”；点击后设 `stick=true`，调用现有 `scrollToBottom`。
- 折叠/展开过程与引用时不强制滚到底部；用户在读旧消息时，普通聊天SSE的后续追加不能拉走视口。
- 范围说明：冻结的 `WorkflowCard.vue:115–120` 在工作流实时完成时有一次 `scrollIntoView`。本轮保留该既有行为；不把“普通聊天不抢滚动”夸成所有工作流也不抢滚动，不通过全局禁用滚动拦截它。
- 授权门 `gateBlocking`、孤儿工作流恢复条、会话 ID、恢复历史、请求发送管线均不改。

### 只读源码查看

`FileViewer.vue` 目前在 `loading=false` 之前找 `codeEl`，而 `<pre>` 在 `v-else` 下：加载期元素不存在，行号高亮虽可出现，滚动定位可能没执行。此处一起修正：

- 有效请求成功后先更新内容并结束加载，等待 `nextTick`，再定位高亮行。
- 监听 path 与 line；同路径换行号也应重新定位。用请求序号避免慢的旧请求覆盖新文件。
- 加 `role=dialog`、`aria-modal`、标题关联。打开后焦点进弹层，Tab留在弹层；Esc、关闭按钮、遮罩都关闭，关闭后焦点返回触发的引用按钮。通过 AskApp 保存触发元素，不依赖弹层关闭时已经销毁的节点。
- 保持只读，不添加写文件行为。目标行超过现有1500行加载上限时明确提示“目标行未加载”，不假装定位成功；本轮不扩展大文件编辑器。

## 7. D 阶段：低成本但有效的验收

### 先有真实组件夹具，再改样式

A阶段开工前先建测试夹具并保存 before 截图；A/B/C每阶段复用同一夹具，不变更测试内容来制造“改善”。

`ask-message.fixture.html` 只放在 `frontend/tests/`，通过 Vite 开发服务访问；直接导入 Vue、真实 AskMessage、现有字体和问答页样式/皮肤。用 `createApp/h/reactive` 挂载组件，不复制一套假消息HTML，不访问 Vue 私有实例字段。用测试专用对象暴露 `setCase / appendEvent / setText / finish / setPrefs`，每次操作后等待 Vue 完成渲染。

最少准备以下数据：

- `long-answer`：两轮问答、约800字中文、三级标题、列表、30行代码、8处文件引用（包含不同目录的同名文件）。
- `running`：没有正文 → think → action → observation → 下一段think → 正文逐段增加 → final；事件之间由测试调用推进，不用任意 sleep 赌时间。
- `reason-only`：有原生思考但无工具；`history`：无 flow、仅 trace 含 reasoning；验证已有回放分支。
- `failed/stopped/degraded/warn/verbatim`：包含有正文与空正文两种关键边界。
- `permission`：真实 `msg.ask` 字段；`workflow`：真实 WorkflowCard，不用占位div替代。

基础消息字段必须完整：`id/role/text/status/trace/reasoning/notices/plan`；其他字段按 `ChatMsg` 类型填写。

Workflow夹具拦截原组件请求并返回真实形状状态，参考 `workbench/api/agent.ts:195` 的 WorkflowState 和原工作流 composable；所有 approve/execute 等写请求在浏览器路由中计数并返回隔离响应，禁止真的审批或执行用户项目。

### 生产页面的分帧测试方案（固定做法，不用另起服务）

在专项浏览器脚本内，用 `page.addInitScript` 包装 `window.fetch`，仅拦截当前源的 `POST /api/chat`；返回真正的 `Response`，`Content-Type: text/event-stream`，body为可手动推进的 `ReadableStream`。其余GET继续走原fetch；其他写操作按测试用例精确拦截并计数，不能落到真实后端。

- 暴露测试专用 `window.__askTransport`：`calls`计数、`emit(callId, event)`、`finish(callId, text)`、`disconnect(callId)`。每次请求分配独立callId，至少支持连续两轮，不能把上一轮controller复用给下一轮。
- `emit` 用 `TextEncoder` 编码 `data: ${JSON.stringify(event)}\n\n` 后enqueue；测试必须等待DOM出现该阶段标记，再推进下一阶段。不要把所有事件一次性吐完。
- `finish` 依次发送 `{type:'final',text}`、`{type:'done'}`，再关闭流。`disconnect` 不发送done，专门验证断流错误。
- 监听请求的AbortSignal；用户停止或重定向时让对应stream以AbortError结束、移除监听，测试断言旧轮不再更新、新轮可完成。
- 第一轮运行流程固定为 reasoning → action → observation → reasoning → token → token → final → done。最终正文必须与最终final相等，状态不再是streaming/error，不能只断言页面出现了某句话。
- 这层经过生产 `postSse` 和 `useChatStream`，验证真实应用渲染与中断逻辑；它是fetch边界桩，不是HTTP传输或模型集成测试。报告中明确这一点。
- 工作流HTTP、源码读取、会话历史使用Playwright路由桩；不要用fetch桩覆盖所有API。

### 必跑行为断言

1. 普通回答默认过程关闭、正文可见；展开/收起不删正文，不重排事件顺序。
2. 流式期间每个中间阶段实际渲染；手动收起最新工具行后，下一次追加仍关闭。
3. 原生思考、无工具、旧历史都可展示；showTrace=false 和 autoExpandTrace=false 分别验证。
4. stopped空正文、error部分正文、degraded、warn、verbatim的标签符合表格；无错误成功态。
5. 8处引用默认折叠；展开后默认6处、全部8处、再次收回6处；maxRefs=0时显示全部。
6. 引用点击的 path/line 正确；源码行定位在加载完成后发生；A文件慢返回不能覆盖后打开的B文件。
7. 源码弹层键盘焦点闭环；关闭后焦点回到原引用。行号超出加载范围有明确说明。
8. 复制成功、失败都测，断言剪贴板收到的恰是 msg.text，不是 innerHTML 或推理记录。
9. 流式过程中用户向上滚动，追加正文不抢滚动；回到最新回答按钮确实恢复跟随。
10. 澄清/授权选项一经点击锁定，emit恰好一次；工作流审批卡不因过程折叠被卸载，gate/activity仍传给AskApp。
11. 中文输入法回车不发送，正常发送/停止/重定向、附件、主题切换、窄屏导航不退化；复跑既有 product-ui.browser.mjs。
12. 对比度检测以正常文本4.5:1、大字3:1为门槛，覆盖新正文链接、状态、引用、hover、展开明细和placeholder；保留禁用控件豁免。注入白字白底必须失败，去掉后恢复。

### 页面与截图

- `zinc / graphite / paper / seed` × `1440×900 / 1024×768 / 390×844`，检查“长回答完成态”和“过程展开态”。
- 默认皮肤另测1440×600矮屏，以及运行中、错误、来源弹层状态。
- 字体加载完成后截图；减少动效模式保证可比较。截图后必须读图，不只是检查文件存在。
- 断言内容区实际宽度与边界：不能只看 document.scrollWidth。祖先 overflow:hidden 会把坏布局藏起来；本项目上一轮就出现过“全页不溢出，但主区只有170px”的假通过。
- 第一屏应看到正常字级的正文开头；禁止把过程整块隐藏/把全部内容缩小后宣称减噪。
- 输出 `artifacts/shots/ask-conversation-before-*.png`、`ask-conversation-after-*.png` 与 `ask-conversation-report.json`。报告必须区分组件状态回放、HTTP接口桩和真实模型调用；本轮不要求花费真实模型额度。

### 现有测试的盲点

现有 `product-ui.browser.mjs:76–79` 给聊天接口一次性返回final，且缺少 `{type:'done'}`；冻结的 `workbench/api/sse.ts:174–175` 会把这种EOF判为断流，但旧用例只等正文出现，未检查成功终态。另外90行起主要截图是空白欢迎页。

本轮必须先修正这份测试桩：成功流补done并关闭，等待按钮/消息状态恢复，断言没有断流提示；另保留一个故意缺done的失败用例。原先19组通过不能证明长回答、过程展开和逐阶段流式呈现正确；也不能删除该脚本原有的工作台回归。

## 8. 命令与交付条件

以下命令都从仓库根目录运行：

```bash
git status --short
npm --prefix frontend run dev -- --port 5174
```

5173在规划会话中由另一项目占用，不能停掉对方服务。5174也需要先探测实际页面标题；被占用时只通过命令行选择空闲回环端口，不改仓库配置。开发请求仍沿用现有后端代理，默认后端8000，换后端端口沿用 DOCMIND_PORT。

```bash
npm --prefix frontend run typecheck
npm --prefix frontend run test:node
node frontend/node_modules/eslint/bin/eslint.js frontend/src/ask/AskApp.vue frontend/src/ask/components/AskMessage.vue frontend/src/ask/components/FileViewer.vue frontend/src/ask/messagePresentation.ts
git diff --check
```

新增浏览器脚本约定：组件夹具地址读 `DOCMIND_UI_FIXTURE_URL`，真实应用地址读 `DOCMIND_UI_TEST_URL`。开发夹具必须读实际启动端口；不把测试入口加入 Vite 生产 input。

```bash
DOCMIND_UI_FIXTURE_URL=http://127.0.0.1:5174/tests/ask-message.fixture.html DOCMIND_UI_TEST_URL=http://127.0.0.1:5174 node frontend/tests/ask-conversation.browser.mjs
```

最终先检查 `web/` 没有他人的手改，再使用既有构建命令更新产物；不要手改哈希JS/CSS，也不要创建新的清理脚本。

```bash
git status --short
npm --prefix frontend run build
DOCMIND_UI_TEST_URL=http://127.0.0.1:8000 node frontend/tests/product-ui.browser.mjs
DOCMIND_UI_FIXTURE_URL=http://127.0.0.1:5174/tests/ask-message.fixture.html DOCMIND_UI_TEST_URL=http://127.0.0.1:8000 node frontend/tests/ask-conversation.browser.mjs
```

第二条专项运行中：组件状态回放仍走开发夹具；页面级发送、引用、弹层和滚动断言必须跑在8000返回的生产产物上。不要把开发验证结果改个文件名写成生产结果。

完成判据：A/B/C全部完成；D的行为、状态和视觉证据齐全；没有本轮引入的类型/测试/控制台错误；生产页面实际显示新对话态；工作台不被全局CSS误改。仅改了欢迎页、没有长回答前后对照，直接判为未完成。

最终汇报只需：改动清单、前后截图、测试结果、已知未验证项。不要再次宣称“测试通过所以成熟”；是否达到目标由真实对话截图和明确交互共同证明。

## 9. 低成本模型的推荐分轮方式

不要让多个模型同时改 AskMessage，也不要让模型先花一轮重新出设计方案。使用同一工作树，分四轮串行执行；每轮都读本文件，按当前代码继续，不从HEAD恢复。

1. **第一轮指令**：先完成第7节的真实组件夹具和before截图，然后只实施A。提交阶段结果：消息结构、长回答截图、字号/宽度测量、类型检查；不碰B/C。
2. **第二轮指令**：在第一轮结果上只实施B。提交过程折叠与状态纯函数、单测、分阶段回放截图；证明三层折叠变为整体+单步两层，且顺序和偏好未丢。
3. **第三轮指令**：只实施C。提交引用列表、复制反馈、输入与普通SSE滚动、只读源码焦点与定位；按明确边界测试，不新增共享后端或工作流行为。
4. **第四轮指令**：完整执行D，补旧测试缺done的问题，使用分帧fetch桩验证生产页面；跑构建、主题/尺寸矩阵和工作台回归，修完本轮缺陷再交付。

以上“提交阶段结果”是向用户汇报，不是创建git提交。用户未明确要求时，不执行git commit或push。

## 10. 实施记录（2026-10-28）

已完成 A/B/C/D 与计划中的测试修正：

- 工作台表面：[style.css](../frontend/src/workbench/style.css) 编辑器底色从 `--accent-soft` 改为中性白，强调色只保留在选中/定位等小面积信号；文件树类型胶囊收成文字标签，未跟踪文件从实心绿点改为空心弱标记，整列不再被绿色铺满。默认 zinc 恢复原来的冷白 + `#0f766e` 青绿，避免新绿色与工作台既有状态色冲突。
- 工作台设置：[SettingsView.vue](../frontend/src/workbench/components/SettingsView.vue) 保持 1040px 宽、200px 导航、指标卡与卡片化供应商表；它现在使用恢复后的冷白/青绿令牌。
- 首屏构图：[AskApp.vue](../frontend/src/ask/AskApp.vue) 空对话态改为工作台式首页：左侧开场与快捷开始，右侧当前工作区与资料；桌面全宽输入区，窄屏收起右栏保证输入框在首屏。侧栏在首屏隐藏重复的项目/资料块。
- 模型设置：[ModelSettingsDialog.vue](../frontend/src/workbench/components/ModelSettingsDialog.vue) 改为 720px 宽、双列预设、非告警提示与可读 placeholder；配色随 zinc 恢复为冷白 + 青绿。
- 消息主体：[AskMessage.vue](../frontend/src/ask/components/AskMessage.vue) 去掉常驻头像列，正文 16px / 1.8，标题 24/20/17px；用户气泡改为浅中性底。
- 过程与状态：新增单层「执行过程」折叠；[messagePresentation.ts](../frontend/src/ask/messagePresentation.ts) 统一 8 种展示状态，失败 / 停止 / 降级 / 一致性提示不再冒充完成。
- 引用与操作：文中引用默认折叠，展开后 6 条上限并可查看全部；复制回答对成功和失败分别给出真实反馈；输入区加入「回到最新回答」。
- 源码查看：[FileViewer.vue](../frontend/src/ask/components/FileViewer.vue) 修复加载后行定位，补对话框角色、焦点闭环、Esc、目标行超限提示。
- 测试：新增 [ask-message.fixture.html](../frontend/tests/ask-message.fixture.html)、[ask-conversation.browser.mjs](../frontend/tests/ask-conversation.browser.mjs)、[messagePresentation.test.mjs](../frontend/tests/messagePresentation.test.mjs)；修正既有产品测试成功流缺少 `done` 的桩。

实际结果：

- 145 项前端单测通过；修改组件 ESLint、`vue-tsc` 通过。
- 对话验收 16 组交互（含 1440/1024/390 首屏工作台构图）、13 组主题/尺寸对比度检查通过；最低 4.71:1，无横向溢出、无页面异常、无后端写入。
- 生产页面另跑 19 组既有回归通过；生产聊天的分帧事件来自 fetch 边界桩，不代表真实模型连通性。
- 工作台未改行为；本轮涉及问答页首屏、消息、源码查看和对应测试。

已知未验证：真实后端模型流式延迟下的阅读节奏、真实工作流审批链路、真实大文件源码加载性能；这些需要连接真实服务单独复核。仓库旧的像素基线 `docs/screenshots/visual` 未重建：本轮是有意的主题改版，基线脚本不在本次验收范围内，需要单独重生成并人工确认。
