# 工具说明的可见性与双份文案

> 立项日期：**2026-10-08**。起因是想按「大类 → 小类」两级目录省上下文
> （参考 Claude Code 的可观察行为，不用泄露源码）。
> **动手前实测发现原判断错了两次**，本篇记录实测结果与错在哪。

---

## 1. 三个实测事实

全部来自 `python -c` 读 `tools.TOOLS` 与 `agent.SYSTEM_PROMPT`，可复算。

### 1.1 ⚠️ 更正：本节原标题「20 个工具不可见」是错的

原写法把「在 progressive 索引里没露脸」当成「完全不可见」。**错了。**
progressive 模式（`DOCMIND_TOOL_EXPANSION=progressive`，**默认开启**）本来就
只给 14 个核心工具完整用法，其余进 `【扩展工具索引】` 一行概要 —— 那**就是**披露。

真实情况是**两条独立事实**：

| | 实测 |
|---|---|
| `SYSTEM_PROMPT`（progressive 后）16442 字符，核心 14 个 + 索引 83 个 | 正常 |
| `_SYSTEM_PROMPT_FULL`（手工维护的全量段）里只有 **94 个**工具块 | **22 个从未写进去** |

那 22 个既不在核心、也不在索引 —— 因为**索引是从全量段派生的**，全量段里就没有它们。
`dev_patch`（原子打 unified diff）在整份系统提示里出现 **0 次**。

### 1.2 工具说明是**独立手写**的，与 `ToolSpec.description` 内容不同

| 工具 | 提示里手写版 | `ToolSpec.description` |
|---|---|---|
| `dev_page_action` | 62 字符 | **1046** 字符 |
| `dev_vue` | 短版 | **1167** 字符 |

可见工具里 **81 个的提示版明显短于 desc 版**；提示版中位数 **60** 字符，
desc 版中位数 **163** 字符。**手写版是刻意精简的，不是 desc 的复制。**

### 1.3 `_tools_section` 对 progressive 后的提示返回空 —— **设计如此，不是 bug**

`Agent.__init__` 里那条「动态 join 全部 description」也**从不执行**：
116 个工具的 `applications` 全部是 `frozenset({"developer"})`，受限应用分支
（`application_id != "developer"`）当前没有任何工具，是死路径。

---

## 2. 我错在哪（**三次**，都是「先下结论再核实」）

| # | 我先说的 | 实测 | 错因 |
|---|---|---|---|
| 1 | 「116 个工具的 description 共 ≈7454 tokens，**每次会话都要付**」 | 对 developer **不成立**。实际付的是 `SYSTEM_PROMPT` 的 4110 tokens | 只量了 `ToolSpec.description` 的总量，**没查它是否真被送进上下文** |
| 2 | 「两套文案重复，换目录能省 99%」 | **不重复**，手写版比 desc 短（60 vs 163 中位数）。换目录是**降级** | 没逐条比对两个来源的内容 |
| 3 | 「111 个工具」 | 实际 **116 个**。我数 `tools.TOOLS` 时漏了 `delegate` / `orchestrate` / `tool_search` / `dev_use_skill` —— 后两个不在 `tools.TOOLS` 里而由 `agent.py` 注入 | 只用了一个来源，没交叉核对 |

**第 3 次的连锁后果最严重**：正因为少算了 5 个，我没发现注册表里**已经有 `tool_search`**，
差点再造一个 `tool_catalog` 重复造轮子。它连注释都写着「参考 Claude Code 的同名设计」。

**教训**：算「覆盖率」时要**交叉核对两个来源**。只看一个来源，
你会漏掉另一个来源注入的东西 —— 而那正是最容易重复造轮子的地方。

---

## 2b. 已修：22 个工具从未被披露

`Agent._assemble_system_prompt` 之前插入 `_backfill_tool_section()`：
从 `ToolSpec` 派生出「注册表里有、提示段里没有」的工具块，补进「可用工具」段。

**刻意从注册表派生，而不是手写 22 条** —— 手写的下一次新增工具又会漏，
而漏了不会报错（这正是 §3.1 那类静默失效）。

成本：progressive 会把非核心工具压成一行概要（`_short_desc`，limit=42），
所以最终提示 **+1333 字符（≈+333 tokens）**，`dev_patch` 等 22 个进入索引。

**`full` 模式不受影响**（不出现 `【扩展工具索引】`），有守卫钉住。

### 2c. 已修：披露只做了一半（`_tool_blocks` 漏了补全）

补完 `SYSTEM_PROMPT` 之后立刻实测 `tool_search`，发现**取不回来**：

| 查询 | 补全修复前返回 |
|---|---|
| `web_weather` | **「没有匹配「web_weather」的工具」** |
| `dev_patch` | `dev_diagnostics` 的用法 |
| `dev_mcp_add` | `dev_mcp_search` 的用法 |

根因：`_tool_blocks()`（`tool_search` 的数据源）读的是**未补全**的
`_SYSTEM_PROMPT_FULL`。所以那 22 个**在索引里露了面，却取不回完整用法** ——
模型知道有 `web_weather`，一调却得到「没有匹配」。

