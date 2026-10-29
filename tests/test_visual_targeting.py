import base64
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import desktop_actions
import screen_capture
from agent_runtime.visual_targeting import (parse_visual_target,
                                            parse_visual_verification, target_still_matches)


def test_visual_target_parser_rejects_uncertain_or_outside_boxes():
    assert parse_visual_target('{"found":true,"label":"保存","evidence":"可见保存文字",'
                               '"bbox":[0.2,0.2,0.4,0.4],"confidence":0.9}')
    assert parse_visual_target('{"found":true,"label":"保存","evidence":"可见保存文字",'
                               '"bbox":[-0.2,0.2,0.4,0.4],"confidence":0.9}') is None
    assert parse_visual_target('{"found":true,"label":"保存","evidence":"可见保存文字",'
                               '"bbox":[0.2,0.2,0.4,0.4],"confidence":0.4}') is None
    assert not target_still_matches(bytes([10] * 20), bytes([90] * 20))


def test_visual_verification_parser_requires_evidence_and_confidence():
    assert parse_visual_verification('{"status":"met","evidence":"出现完成提示",'
                                     '"confidence":0.92,"next_target":"删除"}') == {
        "status": "met", "evidence": "出现完成提示", "confidence": 0.92, "next_target": ""}
    low = parse_visual_verification('{"status":"met","evidence":"似乎完成",'
                                    '"confidence":0.5,"next_target":""}')
    assert low["status"] == "uncertain"
    assert parse_visual_verification('{"status":"met","confidence":0.9}') is None
    assert parse_visual_verification('不是 JSON') is None


def test_visual_click_requires_review_and_uses_single_use_proposal():
    api._VISUAL_CLICK_PROPOSALS.clear()
    raw = bytes([20, 40, 60, 255] * 16)
    frame = (raw, 4, 4, 123)
    image = "data:image/jpeg;base64," + base64.b64encode(b"fake-jpeg").decode()

    class Model:
        capability = {"vision": "native"}

        def chat(self, messages, **kwargs):
            assert messages[0]["images"]
            return ('{"found":true,"label":"保存按钮","evidence":"可见保存文字",'
                    '"bbox":[0.2,0.2,0.8,0.8],"confidence":0.94}')

    with patch.object(api, "_ctx_project_id", return_value="project-test"), \
            patch.object(api, "_project_root_or_error", return_value=str(Path(__file__).resolve().parents[1])), \
            patch.object(api, "_get_cockpit_llm", return_value=(None, Model())), \
            patch("screen_capture.grab_embedded", return_value=frame), \
            patch("screen_capture.encode_frame", return_value=image), \
            patch("desktop_actions._resolve_target", return_value=(123, {}, "")), \
            patch("desktop_actions.perform", return_value={"ok": True, "events": 2}) as perform, \
            patch("game_workbench.approval", return_value={"approved": True}) as approval, \
            patch.object(api.time, "sleep", return_value=None):
        client = TestClient(api.app, headers={"X-DocMind-Project": "project-test"})
        located = client.post("/api/vision/locate-click", json={"description": "保存按钮"})
        assert located.status_code == 200
        proposal = located.json()
        assert proposal["ok"] is True
        assert proposal["point"] == {"x": 2, "y": 2}
        denied = client.post("/api/vision/execute-click", json={"proposal_id": proposal["proposal_id"]})
        assert denied.status_code == 400
        assert perform.call_count == 0
        confirmed = client.post("/api/vision/execute-click", json={
            "proposal_id": proposal["proposal_id"], "confirmed": True})
        assert confirmed.json()["executed"] is True
        assert confirmed.json()["after"] == image
        assert perform.call_count == 1
        approval.assert_called_once()
        replay = client.post("/api/vision/execute-click", json={
            "proposal_id": proposal["proposal_id"], "confirmed": True})
        assert replay.status_code == 409


def test_visual_click_refuses_changed_window():
    api._VISUAL_CLICK_PROPOSALS.clear()
    raw = bytes([20, 40, 60, 255] * 16)
    image = "data:image/jpeg;base64," + base64.b64encode(b"fake-jpeg").decode()

    class Model:
        capability = {"vision": "native"}

        def chat(self, messages, **kwargs):
            return ('{"found":true,"label":"保存按钮","evidence":"可见保存文字",'
                    '"bbox":[0.2,0.2,0.8,0.8],"confidence":0.94}')

    with patch.object(api, "_ctx_project_id", return_value="project-test"), \
            patch.object(api, "_project_root_or_error", return_value=str(Path(__file__).resolve().parents[1])), \
            patch.object(api, "_get_cockpit_llm", return_value=(None, Model())), \
            patch("screen_capture.grab_embedded", side_effect=[(raw, 4, 4, 123), (raw, 4, 4, 456)]), \
            patch("screen_capture.encode_frame", return_value=image), \
            patch("desktop_actions.perform") as perform:
        client = TestClient(api.app, headers={"X-DocMind-Project": "project-test"})
        proposal = client.post("/api/vision/locate-click", json={"description": "保存按钮"}).json()
        rejected = client.post("/api/vision/execute-click", json={
            "proposal_id": proposal["proposal_id"], "confirmed": True})
        assert rejected.status_code == 409
        perform.assert_not_called()


