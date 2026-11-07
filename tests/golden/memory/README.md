# 记忆 / 压缩黄金回归套件

这套用例只做一件事：**任何人改动摘要分节化（pinned 固定事实 + rolling 近期进展）或周边记忆逻辑时，立刻知道有没有破坏既有性质。**

- 默认全量离线，确定性可复现：固定响应桩 LLM，不联网、不依赖模型额度。
- 每个用例一个独立临时目录（临时 `.docmind_sessions` / sqlite / memory），跑完即删；绝不写仓库里的真实记忆、`data/state.db` 或真实会话文件。
- 真实模型用例单独放在 `cases/live/`，默认 `SKIP`；只有 `pytest --live`（或环境变量 `DOCMIND_GOLDEN_LIVE=1`）才跑，且不参与快照比对。
- 24 条离线用例已接入 pytest（`tests/test_memory_golden.py`），CI 每次都会跑；`runner.py` 只用于 `--live` 与单例调试。
- 不改产品代码。本套件只通过公开/现有函数调用产品逻辑；若某条性质必须改产品代码才能测，应在评审里停下来讨论，而不是顺手改。

## 目录

```
tests/golden/memory/
  README.md            本文件
  runner.py            运行器：--all / --case / --update-golden / --live
  cases/*.json         声明式用例：种子对话、预算、桩响应、断言
  cases/live/*.json    live 组（默认跳过）
  expected/*.json      黄金快照（逐字期望观测结果）
```

pytest 入口在 `tests/test_memory_golden.py`：24 条离线用例逐条参数化，
并额外守住「用例数固定 24」与「cases/expected 一一对应」两道闸。

## 每条用例为什么存在

| 用例 | 守住的性质（一句话） |
| --- | --- |
| `hard_constraints_survive` | 早期硬约束必须穿过 20 轮压缩后仍逐字可见，不能被 rolling 淘汰。 |
| `pinned_not_in_compact_prompt` | 压缩 LLM 的提示词只能收到 rolling，绝不能再次收到 pinned 正文。 |
| `pinned_absent_all_prompts` | pinned 正文与「【固定事实…】」标记不得出现在任何一份压缩/合并提示词里（全量计数，不是抽检）。 |
| `pinned_prefix_checker_teeth` | 对真实 pinned 做改字节/截断/重排三种变异，同一套「旧前缀逐字节保留 + 只追加尾部」判定必须全部判红。 |
| `pinned_tombstone` | 用户删除固定事实后跑完整提取，该事实不得回流 pinned，审计里必须有 `pinned_readd_suppressed`。 |
| `pinned_model_no_delete` | 模型路径只能追加：更短摘要、删除接口、空列表覆盖三种攻击都必须被忽略。 |
| `pinned_monotonic` | 90 轮压测下旧 pinned 逐字节保留在最前、增长只追加尾部，条目数在非 user 路径不得减少。 |
| `pinned_bytes_stable` | 连续 90 轮压缩后 pinned 逐字节不变（字节相等 + sha256 相等）。 |
| `pinned_idle_bytes_identical` | 没有新硬约束的轮次，pinned 必须逐字节完全相同（旧前缀逐字节保留的对照组）。 |
| `rolling_evict` | rolling 超限时只淘汰最旧段，最新段保留，并留下可定位的省略标记。 |
| `pinned_full_evict` | pinned 满时出现「固定事实前缀已满」，最旧非约束条目移入 rolling 且内容不丢。 |
| `hard_absorbed` | pinned 未满时，压缩中途新增的硬约束直接进入固定事实。 |
| `cap_extreme` | 分节标记唯一，极端上限 256 下不腰斩，总长永不超上限。 |
| `det_cap_guard` | DET 回退路径下 cap 系列从未收到含 pinned 的串，裁剪/淘汰标记也不会切在 pinned 内部。 |
| `idempotent` | `maybe_compact` 重复调用幂等：第二次不二次压缩，文本与轮次都不变。 |
| `legacy_migration` | 无标记旧摘要逐字截头进 pinned，标注 `pinned_from_legacy_head`，两次结果一致。 |
| `write_inject` | 写回磁盘的摘要与真正注入 prompt 的片段逐字一致（比对实际 system 消息）。 |
| `budget_matrix` | 同一条用例在 770 字小预算与 2117 字默认预算下都成立，断言相同。 |
| `ordinary_fact_not_pinned` | 普通事实（不含必须/不得/禁止等词）不得被固定进 pinned，防止过度固定。 |
| `negative_checkers` | 故意构造超长、标记被切开、pinned 被淘汰三种坏结果，检查器必须判红。 |
| `taskbook_evict` | 任务书超过 80 条时不丢新条目，只淘汰最旧 `done`。 |
| `profile_panel` | 画像注入 ≤1800 字，且面板 profile 分母与后端常量同源，不写死数字。 |
| `agent_memory_isolation` | A agent 写入的记录不出现在 B agent 的检索结果。 |
| `subagent_sessions` | 子 agent 会话写真实会话、默认列表不展示，`include_subagents=True` 才可见。 |
| `live_smoke`（live） | 真实模型压缩冒烟；默认跳过，仅用于人工验证真实链路。 |

