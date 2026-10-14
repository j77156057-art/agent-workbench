"""与 code_root 无关的地板（docs/known-defect-no-write-path-floor.md ③）。

验证 Hermes hardline blocklist 移植：凭据存储类 + NT 设备命名空间路径，
读写都拦、无 override；.env 类写可读禁。覆盖 §2 实测敞口与 §5「靠巧合成立」的护栏。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools as T  # noqa: E402


class HardFloorTest(unittest.TestCase):
    def test_credential_store_blocked_everywhere(self):
        home = os.path.normpath(os.path.expanduser("~"))
        cases = [
            os.path.join(home, ".ssh", "id_rsa"),
            os.path.join(home, ".aws", "credentials"),
            os.path.join(home, ".kube", "config"),
            os.path.join(home, ".netrc"),
            os.path.join(home, ".gnupg", "pubring.gpg"),
            # §2 泄漏行：HOME 的上级目录写 .netrc（code_root=上级时）
            os.path.join(os.path.dirname(home), ".netrc"),
            # 项目内同名目录（强化：即便 code_root 指向含同名目录的项目也拦）
            os.path.join(ROOT, ".ssh", "id_rsa"),
            # /etc/sudoers
            "/etc/sudoers",
            "C:/etc/sudoers",
        ]
        for c in cases:
            with self.subTest(c):
                self.assertIsNotNone(
                    T._hard_floor_reason(c),
                    msg="凭据路径必须被硬地板拦截：%s" % c)

    def test_normal_paths_allowed(self):
        cases = [
            os.path.join(ROOT, "a.py"),
            os.path.join(ROOT, "src", "main.py"),
            # .env 写允许（仅读被拒），不属于硬地板
            os.path.join(ROOT, ".env"),
            "/usr/local/bin/tool",
        ]
        for c in cases:
            with self.subTest(c):
                self.assertIsNone(
                    T._hard_floor_reason(c),
                    msg="普通路径不应被硬地板拦截：%s" % c)

    def test_nt_device_prefix_explicit(self):
        # 显式识别（修复 §5「靠巧合成立」的设计债）
        self.assertEqual(T._nt_device_prefix(r"\??\UNC\evil\share"), "\\??\\")
        self.assertEqual(T._nt_device_prefix(r"\\.\PhysicalDrive0"), "\\\\.\\")
        self.assertEqual(T._nt_device_prefix(r"\\?\GLOBALROOT\Device"),
                         "\\\\?\\GLOBALROOT")
        self.assertEqual(T._nt_device_prefix(r"\\?\C:\ok.txt"), "\\\\?\\")
        # 合法 UNC（\\server\share）不应被识别为设备命名空间（Hermes：不拦）
        self.assertIsNone(T._nt_device_prefix(r"\\server\share\x"))

    def test_nt_device_blocked_by_floor(self):
        for p in (r"\??\UNC\evil\share", r"\\.\PhysicalDrive0",
                  r"\\?\C:\ok.txt"):
            with self.subTest(p):
                reason = T._hard_floor_reason(p)
                self.assertIsNotNone(reason, msg="NT 设备路径必须被拦：%s" % p)
                self.assertIn("NTLM", reason)

    def test_env_read_denied(self):
        # .env 类读被拒，但普通文件不被误伤
        self.assertTrue(T._env_read_denied(os.path.join(ROOT, ".env")))
        self.assertTrue(T._env_read_denied(os.path.join(ROOT, ".env.production")))
        self.assertTrue(T._env_read_denied(os.path.join(ROOT, ".envrc")))
        self.assertFalse(T._env_read_denied(os.path.join(ROOT, "config.py")))

    def test_resolve_in_root_blocks_credential_regardless_of_root(self):
        home = os.path.normpath(os.path.expanduser("~"))
        # 不论 code_root 设成什么，凭据路径都被地板拦截（返回 (None, root_abs, reason)）
        target, root_abs, reason = T._resolve_in_root(
            os.path.join(home, ".ssh", "id_rsa"))
        self.assertIsNone(target)
        self.assertIn("安全策略拒绝", reason)

    def test_resolve_in_root_allows_normal_project_file(self):
        target, root_abs, reason = T._resolve_in_root(
            os.path.join(ROOT, "a.py"))
        # 正常项目文件：要么解析成功，要么因越界被拒（但不会是安全策略拒绝）
        if target is None:
            self.assertNotIn("安全策略拒绝", reason or "")


if __name__ == "__main__":
    unittest.main()
