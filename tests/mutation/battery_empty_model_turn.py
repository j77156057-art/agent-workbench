"""空模型响应不再崩成 AttributeError 的变异电池。

## 来源

旧代码把 `content=None` 原样赋给 `acc`，下一行 `acc.strip()` 立刻抛
`AttributeError: 'NoneType' object has no attribute 'strip'`：
**整个子代理失败、任务判红**，而真实情况只是「模型这次什么都没说」。

关键在于**要区分两种None**（`agent.py:3368` 起的注释写明了）：

- **带 `tool_calls` 的 `None`** → 正常，那是原生 function-calling 通道的形状；
- **不带 `tool_calls` 的 `None`** → 空响应，需要归一。

所以修在**agent 侧**（`if acc is None: acc = ""`），**不改 `llm.py`**——
`llm.py` 的 `getattr(msg, "content", None)` 对 tool-call-only 消息**确实是对的**，
改它会破坏原生 FC 通道。`LlmContractTests` 就是钉住这一点的：
`chat` 对 tool-call-only 消息**仍允许**返回 None。

## 为什么这块不能只写一条变异

`if acc is None: acc = ""` 是单行，变异空间很小。但真正要防的是**将来有人
"顺手清理"这一段**——所以这块电池的价值在于把「为什么不能改 llm.py」也钉住：
`LlmContractTests` 与 `EmptyModelTurnTests` 同属一块电池，两边一起被变异打到才算数。
"""
BATTERY_NAME = 'empty_model_turn'
MODULE = 'agent'
TARGET = 'agent.py'
CASE_ID = 'workflow-events-and-guards-survive-real-run'
TEST_MODULE = 'tests.test_empty_model_turn'
#: 同`battery_reflection_evidence`：这几个测试都是 `import agent` 后用 `agent.` 断言，
#: BIND 必须是 `agent`，否则注入的名字没人用、断言走真模块 → 全报"活下来"。
BIND = 'agent'
UNIT_CLASSES = (
    'EmptyModelTurnTests',
    'LlmContractTests',
    #: 本会话新加。原有两类断不到「原生 FC 的 last_tool_calls 归一」与
    #: 「归一 vs 取消检查的先后」——它们的脚本全走文本协议（回复里自带
    #: `Action:`），模型从不设 `last_tool_calls`，那两条分支一次都没被走到。
    'NativeToolCallStillReachesTheTextProtocolTests',
    'CancellationBeatsEmptyTurnTests',)
LIVE = ()

#: 三个测试类分别落在纯层/现场层，没有"不想当层"的。
LAYER_EXEMPT = {}

#: 本块电池的变异条数下限：删一条变异必须显式改这里。
MINIMUM_MUTATIONS = 4

# ---- 锚点定义（每条都用 `grep -c -F` 核过命中数）----

#: 空响应归一成空串的那两行。
_EMPTY_TURN = 'if acc is None:\n                            acc = ""'

#: 归一之后立刻做取消检查——顺序有意义（先归一再让取消有机会抛出）。
_AFTER_EMPTY = '                            acc = ""\n                        _raise_if_cancelled()'

#: 工具调用归一：`last_tool_calls` 为空时走文本协议。
_CALLS_FETCH = ('calls = (getattr(self.llm, "last_tool_calls", None) or []) if use_tools else []')

MUTATIONS = [
    # ---- 归一本身----
    # 注意不能写成 `if False:` —— 那会掏空下面的缩进块，源码直接编译不过。
    # 引擎会把它报成「这条变异自己写歪了」（装不进来的变异证明不了任何判据）。
    # 保持缩进的等价破坏是「条件反过来」：`acc is not None` 时才赋空串。
    ("空响应不再归一（条件反过来）——又崩成 AttributeError: 'NoneType' has no attribute 'strip'",
     _EMPTY_TURN,
     'if acc is not None:\n                            acc = ""'),

    ("归一写成把 None 变成 'None' 字面量——不再崩，但结论里多出 'None' 四个字符",
     _EMPTY_TURN,
     'if acc is None:\n                            acc = "None"'),

    ("归一挪到取消检查之后——空响应先把取消标记吞掉",
     _AFTER_EMPTY,
     '                            acc = ""'),

    # ---- 原生 FC 通道那条不能被顺手改掉 ----
    ("工具调用归一被整体跳过：last_tool_calls 拿不到，原生 FC 通道的调用全部丢失",
     _CALLS_FETCH,
     'calls = []'),
]
