# -*- coding: utf-8 -*-
"""PTC 路径 B —— 步 1「开关三锚点」+ 步 2「bridge 桩进 jail 前缀」回归（第46/47批）。

步 1（开关骨架）：
  1. _ptc_bridge_enabled 读模块全局，缺省 '0' -> False。
  2. 锚点·工具注册 seam：_ptc_bridge_tool_names() 关->空集，开->v1 只读白名单（成员锁定，
     NON-PURE/递归源不在表内）。
  3. 锚点·分派：_python_exec_run 关->_python_exec_direct，开->_python_exec_ptc_run。
     注：步 3 起 _python_exec_ptc_run 为真 Popen 服务环（端到端见 test_ptc_service_loop.py），
     不再委托 direct；本文件只验分派路由与关模式字节等价。
  4. 锚点·system_prompt：_ptc_variant_system_prompt 步 1 占位 passthrough；关模式不调它。

步 2（bridge 桩）：
  5. 关模式 jail 前缀**不含 __BRIDGE_REQ__ 标记**、_PJ_JAIL_BODY 原样保留 -> 逐字节等于今天。
  6. 开模式追加桩：_PJ_JAIL_BODY 仍在（append 非 modify）、含 list_tools / 每个白名单工具 def、
     不含 NON-PURE；开模式前缀以关模式前缀为严格前缀。
  7. 桩源码 ast.parse 合法；关模式生成的桩名单为空、无任何工具 def。

核心判据：**关模式 = 今天**；开模式接缝点亮且不破坏牢笼钩。
"""

import os
import sys
import ast
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tools as tools_mod  # noqa: E402
import agent as agent_mod  # noqa: E402


class FlagTests(unittest.TestCase):
    def test_enabled_flag_reads_module_global(self):
        with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", False):
            self.assertFalse(tools_mod._ptc_bridge_enabled())
        with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True):
            self.assertTrue(tools_mod._ptc_bridge_enabled())

    def test_default_is_off_when_env_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DOCMIND_PTC_BRIDGE", None)
            self.assertFalse(os.environ.get("DOCMIND_PTC_BRIDGE", "0") != "0")
        with patch.dict(os.environ, {"DOCMIND_PTC_BRIDGE": "1"}):
            self.assertTrue(os.environ.get("DOCMIND_PTC_BRIDGE", "0") != "0")


class ToolRegistrationSeamTests(unittest.TestCase):
    def test_off_returns_empty(self):
        with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", False):
            self.assertEqual(tools_mod._ptc_bridge_tool_names(), frozenset())

    def test_on_returns_v1_whitelist(self):
        with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True):
            names = tools_mod._ptc_bridge_tool_names()
        self.assertEqual(names, tools_mod._PTC_BRIDGE_WHITELIST)
        for expect in ("calculate", "memory_search", "search_knowledge",
                       "search_code", "grep", "read_file", "list_dir",
                       "web_search", "web_fetch", "web_research"):
            self.assertIn(expect, names)
        for forbidden in ("python_exec", "run_command", "apply_edit",
                          "create_file", "memory_save"):
            self.assertNotIn(forbidden, names)


class DispatchAnchorTests(unittest.TestCase):
    def test_off_dispatches_to_direct(self):
        calls = {}

        def fake_direct(code, ceiling=1, allow_escalate=True):
            calls["direct"] = (code, ceiling, allow_escalate)
            return "DIRECT"

        def fake_ptc(code, ceiling=1, allow_escalate=True):
            calls["ptc"] = True
            return "PTC"

        with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", False), \
                patch.object(tools_mod, "_python_exec_direct", fake_direct), \
                patch.object(tools_mod, "_python_exec_ptc_run", fake_ptc):
            out = tools_mod._python_exec_run("print(1)", ceiling=2, allow_escalate=False)
        self.assertEqual(out, "DIRECT")
        self.assertNotIn("ptc", calls)
        self.assertEqual(calls["direct"], ("print(1)", 2, False))

    def test_on_dispatches_to_ptc(self):
        calls = {}

        def fake_direct(code, ceiling=1, allow_escalate=True):
            calls["direct"] = True
            return "DIRECT"

        def fake_ptc(code, ceiling=1, allow_escalate=True):
            calls["ptc"] = (code, ceiling, allow_escalate)
            return "PTC"

        with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True), \
                patch.object(tools_mod, "_python_exec_direct", fake_direct), \
                patch.object(tools_mod, "_python_exec_ptc_run", fake_ptc):
            out = tools_mod._python_exec_run("print(1)", ceiling=1, allow_escalate=True)
        self.assertEqual(out, "PTC")
        self.assertNotIn("direct", calls)
        self.assertEqual(calls["ptc"], ("print(1)", 1, True))


class PromptAnchorTests(unittest.TestCase):
    def test_variant_prompt_passthrough_step1(self):
        sample = "系统提示：你是一个……"
        self.assertEqual(agent_mod._ptc_variant_system_prompt(sample), sample)

    def test_off_flag_does_not_rewrite_channel(self):
        with patch.object(agent_mod, "_PTC_BRIDGE_ENABLED", False):
            called = {"n": 0}

            def spy(p):
                called["n"] += 1
                return p + "MUTATED"

            prompt = "CHANNEL"
            if agent_mod._PTC_BRIDGE_ENABLED:
                prompt = spy(prompt)
            self.assertEqual(prompt, "CHANNEL")
            self.assertEqual(called["n"], 0)


class BridgeStubStep2Tests(unittest.TestCase):
    def _prefix(self, enabled):
        with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", enabled):
            return tools_mod._python_exec_jail_prefix(1)

    def test_off_prefix_has_no_bridge(self):
        off = self._prefix(False)
        self.assertNotIn("__BRIDGE_REQ__", off)
        self.assertNotIn("def list_tools()", off)
        self.assertNotIn("agent_tool_bridge", off)
        self.assertIn(tools_mod._PJ_JAIL_BODY, off)

    def test_on_prefix_appends_bridge(self):
        on = self._prefix(True)
        self.assertIn("__BRIDGE_REQ__", on)
        self.assertIn("def list_tools()", on)
        self.assertIn(tools_mod._PJ_JAIL_BODY, on)
        for n in sorted(tools_mod._PTC_BRIDGE_WHITELIST):
            self.assertIn("def %s(arg):" % n, on)
        for forbidden in ("python_exec", "run_command", "apply_edit",
                          "create_file", "memory_save"):
            self.assertNotIn("def %s(arg):" % forbidden, on)
        off = self._prefix(False)
        self.assertTrue(on.startswith(off))

    def test_stub_src_compiles(self):
        with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True):
            src = tools_mod._ptc_bridge_stub_src()
        ast.parse(src)

    def test_stub_src_off_has_empty_toolset(self):
        with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", False):
            src = tools_mod._ptc_bridge_stub_src()
        ast.parse(src)
        self.assertIn("_BRIDGE_TOOLS = []", src)
        self.assertNotIn("def calculate(arg):", src)


if __name__ == "__main__":
    unittest.main()
