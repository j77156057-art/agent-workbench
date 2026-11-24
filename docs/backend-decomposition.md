# 后端巨型函数拆分 · 设计稿

> 状态：本轮实现完成（2026-11-24）。本稿是**动手前的契约**，实现必须按稿走；
> 稿与实现不一致时，先改稿再改码。

## 1. 目标与边界

**目标**：把后端 ≥200 行的「巨型函数」降到可维护规模，同时**行为逐字不变**
（接口路径/方法/响应结构、事件流字段、算法与提示词一律不动）。

**本版不做什么**（边界比功能列表更重要）：

1. 不改任何 HTTP 接口的路径、方法、状态码、响应结构、标签、依赖、注册顺序。
2. 不改算法、提示词、阈值、重试策略。
3. 不动 `agent_runtime/game_workflow.py:build_langgraph`（757 行）——
   它是**声明式建图**，拆开反而更碎，属于"看起来大但其实清楚"。
4. 不动独立脚本（`verify_engine_embed.py:main` 等）—— 它们不是服务路径。
5. 不做「顺手改点别的」：本轮只搬移与切分，任何行为改动都要另开一轮。

## 2. 环境事实约束（会改变做法，必须先认）

- **并发写者共享同一工作树**：只能按明确路径提交自己的文件，绝不 `git add -A`，
  绝不整份提交别人的在制品（`VERSION` / `landing-site/**` / `packaging/**`）。
- **CI 有 9 条 job**，其中「改动行覆盖率门」的聚合下限是 90%
  （`agent_runtime/coverage_eval.py` 的 `AGGREGATE_FLOOR`）。
- **CI 跑在 Linux runner**：测试里不得写死本机绝对路径，要从 `__file__` 推。
- **freeze-guard**：改文件期间不跑测试；跑完确认 freeze-guard 的违规清单
  没有新增条目（该清单在本机临时目录里，**不要把它的路径写进仓库文档**，
  隐私门禁会判成泄露本机路径）。
- 本机 git 协议不通 GitHub，推送走 REST + 固定 IP 的脚本。

## 3. 现状实测（AST 扫描，不是印象）

全后端 14765 个函数，≥200 行的 **25 个、合计 11147 行**：

| 函数 | 行数 | 性质 |
|---|---|---|
| `agent.py:_run` | 1973 | 流程编排（生成器主循环 1653 行） |
| `api_routes/agent.py:build_router` | 1233 | 端点注册（57 端点 + 9 helper + 2 大闭包） |
| `agent_runtime/game_workflow.py:build_langgraph` | 757 | 声明式建图 —— **本版不动** |
| `api.py:chat` | 656 | 端点 + 嵌套 SSE 生成器（300 行） |
| `verify_engine_embed.py:main` | 592 | 独立脚本 —— **本版不动** |
| `agent.py:_run_child` | 543 | 子代理执行 |
| `agent.py:_run_shell` | 474 | shell 模式执行 |
| `api.py:live_vision_stream` | 388 | 端点 |
| `game_workflow.py:_execute_unleased` | 387 | 流程编排 |
| `workbench_fs.py:build_relation_graph` | 351 | 纯计算 |
| `game_workflow.py:_run_task_dag_langgraph` | 321 | 流程编排 |
| `output_audit.py:audit_evidence` | 312 | 纯计算 |
| `api.py:event_stream` | 300 | SSE 生成器 |
| 其余 12 个 | 201~280 | 解析器 / 工具 / 配置 |

**耦合度实测**（AST 自由变量分析：每个候选块写外层状态的变量个数）：

| 候选块 | 行数 | 写外层状态 | 抽取代价 |
|---|---|---|---|
| `_run` 的 `native_queue` 段 | 177 | 5 | 低 |
| `_run` 的续写 nudge 段 | 84 | 2 | 低 |
| `_run` 的降级收尾段 | 60 | 1 | 低 |
| `_run` 的空回复 nudge 段 | 63 | 6 | 低 |
| `_run` 的 final_body 段 | 206 | 16 | 中 |
| `_run` 的**工具执行段** | **666** | **68** | **高**（需状态载体） |
| `_run` 的 journal 恢复段 | 122 | 48 | 高 |
| `_run_child` 主执行段 | 120 | 9 | 低 |
| `_run_shell` 主执行段 | 280 | 13 | 中 |

