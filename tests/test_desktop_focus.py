# -*- coding: utf-8 -*-
"""窗口聚焦回归：engine/embedded 走 desktop_bridge，foreground 才用原生 API。"""
import unittest
from unittest.mock import patch

import desktop_actions


class DesktopFocusTests(unittest.TestCase):
    def test_engine_target_uses_bridge_focus(self):
        with patch("desktop_bridge.focus", return_value={"ok": True}) as focus, \
                patch.object(desktop_actions, "_user32") as user32:
            ok, error = desktop_actions._focus(4242, "engine", "prj-1")
        self.assertTrue(ok)
        self.assertEqual(error, "")
        focus.assert_called_once_with(4242, hwnd_host=4242, keep_attached=False)
        user32.assert_not_called()

    def test_embedded_target_uses_host_window(self):
        with patch("desktop_bridge.host_hwnd", return_value=99), \
                patch("desktop_bridge.focus", return_value={"ok": True}) as focus:
            ok, error = desktop_actions._focus(12, "embedded", "prj-1")
        self.assertTrue(ok)
        self.assertEqual(error, "")
        focus.assert_called_once_with(12, hwnd_host=99, keep_attached=False)

    def test_bridge_failure_is_reported_not_faked(self):
        with patch("desktop_bridge.focus",
                   return_value={"ok": False, "error": "attached but no foreground"}):
            ok, error = desktop_actions._focus(4242, "engine", "prj-1")
        self.assertFalse(ok)
        self.assertIn("foreground", error)


if __name__ == "__main__":
    unittest.main()
