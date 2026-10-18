# -*- coding: utf-8 -*-
"""python_exec 三档分级同意·**静态预扫描**分类器回归（tools.py:_python_exec_classify）。

分类器只决定「跑不跑 / 执行前要不要先弹窗请求同意」，返回 (tier, reason)，tier∈{-1,1,2,3}：
  · -1 永不放行硬底线（凭据/NT、系统关键目录、格式化/磁盘级命令）；
  · 1  免批（只读 / code_root 内增改）；
  · 2  一次确认（删除根内文件 / code_root 外新建 / 出网）；
  · 3  三连确认（删除或覆盖 code_root 外既有文件 / 拉起子进程 shell）。

权威判定仍在子进程运行期审计钩（另见 test_python_exec_jail.py / Ceiling / Events）；
本文件只锁预扫描档位，不真跑子进程。
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


class ExecClassifyTests(unittest.TestCase):
    def _tier(self, code):
        """在一次性 code_root 下跑分类器，返回最坏档位。"""
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
                set_runtime("code_root", d)
                return tools_mod._python_exec_classify(code)[0]

    # ---------------------------------------------------------------- 第一档（免批）
    def test_pure_compute_is_tier1(self):
        self.assertEqual(self._tier("print(1 + 1)"), 1)

    def test_read_only_is_tier1(self):
        self.assertEqual(self._tier("open('x.txt').read()"), 1)

    def test_in_root_write_is_tier1(self):
        self.assertEqual(self._tier("open('a.txt', 'w').write('x')"), 1)

    def test_in_root_relative_create_is_tier1(self):
        # 相对路径必须锚到 code_root（子进程 cwd 基准），否则会被误判越界。
        self.assertEqual(self._tier("open('sub/a.txt', 'w').write('x')"), 1)

    def test_in_root_copy_is_tier1(self):
        self.assertEqual(self._tier("import shutil; shutil.copy('a', 'b')"), 1)

    # ---------------------------------------------------------------- 第二档（一次确认）
    def test_in_root_delete_is_tier2(self):
        self.assertEqual(self._tier("import os; os.remove('a.txt')"), 2)

    def test_new_file_outside_root_is_tier2(self):
        home = os.path.expanduser("~")
        probe = os.path.join(home, "__docmind_classify_new__.txt")
        self.assertFalse(os.path.exists(probe))
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            with tempfile.TemporaryDirectory() as d:
                set_runtime("code_root", d)
                self.assertEqual(tools_mod._python_exec_classify(
                    "open(%r, 'w').write('x')" % probe)[0], 2)

    def test_network_import_is_tier2(self):
        self.assertEqual(self._tier("import socket; socket.socket()"), 2)
        self.assertEqual(self._tier("import urllib.request"), 2)

    # ---------------------------------------------------------------- 第三档（三连确认）
    def test_delete_existing_outside_root_is_tier3(self):
        home = os.path.expanduser("~")
        probe = os.path.join(home, "__docmind_classify_del__.txt")
        try:
            open(probe, "w").write("x")  # 既有文件 → 删除 = 不可回滚
            with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
                with tempfile.TemporaryDirectory() as d:
                    set_runtime("code_root", d)
                    self.assertEqual(tools_mod._python_exec_classify(
                        "import os; os.remove(%r)" % probe)[0], 3)
        finally:
            if os.path.exists(probe):
                os.remove(probe)

    def test_overwrite_existing_outside_root_is_tier3(self):
        home = os.path.expanduser("~")
        probe = os.path.join(home, "__docmind_classify_over__.txt")
        try:
            open(probe, "w").write("x")  # 覆盖既有文件（非新建）
            with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
                with tempfile.TemporaryDirectory() as d:
                    set_runtime("code_root", d)
                    self.assertEqual(tools_mod._python_exec_classify(
                        "open(%r, 'w').write('y')" % probe)[0], 3)
        finally:
            if os.path.exists(probe):
                os.remove(probe)

    def test_subprocess_import_is_tier3(self):
        self.assertEqual(self._tier("import subprocess; subprocess.run(['ls'])"), 3)

    def test_os_system_shell_is_tier3(self):
        # 非危险命令的 shell 拉起 = 第三档（越出 python_exec 计算用途）
        self.assertEqual(self._tier("import os; os.system('dir')"), 3)

    def test_shutil_rmtree_outside_root_is_tier3(self):
        home = os.path.expanduser("~")
        probe = os.path.join(home, "__docmind_classify_rmtree__")
        os.makedirs(probe, exist_ok=True)
        try:
            with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
                with tempfile.TemporaryDirectory() as d:
                    set_runtime("code_root", d)
                    self.assertEqual(tools_mod._python_exec_classify(
                        "import shutil; shutil.rmtree(%r)" % probe)[0], 3)
        finally:
            import shutil as _sh
            _sh.rmtree(probe, ignore_errors=True)

    # ---------------------------------------------------------------- -1 永不放行硬底线
    def test_credential_write_is_never(self):
        home = os.path.expanduser("~")
        probe = os.path.join(home, ".ssh", "id_rsa")
        self.assertEqual(self._tier("open(%r, 'w').write('x')" % probe), -1)

    def test_system_dir_write_is_never(self):
        self.assertEqual(self._tier("open(r'C:/Windows/System32/evil.dll', 'w').write('x')"), -1)

    def test_format_command_is_never(self):
        self.assertEqual(self._tier("import os; os.system('format C: /Y')"), -1)

    def test_rm_rf_root_is_never(self):
        self.assertEqual(self._tier("import os; os.system('rm -rf /')"), -1)

    # ---------------------------------------------------------------- 反误伤（call_name 回归）
    def test_list_remove_not_mistaken_for_os_remove(self):
        # 裸方法名 .remove / .system 若不加 os./shutil./subprocess. 限定会假阳，
        # 这是曾经修过的 bug（mylist.remove(x) 被当成删除文件）。
        self.assertEqual(self._tier("x = [1, 2]; x.remove(1); print(x)"), 1)

    def test_syntax_error_falls_back_to_tier2(self):
        # 解析不了的代码往保守侧靠：宁可多问一次，绝不免批放行。
        self.assertEqual(self._tier("def broken(: pass"), 2)


if __name__ == "__main__":
    unittest.main()
