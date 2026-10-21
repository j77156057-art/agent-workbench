# DocMind · 面向工程代码的 Agent 运行时（Harness）

一个**可离线运行的 Agent 运行时**：把「能跑的 agent」补齐成**可观测、可编排、可回归**的工程系统。
上层落地为一个本地工程工作台（代码问答 / 受控改写 / 场景与运行时可视化）——**但项目主体是 Harness，工作台只是它的落地场景**。

**要解决的两个失败模式**：

1. **幻觉**——AI 凭印象说"伤害计算在 player.py"，其实没有这个文件；
2. **失控**——改完不知道改了什么、烧了多少 token、下次会不会再犯。

应对方式是把 agent 拆成六层，逐层补上工程护栏（逐项详解见「Harness 能力」章节）：

| 层 | 职责 | 关键实现 |
|---|---|---|
| **E 执行循环** | ReAct + 反思重试、原生 function-calling、plan 模式、子代理委派、并行只读批次、DAG 多代理编排与失败重规划 | `agent.py` · `orchestrator.py` |
| **T 工具注册** | **125 个工具**；参数校验、域名白名单、**受控写**（先读后写 + 体积上限 + 语法校验 + 人工确认） | `tools.py` · `agent_runtime/tool_domains.py` |
| **C 上下文管理** | prompt token 预算、观察截断、轨迹裁剪（保 system 与当前问题）、历史滚动摘要 | `agent.py::_fit_budget` |
| **S 状态存储** | 逐轮 trace 账本（**只记元数据不记正文**）、会话隔离与持久化、跨重启恢复 | `agent_trace.py` · `sessions.py` |
| **L 生命周期钩子** | 钩子 / 技能热插拔、人工审批门、按 provider 计价的成本熔断 | `hooks.py` · `skills.py` · `pricing.py` |
| **V 评估接口** | 黄金题规则打分 + 可选 LLM-judge + **baseline 回归门**（回归即退出码 1） | `agent_eval.py` · `run_golden.py` · `golden/` |

**三条设计取向**：① **护栏复用**——原生 FC 与并行批次都不新开执行路径，直接走既有护栏，避免"两套语义"；② **有界**——轨迹、观察、历史、批次全部截断或摘要，不让上下文与提示词爆炸；③ **不越权**——编排器只改未执行任务、写工具绝不并发、钩子异常一律吞掉。

**零 API Key 可跑**：默认 provider 就是 `mock`（离线），也可换本地 Ollama / llama.cpp 或任意 OpenAI 兼容云端。

![DocMind · Agent Harness 六层架构](docs/architecture.svg)

---

## 现在的形态（2026-10）

| 规模 | 数字 | 出处 |
|---|---|---|
| Agent 工具 | **125** 个，分 **16** 个 domain | `tools.py` 的 `TOOLS`（121）+ `agent.py` 导入期注入的 4 个（`orchestrate` / `delegate` / `dev_use_skill` / `tool_search`） |
| HTTP 端点 | **256** 条 | `api.py` 137 + `api_routes/` 119（agent / engine / gpu / eda / projects / resources） |
| 后端测试 | **5702** 项 | `tests/` 397 个文件 |
| 前端测试 | **129** 项 | `frontend/tests/*.test.mjs` + `frontend/src/**/*.test.ts` |
| 前端界面 | **57** 个组件 / **29** 个 composable | `frontend/src/workbench/` |

