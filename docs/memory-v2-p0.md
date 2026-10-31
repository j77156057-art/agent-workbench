# 记忆系统 v2 · P0 规格

> 状态：记忆系统 v2 P0 + P1 完成（含学习闭环、真实迁移、bge-m3 语义、记忆评测与宪法闸门，2026-10-31；真实设置已开启）。
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

**P0-4 实现说明**：项目候选写在 `projects/<pid>/facts|lessons/`；晋升门校验"通过的 test/verify/merge 回执 + 采纳（4–5 星或复用≥2）+ 通用化（拒绝密钥/项目标识/强上下文指代，绝对路径替换为 `<path>`）"。晋升后 `confidence=0.85`，`importance≥0.7` 才自动 pin；未 pin 的共享条目只进索引，不进每轮提示词。实时固化由单 worker 异步执行，跳过未完成/平凡轮，失败只计量；模型可用 `DOCMIND_MEMORY_MODEL` 单独指定（仅限同 provider，否则回退主模型）。

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

前端：`ChatComposer.vue` 增加 `memory` 事件；新增 `MemoryContextPanel.vue`；`frontend/src/workbench/api/memoryPanel.ts`。

**P0-5 实现说明**：面板已落地——输入区「记忆」按钮打开右侧抽屉（Teleport 到 body，避免被对话台定位上下文裁切），按层显示字符/预算，支持「下一回合 / 上一回合」双视图；L0 宪法、L1 画像、L2 共享条目、L3 任务书、L4 钩子可直接编辑，episodes / 错误账本 / 项目规则 / 历史摘要只读。L1 编辑沿用现有画像接口（SQLite），文件化的 `profile/declared.md` 留到 P1。"可召回但未注入"按 §15 决策留到 P1，面板只显示已注入项。

## 10. 固化模型设置

- 新设置 `DOCMIND_MEMORY_MODEL`：空 = 跟随主模型（默认）；或 `provider:model` 显式指定。
- 设置页「长期记忆」分区新增下拉：跟随主模型 / 指定模型；保存后落 `runtime` + `.docmind_state.json`。
- 运行时：显式模型不可用 → 记录一次告警，回退主模型 clone，**不丢回合**。
- 计量记录**实际生效模型**；报告单列"固化成本占比"。

**P0-4 实现说明**：配置层已支持 `get_memory_model/set_memory_model`（runtime 优先、env `DOCMIND_MEMORY_MODEL` 回落、重启恢复）；设置页下拉在 P0-5 补。显式模型只在同 provider 下生效，避免把 A 家的 Key 发到 B 家端点。

**P0-5 实现说明**：设置页「智能体」分区新增「分层记忆 v2」开关与固化模型输入（留空 = 跟随主模型），切换即时落盘；浏览器实测开关回显与 `GET/POST /api/memory-settings` 一致。

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

**P0-4 实测（本机旧库）**：归档 464 条、共享候选 41 条、偏好 1 条、checkpoint 352 条、experience 23 条、退休 13 条；第二次运行直接跳过；`--force` 不产生重复文件。旧库以只读 URI 打开。

## 12. 计量（不设硬预算）

先测后定阈值：

- 每次固化：模型、tokens in/out、延迟、ops 数、跳过原因、积压数。
- 每回合：各层注入字符、检索命中、评分卡出现 / 作答。
- 任务小时汇总：`python -m agent_runtime.memory report --by task-hour`，输出每小时任务成本与 p50/p95。
- 结构性控制（不是预算）：单 worker；前台生成时暂停；积压 >1 时合并为一次调用；同会话 10 分钟 >20 次触发则退避 10 分钟；提供商报错跳过。
- 可选软上限：设置页可填每日上限，默认"不限"。
- 定阈值：跑满 2 周或 1000 轮后，按 p95 的 2 倍给出建议值，用户确认后启用。

**P0-4 实现说明**：计量落在 `metrics/<date>.jsonl`；`python -m agent_runtime.memory report` 汇总调用数、token、p50/p95、晋升数。结构性控制已实现：单 worker、同会话在跑则跳过、平凡轮跳过、LLM 异常只计量。仍未设任何硬预算。

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

**P0 完成验收（2026-10-31）**：后端 **271 passed**（memory v2 53 + trace/记忆/prompt/plan/hooks/API）；前端 **158** 个 node 测试通过，`vue-tsc` 与 `vite build` 通过。Edge 实测：上下文面板 7 层显示、宪法编辑保存后 `version 0→1` 且 `raw` 持久化、钩子添加/删除、失败回执拦截与用户覆盖；设置页 v2 开关回显与服务端一致；普通回答不弹满意度卡，交付型回合弹并成功写入 `feedback.jsonl`。

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

## 16. P1 增量（2026-10-31）

本轮完成四项：按需检索、关联边、画像文件化、真实成本校准。P0 的六个层与开关不变。

