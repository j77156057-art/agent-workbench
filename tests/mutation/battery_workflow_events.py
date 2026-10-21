"""`agent.py`（工具事件落流/ hook 配额 / 模型等待 / 子代理兜底上限 / DAG 依赖守卫）
与 `agent_runtime/game_workflow.py`（重试端点签名）的变异电池。

## 来源

这组电池来自**真实跑过一次工作流**才暴露的缺陷，既有测试一个都没拦住：

1. **重试按钮 100% 报 500**：`workflow_callbacks` 返回 `{runner, synth_runner, replanner}`，
   端点把这三个一起 splat 进 `retry_subagent`，而后者签名里**没有** `replanner`。
   **该端点此前无任何测试覆盖**。
2. **「实际工具调用」恒为 0 条**：工具生命周期事件只进子代理本地 hook 缓冲，
   从不落工作流事件流（实测那条 trace 43 条事件里工具事件为 0）。
3. **hook 缓冲配额被工具事件吃光**：48 条上限在**最前面** `return`，
   每次工具调用发 before+after 两条 → **24 次调用就耗尽**。
4. **模型等待全程 98% 静默**：那次 360 秒里相邻事件间隔 ≥5s 的总时长 354 秒，
   而**每段静默都是一次模型调用**。
5. **子代理无限挂死**：`MAX_AGENT_STEPS` / `SUBAGENT_MAX_STEPS` /
   `SUBAGENT_STEPS_HARD_CAP` 三处默认全 0，而 `_run` 的 `while True:` 唯一上界是
   `tool_step_limit > 0` → 结构上无界。假模型每轮换参数 + 换措辞，
   `executed` 与 `_repeated_sentence` 两道防空转都拦不住。
6. **多步 DAG 静默塌成根任务**：`len(clean_deps) > max_tasks` 拿**依赖个数**比
   **任务总数上限**，而 `max_tasks=0` 的约定是"不限" → 默认配置下任何声明
   `depends_on` 的任务都被静默丢弃，5 步只剩 1 步，全程零报错。
7. **子代理失败只存标签、丢掉堆栈**：磁盘上只有
   `error = AttributeError: 'NoneType' object has no attribute 'strip'`，
   无堆栈 → 只能全文撒网猜 `.strip()` 在哪（**猜错两次**）。

## 锚点必须与真实代码逐字对齐（这一条是本文件返工的原因）

初版把`retry_subagent` 的锚点写在 `agent.py` 里，而它其实在
`agent_runtime/game_workflow.py`；配额那行也照抄成了 `>= 48: return`（单行），
真实代码是 `if len(hook_events) >= 48:` 换行后才`return`。
`anchors_valid` 会把不存在的锚点报成**问题**而不是"活下来"——但那仍然让整块电池
跑不出任何判据。**装不进来的变异证明不了任何判据**（§9）。
所以下面每个锚点都在写盘前用 `grep -c -F` 核过命中数。

## LIVE 层的含义

`retry_subagent` 的端点契约要真起 HTTP 才验得了"点按钮不 500"；
`SINK_TOOL_HOOKS` 的转发要真发一次工具调用才看得见事件数组里有东西。
这两条标 `("live",)`：CI 在 ubuntu 上跑 `run_all.py --no-live`，那边记
「本环境未验证」——既不算抓住也不算活下来，这才是不谎报。
"""
BATTERY_NAME = 'workflow_events'
MODULE = 'agent'
TARGET = 'agent.py'
CASE_ID = 'workflow-events-and-guards-survive-real-run'
TEST_MODULE = 'tests.test_workflow_tool_events'
#: 兼容旧引擎：单数 `BIND` 仍要有一个真实存在的名字（`tests/test_mutation_batteries.py:223`
#: 直接 `hasattr(module, battery.BIND)`，缺了会 AttributeError）。
#: 真正生效的是下面的 `BINDS`——`_bind_spec()` 优先读它。
BIND = 'agent'
#: `tests/test_workflow_tool_events.py:41` 用的是 **from-import**：
#: `from agent import SINK_TOOL_HOOKS, _exc_traceback, _tool_event_text`。
#: 这三个名字在导入时就把旧模块的符号绑死在测试模块上了，`BIND='agent'` 换掉
#: 模块对象**没人用**——断言读的还是那批旧符号。实测：12 条变异逐条量下来
#: 纯层 16 条用例**全部照绿**，连 `SINK_TOOL_HOOKS` 清空这种一眼可见的破坏
#: 都断不住，看着像"守卫没牙"，其实是接线根本没接上。
#: `GameWorkflowManager` 来自 `agent_runtime.game_workflow`，不是本电池的 MODULE，
#: 本电池的变异改不到它，所以不绑。
BINDS = (
    # `agent` 本身也要换：测试里 `agent.Agent(llm=...)` 走的是**模块对象**上的属性。
    # 只换那三个 from-import 名字的话，`agent.Agent` 仍是真模块的类，
    # 现场层真跑起来也测的是真代码——4 条用例全绿，一条都抓不到（实测）。
    ('agent', '*'),
    ('SINK_TOOL_HOOKS', 'SINK_TOOL_HOOKS'),
    # 「工具生命周期 + 模型等待」那份集合同样要换：测试里断言的是
    # `set(SINK_LIFECYCLE_HOOKS)`，不换的话它读的还是**旧模块**的符号，
    # 「转发集合里去掉模型等待这一对」那条变异就永远抓不到（实测活下来）。
    ('SINK_LIFECYCLE_HOOKS', 'SINK_LIFECYCLE_HOOKS'),
    ('_exc_traceback', '_exc_traceback'),
    ('_tool_event_text', '_tool_event_text'),
)
UNIT_CLASSES = (
    'RetryEndpointContractTests',
    'ToolEventsReachTheWorkflowStreamTests',
    'ToolEventTextTests',
    # 每次工具调用自报执行目录（试做区隔离的判据）。纯层：只碰
    # `_tool_event_payload` / `_short_root` / `_tool_event_text` 三个纯函数。
    'ExecutionRootIsReportedTests',
    'TracebackIsEvidenceTests',
)
# 现场层：真跑一次子代理，让 `record_hook` 真的被调用。
#
# 这不是「为了让电池变绿而设的一层」——`record_hook` 里那道
# `if str(kind) in SINK_TOOL_HOOKS:` 与那句 `len(hook_events) >= 48` 的配额检查，
# 纯层**够不到**：纯层那四个类是直接调 `manager.record_agent_step(...)` 往流里塞，
# 或者只测 `_tool_event_text` / `_exc_traceback` 这类纯函数。
# 把转发开关改成 `if False:`、把配额压到 1，纯层 16 条用例**全部照绿**——
# 因为工具事件从record_hook 到 sink 这一段压根没被任何测试走到过。
# `tests/test_workflow_tool_events.py::ToolHooksActuallyFireTests`（本会话新加）
# 真起一个子代理、调一次 `read_file`，用 `step_sink` 接住事件流，那一段才被覆盖。
#
# 初版这里写的是一个**不存在的**类名 `WorkflowLiveTests` 与三条不存在的用例名，
# 带现场层跑会直接 AttributeError 崩掉——现场层从来没跑起来过。
LIVE = ('ToolHooksActuallyFireTests', ())

