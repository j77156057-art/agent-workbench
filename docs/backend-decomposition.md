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

**字段表是 AST 实测出来的，不是拍脑袋列的**（2026-11-24 落地时的真实清单）：
按「出现在 ≥2 个候选阶段块里」筛第一轮，只得到 30 个；但**只在一个块里出现、
却要在块外继续用**的名字同样必须进载体 —— 落地时又补了 11 个：

| 补进来的 | 为什么 |
|---|---|
| `iterations` / `evidence_nudges` / `verbatim_dump_nudges` / `evidence_dump_nudges` / `observation_echo_nudges` / `continuation_nudges` / `marker_echoes` / `plan_emitted` | journal 恢复段会把它们从 journal 里读回来，`_run` 后面还要用 |
| `_last_ctx_at` | 工具执行段要写它（实时刷新上下文用量的限频时刻） |
| `_resume_tip` | journal 恢复段写、`_run` 读（续跑提示语） |

> 判据不是「出现几次」，而是**「这个变量归谁所有」**：只要它跨越了方法边界，
> 就必须由载体（或显式参数）承载，否则就是一处静默的行为变化。

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
| **抽出的方法写的外层状态回不去** | 抽出的方法里 `_resume_tip = ...`，`_run` 读到的还是初值 | 抽之前先查「方法里写、`_run` 里读」的名字，一律进载体 |
| **源码钉桩型测试的钉子搬家** | 9 个测试文件按**源码文本**钉桩 `Agent._run`（`inspect.getsource`），代码搬出 `_run` 后钉子落空 → 十几个用例红 | 新增 `tests/agent_loop_source.py`：把主循环各方法的源码拼接、并按载体字段表还原 `st.<字段>` 前缀；钉桩继续对着「逻辑形状」，牙不变 |
| **绕过 helper 直读源文件** | 有的守卫自己 `open("agent.py")` 读原文，正则认的是裸名字 → 载体前缀 `st.` 让正则失配 | 所有源码级守卫统一走 helper（含「钉整个模块」和「读磁盘原文」两种都要换） |
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

### 第二轮：`agent.py:_run`（1973 → 997 行）+ `_TurnState` 载体

按 §6 落地，顺序是「**先上载体（纯搬运）→ 再抽块**」，每一步都用
**事件流等价对拍**验收（6 个脚本化场景 / 1034 个事件，改前录基线、改后逐条比）：

| 抽出 | 行数 | 变成 |
|---|---|---|
| 格式纠偏段 | 63 | `_handle_format_stall` |
| 续写纠偏段 | 84 | `_handle_nudge` |
| 终态兜底段 | 60 | `_handle_final_fallback` |
| journal 恢复段 | 122 | `_restore_from_journal` |
| 工具执行段 | 666 | `_run_tool_action` |

- `_run`：**1973 → 997 行**（剩下的 792 行是主循环本身：模型调用与流式解析）
- 新增 `agent_runtime/turn_state.py`（`_TurnState`，42 个字段，无方法无校验）
- `st.` 改名：563 处 Name 节点，纯机械替换
- 事件流对拍：6 场景 / 1034 事件**逐条一致**
- agent 侧测试：236 passed

**踩到的坑**：第一版载体的字段表按「出现在 ≥2 个块里」筛，漏了
「只在一个块里出现、但块外还要用」的名字 → `_resume_tip` 留在方法里，
`_run` 读到初值 → resume 路径 5 个用例红（`UnboundLocalError`）。
事件流对拍**没抓到**它（那几个场景没走到那条分支）—— 是测试套件抓到的。
→ 判据改成「**这个变量归谁所有**」，而不是「出现几次」。

### 第三轮：工具执行段再拆一层（`_run_tool_action` 669 → 53 行）

工具执行段按 AST 切出 5 个阶段，**跨阶段共享名只有 10 个**（都进了载体）：