> 2026-09 之后新增的能力（此前 README 完全没有）：
>
> - **内置浏览器**（`browser_action`）——只许公网，与只许回环的 `dev_page_action` **会话域互不相通**；护栏是协议 / 主机名后缀 / IP 段 / DNS 解析 / 导航后复核五道闸。
> - **视觉定位 + 视觉点击**（`vision_locate_click` / `vision_click`）——**故意拆成两个工具**：合成一个，模型就会以为"定位成功 = 可以点"。执行侧走审批台账，模型自称确认不算数。
> - **引擎控制**（`engine_start` / `engine_stop` / `engine_reload`）——与 HTTP 端点同一条底层路径，共用 GPU 租约与引擎日志。
> - **EDA 验收**（`eda_run_script` / `eda_report_gate`）——真跑 Vivado / KLayout / 仿真并解析成结构化事实；**只判不回滚**，真实回滚仍走人工审批门。
> - **产物通道**——模型写的文件、截的图**直接摆在对话栏**（图片给缩略图，文件给可点的名字），不用再自己去路径里搜。
> - **任务域**（校验 / 快照 / 回滚 / 性能剖析）、**实时对话**（画面+语音经 WebSocket 送入本地实时模型）、**语音模型界面化配置**（STT / TTS / Omni 不必再改 .env 重启）、**GPU 监控**、**AI 运行台**、**PTC 写侧 bridge**。
> - **关系图改成分层继承树**（原先是力导向，1245 条继承边画成毛球）。

## 落地场景：本地工程工作台

上层是一个**本地、单人**的工程工作台（以游戏 / Mod 工程作为验证场景）：任务分区、每区独立 Git、契约方向校验、变更集回滚、选区 AI、符号 / 关系图、引擎嵌入、场景画布、运行时时间线、GPU 协调。

它存在的意义是给 Harness 提供一个**真实、复杂、有副作用**的落地环境——**不是通用 ALM 平台：不做多用户协作与角色权限**（本机默认免鉴权，可按需开启单 token 校验）。

### 工作台现在有哪些面板

| 入口 | 干什么 |
|---|---|
| **AI 助手**（常驻对话台） | ReAct agent + SSE；答案里的文件引用、**产物卡**（模型写下的文件、截到的图）直接可点；含模型切换、审批门、孤儿工作流恢复 |
| **代码地图** | 跨文件按分区浏览全部符号，名称 / 签名 / 文档实时搜索，点符号跳到行 |
| **关系图** | 全项目类 / 脚本 / 场景 + 三类有向边。**左→右分层继承树**（不是力导向）；大扇出折叠成「N 个子类」卡，点开才展开；搜索只看相关子树 |
| **Unity 图** | Unity GUID 引用图，解析不到的 guid 聚为「缺失 / 外部」节点并标红（仍是力导向布局） |
| **流程图** | 把每轮问答画成纵向流水线：提问 → AI 决策 → 调工具 → 组织回答，节点带耗时 / token / 花费，失败标红 |
| **改动总览** | 仓库级未提交改动清单（只读，按仓库分列） |
| **任务与生成** | 任务分支、Godot 控制、ComfyUI 画图、Unreal 资产 |
| **GPU 监控** | 每卡显存 / 利用率 / 温度、近 20 分钟占用曲线、持有者强制回收、FIFO 排队取消、Ollama 空闲卸载 |
| **EDA 多模态验收** | 结构化报告解析 × 视觉回归 → 三态合并门（met / unmet / uncertain） |
| **实时对话** | 摄像头 / 屏幕共享画面与语音经 WebSocket 送入本地实时模型 |
| **运行与引擎** | 探测项目引擎 → 选引擎 → 探可执行文件 → 写项目配置 |
| **AI 运行台** | 历史会话、逐轮轨迹、技能与断点 |
| **分区图** | 依赖方向 DAG（按依赖深度分层）+ 每区状态卡 + 契约校验横幅 |
| **场景画布 / 运行时时间线** | Godot `.tscn` 可视化 + 可编辑画布；游戏运行事件的多轨道时间轴 |
| **素材中心** | Poly Haven 单件素材、Kenney 素材包、我的素材库、ComfyUI 生成 |
| **统一设置页** | 用量与费用、联网搜索、MCP 连接器、Agent 策略、模型、运行时、外观；**语音模型（STT / TTS / Omni）也在页内配** |

> **MCP 引擎桥接** 与 **Web 试玩导出** 的产品级使用文档与边界说明见 [`docs/integrations.md`](docs/integrations.md)。设置页搜索 `EDA` / `PCB` / `KiCad` 时可联网发现真实 MCP Registry 候选；用户选择并测试连接后，系统从真实 `tools/list` 生成能力与表单候选，**必须人工批准**才加入 Agent 路由。搜索摘要不会变成可执行命令；测试本地候选时可能首次下载对应 MCP 包。

