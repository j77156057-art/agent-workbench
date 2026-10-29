# -*- coding: utf-8 -*-
"""工作台切项目必须换会话：会话在服务端终身归属首次使用的项目。

2026-10-29 真机/HTTP 复现：同一 session 先在 A 项目提问，再切到 B 提问 ——
B 的对话被追加进 A 的会话（A 能看到 B 的问题），B 自己刷新后是 0 轮
（owner_project_id != B 被隐藏），且会话的 code_root 仍绑 A。
本文件锁住前端「按项目记住会话、切项目选回/新建」的契约。
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ProjectSessionScopeTests(unittest.TestCase):
    def setUp(self):
        self.dock = (ROOT / "frontend/src/workbench/components/ChatDock.vue").read_text(
            encoding="utf-8")

    def test_project_sessions_are_remembered_per_project(self):
        self.assertIn("docmind.workbenchSession:", self.dock)
        self.assertIn("rememberProjectSession", self.dock)
        self.assertIn("recalledProjectSession", self.dock)

    def test_project_change_is_handled_by_session_switch(self):
        self.assertIn("'docmind:project-context-changed', onProjectChanged", self.dock)
        self.assertNotIn("'docmind:project-context-changed', resetChatContext", self.dock)
        block = self.dock[self.dock.index("async function onProjectChanged"):]
        block = block[:block.index("\nfunction onPageHide")]
        self.assertIn("rememberProjectSession(chatProject, chatSession)", block)
        self.assertIn("createSession()", block, "目标项目没有记录时必须新建会话")
        self.assertIn("selectSession(sid)", block, "有记录时切回该项目自己的会话")
        self.assertIn("restoreHistory(true)", block)

    def test_session_switches_update_the_project_mapping(self):
        for fn in ("switchSession", "createConversation"):
            start = self.dock.index("async function " + fn)
            block = self.dock[start:start + 1200]
            self.assertIn("rememberProjectSession(chatProject, chatSession)", block,
                          f"{fn} 改了会话却没更新项目映射")


if __name__ == "__main__":
    unittest.main()
