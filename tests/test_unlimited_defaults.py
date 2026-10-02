"""「0 = 不限」这条语义必须处处成立，否则用户改了设置却没生效。

**这个测试存在的理由是一串真实的坑**：把默认值改成 0 之后，「0 到底表示什么」
在代码里有五处不同理解，任何一处不一致都会让用户"填了 0 却还是被拦"——
而那种故障从界面上完全看不出来（数字在那儿，就是不生效）。

被钉住的五处（每一条都曾经或很容易出错）：

1. `capacity.DEFAULTS` 四项全是 0；
2. `normalize_settings` **原样保留 0**（不能"修正"回默认值）；
3. `agent._step_budget` 不能在 0 时回落到硬编码的 8；
4. `agent._resolve_child_max_steps` 不能把 0 用 `max(1, …)` 压成 1 步
   —— 那是本轮改动里最容易写错的一处，而且比不改更糟（1 步 < 原默认 24）；
5. `agent._task_token_budget` 与 `RunBudget(0)` 一致地表示不限。

另外钉住：**上限归零了，护栏不能跟着一起消失**。重复调用、失败熔断、格式失守
早退（`_MAX_FORMAT_STALLS`）都必须还在——它们防的不是"多跑几步"，而是"同一句话
重复八遍"和"原地空转"，与步数上限无关。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402
from agent_runtime import capacity  # noqa: E402


class DefaultsAreUnlimitedTests(unittest.TestCase):

    def test_the_four_limits_default_to_zero(self):
        for key in ("max_agents", "main_max_steps", "child_max_steps",
                    "task_token_budget"):
            self.assertEqual(capacity.DEFAULTS[key], 0,
                             "%s 的默认值应是 0（不限）" % key)

    def test_nudge_guards_are_kept(self):
        """放开步数不等于放开纠偏——纠偏防的是"同一提示重复 8 遍"。"""
        self.assertGreaterEqual(capacity.DEFAULTS["max_nudges"], 1)
        self.assertGreaterEqual(capacity.DEFAULTS["max_final_continuations"], 1)

    def test_zero_survives_normalization(self):
        """实测踩过的坑：校验里写成 `if x < 0 …: x = DEFAULTS[x]`，而当时 DEFAULTS
        是 8——用户填 0 会被"修正"回 8，看着改了却没生效。"""
        result = capacity.normalize_settings({
            "max_agents": 0, "main_max_steps": 0, "child_max_steps": 0,
            "task_token_budget": 0})
        for key in ("max_agents", "main_max_steps", "child_max_steps",
                    "task_token_budget"):
            self.assertEqual(result[key], 0, key)

    def test_a_negative_value_is_still_rejected(self):
        result = capacity.normalize_settings({"main_max_steps": -1})
        self.assertEqual(result["main_max_steps"], 0)

    def test_an_out_of_range_value_is_still_clamped(self):
        self.assertEqual(capacity.normalize_settings({"main_max_steps": 99999})["main_max_steps"],
                         capacity.DEFAULTS["main_max_steps"])
        self.assertEqual(capacity.normalize_settings({"max_agents": 999})["max_agents"],
                         capacity.DEFAULTS["max_agents"])

    def test_capacity_reports_unlimited_as_none(self):
        self.assertIsNone(capacity.remaining_agent_capacity(0, 9999))


class StepBudgetTests(unittest.TestCase):
    """0 不能在途中被"救回"成某个硬编码值。"""

    def setUp(self):
        self._old = agent_mod.MAX_AGENT_STEPS
        agent_mod.MAX_AGENT_STEPS = 0

    def tearDown(self):
        agent_mod.MAX_AGENT_STEPS = self._old

    def test_the_main_step_budget_is_zero_not_eight(self):
        """`agent.py` 原先在 base==0 时回落硬编码 8——那正是要消除的拦截。"""
        self.assertEqual(agent_mod._step_budget("随便问点什么"), 0)

    def test_audit_questions_still_get_their_own_floor(self):
        """审查类问题的独立下限是"至少这么多"，与"默认不限"不冲突。"""
        budget = agent_mod._step_budget("检查当前项目有哪些 bug")
        self.assertGreaterEqual(budget, 0)
        self.assertEqual(budget, max(0, int(agent_mod.AUDIT_MAX_AGENT_STEPS)))

    def test_child_steps_zero_means_unlimited_not_one(self):
        """本轮最容易写错的一处：末尾 `max(1, min(requested, cap))` 会把 0 压成 1，
        那比默认的 24 严得多，是"改了更糟"的典型。"""
        self.assertEqual(agent_mod._resolve_child_max_steps(0), 0)
        self.assertEqual(agent_mod._resolve_child_max_steps(None), 0)

    def test_an_explicit_child_value_is_still_honoured(self):
        self.assertEqual(agent_mod._resolve_child_max_steps(120), 120)

    def test_the_token_budget_is_unlimited(self):
        self.assertEqual(agent_mod._task_token_budget(), 0)

    def test_run_budget_treats_zero_as_unlimited(self):
        from agent_runtime.run_budget import RunBudget
        budget = RunBudget(agent_mod._task_token_budget())
        self.assertTrue(budget.preflight(10_000_000, 1_000_000),
                        "0 必须让 preflight 放行，否则大 prompt 会在第一轮就被掐")
        self.assertIsNone(budget.remaining)


class GuardsSurviveTests(unittest.TestCase):
    """上限归零，护栏不能跟着消失。"""

    def test_the_format_stall_guard_is_still_on(self):
        """它防的正是用户最早报告的现象（同一句话重复 8 遍），与步数上限无关。"""
        self.assertGreaterEqual(agent_mod._MAX_FORMAT_STALLS, 1)

    def test_nudges_are_still_bounded(self):
        settings = capacity.normalize_settings({})
        self.assertLessEqual(settings["max_nudges"], 20)
        self.assertLessEqual(settings["max_final_continuations"], 20)

    def test_repeat_protection_is_still_active(self):
        """同一 (工具, 参数) 重复调用必须仍被拒——步数归零后，这是仅剩的兜底之一。"""
        # 重复护栏不在 capacity 里，靠的是 Agent.run 里的 executed 集合。
        source = open(agent_mod.__file__, encoding="utf-8").read()
        self.assertIn("executed = set()", source)


class ApiDefaultsTests(unittest.TestCase):
    """接口层的默认值必须与 capacity 一致，否则页面显示 0、后端却是旧值。"""

    def test_the_request_model_defaults_match(self):
        from api import AgentCapacityReq
        req = AgentCapacityReq()
        for key in ("max_agents", "main_max_steps", "child_max_steps",
                    "task_token_budget"):
            self.assertEqual(getattr(req, key), 0, key)
            self.assertEqual(getattr(req, key), capacity.DEFAULTS[key], key)

    def test_zero_is_accepted_by_the_validators(self):
        from api import AgentCapacityReq
        req = AgentCapacityReq(max_agents=0, main_max_steps=0, child_max_steps=0,
                               task_token_budget=0)
        self.assertEqual(req.main_max_steps, 0)


class HelpTextTests(unittest.TestCase):
    """帮助文本不能直接把 0 印给用户——数字 0 不是自解释的。"""

    def test_it_says_unlimited_instead_of_zero(self):
        import inspect
        source = inspect.getsource(agent_mod._capacity_help_response)
        self.assertIn("不限", source)
        self.assertIn('unlimited = lambda', source,
                      "0 必须被翻译成「不限」再显示；光印一个 0 用户看不懂")


if __name__ == "__main__":
    unittest.main()
