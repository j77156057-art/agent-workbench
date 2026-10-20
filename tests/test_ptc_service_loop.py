# -*- coding: utf-8 -*-
"""PTC 路径 B —— 步 3「Popen 双读服务环」+ 步 4「父侧白名单二次校验」端到端回归（第48/50批）。

真 jail 前缀 + bridge 桩（步 2）+ 真 Popen 服务环（步 3）+ 白名单门（步 4）：
  步 3：round-trip / 多次调用 / fail-closed(无 server) / Windows 中文往返 / stderr 双 drain。
  步 4：非白名单工具（含子脚本绕过命名包装直调 _bridge_call("apply_edit")）在
        execute_tool【之前】被父侧拒、**绝不调 server（=不归因）**；白名单工具照常放行。

真起 `python -c` 子进程（与 test_python_exec_jail 同机制），仅本地模式。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tools as tools_mod  # noqa: E402
from config import set_runtime  # noqa: E402


class _LoopHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, code, server, enabled=True):
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            set_runtime("code_root", self.tmp)
            set_runtime("exec_approval", True)
            with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", enabled), \
                    patch.object(tools_mod, "_PTC_BRIDGE_SERVER", server):
                return tools_mod._python_exec_ptc_run(code, ceiling=1, allow_escalate=False)


class RoundTripTests(_LoopHarness):
    def test_single_call_served_and_answer_returned(self):
        log = []

        def server(name, arg):
            log.append((name, arg))
            return (True, "ECHO:%s" % arg)

        code = 'ok, r = calculate("2+2")\nprint("R1", r)\nprint("FIN")'
        out = self._run(code, server)
        self.assertIn("R1 ECHO:2+2", out)
        self.assertIn("FIN", out)
        self.assertEqual(log, [("calculate", "2+2")])

    def test_multiple_calls_all_served(self):
        log = []

        def server(name, arg):
            log.append((name, arg))
            return (True, "%s=%s" % (name, arg))

        code = ('a, ra = calculate("1")\n'
                'b, rb = grep("needle")\n'
                'print(ra); print(rb); print("END")')
        out = self._run(code, server)
        self.assertEqual(log, [("calculate", "1"), ("grep", "needle")])
        self.assertIn("END", out)
        self.assertIn("calculate=1", out)
        self.assertIn("grep=needle", out)

    def test_no_server_fail_closed_no_hang(self):
        code = ('ok, r = calculate("9")\n'
                'print("SERVED" if ok else "NOTSERVED", str(r))\n'
                'print("DONE")')
        out = self._run(code, None)
        self.assertIn("NOTSERVED", out)
        self.assertIn("服务未就绪", out)
        self.assertIn("DONE", out)


class GbkDrainTests(_LoopHarness):
    def test_chinese_roundtrip_no_garble(self):
        def server(name, arg):
            return (True, "结果:%s" % arg)

        code = 'ok, r = calculate("中文入参")\nprint(r)\nprint("输出完成")'
        out = self._run(code, server)
        self.assertIn("结果:中文入参", out)
        self.assertIn("输出完成", out)

    def test_stderr_does_not_deadlock(self):
        def server(name, arg):
            return (True, "x")

        code = ('import sys\n'
                'sys.stderr.write("trace-line-1\\n")\n'
                'ok, r = calculate("1")\n'
                'sys.stderr.write("trace-line-2\\n")\n'
                'print("ANSWER", r)')
        out = self._run(code, server)
        self.assertIn("ANSWER", out)
        self.assertTrue(any("trace-line-1" in ln for ln in tools_mod._PTC_LAST_TRACE),
                        "stderr trace 应被 drain 收集")


class WhitelistStep4Tests(_LoopHarness):
    def test_bypass_named_wrapper_rejected_before_execute(self):
        # 子脚本绕过命名包装，直接调 _bridge_call("apply_edit", ...) —— 仍须被父侧拒。
        log = []

        def server(name, arg):
            log.append(name)
            return (True, "SHOULD_NOT_BE_CALLED")

        code = ('ok, r = _bridge_call("apply_edit", "somefile")\n'
                'print("OK" if ok else "REJECTED", str(r))\n'
                'print("END")')
        out = self._run(code, server)
        self.assertIn("REJECTED", out)
        self.assertIn("白名单", out)
        self.assertIn("END", out)
        # 关键：被拒工具绝不进 execute_tool / server
        self.assertEqual(log, [])
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("rejected"), 1)
        self.assertEqual(st.get("executed"), 0)

    def test_python_exec_and_mutating_rejected(self):
        log = []

        def server(name, arg):
            log.append(name)
            return (True, "X")

        code = ('a, ra = _bridge_call("python_exec", "import os;os.system(\'rm -rf /\')")\n'
                'b, rb = _bridge_call("run_command", "del x")\n'
                'print("A", a, "B", b)\n'
                'print("END")')
        out = self._run(code, server)
        self.assertIn("END", out)
        self.assertEqual(log, [])  # 两者都被父侧拒、不调 server
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("rejected"), 2)
        self.assertEqual(st.get("executed"), 0)

    def test_whitelist_passes_mixed_with_reject(self):
        log = []

        def server(name, arg):
            log.append((name, arg))
            return (True, "ok:%s" % arg)

        code = ('a, ra = calculate("1+1")\n'
                'b, rb = _bridge_call("apply_edit", "x")\n'
                'print("A", a, "B", b)\n'
                'print("END")')
        out = self._run(code, server)
        self.assertEqual(log, [("calculate", "1+1")])  # 仅白名单那一条进 server
        self.assertIn("END", out)
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("executed"), 1)
        self.assertEqual(st.get("rejected"), 1)


if __name__ == "__main__":
    unittest.main()