结论：**不能一刀切**。低耦合块可以直接搬成方法；高耦合块必须先有状态载体。

## 4. 四类拆分策略

- **A 端点注册型**（`build_router`）→ 按**连续路由段**拆成多个注册函数。
  连续段是硬约束：为的是保住路由注册顺序（§7）。
- **B 流程编排型**（`_run` / `_run_child` / `_run_shell` / `chat`）→
  抽出阶段函数；跨阶段共享的可变状态收进一个**状态载体**。
- **C 声明/解析型**（`build_langgraph` / `_extract_java`）→ 本版不动或另行处理。
- **D 脚本型**（`verify_*.py:main`）→ 本版不动。

## 5. 分层与模块职责

### T1 `api_routes/agent.py:build_router`（1233 → 23 行）

#### 5.1 为什么**不**拆成多个文件（实测踩坑后的修订）

第一版设计是「拆成 11 个子模块」。落地前查打桩点，发现 **6 个测试文件直接
patch `api_routes.agent` 的模块属性**：

```
tests/test_retrieval_api.py:25      patch("api_routes.agent.evaluate_modes", ...)
tests/test_retrieval_api.py:47      patch("api_routes.agent.retrieval_status", ...)
tests/test_visual_feedback.py:148    patch("api_routes.agent.WORKFLOWS", manager)
tests/test_sse_atomic_subscribe.py:53  api_agent.WORKFLOWS = manager      # 赋值式！
tests/test_workflow_start_consistency.py:50  api_agent.WORKFLOWS = ...
tests/test_workflow_ownership.py:41  api_agent.WORKFLOWS = manager        # 赋值式！
tests/test_review_code_root_wiring.py:166 patch.object(route_mod.WORKFLOWS, "get", ...)
```

把端点搬到子模块后，子模块里的 `WORKFLOWS` 是**它自己 import 的那一份**；
测试改 `api_routes.agent.WORKFLOWS`（尤其是**赋值式**的）根本不会影响子模块
→ 端点继续用真 manager → 测试要么红、要么**静默串味**（断言的其实不是它以为
的那个对象）。这种失败比直接报错更难查。

因此本轮**保留单文件**：拆的是**函数**，不是**文件**。模块级名字
（`WORKFLOWS` / `evaluate_modes` / `retrieval_status` / `review_output`）
一个都不动，打桩点全部原地有效。

> 若以后要真拆文件，必须先把这些打桩点改成「打桩**被测模块**的那个名字」，
> 并逐条确认端点确实用上了假对象 —— 那是一次独立的、有风险的重构，不该藏在
> 「拆巨型函数」里。

#### 5.2 实际切法

按**连续路由段**切（连续 = 路由注册顺序天然不变），共 11 个注册函数：

| 注册函数 | 覆盖端点 | 行数 |
|---|---|---|
| `_register_policy` | route / permission / routing_status / project_profile / langsmith_status | 35 |
| `_register_workflow` | workflow_backend / start / active / list / get / events | 145 |
| `_register_eval` | skill_candidate(+approve) / evaluation(+dataset / dataset_sync) / retrieval_evaluate / retrieval_runtime_status | 71 |
| `_register_planning` | choice / research / research_run / plan / approve | 147 |
| `_register_preview` | preview(+artifact) / acceptance(+decide) | 71 |
| `_register_visual` | visual_feedback / feedback_project / status / snapshot_save / snapshot_file | 45 |
| `_register_project` | checkpoint / project_checkpoint / project_rollback / preview_adapters(+config) / stage_apply / stage_review / stage_cleanup / preview_adapter_decision | 109 |
| `_register_acceptance` | acceptance_report | 26 |
| `_register_lifecycle` | interrupt / delete / resume / revise / revise_approve / execute / subagent_retry | 94 |
| `_register_approvals` | approvals / approval_requests / approval_request_decide / create_approval / decide / get_approval | 58 |
| `_register_connectors` | connectors / connector_route | 24 |

