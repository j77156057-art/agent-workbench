# v2 自主舱删除阶段实施计划

> 状态：**待审批**（计划已拟，实施未启动）
> 上游：`docs/cockpit-v2-redesign.md`（v2 设计 + §7 三处清场已落 commit `402ed8b`）
> 范围：§1.3 删除清单 2.0 中**已确认可删**的 6 个后端模块 + 1 个前端组件 + 拆 §1.2 真实依赖 + App.vue 8 处收口
> 不可删（本文不删，但列出确认依据）：`lanes.py` / `independent_review.py` / `verification.py` / `visual_acceptance.py` / `acceptance.py` / `cockpit_policy.py` / `WorkflowCard.vue` / `FlowCanvas.vue`

---

## 0. 元约束（跨全程生效）

| 约束 | 依据 |
|---|---|
| **两并发写入者** | 工作记忆「铁律」：动手先 `git status`；**禁 `git add -A`**；只加明确路径；提交前 `git diff -U0 <f>\|grep '^@@'` 核 hunk |
| **交叠文件**（`App.vue` 等前端 + `api_routes/agent.py` 等后端） | 提交前必须 `git status -sb` 检外部改动；如有冲突先 `git fetch` + 与并行 agent 协调（双写者纪律） |
| **新架构未实施** | 文档状态「决策已定，**未实施**」；当前 `game_workflow` 仍被 `tools.py`(12)/`api_routes/agent.py`(8)/`agent.py`(4)/`langsmith.py`(4) 硬引用——**直接整库删会打断在跑 Agent**。本计划通过分阶段拆耦合让删除逐步变成「只删自己」 |
| **`.py` 测试期间不可动**（freeze-guard） | 后台跑测试期间不可改其作用域内任何 `.py`；测试命令也得避开被测文件 |
| **safe-delete 批量闸** | 删 >50 文件 / >5 目录时会被 `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 拦；本计划每步 ≤6 文件，无需绕；如某步聚合 >50 应主动拆批 |
| **`CODEBUDDY_SAFE_DELETE_ENABLED=0`** 仅在清 `web/assets` 等 gitignore 产物时用，**不能用于删源码** | 否则破坏双写者铁律的可见性 |
| **沙箱 MITM 拦 `github.com`** | 推送走 `github-api-push` skill（Git Data API，保 SHA、快进、不强推） |
| **桌面端只吃 `web/`** | 前端改动收尾必跑 `npm run build`（先 `cp -r web D:/Temp/web-bak-<时间>/`） |

---

## 1. 前置：与并行 agent 协调

`git status -sb` 在每步开始前都跑一次。本轮 2026-10-13 19:04 侦察到并行 agent 在改：
- `config.py`（CRLF 归一化伪改动，无内容 diff，但 status 显示 M）
- `tests/conftest.py`（同上）
- `tests/test_session_owner_project.py`（同上）

这些文件**与本计划删除集无交叠**（不删它们），但实施期间如出现新的非我改动：
1. 立即**暂停**当前步骤
2. 通知并行 agent（via handoff/START-HERE.md 约定的渠道）
3. 协商 commit 顺序（建议：本计划作者先 commit 自己部分，对方再 commit 自己的）

**关键协调点**：`App.vue` 是双写者交叠文件。本计划 §3 有 8 处 `App.vue` 改动（删除 `cockpitRef`/`cockpitReady`/`cockpitRuntime`/`cockpitFiles`/`everMounted.cockpit`/`workspace==='cockpit'` 全部 8 个引用位）。提交前必须 `git status` 确认 App.vue 无对方未提交改动，否则按双写者纪律先复核再决定。

---

## 2. 目标行数（与 §1.3 修正后一致）

| 删 | 模块 | 行数 | 验证 |
|---|---|---|---|
| **后端** | `game_workflow.py` | 5,508 | 唯一编排本体；新设计取代 |
| | `workflow_eval.py` | 291 | 仅 `game_workflow` 一族引用 |
| | `workflow_profiles.py` | 261 | 仅 `game_workflow` 一族引用 |
| | `workflow_reflection.py` | 183 | 仅 `game_workflow` 一族引用 |
| | `plan_state.py` | 203 | 被 L1 决策索引取代（决策 3/10） |
| | `acceptance_contract.py` | 80 | 仅被 `game_workflow.py:29-30` import（`acceptance_from_tasks` / `approve_acceptance`） |
| **前端** | `AutonomousCockpit.vue` | 3,454 | 整舱组件；实时流/窗口嵌入已迁出 |
| | `CockpitApprovalQueue.vue` | 148 | 仅被 `AutonomousCockpit.vue:36, 2960` 引用 |
| **后端小计** | | **6,526** | |
| **前端小计** | | **3,602** | |
| **合计** | | **10,128** | 与 §1.3 修正值一致 |

---

## 3. 五阶段分步实施（按 §6 验收判据倒推依赖）

> **设计原则**：每阶段都满足「向后兼容可回滚」——即在删完前一个阶段后，被删模块的旧调用方**仍可工作**；每个阶段结束都跑 `npm run build` + 全量 pytest；如失败可 `git reset --hard <prev-commit>` 干净回退。

### 阶段 A：后端 `acceptance_contract.py` 迁移到 game_workflow 子模块（**迁出顶级，非删**）

> **修订（self-review v1.0 → v1.1）**：实测 `game_workflow.py` 用 `acceptance_contract` 符号的位置**远超 import 行**——共 **22 处**（`:29-30` import + `:594` `unmet_acceptance_items` + `:969/:2796/:2838/:3240-3243/:4278-4281/:4322-4358/:4522-4523` 字段操作与调用），分布在 `state.acceptance_contract` 字段操作、`acceptance_from_tasks` / `approve_acceptance` / `revise_acceptance` / `acceptance_on_plan_changed` 4 个函数调用。
>
> 另一关键发现：`agent_runtime/game_workflow` 是**单文件**（`game_workflow.py` 312KB），**55 处外部 import 站点**（含 `api.py:161` / `api_routes/agent.py:14` / `tests/mutation/battery_*.py` / `agent_runtime/acceptance.py:21`）。把 game_workflow.py 拆成包（`__init__.py` + `_workflow.py`）会触动 55 个 import 站点，**改动面过大**。A 阶段采用**最小侵入方案**：
>
> - 保留 `agent_runtime/game_workflow.py` 单文件结构（不动 55 处 import 站点）
> - 把 `agent_runtime/acceptance_contract.py` 的 4 个公开符号（`AcceptanceError` / `approve_acceptance` / `acceptance_from_tasks` / `acceptance_on_plan_changed` / `revise_acceptance` / `unmet_acceptance_items`）**复制到 `game_workflow.py` 顶部**作为内嵌辅助函数（私有 `_acceptance_*` 命名）
> - 改 `game_workflow.py:29-30` import 为内嵌调用
> - **不删** `agent_runtime/acceptance_contract.py` 顶级模块（D 阶段一起删；D 阶段前的 B/C 阶段不引用它，无影响）
> - **关键约束**：B/C 阶段**不动 acceptance_contract**——但 `workflow_eval.py` / `workflow_profiles.py` / `workflow_reflection.py` 自身也不 import acceptance_contract（实测 0 命中），所以 B/C 阶段安全

**A.1 改动**
- **复制** `agent_runtime/acceptance_contract.py` 4 个公开符号（保留 80 行代码全文作为「权威实现」）→ 在 `agent_runtime/game_workflow.py:25` 之前插入 `_ACCEPTANCE_CONTRACT_SNIPPET` 注释块，**指向** `acceptance_contract.py` 而非复制（避免 80 行双份）
- **改 import 路径**：`agent_runtime/game_workflow.py:29-30` 的 `from .acceptance_contract import AcceptanceError, approve as approve_acceptance` 与 `from .acceptance_contract import (from_tasks as acceptance_from_tasks, ...)` 改为：
  ```python
  from .acceptance_contract import (
      AcceptanceError, approve as approve_acceptance,
      from_tasks as acceptance_from_tasks,
      revise as revise_acceptance,
      on_plan_changed as acceptance_on_plan_changed,
      unmet_items as unmet_acceptance_items,
  )
  ```
  （**仅做"未公开符号补全"**，不挪文件、不内联——A.2 实测自审 v1.0 的"22 处用法"全部是字段/方法调用，import 行的 6 个符号补齐即可）
- **保留其余 22 处用法不动**（`AcceptanceError` / `state.acceptance_contract` / 4 个函数名都不变）
- **不删测试**

**A.2 验收**
- `pytest tests/ -q` 全量绿（A 阶段要确认无副作用）
- `python -c "import api"` 无 ImportError
- `git grep "from \.acceptance_contract\|agent_runtime\.acceptance_contract\b"` 应仅剩 `agent_runtime/game_workflow.py:29-30` 1 处业务引用（其余都在 acceptance_contract.py 自身或 test）
- `git grep "from \._acceptance\b"` 应**为空**（未引入 `_acceptance` 子模块；A.1 没动文件结构）

**A.3 提交**：commit msg「A 阶段：`acceptance_contract` 6 个公开符号补全到 game_workflow import 行（`unmet_items` / `revise` / `on_plan_changed` 之前隐式使用）。本阶段**只补 import，不挪不删**。B/C/D 阶段按需处理 acceptance_contract 的去留。」

**A.4 文档入库**：本计划 `docs/cockpit-v2-deletion-plan.md`（v1.1 修订版）随本 commit 一起入库（按 `docs/next-steps.md` §0 规则，doc-only 必随真实改动入库；本阶段是该计划的首笔真实改动）。

**A.5 阶段 A 后状态**：`acceptance_contract.py` 顶级模块**仍未删**（80 行），但只剩 game_workflow.py 一处业务引用。真正删除推迟到 D 阶段与 game_workflow 一起删。**A 阶段行数 0 减**——避免"小步快走"承诺落空；A 阶段真实价值是**为 D 阶段铺路**，把 import 路径稳定性核到字节级。

---

### 阶段 B：后端 `plan_state.py` 独立迁移（L1 决策索引取代；与 game_workflow 解耦前置）

**B.1 改动**
- **读全文** `plan_state.py`（203 行）—— 查是否还有 game_workflow 之外的引用（按本轮侦察只 game_workflow 一族，但需 `git grep -n "plan_state"` 实证）
- **判断保留/迁移/删除**：
  - 若功能**被新设计完全取代**：直删
  - 若有**别的模块真在用**：迁移到 `agent_runtime/memory/` 或 `agent_runtime/checkpoint/` 新位置（按 §4.1 三层记忆层 L2 的位置）
- **本计划默认**：直删（§1.3 已分类为「被 L1 决策索引取代」），但实际动手前**必须** `git grep` 实证无外部引用，否则升级为「迁移」步骤
- 删 `game_workflow.py` 中对 `plan_state` 的所有 import + 调用
- 删 `tests/test_plan_state.py`（按本轮侦察 23 引用，主要是 self-test）

**B.2 验收**
- `pytest tests/ -q` 全绿（与本阶段删除集无交叠的测试应不受影响）
- `git grep "plan_state"` 应为空

**B.3 提交**：commit msg「`plan_state` 移除；新设计 L1 决策索引取代其能力（决策 3/10）」

---

### 阶段 C：后端 `workflow_*` 三件套（仅 game_workflow 一族引用；最安全的起步）

**C.1 改动**
- **读全文** `workflow_eval.py` / `workflow_profiles.py` / `workflow_reflection.py` 各自 docstring + 头 20 行，确认无 game_workflow 之外的引用
- 删 3 个文件
- 删 `game_workflow.py` 中对 3 个模块的 import + 调用
- 删测试：
  - `tests/test_workflow_eval_dataset.py`（按本轮侦察 1 处 import）
  - `tests/test_workflow_profiles.py`（2 处）
  - 查 `git grep` 是否有 self-test

**C.2 验收**
- `pytest tests/test_workflow_* tests/test_game_workflow* -q` 全绿
- `git grep "workflow_eval\|workflow_profiles\|workflow_reflection"` 应为空

**C.3 提交**：commit msg「`workflow_eval` / `workflow_profiles` / `workflow_reflection` 移除；仅 `game_workflow` 一族使用，无外部依赖」

---

### 阶段 D：后端 `game_workflow.py`（主体，5,508 行 + 跨模块解耦）

> 这是**最关键**的一步。`game_workflow` 仍被 `tools.py`(12)/`api_routes/agent.py`(8)/`agent.py`(4)/`langsmith.py`(4) 引用，需先**分批把这些引用迁出**，再删 game_workflow 本身。

**D.1 拆耦合子步骤（按依赖深度从外到内）**

| 子步 | 目标 | 动作 | 验证 |
|---|---|---|---|
| D.1.1 | 让 `tools.py` 12 处不依赖 game_workflow | 对每处引用：① 若是工具 import 整个 game_workflow 改 import 所需具体符号；② 若无替代符号，标记为「**该工具依赖 game_workflow 的具体能力 X**，需先有 X 的替代实现才能删」 | 改完跑 `tools` 相关测试 |
| D.1.2 | 让 `api_routes/agent.py` 8 处不依赖 | 同 D.1.1 模式 | `pytest tests/test_api_routes.py tests/test_workflow_chat_stream.py` |
| D.1.3 | 让 `agent.py` 4 处不依赖 | 同上 | `pytest tests/test_agent* tests/test_agent_capacity.py` |
| D.1.4 | 让 `langsmith.py` 4 处不依赖 | langsmith 是可选遥测；若 game_workflow 调用 langsmith 记录工作流事件，可改为 `if HAS_GAME_WORKFLOW: ...` 守卫或直接删 | 改完跑 langsmith 自身测试（如有） |
| D.1.5 | 改 `game_workbench.py`（`api.py` 同级，是真实 server entry） | 同样模式 | `pytest tests/test_game_workbench*.py`（如存在） |

> **D.1.6 关键决策点**：每处 game_workflow 引用都是「`game_workflow` 暴露的某能力 X」→ 新设计里 X 的替代实现是否就绪？若**未就绪**（决策 7/8/9 仅 §7 三处落 commit，整库 §4 新架构未实施），则该处只能：
> - **选项 α**：保留 game_workflow，**本阶段 D 不执行**（推迟到新架构就绪后）
> - **选项 β**：在 game_workflow 与调用方之间加一个 shim 层（薄 import），删 game_workflow 后 shim 变 stub 或抛 `NotImplementedError`——**这违反 §1「全部推翻重做」精神**，不推荐
> - **本计划推荐选项 α**：D 阶段**整阶段挂起**，先把 A/B/C 三个安全阶段做完，验证「只删孤立模块」的可行性；D 推迟到新架构至少 L0/L1 落地后再启动

**D.2 D 阶段完整流程（如选 α 推迟）**
- D.1.1–D.1.5 子步骤**暂不动**；仅在 A/B/C 三阶段全部完成后做最终侦察：
  - `git grep "game_workflow" -- '*.py'` 应只剩 0 处（按本轮侦察，A/B/C 三阶段删除后 game_workflow 已无内部引用，但 tools/agent/api_routes 的 12+8+4+4=28 处外引用仍存）
  - 此时**必须**先有 D.1.1–D.1.5 的拆耦合方案与替代实现，否则不能进 D.2

**D.3 D.2 主删子步骤（仅 D.1 全绿后执行）**
- 删 `game_workflow.py`
- 删测试（按本轮侦察）：
  - `tests/test_game_workflow.py`（24 引用）
  - `tests/test_game_workflow_e2e.py`（1）
  - `tests/test_workflow_*.py` 系列（与 game_workflow 强耦合的共 ~14 个）
  - `tests/mutation/battery_workflow_events.py` / `battery_plan_dag.py` / `battery_replan_rejection.py` / `battery_failed_then_remediated.py`（4 个 mutation battery）
  - 与 game_workflow 关联的 acceptance 项目

**D.4 验收（§6 判据 10）**
- 末尾整体复核（`independent_review`）能跨票抓到集成错误——此判据在 §4.5 已落地（commit `402ed8b` 的 fail-closed 改动）
- 实时对话视频流与窗口嵌入**不回归**（判据 1，阶段 E 后验）

**D.5 提交**：每 D.1 子步独立 commit，commit msg 注明「拆 game_workflow 耦合 — 子步 N/5（外层 tools / api_routes / agent / langsmith / game_workbench）」

---

### 阶段 E：前端 `AutonomousCockpit.vue` + `CockpitApprovalQueue.vue`（与 §1.2 App.vue 8 处收口同步）

> 后端 A/B/C/D（按 α 选项可能仅 A/B/C）做完后，前端删除才能安全启动。
> 实时流与窗口嵌入已在 commit `402ed8b` 迁出舱（§7 三处清场第 1 项）。
> §1.2 列了 9 处真依赖/语义耦合，**8 处在 App.vue，1 处在 workflow-format.ts 注释**。

**E.1 App.vue 9 行号收口（与 §1.2 第 39-44 行 + 本轮侦察对照；commit 402ed8b 后行号偏移 +5）**

> **修订（self-review v1.0 → v1.1）**：原表标 :650/:651 "保留" 是误读 §1「保留 ①实时对话视频流 ②窗口嵌入」——保留的是**机制**（ChatDock + SceneRuntimePanel），不是具体 id。删舱后 `workspace === 'cockpit'` 恒假，:650/:651 永远不显示，应**删**。原表漏 :688（`#wb-chat-slot` 的 v-show 条件）。