## 🧱 技术栈

- 后端：Python · FastAPI（HTTP + SSE）· Chroma 多集合（文档 / 代码 / 经验）· OpenAI 兼容多 Provider（qwen / deepseek / ollama / llamacpp / mock）· PyInstaller + pywebview
- 前端：Vue 3.5 · Vite 5 · TypeScript · CodeMirror 6 · Vue Flow · 手写深色设计系统
- 验证：后端 `pytest` **5702 项** · 前端 node 测试 **129 项** · 场景画布自检 · 浏览器冒烟（Playwright + 系统 Edge）· 引擎嵌入实机自检
- 评测：黄金题库 40 题（单跳 / 多跳 / 抗干扰三档）+ baseline 回归门，跑题用 `run_golden.py`、打分用 `agent_eval.py`
- CI：GitHub Actions（`.github/workflows/harness.yml`）——依赖安装 → 前端构建 → 前端测试 → 编译检查 → 评测门 → 变异测试 → 检索回归 → 离线工作流验收 → 全量 pytest → Postgres 严格集成 → 真实 LLM 冒烟

## 📁 目录结构

```
rag-agent/
├── api.py                 # HTTP / SSE 总入口（137 条路由：chat / turn / 工作台 fs / mcp / regions /
│                          #   engine / desktop-host / selection-ai / scene / runtime / GPU /
│                          #   trace / sessions / budget / hooks / skills / voice / vision …）
├── api_routes/            # 领域路由（119 条）：agent（工作流 37+）· engine（33）· gpu（12）·
│                          #   eda（6）· projects（5）· resources（素材与 ComfyUI）
├── agent.py               # ReAct 循环、反思重试、代码优先路由、证据护栏；run/_run 埋点外壳；
│                          #   原生 function-calling、plan 模式、子代理委派、并行批次
├── tools.py               # 121 个工具注册表（16 个 domain，见 agent_runtime/tool_domains.py）
├── agent_runtime/         # 运行时契约与能力实现：工具分类、浏览器、视觉点击、引擎控制、
│                          #   预览适配器、上下文路由、工作流、验收、区域…（60+ 模块）
├── api_routes/../applications/
│   └── developer/         # 开发问答 / 游戏工作台的应用边界
├── regions.py             # 分区 2.0：声明式配置、契约校验（DAG 无环 / 导出存在）、变更集与回滚
├── scene_runtime.py       # 场景画布内核：.tscn 行块解析 → 图模型 → 受控编辑（可回滚 + 可撤销）
├── workbench_fs.py        # 沙箱文件树、读写、git 状态 / 历史 / 回滚、符号地图、关系图
├── symbols.py             # 多语言符号抽取 + 代码感知分块
├── game_workbench.py      # 引擎 catalog / 启停 / 嵌入、任务 / 资产 / bug 工作流、审批台账
├── engine_adapters.py     # 引擎适配薄封装
├── desktop_bridge.py      # Win32：查找宿主窗口 / SetParent 嵌入 / resize / focus（纯 ctypes）
├── desktop.py             # 桌面启动器（单实例保护 + pywebview 窗口，失败即回退浏览器）
├── gpu_coordinator.py     # GPU 租约队列（TTL 过期 / 移交 + 显存阈值）
├── mcp_client.py          # MCP 桥接（用法与边界见 docs/integrations.md）
├── web_export.py          # Godot Web 导出与本地试玩
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
├── dependency_bootstrap.py # 空白电脑的依赖自举：建 .venv、装 requirements、装前端并构建 web/
├── golden/                # 黄金题库与基线结果：questions_v2.json（40 题）/ results_baseline.jsonl
├── frontend/              # Vue 工作台（构建产物输出到 ../web）
│   └── src/workbench/
│       ├── components/    # 57 个组件：ChatDock / RelationGraph / SceneCanvas / HarnessPanel …
│       ├── composables/   # 29 个：useChatStream / useMessageRender / useWorkflowCard …
│       └── lib/           # 纯函数模块（可被 node 单测直接跑）：关系图分层树、产物展示口径
├── tests/                 # 后端 pytest 5700 项
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

**空白电脑只需要两样东西：Python ≥ 3.11 和 Node ≥ 22**（Node 只在需要构建前端时用得上）。

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
cd frontend && npm ci && npm run build      # 产物 -> ../web

# 5. 打开
#   工作台 : http://127.0.0.1:8000/workbench
#   问答页 : http://127.0.0.1:8000
```