| 阶段 | 行数 | 方法 |
|---|---|---|
| guard | 272 | `_tool_guard`（永久失败、写工具确认、重复调用、空参、步数上限、allowlist、租约） |
| exec | 81 | `_tool_execute`（真正执行工具） |
| observe | 96 | `_tool_observe`（整理成 observation 事件与证据） |
| fail | 130 | `_tool_failure`（失败分类与计数） |
| wrap | 87 | `_tool_wrap`（审计、复核、写 trail、快照） |

`_run_tool_action` 变成 53 行的纯编排。**≥200 行的后端函数：22 → 21**。

这轮踩到的两个坑（都是「机器改写」特有的）：

1. **第二层抽取把返回值翻面了**：上一层已把「继续主循环」写成 `return "continue"`、
   「整轮终止」写成 `return "stop"`；第二层再统一把 `ast.Return` 改成
   `return "stop"` → **"continue" 被翻成 "stop"** → 工具执行一轮就断
   （事件流对拍当场抓到：工具场景少了后半段）。
   → 改写规则必须**按值区分**：带值的 return 不动，只改裸 return。
2. **同名变量在别处是循环变量**：`sig` / `obs` 在 `_run` 的并行批次里是
   `for nm, ar, obs, _ok, imgs, _ek in results:` 的循环变量，与工具段那两个
   同名但无关 → 改名**只能限定在 `_run_tool_action` 内**，别全函数扫。

### 第四轮：「一轮」的边界定义 + 主循环拆成子方法

主循环原来 792 行、是一个 25 段的状态机。现在把它定义成**「一轮」**并拆开。

#### 「一轮」是什么

**一轮 = 主循环的一次迭代 = 准备 → 取模型输出 → 决定下一步**（不是「一轮问答」——
一轮问答是整个 `_run` 调用，对应载体 `_TurnState` 的生命周期）。

| 段 | 方法 | 行数 | 内容 |
|---|---|---|---|
| A 准备 | `_run_round_prepare` | 178 | 取消检查、轮次计数、快照、deadline、步数上限、并行批次、定时提醒、构造 messages |
| B 取输出 | `_run_round_ask` | 189 | 原生队列或模型调用（流式解析、token 过滤、reasoning 合并），tool_calls 归一进文本协议 |
| C 决定 | `_run_round_parse` | 69 | 解析响应、续写、复读检测、thought/叙述/plan 事件 |
| | `_run_round_decide` | 170 | 工具动作分发、未知工具兜底、终态处理编排 |
| | `_run_round_finalize_guards` | 104 | 终态守卫（前三个：截断续写、裸铺源码、裸证据） |
| | `_run_round_finalize` | 113 | 终态守卫（后三个）+ 纠偏与兜底收尾 |

外加 `_init_turn_state`（96 行）把这一轮问答的状态初始化独立出来。
**`_run` 从 997 行降到 141 行**——它现在只剩：建载体 → 初始化 → 两个局部闭包
（`_snapshot` / `_evidence_final`）→ 23 行的主循环骨架。

#### 边界契约（三条）

1. **段间只用控制码通信**：`None` = 继续往下、`"stop"` = 整轮结束、
   `"continue"` = 下一轮。控制码沿方法链一路往上抛，最顶层的 `_run` 把它
   翻译成 `return` / `continue`。
2. **跨段状态一律走载体**（`_TurnState`，现 55 个字段）：
   轮内量（`acc` / `parsed` / `messages` / `finish_reason` / `pending_final` …）
   每轮刷新，跨轮量（trail / 计数 / 账本 / 证据）按问答累积。
   **常量走参数**：每轮问答只算一次的配额（`tool_step_limit` /
   `observation_limit` / `lookup_output_limit` / `max_nudges` /
   `max_final_continuations`）不进载体。
3. **事件流契约不变**：token / thought / action / observation / reflection /
   plan / notice / final 的字段与顺序一字未改——事件流等价对拍逐条比对。

