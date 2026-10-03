"""多步 DAG 不被静默截断 的变异电池（`agent_runtime.game_workflow.py` 的
`_parse_generated_tasks` 依赖守卫 + `_raw_task_count` 诊断）。

## 来源

**多步 DAG 静默塌成根任务**（实测 `wf-f3da9d3ffb32`）：5 步的任务图跑成 1 步，
**全程零报错**——每一步都"成功"，所有断言都绿，只有拿**任务数**去数才看得见。

## 这块缺陷的真实代码位置（初版写错过一次，记在这里）

守卫是 `game_workflow.py:2084`：

```
if task_id in clean_deps or len(clean_deps) > _MAX_TASK_DEPS:
    continue
```

`_MAX_TASK_DEPS = 16` 是**单个任务允许的依赖条数上限**。初版电池把它误读成
「任务总数上限」，把变异写成"拿依赖个数比任务总数"——**那不是代码里的形状**，
装不进去的变异证明不了任何判据（§9）。

## 初版 16 条里11 条「活下来」，那不是守卫没牙，是我写歪了（这一条要记）

第一版按"防御性编程"的直觉给**周边清洗代码**加变异：清洗依赖 id、过滤空值、
检查任务合法性…… 结果 11 条全活下来。逐条读守卫的真实断言才发现：
`DependencyGuardTests` 断的是**具体值**（`_MAX_TASK_DEPS > 0`、
40 条 fan-in 的 `sink` 不许在结果里、自依赖任务被拒），
它**根本不管**依赖 id 有没有被 `re.sub` 规范化、空串依没被过滤。

**教训（§9 那条"装不下来的变异证明不了任何判据"的姊妹条）**：
变异要落在**守卫真的断言的那一行**上。给一段没人断言的周边代码加变异，
报出来的是"活下来"，而正确结论是"这条变异不该写"。
**报"活下来"时先问：我写的这条，守卫真的断言过它吗？**

所以下面每条变异都对应守卫里**一条具体断言**。

## 层归属：初版把 4 个类里的 2 个错标成现场层（实测后改回）

初版 `LIVE = ('TruncationMustNotBeSilentTests', 'DagApprovalPathTests', ())` ——
**三元素，元测试与引擎都会当场崩**（`tests/test_mutation_batteries.py:231` 与
`engine.py:149` 都写的是 `class_name, cases = battery.LIVE`，恰好两元素）。
本会话带现场层跑五块合并时崩在这里：`ValueError: too many values to unpack`。

但把它改成两元素只是让崩溃消失，**层归属本身仍然是错的**，实测依据：

- 探针量两个类的真实耗时与红数（`.tmp/probe_layer.py`）：
  `TruncationMustNotBeSilentTests` 0.25s / `DagApprovalPathTests` 0.79s，全绿。
- 两个类只碰 `tempfile.mkdtemp()`，**不碰浏览器、不碰工具链、不碰网络**——
  按引擎头部对现场层的定义（"现场跑不了 ≠ 抓住了"那段：现场层是给真浏览器/
  真工具链准备的），它们本来就是纯层材料。
- 元测试 `test_live_marks_and_live_layer_agree_both_ways` 两个方向都查：
  本电池一条 `("live",)` 标记的变异都没有，**声明了 LIVE 就等于"现场层是装饰"**，
  会直接红。

所以正确修法是**把它们移进 `UNIT_CLASSES`、把 `LIVE` 清空**。
若后来真出现只有真环境才能断的守卫，再加回来，并且那条变异**必须**带
`("live",)` 标记——否则 `--no-live` 跑的时候它会被记成"活下来"，
CI 步骤红，而那条红只是"这台机器没有浏览器"，不是判据没牙。

注意 `LAYER_EXEMPT` 与 `LIVE` 的区别：元测试
`test_every_test_class_is_reachable_from_a_layer` 要求测试文件里每个带
`test_` 的类，要么在某层的 `*_CLASSES`/`LIVE[0]` 里，要么在 `LAYER_EXEMPT`
里并写清原因。移进 `UNIT_CLASSES` 之后四个类全部有归属，`LAYER_EXEMPT` 可以留空。
"""
BATTERY_NAME = 'plan_dag'
MODULE = 'agent_runtime.game_workflow'
TARGET = 'agent_runtime/game_workflow.py'
CASE_ID = 'workflow-events-and-guards-survive-real-run'
TEST_MODULE = 'tests.test_plan_dag_not_truncated'
#: 兼容旧引擎：单数 `BIND` 仍要有一个真实存在的名字（`tests/test_mutation_batteries.py:223`
#: 直接 `hasattr(module, battery.BIND)`，缺了会 AttributeError）。
#: 真正生效的是下面的 `BINDS`——`_bind_spec()` 优先读它。
BIND = 'GameWorkflowManager'
#: `tests/test_plan_dag_not_truncated.py` 用的是 **from-import**：
#: `from agent_runtime.game_workflow import (_MAX_TASK_DEPS, GameWorkflowManager,
#: WorkflowError, _raw_task_count)`。这四个名字在**导入时**就把旧模块的符号绑死在测试
#: 模块上了，引擎的 `BIND='gw'` 注入进去没人用——10 条变异里 8 条因此报"活下来"，
#: 逐层量下来纯层与现场层**全都照绿**，看着像守卫没牙，其实是接线根本没接上。
#: 所以这里必须用 `BINDS` 把每个名字逐一换成变异后模块里的对应属性。
BINDS = (
    ('_MAX_TASK_DEPS', '_MAX_TASK_DEPS'),
    ('GameWorkflowManager', 'GameWorkflowManager'),
    ('WorkflowError', 'WorkflowError'),
    ('_raw_task_count', '_raw_task_count'),
)
#: 四个类全在纯层。理由见文件头「层归属」那一段（实测 0.25–0.79s，只用
#: `tempfile.mkdtemp()`，无外部依赖），不是"懒得标现场"。
UNIT_CLASSES = (
    'DependencyGuardTests',
    'RawTaskCountTests',
    'TruncationMustNotBeSilentTests',
    'DagApprovalPathTests',)
