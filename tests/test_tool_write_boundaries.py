"""写边界契约：确认哈希、五工具统一入口与敏感内容识别。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_runtime.enterprise_sandbox import SandboxUnavailable, enforce_file_write_boundary


class ToolWriteBoundaryTests(unittest.TestCase):
    def test_confirm_edit_rejects_hash_change_without_overwriting(self):
        import tools
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "a.txt")
            target.write_text("old\n", encoding="utf-8")
            pid = tools.stage_edit("apply_edit", str(target), "a.txt", "old\n", "new\n", "diff")
            target.write_text("someone-else\n", encoding="utf-8")
            ok, reason = tools.confirm_edit(pid)
            self.assertFalse(ok)
            self.assertIn("哈希", reason)
            self.assertEqual(target.read_text(encoding="utf-8"), "someone-else\n")

    def test_sensitive_content_is_blocked_even_with_safe_filename(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}):
            with self.assertRaises(SandboxUnavailable):
                enforce_file_write_boundary(project_root=root, target=str(Path(root, "notes.txt")),
                                            content="-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key")
            # 明确的示例占位值不应误伤正常开发文本。
            enforce_file_write_boundary(project_root=root, target=str(Path(root, "notes.txt")),
                                        content='api_key = "example-test-value"')

    def test_five_tools_share_the_same_content_boundary(self):
        import tools
        secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key"
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_external_access_mode", return_value="safe"):
            root = Path(temporary)
            with patch("tools._get_code_root", return_value=str(root)):
                generated = json.loads(tools.generate_data_file(json.dumps(
                    {"path": "notes.txt", "format": "txt", "data": secret})))
                self.assertFalse(generated["ok"])
                self.assertIn("SandboxUnavailable", generated["error"])
                self.assertFalse(Path(root, "notes.txt").exists())

                Path(root, "a.txt").write_text("one\n", encoding="utf-8")
                patch_text = "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,2 @@\n one\n+" + secret
                with patch("agent_runtime.patch_apply.apply_patch") as apply:
                    result = tools.dev_patch(patch_text)
                self.assertIn("SandboxUnavailable", result)
                apply.assert_not_called()

                Path(root, "source.txt").write_text(secret, encoding="utf-8")
                moved = tools.dev_move("from: source.txt\nto: destination.txt")
                self.assertIn("SandboxUnavailable", moved)
                self.assertFalse(Path(root, "destination.txt").exists())

                Path(root, "requirements.txt").write_text("", encoding="utf-8")
                report = {"declared_source": "requirements.txt", "declared_count": 0,
                          "missing_total": 1, "missing": [
                    {"distribution": "-----BEGIN RSA PRIVATE KEY-----", "module": "demo",
                     "importers": ["a.py"], "file_count": 1}]}
                with patch("agent_runtime.deps_sync.reconcile", return_value=report), \
                        patch("agent_runtime.deps_sync.apply") as apply_deps:
                    deps = tools.dev_deps("action: apply")
                self.assertIn("SandboxUnavailable", deps)
                apply_deps.assert_not_called()

                regions = Path(root, "regions")
                regions.mkdir()
                src_dir = Path(regions, "src")
                dst_dir = Path(regions, "dst")
                src_dir.mkdir()
                dst_dir.mkdir()
                Path(src_dir, "secret.txt").write_text(secret, encoding="utf-8")
                rmap = {"src": {"dir": "regions/src", "depends_on": []},
                        "dst": {"dir": "regions/dst", "depends_on": []}}
                with patch("tools._require_regions", return_value=((str(root), rmap), None)), \
                        patch("tools.create_file") as create:
                    moved_region = tools.dev_refactor(
                        "src_region: src\nsrc_path: secret.txt\ndst_region: dst\ndst_path: copied.txt")
                self.assertIn("拒绝搬移", moved_region)
                create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