> 这轮抽块全部是**机器搬运**（AST 切片 + 自动算参数 + 自动改控制流），
> 人在里面只做三件事：定边界、定命名、定「哪些量归载体、哪些走参数」。

### 第一轮：路由族巨型函数

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

**下一步建议**：`_run_tool_action`（669 行）内部还有 5 个天然阶段，
AST 实测的阶段边界与**跨阶段共享名只有 7 个**（action_name / action_arg /
_tool_ok / _result / obs 等），再拆一层的代价已经很低：

| 阶段 | 行数 | 内容 |
|---|---|---|
| guard | ~270 | 永久失败、写工具确认、重复调用、空参、步数上限、allowlist、租约 |
| exec | ~80 | 真正执行工具、拿观察结果 |
| observe | ~95 | 整理成 observation 事件与证据 |
| fail | ~127 | 失败分类与计数（要不要强制收尾） |
| wrap | ~85 | 审计、复核、写入 trail、快照 |

再往后是 `_run` 剩下的 792 行主循环（模型调用与流式解析）—— 它是真正的
状态机，要拆得先把「一轮」的形状想清楚，不是纯搬移能解决的。

另外 `_run_child`（543）/ `_run_shell`（474）的主执行段（分别写 9 / 13 个
跨段状态）可以照 `_run` 的做法拆。

## 13. 第五轮：外壳与子代理两层的主执行段（`_run_shell` 474 → 79 / `_run_child` 543 → 203）

这一轮拆的是另外两层巨型函数：`run()` 下面的**外壳**（`_run_shell`）与
**子代理**（`_run_child`）。两者的共同点是：主执行段都是一整块 `try:`，
块里既有跨段的共享可变状态，又混着「早退 / 断连 / 失败」三类控制流。

### 边界怎么定的

| 层 | 主执行段（拆前） | 拆后 |
|---|---|---|
| 外壳 | `try:` 块 280 行（事件泵 53 + 收尾 154 + 压缩 32 + 失败 22） | `_run_shell` 只剩编排 79 行 |
| 子代理 | `try:` 块 120 行（事件泵 95 + 失败 18 + 还原 1） | `_run_child` 只剩编排 203 行 |

**外壳**拆成 9 个方法：

| 方法 | 职责 |
|---|---|
| `_shell_prepare_resume` | 会话压缩 + 断点续跑现场解析 |
| `_shell_open_turn` | 预算账本 + trace turn + 撤销令牌 + 截止时间 |
| `_shell_gates` | 四道前置门：能力问答 / 预算熔断 / 每分钟限流 / 前置钩子 |
| `_shell_pump` | 把内层 `_run` 的事件流搬到外壳事件流 |
| `_shell_compact_after` | 正常跑完后的历史压缩（要 yield notice，故不在 `finally` 里） |
| `_shell_on_error` | 失败收尾：历史兜底 + 失败原因 |
| `_shell_finish` | 断点落库 / 记忆落库 / 终态判定 / trace / 计价 / 后置钩子 |
| `_shell_persist_checkpoint` | 落 checkpoint |
| `_shell_update_journal` | 换 journal 现场并立刻落盘 |

**子代理**拆成 3 个方法：`_child_prepare`（角色工具策略 → 子 Agent → 提问串，182 行）、
`_child_pump`（事件泵，92 行）、`_child_build_output`（出报告，69 行）。

### 新增两个状态载体（`agent_runtime/run_state.py`）

`_TurnState` 只管 `_run`；这一轮的两层各有自己的共享状态：

- `_ShellState`（14 字段）：外壳跨段共享的可变状态。
- `_ChildState`（38 字段）：一次子代理运行的输入 + 工具策略 + 运行现场。

