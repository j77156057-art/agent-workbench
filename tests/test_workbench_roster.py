"""卡片墙兜底重建（roster include_restored）的归属与诚实性判据。

快照被清理后以 sub-*.json 会话重建卡片：
- 终态不在会话文件里，状态必须标 unknown，不补造成 completed；
- 归属不明（owner_project_id 缺失）排除，且不误报成「其他项目」；
- 已有快照的 agent 不重复兜底；开关关闭时不扫会话。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import agent_workbench
import sessions


class RosterRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_roster_")
        self._old_dir = sessions.SESSIONS_DIR
        sessions.SESSIONS_DIR = self.tmp
        self._nodes = patch.object(agent_workbench, "_delegation_nodes", return_value=[])
        self._nodes.start()
        # 角色映射会 import agent（重）；本组只判归属与状态，置空即可
        self._roles = patch.object(agent_workbench, "_persona_role_map", return_value={})
        self._roles.start()

    def tearDown(self):
        self._roles.stop()
        self._nodes.stop()
        sessions.SESSIONS_DIR = self._old_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_sub(self, agent_id, owner, turns=None):
        data = {
            "session_id": "sub-" + agent_id,
            "owner_project_id": owner,
            "updated_at": "2026-11-16T00:00:00+00:00",
            "turns": turns if turns is not None else [
                {"user": "【调研员】找 X", "assistant": "找到了"},
            ],
        }
        path = os.path.join(self.tmp, "sub-" + agent_id + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    def _cards(self, project="prj-test", session="web-1", include_restored=True):
        out = agent_workbench.roster(project, session, include_restored=include_restored)
        self.assertTrue(out["ok"])
        return out, {card["agent_id"]: card for card in out["items"]}

    def test_restored_card_reports_unknown_status(self):
        self._write_sub("ag-aaa", "prj-test")
        out, cards = self._cards()
        self.assertEqual(out["restored_from_sessions"], 1)
        card = cards["ag-aaa"]
        self.assertEqual(card["status"], "unknown")
        self.assertEqual(card["status_label"], "结果未知")
        self.assertEqual(card["message_count"], 2)
        self.assertEqual(card["archive_source"], "sessions")
        self.assertEqual(card["source"], "session")
        self.assertFalse(card["is_main"])

    def test_owner_unknown_excluded_other_project_counted(self):
        self._write_sub("ag-bbb", "")
        self._write_sub("ag-ccc", "prj-other")
        out, cards = self._cards()
        self.assertNotIn("ag-bbb", cards)
        self.assertNotIn("ag-ccc", cards)
        self.assertEqual(out["restored_from_sessions"], 0)
        self.assertEqual(out["other_project_sessions"], 1)

    def test_agent_with_snapshot_not_restored_again(self):
        self._write_sub("ag-ddd", "prj-test")
        with patch.object(agent_workbench, "_delegation_nodes", return_value=[
                {"id": "ag-ddd", "session_id": "ask-9", "status": "completed",
                 "role": "researcher"}]):
            out, cards = self._cards()
        # 快照归属 ask-9 那面墙，web-1 不展示，也不兜底重建
        self.assertNotIn("ag-ddd", cards)
        self.assertEqual(out["restored_from_sessions"], 0)

    def test_no_restore_without_flag(self):
        self._write_sub("ag-eee", "prj-test")
        out, cards = self._cards(include_restored=False)
        self.assertNotIn("ag-eee", cards)
        self.assertEqual(out["restored_from_sessions"], 0)
        self.assertEqual(out["other_project_sessions"], 0)


if __name__ == "__main__":
    unittest.main()