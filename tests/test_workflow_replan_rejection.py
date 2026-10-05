# -*- coding: utf-8 -*-
"""「重规划没采纳任何东西」这一族：拒绝分支本身必须是**全路径**的。

## 这一族在防什么

`replan_node` 里那条「拒绝」分支要读好几个只在 `try` 里赋值的局部变量
（`proposal_map` / `error` / `known_results` / `max_subagents` / `delta`）。
于是**任何**在赋值之前抛出的异常——重规划调用炸了、提案规范化炸了——都会让这条
分支**自己先崩**：`UnboundLocalError`，整个图节点挂掉，工作流变成一个没人能
解释的失败。

实测被探针 `.tmp/probe_rejected_tasks.py` 逮到，连撞三次（`proposal_map`
→ `error` → `max_subagents`），每次补一个才走到下一个。这正是「探针优于断言」
的例子：单测当时全绿，因为**没有一个用例让重规划抛异常**。

## 第二件事：这条分支还会抹掉计划

它以前回 `"tasks": []`。图随后落到 `fail`、计划不会再被派发，所以「派发」上
看不出问题；但 `graph_state` 是**落盘的审计记录**，里面同时躺着这批任务的
`results` 和一份空计划——读的人（或读状态的工具/测试）会以为「压根没有任务」。

真跑实测（`wf-17aae90e34c5`）：`graph.tasks=[]` 而 `state.tasks` 有 3 个、
`graph.results` 有 3 条。

## 第三件事：异常不能谎报成「提案无效」

`replan_error` 以前对「重规划调用抛异常」和「模型给了空提案」报同一句话。
后者引导用户去检查提案怎么写，而前者的真因是服务端/网络/解析炸了——
改提示词一点用都没有。
"""
import unittest

from agent_runtime.game_workflow import (
    STATUS_FAILED,
    GameWorkflowManager,
    WorkflowPolicy,
    normalize_replan_proposal,
    replan_reject_reason,
)

PLANNED = [
    {"id": "alpha", "role": "coder", "task": "第一件事"},
    {"id": "beta", "role": "tester", "task": "第二件事", "depends_on": ["alpha"]},
]


def _boom(*_args, **_kwargs):
    raise RuntimeError("重规划服务不可用")


class RejectReasonTests(unittest.TestCase):
    """判据是纯函数，正反向都能穷举。"""

    def test_an_empty_proposal_says_empty(self):
        self.assertEqual(replan_reject_reason({}, None, 0, 0), "empty_proposal")

    def test_reverse_guard_an_exception_must_not_be_reported_as_an_empty_proposal(self):
        """**反向守卫**：过程炸了不能报「提案无效」。

        这条最容易被"顺手统一"掉——反正都是「没采纳」。但两种原因引导用户
        做的事完全不同。
        """
        detail = replan_reject_reason({}, None, 0, 0, error="RuntimeError")
        self.assertEqual(detail, "replan_error(RuntimeError)")
        self.assertNotIn("empty_proposal", detail)

    def test_reverse_guard_an_exception_outranks_every_other_explanation(self):
        """**反向守卫**：异常名必须排在判据最前面。

        即使提案同时是空的、容量也用尽了——过程没跑完就是没跑完，
        报「容量用尽」会把用户引到错误的方向。
        """
        self.assertEqual(
            replan_reject_reason({}, None, 4, 4, error="TimeoutError"),
            "replan_error(TimeoutError)")

    def test_the_capacity_exhaustion_label_still_names_its_numbers(self):
        self.assertEqual(replan_reject_reason({"add": ["x"]}, None, 4, 4),
                         "subagent_capacity_exhausted(4/4)")

    def test_junk_proposals_do_not_raise(self):
        for bad in (None, [], {}, "x", 0):
            self.assertIsInstance(replan_reject_reason(bad, None, 0, 0), str)
            self.assertIsInstance(normalize_replan_proposal(bad), dict)