**为什么外壳必须用载体，而不是局部变量**：外壳是**生成器**。事件在 `yield` 出去
的过程中被消费者打断（客户端断连）时，生成器栈上的局部变量会一起消失 ——
而收尾逻辑（`finally`）恰恰要在断连后读 `final_text` / `journal_state` /
`recovery_events` 才能把现场落盘。所以**跨 yield 存活的状态必须挂在载体上**。
（子代理那一层没有这个约束：它的 `except` 只接 `Exception`，断连时 `GeneratorExit`
不会被接住，块后面的代码根本不会执行。）

### 这次踩到的三个坑

1. **抽出方法不等于抽出早退**：`_child_prepare` 里那条「点名了工具却被安全边界
   筛光 → fail-closed」的早退，原本是 `_run_child` 的 `return {...}`。搬进子方法
   后它变成了子方法的返回值，**没人接** → 9 条守卫红（`KeyError: dropped_tools`）。
   → 抽块时必须把块里的**早退**一并列出来：留在原地，或显式回传给调用方。
2. **源码钉桩守卫会跟着代码搬家失明**：子代理自治那 11 条守卫查的是
   `_run_child` 函数体里的形状（`allowed = [...]` / `list(spec['tools'])` /
   `question = spec['hint']`）。逻辑分家之后它们扫的是**错误的函数**。
   → 沿用 `_run` 那轮的既有做法（`tests/agent_loop_source.py`）：给守卫一个
   **合成视图**（各方法函数体按顺序拼成一个函数 + 载体前缀还原），牙不变。
3. **变异电池的锚点写死了源码文本**：`battery_subagent_step_floor` 的 `_OVERRIDE`
   锚的是 `child.tool_step_override = cap if cap > 0 else SUBAGENT_STEP_FLOOR`，
   搬家后变成 `cs.child.tool_step_override = cs.cap if ...` → 锚点找不到 →
   整块电池脱钩。→ 锚点随代码同步更新（与 `battery_empty_model_turn` 同一套规矩）。
4. **载体字段名必须与拆分前一字不差**：第一版把 `_file_changes` /
   `_seen_file_changes` / `_needs_confirm` 起了个「干净」的载体名
   （`file_changes` / `seen_file_changes` / `needs_confirm`），结果
   `tests/test_write_tool_reports_file_changes.py` 那 4 条守卫（它们查的正是
   `_file_changes.append` 这个形状）当场失明。→ 载体字段名照抄原局部变量名，
   连**下划线**都照抄；§6 的「字段名一字不差」这条约定在这里同样适用。

### 验收

- 事件流等价对拍：**11 场景 / 1034 事件逐条一致**（新增 4 个子代理场景 +
  1 个外壳失败场景；归一化从一层改成递归）
- 类成员对照：HEAD 83 → 95，**丢失 0**
- 变异电池锚点：全部命中（脱钩 0）

## 14. 第六轮设计稿：`api.py:chat`（656 行）的拆分

> 本节是**设计稿**，本轮只出稿不动代码。行号一律标注「**拆分前**」，实现时会漂。

### 14.1 目标与不做的事

**目标**：把 `POST /api/chat` 这个 656 行的端点处理器拆成「可读、可测、边界清楚」的形态，**同时保证 SSE 事件流逐帧不变**。

**本轮不做**：

- 不改 `api.py` 的端点注册顺序、不改 `chat` 的签名与 Form 注解（FastAPI 靠注解注入）。
- 不动前端（`web/`）。
- 不改 `api.py` 的域拆分（8600+ 行 / 203 路由按域拆到 `api_routes/*`）—— 那是**另起一稿**的事。
- 不新增依赖、不改事件流协议。

### 14.2 现状实测（AST 扫描，不是印象）

`api.py:chat` = **3098–3753，656 行**，`async def`，**模块级函数**（不是方法）。

它天然分两段：

| 段 | 拆分前行号 | 行数 | 干什么 |
|---|---|---|---|
| 前段 | 3098–3451 | 354 | 参数归一化 → 视觉准备 → provider/路由/降级 → 上下文 → 覆盖参数 → 打开上下文 |
| 后段 | 3452–3751 | 300 | 嵌套的 `async def event_stream()`：SSE 事件流主体 |

