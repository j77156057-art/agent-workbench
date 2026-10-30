# 记忆系统 v2 · P0 规格

> 状态：P0-1、P0-2、P0-3 已落地（2026-10-31，flag 默认关）；P0-4 起待做。
> 开关：全部新路径挂在 `DOCMIND_MEMORY_V2` 下，**默认关**；关时提示词与行为和今天逐字节一致。
> 关联：`docs/agent-self-verify-memory.md`（经验记忆）、`docs/agent-architecture-review.md`（分层审查）。

## 0. 目标与非目标

**目标**：把现在的"逐轮转写 + 关键词召回"升级成三层作用域、六层内容的记忆系统：宪法常驻、共享层按证据晋升、任务书跨窗口、会话私有用钩子抗压缩、回执与评分驱动学习。

**非目标（P0 不做）**：晋升全自动、多窗口冲突自动合并、跨设备同步、图数据库。

**P0 完成判据**：分层可读写、索引可重建、宪法生效且可审计、任务书完成态受回执约束、星级可归因、上下文面板可查看并编辑、旧库迁移幂等、flag 关时零回归。

## 1. 三层作用域与六层内容

| 作用域 | 层 | 内容 | 写者 |
|---|---|---|---|
| global | L0 宪法 | 身份、不可越界、证据纪律、升级规则、记忆边界 | 仅用户（模型只能提案） |
| global | L1 画像 | 用户声明 + 确定性学习计数 | 用户 / 系统 |
| global | L2 共享记忆 | 事实、教训、技能 | 模型提炼 → 晋升门；技能需审批 |
| project | L3 任务书 | 目标、条目、状态、回执引用 | 模型写，状态由回执裁决 |
| project | 项目规则 | `DOCMIND_RULES.md`（已有） | 用户 / 分区初始化 |
| session | L4 会话私有 | 结构化回合、钩子、错误账本 | 系统自动 |
| session | L5 归档 | 快照、摘要、交接包 | 后台任务 |

## 2. 目录与文件格式

```
.docmind/memory/
  constitution.md                 # L0
  profile/declared.md             # L1 用户声明
  profile/learned.json            # L1 确定性计数（可清空）
  shared/facts/<id>.md
  shared/lessons/<id>.md
  shared/skills/<name>/SKILL.md   # 沿用现有 skills.py 格式
  shared/index.sqlite3            # 可重建索引
  projects/<prj-id>/
    taskbook.json  taskbook.md
    events.jsonl                  # 追加式事件日志
    receipts/<id>.json            # 系统写，append-only
    session/<sid>/episodes.jsonl
    session/<sid>/hooks.md
    session/<sid>/errors.md
    session/<sid>/feedback.jsonl
    archive/legacy/               # 迁移产物（只读）
  ledger.jsonl                    # 写入/晋升/退休/删除/编辑审计
  metrics/<date>.jsonl            # 固化与注入计量
```

### 2.1 constitution.md

```yaml
---
version: 3
updated: 2026-10-30T12:00:00+08:00
hash: sha256:...          # 正文 + clauses 的哈希，用于审计与变更留痕
clauses:
  - id: C-001
    text: 未经用户明确批准，不得 push、强推或改写已发布历史
    machine_check: git_push_requires_approval
    enforced_by: tools.py:COMMAND_BLACKLIST
---
正文（人读；模型读到的是正文 + clauses 摘要）
```

哈希只做审计，不做访问控制；"模型改不了"靠**没有写入路径**保证。

### 2.2 记忆条目

```yaml
---
id: mem-...
kind: fact | lesson | skill_ref
scope: global | project | session
status: candidate | active | superseded | retired
confidence: 0.0-1.0      # 由回执/采纳生成，模型不可直写
importance: 0.0-1.0
usage: 0
edges: [{type: derived_from|supersedes|used_in|refines, to: id}]
receipts: [rc-...]
created: ...  updated: ...  last_used: ...
---
正文（≤600 字符）
```

学习状态（confidence/importance/usage/edges）必须落在文件里，索引重建不丢失。

### 2.3 回合记录（L4）

```json
{"id":"ep-...","ts":"...","session":"...","task":"...","goal":"...",
 "actions":[{"tool":"...","ok":true,"digest":"..."}],
 "result":{"status":"ok|fail|partial","summary":"..."},
 "decisions":["..."],"errors":[{"sig":"...","cause":"..."}],
 "next":"...","receipts":["rc-..."],"tokens":{"in":0,"out":0}}
```

