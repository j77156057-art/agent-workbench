"""子代理兜底上限（`SUBAGENT_STEP_FLOOR`）的变异电池。

## 来源

**子代理无限挂死**（实测 `wf-f3da9d3ffb32`）。`MAX_AGENT_STEPS` /
`SUBAGENT_MAX_STEPS` / `SUBAGENT_STEPS_HARD_CAP` 三处默认全 0，而 `_run` 的
`while True:` 唯一上界是 `tool_step_limit > 0` → **结构上无界**。

假模型每轮换参数 + 换措辞，于是 `executed`（同一动作重复）与 `_repeated_sentence`
（同一句重复）**两道防空转都拦不住**——它每轮看起来都是"新的"。

修法是 `SUBAGENT_STEP_FLOOR`：cap 解析成 0 时落兜底上限而不是"不设"
（`agent.py:4539`）。**0 的语义仍然是"不人为收紧"**，只是不再等于"字面无限"。

## 初版 5 条「活下来」：不是守卫没牙，是层选错了

`SubagentStepFloorTests` 初版只有**两条纯层断言**：

1. `test_the_floor_is_a_positive_finite_number` —— `0 < FLOOR < 100000`
2. `test_the_floor_is_not_the_old_one_step_clamp` —— `FLOOR != 1`

其余（"cap==0 时子代理真的拿到 override"、"真跑一遍 60 秒内收尾"）在
`SubagentRunTerminatesTests`，**要真跑 ReAct 循环**。

初版把"cap==0 时落兜底"写成纯层变异 → 纯层跑不到那条断言 → 报"活下来"。
**但那条变异本身是对的**，`test_the_child_gets_an_override_even_when_cap_resolves_to_zero`
就断它——那条断言用 spy 替换了 `Agent.run`，**只读 `child.tool_step_override`
这个数，不跑 ReAct**。初版没注意到它已经落在纯层里，于是转头去标现场层，
方向就错了（详见下一节）。

**教训**：报"活下来"时先问**这条断言在哪个层**。
层选错了，正确的变异也会被误报成没牙；而"往更重的层挪"未必是对的方向——
重层自己可能先挂。

## 现场层为什么最终被删掉（初版标错了，实测推翻）

初版 `LIVE = ('SubagentRunTerminatesTests', ())`，理由是"它要真跑 ReAct 循环"。
本会话把它删掉，两条实测依据：

1. **7 条变异全部在纯层就红，且是 0.00 秒**（`.tmp/probe_pure4.py` 逐条量）。
   抓它们的那条纯层断言是
   `test_the_child_gets_an_override_even_when_cap_resolves_to_zero` ——
   它用 spy 替换了 `Agent.run`，**只读 `child.tool_step_override` 这个数，不跑 ReAct**。
2. **现场类在变异下必然挂死整块电池**（`.tmp/probe_hang.py` 受控实验，外层 40 秒墙钟）：
   | 组 | 结果 |
   |---|---|
   | 对照组（未变异） | `用时 0.9s ran=2 红=0`，exit=0 |
   | 变异组（拆掉兜底上限那一行） | 40 秒被杀，`exit=124`，一行输出都没有 |

   原因就是这块电池要治的那个病：`cap==0 时不再落兜底上限` 一改，
   `while True:` 的唯一上界 `tool_step_limit > 0` 重新变成**结构上无界**，
   `test_a_child_that_never_concludes_still_terminates` 里那道 60 秒断言
   **根本没机会执行**——它不是"跑得慢然后红"，是永不返回。
   引擎对变异后的模块**没有超时**（`_run_mutations` 里只有
   "纯层/门禁/现场"三层，没有看门狗），所以把这条变异送进现场层
   = 电池挂到 CI 超时。

   换句话说：现场层在这里**不是慢，是死**。慢还能靠超时兜，死不能。

所以正确处置是 `LIVE = ()` + `SubagentRunTerminatesTests` 进 `LAYER_EXEMPT`。
它仍然是一条有价值的守卫（进 `tests/` 正常跑 pytest 时会真跑一遍），
只是不能作为**变异判据**的那一层：它要断的东西纯层已经断得了，
而它自己会在变异副本上永挂。
"""
BATTERY_NAME = 'subagent_step_floor'
MODULE = 'agent'
TARGET = 'agent.py'
CASE_ID = 'workflow-events-and-guards-survive-real-run'
TEST_MODULE = 'tests.test_subagent_step_floor'
#: 测试是 `import agent` 后用 `agent.SUBAGENT_STEP_FLOOR` 断言的，BIND 必须是 `agent`。
BIND = 'agent'
UNIT_CLASSES = ('SubagentStepFloorTests',)
#: 空：无一条变异需要现场层。理由见文件头「现场层为什么最终被删掉」——
#: 7/7 在纯层 0.00 秒内就红，而现场类在变异副本上会**永挂**（实测 40s 被杀）。
LIVE = ()