后段内部再分（实测提纲）：

| 块 | 拆分前行号 | 行数 | 干什么 |
|---|---|---|---|
| 流准备 | 3456–3478 | 23 | gen / worker / worker_stop / events / wf_holder / inflight |
| 前置事件 | 3480–3495 | 16 | turn、route、notice×2 |
| 云端脱敏 | 3496–3497 | 2 | cloud_question / cloud_context |
| `produce()` | 3499–3605 | 107 | **worker 线程体**：瞬断重试层 + fallback + 消费 `agent.run` |
| 起 worker + 心跳计时 | 3607–3612 | 6 | — |
| 事件泵 `while True` | 3614–3680 | 67 | poll → 心跳 / 转发 payload / workflow 补发 |
| 断连分支 | 3681–3683 | 3 | `(GeneratorExit, KeyboardInterrupt, CancelledError)` → raise |
| 失败收尾 | 3685–3726 | 42 | 分类 → `final(status=error)` → consistency → yield |
| finally 收尾 | 3729–3750 | 22 | 停 worker、关生成器、pop pending、终态标记、turn trace |
| done | 3751 | 1 | `data: {"type":"done"}` |

**三个关键实测结论**：

1. **闭包面是干净的单向数据流**：`event_stream` 读 `chat` 的 **21 个名字，全部只读**（写方向为 0）。
2. **流内真正跨段共享的只有 6 个**：`gen` / `worker` / `worker_stop` / `events` / `wf_holder` / `inflight`（其余 32 个局部都是单块内自用）。
3. **yield 点共 9 处**（见 §14.6）。

### 14.3 边界怎么定的：两段式

- **前段 = 算出这一轮怎么跑**（纯判定与配置装配，不产帧）。它的产物就是那 21 个「只读输入」。
- **后段 = 把这一轮跑成 SSE**（唯一产帧的地方）。

这两段之间**没有回写**——这是实测出来的事实，不是设计愿望：`event_stream` 对 `chat` 的 21 个闭包名一个都没改。所以状态边界可以直接照着闭包面切，不需要发明新的抽象。

### 14.4 状态边界：两个载体

沿用 §6 的约定：**字段名与拆分前一字不差**，载体不带方法、不做校验。

#### `_ChatState`（前段 → 后段的接口，21 个字段）

| 组 | 字段 |
|---|---|
| 身份 | question、session_id、turn_id、resume_checkpoint、request |
| 视觉 | vision_images、vision_context、vision_audit |
| 路由 | routing、is_cloud、cloud_llm、search_degraded_notice |
| 覆盖参数 | tool_mode_override、plan_mode_override、web_enabled_override、thinking_enabled_override、thinking_level_override、narration_override |
| 运行对象 | selected_agent、_redir_key |
| 上下文 | request_context、agent_context |

字段数与闭包面**一一对应**（21 = 21），不多不少。

#### `_ChatStreamState`（流内跨段共享，6 个字段）

gen、worker、worker_stop、events、wf_holder、inflight。

**为什么这 6 个必须进载体**：`produce()` 是个嵌套函数，靠 `nonlocal gen` 把 worker 线程里拿到的生成器句柄交回主循环；抽成方法后 `nonlocal` 失效，`gen` 必须挂在载体上。`worker_stop` 是断连信号，`events` 是跨线程队列，`inflight` 是前端「继续」用的现场账本——这四个跨线程/跨块共享。

**载体放哪**：新建 `agent_runtime/chat_state.py`（api.py 侧目前没有载体先例，agent.py 的在 `agent_runtime/turn_state.py`；同名不同层，分文件更清楚）。

### 14.5 子方法清单（11 个）

#### 前段 6 个

