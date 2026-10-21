# Agent 工作台 能力边界对照表

> 适用对象：所有参与 Agent 工作台 规划、开发、接需求的人。
> 本文档是**设计层面的能力边界约定**（哪些该管、哪些该集成第三方、哪些明确不碰），不是实现说明。判断一个新需求"该不该做"时，先回这里对表。

---

## 1. 为什么要有边界（设计宪章回顾）

Agent 工作台 立项时的定位是：**本地、单人、面向游戏 / Mod 工程的 AI 研发脚手架**，不是通用 ALM 平台。两个硬约束决定了边界：

- **不造引擎、不重做平台**：渲染 / 物理 / 音频内核、多人后端、商店上架、CI 运营都归专业工具与平台。
- **不做多用户 / DB / 鉴权**：本地单人使用，状态存本地 JSON 与分区 git，无账户体系。

边界的目的不是"偷懒"，而是把有限精力压在真正差异化的地方——**让 AI 在真实的游戏 / Mod 工程代码上检索、受控改写、可视化编辑而不崩**。凡是不属于这一层的东西，要么交给第三方（集成），要么明确不碰（红线）。

---

## 2. 能力边界对照表（核心）

立场三档：**管**（Agent 工作台 自有能力）／**集成**（提供接缝，由第三方实现）／**不碰**（明确红线）。

| 能力域 | 子类 / 代表工具 | 立场 | 说明 |
|---|---|---|---|
| **引擎运行时** | Unity / UE / Godot / GameMaker / Phaser / Bevy | 管（嵌入+试玩）＋ 不碰（本体） | 不造引擎；但能把引擎窗口 **HWND 嵌入**工作台、导出 **Web 试玩**；渲染/物理内核归引擎 |
| **语言 / IDE** | C# / C++ / GDScript / Lua / Rust；VS / Rider / VSCode | 管（代码层）＋ 不碰（IDE 本体） | 代码问答、受控改写、选区 AI、符号图是强项；IDE 仍是用户自己的工具 |
| **美术与资产创作** | Blender / Maya / ZBrush / Substance / Photoshop / Krita / Aseprite / Spine / Tiled | **不碰（明确）** | 二进制 / 专业创作工具；Agent 工作台 只处理代码与 `.tscn` 文本，不生成或编辑美术源文件 |
| **音频** | FMOD / Wwise / Audacity / Reaper / FL Studio / Audition | **不碰（明确）** | 同上；可**生成对接 FMOD/Wwise 的代码**，但不制作音频 |
| **版本控制（代码）** | Git + GitHub / GitLab | **管** | 分区独立 git、契约 DAG 校验、变更集整体回滚——核心能力之一 |
| **资产管理（大二进制）** | Perforce / PlasticSCM / Git-LFS | **不碰（明确）** ＋ 集成（留 seam） | 美术/音频二进制整体改、难 merge，主流用 Perforce；Agent 工作台 的 git 方案不为此设计，应交专业工具负责 |
| **项目管理 / 缺陷 / 沟通** | Jira / Trello / Asana / Notion / HacknPlan / Linear / Bugzilla / Slack / Discord | 集成（MCP/API seam）＋ 不碰（本体） | 不重做 PM/IM；可通过 **MCP 桥**读 Jira 任务、查 Bug，但不替代 |
| **中间件·物理** | PhysX / Havok | **不碰（引擎层）** | 物理仿真归引擎 |
| **中间件·音频** | FMOD / Wwise | **不碰（本体）** ＋ 集成（生成对接代码） | 见"音频"行 |
| **中间件·联机** | Photon / Unity Gaming Services / Epic Online Services / Steamworks | **不碰（明确）** ＋ 集成（生成对接代码） | 多人后端 / 服务器归专业服务；Agent 工作台 可生成接入代码，不运营后端 |
| **中间件·分析** | GameAnalytics / Amplitude / Firebase | **不碰（明确）** | 遥测 / 变现分析归第三方 |
| **测试与性能剖析** | RenderDoc / Unity Profiler / nUnit / CppUnit / UFE | 管（Agent 自检＋评测门＋单测）＋ 集成（消费产物） | 自带 **1065 单测 + 评测回归门 + 引擎嵌入真机自检**；可读取引擎 profiler / 截帧产物，但不替代 RenderDoc |
| **构建 / CI / 发布 / 平台 SDK** | CI/CD；Sentry / Backtrace；Steamworks / 主机 SDK；DirectX12 / Vulkan | **不碰（明确）** ＋ 集成（生成模板） | 发布、商店上架、崩溃上报归第三方 / 平台；Agent 工作台 可产出 CI 脚本模板、注入 Sentry 的代码片段，但不运营 |
| **AI 与程序化工具** | Unity Muse / Adobe Firefly / NVIDIA Omniverse | **不碰（明确）** ＋ 集成（走 API 工具调用） | AI 资产生成归专业工具 / API；Agent 工作台 已有 `gen_video_prompt`、`web_search`，可桥接但**不能替代**建模 / 绘图 |
| **素材商店与合规** | Asset Store / Fab / Kenney / OpenGameArt；许可 / GDPR | 管（素材筛选）＋ 不碰（法务执行） | `search_assets` 基于本地 CC0 清单做检索，不下载版权素材；合规只做**提示**，不审核 / 担保 |

