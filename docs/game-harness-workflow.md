# 开发工作流 Harness（通用 / 游戏 / EDA）

工作流由 `agent_runtime.game_workflow.GameWorkflowManager` 管理。安装 LangGraph 时，choice、research、approval 三个人工门通过同一条带持久化 SQLite checkpointer 的真实线程使用 `interrupt()/Command(resume=...)` 暂停和恢复；`execute_wave → review → replan → execute_wave` 也由该状态图驱动，每个执行波再通过 LangGraph `Send` 把无依赖的 Subagent 扇出为独立节点。SQLite saver 不可用时降级为内存 checkpoint；LangGraph 不可用时使用原生编排器兼容执行，因此不会阻断 Harness。

## 领域画像（kind）与对话入口

工作流不局限于游戏。`agent_runtime.workflow_profiles` 注册三种领域画像，由工作流状态的 `kind` 字段区分（旧持久化状态无该字段时迁移为 `game`，不强制写回）：

- `generic`（默认，通用开发）：方案与任务文案领域无关，兜底任务链为 research → implement → verify；适用于软件、数据工程、工具链和自动化等任意长链路任务。
- `game`（游戏开发）：保持既有 2D/3D 澄清文案，兜底任务链为 design → prototype → verify。
- `eda`（原理图/PCB 电子设计）：方案围绕 ERC/DRC 体检、原理图→PCB 流程组织；兜底任务链为 research（web 工具，`mcp=deny`）→ schematic（`schematic` 角色，`mcp=allow`）→ layout（`layout` 角色，`mcp=allow`）→ verify（`tester`，`mcp=allow`）。涉及 EDA 连接器（`dev_route_connector`/`dev_list_connector_tools`/`dev_mcp_call`）的任务必须带显式 `tools` 白名单，任务描述要求子代理先 `dev_list_connector_tools` 核实工具名再 `dev_mcp_call`，验收以 ERC/DRC 零错误或逐项列明豁免为准。

除 HTTP 接口与工作台外，对话内主 Agent 还可通过 `start_workflow(goal, kind?, web?)` 工具把长链路目标升级为工作流：它只创建工作流并停在**方案选择门**，方案选择、任务 DAG 确认和执行审批都必须由用户在工作台完成，Agent 不能代审批。判据是多阶段/多角色协作、含副作用阶段、需要人工门或跨窗口恢复之一；单个独立子任务仍用 `delegate`，一轮内当场并行出结果用 `orchestrate`。子代理角色白名单不包含该工具，防止套娃。

## 检索组件边界

Harness 保留自己的 embedding、Chroma、项目隔离、权限和上下文预算；`agent_runtime.retrieval.ChromaRetriever` 仅实现 LangChain `BaseRetriever` 接口，让知识库和代码库检索可以被 LangChain 的 Retriever/Reranker 组件替换或组合。当前工作流在生成方案和任务 DAG 前，会通过该接口读取有界的本地证据，并保留来源和代码行号。LangChain 不接管 Agent 执行循环，避免与 LangGraph、工具审批和失败重规划形成第二套控制平面。

### 检索模式与评估

- `DOCMIND_RETRIEVAL_MODE=dense` 只使用现有 Chroma 向量召回。
- `DOCMIND_RETRIEVAL_MODE=hybrid`（默认）使用 BM25 与向量召回的 Reciprocal Rank Fusion；`DOCMIND_DENSE_WEIGHT` 和 `DOCMIND_BM25_WEIGHT` 可分别调整权重。
- `DOCMIND_RETRIEVAL_MODE=hybrid_rerank` 在混合候选集后强制尝试 `sentence-transformers` Cross-Encoder；未安装依赖、模型下载失败或推理失败时自动保留混合排序。也可以通过 `DOCMIND_RERANKER=heuristic` 使用无额外依赖的确定性重排。

BM25 默认使用持久化 SQLite lexical postings：默认写入 `<STATE_ROOT>/.docmind/retrieval/<collection>.bm25.sqlite3`。首次同步或收到 Chroma 变更失效通知时更新倒排表；正常查询只读取 query term 的 postings，不再每次从 Chroma 加载全量正文。更新会记录 added/removed/updated，权限过滤会在 lexical 和 dense 两侧同时执行。