### 16.1 按需检索（可召回未注入）
- `index.search` 按关键词打分并叠加 pin 分，**按当前项目隔离**（`global` + 当前项目），项目候选（status=candidate）也在检索范围内；索引新增 `body` 与 `project` 列，旧索引自动补列。
- `memory_search` 在 v2 开启时合并 v2 检索结果（带 `source=memory_v2`、scope、置信、关联来源），v2 关时输出保持旧形状。
- 语义召回：`memory_semantic`（设置页 v2 分区）或旧 `memory_embed` 开启时，重建索引会为条目写向量，检索按 `0.35·语义 + 0.20·关键词 + 0.20·重要度 + 0.15·置信 + 0.10·时效` 排序；嵌入后端故障自动退化为关键词。注意：本机 Ollama 尚无 `bge-m3` 时会退回本地哈希（非语义），要真实语义需选 qwen 嵌入或先拉取 `bge-m3`。
- 上下文面板「共享记忆」下新增「可召回但未注入」折叠区：共享未 pin 条目可直接 pin，项目候选可点「晋升」；晋升失败会展示具体原因（缺回执/缺采纳/通用化不通过）。

### 16.2 关联边
- 边类型：`supersedes / derived_from / used_in / refines / contradicts`；存储在条目 frontmatter 的 `edges`，索引同步到 edges 表。
- 晋升保留边；`POST/DELETE /api/memory/items/{id}/links` 增删；检索对前 3 条结果做 1 跳扩展（同样受项目隔离约束），返回 `related_to`。
- 面板支持增加/解除边：在 pin 条目下选择类型、填目标 id 即可；浏览器实测「解除 → 添加」两端生效。
- 语义召回已接入（见 16.1）；图数据库与 Personalized PageRank 仍未引入。

### 16.3 画像文件化
- 新增 `profile/declared.md`（frontmatter 存声明字段）与 `profile/learned.json`（只读镜像）。
- 首次读取从 SQLite 导入；旧接口/显式自我介绍更新 SQLite 后，下次读取按 `updated` 自动合并进文件；面板与 `PATCH /api/agent/profile` 在 v2 下写文件并镜像 SQLite。
- v2 开启时 Agent 注入改用文件画像；关闭时仍用旧 SQLite 画像，行为不变。

### 16.4 固化成本校准（真实 5 回合，qwen-plus）
只读任务（README/docs/memory 模块/测试/总结），`qwen-plus`，v2 开、旧蒸馏关：

| 项目 | 实测 |
|---|---|
| 主模型 | 11 calls / 477,962 in / 2,619 out / ¥0.1628（≈¥0.0326/轮） |
| 固化 | 5 calls / 3,383 in / 816 out / ¥0.0043（≈¥0.0009/轮），p50 3.6s / p95 4.9s |
| 记忆产物 | 5 个项目候选，0 晋升（无回执/采纳） |
| 外推（按轮数） | 60 轮 ≈ ¥2.01；120 轮 ≈ ¥4.01；200 轮 ≈ ¥6.69；固化约占 2.5% |

说明：这是**有界样本**，不是数小时长任务；外推假设每轮开销接近本样本（主模型 token 主要来自工具 schema/历史，长任务会波动）。固化已加 `max_output_tokens=1024`（qwen-plus 的 32768 上限下此前会 400），因此成本与延迟都受控；仍未设硬预算。

**完整 53 题长任务（qwen-plus，2026-10-31）**：只读黄金题库，排除 3 道会写文件的（G41/G43/G45），独立记忆/轨迹目录：

| 项目 | 实测 |
|---|---|
| 结果 | 53/53 完成；评测 **47 通过 / 6 失败，通过率 88.7%，平均分 0.96** |
| 运行 | 1440.5s（约 24 分钟），142 次工具动作 |
| 固化 | 49 calls / 4 keep=false / 0 error；28,601 in / 6,665 out；p50 4.7s / p95 7.3s；**¥0.0223** |
| 记忆产物 | 49 个项目候选，0 晋升（当时学习闭环未接；现在可采纳/按星级晋升） |
| 失败 | G50/G51 是 booklog 题但按 rag-agent 项目跑（项目不匹配）；G01 选 read_file 而非 grep；G26/G27 无工具动作；G42 复述了不该复述的原文 |

主模型精确账单未进 trace（golden runner 不记录 token）；按 5 回合样本 ¥0.0326/轮估算，53 题主模型约 ¥1.7。固化实测 ¥0.0223，仍只占约 1–2%。

### 16.5 仍未做
多用户/团队隔离（需要先定认证与共享边界）与跨设备同步（需要传输与冲突协议）。图检索已用本地 Personalized PageRank 落地（不引入外部图数据库）；其余（语义召回、边编辑、学习闭环、去重/保留、跨会话错误、bge-m3 语义、记忆评测、宪法闸门、真实迁移）均已落地。

### 16.6 应用状态与模型
- `.docmind_state.json` 已开启 `memory_v2: true`；默认模型现为 **opencode / deepseek-v4.1-flash**，Key 优先读环境变量 `OC_KEY`（其次 `OPENCODE_API_KEY`，最后全局密钥库）；`/api/config` 实测返回 `opencode/deepseek-v4.1-flash` + `has_key=true`，3 题冒烟 3/3 通过。
- qwen-plus 仍可用：`output_token_budget` 新增 qwen 厂商上限 32768，修复 `max_tokens` 400；5 回合与 53 题长任务均验证通过，可随时在设置里切回。
- 注意：opencode 的长任务曾在前 21 题后长时间无新结果（已停止）；短会话已验证正常。若长任务再次停滞，优先检查 opencode 会话/限流，或临时切回 qwen-plus。


