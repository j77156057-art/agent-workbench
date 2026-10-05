# -*- coding: utf-8 -*-
"""「失败被补救成功」这一族。

用户定下的口径：

    失败可以先报出来、标红，下一条补一句补救成功，失败的历史留着给用户看；
    但既然已经补救成功了，**程序那里可以通过**。

这一族的核心风险是**判据被偷偷放宽**——一旦「补救成功」只靠模型自述、
或者只靠「有个后续任务成功了」，那模型随便追加一个无关的成功任务就能把
真失败洗白。所以下面每条正面用例都配一条反向守卫。
"""
import unittest

from agent_runtime.game_workflow import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    GameWorkflowManager,
    WorkflowPolicy,
    remediated_task_ids,
)


def _history(helper, dead, *, status="applied"):
    """一条「重规划引入了 helper，且 helper 声明过对 dead 的依赖」的记录。"""
    return [{
        "status": status,
        "adopted": {"added": [helper], "dropped": [], "replaced": []},
        "demoted_deps": [{"task": helper, "demoted_deps": [dead]}],
    }]


class RemediatedTaskIdsTests(unittest.TestCase):
    """判据本身。它是纯函数，所以正面/反向都能穷举，不需要跑工作流。"""

    def test_a_failed_task_with_a_declared_successful_helper_counts(self):
        rows = {"broken": {"status": "failed"}, "repair": {"status": "ok"}}
        self.assertEqual(remediated_task_ids(_history("repair", "broken"), {"results": rows}),
                         {"broken": "repair"})

    def test_reverse_guard_an_unrelated_successful_task_does_not_count(self):
        """**反向守卫**：补救任务没声明过对失败任务的依赖 -> 不算补救。

        这是最容易被放宽的一条：只要「后面有个任务成功了」就放行，模型加个
        无关的成功任务就能把真失败洗白。这里必须咬住。
        """
        history = [{
            "status": "applied",
            "adopted": {"added": ["unrelated"], "dropped": [], "replaced": []},
            "demoted_deps": [],          # 没有任何依赖声明
        }]
        rows = {"broken": {"status": "failed"}, "unrelated": {"status": "ok"}}
        self.assertEqual(remediated_task_ids(history, {"results": rows}), {})

    def test_reverse_guard_a_helper_that_itself_failed_does_not_count(self):
        """**反向守卫**：补救任务自己也失败 -> 不算补救成功。"""
        rows = {"broken": {"status": "failed"}, "repair": {"status": "failed"}}
        self.assertEqual(remediated_task_ids(_history("repair", "broken"), {"results": rows}), {})

    def test_reverse_guard_a_helper_that_never_ran_does_not_count(self):
        """**反向守卫**：补救任务被派出去但没有结果 -> 不算（不能假设它成功）。"""
        rows = {"broken": {"status": "failed"}, "repair": {}}
        self.assertEqual(remediated_task_ids(_history("repair", "broken"), {"results": rows}), {})

    def test_reverse_guard_a_rejected_replan_does_not_count(self):
        """**反向守卫**：重规划被**拒绝/未采纳**时不产生补救关系。

        模型提了个补救任务但被契约拒了（超上限 / 丢必需任务），那次提议不算数。
        """
        rows = {"broken": {"status": "failed"}, "repair": {"status": "ok"}}
        self.assertEqual(
            remediated_task_ids(_history("repair", "broken", status="rejected"),
                               {"results": rows}), {})

    def test_a_task_that_never_failed_is_not_reported(self):
        """没有失败就没有补救要说——否则界面会给正常任务挂上补救标记。"""
        rows = {"fine": {"status": "ok"}, "repair": {"status": "ok"}}
        self.assertEqual(remediated_task_ids(_history("repair", "fine"), {"results": rows}), {})

    def test_junk_input_is_tolerated(self):
        """别让脏数据把整个执行打崩——判据降级成「什么都不补救」。"""
        for bad in (None, [], [{"status": "applied"}], ["x"], [{"adopted": "no"}]):
            self.assertEqual(remediated_task_ids(bad, {"results": {"a": {"status": "failed"}}}),
                             {}, bad)
        self.assertEqual(remediated_task_ids([{"status": "applied"}], None), {})
        self.assertEqual(remediated_task_ids(None, {"results": None}), {})


