# -*- coding: utf-8 -*-
"""开发舱「重试该成员」按钮、以及「实际工具调用」面板。

三个缺陷全部来自**真实跑过一次工作流**才暴露，单元测试与既有测试一个都没拦住。

## 一、重试按钮 100% 报500

``workflow_callbacks`` 返回 ``{runner, synth_runner, replanner}``，端点把这三个
一起 splat 进 ``GameWorkflowManager.retry_subagent``，而后者签名里**没有**
``replanner``：

    TypeError: GameWorkflowManager.retry_subagent() got an unexpected keyword
    argument 'replanner'

前端拿到的不是 JSON，只能显示「响应不是合法JSON（HTTP 500）」。
签名与调用方脱节，却没有任何测试覆盖这个端点。

## 二、「实际工具调用」恒为 0 条

面板按 ``kind in {before_tool, after_tool, before_mcp, after_mcp}`` 从**持久化
事件数组**里过滤。而工具生命周期事件只进子代理自己的 hook 缓冲（``record_hook``），
从不落到工作流事件流——面板没撒谎，它读的那条流里确实没有工具事件。
同一屏的任务面板里却明明列着 read_file / grep / python_exec。

## 三、hook 缓冲 48 条配额被工具事件吃光

``record_hook`` 的上限检查在**最前面**：每次工具调用发 before+after 两条，
**24 次工具调用就把配额用光**，之后连 trace 里的工具事件也不再记录。
"""
import ast
import inspect
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402
from agent import SINK_TOOL_HOOKS, _exc_traceback, _tool_event_text  # noqa: E402
from agent_runtime.game_workflow import GameWorkflowManager  # noqa: E402

ROUTE_FILE = os.path.join(ROOT, "api_routes", "agent.py")
COCKPIT_VUE = os.path.join(ROOT, "frontend", "src", "workbench", "components",
                           "AutonomousCockpit.vue")