受限部署可以设置 `DOCMIND_LEXICAL_BACKEND=snapshot`，使用带指纹的 JSON snapshot（`<collection>.bm25.json`）；`DOCMIND_LEXICAL_PERSIST=0` 可关闭 JSON 持久化，`DOCMIND_LEXICAL_INDEX_DIR` 可更换目录。两种后端都支持安全回退和状态诊断。

可用 `python -m benchmarks.retrieval_sqlite --sizes 1000,10000,100000` 做本机基准。当前环境实测：1k 文档构建/查询/更新约 `72/7/16ms`，10k 约 `455/55/39ms`，100k 约 `12442/794/336ms`；实际部署仍应在目标机器重跑并据此调节候选数和同步窗口。

工作流保存的五层上下文（route/project/task/subagent/output）通过持久化压缩队列处理。默认后端是面向单机/Windows 的原子 JSON 队列：状态写入 `<STATE_ROOT>/.docmind/context_compression.json`，任务状态、源快照 digest、结果 digest、尝试次数和异常都可跨窗口恢复，不触碰 SQLite 原生扩展。需要多进程队列时可设置 `DOCMIND_CONTEXT_COMPRESSION_BACKEND=sqlite`；只有显式设置 `DOCMIND_CONTEXT_COMPRESSION_ASYNC=1` 才启用后台 SQLite worker，否则在当前保存调用内同步完成并校验 digest。进程重启后 queued 或超时 running 任务会被重新领取；保存等待超时会同步回退，但队列任务仍可由后续进程完成。

召回评估接口为 `POST /api/agent/retrieval/evaluate`，请求示例：

```json
{
  "cases": [
    {"id": "movement", "query": "movement controller", "relevant_sources": ["player.py"]}
  ],
  "collection": "docmind_code",
  "top_k": 5,
  "modes": ["dense", "hybrid", "hybrid_rerank"],
  "metric": "mrr",
  "ks": [1, 3, 5]
}
```

每个 case 可用 `relevant_ids`、`relevant_sources` 或 `relevant_terms` 标注相关文档；返回各模式的 Recall@K、MRR、错误数、逐 query 明细，以及按 `metric` 选出的最佳模式。`sentence-transformers` 是可选依赖，不影响默认安装和 dense/hybrid 模式。

仓库内的 `.github/retrieval-eval.json` 是脱敏的游戏开发检索集，覆盖移动控制器、Godot 场景、工作流审批和 MCP 超时四类真实查询契约；`.github/retrieval-eval-baseline.json` 固定 MRR/Recall 基线。CI 用 `python -m agent_runtime.retrieval_eval ...` 运行离线 BM25 gate，运行时再通过 API 对当前 Chroma 的 Dense/Hybrid/Reranker 做同一套标注评估。

对于已经建立 Chroma 索引的真实项目，可使用不复制语料的 live gate：

```text
.venv\\Scripts\\python.exe -m agent_runtime.retrieval_eval \\
  --live-cases .github\\retrieval-eval-live.json \\
  --collection docmind_code \\
  --modes dense,hybrid,hybrid_rerank \\
  --baseline .github\\retrieval-eval-live-baseline.json \\
  --minimum 0.75 --json
```

live 数据集只保存查询和相关来源/术语，实际文档仍从项目自己的 Chroma 读取；命令会分别对 Dense、Hybrid 和 Reranker 做 MRR/Recall 门禁。当前工作区真实 `docmind_code` 集合（335 个切片）的八条验收查询覆盖玩家、主场景、敌人、HUD、运行时桥接、分区、项目配置和敌人场景，三种模式均为 MRR `0.9375`、Recall@1 `0.875`、Recall@3/5 `1.0`，并通过基线。

## 状态流

```text
start
  -> awaiting_choice
  -> (web_research -> awaiting_research -> awaiting_choice)
  -> planning
  -> plan interrupt（提交/恢复任务 DAG）
  -> planned
  -> awaiting_approval（safe 模式）
  -> executing
  -> execute_wave
  -> review
     -> completed
     -> replan -> execute_wave（受 max_replans / max_steps 限制）
     -> failed / interrupted
```

## HTTP 接口

