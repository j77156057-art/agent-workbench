# `ContextRouter` 召回不足导致关键工具被收窄掉（**已修：撤除相关性收窄**）

> 立档：**2026-10-08**。状态：**已修** —— 撤除按关键词决定工具组的机制，
> 详见 §11。
> 与 `docs/known-defect-tool-visibility-and-dual-text.md` 是**不同族**：
> 那篇是「工具从未被披露」，本篇是「披露了但按需收窄把它收走了」。

## 1. 一句话

`ContextRouter` 的召回正则只认**名词**，所以**动词型请求一个都不命中** ——
「跑一下测试」「装个依赖」「这条能提交吗」都只拿到 **26/116** 个工具，
而 `dev_testrun` / `dev_deps` / `dev_commit` 全在外面。

## 2. 实测

```
问题                      有效工具数   该在里面的工具
改一下 README 里的端口      26/116      read_file/apply_edit 在（核心工具无条件放行）
跑一下测试                  26/116      dev_testrun    缺
装个依赖                    26/116      dev_deps       缺
这条能提交吗                26/116      dev_commit     缺
重构这个模块               105/116      （命中 _CODE 的「重构」）
```

**注意第一行**：核心工具（`CORE_TOOL_NAMES` + `dev_use_skill`）在
`_effective_tool_names` 里**无条件放行**，所以 `read_file`/`apply_edit` 不受影响。
我先前一度以为「连 `read_file` 都没有」—— **那是错的**，已更正。

## 3. 这不是小漏：机制与召回互相打脸

`dev_testrun` 的 description 里明写着：

> 跑 pytest 并回【结构化】判定，**别再 `run_command("pytest -q")` 自己读一整屏文本**

而现在恰好是「跑一下测试」把它收走 —— 模型只能退回 `run_command`，
正是那个工具要防的事。

## 4. 根因

`agent_runtime/context_router.py` 的四个召回正则：

```python
_CODE    = 代码|源码|函数|方法|类|接口|实现|报错|异常|调用|文件|路径|配置|重构|修复|...
_WEB     = 联网|网上|最新|近期|今天|天气|气温|当前版本|搜索|github|...
_MCP_DISCOVERY / _ADAPTER_DISCOVERY
```

**全部以名词为主，动词几乎不覆盖。**「跑」「装」「提交」「截图」一个都没有。

（`_WEB` 里有「天气」是个例外 —— 说明加词确实管用，但那正是无限 regress 的起点。）

## 5. 修法：**检索即放行**，不动判据

### 为什么不是「加动词词」

写意图那一族今晚已被证过一次：词表**逐条消息判定、缺陷跨轮**，必然无限 regress。
加进去「跑测试」，明天「执行测试套件」「verify the build」还是漏。

### 为什么不是「改成看工具组」

没有判据。路由的目的就是**收窄**；「有 developer 组工具就全给」= 退回全量，
progressive 白做。

### 采用的：判据是**模型的动作**

`Agent._effective_tool_names()` 放行**本轮已被 `tool_search` 取回过**的工具。

「跑一下测试」要匹配到「测试」才放开 `dev_testrun` 是**猜**；
模型自己去 `tool_search("dev_testrun")` 是**说**。

链条实测 —— 原本只断在最后一环：

```
1) 索引里有 dev_testrun 那一行          True
2) 索引段头告知了 tool_search            True
3) tool_search 在有效集（可调）           True
4) tool_search 能取回 dev_testrun 用法    True
5) dev_testrun 本身在有效集（可调）      False   ← 断在这
```

只改第 5 环。检索机制一个字没动。

## 6. 检索不是授权

opencode codemode 的 `Authority Boundary` 一节把两件事分给不同主体：

- `CodeMode owns:` … **Model-facing tool discovery and instructions**
- `The host owns:` Authentication and **authorization**. Tool selection and
  immutable scope. … **approval**…

并明说：

> **A program cannot gain authority through prose or generated code.**

所以被搜出来的**写**工具只是「看得见」，能不能写**仍走写意图判定**
（`_intent_for_top_turn` + 租约）。本笔一个字没动那条护栏，
并有行为级守卫钉住（搜出 `dev_commit` 后，未授权的一轮仍 `denied`）。

## 7. 无上限，以及它的已知代价

放开**没有数量上限**（用户明选）。实测滥用形态：

| 场景 | 有效工具数 |
|---|---|
| 检索前 | 26 |
| 检索一个 | 27 |
| 把 developer 组全搜一遍 | **97**（全量 116 的 **84%**） |

**即 progressive 可被一次检索绕过。** 所以把放大记进
`Agent.tool_search_expansions` —— **让它可观测，而不是无声退化**。
下一个人看到它就该知道收窄在这轮失效了。

正常用法只放开实际检索过的那几个。

## 8. 每轮重置

`Agent._run` 开头 `self.tool_search_expansions = set()`，
检索得到的可见性**不跨轮**。不重置的话多轮下来有效集只增不减，
progressive 会被逐步绕光。

## 9. 端到端实测

真跑完整 ReAct 循环（LLM stub，两轮）：

