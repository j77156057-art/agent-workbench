# -*- coding: utf-8 -*-
"""PTC 路径 B —— 步 5「模式专属硬护栏」端到端回归（第51批，真起子进程）。

"整段计 1 步"移除每调用 governor，故这些硬上限是上线前置。逐条验：命中即拦、
被拦子调用**不进 execute_tool**（executed 不涨）、单列 blocked、首个 limit_hit 记录、
最终返回**诚实标注未全部执行**（绝不把被拦的说成跑成）。

为提速/降 flakiness，各上限 monkeypatch 成小值；真 subprocess 仅本地模式。
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


class _LimitHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = []

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _server(self, name, arg):
        self.log.append((name, arg))
        return (True, "r:%s" % arg)

    def _run(self, code, **overrides):
        env = {"DOCMIND_EXECUTION_MODE": "local"}
        with patch.dict(os.environ, env):
            set_runtime("code_root", self.tmp)
            set_runtime("exec_approval", True)
            ctxs = [
                patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True),
                patch.object(tools_mod, "_PTC_BRIDGE_SERVER", self._server),
            ]
            for k, v in overrides.items():
                ctxs.append(patch.object(tools_mod, k, v))
            from contextlib import ExitStack
            with ExitStack() as st:
                for c in ctxs:
                    st.enter_context(c)
                return tools_mod._python_exec_ptc_run(code, ceiling=1, allow_escalate=False)


class MaxCallsTests(_LimitHarness):
    def test_executed_capped_at_max(self):
        code = ('res = []\n'
                'for i in range(6):\n'
                '    ok, r = calculate(str(i))\n'
                '    res.append(str(ok))\n'
                'print("CALLS", " ".join(res))\n'
                'print("END")')
        out = self._run(code, _PTC_MAX_CALLS=3)
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st["executed"], 3)
        self.assertEqual(st["blocked"], 3)
        self.assertEqual(st["limit_hit"], "max_calls")
        # server 只被调 3 次（被拦的绝不进 execute_tool）
        self.assertEqual(len(self.log), 3)
        # 诚实标注未全部执行
        self.assertIn("护栏上限被拦", out)
        self.assertIn("未执行", out)
        self.assertIn("END", out)


class WebRateTests(_LimitHarness):
    def test_web_extra_limit(self):
        code = ('for i in range(3):\n'
                '    ok, r = web_search("q%d" % i)\n'
                'print("END")')
        out = self._run(code, _PTC_MAX_WEB_CALLS=1, _PTC_MAX_CALLS=32)
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st["executed"], 1)
        self.assertEqual(st["web"], 1)
        self.assertEqual(st["blocked"], 2)
        self.assertEqual(st["limit_hit"], "max_web")
        self.assertEqual(len(self.log), 1)
        self.assertIn("END", out)


class WallClockTests(_LimitHarness):
    def test_wall_clock_blocks(self):
        code = ('ok, r = calculate("1")\n'
                'print("A", ok)\n'
                'print("END")')
        # deadline 立即到期：首个请求即被墙钟拦
        out = self._run(code, _PTC_WALL_CLOCK=0.0)
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st["executed"], 0)
        self.assertEqual(st["blocked"], 1)
        self.assertEqual(st["limit_hit"], "wall_clock")
        self.assertEqual(len(self.log), 0)
        self.assertIn("END", out)


class ByteCapTests(_LimitHarness):
    def test_result_bytes_cap_blocks(self):
        # server 每次回 ~200 字节；上限 150 → 首个执行后字节满，第二个被拦。
        code = ('a, ra = calculate("1")\n'
                'b, rb = calculate("2")\n'
                'print("A", a, "B", b)\n'
                'print("END")')

        def big_server(name, arg):
            self.log.append((name, arg))
            return (True, "x" * 200)

        with patch.object(tools_mod, "_PTC_BRIDGE_SERVER", big_server):
            out = self._run_plain(code, _PTC_MAX_RESULT_BYTES=150, _PTC_MAX_CALLS=32)
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st["executed"], 1)
        self.assertEqual(st["blocked"], 1)
        self.assertEqual(st["limit_hit"], "bytes")
        self.assertEqual(len(self.log), 1)
        self.assertIn("END", out)

    def _run_plain(self, code, **overrides):
        # 复用 _run 但临时替换 self._server 已在外部 patch；直接走同一套 patch 栈
        env = {"DOCMIND_EXECUTION_MODE": "local"}
        with patch.dict(os.environ, env):
            set_runtime("code_root", self.tmp)
            set_runtime("exec_approval", True)
            ctxs = [patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True)]
            for k, v in overrides.items():
                ctxs.append(patch.object(tools_mod, k, v))
            from contextlib import ExitStack
            with ExitStack() as st:
                for c in ctxs:
                    st.enter_context(c)
                return tools_mod._python_exec_ptc_run(code, ceiling=1, allow_escalate=False)


if __name__ == "__main__":
    unittest.main()
