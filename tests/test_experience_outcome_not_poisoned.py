# -*- coding: utf-8 -*-
"""经验库不能把「完全成功的只读工作流」记成 `repeated-fail`。

## 实测（四次只读真跑落盘的 experience 事件，形状完全一致）

    {"kind": "experience", "recorded": false, "outcome": "repeated-fail"}

那一轮四任务全 ok、十项检查全 True、`evidence_verified`、引用不过 0 条 ——
**全程成功**。只因为 `state.status` 是 `failed`（`review.ok` 含 `project.ok`，
而它 `unverified`），就被标成「反复失败」。

## 为什么这条比前几个下游更要紧

`experience.recall_similar()` 会把经验库条目检索出来**指导后续规划**。
所以一次完全成功的分析一旦记进去，就成了**反面教材**。

目前 `recorded: false` 只是因为嵌入不可用静默降级（Ollama 没起）、库还是空的
—— **一旦嵌入可用就会真的写进去**。这是潜伏的数据投毒，不是没发生。

## 封闭取值集合

`experience._OUTCOMES = ("success", "fail-then-fixed", "repeated-fail")`，
第 91 行校验。所以不能新增第四个取值，只能映射到 `success`，并把保留意见
写进 decision 那一行：**标签不撒谎，披露也不丢**。

## 反向守卫

  · `agent_ok` 为假 -> 仍必须 `repeated-fail`
  · `project.status == "failed"`（独立测试真失败）-> 仍必须 `repeated-fail`
  · `project` 这一项整个缺失（未知）-> 仍必须 `repeated-fail`
  · decision 那行必须写出「未做独立验证」，不能只靠 outcome=success 蒙混
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.game_workflow import _experience_outcome  # noqa: E402


UNVERIFIED_REVIEW = {
    "ok": False, "agent_ok": True, "failures": [],
    "checks": {"has_result": True, "evidence_verified": True},
    "project": {"ok": False, "status": "unverified", "independent": True,
                "change_count": 0, "changes": [],
                "message": "试做区没有容器级命令隔离，不能作为独立测试通过的证据"},
}


class UnverifiedIsSuccessTests(unittest.TestCase):
    def test_the_real_run_is_not_recorded_as_a_failure(self):
        outcome, verdict = _experience_outcome(UNVERIFIED_REVIEW, "failed")
        self.assertEqual(outcome, "success",
                         "四任务全 ok 的只读跑被记成 repeated-fail = 反面教材")
        self.assertIn("未做独立验证", verdict,
                      "只写 outcome=success 而不披露验证缺口，是另一种粉饰")

    def test_a_genuinely_completed_run_still_says_passed(self):
        outcome, verdict = _experience_outcome(
            {"ok": True, "agent_ok": True,
             "project": {"ok": True, "status": "passed"}}, "completed")
        self.assertEqual(outcome, "success")
        self.assertEqual(verdict, "通过")
        self.assertNotIn("未做独立验证", verdict)


class RealFailuresStayFailuresTests(unittest.TestCase):
    """**反向守卫**：真的没过，就必须进反面教材。"""

    def test_agent_ok_false(self):
        outcome, _ = _experience_outcome(
            {"ok": False, "agent_ok": False,
             "project": {"ok": False, "status": "unverified"}}, "failed")
        self.assertEqual(outcome, "repeated-fail",
                         "内容复核没过时不能因为 project.status==unverified 就洗成 success")

    def test_project_status_failed(self):
        outcome, _ = _experience_outcome(
            {"ok": False, "agent_ok": True,
             "project": {"ok": False, "status": "failed"}}, "failed")
        self.assertEqual(outcome, "repeated-fail",
                         "独立测试真失败了，必须留作反面教材")

    def test_project_missing_is_treated_as_unknown(self):
        outcome, _ = _experience_outcome({"ok": False, "agent_ok": True}, "failed")
        self.assertEqual(outcome, "repeated-fail",
                         "没有 project 这一项属于未知，不能默认往好的方向洗")

    def test_review_missing_entirely(self):
        outcome, _ = _experience_outcome(None, "failed")
        self.assertEqual(outcome, "repeated-fail")


class OutcomeVocabularyIsRespectedTests(unittest.TestCase):
    def test_only_real_outcomes_are_used(self):
        """`_OUTCOMES` 是封闭集合（第 91 行校验），不能新增取值。"""
        from experience import _OUTCOMES
        cases = (
            (UNVERIFIED_REVIEW, "failed"),
            ({"ok": True, "agent_ok": True, "project": {"ok": True, "status": "passed"}},
             "completed"),
            ({"ok": False, "agent_ok": False,
              "project": {"ok": False, "status": "failed"}}, "failed"),
            ({"ok": False, "agent_ok": True}, "failed"),
            (None, "completed"),
        )
        for review, status in cases:
            with self.subTest(review=review):
                outcome, _ = _experience_outcome(review, status)
                self.assertIn(outcome, _OUTCOMES)


if __name__ == "__main__":
    unittest.main()