| 方法 | 拆分前行号 | 职责 | 边界注意 |
|---|---|---|---|
| `_chat_normalize` | 3121–3142 | 14 个 `_plain_field` 归一化、images 类型校验、turn_id/question 规整 | 纯函数式，不碰运行时 |
| `_chat_prepare_vision` | 3143–3209 | 视觉准备、**空问题 400 早退**、ui_context 分支、视觉审计 | 早退要能中止整轮 → 返回控制码 |
| `_chat_resolve_provider` | 3210–3244 | provider/embedding 探测、云端方案解析、ollama 降级判定 | 产出 search_degraded_notice |
| `_chat_resolve_context` | 3245–3300 | code_root、摄取、hints、项目审计、selected_agent、实时告警特例 | 产出 request_context |
| `_chat_resolve_overrides` | 3301–3441 | tool/plan/web/thinking/narration 覆盖 + 各 ui_context 特例 | 产出 6 个 override |
| `_chat_open_context` | 3442–3451 | 重定向键、活跃 run、request_context、agent_context | 产出 agent_context |

#### 后段 5 个

| 方法 | 拆分前行号 | 职责 | 边界注意 |
|---|---|---|---|
| `_chat_stream_open` | 3456–3498 | 流准备 + 前置事件（turn/route/notice×2）+ 云端脱敏 | **副作用必须留在流开始时**（见 §14.7） |
| `_chat_worker_produce` | 3499–3605 | worker 线程体：瞬断重试层、fallback、消费 `agent.run` | 产出写进 `ss.events`；`gen` 写 `ss.gen` |
| `_chat_stream_pump` | 3607–3680 | 起 worker、心跳计时、事件泵（poll/转发/workflow 补发） | 唯一产流帧的循环 |
| `_chat_stream_fail` | 3685–3726 | 失败收尾：分类 → final(status=error) → consistency → yield | 整体搬，别拆散 |
| `_chat_stream_finish` | 3729–3750 | 停 worker、关生成器、pop pending、终态标记、turn trace | 对应 finally |

拆完 `chat` 剩下 **~70 行编排**：归一化 → 视觉 → provider → 上下文 → 覆盖 → 打开上下文 → 返回 StreamingResponse。

### 14.6 事件流不变约束（硬约束）

9 个 yield 点 → 6 种帧：

| 序 | 帧 | 触发条件 | 不变式 |
|---|---|---|---|
| 1 | `data: {"type":"turn",turn_id,session_id}` | 无条件 | **必须是第一个 data 帧** |
| 2 | `data: {"type":"route",route,complexity}` | 无条件 | 必须是第二个 |
| 3 | `data: {"type":"notice",text}` | vision_audit.mode ∉ (none, native) | 在 route 之后 |
| 4 | `data: {"type":"notice",text}` | search_degraded_notice 非空 | 在 route 之后 |
| 5 | `: keep-alive` | 心跳到期 | **不是事件**（parse_events 丢弃），但行数与位置不得被吞 |
| 6 | `data: {payload}` | agent.run 的事件 | **逐条原样转发** |
| 7 | `data: {"type":"workflow","phase":"started",...}` | start_workflow 成功后 | 紧跟触发它的那条 |
| 8 | `data: {final}` | final 事件 | — |
| 9 | `data: {"type":"done"}` | 无条件 | **必须是最后一个 data 帧** |

#### 六条不变式（拆完必须逐条成立）

1. **帧序**：`turn` → `route` → (notice*) → (流) → `done`。三者（turn/route/done）的相对位置**不可换**。
2. **心跳不是事件、但不能消失**：`: keep-alive` 行在 `parse_events` 里被丢掉（那是协议设计），但它夹在流中间——被吞掉前端就失去断连探测能力。
3. **透传不改形**：第 6 类帧是 `agent.run` 的原样转发（`json.dumps(payload, ensure_ascii=False)`），**不得加字段、不得改顺序、不得合并**。
4. **失败终态形状**：`final.status == "error"` + `error_kind`，可选 `checkpoint_id` / `consistency` / `consistency_ratio`，且**只出现一次**。
5. **done 必到**：正常路径与失败路径都要在最后 yield 一次 `{"type":"done"}`；客户端断连（GeneratorExit）路径**不** yield done——那是异常传播，不是契约违约。
6. **早退不产帧**：问题为空时直接 `JSONResponse(400)`，**不产生任何 SSE 帧**。

