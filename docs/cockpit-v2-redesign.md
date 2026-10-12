# 自主开发舱 v2 重构设计（推翻重做）

> 状态：**决策已定，未实施**。
> 上游：`docs/autonomous-development-cockpit-design.md`（v1，本次推翻）。
> 来源：2026-10-12 关于 `mattpocock/skills` 中 `/wayfinder` 与 `/implement-spec` 的调研 + 六轮 grilling。
> 本文只记录**已拍板的决策**与**证据**，不记录实现步骤（实施计划另立）。

---

## 1. 范围：保留 / 删除（修正版）

| 侧 | 内容 | 行数 |
| --- | --- | --- |
| **保留** | `agent_runtime/realtime_*.py`(8) + `vision.py` | 2,488 |
| **保留（修正）** | `agent_runtime/lanes.py` | 609 |
| **删除** | `game_workflow` 5508 / `lanes`→移出 / `workflow_eval` 291 / `workflow_profiles` 261 / `workflow_reflection` 183 / `acceptance` 185 / `plan_state` 203 / `cockpit_policy` 94 / `acceptance_contract` 80 / `independent_review` 106 / `verification` 40 | ~7,560（移出 lanes 后为 ~6,951） |
| **删除（前端）** | `AutonomousCockpit` 3454 / `WorkflowCard` 700 / `FlowCanvas` 640 / `CockpitApprovalQueue` 148 | 4,942 |

产品范围：**只保留①实时对话视频流 ②窗口嵌入，其余全部推翻重做。**

### 1.1 修正：`lanes.py` 不属于废墟

初版分桶把它列进删除侧，是**错的**。其 docstring 首句即为本次重构所需：

> "Lane registry: **exclusive file ownership for parallel agents on one repo** … 把『谁在动哪些文件』变成机器可查的登记表（带 TTL 的租约）：认领冲突当场拒绝、过期自动可被接手 … **worktree 是可选的开通动作而不是前提**。"

要点三条：冲突域是**文件 pattern 级**（fnmatch），状态存**项目状态目录**（不污染 git 工作树），worktree 仅可选。因此 `implement-spec` 那套"每个 implementer 一个 worktree + merger subagent"在本项目**不必照搬**。

### 1.2 删除前必须先拆的耦合（已精确复核，初版描述偏轻）

初版称"`liveStreamControl.ts` / `liveAudioControl.ts` 等**均引用** `AutonomousCockpit`"——**不准确**，
复核后三处只是**注释**，真正的代码依赖只有 `App.vue`，而它的深度远超预期：

| 位置 | 性质 | 处理 |
| --- | --- | --- |
| `liveStreamControl.ts:5` | 注释（"由 AutonomousCockpit.vue 驱动"） | 改注释即可 |
| `liveAudioControl.ts:4` | 注释（"接口留在 AutonomousCockpit 接线层"） | 改注释即可 |
| `workflow-format.ts:146` | 注释（"与 AutonomousCockpit.timelineLabel 同步"）——**暴露语义耦合**：`timelineLabel` 表只在 `AutonomousCockpit.vue:878/954/2782`，删舱后这份同步失去参照 | 需确认 `workflow-format` 的对应实现是否独立可用 |
| **`App.vue:38`** | **真依赖**：`defineAsyncComponent(() => import(...))` | 删时移除 |
| **`App.vue:71`** | `cockpitRef = ref<{ showOutputs: () => void }>` | 删时需改类型来源 |
| **`App.vue:650`** | 组件挂载 `v-show="workspace === 'cockpit' && !runtimeResident"` | 删时需改 |
| **`App.vue:699`** | **回调依赖**：`@close="cockpitRuntime = false; cockpitRef?.showOutputs()"` | 删时需改 |
| **`App.vue:697-698`** | ⚠ **这就是"窗口嵌入"**：`SceneRuntimePanel` 的 `mode` / `host` 依赖 `cockpitReady && cockpitRuntime`，宿主是 **`#wb-cockpit-runtime-slot`（舱内的 slot）** | **删舱会直接打碎窗口嵌入** —— 正是 §1 警告的"保留项跟着一起碎"的具体行号 |
| `App.vue:92/344/603/652/683` | `workspace === 'cockpit'` 作为 workspace tab 的分支（含快捷键 `4`） | 需重新定义该 tab 语义 |

> ⚠ **`App.vue` 属于双写者交叠文件**（对方高频提交）。改动前须 `git status`，只加明确路径，禁 `git add -A`。

### 1.3 删除清单 2.0（逐模块读 docstring 复核后）