> 服务地址请用 `127.0.0.1` 而不是 `localhost`（本机 IPv6 解析会连不上）；路径含单引号用户名时 shell 一律用双引号包裹。
>
> `frontend/.npmrc` 里有 `min-release-age=2`：npm 不会安装**发布不足 2 天**的包版本。
> 这是供应链护栏（挡"刚发就被投毒"的版本），代价是极端情况下依赖解析结果与别处不同。

**不想手动敲这四步？** 直接跑 `启动DocMind.bat`（Windows）——首次启动会自动建 `.venv`、装 `requirements.txt`、装前端锁定依赖并构建 `web/`，之后每次启动直接可用。

### 空白电脑自检清单

按下面顺序走一遍，就知道"换台新机器能不能跑起来"：

| # | 检查 | 命令 / 预期 |
|---|---|---|
| 1 | Python 版本够 | `python -V` → ≥ 3.11（`dependency_bootstrap.py` 会在**装依赖之前**就拦下老版本并说清楚要装哪个） |
| 2 | Python 依赖装得上 | `pip install -r requirements.txt` → 无报错（国内加 `-i https://mirrors.aliyun.com/pypi/simple/`） |
| 3 | 前端依赖装得上 | `cd frontend && npm ci` → 依据 `package-lock.json` 精确还原（Node ≥ 22，见 `frontend/.nvmrc` 与 `package.json` 的 `engines`） |
| 4 | 前端能构建 | `npm run build` → 产物落到 `../web`（`web/workbench.html` + `web/assets/`） |
| 5 | 后端能起 | `python -m uvicorn api:app --port 8000` → `GET /api/health` 返回 `{"ok":true}` |
| 6 | 不用配置也能用 | 不建 `.env` 直接起 → provider 默认 `mock`，`POST /api/chat` 能拿到完整 SSE 回答 |
| 7 | 页面能打开 | `/`（问答页）与 `/workbench`（工作台）都 200，且 `/workbench` 引用的 `assets/*.js` 都能取到 |

> 第 6 步是「开箱即用」的底线：**没有 `.env`、没有 API Key 也能问答**（走本地 mock 模型）。`.env.example` 只是把可选项摊开给你看，不是必填清单。
> 想接真实模型再改 `LLM_PROVIDER` / `LLM_API_KEY`，或直接用页面里的「⚙ 模型设置」——保存后立即生效，不用重启。

### 用真实模型（以通义千问为例）

两种方式任选其一：

- **页面内切换（推荐，免重启）**：打开问答页点右上角「⚙ 模型设置」，选 provider、填 Key、点「保存并切换」即时生效；provider、模型、自定义接口地址和 embedding 选择会跨重启恢复。API Key 不写入普通状态文件，而是按项目存入本机受保护密钥库，启动时自动加载（切换项目后使用对应项目的密钥）。Key 输入框留空表示"不修改已有 Key"；如果升级/切换启动方式后提示"无法解密"，在当前用户下重新保存一次即可完成迁移。
- **改 `.env`**：`LLM_PROVIDER=qwen` / `EMBEDDING_PROVIDER=qwen` / `DASHSCOPE_API_KEY=...`，改完重启服务。

```bash
# 不依赖前端直接调 API
curl -X POST http://127.0.0.1:8000/api/chat -F "question=DocMind 支持哪些文件格式？"
```

### 图片 / 视频视觉链路