### 16.7 学习闭环与真实迁移（2026-10-31）

- **候选采纳**：面板「可召回但未注入」里的项目候选新增「采纳」。采纳会生成 `review` 回执（`cmd=user_review`）、置 `adopted=true`、`usage+1`、清除冻结，并立即尝试晋升；成功后共享条目与索引同步 upsert。
- **星级归因**：4–5 星会对该 task 的候选生成 `review` 回执并采纳/晋升；1–2 星置 `promotion_frozen=true` 且 `evaluate` 拒绝晋升（不删除）；3 星中性。没有 task 的星级只统计，不做归因（防止误伤所有候选）。
- **复用计数**：`memory_search` 命中 v2 条目与采纳都会递增 `usage/last_used`，`pin_score` 的复用权重开始生效；索引 `touch` 同步。
- **索引一致性**：共享条目编辑、晋升、采纳、状态/pin 变化立即 `upsert_item`；`rebuild` 现在比较标题/状态/置信/重要度/pin/usage/时间，不再只看 body_hash。
- **真实迁移已执行（按原项目隔离）**：旧库只读导出 **471** 条归档、41 条事实 + 23 条经验教训（按原 scope 落项目候选、低置信、不 pin）、1 条偏好、354 条 checkpoint、23 条 experience、13 条退休；第二次运行 `skipped`；索引重建 **85 条，verified=true**。共享层 0（不再把项目记忆灌进 global）；当前项目候选 46 条可召回。第一轮误灌 global 的产物已移到 `archive/legacy/migrated-shared-*` 并按 scope 重跑。
- **验证**：后端 **283 passed**、前端 158 node、build/typecheck 通过；Edge 实测候选「采纳」后 1→0、提示「已采纳并 pin」、无控制台错误。


### 16.8 去重、保留与索引一致性（2026-10-31）

- **写入近似去重**：`create_candidate` 先找同 kind 的共享 active/项目候选；中文 bigram + 英文词 Jaccard，短句叠加 `SequenceMatcher`，阈值 0.88。命中则合并 receipts、重要度（取高）、usage、last_used，不再新增碎片；共享副本优先，不同 kind 不合并。
- **历史清理**：`python -m agent_runtime.memory dedupe --project <id>` 对存量候选做一次合并；本机 28 条检查、0 条命中（本轮候选彼此差异足够大）。
- **回合保留**：`episodes.jsonl` 超过 512KB 自动把旧行移到 `projects/<pid>/archive/episodes/`，保留最近 200 条；`latest` 仍只读尾部。
- **索引一致性**：`upsert_item` 在编辑/晋升/采纳/状态/pin 变化时立即同步行、边、FTS 与向量；`rebuild` 比较标题/状态/置信/重要度/pin/usage/时间，不再只看 body_hash。
- **验证**：后端 **289 passed**；前端 158 node、build 通过；历史去重 CLI 与真实迁移幂等均已实跑。

### 16.9 跨会话错误记忆（2026-10-31）

- `errors.record` 现在同时写会话账本与项目汇总 `projects/<pid>/errors.md`；同一错误签名跨窗口累计次数。
- L4 注入在会话错误之外追加「跨会话易错」，并排除本窗口已出现的签名，避免重复；总预算仍受 600 字符上限约束。
- 上下文面板 L4 分开展示「本窗口易错点」与「跨会话易错点」，均为只读。
- 验收：后端 **288 passed**；跨会话签名计数、注入和面板数据均有测试覆盖。

### 16.10 bge-m3 语义后端与迁移隔离修正（2026-10-31）

- `ollama pull bge-m3` 完成（1.2GB，Ollama 0.40.13）；`EmbeddingClient` 实测 **1024 维、degraded=false**。
- 已落盘 `embedding_provider=ollama`、`embedding_model=bge-m3`、`memory_semantic=true`；索引重建 **85 条 / 85 个向量，5.5s**，混合检索的 `_semantic` 实测约 0.42–0.60。
- **迁移隔离修正**：原迁移把旧项目记忆写成 global 共享，检索会带出别的项目路径；已改为按原 scope 落项目候选（`project_item_id` 带项目前缀），旧 global 迁移产物归档后重跑。当前共享层 0，当前项目候选 46，跨项目不再互相召回。
- 注：语义向量按模型维度固定；若改用 qwen 嵌入需重建索引，避免维度不一致。

### 16.11 记忆评测与宪法闸门（2026-10-31）

**记忆专用评测**：`python -m agent_runtime.memory eval [--semantic]`
- 临时库种 6 条标准事实 + 6 条干扰项、6 个查询；同时跑晋升门（缺回执拒绝/采纳晋升）、历史去重、跨项目隔离、低星冻结。
- 实测：关键词 recall@3 **0.833 / MRR 0.833**；bge-m3 语义 **recall@3 1.0 / MRR 1.0**；7 项结构检查全 true。
- 每次运行使用临时记忆目录、结束清理，不污染真实库；`--semantic` 需要可用嵌入后端，失败时明确标注 `semantic_effective=false`。