#: `SubagentRunTerminatesTests` 仍是好守卫（`tests/` 里跑 pytest 会真跑一遍），
#: 但**不能**当变异判据的层：它断的东西纯层已经断得了，而它在变异副本上不返回。
#: 引擎对变异模块没有超时，送进去 = 电池挂到 CI 超时（实测见上）。
LAYER_EXEMPT = {
    'SubagentRunTerminatesTests': (
        '要真跑 ReAct 循环，拆掉兜底上限那条变异下 while True 永挂'
        '（实测 40s 被杀 / exit=124），而它要断的 7 条纯层 0.00s 就断掉了'),
}

#: 本块电池的变异条数下限：删一条变异必须显式改这里。
MINIMUM_MUTATIONS = 7

# ---- 锚点定义（每条都用 `grep -c -F` 核过命中数 = 1）----

#: 兜底上限本体。注意 `max(1, …)` 是**语义的一部分**（下限 1），不是随手写的。
_FLOOR = 'SUBAGENT_STEP_FLOOR = max(1, int(os.getenv("DOCMIND_SUBAGENT_STEP_FLOOR", "200")))'

#: cap==0 时落兜底上限，而不是"不设"。**纯层能断**：断言只读 `child.tool_step_override`
#: 这个数（`Agent.run` 被 spy 替掉），不跑 ReAct——见下面那段注释。
_OVERRIDE = 'child.tool_step_override = cap if cap > 0 else SUBAGENT_STEP_FLOOR'

MUTATIONS = [
    # ---- 纯层能断的：兜底上限的**值** ----
    # 判据：0 < SUBAGENT_STEP_FLOOR < 100000
    ("兜底上限下限 max(1,…) 去掉：配置写 0 时兜底上限真的是 0 → 又是结构上无界",
     _FLOOR,
     'SUBAGENT_STEP_FLOOR = int(os.getenv("DOCMIND_SUBAGENT_STEP_FLOOR", "200"))'),

    ("兜底上限夹回 1 步：在设置里填 0 反而只能走一步，比默认 24 严得多",
     _FLOOR,
     'SUBAGENT_STEP_FLOOR = max(1, int(os.getenv("DOCMIND_SUBAGENT_STEP_FLOOR", "1")))'),

    ("兜底上限抬到 10**9：把「字面无限」换个写法，纯层的「有限」判据要抓住它",
     _FLOOR,
     'SUBAGENT_STEP_FLOOR = max(1, int(os.getenv("DOCMIND_SUBAGENT_STEP_FLOOR", "1000000000")))'),

    # ---- 「兜底上限有没有真的落到子代理身上」：也是**纯层** ----
    # 初版把这四条标成现场（`("live",)`），理由是「要真跑 ReAct 循环」。
    # 逐条量过之后这个理由**不成立**：`SubagentStepFloorTests` 里那条
    # `test_the_child_gets_an_override_even_when_cap_resolves_to_zero`
    # 用 spy 替换了 `Agent.run`，**不跑 ReAct**，只读 `child.tool_step_override`。
    # 标错层的代价：CI 跑 `--no-live` 时它们会被记成「本环境未验证」而不是「抓住」，
    # 抓取率凭空少了 4 条，而纯层明明断得了。
    #
    # 实测（`.tmp/probe_pure4.py`，逐条注入 + 40s 墙钟）：7 条**全部**在纯层红，
    # 每条 ran=4 红=1，**每条 0.00 秒**。所以这四条留在纯层是对的。
    #
    # 反过来，「送去真跑类验」这条路是**死路**（`.tmp/probe_hang.py`）：
    # 第一条变异一改，`while True:` 永挂，40 秒被杀（exit=124），
    # 引擎对变异模块没有超时 → 电池会挂到 CI 超时。详见文件头那一节。
    ("cap==0 时不再落兜底上限（`while True:` 重新变成结构上无界）",
     _OVERRIDE,
     'child.tool_step_override = cap'),

    ("兜底上限只留给 cap>1 的分支——而 cap 恒为 0，等于永不生效",
     _OVERRIDE,
     'child.tool_step_override = cap if cap > 1 else 0'),

    ("兜底上限取反：cap>0 时才落兜底、cap==0 时留 0——正好把要治的形状换了个方向",
     _OVERRIDE,
     'child.tool_step_override = SUBAGENT_STEP_FLOOR if cap > 0 else 0'),

    ("兜底上限直接被忽略：子代理拿到的永远是 cap 自己（0 = 不设上限）",
     _OVERRIDE,
     'child.tool_step_override = cap + 0 if cap > 0 else cap'),
]
