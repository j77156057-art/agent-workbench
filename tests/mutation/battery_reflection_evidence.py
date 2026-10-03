"""反思判据可见性 / 模型等待事件配对 / 试做区 cwd 自报 / 空响应归一 的变异电池。

## 来源

四条都来自 `wf-f3da9d3ffb32` 那次真实工作流，既有测试一个都没拦住：

**① 反思判据结构上只能判「结论复述了吗」**。`traj` 传进来了，但 prompt 里只有
`步骤数：N；失败步骤：M`，**观察一个字都没给** → 判据不知道观察里有
`WRITE_OK: True` / `actual == after: True`，于是**三次正确的字节级写入被连续判失败**，
逼出两轮 replan 重做同一件事。修法是把 `_traj_evidence_block(traj)` 放进 prompt。

**② 证据块取头部而不是尾部**。子代理的最终写盘验证总在最后几步，头部只有最初的
目录探查——取头部等于把要judge 的那段正好丢掉。

**③ 全程 98% 静默**。那次 360 秒里相邻事件间隔 ≥5s 的总时长 354 秒，
**每段静默都是一次模型调用**。修法是 `llm_wait_start/end` 成对发出。

**④ 试做区隔离疑似失效**。子代理用相对路径 `open("values/level.gd","wb")` 写入，
事后核对：真实项目 mtime 22:53:12 已改、试做区 workspace 仍是 09-11 的原始 mtime。
已排除三种猜测（scope 机制正常、是 contextvar 不会跨线程、并行批次已做 context 复制），
**仍未定位**——所以只加了 `stage_cwd_report` 让工具自报实际执行目录。
**本电池证明不了隔离本身修好了**，它只证明"自报那一行还在"。

**⑤ 空响应崩成 AttributeError**。`llm.py` 的 `getattr(msg, "content", None)` 在
`content=None` 时原样返回；缺的是 agent 侧**没区分"带 tool_calls 的 None"（正常）
与"不带 tool_calls 的 None"（空响应）**。修在agent 侧不改 llm（改 llm 会破坏原生 FC 通道）。

## 锚点逐条核过

初版把 `_traj_evidence_block(traj)` 的锚点写成 `evidence_block = _traj_evidence_block(traj)`，
而真实代码是在 f-string 里内联 `f"{_traj_evidence_block(traj)}\n"`。
`anchors_valid` 会把不存在的锚点报成问题，但**装不进来的变异证明不了任何判据**（§9）。
所以下面每条锚点都在写盘前用 `grep -c -F` 核过命中数。

## ⑤ 这块是 `agent.py` + `agent_runtime/tools.py` 两个模块

引擎一个电池只认一个 `TARGET`，所以 `tools.py` 那两条（试做区自报）**放在
`battery_stage_cwd.py`**，不混进这里。混进来会让 `MODULE='agent'` 的导入路径
与实际被改的文件不是同一个，报告就成了自己写给自己的假分数。
"""
BATTERY_NAME = 'reflection_evidence'
MODULE = 'agent'
TARGET = 'agent.py'
#: 门禁题必须是 `DEV_DATASET` 里**真实存在**的 id：引擎的 `gate_failures` 找不到就回
#: 「门禁题不存在」，而对照组一红引擎就直接 `return`——14 条变异**一条都不会跑**，
#: 汇总却打成「0/14 抓到」，看着像守卫没牙，其实是这块电池根本没开工。
#: 曾经写过一个不存在的 `reflection-criterion-can-see-observations`，代价就是 0/14。
#: 现用对方新加的 `workflow-events-and-guards-survive-real-run`：它的
#: `sink_forwarded_includes_llm_wait` 正好覆盖下面那两条 SINK 变异。
CASE_ID = 'workflow-events-and-guards-survive-real-run'
TEST_MODULE = 'tests.test_reflection_sees_evidence'
#: `BIND` 必须是**测试模块里那个名字**。引擎做 `setattr(测试模块, BIND, 变异后的模块)`，
#: 而这几个测试文件都是 `import agent` 后用 `agent._traj_evidence_block(...)` 断言的
#: —— BIND 写成 `re` / `emt` / `ssf` 之类，注入的是另一个名字，断言**仍然走真模块**，
#: 于是每条纯层变异都会报"活下来"。**报'活下来'时先怀疑接线**（§9）。
BIND = 'agent'
UNIT_CLASSES = (
    'LlmWaitEventTests',
    'StageCwdReportTests',
    'TrajEvidenceBlockTests',
    #: 本会话新加：既有`test_it_takes_the_tail_where_the_write_verification_lives`
    #: 用 30×5 字符的数据（正文 639 < limit 1600），**压根不进截断分支**，
    #: 于是「取尾部 / 顺序复位」这两条变异在它面前是死代码。没有这个类，
    #: 那两条只能记「活下来」——但根因是守卫没牙，不是变异写歪。
    'TruncatedEvidenceKeepsTheTailTests',
    'ReflectionSeesEvidenceTests',
    'DeterministicFallbackTests',
)
LIVE = ()

