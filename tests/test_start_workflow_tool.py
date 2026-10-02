# -*- coding: utf-8 -*-
"""T6：对话内 start_workflow 工具（依赖注入 launcher、人工门、分类护栏）。"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
import tools as tools_mod  # noqa: E402
from agent_runtime.game_workflow import GameWorkflowManager  # noqa: E402
from agent_runtime.tools import Capability, coerce_tool_spec  # noqa: E402


class StartWorkflowToolTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.manager = GameWorkflowManager(tempfile.mkdtemp())
        self.calls = []

        def launcher(goal, *, kind="generic", web_enabled=False):
            self.calls.append({"goal": goal, "kind": kind, "web": web_enabled})
            return self.manager.start(
                goal, kind=kind, web_enabled=web_enabled, llm_enabled=False,
                project_root=self.root)

        tools_mod.set_workflow_launcher(launcher)
        tools_mod.set_runtime("code_root", self.root)
        self._old_code_root = tools_mod.CODE_ROOT
        tools_mod.CODE_ROOT = ""

    def tearDown(self):
        tools_mod.set_workflow_launcher(None)
        tools_mod.set_runtime("code_root", "")
        tools_mod.CODE_ROOT = self._old_code_root

    def test_start_generic_workflow_waits_at_choice_gate(self):
        text = tools_mod.start_workflow("把一个 EDA 网表校验流程完整跑通")
        self.assertNotIn("start_workflow 失败", text)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["kind"], "generic")
        wid = next(line for line in text.splitlines()
                   if "ID：" in line).split("ID：", 1)[1].split("）")[0]
        state = self.manager.get(wid)
        self.assertEqual(state["kind"], "generic")
        # 停在方案选择门：任务 DAG 尚未生成、更未执行
        self.assertIn(state["status"], ("awaiting_choice", "generating_options"))
        self.assertEqual(state["tasks"], [])
        self.assertIn("方案选择门", text)
        self.assertIn("通用开发", text)
        # 选项清单已渲染
        self.assertIn("可选方案：", text)

    def test_keyed_args_and_plain_text_goal(self):
        tools_mod.start_workflow(
            "goal: 做一条多阶段数据流水线\nkind: game\nweb: true")
        self.assertEqual(self.calls[-1],
                         {"goal": "做一条多阶段数据流水线", "kind": "game", "web": True})
        # 整段纯文本即目标；kind 缺省 generic
        tools_mod.start_workflow("随便一段没有字段名的长目标")
        self.assertEqual(self.calls[-1]["goal"], "随便一段没有字段名的长目标")
        self.assertEqual(self.calls[-1]["kind"], "generic")

    def test_invalid_kind_falls_back_generic(self):
        tools_mod.start_workflow("goal: 长任务\nkind: wat")
        self.assertEqual(self.calls[-1]["kind"], "generic")

    def test_web_default_inherits_session_flag(self):
        token = tools_mod.set_session_web_enabled(True)
        try:
            tools_mod.start_workflow("goal: 长任务")
            self.assertTrue(self.calls[-1]["web"])
        finally:
            tools_mod._session_web_enabled.reset(token)
        tools_mod.start_workflow("goal: 长任务")
        self.assertFalse(self.calls[-1]["web"])
        # 显式 web:false 覆盖会话开关
        token = tools_mod.set_session_web_enabled(True)
        try:
            tools_mod.start_workflow("goal: 长任务\nweb: false")
            self.assertFalse(self.calls[-1]["web"])
        finally:
            tools_mod._session_web_enabled.reset(token)

    def test_game_kind_passes_through(self):
        tools_mod.start_workflow("goal: 做个多关卡游戏\nkind: game")
        self.assertEqual(self.calls[-1]["kind"], "game")

    def test_empty_goal_fails_without_launcher(self):
        text = tools_mod.start_workflow("   ")
        self.assertIn("失败", text)
        self.assertEqual(self.calls, [])

    def test_launcher_exception_returns_failure_text(self):
        def boom(goal, *, kind, web_enabled):
            raise RuntimeError("boom")

        tools_mod.set_workflow_launcher(boom)
        text = tools_mod.start_workflow("goal: 长任务")
        self.assertIn("start_workflow 失败", text)
        self.assertIn("boom", text)


class StartWorkflowGuardsTests(unittest.TestCase):
    def setUp(self):
        tools_mod.set_workflow_launcher(lambda *a, **k: self.fail("无 code_root 不应调 launcher"))
        tools_mod.set_runtime("code_root", "")
        self._old_code_root = tools_mod.CODE_ROOT
        tools_mod.CODE_ROOT = ""

    def tearDown(self):
        tools_mod.set_workflow_launcher(None)
        tools_mod.CODE_ROOT = self._old_code_root

    def test_without_code_root_fails_and_creates_nothing(self):
        text = tools_mod.start_workflow("goal: 没有项目根目录的长任务")
        self.assertIn("失败", text)
        self.assertIn("代码库根目录", text)

    def test_without_registered_launcher_fails(self):
        tools_mod.set_workflow_launcher(None)
        root = tempfile.mkdtemp()
        tools_mod.set_runtime("code_root", root)
        try:
            text = tools_mod.start_workflow("goal: 长任务")
            self.assertIn("启动通道未初始化", text)
        finally:
            tools_mod.set_runtime("code_root", "")

    def test_classified_as_admin_and_non_parallel(self):
        spec = coerce_tool_spec("start_workflow", tools_mod.TOOLS["start_workflow"])
        self.assertEqual(spec.capability, Capability.ADMIN)
        self.assertFalse(spec.parallel_safe)
        # 原来还有一句 assertIn(..., agent_mod._NO_PARALLEL_TOOLS)，断言的是
        # 一张运行期不读的死副本；上面两行断的 coerce_tool_spec 才是真实判定。

    def test_subagents_cannot_call_start_workflow(self):
        for role, spec in agent_mod._SUBAGENT_ROLES.items():
            self.assertNotIn(
                "start_workflow", spec["tools"],
                "子代理角色 %s 不得持有 start_workflow（防套娃工作流）" % role)

    def test_prompt_documents_escalation_boundary(self):
        blob = tools_mod.TOOLS["start_workflow"]["description"] + "\n" + agent_mod.SYSTEM_PROMPT
        for word in ("delegate", "orchestrate", "start_workflow", "方案选择门",
                     "审批", "跨窗口", "人工门"):
            self.assertIn(word, blob, "编排边界文案缺少：%s" % word)
        # 明确不局限游戏
        self.assertIn("不局限于游戏", blob)


if __name__ == "__main__":
    unittest.main()