class FailedThenRemediatedWorkflowTests(unittest.TestCase):
    """真跑工作流，验证最终状态、留痕与「不重复派发」。"""

    def setUp(self):
        import tempfile

        self.manager = GameWorkflowManager(tempfile.mkdtemp())

    def tearDown(self):
        self.manager.close()

    def _run(self, replanner, policy=None):
        state = self.manager.start(
            "验证失败后补救",
            policy=policy or WorkflowPolicy(approval_mode="high", max_replans=1, max_steps=4),
        )
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "broken", "role": "coder", "task": "制造一个可观察失败"}])
        calls = []

        def runner(task, context):
            calls.append((task["id"], dict(context)))
            if task["id"] == "broken":
                return {"status": "failed", "conclusion": "失败诊断",
                        "error": "可恢复错误", "steps": 1}
            return {"status": "ok", "conclusion": "补救完成", "steps": 1}

        done = self.manager.execute(wid, runner, replanner=replanner)
        return done, calls

    def _repair_planner(self, failed, results, attempt):
        return [{"id": "repair", "role": "coder", "task": "修复失败任务",
                 "depends_on": ["broken"]}]

    def test_remediated_failure_passes_but_the_failure_stays_visible(self):
        done, _calls = self._run(self._repair_planner)

        # 程序层面放行
        self.assertEqual(done["status"], STATUS_COMPLETED)
        # 失败历史没被抹掉：结果里它仍是 failed
        self.assertEqual(done["results"]["results"]["broken"]["status"], "failed")

    def test_the_failure_row_carries_the_remediation_note(self):
        """界面要能说「后来补上了」，所以留痕必须落在**成员行**上，
        而不只是落在事件流里（事件流会被折叠掉）。"""
        done, _calls = self._run(self._repair_planner)

        row = next(item for item in done["subagents"] if item["id"] == "broken")
        self.assertEqual(row["status"], "failed")            # 仍然标红
        self.assertEqual(row["remediated_by"], "repair")      # 但说了是谁补的
        self.assertIn("补救成功", row["remediation_note"])

    def test_the_helper_row_is_not_marked_as_remediated(self):
        """反向守卫：只有**失败过**的任务才挂补救标记，成功的不挂。"""
        done, _calls = self._run(self._repair_planner)

        helper = next(item for item in done["subagents"] if item["id"] == "repair")
        self.assertEqual(helper["status"], "ok")
        self.assertIsNone(helper.get("remediated_by"))

    def test_the_remediation_is_recorded_as_its_own_event(self):
        done, _calls = self._run(self._repair_planner)

        marks = [e for e in done["events"] if e["kind"] == "subagent_remediated"]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["task_id"], "broken")
        self.assertEqual(marks[0]["by"], "repair")

    def test_a_failed_task_is_not_dispatched_twice(self):
        """**反向守卫（副作用类）**：失败的任务已经跑过一次，不能因为进了
        重规划后的计划就**再跑一遍**——重跑会再写一遍文件、再花一遍 token。"""
        done, calls = self._run(self._repair_planner)

        self.assertEqual([c[0] for c in calls], ["broken", "repair"])
        self.assertEqual(done["replans"], 1)

    def test_a_plain_failure_with_no_replan_is_still_failed(self):
        """**反向守卫（最关键的一条）**：没有补救时绝不能放行。

        如果这条红了，说明「补救」被写成了「有后续波次就当过了」之类的开关。
        """
        done, _calls = self._run(lambda failed, results, attempt: None)

        self.assertEqual(done["status"], STATUS_FAILED)
        row = next(item for item in done["subagents"] if item["id"] == "broken")
        self.assertIsNone(row.get("remediated_by"))
        self.assertEqual(
            [e for e in done["events"] if e["kind"] == "subagent_remediated"], [])

    def test_a_helper_that_fails_does_not_rescue_the_workflow(self):
        """**反向守卫**：补救任务自己也失败 -> 整件事还是失败。"""
        def replanner(failed, results, attempt):
            return [{"id": "repair", "role": "coder", "task": "修复失败任务",
                     "depends_on": ["broken"]}]

        state = self.manager.start(
            "补救也失败", policy=WorkflowPolicy(approval_mode="high", max_replans=1,
                                                max_steps=4))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "broken", "role": "coder", "task": "制造失败"}])

        def runner(task, context):
            return {"status": "failed", "conclusion": "还是不行", "steps": 1}

        done = self.manager.execute(wid, runner, replanner=replanner)
        self.assertEqual(done["status"], STATUS_FAILED)
        self.assertEqual(
            [e for e in done["events"] if e["kind"] == "subagent_remediated"], [])

    def test_a_failed_optional_task_does_not_fail_the_workflow(self):
        """**反向守卫**：**跑了并失败**的可选任务不算这件事失败。

        `_live_failed` 是新引入的全量判据（`execution_ok` 靠它），它必须保留
        `run_plan` 一直有的「可选任务豁免」语义：
        `ok = all(status == ok or item.optional or by_id[task].optional)`。

        为什么必须查**任务表**而不只看结果行：结果行里
        `{"status": "failed", ...}` **不带** `optional` 标记（只有
        `_subagent_records` 生成的成员行带）。判据里少写豁免会怎样：
        一个可选任务失败就把整件事拖成 failed——而那正是「可选」的意思。

        这条与 `test_a_helper_that_fails_does_not_rescue_the_workflow` 方向相反：
        那里失败的是**必需**任务，那条守的就是「不能放行」。

        顺带记一个踩过的坑：曾把这里写成「可选上游失败 -> 下游被 blocked」。
        那个形状**不会发生**——`dependency_optional` 为真时压根不阻断，
        下游会照跑。可选任务唯一会变成 blocked 的场合是撞步数上限，
        而那时 `report["ok"]` 已经是 False，本来就不会报完成。
        """
        state = self.manager.start(
            "可选任务失败", policy=WorkflowPolicy(approval_mode="high", max_replans=0,
                                                max_steps=6))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [
            {"id": "core", "role": "coder", "task": "正事"},
            {"id": "flavor", "role": "coder", "task": "锦上添花",
             "depends_on": ["core"], "optional": True},
        ])

        def runner(task, context):
            if task["id"] == "core":
                return {"status": "ok", "conclusion": "正事做完了", "steps": 1}
            return {"status": "failed", "conclusion": "锦上添花没做成", "steps": 1}

        done = self.manager.execute(wid, runner)
        rows = done["results"]["results"]
        self.assertEqual(rows["core"]["status"], "ok")
        self.assertEqual(rows["flavor"]["status"], "failed")
        self.assertEqual(done["status"], STATUS_COMPLETED)
        # 豁免只影响最终判定，失败历史照旧留着
        row = next(item for item in done["subagents"] if item["id"] == "flavor")
        self.assertEqual(row["status"], "failed")

    def test_an_unrelated_helper_does_not_rescue_the_workflow(self):
        """**反向守卫**：模型追加一个跟失败无关的成功任务，不算补救。

        这是判据被放宽时最典型的症状：只要后面有成功任务就放行。
        """
        state = self.manager.start(
            "无关补救", policy=WorkflowPolicy(approval_mode="high", max_replans=1,
                                              max_steps=4))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "broken", "role": "coder", "task": "制造失败"}])

        def runner(task, context):
            if task["id"] == "broken":
                return {"status": "failed", "conclusion": "失败诊断", "steps": 1}
            return {"status": "ok", "conclusion": "做了点别的", "steps": 1}

        # 补救任务与 broken **没有任何依赖关系**
        done = self.manager.execute(wid, runner, replanner=lambda f, r, a: [
            {"id": "sidequest", "role": "coder", "task": "顺手做点别的"}])
        self.assertEqual(done["status"], STATUS_FAILED)
        row = next(item for item in done["subagents"] if item["id"] == "broken")
        self.assertIsNone(row.get("remediated_by"))


if __name__ == "__main__":
    unittest.main()