**判据纠错**：不能用"名字里带 cockpit / workflow / 属于旧舱"来判定删除，正确判据是
**"新舱还需不需要这个能力"**。反例：`realtime_protocol.py` docstring 写着
*"for the autonomous cockpit realtime session"*，而实时流恰恰是**保留项**；
`visual_acceptance.py` 写着 *"for the autonomous development cockpit"*，但决策 8/9 要用它。
**名字带 cockpit 完全不能作为删除依据。**

| 分类 | 模块 | 行数 | 依据 |
| --- | --- | --- | --- |
| **删：能力被新设计取代** | `game_workflow.py` | 5,508 | 编排本体，由新设计取代 |
| | `workflow_eval.py` / `workflow_profiles.py` / `workflow_reflection.py` | 735 | 仅 `game_workflow` 一族引用 |
| | `plan_state.py` | 203 | 被 L1 决策索引取代（决策 3/10） |
| | `acceptance_contract.py` | 80 | 仅被 `game_workflow.py:29-30` import（`acceptance_from_tasks` / `approve_acceptance`），属舱的工作流验收契约 |
| | `CockpitApprovalQueue.vue` | 148 | 仅被 `AutonomousCockpit.vue:36` 引用 |
| **迁移：能力保留，改归属** | `lanes.py` / `independent_review.py` / `verification.py` / `visual_acceptance.py` | 1,764 | 决策 5/7/8/9 的实现基础 |
| **不能删：被别处依赖** | `WorkflowCard.vue` | 700 | 问答页 `AskMessage.vue:9` 与 `ChatDock.vue:37` **异步引入并渲染**（`:215` / `:1353`） |
| | `FlowCanvas.vue` | 640 | 挂在**开发台** `App.vue:33`（`everMounted.flow`，`:718`），不是舱专属 |
| **保留：本就不属于舱** | `acceptance.py` | 185 | `run_real_project_acceptance` 全仓只被自身 `main()`（`:177`）调用，是带 argparse 的**独立 CLI 剧本**；无任何模块 import，不在 `game_workflow` 链路上。agent 侧引擎自检走的是 `self_verify(scope:"engine")`（`agent.py:582`）。属"游戏工程验收"线，与保留项 Godot / 窗口嵌入同线 |
| **迁移：通用能力，非舱专属** | `cockpit_policy.py` | 94 | 名字带 cockpit，但消费者是 `api_routes/agent.py:30`（`pending_gate_requests`，`:1173/:1181/:1185`），前端消费方是 `AgentPolicyPanel.vue` / `HarnessPanel.vue` / `RegionMapDialog.vue` / `FlowCanvas.vue` —— **全在开发台侧** |

**修正后的真实可删体量：后端 6,526 行**（`game_workflow` 5508 + `workflow_*` 735 + `plan_state` 203 + `acceptance_contract` 80）+ **前端 3,602 行**（`AutonomousCockpit` 3454 + `CockpitApprovalQueue` 148）= **10,128 行**。
（初版估的 7,560 / 4,942 双向偏高：后端把不该删的算进去了，前端把不能删的算进去了。）

> **两次分桶错误的共同教训**：`lanes`（按"属于旧舱"误删）与 `WorkflowCard` / `FlowCanvas`
> （按"名字像舱"误删）。**删除前必须逐个读 docstring + 查反向引用**，两者缺一不可。

---

