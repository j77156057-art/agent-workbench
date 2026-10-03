# -*- coding: utf-8 -*-
"""计划 DAG 不许被静默截断——以及 DAG 审批不许抛 NameError。

## 两个缺陷

**一、依赖守卫拿错了比较对象。** ``_parse_generated_tasks`` 里写的是

    if task_id in clean_deps or len(clean_deps) > max_tasks:

``max_tasks`` 是**任务总数上限**，且约定 ``0`` 表示**不限**
（``max_tasks=max_subagents if max_subagents > 0 else 0``）。把它当成
**依赖个数**的上界来比，于是默认配置下（``max_agents`` 未配 -> 0）：

    输入 5 步：survey -> implement -> testrun -> fix -> report
    实际存活 1 步：['survey']

**任何声明了 ``depends_on`` 的任务都被静默丢弃**，多步 DAG 塌成只剩根任务。
而调用方拿到的 ``parsed`` 非空，于是照样落库、照样发 ``plan`` 事件、照样弹
审批框——全程零报错。计划生成的验证节点也拦不住：它只查"落库的计划能否
执行"，查不出"落库的计划已经少了一大半"。

**二、``approve_dag_revision`` 少了一行 ``policy = ...``。** 从
``request_dag_revision`` 复制过来时漏掉，于是「用户批准 DAG 修订」一走到就
抛 ``NameError: name 'policy' is not defined``。而这正是 vibecoding 主循环里
最核心的一次交互：模型改计划 → 用户点批准 → 真正开跑。

两条都是"看起来做完了、实际没生效"：一个少做了一半，一个整条路走不通。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runtime.game_workflow import (  # noqa: E402
    _MAX_TASK_DEPS,
    GameWorkflowManager,
    WorkflowError,
    _raw_task_count,
)

#: 一条常规开发 DAG：读代码 -> 改 -> 测 -> 修 -> 汇报。
#: 每一步都依赖前一步，正是最容易被那个守卫吃掉的情形。
DEV_DAG = [
    {"id": "survey", "task": "读现有实现，摸清要改哪几处", "depends_on": []},
    {"id": "implement", "task": "改代码", "depends_on": ["survey"]},
    {"id": "testrun", "task": "跑测试", "depends_on": ["implement"]},
    {"id": "fix", "task": "修失败项", "depends_on": ["testrun"]},
    {"id": "report", "task": "汇报", "depends_on": ["fix"]},
]


def _ids(tasks):
    return [t["id"] for t in tasks]


class DependencyGuardTests(unittest.TestCase):
    """守卫该管依赖个数，但上限得是依赖自己的上界，不是任务总数上限。"""

    def _parse(self, raw, max_tasks):
        return GameWorkflowManager._parse_generated_tasks(raw, max_tasks=max_tasks)

    def test_an_unlimited_plan_keeps_tasks_that_declare_dependencies(self):
        """回归：``max_tasks=0``（默认）曾把带依赖的任务全部丢掉。"""
        got = self._parse({"tasks": DEV_DAG}, max_tasks=0)
        self.assertEqual(_ids(got), [t["id"] for t in DEV_DAG],
                         "默认配置下多步 DAG 塌成了只剩根任务——"
                         "带 depends_on 的任务被静默丢弃了")

    def test_a_realistic_five_step_plan_survives_intact(self):
        got = self._parse({"tasks": DEV_DAG}, max_tasks=0)
        self.assertEqual(len(got), 5, "5 步计划只剩 %d 步：%s" % (len(got), _ids(got)))
        for task in DEV_DAG[1:]:
            self.assertIn(task["id"], _ids(got))

    def test_the_dependency_count_bound_is_its_own_constant(self):
        """依赖个数的上界必须独立于 ``max_tasks``，且真的有上界。"""
        self.assertGreater(_MAX_TASK_DEPS, 0, "依赖上界不能是 0（那等于全丢）")
        fan_in = {"tasks": [
            {"id": "sink", "task": "汇总", "depends_on": ["d%d" % i for i in range(40)]},
            {"id": "d0", "task": "叶子 0", "depends_on": []},
        ] + [{"id": "d%d" % i, "task": "叶子 %d" % i, "depends_on": []}
             for i in range(1, 40)]}
        got = self._parse(fan_in, max_tasks=0)
        self.assertNotIn("sink", _ids(got),
                         "依赖个数超过上界仍被接受——上界形同虚设")

    def test_max_tasks_still_caps_the_number_of_tasks(self):
        """修的是比较对象，不是把上限整个废掉。"""
        got = self._parse({"tasks": DEV_DAG}, max_tasks=2)
        self.assertEqual(_ids(got), ["survey", "implement"],
                         "max_tasks 仍是任务总数上限")

    def test_a_task_depending_on_itself_is_still_rejected(self):
        """守卫的另一半还在，别把真检查一起删了。"""
        got = self._parse({"tasks": [
            {"id": "a", "task": "自环", "depends_on": ["a"]},
            {"id": "b", "task": "正常", "depends_on": []},
        ]}, max_tasks=0)
        self.assertEqual(_ids(got), ["b"], "自依赖没有被拒")


class TruncationMustNotBeSilentTests(unittest.TestCase):
    """部分丢弃此前完全无声。现在差额必须变成事件。"""

    def setUp(self):
        self.manager = GameWorkflowManager(tempfile.mkdtemp())

    def _events(self, wid):
        return self.manager.get(wid).get("events") or []

    def _event_kinds(self, wid):
        return [e.get("kind") for e in self._events(wid)]

    def test_dropping_some_tasks_emits_a_truncation_event(self):
        def task_generator(_r, _s, _p):
            return {"tasks": [
                {"id": "impl", "task": "实现", "depends_on": []},
                # 依赖 "nowhere"——未知依赖会让**整个图**被拒，所以改用
                # 空描述触发单条丢弃：这里用一个重复 id。
                {"id": "impl", "task": "重复 id，必须被丢", "depends_on": []},
                {"id": "dup", "task": "", "depends_on": []},
            ]}

        state = self.manager.start(
            "做一个 2D 游戏",
            option_generator=lambda _r, _p: {
                "options": [{"id": "small", "title": "小", "summary": "s",
                             "recommended": True}]},
            task_generator=task_generator)
        wid = state["workflow_id"]
        self.manager.choose(wid, "small")
        self.assertIn("plan_generation_truncated", self._event_kinds(wid),
                      "3 条任务只留下 %d 条，却没有任何截断信号"
                      % len(self.manager.get(wid)["tasks"]))
        trunc = [e for e in self._events(wid)
                 if e.get("kind") == "plan_generation_truncated"][0]
        self.assertEqual((trunc["offered"], trunc["kept"], trunc["dropped"]),
                         (3, 1, 2), "截断事件必须说清少了多少")

    def test_an_intact_plan_emits_no_truncation_event(self):
        def task_generator(_r, _s, _p):
            return {"tasks": [dict(t) for t in DEV_DAG]}

        state = self.manager.start(
            "做一个 2D 游戏",
            option_generator=lambda _r, _p: {
                "options": [{"id": "small", "title": "小", "summary": "s",
                             "recommended": True}]},
            task_generator=task_generator)
        wid = state["workflow_id"]
        chosen = self.manager.choose(wid, "small")
        self.assertEqual(_ids(chosen["tasks"]), [t["id"] for t in DEV_DAG],
                         "5 步计划没有完整落库")
        self.assertNotIn("plan_generation_truncated", self._event_kinds(wid),
                         "计划完整却报了截断——信号本身不可信了")


class RawTaskCountTests(unittest.TestCase):
    """诊断计数可以宽容，但绝不能把规划搞崩。"""

    def test_it_handles_the_shapes_the_parser_handles(self):
        self.assertEqual(_raw_task_count({"tasks": DEV_DAG}), 5)
        self.assertEqual(_raw_task_count({"plan": {"tasks": DEV_DAG}}), 5)
        self.assertEqual(_raw_task_count(DEV_DAG), 5)
        self.assertEqual(_raw_task_count('{"tasks": []}'), 0)
        self.assertEqual(_raw_task_count('```json\n{"tasks": [1, 2]}\n```'), 2)

    def test_it_returns_zero_instead_of_raising(self):
        for bad in (None, 42, "not json at all", {"nope": 1}, object()):
            self.assertEqual(_raw_task_count(bad), 0)


class DagApprovalPathTests(unittest.TestCase):
    """模型改计划 → 用户点批准 → 真正开跑。这条路曾经一走就 NameError。"""

    def setUp(self):
        self.manager = GameWorkflowManager(tempfile.mkdtemp())
        state = self.manager.start("做一个 2D 游戏原型")
        self.wid = state["workflow_id"]
        self.manager.choose(self.wid, "recommended")
        self.manager.plan(self.wid, tasks=[dict(t) for t in DEV_DAG])

    def test_approving_a_dag_revision_completes_instead_of_raising(self):
        staged = self.manager.request_dag_revision(self.wid, [
            {"id": "survey", "task": "摸清要改哪几处", "depends_on": []},
            {"id": "implement", "task": "改代码", "depends_on": ["survey"]},
        ])
        self.assertEqual(staged["status"], "interrupted")
        self.assertEqual(_ids(staged["pending_tasks"]), ["survey", "implement"])

        approved = self.manager.approve_dag_revision(self.wid, True)
        self.assertEqual(_ids(approved["tasks"]), ["survey", "implement"],
                         "批准后落库的计划与待审计划不一致")
        self.assertEqual(approved["pending_tasks"], [])

    def test_a_staged_revision_keeps_dependent_tasks_intact(self):
        """待审队列也走同一个解析器——别只修好落库那一半。"""
        staged = self.manager.request_dag_revision(self.wid, DEV_DAG)
        self.assertEqual(_ids(staged["pending_tasks"]), [t["id"] for t in DEV_DAG],
                         "待审计划又被截断了")

    def test_denying_a_revision_clears_it_without_touching_tasks(self):
        before = _ids(self.manager.get(self.wid)["tasks"])
        self.manager.request_dag_revision(self.wid, [
            {"id": "only", "task": "换个方向", "depends_on": []}])
        denied = self.manager.approve_dag_revision(self.wid, False)
        self.assertEqual(denied["pending_tasks"], [])
        self.assertEqual(_ids(denied["tasks"]), before,
                         "拒绝修订却动了原计划")

    def test_approving_with_nothing_staged_raises(self):
        with self.assertRaises(WorkflowError):
            self.manager.approve_dag_revision(self.wid, True)


if __name__ == "__main__":
    unittest.main()