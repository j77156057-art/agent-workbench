# 记忆系统 v2 · P0 规格

> 状态：记忆系统 v2 P0 + P1 完成（含学习闭环与真实迁移，2026-10-31；真实设置已开启）。
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
多用户/团队隔离（需要先定认证与共享边界）、跨设备同步（需要存储与冲突合并协议）、图数据库/PagerRank。语义召回、边编辑、学习闭环与真实迁移已落地。

### 16.6 应用状态与模型
- `.docmind_state.json` 已开启 `memory_v2: true`；默认模型现为 **opencode / deepseek-v4.1-flash**，Key 优先读环境变量 `OC_KEY`（其次 `OPENCODE_API_KEY`，最后全局密钥库）；`/api/config` 实测返回 `opencode/deepseek-v4.1-flash` + `has_key=true`，3 题冒烟 3/3 通过。
- qwen-plus 仍可用：`output_token_budget` 新增 qwen 厂商上限 32768，修复 `max_tokens` 400；5 回合与 53 题长任务均验证通过，可随时在设置里切回。
- 注意：opencode 的长任务曾在前 21 题后长时间无新结果（已停止）；短会话已验证正常。若长任务再次停滞，优先检查 opencode 会话/限流，或临时切回 qwen-plus。


### 16.7 学习闭环与真实迁移（2026-10-31）

- **候选采纳**：面板「可召回但未注入」里的项目候选新增「采纳」。采纳会生成 `review` 回执（`cmd=user_review`）、置 `adopted=true`、`usage+1`、清除冻结，并立即尝试晋升；成功后共享条目与索引同步 upsert。
- **星级归因**：4–5 星会对该 task 的候选生成 `review` 回执并采纳/晋升；1–2 星置 `promotion_frozen=true` 且 `evaluate` 拒绝晋升（不删除）；3 星中性。没有 task 的星级只统计，不做归因（防止误伤所有候选）。
- **复用计数**：`memory_search` 命中 v2 条目与采纳都会递增 `usage/last_used`，`pin_score` 的复用权重开始生效；索引 `touch` 同步。
- **索引一致性**：共享条目编辑、晋升、采纳、状态/pin 变化立即 `upsert_item`；`rebuild` 现在比较标题/状态/置信/重要度/pin/usage/时间，不再只看 body_hash。
- **真实迁移已执行**：旧库只读导出 **471** 条归档、41 条事实 + 13 条经验教训（active 低置信、不 pin）、1 条偏好、354 条 checkpoint、23 条 experience、13 条退休；第二次运行 `skipped`；索引重建 **82 条，verified=true**。迁移条目可被检索、可在面板 pin/编辑。
- **验证**：后端 **283 passed**、前端 158 node、build/typecheck 通过；Edge 实测候选「采纳」后 1→0、提示「已采纳并 pin」、无控制台错误。
