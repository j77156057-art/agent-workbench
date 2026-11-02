"""P1 Web 导出辅助逻辑测试（不依赖真实 Godot/网络）。"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_export as we  # noqa: E402


class WebExportHelpersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docmind-web-")
        with open(os.path.join(self.tmp, "project.godot"), "w", encoding="utf-8") as f:
            f.write('config_version=5\n\n[application]\nconfig/name="T"\n')

    def _read_proj(self):
        with open(os.path.join(self.tmp, "project.godot"), encoding="utf-8") as f:
            return f.read()

    def test_ensure_bridge_injects_and_idempotent(self):
        changed = we.ensure_bridge(self.tmp)
        self.assertIn(we.BRIDGE_SCRIPT_REL, changed)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "addons", "docmind_bridge", "docmind_bridge.gd")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, ".docmind", ".gdignore")))
        proj = self._read_proj()
        self.assertIn("[autoload]", proj)
        self.assertIn('DocmindBridge="*res://addons/docmind_bridge/docmind_bridge.gd"', proj)
        # 第二次无变更
        self.assertEqual(we.ensure_bridge(self.tmp), [])

    def test_ensure_bridge_keeps_existing_autoload(self):
        with open(os.path.join(self.tmp, "project.godot"), "w", encoding="utf-8") as f:
            f.write('[autoload]\n\nGlobalThing="*res://global.gd"\n')
        we.ensure_bridge(self.tmp)
        proj = self._read_proj()
        self.assertIn('GlobalThing="*res://global.gd"', proj)
        self.assertIn('DocmindBridge=', proj)

    def test_web_preset_create_and_dedup(self):
        added, idx = we.ensure_web_preset(self.tmp)
        self.assertTrue(added)
        self.assertEqual(idx, 0)
        with open(os.path.join(self.tmp, "export_presets.cfg"), encoding="utf-8") as handle:
            cfg = handle.read()
        self.assertIn('platform="Web"', cfg)
        self.assertIn(".docmind/web/index.html", cfg)
        added2, idx2 = we.ensure_web_preset(self.tmp)
        self.assertFalse(added2)
        self.assertEqual(idx2, 0)

    def test_web_preset_appends_after_existing(self):
        with open(os.path.join(self.tmp, "export_presets.cfg"), "w", encoding="utf-8") as f:
            f.write('[preset.0]\n\nname="Win"\nplatform="Windows Desktop"\n')
        added, idx = we.ensure_web_preset(self.tmp)
        self.assertTrue(added)
        self.assertEqual(idx, 1)

    def test_play_token_stable_and_bound_to_path(self):
        t1 = we.play_token(self.tmp)
        t2 = we.play_token(os.path.abspath(self.tmp) + os.sep + "." + os.sep)
        self.assertEqual(t1, t2)
        self.assertEqual(len(t1), 16)

    def test_root_for_token_cache_and_fallback(self):
        token = we.play_token(self.tmp)
        we._PLAY_TOKEN_ROOTS.pop(token, None)
        self.addCleanup(lambda: we._PLAY_TOKEN_ROOTS.pop(token, None))

        # 浏览器 GET /play/<token> 不带项目头，必须能从 token 反查 root。
        # Fallback：扫描项目注册表 + code_root。
        fake_projects = type(sys)('fake_projects')
        fake_projects.list_projects = lambda: [{'root': self.tmp}]
        fake_config = type(sys)('fake_config')
        fake_config.get_runtime = lambda key: None
        fake_config.CODE_ROOT = ''
        with patch.dict(sys.modules, {'projects': fake_projects, 'config': fake_config}):
            self.assertEqual(we.root_for_token(token), self.tmp)

        # 命中缓存后，不再依赖 sys.modules 也能返回。
        self.assertEqual(we.root_for_token(token), self.tmp)
        self.assertIsNone(we.root_for_token('not_a_token'))

    def test_resolve_play_file_and_traversal(self):
        web_dir = os.path.join(self.tmp, ".docmind", "web")
        os.makedirs(web_dir)
        with open(os.path.join(web_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html></html>")
        self.assertTrue(we.resolve_play_file(self.tmp, "index.html").endswith("index.html"))
        self.assertIsNone(we.resolve_play_file(self.tmp, "../../../etc/passwd"))
        self.assertIsNone(we.resolve_play_file(self.tmp, "nope.html"))

    def test_inject_html_bridge_idempotent(self):
        idx = os.path.join(self.tmp, "index.html")
        with open(idx, "w", encoding="utf-8") as f:
            f.write("<html><body><canvas></canvas></body></html>")
        self.assertTrue(we._inject_html_bridge(idx))
        with open(idx, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("__docmind_recv", text)
        self.assertFalse(we._inject_html_bridge(idx))

    def test_tpz_url_stable_only(self):
        url, tag = we._tpz_url("4.7.2.stable.official.ed1daf0bf")
        self.assertEqual(tag, "4.7.2-stable")
        self.assertIn("Godot_v4.7.2-stable_export_templates.tpz", url)
        with self.assertRaises(ValueError):
            we._tpz_url("4.8.beta1.official.abc")

    def test_template_dir_parsing(self):
        tdir = we._template_dir("4.7.2.stable.official.ed1daf0bf")
        self.assertTrue(tdir.endswith(os.path.join("export_templates", "4.7.2.stable")))
        self.assertIsNone(we._template_dir("bad"))

    def test_templates_status_missing_exe(self):
        st = we.templates_status(os.path.join(self.tmp, "no-such-godot.exe"))
        self.assertFalse(st["installed"])
        self.assertIsNone(st["version"])


if __name__ == "__main__":
    unittest.main()