**宪法条款代码闸门**：`agent_runtime/memory/gates.py`
- 8 条 `machine_check` 逐条绑定真实落点：C-001/002/006 命令黑名单、C-003 任务书回执门、C-004 无模型写入路径、C-005 密钥扫描、C-007 晋升脱敏、C-008 删除确认。
- 宪法加载时会校验并暴露 `gates_ok/missing_gates`；未绑定条款写入 ledger，面板 L0 显示“未绑定闸门”。
- `tests/test_memory_gates.py` 对每条条款调用真实闸门做回归；`tests/test_memory_eval.py` 锁住评测最低线。
- 总回归：后端 **302 passed**；前端 158 node、build/typecheck 通过。

### 16.12 本地图检索 / Personalized PageRank（2026-10-31）

- 不引入 Neo4j 等外部图库：在现有 edges 表上做 **Personalized PageRank**，边按无向激活传播（关联/取代语义仍保留在边的 type 上）。
- 检索先用关键词 + 语义得到基础档序，再以 top-3 为种子做 PPR，把 1–2 跳关联条目带进结果（`_graph` 标记），并继续受项目隔离与状态过滤约束；无 edges 时回退到原来的 1 跳扩展。
- 测试：链式 A→B→C 中只查 A 的命中，PPR 仍能带出两跳 C；项目隔离与原有边测试保持通过。
- 总回归 **302 passed**。

### 16.13 长任务实测：钩子 / 压缩 / 固化质量（2026-10-31）

- **真实任务**：临时项目 ORCHID-2718，6 回合读代码、制造一次确定性读文件失败、修两个 bug、写决策文档、全量测试；用 `DOCMIND_SESSION_MAX_TURNS=4` 强制触发会话压缩；模型 opencode/deepseek-v4.1-flash。
- **自动钩子**：确实自动生成，但目前只有两类——工具失败（`error`）与用户明确“记住/约定”（`user_agreement`）；普通结论不会自动进 L4。首轮出现 4 条假/错钩子：pytest 缓存 `拒绝访问` 警告被 `_is_failure` 当失败、失败文本截到 400 字只剩警告头、钩子用 `journal.last_action` 把 `run_command` 失败标成 `grep`。已修：退出码 0 / 结构化 `ok:true` 直接判成功；`Turn.last_failure_tool` 记录真正失败工具；钩子按失败签名去重并只留 `FAILED/error` 行。
- **自动压缩**：压缩触发后摘要从 448 → 1110 → 1200 字符；首轮摘要因每轮只喂 400 字而丢掉两个 API 签名，且只追加不合并、无上限。已修：每轮源文本 1200 字、提示词只留长期事实/精确标识符/错误码、旧新摘要超 1200 时二次合并并封顶、硬截断带省略号。
- **压缩后记忆**：同会话重启（只靠摘要 + 保留轮次）能准确答出项目代号、三个 API 签名、`ERR_EMPTY_INPUT`、`ORCHID-2718 sum=6`、已废弃旧代号；新窗口通过 `memory_search` 也能召回 v2 候选与旧 episode。
- **长期记忆**：首轮 4 条候选，2 条有用，1 条“本轮未验证”中间态、1 条低价值；已修：`consolidate._trivial` 跳过明确“尚未完成验证”的回合。复测 3 条候选：规格偏差事实、终检清单、确定性读文件失败经验；0 条晋升（仍需回执 + 采纳/5 星 + 泛化，符合设计）。
- **候选时间失效**：固化时把最近 10 条项目候选一并交给模型；新回合若证明旧候选过时，输出 `supersedes` 旧 id。`promote.supersede` 把旧条目改为 `superseded`（不再检索/可召回），并在新条目上加 `supersedes` 边。真实 LLM 已在 scratch 项目上把“compute 返回 0、测试失败”的旧诊断替换为终检后的新事实。
- **中间轮验证**：`run_command` 命中测试命令（pytest/npm test/go test 等）且退出码 0，同时测试后未再改文件 → 收尾门接受为 `verification_status=test_command`，不再替换成“未验证”；`record_turn` 同步写 `type=test, result=pass` 回执，供后续晋升门使用。
- **仍未做**：已晋升到共享层条目的自动失效仍需人工/共享层流程；候选取代依赖模型判断，可能漏判；摘要提示词已收紧一次性指令/环境噪声，但仍需长期观察。
- **验证**：相关后端套件全绿（最近三组 **126 / 108 / 31 passed**：failure markers、session archive、memory v2、trace、memory opt/eval/gates）；真实复验 write+pytest 回合 final 未再被替换，状态为 `test_command`，回执含 `test/pass`；复测报告见 `artifacts/longtask/report.json` 与 `artifacts/longtask2/report.json`。

### 16.14 全量回归基线（2026-10-31）

- **后端 unittest discover**：5114 tests / 1300s，6 failures + 10 errors + 14 skipped。其中只有 `test_llmslice_clip` 是本次摘要改动引入的（提示词含“截断”被旧断言误读、源上限已从 400 提升到 1200）；已在 `2bca9da` 修正并单独 6/6 通过。其余 **15 条红全部在父提交 874257e 的干净 worktree 上同形复现**：engine_start 相关 10 条、voice secrets 2 条、workflow SSE 2 条、MCP live playwright 1 条，均为存量/环境问题，不属于本次改动。
- **CI pytest-style lane**：1084 passed / 1 failed / 1 skipped；唯一失败 `test_live_vision` 的 `origin not allowed` 同样在父提交复现。
- **前端**：`npm run test:node` **158 passed**；`npm run build`（含 typecheck）通过。
- **静态/评测门**：`compileall`、`workflow_eval --self-check`、`dev_eval --self-check` 均通过（pass_rate 1.0，无回归）。

