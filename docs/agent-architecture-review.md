# Agent 工作台架构评估与重构设计

> 评估对象：`D:\WorkBuddy\rag-agent`（Agent 工作台）现有 Agent 运行时
> 评估框架：H = (E, T, C, S, L, V) + P（六层功能 + 架构范式）
> 评估日期：2026-09-19
> 评级图例：🟢 健康 / 🟡 有债但可控 / 🔴 结构性缺陷（随规模恶化）

---

## 1. 需求规格（从代码反推）

| 维度 | 内容 | 状态 |
|---|---|---|
| 目标与用途 | 本地单人、面向游戏/Mod 工程的 AI 研发脚手架；RAG + ReAct Agent + 分区工作台 + 引擎嵌入 | 已确认（项目记忆） |
| 成功标准 | 1065 项单测全绿；场景画布 54 项、UI 23 项、引擎真机 79 项 | 已确认（项目记忆） |
| 任务结构 | 混合：代码问答（检索为主）/ 受控改码（写+验证）/ 多角色协作（delegate+orchestrate）/ 声明式流程（flows） | 从代码反推，**默认假设** |
| 数据边界 | 单项目 code_root（ContextVar 绑定）+ 分区子目录 + 白名单越界目录 | 已确认（regions/config） |
| 硬约束 | 离线可用（mock provider + local embedding）；本机 12GB 显存；Windows 桌面单进程 | 已确认 |
| 用户场景 | 单人桌面 GUI（pywebview + Vue），非多用户、非服务端 | 已确认 |
| 边界 | 不做多用户/鉴权/数据库；不改只读契约；前端 AI 改写不自动落盘 | 已确认（项目铁律） |
| 技术栈约束 | Python 3.13 + FastAPI + PyInstaller + Vue 3；Ollama/DashScope/DeepSeek 多 provider | 已确认 |

---

## 2. 架构范式 P（现状）

| 子维度 | 现状决策 | 证据 | 评级 |
|---|---|---|---|
| 扩展方式 | **嵌入式**（工具可加，执行结构不可换） | 工具加在 `tools.py:3507 TOOLS` 字典；E 层是唯一硬编码 `while True`（`agent.py:1144`） | 🟡 |
| 配置方式 | **命令式**（env var 驱动） | `agent.py:37-64` 十余个 `DOCMIND_*`；`flows.py` 是声明式孤岛，Agent 不消费 | 🔴 |
| 部署拓扑 | **单机**（子代理同进程递归） | `_run_child`（`agent.py:1742`）直接 `Agent(...)`，`depth` 上限 2 | 🟢（符合场景） |
| 编排模式 | **中心化**（主 Agent 调度子代理） | `_delegate` / `_orchestrate_tool` 均由父 Agent 驱动 | 🟢（符合场景） |

**核心判断**：P 的两个单机向子维度（部署/编排）选得对，不需改。真正的问题是**扩展方式与配置方式**——它们把 E 和 T 锁死成了"只能加工具、不能改结构、参数靠环境变量"。

---

## 3. 六层现状评估

### 3.1 E 执行循环 — 🔴

**证据**：`agent.py:1144-1684`，单个 `while True` 约 540 行。

耦合在这一处的九件事：deadline 超时(1147)、步数上限(1155)、并行批次派发(1166)、token 预算裁剪(1194)、四类护栏（写意图 1273 / 白名单 1400 / 联网开关 1409 / 重复检测）、失败计数与强制收尾(1471-1502)、反思与截断纠偏 Nudge(1503-1660)、verbatim 短路(1553)、trace/成本/上下文上报(1457/1447)。

**问题**：

1. **五套终止条件并存，V 层缺席**：deadline(1147)、`iterations > MAX+缓冲`(1155)、`forced_finals`(1267)、模型自给 Final(1581)、证据兜底(1122)。其中 `turn.verified = True`（1549）写入后**全项目无人读取**——自验证结果不参与终止判定。这是 checklist 里"让模型给自己判卷 = 循环论证"的典型形态。
2. **策略选择器/策略库缺失**：`plan_mode` 只是追加一条 system 消息（713-718），`orchestrate` 是工具入口而非 E 层策略。H 框架要求的"策略库"在此等于 1 个硬编码循环。
3. **魔鬼代言人反例**：`_fit_budget`（885-907）每轮循环都调 `_prompt_tokens` → 一次真实 `count_tokens`（ollama 是网络往返）。裁剪循环上限 40 次 ⇒ **单回合最多 40 次额外网络往返**用于算 token。当前靠 `_CTX_EMIT_INTERVAL` 限频的只有 UI 上报，裁剪本身没有限频。
4. **顺序派发无异常隔离**：`obs = TOOLS[action_name]["func"](action_arg)`（1445）外层无 try/except；而并行路径 `_run_batch._one`（1702-1720）有。**一致性问题：为什么并发路径有异常隔离、顺序路径没有？** 后果：任一工具抛未捕获异常 → 炸穿生成器 → 整轮失败（子代理路径 1802 反而有兜底）。

