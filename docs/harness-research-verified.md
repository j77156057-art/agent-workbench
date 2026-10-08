# 三个 harness 的一手来源核实记录

> 核实日期：**2026-10-08**。全部结论来自**项目自身的一手文件**（README / docs /
> 包的 README），不用二手转述、不用搜索摘要。
> **Claude Code 泄露源码不作为来源**（既定纪律，本次遵守）。
>
> 这篇替代 `docs/next-steps.md` 第 3 节的旧版 —— 旧版是昨晚凭搜索摘要写的，
> 有两处不准确，已在第 5 节列出更正。

---

## 0. 一句话结论

三个 harness 在「agent 能不能越权」这同一个问题上给了**三种不同立场**，
**而我们目前做的那一种恰好是被最多方否掉的那一种**。

| harness | 立场 | 一句话 |
|---|---|---|
| **Pi** | **弃权** | 不做权限系统。transcript 审查**不是**安全边界，推给容器 |
| **Hermes** | **双轨** | 做审批（LLM 辅助风险判定 + fail-closed 超时 + 非交互默认拒）**且**做容器隔离；并明说写守卫只是纵深防御、不是边界 |
| **opencode codemode** | **语法化** | 让越权**在语法上不可表达**；授权明确列为 `Non-Goals`，归宿主 |
| **DocMind（现状）** | **文本推断** | 从用户那句话猜授权 —— **正是 Pi 明说「不构成安全边界」的那一类** |

---

## 1. Pi —— `earendil-works/pi`

来源：`README.md`、`packages/coding-agent/docs/security.md`

### 1.1 确实没有权限系统（核实）

README 原文：

> Pi does not include a built-in permission system for restricting filesystem,
> process, network, or credential access. By default, it runs with the permissions
> of the user and process that launched it.

`security.md` 原文：

> Pi can read, change, and execute files with the permissions of the account that
> started it, and **it does not ask for approval before every tool call**.

### 1.2 **关键发现：transcript 审查被明确否认为安全边界**

`security.md` 原文（逐字）：

> **Watching the transcript, using project trust, and reviewing changes do not
> create a security boundary.**

这一条**直接命中我们**。核实了我们自己的实现（不是凭印象）：

| 我们的机制 | 代码位置 | Pi 的分类 | 算不算边界 |
|---|---|---|---|
| `_write_intent(question)` 从消息文本判授权 | `agent.py:3172` | watching the transcript | ❌ **不是** |
| `_ambiguous_write_request()` 从消息文本判能力问句 | `agent.py:2048` | watching the transcript | ❌ **不是** |
| `_lease_blocked()` 查租约表 | `agent.py:3269` | 结构化授权 | ✅ 是 |
| `_intent_for_delegated()` 查 `tool_allowlist` + 父租约 | `agent.py:4457` | 结构化授权 | ✅ 是 |
| `loopback_url()` 回环校验 | `dev_server.py:53` | 限制可达资源 | ✅ 接近 Pi 的隔离思路 |

**这不否定已提交的代码。** 今天 17 笔里，租约 / 委派 / 结构化那部分是站得住的；
词表判定那部分修的是**过拦与误判**，方向是安全的。但它给出了一条硬约束：

> **别再往词表里加词了。** 每加一个词就多一处「靠散文维持」的地方，而 Pi 的判断是
> 那一整类都不构成边界。

这与我们在 `docs/next-steps.md` 第 1 节独立得出的结论一致，现在有外部一手来源背书。

### 1.3 Pi 给的替代方向

`security.md` 的三层运行方式，越往下越强：

| 怎么跑 | 还剩下什么没被保护 |
|---|---|
| 直接跑（当前用户权限） | 该用户碰不到的东西 |
| 整个跑在容器/虚拟机/sandbox 里 | 你没暴露给它的宿主文件与进程 |
| 隔离环境外 + 只有内置工具在隔离区内 | 通过那些工具做的事伤不到宿主（较窄） |

> Safety comes from limiting the files, credentials, processes, and network
> services Pi can access and affect ... **Watching the transcript, using project
> trust, and reviewing changes do not create a security boundary.**

**注意 Pi 连「project trust」一起否掉了** —— 那是个启动期加载哪些项目资源的机制，
与我们「角色提示词决定权限」的错误（`fa81aa3` 修的那族）同类：
`security.md` 说「Project trust does not limit what tool calls can access or affect」。

### 1.4 供应链硬化（核实，逐条）

README 的 "Supply-chain hardening" 一节：

