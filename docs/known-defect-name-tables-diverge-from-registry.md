# 已知缺陷：按名字索引的权威分类表与注册项声明脱节（**已修**）

**状态**：已修（`read_only_uses_running_tool_specs`）
**发现于**：`6431721` 之后那次子代理改动（2026-10-06）
**发现方式**：为「只读角色不能改东西」找一个权威判据时，`agent_runtime.tools`
的分类表里找不到 `*_external_file` 三兄弟。

> **本文档的原始结论有一半是错的，保留在下面「已被推翻的判断」里。**
> 当初只查了 external 三兄弟就记档，范围说窄了；而且当时以为它们「被误判成
> read_local/pure」，实际注册层早已显式声明过。修的过程中真跑查出的是另一个
> 更大也更硬的问题。

## 真跑查出来的（原始记档漏掉的）

### 一、不是「这三个被误判」，是「按名字的表漏了 14 个」

原始记档说这三个工具「不在任何权威表里，所以被当成只读」。**前半句对，后半句
错**。`tools.py` 的注册项里早就显式声明了：

```python
"create_external_file": {
    "func": create_external_file,
    # 写盘工具：曾被声明为 read_local/pure/parallel_safe，
    # 于是绕过写意图护栏、拿不到幂等账本、还能和只读工具并发写。
    "capability": "write_external",
    "side_effect": "mutating",
    "parallel_safe": False,
},
```

真跑确认分类本来就是对的：`write_external / mutating / parallel_safe=False`。

真正的机制是 `coerce_tool_spec` 末尾那句：

```python
capability=Capability(value.get("capability", capability)),
```

**注册项显式声明的 capability 优先于表。** 于是同一个注册表里并存两套分类
机制，而按 name 求并集的消费者看不见声明的那一批。真跑量出来的规模：

| | 数量 |
|---|---|
| capability 由名字表决定 | 33 |
| capability 由注册项声明决定 | 15 |
| **有副作用却不在任何名字表里** | **14** |

那 14 个是：

```
create_external_file  edit_external_file  delete_external_file
dev_patch  dev_move  dev_deps  dev_pricing  dev_skill_create
generate_data_file  memory_save  memory_forget
dev_lanes  dev_plan  dev_propose
```

不只 external 三兄弟 —— **`dev_patch` / `dev_move` / `memory_save` 这些最核心的
写工具也都在里面**。

### 二、活漏洞：只读 planner 能拿到 11 个会改东西的工具

`agent.py` 的只读角色护栏当时正是「三张表的并集 + 三个手写例外名」。真跑
`_run_child` 并从子代理提示的「允许工具=」那行读实际生效的工具集：

```
工具                     拿到?  会改东西?
create_external_file      False   True
dev_deps                  True    True     ← 泄漏
dev_lanes                 True    True     ← 泄漏
dev_move                  True    True     ← 泄漏
dev_patch                 True    True     ← 泄漏
dev_plan                  True    True     ← 泄漏
dev_pricing               True    True     ← 泄漏
dev_propose               True    True     ← 泄漏
dev_skill_create          True    True     ← 泄漏
generate_data_file        True    True     ← 泄漏
memory_forget             True    True     ← 泄漏
memory_save               True    True     ← 泄漏
```

external 三兄弟之所以没泄漏，只是因为并集旁边手写了那份例外名单 ——
那正是 `test_tool_taxonomy.py` 存在的意义所反对的形态。

后果是本仓库反复出现的那一类：**角色自称只读、实际改了文件，而报告里这次
改动仍记为只读分析。**

### 三、查这个时我踩的两个坑（都记在这里，因为它们会骗到下一个人）

**坑 1：`tools.TOOLS` 里躺的已经是 `ToolSpec` 对象。**
`tools.py` 文件末尾调了 `upgrade_registry(TOOLS)`，就地把原始 dict 换掉；
而 `coerce_tool_spec` 第一行是 `if isinstance(value, ToolSpec): return value`。
于是直接读 `tools.TOOLS` 量到的全是**冻结之后**的结果 —— 我第一版探针
「清空三张表后变化 0 个」就是这么来的，是**假的**（表在注册那一刻是起作用的）。
要量真实影响面，必须重新 exec 出 `TOOLS = {...}` 那段字面量。

