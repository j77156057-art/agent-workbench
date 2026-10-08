# `ContextRouter` 召回不足导致关键工具被收窄掉（已修）

> 立档：**2026-10-08**，同笔修复并留档。
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
需要人来定。