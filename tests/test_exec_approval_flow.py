# -*- coding: utf-8 -*-
"""python_exec 执行策略回归（tools.py:python_exec / stage_exec / list_pending_execs /
confirm_exec / reject_exec）。

「留地板、拆其余」重构后的策略：
  · 免批档（1）、第二档（2）、第三档（3）：全部**直接执行**，不再登记 pending、
    不再要求人工连点确认。工作台定位是「能干活」，二/三档确认弹窗属于被拆掉的设卡。
  · 硬底线（-1）：凭据 / NT 设备 / 系统关键目录 / 格式化·磁盘级破坏 → 执行前就
    __EXEC_BLOCKED__ 拒，根本不进 pending；即便强行登记，确认后运行期钩仍二次复核把
    凭据写拦死。这条地板**永久保留**。
本文件因此改为断言：tier2/3 直接跑（无 pending、真落盘/真删），而 -1 仍被硬拦。
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

    # ---------------------------------------------------------------- 第二档：直接执行、无 pending
    def test_tier2_runs_directly_no_pending(self):
        home = os.path.expanduser("~")
        target = os.path.join(home, "__docmind_flow_new2__.txt")
        try:
            with tempfile.TemporaryDirectory() as d:
                out = self._python_exec("open(%r, 'w').write('x')" % target, d)
                self.assertNotIn("__EXEC_PENDING__", out,
                                 "第二档应直接执行、不再登记待确认：%r" % out)
                self.assertNotIn("__EXEC_BLOCKED__", out)
                self.assertTrue(os.path.exists(target), "第二档应直接落盘")
                self.assertEqual(tools_mod.list_pending_execs(), [],
                                 "拆掉审批后不该有任何 pending")
        finally:
            if os.path.exists(target):
                os.remove(target)

    # ---------------------------------------------------------------- 第三档：直接执行、无 pending
    def test_tier3_runs_directly_no_pending(self):
        home = os.path.expanduser("~")
        victim = os.path.join(home, "__docmind_flow_del3__.txt")
        try:
            open(victim, "w").write("x")  # 既有文件 → 根外删除 = 第三档
            with tempfile.TemporaryDirectory() as d:
                out = self._python_exec("import os; os.remove(%r)" % victim, d)
                self.assertNotIn("__EXEC_PENDING__", out,
                                 "第三档应直接执行、不再连点确认：%r" % out)
                self.assertNotIn("__EXEC_BLOCKED__", out)
                self.assertFalse(os.path.exists(victim), "第三档应直接删除")
                self.assertEqual(tools_mod.list_pending_execs(), [])
        finally:
            if os.path.exists(victim):
                os.remove(victim)

    # ---------------------------------------------------------------- 拆审批后无 pending 可拒
    def test_no_pending_created_for_tier2(self):
        home = os.path.expanduser("~")
        target = os.path.join(home, "__docmind_flow_reject__.txt")
        try:
            with tempfile.TemporaryDirectory() as d:
                out = self._python_exec("open(%r, 'w').write('x')" % target, d)
                # 不再产生 pending，因此没有 id 可拒；操作已直接执行。
                self.assertEqual(tools_mod.list_pending_execs(), [],
                                 "拆掉审批后不该登记任何 pending：%r" % out)
                self.assertTrue(os.path.exists(target), "应已直接执行")
                # 拒绝不存在的 id → False（幂等安全）。
                self.assertFalse(tools_mod.reject_exec("nonexistent-id"))
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
