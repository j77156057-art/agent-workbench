# -*- coding: utf-8 -*-
"""start_workflow 工具的工作流门：聊天回合内未点名工作流则软拦截。

``_CHAT_WORKFLOW_ALLOWED`` 默认 True —— 回合之外（直接调工具 / 单测 / 前端显式
POST /api/agent/workflow/start）照常放行；agent 会在回合开始时按用户是否点名
工作流把它置 False。本测试直接操纵这个 ContextVar，验证工具门的两侧行为。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools as tools_mod  # noqa: E402
from agent_runtime.game_workflow import GameWorkflowManager  # noqa: E402
from agent_runtime.tools import _CHAT_WORKFLOW_ALLOWED, chat_workflow_allowed  # noqa: E402


class StartWorkflowGuardTests(unittest.TestCase):
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

    def test_default_allows_creation(self):
        # 未置位（默认 True）时，工具照常创建工作流。
        self.assertTrue(chat_workflow_allowed())
        text = tools_mod.start_workflow("goal: 把一个多阶段流程完整跑通")
        self.assertNotIn("start_workflow 未执行", text)
        self.assertNotIn("start_workflow 失败", text)
        self.assertEqual(len(self.calls), 1)

    def test_blocked_turn_soft_blocks_without_creating(self):
        token = _CHAT_WORKFLOW_ALLOWED.set(False)
        try:
            text = tools_mod.start_workflow("goal: 我想让游戏自动跑起来")
        finally:
            _CHAT_WORKFLOW_ALLOWED.reset(token)
        self.assertIn("start_workflow 未执行", text)
        # 关键：软拦截不能留下任何工作流记录。
        self.assertEqual(len(self.calls), 0)
        # 引导语必须点名内联工具。
        for hint in ("engine_start", "dev_desktop_action", "game_screenshot"):
            self.assertIn(hint, text)

    def test_blocked_then_explicit_request_allowed(self):
        # 回合内先被拦，用户随后显式点名 → 门开，正常创建。
        token = _CHAT_WORKFLOW_ALLOWED.set(False)
        try:
            blocked = tools_mod.start_workflow("goal: 帮我点一下游戏")
        finally:
            _CHAT_WORKFLOW_ALLOWED.reset(token)
        self.assertIn("start_workflow 未执行", blocked)
        self.assertEqual(len(self.calls), 0)

        ok_token = _CHAT_WORKFLOW_ALLOWED.set(True)
        try:
            text = tools_mod.start_workflow("goal: 开启工作流跑完整自检")
        finally:
            _CHAT_WORKFLOW_ALLOWED.reset(ok_token)
        self.assertNotIn("start_workflow 未执行", text)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
