"""「失败被补救成功」这一族的变异电池。

## 这一族在防什么

用户定下的口径是：

> 失败可以先报出来、标红，下一条补一句补救成功，失败的历史留着给用户看；
> 但既然已经补救成功了，**程序那里可以通过**。

这口径天生带着一个**会被悄悄放宽**的开关——只要把「补救成功」的判据写成
「后面有个任务跑成功了」，模型随手追加一个无关的成功任务就能把真失败洗白。
所以本电池的绝大多数条目都在咬「判据被放宽」这个方向，而不是咬「判据不存在」。

## 判据（只有一份，在 `remediated_task_ids`）

同时满足三条才算补救成功：

1. 该任务真的 `failed`（历史不会被抹掉）
2. 有一个**由重规划引入**的任务（`adopted.added` / `adopted.replaced`），
   且那次重规划**真的被采纳**（`applied` / `simulated`）
3. 那个补救任务**声明过对它的依赖**（`demoted_deps` 记录或仍在的 `depends_on`），
   并且**自己跑成了 ok**

第 3 条就是「放宽」的入口。实测把它改成 `declared = True`，
`tests/test_workflow_failed_then_remediated.py` 立刻红两条：
一条纯函数级（无关的成功任务不算补救），一条真跑工作流级
（`broken` 失败 + 模型加了个 `sidequest` -> 本该是 failed，却 completed）。

## 本电池里**故意没有**的一条变异，以及为什么

`execute_wave_node` 里判定「还有没有待跑任务」的 `pending` 那行：

```
pending = next((ids for ids in waves
                if any(task_id not in results
                       and task_id not in external for task_id in ids)), None)
```

把 `and task_id not in external` 删掉当然是一条真实缺陷（死循环），
但它**证明不了任何判据**——它只会让电池卡到超时。§9 那条「装不下来的变异
证明不了任何判据」的姊妹条：一条把 runner 跑挂的变异，报出来的是超时，
而正确结论是「这条变异不该写进电池，写进测试」。所以它留在
`tests/test_workflow_failed_then_remediated.py` 的行为层由真跑兜住
（用例本身有超时），不进电池。

## 层归属

两个类全在纯层：`RemediatedTaskIdsTests` 只调纯函数，
`FailedThenRemediatedWorkflowTests` 只用 `tempfile.mkdtemp()` 起一个
`GameWorkflowManager` 并喂假 runner——不碰浏览器、不碰工具链、不碰网络。
实测两个类合计 ~7s。
"""
BATTERY_NAME = 'failed_then_remediated'
MODULE = 'agent_runtime.game_workflow'
TARGET = 'agent_runtime/game_workflow.py'
CASE_ID = 'workflow-events-and-guards-survive-real-run'
TEST_MODULE = 'tests.test_workflow_failed_then_remediated'
#: 兼容旧引擎：单数 `BIND` 必须真实存在（`tests/test_mutation_batteries.py:223`
#: 直接 `hasattr(module, battery.BIND)`）。真正生效的是下面的 `BINDS`。
BIND = 'GameWorkflowManager'
#: 测试模块用的是 from-import，导入时就把旧模块的符号绑死在测试模块上了。
#: 引擎的 `BIND='gw'` 注入进去没人用——必须逐一换成变异后模块里的对应属性。
BINDS = (
    ('STATUS_COMPLETED', 'STATUS_COMPLETED'),
    ('STATUS_FAILED', 'STATUS_FAILED'),
    ('GameWorkflowManager', 'GameWorkflowManager'),
    ('WorkflowPolicy', 'WorkflowPolicy'),
    ('remediated_task_ids', 'remediated_task_ids'),
)
UNIT_CLASSES = (
    'RemediatedTaskIdsTests',
    'FailedThenRemediatedWorkflowTests',)
LIVE = ()
LAYER_EXEMPT = {}

#: 本块电池的变异条数下限：删一条变异必须显式改这里。
MINIMUM_MUTATIONS = 11

# ---- 锚点定义（每条都用 `str.count` 核过命中数 = 1）----

#: 补救关系的「依赖声明」判据——本族最容易被放宽的一行。
_DECLARED = ('declared = task_id in demoted.get(helper, []) or task_id in [\n'
             '                    str(d) for d in (row.get("depends_on") or [])]')

#: 补救任务自己必须跑成 ok。
_HELPER_OK = 'if not isinstance(row, Mapping) or str(row.get("status") or "") != "ok":'

#: 重规划必须**真被采纳**。
_HISTORY_STATUS = 'if str(entry.get("status") or "") not in {"applied", "simulated"}:'