| # | 决策 |
| --- | --- |
| 1 | 推翻重做，只保留实时对话视频流 + 窗口嵌入 |
| 2 | 新舱仍需 agent **自主多步**（不是纯指令执行的终端） |
| 3 | 共享记忆写权限 = **A+B**：编排会话写共享层；小对话只写自己票的回执；共享层 **append-only** |
| 4 | wayfinder **搬**：grilling、map（destination / fog / frontier / out-of-scope）、research 票 AFK 并行、一 session 一票。**不搬**：并行调度、共享记忆 |
| 5 | 并行冲突域 = **悲观租约**（lanes 文件 pattern 级，冲突即拒绝）；worktree 仅作可选开通 |
| 6 | 切分失败兜底 = **C（混合）**：先等一个 TTL 窗口 → 超时给**一次**重切机会 → 仍失败则降级串行，并在图谱上标注"此处本可并行但被文件边界挡住" |
| 7 | 悲观租约靠**两层**成立：**开工前声明**（用于调度：提前算并行度、排 frontier）+ **写工具落盘时拦截**（用于安全：不依赖声明完整性）。越界不停死，而是停下 + 上报 + 触发一次重切（接决策 6） |
| 8 | 验收证据纪律 = **fail-closed**：没有真实命令输出（或跑不动）就**不许关票**。等价于**每票关闭前必验** |
| 9 | 在每票即验之上，**末尾再叠一次整体独立复核**（`independent_review`：checkpoint sha256 比对 + 真跑测试），用于抓跨票集成错误 |
| 10 | 冷启动基线 = **混合 C**：L0 只放几乎不变的结构性事实（目录布局、技术栈、禁区）；"功能是否已存在"每次开工前**强制查一次**（`ts_index` / `code_intel`），结果落 **L2（尾部，不进缓存锚点）**，查证结论记进 L1 |
| 11 | 末尾复核失败 → **只回滚涉事的票**（lane 级），其余保留。**不整批回滚** |
| 12 | 补不出可执行判据的改动 → **不进自主舱**，切票阶段即判为"不可自主"，转人工待办，并写进图谱的 **`Out of scope`**（按 wayfinder 语义：永不毕业） |
| 13 | E 类四条失败裁决（末尾复核失败 / 重切二次失败 / 死锁放弃 / L1 提升）= **挂起等人**，不自动走保守分支。产品定位随之明确：**正常路径无人值守，失败路径等人** |
| 14 | 挂起期间的 lane 租约 = **分情况 C**：复核失败类挂起（改动已完成）→ **释放租约**，只保留产物；死锁 / 重切类挂起（改动未完成）→ **续租等待** |
| 15 | 窗口嵌入迁到 workspace 层（`#wb-cockpit-runtime-slot` / `#wb-cockpit-chat-slot` 移出舱），精确改动集见 §7.0 |
| 16 | 默认 lane pattern 推导 = **机械 5 条**（见 §5.2），外加**冲突足迹回显**、**不设硬阈值** |
| 17 | lane 级回滚 = **三件套**：lane 基线 + 写账本 + 子集回滚（见 §5.2），前置必修 `project_checkpoint._iter_files` 的静默全仓陷阱 |
| 18 | 「补不出判据」= **先试再判（税码）+ 造神谕优先 + 证据随行 + 单票重切跳闸**（见 §5.2），不设会话费率阈值 |

选定 6 的理由：无人值守场景下"静默写坏"比"慢一点"贵得多；但纯粹降级串行会让并行度在无告警的情况下塌掉，因此失败必须**留痕**（落进 fog，而非假装没发生）。

---

## 3. 从 wayfinder 搬什么（实测对照，勿凭记忆）

权威源：`mattpocock/skills`（MIT）→ `skills/engineering/wayfinder/SKILL.md`、`docs/engineering/wayfinder.md`、`skills/engineering/implement-spec/SKILL.md`。

| 需要的能力 | wayfinder 有吗 | 落在哪 |
| --- | --- | --- |
| 多轮询问调研（grilling） | ✅ 本体 | `wayfinder` |
| 图谱：destination / fog of war / frontier / out of scope | ✅ | `wayfinder` |
| research 子会话并行调研 | ✅ 唯一允许并行的票类型 | `wayfinder` |
| 调研后产出方案文档 | ⚠️ 半有：交棒 `/to-spec`，自身不写文档 | `to-spec` |
| **串行/并行调度 + 多会话执行 + 末尾统一审查** | ❌ **没有**（明说"并行多票更容易错，默认一次一张"） | **`implement-spec`** |
| **共享记忆层** | ❌ **完全没有** | 自研 |

**证据**：wayfinder 的 SKILL.md 对 `memory` / `worktree` / `shared context` / `cache` **命中 0 次**；命中集中在 `implement-spec`（3 次）。

### 3.1 `implement-spec` 才是并行调度的参照（原文要点）

- tickets 不是步骤清单，是 **task graph + blocking**，永远存在一个 **frontier**
- implementer 各自 **worktree + branch**，开工前确认基于 integration branch，完成后先合回再报完成
- 用专门的 **merger subagent** 合并；全部完成后**只做一次** `code-review`；收尾清理 worktree
- 通信立场：*"Communicate primarily through **context pointers** … Don't duplicate information already available via pointers."*

**该立场与本项目"共享记忆块"诉求直接冲突**：指针路线各会话前缀不同 → prompt cache 前缀 Miss；记忆块路线缓存好但会腐坏。折中见 §4.1。

---

## 4. 新架构

### 4.1 三层记忆前缀（缓存友好）

| 层 | 内容 | 变更规则 |
| --- | --- | --- |
| **L0** | 项目契约、目录约定、禁区 | **永不变**，缓存锚点 |
| **L1** | 决策索引（一行 gist + 链接） | **只追加**：只能在尾部追加，不重排、不改写历史条目 |
| **L2** | 本次按需读取的指针（文件、票） | 每次不同，置于**尾部**，不进锚点 |

