"""「重规划被拒/抛异常」这一族的变异电池。

## 这一族在防什么

`replan_node` 的「拒绝」分支要读好几个**只在 `try` 里赋值**的局部变量
（`proposal_map` / `error` / `known_results` / `max_subagents` / `delta`）。
于是任何在赋值之前抛出的异常——重规划调用炸了、提案规范化炸了——都会让这条
分支**自己先崩**：`UnboundLocalError`，整个图节点挂掉，工作流变成一个没人能
解释的失败。

实测被探针 `.tmp/probe_rejected_tasks.py` 逮到，**连撞三次**
（`proposal_map` → `error` → `max_subagents`），每次补一个才走到下一个。
这正是「探针优于断言」的例子：单测当时全绿，因为没有一个用例让重规划抛异常。

同一分支还有两处留痕问题：
- 回 `"tasks": []`，把计划从**落盘的审计记录**里抹掉（真跑 `wf-17aae90e34c5`：
  `graph.tasks=[]` 而 `state.tasks` 有 3 个、`graph.results` 有 3 条）
- 把「重规划调用抛异常」和「模型给了空提案」报同一句话；后者引导用户去检查
  提案怎么写，而前者的真因是服务端/网络/解析炸了

## 故意不在本电池里的一条

`replan_history[-1]["refused_drop"]`（用户批准的必需任务被保护地拒绝）
在真跑层面**没有断言**，只有 `required_task_ids` / `keep_required_tasks` 的
单测（`tests/test_replan_keeps_approved_tasks.py`）。那是**另一族**，按规矩不混在
一次改动里，所以这里既不加断言也不假装它被覆盖——记下来，等它自己成为一族。

## 层归属

两个类全在纯层：`RejectReasonTests` 只调纯函数；
`RejectedReplanBranchTests` 只用 `tempfile.mkdtemp()` 起一个
`GameWorkflowManager` 并喂假 runner——不碰浏览器、不碰工具链、不碰网络。
"""
BATTERY_NAME = 'replan_rejection'
MODULE = 'agent_runtime.game_workflow'
TARGET = 'agent_runtime/game_workflow.py'
CASE_ID = 'workflow-events-and-guards-survive-real-run'
TEST_MODULE = 'tests.test_workflow_replan_rejection'
BIND = 'GameWorkflowManager'
#: 测试模块用 from-import，导入时就把符号绑死在测试模块上了——必须逐一换成
#: 变异后模块里的对应属性，否则引擎注入的 `BIND='gw'` 没人用（`battery_plan_dag`
#: 文件头记过这个坑：10 条变异里 8 条因此报「活下来」）。
BINDS = (
    ('STATUS_FAILED', 'STATUS_FAILED'),
    ('GameWorkflowManager', 'GameWorkflowManager'),
    ('WorkflowPolicy', 'WorkflowPolicy'),
    ('normalize_replan_proposal', 'normalize_replan_proposal'),
    ('replan_reject_reason', 'replan_reject_reason'),
)
UNIT_CLASSES = (
    'RejectReasonTests',
    'RejectedReplanBranchTests',)
LIVE = ()
LAYER_EXEMPT = {}

#: 本块电池的变异条数下限：删一条变异必须显式改这里。
MINIMUM_MUTATIONS = 10

# ---- 锚点定义（每条都用 `str.count` 核过命中数 = 1）----

#: 异常判据必须排在最前面。
_ERROR_FIRST = 'if error:\n        return "replan_error(%s)" % error'

#: 异常名必须真的传进判据（否则上面那条形同虚设）。
_ERROR_PASSED = 'len(known_results), error=error)'

#: 拒绝分支读的那几个变量必须**提前**有值——这是崩溃的根因所在。
_PREINIT = ('proposal_map: dict[str, Any] = {}\n'
            '            refused_drop: list[str] = []\n'
            '            delta: Any = None')

#: 崩溃兜底：任何异常都要降级成「没采纳」，不能让图节点挂掉。
_RECOVER = ('except Exception as exc:\n'
            '                delta = None\n'
            '                error = type(exc).__name__\n'
            '                demoted = []')