```
第1轮  Action: tool_search  Action Input: dev_testrun
第2轮  原生通道 tools schema = 27 个   dev_testrun 在里面 ✓
expansions = {'dev_testrun'}
```

即：**查了用法 → 下一轮就能调**。

## 10. 未做

**召回正则仍未修。** 「跑一下测试」依然只命中 26 个工具 ——
本笔只是让模型**能自救**（检索即放行），没有让路由**更聪明**。

### 10.1 已试过并否决：文本算法修召回（实测，非推测）

两条路都写了探针（`tools/mutation/probe_recall_scoring.py`、
`probe_recall_ngram.py`，可复算）：

| 方案 | 目标进前 3 |
|---|---|
| `_TERM_SPLIT` 切词（复用同文件 `_skill_score` 的判据） | **1 / 12** |
| n-gram（2~4 字，中文无空格的标准做法） | **5 / 12** |

**n-gram 失败的两个独立模式：**

**模式 A：用户写法不是连续子串**

```
用户: '截个图看看页面'   含连续「截图」吗? False
```

「截个图」中间插了量词「个」。要救得先剥量词 —— 那又是一个词表。

**模式 B：那个词压根不在描述里**

```
web_search       描述含「高铁」? False
dev_diagnostics  描述含「报错」? False
```

**这是词汇差异，任何文本重合算法都救不了。** 要救只能改 112 条描述的措辞 ——
那是另一笔大改，且会动模型输入。

### 10.2 放宽召回的代价（实测）

原生通道 tool schema 成本，**每次模型调用都付**：

| 方案 | 工具数 | tokens |
|---|---|---|
| 现状（动词型请求） | 26 | **2 947** |
| 永远含 developer 组 | 97 | 12 842（4.4 倍） |
| 全量 | 116 | 14 490 |

### 10.3 三个 harness 都不做相关性召回（2026-10-08 核实）

| harness | 怎么决定暴露什么 |
|---|---|
| **Pi** | **不涉及工具选择** —— 安全模型是「进程边界」而非「工具边界」。`security.md` 通篇讲文件/凭据/进程/网络的可达性与三种隔离运行方式，**没有任何按问题挑工具的机制** |
| **Hermes** | **用户/平台显式 toolsets**（`hermes tools`、`--toolsets "web,terminal"`），另有平台 preset 与动态 `mcp-<server>` 工具集。**零自动召回** |
| **opencode** | **预算截断 + 轮转公平 + 检索** |

opencode codemode `Discovery` 一节原文：

> **Every tool namespace is always listed** with its tool count regardless of
> budget. Selection is **round-robin** across namespaces for fairness.

**没有相关性打分。** 我们的 `_CODE`/`_WEB` 关键词判定是三家里**唯一的异类**，
也是唯一失效的那个。

> ⚠ **更正**：本节早前写「Pi 有 4 个工具（read/write/edit/bash）」——
> **核实不到**。`security.md` 与 `README.md` 都没写工具数量，那个数字来自第三方
> 站点（搜索摘要）。已删掉 —— **不拿二手来源的数字当事实**。

### 10.4 已抄的一条（无争议）

opencode 同一节：

> The instructions state exactly **how comprehensive the list is**, both overall
> (`COMPLETE list` vs `PARTIAL - N of M shown`)

已照做，并补了 opencode 没有的一层 —— 我们还有 `_effective_tool_names()`
的**运行期按组收窄**，所以段头额外说明「本轮实际可调的通常比这少，
看到某个工具却调不动时先 `tool_search` 再重试，仍不可用就如实说明」。

守卫：`tests/test_tool_index_completeness.py`。

### 10.5 若将来要真修，得换判据类型

不是文本重合，是**语义相似度**（embedding）。但那是新依赖 + 每次请求有成本，
且 `retrieval` 那套向量检索是给**代码检索**用的，不是给**工具召回**用的 ——
独立一笔，不该顺手做。

**另一个方向是取消收窄**（三家的做法），代价见 §10.2 —— 那是产品取舍不是 bug 修复，
需要人来定。**→ 已决：撤除。见 §11。**

---

## 11. 已撤除相关性收窄（2026-10-08，用户明选「撤」）

`ContextRouter.route()` 现在**恒定**给出全部非外网工具组，
外网组只由用户的 `web_enabled` 决定：

```python
groups.update(("code", "developer", "general", "game", "assets",
                "knowledge", "orchestration"))
if web_enabled:
    groups.add("web")
```

### 代价（实测，原生通道 tool schema，**每次模型调用都付**）

| | 工具数 | tokens/轮 |
|---|---|---|
| 撤除前（动词型请求） | 26 / 116 | 2 947 |
| **撤除后（联网关）** | **109** | **13 832**（**4.7 倍**） |
| 撤除后（联网开） | 116 | 14 490 |

撤除后各类请求拿到**同一套**工具，`dev_testrun` / `dev_deps` / `dev_commit` /
`game_screenshot` / `dev_find_references` 全部到位。

### 外网组为什么一并改

