# -*- coding: utf-8 -*-
"""python_exec 三档分级同意·**待确认往返**回归（tools.py:python_exec / stage_exec /
list_pending_execs / confirm_exec / reject_exec）。

镜像 dev 写工具那套 pending→人工点击→落盘的往返，只是这里一次批准的是「整段代码待执行」：
  · 第二档（一次确认）：确认 1 次即真跑；未确认前绝不落盘。
  · 第三档（三连确认）：点不满 3 次只递增计数、返回「还需 N 次」且**不执行**；
    满 3 次才以该档为天花板真跑。
  · 拒绝：直接丢弃，代码永不运行。
  · 免批档：执行前不进 pending、直接跑。
  · 硬底线：执行前就 __EXEC_BLOCKED__ 拒，根本不进 pending；即便强行登记，确认后
    运行期钩仍二次复核把凭据写拦死。
"""

import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tools as tools_mod  # noqa: E402
from config import set_runtime  # noqa: E402


def _pid_of(out):
    m = re.search(r"__EXEC_PENDING__ pid=(\S+)", out)
    return m.group(1) if m else None


class ExecApprovalFlowTests(unittest.TestCase):
    def setUp(self):
        # 每条用例独立：清掉全局待确认表，避免互相串味。
        tools_mod._PENDING_EXEC.clear()

    def tearDown(self):
        tools_mod._PENDING_EXEC.clear()

    def _python_exec(self, code, root):
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            set_runtime("code_root", root)
            set_runtime("exec_approval", True)
            return tools_mod.python_exec(code)

    # ---------------------------------------------------------------- 免批档
    def test_tier1_runs_directly_no_pending(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._python_exec("open('inroot.txt', 'w').write('x'); print('DIRECT_OK')", d)
            self.assertIn("DIRECT_OK", out, "根内写应直接跑：%r" % out)
            self.assertNotIn("__EXEC_PENDING__", out)
            self.assertTrue(os.path.exists(os.path.join(d, "inroot.txt")))
            self.assertEqual(tools_mod.list_pending_execs(), [])

    # ---------------------------------------------------------------- 硬底线：执行前就拒
    def test_never_floor_blocked_no_pending(self):
        home = os.path.expanduser("~")
        probe = os.path.join(home, ".ssh", "id_rsa")
        with tempfile.TemporaryDirectory() as d:
            out = self._python_exec("open(%r, 'w').write('x')" % probe, d)
            self.assertIn("__EXEC_BLOCKED__", out, "凭据写应执行前硬拒：%r" % out)
            self.assertEqual(tools_mod.list_pending_execs(), [], "永不放行档不该进 pending")

    # ---------------------------------------------------------------- 第二档：一次确认即跑
    def test_tier2_one_click_executes(self):
        home = os.path.expanduser("~")
        target = os.path.join(home, "__docmind_flow_new2__.txt")
        try:
            with tempfile.TemporaryDirectory() as d:
                out = self._python_exec("open(%r, 'w').write('x')" % target, d)
                self.assertIn("__EXEC_PENDING__", out)
                self.assertIn("tier=2", out)
                self.assertIn("need=1", out)
                pid = _pid_of(out)
                self.assertIsNotNone(pid)
                self.assertFalse(os.path.exists(target), "未确认前绝不能落盘")
                pend = tools_mod.list_pending_execs()
                self.assertEqual(len(pend), 1)
                self.assertEqual(pend[0]["need"], 1)
                self.assertEqual(pend[0]["tier"], 2)
                ok, res = tools_mod.confirm_exec(pid)
                self.assertTrue(ok, "第二档确认 1 次即应执行：%r" % res)
                self.assertTrue(os.path.exists(target), "确认后文件应真落盘")
                self.assertEqual(tools_mod.list_pending_execs(), [], "执行后 pending 应清空")
        finally:
            if os.path.exists(target):
                os.remove(target)

    # ---------------------------------------------------------------- 第三档：三连确认计数门
    def test_tier3_requires_three_clicks(self):
        home = os.path.expanduser("~")
        victim = os.path.join(home, "__docmind_flow_del3__.txt")
        try:
            open(victim, "w").write("x")  # 既有文件 → 根外删除 = 第三档
            with tempfile.TemporaryDirectory() as d:
                out = self._python_exec("import os; os.remove(%r)" % victim, d)
                self.assertIn("__EXEC_PENDING__", out)
                self.assertIn("tier=3", out)
                self.assertIn("need=3", out)
                pid = _pid_of(out)
                # 第 1 次：不达标，只递增计数、绝不执行。
                ok1, msg1 = tools_mod.confirm_exec(pid)
                self.assertFalse(ok1, "三连确认第 1 次不该执行：%r" % msg1)
                self.assertIn("还需", msg1)
                self.assertTrue(os.path.exists(victim), "未达阈值绝不能真删")
                # 第 2 次：仍差 1。
                ok2, msg2 = tools_mod.confirm_exec(pid)
                self.assertFalse(ok2, "第 2 次仍不该执行：%r" % msg2)
                self.assertTrue(os.path.exists(victim))
                pend = tools_mod.list_pending_execs()
                self.assertEqual(len(pend), 1)
                self.assertEqual(pend[0]["confirms"], 2)
                # 第 3 次：达标，真跑，文件删除，pending 清空。
                ok3, res3 = tools_mod.confirm_exec(pid)
                self.assertTrue(ok3, "第 3 次应执行：%r" % res3)
                self.assertFalse(os.path.exists(victim), "确认后应真的删除")
                self.assertEqual(tools_mod.list_pending_execs(), [])
        finally:
            if os.path.exists(victim):
                os.remove(victim)

    # ---------------------------------------------------------------- 拒绝：丢弃不执行
    def test_reject_discards_without_running(self):
        home = os.path.expanduser("~")
        target = os.path.join(home, "__docmind_flow_reject__.txt")
        try:
            with tempfile.TemporaryDirectory() as d:
                out = self._python_exec("open(%r, 'w').write('x')" % target, d)
                pid = _pid_of(out)
                self.assertIsNotNone(pid)
                self.assertTrue(tools_mod.reject_exec(pid))
                self.assertEqual(tools_mod.list_pending_execs(), [])
                self.assertFalse(os.path.exists(target), "拒绝后代码绝不能运行")
                # 重复拒绝不存在 id → False（幂等安全）。
                self.assertFalse(tools_mod.reject_exec(pid))
        finally:
            if os.path.exists(target):
                os.remove(target)

    # ---------------------------------------------------------------- 确认时硬底线复现拦
    def test_confirm_rechecks_hardline_at_ceiling(self):
        """即便强行把写凭据的代码登记成第三档并连点 3 次，运行期钩仍二次复核拦死。"""
        home = os.path.expanduser("~")
        probe = os.path.join(home, ".ssh", "docmind_flow_confirm_probe")
        try:
            code = "open(%r, 'w').write('x')" % probe
            with tempfile.TemporaryDirectory() as d:
                with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
                    set_runtime("code_root", d)
                    pid = tools_mod.stage_exec(code, 3, "删除 code_root 外既有文件", source="test")
                    tools_mod.confirm_exec(pid)
                    tools_mod.confirm_exec(pid)
                    ok, res = tools_mod.confirm_exec(pid)  # 第 3 次达标 → 真跑
                self.assertTrue(ok, "达标后应进入执行分支：%r" % res)
                self.assertIn("安全拦截", res, "运行期钩应复核拦死凭据写：%r" % res)
            self.assertFalse(os.path.exists(probe), "三连确认也绝不能写出凭据文件")
        finally:
            if os.path.exists(probe):
                os.remove(probe)


if __name__ == "__main__":
    unittest.main()