端点代码**逐字搬运**（AST 切片，缩进不变）。

#### 5.3 拆分脚本必须把**文件尾部**一起拼回去（实测踩坑）

第一版脚本按「头部 + 重建函数」重写文件，**没把尾部接回去** →
`api_routes/agent.py` 的 `__all__ = [name for name in globals() if
name.endswith("Req")]` 被丢掉 → CI 四个 sqlite 分片全红；同一批脚本在
`api.py` 上甚至截掉了后 850 行（靠 ruff 突然报 42 条 F401 暴露：
`logging` / `StaticFiles` / `regions.*` 变成「未使用」）。

两个要点：

1. **脚本重写文件 = 手工编辑的等价风险**。用 AST 切片重建时，文件里
   「函数之外的一切」（`__all__` / 常量 / 尾部 re-export）都得显式接回去。
2. **路由快照查不出这类丢失**（`__all__` 不影响路由表）。所以验收要再加
   一条：**顶层符号清单对照**（AST 取函数/类/模块级赋值名，与拆分前逐一比对，
   「丢失」必须为空）。

#### 5.4 依赖 ctx 的 helper 怎么搬

原先 9 个 helper 是 `build_router` 里的闭包（靠闭包捕获 `ctx`）。拆成
模块级注册函数后，闭包没了。做法是改成**工厂**：

```python
def make_require_workflow_project(ctx):
    def require_workflow_project(workflow_id: str, state) -> dict | None:
        ...          # 原闭包体，一字未改
    return require_workflow_project
```

注册函数里绑定一次局部别名：

```python
def _register_workflow(router, ctx):
    launch_workflow = make_launch_workflow(ctx)
    require_workflow_project = make_require_workflow_project(ctx)
    ...
```

于是**各端点的函数体一字未改**（除工厂/别名这几行）。不依赖 ctx 的三个
（`_canonicalize_root` / `_ownership_blocked` / `workflow_evidence`）
直接降为模块级函数。

两个进程级注册点（`WORKFLOWS.set_execution_resolver` 与
`tools.set_workflow_launcher`）**仍在 `build_router` 里、且时机不变**
（构建时各做一次）。

### T2 `api_routes/engine.py:build_router`（248 → 26 行）✅ 已做
同样按连续段切成 5 个注册函数（discovery / unreal / skillconfig / runtime /
addonweb），与 T1 同法。注意该文件里 `root_or_error` 原先是 `build_router`
的闭包变量，搬到子函数后必须**在每个注册函数内重新绑定**
（`root_or_error = ctx._project_root_or_error`），否则 NameError。

### T7 `api.py:_apply_config`（273 → 169 行）✅ 已做
- 7515-7627（校验 + 解析 + 能力覆盖落盘）→ `_resolve_config(req)`，
  返回 `(resolved, failure)`：失败路径原本 `return X`，现在
  `return None, X`，由调用方原样返回；
- `_apply_config` 改成先解包再继续，**副作用顺序一字未改**。

### T3 `api.py:chat`（656）
- 参数解析与上下文装配 → 抽成独立函数；
- 嵌套的 `event_stream` 生成器（300 行）→ 提为模块级生成器。
契约：SSE 行格式与字段名逐字不变。

### T4 `agent.py:_run_child`（543）/ T5 `agent.py:_run_shell`（474）
低耦合段直接抽成方法；共享状态用 §6 的载体。

### T6 `agent.py:_run`（1973）
先做低耦合段（native_queue / 两个 nudge 段 / 降级收尾，共约 380 行）；
**工具执行段（666 行）与 journal 恢复段（122 行）留到下一轮**——它们要 68 /
48 个跨段状态，先上状态载体再拆。

## 6. 数据模型：状态载体 `_TurnState`