### 16.15 共享层时间失效 + 定期维护 + 健康面板（2026-10-31）

- **共享条目失效**：`promote.supersede_shared(old, new)` 把旧共享条目置 `superseded`，写 `superseded_by` / `superseded_at` / `valid_until`，并给新条目加 `supersedes` 边；旧正文保留审计，但从 `shared_items`、可召回列表和索引检索中排除。手动入口：`POST /api/memory/items/{id}/supersede`、CLI `invalidate --old --new`。
- **晋升后生效**：固化时把 active 共享条目一并交给模型；若新候选声明取代共享条目，先写入候选的 `pending_supersedes`，**只有候选通过回执 + 采纳/星级 + 泛化晋升门后**才真正让旧共享条目失效，避免未晋升候选提前改坏共享层。
- **定期维护**：`health.maintain(project, apply=False, retention_days=90)` 先 dry-run，`apply=True` 才执行：项目候选去重、共享 active 去重（跳过 pin）、把超过保留期的 `superseded/merged` 条目复制到 `archive/memory/` 后移出工作区，最后重建索引。CLI：`python -m agent_runtime.memory maintain [--apply]`；API：`POST /api/memory/maintain`（apply 需 `confirm=true`）。
- **健康面板**：`GET /api/memory/health` + 上下文面板底部「健康」按钮；展示共享 active/pin/失效、项目候选/晋升、重复项、缺向量、待归档失效、上下文预算与近 7 天固化；提供「预览维护 / 执行维护」。当前实测：共享 0 / 项目候选 46 / 晋升 0 / 重复 0 / 缺向量 5 / 待归档 0。
- **验证**：记忆后端套件 **117 passed**；前端 typecheck、build 通过；Edge 实测健康面板与「预览维护」按钮正常（截图 `artifacts/shots/memory-health-panel.png`）。
- **仍未做**：pin 的共享条目不做自动去重；GC 只处理失效/已合并条目，不自动删除 active 事实；维护目前是手动/CLI，未接定时调度。（定时调度已在 16.16 接上，其余不变。）

### 16.16 定时器与 Plan 长跑（2026-10-31）

- **内部定时器（记忆体检）**：`agent_runtime/maintenance.py` 起一条守护线程，按用户在设置页填的「内部记忆检查间隔（分钟，0 = 关闭）」周期性跑 `health.report` + `health.maintain(apply=True)`，结果写 `.docmind/memory/maintenance-last.json`，健康面板的 `scheduler` 区块直接读它。**先睡后跑**：进程重启不会立刻补一发维护。间隔持久化在 runtime + `STATE_FILE`（`memory_check_interval_minutes`），API 走既有的 `GET/POST /api/memory-settings`。
- **外部定时器（模型自己挂）**：`agent_runtime/timers.py` 按 `projects/<pid>/session/<sid>/timers.json` 落盘，工具 `timer_set` / `timer_list` / `timer_cancel`（域 `plan`）。到期提醒在**每次准备把消息发给模型之前**注入 `trail`（`agent.py` 的 ReAct 循环内，不是回合开头），所以「本回合起个后台任务、下一迭代就被提醒」这条主用法成立；同时上抛 `notice` 事件让人类看得见（前端 `useChatStream` 已有 notice 通道）。重复定时器**不会自动关**，提醒正文里直接给出 `timer_cancel(timer_id=...)`；一次性提醒触发即 `fired`。上限只数**活动**定时器（`MAX_ACTIVE=24`），终态行留最近 12 条，否则长会话挂满一次性提醒后会永久设不动新定时器。
- **Plan 长跑**：`agent_runtime/plan_runner.py` 后台线程连续驱动真 `Agent`，每轮用 `system_context` 重申不变量（推进任务书 / 不许停下等用户 / 未验证不许勾 / 取舍记账 / 不能自关）。**停止门是任务书**：`_closed()` 只认 `done` 与 `done_pending_merge`；全部划掉后转入 `reviewing`，按 `review_interval_minutes` 反复问四件事（审查过全部改动？真实测试跑完？确定没有遗漏？还有什么可改进？），发现新工作就 `dev_plan(append)` 回到执行。runner 走的是真 `Agent.run`，所以每一轮照常落进该会话的历史与固化管线——回开发台能看到长跑期间每个回合的工具明细与 diff（那些「【Plan 长跑 · 执行】」开头的 user 消息是 runner 发的，不是用户打的）。
- **只有用户能关**：模型侧没有 stop 工具；`POST /api/plan/stop` 要求 `confirm=true`，前端还要过一道二次确认。关闭面板 ≠ 停止 Plan（实测钉住）。Plan 运行期间普通 `/api/chat` 返回 409，避免用户消息和 runner 同踩一个会话。
- **只开放在开发台**：`/api/plan/**` 全部经 `_require_workbench_surface` 校验 `X-DocMind-Surface: workbench`，问答页拿不到这个头即 403；前端 Plan 面板也只挂在开发台对话台。
- **决策账本**：`agent_runtime/plan_log.py` 每会话一份 `plan-log.jsonl`（start / turn / review / stop / error 由 runner 自动记，含动作清单与任务书快照；`plan_decision` 工具记模型主动写的取舍），超 1 MB 保留最近 2000 行。面板「决策账本」区展示，供用户事后逐条审查。
- **验证**：`tests/test_timers.py` 6 passed（含**回合内**设定时器 → 同回合回灌）、`tests/test_maintenance.py` 3 passed、`tests/test_plan_runner.py` 5 passed（含 runner 真驱动一轮并记账、空任务书首轮要求建计划）、`tests/test_plan_api.py` 9 passed（surface 403 / 记忆 v2 门 / confirm 门 / 会话互斥 / 状态透传）。前端 `npm run typecheck` + `vite build` 通过；`node verify_plan_ui.mjs` 在一次性夹具（独立 `DOCMIND_MEMORY_DIR`、Plan 请求走 route 桩，不发真实模型请求）上 **12 项全绿**，截图 `artifacts/shots/plan-panel.png`、`artifacts/shots/plan-panel-running.png`。
- **仍未做**：会话空闲时到期的定时器不会自己唤起一轮（只在模型下一次准备发言时回灌）；Plan 的复盘间隔仍是全局每会话一个值，没有按任务差异化；runner 复用会话历史，长 Plan 的上下文压缩仍走既有 L5 路径，未单独调预算。