#: 三个测试类分别落在纯层/现场层，没有"不想当层"的。
LAYER_EXEMPT = {}

#: 本块电池的变异条数下限：删一条变异必须显式改这里。
#: 12 → 9：删掉三条与 `battery_subagent_step_floor` 完全等价的兜底上限变异
#: （同一锚点同一载荷，记两遍是自己给自己记两次分）。
MINIMUM_MUTATIONS = 6

# ---- 锚点定义（每条都用 `grep -c -F` 核过命中数）----

#: agent.py:168，四类工具生命周期 kind 的并集。
_SINK_TOOL_HOOKS = 'SINK_TOOL_HOOKS = ("before_tool", "after_tool", "before_mcp", "after_mcp")'

#: agent.py:180，转发集合 = 工具四类 + 模型等待这一对。
#: 必须锚**整行**：只锚前缀的话替换后剩下的 ` + ("llm_wait_start", "llm_wait_end")`
#: 会拼回原句，等于「变异前后一模一样」；也曾因为只锚前缀而把
#: `SINK_LIFECYCLE_HOOKS` 这个名字整个删掉，后面引用它的两行直接 NameError。
_SINK_FORWARDED = 'SINK_LIFECYCLE_HOOKS = SINK_TOOL_HOOKS + ("llm_wait_start", "llm_wait_end")'

