# DocMind · 面向工程代码的 Agent 运行时（Harness）

一个**可离线运行的 Agent 运行时**：把「能跑的 agent」补齐成**可观测、可编排、可回归**的工程系统。
上层落地为一个本地工程工作台（代码问答 / 受控改写 / 场景与运行时可视化）——**但项目主体是 Harness，工作台只是它的落地场景**。

IT 查询助手已经拆分为独立项目 `docmind-it-assistant`；本仓库只保留开发问答、开发工具与游戏工作台，不再提供 IT 页面或 IT API。

**要解决的两个失败模式**：

1. **幻觉**——AI 凭印象说"伤害计算在 player.py"，其实没有这个文件；
2. **失控**——改完不知道改了什么、烧了多少 token、下次会不会再犯。

应对方式是把 agent 拆成六层，逐层补上工程护栏（逐项详解见「Harness 能力」章节）：

| 层 | 职责 | 关键实现 |
|---|---|---|
| **E 执行循环** | ReAct + 反思重试、原生 function-calling、plan 模式、子代理委派、并行只读批次、DAG 多代理编排与失败重规划 | `agent.py` · `orchestrator.py` |
| **T 工具注册** | 47 个工具；参数校验、域名白名单、**受控写**（先读后写 + 体积上限 + 语法校验 + 人工确认） | `tools.py` |
| **C 上下文管理** | prompt token 预算、观察截断、轨迹裁剪（保 system 与当前问题）、历史滚动摘要 | `agent.py::_fit_budget` |
| **S 状态存储** | 逐轮 trace 账本（**只记元数据不记正文**）、会话隔离与持久化、跨重启恢复 | `agent_trace.py` · `sessions.py` |
| **L 生命周期钩子** | 钩子 / 技能热插拔、人工审批门、按 provider 计价的成本熔断 | `hooks.py` · `skills.py` · `pricing.py` |
| **V 评估接口** | 黄金题规则打分 + 可选 LLM-judge + **baseline 回归门**（回归即退出码 1） | `agent_eval.py` · `run_golden.py` · `golden/` |

**三条设计取向**：① **护栏复用**——原生 FC 与并行批次都不新开执行路径，直接走既有护栏，避免"两套语义"；② **有界**——轨迹、观察、历史、批次全部截断或摘要，不让上下文与提示词爆炸；③ **不越权**——编排器只改未执行任务、写工具绝不并发、钩子异常一律吞掉。

零 API Key 可跑（mock / 本地 Ollama / llama.cpp），云端 Provider 可随时切换。

![DocMind · Agent Harness 六层架构](docs/architecture.svg)

---

## 最近更新（2026-09）

> 近期围绕「云端大模型用足上下文、本地计费更准、运行台更可读」做了一轮打磨：

- **输出预算按真实上下文窗口派生（不再写死 3072）**：`config.py` 新增 `output_token_budget()`，默认取「窗口 × 0.33」，夹在 `[3072, 65536]` 且不超过窗口本身。云端模型（DeepSeek 等）若静态画像只命中厂商默认窗口，会自动**联网检索官方文档**判定真实窗口（如 DeepSeek 默认 131k → 检索校正到 1M），结果缓存到 `.docmind_model_context.json`，避免重复联网；1M 窗口给到 ≈65k 输出上限，彻底告别"畏手畏脚"。环境变量可微调：`DOCMIND_OUTPUT_FRACTION`（默认 0.33）、`DOCMIND_OUTPUT_MIN_TOKENS`、`DOCMIND_OUTPUT_MAX_TOKENS`。同时修复了 Ollama `num_predict` 兜底公式（按 `窗口 − prompt 预算 − 512` 给输出留量），大窗口模型输出不再被错误掐到 512 地板。
- **计费纳入 prompt-cache 折扣**：`pricing.py` 为 DeepSeek / Qwen 引入缓存命中（按输入价 1/10）与缓存写入（按输入价 1.25×）单价；运行台费用不再把缓存命中的 token 按全价重复计，花费估算更贴近真实。
- **运行台展示缓存命中明细**：前端 `HarnessPanel` / `FlowCanvas` 每轮 trace 在费用后追加「缓存命中 N token（按折扣价计费）」，直观看到 prompt caching 省了多少。
- **续写纠偏重试关思考**：思考型模型（qwen3 开思考 / reasoner 原生思考）偶发把整段输出预算烧在 reasoning 上导致被长度截断、继而反复触发续写纠偏。现在截断续写分支会在重试那一轮**临时关闭思考**，把预算全留给答案；不影响后续正常轮次。

---

## 落地场景：本地工程工作台

上层是一个**本地、单人**的工程工作台（以游戏 / Mod 工程作为验证场景）：任务分区、每区独立 Git、契约方向校验、变更集回滚、选区 AI、符号 / 关系图、引擎嵌入、场景画布、运行时时间线、GPU 协调。

它存在的意义是给 Harness 提供一个**真实、复杂、有副作用**的落地环境——**不是通用 ALM 平台：不做多用户协作与角色权限**（本机默认免鉴权，可按需开启单 token 校验）。

### 工作台六件事（2026-09 当前形态）