| 行号 | 当前内容 | 删除后状态 |
|---|---|---|
| `:38` | `const AutonomousCockpit = defineAsyncComponent(...)` | **删整行** |
| `:68` | `const cockpitReady = ref(false)` | **删整行** |
| `:69` | `const cockpitRuntime = ref(false)` | **删整行**（注意：仅当 §7.0 第 1 项已完成——本轮已完成） |
| `:70` | `const cockpitFiles = ref(false)` | **删整行** |
| `:71` | `const cockpitRef = ref<{ showOutputs: () => void } \| null>(null)` | **删整行** |
| `:90-103` | `everMounted` 中 `cockpit: false` + `watch(workspace)` 进入 cockpit 的整段 | **删 cockpit 字段 + 整段 watch**（若 watch 整段只服务 cockpit 则删整段；否则只删 `if (v !== 'cockpit') return` + `everMounted.cockpit = true` + nextTick 内的 `.acp` selector） |
| `:344` | `else if (e.key === '4') { ... setWorkspace('cockpit') }` | **删整行**（快捷键 4 移除；或重新定义为「窗口嵌入」快捷键——按 §1「产品范围：只保留①实时对话视频流 ②窗口嵌入」，窗口嵌入已在主区，可保留 `4` 键并改绑到 `runtimeResident`） |
| `:603` | `v-show="workspace !== 'cockpit' \|\| cockpitFiles"` | 改为 `v-show="!runtimeResident"`（cockpitFiles 仅当 cockpit 在场时才有意义；删舱后该字段消失） |
| `:650` | `<div id="wb-cockpit-runtime-slot" v-show="workspace === 'cockpit' && cockpitRuntime">` | **删整行**（删舱后条件恒假，slot 永不显示；窗口嵌入由 :646 `#wb-playpane-slot` 承担） |
| `:651` | `<div id="wb-cockpit-chat-slot" v-show="workspace === 'cockpit'">` | **删整行**（同上；ChatDock 由 :688 `#wb-chat-slot` 承担并合并 `scope="default"`） |
| `:655-656` | `<AutonomousCockpit ref="cockpitRef" v-if="everMounted.cockpit" ... />` | **删整块**（含 3 个 `@event` 绑定） |
| `:657` | `<div v-if="workspace !== 'cockpit'" class="wb-editor-shell">` | 改为 `<div v-show="..." class="wb-editor-shell">` 或直接去掉 v-if（删舱后条件恒真，v-if 多余；按风格优先去掉 v-if） |
| `:688` | `<div id="wb-chat-slot" v-show="workspace !== 'cockpit' && !runtimeResident" />` | 改为 `<div id="wb-chat-slot" v-show="!runtimeResident" />`（删舱后 `workspace !== 'cockpit'` 恒真） |
| `:701` | `<Teleport v-if="cockpitReady" defer to="#wb-cockpit-chat-slot">` | **改**为 `defer to="#wb-chat-slot"`（与 `:700` ChatDock 默认合并；cockpit scope 不再存在） |
| `:702-704` | `SceneRuntimePanel :mode="...cockpit..."` | **改**：删 `cockpitReady && cockpitRuntime` 条件分支，:mode 与 :host 简化为 `runtimeResident ? 'docked' : 'popup'` / `'#wb-playpane-slot'` |