缓存命中的是 **L0 + L1**。要害在 L1 的两条纪律：**只能追加、不许重写**——一旦允许重排或改写历史条目，前缀改变，L0+L1 的缓存全废。

结构对应关系：L1 即 wayfinder 的 *"map 是 index 不是 store"*；小对话的回执留在各自票内，由编排会话"提升"（promote）为 L1 的一行。

### 4.2 会话拓扑与隔离

- **大对话独立**：自主舱的一整个大对话与开发台、问答页**不共享**，禁止会话泄漏
- **小对话在大对话内**，各自**独立上下文**
- **共享记忆只在舱内共享**

连带后果（必须处理）：舱内记忆是**冷启动**的，继承不到开发台既有上下文 → 必须显式设计"从仓库/开发台导入基线"这一步，否则"重复造轮子"这个坑不但没堵上，反而更严重。

### 4.3 并行：悲观租约 + 重切上限 1

- 小对话开工前 `claim` 其要动的文件 pattern；claim 失败即排队或改派，**绝不并行写同一批文件**
- worktree 仅在"同文件确需被两个会话同时改"时才开通（lanes 已支持，非前提）
- 重切策略按决策 6（TTL 窗口 → 一次重切 → 降级串行 + 标注）
- **死锁防护（必做）**：lanes 只做"认领冲突当场拒绝"，**不检测环**。悲观租约 + 排队天然可能出现 A 等 B 的文件、B 等 A 的文件。需显式设防（如按 ticket 全局序申请租约，或等待超时即放弃并上报）

> **实测前提：`lanes` 目前是君子协定，不是硬约束**（这正是决策 7 要加第二层的理由）
> - `dev_lanes` 是**注册给 agent 主动调用的工具**（`agent.py:615` 用法说明、`tool_domains.py:107` 注册、`tools.py:146` 白名单）；`claim` / `check` / `release` / `heartbeat` 全靠调用方自觉，**没有任何写路径在落盘前反查 lane 归属**
> - 声明粒度两难，`agent.py:615` 原文：*"排他按目录前缀保守判定：`*.vue` 这类无目录前缀的模式等于占下全仓，请写成 `frontend/src/**/*.vue`"* —— **写宽 = 占全仓（并行度塌）；写窄 = 漏报（安全性塌）**
> - 能力租约是**动作级**而非文件级：`agent.py:3063` `_lease_blocked(action_name)` 依据工具自声明的 `spec.capability`，且 `agent.py:3701-3702` 自述对话路径下 `capability_lease` 恒空 → 永远放行。**不能替代文件级执法**

### 4.4 可直接复用的既有资产

| 资产 | 位置 | 用途 |
| --- | --- | --- |
| 文件级租约 | `agent_runtime/lanes.py` | 并行冲突域（决策 5） |
| 分区机制 | `分区开发设计.md` §4「每分区独立仓库 + 变更集」，§5 dev_* 工具已落地 | 粗粒度并行/隔离的第二级 |
| 能力租约 | `agent.py:3063` `_lease_blocked(action_name)`，`spec.capability` 由工具自声明 | 与"谁声明要动哪些文件"相关，见 §5 |
| 独立复核 | `agent_runtime/independent_review.py`（106 行）：对比 checkpoint 的 **sha256** 与实际文件，并**真跑** `test_commands`；*"Model-generated test claims never satisfy this gate"* | 末尾验收（见 §5 第 3 项） |
| 验证证据纪律 | `agent_runtime/verification.py`（40 行）：*"absence of evidence is not success"*；`skipped` 不计入 `executed`，`partial`/`skipped` **不得声称完成** | 每票验收的判定口径 |

> ⚠ **`independent_review` 的自杀式默认值**：`execution_mode() != "enterprise"` 时直接返回
> `{"ok": True, "status": "local_mode", "independent": False}` —— **静默放行**。在无人值守的自主舱里，
> 这等于"看起来验过了、其实没验"，比不验更危险。**采用前必须把它改成 fail-closed**（见决策 8）。

### 4.5 验收：fail-closed，每票即验 + 末尾一次独立复核

