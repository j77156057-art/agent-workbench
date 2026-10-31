# -*- coding: utf-8 -*-
"""采纳/晋升失败时，错误里必须带上「在哪个项目里找的」。

真实踩过的坑：面板的采纳按请求头里的项目取候选，顶栏停在别的项目时同一句
「未找到待晋升候选」和「点了没生效」在界面上完全无法区分 —— 用户因此白按一次、
还回来问"是不是没落库"。这里钉住两件事：找不到要带出处；晋升门本身的原因不许被稀释。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402


class AdoptProjectHintTests(unittest.TestCase):
    def _post(self, path):
        from starlette.testclient import TestClient
        with patch.object(api, "get_memory_v2", return_value=True), \
                patch.object(api, "_request_project_id", return_value="prj-hint"), \
                patch.object(api.projects, "get_project",
                             return_value={"name": "orchid-loop", "root": "D:/x"}), \
                TestClient(api.app) as client:
            return client.post(path)

    def test_adopt_not_found_names_the_project(self):
        with patch("agent_runtime.memory.promote.adopt",
                   return_value={"ok": False, "reasons": ["未找到待晋升候选"]}):
            resp = self._post("/api/memory/items/mem-nope/adopt")
        self.assertEqual(resp.status_code, 404)
        detail = resp.json()["detail"]
        self.assertIn("未找到待晋升候选", detail)
        self.assertIn("orchid-loop", detail, "错误里没说是哪个项目 —— 和「点了没反应」无法区分")

    def test_promote_not_found_names_the_project(self):
        with patch("agent_runtime.memory.promote.promote",
                   return_value={"ok": False, "reasons": ["未找到候选条目"]}):
            body = self._post("/api/memory/items/mem-nope/promote").json()
        self.assertFalse(body["promoted"])
        self.assertIn("orchid-loop", body["reasons"][0])

    def test_gate_reasons_pass_through_untouched(self):
        """**反向守卫**：门本身的原因不许被加后缀稀释 —— 用户要照那句话去补条件。"""
        with patch("agent_runtime.memory.promote.promote",
                   return_value={"ok": False, "reasons": ["缺采纳/复用信号", "重要度不足"]}):
            body = self._post("/api/memory/items/mem-real/promote").json()
        self.assertEqual(body["reasons"], ["缺采纳/复用信号", "重要度不足"])

    def test_unresolvable_project_still_says_something(self):
        with patch("agent_runtime.memory.promote.adopt",
                   return_value={"ok": False, "reasons": ["未找到待晋升候选"]}), \
                patch.object(api.projects, "get_project", side_effect=RuntimeError("注册表读不动")):
            from starlette.testclient import TestClient
            with patch.object(api, "get_memory_v2", return_value=True), \
                    patch.object(api, "_request_project_id", return_value="prj-hint"), \
                    TestClient(api.app) as client:
                resp = client.post("/api/memory/items/mem-nope/adopt")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("prj-hint", resp.json()["detail"], "取不到名字时至少把 pid 说清楚")


if __name__ == "__main__":
    unittest.main()