#: 拒绝时**不许**回 `"tasks": []`（那会把计划从审计记录里抹掉）。
_NO_TASK_WIPE = '"replan_count": attempt, "replan_error": detail,'

#: 拒绝也要记下模型**原本说了什么**。
_RAW_KEPT = ('"proposal_raw": _clean_text(json.dumps(\n'
             '                                        proposal_map, ensure_ascii=False, default=str), 800)')

#: 「全被安全边界拒绝」这个标签。
_SAFE_BOUNDARY = 'return "all_proposals_rejected_by_safety_boundary"'

#: 「提案本身空」。
_EMPTY = 'if not proposal_map:\n        return "empty_proposal"'

#: 「容量用尽」——最容易被写成「提案无效」的那条。
_CAPACITY = ('if max_subagents > 0 and known_results >= max_subagents:\n'
             '        return "subagent_capacity_exhausted(%d/%d)" % (known_results, max_subagents)')

#: 有 ignored 但也有采纳 -> 带计数的中间标签。
_IGNORED_COUNT = 'return "empty_or_invalid_proposal(ignored=%d)" % len(ignored)'

#: 有 ignored 但也有采纳 -> 带计数的中间标签。
#:
#: **这一条不写成变异**：从唯一调用点到不了。`replan_node` 只在
#: `not (delta or {}).get("changed")` 时问本函数，而 `apply_proposal` 里
#: `changed = bool(added or dropped or replaced)` —— changed 为假时三者必空，
#: 于是 `ignored` 非空的话前面那条 `all_proposals_rejected_by_safety_boundary`
#: 一定先返回。给它加变异，报出来的是「活下来」，而正确结论是**这条变异不该写**
#: （`battery_plan_dag` 文件头 §9 那条的姊妹条）。代码里那条分支留着当防御，
#: 并写了注释说明它当前到不了。
_IGNORED_COUNT = 'return "empty_or_invalid_proposal(ignored=%d)" % len(ignored)'

MUTATIONS = [
    # ---- 崩溃（本族的根因）----
    ("拒绝分支的局部变量不再预置：重规划抛异常时 UnboundLocalError 崩掉图节点",
     _PREINIT,
     'pass  # 变异：不再预置，拒绝分支读到未赋值变量'),

    ("异常不再降级成「没采纳」：任何异常直接穿透，图节点崩掉",
     _RECOVER,
     'except Exception as exc:\n                raise  # 变异：不降级'),

    # ---- 谎报（异常被说成「提案无效」）----
    ("异常判据被摘掉：重规划抛异常被报成「提案无效」",
     _ERROR_FIRST,
     'if False:\n        return "replan_error(%s)" % error'),

    ("异常判据降到最后：提案同时为空/容量用尽时报错方向",
     _ERROR_FIRST,
     'if error and not proposal_map and max_subagents <= 0:\n'
     '        return "replan_error(%s)" % error'),

    ("异常名不再传进判据：`_error` 读了等于没读",
     _ERROR_PASSED,
     'len(known_results))'),

    # ---- 抹掉留痕 ----
    ("拒绝时把计划抹成空：graph_state 里只剩 results、看不到任务",
     _NO_TASK_WIPE,
     '"replan_count": attempt, "replan_error": detail, "tasks": [],'),

    ("拒绝时不记模型原本说了什么：用户看到一句无法追问的拒绝",
     _RAW_KEPT,
     '"proposal_raw": ""'),

    # ---- 标签退化回「一切都是提案无效」----
    ("安全边界拒绝的标签退化成通用标签：分不清是边界拒还是提案写得烂",
     _SAFE_BOUNDARY,
     'return "empty_or_invalid_proposal"'),

    ("空提案的标签退化成通用标签",
     _EMPTY,
     'return "empty_or_invalid_proposal"'),

    ("容量用尽不再报自己的标签：用户去检查提案怎么写，而真因是没额度",
     _CAPACITY,
     'if False:\n'
     '        return "subagent_capacity_exhausted(%d/%d)" % (known_results, max_subagents)'),
]