工作台的 ChatDock 支持选择、粘贴或拖拽图片。模型能力画像会区分"模型原生识图"和"Harness 视觉辅助"：原生视觉模型直接收到图片；文本模型不会被误传图片，而是由已配置的视觉模型生成有界观察，再交给当前 Agent 结合代码和日志复核。视频入口会用 imageio/ffmpeg 抽取关键帧，生成时间轴观察后填回对话输入，适合检查游戏启动画面、HUD 重叠、摄像机越界和长时间游玩中的可见异常。

会话历史与一键切换在 ChatDock 顶部「会话」菜单：「新对话」会创建新的隔离 `session_id`，旧对话仍保留。列表用首条用户问题作为标题，切换后会回灌完整问答；会话内容按项目持久化在 `.docmind_sessions/`，服务重启后仍可继续。

```dotenv
# 可选：为不支持视觉的主模型配置本地视觉兜底（默认不出网）
DOCMIND_VISION_PROVIDER=ollama
DOCMIND_VISION_MODEL=qwen3-vl:8b
# DOCMIND_VISION_BASE_URL=http://127.0.0.1:11434/v1
# DOCMIND_VISION_API_KEY=
CHAT_VIDEO_MAX_BYTES=209715200
CHAT_VIDEO_MAX_FRAMES=8
```

打开"模型设置"可分别确认深度思考、图片识别和视频识别能力；确认结果按 `provider/model` 持久化。`unknown` 不等同于"不支持"，而是要求 Harness 使用已配置的视觉模型，或等待用户确认后再把原图交给当前模型。

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

脚本会同步构建依赖、构建 PyInstaller onedir 包、写入 `install-manifest.json`，并把 `uninstall.bat`、`upgrade_portable.ps1` 和卸载脚本放进发布目录。检测到 Inno Setup 的 `ISCC.exe` 时还会额外生成安装器；安装器的开始菜单包含独立的"卸载 DocMind"入口。

便携版升级：

```powershell
powershell -ExecutionPolicy Bypass -File installer\upgrade_portable.ps1 `
  -Package .\dist\release\DocMind -InstallDir "$env:LOCALAPPDATA\DocMind"