def _workflow_callback_keys():
    """从 api_routes/agent.py 里**真的**取出 workflow_callbacks 的返回键。

    刻意不把键名硬编码在测试里：那个 dict 一改，测试必须跟着失败——
    这正是本缺陷的成因（签名与调用方各写各的）。
    """
    tree = ast.parse(open(ROUTE_FILE, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "workflow_callbacks":
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                return [k.value for k in sub.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    raise AssertionError("api_routes/agent.py 里找不到 workflow_callbacks 的 return dict")


class RetryEndpointContractTests(unittest.TestCase):
    """端点 splat 的每个键，方法签名都必须收得下。"""

    def test_retry_subagent_accepts_every_key_the_endpoint_passes(self):
        params = inspect.signature(GameWorkflowManager.retry_subagent).parameters
        has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD
                         for p in params.values())
        for key in _workflow_callback_keys():
            self.assertTrue(has_var_kw or key in params,
                            "端点会把 %r 传给 retry_subagent，但签名里没有它"
                            " —— UI 上的「重试该成员」每次都 500" % key)

    def test_the_missing_key_was_replanner(self):
        """把这个 bug 的具体形态钉住：曾经缺的正是 replanner。"""
        params = inspect.signature(GameWorkflowManager.retry_subagent).parameters
        self.assertIn("replanner", params,
                      "replanner 仍未回到签名上")

    def test_replanner_is_optional(self):
        """可选性很重要：没有规划器时重试必须照常工作，而不是报参数错。"""
        params = inspect.signature(GameWorkflowManager.retry_subagent).parameters
        self.assertIs(params["replanner"].default, None)


class ToolEventsReachTheWorkflowStreamTests(unittest.TestCase):
    """工具生命周期事件必须落进持久事件流，面板才有东西可显示。"""

    def setUp(self):
        self.manager = GameWorkflowManager(tempfile.mkdtemp())
        state = self.manager.start("做一个 2D 游戏原型")
        self.wid = state["workflow_id"]
        self.manager.choose(self.wid, "recommended")
        self.manager.plan(self.wid, tasks=[{"id": "t1", "task": "改个常量",
                                            "depends_on": []}])

    def _kinds(self):
        return [e.get("kind") for e in self.manager.get(self.wid)["events"]]

    def test_a_before_tool_step_becomes_a_persisted_workflow_event(self):
        self.manager.record_agent_step(self.wid, "t1", "coder", {
            "type": "before_tool", "text": "调用前 · read_file"})
        self.assertIn("before_tool", self._kinds(),
                      "工具事件没有落进持久事件流——面板会显示 0 条")

    def test_all_four_hook_kinds_land(self):
        for kind in SINK_TOOL_HOOKS:
            self.manager.record_agent_step(self.wid, "t1", "coder", {
                "type": kind, "text": "x · y"})
        kinds = self._kinds()
        for kind in SINK_TOOL_HOOKS:
            self.assertIn(kind, kinds, "%s 没有落进事件流" % kind)

    def test_tool_events_carry_task_and_role(self):
        self.manager.record_agent_step(self.wid, "t1", "tester", {
            "type": "after_tool", "text": "调用后 · grep"})
        ev = [e for e in self.manager.get(self.wid)["events"]
              if e.get("kind") == "after_tool"][0]
        self.assertEqual(ev.get("task_id"), "t1")
        self.assertEqual(ev.get("role"), "tester")

    def test_plain_steps_still_do_not_pollute_the_workflow_stream(self):
        """thought/action/observation 不落持久流（那是刻意的：避免每步落盘膨胀）。"""
        for step in ("thought", "action", "observation"):
            self.manager.record_agent_step(self.wid, "t1", "coder",
                                           {"type": step, "text": "x"})
        self.assertEqual(self._kinds().count("subagent_step"), 0)

    def test_the_cockpit_labels_and_the_backend_kinds_agree(self):
        """跨层一致性：前端的中文标签必须覆盖后端会发的每一种 kind。

        起因是实测那次运行发出 27 种 kind、前端只标了 20 种，**界面上一半是英文**
        —— 包括最该看懂的 `graph_replan_start`。这里从**真实 trace** 里取 kind
        集合，而不是硬编码一份：真实集合变了而标签没跟上，本用例会红。
        """
        import json
        import os

        wf_dir = os.path.join(ROOT, ".docmind", "workflows")
        if not os.path.isdir(wf_dir):
            self.skipTest("没有本机跑过的工作流状态可比对")
        kinds = set()
        for name in os.listdir(wf_dir):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(wf_dir, name), encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue
            for event in (data.get("events") or []):
                if isinstance(event, dict) and event.get("kind"):
                    kinds.add(str(event["kind"]))
        if not kinds:
            self.skipTest("工作流状态里没有事件可比对")

        src = open(COCKPIT_VUE, encoding="utf-8").read()
        start = src.index("const timelineLabel")
        window = src[start:src.index("\n}", start)]
        # 标签表里key 是**不带引号的标识符**（`after_tool: '工具调用前'`），
        # 所以要按 `kind:` 找；第一版按 `'kind'` 找，结果连已标注的也报缺失。
        unlabelled = sorted(k for k in kinds if ("%s:" % k) not in window)
        # 这几个是子代理实时轨迹的 step_type，不是工作流事件 kind
        unlabelled = [k for k in unlabelled
                      if k not in {"thought", "action", "observation"}]
        self.assertEqual(unlabelled, [],
                         "这些 kind 在开发舱时间线上会显示成英文：%s" % unlabelled)

    def test_the_cockpit_filter_list_and_the_backend_constant_agree(self):
        """跨层一致性：面板的字面量与后端常量必须同源。

        面板里写死了这四个 kind。哪天有人只改一边，这里就会红——
        而症状是「工具明明调了，面板却显示 0 条」，极难从界面上定位。
        """
        src = open(COCKPIT_VUE, encoding="utf-8").read()
        marker = "actualToolCalls"
        idx = src.index(marker)
        window = src[idx:idx + 600]
        listed = set()
        for name in SINK_TOOL_HOOKS:
            if "'%s'" % name in window:
                listed.add(name)
        self.assertEqual(listed, set(SINK_TOOL_HOOKS),
                         "前端面板过滤的 kind 与后端 SINK_TOOL_HOOKS 不一致："
                         "面板认 %s，后端发 %s"
                         % (sorted(listed), sorted(SINK_TOOL_HOOKS)))


class ToolEventTextTests(unittest.TestCase):
    """事件行是给人和 UI 看的摘要，不是证据主体。"""

    def test_it_names_the_tool_and_the_stage(self):
        text = _tool_event_text("before_tool", {"tool": "read_file"}, {})
        self.assertIn("read_file", text)
        self.assertIn("调用前", text)

    def test_a_blocked_call_says_so(self):
        text = _tool_event_text("after_tool", {"tool": "apply_edit"},
                                {"blocked": True, "reason": "需要审批"})
        self.assertIn("被拦截", text)
        self.assertIn("需要审批", text)

    def test_it_reports_duration_when_known(self):
        text = _tool_event_text("after_tool",
                                {"tool": "grep", "duration_ms": 42}, {})
        self.assertIn("42", text)

    def test_a_toolless_payload_does_not_crash(self):
        self.assertEqual(_tool_event_text("before_tool", {}, {}), "before_tool")


class TracebackIsEvidenceTests(unittest.TestCase):
    """子代理失败只存 `type: msg` 时，排查只剩一句标签。"""

    def test_it_names_the_function_that_raised(self):
        try:
            raise AttributeError("'NoneType' object has no attribute 'strip'")
        except AttributeError as exc:
            text = _exc_traceback(exc)
        self.assertIn("AttributeError", text)
        self.assertIn("strip", text)
        self.assertIn("test_it_names_the_function_that_raised", text,
                      "栈里没有抛出点——那这条栈依然没法用来定位")

    def test_it_degrades_instead_of_raising(self):
        class Weird(BaseException):
            def __str__(self):
                raise RuntimeError("连 __str__ 都炸")

        text = _exc_traceback(Weird())
        self.assertIn("Weird", text)

    def test_agent_exposes_the_helper_it_uses(self):
        self.assertTrue(callable(agent._exc_traceback))


if __name__ == "__main__":
    unittest.main()