#### 怎么保证它不变（判据，不是口号）

| 判据 | 落点 |
|---|---|
| **事件流对拍**（新增） | `.tmp/chat_equiv.py`：录基线 → 拆后**逐帧比**；既比事件表（parse_events），也比**原始行**（心跳算在内） |
| **既有判据不许动** | `agent_runtime/chat_sse.py`（纯函数判定表）、`tests/test_chat_sse.py`、`tests/mutation/battery_chat_sse.py`（18 条变异下限），以及另外 26 个涉及 chat SSE 的测试文件 |
| **结构性锚点**（新增） | 拆完后 `chat` 里**不允许再出现 `yield`** —— SSE 帧只允许在 `_chat_stream_*` 里发。这条把「有人顺手在编排层加了一帧」变成不可能 |
| **顺序锚点**（新增） | `_chat_stream_open` 里「turn → route → notice」三帧的**相对次序**被一条守卫钉住（不是「都出现过」） |

### 14.7 失败形态与处置

| 形态 | 症状 | 处置 |
|---|---|---|
| **帧序漂移** | 前端拿不到 route；done 提前或缺失 | 事件流对拍当场抓到 |
| **副作用提前** | `push_pending_stream()` / `_register_in_flight_turn()` 从「流开始时」变成「请求时」——**请求还没开始消费就占了 pending stream** | 载体初始化留在 `_chat_stream_open` **内部**（流开始时惰性执行），不挪到 `chat` 里 |
| **闭包丢失** | `nonlocal gen` 失效 → worker 句柄丢 → 断连停不掉后端 worker | `gen` 进 `_ChatStreamState` |
| **异常被拆散** | `except BaseException` 的收尾被切碎 → 失败终态形状变了（少了 error_kind / consistency） | `_chat_stream_fail` **整体搬**，不拆 |
| **早退丢失** | 空问题不再 400，直接进流 | `_chat_prepare_vision` 返回控制码，`chat` 据此提前返回 |
| **心跳被吞** | 断连探测失效（长思考时前端以为死了） | 泵内 keep-alive 不动 |
| **透传被改形** | payload 多了字段 / 顺序变了 → 前端渲染错位 | 对拍比原始行 |

### 14.8 验收标准

1. **事件流对拍**：多场景（正常轮 / 工具轮 / 失败轮 / 降级轮 / 早退 400）逐帧一致，含心跳行。
2. **既有判据全绿**：chat SSE 相关测试文件 + 变异电池 ≥18 条。
3. **新增两条锚点守卫**：`chat` 里无 `yield`；turn→route→notice 次序被钉。
4. **不改签名**：`chat` 的参数与 Form 注解逐字未变（FastAPI 注入靠注解）。
5. **CI 9/9 全绿**。

### 14.9 风险登记册

| 风险 | 等级 | 缓解 |
|---|---|---|
| api.py 侧没有载体先例，字段表可能定错 | 中 | 字段表**照抄闭包面实测**（21 个），并用 `tests/agent_loop_source.py` 的前缀还原机制保持源码钉桩可用 |
| `_register_in_flight_turn` 的时机 | **高** | 载体初始化留在流内（§14.7） |
| `produce()` 的 `nonlocal` 语义 | **高** | `gen` 进载体 |
| FastAPI 端点注入依赖注解 | 低 | 签名与注解一字不动 |
| 前端对帧序有隐含依赖 | 中 | 对拍 + 顺序锚点 |

### 14.10 待确认事项

- `api.py` 本身按域拆到 `api_routes/*`（另起一稿），**本轮不做**。
- 是否给 api.py 单独建 `agent_runtime/chat_state.py`，还是并入现有 `agent_runtime/turn_state.py`？前者更清楚（不同层、不同生命周期），后者少一个文件。