原文只留在会话文件；episode 只存结构化摘要，**不存逐字长文**。

### 2.4 钩子与错误账本

```json
{"id":"hk-...","kind":"fact|decision|error|user_agreement","text":"...",
 "source_turn":"...","verified":true,"pinned":true}
```

错误账本：`{sig, cause, fix, verified, occurrences}`；`sig` 为归一化错误签名（类型 + 文件 + 行）。

### 2.5 回执与评分

```json
{"id":"rc-...","type":"test|verify|command|merge|review","cmd":"...",
 "result":"pass|fail|uncertain","evidence":"...","ts":"...","session":"...","task":"..."}
{"id":"fb-...","stars":1,"task":"...","session":"...",
 "memories_used":["..."],"memories_written":["..."],"ts":"..."}
```

回执由系统写，模型不可改；评分仅用户可写。

## 3. 索引（可重建）

表：`items / edges / vectors / usage / receipts_ref` + FTS5 关键词表。

- 写入顺序：文件 → 索引；文件是事实来源。
- `body_hash` 变更检测：启动时与 `rebuild --verify` 时比对，手工改文件会被发现并重新索引。
- 重建：`python -m agent_runtime.memory rebuild` 扫文件重建全表；向量只对 hash 变化的条目重算。
- 验收：删掉 `index.sqlite3` 后重建，条目、边、评分、用量与删前一致。

## 4. 注入顺序、优先级与预算

1. 系统工具规范（现状，不动）
2. L0 宪法（≤600 字符，带 version）
3. L1 画像（≤500）
4. 项目规则（已有）
5. L2 pinned（≤5 条 / ≤800 字符）
6. L3 任务书摘要（≤400，未完成前 8 项 + 下一件事）
7. L4 钩子 + 压缩摘要（≤600）
8. 历史滑窗 + 当前问题

前 7 块顺序固定、内容稳定，利于 prompt 缓存。优先级：**平台安全 > 宪法 > 当前明确请求 > 项目规则 > 画像 > 记忆**。

每回合把实际注入块写入 turn trace：`injection: [{layer, chars, hash, text}]`，供上下文面板与验收测试对账。

## 5. 宪法与代码闸门

| 条款 | machine_check | 执行点 |
|---|---|---|
| 禁强推 / 改写已发布历史 | `git_force_push_blocked` | 命令黑名单 |
| 提交 / 推送需批准 | `git_push_requires_approval` | 审批层 |
| 无回执不得声称完成 | `done_requires_receipt` | 任务书状态机 |
| 不得写 L0 | `constitution_readonly` | 存储层路径守卫 |
| 不得把密钥写进记忆 | `secret_rejected` | scrub + 写入拒绝 |
| 危险 / 递归删除需确认 | `destructive_needs_confirm` | 命令闸门 |
| 跨项目晋升须脱敏 | `promotion_scrubbed` | 晋升门 |
| 删除记忆 / 技能需确认 | `memory_delete_needs_confirm` | 审批层 |

其余条款标注"提示级"，不假装能强制。宪法加载器：注入前校验 hash；不一致 → 记 ledger + 版本提示，但仍注入（文件归用户所有）。

## 6. 写入路径

```
回合结束（同步、无模型）
  ├─ episode + hooks + errors + receipts
  └─ 评分卡（仅交付型回合）

后台（异步、单 worker）
  ├─ 候选抽取 → ADD / UPDATE / DELETE / NOOP
  ├─ 去重合并；冲突标 superseded（保留"曾经为真"）
  └─ 评分 / 衰减；候选晋升（不自动 pin）
```

晋升门（session/project → global）硬条件：≥1 张验证回执 ∧ ≥1 次采纳/复用 ∧ 通用性检查（脱敏、去路径、去客户名、脱离项目可读懂）。技能只能提案 → 用户批准。

## 7. 任务书

- 模型按判据自评：跨回合 / 从零建项目 / 步骤 >5 且相互依赖 / 用户要计划。**不阻断开工。**
- 系统兜底：连续 15 次工具调用、或即将压缩、或会话结束仍有未完成多步工作且任务书为空 → 从 journal 草拟，下一轮请模型确认。
- 状态机：`todo → doing(窗口租约) → done-pending-merge → done`。
- `done` 只认"回执 + 合并记录"；用户可强制完成，但写 `user_override` + ledger。