### 16.17 评测流量与真实记忆隔离（2026-10-31）

- **实测到的污染**：真实项目 prj-af6142eae52c（rag-agent）的 56 个记忆会话里 **55 个是 `golden-*` 评测跑**，只有 1 个真实 web 会话；55 个 episode 全部来自评测；46 条候选里 28 条挂在评测 episode 上、18 条来自旧库迁移回填；另外留下 12 条 pinned 的误判 error 钩子（`_is_failure` 修好之前记的）。也就是说「记忆在长东西」这件事几乎全是评测流量制造的，真实使用反而一条没留下 —— 晋升门永远等不到回执。
- **机制**：`Agent(memory_write_project=...)` 只改**写入** scope，**读**仍按被问项目。落点有两处，缺一不可：v2 是 `agent.py` 回合收尾唯一的写入处（`turns.record_turn` + `consolidate.schedule`）；v1 是同一回合的 `_memory.remember(scope, "episode", ...)` 与两处 `capture_preferences(scope, ...)`，改投新增的 `Agent.memory_write_scope`。只隔 v2 等于隔了半个 —— `memory_search` 把 v1/v2 两张表一起回灌进提示词，实测真实项目 v1 库里 136 条 episode 有 **54 条标题逐字命中黄金题库**。空串 = 与今天逐字节一致。读不能一起隔离的理由：改读等于改提示词构成，黄金题基线就不可比了。
- **默认值即防线**：`run_golden.py` 的 `EVAL_MEMORY_SCOPE = "eval-golden"`，`--memory-write-project` 可覆盖（传空串退回旧行为，做记忆回归时才用）；banner 会打印「记忆写入=eval-golden」。与既有的 `DOCMIND_GOLDEN_NO_LOCK` 同一个理由：评测不该改动任何长期状态。
- **存量搬运**：55 个 `golden-*` 会话目录整目录先复制到 `archive/2026-10-31-golden-sessions-pre-isolation/`（含 manifest.json），再把 episode 记录里的 `project` 字段改成 `eval-golden`、move 进 `projects/eval-golden/session/`。真实项目会话数 56 → 1。索引不受影响（`_PATTERNS` 只扫 `facts/`、`lessons/`，不扫 `session/*/episodes.jsonl`），99 items / 99 vectors 不变。v1 侧：54 条命中题库的 episode 行整行导出到 `archive/2026-10-31-golden-rows-pre-isolation/memories.json` 后再删，本项目 136 → 82 条；删前另存了整库副本。
- **保留的判断**：46 条候选**没有**跟着搬 —— 它们是这个代码库的真实事实（常量默认值、集合名、护栏语义），只是获取路径是评测。要一并搬走是 `projects/prj-af6142eae52c/{facts,lessons}/*.md` 里 `source_turn` 命中评测 episode 的那 28 条，一条脚本即可，代价是真实项目候选从 46 掉到 18。
- **仍留的小口子**：`_memory.capture_explicit_profile(question)` 不按 scope 键控（它写的是用户画像），所以理论上仍有一条评测题能往 L1 画像里学一条偏好；实测题库里没有「我叫…/请用中文…」这类自述句式，故未动。
- **验证**：`tests/test_eval_memory_isolation.py` 5 passed —— 隔离写入只落 eval 桶且真实项目连 session 目录都不建、**反向守卫**（不传参必须逐字保持旧行为）、读取仍跟被问项目（L3 任务书照样注入）、`run_golden.run_one` 的默认值本身就是隔离、v1 episode 行跟着走。两次变异各自把落点改回 `self.project_id` / `self.memory_scope`，对应用例立刻红（`0 != 1`、`'episode' not found in []`），改回即绿。