- **证据纪律（决策 8 = fail-closed）**：没有真实命令输出（或跑不动）就**不许关票**，退回执行。口径直接沿用 `verification.py`（`skipped` 不计入 `executed`，`partial`/`skipped` 不得声称完成）
- **每票即验**：由 fail-closed 蕴含——关票逐张发生，"无证据不许关"即等价于每票关闭前必验
- **末尾再叠一次整体复核（决策 9）**：走 `independent_review` 的 checkpoint sha256 全量比对 + 真跑测试命令
- **为什么必须叠**：悲观租约只保证两个小对话不碰同一批文件，**不保证各自正确的改动在语义上兼容**（A 改接口签名、B 按旧签名调用，两张票各自测试都过，合起来炸）。跨票集成错误只有末尾整体复核能抓到
- **UI 类改动的可验判据已存在**：`visual_acceptance.py`（1009 行，browser-backed，缺浏览器即**显式失败**）+ 前端契约测试（`test_frontend_bundle_fresh.py`、`test_frontend_node_tests.py`、`test_runtime_contracts.py`、`test_realtime_protocol_contract.py`）
- **必须先消除的矛盾**：仓库内两个验收模块立场相反——`visual_acceptance` 是 fail-closed，`independent_review` 在 `local_mode` 下是 fail-open。统一到 fail-closed，否则票级"不许过"与末尾"算过"会互相打架

### 4.6 需要等人的决策点，以及如何在舱内消除它们

| 类 | 决策点 | 证据 |
| --- | --- | --- |
| **A 结构性不可逆** | `commit_region` / `commit_all` / `rollback_changeset` / `apply_regions` / `rollback_skill` | `agent.py:651/698`，L3 审批门，`dev_approve` 后 30 分钟内放行 |
| **B 外部能力与信任边界** | MCP 连接器安装（`dev_mcp_add` 返回 blocked，**`dev_approve` 对 MCP 会被拒绝**）、MCP 能力路由（`dev_mcp_decide`）、预览适配器激活/回滚（`dev_preview_adapter_approve/rollback`）、工具安装 `install_tool`、技能启用（`dev_skill_approve`）、`dev_desktop_action` 首调审批门、外部目录写（高权限模式 + 白名单 + 人工确认护栏） | `agent.py:572-598`、`agent.py:631` |
| **C 设计选择（wayfinder HITL）** | destination 定什么、prototype 选哪个方案、fog 何时毕业成 ticket | wayfinder `grilling` / `prototype` 票；其 docs 自承"这个选择是你的，技能没写清楚" |
| **D 判据缺失** | 纯文案 / 纯样式等补不出客观判据的改动 | 决策 12：不进舱，写 `Out of scope` |
| **E 新设计新引入** | ①末尾复核失败：修还是 lane 回滚 ②重切第 2 次失败后：降级还是停 ③死锁放弃后：改派还是停 ④L1 提升：编排自动还是人确认 | 决策 6 / 11 只定了**回滚粒度**，未定**谁裁决**；决策 13 定为**挂起等人** |

> ⚠ **决策 13 与决策 5 的冲突（必须先解决）**：`lanes.py` 的租约是**带 TTL 的**
> （`DEFAULT_TTL_MINUTES = 120`，可调 5 分钟 ~ 24 小时，靠 `heartbeat` 续租）。
> 一张票挂起等人时，它 claim 的文件 pattern 仍处于占用状态：
> - **继续 heartbeat** → 文件被无限期占住，其他票饿死，并行度塌（抵消决策 5）
> - **不续租** → TTL 到期自动释放，别的会话可认领同一批文件，**悲观租约被静默破坏**（决策 5 失效）
>
> 处置方式见 §5.1 第 4 条。

**关键发现：A、B、C、D 四类都能挪出舱，因此不会卡住无人值守。**

- **A 类已有现成解法**：`proposal.py` docstring 写明 *"只跑只读 git 子命令，绝不 add/commit/push"*、`dev_propose`（`agent.py:623`）*“**不提交、不推送**（提交与推送是人的决定）”*、`handoff.py` *"补的是**决策材料**，不是代替决策"*。⇒ **舱不持 commit/push 能力，只产出 proposal**，A 类门禁在舱内根本不触发
- **B 类**：装配期一次性由人授权；舱内若触发，说明该能力未授权 → 该票判为"不可自主"（决策 12）
- **C 类**：属 wayfinder 规划层（舱**之前**的 grilling/prototype 阶段），不在执行舱内
- **D 类**：已由决策 12 移出

⇒ **真正会在舱内卡住人的只剩 E 类四条**，而这四条的共同点是"失败路径的裁决权"。

---

原 §5 三个未决项已全部定案：

| 原问题 | 结论 |
| --- | --- |
| 票的"要动哪些文件"由谁声明 | 决策 7（声明 + 拦截两层） |
| 冷启动基线导入 | 决策 10（混合：结构快照 L0 + 开工前强制查一次 L2） |
| 末尾复核失败的返工策略 | 决策 11（lane 级回滚，不整批） |
| 走不了自主路径的改动 | 决策 12（不进舱，写进 `Out of scope`） |
| 测试与审查的位置 | 决策 8 / 9（每票即验 + 末尾整体复核） |

### 5.1 实施期待定项 —— **已全部定案**（2026-10-12 收盘）