**这是「披露」这件事的两个环节**：①进提示 ②能被检索到。我只做了 ①。

修法：`_tool_blocks()` 也走 `_backfill_tool_section()`。修后覆盖 115 个
（缺的 `tool_search` 是核心工具，注入在 `_SYSTEM_PROMPT_FULL` 构造之后，
不需要检索自己—— 有守卫说明这个边界）。**三例现已全部正确。**

顺带发现**检索本身**也有缺陷（精确名可能返回别的工具），
但那是 `tool_search` 自己的族，另立 `docs/known-defect-tool-search-substring-mismatch.md`。

---

## 3. 真正存在的问题

### 3.1 ~~20 个工具模型完全看不到~~ **已修**（§2b）

`dev_patch`（原子打 diff）、`create_external_file`（受控越界写）、`memory_forget`、
`web_weather` 等 22 个曾**在整份系统提示里出现 0 次**。现由
`_backfill_tool_section()` 从注册表补齐，有守卫钉住。

### 3.2 两套文案会漂移（**未修**，需要真跑验证）

今天改 `tools.py` 里 `preview_project` 的端口措辞时，`agent.py` 必须**同步改**，
否则教模型的是旧文案。**今天已经这么漂过一次**（`5cbe94b`）。

但**不能靠合并解决** —— §1.2 证明手写版是**刻意精简**的（60 vs 163 中位数），
拿 `ToolSpec.description` 替换它是**降级**。

真正的修法是**划清分工**：

* `ToolSpec.description` —— **是什么**（给 `tool_search` / 目录 / 任何按需披露用）
* `SYSTEM_PROMPT` 手写段 —— **怎么用**（何时必须调、有什么约束、与其他工具的边界）

现在两者混在同一个 `- name(args):` 形态里，所以「同一事实两个来源」。
拆开后漂移不再是缺陷 —— 它们本就不该是同一句话。

**这一项要真跑验证**（改了模型输入），单独一笔。

### 3.3 96 条手写说明无法按需加载

它们必须常驻（核心 14 个 + 索引 83 行），但模型每个问题都用不到大部分。
`tool_search` 已提供按需取回，**这条路已经通了**，缺的是让模型更早知道
「哪一类工具存在」—— 那正是两级目录想解决的，但接进提示需要先做 §3.2。

---

## 4. 两级分类：已建成，但**暂不接进提示**

`agent_runtime/tool_domains.py`（声明式 111 个工具 → 14 大类 / 37 小类）
与 `agent_runtime/tool_catalog.py`（目录渲染 + `tool_catalog` 下钻工具）
**已落地并有守卫**，但**没有替换 `SYSTEM_PROMPT`**。

理由基于 §1.2：目录里展开的是 `ToolSpec.description`（中位数 163 字符），
而提示里现在是手写精简版（**中位数 60 字符**）。**直接换是降级** ——
模型会从「60 字精准说明」变成「163 字泛泛描述」。

**要接进提示，前置条件是先把 96 条手写说明迁到 `ToolSpec.description`。**
那是改变模型输入的改动，必须真跑验证，且是独立一笔。

---

## 5. 计划

| 步 | 内容 | 风险 |
|---|---|---|
| ✅ 1 | 实测并立档（本篇） | 零 |
| ✅ 2 | 守卫：每个工具必须被披露 + 目录与 `ToolSpec` 一致 | 零 |
| ✅ 2b | **修 22 个工具从未披露**（`_backfill_tool_section`） | 低：只增加披露，不改写既有文案 |
| ⬜ 3 | 划清「是什么 / 怎么用」分工，再考虑两级目录 | **高**：改变模型输入，须真跑验证 |

**`tool_search` 已存在且正常**（`DOCMIND_TOOL_EXPANSION=progressive` 默认开启，
14 个核心 + 扩展索引 + 按需取回，注释里明写参考 Claude Code 同名设计）。
**所以本篇不是「要建按需披露」，而是「已有的按需披露漏了 22 个」。**

第 3 步**不与别的族混做**。特别地，以下是**另一族**（路由召回不足），
不得混入：实测 `ContextRouter.route()` 对「改一下 README 里的端口」只给
`{general, orchestration}`，连 `read_file`/`apply_edit` 都没有 ——
因为 `_CODE` 正则只认「函数/类」那类词。

---

## 6. 分类维度为什么不按 `capability`

`capability` 是**权限维度**（`Agent._is_write_tool` 查它），不是功能维度。
实测 `READ_LOCAL` 那 50 个里同时有 `dev_vue`（把 .vue 挂进真浏览器）
和 `dev_probe`（探活）—— 完全不同的域。

两个维度正交，各自管一件事：

* `capability` —— **能不能做**（护栏查这个）
* `domain` / `facet` —— **做什么、怎么找**（模型查这个）

**改动 `capability` 去迎合分类好用，会动摇护栏的权威依据。** 不做。