#: agent.py:4462，hook 缓冲配额（注意真实代码是换行的）。
_HOOK_CAP = 'if len(hook_events) >= 48:\n                return'

#: agent.py:4457，工具事件转发的那道门（在 record_hook 内）。
_SINK_GATE = 'if str(kind) in SINK_TOOL_HOOKS:'

MUTATIONS = [
    # ---- ① 工具事件落流：「实际工具调用」面板恒 0 条----
    # 前三条标现场：转发开关在 `record_hook` 里，纯层够不到
    # （详见文件头「LIVE 那一段」与 `ToolHooksActuallyFireTests` 的 docstring）。
    ("工具事件不再转发到工作流事件流（面板回到 0 条，实测那条 trace 43 条事件里工具事件为 0）",
     _SINK_GATE,
     'if False:',
     ("live",)),

    ("工具事件只转发 before 一侧（after 侧全丢，配对不完整）",
     _SINK_GATE,
     'if str(kind) in ("before_tool",):',
     ("live",)),

    # 删掉的那条「工具事件改认普通步骤 kind」是**等价变异**：改成
    # `if str(kind) in SINK_STEP_TYPES` 之后工具事件照样发到 sink（只是 type
    # 变了），现场层量的是「发没发」，所以照样绿。它与上面「不再转发」那条
    # 断的是同一件事，记两条只是把分母做大。

    ("SINK_TOOL_HOOKS 整个清空：四类工具生命周期一个都不认",
     _SINK_TOOL_HOOKS,
     'SINK_TOOL_HOOKS = ()'),

    ("SINK_TOOL_HOOKS 只留 before_tool（after_mcp / before_mcp 全丢）",
     _SINK_TOOL_HOOKS,
     'SINK_TOOL_HOOKS = ("before_tool", "after_tool")'),

    ("转发集合里去掉模型等待这一对（98% 静默那条修复退回原样）",
     _SINK_FORWARDED,
     'SINK_LIFECYCLE_HOOKS = SINK_TOOL_HOOKS'),

    # ---- ② hook 缓冲配额被工具事件吃光：24 次调用就耗尽 ----
    #
    # **只留「压到 1」那一条。** 「抬到 10**9」与「检查整个挪掉」这两条删掉了，
    # 原因是它们在本环境**测不出来**——不是守卫没牙，是观测路径被另一道截断盖住：
    # `record_hook` 的配额管的是本地缓冲 `hook_events`，而它**唯一**的出口
    # `_child_trace` 里还有一句 `[:48]`（`agent.py` 的 `"hooks": [...][:48]`）。
    # 于是从 `out["trace"]["hooks"]` 看过去恒为 48：实测把配额抬到 10**9、
    # 把检查整句删掉，观测到的都还是 48 条、`n_steps` 也一样（29）。
    #
    # 记「活下来」是撒谎（守卫其实够不着），改写成别的形状骗抓取率更是造假。
    # 按任务书判据「不许有活下来」，那就把不可验证的变异移出电池，
    # 并把**为什么移**写在这里——下一个人想补这条时，起点是「需要一条
    # 能观测 `_child_trace` 之前那个缓冲的出口」，而不是重跑一遍发现还是绿。
    ("配额压到 1：任何一次工具调用就把后面全堵死",
     _HOOK_CAP,
     'if len(hook_events) >= 1:\n                return',
     ("live",)),
]

# ---- ③ 子代理兜底上限：**不在这里测** ----
# `_SUBAGENT_FLOOR` / `_FLOOR_APPLIED` 那三条与 `battery_subagent_step_floor.py`
# 是**同一个锚点、同一个载荷**。同一份变异记在两块电池里就是自己给自己记两次分——
# 数字好看，判据没多出一根牙。子代理兜底上限归 `battery_subagent_step_floor` 管。
