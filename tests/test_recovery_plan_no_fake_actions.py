# -*- coding: utf-8 -*-
"""恢复计划不该凭空造出「不存在的下一步」。

## 实测（五次真跑形状完全一致）

`wf-f609ea8daf22` / `wf-fc3cf86c5eaf` / `wf-fd543f79c197` / `wf-e042489bf0fe` /
`wf-af6a2e4a9b2b`：

    四任务全 ok，failed_task_ids: []，uncertain_task_ids: []
    review.project.change_count = 0，changes = []，diff = ""
    project_checkpoint.file_count = 87        <- 全是**改动前就存在**的文件
    recovery.status = "required"
    recovery.summary = "执行结果未通过复核"
    options = [restore_project_checkpoint]     <- 一个文件都没改，却让用户回滚

用户看到的是「失败后的下一步 · 需要用户审核」，外加一个「恢复执行前项目快照」
按钮。那 87 个文件这轮**一个都没动过**，点下去只是把它们「恢复」一遍。

`state.status` 之所以是 `failed`，是因为 `review.ok` 里含
`review.project.ok`，而后者是 `unverified`——**试做区没有容器级命令隔离**。
那是一条正确的安全声明，本文件**不去碰它**，它照旧显示在复核区里。

## 这里修的是「恢复计划凭空造出不存在的动作」

## 反向守卫（这一族的刀必须留在该留的地方）

  · **有实际改动 + 有快照 -> 回滚入口必须仍在**（这是破坏性动作，不能被顺手藏掉）
  · 复核**没跑过**（`review` 为空/缺失）-> 视为「改动未知」-> **不抑制**
  · 有失败/阻塞任务 -> 「重试失败任务」照旧出现
  · 有不确定副作用 -> 「先确认不确定的副作用」照旧出现（且带 task_ids）
  · 什么都没发生 -> 不产出计划（`{}`），而不是编一个假面板
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.game_workflow import build_recovery_plan  # noqa: E402


def _state(*, failed=None, uncertain=None, change_count=None,
           changes=None, checkpoint=True, review=True, project_review=True):
    """构造一个最小工作流状态；默认 = 实测那一轮（只读、全 ok）。"""
    results = {}
    for tid in failed or []:
        results[tid] = {"status": "failed"}
    for tid in uncertain or []:
        # 「不确定副作用」不是靠 status 认的——`build_recovery_plan` 找的是
        # `error_kind == "idempotency_in_doubt"` 这类信号。第一版这里把它写成
        # status="ok"，于是「有不确定任务」这个前提根本没成立，测试是空跑的。
        results[tid] = {"status": "ok", "error_kind": "idempotency_in_doubt",
                        "error": "工具可能已执行但结果未落账（不确定执行）"}
    state = {
        "results": {"results": results},
        "project_checkpoint": {"id": "cp-1", "file_count": 87} if checkpoint else {},
    }
    if review:
        rv = {"ok": (change_count or 0) == 0 and not (changes or [])}
        if project_review:
            rv["agent_ok"] = True
            rv["project"] = {"ok": False, "status": "unverified",
                             "change_count": change_count or 0,
                             "changes": changes or [], "diff": ""}
        state["review"] = rv
    return state


class NothingHappenedTests(unittest.TestCase):
    """实测那一轮：没失败、没改动 -> 不产出恢复计划。"""

    def test_no_plan_at_all(self):
        plan = build_recovery_plan(_state(), error="执行结果未通过复核")
        self.assertEqual(plan, {},
                         "一个文件都没改、一个任务都没失败，却要求用户审核一个"
                         "「失败后的下一步」——这是编出来的动作")

    def test_change_count_zero_alone_is_enough(self):
        plan = build_recovery_plan(_state(change_count=0), error="x")
        self.assertEqual(plan, {})

    def test_changes_list_alone_is_enough(self):
        plan = build_recovery_plan(_state(change_count=0, changes=[{"path": "a.py"}]),
                                   error="x")
        self.assertNotEqual(plan, {}, "changes 非空说明有改动，不该抑制")


class RollbackStillOfferedWhenThereAreChangesTests(unittest.TestCase):
    """**反向守卫**：回滚是破坏性动作的前置按钮，有改动时必须仍在。"""

    def _rollback(self, **kw):
        plan = build_recovery_plan(_state(**kw), error="x")
        return [o for o in plan.get("options") or [] if o.get("action") == "rollback"]

    def test_rollback_is_offered_when_files_changed(self):
        found = self._rollback(change_count=3)
        self.assertTrue(found,
                        "有 3 个文件改动却把回滚入口藏了——用户就再也退不回去了")

    def test_rollback_is_offered_when_review_never_ran(self):
        """崩溃恢复路径上 review 可能还是空的：未知不等于没改动。"""
        found = self._rollback(review=False)
        self.assertTrue(found,
                        "复核没跑过时不能假定没改动——否则正好藏掉用户需要的回滚")

    def test_rollback_is_offered_when_project_review_absent(self):
        found = self._rollback(project_review=False)
        self.assertTrue(found, "project 复核这一项不存在时属于未知，不该假定零改动")

    def test_no_rollback_without_a_checkpoint(self):
        found = self._rollback(change_count=3, checkpoint=False)
        self.assertFalse(found, "没有快照就没有可恢复的东西")


class RealFailuresStillGetTheirOptionsTests(unittest.TestCase):
    """**反向守卫**：真有失败/不确定时，选项一个都不能少。"""

    def test_retry_failed_tasks_still_offered(self):
        plan = build_recovery_plan(_state(failed=["t1"], change_count=0), error="x")
        ids = [o.get("id") for o in plan.get("options") or []]
        self.assertIn("retry_failed_tasks", ids)
        self.assertEqual(plan.get("status"), "required")
        retry = next(o for o in plan["options"] if o["id"] == "retry_failed_tasks")
        self.assertEqual(retry["task_ids"], ["t1"])

    def test_uncertain_side_effect_still_offered(self):
        plan = build_recovery_plan(_state(uncertain=["t2"], change_count=0), error="x")
        ids = [o.get("id") for o in plan.get("options") or []]
        self.assertIn("inspect_uncertain_side_effect", ids)
        self.assertEqual(plan["evidence"]["uncertain_task_ids"], ["t2"])

    def test_inspect_evidence_fallback_when_only_a_checkpoint_differs(self):
        """有改动但没失败 -> 至少有回滚；若快照不存在则退回「查看证据」。"""
        plan = build_recovery_plan(_state(change_count=2, checkpoint=False), error="x")
        ids = [o.get("id") for o in plan.get("options") or []]
        self.assertEqual(ids, ["inspect_failure_evidence"])


if __name__ == "__main__":
    unittest.main()