**E.2 文件级删除**
- 删 `frontend/src/workbench/components/AutonomousCockpit.vue`（3,454 行）
- 删 `frontend/src/workbench/components/CockpitApprovalQueue.vue`（148 行）
- 检查并更新 `frontend/src/workbench/liveStreamControl.ts:5` / `liveAudioControl.ts:4` 的「由 AutonomousCockpit 驱动」注释（按 §1.2 改注释即可）

**E.3 workflow-format.ts:146 语义耦合**
- 注释提到「与 AutonomousCockpit.timelineLabel 同步」，**4 个状态键**（`skill_candidate` 等）实际是被 WorkflowCard / ChatDock / AskMessage 三处共享的 `statusLabel` 共同消费
- 改注释：「与 WorkflowCard.timelineLabel 同步」即可（WorkflowCard 保留，本身就有 `timelineLabel`）

**E.4 verification + typecheck + build**
- `cd frontend && npm run build`（先备份 `web/` 到 `D:/Temp/web-bak-<时间>/`）
- 走 commit `402ed8b` 同款流程：复制 skill 脚本到 `D:/Temp/` → 跑 `push_via_api.py`（待后端 commit 一起推，不分段推以免 `main` 中间状态半残）

**E.5 验收**
- `npm run typecheck` 0 错 0 警
- `npm run build` 绿
- 启动 `uvicorn.run('api:app', port=8100)`，playwright(msedge) 访问 `http://127.0.0.1:8100/workbench.html`：
  - 切到任何工作区，**没有**「自主开发舱」tab 或入口
  - 切到代码/资产/概览工作区时窗口嵌入槽位（`#wb-playpane-slot`）仍工作（保留项 ② 判据 1）
  - 实时对话视频流（保留项 ① 判据 1）若启用，ChatDock `scope="default"` 仍能 toggle 摄像头/屏幕共享
  - 整个界面不出现"自主开发舱"字样、不出现 `wb-cockpit-*` id 的视觉残留

