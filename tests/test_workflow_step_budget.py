# -*- coding: utf-8 -*-
"""T4：子代理逐任务步数预算 + 工作流步数自动放大 + telemetry 聚合（离线）。"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
from agent_runtime.game_workflow import (  # noqa: E402
    GameWorkflowManager, WorkflowPolicy, effective_workflow_max_steps)


class ParseGeneratedTasksMaxStepsTests(unittest.TestCase):
    def _parse(self, *items):
        raw = {"tasks": [dict(item) for item in items]}
        return GameWorkflowManager._parse_generated_tasks(raw)

    def test_valid_max_steps_retained(self):
        tasks = self._parse({"id": "a", "role": "coder", "task": "实现",
                             "depends_on": [], "max_steps": 10})
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["max_steps"], 10)

    def test_out_of_range_and_garbage_dropped(self):
        tasks = self._parse(
            {"id": "a", "role": "coder", "task": "实现", "max_steps": 1001},
            {"id": "b", "role": "coder", "task": "自测", "depends_on": ["a"],
             "max_steps": 0},
            {"id": "c", "role": "tester", "task": "验收", "depends_on": ["b"],
             "max_steps": "x"},
            {"id": "d", "role": "tester", "task": "回归", "depends_on": ["c"],
             "max_steps": True},
        )
        self.assertEqual([t["id"] for t in tasks], ["a", "b", "c", "d"])
        for task in tasks:
            self.assertNotIn("max_steps", task, "非法 max_steps 必须丢弃回退默认：%r" % task)

    def test_fractional_float_rejected_but_integral_float_and_str_ok(self):
        from orchestrator import _strict_int_steps
        self.assertIsNone(_strict_int_steps(10.7))
        self.assertEqual(_strict_int_steps(10.0), 10)
        self.assertEqual(_strict_int_steps(" 8 "), 8)
        self.assertIsNone(_strict_int_steps("10.7"))
        self.assertIsNone(_strict_int_steps(True))
        tasks = self._parse({"id": "a", "role": "coder", "task": "实现",
                             "max_steps": 10.7})
        self.assertNotIn("max_steps", tasks[0])
        tasks = self._parse({"id": "b", "role": "coder", "task": "实现",
                             "max_steps": "8"})
        self.assertEqual(tasks[0]["max_steps"], 8)

    def test_boundary_values(self):
        tasks = self._parse(
            {"id": "a", "role": "coder", "task": "x", "max_steps": 1},
            {"id": "b", "role": "coder", "task": "y", "depends_on": ["a"],
             "max_steps": 12},
            {"id": "c", "role": "coder", "task": "z", "depends_on": ["b"],
             "max_steps": 1001},
        )
        self.assertEqual(tasks[0]["max_steps"], 1)
        self.assertEqual(tasks[1]["max_steps"], 12)
        self.assertNotIn("max_steps", tasks[2])

    def test_unlimited_agent_count_and_larger_task_step_cap(self):
        policy = WorkflowPolicy(max_subagents=0)
        self.assertEqual(policy.normalized().max_subagents, 0)
        tasks = GameWorkflowManager._parse_generated_tasks([
            {"id": "a", "role": "coder", "task": "长任务", "max_steps": 120},
            {"id": "b", "role": "tester", "task": "验证"},
        ], max_tasks=0)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["max_steps"], 120)


class EffectiveMaxStepsTests(unittest.TestCase):
    def setUp(self):
        self._old_default = agent_mod.SUBAGENT_MAX_STEPS
        agent_mod.SUBAGENT_MAX_STEPS = 6

    def tearDown(self):
        agent_mod.SUBAGENT_MAX_STEPS = self._old_default

    def test_table_driven_auto_budget(self):
        self.assertEqual(effective_workflow_max_steps(40), 0)
        with patch.dict(os.environ, {"DOCMIND_WORKFLOW_STEPS_MIN": "24",
                                     "DOCMIND_WORKFLOW_STEPS_MAX": "200"}, clear=False):
            self._assert_bounded_auto_budget()

    def _assert_bounded_auto_budget(self):
        cases = [
            (1, 24),    # 1*6+4=10 → 下限 24
            (4, 28),    # 4*6+4=28
            (16, 100),  # 16*6+4=100
            (40, 200),  # 40*6+4=244 → 硬顶 200
            (0, 24),
        ]
        for count, expected in cases:
            self.assertEqual(
                effective_workflow_max_steps(count), expected,
                "任务数 %s 的自动预算应为 %s" % (count, expected))

    def test_explicit_policy_wins(self):
        self.assertEqual(effective_workflow_max_steps(16, explicit=50), 50)
        self.assertEqual(effective_workflow_max_steps(16, explicit=300), 300)
        # 显式 24 等价于「未调高」，自动预算照常生效
        with patch.dict(os.environ, {"DOCMIND_WORKFLOW_STEPS_MIN": "24",
                                     "DOCMIND_WORKFLOW_STEPS_MAX": "200"}, clear=False):
            self.assertEqual(effective_workflow_max_steps(16, explicit=300), 200)
            self.assertEqual(effective_workflow_max_steps(4, explicit=24), 24)


class PlanBudgetTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {"DOCMIND_WORKFLOW_STEPS_MIN": "24",
                                                "DOCMIND_WORKFLOW_STEPS_MAX": "200"}, clear=False)
        self.env_patch.start()
        self.manager = GameWorkflowManager(tempfile.mkdtemp())
        self._old_default = agent_mod.SUBAGENT_MAX_STEPS
        agent_mod.SUBAGENT_MAX_STEPS = 6

    def tearDown(self):
        agent_mod.SUBAGENT_MAX_STEPS = self._old_default
        self.env_patch.stop()

    def _start_and_plan(self, task_count, policy):
        state = self.manager.start("多阶段开发任务", policy=policy)
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        tasks = [{"id": "t%d" % i, "role": "tester", "task": "任务 %d" % i,
                  "depends_on": (["t%d" % (i - 1)] if i else []),
                  "max_steps": 10}
                 for i in range(task_count)]
        return self.manager.plan(wid, tasks)

    def test_auto_budget_scales_with_task_count(self):
        planned = self._start_and_plan(
            4, WorkflowPolicy(max_subagents=4))
        self.assertEqual(planned["policy"]["max_steps"], 28)
        self.assertEqual(planned["context_layers"]["step_budget"],
                         {"effective_max_steps": 28, "source": "auto",
                          "task_count": 4})
        # max_steps 回显到子代理名册
        self.assertTrue(all(row.get("max_steps") == 10
                            for row in planned["subagents"]))

    def test_large_plan_hits_hard_upper_bound(self):
        planned = self._start_and_plan(
            16, WorkflowPolicy(max_subagents=16))
        self.assertEqual(planned["policy"]["max_steps"], 100)

    def test_explicit_policy_is_preserved(self):
        planned = self._start_and_plan(
            16, WorkflowPolicy(max_subagents=16, max_steps=50))
        self.assertEqual(planned["policy"]["max_steps"], 50)
        self.assertEqual(
            planned["context_layers"]["step_budget"]["source"], "explicit")

    def test_explicit_low_policy_not_overwritten_by_auto_budget(self):
        # 显式给低于自动下限 24 的预算也必须尊重（review m-4）。
        planned = self._start_and_plan(
            4, WorkflowPolicy(max_subagents=4, max_steps=10))
        self.assertEqual(planned["policy"]["max_steps"], 10)
        self.assertEqual(
            planned["context_layers"]["step_budget"],
            {"effective_max_steps": 10, "source": "explicit", "task_count": 4})


class EnvConfigurableStepLimitsTests(unittest.TestCase):
    ENV = {"DOCMIND_WORKFLOW_STEPS_MIN": "50",
           "DOCMIND_WORKFLOW_STEPS_MAX": "120"}

    def test_env_overrides_auto_budget(self):
        with patch.dict(os.environ, self.ENV, clear=False):
            # 自动公式跟随子代理默认 24 步，夹取区间为 [50,120]
            self.assertEqual(effective_workflow_max_steps(0), 50)
            self.assertEqual(effective_workflow_max_steps(4), 100)
            self.assertEqual(effective_workflow_max_steps(10), 120)
            self.assertEqual(effective_workflow_max_steps(40), 120)
            # 显式值：高者夹硬顶，中段保留，低于下限者视为未调高走自动
            self.assertEqual(effective_workflow_max_steps(1, explicit=300), 120)
            self.assertEqual(effective_workflow_max_steps(1, explicit=80), 80)
            self.assertEqual(effective_workflow_max_steps(4, explicit=24), 24)

    def test_invalid_env_falls_back_to_builtin_defaults(self):
        with patch.dict(os.environ, {"DOCMIND_WORKFLOW_STEPS_MIN": "abc",
                                     "DOCMIND_WORKFLOW_STEPS_MAX": "  "},
                        clear=False):
            self.assertEqual(effective_workflow_max_steps(1), 0)
            self.assertEqual(effective_workflow_max_steps(40), 0)

    def test_max_below_min_is_lifted(self):
        with patch.dict(os.environ, {"DOCMIND_WORKFLOW_STEPS_MIN": "100",
                                     "DOCMIND_WORKFLOW_STEPS_MAX": "50"},
                        clear=False):
            self.assertEqual(effective_workflow_max_steps(0), 100)
            self.assertEqual(effective_workflow_max_steps(40), 100)

    def test_policy_default_and_normalized_follow_env(self):
        with patch.dict(os.environ, self.ENV, clear=False):
            self.assertEqual(WorkflowPolicy().max_steps, 50)
            self.assertEqual(WorkflowPolicy(max_steps=300).normalized().max_steps, 120)
            # 显式低值仍必须存活（review m-4），下限只作用于自动预算
            self.assertEqual(WorkflowPolicy(max_steps=10).normalized().max_steps, 10)

    def test_start_and_plan_respect_env_limits(self):
        manager = GameWorkflowManager(tempfile.mkdtemp())
        old_default = agent_mod.SUBAGENT_MAX_STEPS
        agent_mod.SUBAGENT_MAX_STEPS = 6
        try:
            with patch.dict(os.environ, self.ENV, clear=False):
                tasks = [{"id": "t%d" % i, "role": "tester", "task": "任务",
                          "depends_on": (["t%d" % (i - 1)] if i else []),
                          "max_steps": 10}
                         for i in range(4)]
                # 未显式指定：默认值 50 与配置下限相同 → 自动预算 50
                state = manager.start("环境变量预算-自动",
                                      policy=WorkflowPolicy(max_subagents=4))
                wid = state["workflow_id"]
                manager.choose(wid, "recommended")
                planned = manager.plan(wid, tasks)
                self.assertEqual(planned["policy"]["max_steps"], 50)
                self.assertEqual(
                    planned["context_layers"]["step_budget"]["source"], "auto")

                # 显式 40（≠配置下限 50）必须被识别为显式并保留
                state2 = manager.start("环境变量预算-显式",
                                       policy=WorkflowPolicy(max_subagents=4, max_steps=40))
                wid2 = state2["workflow_id"]
                manager.choose(wid2, "recommended")
                planned2 = manager.plan(wid2, tasks)
                self.assertEqual(planned2["policy"]["max_steps"], 40)
                self.assertEqual(
                    planned2["context_layers"]["step_budget"]["source"], "explicit")
        finally:
            agent_mod.SUBAGENT_MAX_STEPS = old_default


class _EndlessActionsLLM:
    """每次被调用都返回一个新的 read_file 动作；靠参数变化绕过重复调用护栏。"""
    provider = "fake"
    model = "step-budget"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, stream=True, **kwargs):
        self.calls += 1
        text = ("Thought: 继续核对\nAction: read_file\n"
                "Action Input: f-%d.txt" % self.calls)
        # 子代理以 stream=False 跑：非流式调用必须返回纯字符串
        return [text] if stream else text

    def count_tokens(self, text):
        return 0


class RunChildStepCapTests(unittest.TestCase):
    #: 这些用例要验证「硬顶把申请值夹到 12」，所以硬顶必须**只在它们跑的时候**
    #: 是12。此前它写在 `setUp` 里，于是**整个类**都带着硬顶 12——
    #: 包括 `test_global_child_step_limit_caps_task_override` 那条，而它断言的是
    #: `_resolve_child_max_steps(120) == 24`（配置的 `child_max_steps=24`）。
    #:
    #: 夹取链是 `min(24, HARD_CAP=12)` = 12，所以它期望 24、实测 12，
    #: **隔离单跑也红** —— 矛盾在类内部，与测试顺序无关。
    #:
    #: 那条测试的**意图**是「全局配置的上限能压住任务申请值」，而硬顶是**另一套
    #: 机制**。让它继承硬顶等于让它同时验证两件事，且两件事互相矛盾。
    _HARD_CAP = 12

    def setUp(self):
        self._old_default = agent_mod.SUBAGENT_MAX_STEPS
        agent_mod.SUBAGENT_MAX_STEPS = 6

    def tearDown(self):
        agent_mod.SUBAGENT_MAX_STEPS = self._old_default

    def _with_hard_cap(self, value):
        old = agent_mod.SUBAGENT_STEPS_HARD_CAP
        agent_mod.SUBAGENT_STEPS_HARD_CAP = value
        self.addCleanup(setattr, agent_mod, "SUBAGENT_STEPS_HARD_CAP", old)

    def _run(self, max_steps, hard_cap=_HARD_CAP):
        self._with_hard_cap(hard_cap)
        executed = []

        def fake_read(arg):
            executed.append(str(arg))
            return "内容 %s" % arg

        llm = _EndlessActionsLLM()
        parent = agent_mod.Agent(
            llm=llm, tool_registry={"read_file": {"func": fake_read}})
        result = parent._run_child(
            "researcher", "逐文件核对", reflect=False, max_steps=max_steps)
        return result, executed

    def test_ten_actions_when_max_steps_ten(self):
        result, executed = self._run(10)
        self.assertEqual(len(executed), 10, "max_steps=10 应恰好执行 10 个动作")
        self.assertEqual(result["max_steps"], 10)

    def test_request_above_hard_cap_is_clamped(self):
        result, executed = self._run(99)
        self.assertEqual(len(executed), 12, "99 必须被硬顶夹到 12")
        self.assertEqual(result["max_steps"], 12)

    def test_default_when_omitted(self):
        _result, executed = self._run(None)
        self.assertEqual(len(executed), 6)

    def test_garbage_falls_back_to_default(self):
        _result, executed = self._run("x")
        self.assertEqual(len(executed), 6)

    def test_global_child_step_limit_caps_task_override(self):
        """配置的 `child_max_steps` 能压住任务申请值。

        这条要验证的是**配置上限**这一条路径，而它有两个前提——类里的 `setUp`
        恰好把两个都破坏了，所以它此前**隔离单跑也红**：

        | setUp 里的设置 | 后果 |
        |---|---|
        | `SUBAGENT_MAX_STEPS = 6` | `_resolve_child_max_steps` 里 `if SUBAGENT_MAX_STEPS <= 0 and configured_steps > 0` 整段跳过 -> **配置上限根本不生效**，返回 120 |
        | `SUBAGENT_STEPS_HARD_CAP = 12` | 夹取链 `min(24, 12)` -> 返回 12 |

        而它断言 24。所以这条必须把两者都归零，才是在测「配置上限」这一件事。
        """
        agent_mod.SUBAGENT_MAX_STEPS = 0   # 让配置上限这条路径生效
        self._with_hard_cap(0)             # 0 = 不设硬顶
        with patch("agent_runtime.capacity.get_settings", return_value={
                "max_agents": 8, "child_max_steps": 24, "local_auto_limit": True}):
            self.assertEqual(agent_mod._resolve_child_max_steps(120), 24)

    def test_the_hard_cap_still_wins_over_the_configured_limit(self):
        """反向也钉住：硬顶存在时它**确实**压过配置上限。

        没有这一条，上面的修法可能变成「把两个开关都偷偷放开了」——测试会绿，
        而夹取行为被削弱。那是本条最需要防的回归。
        """
        agent_mod.SUBAGENT_MAX_STEPS = 0
        self._with_hard_cap(12)
        with patch("agent_runtime.capacity.get_settings", return_value={
                "max_agents": 8, "child_max_steps": 24, "local_auto_limit": True}):
            self.assertEqual(agent_mod._resolve_child_max_steps(120), 12,
                             "硬顶不再压住配置上限——夹取机制被削弱了")


class _CyclicActionsLLM:
    """按固定动作序列循环产出；参数带递增序号以绕过同参重复护栏。"""
    provider = "fake"
    model = "nav-free"

    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0

    def chat(self, messages, stream=True, **kwargs):
        name = self.actions[self.calls % len(self.actions)]
        self.calls += 1
        text = ("Thought: 继续勘察\nAction: %s\n"
                "Action Input: x-%d" % (name, self.calls))
        return [text] if stream else text

    def count_tokens(self, text):
        return 0


class _SameNavLLM:
    """每次都用相同参数调用 list_dir（用于验证免费额度不能靠重复空转刷）。"""
    provider = "fake"
    model = "same-nav"

    def chat(self, messages, stream=True, **kwargs):
        text = "Thought: 再看一眼\nAction: list_dir\nAction Input: ."
        return [text] if stream else text

    def count_tokens(self, text):
        return 0


class NavFreeStepsTests(unittest.TestCase):
    """目录勘察免费额度：每轮前 NAV_FREE_STEPS 次 list_dir 不占工具步数。"""

    def setUp(self):
        self._old_nav = agent_mod.NAV_FREE_STEPS
        self._old_default = agent_mod.SUBAGENT_MAX_STEPS
        self._old_base = agent_mod.MAX_AGENT_STEPS
        agent_mod.NAV_FREE_STEPS = 2
        agent_mod.SUBAGENT_MAX_STEPS = 6
        # 压低动态预算基线，保证任意 cap 都以 tool_step_override 生效，
        # 排除 _step_budget 分档对这些用例的干扰。
        agent_mod.MAX_AGENT_STEPS = 1

    def tearDown(self):
        agent_mod.NAV_FREE_STEPS = self._old_nav
        agent_mod.SUBAGENT_MAX_STEPS = self._old_default
        agent_mod.MAX_AGENT_STEPS = self._old_base

    def _run(self, llm, max_steps):
        nav_args, read_args = [], []

        def fake_nav(arg):
            nav_args.append(str(arg))
            return "目录 %s" % arg

        def fake_read(arg):
            read_args.append(str(arg))
            return "文件 %s" % arg

        parent = agent_mod.Agent(
            llm=llm, tool_registry={"list_dir": {"func": fake_nav},
                                    "read_file": {"func": fake_read}})
        # planner 是少数自带 list_dir 白名单的只读角色（researcher 没有）；
        # 任务措辞避开「代码」等关键词，防止命中 _step_budget 高档位。
        result = parent._run_child(
            "planner", "勘察目录与文件结构", reflect=False, max_steps=max_steps)
        return result, nav_args, read_args

    def test_first_two_list_dir_do_not_cost_steps(self):
        # 纯 list_dir 序列、付费上限 3：2 次免费 + 3 次付费 = 5 次实际执行，
        # 第 6 次撞上限被强制收尾，不得执行。
        result, nav, read = self._run(_CyclicActionsLLM(["list_dir"]), 3)
        self.assertEqual(read, [])
        self.assertEqual(len(nav), 5, "前 2 次 list_dir 应免费，之后才计入 3 步上限")
        self.assertEqual(result["max_steps"], 3)

    def test_free_nav_mixed_with_paid_reads(self):
        # 交替 list_dir/read_file、付费上限 4：
        # nav(免费) read(1) nav(免费) read(2) nav(付费=3) read(4) → 下一次被拦
        result, nav, read = self._run(
            _CyclicActionsLLM(["list_dir", "read_file"]), 4)
        self.assertEqual(len(nav), 3, "两次免费 + 一次付费 list_dir")
        self.assertEqual(len(read), 3, "三次 read_file 均占付费步数")

    def test_free_nav_still_runs_after_paid_budget_exhausted(self):
        # 付费预算已烧光时，仍有免费导航额度也应放行 list_dir。
        # 序列：read x2（耗尽 2 步）→ list_dir（免费放行）→ 再 read 被强制收尾
        result, nav, read = self._run(
            _CyclicActionsLLM(["read_file", "read_file", "list_dir"]), 2)
        self.assertEqual(len(read), 2)
        self.assertEqual(len(nav), 1, "付费步数耗尽后免费 list_dir 仍应执行一次")

    def test_identical_list_dir_cannot_farm_free_quota(self):
        # 同参重复导航仍由防重复护栏拦截：免费额度不能被无限空转刷取。
        result, nav, read = self._run(_SameNavLLM(), 6)
        self.assertEqual(nav, ["."], "相同参数的 list_dir 只允许实际执行一次")
        self.assertEqual(read, [])


class WorkflowTelemetryStepsTests(unittest.TestCase):
    def test_subagent_tool_steps_summed_in_observability(self):
        manager = GameWorkflowManager(tempfile.mkdtemp())
        state = manager.start(
            "长链路多角色开发",
            policy=WorkflowPolicy(approval_mode="high", max_subagents=8))
        wid = state["workflow_id"]
        manager.choose(wid, "recommended")
        steps_plan = {"a": 2, "b": 3, "c": 5}
        tasks = [{"id": "a", "role": "tester", "task": "调研",
                  "max_steps": 8},
                 {"id": "b", "role": "coder", "task": "实现",
                  "depends_on": ["a"], "max_steps": 8},
                 {"id": "c", "role": "tester", "task": "验证",
                  "depends_on": ["b"], "max_steps": 8}]
        planned = manager.plan(wid, tasks)
        # 默认 0 表示无限；计数仍在 observability 中持续记录。
        self.assertEqual(planned["policy"]["max_steps"], 0)
        self.assertEqual(
            planned["context_layers"]["step_budget"]["source"], "auto")

        def runner(task, context):
            return {"status": "ok", "conclusion": "完成",
                    "steps": steps_plan[task["id"]],
                    "max_steps": task.get("max_steps")}

        done = manager.execute(wid, runner)
        self.assertEqual(done["status"], "completed")
        telemetry = done["observability"]
        self.assertEqual(telemetry["subagent_tool_steps"], 10)
        rows = {row["id"]: row for row in done["subagents"]}
        self.assertEqual(rows["a"]["steps"], 2)
        self.assertEqual(rows["c"]["steps"], 5)
        self.assertEqual(rows["a"]["max_steps"], 8)


if __name__ == "__main__":
    unittest.main()