class RejectedReplanBranchTests(unittest.TestCase):
    """真跑工作流：拒绝分支不许崩、不许抹计划、不许谎报。"""

    def setUp(self):
        import tempfile

        self.manager = GameWorkflowManager(tempfile.mkdtemp())

    def tearDown(self):
        self.manager.close()

    def _run(self, replanner):
        state = self.manager.start(
            "重规划被拒", policy=WorkflowPolicy(approval_mode="high", max_replans=1,
                                              max_steps=6))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, PLANNED)

        def runner(task, context):
            return {"status": "failed", "conclusion": "诊断", "steps": 1}

        return self.manager.execute(wid, runner, replanner=replanner)

    def _assert_plan_survived(self, done, title):
        """被拒时图状态里必须还看得到计划。"""
        graph_tasks = [str(t.get("id")) for t in
                       ((done.get("graph_state") or {}).get("tasks") or [])]
        self.assertEqual(graph_tasks, ["alpha", "beta"], title)
        self.assertEqual(sorted((done.get("graph_state") or {}).get("results") or {}),
                         ["alpha", "beta"], title)

    def test_a_raising_replanner_degrades_instead_of_crashing(self):
        """这条以前直接 `UnboundLocalError` 崩掉整个图节点。"""
        done = self._run(_boom)

        self.assertEqual(done["status"], STATUS_FAILED)
        self.assertEqual(done["graph_state"]["event"], "fail")
        self._assert_plan_survived(done, "抛异常时计划必须还在")

    def test_reverse_guard_the_exception_name_is_not_lost(self):
        """**反向守卫**：不能把异常统一成「提案无效」。"""
        done = self._run(_boom)

        detail = str(done["graph_state"].get("replan_error") or "")
        self.assertIn("RuntimeError", detail)
        self.assertNotIn("empty_proposal", detail)

    def test_an_empty_proposal_is_still_reported_as_empty(self):
        """对照组：这条**不该**被异常那条判据吞掉。"""
        done = self._run(lambda failed, results, attempt: [])

        self.assertEqual(done["graph_state"]["replan_error"], "empty_proposal")
        self._assert_plan_survived(done, "空提案时计划必须还在")

    def test_reverse_guard_a_drop_of_executed_tasks_is_refused_and_the_plan_survives(self):
        """**反向守卫**：想 drop 已经跑过的任务会被安全边界拒绝，计划还得留着。

        这里断言的是 `apply_proposal` 自己的「已执行任务不可删改」边界，
        **不是**「用户批准的必需任务不许 drop」——后者要求验收条件里
        `user_approved` 且 `evidence` 指着 `task:<id>`，本文件的 `PLANNED` 没有，
        于是 `_required_task_ids_for_graph` 返回空集、`refused_drop` 也是空的。

        那一族归 `tests/test_replan_keeps_approved_tasks.py`（16 条单测）管，
        不在这里重复——**不同族不混在一次改动里**。顺带记一条**尚未覆盖**的：
        `replan_history[-1]["refused_drop"]` 这个字段在**真跑层面**没有任何断言，
        只有 `required_task_ids` / `keep_required_tasks` 的单测覆盖。
        """
        done = self._run(lambda failed, results, attempt: {"drop": ["alpha", "beta"]})

        self.assertEqual(done["graph_state"]["replan_error"],
                         "all_proposals_rejected_by_safety_boundary")
        self._assert_plan_survived(done, "拒绝 drop 后计划必须还在")
        entry = (done["graph_state"]["replan_history"] or [])[-1]
        self.assertEqual(entry["status"], "rejected")
        self.assertEqual(entry["offered"], {"drop": ["alpha", "beta"]})

    def test_reverse_guard_the_rejection_is_recorded_with_the_raw_proposal(self):
        """**反向守卫**：拒绝也要留痕——包括模型**原本说了什么**。

        只记「被拒了」而不记「说了什么」，用户看到的是一句无法追问的拒绝。
        """
        done = self._run(lambda failed, results, attempt: {"drop": ["alpha"]})

        entry = (done["graph_state"]["replan_history"] or [])[-1]
        self.assertEqual(entry["status"], "rejected")
        self.assertIn("alpha", str(entry.get("proposal_raw") or ""))

    def test_a_valid_proposal_is_unaffected(self):
        """对照组：合法提案仍然被采纳，计划里多出补救任务。"""
        done = self._run(lambda failed, results, attempt: [
            {"id": "fixup", "role": "coder", "task": "补救", "depends_on": ["alpha"]}])

        history = done["graph_state"]["replan_history"]
        self.assertEqual(history[-1]["status"], "applied")
        self.assertEqual(history[-1]["adopted"]["added"], ["fixup"])
        self.assertEqual(
            [str(t.get("id")) for t in (done["graph_state"]["tasks"] or [])],
            ["alpha", "beta", "fixup"])
        self.assertEqual(done["graph_state"]["replan_error"], "")


if __name__ == "__main__":
    unittest.main()