## 15. 实施结果与对 §14 的修正（2026-11-25）

按 §14 的设计实施完成。**代码形态与设计稿有两处不一致**，都是实施中实测出来的，
记在这里（设计稿保留原样，便于对照「当初以为是什么样」）。

### 15.1 实际形态

| 项 | §14 设计 | 实际 | 说明 |
|---|---|---|---|
| 子方法数 | 11（前段 6 + 后段 5） | **13（前段 7 + 后段 6）** | 后段的「流准备」必须**独立于 try 之外**（见 §14.7 的「副作用提前」），所以从「流准备 + 前置事件」里拆出两个方法 |
| `_ChatState` 字段 | 21（闭包面） | **38** | 21 是「前段 → 后段」的接口；另有 17 个是**前段各方法之间**跨界的变量（入参 + 派生量），不收进载体就没地方放 |
| `_ChatStreamState` 字段 | 6 | **9** | 加 `cloud_question` / `cloud_context`（流准备算出来、worker 线程体要用）与 `stopped`（见 §15.2 第 4 条） |
| 载体位置 | 待定 | `agent_runtime/chat_state.py` | 与 `agent_runtime/turn_state.py` 分开：生命周期不同、字段不重叠 |

`chat` 本身从 **656 行降到 40 行**（只剩编排：归一化 → 守卫 → 视觉 → provider →
上下文 → 覆盖 → 打开上下文 → 返回 StreamingResponse）。

### 15.2 实施中踩到的四个坑

1. **抽出方法不等于抽出早退**（§14.7 已列，实测确认代价）。
   `_chat_resolve_provider` 里那条 `return StreamingResponse(_ollama_down_stream(...))`
   （ollama 不可达时的降级流）搬进子方法后**没人接** → 降级路径直接失效。
   实测：前段一共有 **5 条早退**（409 / 400×3 / 降级流），必须逐条列出来，
   子方法返回「响应或 None」，编排层负责回传。
2. **async 判定不能用子串**。用 `"await " in body` 判协程时，注释里出现的
   "await" 会把**同步块误判成协程** → 协程没被 await → 那一段整个走空
   （表现为「模型没被调用 + final error」）。→ 判定必须看 **AST 的 Await / Yield 节点**。
3. **async 生成器里没有 `yield from`**。事件流拆成子生成器后，`yield from` 是语法错误，
   只能 `async for ... yield`。代价：**子生成器的清理不再由外层确定性触发**
   → 所以「停 worker / 关生成器 / 终态标记」这些收尾必须留在**外层的 `finally`**。
4. **`return` 的语义要带出去**。原循环里的 `return` = 直接退出流、**不发 done 帧**；
   拆成子生成器后 `return` 只会结束子生成器，外层会继续往下走 → 多发一个 done。
   → 用 `ss.stopped` 标志把「不发 done」这个语义带回最外层。

### 15.3 验收

| 项 | 结果 |
|---|---|
| 事件流对拍 | **11 场景 / 136 帧逐帧一致**（含原始行 —— 心跳也在内） |
| 抽出的 13 块 | 经「反向重命名」（`st.X`/`ss.X` → `X`）与**原文逐行等价**，差异仅为有意改动（去掉 `nonlocal gen`、线程目标改为模块级函数、`return` 前加 `ss.stopped = True`） |
| 相关测试面 | 27 个 chat SSE 相关测试文件 + 子代理/编排面全绿 |
| 新增结构性锚点 | `tests/test_chat_stream_contract.py` 4 条（编排层不许产帧 / turn→route 次序 / done 在收尾之后 / 心跳仍在） |
| 失明的源码守卫 | 2 条改看**合成视图**（`tests/agent_loop_source.py` 新增 `agent_chat_source()` / `agent_chat_stream_source()`）—— 与 `_run` 那轮同一套办法 |