#: 三个测试类分别落在纯层/现场层，没有"不想当层"的。
LAYER_EXEMPT = {}

#: 本块电池的变异条数下限：删一条变异必须显式改这里。
#: 14 → 13：删掉两条与 `battery_empty_model_turn` 完全等价的空响应归一变异，
#: 并把一条「取头部」改写成两条真正落在截断分支内的变异（-2 +1 = 13）。
MINIMUM_MUTATIONS = 13

# ---- 锚点定义（每条都用 `grep -c -F` 核过命中数）----

#: agent.py:2075，证据块以**尾部**为准进 prompt。
_EVIDENCE_IN_PROMPT = 'f"{_traj_evidence_block(traj)}\\n"'

#: agent.py，证据块函数签名（limit 是有界保证）。
#:
#: 默认值 2026-10 从 1600 抬到 `_DEFAULT_EVIDENCE_CHARS`（6400）：实测
#: `wf-2c5612c6ee53` 的 `design-fix` 读了 6 个文件合计 5614 字，1600 的预算下判据
#: 只看到 3 步、每步 3 成，于是判了三句**都判得对但材料是 harness 饿出来的**结论
#: （「所有观察均被截断」「无法核验输入映射」「缺少 Main.tscn 的读取证据」）。
#: 有界性这条守卫**没有因此放松**：6400 仍然是硬上限，变异照样打得中。
_BLOCK_SIG = 'def _traj_evidence_block(traj, limit: int = _DEFAULT_EVIDENCE_CHARS) -> str:'

#: agent.py:2240，截断时如实标注，不假装给全了。
#:
#: 尾部多一个 `%s` 是**形状声明**（`_shape(...)`）——它排在正文之后，是刻意为之：
#: 截断取尾部，声明若排在最前面，预算一紧就被切掉，而那正是判据最需要它的时刻。
_TRUNC_NOTE = '"工具观察（共 %d 步，下面是**最后 %d 步**，前面的未提供）：\\n%s\\n%s"'

#: agent.py:2026，失败步骤被标出来。
_OK_MARK = 'mark = "" if ok is None else (" [失败]" if ok is False else " [成功]")'

#: agent.py:2074，反思判据改成「以工具观察为准」。
_EVIDENCE_RULE = '"**证据以「工具观察」为准**：观察里有的结论不必逐字复述，"'

#: agent.py:180，模型等待这一对要转发出去。
#: 必须锚**整行**：只锚前缀的话，替换后剩下的 ` + ("llm_wait_start", "llm_wait_end")`
#: 会拼回原句，等于「变异前后一模一样」——自己给自己记一分假成绩。
_WAIT_PAIR = ('SINK_LIFECYCLE_HOOKS = SINK_TOOL_HOOKS + ("llm_wait_start", "llm_wait_end")')

