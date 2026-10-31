# -*- coding: utf-8 -*-
"""Plan 长跑 API 契约：只开放开发台、只由用户停止、运行期间独占会话。

三条闸门各钉一条，漏一条的后果都很具体：
- 没有 surface 校验 → 问答页（面向外部用户）也能启动无人值守长跑；
- 没有 confirm → 一次误点的 POST 就停掉用户挂了一夜的 Plan；
- 聊天不互斥 → 用户消息和 runner 同时驱动同一会话，历史互相踩。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402

_WORKBENCH = {"X-DocMind-Surface": "workbench"}


def _post_plan(client, url, payload, headers=None):
    return client.post(url, json=payload, headers=headers or {})


class PlanSurfaceGateTests(unittest.TestCase):
    def test_plan_routes_closed_to_non_workbench(self):
        from starlette.testclient import TestClient
        with TestClient(api.app) as client:
            for url, payload in (
                ("/api/plan/start", {"session_id": "s", "goal": "跑完全部任务"}),
                ("/api/plan/stop", {"session_id": "s", "confirm": True}),
            ):
                with self.subTest(url=url):
                    resp = _post_plan(client, url, payload)
                    self.assertEqual(resp.status_code, 403,
                                     "缺少 X-DocMind-Surface 也必须拒绝")
                    self.assertIn("开发台", resp.json()["detail"])
            resp = client.get("/api/plan/status?session_id=s")
            self.assertEqual(resp.status_code, 403)
            resp = client.get("/api/plan/log?session_id=s")
            self.assertEqual(resp.status_code, 403)

    def test_workbench_surface_passes_gate(self):
        """反向守卫：闸门不能把开发台自己也一并挡死——403 之外的错误才算过了 header 校验。"""
        from starlette.testclient import TestClient
        with patch.object(api, "get_memory_v2", return_value=False), \
                patch.object(api, "_request_project_id", return_value="p"), \
                TestClient(api.app) as client:
            resp = _post_plan(client, "/api/plan/start",
                              {"session_id": "s", "goal": "跑完全部任务"}, _WORKBENCH)
            self.assertEqual(resp.status_code, 409,
                             "应停在「记忆 v2 未启用」，而不是 surface 403")


class PlanStartGuardTests(unittest.TestCase):
    def _client(self):
        from starlette.testclient import TestClient
        return patch.object(api, "get_memory_v2", return_value=True), TestClient(api.app)

    def test_goal_required(self):
        flag, client = self._client()
        with flag, patch.object(api, "_request_project_id", return_value="p"), client as c:
            resp = _post_plan(c, "/api/plan/start", {"session_id": "s", "goal": "  "},
                              _WORKBENCH)
            self.assertEqual(resp.status_code, 400)
            self.assertIn("goal", resp.json()["detail"])

    def test_session_required(self):
        flag, client = self._client()
        with flag, patch.object(api, "_request_project_id", return_value="p"), client as c:
            resp = _post_plan(c, "/api/plan/start", {"session_id": "", "goal": "干完"},
                              _WORKBENCH)
            self.assertEqual(resp.status_code, 400)

    def test_stop_requires_confirm(self):
        from starlette.testclient import TestClient
        with patch.object(api, "_request_project_id", return_value="p"), \
                TestClient(api.app) as c:
            resp = _post_plan(c, "/api/plan/stop", {"session_id": "s"}, _WORKBENCH)
            self.assertEqual(resp.status_code, 400)
            self.assertIn("confirm", resp.json()["detail"])

    def test_start_rejects_session_already_running_plan(self):
        flag, client = self._client()
        with flag, patch.object(api, "_request_project_id", return_value="p"), \
                patch("agent_runtime.plan_runner.is_active", return_value=True), \
                client as c:
            resp = _post_plan(c, "/api/plan/start",
                              {"session_id": "s", "goal": "干完"}, _WORKBENCH)
            self.assertEqual(resp.status_code, 409)


class ChatExclusionTests(unittest.TestCase):
    def test_chat_rejected_while_plan_running(self):
        from starlette.testclient import TestClient
        with patch("agent_runtime.plan_runner.is_active", return_value=True), \
                patch.object(api, "_request_project_id", return_value="p"), \
                TestClient(api.app) as client:
            resp = client.post("/api/chat", data={"question": "顺手问一句",
                                                  "session_id": "s-plan-api"},
                               headers={"X-DocMind-Project": "p"})
            self.assertEqual(resp.status_code, 409)
            self.assertIn("Plan", resp.json()["error"])

    def test_chat_allowed_when_no_plan(self):
        """反向守卫：`is_active` 判错方向会把普通问答全量打死，这里必须放行到下一步。"""
        from starlette.testclient import TestClient
        with patch("agent_runtime.plan_runner.is_active", return_value=False), \
                patch.object(api, "_request_project_id", return_value="p"), \
                patch.object(api, "_read_chat_images", side_effect=AssertionError(
                    "已越过 Plan 闸门")) as _stub, \
                TestClient(api.app) as client:
            try:
                resp = client.post("/api/chat", data={"question": "普通问题",
                                                      "session_id": "s-plan-api2"},
                                   headers={"X-DocMind-Project": "p"})
            except AssertionError:
                return
            self.assertNotEqual(resp.status_code, 409)
            self.assertIsNotNone(_stub)


class PlanStatusPassthroughTests(unittest.TestCase):
    def test_status_returns_taskbook_and_log(self):
        from starlette.testclient import TestClient
        fixture = {"ok": True, "active": True,
                   "state": {"goal": "干完", "status": "running", "iteration": 3},
                   "taskbook": {"items": [{"key": "k1", "text": "第一步", "state": "todo"}],
                                "pending": 1, "done": 0, "total": 1},
                   "log": [{"kind": "turn", "text": "做了第一步", "ts": "2026-10-31T00:00:00+00:00"}]}
        with patch("agent_runtime.plan_runner.status", return_value=fixture), \
                patch.object(api, "_request_project_id", return_value="p"), \
                TestClient(api.app) as client:
            resp = client.get("/api/plan/status?session_id=s", headers=_WORKBENCH)
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertTrue(body["active"])
            self.assertEqual(body["taskbook"]["items"][0]["text"], "第一步")
            self.assertEqual(body["log"][0]["kind"], "turn")


if __name__ == "__main__":
    unittest.main()