**E.6 提交**：commit msg「前端删 `AutonomousCockpit` + `CockpitApprovalQueue`；App.vue 8 处 `cockpit*` 收口；窗口嵌入 + 实时流保留（判据 1 验证）」

---

## 4. 与 §6 验收判据的对应表

| 判据 | 验证方法 | 阶段 | 状态 |
|---|---|---|---|
| 1. 实时对话视频流 + 窗口嵌入不回归 | E.5 playwright E2E（§7.0 同款流程） | E | 计划内 |
| 2. 跨 lane 写拦截 | 已由 commit `402ed8b` 的 `test_lanes_write_gate.py` 5 测试绿覆盖 | （前置） | 已通过 |
| 3. L1 索引只增不改 | 新架构未实施，无 L1 实体 | — | **主动放弃**（依赖 §4 实施） |
| 4. A↔B 循环等待放弃并上报 | 新架构未实施；保留待 §4.3 实施后验 | — | **主动放弃** |
| 5. 新舱会话隔离 | 同上 | — | **主动放弃** |
| 6. 重切上限 1 | 同上 | — | **主动放弃** |
| 7. 写工具拦截越界写 | 同 2 | （前置） | 已通过 |
| 8. fail-closed：票无真实命令输出不可关闭 | 已由 commit `402ed8b` 的 `test_independent_review_fail_closed.py` 2 测试绿覆盖 | （前置） | 已通过 |
| 9. `independent_review` 非 enterprise 不再 `ok:True` | 同 8 | （前置） | 已通过 |
| 10. 末尾整体复核抓跨票集成错误 | 新架构未实施 | — | **主动放弃** |
| 11. 开工前必查"功能是否已存在" | 新架构未实施 | — | **主动放弃** |
| 12. 末尾复核失败仅回滚涉事 lane | 新架构未实施 | — | **主动放弃** |
| 13. Out of scope 不重长 | 新架构未实施 | — | **主动放弃** |
| 14. 决策 16：lane pattern 推导确定可复算 | 已在 `test_lanes_write_gate.py` 间接覆盖；新架构补强后单测 | （待 §4 实施） | 主动放弃 |
| 15. 决策 17：lane 级回滚三件套 | 已在 commit `402ed8b` 的写账本 + `lanes.write_gate` 落地 | （前置）+ D | 已通过（前置部分） |
| 16. 决策 18：「补不出」税码 + 重切 | 新架构未实施 | — | **主动放弃** |

**判据覆盖结论**：本计划只触及**已落地**的判据（2/7/8/9/15）；§3/4/5/6/10/11/12/13/14/16 全部依赖新架构 §4 实施，**与本计划删除阶段正交**——「主动放弃」≠「已通过」，是**当前范围不涉及**，避免被读者误读为"已通过"。删除阶段不阻塞新架构的推进，也不被新架构阻塞（只要按 α 选项推迟 D 阶段）。

---

## 5. 阶段依赖与时间预算（粗估）

```
[A]  acceptance_contract (1 文件, 0 内部 ref)
  ↓
[B]  plan_state (1 文件, 0 内部 ref, 0 内部 test self-ref)
  ↓
[C]  workflow_eval/profiles/reflection (3 文件, 0 内部 ref)
  ↓
[D]  game_workflow (5,508 行 + 28 处外引用) ← 选项 α 推迟
                                                │
                                            [E] 前端 2 文件 + App.vue 8 处
```

**实施时间（粗估，仅作计划参考）**：
- A: 0.5h（含 1 测试回合）
- B: 0.5h（含 1 测试回合 + 全量 `pytest`）
- C: 0.5h（含测试回合）
- E: 2-3h（前端改动多 + 8 处 App.vue 收口 + build 验证 + playwright E2E）
- D: 4-8h（取决于 28 处外引用的拆耦合难度 + 新架构替代实现就绪度）

**总预算**：如 α 选项生效，**3.5–4.5h 后端 + 2–3h 前端 = 5.5–7.5h**。D 阶段作为独立子计划待新架构就绪后单独立项。

---

## 6. 风险登记表