### 16.18 学习闭环第一次真正闭合（2026-10-31）

在此之前共享层一直是 0 —— 机制全绿但没人真的走过去。这次用隔离夹具项目 orchid-loop（`prj-7a57e11901e9`）跑通了一遍，通道是**真的** `POST /api/chat`（与工作台同一端点同一份表单），不是脚本自造的中间态。

- **真任务**：夹具的 SPEC.md 与实现有两处真实不一致 —— Known issue 声称 `parse("1, 2, 3")` 会抛 `ValueError`（其实 `int()` 容忍空白、已有测试通过），而 `compute(items, strict=True)` 的 `ERR_NEGATIVE_INPUT` 既没进 SPEC 也没测试覆盖，而 SPEC 自己写着「Every behavior change needs a test」。要求：核实、补齐、**最后一次改动之后**再跑 `python -m pytest tests -q`。
- **链路实测**：基线 6 passed → 改 SPEC + 补 2 条测试 → 终跑 **8 passed / exit 0** → 回执 `rc-20261031081954-df5e65 = test/pass` → 固化出候选 `mem-bd4d8a4f8c531e62`（正文含 strict 与非严格路径两条事实，并挂 2 条回执）→ 采纳 → **`shared.active=1 / pinned=1 / confidence=0.85 / receipts=3`**，项目侧 `by_status` 变成 `candidate 5 / promoted 1 / superseded 1`。
- **晋升门是"差一步"被证实的**：采纳前实跑 `promote.evaluate()`，未满足项只有一句「缺采纳/复用信号」；回执、重要度 0.7、泛化（正文一字未被削）、无冻结全过。也就是说门没写坏，之前只是从来没有真实信号。
- **对照（关键）**：晋升**之前**在另一个空项目 `prj-b6cd69a42c54` 开新窗口问「strict 负数的错误码 + 你从哪层知道」→ 注入侧 L2 **0 字符**，回答侧模型**拒绝编造**并自述「来源层 = 无（证据缺失）」。晋升**之后**同一问题同一项目 → 注入侧 L2 **252 字符**命中该条，回答侧给出正确错误码且自述「来源层是共享记忆（跨项目沉淀），本轮没有调用任何工具」。唯一变量就是那次采纳。
- **采纳落点**：`POST /api/memory/items/{id}/adopt` → 写 `review/pass · user_review` 回执 + `adopted=True` + `usage+1` → 立刻 `promote()`。面板按钮是「采纳即晋升」，成功文案「已采纳并 pin」（confidence 0.85 ≥ 0.8 且 importance 0.7 ≥ 0.7 才 pin）。
- **踩到的真问题**：第一次点击**没落到后端** —— 账本无 `memory.promote`、没有 `user_review` 回执、候选文件 mtime 未变。根因是 `/api/memory/items/{id}/adopt` 按**请求头里的项目**找候选（`_request_project_id()`），顶栏停在别的项目时同一 id 得到的是同一句 404「未找到待晋升候选」，红字极易被当成"点了没反应"。把顶栏切到候选所属项目后一次成功。
- **告警侧的可观测变化**：`shared_empty` 与 `no_promotion` 两条告警在项目晋升后**消失**，只剩 `consolidation_errors`（2026-10-30 一次 402 余额错误，非代码问题）。
- **善后**：为对照临时登记的 `prj-b6cd69a42c54` 已注销（`remove_project` 只注销不删盘），一次性服务已停、端口 8000 已释放，服务端当前项目已还原为 rag-agent；orchid-loop 保留在册，因为晋升条目的 `promoted_from` 指向它。

### 16.19 采纳失败要说清「在哪个项目里找的」（2026-10-31）

- **动机**：16.18 里第一次采纳白按一次 —— `/api/memory/items/{id}/adopt` 与 `/promote` 都按请求头里的项目取候选，顶栏停在别的项目时返回的是光秃秃一句「未找到待晋升候选」，和"点了没生效"在界面上完全同形。
- **改法**：`api._memory_project_hint(project, reasons)` 只给含「未找到」的原因补上出处（项目**名**，取不到名字时退回 pid），晋升门本身的原因（「缺采纳/复用信号」「重要度不足」…）**原样透出**，不许被后缀稀释 —— 用户要照那句话去补条件。
- **实测响应**：`{"detail":"未找到待晋升候选（当前项目：rag-agent；该候选可能属于别的项目，请先把顶栏切过去）"}`；`/promote` 同形。
- **验证**：`tests/test_memory_adopt_project_hint.py` 4 passed（adopt 带项目名、promote 带项目名、**反向守卫**门原因逐字不变、注册表读不动时至少报 pid）。变异验证：把 adopt 那行退回 `reasons[0]` 后两条立刻红（`'orchid-loop' not found in '未找到待晋升候选'`）。

### 16.20 注入上限改为「固定下限 + 随窗口缩放的天花板」（2026-10-31）

