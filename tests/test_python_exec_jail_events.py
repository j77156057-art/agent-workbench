# -*- coding: utf-8 -*-
"""python_exec 运行期审计钩·**事件分档**回归（真跑子进程，PEP-578 钩为权威判据）。

覆盖预扫描抓不到、只有钩在触发那一刻才拦得住的路径事件，逐档验证「按 _PJ_CEILING
放行/拦停」：
  · open(...,'w')            写文件；
  · os.remove / unlink / rmdir / shutil.rmtree   删除；
  · os.rename / replace / mkdir / link / symlink / truncate / chmod / shutil.copy/move  写侧；
  · os.system / os.popen / os.exec* / os.spawn* / subprocess.*  拉起子进程；
  · socket.connect / getaddrinfo / sendto        出网。

这里用 tools_mod._python_exec_run(code, ceiling=...) 直接喂子进程，绕开执行前静态预扫描，
专测钩本身。ceiling=1 时越界/子进程/出网一律 PermissionError；升到对应档位才放行。
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


class ExecHookEventTests(unittest.TestCase):
    def _run(self, code, ceiling=1, root=None):
        if root is not None:
            with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
                set_runtime("code_root", root)
                set_runtime("exec_approval", True)
                out = tools_mod._python_exec_run(code, ceiling=ceiling, allow_escalate=False)
            return root, out
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
                set_runtime("code_root", d)
                set_runtime("exec_approval", True)
                out = tools_mod._python_exec_run(code, ceiling=ceiling, allow_escalate=False)
            return d, out

    # ---------------------------------------------------------------- 删除事件
    def test_delete_outside_root_blocked_at_ceiling1(self):
        home = os.path.expanduser("~")
        probe = os.path.join(home, "__docmind_evt_del__.txt")
        try:
            open(probe, "w").write("x")
            _, out = self._run("import os; os.remove(%r)" % probe, ceiling=1)
            self.assertIn("安全拦截", out, "根外删除应被拦：%r" % out)
            self.assertIn("PermissionError", out, "应 PermissionError 中止：%r" % out)
            self.assertTrue(os.path.exists(probe), "被拦时文件绝不能真删")
        finally:
            if os.path.exists(probe):
                os.remove(probe)

    def test_delete_in_root_blocked_at_ceiling1_allowed_at_ceiling2(self):
        # victim 落在 code_root【内】→ 根内删除=第二档：ceiling=1 拦、ceiling=2 放行真删。
        with tempfile.TemporaryDirectory() as d:
            victim = os.path.join(d, "gone.txt")
            open(victim, "w").write("x")
            _, out = self._run("import os; os.remove(%r); print('DEL_OK')" % victim,
                               ceiling=1, root=d)
            self.assertIn("安全拦截", out, "根内删除在免批档应拦：%r" % out)
            self.assertTrue(os.path.exists(victim), "未授权时文件不能被真删")
            _, out2 = self._run("import os; os.remove(%r); print('DEL_OK')" % victim,
                                ceiling=2, root=d)
            self.assertIn("DEL_OK", out2, "根内删除在 ceiling=2 应放行：%r" % out2)
            self.assertFalse(os.path.exists(victim), "放行后应真的删除")

    # ---------------------------------------------------------------- shell / 子进程事件
    def test_os_system_blocked_at_ceiling1_allowed_at_ceiling3(self):
        _, blocked = self._run("import os; os.system('echo hi')", ceiling=1)
        self.assertIn("安全拦截", blocked, "os.system 在免批档应拦：%r" % blocked)
        _, ok = self._run("import os; os.system('echo hi')", ceiling=3)
        self.assertNotIn("安全拦截", ok, "os.system 在 ceiling=3 应放行：%r" % ok)

    def test_subprocess_popen_blocked_at_ceiling1(self):
        _, out = self._run("import subprocess; subprocess.Popen(['python', '-c', 'print(1)'])",
                           ceiling=1)
        self.assertIn("安全拦截", out, "子进程拉起在免批档应拦：%r" % out)
        self.assertIn("PermissionError", out, "应 PermissionError 中止：%r" % out)

    # ---------------------------------------------------------------- 出网事件
    def test_socket_connect_blocked_at_ceiling1(self):
        # 连一个不会真成功的地址即可——钩在 connect 触发瞬间就拦，轮不到网络结果。
        code = ("import socket; s=socket.socket(); "
                "s.connect(('127.0.0.1', 65000))")
        _, out = self._run(code, ceiling=1)
        self.assertIn("安全拦截", out, "出网在免批档应拦：%r" % out)

    def test_socket_connect_allowed_at_ceiling2(self):
        code = ("import socket; s=socket.socket();\n"
                "try:\n"
                "    s.connect(('127.0.0.1', 65000))\n"
                "except OSError:\n"
                "    print('NET_ATTEMPTED')")
        _, out = self._run(code, ceiling=2)
        # ceiling=2 放行网络事件本身；连接被拒是普通 OSError，不是安全拦截。
        self.assertNotIn("安全拦截", out, "出网在 ceiling=2 不应被钩拦：%r" % out)

    # ---------------------------------------------------------------- 写侧 rename/mkdir
    def test_mkdir_in_root_allowed_at_ceiling1(self):
        _, out = self._run("import os; os.mkdir('newdir'); print('MKDIR_OK')", ceiling=1)
        self.assertIn("MKDIR_OK", out, "根内建目录=第一档，应放行：%r" % out)

    def test_rename_onto_outside_root_blocked_at_ceiling1(self):
        home = os.path.expanduser("~")
        dst = os.path.join(home, "__docmind_evt_rename_dst__.txt")
        try:
            _, out = self._run(
                "open('src.txt','w').write('x'); import os; os.rename('src.txt', %r)" % dst,
                ceiling=1)
            self.assertIn("安全拦截", out, "越界 rename 目标应被拦：%r" % out)
            self.assertFalse(os.path.exists(dst), "被拦时目标绝不能落盘")
        finally:
            if os.path.exists(dst):
                os.remove(dst)


if __name__ == "__main__":
    unittest.main()