原先 `web_enabled and _WEB.search(query)` —— 而实测「查下高铁票」「查高铁」
**都不命中**，于是**用户把联网开关打开了，`web_search` 却调不到**。
授权已给而能力不可达，与 `developer` 那条同病。

现在**开关本身就是闸门**：用户显式表达的意图不该再被关键词猜一遍。

### 保留的收窄

| 机制 | 为什么留 |
|---|---|
| 系统提示的 progressive 分层（`CORE_TOOL_NAMES` + `【扩展工具索引】`） | 那是**预算截断**，正是 opencode `Discovery` 明确保留的那一半（「Every tool namespace is always listed ... budget」），且有预算上限 |
| `web_enabled` 开关 | 安全开关，不是相关性猜测 |
| ~~`tool_search` 检索即放行~~ | **已删除**，见 §12 —— 它彻底不生效，且在白名单场景下是越权。`tool_search` **本身保留**，职责变成纯粹的「按需读长用法」 |
| 证据侧 `sources` 仍按问题判定 | 它会作为系统消息注入「本轮证据来源」，说 web 而没查 web 是不诚实的 |

### 被这一笔改变的三处前提（都改了，**不是删测试**）

撤除动作消灭了「某工具本来不在有效集里」这个前提，三处守卫/测试因此红：

1. `test_tool_search_expands_visibility.py` 的 5 条 —— 「检索前 26 个」
   不再成立。改成断言**机制本身**（检索确实记录命中、只多检索过的那些、
   有效集不超过注册表、重置生效），并在 docstring 里写明前提变更与原因。
2. `test_progressive_architecture.py::test_context_router_only_exposes_web_when_needed`
   —— 它断言的正是被撤掉的「靠措辞决定 web 组」。改写成
   `test_web_exposure_follows_the_switch_not_the_wording`：开则 web 组在、
   关则连 `sources` 里都没有。**改了断言的前提，没有删它。**
3. `test_no_relevance_narrowing.py`（新增 8 条）—— 含一条
   **「撤相关性不等于撤联网安全开关」**，一条**「代价就是 13832 tokens」**
   （将来有人想优化回去会先撞到它，从而知道自己在撤掉什么）。

**我没有为了让套件变绿而删任何一条。**

### 守卫

`tests/test_no_relevance_narrowing.py` 8 条。变异实测两次都咬：
恢复按 `_CODE` 收窄 → 3 failed；web 组无视开关 → 1 failed。

组合回归 **296 passed**。

---

## 12. 「检索即放行」已删除（2026-10-08，承接 §11）

`0fd31d2` 加的那条例外（本轮被 `tool_search` 取回过用法的工具进入有效集）
**连同配套的 `_TOOL_SEARCH_EXPANSIONS` / `Agent.tool_search_expansions`
一并删除**。

### 为什么删：它彻底不生效，且在白名单场景下是越权

撤除关键词收窄后实测 **8 种场景（4 类请求 × 联网开/关）全部零变化**：

```
联网关 109 -> 109    联网开 116 -> 116    变化集合恒为空
子代理白名单（tool_allowlist=['tool_search','read_file']）同样零变化
```

而它想做的事在白名单场景下**恰恰是越权** —— `names` 从白名单取，
检索把白名单外的工具捞回来 = 子代理靠检索扩权。
codemode 的 `Authority Boundary` 也明说 discovery 与 authorization 分属不同主体。

留着一条从不生效、且有已知代价（无上限，实测一次检索能把 developer 组
84% 捞回来）的机制，比删掉更糟 —— **下一个人会以为它在起作用。**

### `tool_search` 本身保留

它的价值没有随那条例外消失：系统提示里 `dev_vue` 的说明有 **1167 字符**、
`dev_page_action` 1046 —— 模型不必一开始就背这些。

**那是「按需读长用法」，不是「补救收窄错误」。** 这是它现在唯一的职责。

### 守卫重写并改名（不是逐条改断言）

`tests/test_tool_search_expands_visibility.py` 整个重写，**改名**为
`tests/test_tool_search_usage_only.py` —— 它守的不再是「expands visibility」。

保留的是**与收窄无关**的边界：检索**不是授权**（含机制级证据）、
精确名命中必须返回自己。加上三条**钉住删除**的守卫：机制不在了、
属性不在了、**检索对暴露零影响**（如实记录现状，不是期望）、
**检索不得扩子代理白名单**。

### 途中两个我自己的错

1. **守卫第一版写 `hasattr(Agent, 'tool_search_expansions')`** —— 那是对**类**的
   检查，而实例属性不构成类属性，所以哪怕 `__init__` 里加回了属性，
   断言仍恒真。**变异实测就是这么漏过去的**（报 8 passed 而实际有调用点）。
   改成对**实例**用 `hasattr`，才拿到真的 1 failed。
2. 挑「授权样本」时用了「改吧，把端口改掉」，它实测判 **denied** ——
   而那**不是检索造成的**，是已记录的**谓语动词缺口**（裸「改」不在词表，
   `6069386` 定的限制，见题库 B 节）。换用实测 granted 的「改一下端口」。

**两处都是「样本/判据没核实」**，不是代码问题。