**评级理由**：不是"代码写得差"，而是**结构不允许替换执行策略**。加一种新执行模式（比如"先规划后执行"或"生成-过滤"）必须改这个 540 行函数。

### 3.2 T 工具注册 — 🔴

**证据**：`tools.py:3507 TOOLS = {name: {"description": str, "func": callable}}`，**57 个工具**；`tool_schemas()`（3689）导出 OpenAI 风格 schema 供原生通道用。

**问题**：

1. **契约是 `str -> str`**，无输入 schema、无输出类型。后果是字符串解析泄漏进执行循环——`agent.py` 里 300+ 行参数修补：`_normalize_tool_arg`(441)、`_ARG_PREFIX_TOOLS`(384)、`_NO_ARG_TOOLS`(359)、`_OPTIONAL_ARG_TOOLS`(364)、`_EMPTY_ARG_OBS`(365)、`_RE_GREP_KEYED_INLINE`(406)、`_RE_GREP_PATH_LINE`(411)；`tools.py` 里 `_parse_keyed`(2474) 被各工具重复手写多行 `key: value` 解析。
2. **失败判定靠观察文本子串匹配**：`_is_failure(obs)`(508) + `_FAILURE_MARKERS`(291)。新增工具的失败文案若未登记进该元组，死循环护栏就失效——项目记忆里已为此立了铁律（`_FAILURE_MARKERS` 必须覆盖全部失败文案）。**这是最脆的一环**：一个字符串常量表成了执行循环安全性的单点依赖。改用结构化 `error_kind` 可根治。
3. **文档双份、靠人肉同步**：`SYSTEM_PROMPT` 手写工具文档（95-217，13441 字符 ≈ 8–9k token）与 `TOOLS[*]["description"]`（合计 7350 字符）是两份独立文本。实测两者**当前一致**（提示词列 53 个、注册表 57 个，差 4 个越界工具因分组写法未被正则命中，非漂移）。但没有任何机制保证持续同步——项目铁律「新增工具三步：tools.py → SYSTEM_PROMPT → api.py」正是这个耦合被制度化的证据。**风险是未来的必然漂移，不是已发生的 bug。**
4. **全量注入**：顶层 `_effective_tool_names()`(674) 永远返回全部工具（白名单只用于子代理）。13441 字符系统提示 + 57 个工具说明 = 每轮固定税，对 32k 窗口本地模型约占 **25–30%**（估算，见 §13 待实测）。而 `_fit_budget` 裁剪时**永不精简工具文档**——无论问题多简单都全额付税。
5. **特权派发路径**：`delegate` / `orchestrate` 在 TOOLS 里是占位实现（538/544），真实逻辑靠 `if action_name == "delegate"` 特判（1439）。**一致性质问：为什么这两个工具不走统一派发？**

### 3.3 C 上下文管理 — 🟡

**做得好的**：三层预算设计（prompt_budget 751 / history window 761 / fit_budget 885）合理；`_history_window` 用"整段一次计数 + 按字符占比估算"把 O(N) 次网络往返压到 1 次（767-799），这是有意识的性能设计；压缩触发口径与 UI 展示口径刻意同源（832-838）。

**问题**：head 里堆 4–6 条 system 消息（通用规则 / 项目规则 / 技能目录 / 计划模式 / 联网开关 / 历史摘要），加上全量工具文档，形成**不可裁剪的固定税**。裁剪只丢 trail 往返与历史问答对（887-889），工具文档永不动。

### 3.4 S 状态存储 — 🔴

**至少 6 个真相源**：会话 `sessions.py` JSON / trace `agent_trace.py` JSONL / 分区变更集 `dev_changesets.jsonl` / 分区配置 `regions.json` / 项目状态 `.docmind_state.json` / 项目注册表 `projects.py`。

