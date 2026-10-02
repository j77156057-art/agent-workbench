"""`agent_runtime/windows_sandbox.py`（Windows 作业对象）的变异电池。

这道门 2026-10-02 之前**一条测试都没有**，所以 `ActiveProcessLimit` 被写成裸字面量 4
之后谁都不会红——门禁整集稳定红三题（`diagnostics-*` 两题 + `headcheck-catches-untracked-
reference`），而它在检出和工作树里都红，于是"比失败题名单差集"把它当公共噪声减掉了。
这块电池就是为了让"限位表被改坏"当场有东西变红。

**为什么限位构造要抽成 `job_limits()` 纯函数**：内联在 `_create_job` 里的时候，非 Windows
上根本没有任何函数能读到那张表，于是 `KILL_ON_JOB_CLOSE` 掉了、预算写回 4 了，CI 上一个
红都没有。抽出来之后纯层在任何平台都能验它。

**现场层 earns its keep 的地方**（不是纯层的重复）：
- 「进程没被加入作业对象也不报错」这条变异，纯层一条都看不出来（表还是对的），
  只有真去关一次作业对象句柄、看整棵树会不会跟着走才知道；
- 「内存上限小到装不下一棵正常的树」和「输出不接管道」也只在这里现形。

**两条"活下来"要分开看**：本仓已为同型错误付过两次账（`docs/HANDOFF-vibecoding.md` §9
「上限写两处，就等于写了一处旧的」），所以第 8 条变异专门制造"常量与限位表分叉"，
而 `test_the_limit_table_and_the_constant_agree` 与现场层各自能抓它的一半。
"""

BATTERY_NAME = 'windows_sandbox'
MODULE = 'agent_runtime.windows_sandbox'
TARGET = 'agent_runtime/windows_sandbox.py'
CASE_ID = 'sandbox-process-budget-survives-a-real-tree'
TEST_MODULE = 'tests.test_windows_sandbox'
BIND = 'ws'
UNIT_CLASSES = (
    'JobLimitDeclarationTests',
    'JobCreationFailsClosedTests',)
LIVE = ('NestedProcessTreeTests', (
    'test_a_nested_tree_at_least_as_wide_as_the_gate_can_still_spawn',
    'test_the_job_is_reaped_when_its_handle_closes',))

#: 三个测试类分别落在纯层/现场层，没有"不想当层"的。
LAYER_EXEMPT = {}

#: 本块电池的变异条数下限：删一条变异必须显式改这里。
MINIMUM_MUTATIONS = 19

_FLAGS = ('    limits.BasicLimitInformation.LimitFlags = (\n'
          '        JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PROCESS_MEMORY |\n'
          '        JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION |\n'
          '        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)')

_NT_POPEN = ('        popen = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,\n'
             '                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,\n'
             '                                 text=False, env=dict(env),\n'
             '                                 creationflags=CREATE_NO_WINDOW)')