- **为什么不整体改成按比例**：压缩的历史是无界的，比例合理；注入的内容是有界的 —— 宪法不会因为换个大模型就该写得更长。实测两套口径的差别很直白：5% 预算在 8k 窗口模型上只给 230 tok，**连 L0 宪法一条（472 字符 ≈ 425 tok）都装不下**；在 1M 窗口上给 35,101 tok，而各层真实占用是 L0 79% / L1 67% / L3 36% / L2 32% / L4 **0%** —— 卡住注入的一直是内容量，不是预算。所以比例只能当**帽子**，不能当**分配**。
- **做法**：`injection.DISCRETIONARY_FLOORS = {L2:800, L3:400, L4:600}`（就是原来的固定值，即下限）与 `DISCRETIONARY_CAPS = {L2:3200, L3:1200, L4:2400}`；`discretionary_budgets(prompt_tokens)` 先把**整池** clamp 到 `[1800, 6800]`（`池 = 预算 × GROW_RATIO(0.02) × CHARS_PER_TOKEN(1.1)`，比例可 `DOCMIND_MEMORY_INJECT_RATIO` 覆盖），再按各层下限占比分余量 —— 保持 800:400:600 的相对比例不随窗口漂移。
- **L0 / L1 刻意不进这张表**：`layer_chars` 对它们直接 `ValueError`。这两层没有"这轮先不注入"的退路，换模型也不会变长。
- **实测生效值**：8k/16k 本地窗口 → 逐字等于今天的 800/400/600；当前 deepseek（预算 87,307）→ 853/426/640；1M 窗口 → 3022/1200/2266（L3 先顶到帽，余量**不挪给别层** —— 宁可少注入也不失控，所以有效总池 6488 < cap 合计 6800）。
- **接线**：`agent._build_messages` 三处按 `self._prompt_budget()` 传（用的是**本模型**的预算，会话级换模型会自动跟着变）；`context_panel.next_view` 用运行时模型估一份，并把同一个数字同时用于取数和**面板显示的 budget**，否则界面会显示一份假的。估不出预算时 `_current_prompt_tokens()` 返回 0 → 退回下限，绝不放大。
- **验证**：`tests/test_memory_inject_scaling.py` 7 passed —— 小窗口逐字等于今天（反向守卫）、大窗口增长但逐层 ≤ cap 且相对比例不漂、L0/L1 拒绝缩放、估不出预算退回下限、builder 真接到更大天花板（注入内容确实变多）、**Agent 确实把自己那个模型的 prompt 预算传给三层**、面板显示的预算与它真正用的同一份。变异验证：把 L3 那处接线删掉，接线守卫立刻红（`Items in the second set but not the first: 'L3'`）。

### 16.22 跨回合 prompt 前缀与命中率视图（2026-10-31）

- **先纠正一次假测量**：我一度报"命中率 0%"，是因为按 API 原始字段名（`cached_tokens`/`cache_read_input_tokens`）去扫轨迹，而落库键叫 **`cache_read_tokens`**（agent_trace.py:229）。搜错键 = 探针自己造了个零。真实基线：**6150 回合、123,833,918 输入 token、109,500,023 命中读 = 按 token 88.42%；但只有 794 个回合命中 = 按回合 12.91%；累计省 ¥96.61**（deepseek 94.0%/76.0%，custom 62.3%/74.2%，ollama 与 mock 恒 0 —— 本地与假模型没有 provider 缓存）。
- **打断前缀的东西**：`【当前时间】` 带秒级时间戳，原先作为 `messages[2]` 插在渠道提示词与宪法之后。`now` 每回合只取一次，所以**回合内**逐步重发的前缀是稳的（88% 就是这么来的）；被打断的是**跨回合** —— 下一回合历史只在尾部追加，本可整段复用，却从第 3 条起全废。
- **试过的做法与它的下场（已撤销）**：把整块（值 + 天气/票价政策文字）移到历史之后、当前问题之前。**全量回归当场拒绝**：`tests/test_fit_budget_head_prune.py::test_prunes_only_history_pairs_never_orphan_assistant_first` 红（`'system' not found in ('user', 'assistant')`，单独跑 0.13s 即复现）。`_fit_budget` 的成对删除建立在一条不变量上 —— **`_head_fixed_end` 与当前问题之间只允许 user/assistant 成对历史** —— 插一条 system 进去就把它打破了。对照实验同时证实这是代码问题而非数据问题：干净 worktree 空数据 28 红，加上这批改动、同样空数据 149 红，两次跳过数都是 52，数据变量被排除在外。
- **结论**：想拿这份缓存收益，要改的是**裁剪器**（跳过非历史消息，或把易变块正式纳入固定区），不是挪消息。已留一条守卫钉住这条不变量：`test_memory_v2_p0.test_time_block_stays_in_the_fixed_head` —— 断言时间块位于 `_head_fixed_end` 之前，且可裁区只剩成对历史。
- **度量（已落地）**：`agent_trace.summary()` 早就算好了 token 与 `cache_saved_cny`，缺的是**按回合**那条。新增 `cache_hit_turns / cache_hit_rate / cache_hit_turn_share`（顶层 + 每个 provider），设置页「用量与费用」加一行同时报两条率 —— 只看 token 率会把"跨回合几乎没复用"这件事完全藏住。
- **验证**：撤销后针对性复跑 **176 passed / 0 failed**（`test_fit_budget_head_prune`、天气恢复、票价澄清、指代门、回执门、注入缩放、memory v2 全套 88 条含新增守卫）。前端 typecheck + build 通过。