**问题**：

1. **trace 不可回放**：`agent_trace` 只记元数据，`error` 被脱敏成 `{error_kind, chars}`（隐私设计，合理）。但副作用是**无法从 trace 重建回合** ⇒ V 层做不了真回归，只能靠 `golden/` 的静态题集。
2. **写治理不一致**：分区写有审批 + changeset + 回滚（`regions.py:975`）；会话写（`sessions.save`）和普通文件写无治理。**一致性质问：为什么分区写可回滚、普通写不可？**
3. **并发写**：`sessions._write` 无 WAL/锁；`_run_batch` 用线程池并发执行只读工具（有 `_REGION_LOCKS`，1493），但写工具被 `_NO_PARALLEL_TOOLS`(56) 排除——这点处理是对的。

### 3.5 L 生命周期钩子 — 🟡（结构错位）

**证据**：`hooks.py` 提供 pre/post tool + pre/post turn 四组挂载点（113-172），热插拔目录；调用点仅 4 处（`agent.py:992/1079/1421/1446`）。

**问题：双门并存，且错配**

| | 可插拔门（hooks） | 内联门（工具函数体内） |
|---|---|---|
| 承载内容 | 几乎为空（用户自定义） | approval TTL、越区拦截、路径沙箱、200KB 上限、.py 语法校验、`run_command` 黑名单(2404) |
| 可配置 | 是 | 否 |
| 可绕过 | 是（不写 hook 就不拦） | 否 |

→ 真正的安检门是**不可配置的**（改一条拦截规则要动工具函数），可配置的门是**空的**。这是 L 层最典型的反模式。checklist 要求"高危拦截要纵深、不能单点"——现状是单点且不可配。

### 3.6 V 评估接口 — 🔴

**证据**：`self_verify`(`agent.py:73`)= py_compile + 单测 + typecheck，写工具成功后自动触发(1536-1549)，结果回填模型，但**不 gate 终止**；`agent_eval.py`(230 行) 是独立 CLI 打分器（must_include / regex / must_call / no_error），未接进运行时；`golden/` 存静态题集。

**问题**：V 是"离线人工跑的脚本"，不是运行时闭环层。`turn.verified` 写了没人读；`agent_eval` 的判定结果不回流到 E 的终止条件，也不进 CI 门（需确认，见 §13）。checklist 的"评测反馈闭环 / badcase 回流 / 回归门禁"三项均缺失。

---

## 4. 重构目标与分期

> 原则：**按依赖顺序分期，每期独立可交付、独立可回滚**。不追求一次重写。

### P0（低风险高收益，建议先做）：T 层契约化

**目标**：消灭 `str -> str`，工具自带 schema；失败判定结构化；文档自动生成。

**可编码规格**：

```python
# tools/spec.py
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Any

class Capability(str, Enum):
    READ_LOCAL = "read_local"        # 读 code_root
    READ_EXTERNAL = "read_external"  # 读白名单外目录
    WRITE_LOCAL = "write_local"
    WRITE_EXTERNAL = "write_external"
    EXEC = "exec"                    # python_exec / run_command
    NETWORK = "network"              # web_* / dev_http_request
    ADMIN = "admin"                  # commit / rollback / apply_regions

class SideEffect(str, Enum):
    PURE = "pure"                    # 只读、可并发、可缓存
    IDEMPOTENT_WRITE = "idempotent_write"
    MUTATING = "mutating"            # 有副作用，需治理
    IRREVERSIBLE = "irreversible"    # 需审批

@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str = "string"             # string|integer|boolean|object|array
    required: bool = True
    description: str = ""
    multiline: bool = False          # True: 解析时整段抓取到下一个键之前
    default: Any = None

@dataclass(frozen=True)
class ToolSpec:
    name: str
    summary: str                     # ≤40 字，进目录（常驻注入）
    detail: str                      # 完整说明（按需注入）
    params: tuple[ParamSpec, ...]
    capability: Capability
    side_effect: SideEffect
    parallel_safe: bool
    group: str = "general"           # 按需注入分组
    verbatim: bool = False           # 结果即最终答案（calculate/gen_video_prompt）
    func: Callable[[dict[str, Any]], "ToolResult"] = None

@dataclass
class ToolResult:
    ok: bool
    text: str = ""                   # 给模型的 Observation
    data: Any = None                 # 结构化载荷，供 V / 前端消费
    error_kind: str = ""             # 失败分类（替代 _is_failure 字符串匹配）
    artifacts: dict = field(default_factory=dict)  # {"written": [rel_path], "cost": {...}}
```