**P0-3 实现说明**：模型自评入口复用现有 `dev_plan`——当前计划 ≥3 项才镜像成任务书；长任务跑满 15 步仍无任务书时由系统草拟。计划条目被勾选只进入 `done_pending_merge`，必须再由验收回执和合并引用才能转 `done`。

## 8. 星级反馈

- 对话下方小卡片：五颗星 + 确定，可忽略；只在交付型回合出现（写了文件 / 跑了验证 / 勾掉任务书条目）。
- 评分挂任务书 id；没有则挂本轮交付。归因到本次"用到的 + 写入的"记忆。
- 4–5 星：相关记忆 confidence +0.05（封顶 1.0），计一次采纳；3 星中性；1–2 星：相关记忆**晋升冻结**，错误账本记一条，不删除。
- API：`POST /api/feedback/task`；存储 `feedback.jsonl`；下一轮最多带一句"上一任务 N 星"。

**P0-3 实现说明**：前端在"交付型回合"（后端 final 带 `deliverable`）的答案下方显示五颗星 + 确定/忽略；提交走 `POST /api/feedback/task`，落 `session/<sid>/feedback.jsonl`。P0 先存外部信号（`memories_used/written` 暂为空），归因与置信增减在 P4 接晋升时消费。

## 9. 上下文面板（查看 + 编辑）

### 9.1 交互

- 入口：对话输入区新增"上下文"按钮（图标 + tooltip「查看本项目装载的记忆」）；点击打开**右侧抽屉**，不遮挡对话。
- 抽屉结构：顶部总览（本次注入总字符 / 预算、生效时机「下一回合」）；下方按层分页：
  `宪法 / 画像 / 共享记忆 / 任务书 / 钩子 / 项目规则 / 历史摘要`。
- 每层显示：渲染文本、字符数 / 上限、来源徽标（用户 / 系统 / 模型 / 回执）、可编辑状态。
- 双视图：「下一回合将注入」（默认，可编辑）与「上一回合实际注入」（只读，来自 turn trace `injection`）。

### 9.2 可编辑范围

| 层 | 可编辑 | 规则 |
|---|---|---|
| L0 宪法 | 是（仅用户） | 二次确认 + 版本自增 + ledger；提示"影响所有项目" |
| L1 画像 | 声明可编辑；学习计数只读 + 一键清空 | 保存即生效 |
| L2 共享记忆 | 文本 / pin / 删除 | 全局条目二次确认；编辑后 `source=user_edit`、`receipts=[]`、`confidence=0.5`、晋升冻结至重新验证；删除需确认 |
| L3 任务书 | 条目文本 / 勾选 / 排序 | `done` 需回执或 `user_override` |
| L4 钩子 | 文本 / pin / 删除 | 保存即生效 |
| L4 episodes | **只读** | 系统证据，不进编辑面 |
| 项目规则 | 只读（给"打开文件"入口） | 避免面板与 ingest 双写 |
| 历史摘要 | 只读 | 由压缩任务生成 |

### 9.3 生效与并发

- 编辑写文件 → 更新索引 → **下一回合生效**；面板显示"已保存，下回合生效"。
- 乐观并发：`PUT` 带 `base_version`（文件 `updated` 哈希），冲突返回 409，面板提示"另一窗口已修改，请刷新"。
- 文件写入原子化（tmp + `os.replace`）；多窗口共享 project 状态，靠 `file_locks` 串行化。

### 9.4 安全

- 用户编辑仍属"记忆文本"，除 L0 外**不是指令**，面板展示该提示。
- 写入前跑密钥扫描（C-005）：命中 → 拒绝并说明，不静默脱敏。
- 所有编辑 / 删除写 ledger（层、条目、diff hash、时间）。

### 9.5 API

```
GET    /api/memory/context?session_id=&project_id=&mode=next|last
PUT    /api/memory/constitution        {body, base_version}
PUT    /api/memory/profile/declared    {body}
PATCH  /api/memory/items/{id}          {title?, body?, pinned?}   # L2
DELETE /api/memory/items/{id}?confirm=true
PATCH  /api/memory/hooks/{id}          {text?, pinned?}
PATCH  /api/memory/taskbook/items/{key} {text?, done?, order?}
POST   /api/memory/reindex
```

前端：`ChatComposer.vue` 增加 `memory` 事件；新增 `MemoryContextPanel.vue`；`frontend/src/workbench/api/memory.ts`。

## 10. 固化模型设置

