# -*- coding: utf-8 -*-
"""工作流状态改成**三值**结局：`completed` / `completed_unverified` / `failed`。

## 为什么

实测（`wf-80dc902c8cae` 等连续多次只读真跑）：四任务全 ok、十项检查全 True、
`evidence_verified`、引用不过 0 条、`change_count = 0`，而 `status` 一直是
`failed`——唯一原因是 `review.ok = agent_ok and project.ok` 而
`project.status == "unverified"`（试做区没有容器级命令隔离）。

那个声明是**正确的安全声明**，不该被改弱；但压成同一个布尔会让下游要么谎报
失败（已修的四处消费者），要么**整块功能不启动**（`evaluate_workflow` /
`skill_candidate` 要求 `status == "completed"`，所以只读工作流的质量评估从来没跑过）。

所以给后端一个能表达「完成了但没独立验证」的结局。

## 漏改的代价不是「显示不对」，是**死锁**

终态判定漏掉 `completed_unverified` →
`is_terminal()` 返 False → `active_for_project()` 认为工作流还活着 →
**下一次 `start()` 被互斥拒绝**。所以本文件把「终态必须含新值」钉死，
并且要求所有终态判断走 `is_terminal_status()` 而不是写字面量集合。

## 反向守卫（本文件的主要价值）

  · `project.status == "failed"`（独立测试真失败）-> 仍必须 `failed`
  · `agent_ok` 为假 -> 仍必须 `failed`
  · 执行本身没成（`extra_ok` 假）-> 仍必须 `failed`
  · 终态集合**必须**含 `completed_unverified`（漏了就是死锁）
  · 取消终态性必须真的生效（`completed_unverified` 不算活动态）
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.game_workflow import (  # noqa: E402
    STATUS_COMPLETED, STATUS_COMPLETED_UNVERIFIED, STATUS_FAILED,
    TERMINAL_STATUSES, is_terminal_status, is_successful_status, review_status,
    settle_status,
)


class SettleStatusTests(unittest.TestCase):
    def test_the_real_run_shape(self):
        """实测那一轮：内容过了、独立验证缺失 -> completed_unverified。"""
        self.assertEqual(
            settle_status(agent_ok=True, project_ok=False, project_status="unverified"),
            STATUS_COMPLETED_UNVERIFIED)

    def test_everything_passed(self):
        self.assertEqual(
            settle_status(agent_ok=True, project_ok=True, project_status="passed"),
            STATUS_COMPLETED)

    def test_no_project_review_at_all_counts_as_completed(self):
        """没有试做区（`local_mode`）时 project_ok 由调用方给 True。"""
        self.assertEqual(
            settle_status(agent_ok=True, project_ok=True, project_status="local_mode"),
            STATUS_COMPLETED)


class RealFailuresStayFailedTests(unittest.TestCase):
    """**反向守卫**：真的没过，绝不能被洗成「完成」。"""

    def test_project_status_failed_is_a_real_failure(self):
        """`independent_review` 里 ok=False 的另一个取值就是真失败。"""
        self.assertEqual(
            settle_status(agent_ok=True, project_ok=False, project_status="failed"),
            STATUS_FAILED,
            "独立测试真失败了，绝不能因为「ok 是 False」就一起放行")

    def test_agent_ok_false(self):
        self.assertEqual(
            settle_status(agent_ok=False, project_ok=False, project_status="unverified"),
            STATUS_FAILED,
            "内容复核没过时，不能靠 project.status==unverified 洗成完成")

    def test_execution_itself_failed(self):
        self.assertEqual(
            settle_status(agent_ok=True, project_ok=True, project_status="passed",
                          extra_ok=False),
            STATUS_FAILED,
            "执行本身没成时，无论复核结论如何都是失败")

    def test_unknown_project_status_is_failed(self):
        for status in ("", "weird", "passed_but_unclear"):
            with self.subTest(status=status):
                self.assertEqual(
                    settle_status(agent_ok=True, project_ok=False, project_status=status),
                    STATUS_FAILED,
                    "只放行 unverified 一种；未知取值一律按失败处理")

    def test_a_workflow_that_changed_files_is_never_unverified(self):
        """**既有安全规则，第一版漏了，被既有测试抓住。**

        `tests/test_enterprise_sandbox_review.py::test_workflow_cannot_complete_
        when_independent_review_is_unverified`：enterprise 模式 + stage_enabled，
        runner 真的写了文件，独立复核 `unverified` -> 必须是 `failed`。

        改动过东西、独立验证又没做成，口头说「完成了」是不成立的；
        而 `24c845c` 的原作者写过「一次纯只读侦察（change_count=0）本来就没
        打算跑测试」——所以这个结局本来就只属于只读任务。
        """
        self.assertEqual(
            settle_status(agent_ok=True, project_ok=False,
                          project_status="unverified", changed=True),
            STATUS_FAILED,
            "改过项目的 workflow 缺独立验证时不得报完成")

    def test_changed_files_win_even_when_project_ok(self):
        """改动本身不判失败——独立验证通过时照样 completed。"""
        self.assertEqual(
            settle_status(agent_ok=True, project_ok=True, project_status="passed",
                          changed=True),
            STATUS_COMPLETED)


class ReviewStatusFallbackTests(unittest.TestCase):
    """没有 project_root 时后端**不存** agent_ok / project，读取器必须回退。"""

    def _review(self):
        from agent_runtime.game_workflow import review_status
        return review_status

    def test_no_project_review_keeps_the_plain_verdict(self):
        """第一版直接 bool(None) -> False，把本来 completed 的全判成 failed
        （实测 10 条测试同时红：`'failed' != 'completed'`）。"""
        self.assertEqual(self._review()({"ok": True}), STATUS_COMPLETED)

    def test_no_project_review_and_not_ok(self):
        self.assertEqual(self._review()({"ok": False}), STATUS_FAILED)

    def test_unverified_read_only(self):
        review = {"ok": False, "agent_ok": True,
                  "project": {"ok": False, "status": "unverified",
                              "change_count": 0, "changes": []}}
        self.assertEqual(self._review()(review), STATUS_COMPLETED_UNVERIFIED)

    def test_unverified_but_changed_stays_failed(self):
        review = {"ok": False, "agent_ok": True,
                  "project": {"ok": False, "status": "unverified",
                              "change_count": 3, "changes": [{"path": "a.py"}]}}
        self.assertEqual(self._review()(review), STATUS_FAILED)

    def test_extra_ok_false_overrides_everything(self):
        review = {"ok": True, "agent_ok": True,
                  "project": {"ok": True, "status": "passed"}}
        self.assertEqual(self._review()(review, extra_ok=False), STATUS_FAILED)

    def test_review_is_none(self):
        self.assertEqual(self._review()(None), STATUS_FAILED)


class TerminalStatusSetTests(unittest.TestCase):
    """**漏了就是死锁**——本组是这个改动里最重要的守卫。"""

    def test_the_new_value_is_terminal(self):
        self.assertIn(STATUS_COMPLETED_UNVERIFIED, TERMINAL_STATUSES)
        self.assertTrue(is_terminal_status(STATUS_COMPLETED_UNVERIFIED),
                        "漏掉它 -> is_terminal() 返 False -> active_for_project 认为"
                        "工作流还活着 -> 下一次 start() 被互斥拒绝 = 死锁")

    def test_the_new_value_is_not_active(self):
        self.assertFalse(
            any(is_terminal_status(s) is False
                for s in (STATUS_COMPLETED, STATUS_COMPLETED_UNVERIFIED,
                          STATUS_FAILED, "interrupted")),
            "四个终态都必须真的终结")

    def test_non_terminal_values(self):
        for status in ("", None, "preparing", "executing", "awaiting_approval",
                       "planned", "awaiting_choice", "reviewing", "replanning"):
            with self.subTest(status=status):
                self.assertFalse(is_terminal_status(status))
                self.assertFalse(is_successful_status(status))


class SuccessfulStatusTests(unittest.TestCase):
    def test_both_success_values(self):
        self.assertTrue(is_successful_status(STATUS_COMPLETED))
        self.assertTrue(is_successful_status(STATUS_COMPLETED_UNVERIFIED),
                        "完成了就是完成了；缺的只是独立验证")

    def test_failed_is_not_successful(self):
        self.assertFalse(is_successful_status(STATUS_FAILED))
        self.assertFalse(is_successful_status("interrupted"))


class TerminalSetHasOneSourceOfTruthTests(unittest.TestCase):
    def test_the_class_alias_points_at_the_same_object(self):
        from agent_runtime.game_workflow import GameWorkflowManager
        self.assertEqual(
            set(GameWorkflowManager._TERMINAL_STATUSES), set(TERMINAL_STATUSES),
            "老引用点必须与唯一真相源一致，否则两处会漂移")


if __name__ == "__main__":
    unittest.main()