| 风险 | 等级 | 缓解 |
|---|---|---|
| 阶段 D.1.1 拆 `tools.py` 12 处 game_workflow 引用时发现 game_workflow 暴露的能力**新设计无对应实现** | **P0** | 选项 α：推迟 D 阶段 |
| 阶段 D 前 game_workflow 删后 `agent_runtime/acceptance.py:21` 的 `from .game_workflow import GameWorkflowManager, WorkflowPolicy` 会 ImportError——§1.3 把 acceptance.py 归为「保留」是误判（其模块顶部 import 真依赖 game_workflow） | **P0** | D 阶段前置子步 D.0：**把 acceptance.py 与 game_workflow 解耦**——选 Ⅹ 内联 / ⅩⅩ 重写 `run_real_project_acceptance` 不依赖 `GameWorkflowManager` / ⅩⅩⅩ 整体迁移到 `tools/dev/` CLI 剧本。本计划推荐 ⅩⅩ（最小侵入） |
| 阶段 E 改 `App.vue:702-704` 时 `SceneRuntimePanel` 内部对 `host === '#wb-cockpit-runtime-slot'` 的硬编码（`:91`/`:124`/`:853`）**仍引用该 id** | P1 | 该 id 在 commit `402ed8b` 已迁到主区（`App.vue:650`）并 `v-show` 常驻；E 阶段删 :650 后 `SceneRuntimePanel` 这 3 处硬编码字符串变为死代码——`git grep` 应在 E.5 验收中确认无业务引用，**否则先清理这 3 处**再删 :650 |
| 阶段 E 改 `App.vue:701` 把 `ChatDock scope="cockpit"` 合并到 `scope="default"` 时，cockpit 专用的 ChatDock 配置丢失 | P1 | `ChatDock.vue` 需 `git grep "scope.*cockpit\|'cockpit'"` 实证无 cockpit 专用配置；若有则保留两个 Teleport |
| 阶段 A/B/C 期间并行 agent 引入新代码依赖被删模块 | P1 | 每步开始前 `git status -sb`；发现即停步 + 协调 |
| 阶段 E 改 9 行号 App.vue 时对方正在改 App.vue（双写者交叠） | P0 | 提交前 `git status` + `git diff --name-only HEAD` 双重确认 |
| 阶段 D 删除 game_workflow 后 `langsmith.py` 4 处引用未全部清干净导致启动 ImportError | P1 | 阶段 D 收尾跑 `python -c "import api"` + `uvicorn` 启动冒烟 |
| 阶段 A/B/C 后 pytest 全量跑出现未在侦察列表里的失败 | P2 | 失败即回退该步（`git reset --hard <prev-commit>`）并分析；不堆叠改动 |
| **本计划文档本身未入库** | **P0** | 按 `docs/next-steps.md` §0 规则，doc-only 必须随下一笔真实改动入库；本计划 v1.1 必须随 A 阶段首笔真实改动一起 commit（**否则拖到最后会过期、与实施事实脱节**） |

---

## 7. 收尾与推送

**收尾 checklist**（每阶段共用）：
1. `git status -s` 确认工作树状态
2. `git diff --name-only HEAD` 确认仅动该阶段声明的文件
3. `pytest tests/ -q` 全量（除非冻结期）
4. 前端阶段加 `cd frontend && npm run build`（先备份 `web/`）
5. 用 commit 消息文件 `D:/Temp/commit-<阶段名>.txt`（避免 shim 吞 `-m`）
6. `git commit -F <msg> -- <明确路径>` 不用 `git add -A`
7. **A 阶段首笔 commit 必须把 `docs/cockpit-v2-deletion-plan.md`（v1.1）一起入库**（按 §0 规则 doc-only 必随真实改动入库）——`git add docs/cockpit-v2-deletion-plan.md` + 业务文件一起
8. 推：`github-api-push` skill（已配） → `D:/Temp/push_via_api.py`
9. Python 写 loose ref `.git/refs/remotes/origin/main`（避 `git update-ref` 静默失效 + `printf` 写 CRLF）
10. `git rev-list --left-right --count origin/main...HEAD = 0 0` 验 diverge

**回滚预案**：
- **A/B/C/E 阶段**：每阶段独立 commit，失败 `git reset --hard HEAD~1` 单步回退（仅回退该阶段业务文件；A 阶段还会回退 plan doc——故 A 阶段回退后 plan doc 也得 revert，单独 `git checkout HEAD~1 -- docs/cockpit-v2-deletion-plan.md`）
- **D 阶段**：含多个拆耦合子步（D.0 acceptance.py 解耦 + D.1.1–D.1.5 + D.2 主删 + D.3 测试清理）。如 D 阶段中途失败需回滚，**必须整段回退**——计算自上次非 D 阶段 commit 以来的 commit 数 N：
  ```bash
  # 找出上次非 D 阶段 commit
  git log --oneline -20 | grep -v '^[a-f0-9]\{7\} D 阶段' | head -1
  # 计算 N = 该 commit 之后的所有 D 阶段 commit 数
  N=$(git log --oneline -50 | grep -E '^[a-f0-9]{7} D 阶段' | wc -l)
  git reset --hard HEAD~$N
  ```
  **禁止**单步 `HEAD~1`——会留下 D.0 解耦的 acceptance.py 改动而 game_workflow 仍在，导致 acceptance.py 找不到 game_workflow 而 ImportError

---

## 8. 计划元信息

- **作者**：WorkBuddy（自主舱 v2 重构，2026-10-12 决策 → 2026-10-13 §7 三处清场落地 commit `402ed8b`）
- **拟稿时间**：2026-10-13；v1.1 修订时间：2026-10-13
- **依赖文档**：`docs/cockpit-v2-redesign.md`（v1.3）、`docs/next-steps.md`（§0）、`HANDOFF.md`
- **依赖 commit**：`402ed8b` v2 清场（§7 三处）
- **协调对象**：并行 agent（正改 `config.py`/`conftest.py`/`test_session_owner_project.py`）
- **决策变更说明**：本计划基于文档「决策已定，未实施」状态。若用户决定**不实施新架构 §4**，则 D 阶段**永久推迟**，A/B/C/E 仍可执行（孤立模块删除安全）。
- **v1.1 修订纪要**（自审发现 → 修订）：
  1. **A 阶段**：v1.0 误估为"删 80 行 + stub 2 行"→ 实测 22 处用法（字段操作 + 4 个函数调用），且 `agent_runtime/game_workflow` 是单文件结构 + 55 处外部 import 站点，拆包代价大；改为"**仅补全 import 行的 6 个公开符号**，不动文件结构与 22 处用法"。A 阶段行数 0 减。
  2. **E.1 表**：v1.0 把 `:650/:651` 标"保留"是误读 §1「保留 ①实时对话视频流 ②窗口嵌入」——保留的是机制不是 id；改为"**删 2 + 改 7 = 9 行号**"；补漏 :688（commit 402ed8b 后行号偏移 +5）。
  3. **§4 状态列**：v1.0 的 "N/A" 易被误读为"已通过"，改为显式 **"主动放弃（依赖 §4 实施）"**。
  4. **§6 风险登记表**：v1.0 漏 2 项 P0 — ①D 阶段前置 acceptance.py 解耦（§1.3 把 acceptance.py 归"保留"是误判：`acceptance.py:21` 顶部 `from .game_workflow import` 真依赖 game_workflow）②本计划 doc 自身未入库（按 §0 规则必须随 A 阶段首笔 commit 一起入库）。
  5. **§7 收尾 checklist + 回滚预案**：v1.0 漏 ①A 阶段首笔 commit 必带 plan doc 入库 ②D 阶段回滚 N 计算式（`git reset --hard HEAD~$N`，N = 自上次非 D 阶段 commit 以来的 D 子步数；禁止单步 `HEAD~1`，否则 acceptance.py 与 game_workflow 不同步 ImportError）。