| 能力 | 一句话 |
|---|---|
| **分区开发** | 一个按钮把工程切成 assets / values / behaviors / levels / ui / audio / net / bugs 等分区，每区**独立 git**；Agent 的写操作被约束在对应分区内，越区写直接拒；依赖是单向 DAG，契约校验防循环耦合 |
| **受控改写** | 所有 AI 写操作走 `apply_edit` / `create_file`（先读后写 + 体积上限 + `.py` 语法校验 + 人工确认）；跨区搬移校验依赖方向；改动可单区提交或**跨区变更集整体回滚** |
| **选区 AI** | 编辑器里选中一段代码 → 解释 / Review / 提问（走 Agent，带证据）或**改写**（走直连快通道 + LCS diff 预览，接受才落盘） |
| **符号与关系图** | 多语言符号抽取（Python `ast` / GDScript / Java…）+ 继承 / 场景挂载 / 调用边的关系图；场景画布另有四类边 |
| **场景画布** | Godot `.tscn` 的**可视化 + 可编辑**画布：层级树 / 空间坐标两种布局，节点父子层级、实例（instance）、position / transform、资源引用一目了然；异步加载、实际项目修改和面板尺寸变化后会自动重新适配视口，解析为空时给出明确诊断；新增 / 删除 / 改名 / 换父 / 复制 / 改属性 / 拖拽写回位置，**全部可撤销，且撤销能逐字节还原文件** |
| **运行时时间线** | 把游戏跑起来产生的事件（掉血 / 死亡 / 生成 / 变量变化）画成多轨道时间轴：类型筛选、时间缩放、会话分组、数值曲线、导出 JSON、点事件跳代码行 |

配套：**引擎嵌入**（Godot / Unity / Unreal 启停 + Win32 HWND 嵌进工作台）、**Web 试玩**（导出 WASM 在画布里边玩边改）、**MCP 桥接**、**GPU 租约队列**、**桌面打包**（PyInstaller onedir，双击即用）。

> **MCP 引擎桥接** 与 **Web 试玩导出** 的产品级使用文档与边界说明见 [`docs/integrations.md`](docs/integrations.md)。设置页搜索 `EDA` / `PCB` / `KiCad` 时可联网发现真实 MCP Registry 候选；用户选择并测试连接后，系统从真实 `tools/list` 生成能力与表单候选，必须人工批准才加入 Agent 路由。搜索摘要不会变成可执行命令；测试本地候选时可能首次下载对应 MCP 包。

## 🧱 技术栈

- 后端：Python · FastAPI（HTTP + SSE）· Chroma 多集合（文档 / 代码 / 经验）· OpenAI 兼容多 Provider（qwen / deepseek / ollama / llamacpp / mock）· PyInstaller + pywebview
- 前端：Vue 3.5 · Vite 5 · TypeScript · CodeMirror 6 · Vue Flow · 手写深色设计系统
- 验证：`unittest` **450 项** · 场景画布自检 54 项 · 浏览器冒烟 27 项（Playwright + 系统 Edge）· 引擎嵌入实机自检 79 项
- 评测：黄金题库 40 题（单跳 / 多跳 / 抗干扰三档）+ baseline 回归门，跑题用 `run_golden.py`、打分用 `agent_eval.py`

## 📁 目录结构

```
rag-agent/
├── api.py                 # HTTP / SSE 总入口（159 条路由：chat / ingest / 工作台 fs / regions /
│                          #   engine / desktop-host / selection-ai / scene / runtime / MCP / GPU /
│                          #   trace / sessions / budget / hooks / skills / orchestrate）
├── agent.py               # ReAct 循环、反思重试、代码优先路由、证据护栏；run/_run 埋点外壳；
│                          #   原生 function-calling、plan 模式、子代理委派、并行批次
├── tools.py               # 47 个工具：9 基础 + 受控写 + 分区/研发工具 + delegate/orchestrate/dev_use_skill
├── agent_runtime/         # 跨应用工具结果、验证与应用权限/状态命名空间契约
├── applications/
│   └── developer/         # 开发问答/游戏工作台的应用边界
├── regions.py             # 分区 2.0：声明式配置、契约校验（DAG 无环 / 导出存在）、变更集与回滚
├── scene_runtime.py       # 场景画布内核：.tscn 行块解析 → 图模型 → 受控编辑（可回滚 + 可撤销）
├── workbench_fs.py        # 沙箱文件树、读写、git 状态 / 历史 / 回滚、符号地图、关系图
├── symbols.py             # 多语言符号抽取 + 代码感知分块
├── game_workbench.py      # 引擎 catalog / 启停 / 嵌入、任务 / 资产 / bug 工作流
├── engine_adapters.py     # 引擎适配薄封装
├── desktop_bridge.py      # Win32：查找宿主窗口 / SetParent 嵌入 / resize / focus
├── desktop.py             # 桌面启动器（单实例保护 + pywebview 窗口）
├── gpu_coordinator.py     # GPU 租约队列（TTL 过期 / 移交 + 显存阈值）
├── mcp_client.py          # MCP（Model Context Protocol）桥接（用法与边界见 docs/integrations.md）
├── web_export.py          # Godot Web 导出与本地试玩（用法与边界见 docs/integrations.md）
├── llm.py / embeddings.py / vectorstore.py / ingest.py / config.py
├── agent_trace.py         # 逐轮 trace + token 账本（JSONL，只记元数据；/trace 查看页）
├── sessions.py            # 会话隔离 + 持久化 + 滚动摘要
├── pricing.py             # 按 provider 计价 + 全局/会话预算熔断
├── hooks.py               # 工具/回合钩子热插拔（.docmind/hooks/*.py）
├── skills.py              # 内置 + 用户技能热插拔（agent_skills / .docmind/skills）
├── artifact_tools.py      # DOCX / PDF / PPTX / XLSX 结构化生成与回读校验
├── agent_skills/          # 随源码和桌面包发布的文档、PDF、演示、表格技能
├── agent_eval.py          # 黄金题自动打分 + baseline 回归门（回归即退出码 1）
├── run_golden.py          # 黄金题库运行器：跑题产出 results.jsonl 供 agent_eval.py 打分
├── orchestrator.py        # 多代理编排：任务图 DAG + 并行 + 重规划 + 结果合成
├── golden/                # 黄金题库与基线结果：questions_v2.json（40 题）/ results_baseline.jsonl
├── frontend/              # Vue 工作台（构建产物输出到 ../web）
│   └── src/workbench/components/
│       ├── SceneCanvas.vue / SceneNodeCard.vue / SceneFileCard.vue   # 场景画布
│       └── RuntimeTimeline.vue                                       # 运行时时间线
├── tests/                 # unittest 450 项
└── verify_scene_canvas.py / verify_scene_canvas_ui.mjs   # 场景画布自检 + 浏览器冒烟
```