| 原问题 | 结论 |
| --- | --- |
| 默认 lane pattern 的推导规则 | **决策 16**（§5.2） |
| `project_checkpoint` 的 lane 级改造方式 | **决策 17**（§5.2） |
| 「补不出判据」的判定规则 | **决策 18**（§5.2） |
| 挂起期间 lane 租约的处置 | **决策 14**（分情况 C） |

> §5.1 原第 2 条的表述**已过时**：实测 `restore_checkpoint` **本来就支持 `selected_paths`
> 子集回滚**（`project_checkpoint.py:133-179`），lane 级回滚缺的不是回滚 API，
> 而是「按 pattern 建基线」与「实际写了什么的账本」。见 §5.2 决策 17。

### 5.2 三条实施期决策的完整规则

#### 决策 16：默认 lane pattern 推导（机械 5 条 + 冲突足迹回显）

**前提（实测）**：`lanes.py` 有**两个语义不同的匹配器** —— 冲突判定用 `literal_prefix`
（`:58-71`，最长无通配目录前缀；`*.vue` 这类无目录前缀的模式前缀为 `''`，
`_nested('', x)` 恒真 ⇒ 冲突足迹 = 全仓，docstring 明说不假装窄），
归属检查用 `pattern_matches`（`:81-97`，fnmatch 按具体路径判）。
⇒ 一个 pattern 的**冲突足迹**与**实际归属足迹**可以差很远：
`frontend/src/**/*.vue` 归属只覆盖 `.vue`，冲突足迹却是整个 `frontend/src`。
另：`grep "lanes\." agent.py` = **0 命中**， lanes 是纯工具级、无程序集成（君子协定的硬证据）。

**规则**（确定性、可审计）：

| # | 规则 |
| --- | --- |
| 1 | `W` = 票面声明的**具体文件**集合；不接受把目录或通配符当作输入 |
| 2 | 按目录分组：该目录下**现有文件全部** ∈ `W` → 发 `目录/**`；否则逐个发精确文件路径 |
| 3 | **禁止无目录前缀的 pattern**（`*.vue` 这类冲突足迹 = 全仓） |
| 4 | `W` 为空 → **拒绝认领**，不默认全仓 |
| 5 | 展开后 pattern 数 > `MAX_PATHS`(40) → 拒绝并要求重切票（>40 个离散文件的票本就太粗） |

**为什么偏窄是安全的**：决策 7 已把安全性交给第二层（落盘时 `check`），第一层只管调度。
窄的代价 = 「停下 + 上报 + 触发一次重切」（有界、可见）；宽的代价 = 并行度塌（影响他人）。

**DoS 防护 = 回显，不是阈值**：`claim` / `status` 的返回里**回显每个 pattern 的冲突前缀**
（`literal_prefix` 反推，零额外成本——冲突判定本来就要算它）。
例：`frontend/src/**/*.vue` → 明说「此模式阻塞 `frontend/src` 下全部文件」。
**刻意不设「冲突足迹占比 > X% 即拦」的硬阈值**：那是新的魔法常数，会误伤合法大重构，
且定值需要基准数据。等真出现 starvation 证据再加阻断。

**残留风险（记录在案）**：票面写一个大 `W` 即可合法占住大范围——但推导机械来自 `W`，
过度声明必然体现在票面上，**可审计**；冲突是显式拒绝 + owner 可见 + `status` 可查。

#### 决策 17：lane 级回滚三件套

**关键判断**：共享脏树里 **git 无法把改动归属到 lane**（`git diff` 混着别人的在途改动），
只有写路径能归属 ⇒ 必须有一本「实际写了什么」的账，光靠 checkpoint 不够。

| 件 | 做法 |
| --- | --- |
| **lane 基线** | claim 时把 pattern 解析成具体文件 → `create_checkpoint(files=...)`，manifest 加 `lane` 字段 |
| **写账本** | 挂在决策 7 第二层**同一个闸**上（闸本来就要看每个写路径）：记 `{lane, path, op: modify｜create, before_sha256}`。存项目状态目录（与 `lanes.json` 同规矩，原子写） |
| **回滚** | `restore_checkpoint(selected_paths=账本里该 lane 的 paths)`；`op=create` 的文件不在基线里、回滚**不能删** → 显式列「未回滚清单」，按决策 13 交人裁，**不静默算成功** |

**收票时的交叉校验（双来源对账）**：写账本（权威）vs
`git diff --name-only HEAD -- <lane patterns>`（按范围归因）。不一致只可能是
「写了 claim 之外的文件」（逃逸）或「force 共管下别人写了你的范围」——两种都必须浮到台面。
**不让单一来源自己证明自己。**