- `POST /api/agent/workflow/start`：根据自然语言目标生成方案选项。
- `POST /api/agent/workflow/{id}/choice`：选择推荐方案、设计优先、联网检索或自定义描述。
- `POST /api/agent/workflow/{id}/research`：提交联网检索摘要并重新生成方案。
- `POST /api/agent/workflow/{id}/research/run`：调用现有受控 `web_research` 工具，自动提交检索摘要。
- `POST /api/agent/workflow/{id}/plan`：生成或提交主 Agent 的任务分工。
- `POST /api/agent/workflow/{id}/approve`：批准/拒绝有副作用的执行计划；传入 `auto_execute: true` 时，会优先从当前进程回调恢复，进程重启后由持久化的 session 标识重建 API runner，再继续执行；无法重建时安全降级为 `planned`。
- `POST /api/agent/workflow/{id}/execute`：并行运行现有角色化 subagent，并触发合成、失败重规划和输出审核。
- `POST /api/agent/workflow/{id}/interrupt`、`resume`：人工中断与恢复。
- `POST /api/agent/workflow/{id}/checkpoint`：读取并保存 LangGraph 的可检查状态快照（不会重复执行节点；`approved` 仅用于兼容地恢复审批门）。
- 工作台支持“修改未执行任务”：用户可在 planned/executing/interrupted 状态提交新的任务目标和依赖；后端先把新 DAG 放入 `pending_tasks` 并中断当前工作流，必须经过 DAG 审核后才替换任务图、清理旧结果并重新执行，已完成任务不会被 UI 直接改写。
- `GET /api/agent/workflow/{id}`：读取完整状态、事件、任务和审核结果。
- `GET /api/agent/workflow/{id}/evaluation`：运行离线确定性质量评估；可作为 LangSmith evaluator 的本地回归门。
- `GET /api/agent/workflow/evaluation/dataset`：查看固定的脱敏游戏开发评估样例和质量标准。
- `POST /api/agent/workflow/evaluation/dataset/sync`：在启用 LangSmith 时创建/同步固定 dataset；无 key 或网络时返回 no-op 状态。
- `GET /api/agent/workflow/{id}/skill-candidate`：读取成功工作流生成的脱敏 Skill 候选。
- `POST /api/agent/workflow/{id}/skill-candidate/approve`：用户批准后才写入用户 Skill 目录；拒绝则仅记录结果。
- `POST /api/skills/update`：在审批后更新已有用户 Skill；旧版本会保留在 `.history`，可通过 `/api/skills/rollback` 恢复上一版本。
- `GET /api/skills/regression?name=...&baseline=...&minimum=...`：根据 Skill 的历史成功/失败结果计算回归状态；没有评估样本时标记为未评分。
- `GET /api/agent/workflow/backend`：查看当前使用 LangGraph 还是原生 fallback，以及 checkpoint 健康、schema 版本和严格模式诊断。
- `GET /api/agent/retrieval/status`：查看当前检索模式、Reranker、BM25 持久化和 lexical snapshot 命中/重建统计。

工作台右上角“AI 运行台”的“开发工作流”标签提供同一组操作：启动前选择领域（通用开发/游戏开发/EDA 电子设计，默认通用），随后进行方案选择/自定义、自动联网检索、生成分工、审批、执行、中断、恢复和事件查看；详情头部会显示该工作流的领域标签。

## 视觉观察通道

工具回传的图片统一走 `agent_runtime.vision.attach_tool_observation` 能力门，任何调用方不得自行拼装多模态消息：

- 当前模型能力画像为原生视觉（`vision=native`）时，图片作为 `images` 挂到 Observation 消息直接给模型，单条观察最多 4 张；上下文预算收窄时只保留最后一条带图观察，用户当轮上传的图片不受影响。
- 非视觉模型绝不收到 image content：配置 `DOCMIND_VISION_MODEL`（及 provider/key/base_url）后，由 Harness 视觉模型把图片转成有界文字观察再回注主上下文；未配置或视觉层报错时追加明确提示并继续回合，不阻断任务。
- `web_fetch`/`web_research`（builtin provider）在 `DOCMIND_WEB_IMAGES`（默认开）且具备看图能力时，从同一份 HTML 按 og 图/正文相关性抽取候选图，经 SSRF 回环与元数据守卫、大小/超时限制和 Pillow JPEG 压缩后回传；图片只活在当轮消息中，历史与持久化只存文本和来源 URL。
- `game_screenshot` 工具按「MCP 连接器截图 → 嵌入窗 → 前台窗」顺序取帧（纯 ctypes `screen_capture`，不引入 pywin32，不向模型暴露任意 HWND/进程枚举），保存到项目 `.docmind/screenshots/` 并把缩略图作为观察过门；tester 等角色可在游戏运行过程中截图核对当前画面是否与预期一致、是否出现报错弹窗。

