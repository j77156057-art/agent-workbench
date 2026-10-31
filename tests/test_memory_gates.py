"""宪法条款逐条回归：每个 machine_check 都必须落在真实代码闸门上。

这不是复述提示词：测试直接调用真实闸门（命令黑名单、回执门、脱敏、
删除确认），并检查宪法加载器会标出未绑定条款。
"""
import os
import unittest

import agent_memory  # noqa: F401 —— 确保 memory 包导入链与生产一致
from agent_runtime.memory import constitution, gates, promote, store, taskbook
from tests.test_memory_v2_p0 import _MemBase
from tools import _cmd_is_blocked


class GateRegistryTests(_MemBase):
    def test_default_clauses_all_have_gates(self):
        self.assertEqual(gates.missing_checks(constitution.DEFAULT_CLAUSES), [])
        checks = {item["machine_check"] for item in constitution.DEFAULT_CLAUSES}
        self.assertEqual(checks, set(gates.GATES))

    def test_unknown_check_is_flagged(self):
        self.assertEqual(
            gates.missing_checks([{"id": "C-999", "machine_check": "not_a_gate"}]),
            ["C-999"],
        )

    def test_constitution_load_marks_missing_gates(self):
        path = store.safe_path("constitution.md")
        store.atomic_write(path, "---\nversion: 1\nclauses:\n- id: C-999\n  text: 测试\n  machine_check: not_a_gate\n---\n正文\n")
        doc = constitution.load(ensure=True)
        self.assertFalse(doc["gates_ok"])
        self.assertEqual(doc["missing_gates"], ["C-999"])


class ClauseBehaviorTests(_MemBase):
    def test_c001_c002_git_writes_blocked(self):
        self.assertTrue(_cmd_is_blocked("git push --force")[0])
        self.assertTrue(_cmd_is_blocked("git push origin main")[0])
        self.assertTrue(_cmd_is_blocked("git commit -m test")[0])
        self.assertFalse(_cmd_is_blocked("git status")[0])

    def test_c003_done_requires_receipt(self):
        taskbook.open_book("prj", "任务", [{"text": "条目一"}])
        key = taskbook.items("prj")[0]["key"]
        with self.assertRaises(ValueError):
            taskbook.complete("prj", key)

    def test_c004_no_model_write_path(self):
        source = open("tools.py", encoding="utf-8").read()
        self.assertNotIn("constitution.save", source)
        self.assertNotIn("constitution.save_text", source)

    def test_c005_secret_rejected(self):
        self.assertTrue(promote.secret_hit("api_key=sk-abcdefghijklmnop"))

    def test_c006_destructive_commands_blocked(self):
        self.assertTrue(_cmd_is_blocked("rm -rf /")[0])
        self.assertTrue(_cmd_is_blocked("shutdown -s")[0])

    def test_c007_promotion_scrubs_paths(self):
        cleaned, reasons = promote.genericize(
            "配置文件在 D:\\work\\proj\\conf.ini，改完要重启验证", "prj")
        self.assertIn("<path>", cleaned)
        self.assertNotIn("D:\\work", cleaned)
        self.assertEqual(reasons, [])

    def test_c008_delete_needs_confirmation(self):
        with self.assertRaises(ValueError):
            gates.require_confirm(False, "删除共享记忆")
        gates.require_confirm(True, "删除共享记忆")


if __name__ == "__main__":
    unittest.main()