**配套改造**：

1. `TOOLS: dict[str, ToolSpec]`；保留 `str` 入参的旧工具函数，用适配层 `legacy_adapter(fn) -> Callable[[dict], ToolResult]` 包装，**57 个工具不必一次性重写**。
2. **失败判定结构化**：`ToolResult.error_kind` 取代 `_is_failure(obs)`。`_FAILURE_MARKERS` 保留为 legacy 兜底，新工具一律用 `error_kind`。这是根治"死循环护栏单点依赖字符串表"的关键。
3. **提示词自动渲染**：`render_tool_prompt(names, mode="summary"|"detail")` 从 ToolSpec 生成；`SYSTEM_PROMPT` 缩小为"格式 + 通用准则"，工具段落由渲染器产出。**双份文档问题自动消失**。
4. **按需注入**：按 `group` 常驻 summary（约 57×40 字 ≈ 2.3k 字符），命中后由模型用 `dev_use_skill` 式机制取 detail。固定税从 ~13.4k 字符降到目标 ≤4k 字符。
5. **顺序派发补异常隔离**：`_run` 中工具调用包 try/except，异常转成 `ToolResult(ok=False, error_kind="exception")`——与并行路径对齐。

**验收**：`tests/test_tool_spec.py` 覆盖 schema 校验、渲染、legacy 适配；现有 1065 项测试不回退；实测单轮 prompt 字符数下降 ≥50%。

### P1（中等）：E 层策略化

**目标**：把 540 行循环拆成可替换的执行策略，终止条件收敛到一处，V 接入终止判定。

```python
# agent/loop.py
@dataclass
class StopDecision:
    stop: bool
    kind: str = ""      # final|forced|budget|deadline|max_steps|verified_failed
    text: str = ""
    outcome: str = ""   # 写进 trace 的结局标记

class Guard(Protocol):
    def check(self, ctx: "TurnContext", call: "ToolCall") -> "GuardVerdict": ...

@dataclass
class GuardVerdict:
    allow: bool
    reason: str = ""
    rewrite: dict | None = None       # 改写参数（替代 _normalize_tool_arg 的部分职责）
    requires_approval: bool = False

class Policy(Protocol):
    """执行策略：react / plan_then_act / orchestrate"""
    def next_step(self, ctx: "TurnContext") -> Step: ...
    def should_stop(self, ctx: "TurnContext") -> StopDecision: ...
```

**落地**：

- 抽出 `Guards`：写意图护栏、白名单、联网开关、重复检测、能力审批——每个独立类、独立可测，取代散落在主循环里的 if。
- 抽出 `Terminator`：**唯一**终止判定入口，消费 `deadline / budget / max_steps / policy / verify` 五路信号。当前 5 套终止条件全部迁入。
- `Terminator` 接入 V：`self_verify` 未通过且已无可修步数 → `StopDecision(kind="verified_failed")`，而不是静默放行。
- `_fit_budget` 加限频/缓存：同 trail 长度只算一次，避免单回合 40 次网络往返。

**验收**：`tests/test_terminator.py` 表驱动覆盖全部终止分支；`tests/test_guards.py` 每护栏独立用例。

### P2（较大）：S 事件溯源 + L 唯一安检门

**目标**：所有副作用走统一的 Effect 通道；trace 可回放；拦截规则可配置。

```python
@dataclass
class Effect:
    kind: str            # file_write|file_delete|region_commit|shell|http
    target: str
    before: str | None = None      # 原内容/原状态，用于回滚
    after: str | None = None
    capability: Capability = Capability.WRITE_LOCAL
    inverse: Callable[[], bool] | None = None

@dataclass
class Event:
    seq: int
    turn_id: str
    kind: str            # llm_call|tool_call|guard_block|approval|verify|final
    ts: float
    payload: dict        # 元数据；敏感字段按 flows._classify_error 同源脱敏
    cost: dict           # {"in": int, "out": int, "cny": float}
```

**落地**：