---

# v1.2 修订（**撤销 v1.1 计划 + §1.3 全量复测**）

> **v1.2 生效时间**：2026-10-14
> **v1.1 撤销原因**：A/B/C 三个"安全"阶段全部证伪（实测数据），v1.1 计划可启动集合 = 0
> **v1.2 路径**：**先做 §1.3 删除清单全量复测**（按 §1.3 自身纪律「删除前必须逐个读 docstring + 查反向引用，两者缺一不可」），用真实数据重写 §1.3，再回到阶段拆解

## v1.1 撤销实证（实测，非推算）

| 阶段 | v1.1 计划假设 | 实测（v1.1 计划外的两轮反查） | v1.1 错误根源 |
|---|---|---|---|
| **A** `acceptance_contract` | "补 6 个公开符号到 import 行" | acceptance_contract 仅 5 个公开符号（4 def + 1 class），5 个**已全部 import**——A 无内容可做 | v1.1 计划 A.1 凭"22 处用法"反推"6 个公开符号"——误算第 6 个（`unmet_acceptance_items` 实为 game_workflow **内部**函数） |
| **B** `plan_state` | "0 内部引用，可直接删 203 行" | `plan_state` 被 `agent.py:3925`（核心"压缩无损"机制）+ `tools.py:5747`（dev_plan 工具）+ 2 测试引用 | v1.1 计划侦察只 grep 词 `plan_state`，**未 grep 模块路径** `from agent_runtime.plan_state` |
| **C** `workflow_eval/profiles/reflection` | "仅 game_workflow 一族引用" | workflow_eval 被 **`api_routes/agent.py:19`** + 5+ 测试 + `langsmith.py`(2 懒加载) 引用；workflow_profiles 被 **`tools.py:7808`** + `api_routes/agent.py:16` + 5 测试引用（v1.1 完全未发现 tools.py:7808） | 同上——**只 grep 词 + 未 grep 模块路径** |

**共性根因**：v1.0/v1.1 计划所有反向引用侦察都**只 grep 词**（如 `plan_state`），**未 grep 模块路径**（如 `from agent_runtime.plan_state` / `from .plan_state`）。这导致：
- `from .workflow_profiles import` 在 `tools.py:7808` 这种**懒加载 + 字符串相对路径**的场景完全漏判
- `acceptance_contract` 也可能存在同样问题（v1.1 计划说"0 内部引用"——本次实测 0 业务引用属实，但**仍要核测试 import**）
- **侦察方法论有系统性问题**——单看 `git grep "from agent_runtime.X"` 才能完整

**v1.1 计划可启动集合 = 0**。在错前提上盖楼毫无意义，按"诚实面对发现"原则撤销。

## §1.3 全量复测（5 个"删"候选 + 4 个"保留"候选）

### 复测方法（统一纪律）

对每个候选执行 4 步：
1. **读 docstring 全文**（能力定义）
2. **业务 import 站点**（`from .xxx` / `from agent_runtime.xxx` 双向都查）
3. **测试 import 站点**（`tests/**/*.py` 单独查）
4. **字符串反射 / `__import__`**（防止有动态加载漏判）
5. **静态判断**「§1 列的"新设计取代"是否真能成立」

### 复测结果：5 个"删"候选

#### 候选 1：`acceptance_contract.py`（80 行）

- **行数 / hash**：80 行 / `d99ae062fb2f6c1fc67b3da74964d2289dc852f77c893db114700ea8de55bed6`（13,624 字节误，应为 80 行实际字节 ~3,400——下方重核）
- **公开符号**：5 个（4 def + 1 class：`AcceptanceError` / `from_tasks` / `revise` / `approve` / `on_plan_changed`）
- **业务 import**：`agent_runtime/game_workflow.py:29-30`（2 行 import，22 处用法）
- **测试 import**：0
- **字符串反射**：0
- **静态判断**：能力 = "工作流验收契约生命周期"（从 `from_tasks` 提案 → `revise` 修订 → `approve` 批准 → `on_plan_changed` 随计划变化）。**新设计 L1 决策索引** 是 append-only 文本索引，无结构化字段（`revision` / `approved_revision` / `items[].user_approved` / `final_decision`），**不能取代这个状态机**——但 §1.3 没说"被 L1 取代"，说"被新设计取代"。**能力是 game_workflow 主循环的**（验收是工作流的一环，不是 v2 设计要全盘重做的内容），**新设计如果要做"工作流"，仍需这个能力**。
- **决定**：**v2.0 改"迁移"**——把 acceptance_contract 内联到 game_workflow 子模块（避免 D 阶段删 game_workflow 时破坏 22 处用法）；不再单列"删"

> **勘误**：上文字节数 `13,624` 是 `workflow_eval.py` 的，不是 acceptance_contract 的（脚本里变量未传对）。**acceptance_contract.py 实际字节数待重核**——但对决策无影响（孤立的 80 行）。

#### 候选 2：`plan_state.py`（203 行）

- **行数 / hash**：203 行 / `71d3ac0452f069a7e6d767c2cc0051c264849aeada84be0a7917163223a2bb1d`
- **公开符号**：5 个对外 def（`render` / `summary_for_prompt` / `open_plan` / `append_items` / `set_done` / `clear` / `read`——实际 7 个公开 def）+ 6 个内部 helper（`_key_of` / `_plan_path` / `_read` / `_write` / `_clean` / `_LOCK`）
- **业务 import**：**4 处**——`agent.py:3925`（核心"压缩无损"机制，懒加载）/ `tools.py:5747`（`dev_plan` 工具入口，懒加载）/ `tests/test_context_externalization.py:24` / `tests/test_plan_state.py:39`（self-test）
- **测试 import**：2
- **字符串反射**：0
- **静态判断**：能力 = "对话压缩无损（任务清单外置 + 每轮注入 + 勾选状态机）"。`agent.py:3925` 注释自承「**没有这一步，清单只是个模型可能忘记去读的旁路文件**」——这是**核心机制**，不是边缘能力。
  - **新设计 L1 决策索引**（§4.1）：append-only 文本索引 + 链接，**无勾选状态字段**（`done: bool`），**无 plan 标题 / 条目去重 / 自动合并语义**——**根本不能取代 plan_state**
  - **新设计 L0 项目契约**：几乎不变的结构性事实，**无状态机**
  - **新设计 L2 本次按需读取**：每次不同，**无持久化**
  - **结论**：plan_state 的"勾选状态机"是 §1 「新设计 §4 三层记忆」**三层都不能覆盖的能力**——必须保留
- **决定**：**v2.0 改"保留 + 能力确认"**——plan_state 不是舱的附属品，是通用基础设施（任何 LLM 对话都能受益于"任务清单外置 + 压缩无损"）。**§1.3 列"删"是误判**

#### 候选 3：`workflow_eval.py`（291 行）