流程编排类函数里跨阶段共享的可变状态，收进一个 dataclass（放
`agent_runtime/turn_state.py`），**只做搬运，不加逻辑**：

| 字段组 | 例子 | 用途 |
|---|---|---|
| 失败/诊断 | `failures`, `fail_total`, `forced_finals`, `forced_final_reason`, `_permanent_failures` | 工具失败聚合 |
| 提示 | `nudges`, `evidence_nudges`, `continuation_nudges`, `observation_echo_nudges`, `verbatim_dump_nudges`, `evidence_dump_nudges` | 各类 nudge 计数与文本 |
| 观测 | `last_action`, `last_obs`, `last_exec_obs`, `executed`, `iterations` | 循环状态 |
| 证据 | `evidence`, `audit_steps`, `marker_echoes`, `format_stalls` | 证据与反模式计数 |
| 收尾 | `pending_final`, `final_continuations`, `tool_steps`, `nav_free_used` | 最终回答与配额 |

**不变量**：字段名与现在**一字不差**；载体不带方法、不做校验、不改变量含义；
搬运后 `git diff` 里出现的改动应当**只有** `name` → `st.name` 的机械替换。

## 7. 核心接口契约

1. `api_routes/agent.py:build_router(ctx)` 的**对外签名不变**，
   `api.py:8574` 的挂载点不用改；返回值仍是同一个 `APIRouter`。
2. **路由注册顺序不变**：FastAPI 按注册顺序匹配，
   `/api/agent/workflow/backend` 必须早于 `/api/agent/workflow/{workflow_id}`。
   本仓库实测：全表 359 条路由里只有 2 处「参数路径在前、静态路径在后」的
   潜在遮蔽（`/api/tasks/{task_id}` vs `/api/tasks/validate`、
   `/api/comfy/templates/{template_id}` vs `/api/comfy/templates/apply`），
   **都不在 `/api/agent` 族内**；本轮不动它们。
3. SSE 契约：`data: {json}\n\n` 行格式、字段名、`done` 尾帧不变。
4. `ctx` 仍是 `sys.modules["api"]`（现由 `api.py:8587` 传入）。

## 8. 失败分类与处置（重构失败通常长这样）

| 形态 | 症状 | 处置 |
|---|---|---|
| **路由错位** | 某个端点 404 或被更宽的路径吃掉 | 路由表快照对拍立刻抓到；按原行序分段 |
| **闭包变参数后漏传** | 端点运行时报 `NameError`/拿到 None | 工厂必须显式 `return`（漏 `return` 会让绑定变成 None） |
| **打桩点搬家** | 测试红，或更糟：静默用错对象 | 模块级名字一律不动；只拆函数不拆文件 |
| **脚本重写丢尾部** | `__all__` / 常量 / re-export 消失 | 生成器拼回尾部 + 顶层符号清单对照 |
| **状态搬运漏一个字段** | 行为细微变化（如 nudge 少发一次） | 变异电池 + 事件流逐事件对拍 |
| **导入环** | 新模块互相 import → ImportError | 子模块只依赖 `agent_support`，`agent_support` 不反向依赖子模块 |

## 9. 测试与验收标准

1. **顶层符号清单对照（硬门）**：AST 取拆分前后每个文件的顶层
   函数/类/模块级赋值名，逐一比对，「丢失」必须为空。这一条能抓住
   `__all__`、常量、尾部 re-export 之类**路由快照查不出**的丢失。
2. **路由表对拍（硬门）**：重构前后各导一份规范化快照
   （路径 / 方法 / 端点名 / 状态码 / 响应模型 / 标签 / 依赖 / summary /
   description / **顺序**），逐条**完全一致**。
3. **端点冒烟**：对被拆模块的每个端点发一次真实请求，断言状态码与响应结构
   不变（不是只查表 —— 表查不出运行期才炸的问题）。
4. **现有测试全绿**：`pytest tests/` + CI 的 4 个 sqlite 分片。
5. **变异电池**：`tests/mutation/run_all.py --no-live` 全绿。
6. **改动行覆盖率** ≥ 90%（CI 门）。