工作流面板的“最近检索”区域显示本地检索事件，包括集合、模式、耗时、来源、Dense/BM25/Hybrid/Reranker 分数和有限片段；“工具与 MCP 轨迹”区域会展开每个 Subagent 的 action、观察结果、失败字段和任务线程；工作流事件也可展开查看重规划、审批、Hook 和恢复原因。LangSmith 启用时，同一查询会创建 `retrieval.query`、`retrieval.dense`、`retrieval.bm25` 和 `retrieval.reranker` 子 Span；只上传计数、分数和耗时等脱敏元数据，不上传查询或文档正文。

检索器支持 `source_allowlist`，Dense 查询通过 Chroma `where` 条件过滤，Hybrid 的 snapshot/SQLite lexical 路径也会再次过滤来源，避免权限边界只在向量召回侧生效。工作台“技能与扩展”页可配置声明式 Hook 断点（生命周期、可选匹配文本、阻断/仅记录、用户提示），配置写入 `breakpoints.json`，不会执行 UI 输入的 Python。

真实 provider 的动态方案/DAG 合约测试位于 `tests/test_live_llm_workflow.py`，默认跳过；设置 `DOCMIND_LLM_LIVE=1` 和非 `mock` 的 `LLM_PROVIDER` 后，它会实际请求 provider，校验严格 JSON、唯一推荐项、动态角色/人设/工具/MCP 字段和依赖 DAG。CI 仅在仓库变量 `DOCMIND_LLM_LIVE=1` 时启用该 smoke test。当前开发机已用 `ollama/qwen3:4b` 实际通过该测试，并进一步通过 `DOCMIND_REAL_LLM_E2E=1` 跑通 `tests/test_live_real_harness_e2e.py` 的真实模型→文件工具→Godot headless→复核链路；测试使用临时项目副本，不改源项目。

## 端到端验收

`tests/test_game_workflow_e2e.py` 提供一个离线 Godot 最小项目 fixture。它使用真实的 `GameWorkflowManager`，依次验证需求澄清、推荐方案、四任务 DAG、审批、无依赖任务并行、项目文件创建、静态自测、结果复核、五层上下文状态和 LangSmith 事件计数。测试 runner 只替代实际 ToolSpec 执行器，所有文件仍写入临时项目根目录，不访问网络或用户项目。若开发机存在真实项目，可运行 `tests/test_real_godot_acceptance.py`；它会调用真实 Godot headless 主场景并要求收到 `player_ready` 运行事件，当前 `<sample-project>` 已实际通过该验收。

完整真实项目副本验收使用：

```text
$env:DOCMIND_REAL_GAME_E2E="1"
$env:DOCMIND_NO_TEST_ISOLATION="1"
.venv\\Scripts\\python.exe -m unittest tests.test_real_project_workflow_e2e -q
```

也可以使用可复用的验收 runner 输出机器可读报告（默认复制项目后执行，源项目保持只读）：

```text
.venv\\Scripts\\python.exe -m agent_runtime.acceptance \\
  --project <sample-project> \\
  --godot D:\\Tools\\Godot\\Godot_v4.7.2-stable_win64_console.exe \\
  --collection docmind_code --json
```

报告会逐项记录检索、澄清、分工、审批、执行、Godot headless、自测复核和源项目完整性；使用 `--require-event` 可把运行时事件（例如 `player_ready`）提升为硬门禁。CI 或发布脚本应检查进程退出码和报告中的 `ok`、`source_unchanged`。

该测试从真实项目的 Chroma 代码索引读取证据，把项目复制到临时目录，在副本中执行需求澄清、方案选择、两个无依赖 Subagent 并行、文件写入、审批后的执行、Godot 导入初始化、主场景 Playtest、结果复核和输出审计；源项目不会被写入。当前真实 `<sample-project>` 副本验收已通过。

任务 DAG 可为每个 Subagent 指定 `persona`、`tools`、`mcp`（`auto|allow|deny`）和 `reflection`。主 Agent 不固定生成成员数量：简单任务可以直接派一个执行代理；复杂文件任务可以先派只读 `dispatcher`（兼容 `planner`，负责任务拆解与分工），由它分析目录、依赖和验收标准，并返回受限 `tasks` JSON。主 Agent 校验后才把这些任务动态加入 DAG，再决定实际执行成员数量。工具权限只能在角色白名单内进一步收窄，不能由模型扩大；`dispatcher/planner` 强制 `MCP=deny` 且不允许写文件，主 Agent 负责汇总、终审和最终写入决策。这些字段会经过白名单和长度校验，持久化到 `subagents`，执行后记录反思结论；反思失败会把任务交回主 Agent 的复核/重规划路径。