**前置必修（今天实测到的静默陷阱）**：`project_checkpoint._iter_files`（`:62-84`）的
`files` 参数**只认具体文件**，传 pattern 会被静默跳过、**然后照样全仓 walk** ⇒
`create_checkpoint(files=['frontend/src/**/*.vue'])` 会照抄整个项目，
并被 `_MAX_FILES=800`（`:30`）静默截断。必须：① 支持 pattern→文件解析；
② 「传了 `files` 却零匹配」从静默全仓改为**显式报错**。

**为何不用 git worktree 绕开**：worktree 是可选开通、默认不开（`lanes.py:8`）；
共享树 + 悲观租约是本项目选的轻路线，回滚能力必须在这条路上成立。

#### 决策 18：「补不出判据」的判定规则

**要防的**：模型把难验的票一律判「补不出」→ 活儿被悄悄推出舱 → 舱里只剩简单活、人手里堆债。

**可用神谕清单**（仓库现成，勿凭记忆）：pytest / 契约测试（`test_frontend_bundle_fresh`、
`test_runtime_contracts`、`test_realtime_protocol_contract`）、`dev_diagnostics`
（ruff + vue-tsc）、`visual_acceptance`（1009 行 fail-closed，能判「画面变了」判不了
「好不好看」）、`dev_probe`（http/cli + `expect_status` / `expect_text` / `forbid_text`）、
`ts_index` / `code_intel`（符号在不在）。

| # | 规则 |
| --- | --- |
| 1 | **先试再判**：判「补不出」前必须点名「最接近的神谕 + 为什么不够」，从固定税码选：**无 executable 神谕 / 需人的审美判断 / 需沙箱外外部系统 / 需无法复现的数据状态**。点不出税码 = 判定无效 |
| 2 | **造神谕优先于踢出舱**：属「无 executable 神谕」类 → 先派生前置票「给 X 写验证」。该票本身可验（红-绿规则：修前必红、修后必绿）且能自主完成；落地后原票即变为可验。**只有后三类税码才进 `Out of scope`** |
| 3 | **证据随行**：每次判定都把尝试输出贴进 `Out of scope` 条目——人审的是证据，不是主张 |
| 4 | **单票重切跳闸（不设会话费率阈值）**：每张被判「无神谕」的票，编排方必须用决策 6 那「一次重切」的机会试拆窄；拆出的窄票里只要有一张能导出神谕，就说明**是票切错了、不是活不能验**，按窄票继续；重切后仍无神谕才进 `Out of scope` |

**为什么不设「会话内补不出率 > 1/5」这类阈值**：魔法常数，且小会话（如 4 张票）永远不跳。
改为**结构性**的逐票重切，直接复用已定案的决策 6 机制。会话费率只作**诊断信号**报告，不当门禁。

---

## 6. 验收判据（可证伪）

| # | 判据 |
| --- | --- |
| 1 | 删除完成后，实时对话视频流与窗口嵌入功能**不回归**（当前它们与 `AutonomousCockpit` 耦合，见 §1.2） |
| 2 | 两个小对话 claim 同一文件 pattern 时，后者**被拒**，且不会产生任何写入 |
| 3 | L1 索引在任意一轮结束后**只增不改**：历史条目的字节内容不变（可用 hash 比对验证） |
| 4 | 出现 A↔B 循环等待时，系统在超时内**放弃并上报**，不死等 |
| 5 | 新舱会话内容不出现在开发台 / 问答页的会话中（隔离约束，需可复现的验证脚本） |
| 6 | 重切最多发生 1 次；第 2 次失败必须降级串行并在图谱留下标注 |
| 7 | 任一小对话写入**不在其 lane 内**的文件时，写工具**拦截成功且不落盘**（决策 7 的第二层真的生效，不是君子协定） |
| 8 | 一张票在**没有真实命令输出**时**无法被关闭**（fail-closed，决策 8）；伪造/自述的测试结论不得通过此闸 |
| 9 | `independent_review` 在非 enterprise 环境下**不再返回 `ok: True`**，而是显式失败（消除 §4.5 的立场矛盾） |
| 10 | 两张各自测试通过、但语义冲突的改动，能在**末尾整体复核**阶段被抓到（跨票集成错误不漏网） |
| 11 | 小对话开工前**必有一次"功能是否已存在"的查询**，且该查询结果位于 L2 尾部——即它不破坏 L0+L1 的缓存锚点（用前缀 hash 稳定性验证） |
| 12 | 末尾复核失败时，被回滚的文件集合**等于涉事 lane 认领的 pattern**，其余 lane 的产物字节不变 |
| 13 | 一张被判为"不可自主"的改动，出现在图谱 **`Out of scope`** 段，且在后续轮次中**不重新长回** ticket 或 fog |
| 14 | **决策 16**：同一份票面声明 `W` 推导出的 pattern 集合**确定可复算**（同输入必同输出，可写单测）；任一发出的 pattern 其 `literal_prefix` **不等于 `''`**（无裸通配）；`claim` / `status` 返回里**能读到每个 pattern 的冲突前缀**（饿死他人当场可见） |
| 15 | **决策 17**：构造"某 lane 只改 3 个文件、另建 1 个新文件"的场景 → 回滚后**恰好那 3 个文件**回到基线字节（sha256 比对），其余文件字节不变；新建文件**不被删除**且被显式列进「未回滚清单」。另：修复后 `create_checkpoint(files=[<零匹配的 pattern>])` 必须**显式报错**，不得静默改成全仓快照 |
| 16 | **决策 18**：任一张"补不出"判定必须同时带着**税码**与**尝试输出**；且进 `Out of scope` 之前必须**先发生过一次重切尝试**。反例（任一出现即红）：没税码 / 没尝试输出 / 没重切就直接出舱 |

