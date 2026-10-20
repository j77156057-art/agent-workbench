# -*- coding: utf-8 -*-
"""PTC 路径 B —— 步 7「断连无孤儿」回归（第52批）。

服务环必须保证：`_python_exec_ptc_run` 返回后，子进程一定被 reap（wait/kill 收尸），
绝不留下孤儿/僵尸；且退出码诚实回传给 _PTC_LAST_STATS["returncode"]。

用 Popen 间谍捕获真实子进程对象，跑完断言 proc.poll() is not None（已终止且已收尸）：
  · 正常非零退出（sys.exit(7)）→ 服务环仍把 stdout 回收、returncode=7、无孤儿。
  · 超时 kill（死循环 + 极小 _PYTHON_EXEC_TIMEOUT）→ 报超时、kill 收尸、returncode 非零、无孤儿。
  · 断连（server 提前 close_child stdin）→ 子侧 stub readline 见 EOF 抛
    'bridge disconnected'、子进程非零退出、被收尸、无孤儿。
真起 `python -c` 子进程，仅本地模式。
"""

import os
import sys
import time
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tools as tools_mod  # noqa: E402
from config import set_runtime  # noqa: E402


class _OrphanHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_capture(self, code, server=None, exec_timeout=None):
        """跑服务环，并用 Popen 间谍捕获真实子进程对象，返回 (out, proc)。"""
        captured = {}
        real_popen = tools_mod.subprocess.Popen

        def spy(*a, **k):
            p = real_popen(*a, **k)
            captured["proc"] = p
            return p

        kw = {}
        if exec_timeout is not None:
            kw["_PYTHON_EXEC_TIMEOUT"] = exec_timeout
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            set_runtime("code_root", self.tmp)
            set_runtime("exec_approval", True)
            with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True), \
                    patch.object(tools_mod, "_PTC_BRIDGE_SERVER", server), \
                    patch.object(tools_mod.subprocess, "Popen", spy), \
                    patch.object(tools_mod, "_PTC_TRACE_PATH", None), \
                    patch.object(tools_mod, "_PYTHON_EXEC_TIMEOUT",
                                 exec_timeout if exec_timeout is not None
                                 else tools_mod._PYTHON_EXEC_TIMEOUT):
                out = tools_mod._python_exec_ptc_run(code, ceiling=1, allow_escalate=False)
        return out, captured.get("proc")


class NoOrphanTests(_OrphanHarness):
    def test_nonzero_exit_is_reaped_not_orphan(self):
        def server(name, arg):
            return (True, "1")

        code = ('ok, r = calculate("1")\n'
                'print("DONE", r)\n'
                'import sys\n'
                'sys.exit(7)')
        out, proc = self._run_capture(code, server)
        self.assertIsNotNone(proc, "应捕获到子进程对象")
        # 返回后子进程必须已被收尸（poll 非 None = 已终止且父已 wait）。
        self.assertIsNotNone(proc.poll(), "子进程未被收尸 → 疑似孤儿/僵尸")
        self.assertNotEqual(proc.returncode, 0)
        st = tools_mod._PTC_LAST_STATS
        self.assertEqual(st.get("returncode"), proc.returncode)  # 退出码诚实回传
        self.assertIn("DONE", out)

    def test_timeout_kill_leaves_no_orphan(self):
        # 死循环 + 极小超时 → 服务环 kill + wait 收尸；不得残留运行中的子进程。
        code = ('import time\n'
                'while True:\n'
                '    time.sleep(0.05)')
        t0 = time.monotonic()
        out, proc = self._run_capture(code, server=lambda n, a: (True, "x"),
                                      exec_timeout=2)
        self.assertIsNotNone(proc)
        self.assertIsNotNone(proc.poll(), "超时后子进程未被 kill/收尸 → 孤儿")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("\u8d85\u65f6", out)                 # "超时"
        # 收尸应发生在超时后不久，不无限等待。
        self.assertLess(time.monotonic() - t0, 20)

    def test_disconnect_child_exits_and_is_reaped(self):
        # 断连语义：server 收到第一个请求后主动关闭子进程 stdin，模拟"父侧断连"。
        # 子侧 stub 的 _bridge_call 在 readline() 见 EOF → 抛 'bridge disconnected'
        # → 子进程非零退出、被服务环 wait 收尸、无孤儿。
        holder = {}

        def server(name, arg):
            # 关掉 stdin 触发子侧 EOF；返回一个占位回执前先关闭管道。
            try:
                holder["proc"].stdin.close()
            except Exception:  # noqa: BLE001
                pass
            return (False, "\u65ad\u8fde")  # "断连"

        real_popen = tools_mod.subprocess.Popen

        def spy(*a, **k):
            p = real_popen(*a, **k)
            holder["proc"] = p
            return p

        code = ('ok, r = calculate("1")\n'
                'print("AFTER", ok)\n'        # 断连后可能永远走不到这里
                'print("END")')
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            set_runtime("code_root", self.tmp)
            set_runtime("exec_approval", True)
            with patch.object(tools_mod, "_PTC_BRIDGE_ENABLED", True), \
                    patch.object(tools_mod, "_PTC_BRIDGE_SERVER", server), \
                    patch.object(tools_mod.subprocess, "Popen", spy), \
                    patch.object(tools_mod, "_PTC_TRACE_PATH", None):
                out = tools_mod._python_exec_ptc_run(code, ceiling=1, allow_escalate=False)
            proc = holder.get("proc")
        self.assertIsNotNone(proc)
        self.assertIsNotNone(proc.poll(), "断连后子进程未被收尸 → 孤儿")
        # 无论退出码，运行函数必须已返回（没挂起），且不留活口。
        self.assertNotEqual(proc.poll(), None)


if __name__ == "__main__":
    unittest.main()