## 10. 风险登记册

| 风险 | 影响 | 缓解 |
|---|---|---|
| 路由顺序变化导致端点被吃掉 | 线上 404 / 行为漂移 | 按原行序分段 + 快照对拍 |
| 闭包转显式参数时漏传 | 运行期异常 | 工厂必须显式 `return`；冒烟逐端点打真请求 |
| 打桩点搬家（拆文件导致） | 测试红或静默串味 | **本轮不拆文件**（§5.1） |
| 脚本重写丢文件尾部 | `__all__`/常量消失 → CI 红 | 生成器拼回尾部 + 符号清单对照 |
| 状态搬运漏字段 | 细微行为漂移 | 变异电池 + 事件流对拍 |
| 与并发写者改到同一文件 | 提交串味 | 只提交自己的路径；撞同一文件先提示 |
| 一次拆太多，回滚成本高 | 事故半径大 | 按 T1→T6 分批，每批独立提交 + 独立 CI |

## 11. 待确认事项

1. **`api.py` 本身（8600+ 行、203 个路由）要不要也拆**？
   本稿只处理「巨型函数」，没动 `api.py` 的整体体量。若要拆，
   应另起一稿（按域迁到 `api_routes/*`，与既有惯例一致），工作量约等于 T1 的数倍。
2. **`game_workflow.py:build_langgraph`（757 行）**：本稿按"声明式、不算病态"
   排除。若你认为它也该拆，需要先定"拆完怎么读"的目标形态。
3. **`symbols.py` 的语言解析器**（`_extract_java` 280 / `_extract_gdscript` 201）：
   拆成 per-language 模块有独立价值，但与本次「函数太大」是另一类问题，未纳入。
4. **端点族要不要真拆文件**：§5.1 的修订把这件事推到下一轮。若要拆，
   需先单独立项处理 6 个测试文件的打桩点迁移。

## 12. 本轮结果（2026-11-24）

已完成三处拆分，全部**逐字搬运 + 硬门对拍**：

| 目标 | 拆前 | 拆后 | 验收 |
|---|---|---|---|
| `api_routes/agent.py:build_router` | 1233 行 | 23 行（11 个 `_register_*`） | 路由表 359 条逐条一致；符号 0 丢；84 个直接依赖测试绿 |
| `api_routes/engine.py:build_router` | 248 行 | 26 行（5 个 `_register_*`） | 同上（143 测试绿） |
| `api.py:_apply_config` | 273 行 | 169 行 + `_resolve_config` 129 行 | 同上（230 测试绿） |

**≥200 行的后端函数：25 个 → 22 个**。

### 剩余的巨型函数与下一步

| 函数 | 行数 | 为什么还没动 |
|---|---|---|
| `agent.py:_run` | 1973 | 主循环 1653 行；工具执行段写 68 个跨段状态、journal 恢复段写 48 个 → 先上 §6 的状态载体 |
| `game_workflow.py:build_langgraph` | 757 | 声明式建图，拆了更碎（§1 边界 3） |
| `api.py:chat` / `event_stream` | 656 / 300 | 嵌套 SSE 生成器闭包捕获约 45 个局部量 |
| `agent.py:_run_child` / `_run_shell` | 543 / 474 | 主执行段写 9 / 13 个跨段状态 |
| `api.py:live_vision_stream` | 388 | WebSocket 协议处理，205 行 try 写 25 个状态 |
| `workbench_fs.py:build_relation_graph` | 351 | 三个大循环 + 4 个闭包 helper |
| `output_audit.py:audit_evidence` | 312 | 判定段互相依赖 |
| `symbols.py:_extract_java` 等解析器 | 280 / 201 | 字符级解析器，状态机式循环 |

**下一步建议**：按 §6 先做 `_TurnState` 状态载体（只搬运、不加逻辑），
再依次拆 `_run` 的低耦合段（native_queue / 两个 nudge 段 / 降级收尾，
约 380 行）与 `_run_child` / `_run_shell` 的主执行段。