## 🚀 快速开始

### 方式一：下载即用（不想配环境就直接试用）

到 **[Releases](https://github.com/j77156057-art/rag-agent/releases)** 下载 `DocMind-v1.0.0-win64.zip`（约 **160 MB**），解压后**双击 `DocMind.exe`** 即可。

- **无需安装 Python**、无需 API Key、**无需联网**——默认本地 mock 模型，开箱即可跑通完整流程
- 控制台窗口会打印本地地址并自动用默认浏览器打开界面（浏览器即桌面端）；关闭该窗口即停止
- 需要 Windows 10 / 11 **64 位**；「分区开发」的 git 功能需另行安装 Git，未装不影响其余功能
- 压缩包内附 `使用说明.txt`

### 方式二：从源码运行（开发 / 二次开发）

```bash
# 1. 建虚拟环境并装依赖（国内建议阿里云镜像）
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 2. 配置（可选，默认 mock 模式无需 key）
cp .env.example .env      # 改 LLM_PROVIDER=qwen 并填 DASHSCOPE_API_KEY

# 3. 启动后端
.venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8000

# 4. 前端：开发用 vite dev，或构建一次产物给后端托管
#    ⚠️ 必需：工作台页 /workbench 依赖这里的构建产物（web/workbench.html 与 web/assets/）。
#    web/assets/ 与 web/workbench.html 被 .gitignore 排除，所以 clone / 下载 ZIP 后并不存在；
#    不执行这一步，打开 /workbench 会提示「工作台前端未构建」（问答页 / 不受影响）。
cd frontend && npm install && npm run build      # 产物 -> ../web

# 5. 打开
#   工作台 : http://127.0.0.1:8000/workbench
#   问答页 : http://127.0.0.1:8000
```

> 服务地址请用 `127.0.0.1` 而不是 `localhost`（本机 IPv6 解析会连不上）；路径含单引号用户名时 shell 一律用双引号包裹。

### 用真实模型（以通义千问为例）

两种方式任选其一：

- **页面内切换（推荐，免重启）**：打开问答页点右上角「⚙ 模型设置」，选 provider、填 Key、点「保存并切换」即时生效；provider、模型、自定义接口地址和 embedding 选择会跨重启恢复。API Key 不写入普通状态文件，而是按项目存入本机受保护密钥库，启动时自动加载（切换项目后使用对应项目的密钥）。Key 输入框留空表示“不修改已有 Key”；如果升级/切换启动方式后提示“无法解密”，在当前用户下重新保存一次即可完成迁移。
- **改 `.env`**：`LLM_PROVIDER=qwen` / `EMBEDDING_PROVIDER=qwen` / `DASHSCOPE_API_KEY=...`，改完重启服务。

```bash
# 不依赖前端直接调 API
curl -X POST http://127.0.0.1:8000/api/chat -F "question=DocMind 支持哪些文件格式？"
```

### 图片 / 视频视觉链路

工作台的 ChatDock 支持选择、粘贴或拖拽图片。模型能力画像会区分“模型原生识图”和
“Harness 视觉辅助”：原生视觉模型直接收到图片；文本模型不会被误传图片，而是由已配置
的视觉模型生成有界观察，再交给当前 Agent 结合代码和日志复核。视频入口会用 imageio/ffmpeg
抽取关键帧，生成时间轴观察后填回对话输入，适合检查游戏启动画面、HUD 重叠、摄像机越界和
长时间游玩中的可见异常。

ChatDock 顶部的“会话”菜单提供当前项目的会话历史和一键切换；“新对话”会创建新的隔离
`session_id`，旧对话仍保留。列表用首条用户问题作为标题，切换后会回灌完整问答；会话内容
按项目持久化在 `.docmind_sessions/`，服务重启后仍可继续。

```dotenv
# 可选：为不支持视觉的主模型配置本地视觉兜底（默认不出网）
DOCMIND_VISION_PROVIDER=ollama
DOCMIND_VISION_MODEL=qwen3-vl:8b
# DOCMIND_VISION_BASE_URL=http://127.0.0.1:11434/v1
# DOCMIND_VISION_API_KEY=
CHAT_VIDEO_MAX_BYTES=209715200
CHAT_VIDEO_MAX_FRAMES=8
```

打开“模型设置”可分别确认深度思考、图片识别和视频识别能力；确认结果按
`provider/model` 持久化。`unknown` 不等同于“不支持”，而是要求 Harness 使用已配置的
视觉模型，或等待用户确认后再把原图交给当前模型。

## 🖥️ 桌面端（原生窗口一键启动）

`desktop.py` 把后端服务与前端组装成桌面窗口。**优先使用 pywebview 原生窗口（Windows 走 Edge WebView2）；若目标机器缺少 WebView2 运行时，会自动降级为「控制台窗口 + 打开默认浏览器」**——这条降级路径正是为「双击没反应」准备的（原生窗口在缺运行时会静默失败：不报错也不弹窗、进程却还活着），两种模式都能正常使用。启动时另有**单实例保护**：若 8000 端口已在提供 DocMind 服务，直接打开浏览器并退出，不会堆积进程。

**两个启动脚本，按场景选**：

| 脚本 | 跑什么 | 前置条件 | 用途 |
|---|---|---|---|
| `启动DocMind.bat` | 调用自动依赖同步器后启动当前源码；源码不可用时回退 `dist\DocMind\DocMind.exe` | 仅需安装 Python 3.11+；首次启动需要联网安装依赖 | 仓库内日常启动（默认跟随最新源码） |
| `run_desktop.bat` | 同上，兼容旧的源码启动入口 | 同上 | 命令行/自动化启动 |

```bash
# 默认启动当前源码；首次启动会创建 .venv、安装 requirements.txt、安装前端锁定依赖并构建 web/
启动DocMind.bat

# 打包模式（演示 / 交付）：先构建，再直接运行打包产物
.venv\Scripts\python.exe -m PyInstaller docmind.spec --noconfirm   # 产出 dist\DocMind\DocMind.exe
dist\DocMind\DocMind.exe
```

> ⚠️ **`dist/` 不入库**（见 `.gitignore`）。依赖同步器按 `requirements.txt` 和 `frontend/package-lock.json` 的 SHA-256 记录状态；文件发生变化时，下次启动自动执行依赖升级和前端重建。若目标机器没有 npm，只要已有 `web/assets` 构建产物仍可启动；首次没有构建产物时必须安装 Node.js。

### 发布、更新与卸载

推荐使用发布脚本生成可交付包：

```powershell
pwsh -File packaging/build_release.ps1
```

脚本会同步构建依赖、构建 PyInstaller onedir 包、写入 `install-manifest.json`，并把 `uninstall.bat`、`upgrade_portable.ps1` 和卸载脚本放进发布目录。检测到 Inno Setup 的 `ISCC.exe` 时还会额外生成安装器；安装器的开始菜单包含独立的“卸载 DocMind”入口。

便携版升级：

```powershell
powershell -ExecutionPolicy Bypass -File installer\upgrade_portable.ps1 `
  -Package .\dist\release\DocMind -InstallDir "$env:LOCALAPPDATA\DocMind"
```

便携版卸载：双击安装目录中的 `uninstall.bat`，或运行 `uninstall_portable.ps1`。卸载器会先停止 DocMind，确认目标确实是 DocMind 安装目录后延迟删除整个便携目录；源码项目不受影响，但该目录内的索引、会话和配置也会随目录删除。需要保留数据时，请先设置独立的 `DOCMIND_STATE_ROOT` 并备份该目录。

- **引擎嵌入**：Godot / Unity / Unreal 的窗口按 Win32 HWND 规则嵌进工作台（`desktop_bridge.py`）。
  **已在 Godot 4.7.2 + 真 Win32 宿主下实机验证**（`verify_engine_embed.py` 68 项全绿）：置父与样式摘除、按客户区（或前端指定的"引擎视窗"矩形）铺排、
  宿主 resize 跟随、**真实合成键鼠（SendInput）送达引擎并回显事件**、解除嵌入后窗口原样还原、停止后无孤儿进程/窗口、父子 DPI 一致（本机 150% 缩放实测）。
  嵌入是**可逆**的：`detach` 会恢复原始父窗口、窗口样式与屏幕位置——不保存这些状态直接 `SetParent(NULL)`，窗口会带着 `WS_CHILD` 变成看不见的顶层窗口。
  试玩器里有「嵌入工作台」开关：勾上后点「桌面窗口启动」，游戏画面直接落在弹窗的引擎视窗上，工作台界面照常可用；
  另有「聚焦 / 解除嵌入 / 停止桌面窗口」；关弹窗或切走 tab 会自动解除嵌入。浏览器模式下开关自动禁用并提示需要桌面端。
  未覆盖：100%/125% 缩放的实机数据（本机显示器当前是 150%，脚本会打印 DPI 并按实际坐标断言）。
- **打包成独立 exe（onedir 目录分发）**：`docmind.spec` 一条命令产出 `dist\DocMind\DocMind.exe`，把整个 `dist\DocMind` 目录一起分发即可，目标机器无需安装 Python。构建命令：`.venv\Scripts\python.exe -m PyInstaller docmind.spec --noconfirm`（产物在 `dist\DocMind`）。
- **分发版能力边界**：分包 `builtin:py` 校验以及 `create_artifact` 的 DOCX / PDF / PPTX / XLSX 生成均在进程内完成（exe 与源码行为一致）；但 playtest 自动测试、cProfile 剖析、`python_exec` 需要真实 Python 环境，请在源码 `.venv` 里用；分区的 git 操作要求目标机器装有 Git。

## 🧭 Harness 能力（Agent 运行时）

把「能跑的 agent」补齐成「**可观测、可编排、可回归**」的运行时。以下能力都在仓库根，纯本地、无外部依赖：

| 能力 | 说明 | 入口 |
|---|---|---|
| **逐轮 trace + token 账本** | 每个回合一条 JSONL：`turn_id / session_id / messages 哈希 / 工具调用序列 / tokens in-out / 各步延迟 / finish_reason / 结局 / cost_cny`。**只记元数据不记正文**（不落 prompt/回答原文），超 8 MB 自动轮转 | 页面 **`/trace`**（截图 `docs/screenshots/trace-ledger.png`）；`GET /api/trace`、`/api/trace/summary`、`POST /api/trace/clear` |
| **会话隔离 + 持久化** | `Agent(session_id=)` 按会话隔离（传空=纯内存，行为与旧版一致）；历史落盘、超阈值把早期轮次**压成摘要**；不再共用单例导致历史串台 | `/api/chat` 的 `session_id`；`GET /api/sessions`、`DELETE /api/sessions/{id}` |
| **LLM 弹性** | 重试 + 指数退避（429 / 5xx / 超时 / 网络可重试，**4xx 明确不重试**）+ 统一 `timeout`/`deadline`；SSE 客户端断连即关闭内层生成器中止回合并记账 | `DOCMIND_LLM_*`、`DOCMIND_TURN_DEADLINE_S` |
| **评测自动化** | 黄金题 40 条（单跳 / 多跳 / 抗干扰三档）规则打分（`must_include / any_of / must_not_include / regex / must_call / 动作边界 / no_error`）+ 可选 LLM-judge + **baseline 回归门**（pass→fail 即退出码 1） | `run_golden.py` 跑题、`agent_eval.py` 打分、`agent-golden-eval` skill 的 `gate.py`（已接入冻结发布流程） |
| **原生 function-calling** | 由工具表生成 OpenAI 风格 schema；`tool_calls` 归一进文本协议后**复用全部既有护栏**（写意图 / 防重复 / 步数 / 证据兜底），事件类型不变 | `DOCMIND_TOOL_MODE=react\|native\|auto` |
| **按任务动态执行预算** | 普通问答保持较小上限；代码任务自动放宽，完整项目缺陷审查可继续核对脚本、场景、输入和 UI。观察长度同步扩展，并继续受重复调用、连续失败、总步数与 deadline 护栏约束 | `MAX_AGENT_STEPS`、`DOCMIND_CODE_MAX_STEPS`、`DOCMIND_AUDIT_MAX_STEPS`、`OBS_MAX_CHARS`、`DOCMIND_AUDIT_OBS_MAX_CHARS` |
| **多代理编排器** | 任务图 DAG（校验 + 拓扑分波 + 同波并行）+ **dispatcher/planner 动态拆解并派发任务**（主 Agent 校验后加入 DAG）+ **下游注入上游结论** + **失败自动重规划**（提案 `add / drop / replace`，只能改**尚未执行**的任务）+ 结果合成 + **子代理执行轨迹回传**给 replanner 做失败归因 | `orchestrate` 工具、`POST /api/orchestrate` |
| **成本熔断** | 按 provider/model 计价（可 `.docmind_pricing.json` 覆盖；本地 provider 恒 0）+ 全局/会话累计预算；**回合前拒绝、回合后累计** | `GET/POST /api/budget` |
| **hooks / 技能热插拔** | `.docmind/hooks/*.py` 的 `pre/post_tool`、`pre/post_turn`（单个钩子异常被隔离）；`agent_skills/*/SKILL.md` 提供内置技能，`.docmind/skills` 可同名覆盖，正文由 `dev_use_skill` 按需取 | `POST /api/hooks/reload`、`POST /api/skills/reload` |
| **办公文档产物** | 内置 `documents / pdf / presentations / spreadsheets` 四个技能；`create_artifact` 生成真实 DOCX、PDF、PPTX、XLSX，回读验证后写入当前项目 `artifacts/`，同名文件自动分配新名称 | `dev_use_skill`、`create_artifact` |
| **并行工具批次** | 一轮多条**只读**调用并发执行（结果**保序**回填）；批内只要含写/副作用工具就整批退回顺序，**绝不并发写** | `DOCMIND_PARALLEL_TOOLS`、`DOCMIND_PARALLEL_MAX` |
| **渐进式工具暴露** | 47 个工具的完整描述写进系统提示会撑爆 `PROMPT_TOKEN_BUDGET`（实测触发 `system prompt has been truncated` 并让模型输出格式崩溃）。改为默认只注入 13 个核心工具的完整用法，其余只给一行索引，按需用 `tool_search(工具名)` 取回（连同该工具的细则一起返回） | `DOCMIND_TOOL_EXPANSION=progressive`（默认 `full`，行为不变） |

### 游戏 Harness 验收闭环（当前状态）

Harness 现在可以对真实 Godot 项目执行完整验收链路：

`检索 → 需求澄清 → 动态分工 → 审批 → 并行 Subagent → 受控文件修改 → Godot headless Playtest → 证据复核`

验收 runner 默认复制项目到临时目录，源项目保持只读；报告会记录每个阶段、任务线程、工具调用、Subagent 状态、上下文压缩摘要、失败原因、耗时和 token 计数。入口是 [`agent_runtime/acceptance.py`](agent_runtime/acceptance.py)，工作流实现见 [`agent_runtime/game_workflow.py`](agent_runtime/game_workflow.py)，前端观测面板见 [`frontend/src/workbench/components/HarnessPanel.vue`](frontend/src/workbench/components/HarnessPanel.vue)。

```powershell
.\.venv\Scripts\python.exe -m agent_runtime.acceptance `
  --project D:\WorkBuddy\godot_sample `
  --godot D:\Tools\Godot\Godot_v4.7.2-stable_win64_console.exe `
  --collection docmind_code `
  --require-event player_ready `
  --json
```

上下文路由和五层压缩由 [`agent_runtime/context_router.py`](agent_runtime/context_router.py) 提供；检索生产化和离线评估分别见 [`agent_runtime/retrieval.py`](agent_runtime/retrieval.py)、[`agent_runtime/retrieval_eval.py`](agent_runtime/retrieval_eval.py)。CI 在 [`.github/workflows/harness.yml`](.github/workflows/harness.yml) 中执行工作流成功率、Recall/MRR 和可选真实项目验收门禁。

当前已在本地 Godot 样例项目验证通过；多项目、多引擎、大规模并发和长期运行评估仍属于后续生产化工作。

**三条设计取向**：① **护栏复用**——原生 FC 与并行批次都不新开执行路径，直接走既有护栏，避免"两套语义"；② **有界**——轨迹、观察、历史、批次全部截断或摘要，不让上下文与提示词爆炸；③ **不越权**——编排器只能改未执行任务（不回滚已产生的副作用）、写工具绝不并发、钩子异常一律吞掉。

快速上手（起服务后）：

```bash
# 看一眼逐轮账本（谁调了什么工具、烧了多少 token、花了多少钱、什么结局）
http://127.0.0.1:8000/trace

# 跑一张任务图（无依赖的并行、有依赖的等上游结论；失败会自动补图）
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/orchestrate -H "Content-Type: application/json" \
  -d '{"tasks":[{"id":"a","role":"researcher","task":"查 X 的实现"},
                {"id":"b","role":"reviewer","task":"评审上一结论","depends_on":["a"]}],"synth":true}'

# 设一个 5 元成本上限（本地模型恒 0 花费，不会误伤离线演示）
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/budget -H "Content-Type: application/json" \
  -d '{"limit_cny":5}'
```

新增环境变量全部列在 `.env.example`（`DOCMIND_TRACE*` / `DOCMIND_SESSION_*` / `DOCMIND_LLM_*` / `DOCMIND_TOOL_MODE` / `DOCMIND_ORCH_*` / `DOCMIND_BUDGET_CNY` / `DOCMIND_HOOKS_DIR` / `DOCMIND_SKILLS_DIR` / `DOCMIND_PARALLEL_*`）。

联网研究不会把“返回了非空页面”直接视为有效证据：搜索结果与主题明显不相关时会被标记为低相关，Agent 可继续改写关键词并切换年份、平台、地区或项目案例。研究型问题的搜索次数由 Agent 根据证据覆盖度决定，同时受 `DOCMIND_WEB_SEARCH_FAIL_LIMIT`、`DOCMIND_WEB_SEARCH_TOTAL_LIMIT` 和总步数上限约束；`web_research` 默认读取最多 5 个来源正文，可用 `DOCMIND_WEB_RESEARCH_MAX_SOURCES` 调整。

> **能力边界（不夸大）**：子代理**不共享**父上下文（靠上游结论注入传递）；重规划只改**未执行**的计划、**不回滚**已执行任务；轨迹是**有界摘要**（全文在 `.docmind_traces.jsonl`）；hooks 无沙箱（`.py` 直载，权限等同本服务）；技能本身是提示词指引，办公文件由受控的 `create_artifact` 工具执行，不运行技能内任意脚本。

## 🧪 测试与自检

```bash
# 全量回归（pytest 当前约 1247 项；MinGit 在 PATH 时 git 用例会实际执行）
.venv\Scripts\python.exe -B -m unittest discover -s tests

# Harness 工作流、检索、观测和真实项目契约测试
.venv\Scripts\python.exe -m pytest tests/test_game_workflow.py tests/test_game_workflow_e2e.py tests/test_retrieval_adapter.py tests/test_retrieval_api.py tests/test_workflow_observability.py -q

# 离线评估门禁
.venv\Scripts\python.exe -m agent_runtime.workflow_eval --self-check --baseline .github/workflow-eval-baseline.json --json
.venv\Scripts\python.exe -m agent_runtime.retrieval_eval --dataset .github/retrieval-eval.json --baseline .github/retrieval-eval-baseline.json --minimum 0.75 --json

# 场景画布 —— 后端自检：进程内起 FastAPI + 临时 Godot 工程，走真实路由，不占端口
.venv\Scripts\python.exe verify_scene_canvas.py

# 引擎嵌入 —— 实机自检（真 Godot + 真 Win32 宿主；会短暂弹窗并自动把鼠标移回原处）
.venv\Scripts\python.exe verify_engine_embed.py

# 场景画布 —— 浏览器冒烟：先在另一个终端起演示服务，再用托管 node 执行
.venv\Scripts\python.exe verify_scene_canvas.py --serve 8011
node verify_scene_canvas_ui.mjs http://127.0.0.1:8011
```

浏览器冒烟会产出 `docs/screenshots/scene-canvas.png` 与 `docs/screenshots/runtime-timeline.png`。

---

## 🗺️ 场景画布

把 `.tscn` 画成可操作的节点图。**为什么不是"把节点树画成容器套容器"**：真实场景动辄 4–6 层，容器嵌套表达"归属"还行，但场景开发真正高频的是**空间关系**（position / transform），容器根本表达不了"这几个东西该画在哪"。所以画布用**扁平节点 + 四类边**：

| 边 | 含义 | 样式 |
|---|---|---|
| `hierarchy` | 父子层级（上 → 下） | 灰色实线 |
| `script` | 节点 ↔ 脚本文件 | 紫色虚线 |
| `instance` | 节点 ↔ 被实例化的子场景 | 青色点线 |
| `reference` | 节点 ↔ 贴图 / 材质等资源 | 灰色（默认隐藏，可开） |

两种布局：**层级布局**（DFS 前序的水平树）与**空间布局**（直接按场景坐标落点，拖拽即回写 `position`）。双击文件卡在编辑器打开该脚本 / 场景；右侧检查器列出全部属性。

**安全与可逆性**（这部分才是真功夫，不是画得像就行）：

- 所有写入经双层沙箱（`code_root` 包含性 + 分区 / 契约保护）；属性值含换行会被拒（防注入）；
- 改写走"行块模型"，**写完即自检**（父节点存在 / 同级不重名 / 父块先于子块），任一项不通过就**原样回滚**；
- 每个操作都回传一条与接口请求体**同形**的 `undo`，前端原样回传即可撤销——并有回归用例保证**撤销后文件逐字节还原**（含空行位置与属性顺序）。

## ⏱️ 运行时时间线

游戏跑起来后事件写进 `.docmind_runtime.jsonl`（引擎侧按约定打印 `DOCMIND_EVENT {json}` 即被抓取）。时间线把这些事件画成多轨道时间轴：按类型分轨、按来源 / 会话 / 关键字筛选、时间缩放、数值指标曲线、导出 JSON、点事件跳代码行。清空时用**字节游标**归零，不截断正在被引擎写入的日志。

---

## 🎮 素材筛选工具（`search_assets`）

针对"做游戏/需要美术资源"的场景，新增一个可让 Agent 按需筛选素材的工具。

**为什么不直接爬素材库 API？** Kenney / OpenGameArt / itch.io 要么没有稳定的公开 API，要么被 Cloudflare 保护（爬取易失败且不礼貌）。业界做法（如 Arcane Assets MCP）同样是维护一份本地/远程的素材清单 JSON，由 Agent 在其上做检索。因此这里用一份人工精选、可持续扩充的 `assets_catalog.json`（目前含 Kenney CC0 系列 + OpenGameArt 示例共 26 条），配合 `search_assets` 的关键词展开与打分排序实现"按提问筛素材"。

- **能力**：按关键词（支持中文同义词展开，如"角色/精灵→character/sprite"）、类型（`2d-sprites`/`tilesets`/`ui`/`audio`/`fonts`/`3d`）、许可（`CC0`/`CC-BY`/`CC-BY-SA-3.0`）筛选。
- **扩展**：往 `assets_catalog.json` 追加条目即可，工具无需改动；真实接入可在 `tools.py` 中把 `search_assets` 换成对远程素材索引/商店 API 的调用。
- **演示**：
  ```bash
  curl -X POST http://localhost:8000/api/chat -F "question=帮我找一个 CC0 的角色精灵素材"
  ```
  在 mock 模式下，LLM 会自动识别"素材"意图并优先调用 `search_assets`。

## 🔧 代码执行（`python_exec`）

让 Agent 真正"动手"而非只动嘴：把自然语言意图转成可执行 Python 在受限子进程中运行，得到确定性的计算结果或数据变换产物。

- **用途**：数值/统计计算、轻量数据处理、文本变换、为可视化/下游工具生成结构化数据等。相比 `calculate`（只能做算术），`python_exec` 能跑任意 Python。
- **健壮性设计**：
  - 输入来自 LLM，模型常把代码裹在 ```` ```python ```` 围栏或加一句 "python" 提示行——工具会先用正则把这些噪音清理掉，只留可执行代码；清理后为空则直接返回「未提供有效代码」，避免把三引号当代码误执行。
  - 通过 `subprocess.run([sys.executable, "-c", code], ...)` 在当前用户权限下运行，超时 **12s**（死循环会返回超时提示而非卡死），stdout/stderr 截断到 1500 字。
  - 这是本地开发工具，**仅用于可信代码**；若要放开到不可信场景，需加沙箱/资源限额。
- **演示**：
  ```bash
  curl -X POST http://localhost:8000/api/chat -F "question=用代码算一下 1 到 100 所有平方数的和"
  ```
  Agent 会调用 `python_exec` 执行 `sum(i*i for i in range(1,101))`，返回 `338350`。

## 🌐 联网搜索（`web_search`）

当知识库不足，或问题有时效性/外部信息时，Agent 可自主联网补充证据，而不是只会"答不知"。

- **实现**：直接抓取 DuckDuckGo HTML 结果页（`urllib.request` + 正则提取标题/摘要/跳转链接，并还原 `uddg=` 编码后的真实链接），**无需 API Key**。
- **降级**：若运行环境访问不了外网，或 DDG 改版导致未解析到结果，工具会返回明确原因（如"搜索失败: …（请确认运行环境能访问外网，或换用带 Key 的搜索引擎）"），Agent 据此改用知识库或坦诚告知，不会假装搜索成功。
- **可替换**：只需改 `tools.py` 里的 `web_search` 一个函数，即可换成 SerpAPI / Bing 等带 Key 的方案，Agent 侧零改动。
- **演示**：
  ```bash
  curl -X POST http://localhost:8000/api/chat -F "question=查一下 MiniMax H3 模型支持的最大视频时长"
  ```

## 🎬 视频提示词生成（`gen_video_prompt`）

把一句中文创意，转成符合 **MiniMax H3** 三段结构、可直接粘贴进 ComfyUI `MiniMaxH3ImageToVideo` 节点的视频提示词——打通"文字创意 → 可生成视频"的链路。

- **为什么是确定性拼装 + LLM 润色**：早期让模型直接写，弱模型（如 7B）会把"三段结构"理解成"三幕结构"而输出错误格式。改为**三段骨架（integrated_multimodal_description / overall_soundscape / non_diegetic_music）由工具确定性拼装**，保证永远合规；LLM 只负责润色 Shot 1 的英文分镜描述，失败时回退到确定性模板句。无论模型强弱都产出可用提示词。
- **声音设计自动贴合场景**：根据关键词（雨/夜/风/海/城/森/雪…）自动填充画内音（diegetic），并附非画内配乐模板，输出带约束说明（无负面词、FPS=24、时长 4–15s 等）。
- **演示**：
  ```bash
  curl -X POST http://localhost:8000/api/chat -F "question=给我生成一个雨夜城市的视频提示词"
  ```
  Agent 调用 `gen_video_prompt`，输出含 `integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music` 三段结构，画内音自动带 "steady rain" + "distant night traffic" + "low city hum"。

## 💻 代码问答模式（`search_code` / `read_file` / `grep`）

把 Agent 从"只能读文档"升级为"也懂你的工程代码"：索引一份源代码目录后，它能检索函数/类、打开具体文件、按正则定位符号或报错——这正是做游戏/大项目时防止 AI「代码堆叠、凭空幻觉」的关键：让 AI 先检索到已有结构，再精准改某一处，而不是往一个文件里硬塞。

- **怎么用**：把代码根目录交给 DocMind 索引一次即可（之后所有代码问答都基于这份索引）：
  ```bash
  curl -X POST http://localhost:8000/api/ingest_code -F "root=C:/path/to/your/game-project"
  ```
  也可在 `.env` 里固定 `CODE_ROOT`，启动后自动可用。
- **三个工具**：
  - `search_code(query)`：在已索引源码/配置中检索相关函数、类、配置片段（按符号名+路径呈现），回答"某功能在哪实现/某函数做什么"。
  - `read_file(path)`：读取代码库某个文件（path 相对 `code_root`），需要看完整实现时用。
  - `grep(pattern)`：按正则在代码库里搜文本/符号，返回 `文件:行号: 内容`，定位某变量/某报错位置时用。
- **设计要点**：
  - **代码感知分块**：散文按段落切即可，代码必须按「文件 + 函数/类」切——否则一个函数被腰斩、两个不相关的函数被粘成一段，检索质量会崩。Python 用 `ast` 精确拿到每个 `def/class`（含方法）的起止行与名字；其它语言用正则启发式识别定义行。切片过大时再按行兜底切。
  - **独立集合**：代码存进单独的 Chroma 集合（`docmind_code`），与文档集合互不污染；`search_knowledge` 管文档，`search_code` 管代码。
  - **路径沙箱**：`read_file` / `grep` 只允许访问 `code_root` 之内，越界访问会被拒绝，不会误读本机其它文件。
  - **跳过噪音**：索引时自动跳过 `.git` / `node_modules` / `__pycache__` / `.venv` 等目录，并跳过超大文件（>500KB），避免把构建产物/压缩包塞进索引。
- **演示**：
  ```bash
  # 索引后，问 Agent：
  curl -X POST http://localhost:8000/api/chat -F "question=伤害计算是在哪个函数里实现的？把那段代码给我看看"
  # Agent 会先 search_code 定位 DamageCalculator.compute，再用 read_file 打开具体文件。
  ```
- **注意**：代码问答依赖语义检索，建议使用真实 embedding（`.env` 设 `EMBEDDING_PROVIDER=qwen`）；离线 `local` 模式向量为随机占位，仅用于跑通链路、检索结果不具语义意义。切换 embedding 维度时只需重建文档集合，代码集合可独立保留。