```

便携版卸载：双击安装目录中的 `uninstall.bat`，或运行 `uninstall_portable.ps1`。卸载器会先停止 DocMind，确认目标确实是 DocMind 安装目录后延迟删除整个便携目录；源码项目不受影响，但该目录内的索引、会话和配置也会随目录删除。需要保留数据时，请先设置独立的 `DOCMIND_STATE_ROOT` 并备份该目录。

- **引擎嵌入**：Godot / Unity / Unreal 的窗口按 Win32 HWND 规则嵌进工作台（`desktop_bridge.py`）。
  **已在 Godot 4.7.2 + 真 Win32 宿主下实机验证**（`verify_engine_embed.py` 全绿）：置父与样式摘除、按客户区（或前端指定的"引擎视窗"矩形）铺排、
  宿主 resize 跟随、**真实合成键鼠（SendInput）送达引擎并回显事件**、解除嵌入后窗口原样还原、停止后无孤儿进程/窗口、父子 DPI 一致（本机 150% 缩放实测）。
  嵌入是**可逆**的：`detach` 会恢复原始父窗口、窗口样式与屏幕位置——不保存这些状态直接 `SetParent(NULL)`，窗口会带着 `WS_CHILD` 变成看不见的顶层窗口。
  试玩器里有「嵌入工作台」开关：勾上后点「桌面窗口启动」，游戏画面直接落在弹窗的引擎视窗上，工作台界面照常可用；
  另有「聚焦 / 解除嵌入 / 停止桌面窗口」；关弹窗或切走 tab 会自动解除嵌入。浏览器模式下开关自动禁用并提示需要桌面端。
- **打包成独立 exe（onedir 目录分发）**：`docmind.spec` 一条命令产出 `dist\DocMind\DocMind.exe`，把整个 `dist\DocMind` 目录一起分发即可，目标机器无需安装 Python。
- **分发版能力边界**：分包 `builtin:py` 校验以及 `create_artifact` 的 DOCX / PDF / PPTX / XLSX 生成均在进程内完成（exe 与源码行为一致）；但 playtest 自动测试、cProfile 剖析、`python_exec` 需要真实 Python 环境，请在源码 `.venv` 里用；分区的 git 操作要求目标机器装有 Git。

## 🧭 Harness 能力（Agent 运行时）

把「能跑的 agent」补齐成「**可观测、可编排、可回归**」的运行时。以下能力都在仓库根，纯本地、无外部依赖：

| 能力 | 说明 | 入口 |
|---|---|---|
| **逐轮 trace + token 账本** | 每个回合一条 JSONL：`turn_id / session_id / messages 哈希 / 工具调用序列 / tokens in-out / 各步延迟 / finish_reason / 结局 / cost_cny`。**只记元数据不记正文**（不落 prompt/回答原文），超 8 MB 自动轮转 | 页面 **`/trace`**；`GET /api/trace`、`/api/trace/summary`、`POST /api/trace/clear` |
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
| **渐进式工具暴露** | 125 个工具的完整描述写进系统提示会撑爆 `PROMPT_TOKEN_BUDGET`（实测触发 `system prompt has been truncated` 并让模型输出格式崩溃）。改为默认只注入核心工具的完整用法，其余只给一行索引，按需用 `tool_search(工具名)` 取回（连同该工具的细则一起返回） | `DOCMIND_TOOL_EXPANSION=progressive`（默认 `full`，行为不变） |
| **能力租约** | 读项目 / 读外部 / 写项目 / 写外部 / 执行 / 联网 / 管理七类能力**分别租用**；`read_external` 单独记账，模型"顺手读了外部文件"能被查出来 | `agent.py` 的 `Capability` 租约 |
| **产物通道** | 工具在**真正落盘那一刻**自报产物（路径 / 标签 / 大小 / 证据链），随 observation 事件下发；对话栏直接渲染成产物卡——**图片给缩略图，文件给可点的名字**，不用再去路径里搜。写失败的不算产物 | `agent.py` 的 observation 事件、`ArtifactCards.vue` |

### 工具全景：16 个 domain / 125 个工具

分类是**声明式**的（`agent_runtime/tool_domains.py`），不是按名字猜的——新增工具不声明会红。完整清单用 `tool_catalog` 工具现查。

| domain | 覆盖 |
|---|---|
| `files` | 项目内文件读写、按内容/符号/glob 检索、引用点定位、原子批量改写、白名单目录外的读写删 |
| `verify` | 跑测试、CI 结论、契约校验、依赖对账、索引重建、变更集回滚、审批状态查询 |
| `frontend` | 起 dev server、真实浏览器里点/填/读、桌面截屏与操作、**视觉定位 + 视觉点击**、Vue 组件级与应用级验收 |
| `preview_adapter` | 为新文件类型设计声明式预览适配器：草稿 → 审批激活 → 受控刷新 → 回滚 → fixture 测试 |
| `mcp` | 连接器配置与探活、调用、官方 Registry 检索、**从自然语言需求发现候选**、能力审批、隔离安装与审计 |
| `plan` | 计划、区域提议与落地、分区读写校验、**工作流启动**、通道、交接、定价、重构 |
| `task` | 缺陷登记与更新、技能创建与审批、资产登记与素材目录 |
| `git` | diff、log、单区提交、全部提交并记变更集 |
| `data` | 数据文件识别与生成、**DOCX/PDF/PPTX/XLSX 产物**、数值计算、视频提示词 |
| `jobs` | 跑命令、跑 Python（受限子进程）、看与取消后台任务 |
| `web` | 搜索、抓正文、批量研究、字幕、翻译、天气、**内置浏览器**（只许公网） |
| `knowledge` | 检索已上传资料、记忆存取、召回经验 |
| `game` | 引擎启停与热重载、引擎截图、数值模拟、影响面、发布前检查、任务登记 |
| `eda` | **真跑** Vivado/KLayout/仿真并解析输出、在线报告 → 结构化事实 → 三态验收门 |
| `net` | 受白名单约束的 HTTP 请求（防 SSRF） |
| `catalog` | 按大类查工具清单与用法 |

### 新接给模型的能力

这批是 2026-09 之后才有的，实现都在 `agent_runtime/`：

- **内置浏览器**（`browser_action`）——只许公网。协议 / 主机名后缀 / IP 段 / DNS 解析 / 导航后复核**五道闸**。
  与只许本机回环的 `dev_page_action` **会话域互不相通**（一个域一个会话）。
- **视觉定位 + 视觉点击**（`vision_locate_click` / `vision_click`）——**故意拆成两个工具**：
  合成一个，模型就会以为"定位成功 = 可以点"。执行侧的放行条件是**审批台账里有人批过**，
  模型自称确认不算数；HTTP 出口则要求前端回传 `confirmed=true`。
- **引擎控制**（`engine_start` / `engine_stop` / `engine_reload`）——与 `POST /api/engine/start|stop|reload`
  同一条底层路径（`game_workbench`），共用 GPU 租约与引擎日志。
- **EDA 验收**（`eda_run_script` / `eda_report_gate`）——`eda_run_script` 真跑脚本并解析输出（fail-closed，
  工具没装就返回 `tool_unavailable`）；`eda_report_gate` 把在线报告解析成结构化事实，
  给 **met / unmet / uncertain 三态结论**。**只判不回滚** —— 真实回滚仍走人工审批门。

### 游戏 Harness 验收闭环

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

### 工作流：两套并存，别混

1. **开发工作流**（跨窗口持久 + 人工门）：入口工具 `start_workflow`，HTTP 面 `/api/agent/workflow/**` 共 37+ 端点，
   事件流 `GET /api/agent/workflow/{id}/events`（SSE，含重放 + 心跳）。
   状态机：**方案选择 → 计划审批 → 执行 → 分阶段 apply/review/cleanup → 验收 → 视觉反馈 →
   checkpoint / project-checkpoint / rollback → interrupt / resume / revise**。`kind` 支持 `generic|game|eda`。
2. **工具流编排器** `/api/flows`：列表 / 保存 / 删除 / run-step（`flows.py`）。

前端：对话流内的 `WorkflowCard.vue`、人工断点模态 `WorkflowGateDialog.vue`、产物与视觉反馈 `WorkflowPreview.vue`、
成员抽屉 `WorkflowMembersDock.vue`、历史 `WorkflowHistoryPopover.vue`。后端可选 LangGraph（未装时回退确定性 manager）。

### 审批与权限

- **人工审批门** `require_approval(root, action, target)`：写工具安装、preview adapter 回滚、技能回滚、**视觉点击**等都要过。
- **审批台账**：追加写 `.docmind/approvals.jsonl`，可查可查。HTTP 面 `/api/agent/approvals`、`/approvals/decide`、
  `/approval-requests`、`/approval-requests/decide`；旧的写 / 执行待确认轨 `/api/pending_edits`、`/api/pending_execs`。
- **分级执行确认**：`DOCMIND_EXEC_APPROVAL` / `TIER2_CONFIRM` / `TIER3_CONFIRM`（第三档要连点 3 次）。
- **区域治理**：`regions.py` —— 分区独立 git + 依赖单向 DAG + 导出桩 + 契约校验器；
  Agent 越区写直接拒。`dev_approve` **Agent 不能自批**。
- **EDA 侧只判不执行**：HTTP 层开一个"自动 revert"入口 = 静默放行风险，所以没有这个口子。

## 🧪 测试与自检

```bash
# 后端全量回归
.venv\Scripts\python.exe -m pytest tests -q

# 前端行为测试（node 直跑生产模块，129 项）
cd frontend && npm run test:node

# 场景画布：后端自检 + 浏览器冒烟
.venv\Scripts\python.exe verify_scene_canvas.py
node verify_scene_canvas_ui.mjs
```

**离线评估门禁**：场景画布、EDA 视觉回归、真实路由（进程内起 FastAPI + 临时 Godot 工程，真实路由，不占端口）。
**引擎嵌入**：实机自检（真 Godot + 真 Win32 宿主：短暂窗并自动把鼠标移回原处）。
**场景画布**：浏览器冒烟（先在另一个终端起服务，再执行 node 执行）。
**变异测试电池**：改坏实现，门禁必须抓到。

## 🎮 场景画布

Godot `.tscn` 的**可视化 + 可编辑**画布：层级树 / 空间坐标两种布局，节点父子层级、实例（instance）、
position / transform、资源引用一目了然；异步加载、实际项目修改和面板尺寸变化后会自动重新适配视口，
解析为空时给出明确诊断；新增 / 删除 / 改名 / 换父 / 复制 / 改属性 / 拖拽写回位置，
**全部可撤销，且撤销能逐字节还原文件**。

## ⚠️ 运行时红线

- Agent **不许**给自己批权限（`dev_approve` 只认工作台里人的操作）。
- 写工具**绝不并发**；并行批次里只要有写 / 副作用工具，整批退回顺序执行。
- 编辑器**默认整文件读**，区间读是例外而不是默认。
- 钩子异常一律吞掉，不许把用户的项目搞挂。
- 编排器只能改**尚未执行**的任务，不回滚已产生的副作用。

## 📚 配置项

`.env.example` 是可选项的摊开清单（**不是必填**）。分组：

| 组 | 代表变量 |
|---|---|
| LLM Provider | `LLM_PROVIDER`（`mock` / qwen / deepseek / ollama / llamacpp）、`LLM_MODEL`、`LLM_API_KEY` |
| Harness 视觉辅助 | `DOCMIND_VISION_PROVIDER/MODEL/BASE_URL/API_KEY`、`DOCMIND_VISION_ALLOW_UNVERIFIED` |
| Embedding | `EMBEDDING_PROVIDER`、`EMBEDDING_MODEL` |
| 路径与检索 | `CHROMA_DIR`、`CHUNK_SIZE`、`TOP_K`、`DOCMIND_RETRIEVAL_MODE`（dense/hybrid/hybrid_rerank） |
| Agent 账本 / 会话 / 弹性 | `DOCMIND_TRACE`、`DOCMIND_LANGSMITH_*`、`DOCMIND_CHECKPOINT_POSTGRES_DSN`、`DOCMIND_SESSION_*` |
| 语音 STT / TTS | `DOCMIND_STT_URL/API_KEY/MODEL`、`DOCMIND_TTS_URL/API_KEY/MODEL/VOICE` |
| 实时多模态 Omni | `DOCMIND_REALTIME_PROVIDER`、`DOCMIND_OMNI_API_KEY/MODEL/VOICE` |
| LLM 调用 / 联网预算 | `DOCMIND_LLM_TIMEOUT/RETRIES`、`DOCMIND_WEB_SEARCH_FAIL_LIMIT` |
| 工具通道 / 子代理 / 计划 | `DOCMIND_TOOL_MODE`、`DOCMIND_SUBAGENT_MAX_DEPTH`、`DOCMIND_PARALLEL_TOOLS` |
| 成本熔断 | `DOCMIND_BUDGET_CNY`、`DOCMIND_SESSION_BUDGET_CNY` |
| 热插拔目录 | `DOCMIND_HOOKS_DIR`、`DOCMIND_SKILLS_DIR` |

**几类配置不在 `.env` 里**：

- **联网搜索 / URL 抓取的 provider**（`WEB_SEARCH_PROVIDER`、`WEB_FETCH_PROVIDER`、`EXTERNAL_API_ALLOWLIST` 等）
  只在 `config.py` 里读 —— 改它们要改环境变量，`.env.example` 里**没有列**。
- **审批 / 权限模式**（`DOCMIND_EXEC_APPROVAL`、`EDIT_CONFIRM`、`DOCMIND_ACCESS_MODE`）。
- **MCP 连接器 / 区域治理 / 引擎连接器**：走**应用内设置页 + 落盘文件**（`/api/config`、`/api/agent/connectors`、`/api/engine/config`、`regions.json`），不走 `.env`。

## 🔌 集成文档

- [`docs/integrations.md`](docs/integrations.md) —— MCP 引擎桥接、Web 试玩导出的产品级用法与边界。
- [`docs/known-defect-*.md`](docs/) —— 已定位缺陷与口径说明（避免同一个坑被重新踩一遍）。