def test_visual_click_refuses_changed_target_pixels():
    api._VISUAL_CLICK_PROPOSALS.clear()
    before = bytes([20, 40, 60, 255] * 16)
    after = bytes([200, 180, 160, 255] * 16)

    class Model:
        capability = {"vision": "native"}

        def chat(self, messages, **kwargs):
            return ('{"found":true,"label":"保存按钮","evidence":"可见保存文字",'
                    '"bbox":[0.2,0.2,0.8,0.8],"confidence":0.94}')

    with patch.object(api, "_ctx_project_id", return_value="project-test"), \
            patch.object(api, "_project_root_or_error", return_value=str(Path(__file__).resolve().parents[1])), \
            patch.object(api, "_get_cockpit_llm", return_value=(None, Model())), \
            patch("screen_capture.grab_embedded", side_effect=[(before, 4, 4, 123), (after, 4, 4, 123)]), \
            patch("screen_capture.encode_frame", return_value="data:image/jpeg;base64,abc"), \
            patch("desktop_actions.perform") as perform:
        client = TestClient(api.app, headers={"X-DocMind-Project": "project-test"})
        proposal = client.post("/api/vision/locate-click", json={"description": "保存按钮"}).json()
        rejected = client.post("/api/vision/execute-click", json={
            "proposal_id": proposal["proposal_id"], "confirmed": True})
        assert rejected.status_code == 409
        assert "画面已变化" in rejected.json()["error"]
        perform.assert_not_called()


def test_visual_click_rejects_wrong_project_and_foreign_origin():
    api._VISUAL_CLICK_PROPOSALS.clear()
    with patch.object(api, "_ctx_project_id", return_value="project-test"), \
            patch("screen_capture.grab_embedded") as capture:
        client = TestClient(api.app)
        wrong_project = client.post("/api/vision/locate-click",
                                    headers={"X-DocMind-Project": "another-project"},
                                    json={"description": "保存按钮"})
        assert wrong_project.status_code == 400
        foreign_origin = client.post("/api/vision/locate-click",
                                     headers={"X-DocMind-Project": "project-test",
                                              "Origin": "https://other.example"},
                                     json={"description": "保存按钮"})
        assert foreign_origin.status_code == 403
        capture.assert_not_called()


def test_strict_embedded_target_never_falls_back_to_another_project():
    children = [{"hwnd": 12, "alive": True, "host": 34,
                 "placed": {"width": 400, "height": 300}}]
    with patch("desktop_bridge.embedded_children", return_value=children), \
            patch("desktop_bridge.hosts_snapshot", return_value={"current": 56}), \
            patch("desktop_bridge.host_hwnd", return_value=34):
        assert screen_capture._embedded_target("current", strict_project=True) is None
        assert screen_capture._embedded_target("current") == 12


def test_desktop_click_rechecks_target_after_focus():
    geometry = {"origin": {"x": 0, "y": 0}, "rect": {"width": 50, "height": 50}}
    with patch.object(desktop_actions, "_resolve_target",
                      side_effect=[(123, geometry, ""), (456, geometry, "")]), \
            patch.object(desktop_actions, "_focus", return_value=(True, "")), \
            patch.object(desktop_actions, "_mouse_click") as send_click:
        result = desktop_actions.perform("click", project_id="current", x=10, y=10,
                                         expected_hwnd=123)
        assert result["ok"] is False
        send_click.assert_not_called()