**坑 2：按名字猜写工具的启发式天生漏检。**
`test_tool_taxonomy.py` 那条守卫带 `startswith(("dev_", "create_", "apply_"))`
前缀过滤 —— `edit_external_file` / `delete_external_file` **根本不进检查**，
`delete_` 还不含任何提示词。它们当初能溜过去就是这个原因。

## 修法（两层，缺一不可）

1. **只读护栏不再按名字算，改为逐个问 spec**：capability ∈ {WRITE_LOCAL,
   WRITE_EXTERNAL, ADMIN, EXEC} **或** side_effect ≠ PURE。判据对「下一个
   新工具」免疫 —— 不管它叫什么、有没有进表。
2. **把漏掉的 14 个名字补进权威表**，让表本身重新完整（表仍要分类另外 33 个
   工具，且是对外导出的）。

守卫：`tests/test_tool_taxonomy.py` 新增 `RegistrationDeclarationsStayHonestTests`
（声明与表不得打架 / 声明了写能力的必须在某张表里）、
`tests/test_agent_subagent_autonomy.py` 三条行为守卫（合成工具造出「下一个
新工具」的场景 + 对真实注册表 48 个会改东西的工具逐个试）。
变异 10/10。

## 原始记档里「修之前该先做的事」四条，逐条交代

1. ✅ 真跑了三个工具。默认配置（安全模式）拒绝；`set_runtime("external_access_mode", "high")`
   + `DOCMIND_EXTERNAL_DIRS` 白名单下才真落盘。**注意权限不是环境变量** ——
   我第一次猜了三个环境变量名全被挡回。
2. ✅ 属 `WRITE_TOOLS`（会改磁盘），但 capability 是 `write_external` 这一档，
   与 `write_local` 是一等区分，不是同一档。
3. ❌ **没查**（`web_fetch` 是否会改东西）。不在本次范围内。
4. ✅ 做了 —— 而且发现原来那条守卫**结构上就防不住**：它只防「agent.py 出现
   影子副本」，不防「权威表漏登记」。新守卫直接对着注册表声明查表齐不齐。

## 刻意没做的一件事

- **`delete_external_file` 没改成 `IRREVERSIBLE`。** 探针查过：
  `IRREVERSIBLE` 除了在 `coerce_tool_spec` 里覆盖 `side_effect` 之后**没有任何
  消费点**，而 `mutating` 已经拿到幂等账本的保护（`side_effect != PURE` 就走
  账本）。所以这是**零行为收益的空改**，还会撞上
  `test_no_write_tool_is_mistakenly_parallel_safe`。要做的话得先给
  `IRREVERSIBLE` 找到真实消费者 —— 也就是说「不可逆」这一档目前是**只有名字
  没有语义**的，跟当初 `READ_EXTERNAL` 的处境一样。

## 已单独修掉的一件事

- **`read_external_file` 判成 `read_local`（另一个族，读侧）。**
  `Capability.READ_EXTERNAL` 定义着、导出着，**一个工具都没落进去**；前端
  `AutonomousCockpit.vue` 早就备好了标签「读取外部资料」。也就是说这一档是
  预留好的，只是没人用。

  后果实测（不是推断）：`Agent._lease_blocked` 是按 capability **字面值**比对
  租约的，而 `read_external_file` 没声明 capability → 按默认值落到 `read_local`
  → **只租用了「读项目内」的工作流，照样能把项目外的文件读出去**。租约就是用来
  划权限边界的，而「项目外」和「项目内」在权限上是两件事。

  探针确认这是**一处窄而真**的改动：全仓按 capability 字面值分流的地方只有
  `_lease_blocked` 一处；`_is_write_tool` / `_web_blocked` / `_arg_required` /
  `_is_verbatim_tool` 都不看 `read_local` 这一档。而且默认租约授**全部**
  Capability（`game_workflow.py` 两处建租约都不传能力），所以正常路径不受影响，
  只有显式窄租约才开始拦它 —— 那正是本意。

  守卫：`tests/test_external_read_is_its_own_capability.py` 9 条 + 变异 7/7。

## 已被推翻的判断

- ~~「这三个工具被误判成 read_local / PURE / parallel_safe」~~ —— 注册层早已修过。
- ~~「影响包括能力租约 / 审批门按分类表判断要不要人工确认，这三个绕过判断」~~ ——
  审批门与租约走 `ToolSpec`（读 capability），不走名字表，所以**没绕过**。
  真正受影响的只有「按 name 求并集」的那一处消费者：只读角色护栏。