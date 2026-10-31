# Agent 工作台

> **面向工程代码的 Agent 运行时（Harness）** —— 把「能跑的 agent」补齐成**可观测、可编排、可回归**的工程系统。

[![CI](https://github.com/j77156057-art/agent-workbench/actions/workflows/harness.yml/badge.svg)](https://github.com/j77156057-art/agent-workbench/actions/workflows/harness.yml)
![Python](https://img.shields.io/badge/Python-%3E%3D3.11-3776AB?logo=python&logoColor=white)
![Node](https://img.shields.io/badge/Node-%3E%3D22-339933?logo=node.js&logoColor=white)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)
![Vue](https://img.shields.io/badge/UI-Vue%203.5-42b883?logo=vuedotjs&logoColor=white)
![Offline](https://img.shields.io/badge/offline-zero--API--key-8b949e)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**▶ 免安装直接跑：[Releases · DocMind-20261030-994a318-win64.zip](https://github.com/j77156057-art/agent-workbench/releases)** —— 解压双击 `DocMind.exe`。不需要 Python、不需要 API Key、不需要联网，默认本地 mock 模型就能跑通完整流程。

**▶ 60 秒演示** —— 在一个真实 Godot 工程上走完五步：提问 → 模型写出数值配置文件并给出可点开的产物卡 → 关系图展开类的继承与场景挂载 → 场景画布里改节点属性、写回 `.tscn` → 一键撤销还原。

<div align="center">
  <img src="docs/screenshots/demo-poster.png" alt="60 秒演示：提问、产物卡、关系图、场景画布与撤销还原" width="760" />
</div>

<sub>演示里的每一步都是真操作：写入的文件落在工程里，关系图来自真实的继承与挂载解析，场景属性真的写回 `.tscn`，撤销真的把它还原。
[看 MP4（1440×900，62 秒）](https://raw.githubusercontent.com/j77156057-art/agent-workbench/main/docs/screenshots/demo.mp4) · [仓库内文件](docs/screenshots/demo.mp4)</sub>

<div align="center">
  <img src="docs/screenshots/trace-ledger.png" alt="Trace 账本：逐回合的 token / 延迟 / 工具序列 / 结局，只记元数据" width="49%" />
  <img src="docs/screenshots/scene-canvas.png" alt="场景画布：Godot .tscn 的可视化与可编辑节点图" width="49%" />
</div>

<sub>左：Trace 账本 —— 逐回合 token / 延迟 / 工具序列 / 结局，**只记元数据，不落 prompt 与回答正文**。
右：场景画布 —— Godot `.tscn` 的可视化与可编辑节点图，父子边与实例化边分色，改动逐字节可撤销。</sub>

---

## 它是什么

「Agent 工作台」有两个身份，合起来才完整：

| | |
|---|---|
| **Harness（主体）** | 一个离线可跑的 Agent 运行时。成体系的工具注册表、HTTP 端点与后端测试之外，还有逐轮 trace 账本、能力租约、人工审批台账、区域治理、DAG 多代理编排、黄金题回归门 —— **可观测、可编排、可回归**。 |
| **工作台（落地）** | 面向游戏 / Mod 工程的本地工作台：代码问答、受控改写、场景画布、运行时时间线、引擎嵌入、GPU 协调、EDA 验收。 |

它不是运行时外面套的一层皮，而是 Harness 的**真实落地环境** —— 一个复杂、有副作用、会被改坏的真实工程。没有这样的环境，护栏都是纸面上的。

**零 API Key 即可运行**：默认 provider 就是本地 `mock`，装完依赖就能问答、能改代码、能跑验收。云端模型随时切换，界面里改完即生效，不用重启。

## 为什么需要它

Agent 接进真实工程时，栽的是同两个跟头：

**一、幻觉。** AI 凭印象说"伤害计算在 `player.py`"，而这个文件根本不存在。工程化的解法不是"请模型更小心"，而是**让每条结论都带可核对的证据**：文件行号、代码块哈希、检索命中数、工具返回原文。

**二、失控。** 改完不知道改了什么、烧了多少 token、下次会不会再犯。解法是把 Agent 拆成六层，每层都有**能被测出来的**护栏，而不是写在提示词里的祈祷。

<div align="center">
  <img src="docs/architecture.svg" alt="Agent 工作台 · 六层架构" width="86%" />
</div>

| 层 | 职责 | 关键实现 |
|---|---|---|
| **E 执行循环** | ReAct + 反思重试、原生 function-calling、plan 模式、子代理委派、并行只读批次、DAG 多代理编排与失败重规划 | `agent.py` · `orchestrator.py` |
| **T 工具注册** | 声明式工具注册表；参数校验、域名白名单、**受控写**（先读后写 + 体积上限 + 语法校验 + 人工确认） | `tools.py` · `agent_runtime/tool_domains.py` |
| **C 上下文管理** | prompt token 预算、观察截断、轨迹裁剪（保 system 与当前问题）、历史滚动摘要 | `agent.py::_fit_budget` |
| **S 状态存储** | 逐轮 trace 账本（**只记元数据不记正文**）、会话隔离与持久化、跨重启恢复 | `agent_trace.py` · `sessions.py` |
| **L 生命周期钩子** | 钩子 / 技能热插拔、人工审批门、按 provider 计价的成本熔断 | `hooks.py` · `skills.py` · `pricing.py` |
| **V 评估接口** | 黄金题规则打分 + 可选 LLM-judge + **baseline 回归门**（回归即退出码 1） | `agent_eval.py` · `run_golden.py` · `golden/` |

三条设计取向贯穿全部实现：

1. **护栏复用** —— 原生 function-calling 与并行批次都不新开执行路径，直接走既有护栏。避免"两套语义"。
2. **有界** —— 轨迹、观察、历史、批次全部截断或摘要，不让上下文与提示词爆炸。
3. **不越权** —— 编排器只改未执行的任务；写工具绝不并发；钩子异常一律吞掉。

## 界面

工作台是同一套 Web UI，桌面端只是把它装进原生窗口。面板按用途分四组：

| 分组 | 面板 | 解决什么 |
|---|---|---|
| **理解代码** | AI 助手 · 代码地图 · 关系图 · Unity 图 · 流程图 · 改动总览 · 分区图 | 符号级检索、继承与引用关系、每轮问答的决策链路、未提交改动、区域依赖 DAG |
| **改动与验收** | 选区 AI · 场景画布 · 运行时时间线 · EDA 多模态验收 · 实时对话 | 选中即解释 / 改写；`.tscn` 可视化可编辑；游戏运行事件时间轴；Vivado / KLayout 报告的三态验收；画面 + 语音直送本地实时模型 |
| **工程设施** | 任务与生成 · GPU 监控 · 运行与引擎 · AI 运行台 · 素材中心 | 任务分支与 ComfyUI 出图、显存租约与排队、引擎启停与嵌入、逐轮轨迹与断点、素材库与生成 |
| **工作台底座** | 概览驾驶舱 · 文件树 / 编辑器 · 统一设置页 · 模型设置 | 项目概览与上手、CodeMirror 多标签编辑、模型 / MCP / Agent 策略 / 语音模型的统一配置 |

几个值得单说的：

- **关系图**用**左→右分层继承树**而不是力导向。真实项目实测 1485 节点 / 1245 继承边 —— 力导向画这个规模只会得到一团毛球；分层树让继承关系一眼可读，代价是大扇出要折叠成「N 个子类」卡（点开才展开），搜索只看相关子树。
- **产物卡**：模型写下的文件、截到的图**直接摆在回答下面** —— 图片给缩略图，文件给可点的名字。不用再去路径里搜"它到底生成了什么"。
- **推理轨迹按段夹在发言中**：AI 的**解说文字平铺常显**（像正文段落）；说完一段、跑完一批工具，紧跟一行**灰色活动摘要**（「✎ 编辑 N 个文件 · 检索 N 次 · 思考 N 次」），然后接着下一段发言 —— 不是把整轮推理铺完再收一个总摘要。**单步技术明细（工具调用 / 返回结果）默认收起**，点某一段的摘要只展开**这一段**的明细，可逐条看全文与耗时。解说、分段与摘要**刷新后不丢** —— 每轮轨迹以 `turn_id` 落一份不截断的旁路文件，重开页面照样从磁盘读回来，不再出现"一刷新推理过程全没了"。模型的**原生思考流（`reasoning_content`）作为「✦ 深度思考」块摊进单步明细**：碎片流在后端合并成整块按 `turn_id` 落盘，前端用左侧强调条 + 主色单独呈现，只剥真代码转储、尽量保留连贯思路，点开摘要即可读到。
- **查看改动 diff**：写文件类工具（`apply_edit` / `create_file` / `dev_apply_edits`）随观察带上结构化的 before/after，工具行出现「查看改动 · N 个文件」胶囊，点开是行级统一 diff。这份 diff **模型侧看不到**（对 agent 决策与黄金题评分中性），超大文件只退化为简短 unified diff，同样落进旁路轨迹**刷新后可再看**。
- **场景画布**：Godot `.tscn` 的节点图，父子边与实例化边分色，属性与位置可直接改写，**全部可撤销且撤销能逐字节还原文件**。
- **实时对话**：摄像头 / 屏幕共享的画面与语音经 WebSocket 送入本地实时多模态模型。
- **任务面板**：范围校验、存快照、回滚快照、影响面分析四个动作都在任务面板里 —— 先圈定这个任务允许碰哪些路径，越界当场拦下；回滚把文件恢复到快照时的内容（快照之后新建的文件会被删掉）；任务分支按 `docmind/<任务名>` 开 git 分支，工作区有未提交修改时拒绝切换。
- **上下文记忆**：对话输入框右侧的「记忆」打开右侧抽屉，按 L0 宪法 / L1 画像 / L2 共享记忆 / L3 任务书 / L4 会话钩子 / 历史摘要展示下一回合实际注入的内容；宪法、画像、任务书、钩子可直接改，候选可采纳 / 晋升，关联边可增删，索引可重建。
- **语音模型**：STT / TTS / 实时 Omni 的地址、模型与音色在设置页里改并落盘，**不必改 `.env` 再重启**。
- **Plan 长跑**：对话输入框右侧的「长跑」打开 Plan 面板 —— 填目标与复盘间隔，模型在后台连续执行任务书，**全部划掉之前不会停**；划掉后按间隔定期复盘（审查过全部改动？真实测试跑完？确定没有遗漏？还有什么可改进？）。每轮动作与模型主动记录的取舍进「决策账本」供事后审查。**只有用户能停**：关闭面板不停、模型也没有停止工具；Plan 运行期间普通发消息返回 409。

## 能力

### Agent 工具全景：按 domain 声明式分组

分类是**声明式**的（`agent_runtime/tool_domains.py`），不按名字猜 —— 新增工具不声明会红。完整清单用 `tool_catalog` 工具现查。

| domain | 覆盖 |
|---|---|
| `files` | 项目内文件读写、按内容 / 符号 / glob 检索、引用点定位、原子批量改写、白名单目录外的读写删 |
| `verify` | 跑测试、CI 结论、契约校验、依赖对账、索引重建、变更集回滚、审批状态查询 |
| `frontend` | 起 dev server、真实浏览器里点 / 填 / 读、桌面截屏与操作、视觉定位 + 视觉点击、Vue 组件级与应用级验收 |
| `preview_adapter` | 为新文件类型设计声明式预览适配器：草稿 → 审批激活 → 受控刷新 → 回滚 → fixture 测试 |
| `mcp` | 连接器配置与探活、调用、官方 Registry 检索、从自然语言需求发现候选、能力审批、隔离安装与审计 |
| `plan` | 计划、区域提议与落地、分区读写校验、工作流启动、通道、交接、定价、重构；会话定时器（`timer_set` / `timer_list` / `timer_cancel`）与 Plan 决策记账（`plan_decision`） |
| `task` | 缺陷登记与更新、技能创建与审批、资产登记与素材目录 |
| `git` | diff、log、单区提交、全部提交并记变更集 |
| `data` | 数据文件识别与生成、DOCX / PDF / PPTX / XLSX 产物、数值计算、视频提示词 |
| `jobs` | 跑命令、跑 Python（受限子进程）、查看与取消后台任务 |
| `web` | 搜索、抓正文、批量研究、字幕、翻译、天气、内置浏览器（只许公网） |
| `knowledge` | 检索已上传资料、记忆存取、召回经验 |
| `game` | 引擎启停与热重载、引擎截图、数值模拟、影响面、发布前检查、任务登记 |
| `eda` | 真跑 Vivado / KLayout / 仿真并解析输出、在线报告 → 结构化事实 → 三态验收结论 |
| `net` | 受白名单约束的 HTTP 请求（防 SSRF） |
| `catalog` | 按大类查工具清单与用法 |

### 长期记忆：分层、可召回、可审计

在设置页开启「分层记忆 v2」后启用；关闭时回到旧的逐字记忆，提示词与行为不变。记忆**以文件为准、SQLite 只做可重建索引**，所以索引坏了不会丢事实，旧库也能按原项目隔离迁移（不会灌进 global）。

| 层 | 存什么 | 机制 |
|---|---|---|
| **L0 宪法** | 模型不可改的工作原则；用户可编辑 | 每轮常驻、600 字上限；版本 / 哈希 / 条款闸门三重校验，关键条款绑定真实代码门 |
| **L1 画像** | 用户明确说过的称呼、语言、时区、所在地、备注；系统学到的使用习惯 | 文件为准（`profile/declared.md`）；画像与习惯分存，不推断未声明属性；可一键清空习惯 |
| **L2 共享记忆** | 跨项目可复用的事实与经验 | 晋升门：通过回执 + 采纳/4–5 星 + 泛化；未晋升的项目候选按需检索；新证据可把过时候选标为 `superseded`；共享条目带 `superseded_by/valid_until`，新候选晋升后才生效 |
| **L3 任务书** | 长任务的 todo / doing / done 看板 | 从计划或连续动作自动草拟；**完成只认测试/验收/合并回执**，跨窗口可见，不把"做完了"当证据 |
| **L4 会话钩子** | 本窗口关键约定、易错点、下一步 | 压缩后仍保留；错误按签名跨会话累计；80 条上限，超限先淘汰未 pin 的旧条目 |
| **L5 归档** | 被压缩的早期回合与旧记忆 | 只增不删；UI 可回灌完整逐轮，压缩只影响喂模型的工作窗口 |

召回与固化：

- **混合召回**：关键词 + bge-m3 语义向量（1024 维）+ 重要度 / 置信度 / 时效加权；再按关联边做 1–2 跳 Personalized PageRank。嵌入后端不可用时自动退回关键词，不影响问答。
- **每轮异步固化**：单 worker 抽取可复用事实/经验，跳过寒暄、空摘要和明确未验证的回合；候选先留在项目层，不自动晋升。deepseek-v4.1-flash 实测固化成本约 **¥0.001/轮**（随模型与输入变化）。
- **可审计**：测试 / 验收 / 合并 / 评审回执不可变；对话下方 5 星反馈会采纳/晋升（4–5 星）或冻结（1–2 星）；跨会话错误账本与 L4 注入都受字符预算限制。
- **评测流量隔离**：黄金题（`run_golden.py`）的记忆**写入**默认改投独立的 `eval-golden` scope（v1 旧库与 v2 分层记忆两条写入路径都管），读仍按被问项目 —— 改读等于改提示词，基线就不可比了。理由很具体：实测真实项目 56 个记忆会话里 55 个是评测跑出来的，v1 库 136 条 episode 有 54 条标题逐字命中题库，而晋升门只认真实回合的回执。`--memory-write-project ""` 可退回旧行为做记忆回归。
- **面板可改**：右侧「上下文记忆」按层展示实际注入内容，支持编辑宪法/画像/钩子/任务书、采纳/晋升候选、增删关联边、重建索引；「上一回合」还能看到实际注入快照。
- **注入预算**：L0 宪法 600 / L1 画像 500 字符是**固定**的（换大模型也不会写得更长，且没有"这轮先不注入"的退路）；L2 共享 800、L3 任务书 400、L4 钩子 600 是**下限**，整池按 `prompt 预算 × 2%` 长一道天花板（上限 3200/1200/2400），所以 8k 本地模型上逐字等于今天的值、百万窗口上也最多 6488 字符，不会随窗口失控。比例可用 `DOCMIND_MEMORY_INJECT_RATIO` 调。
- **失效与维护**：共享条目可手动标记失效（旧条目保留审计、退出召回）；`python -m agent_runtime.memory maintain` 默认 dry-run，显式 `--apply` 才做候选去重、共享去重（跳过 pin）和失效归档（先复制到 `archive/memory/`），再重建索引。底部「健康」面板展示共享/候选/重复/缺向量/待归档/上下文预算与近 7 天固化，并提供预览/执行维护。

### 等待与无人值守：定时器 + Plan 长跑

- **内部定时体检**：设置页「内部记忆检查间隔（分钟，0 = 关闭）」——后台线程按间隔跑健康检查 + 保守维护（去重 / 失效归档 / 重建索引），结果写 `maintenance-last.json` 并显示在健康面板。先睡后跑，重启不会立刻补一发。
- **模型自己挂的定时器**：`timer_set` / `timer_list` / `timer_cancel`。用于"等后台构建 / 轮询长任务 / 隔一段时间回看有没有新进展"。提醒在**模型每次准备发言之前**注入上下文，所以同一回合里挂的定时器同一回合就能收到；重复定时器不会自动关，正文里直接给出对应的 `timer_cancel(timer_id)`，任务完成必须由模型取消。
- **Plan 长跑（只在开发台）**：对话台「长跑」芯片打开面板，填目标 + 复盘间隔即可无人值守连跑。**停止门是任务书**：全部划掉（`done` / `done_pending_merge`）之前模型不会停；划掉后转入定期复盘，按间隔反复问四件事——审查过全部改动？真实测试跑完？确定没有遗漏？还有什么可改进？发现新工作就追加任务回到执行。
- **只有用户能关**：模型侧没有关闭 Plan 的工具；`/api/plan/stop` 要求 `confirm=true` 且前端有二次确认，**关闭面板不等于停止**。Plan 运行期间普通发消息返回 409，避免用户消息与后台 runner 同踩一个会话。路由全部校验 `X-DocMind-Surface: workbench`，问答页拿不到即 403。
- **决策账本**：每轮的目标、动作清单、结论与任务书快照，加上模型用 `plan_decision` 主动记的取舍，全部追加到该会话的 `plan-log.jsonl`，面板「决策账本」区可逐条回看——事后审查靠这个，不靠模型自述。

### 几处刻意的设计

工具清单能回答"有什么"，回答不了"为什么这么切"。下面几处是实现里**主动做出来的选择**：

- **浏览与交互分成两个会话域。** 内置浏览器 `browser_action` 只许公网，协议 / 主机名后缀 / IP 段 / DNS 解析 / 导航后复核**五道闸**；本机页面操作 `dev_page_action` 只许回环地址。两者**会话域互不相通** —— 模型没法借"看本机"的通道去读公网。
- **视觉定位与视觉点击是两个工具**（`vision_locate_click` / `vision_click`）。合成一个，模型就会以为"定位成功 = 可以点"。点击的放行条件是**审批台账里有人批过**，模型自称确认不算数。
- **引擎控制与 HTTP 接口走同一份实现**（`engine_start` / `engine_stop` / `engine_reload` ↔ `POST /api/engine/start|stop|reload`），共用 GPU 租约与引擎日志 —— 不会出现"界面停了、模型还能开"的两套状态。
- **EDA 验收只判不回滚**（`eda_run_script` / `eda_report_gate`）。真跑脚本并解析输出（fail-closed，工具没装就返回 `tool_unavailable`）；在线报告解析成结构化事实，给 **met / unmet / uncertain** 三态结论。真实回滚仍走人工审批门。
- **程序化工具调用是独立开关**（`DOCMIND_PTC_BRIDGE` / `DOCMIND_PTC_BRIDGE_WRITE`，默认关）。开启后模型可在一次 `python_exec` 里程序化调用其它工具；写侧走带外确认落盘 + 会话隔离 + 硬底线二次复核。
- **提示词前缀缓存：两条命中率一起看。** 设置页「用量与费用」同时报**按 token**（本机 88%）与**按回合**（13%）—— 只看前者会把"跨回合几乎没复用"完全藏住，因为长回合内逐步重发天然命中。已定位到打断跨回合前缀的东西（`【当前时间】` 带秒级时间戳，位于提示词头部），但**不能简单把它挪到历史之后**：`_fit_budget` 的成对删除依赖「固定头区与当前问题之间只含 user/assistant 成对历史」这条不变量，挪进去会被 `test_fit_budget_head_prune` 当场拦下。要拿这份收益得先改裁剪器。

### 工作流：两套并存

| | 开发工作流 | 工具流编排器 |
|---|---|---|
| 入口 | `start_workflow` / `POST /api/agent/workflow/**` 一组端点 | `POST /api/flows` |
| 生命周期 | 方案选择 → 计划审批 → 执行 → 分阶段 apply/review/cleanup → 验收 → 视觉反馈 → checkpoint / rollback → interrupt / resume / revise | 列表 / 保存 / 删除 / run-step |
| 特征 | 跨窗口持久 + 人工门 + SSE 事件流（含重放与心跳） | 轻量 |

后端可选 LangGraph（未安装时回退确定性 manager），前端是对话流内的卡片 + 人工断点模态 + 产物预览。

## 护栏

这是 Agent 工作台 与"接个 API 就叫 Agent"的项目之间真正的差距。每一道都有**能被测出来的**实现：

| 护栏 | 机制 |
|---|---|
| **证据地板** | 引用必须能落地：文件行号、代码块哈希、检索命中数都是硬门槛。截断过的检索结果**不许**被当成全量（"确定性计数自检"直接判红）。 |
| **能力租约** | 读项目 / 读外部 / 写项目 / 写外部 / 执行 / 联网 / 管理，七类能力**分别租用**。模型"顺手读了外部文件"能被查出来。 |
| **人工审批台账** | MCP 连接器与能力装配、工具安装、预览适配器回滚、技能回滚/更新、**视觉点击**都要过 `require_approval`。台账追加写 `.docmind/approvals.jsonl`，可查。**Agent 不能给自己批**（`dev_approve` 只认工作台里人的操作）。可逆的桌面点击/输入/拖拽**不走逐次审批**（见「范围与取舍」），危险组合键由 `desktop_actions._VK` 白名单挡在门外。 |
| **分级执行确认** | `DOCMIND_EXEC_APPROVAL` / `TIER2_CONFIRM` / `TIER3_CONFIRM`，第三档要连点 3 次。 |
| **区域治理** | 一键把工程切成 assets / values / behaviors / levels / ui / audio / net / bugs 等分区，每区**独立 git**；Agent 越区写直接拒；依赖单向 DAG，契约校验防循环耦合。 |
| **产物自报** | 工具在**真正落盘那一刻**自报产物（路径 / 标签 / 大小 / 证据链），随 observation 事件下发。写失败的不算产物。 |
| **不越权** | 编排器只能改**尚未执行**的任务，不回滚已产生的副作用；写工具绝不并发；钩子异常一律吞掉。 |

## 质量

| | |
|---|---|
| 后端测试 | 全量测试收在 `tests/`，以 `unittest.TestCase` 为主、CI 每次跑全量（`python -m unittest discover`）；module 级 `def test_*` 走独立 pytest lane，`pytest` 与 `unittest` 两条都能收 |
| 前端测试 | `node --test` 直跑生产模块，不做打包后测试 |
| CI | GitHub Actions 单 workflow，两矩阵（SQLite / Postgres 16），覆盖：依赖安装 → 前端构建 → 前端测试 → 编译检查 → 评测门 → **变异测试电池** → 检索回归 → 离线工作流验收 → 全量测试 → Postgres 严格集成 → 真实 LLM 冒烟 |
| 变异测试 | 改坏实现，门禁必须抓到（"写了测试但抓不住 bug"和没写测试一样糟） |
| baseline 回归门 | 黄金题库分 L1 单跳 / L2 多跳 / L3 抗干扰三档，pass→fail 即**退出码 1**，已接入冻结发布流程 |
| 视觉回归 | 多套基线逐像素比对，单像素通道差 > 4 的像素超过 200 个即失败 |
| 真实引擎 | Godot / Unity / Unreal 的 HWND 嵌入在真 Win32 宿主下实机验证（置父、样式摘除、resize 跟随、**真实合成键鼠**送达、解除嵌入还原、DPI 一致） |

## 上手

### 下载即用

到 **[Releases](https://github.com/j77156057-art/agent-workbench/releases)** 下载 `DocMind-20261030-994a318-win64.zip`（约 **230 MB**），解压后**双击 `DocMind.exe`**。

该包构建自 `994a318`（2026-10-30），SHA-256 `d3105a8d32fd282b1a159562223793270e70c31fe7c0bf4eae71337eada033b7`；重打配方见 `packaging/build_release.ps1` 与 `docmind.spec`。

无需安装 Python、无需 API Key、无需联网 —— 默认本地 mock 模型，开箱即可跑通完整流程。

### 从源码运行

空白电脑只需要两样东西：**Python ≥ 3.11** 和 **Node ≥ 22**（Node 只在需要构建前端时用得上）。

```bash
# 1. Python 环境与依赖（国内建议阿里云镜像）
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 2. 配置（可选，默认 mock 模式无需 key）
cp .env.example .env        # 改 LLM_PROVIDER=qwen 并填 DASHSCOPE_API_KEY

# 3. 后端
.venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8000

# 4. 前端产物（/ 与 /workbench 都依赖它；未构建时页面会提示先执行本步）
cd frontend && npm ci && npm run build     # 产物 -> ../web

# 5. 打开
#   工作台 : http://127.0.0.1:8000/workbench
#   问答页 : http://127.0.0.1:8000
```

> Windows 上直接跑 `启动DocMind.bat` 更省事：首次启动自动建 `.venv`、装依赖、装前端并构建 `web/`，之后双击即用。
>
> 服务地址用 `127.0.0.1` 而不是 `localhost`（本机 IPv6 解析会连不上）。
> `frontend/.npmrc` 里有 `min-release-age=2`：npm 不安装**发布不足 2 天**的包版本 —— 供应链护栏，挡"刚发就被投毒"的版本。

### 空白电脑自检清单

换台新机器时按这七步走一遍，就知道"能不能跑起来"：

| # | 检查 | 命令 / 预期 |
|---|---|---|
| 1 | Python 版本够 | `python -V` → ≥ 3.11。老版本会在**装依赖之前**被拦下，并说清楚要装哪个 |
| 2 | Python 依赖装得上 | `pip install -r requirements.txt` → 无报错（国内加阿里云镜像） |
| 3 | 前端依赖装得上 | `cd frontend && npm ci` → 依据 `package-lock.json` 精确还原（Node ≥ 22，见 `.nvmrc`） |
| 4 | 前端能构建 | `npm run build` → 产物落到 `../web`（`web/workbench.html` + `web/assets/`） |
| 5 | 后端能起 | `python -m uvicorn api:app --port 8000` → `GET /api/health` 返回 `{"ok":true}` |
| 6 | 不用配置也能用 | **不建 `.env`** 直接起 → provider 默认 `mock`，`POST /api/chat` 能拿到完整 SSE 回答 |
| 7 | 页面能打开 | `/` 与 `/workbench` 都 200，且 `/workbench` 引用的 `assets/*.js` 都取得到 |

### 用真实模型

页面内切换（推荐，免重启）：问答页右上角「⚙ 模型设置」→ 选 provider、填 Key → 保存即生效。API Key 不写进普通状态文件，而是按项目存入本机受保护密钥库，启动时自动加载。

或直接改 `.env`：`LLM_PROVIDER=qwen` / `EMBEDDING_PROVIDER=qwen` / `DASHSCOPE_API_KEY=...`。

```bash
curl -X POST http://127.0.0.1:8000/api/chat -F "question=Agent 工作台 支持哪些文件格式？"
```

### 跑黄金题库回归（可选）

黄金题是**端到端**回归：拿真实模型跑 `golden/questions_v2.json` 里的题，再用规则判分器 `agent_eval.py` 打分，与 `golden/results_baseline.jsonl` 对比 —— pass→fail 即回归（退出码 1）。与单元测试互补：单测锁确定性契约，黄金题锁「活模型 + 真检索 + 真工具」整条链路不被改坏。

```bash
# 1) 实跑：产出每题结果行（写类题必须 --isolate-fs，跑前备份、跑后还原）
.venv\Scripts\python.exe run_golden.py --ids G01,G02,... --provider deepseek --model deepseek-flash \
    --project-id <prj> --isolate-fs --out golden/results.jsonl --max-seconds 200
# 2) 打分 + 回归门
.venv\Scripts\python.exe agent_eval.py --results golden/results.jsonl --baseline golden/results_baseline.jsonl
```

题库跨**两个 code_root**：本项目自身源码（`config.py`/`tools.py`/`agent.py`/`capacity.py`…）与一份附带的示例库 `booklog`。`--project-id` 决定当前 code_root，所以两套题要**分两批跑**再合并。L3 抗干扰降级档用 `--disable-tools search_code` 单独跑。

没有云端 API Key、或云模型临时不可用（欠费 / 限流）时，可切**本地 Ollama**兜底跑通整库，零 Key：加 `--provider ollama --model <本地已拉取模型>`（如 `qwen3.6:35b-a3b`）即可，其余参数与上面一致。注意这只是**能力演示**——本地小模型的答准率通常低于校准基线用的云端模型，跑出来的结果行**不要**折进 `results_baseline.jsonl` 锚点：基线只在单一校准模型上才有「pass→fail 即回归」的意义，混模型会让回归门失真。

`expect` 支持的判据：`must_include` / `any_of` / `must_not_include` / `regex` / `must_call`（AND，全调过）/ `any_of_call`（OR，可互换的检索工具任一即可）/ `min_actions` / `max_actions` / `no_error`，以及诚实性元标志 `degraded` / `verification_status`（opt-in，老题不受影响）。

几处**踩过的坑**，改题时务必知道：

- **判分器对 `must_not_include` 有子句否定豁免**：禁用词若只出现在否定/辟谣子句里（「不是 12」「`k8s/deployment.yaml` 不存在」），视为模型在**如实反驳**而非幻觉，判通过；肯定式编造仍判失败。
- **`expect` 会被冻结进每行结果**：改了题库的 `expect`，必须**重跑该题**刷新结果行，否则打分用的还是旧判据。
- **`max_actions` 只判分、不封顶运行**：真正兜住死循环的是软超时（`--max-seconds`）与重复/失守早退，不是步数上限。
- **容量类问法会被零-Token 能力面板拦截**：`agent.py` 的 `_CAPACITY_HELP_RE` 命中「容量词（token/步数/上限…）+ 疑问词（多少/如何…）」会直接从设置作答、`actions=0`、根本不检索源码。问「某常量默认值」的题要用祈使句直接点名「打开 `<文件>`、找到 `<标识符>`、报告其赋值」绕开它。
- **只收「活的、可端到端观测、且没被单测锁死」的行为**：已被移除的死门 / 已被单测钉死的确定性逻辑，不进黄金库（否则要么不可复现、要么和单测重复）。

## 代码地图

```
agent-workbench/
├── api.py                 # HTTP / SSE 总入口
├── api_routes/            # 领域路由：agent（含工作流）· engine · gpu · eda · projects ·
│                          #   resources（素材与 ComfyUI）
├── agent.py               # ReAct 循环、反思重试、代码优先路由、证据护栏；原生 FC、plan、子代理委派、并行批次
├── tools.py               # 工具注册表（按 domain 声明式分组）
├── agent_runtime/         # 运行时契约与能力实现
├── regions.py             # 分区 2.0：声明式配置、契约校验、变更集与回滚
├── scene_runtime.py       # 场景画布内核：.tscn 行块解析 → 图模型 → 受控编辑
├── workbench_fs.py        # 沙箱文件树、读写、git 历史与回滚、符号地图、关系图
├── game_workbench.py      # 引擎 catalog / 启停 / 嵌入、任务与资产、审批台账
├── desktop_bridge.py      # Win32：查找宿主窗口 / SetParent 嵌入 / resize / focus（纯 ctypes）
├── desktop.py             # 桌面启动器（单实例保护 + pywebview 窗口，失败即回退浏览器）
├── dependency_bootstrap.py # 空白电脑的依赖自举：建 .venv、装依赖、装前端并构建 web/
├── agent_trace.py         # 逐轮 trace + token 账本（JSONL，只记元数据）
├── sessions.py            # 会话隔离 + 持久化 + 滚动摘要；逐轮推理轨迹按 turn_id 落不截断旁路文件（刷新可恢复）
├── pricing.py / hooks.py / skills.py
├── artifact_tools.py      # DOCX / PDF / PPTX / XLSX 结构化生成与回读校验
├── orchestrator.py        # 多代理编排：任务图 DAG + 并行 + 重规划 + 结果合成
├── agent_eval.py / run_golden.py / golden/   # 黄金题库与 baseline 回归门
├── frontend/              # Vue 3 + Vite 工作台（产物输出到 ../web）
│   └── src/workbench/
│       ├── components/    # 工作台组件
│       ├── composables/   # 组合式逻辑
│       └── lib/           # 纯函数模块（可被 node 单测直跑）
├── tests/                 # 后端全量测试
└── docs/                  # 设计文档与缺陷口径
```

## 配置

`.env.example` 是**可选项**的摊开清单（不是必填）。分组：

| 组 | 代表变量 |
|---|---|
| LLM Provider | `LLM_PROVIDER`（`mock` / qwen / deepseek / kimi / zhipu / siliconflow / openai / opencode / ollama / llamacpp / custom）、`LLM_MODEL`、`LLM_API_KEY` |
| Harness 视觉辅助 | `DOCMIND_VISION_PROVIDER/MODEL/BASE_URL/API_KEY`、`DOCMIND_VISION_ALLOW_UNVERIFIED` |
| Embedding | `EMBEDDING_PROVIDER`、`EMBEDDING_MODEL` |
| 路径与检索 | `CHROMA_DIR`、`CHUNK_SIZE`、`TOP_K`、`DOCMIND_RETRIEVAL_MODE`（dense / hybrid / hybrid_rerank） |
| Agent 账本 / 会话 / 弹性 | `DOCMIND_TRACE`、`DOCMIND_LANGSMITH_*`、`DOCMIND_CHECKPOINT_POSTGRES_DSN`、`DOCMIND_SESSION_*` |
| 语音 STT / TTS / 实时 Omni | `DOCMIND_STT_URL/API_KEY/MODEL`、`DOCMIND_TTS_URL/API_KEY/MODEL/VOICE`、`DOCMIND_OMNI_*` |
| LLM 调用 / 联网预算 | `DOCMIND_LLM_TIMEOUT/RETRIES`、`DOCMIND_WEB_SEARCH_FAIL_LIMIT` |
| 工具通道 / 子代理 / 计划 | `DOCMIND_TOOL_MODE`、`DOCMIND_SUBAGENT_MAX_DEPTH`、`DOCMIND_PARALLEL_TOOLS` |
| 成本熔断 | `DOCMIND_BUDGET_CNY`、`DOCMIND_SESSION_BUDGET_CNY` |
| 热插拔目录 | `DOCMIND_HOOKS_DIR`、`DOCMIND_SKILLS_DIR` |

**几类配置不走 `.env`**：

- **联网搜索 / URL 抓取的 provider**（`WEB_SEARCH_PROVIDER`、`WEB_FETCH_PROVIDER`、`EXTERNAL_API_ALLOWLIST`）只在 `config.py` 里读，`.env.example` 未列。
- **审批与权限模式**（`DOCMIND_EXEC_APPROVAL`、`EDIT_CONFIRM`、`DOCMIND_ACCESS_MODE`）。
- **MCP 连接器 / 区域治理 / 引擎连接器**：走应用内设置页 + 落盘文件（`/api/config`、`/api/agent/connectors`、`/api/engine/config`、`regions.json`）。

## 范围与取舍

本节写明**适用的范围**与**已知的取舍**。过程性的缺陷定位记录（`docs/known-defect-*.md`）属开发工作文档，**不随本仓库公开**；公开的口径与设计见下方「文档」一节。

| 维度 | 现状 |
|---|---|
| 单用户本地 | 本机默认免鉴权，可按需开单 token 校验。**不做**多用户协作与角色权限 |
| 桌面动作审批 | 可逆的点击/输入/拖拽**直接执行**，不做逐次审批 —— 逐次确认卡的骚扰成本高于收益。保留的灾难地板：键名白名单（`Win+r` 这类系统组合键一个事件都不投递）、坐标必须在目标窗口客户区内、引擎窗口只按当前项目根解析。**视觉点击**仍走审批台账（模型自称确认不算数） |
| 平台 | 桌面端打包为 Windows 64 位；服务端与前端跨平台 |
| 分发版能力 | 分包校验与 DOCX / PDF / PPTX / XLSX 生成在进程内完成；playtest、cProfile、`python_exec` 需要真实 Python 环境 |
| 引擎嵌入覆盖 | Godot 4.7.2 + 真 Win32 宿主实机验证；100% / 125% 缩放未覆盖（本机 150%） |
| 大规模并发 | 单机单用户场景已验证；多项目 / 多引擎 / 长期运行评估未覆盖 |
| 许可 | **MIT**（全文见 [`LICENSE`](LICENSE)）。`docs/` 与 `dist/` 里的第三方许可属于各自上游项目，不随本仓库授权 |

## 文档

- [`docs/integrations.md`](docs/integrations.md) —— MCP 引擎桥接、Web 试玩导出的产品级用法与边界
- [`docs/agent-architecture-review.md`](docs/agent-architecture-review.md) —— 架构评审
- [`docs/capability-boundary.md`](docs/capability-boundary.md) —— 能力边界：哪些是真验证过的，哪些不是
- [`docs/screenshots/`](docs/screenshots/) —— 界面实拍