- 新设置 `DOCMIND_MEMORY_MODEL`：空 = 跟随主模型（默认）；或 `provider:model` 显式指定。
- 设置页「长期记忆」分区新增下拉：跟随主模型 / 指定模型；保存后落 `runtime` + `.docmind_state.json`。
- 运行时：显式模型不可用 → 记录一次告警，回退主模型 clone，**不丢回合**。
- 计量记录**实际生效模型**；报告单列"固化成本占比"。

## 11. 迁移

旧库只读导出，幂等可重跑：

| 来源 | 去向 |
|---|---|
| 462 条逐字 episode | `archive/legacy/episodes.jsonl`（不注入） |
| 40 条蒸馏摘要 | `shared/facts/` 候选（低置信、不 pin） |
| 13 条已退休 | 归档保留 |
| 1 fact + 1 preference | L1/L2（preference 保留原文证据） |
| 352 条 checkpoint | `archive/legacy/checkpoints/`（保留 30 天） |
| experience 集合 | L2 lessons（带 project_id 来源） |
| skills | 原地不动 |
| plan-v1.json | 任务书草稿 |

全程 ledger；旧库不改动；跑两遍结果一致。

## 12. 计量（不设硬预算）

先测后定阈值：

- 每次固化：模型、tokens in/out、延迟、ops 数、跳过原因、积压数。
- 每回合：各层注入字符、检索命中、评分卡出现 / 作答。
- 任务小时汇总：`python -m agent_runtime.memory report --by task-hour`，输出每小时任务成本与 p50/p95。
- 结构性控制（不是预算）：单 worker；前台生成时暂停；积压 >1 时合并为一次调用；同会话 10 分钟 >20 次触发则退避 10 分钟；提供商报错跳过。
- 可选软上限：设置页可填每日上限，默认"不限"。
- 定阈值：跑满 2 周或 1000 轮后，按 p95 的 2 倍给出建议值，用户确认后启用。

## 13. 验收测试

1. flag 关：提示词与今天逐字节一致（含 309 校准）
2. 注入顺序与各层字符上限
3. 宪法 hash 变更 → ledger + 版本递增
4. 索引删除后重建 → 条目 / 边 / 评分 / 用量一致
5. 回合记录不含逐字长文；压缩后钩子仍在
6. 无回执的任务条目标不了 done；`user_override` 有审计
7. 1 星 → 相关记忆晋升冻结、不删除；5 星 → 置信增长封顶并计采纳
8. 晋升门拦截含路径 / 密钥的条目
9. 迁移跑两遍一致，旧库未被修改
10. 固化 worker：跳过平凡轮、积压合并、前台优先、失败不破坏数据
11. 任务书兜底：15 步无任务书 → 自动草稿
12. 上下文面板：显示内容与 turn trace `injection` 对账一致；编辑下回合生效；409 冲突；密钥拒绝；删除需确认
13. 固化模型设置：默认跟随主模型；显式模型生效；不可用回退并告警

## 14. 接线点与实施顺序

新增包 `agent_runtime/memory/`：
`store / index / constitution / profile / episodes / hooks / receipts / taskbook / consolidate / promote / feedback / context_panel / migrate / metrics`

改动点：`agent.py`（注入位置与 flag、turn trace `injection`）、`api.py`（memory / feedback 端点）、`ChatComposer.vue` + `MemoryContextPanel.vue`（上下文面板）、`SettingsView.vue`（固化模型）、`plan_state.py`（任务书适配）、`verification_gate.py`（回执来源）。

实施顺序（每步一个 commit，flag 默认关）：

- **P0-1** 骨架：目录、文件存储、索引与重建、宪法加载与注入、flag 与回归测试
- **P0-2** 私有层：结构化回合、钩子、错误账本、回执
- **P0-3** 任务书：事件日志、状态机、回执裁决、系统兜底
- **P0-4** 共享层：晋升门、评分、固化 worker（计量优先）、迁移
- **P0-5** 界面：上下文面板（查看 + 编辑 + 审计）、星级卡、固化模型设置

## 15. 已定的小决策（2026-10-30）

1. **上下文面板 P0 只展示"已注入"条目**；"可召回但未注入"留到 P1，UI 预留折叠位。
2. **用户直接编辑共享记忆**后：`source=user_edit`、清空 `receipts`、`confidence=0.5`、晋升冻结直到重新验证；用户仍可显式 pin（用户意图高于自动门）。
3. **"须知"不单独成层**：并入项目规则 + 开工检查清单，避免第四块常驻内容。
