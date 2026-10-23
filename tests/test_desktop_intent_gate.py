# -*- coding: utf-8 -*-
"""游戏控制内联化：桌面动作「本轮口头授权」判据 + 审批门豁免。

覆盖三件事：
1. ``_desktop_intent_for_turn`` 的双条件判据（动作词 + 具体参数同时出现）。
2. ``dev_desktop_action`` 在 pre-auth 且 target=engine/embedded 时跳过逐次审批门，
   foreground 永不豁免（安全边界回归）。
3. ``_workflow_explicitly_requested`` 只在用户显式点名工作流时为 True。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
import tools as tools_mod  # noqa: E402
import desktop_actions  # noqa: E402
from agent_runtime.tools import _DESKTOP_PRE_AUTHORIZED  # noqa: E402


class DesktopIntentTurnTests(unittest.TestCase):
    """纯函数判据：动作词与具体参数**缺一不可**。"""

    def test_click_with_coords_is_authorized(self):
        self.assertTrue(agent_mod._desktop_intent_for_turn("在(400,300)点一下"))

    def test_click_with_target_and_coords_is_authorized(self):
        self.assertTrue(agent_mod._desktop_intent_for_turn(
            "click target=engine x=400 y=300"))

    def test_type_with_window_target_is_authorized(self):
        self.assertTrue(agent_mod._desktop_intent_for_turn(
            "在游戏窗口里输入 hello"))

    def test_vague_game_request_is_not_authorized(self):
        # 「测试」不是桌面动作词，也没有坐标/目标。
        self.assertFalse(agent_mod._desktop_intent_for_turn("帮我测试游戏"))

    def test_start_engine_is_not_a_desktop_action(self):
        # 引擎启停本就无需审批门，不该被认成桌面动作。
        self.assertFalse(agent_mod._desktop_intent_for_turn("启动引擎看看"))

    def test_action_word_without_params_is_not_authorized(self):
        self.assertFalse(agent_mod._desktop_intent_for_turn("帮我点一下游戏里的按钮"))

    def test_system_question_marker_is_stripped(self):
        # /api/chat 会在问题前拼【系统提示】，判据只看真实用户问题。
        self.assertTrue(agent_mod._desktop_intent_for_turn(
            "用户问题：请在 (520,260) 点击确认"))


class DevDesktopActionPreAuthTests(unittest.TestCase):
    """审批门豁免：pre-auth 只对 engine/embedded 生效，foreground 永远走审批。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        tools_mod.set_runtime("code_root", self.root)
        self._performed = []

        def fake_perform(action, **kw):
            self._performed.append({"action": action, **kw})
            return {"ok": True, "marker": "PERFORMED", "action": action}

        self._fake_perform = fake_perform
        # 逐次审批门一律返回「未批准」，用来证明 pre-auth 是否真的绕过了它。
        self._blocked_gate = {"status": 403, "action": "desktop_action",
                              "reason": "approval_required"}

    def tearDown(self):
        tools_mod.set_runtime("code_root", "")

    def _call(self, arg):
        with patch.object(tools_mod, "_mcp_gate_blocked", return_value=dict(self._blocked_gate)), \
             patch.object(desktop_actions, "perform", side_effect=self._fake_perform), \
             patch("game_workbench.playtest_sandbox_desktop_allowed",
                   return_value=(False, "未开启", {})):
            return json.loads(tools_mod.dev_desktop_action(arg))

    def test_engine_click_preauthorized_skips_gate(self):
        token = _DESKTOP_PRE_AUTHORIZED.set(True)
        try:
            result = self._call("action: click\ntarget: engine\nx: 100\ny: 100")
        finally:
            _DESKTOP_PRE_AUTHORIZED.reset(token)
        self.assertEqual(result.get("marker"), "PERFORMED")
        self.assertTrue(result.get("ok"))
        self.assertEqual(len(self._performed), 1)

    def test_embedded_click_preauthorized_skips_gate(self):
        token = _DESKTOP_PRE_AUTHORIZED.set(True)
        try:
            result = self._call("action: click\ntarget: embedded\nx: 10\ny: 10")
        finally:
            _DESKTOP_PRE_AUTHORIZED.reset(token)
        self.assertEqual(result.get("marker"), "PERFORMED")

    def test_foreground_never_preauthorized(self):
        # 即便 pre-auth 为真，foreground 也必须落回审批门（不执行 perform）。
        token = _DESKTOP_PRE_AUTHORIZED.set(True)
        try:
            result = self._call("action: click\ntarget: foreground\nx: 10\ny: 10")
        finally:
            _DESKTOP_PRE_AUTHORIZED.reset(token)
        self.assertNotEqual(result.get("marker"), "PERFORMED")
        self.assertEqual(len(self._performed), 0)
        self.assertEqual(result.get("reason"), "approval_required")

    def test_no_preauth_still_gated_for_engine(self):
        # 回归保护：没有口头授权时，engine 仍需审批（沙箱也不放行则返回 blocked）。
        token = _DESKTOP_PRE_AUTHORIZED.set(False)
        try:
            result = self._call("action: click\ntarget: engine\nx: 100\ny: 100")
        finally:
            _DESKTOP_PRE_AUTHORIZED.reset(token)
        self.assertNotEqual(result.get("marker"), "PERFORMED")
        self.assertEqual(len(self._performed), 0)
        self.assertEqual(result.get("reason"), "approval_required")


class WorkflowExplicitRequestTests(unittest.TestCase):
    """工作流降级为显式 opt-in：默认不命中，点名才命中。"""

    def test_plain_game_ops_not_a_workflow(self):
        self.assertFalse(agent_mod._workflow_explicitly_requested("启动引擎然后点一下"))
        self.assertFalse(agent_mod._workflow_explicitly_requested("帮我修一下这个报错"))

    def test_explicit_workflow_request_matches(self):
        self.assertTrue(agent_mod._workflow_explicitly_requested("开启工作流跑这个游戏"))
        self.assertTrue(agent_mod._workflow_explicitly_requested("用工作流把它跑完"))
        self.assertTrue(agent_mod._workflow_explicitly_requested("跑一遍完整自检"))
        self.assertTrue(agent_mod._workflow_explicitly_requested("自动化回归测试"))
        self.assertTrue(agent_mod._workflow_explicitly_requested("run a workflow for this"))


if __name__ == "__main__":
    unittest.main()