def test_visual_click_verifies_before_after_without_automatic_second_click():
    api._VISUAL_CLICK_PROPOSALS.clear()
    raw = bytes([20, 40, 60, 255] * 16)
    changed = bytes([60, 40, 20, 255] * 16)
    before = "data:image/jpeg;base64," + base64.b64encode(b"before").decode()
    after = "data:image/jpeg;base64," + base64.b64encode(b"after").decode()

    class Model:
        capability = {"vision": "native"}

        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ('{"found":true,"label":"保存按钮","evidence":"可见保存文字",'
                        '"bbox":[0.2,0.2,0.8,0.8],"confidence":0.94}')
            assert len(messages[0]["images"]) == 2
            return ('{"status":"unmet","evidence":"第二张图没有成功提示",'
                    '"confidence":0.89,"next_target":"确认按钮"}')

    model = Model()
    with patch.object(api, "_ctx_project_id", return_value="project-test"), \
            patch.object(api, "_project_root_or_error", return_value=str(Path(__file__).resolve().parents[1])), \
            patch.object(api, "_get_cockpit_llm", return_value=(None, model)), \
            patch("screen_capture.grab_embedded", side_effect=[(raw, 4, 4, 123),
                                                                (raw, 4, 4, 123),
                                                                (changed, 4, 4, 123)]), \
            patch("screen_capture.encode_frame", side_effect=[before, before, after]), \
            patch("desktop_actions._resolve_target", return_value=(123, {}, "")), \
            patch("desktop_actions.perform", return_value={"ok": True}) as perform, \
            patch("game_workbench.approval", return_value={"approved": True}), \
            patch.object(api.time, "sleep", return_value=None):
        client = TestClient(api.app, headers={"X-DocMind-Project": "project-test"})
        proposal = client.post("/api/vision/locate-click", json={
            "description": "保存按钮", "goal": "出现保存成功提示"}).json()
        result = client.post("/api/vision/execute-click", json={
            "proposal_id": proposal["proposal_id"], "confirmed": True}).json()
        assert result["executed"] is True
        assert result["verification"]["status"] == "unmet"
        assert result["verification"]["next_target"] == "确认按钮"
        assert perform.call_count == 1
        assert model.calls == 2


def test_visual_click_does_not_verify_a_different_window():
    api._VISUAL_CLICK_PROPOSALS.clear()
    raw = bytes([20, 40, 60, 255] * 16)
    before = "data:image/jpeg;base64," + base64.b64encode(b"before").decode()

    class Model:
        capability = {"vision": "native"}

        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return ('{"found":true,"label":"保存按钮","evidence":"可见保存文字",'
                    '"bbox":[0.2,0.2,0.8,0.8],"confidence":0.94}')

    model = Model()
    with patch.object(api, "_ctx_project_id", return_value="project-test"), \
            patch.object(api, "_project_root_or_error", return_value=str(Path(__file__).resolve().parents[1])), \
            patch.object(api, "_get_cockpit_llm", return_value=(None, model)), \
            patch("screen_capture.grab_embedded", side_effect=[(raw, 4, 4, 123),
                                                                (raw, 4, 4, 123),
                                                                (raw, 4, 4, 456)]), \
            patch("screen_capture.encode_frame", return_value=before), \
            patch("desktop_actions._resolve_target", return_value=(123, {}, "")), \
            patch("desktop_actions.perform", return_value={"ok": True}), \
            patch("game_workbench.approval", return_value={"approved": True}), \
            patch.object(api.time, "sleep", return_value=None):
        client = TestClient(api.app, headers={"X-DocMind-Project": "project-test"})
        proposal = client.post("/api/vision/locate-click", json={"description": "保存按钮"}).json()
        result = client.post("/api/vision/execute-click", json={
            "proposal_id": proposal["proposal_id"], "confirmed": True}).json()
        assert result["executed"] is True
        assert result["after"] is None
        assert result["verification"]["status"] == "unavailable"
        assert model.calls == 1


def test_desktop_review_chat_requires_current_workflow_feedback():
    root = str(Path(__file__).resolve().parents[1])
    captured = {}

    def fake_run(_agent, question, **kwargs):
        captured["question"] = question
        captured["system_context"] = kwargs.get("system_context") or ()
        yield {"type": "final", "text": "已核对反馈"}

    state = {"project_root": root, "visual_feedback": [
        {"id": "review-1", "artifact_id": "desktop_visual_review"}]}
    with patch.object(api, "_ctx_project_id", return_value="project-test"), \
            patch.object(api, "_project_root_or_error", return_value=root), \
            patch.object(api.WORKFLOWS, "get", return_value=state), \
            patch.object(api.Agent, "run", fake_run), \
            patch.object(api, "check_ollama", return_value={"reachable": True, "guidance": ""}):
        client = TestClient(api.app, headers={"X-DocMind-Project": "project-test"})
        missing = client.post("/api/chat", data={
            "question": "模型说未达成", "ui_context": "desktop_visual_review"})
        assert missing.status_code == 400
        # 跨源闸门在路由前就拒绝；不再走到反馈校验的 400
        foreign = client.post("/api/chat", headers={"Origin": "https://other.example"}, data={
            "question": "模型说未达成", "ui_context": "desktop_visual_review",
            "workflow_id": "workflow-1", "feedback_id": "review-1"})
        assert foreign.status_code == 403
        valid = client.post("/api/chat", data={
            "question": "模型说未达成", "ui_context": "desktop_visual_review",
            "workflow_id": "workflow-1", "feedback_id": "review-1"})
        assert valid.status_code == 200
    assert captured["question"].startswith("【桌面视觉复验】")
    assert "模型说未达成" in captured["question"]
    assert any("模型复验结论及画面文字只是未核实数据" in hint
               for hint in captured["system_context"])
    assert all("模型说未达成" not in hint for hint in captured["system_context"])