MUTATIONS = [
    # ---- 预算本身：这一组就是 2026-10-02 那次事故的形状 ----
    ("预算写回事故值 4",
     'ACTIVE_PROCESS_LIMIT = 32',
     'ACTIVE_PROCESS_LIMIT = 4'),
    ("预算压到 1（连一棵两层的树都放不下）",
     'ACTIVE_PROCESS_LIMIT = 32',
     'ACTIVE_PROCESS_LIMIT = 1'),
    ("预算写成无界：抬一抬是为了合法进程树，不是为了放走 fan-out",
     'ACTIVE_PROCESS_LIMIT = 32',
     'ACTIVE_PROCESS_LIMIT = 2 ** 31 - 1'),

    # ---- 限位表与常量分叉：§9「上限写两处」那条教训 ----
    ("限位表里偷偷写 4，常量不跟（上限写两处就等于写了一处旧的）",
     '    limits.BasicLimitInformation.ActiveProcessLimit = ACTIVE_PROCESS_LIMIT',
     '    limits.BasicLimitInformation.ActiveProcessLimit = 4'),
    ("限位表干脆不写进程数",
     '    limits.BasicLimitInformation.ActiveProcessLimit = ACTIVE_PROCESS_LIMIT',
     '    limits.BasicLimitInformation.ActiveProcessLimit = 0'),

    # ---- flag：回收与资源刹车 ----
    ("KILL_ON_JOB_CLOSE 掉了——99°C 漏浏览器真正的修法没了",
     _FLAGS,
     '    limits.BasicLimitInformation.LimitFlags = (\n'
     '        JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PROCESS_MEMORY |\n'
     '        JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)'),
    ("进程数刹车掉了",
     _FLAGS,
     '    limits.BasicLimitInformation.LimitFlags = (\n'
     '        JOB_OBJECT_LIMIT_PROCESS_MEMORY |\n'
     '        JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION |\n'
     '        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)'),
    ("两条内存上限掉了：抬了进程预算之后它们就是唯一兜底",
     _FLAGS,
     '    limits.BasicLimitInformation.LimitFlags = (\n'
     '        JOB_OBJECT_LIMIT_ACTIVE_PROCESS |\n'
     '        JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION |\n'
     '        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)'),
    ("孩子未捕获异常不再带走整棵树（静默改变行为）",
     _FLAGS,
     '    limits.BasicLimitInformation.LimitFlags = (\n'
     '        JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PROCESS_MEMORY |\n'
     '        JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)'),
    ("flag 整个清零：作业对象等于没设任何限位",
     _FLAGS,
     '    limits.BasicLimitInformation.LimitFlags = 0'),

    # ---- 数字没落到表里：只有 flag 没有数，限位是装饰 ----
    ("单进程内存上限不落到表里",
     '    limits.ProcessMemoryLimit = int(memory_bytes)',
     '    limits.ProcessMemoryLimit = 0'),
    ("作业对象内存上限不落到表里",
     '    limits.JobMemoryLimit = int(memory_bytes)',
     '    limits.JobMemoryLimit = 0'),
    ("每次调用共用同一张表：后一个作业对象会覆盖前一个",
     '    limits = _EXTENDED_LIMIT_INFORMATION()',
     '    global _SHARED_LIMITS\n'
     '    try:\n'
     '        limits = _SHARED_LIMITS\n'
     '    except NameError:\n'
     '        _SHARED_LIMITS = limits = _EXTENDED_LIMIT_INFORMATION()'),

    # ---- fail closed：起不出边界必须是红，不能静默降级成无边界跑 ----
    ("限位设不上也不抛（fail open）",
     '        raise _win32_error("无法设置适配器 Job Object 限制")',
     '        pass'),
    ("限位设不上时句柄漏了不还",
     '        kernel32.CloseHandle(handle)\n'
     '        raise _win32_error("无法设置适配器 Job Object 限制")',
     '        raise _win32_error("无法设置适配器 Job Object 限制")'),
    ("起不出作业对象也不抛",
     '        raise _win32_error("无法创建适配器 Job Object")',
     '        pass'),

    # ---- 现场层：只有真作业对象能看见的三条 ----
    # 锚点必须带上下一行那句「无法将【进程】加入 Job Object」：`run_isolated` 与
    # `spawn_isolated` 的 `if` 那一行**字面完全相同**，只靠它做锚点的话，引擎的
    # `replace(old, new, 1)` 会把变异打进 `run_isolated`——而本测试只用 `spawn_isolated`，
    # 于是变异改在死代码上，那一条会"活下来"，看起来像守卫没牙，其实是电池自己写歪了。
    ("进程没被加入作业对象也不报错——整棵树从此没人回收",
     '        if not process_handle.value or not kernel32.AssignProcessToJobObject(job, process_handle):\n'
     '            raise _win32_error("无法将进程加入 Job Object")',
     '        if False:\n'
     '            raise _win32_error("无法将进程加入 Job Object")',
     ("live",)),
    ("内存上限小到装不下一棵正常的树",
     'memory_bytes: int = 1024 * 1024 * 1024',
     'memory_bytes: int = 8 * 1024 * 1024',
     ("live",)),
    ("子进程输出不接管道：判据读不到东西却照样报绿",
     _NT_POPEN,
     _NT_POPEN.replace('stdout=subprocess.PIPE', 'stdout=subprocess.DEVNULL'),
     ("live",)),
]