#: 「由重规划引入」的任务集合。锚点必须**盖住整句**：只替换第一行的话，
#: 第二行的续行会变成孤儿语句，变异自己就编译不过（元测试当场报「这条变异
#: 自己写歪了」——这条守卫确实有牙）。
_INTRODUCED = ('introduced = {*list(adopted.get("added") or []),\n'
               '                      *list(adopted.get("replaced") or [])}')

#: 图节点的完成判定：波级无错 **且** 全量结果里没有仍然失败的活儿。
_EXECUTION_OK = '"execution_ok": bool(report.get("ok")) and not _live_failed,'

#: 仍失败且未被补救的任务集合（判据本体）。
_NOT_REMEDIATED = ('and str(tid) not in _remediated\n'
                   '                and not row.get("optional") and str(tid) not in _opt_ids)')

#: 已跑过的任务（带 prior_results）不再派发一次。
_EXTERNAL_SKIP = 'if task_id in results or task_id in external:'

#: 「可选任务」的豁免表。
_OPT_IDS = ('_opt_ids = {str(t.get("id")) for t in effective_tasks\n'
            '                        if isinstance(t, Mapping) and t.get("optional")}')

#: 补救留痕的事件（界面那句「失败已补救」就是它）。留痕只有这一份
#: （`GameWorkflowManager._mark_remediated`），主路径与重试路径都调它——
#: 此前两份拷贝各写一遍时，锚在这里的变异会在另一份上「活下来」，而正确结论
#: 是「有一份没人守」，不是「守卫没牙」。
_EVENT = ('self._event(state, "subagent_remediated", task_id=dead_id, by=helper,\n'
          '                        note="失败历史保留（界面照旧标红），最终判定按补救成功计")')

#: 补救说明写进成员行的文案。
_NOTE = ('row.setdefault("remediation_note",\n'
         '                               "该任务失败后由「%s」补救成功；失败记录保留。" % helper)')

MUTATIONS = [
    # ---- 判据被放宽（本族主方向）----
    ("补救关系不再要求依赖声明：模型追加一个无关的成功任务就能洗白真失败"
     "（守卫断言无关任务不算补救 + 真跑仍 failed）",
     _DECLARED,
     'declared = True  # 变异：放宽成「有跑成功的重规划任务就算补救」'),

    ("补救关系只认任务表上的 depends_on，忽略重规划记下的降级记录"
     "（守卫断言合法补救仍能放行）",
     _DECLARED,
     'declared = task_id in [str(d) for d in (row.get("depends_on") or [])]'),

    ("补救任务的状态检查被摘掉：补救任务自己也失败照样算补救成功",
     _HELPER_OK,
     'if not isinstance(row, Mapping):'),

    ("补救任务的状态检查只排除 failed：派出去但没结果的任务被当成补救成功",
     _HELPER_OK,
     'if not isinstance(row, Mapping) or str(row.get("status") or "") != "failed":'),

    ("采纳状态的枚举不全：被拒绝的重规划也开始产生补救关系",
     _HISTORY_STATUS,
     'if str(entry.get("status") or "") not in {"applied", "simulated", "rejected",\n'
     '                                              "empty_proposal", "no_change"}:'),

    ("补救判据完全不认「由重规划引入」：失败永远不会被补救",
     _INTRODUCED,
     'introduced = set()  # 变异：不认重规划引入的任务'),

    # ---- 判据被绕过（图节点的完成判定）----
    ("execution_ok 只信本波 report[\"ok\"]：全量结果里还失败着也能报完成"
     "（实测「无关补救」由此 completed）",
     _EXECUTION_OK,
     '"execution_ok": bool(report.get("ok")),'),

    ("execution_ok 恒真：验收门以外的一切都不再拦",
     _EXECUTION_OK,
     '"execution_ok": True,'),

    ("已被补救的失败任务仍算「还失败着」：合法补救被判失败",
     _NOT_REMEDIATED,
     'and not row.get("optional") and str(tid) not in _opt_ids)'),

    ("可选任务豁免表被摘掉：跑了并失败的可选任务把整件事拖成 failed"
     "（守卫断言 optional 失败不算失败）",
     _OPT_IDS,
     '_opt_ids = set()'),

    # ---- 副作用与留痕（用户明确要求：历史留着 + 另起一句）----
    ("已跑过的任务被重复派发：失败任务再写一遍文件、再花一遍 token",
     _EXTERNAL_SKIP,
     'if task_id in results:'),

    ("补救不留痕：事件与成员行都不再说明后来补上了",
     _EVENT,
     'pass  # 变异：补救不留痕'),

    ("补救说明不写进行里：界面折叠事件后就说不出「后来补上了」",
     _NOTE,
     'row.setdefault("remediated_by", helper)  # 变异：说明不落行'),
]