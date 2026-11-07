"""视觉 HTTP 点击：统一桌面审批、稳定 ticket 幂等与单次提案消费。"""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import game_workbench
from agent_runtime import enterprise_sandbox, visual_click
from agent_runtime.visual_targeting import target_fingerprint


def proposal_fixture(age_seconds):
    raw = bytes([20, 40, 60, 255] * 16)
    bbox = [0.2, 0.2, 0.8, 0.8]
    entry = {
        "project_id": "pid", "hwnd": 123, "width": 4, "height": 4,
        "capture_target": "embedded", "bbox": bbox, "point": {"x": 2, "y": 2},
        "fingerprint": target_fingerprint(raw, 4, 4, bbox),
        "label": "保存", "evidence": "可见保存文字", "goal": "",
        "image": "data:image/jpeg;base64,abc", "created_at": time.time() - age_seconds,
        "used": False,
    }
    return entry, (raw, 4, 4, 123)


class VisionExecuteApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        enterprise_sandbox._DESKTOP_RESULTS.clear()
        for target, value in (
            ("api._http_origin_allowed", True),
            ("api._vision_action_project", ("pid", None)),
            ("api._project_root_or_error", self.root),
            ("agent_runtime.tools.stage_origin_root", None),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        from starlette.testclient import TestClient
        import api
        self.client = TestClient(api.app)

    def tearDown(self):
        enterprise_sandbox._DESKTOP_RESULTS.clear()
        self.tmp.cleanup()

    def post(self, proposal_id):
        return self.client.post("/api/vision/execute-click", json={
            "proposal_id": proposal_id, "confirmed": True,
        })

    def test_strict_pending_then_one_click_and_repeat_is_idempotent(self):
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_desktop_approval_mode", return_value="strict"), \
                patch("agent_runtime.visual_click.execute", return_value={
                    "ok": True, "executed": True, "used": True, "label": "保存",
                    "point": {"x": 1, "y": 2}, "verification": None,
                }) as perform:
            first = self.post("strict-one")
            self.assertEqual(first.status_code, 202, first.text)
            pending = first.json()
            self.assertEqual(pending["state"], "pending")
            self.assertTrue(pending["pending"])
            self.assertEqual(pending["ticket_id"], "vision-strict-one")
            self.assertFalse(pending["executed"])
            self.assertIsNone(pending["result"])
            self.assertEqual(perform.call_count, 0, "confirmed=true 不能直接点")

            game_workbench.approval(self.root, "desktop_action", "pytest-user",
                                    approved=True, target=pending["target"])
            second = self.post("strict-one")
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json()["state"], "completed")
            self.assertTrue(second.json()["executed"])
            self.assertEqual(perform.call_count, 1)

            game_workbench.approval(self.root, "desktop_action", "pytest-user",
                                    approved=True, target=pending["target"])
            third = self.post("strict-one")
            self.assertEqual(third.status_code, 200)
            self.assertTrue(third.json()["idempotent"])
            self.assertEqual(perform.call_count, 1, "重复批准不得第二次物理点击")

    def test_user_mode_also_waits_for_real_ledger_approval(self):
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_desktop_approval_mode", return_value="user"), \
                patch("agent_runtime.visual_click.execute", return_value={
                    "ok": True, "executed": True, "used": True,
                }) as perform:
            first = self.post("user-one")
            self.assertEqual(first.status_code, 202)
            self.assertEqual(perform.call_count, 0)
            game_workbench.approval(self.root, "desktop_action", "pytest-user",
                                    approved=True, target=first.json()["target"])
            second = self.post("user-one")
            self.assertEqual(second.status_code, 200)
            self.assertEqual(perform.call_count, 1)

    def test_model_auto_approval_executes_once_and_leaves_audit(self):
        import project_state
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_desktop_approval_mode", return_value="model"), \
                patch("agent_runtime.enterprise_sandbox._model_desktop_decision",
                      return_value=(True, "仅点击项目内保存按钮", "model:test")), \
                patch("agent_runtime.visual_click.execute", return_value={
                    "ok": True, "executed": True, "used": True,
                }) as perform:
            response = self.post("model-one")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(perform.call_count, 1)
            self.assertEqual(response.json()["approval"]["actor"], "model:test")
            path = project_state.path(self.root, "desktop_approvals.jsonl")
            rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["channel"], "vision")
            self.assertEqual(rows[-1]["decision"], "approved")
            self.assertEqual(rows[-1]["actor"], "model:test")
            self.assertTrue(rows[-1]["outcome"]["ok"])

    def test_terminal_result_survives_restart_and_pending_is_not_cached(self):
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_desktop_approval_mode", return_value="strict"), \
                patch("agent_runtime.visual_click.execute", return_value={
                    "ok": True, "executed": True, "used": True, "label": "保存",
                    "point": {"x": 1, "y": 2}, "verification": None,
                }) as perform:
            ticket = "vision-restart-one"
            path = enterprise_sandbox._desktop_result_path(self.root, ticket)
            first = self.post("restart-one")
            self.assertEqual(first.status_code, 202)
            self.assertFalse(path.exists(), "pending 不得落终态缓存")
            game_workbench.approval(self.root, "desktop_action", "pytest-user",
                                    approved=True, target=first.json()["target"])
            second = self.post("restart-one")
            self.assertEqual(second.status_code, 200)
            self.assertEqual(perform.call_count, 1)
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["ticket"], ticket)
            self.assertEqual(record["approved_by"], "user:approval_queue")
            self.assertTrue(record["executed_at"])
            self.assertTrue(record["result"]["executed"])

            third = self.post("restart-one")
            self.assertTrue(third.json()["idempotent"])
            expected = third.json()
            enterprise_sandbox._DESKTOP_RESULTS.clear()  # 模拟新进程内存为空
            perform.reset_mock()
            fourth = self.post("restart-one")
            self.assertEqual(fourth.status_code, 200, fourth.text)
            self.assertEqual(fourth.json(), expected)
            self.assertTrue(fourth.json()["idempotent"])
            self.assertEqual(perform.call_count, 0, "重启后必须从磁盘返回，不得再次执行")

    def test_result_file_does_not_persist_request_secrets(self):
        secret = "DO_NOT_PERSIST_API_KEY_123456"
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            result = enterprise_sandbox.execute_desktop_action(
                root=self.root, channel="vision", action="click", target="embedded",
                details={"proposal_id": "safe", "authorization": secret},
                ticket="vision-safe", executor=lambda: {"ok": True, "executed": True})
        self.assertTrue(result["ok"])
        path = enterprise_sandbox._desktop_result_path(self.root, "vision-safe")
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        self.assertNotIn("authorization", raw)
        self.assertEqual(json.loads(raw)["result"], {"ok": True, "executed": True})

    def test_corrupt_result_file_is_safely_ignored_and_atomically_replaced(self):
        ticket = "vision-broken"
        path = enterprise_sandbox._desktop_result_path(self.root, ticket)
        path.write_text('{"schema":1,"ticket":"vision-broken","result":', encoding="utf-8")
        self.assertIsNone(enterprise_sandbox._read_desktop_result(self.root, ticket))
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}), \
                patch("agent_runtime.enterprise_sandbox._execute_desktop_action",
                      return_value={"ok": True, "executed": True}) as perform:
            result = enterprise_sandbox.execute_desktop_action(
                root=self.root, channel="vision", action="click", target="embedded",
                details={"proposal_id": "broken"}, ticket=ticket, executor=lambda: None)
        self.assertTrue(result["ok"])
        self.assertEqual(perform.call_count, 1)
        self.assertEqual(enterprise_sandbox._read_desktop_result(self.root, ticket)["executed"], True)

    def test_ttl_defaults_to_300_seconds_and_is_configurable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(visual_click.proposal_ttl_seconds(), 300.0)
        with patch.dict(os.environ, {"DOCMIND_VISION_PROPOSAL_TTL_SECONDS": "450"}):
            self.assertEqual(visual_click.proposal_ttl_seconds(), 450.0)

    def test_within_300_seconds_can_approve_and_execute(self):
        entry, frame = proposal_fixture(295)
        with patch.dict(visual_click.PROPOSALS, {"within-300": entry}, clear=True), \
                patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_desktop_approval_mode", return_value="strict"), \
                patch("screen_capture.grab_embedded", return_value=frame), \
                patch("screen_capture.encode_frame", return_value="data:image/jpeg;base64,abc"), \
                patch("agent_runtime.visual_click.resolve_vision_llm", return_value=(None, "", "")), \
                patch("desktop_actions._resolve_target", return_value=(123, {
                    "origin": {"x": 0, "y": 0}, "rect": {"width": 4, "height": 4}}, "")), \
                patch("desktop_actions.perform", return_value={"ok": True, "events": 2}) as perform, \
                patch("agent_runtime.visual_click.time.sleep", return_value=None):
            first = self.post("within-300")
            self.assertEqual(first.status_code, 202)
            game_workbench.approval(self.root, "desktop_action", "pytest-user",
                                    approved=True, target=first.json()["target"])
            second = self.post("within-300")
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json()["state"], "completed")
            self.assertEqual(perform.call_count, 1)

    def test_expired_proposal_renews_then_requires_new_approval_and_clicks_once(self):
        entry, frame = proposal_fixture(301)
        ticket = "vision-expired-renewal"
        with patch.dict(visual_click.PROPOSALS, {"expired-renewal": entry}, clear=True), \
                patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "enterprise"}), \
                patch("config.get_desktop_approval_mode", return_value="strict"), \
                patch("screen_capture.grab_embedded", return_value=frame), \
                patch("screen_capture.encode_frame", return_value="data:image/jpeg;base64,abc"), \
                patch("agent_runtime.visual_click.resolve_vision_llm", return_value=(None, "", "")), \
                patch("desktop_actions._resolve_target", return_value=(123, {
                    "origin": {"x": 0, "y": 0}, "rect": {"width": 4, "height": 4}}, "")), \
                patch("desktop_actions.perform", return_value={"ok": True, "events": 2}) as perform, \
                patch("agent_runtime.visual_click.time.sleep", return_value=None):
            first = self.post("expired-renewal")
            self.assertEqual(first.status_code, 202)
            game_workbench.approval(self.root, "desktop_action", "pytest-user",
                                    approved=True, target=first.json()["target"])
            renewed = self.post("expired-renewal")
            self.assertEqual(renewed.status_code, 202, renewed.text)
            self.assertTrue(renewed.json()["renewed"])
            self.assertEqual(renewed.json()["renewal"], 1)
            self.assertNotEqual(renewed.json()["target"], first.json()["target"])
            self.assertIn("无需重新定位", renewed.json()["renewal_message"])
            self.assertFalse(entry["used"])
            self.assertEqual(perform.call_count, 0)
            self.assertFalse(enterprise_sandbox._desktop_result_path(self.root, ticket).exists())

            game_workbench.approval(self.root, "desktop_action", "pytest-user",
                                    approved=True, target=renewed.json()["target"])
            completed = self.post("expired-renewal")
            self.assertEqual(completed.status_code, 200, completed.text)
            self.assertEqual(completed.json()["state"], "completed")
            self.assertEqual(perform.call_count, 1)
            replay = self.post("expired-renewal")
            self.assertTrue(replay.json()["idempotent"])
            self.assertEqual(perform.call_count, 1)

    def test_consumed_proposal_cannot_be_replayed(self):
        entry = {"project_id": "pid", "used": True, "created_at": time.time()}
        with patch.dict(visual_click.PROPOSALS, {"used-once": entry}, clear=True):
            result = visual_click.execute("pid", self.root, proposal_id="used-once", confirmed=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 409)
        self.assertIn("已经执行过一次", result["error"])


if __name__ == "__main__":
    unittest.main()