## 运行

**CI / 日常回归走 pytest**（24 条离线用例逐条独立，失败即定位到 case id）：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_memory_golden.py -q

# 只跑某条
.venv\Scripts\python.exe -m pytest tests/test_memory_golden.py -q -k rolling_evict
```

`runner.py` 保留给两类场景：真实模型 live 组，以及单例调试（带表头、耗时与第一处差异）：

```powershell
# 单例调试（与 pytest 共用同一套执行与快照比对）
.venv\Scripts\python.exe tests/golden/memory/runner.py --case rolling_evict

# 额外跑 live 组（会调用真实模型；也可用 pytest --live 或 DOCMIND_GOLDEN_LIVE=1）
.venv\Scripts\python.exe tests/golden/memory/runner.py --all --live
```

- 表头固定为：用例名 / 结果 / 耗时。
- 失败时会给出**第一处差异的上下文**（前后各 60 字符），而不是只报 mismatch。
- 全量离线应在 60 秒内完成；超时会被记为失败并指出慢用例。
- 退出码：`0` 全过；`1` 有失败；`2` 配置或快照缺失（包括 `--update-golden` 未加 `--yes`）。

## 加新用例的三步

1. **写 JSON**：在 `cases/` 下新建 `<name>.json`，至少包含 `why`、`scenario`、种子数据与 `assertions`。需要新场景时在 `runner.py` 的 `_SCENARIOS` 里加一个纯执行函数，**所有用例数据仍放 JSON，不写死在 Python**。
2. **生成快照**：先运行 `--case <name>`，确认断言通过；再运行 `--update-golden --yes` 生成 `expected/<name>.json`。
3. **评审 diff**：逐字检查快照里 pinned/rolling/提示词/明文结论是否真的符合预期；确认不是把错误输出合法化。

## 快照更新的纪律

**`--update-golden` 是危险操作。** 任何一次快照更新都必须在 PR / 提交说明里写清原因：是产品行为有意改变、断言补强，还是修掉了错误输出。不允许把失败直接刷成通过；运行器在未加 `--yes` 时会拒绝覆盖，并且在断言失败时不会写快照。

## 确定性设计

- `StubLLM` 按预置队列返回固定文本，并记录真实送入的提示词原文，用于「包含/不包含某字串」断言。
- 不使用真实 tokenizer：桩没有 `count_tokens`，走产品里的确定性估算函数。
- 子进程隔离：父进程为每个用例创建临时根目录，设置 `DOCMIND_STATE_ROOT` / `DOCMIND_MEMORY_DIR` / `DOCMIND_MEMORY_DB` / `DOCMIND_SESSIONS_DIR` / `DOCMIND_TURN_TRACE_DIR`，并对每例加超时；用例结束整目录删除。
- 禁用增强记忆相关的环境开关，避免真实向量库/学习模块干扰。
- 快照只记录文本、计数、布尔值，不记录时间戳或绝对路径，因此可跨机器复现。

## 断言类型（JSON）

`contains` / `not_contains` / `is_true` / `is_false` / `equals` / `equals_field` / `int_le` / `int_ge` / `int_eq`（支持 `limit_field` 从观测里取上限）/ `starts_with` / `regex`。字段路径支持点号，例如 `budget_4000.limit`。