---

## 7. 下一步先动哪三处

按"修改代价小 → 能证伪决策"排序，先做这三处，别一上来就删：

| 顺序 | 动作 | 为什么先做 |
| --- | --- | --- |
| **1. 窗口嵌入迁出舱**（决策 15 = A） | 把 `#wb-cockpit-runtime-slot` 从 `AutonomousCockpit.vue:2789` 移到 `App.vue` 主区（与 `#wb-playpane-slot` 并列），并摘掉 `App.vue:699` 对 `cockpitRef?.showOutputs()` 的回调 | 保留项目前挂在舱内，删舱即碎；迁出后删舱只剩删组件 + 删 tab 分支 |
| **2. 给写工具加统一闸** | `tools.py` 所有写类工具落盘前 `lanes.check(path)`，越界即拒。**同一道闸顺手记决策 17 的写账本**（`{lane, path, op, before_sha256}`）——闸本来就要看每个写路径，账本几乎零边际成本，且 lane 级回滚（决策 11/17）只有这里能拿到权威归属 | 它把决策 5 / 7 从君子协定变成硬约束，且**不依赖新舱存在**——现在就能做、现在就能测（判据 7） |
| **3. `independent_review` 改 fail-closed** | 非 enterprise 环境不再返回 `ok: True`，改为显式失败 | 一行默认值的改动，直接消掉 §4.5 的立场矛盾；它也是决策 8 能否成立的前提 |

**这三项都不需要新舱**，是给新舱清场 + 验证核心假设的最小动作。做完再动删除。

### 7.0 第 1 项的精确改动集（决策 15 = A：窗口嵌入迁到 workspace 层）

| 文件 | 改动 |
| --- | --- |
| `App.vue:646` 附近 | 主区内与 `#wb-playpane-slot` **并列**新增 `<div id="wb-cockpit-runtime-slot" v-show="..." />` |
| `AutonomousCockpit.vue:2789` | 移除该 slot div |
| `App.vue:699` | `@close` 去掉 `cockpitRef?.showOutputs()` 回调 |
| `SceneRuntimePanel.vue` | **不改**——它只认选择器字符串（`:91` / `:124` / `:853` 三处硬编码），App.vue 里有该 id 即照常工作 |

> ⚠ **硬约束（`App.vue:641` 原注释）**：*"主区用 v-show 常驻（而非 v-if）：保证 slot 在组件挂载期就已存在于文档中，
> 否则 Teleport 目标在挂载时解析为 null → dock 迁移抛错、常驻面板落空。"*
> ⇒ 新 slot **必须常驻 + `v-show`**，禁用 `v-if`。同理 `SceneRuntimePanel` 实例不能被重建
> （注释："同实例仅切 mode，不重建，故 iframe 与引擎嵌入状态得以保留"）。
>
> ⚠ 连带待确认：`App.vue:696` 把 `ChatDock scope="cockpit"` teleport 进 `#wb-cockpit-chat-slot`
> ——该 slot 同样在舱内，迁出时需一并处理。

**验证方式**：`npm run typecheck` → `npm run build` → 起真后端（`uvicorn.run('api:app', port=8100)`）
用 playwright(msedge) 切到 cockpit 工作区，确认窗口嵌入仍在（拍图自查）。

### 7.1 删除前必做的最后一道检查

那 ~6,951 行里**仍有未读过的模块**（本轮已从删除清单中救回 `lanes` / `independent_review` / `verification` / `visual_acceptance` 四个）。逐个读 docstring 再删，不要按"属于旧舱"归类。
