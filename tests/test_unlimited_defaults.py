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

    def test_all_seven_limits_default_to_zero(self):
        """2026-10：纠偏/续写也并入"默认不限"。

        此前它们刻意留成 2，理由是"纠偏防的是同一提示重复 8 遍"。但那条风险
        已由 `_MAX_FORMAT_STALLS`（格式连续失守即早退，见 agent.py:1980）、
        连续失败熔断、用户取消、截止时间各自兜住，不需要"只给 2 次"这种
        粗粒度上限兼任。长任务被 2 次纠偏掐断的代价更大。
        """
        for key in ("max_agents", "main_max_steps", "child_max_steps",
                    "max_nudges", "max_final_continuations",
                    "task_token_budget"):
            self.assertEqual(capacity.DEFAULTS[key], 0,
                             "%s 的默认值应是 0（不限）" % key)

    def test_nudge_limits_are_unlimited_by_default_but_tunable(self):
        """默认不限；用户填正数时按用户的值生效，不被静默夹回。"""
        self.assertEqual(capacity.DEFAULTS["max_nudges"], 0)
        self.assertEqual(capacity.DEFAULTS["max_final_continuations"], 0)
        self.assertEqual(capacity.normalize_settings({"max_nudges": 5})["max_nudges"], 5)
        self.assertEqual(
            capacity.normalize_settings({"max_final_continuations": 7})["max_final_continuations"], 7)

    def test_zero_survives_normalization(self):
        """实测踩过的坑：校验里写成 `if x < 0 …: x = DEFAULTS[x]`，而当时 DEFAULTS
        是 8——用户填 0 会被"修正"回 8，看着改了却没生效。"""
        result = capacity.normalize_settings({
            "max_agents": 0, "main_max_steps": 0, "child_max_steps": 0,
            "max_nudges": 0, "max_final_continuations": 0,
            "task_token_budget": 0})
        for key in ("max_agents", "main_max_steps", "child_max_steps",
                    "max_nudges", "max_final_continuations",
                    "task_token_budget"):
            self.assertEqual(result[key], 0, key)

    def test_a_positive_nudge_limit_above_twenty_is_kept(self):
        """上界曾是 20，于是"想设 50 次纠偏"会被改回 DEFAULTS(=0, 不限) —— 
        改了个大数反而变成不限，行为不可预期。现在与 main_max_steps 一样放到 1000。"""
        self.assertEqual(capacity.normalize_settings({"max_nudges": 50})["max_nudges"], 50)
        self.assertEqual(
            capacity.normalize_settings({"max_final_continuations": 50})["max_final_continuations"], 50)

    def test_a_negative_value_is_rejected_not_clamped_to_unlimited(self):
        """负值 -> raise（原来是 clamp 到 0）。

        ⚠ 原断言是 `result["main_max_steps"] == 0`。而 0 在这套语义里是
        **不限** —— 那等于「手滑打负号 -> 拿到最宽松配置」，且无提示。
        方法名一直叫 `..._is_still_rejected`，旧实现做的却是 clamp，
        名实不符。D 方案落定后 raise，这个名字才副实。
        """
        with self.assertRaises(ValueError):
            capacity.normalize_settings({"main_max_steps": -1})

    def test_an_out_of_range_value_is_still_clamped(self):
        """超上界 -> 夹到上界（不再回落 DEFAULTS）。

        ⚠ 这条原断言「99999 -> DEFAULTS」。DEFAULTS 全是 0（不限）之后，
        那个行为变成了坑：**想收紧的人拿到最宽松的值**。方法名一直叫
        clamped，旧实现做的却是 fall back，名实不符 —— 现在名副其实。
        """
        self.assertEqual(capacity.normalize_settings({"main_max_steps": 99999})["main_max_steps"],
                         1000)
        self.assertEqual(capacity.normalize_settings({"max_agents": 999})["max_agents"],
                         64)

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

    def test_out_of_range_nudges_clamp_to_the_cap_not_to_unlimited(self):
        """超上界 -> 夹到 1000，**不是**变成 0（不限）。

        ⚠ 这条原名 `..._fall_back_to_unlimited`，原作者有意让越界变不限，
        理由写的是「不是回落成某个正数上限」。那个顾虑是对的（回落成正数
        上限确实糟），但结论选错了：DEFAULTS 改成 0 之后，「回落」就是
        「不限」，于是填大数的人拿到不限 —— 比回落成正数更糟。

        保留原顾虑、修正结论：**夹到上界**既不回落成正数，也不会变成不限。
        """
        settings = capacity.normalize_settings({"max_nudges": 99999})
        self.assertEqual(settings["max_nudges"], 1000)
        self.assertNotEqual(settings["max_nudges"], 0)

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
                    "max_nudges", "max_final_continuations",
                    "task_token_budget"):
            self.assertEqual(getattr(req, key, 0), 0, key)
            self.assertEqual(getattr(req, key, 0), capacity.DEFAULTS[key], key)

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