- `We treat npm dependency changes as reviewed code changes.`
- 直接依赖**钉到精确版本**（`save-exact=true`）
- `.npmrc` 设 **`min-release-age=2`**，避免解析到当天发布的依赖
- `package-lock.json` 是「the dependency ground truth」
- pre-commit 阻止误提交 lockfile，除非 `PI_ALLOW_LOCKFILE_CHANGE=1`
- 生命周期脚本有**显式白名单**，新依赖若带 lifecycle 脚本则检查失败直到人工审
- 安装器从 `install-lock/` 装，把**传递依赖**也钉住
- 定时 workflow 跑 `npm audit --omit=dev` + `npm audit signatures`

### 1.5 遥测 conformance tests（核实）

`packages/telemetry/README.md` —— 核实**不是**「给遥测写了测试」，而是
**一套给适配器用的、与 runner 无关的一致性套件**：

> `@earendil-works/pi-telemetry/testing` exports a **runner-independent conformance
> suite** modeled as grouped cases. A fixture supplies a fresh context and converts
> its backend's finished spans into normalized `RecordedTelemetrySpan` snapshots.

套件覆盖：同步单次准入、返回值与 rejection 的**同一性**、自动与显式状态、
属性合并、事件顺序、settle 后调用惰性、嵌套与并发的父子关系、
**不可读的遥测负载失败必须被吞掉**。

还有一条对我们重要的约束：

> Recording it **must not change** whether the account load runs, succeeds, fails,
> or is persisted.

即**插桩不得改变业务结果**。

---

## 2. Hermes —— `NousResearch/hermes-agent`

来源：`README.md`、`docs/user-guide/security`

### 2.1 它**有**审批（核实，旧记录漏了这条）

README 的文档索引里 Security 一行写着 "Command approval, DM pairing, container
isolation" —— 核实正文确认：安全模型**八层**，第二层就是
`Dangerous command approval — human-in-the-loop for destructive operations`。

三种模式（`approvals.mode`）：

| 模式 | 行为 |
|---|---|
| `smart`（默认） | **用一个辅助 LLM 评估风险**。低风险自动放行（仅对该条），真危险的自动拒，**不确定的上浮为人工询问** |
| `manual` | 危险命令一律问用户 |
| `off` | 全关（等于 `--yolo`） |

**`smart` 模式的三值判定与我们的三值判定是同一个形状** —— 它也把「拿不准」单独
分出来上浮人工。我们花了 17 笔才做出 `_WRITE_INTENT_UNCERTAIN`，它是默认行为。

### 2.2 非交互上下文默认 `deny`（核实）

> `cron_mode: deny` / `single_query_mode: deny` / `unattended_mode: deny`

原文解释：

> These surfaces have **no human who can answer/approve**, so instead of blocking
> for the full approval timeout, **deny blocks the command instantly**.

**这正是我们的 `_intent_for_delegated()` 要求父持有租约**（`fa81aa3`）。独立得出了
同一条规则 —— 没有人能在场批准时，不要等，直接拒。

### 2.3 超时 fail-closed（核实）

> `timeout: 300` ... If no response is given within the timeout, the command is
> **denied by default (fail-closed)**.

并且：过期的询问**不可重开**，且

> A timeout is **not** counted as a denial, so asking again is never penalized.

我们的租约 TTL 夹在 300/900s，**同一个量级**（独立撞上，值得注意）。

### 2.4 硬地板在 `--yolo` 之下（核实）

`Hardline Blocklist (Always-On Floor)`：`rm -rf /`、fork bomb、`mkfs`、`dd if=/dev/zero`、
把不可信 URL 管给 rootfs 的 shell —— 这些**在审批层之前**就拦，
`--yolo` / `mode: off` / cron 的 headless approve / 用户点「always」**都绕不过**，
且**没有 override flag**。

另有一条我们会喜欢的**解析失败即拒**：

> The floor also **fails closed** on a command whose shell quoting cannot be parsed.

### 2.5 **写守卫被明说不是硬边界**（核实）

> ### Defense-in-depth, not a hard boundary
>
> Write guards apply to `write_file` and `patch` only ... **The terminal tool runs as
> the same OS user and can still `cat` or overwrite denied paths via shell commands.**
> The denylist reduces accidental damage and gives models a clear stop signal; **it
> does not sandbox a hostile or compromised agent.**

**与 Pi 那句话是同一个判断，只是更具体。** 写路径 denylist + 可选 safe-root 沙箱
（`HERMES_WRITE_SAFE_ROOT`）是**减少事故**，不是**防敌手**。

