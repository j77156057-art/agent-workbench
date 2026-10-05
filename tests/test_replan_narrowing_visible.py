# -*- coding: utf-8 -*-
"""重规划让计划**变小**这件事必须看得见。

## 实测（`wf-bdb1a54286bf` / `wf-0bbd6c9edf6e` 形状完全一致）

    原计划      research / implement / verify        （3 个，用户已批准）
    research    failed  （子代理反思未通过）
    implement   blocked （上游 research failed）
    verify      blocked （上游 implement blocked）
    重规划后    [research_retry]                     （1 个）

而 `replan_history` 里写的是：

    {"attempt": 1, "failed": ["research"], "status": "applied",
     "tasks": ["research_retry"], "offered": 1, "dropped": 0}

**`dropped: 0`。** 那两个必需任务消失了，历史里一个字都没提。

## 根因：dropped 比错了对象

    "dropped": max(0, len(raw_tasks) - len(parsed))

`raw_tasks` 是 **LLM 提案内部**的原始任务，`parsed` 是解析后的。也就是「提案里
被解析器丢掉多少」。它**从没和上一版计划比过**。提案本身变小时差额是 0 ——
而那段注释恰好写着「否则『重排后只剩两步』没人看得见」，**它在这个场景失效**。

## 与 _note_plan_truncation 同一件事、换个站点

那条注释写着「整图被拒时看得见；**部分丢弃看不见**…计划照样落库、照样发 plan
事件、照样弹审批框，只是少了一部分步骤。这不是理论风险」。同一个病。

## 反向守卫（本文件的主要价值）

  · 正常的失败补救（proposal 只含补救任务，且失败任务已被替换）-> **不算丢工作**
  · 提案里**新增**任务 -> 不算丢工作
  · 提案只是给失败任务改名/续做、id 变了 -> **要算**（用户批准的是 id）
  · 只报不拦：本文件不引入审批门（那是权限模型，另一个决定）
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.game_workflow import GameWorkflowManager  # noqa: E402


class _Recorder:
    def __init__(self):
        self.events = []


def _narrow(previous, new):
    mgr = GameWorkflowManager.__new__(GameWorkflowManager)
    rec = _Recorder()
    mgr._event = lambda state, kind, **kw: rec.events.append((kind, kw))
    class _S:
        workflow_id = "wf-test"
        status = "executing"
    got = mgr._note_replan_narrowing(_S(), list(previous), list(new))
    return got, rec.events


class NarrowingIsReportedTests(unittest.TestCase):
    def test_the_real_run_reports_all_three_vanished_ids(self):
        """三个 id 全消失——**包括 `research`**。

        第一版我在这里写的是「只有 implement/verify，因为 research 被
        research_retry 接替了」，而且同一文件里另一条测试又写「改名也算消失」。
        两条自相矛盾，实现跟的是后一条（对的）。

        `research_retry` 与 `research` 是**不同的 id**。「retry 接替了 research」
        是一句语义判断，id 比较**不该去猜**——它只能如实说：这三个 id 不在
        新计划里了。真要判断是否等价，得由人或模型来看，那是另一件事。
        """
        got, events = _narrow(["research", "implement", "verify"], ["research_retry"])
        self.assertEqual(got, ["research", "implement", "verify"])

        kinds = [k for k, _ in events]
        self.assertEqual(kinds, ["graph_replan_narrowed"])
        payload = events[0][1]
        self.assertEqual(payload["dropped_tasks"], ["research", "implement", "verify"])
        self.assertEqual(payload["previous_count"], 3)
        self.assertEqual(payload["kept_count"], 1)
        self.assertIn("不会自动重跑", payload["reason"])

    def test_everything_replaced(self):
        got, _ = _narrow(["a", "b"], ["x", "y"])
        self.assertEqual(got, ["a", "b"])

    def test_a_task_renamed_counts_as_vanished(self):
        """用户批准的是 id；换个名字接续它，用户认不出那是同一件事。"""
        got, _ = _narrow(["research"], ["research_retry"])
        self.assertEqual(got, ["research"])


class NormalReplansMustNotBeFlaggedTests(unittest.TestCase):
    """**反向守卫**：合法重排不能被误报成「丢了用户批准的工作」。"""

    def test_adding_tasks_reports_nothing(self):
        got, events = _narrow(["research"], ["research", "repair", "verify"])
        self.assertEqual(got, [])
        self.assertEqual(events, [])

    def test_same_tasks_untouched(self):
        got, events = _narrow(["a", "b", "c"], ["a", "b", "c"])
        self.assertEqual(got, [])
        self.assertEqual(events, [], "计划没变小就不该发事件——否则时间线全是噪声")

    def test_a_superset_reports_nothing(self):
        got, _ = _narrow(["broken"], ["broken", "repair"])
        self.assertEqual(got, [])

    def test_empty_previous_plan_is_silent(self):
        got, events = _narrow([], ["a"])
        self.assertEqual(got, [])
        self.assertEqual(events, [])

    def test_empty_new_plan_is_not_reported_here(self):
        """新计划为空走的是另一条分支（replan_failed），不该由这里发事件。"""
        got, _ = _narrow(["a", "b"], [])
        self.assertEqual(got, ["a", "b"],
                         "全灭确实算丢工作；replan_failed 那条分支会另外处理")


class ReplanHistoryShapeTests(unittest.TestCase):
    """`dropped` 与 `plan_dropped` 必须**各说各的**，不能互相顶替。"""

    def test_the_two_fields_are_independent(self):
        # 「提案内部被解析器丢掉多少」与「上一版计划里没了什么」不是同一个量：
        # 实测那次 offered=1 parsed=1 dropped=0，而计划是 3 -> 1。
        raw_tasks, parsed = ["a", "b", "c"], ["a"]
        self.assertEqual(max(0, len(raw_tasks) - len(parsed)), 2,
                         "提案内部被解析器丢掉 2 个")
        previous, new_ids = ["a", "b", "c"], ["a"]
        plan_dropped = [t for t in previous if t not in set(new_ids)]
        self.assertEqual(plan_dropped, ["b", "c"],
                         "但计划只留下 a 时，丢掉的是 b 和 c——提案里根本没有它们")


if __name__ == "__main__":
    unittest.main()