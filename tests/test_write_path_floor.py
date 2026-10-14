"""与 code_root 无关的地板（docs/known-defect-no-write-path-floor.md ③）。

验证 Hermes hardline blocklist 移植：凭据存储类 + NT 设备命名空间路径，
读写都拦、无 override；.env 类写可读禁。覆盖 §2 实测敞口与 §5「靠巧合成立」的护栏。
"""
import os
import sys
import unittest
from unittest import mock

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


class SymlinkFloorTest(unittest.TestCase):
    """审计 F1 回归：code_root 内的符号链接若经 realpath 解析到凭据 / 越界目标，必须被拦；
    指向普通文件的合法 symlink 放行。本沙箱 symlink 惰性（不跟随），故用 mock 模拟
    OS 跟随 symlink 的契约（realpath 返回目的地），使测试与平台无关、直接覆盖 F1 逻辑。"""

    def _fake_realpath(self, link_path, resolved_path):
        real = os.path.realpath
        lp = os.path.normpath(link_path)
        rp = os.path.normpath(resolved_path)

        def fake(p):
            if os.path.normpath(p) == lp:
                return rp
            return real(p)

        return mock.patch.object(T.os.path, "realpath", side_effect=fake)

    def test_floor_check_resolved_blocks_credential_symlink(self):
        home = os.path.normpath(os.path.expanduser("~"))
        root_abs = os.path.normpath(ROOT)
        link = os.path.join(ROOT, "innocent_link.py")
        cred = os.path.join(home, ".ssh", "id_rsa")
        with self._fake_realpath(link, cred):
            r = T._floor_check_resolved(root_abs, link)
        self.assertIsNotNone(r)
        self.assertIn("安全策略拒绝", r)

    def test_floor_check_resolved_blocks_root_escape(self):
        root_abs = os.path.normpath(ROOT)
        link = os.path.join(ROOT, "escape_link.py")
        outside = os.path.normpath(os.path.join(ROOT, "..", "sibling", "x.txt"))
        with self._fake_realpath(link, outside):
            r = T._floor_check_resolved(root_abs, link)
        self.assertIsNotNone(r)
        self.assertIn("代码根目录之外", r)

    def test_floor_check_resolved_allows_normal_symlink(self):
        root_abs = os.path.normpath(ROOT)
        link = os.path.join(ROOT, "normal_link.py")
        normal = os.path.join(ROOT, "real_file.py")
        with self._fake_realpath(link, normal):
            r = T._floor_check_resolved(root_abs, link)
        self.assertIsNone(r)

    def test_floor_check_resolved_fail_closed_on_realpath_error(self):
        root_abs = os.path.normpath(ROOT)
        link = os.path.join(ROOT, "broken_link.py")

        def raise_fn(p):
            raise OSError("bad symlink")

        with mock.patch.object(T.os.path, "realpath", side_effect=raise_fn):
            r = T._floor_check_resolved(root_abs, link)
        self.assertIsNotNone(r)
        self.assertIn("路径解析失败", r)

    def test_resolve_in_root_blocks_credential_symlink(self):
        link = os.path.join(ROOT, "innocent_link.py")
        home = os.path.normpath(os.path.expanduser("~"))
        cred = os.path.join(home, ".ssh", "id_rsa")
        with mock.patch.object(T, "_get_code_root", return_value=ROOT):
            with self._fake_realpath(link, cred):
                target, _root_abs, reason = T._resolve_in_root(link)
        self.assertIsNone(target)
        self.assertIn("安全策略拒绝", reason)

    def test_resolve_create_path_blocks_credential_symlink(self):
        link = os.path.join(ROOT, "innocent_link.py")
        home = os.path.normpath(os.path.expanduser("~"))
        cred = os.path.join(home, ".ssh", "id_rsa")
        with mock.patch.object(T, "_get_code_root", return_value=ROOT):
            with self._fake_realpath(link, cred):
                target, _root_abs, reason = T._resolve_create_path(link)
        self.assertIsNone(target)
        self.assertIn("安全策略拒绝", reason)

    def test_resolve_in_root_allows_normal_symlink(self):
        link = os.path.join(ROOT, "normal_link.py")
        normal = os.path.join(ROOT, "real_file.py")
        with mock.patch.object(T, "_get_code_root", return_value=ROOT):
            with self._fake_realpath(link, normal):
                target, _root_abs, reason = T._resolve_in_root(link)
        self.assertIsNotNone(target)
        self.assertIsNone(reason)


class FloorCheckedTest(unittest.TestCase):
    """审计 F3 回归：_resolve_floor_checked 统一入口 = 字面地板 + realpath 闭合（F1）。"""

    def test_bundles_literal_floor_and_symlink_closure(self):
        home = os.path.normpath(os.path.expanduser("~"))
        root_abs = os.path.normpath(ROOT)
        # 字面凭据（无需 realpath）→ 被拦
        self.assertIsNotNone(
            T._resolve_floor_checked(root_abs, os.path.join(home, ".ssh", "id_rsa")))
        # 字面普通项目文件 → 放行
        self.assertIsNone(
            T._resolve_floor_checked(root_abs, os.path.join(ROOT, "a.py")))
        # symlink → 凭据（realpath 解析）→ 被拦
        link = os.path.join(ROOT, "link.py")
        cred = os.path.join(home, ".ssh", "id_rsa")
        real = os.path.realpath
        with mock.patch.object(
                T.os.path, "realpath",
                side_effect=lambda p: cred if os.path.normpath(p) == os.path.normpath(link) else real(p)):
            self.assertIsNotNone(T._resolve_floor_checked(root_abs, link))
        # 外部白名单路径只重跑地板（check_root=False），越界目的地不闭合回 code_root
        outside = os.path.normpath(os.path.join(ROOT, "..", "sibling", "x.txt"))
        self.assertIsNone(
            T._resolve_floor_checked(None, outside, check_root=False))


if __name__ == "__main__":
    unittest.main()