- 所有写工具改为"产出 Effect"而非"直接落盘"；`EffectRunner` 统一执行，执行前**必经 hooks**（唯一安检门）。工具函数体内的内联护栏迁移为 `Capability` 声明 + Guard 规则 → 拦截规则从代码变成配置。
- trace 升级为事件流（保持脱敏策略不变），支撑回放 ⇒ V 层可做真回归。
- 普通写也进 changeset（与分区写对齐），消灭 §3.4 的治理不一致。

### P3：V 在线闭环

- `agent_eval.py` 判定结果回流：`golden/` 题集进 CI 门；badcase 自动入集。
- 独立读路径（CQRS）：评估不经过生成路径，直接读 Event 流。
- `turn.verified` 成为发布门。

---

## 5. 目标模块结构（增量式，非推翻重来）

```
agent-workbench/
├── agent.py                 # 保留：Agent 门面（对外 API 不变）
├── agent/
│   ├── spec.py              # P0: ToolSpec / ParamSpec / ToolResult / 枚举
│   ├── registry.py          # P0: TOOLS -> ToolSpec 注册 + 校验 + legacy 适配
│   ├── render.py            # P0: 提示词渲染（summary/detail 两档）
│   ├── guards.py            # P1: Guard 协议 + 各护栏实现
│   ├── terminator.py        # P1: 唯一终止判定
│   ├── policies/
│   │   ├── react.py         # P1: 现有循环迁入
│   │   ├── plan_act.py      # P1: plan_mode 正式化
│   │   └── orchestrate.py   # P1: 任务图策略
│   ├── loop.py              # P1: 通用循环骨架（消费 Policy + Guards + Terminator）
│   └── effects.py           # P2: Effect / EffectRunner
├── tools.py                 # 保留：57 个工具实现，逐步 spec 化
└── agent_trace.py           # P2: 升级为事件流（脱敏策略不变）
```

---

## 6. 关键接口签名

```python
def register_tool(spec: ToolSpec) -> None:
    """注册工具；同名重复注册抛 ValueError（当前 dict 静默覆盖，是事故源）。"""

def render_tool_prompt(names: list[str], mode: str = "summary") -> str:
    """mode: 'summary' 只出 name+summary；'detail' 出完整参数说明。"""

def dispatch(ctx: TurnContext, call: ToolCall) -> ToolResult:
    """统一派发：Guard 链 -> 执行 -> post hook。异常一律转 ToolResult(ok=False)。"""

def should_stop(ctx: TurnContext) -> StopDecision:
    """唯一终止判定入口。"""

def apply_effect(eff: Effect, ctx: TurnContext) -> tuple[bool, str]:
    """P2: 执行副作用；失败返回 (False, reason)，成功登记逆操作供回滚。"""
```

---

## 7. 配置格式（P2：拦截规则外置）

```json
{
  "version": 1,
  "capability_rules": [
    {
      "capability": "WRITE_EXTERNAL",
      "requires_approval": true,
      "allowlist_env": "DOCMIND_EXTERNAL_DIRS",
      "max_bytes": 204800
    },
    {
      "capability": "ADMIN",
      "requires_approval": true,
      "approval_ttl_minutes": 30
    },
    {
      "capability": "EXEC",
      "requires_approval": false,
      "blocked_patterns": ["rm -rf", "format", "shutdown"]
    }
  ],
  "tool_groups": {
    "core":   ["search_code", "read_file", "grep", "list_dir"],
    "write":  ["apply_edit", "create_file", "dev_region_edit"],
    "region": ["dev_list_regions", "dev_region_read", "dev_commit"],
    "web":    ["web_search", "web_fetch", "web_research"]
  },
  "inject": { "always": ["core"], "on_demand": ["write", "region", "web"] }
}
```

---

## 8. 层间交叉检查

| 关系 | 现状 | 重构后 |
|---|---|---|
| E ↔ V | V 结果不参与终止（`verified` 无人读） | `Terminator` 消费 verify 信号 |
| E ↔ C | `_fit_budget` 每轮网络计数，无限频 | 缓存 + 限频；工具文档可裁剪 |
| T ↔ L | 护栏内联在工具里，hooks 空转 | 护栏由 `Capability` 声明 + Guard 链统一执行 |
| S ↔ V | trace 不可回放 ⇒ 无法真回归 | 事件流可回放，V 读独立路径 |
| T ↔ C | 57 工具全量注入，固定税不可裁剪 | 分组按需注入 |
| E ↔ T | `delegate`/`orchestrate` 特权派发 | 统一 `dispatch` |

