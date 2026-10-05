# -*- coding: utf-8 -*-
"""重规划不许丢掉**用户批准的必需任务**。

## 实测（wf-58a346c6cbec / wf-02f4a26bf38b，两轮真跑）

重规划**反复**把计划改小，每次都「成功」：

    attempt 1:  proposal 1 个任务 -> 丢掉 research/implement/verify
    attempt 2:  proposal 2 个任务 -> 又丢掉 research_retry

A 轮最终 tasks = [research_files_v2, research_godot_facts_v2]，原始 3 个必需任务
一个都没剩；B 轮 4 个必需任务全被丢掉。

实测数据里验收条件确实是用户批准过的：

    revision=1 approved_revision=1
    accept-research   required=True user_approved=True evidence=['task:research']
    accept-implement  required=True user_approved=True evidence=['task:implement']
    accept-verify     required=True user_approved=True evidence=['task:verify']

## 为什么选「放回去」而不是「拒绝这次重规划」

拒绝的话用户要的活儿一件没干，而这个 harness 的目的是**把活干完**。放回去守住的
是「用户批准过的范围不许被系统单方面缩小」，而重规划该做的补救（新加一个
research_retry）**照样生效**。

## 三个必须处理的坑

  1. 依赖悬空 -> 旧任务指向已丢弃 id 的 `depends_on` 改成 `context_from`
  2. 撞 max_subagents -> 只放回受保护的那几个，并写进历史（不能又变成静默丢弃）
  3. 打转 -> 兜底是现成的 `max_replans`

## 反向守卫（本文件的主要价值）

  · 合同未批准（`user_approved=False`）-> **不保护**，原样返回提案
  · 提案没丢任何受保护任务 -> 原样返回，**不**加重复任务
  · 只有人工检查证据 -> 不保护
  · 不在保护集里的旧任务 -> 不放回
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.game_workflow import (  # noqa: E402
    keep_required_tasks, required_task_ids,
)

APPROVED = {"revision": 1, "approved_revision": 1, "items": [
    {"id": "accept-research", "required": True, "user_approved": True,
     "evidence": ["task:research"], "statement": "调研"},
    {"id": "accept-implement", "required": True, "user_approved": True,
     "evidence": ["task:implement"], "statement": "实现"},
    {"id": "accept-verify", "required": True, "user_approved": True,
     "evidence": ["task:verify"], "statement": "验证"},
]}


class RequiredTaskIdsTests(unittest.TestCase):
    def test_it_reads_the_approved_required_tasks(self):
        self.assertEqual(required_task_ids(APPROVED),
                         {"research", "implement", "verify"})

    def test_unapproved_contract_protects_nothing(self):
        """合同未批准 -> 不保护。系统自己拟的草稿不该被当成用户认可的契约。"""
        draft = {"revision": 1, "items": [
            dict(it, user_approved=False) for it in APPROVED["items"]]}
        self.assertEqual(required_task_ids(draft), set())

    def test_optional_items_are_not_protected(self):
        contract = {"items": [{"id": "a", "required": False, "user_approved": True,
                               "evidence": ["task:opt"]}]}
        self.assertEqual(required_task_ids(contract), set())

    def test_manual_evidence_protects_nothing(self):
        contract = {"items": [{"id": "a", "required": True, "user_approved": True,
                               "evidence": ["人工检查", "artifact:x.png"]}]}
        self.assertEqual(required_task_ids(contract), set())

    def test_garbage_inputs(self):
        for bad in (None, {}, {"items": "不是列表"}, {"items": [None, 3]}):
            with self.subTest(bad=bad):
                self.assertEqual(required_task_ids(bad), set())


class ProtectedTasksArePutBackTests(unittest.TestCase):
    def _previous(self):
        return [
            {"id": "research", "role": "researcher", "task": "调研"},
            {"id": "implement", "role": "coder", "task": "实现",
             "depends_on": ["research"]},
            {"id": "verify", "role": "tester", "task": "验证",
             "depends_on": ["implement"]},
        ]

    def test_the_real_run_shape(self):
        """实测 attempt 1：提案只给了 research_retry。"""
        proposal = [{"id": "research_retry", "role": "researcher", "task": "重做调研"}]
        merged, restored = keep_required_tasks(
            self._previous(), proposal, {"research", "implement", "verify"})
        ids = [i["id"] for i in merged]
        self.assertEqual(set(restored), {"research", "implement", "verify"})
        self.assertIn("research_retry", ids, "重规划的补救必须照样生效")
        self.assertLessEqual(len(ids), len(set(ids)), "不能出现重复 id")

    def test_dangling_dependencies_become_context(self):
        """**坑 1**：`verify` 依赖 `implement`，若 implement 被丢弃那依赖就悬空。"""
        proposal = [{"id": "research_retry", "role": "researcher", "task": "重做调研"}]
        merged, _ = keep_required_tasks(
            self._previous(), proposal, {"research", "implement", "verify"})
        row = next(i for i in merged if i["id"] == "verify")
        self.assertEqual(row["depends_on"], [],
                         "指向不在计划里的 id 的依赖必须摘掉，否则调度器遇到悬空边")
        self.assertIn("implement", row["context_from"],
                      "而它作为跨波次上下文输入应该被保留——那正是依赖存在的意义")

    def test_kept_dependencies_stay(self):
        proposal = [{"id": "research_retry", "task": "重做"},
                    {"id": "implement", "task": "实现"}]
        merged, restored = keep_required_tasks(
            self._previous(), proposal, {"research", "implement", "verify"})
        row = next(i for i in merged if i["id"] == "verify")
        self.assertEqual(row["depends_on"], ["implement"], "implement 还在计划里，依赖应保留")
        self.assertEqual(restored, ["research", "verify"])


class NothingToProtectMeansNothingHappensTests(unittest.TestCase):
    """**反向守卫**：不该保护时完全不动提案。"""

    def _previous(self):
        return [{"id": "a", "task": "A"}, {"id": "b", "task": "B"}]

    def test_empty_protected_set(self):
        proposal = [{"id": "c", "task": "C"}]
        merged, restored = keep_required_tasks(self._previous(), proposal, set())
        self.assertEqual(merged, proposal)
        self.assertEqual(restored, [])

    def test_proposal_already_contains_everything_protected(self):
        """提案没丢任何受保护任务 -> **不**加重复任务。"""
        proposal = [{"id": "implement", "task": "实现"}, {"id": "verify", "task": "验证"}]
        merged, restored = keep_required_tasks(
            self._previous(), proposal, {"implement", "verify"})
        self.assertEqual(merged, proposal, "已经在计划里了，再放回一次就是重复")
        self.assertEqual(restored, [])

    def test_unprotected_old_tasks_are_not_restored(self):
        proposal = [{"id": "c", "task": "C"}]
        merged, restored = keep_required_tasks(self._previous(), proposal, {"b"})
        self.assertEqual([i["id"] for i in merged], ["b", "c"])
        self.assertEqual(restored, ["b"])

    def test_protected_task_absent_from_previous_plan(self):
        """受保护但上一版计划里已经没有 -> 无从放回，提案原样。"""
        proposal = [{"id": "c", "task": "C"}]
        merged, restored = keep_required_tasks([], proposal, {"ghost"})
        self.assertEqual(merged, proposal)
        self.assertEqual(restored, [])

    def test_restored_flag_is_set(self):
        """放回的任务要留痕，便于 trace 上分辨哪些是「系统加回来的」。"""
        # 第一版这里传了 protected={"implement"} 但 previous 里只有 a/b ——
        # 无从放回，next() 直接 StopIteration。是 fixture 错了，不是实现错。
        merged, _ = keep_required_tasks(
            [{"id": "implement", "task": "实现"}], [{"id": "c", "task": "C"}],
            {"implement"})
        row = next(i for i in merged if i["id"] == "implement")
        self.assertTrue(row.get("restored_after_replan"))


if __name__ == "__main__":
    unittest.main()