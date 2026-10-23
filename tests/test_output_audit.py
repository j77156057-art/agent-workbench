import os
import tempfile
import unittest
from pathlib import Path

from agent_runtime.output_audit import audit_evidence, audit_final_output, redact


class OutputAuditTests(unittest.TestCase):
    def test_redacts_common_credentials_and_reports_failure(self):
        report = audit_final_output({"status": "ok", "text": "api_key=super-secret-value"})
        self.assertFalse(report["ok"])
        self.assertIn("[REDACTED]", report["text"])
        self.assertTrue(any(item["kind"] == "credential" for item in report["failures"]))

    def test_structured_failures_include_recovery_and_evidence(self):
        report = audit_final_output({
            "status": "ok", "text": "完成",
            "file_changes": [{"path": "scripts/player.gd", "changed": True}],
            "tests": [{"name": "smoke", "ok": False}],
            "steps": [{"tool": "dev_verify", "ok": False}],
        })
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed_test_count"], 1)
        self.assertEqual(report["tool_failure_count"], 1)
        self.assertTrue(report["evidence"]["files"])
        self.assertTrue(all(item.get("recovery") for item in report["failures"]))

    def test_clean_output_passes(self):
        report = audit_final_output({"status": "ok", "text": "验证通过", "steps": []})
        self.assertTrue(report["ok"])
        self.assertEqual(redact("hello"), "hello")

    def test_location_answer_requires_read_and_valid_line(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "behaviors" / "enemy.gd"
            path.parent.mkdir()
            path.write_text("func _ready():\n\tadd_to_group(\"enemy\")\n", encoding="utf-8")
            answer = "敌人入口在 behaviors/enemy.gd:2。"
            missing = audit_final_output({
                "status": "ok", "text": answer,
                "trace": {"steps": [{"action": "search_code", "ok": True}]},
            }, question="敌人的巡逻逻辑在哪个文件？", code_root=root)
            self.assertFalse(missing["ok"])
            self.assertFalse(missing["evidence_audit"]["checks"]["read_file_present"])
            passed = audit_final_output({
                "status": "ok", "text": answer,
                "trace": {"steps": [
                    {"action": "search_code", "ok": True},
                    {"action": "read_file", "ok": True},
                ]},
            }, question="敌人的巡逻逻辑在哪个文件？", code_root=root)
            self.assertTrue(passed["ok"])

    def test_absolute_windows_reference_keeps_its_drive_prefix(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "shots" / "click.png"
            path.parent.mkdir()
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
            report = audit_final_output({
                "status": "ok", "text": "截图保存在 %s。" % path,
            }, question="截图保存在哪里？", code_root=root)
            audit = report["evidence_audit"]
            self.assertTrue(audit["checks"]["references_exist"],
                            "带盘符的绝对路径被切成相对路径 -> 真实文件也判打不开")
            self.assertEqual([Path(item["path"]) for item in audit["references"]], [path])

    def test_non_location_answer_does_not_require_source_read(self):
        self.assertTrue(audit_evidence({"text": "可以修改，但请先说明目标"}, question="可以修改文件吗？")["ok"])

    def test_invented_project_root_fails_even_without_code_root(self):
        missing = os.path.join(tempfile.gettempdir(),
                               "docmind_missing_root_9f3c1a7d")
        self.assertFalse(os.path.isdir(missing))
        audit = audit_evidence(
            {"text": "代码根目录：%s\n引用为 behaviors/enemy.gd:44。" % missing},
            question="解释一下这个项目")
        self.assertFalse(audit["checks"]["project_root_exists"])
        self.assertFalse(audit["gate_ok"])
        self.assertEqual(audit["verdict"], "FAIL")
        self.assertTrue(audit["content_fail"])
        self.assertEqual(audit["project_root_claims"][0]["path"], missing)

    def test_existing_project_root_passes(self):
        with tempfile.TemporaryDirectory() as root:
            audit = audit_evidence({"text": "项目根目录：%s" % root},
                                   question="解释一下这个项目")
            self.assertTrue(audit["checks"]["project_root_exists"])
            self.assertTrue(audit["gate_ok"])

    def test_negated_or_placeholder_root_is_not_judged(self):
        missing = os.path.join(tempfile.gettempdir(),
                               "docmind_missing_root_9f3c1a7d")
        audit = audit_evidence(
            {"text": "代码根目录不存在：%s" % missing},
            question="解释一下这个项目")
        self.assertNotIn("project_root_exists", audit["checks"])
        placeholder = audit_evidence(
            {"text": "代码根目录：D:\\path\\to\\project"},
            question="解释一下这个项目")
        self.assertNotIn("project_root_exists", placeholder["checks"])


if __name__ == "__main__":
    unittest.main()