本地 Ollama 还有一层资源调度：`max_parallel` 控制同时生成数，默认所有 4B/7B/14B/35B 模型均为 1，避免显存和上下文争抢；`max_subagents` 只限制整张 DAG 的任务总数，因此 4B 模型仍可串行完成设计→实现→验证三阶段，而不会被错误截断为一个任务。可用 `DOCMIND_LOCAL_LLM_MAX_CONCURRENCY` 和 `DOCMIND_LOCAL_SUBAGENT_MAX` 在实测有余量时显式提高上限。

工作流的步数预算随任务规模动态放大：子代理默认最多 `SUBAGENT_MAX_STEPS=24` 步、硬顶 `SUBAGENT_STEPS_HARD_CAP=64`（环境变量可调）；工作流总预算为 `min(硬顶, max(下限, 任务数*24+4))`，内置默认区间 `[24, 200]`，可由部署侧环境变量覆盖：`DOCMIND_WORKFLOW_STEPS_MIN` 设置自动预算下限（同时是「未显式指定」的基准值，简单任务可下调）、`DOCMIND_WORKFLOW_STEPS_MAX` 设置硬顶（复杂任务可上调，颠倒或非法的配置会被安全回退/夹取）。规划时显式给出的 `max_steps`（含低于下限的值）会被尊重并透传到执行节点，但不超过硬顶；默认值与下限相同则视为未指定、走自动预算。多 Agent 并行波各自计步，因此长流程不会被单代理的小预算误截断。

本机 Ollama 单次短 JSON smoke（`num_ctx=8192`，仅作相对参考）测得：`qwen3:4b` 约 13 秒、`qwen3:14b` 约 20 秒、`qwen3.6:35b-a3b` 约 32 秒。完整 Harness 仍建议 4B/14B/35B 默认串行；并发提升必须以目标机器实测显存、超时和失败率为依据。

五层上下文除了预算外，还会在每次 checkpoint 保存 bounded snapshot 和 digest。`route/project/task/subagent/output` 的快照可在另一个进程恢复；如果恢复时发现正文缺失或 digest 不一致，会同步重建快照并标记 `job_state=recovery_sync`，再由 `snapshot_meta.valid` 校验恢复内容。Subagent 只携带依赖摘要，不共享父会话原文。

运行：

```text
.venv\\Scripts\\python.exe -m unittest tests.test_game_workflow_e2e -q
```

`/start` 默认让 LangGraph 的 choice 节点调用当前 Agent 生成严格 JSON 方案，校验失败或模型不可用时自动回退到本地模板；传入 `use_llm: false` 可完全离线运行。选择联网后，LangGraph 的 research 节点调用受控 `web_research`，再把检索摘要交给同一 LLM 生成方案，并保留自定义与联网选项。选择方案后，plan 节点根据用户目标、选定方案、路由/项目上下文生成严格 JSON 任务 DAG；会校验唯一 ID、依赖存在性和环，失败则回退到 design → prototype → verify 模板。方案和任务的规范化结果会写入 checkpoint；它们不会执行工具，也不会修改文件。

## 安全边界

工作流只负责任务状态和调度；文件写入、命令执行、MCP、GPU 和引擎操作仍必须经过现有 `ToolSpec`、审批、沙箱和 `self_verify`。每个工作流有最大任务数、最大重规划次数、最大 subagent 数、最大步数和工具失败上限。子代理和汇总结果会经过乱码、状态、工具失败数和敏感信息复核；启用 `experience_enabled` 后只沉淀脱敏流程摘要。

MCP 工具调用支持有界的连接器故障转移：`dev_mcp_call` 可传入 `fallback_keys`，或提供 `hint` 让语义路由生成候选；主连接器失败后最多尝试有限数量的候选，并返回每次尝试的安全诊断。具有副作用的操作传 `side_effect: true` 后默认不跨连接器重试，只有显式传 `allow_side_effect_fallback: true` 才允许，避免响应丢失造成重复写入。缺失工具安装完成后会实际执行 Python distribution / npm 版本 verifier，并校验目标目录仍在项目沙箱内；verifier 失败会返回替代工具和安装审计。