顺带一条它比我们细的：`~/.ssh/config` 走**审批**而不是硬拦（改 host 别名是例行动作，
但它能携带 `ProxyCommand`/`Match exec`，所以写入**从不静默**），而私钥与
`authorized_keys` 硬拦。

### 2.6 容器后端**跳过**危险命令检查（核实）

> When running in docker, singularity, modal, daytona, or vercel_sandbox backends,
> **dangerous command checks are skipped because the container itself is the security boundary.**

七种后端的隔离强度表里，`local` = `None — runs on host`，其余六个容器/远程后端
才有隔离。

### 2.7 它也撞上了我们同一个语言学难题（核实，最有价值的一条）

Context File Injection Protection 一节末尾，原文：

> The translation-and-execution check **does not connect translation and execution
> verbs across unrelated comma-separated role prose**. ... **These patterns are
> heuristics, not semantic intent detection.**

**「不要跨无关的逗号并列散文连接两个动词」** —— 这正是 `docs/write-intent-corpus.md`
A/B/D 三节反复记录的那个问题（否定只取消它自己管的那个动词、谓语动词缺口、
`不必X只要X`）。一个独立 harness 在**完全不同的功能域**上写下了同一条边界。

### 2.8 从历史审批里挖 allowlist 建议（核实）

`hermes approvals suggest`：扫会话库里**真正执行过**的危险命令，聚合成模式提议。
安全规则三条：默认只读、**破坏类永不提议**（`rm -rf build/` 批准 100 次也永不产出
`rm` 条目）、凭据在打印与 JSON 里都掩码且**掩码过的文本永不作为 allowlist 模式**。

### 2.9 其他核实项

- **FTS5 会话检索**：`FTS5 session search with LLM summarization for cross-session recall` ✅
- **zero-context-cost turn**：`Write Python scripts that call tools via RPC, collapsing
  multi-step pipelines into zero-context-cost turns` ✅
- **七种终端后端**：local / Docker / SSH / Singularity / Modal / Daytona / Vercel Sandbox ✅
- **interrupt-and-redirect** ✅
- SSRF 防护：拦 RFC1918 / loopback / link-local / CGNAT / **云元数据地址**
  （含 `169.254.169.254` 与 `metadata.google.internal`），**DNS 失败按拦处理**
  （fail-closed），**重定向每一跳都重新校验**
- 环境变量过滤：`execute_code` 按名字含 `KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PASSWD|AUTH` 拦
- MCP 子进程只透传 `PATH, HOME, USER, LANG, LC_ALL, TERM, SHELL, TMPDIR` + `XDG_*`

---

## 3. opencode —— `anomalyco/opencode`

来源：`packages/codemode/README.md`、`packages/http-recorder/README.md`

### 3.1 codemode：**确认尚未发布**（核实）

> The package is currently **private to this workspace**.

安装方式是 `"@opencode-ai/codemode": "workspace:*"`。**外部装不了。**
旧记录写「README 明写尚未发布」——方向对，措辞不够准。

### 3.2 codemode 的价值是「越权在语法上不可表达」（核实）

> CodeMode lets a model write a small JavaScript program that can call **only the
> tools supplied by the host**, ... **without receiving ambient filesystem,
> process, network, module, or application authority.**

> **A program cannot gain authority through prose or generated code.** It can only
> exercise authority already present in the supplied tools.
> **Do not expose a broad tool and expect the prompt to restrict it.**

那句 "Do not expose a broad tool and expect the prompt to restrict it" 是对
「用提示词/意图推断去限制一个宽工具」的直接否定 —— **与 Pi 那句同向**。

机制上它靠**不用 `eval`** 的受限 JS 子集解释器 + 三个限额
（`timeoutMs` / `maxToolCalls` / `maxOutputBytes`）。

### 3.3 **codemode 不提供授权**（核实，旧记录漏了这条）

`Non-Goals` 一节原文包含：

> - **Generic permission prompts or approval workflows.**
> - **Application authorization or product policy.**
> - Durable pause/resume, replay, or storage adapters.
> - Exactly-once external side effects.

`Authority Boundary` 一节：

> **The host owns:** Authentication and authorization. Tool selection and immutable
> scope. Credentials and network clients. Persistence, idempotency, **approval**, and
> durable side effects. Logging and redaction policy.

**所以 codemode 不是我们租约/审批的替代品。** 它是一个**可借鉴的边界技术**
（把限制下沉到执行层，而不是靠提示词），授权仍要宿主自建。

### 3.4 **三个执行限额默认全是「无」**（核实，这条容易漏）

