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

若要修召回，可考虑的方向是让 `_CODE` 之类的判据看**工具组与请求的对应关系**
而不是关键词 —— 但那要先说清「解决什么」，且属于另一族。

`docs/known-defect-tool-search-substring-mismatch.md` 记的
`tool_search` 子串匹配缺陷也仍未修；本笔顺带加了「精确名排最前」这一半，
另一半（模糊命中要标注）未做。