---

## 3. Agent 工作台 已具备（"管"那一档）能力清单

落地或已验证的能力，按层归类：

- **AI 内核**：本地 RAG + ReAct Agent（先检索证据再答）、原生 function-calling、多代理编排（任务图 DAG + 失败重规划）、反思重试、成本熔断、评测回归门。
- **工程协作**：分区开发（独立 git + 契约校验 + 变更集回滚）、受控改写（`apply_edit` / `create_file`，先读后写 + 语法校验 + 人工确认）、选区 AI。
- **可视化编辑**：场景画布（Godot `.tscn` 可视化可编辑，撤销可逐字节还原）、运行时时间线（游戏事件多轨时间轴）、符号与关系图。
- **引擎衔接**：引擎嵌入（Godot / Unity / Unreal HWND 嵌入工作台）、Web 试玩导出、MCP 桥（stdio / http 对接外部引擎桥）。
- **运行时（Harness）**：trace 账本、会话隔离持久化、LLM 弹性（重试 / 超时 / 断连记账）、hooks / skills 热插拔、并行工具批次。
- **工具箱**：`search_code` / `read_file` / `grep`、`python_exec`、`web_search`、`gen_video_prompt`、`search_assets`。
- **模型与交付**：mock / ollama / qwen / deepseek 多 provider；GPU 租约队列 + 模型预加载显存护栏；PyInstaller onedir 桌面打包。
- **质量基线**：1065 单测 + 场景画布 54 + 浏览器 23 + 引擎嵌入真机 79。

---

## 4. 集成策略原则（如何接第三方而不越界）

1. **Seam 优先**：能用 MCP 桥、工具调用、代码生成解决的，绝不把能力搬进 Agent 工作台 本体。
2. **代码生成 > 运行时接入**：联机、CI、崩溃上报这类，Agent 工作台 产出**可落地的配置 / 代码片段**（模板），由用户接入真实服务，而非自己运营。
3. **只读消费，不替代**：可读取引擎 profiler / 截帧 / 日志产物做分析，但不重做 RenderDoc / Unity Profiler。
4. **合规只提示不执行**：许可、GDPR、商店条款给出醒目提示与清单，但不做审核或法律担保。
5. **绝不碰**：二进制美术 / 音频创作、大二进制资产管理（Perforce 层）、物理 / 渲染内核、联机后端运营、发布 / 商店上架、变现计费。

---

## 5. 明确不碰（红线清单）

- 多用户 / 数据库 / 鉴权体系（设计宪章硬约束）。
- 通用 ALM：任务流看板、Wiki、IM 重做。
- 美术 / 音频的**原生创作**（建模、雕刻、贴图、编曲、音效制作）。
- 大二进制资产管理层（Perforce / PlasticSCM / Git-LFS）。
- 物理 / 渲染 / 音频引擎内核。
- 联机后端运营、发布与商店上架、变现 / 计费。
- 主机 / 平台认证流程。

---

## 6. 一句话定位

**Agent 工作台 = 引擎之上的"代码与资产代码化协作层 + AI 研发脚手架"**——它解决的是 AI 写游戏代码时"幻觉"与"代码堆叠"两大失败模式，而不是"从建模到上架"的全链路。白空（美术/音频创作、资产管理、发布运营）是**刻意的**，靠集成与红线守住，不该被需求拉扯着补成又一个臃肿平台。