> No limit has a default, on purpose: execution budgets are **host policy, not
> library policy**.

并警告：

> A host with neither should set `maxOutputBytes`, or **oversized results silently
> flood model context**.

**「静默淹没上下文」** —— 正是我们处理过多次的「失败时没有声音」那一族。

### 3.5 工具目录有 token 预算与检索（核实）

`Discovery` 一节：目录默认 **2000 estimated tokens**（`characters / 4`），
按命名空间**轮转**挑选以保证公平，并**明说目录是否完整**
（`COMPLETE list` vs `PARTIAL - N of M shown`）；配 `tools.$codemode.search`
做确定性字段加权检索（路径精确 20 / 路径子串 8 / 描述子串 4 / 可搜文本 2）。

**这一条对我们直接相关，但我们已经有等价物了。** 核实后发现
`agent.py:690-698` 早就实现了同一思路，`DOCMIND_TOOL_EXPANSION=progressive`
**默认开启**：

* `CORE_TOOL_NAMES` 14 个核心工具给完整用法
* 其余进 `【扩展工具索引】`，只给名称 + 42 字概要（`_short_desc` 的 `limit=42`）
* 需要时用 `tool_search(工具名)` 取回完整用法
* 注释里明写「**参考 Claude Code 的同名设计**」

所以**不需要按 codemode 再建一套目录**。实测 `SYSTEM_PROMPT` 16442 字符
（≈4110 tokens）：核心 14 + 索引 83。

**但那条机制漏了 22 个工具** —— 它们从未被写进 `_SYSTEM_PROMPT_FULL`，
既不在核心也不在索引，模型**完全不知道它们存在**（`dev_patch` 出现 0 次）。
已修，见 `docs/known-defect-tool-visibility-and-dual-text.md`。

**反方向也不适用**：把两级目录接进提示是**降级** ——
`SYSTEM_PROMPT` 里 96 个工具的说明是**独立手写**的（长度中位数 60 字符），
`ToolSpec.description` 中位数 163 字符。手写版是刻意精简的，不是 desc 的复制。

### 3.6 核实：三家里**没有一家**按问题相关性挑工具（2026-10-08 追加）

这是查 opencode 的 `Discovery` 时顺带发现的，比上面那条更根本：

| harness | 怎么决定暴露什么 |
|---|---|
| **Pi** | **不涉及工具选择** —— 它的安全模型是「进程边界」而非「工具边界」。`security.md` 通篇讲文件/凭据/进程/网络的可达性与三种隔离运行方式；**没有任何按问题挑工具的机制** |
| **Hermes** | **用户/平台显式 toolsets**（`hermes tools`、`--toolsets "web,terminal"`），另有平台 preset 与动态 `mcp-<server>` 工具集。**零自动召回** |
| **opencode** | **预算截断 + 轮转公平 + 检索**，**无相关性打分** |

opencode 原文：

> **Every tool namespace is always listed** with its tool count regardless of
> budget. Selection is **round-robin** across namespaces for fairness.

我们此前的 `ContextRouter` 关键词判定（`_CODE` / `_WEB`）是**三家里唯一的异类**，
也是唯一失效的那个（动词型请求「跑一下测试」一个都不命中）。
详见 `docs/known-defect-context-router-recall.md`。

> ⚠ **更正**：本节早前写「Pi 有 4 个工具（read/write/edit/bash）」——
> **核实不到**。`security.md` 与 `README.md` 都没写工具数量，那个数字来自第三方
> 站点（搜索摘要）。已删掉 —— **不拿二手来源的数字当事实**。上表改为只陈述
> 一手来源里确实写了的机制。

### 3.6 `http-recorder`：**Public beta，且真的能用**（核实）

> **Public beta.** The API depends on Effect 4 beta and may change.
> Install: `bun add -d @opencode-ai/http-recorder@beta`

机制：本地首次跑**打真实 API 并录成 cassette**，之后确定性重放。

> When **`CI=true`, missing cassettes fail instead of recording.**

三条设计值得抄：

1. **没有覆盖模式，只有删除后重录**：
   > There is intentionally **no public overwrite mode**. Deletion makes the set of
   > recordings being refreshed **visible and reviewable**.
2. **落盘前扫凭据，不安全就拒写**：
   > Before writing, the recorder scans the complete cassette for common credential
   > formats ... **Unsafe cassettes fail without replacing an existing recording.**
3. **明确 redaction 不能替代人看**：
   > Redaction is defense in depth, **not a substitute for review**.

**限制（README 自陈）**：响应在录制与重放时都被缓冲，所以**不适合断言流式时序、
取消或背压**；WebSocket V1 不复现终端 close code 与传输失败。

