"""审批面回归：api_routes 经 ctx 模块对象取用 agent_policy 函数。

2026-11-17 实测：这类重导出对静态扫描不可见，曾被 ruff F401 自动修复删掉，
导致 /api/agent/approvals* 整组 500（AttributeError: module 'api' has no
attribute 'list_approvals'）。这里把「端点活着 + 门审批闭环」钉成用例。
"""
import os
import unittest
from unittest.mock import patch

import skills
from tests.test_agent_trace import _IsoBase


class ApprovalsSurfaceTests(_IsoBase):
    def test_approvals_endpoints_alive_and_gate_loop(self):
        import api
        from starlette.testclient import TestClient

        with patch.object(skills, "SKILLS_DIR", os.path.join(self.tmp, "skills")):
            skills.save_user_skill("ci-skill", "描述", "旧正文", version="1.0.0")
            with patch.object(api, "_request_project_id", return_value="p-approvals"), \
                    patch.object(api, "_project_root_or_error", return_value=str(self.tmp)):
                with TestClient(api.app) as client:
                    listed = client.get("/api/agent/approvals")
                    self.assertEqual(listed.status_code, 200)
                    self.assertTrue(listed.json()["ok"])
                    routing = client.get("/api/agent/routing")
                    self.assertEqual(routing.status_code, 200)
                    gated = client.post("/api/skills/update", json={
                        "name": "ci-skill", "description": "描述",
                        "body": "新正文", "version": "1.1.0"})
                    self.assertTrue(gated.json()["approval_required"])
                    pending = client.get("/api/agent/approval-requests").json()["items"]
                    self.assertTrue(any(x["action"] == "update_skill" for x in pending))
                    approved = client.post("/api/approval", json={
                        "action": "update_skill", "target": "ci-skill",
                        "approved": True, "user": "test"})
                    self.assertTrue(approved.json()["ok"])
                    done = client.post("/api/skills/update", json={
                        "name": "ci-skill", "description": "描述",
                        "body": "新正文", "version": "1.1.0"})
                    self.assertTrue(done.json()["ok"])
            saved = skills.get("ci-skill")
            self.assertIn("新正文", saved)


if __name__ == "__main__":
    unittest.main()