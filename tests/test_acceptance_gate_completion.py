# -*- coding: utf-8 -*-
"""「完成」必须以**用户批准的验收条件**为准，不能只看当前任务表。

## 实测事故（`wf-bdb1a54286bf` / `wf-0bbd6c9edf6e`，形状完全一致）

用户批准的验收条件（`revision=1, approved_revision=1`）要求三个任务：

    accept-research    evidence=['task:research']
    accept-implement   evidence=['task:implement']
    accept-verify      evidence=['task:verify']

`research` 反思未通过 -> `implement`/`verify` 依次 blocked -> **重规划把整份计划换成
了单个 `research_retry`**（`replan_history.dropped` 还写着 0）：

    tasks   = [research_retry]
    results = {research: failed, implement: blocked, verify: blocked, research_retry: ok}
    status  = completed_unverified            <- 「已完成 · 未做独立验证」
    player.gd 含 'dash' = False                <- 用户要的改动一个没做

`report["ok"]` 只看**重规划之后**的任务表，那张表里唯一剩下的任务是 ok 的。

## 为什么这是完成判定的根问题

用户批准的是「三条验收条件」，不是「当前这张任务表」。重规划可以换掉任务表，
但它**不能**让用户批准过的验收条件失效——否则「完成」这个词不再对应任何用户
认可过的东西。

## 反向守卫（本文件的主要价值）

  · 全部必需条件都被 ok/deduped 任务支撑 -> 门必须开（实测 wf-80dc902c8cae、
    wf-23be9f18b3c6 四条全 ok，必须仍能报完成）
  · 没有必需条件 -> 门开（不拦没有验收条件的工作流）
  · `optional` 任务 / `required=False` 条件 -> 不算未满足
  · `evidence` 里非 `task:` 的写法（人工检查）-> 机器不代判
  · `deduped` 视同完成
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.game_workflow import (  # noqa: E402
    STATUS_COMPLETED, STATUS_COMPLETED_UNVERIFIED, STATUS_FAILED, review_status,
    settle_status, unmet_acceptance_items,
)


def _item(id_, task, required=True):
    return {"id": id_, "statement": "做 %s" % task, "method": "检查实际效果",
            "evidence": ["task:%s" % task], "required": required, "user_approved": True}


# ── 实测那一轮的形状 ──────────────────────────────────────────────
BAD_CONTRACT = {"revision": 1, "approved_revision": 1, "items": [
    _item("accept-research", "research"),
    _item("accept-implement", "implement"),
    _item("accept-verify", "verify"),
]}
BAD_RESULTS = {"results": {
    "research": {"status": "failed", "error": "子代理反思未通过"},
    "implement": {"status": "blocked", "error": "上游 research failed"},
    "verify": {"status": "blocked", "error": "上游 implement blocked"},
    "research_retry": {"status": "ok", "conclusion": "重做了调研"},
}}
GOOD_CONTRACT = {"revision": 1, "approved_revision": 1, "items": [
    _item("accept-read-player", "read-player"),
    _item("accept-read-enemy", "read-enemy"),
    _item("accept-verify-constants", "verify-constants"),
    _item("accept-final-summary", "final-summary"),
]}
GOOD_RESULTS = {"results": {k: {"status": "ok"} for k in
                             ("read-player", "read-enemy", "verify-constants", "final-summary")}}


class UnmetDetectionTests(unittest.TestCase):
    def test_the_real_run_is_unmet(self):
        """实测那次：`implement` / `verify` 是 blocked（从未被尝试）且被重规划删掉。"""
        unmet = unmet_acceptance_items(BAD_CONTRACT, BAD_RESULTS)
        self.assertEqual([i["id"] for i in unmet],
                         ["accept-implement", "accept-verify"],
                         "research 自己 failed 但**已被 research_retry 补救**；"
                         "implement/verify 是 blocked —— 用户批准的工作静悄悄消失了")

    def test_it_names_the_missing_tasks(self):
        unmet = unmet_acceptance_items(BAD_CONTRACT, BAD_RESULTS)
        self.assertEqual([i["tasks"] for i in unmet], [["implement"], ["verify"]])

    def test_a_failed_task_alone_does_not_trip_the_gate(self):
        """**反向守卫**：failed 不拦——补救它是重规划的正当用途。

        第一版把 failed 也算未满足，`tests/test_game_workflow.py::
        test_execute_uses_langgraph_for_single_wave_replan_and_context`
        （broken 失败 -> repair 成功 -> 断言 completed）立刻红。
        而且「有任务 failed」本来就已被 `all_ok` / `report["ok"]` 拦住，
        这道门不需要重复管。
        """
        contract = {"items": [_item("a", "broken")]}
        results = {"results": {"broken": {"status": "failed", "error": "可恢复错误"},
                               "repair": {"status": "ok"}}}
        self.assertEqual(unmet_acceptance_items(contract, results), [],
                         "失败后被补救成功，不该被验收门二次拦截")

    def test_task_absent_entirely_counts_as_unmet(self):
        """被重规划彻底删掉的任务连 result 都没有——那也是未满足。"""
        contract = {"items": [_item("a", "gone")]}
        self.assertEqual(unmet_acceptance_items(contract, {"results": {}})[0]["tasks"], ["gone"])

    def test_blocked_because_upstream_failed_counts(self):
        contract = {"items": [_item("a", "impl")]}
        results = {"results": {"impl": {"status": "blocked", "error": "上游 research failed"}}}
        self.assertEqual(unmet_acceptance_items(contract, results)[0]["tasks"], ["impl"])


class GateMustNotFalselyFailGoodRunsTests(unittest.TestCase):
    """**反向守卫**：好工作流必须仍能报完成。"""

    def test_the_earlier_four_ok_run_passes_the_gate(self):
        self.assertEqual(unmet_acceptance_items(GOOD_CONTRACT, GOOD_RESULTS), [])

    def test_a_recovered_replan_passes_the_gate(self):
        """`tests/test_game_workflow.py` 那个形状：broken 失败 -> repair 成功。"""
        contract = {"items": [_item("a", "broken")]}
        results = {"results": {"broken": {"status": "failed"}, "repair": {"status": "ok"}}}
        self.assertEqual(unmet_acceptance_items(contract, results), [])
        self.assertEqual(
            settle_status(agent_ok=True, project_ok=True, project_status="passed",
                          extra_ok=not unmet_acceptance_items(contract, results)),
            STATUS_COMPLETED,
            "补救成功就该能报完成——这是既有测试的断言")

    def test_it_still_reports_completed_unverified(self):
        self.assertEqual(
            settle_status(agent_ok=True, project_ok=False, project_status="unverified"),
            STATUS_COMPLETED_UNVERIFIED,
            "反向守卫：判据不该顺带影响这一支")

    def test_no_contract_at_all(self):
        self.assertEqual(unmet_acceptance_items(None, GOOD_RESULTS), [])
        self.assertEqual(unmet_acceptance_items({}, GOOD_RESULTS), [])
        self.assertEqual(unmet_acceptance_items({"items": []}, GOOD_RESULTS), [])

    def test_no_required_items(self):
        contract = {"items": [_item("opt", "read-player", required=False)]}
        self.assertEqual(unmet_acceptance_items(contract, {"results": {}}), [],
                         "全是可选条件时不该拦")

    def test_optional_task_status_counts_as_satisfied(self):
        contract = {"items": [_item("a", "flaky")]}
        results = {"results": {"flaky": {"status": "failed", "optional": True}}}
        self.assertEqual(unmet_acceptance_items(contract, results), [],
                         "optional 任务失败不该算必需条件未满足")

    def test_deduped_counts_as_done(self):
        contract = {"items": [_item("a", "dup")]}
        results = {"results": {"dup": {"status": "deduped"}}}
        self.assertEqual(unmet_acceptance_items(contract, results), [])

    def test_manual_evidence_is_not_judged_by_machine(self):
        """`人工检查` 这类证据由用户自己判断，机器代判就是把「需要你看一眼」
        误报成失败。"""
        contract = {"items": [{"id": "a", "required": True, "statement": "跑起来看看",
                               "evidence": ["人工检查", "artifact:shot.png"]}]}
        self.assertEqual(unmet_acceptance_items(contract, {"results": {}}), [])

    def test_mixed_required_and_optional_only_reports_required(self):
        contract = {"items": [_item("must", "must"), _item("maybe", "maybe", required=False)]}
        results = {"results": {"must": {"status": "ok"}, "maybe": {"status": "failed"}}}
        self.assertEqual([i["id"] for i in unmet_acceptance_items(contract, results)], [])


class ResultsShapeToleranceTests(unittest.TestCase):
    """`results` 有两种形状，都要认。"""

    def test_flat_shape(self):
        contract = {"items": [_item("a", "t1")]}
        self.assertEqual(unmet_acceptance_items(contract, {"t1": {"status": "ok"}}), [])

    def test_missing_results(self):
        contract = {"items": [_item("a", "t1")]}
        self.assertEqual(len(unmet_acceptance_items(contract, None)), 1)
        self.assertEqual(len(unmet_acceptance_items(contract, "垃圾输入")), 1)


class ComposedVerdictTests(unittest.TestCase):
    """验收门与三值结局组合起来的效果。"""

    def _verdict(self, extra_ok, contract, results):
        unmet = unmet_acceptance_items(contract, results)
        return settle_status(agent_ok=True, project_ok=False,
                             project_status="unverified", changed=False,
                             extra_ok=extra_ok and not unmet)

    def test_read_only_good_run_still_completes(self):
        self.assertEqual(self._verdict(True, GOOD_CONTRACT, GOOD_RESULTS),
                         STATUS_COMPLETED_UNVERIFIED)

    def test_dropped_required_tasks_block_completion(self):
        self.assertEqual(self._verdict(True, BAD_CONTRACT, BAD_RESULTS), STATUS_FAILED,
                         "implement/verify 是 blocked 且被重规划删掉，却报「已完成」"
                         "——这正是实测那次")

    def test_review_status_respects_the_gate_too(self):
        review = {"ok": True, "agent_ok": True,
                  "project": {"ok": True, "status": "passed"}}
        unmet = unmet_acceptance_items(BAD_CONTRACT, BAD_RESULTS)
        self.assertEqual(review_status(review, extra_ok=not unmet), STATUS_FAILED,
                         "复核全过也不能覆盖「已批准的验收条件无法满足」")


if __name__ == "__main__":
    unittest.main()