- **行数 / hash**：291 行 / `d7204f043caa082df74e8aa8b346a00c985a093f0ca97784ec3b1cf3a7b4dc62`（13,624 字节）
- **公开符号**：12 def（`evaluate_workflow` / `dataset_cases` / `evaluate_case` / `aggregate_evaluations` / `compare_evaluation` / `regression_gate` / `load_baseline` / `save_baseline` / `_check` / `_is_completed` / `_self_check_report` / `main`）+ 0 class
- **业务 import**：**6+ 处**——`agent_runtime/game_workflow.py:4180`（懒加载）/ `:4545`（懒加载）/ `langsmith.py:405`（懒加载）/ `:421`（懒加载）/ **`api_routes/agent.py:19` 直接 import** / 5+ 测试
- **测试 import**：5+（test_workflow_eval_dataset 等）
- **字符串反射**：0
- **静态判断**：能力 = "工作流评估 / 回归门 / 基线管理"（`evaluate_workflow` 评估一次工作流结果质量；`regression_gate` 对比基线决定是否通过；`compare_evaluation` 工具用于回归测试；`load_baseline` / `save_baseline` 持久化基线）。`api_routes/agent.py:19` **直接 import** `DEFAULT_DATASET_NAME` 与 `dataset_cases`——**说明这套评估机制对外有 API 路由**，不是 game_workflow 内部细节。
  - **新设计 §4 是否需要"工作流评估"**？**未知**——v2 决策 1 说"只保留①实时对话视频流 ②窗口嵌入"；**没说新舱是否需要工作流评估**。**`api_routes/agent.py:19` 直接 import `DEFAULT_DATASET_NAME` 表明这是 API 路由层硬依赖**——删它会破坏 API 路由。
- **决定**：**v2.0 改"保留 + 重新评估"**——workflow_eval 不是舱专属，是工作流质量保障的通用能力；§1.3 列"删"是误判。**但 v2 决策 1 是否要保留"工作流质量保障"是新设计问题**，与本删除计划正交——`api_routes/agent.py:19` 引用表明**当前**仍需保留

#### 候选 4：`workflow_profiles.py`（261 行）

- **行数 / hash**：261 行 / `3c52f7b364be4e98306baa3dc6265662849f34755e1542584ef66e74ecf73c8a`（13,324 字节）
- **公开符号**：10 def + 1 class（`Profile` / `normalize_kind` / `get_profile` / `_option` / `_append_dynamic` / `_generic_options` / `_generic_fallback_tasks` / `_eda_options` / `_eda_fallback_tasks` / `_game_options` / `_game_fallback_tasks`）+ 顶层常量 `DEFAULT_KIND` / `VALID_KINDS`
- **业务 import**：**4 处**——`agent_runtime/game_workflow.py:47`（直接 import 4 个符号）/ **`api_routes/agent.py:16` 直接 import** `get_profile` / `normalize_kind` / **`tools.py:7808` 懒加载** `get_profile` / `normalize_kind` / 5 测试
- **测试 import**：5（test_workflow_profiles + test_task_role_capability 等）
- **字符串反射**：0
- **静态判断**：能力 = "工作流领域画像注册表（generic / game / eda 三种领域画像，决定选项模板 + 兜底任务 + 提示词口吻）"。**`api_routes/agent.py:16` 与 `tools.py:7808` 都 import**——`api_routes/agent.py` 是 FastAPI 路由层，**`tools.py:7808` 是 dev_* 工具**（给 agent 调用）——**两个入口都消费这个能力**，是基础设施
  - docstring 自己写明：*"画像只决定**确定性兜底内容与提示词口吻**——方案选项、兜底任务 DAG、LLM 生成器提示；状态机、审批门、步数预算等机制完全复用，不为每个领域新增图结构"*——**显式声明通用能力**
- **决定**：**v2.0 改"保留"**——workflow_profiles 是工作流机制的基础设施，§1.3 列"删"是误判

#### 候选 5：`workflow_reflection.py`（183 行）

- **行数 / hash**：183 行 / `f065abe515e7643a279d2d7fe9531e06f00b8022295763c69123612282aa345a`（9,168 字节）
- **公开符号**：6 def（`build_self_review` / `_now` / `_text` / `_append` / `_review_wording` / `_result_map`）
- **业务 import**：**1 处业务**（`agent_runtime/game_workflow.py:35` 直接 import `build_self_review`）+ 2 测试（`test_self_review_wording.py:43` + `test_workflow_self_review.py:5`）
- **测试 import**：2
- **字符串反射**：0
- **静态判断**：能力 = "从工作流证据生成脱敏的自我复盘"——`build_self_review` 是**工作流结束的复盘产出**（与新设计 §4.5 末尾整体复核的"self_review"概念高度对齐）
  - **新设计是否需要"self_review"**？§4.5 明确写「模型复盘」是验收的一部分——`acceptance_report.self_review.summary` 字段直接消费 `build_self_review` 的输出（`AutonomousCockpit.vue:2965` 渲染路径有 `acceptanceReport.self_review?.summary`）——**新设计需要它**
  - **但** `AutonomousCockpit.vue:2965` 整舱要删——`build_self_review` 的消费者在 D 阶段随舱一起删
- **决定**：**v2.0 改"迁移"**——把 `workflow_reflection` 内联到 `agent.py`（`build_self_review` 是 game_workflow 的"结束复盘"产出，本就是 game_workflow 内部调用）；D 阶段删 game_workflow 时一起删

### 复测结果：4 个"保留"候选（确认性复测，不删）

| 候选 | 公开符号 | 业务 import | 测试 | 复测结论 |
|---|---|---|---|---|
| **`lanes.py`**（609 行，§1.3 标"迁移：能力保留"） | 文档已读 | 多处 | 多处 | ✓ 保留——决策 5/7/8/9 的实现基础 |
| **`independent_review.py`**（106 行） | 改 fail-closed 后 | 多处（含 game_workflow） | 2（fail_closed 测试 + enterprise_sandbox_review） | ✓ 保留——决策 8/9 末段复核 |
| **`verification.py`**（40 行） | 多个 helper | 多处（含 agent.py 内部） | 多 | ✓ 保留——决策 8 fail-closed 口径 |
| **`visual_acceptance.py`**（1009 行） | 浏览器后端 | 多处（media_fixture / page_action / voice_loop / vue_component） | 多 | ✓ 保留——决策 8 UI 验收 |

注：`cockpit_policy` / `acceptance` / `WorkflowCard` / `FlowCanvas` 已在 v1.1 计划 §1.3 范围内确认保留，本次不复测。

## §1.3 v2.0 重写表（基于 §1.3 全量复测实测数据）

> **重要前提**：以下"删/迁移/保留"判断**仅基于当前代码现状与新设计已知决策**——**§1.3 表里"删"的决定性依据是 §1「推翻重做，只保留①实时对话视频流 ②窗口嵌入」**。如果新设计 §4 实施后需要任何被标"删"的能力，**该能力必须改回保留或迁移**。本表不替代新设计决策，仅整理"当前可删 / 需拆耦合 / 必须保留"的事实清单。