**对我们的用处**：治 `docs/known-defect-two-flaky-network-tests.md` 那两条随机红。
把三后端搜索的真实响应录一次、提交 cassette、之后重放 ——
**注意它只治上游 HTTP 依赖，治不了「探针脱离 pytest 看见缓存」那类**。

---

## 4. 对我们的方向（已按核实结果修正）

### 4.1 站得住的

| 方向 | 依据 | 我们已有的 |
|---|---|---|
| **权威来自结构，不来自散文** | codemode `Authority Boundary` + Hermes 非交互 `deny` + Pi 否掉 transcript 审查 | `_lease_blocked`、`_intent_for_delegated`、`ToolSpec.capability` 111 个全声明 |
| **三值判定 + 不确定上浮** | Hermes `smart` 模式默认行为 | `_WRITE_INTENT_UNCERTAIN`（今天做出） |
| **非交互上下文默认拒** | Hermes `cron/single_query/unattended_mode: deny` | `_intent_for_delegated` 要求父租约 |
| **超时 fail-closed** | Hermes `timeout: 300` 默认拒绝 | 租约 TTL 300/900s |
| **不给标记就不下结论** | codemode「只调宿主给的工具」 | `owns_port()` 无 markers 返回「无法判归属」 |
| **限制资源而非推断意图** | Pi 三层运行方式 + Hermes safe-root | `code_root`、`loopback_url` |

### 4.2 建议放弃的

| 方向 | 为什么放弃 |
|---|---|
| **继续往写意图词表加词** | Pi 明说那一整类不是边界；Hermes 明说「heuristics, not semantic intent detection」。今天 17 笔的实测也指向同一处 |
| **零上下文成本那半边（Python 脚本调工具）** | 需要 `exec` + 受限 builtins，比 acorn 静态解析明显更不安全；且 `Non-Goals` 把授权划给宿主，我们仍要自建 |
| **录放批准状态** | 见 `docs/write-intent-corpus.md` J1 |

### 4.3 值得单独拿出来做的（按性价比）

| 候选 | 依据 | 代价 |
|---|---|---|
| **工具目录预算 + 明确告知不完整 + 检索** | codemode `Discovery` | **受限应用 agent** 的提示里 111 条 description 全部进（`Agent.__init__` 动态 join）；顶层 agent 是写死 22 条 |
| **给适配器边界加 conformance suite** | Pi telemetry conformance | 我们有适配器边界（`probe`/`owns_port`、preview adapter、MCP connector），但**得先说清「要解决什么」** |
| **cassette 治两条随机红** | http-recorder | 我们是 Python 栈，Effect 实现搬不了，**但模式可搬** |
| **写路径 denylist + safe-root** | Hermes `HERMES_WRITE_SAFE_ROOT` | 我们现在只有 `code_root` |
| **解析失败即拒** | Hermes quote-parsing fail-closed | 与我们无关（我们不解析 shell） |

### 4.4 **明确不做**

- **`strictPort: true`**（今天已决，见 `docs/known-defect-dev-server-port-ownership.md` 第 8.5 节）
- **Claude Code 泄露源码**（纪律）
- **`owns_port` 的生产接线**在契约定下来之前不做 —— 它需要把 `dev_serve` 的 job 记录
  带进 `preview_project` 入参，而**契约该怎么定取决于 4.3 里前两条的方向选择**

---

## 5. 对旧记录的更正

| 旧记录（`next-steps.md` 第 3 节） | 核实后 |
|---|---|
| 「`telemetry` 带 conformance tests」 | ✅ 但**不是**给遥测写的测试，是**给适配器用的 runner-independent 一致性套件** |
| Hermes 只记了 FTS5 + zero-context-cost | ❌ **漏了它有完整审批系统**，且那才是对我们最有参考价值的部分 |
| 「codemode 的价值是越权在语法上不可表达」 | ✅ 但**漏了 `Non-Goals` 明确排除授权** —— 它不能替代我们的租约 |
| 「README 明写尚未发布」 | ✅ 更准：`currently private to this workspace` |
| 「`http-recorder` 能治『测试在说谎』」 | ⚠ **过宽**。它治上游 HTTP 依赖，治不了探针脱离 pytest 看见缓存那类 |

**还有一处是我自己当天写错的**：`docs/write-intent-corpus.md` J1 原先把
「已证不适用」归因到 http-recorder 名下 —— 那是两件不同的事（录**批准状态** vs 录
**上游 HTTP**）。已在该节更正。