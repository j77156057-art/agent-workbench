"""单 workflow 端点的项目归属校验。

之前所有按 `workflow_id` 访问的端点都直接信任 `WORKFLOWS.get` 返回值，
未校验 `state.project_id/project_root` 是否匹配当前项目，导致已知
ID 可跨项目读取/订阅/审批/执行/回滚/删除。`api_routes.agent`
新加的 `require_workflow_project` 把这两道闸补齐。

测试只覆盖端到端：跨项目请求 404 且不修改另一项目工作流状态。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api_routes.agent as api_agent
from agent_runtime.game_workflow import GameWorkflowManager


class _CtxStub:
    """模拟构建时使用的 ctx；`build_router` 把整个对象作为闭包捕获。"""

    def __init__(self, project_id, project_root):
        self._project_id = project_id
        self._project_root = project_root

    def _project_root_or_error(self):
        return self._project_root

    def _request_project_id(self):
        return self._project_id


def _make_client(manager, project_id, project_root):
    api_agent.WORKFLOWS = manager
    router = api_agent.build_router(_CtxStub(project_id, project_root))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class WorkflowOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project_a = self.tmp / "project_a"
        self.project_b = self.tmp / "project_b"
        self.project_a.mkdir()
        self.project_b.mkdir()
        self.manager = GameWorkflowManager(str(self.tmp / "_state"))
        self.wid_a = self.manager.start("A", project_id="pid-a",
                                         project_root=str(self.project_a))["workflow_id"]
        self.wid_b = self.manager.start("B", project_id="pid-b",
                                         project_root=str(self.project_b))["workflow_id"]

    def tearDown(self):
        self.manager.close()

    # ----- 读端点 --------------------------------------------------------

    def test_same_project_get_returns_200(self):
        client = _make_client(self.manager, "pid-a", str(self.project_a))
        resp = client.get(f"/api/agent/workflow/{self.wid_a}")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["ok"])

    def test_cross_project_get_returns_404(self):
        client = _make_client(self.manager, "pid-a", str(self.project_a))
        resp = client.get(f"/api/agent/workflow/{self.wid_b}")
        self.assertEqual(resp.status_code, 404, resp.text)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertNotIn("_status", body)

    def test_cross_project_evaluation_returns_404(self):
        client = _make_client(self.manager, "pid-a", str(self.project_a))
        resp = client.get(f"/api/agent/workflow/{self.wid_b}/evaluation")
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_cross_project_skill_candidate_returns_404(self):
        client = _make_client(self.manager, "pid-a", str(self.project_a))
        resp = client.get(f"/api/agent/workflow/{self.wid_b}/skill-candidate")
        self.assertEqual(resp.status_code, 404, resp.text)

    # ----- 写端点 --------------------------------------------------------

    def test_cross_project_interrupt_returns_404_and_does_not_change(self):
        client = _make_client(self.manager, "pid-a", str(self.project_a))
        resp = client.post(f"/api/agent/workflow/{self.wid_b}/interrupt",
                            json={"reason": "x"})
        self.assertEqual(resp.status_code, 404, resp.text)
        state_b = self.manager.get(self.wid_b)
        self.assertNotIn(state_b["status"], ("interrupted", "failed", "completed"),
                         f"B 状态被意外修改：{state_b['status']}")

    def test_cross_project_delete_returns_404(self):
        client = _make_client(self.manager, "pid-a", str(self.project_a))
        # 先把 B 推到终态，否则 delete 本身会拒
        self.manager._load(self.wid_b)  # 确保加载
        # 模拟终态：直接走 internal setter
        state = self.manager._load(self.wid_b)
        state.status = "completed"
        self.manager._save(state)
        resp = client.delete(f"/api/agent/workflow/{self.wid_b}")
        self.assertEqual(resp.status_code, 404, resp.text)
        # B 仍存在
        self.assertIsNotNone(self.manager.get(self.wid_b))

    def test_cross_project_resume_returns_404(self):
        client = _make_client(self.manager, "pid-a", str(self.project_a))
        resp = client.post(f"/api/agent/workflow/{self.wid_b}/resume")
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_cross_project_checkpoint_returns_404(self):
        client = _make_client(self.manager, "pid-a", str(self.project_a))
        resp = client.post(f"/api/agent/workflow/{self.wid_b}/checkpoint",
                            json={"approved": False})
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_cross_project_skill_approve_returns_404(self):
        client = _make_client(self.manager, "pid-a", str(self.project_a))
        resp = client.post(f"/api/agent/workflow/{self.wid_b}/skill-candidate/approve",
                            json={"approved": True})
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_cross_project_acceptance_decide_returns_404(self):
        client = _make_client(self.manager, "pid-a", str(self.project_a))
        resp = client.post(f"/api/agent/workflow/{self.wid_b}/acceptance/decide",
                            json={"approved": True, "note": "x"})
        self.assertEqual(resp.status_code, 404, resp.text)

    # ----- SSE -----------------------------------------------------------

    def test_cross_project_sse_returns_404(self):
        client = _make_client(self.manager, "pid-a", str(self.project_a))
        resp = client.get(f"/api/agent/workflow/{self.wid_b}/events")
        self.assertEqual(resp.status_code, 404, resp.text)


if __name__ == "__main__":
    unittest.main()
