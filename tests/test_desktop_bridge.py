"""桌面窗口桥测试（仅 Windows 运行后代进程枚举）。"""
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop_bridge as db  # noqa: E402


@unittest.skipUnless(os.name == "nt", "仅 Windows 测试窗口桥")
class DesktopBridgeTest(unittest.TestCase):
    def test_descendant_pids_includes_child(self):
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(4)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            time.sleep(0.8)
            ids = db._descendant_pids(os.getpid())
            self.assertIn(p.pid, ids)
            self.assertIn(os.getpid(), ids)
        finally:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    def test_find_window_returns_none_for_dead_pid(self):
        # 已退出且无窗口的进程不应误命中
        self.assertIsNone(db.find_window(999999999))

    def test_host_holder_roundtrip(self):
        self.assertIsNone(db.get_host())
        db.set_host(12345)
        self.assertEqual(db.get_host(), 12345)
        db.set_host(None)
        self.assertIsNone(db.get_host())

    def test_restore_window_zero_hwnd_is_safe(self):
        # 空句柄必须安全返回 ok=False，绝不抛异常（截图工具据此走兜底）。
        for bad in (0, None, ""):
            out = db.restore_window(bad)
            self.assertIsInstance(out, dict)
            self.assertFalse(out.get("ok"))

    def test_restore_window_dead_hwnd_is_safe(self):
        # 一个不是窗口的数字：is_window 为假 → ok=False、restored/foreground 皆 False，不抛。
        out = db.restore_window(999999999)
        self.assertIsInstance(out, dict)
        self.assertFalse(out.get("ok"))
        self.assertFalse(out.get("restored"))
        self.assertFalse(out.get("foreground"))


if __name__ == "__main__":
    unittest.main()