LIVE = ()

#: 四个测试类分别落在纯层，没有"不想当层"的。
LAYER_EXEMPT = {}

#: 本块电池的变异条数下限：删一条变异必须显式改这里。
MINIMUM_MUTATIONS = 10

# ---- 锚点定义（每条都用 `grep -c -F` 核过命中数 = 1）----

#: 依赖条数上限（守卫断言它必须 > 0）。
_MAX_DEPS = '_MAX_TASK_DEPS = 16'

#: 依赖守卫本体（自依赖 + 依赖条数上限）。守卫断言：自依赖被拒、超上界被拒。
_DEP_GUARD = ('            if task_id in clean_deps or len(clean_deps) > _MAX_TASK_DEPS:\n'
              '                continue')

#: 诊断函数的"绝不抛"兜底。
#:
#: 位置在 `_as_task_list` 而不是 `_raw_task_count`：抽 `_as_task_list` 之前这段
#: 形状处理在本文件里有**两份**，`_raw_task_count` 只取长度。元测试当场抓到
#: 「锚点出现 2 次」——只替换第一处的变异会改错地方。现已消成一份，锚点也
#: 跟着搬过来。§9：「上限写两处，就等于写了一处旧的」。
_RAW_COUNT_FALLBACK = ('        return list(raw) if isinstance(raw, list) else []\n'
                       '    except Exception:\n        return []')

#: 诊断数的是列表长度（守卫断言裸数组与 tasks 包装都要数对）。
_RAW_COUNT_LEN = '        return list(raw) if isinstance(raw, list) else []'

#: 剥``` 围栏。
_FENCE_STRIP = ('                text = re.sub(r"^```(?:json)?\\s*|\\s*```$", "", text,\n'
                '                              flags=re.I | re.S).strip()')

#: 认 plan.tasks 嵌套。
_PLAN_NEST = ('            if inner is None and isinstance(raw.get("plan"), Mapping):\n'
              '                inner = raw["plan"].get("tasks")')

MUTATIONS = [
    # ---- 守卫断言①：_MAX_TASK_DEPS 必须 > 0 ----
    ("依赖条数上限压到 0：依赖条数上界形同虚设（守卫断言 _MAX_TASK_DEPS > 0）",
     _MAX_DEPS,
     '_MAX_TASK_DEPS = 0'),

    ("依赖条数上限压到 -1：任何声明 depends_on 的任务都被丢（守卫断言 > 0）",
     _MAX_DEPS,
     '_MAX_TASK_DEPS = -1'),

    ("依赖条数上限抬到 10**6：40 条 fan-in 照单全收（守卫断言 sink 必须被拒）",
     _MAX_DEPS,
     '_MAX_TASK_DEPS = 10 ** 6'),

    # ---- 守卫断言②：自依赖必须被拒（`test_a_task_depending_on_itself_is_still_rejected`）----
    ("自依赖检查被摘掉——自环任务进图（守卫断言结果里只剩 b）",
     _DEP_GUARD,
     '            if len(clean_deps) > _MAX_TASK_DEPS:\n                continue'),

    # ---- 守卫断言③：超上界必须被拒（`test_the_dependency_count_bound_is_its_own_constant`）----
    ("超上界检查被摘掉——依赖个数上界形同虚设（守卫断言 sink 必须被拒）",
     _DEP_GUARD,
     '            if task_id in clean_deps:\n                continue'),

    # ---- 守卫断言④：守卫整个挪掉（自依赖与超上界都不再拦）----
    ("依赖守卫整个挪掉——自依赖与超上界同时失效",
     _DEP_GUARD,
     '            if False:\n                continue'),

    # ---- 诊断：被丢的任务必须"看得见"，否则又是静默 ----
    ("诊断不再兜底——模型给了非 JSON 时整个规划流程炸掉（守卫断言不抛）",
     _RAW_COUNT_FALLBACK,
     '        return list(raw) if isinstance(raw, list) else []\n'
     '    except Exception:\n        raise'),

    ("诊断对非列表一律返回 0——模型给了 tasks 包装时被报成「只给了 0 个任务」",
     _RAW_COUNT_LEN,
     '        return []'),

    ("诊断不剥``` 围栏——模型带围栏输出时被数成 0 个任务",
     _FENCE_STRIP,
     '                pass'),

    ("诊断不认 plan.tasks 嵌套——包一层的形状被数成 0",
     _PLAN_NEST,
     '            if inner is None:\n                inner = raw.get("plan")'),
]