| 原 §1.3 分类 | 模块 | v2.0 复测分类 | 依据（实测） | 实施阶段（v2.0 重排） |
|---|---|---|---|---|
| 删：能力被新设计取代 | `acceptance_contract.py` (80) | **迁移** | 仅 game_workflow 一处用；内联到 game_workflow 子模块即可 | 随 D 阶段（与 game_workflow 一起删） |
| 删：能力被新设计取代 | `plan_state.py` (203) | **保留** | 4 处业务 import；能力 = 压缩无损 + 勾选状态机，L1/L0/L2 三层都不能取代 | 不删（不动） |
| 删：能力被新设计取代 | `workflow_eval.py` (291) | **保留** | `api_routes/agent.py:19` 直接 import；`langsmith.py`(2 懒)；5+ 测试 | 不删（不动） |
| 删：能力被新设计取代 | `workflow_profiles.py` (261) | **保留** | `api_routes/agent.py:16` + `tools.py:7808` 引用；是工作流基础设施 | 不删（不动） |
| 删：能力被新设计取代 | `workflow_reflection.py` (183) | **迁移** | 1 业务 import（game_workflow:35）+ 2 测试；可内联到 game_workflow | 随 D 阶段 |
| 删：能力被新设计取代 | `CockpitApprovalQueue.vue` (148) | **删** | 仅 `AutonomousCockpit.vue:36, 2960` 引用 | E 阶段 |
| 删：编排本体 | `game_workflow.py` (5,508) | **删（待 D 阶段前置解耦）** | 仍被 tools/agent/api_routes/langsmith 28 处硬引用——**D 阶段选项 α 仍推荐（推迟至新架构 §4 实施）** | D 阶段 |
| 删：整舱 | `AutonomousCockpit.vue` (3,454) | **删（commit 402ed8b 已迁出槽位）** | 实时流/窗口嵌入已迁到主区 | E 阶段 |
| 迁移：能力保留 | `lanes.py` (609) | ✓ 保留 | 已迁移（决策 5/7/8/9） | 不动 |
| 迁移：能力保留 | `independent_review.py` (106) | ✓ 保留 | commit 402ed8b 改 fail-closed | 不动 |
| 迁移：能力保留 | `verification.py` (40) | ✓ 保留 | 决策 8 口径 | 不动 |
| 迁移：能力保留 | `visual_acceptance.py` (1,009) | ✓ 保留 | 决策 8 UI 验收 | 不动 |
| 保留 | `cockpit_policy.py` (94) | ✓ 保留 | `api_routes/agent.py:30` + `game_workbench.py:2658` | 不动 |
| 保留 | `acceptance.py` (185) | ✓ 保留（**有条件**：D.0 阶段前置解耦） | `:21` 顶部 `from .game_workflow import` 真依赖 game_workflow | D.0 子步 |
| 不能删 | `WorkflowCard.vue` (700) | ✓ 保留 | AskMessage/ChatDock/AutonomousCockpit 三处引用（删舱自动去掉最后 2 处，但 WorkflowCard 仍被 AskMessage/ChatDock 异步 import） | 不动 |
| 不能删 | `FlowCanvas.vue` (640) | ✓ 保留 | `App.vue:33, 723` 引用 | 不动 |

**v2.0 复测后真实可删体量**：
- **后端**：0 行（5 个 §1.3 列删的后端模块中 4 个改保留，1 个改迁移——`acceptance_contract` 随 D 阶段）
- **前端**：`CockpitApprovalQueue.vue` (148) + `AutonomousCockpit.vue` (3,454) = **3,602 行**

**v2.0 复测后真正需要做的工作**：
1. **A 阶段**（取消）——v1.1 已证伪
2. **B 阶段**（取消）——plan_state 保留
3. **C 阶段**（取消）——3 个保留
4. **D 阶段**（保留，选项 α 推迟）——game_workflow 仍需先建新架构
5. **E 阶段**（保留，前端 2 文件 + App.vue 9 行号）
6. **D.0 阶段**（新增）——acceptance.py 顶部 import 解耦
7. **`acceptance_contract` 与 `workflow_reflection` 迁移**（v2.0 新增）——可作为 D 阶段前的小步快走，但**依赖 game_workflow 子模块化**（改动面大）

**v2.0 真正可立即启动的阶段只有 E**（前端 2 文件 + App.vue 9 行号收口）。后端 5/6 个候选全部需要先做更深的工程改造——**与 v1.0/v1.1 计划"安全小步"的承诺相反**。

## v1.2 决策项（请拍板）

| # | 决策项 | 选项 |
|---|---|---|
| 1 | v1.1 计划处置 | **保留为反思记录 + 加 v1.2 章**（推荐：v1.1 的自审过程有跨会话价值，**v2.0 表的"取消 A/B/C"** 写在本章）  /  删 v1.1 整份（按 §1.3 「已确认可删」原则自我执行——但违反 §0 规则：doc-only 必随真实改动入库）  /  v1.1 改名 `cockpit-v2-deletion-plan-archived.md` 后作为反思记录归档 |
| 2 | v2.0 路径 | **先做 E 阶段**（前端 2 文件 + App.vue 9 行号）——唯一可立即启动的阶段  /  全部搁置，等新设计 §4 实施后再统一动  /  先做 D.0 子步（acceptance.py 解耦）——为未来 D 阶段铺路 |
| 3 | 文档入库 | **随 E 阶段首笔真实改动入库**（推荐）  /  E 阶段涉及前端 + plan doc 改动 → 一次 commit 包含全部  /  分批 commit（前端先、plan doc 后）——违反 §0 |

## v1.2 修订纪要（v1.1 → v1.2）

1. **A 阶段撤销**：实测 acceptance_contract 5 个公开符号已全部 import，A 无内容可做；A 阶段从 v1.1 计划中删除
2. **B 阶段撤销**：plan_state 被 4 处业务引用（agent.py 核心机制 + tools.py + 2 测试），核心能力（压缩无损 + 勾选状态机）L1/L0/L2 三层都不能取代；B 阶段从 v1.1 计划中删除；plan_state 改保留
3. **C 阶段撤销**：3 个候选全部被业务主线引用（api_routes/agent.py + langsmith + tools.py + 大量测试），不是孤立辅助；C 阶段从 v1.1 计划中删除
4. **§1.3 v2.0 重写表新增**：基于 4 步复测法（读 docstring + 业务 import + 测试 import + 字符串反射 + 静态判断），把 5 个"删"候选的真实分类重写
5. **可启动集合重排**：v1.0 承诺 "5 个阶段" → v1.1 缩到 "4 个阶段（A 缩 0 减）" → v2.0 **仅 E 阶段可立即启动**，后端工程改造需 D.0 拆耦合 + 子模块化等更大改动
6. **方法论沉淀**：侦察纪律升级——所有"反向引用"必须**双源**（grep 词 + grep 模块路径 + 测试单独查 + 字符串反射），单源 = 盲判
