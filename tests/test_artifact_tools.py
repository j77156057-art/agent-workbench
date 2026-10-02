import json
import os
import tempfile
import unittest
from unittest import mock

import artifact_tools
import agent


class ArtifactToolsTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dm_artifacts_")
        self.patch = mock.patch.object(artifact_tools, "get_runtime", lambda key: self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def create(self, payload):
        result = json.loads(artifact_tools.create_artifact(json.dumps(payload, ensure_ascii=False)))
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(os.path.isfile(result["path"]))
        self.assertGreater(result["bytes"], 0)
        return result

    def test_creates_and_validates_docx(self):
        result = self.create({"format": "docx", "filename": "guide.docx", "title": "指南",
                              "sections": [{"heading": "范围", "paragraphs": ["内容"]}]})
        self.assertGreaterEqual(result["validation"]["paragraphs"], 2)

    def test_creates_and_validates_pdf(self):
        result = self.create({"format": "pdf", "filename": "guide.pdf", "title": "指南",
                              "sections": [{"heading": "范围", "paragraphs": ["内容"]}]})
        self.assertGreaterEqual(result["validation"]["pages"], 1)

    def test_project_intro_requires_real_sources_and_substantive_content(self):
        for name in ("project.godot", "player.gd"):
            with open(os.path.join(self.root, name), "w", encoding="utf-8") as file:
                file.write("current game project")
        payload = {
            "format": "pdf", "purpose": "project_intro", "filename": "game.pdf",
            "title": "游戏介绍", "source_files": ["project.godot", "player.gd"],
            "sections": [
                {"heading": heading, "paragraphs": ["项目文件显示的真实功能与待核实事项。" * 14]}
                for heading in ("玩法", "操作", "项目结构", "运行与验证")
            ],
        }
        result = self.create(payload)
        self.assertEqual(result["source_files"], ["project.godot", "player.gd"])
        self.assertGreaterEqual(result["validation"]["pages"], 1)

        payload["source_files"] = ["project.godot", "../outside.py"]
        rejected = json.loads(artifact_tools.create_artifact(json.dumps(payload, ensure_ascii=False)))
        self.assertFalse(rejected["ok"])
        self.assertIn("当前项目以外", rejected["error"])

        payload["source_files"] = ["project.godot", "player.gd"]
        payload["sections"] = [{"heading": "概述", "paragraphs": ["简短"]}]
        rejected = json.loads(artifact_tools.create_artifact(json.dumps(payload, ensure_ascii=False)))
        self.assertFalse(rejected["ok"])
        self.assertIn("过短", rejected["error"])

        # A weak model may omit purpose; the active user request still enforces it.
        payload.pop("purpose")
        token = artifact_tools.bind_project_intro_request(True)
        try:
            rejected = json.loads(artifact_tools.create_artifact(json.dumps(payload, ensure_ascii=False)))
        finally:
            artifact_tools.reset_project_intro_request(token)
        self.assertFalse(rejected["ok"])
        self.assertIn("过短", rejected["error"])

    def test_creates_and_validates_pptx(self):
        result = self.create({"format": "pptx", "filename": "review.pptx", "title": "Review",
                              "slides": [{"title": "Status", "bullets": ["Done"]}]})
        self.assertEqual(result["validation"]["slides"], 2)

    def test_creates_and_validates_xlsx_and_escapes_formulas(self):
        result = self.create({"format": "xlsx", "filename": "review.xlsx",
                              "sheets": [{"name": "Data", "headers": ["Value"],
                                          "rows": [["=HYPERLINK(\"x\")"]]}]})
        from openpyxl import load_workbook
        workbook = load_workbook(result["path"], data_only=False)
        self.assertEqual(workbook["Data"]["A2"].value, "'=HYPERLINK(\"x\")")
        workbook.close()

    def test_rejects_directory_in_filename(self):
        result = json.loads(artifact_tools.create_artifact(json.dumps({
            "format": "pdf", "filename": "../outside.pdf", "title": "x"
        })))
        self.assertFalse(result["ok"])
        self.assertIn("filename", result["error"])

    def test_existing_file_gets_unique_name(self):
        first = self.create({"format": "docx", "filename": "same.docx", "title": "One"})
        second = self.create({"format": "docx", "filename": "same.docx", "title": "Two"})
        self.assertNotEqual(first["path"], second["path"])

    def test_artifact_generation_uses_existing_write_guardrails(self):
        # 断言运行期的 ToolSpec，而不是 agent._WRITE_TOOLS / _NO_PARALLEL_TOOLS
        # 那两张运行期不读的死副本（断言它们等于校验一份与真实行为无关的清单）。
        from agent_runtime.tools import Capability, SideEffect, coerce_tool_spec
        spec = coerce_tool_spec("create_artifact", __import__("tools").TOOLS["create_artifact"])
        self.assertEqual(spec.capability, Capability.WRITE_LOCAL)
        self.assertEqual(spec.side_effect, SideEffect.MUTATING)
        self.assertFalse(spec.parallel_safe)
        self.assertTrue(agent._has_write_intent("请生成一份 PDF 报告"))
        self.assertFalse(agent._has_write_intent("请审查这份 PDF 报告"))


if __name__ == "__main__":
    unittest.main()