---

## 9. 决策表

| 层 | 现状决策 | 目标决策 | 依据 | 状态 |
|---|---|---|---|---|
| E | 单循环硬编码 | Policy + Terminator 可替换 | H 框架策略库；新增执行模式不改循环 | 默认假设 |
| T | `str->str` 无 schema | ToolSpec + ToolResult | checklist：类型完整；根治字符串解析泄漏 | 默认假设 |
| C | 全量注入，固定税 | 分组按需 + 可裁剪 | 32k 窗口下 25–30% 固定税 | 默认假设 |
| S | 6 真相源，不可回放 | 事件流 + 统一 changeset | event-sourcing 模式；V 需可回放 | 默认假设 |
| L | 双门（可配的空 / 不可配的实） | 唯一 Guard 链 + 配置化规则 | capability-security 模式 | 默认假设 |
| V | 离线脚本 | 在线闭环 + CI 门 | checklist：回归门禁 | 默认假设 |
| P·扩展 | 嵌入式 | 插件化（仅 E/T 两层） | 其余子维度保持现状 | 默认假设 |

---

## 10. 待拍板问卷（请逐项回复，或说"按你的建议"）

| # | 问题 | 为什么问 | 建议（默认假设，可推翻） |
|---|---|---|---|
| Q1 | 重构范围做到哪一期？ | 决定工作量与回归风险 | 先做 **P0**（T 层契约化），P1 视 P0 效果再定；P2/P3 暂缓 |
| Q2 | 是否接受"57 个工具逐步迁移"（legacy 适配层共存期）？ | 一次性重写风险极高 | 接受，适配层共存；新工具强制 spec 化 |
| Q3 | 提示词改为自动生成后，现有手写 SYSTEM_PROMPT 中大量"行为准则"（如"禁止重复检索""枚举问题怎么答"）如何处理？ | 这部分不是工具文档，是推理纪律，不能丢 | 保留为独立 `REASONING_RULES` 常量，与工具渲染结果拼接 |
| Q4 | 是否要求 P0 期间保持 1065 项测试全绿且**不改任何测试**？ | 决定能否动 `_is_failure` 等被测试依赖的内部函数 | 保持全绿；`_is_failure` 保留为 legacy 兜底，新增路径并行 |
| Q5 | 并发写入者（另一 AI 高频提交）存在，重构是否限定在不与其交叠的文件？ | 项目铁律 0 | 是：P0 新增 `agent/` 包 + `tools/spec.py`，不动 `api.py`/`tools.py` 主体；交叠文件改动前先 `git status` |

---

## 11. 遗留问题与未知区（诚实标注）

1. **固定税比例未实测**：13441 字符 → token 数是估算（按中文 1 字 ≈ 0.6–1 token，得 8–9k）。需按实际 provider tokenizer 精算后再定 P0 的注入策略阈值。**占位方案**：先用字符数口径（≤4000 字符）作为渲染目标，实测后校准。
2. **`agent_eval.py` 是否已进 CI 门**：未在仓库中发现 CI 配置，未能确认。若未接入，P3 的"回归门禁"需先补 CI 基建。
3. **P2 回滚语义**：分区写有 git 可回滚；普通文件写（`apply_edit`）的 Effect 回滚需在内存中保存 `before`，进程崩溃即失效。需决定是落盘 before 快照（占空间）还是接受"会话内可回滚"。
4. **工具分片注入对弱模型的影响**：按需注入依赖模型主动索取 detail，弱模型可能不会索取 → 召回下降。需 A/B 实测；**若下降明显，退回"全量 summary + 首轮后按命中补 detail"的折中**。
5. **框架未覆盖**：本项目"引擎嵌入 + 场景画布 + ComfyUI 生成"这类 GUI/图形侧能力，H 框架的六层没有对应维度（它们更像 resource adapter 而非 tool）。本次设计未纳入，建议单列一层 `R`（Resource/渲染资源适配器）或归入 T 的 capability 扩展——**需用户判断**。

---

## 12. 本次未做的事

- 未做知识库自进化检索（本次为已有架构评估，非新框架调研，无需扩展知识库）。
- 未改动任何代码，仅新增本评估文档。
- 未运行测试套件（评估为只读调研）。