class EmptyResponseAttributionTests(unittest.TestCase):
    """空响应（outcome=empty）的归因必须**二选一**，不能先甩锅上游再补免责。

    真实故障链（godot_sample 定位扣血逻辑）：`read_file` 连续 5 次「文件不存在」
    ——根因是参数语法（`start:/end:` 写成行内不被识别）——模型拿不到任何信息后
    放弃作答返回空。此时若正文首句仍写「上游返回了空内容，直接重试通常就能拿到」，
    用户会去反复重试，而真正该做的是修工具调用参数。**重试永远修不好参数**。
    """

    def _empty_branch_source(self):
        """截出 `elif not acc.strip():` 这一支的源码。

        注意：`elif not acc.strip():` 在 `_run` 里出现 **两次**（nudge 段也有一个
        同名的），所以不能拿它当唯一起点——否则切到的是上面那段 nudge 逻辑，
        测试会对着错误的代码断言（本文件第一版就踩了这个）。用
        `turn.outcome = "empty"` 作锚点：它在整个 `_run` 里只出现一次。
        """
        import inspect
        src = inspect.getsource(agent_mod.Agent._run)
        anchor = src.index('turn.outcome = "empty"')
        start = src.rindex("elif not acc.strip():", 0, anchor)
        end = src.index("\n            else:", anchor)
        return src[start:end]

    def test_the_two_attributions_are_mutually_exclusive(self):
        blk = self._empty_branch_source()
        self.assertIn("if fail_total:", blk,
                      "必须按「本轮有没有工具失败」分叉，不能只有一条文案")
        self.assertEqual(blk.count("final_text = ("), 2,
                         "两条分支各绑一个 final_text；少一条说明有一支没赋值")

    def test_tool_failure_branch_does_not_tell_the_user_to_retry(self):
        blk = self._empty_branch_source()
        tool_arm = blk[blk.index("if fail_total:"):]
        self.assertIn("大概率无效", tool_arm,
                      "工具失败分支必须明确说「单纯重试大概率无效」")
        self.assertNotIn("直接重试通常就能拿到", tool_arm,
                         "工具失败时不得再喊重试——那会掩盖真正的工具参数问题")

    def test_no_tool_failure_branch_still_explains_transient_upstream(self):
        blk = self._empty_branch_source()
        else_arm = blk[blk.index("else:"):]
        self.assertIn("**没有**工具失败记录", else_arm,
                      "无工具失败时要说清楚排除了工具这一侧")
        self.assertIn("瞬时故障", else_arm,
                      "此时上游瞬时故障才是合理假设")

    def test_the_old_blanket_attribution_is_gone(self):
        blk = self._empty_branch_source()
        self.assertNotIn("这多半不是你的问题写得不清楚", blk,
                         "旧的无条件甩锅文案必须被删干净，不能留在任一分支里")


if __name__ == "__main__":
    unittest.main()