工具生命周期还提供 `before_tool`/`after_tool`、`before_mcp`/`after_mcp`、`mcp_retry`、`before_subagent`/`after_subagent` Hook。它们只接收工具名、角色、耗时、状态和字符数等脱敏元数据；返回 `block: true` 可在工具、MCP 或子代理边界暂停当前动作，配合 `hook_failure=block` 可把异常升级为工作流中断。

LangSmith 只接收脱敏后的 workflow/turn 元数据；问题正文、回答正文、研究摘要、工具参数和工具输出不会自动上传。配置 `DOCMIND_LANGSMITH=1` 与 `LANGCHAIN_API_KEY` 后，工作流开始时会创建一个 root run，choice/research/plan/approval/execute/replan/review 事件和 LLM/研究 provider 会实时创建带 parent 的 child run；恢复进程会沿持久化的 root ID 继续写入，完成或失败时关闭 root。没有 SDK、网络或 API key 时自动 no-op。默认关闭完成后的旧事件账本导出，设置 `DOCMIND_LANGSMITH_EXPORT_FALLBACK=1` 可同时保留该兼容导出。

真实联调可在受控环境设置 `DOCMIND_LANGSMITH_LIVE=1` 与有效 `LANGCHAIN_API_KEY`，运行 `python -m unittest tests.test_langsmith_live -q`；该 smoke test 只发送脱敏元数据，并验证 root/child、flush 和终态关闭；重试逻辑由离线故障注入测试覆盖。

评估使用 `agent_runtime.workflow_eval` 的同一份确定性规则：记录 `completed`、`review.ok`、失败/阻塞任务、步数、重规划次数和 `idempotency_in_doubt`。`dataset_cases()` 提供固定样例，`evaluate_case()` 负责单条 contract，`aggregate_evaluations()` 和 `regression_gate()` 负责 pass-rate 及逐 case 回归。`DOCMIND_WORKFLOW_MIN_PASS_RATE` 默认是 `1.0`；LangSmith evaluator 只是把相同结果映射成远端分数，本地门禁不依赖网络。

当前迁移边界：人工选择/研究/审批、执行波、结果复核、失败重规划、完成/失败路由和 Subagent 并行扇出已经由 LangGraph 控制；DAG 校验、拓扑分波和未安装 LangGraph 时的执行回退仍复用 `orchestrator` 中的纯调度函数。每个并行波会记录独立的 task thread id，为后续 LangSmith 子代理级嵌套 trace 提供关联键。并行 Subagent 还会显式携带 workflow root trace ID，在 worker 内创建独立的 `subagent.<task-id>` span；该 span 下的 LLM/Tool 子调用不依赖线程上下文自动继承。

`.github/workflows/harness.yml` 提供 SQLite/Postgres 两套 CI 矩阵；Postgres job 使用 `postgres:16` service，并额外执行严格 checkpoint 集成测试。

默认 SQLite checkpoint 文件位于工作流状态目录的 `langgraph_checkpoints.sqlite`。生产环境可设置 `DOCMIND_CHECKPOINT_POSTGRES_DSN` 使用 PostgresSaver 和连接池；首次连接会执行幂等 schema setup，已有表不会被覆盖。工作流执行期间，具有副作用的 ToolSpec 调用还会在同目录的 `tool_calls.sqlite` 记录幂等键、完成结果和“不确定执行”状态：进程重启后完成调用只重放结果，执行状态不明的调用会被阻断，避免重复写入或不可逆操作。

生产部署可设置 `DOCMIND_CHECKPOINT_POSTGRES_REQUIRED=1`：Postgres 初始化、schema 版本检查或连接池建立失败时直接拒绝启动，不静默回退 SQLite。schema 版本记录在 `docmind_checkpoint_schema`，当前 workflow schema version 为 1；`/api/agent/workflow/backend` 会执行轻量健康探针并返回错误分类。

Postgres 模式还会创建 `docmind_workflow_leases` 租约表。每个 workflow 执行前获取带 fencing token 的租约，执行波之间刷新，避免多个 worker 同时恢复同一 workflow；租约失联后执行会失败并进入可重试状态。`DOCMIND_WORKFLOW_LEASE_S` 控制租约时长。

设置 `DOCMIND_WORKFLOW_AUTO_RECOVER=1` 后，API 启动阶段会扫描已批准且保存了 session 的 `planned/executing` 工作流，重建 runner 并继续执行；恢复失败的工作流保留为可重试状态并记录 `recovery_failed` 事件。