MUTATIONS = [
    # ---- ① 判据看不见观察：prompt 里没有证据块 ----
    ("证据块整个不进 prompt——判据退回「只看见步骤数与失败数」",
     _EVIDENCE_IN_PROMPT,
     'f""'),

    ("证据块被替换成步骤数——等价于没修（旧版就是这个形状）",
     _EVIDENCE_IN_PROMPT,
     'f"步骤数：{len(traj or [])}"'),

    # ---- ② 证据块取尾部：正好丢掉要judge 的那段 ----
    # 这两条必须落在**截断分支内部**。初版写的是把 `reversed(lines)` 改成 `lines`，
    # 实测那条是**死代码路径**：`test_it_takes_the_tail_where_the_write_verification_lives`
    # 的 30 步数据正文只有 639 字符，小于默认 `limit`，根本不进`if len(body) <= limit`
    # 的 else 分支——`reversed()` 一次都没被调用。「活下来」看着像守卫没牙，
    # 其实是这份数据永远走不到那里（§9：先问「它依赖的是代码还是环境」）。
    # 现在改取真正被执行的两步：遍历方向、最后的顺序复位。
    #
    # 锚点在 2026-10 重写过一次：证据块新增了「声明哪些观察带行号」这一段，
    # 截断循环随之从 `for line in reversed(lines)` 改成按下标倒着走
    # （`kept_idx` 是给声明用的），旧锚点整段消失、`anchors_valid` 报「出现 0 次」。
    # **变异意图没变**：仍要打掉「从尾部取」与「把顺序装回去」这两件事。
    ("证据块从尾部倒着拼——步骤顺序整个倒过来，判据读到的时间线是反的",
     'for i in range(len(lines) - 1, -1, -1):',
     'for i in range(len(lines)):'),

    ("取到的片段不再复位顺序——保留的是头部的 N 步，只是被标成「最后 N 步」",
     '    kept.reverse()\n    kept_idx.reverse()',
     '    pass'),

    # ---- ③ 有界性：limit 不传 / 大到没边 ----
    ("证据块默认 limit 抬到 10**9——「有界」这条判据名存实亡",
     _BLOCK_SIG,
     'def _traj_evidence_block(traj, limit: int = 10 ** 9) -> str:'),

    ("证据块 limit 变成 0——空轨迹也返回非空，判据以为有证据",
     _BLOCK_SIG,
     'def _traj_evidence_block(traj, limit: int = 0) -> str:'),

    # ---- ④ 截断必须如实标注：假装给全了，判据会在"看不到的那段"上下结论 ----
    ("截断时不再标注——上层以为看到的是全部",
     _TRUNC_NOTE,
     '"工具观察（共 %d 步）：\\n%s"'),

    # ---- ⑤ 失败步骤的标记：判据要知道哪步失败了 ----
    ("失败步骤不再标 [失败]——判据只看见一串动作，看不见哪步坏了",
     _OK_MARK,
     'mark = ""'),

    ("失败与成功标记互换——判据把坏步骤当好的",
     _OK_MARK,
     'mark = "" if ok is None else (" [成功]" if ok is False else " [失败]")'),

    # ---- ⑥ 判据规则本身：改成要求逐字复述（就是那条结构缺陷的原形状）----
    ("判据改回要求逐字复述结论——回到「只能判复述长度」那个结构缺陷",
     _EVIDENCE_RULE,
     '"**结论必须逐字复述观察内容**：观察里有的内容必须在结论里重写一遍，"'),

    # ---- ⑦ 空 traj 要明说「无」，不能看起来像证据丢了 ----
    ("空轨迹不再明说——返回空串，判据以为证据没传进来",
     'return "工具观察：无（这一步没有执行任何工具）"',
     'return ""'),

    # ---- ⑧ 空响应归一 **不在这里测** ----
    # `agent.py:3391` 的 `if acc is None: acc = ""` 是子代理 ReAct 循环里的归一，
    # 与 `battery_empty_model_turn.py` 第 1 条**同一个锚点、同一个载荷**。
    # 同一份变异记在两块电池里就是「自己给自己记两次分」——数字好看，判据没多一根牙。
    # 这块电池只管反思判据与事件配对；空响应归一归 `empty_model_turn` 管。

    # ---- ⑨ 模型等待事件成对 ----
    ("模型等待这一对不再转发——时间线回到 98% 静默",
     _WAIT_PAIR,
     'SINK_LIFECYCLE_HOOKS = SINK_TOOL_HOOKS'),

    ("只转发 start 不转发 end——界面算不出「已经等了很久」",
     _WAIT_PAIR,
     'SINK_LIFECYCLE_HOOKS = SINK_TOOL_HOOKS + ("llm_wait_start",)'),
]
