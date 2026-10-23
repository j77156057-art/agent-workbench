"""持续视觉帧入口的输入、限流和桌面只读捕获。"""
import base64
import asyncio
import json
import time
import threading
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import api

# 协议 v1 的握手包：网关在收到当前项目的 hello 之前会关掉连接。
HELLO = json.dumps({
    "v": api.PROTOCOL_VERSION, "type": "hello", "project_id": "stream-test",
    "capabilities": ["video.frame", "audio.chunk", "interrupt"],
})


def test_live_stream_delivers_timestamped_model_observation_without_chat_history():
    seen = []

    async def analyze(file, previous_observation="", focused_region=""):
        seen.append((await file.read(), previous_observation, focused_region, api._ctx_project_id()))
        return {"ok": True, "observations": ["看到桌面画面"], "anomalies": [], "audit": {"mode": "native"}}

    captured_at = int(time.time() * 1000)
    payload = json.dumps({"v": api.PROTOCOL_VERSION, "type": "video.frame", "sequence": 1,
                          "captured_at": captured_at, "focused": True}).encode() + b"\n" + b"jpeg-frame"
    with patch.object(api.projects, "get_project", return_value={"root": "test-root"}), \
            patch.object(api, "analyze_live_frame_ep", side_effect=analyze):
        with TestClient(api.app).websocket_connect(
                "/api/vision/live-stream?project_id=stream-test",
                headers={"origin": "http://testserver"}) as socket:
            socket.send_text(HELLO)
            hello = socket.receive_json()
            assert hello["type"] == "hello.ok"
            assert hello["video_min_interval_ms"] >= api._LIVE_VISION_MIN_INTERVAL * 1000
            socket.send_bytes(payload)
            event = socket.receive_json()
    assert event["type"] == "video.observation"
    assert event["captured_at"] == captured_at
    assert event["sequence"] == 1
    assert event["observations"] == ["看到桌面画面"]
    assert seen == [(b"jpeg-frame", "", "1", "stream-test")]


def test_live_stream_rejects_unregistered_project():
    with patch.object(api.projects, "get_project", return_value=None):
        with TestClient(api.app) as client:
            try:
                with client.websocket_connect("/api/vision/live-stream?project_id=missing"):
                    assert False, "unregistered project was accepted"
            except WebSocketDisconnect as exc:
                assert exc.code == 1008


def test_live_stream_rejects_foreign_origin():
    with patch.object(api.projects, "get_project", return_value={"root": "test-root"}):
        with TestClient(api.app) as client:
            try:
                with client.websocket_connect(
                        "/api/vision/live-stream?project_id=stream-test",
                        headers={"origin": "https://foreign.example"}):
                    assert False, "foreign origin was accepted"
            except WebSocketDisconnect as exc:
                assert exc.code == 1008


def test_live_stream_discards_intermediate_frames_while_model_is_busy():
    entered = threading.Event()
    release = threading.Event()
    seen = []

    async def analyze(file, previous_observation="", focused_region=""):
        seen.append(await file.read())
        if len(seen) == 1:
            entered.set()
            await asyncio.to_thread(release.wait, 2)
        return {"ok": True, "observations": [str(len(seen))], "anomalies": [], "audit": {"mode": "native"}}

    def packet(sequence, value):
        return json.dumps({"v": api.PROTOCOL_VERSION, "type": "video.frame",
                           "sequence": sequence, "captured_at": int(time.time() * 1000),
                           "focused": False}).encode() + b"\n" + value

    with patch.object(api.projects, "get_project", return_value={"root": "test-root"}), \
            patch.object(api, "analyze_live_frame_ep", side_effect=analyze):
        with TestClient(api.app).websocket_connect("/api/vision/live-stream?project_id=stream-test") as socket:
            socket.send_text(HELLO)
            assert socket.receive_json()["type"] == "hello.ok"
            socket.send_bytes(packet(1, b"first"))
            assert entered.wait(2)
            socket.send_bytes(packet(2, b"intermediate"))
            socket.send_bytes(packet(3, b"latest"))
            time.sleep(0.05)
            release.set()
            assert socket.receive_json()["sequence"] == 1
            assert socket.receive_json()["sequence"] == 3
    assert seen == [b"first", b"latest"]


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=="
)


def test_live_frame_accepts_valid_image_without_chat_history():
    api._LIVE_VISION_LAST.clear()
    api._LIVE_VISION_BUSY.clear()
    with patch.object(api, "_get_cockpit_llm", return_value=(None, None)), \
            patch.object(api, "_agent_for", return_value=type("Agent", (), {"llm": type("Model", (), {"capability": {"vision": "none"}})()})()), \
            patch.object(api, "analyze_images", return_value=(None, ("观察到一个按钮",), {"mode": "harness"})) as analyze:
        response = TestClient(api.app).post(
            "/api/vision/frame", files={"file": ("frame.png", PNG, "image/png")})
    assert response.status_code == 200
    assert response.json()["observations"] == ["观察到一个按钮"]
    analyze.assert_called_once()


def test_live_frame_throttles_next_request():
    api._LIVE_VISION_LAST.clear()
    api._LIVE_VISION_BUSY.clear()
    with patch.object(api, "_get_cockpit_llm", return_value=(None, None)), \
            patch.object(api, "_agent_for", return_value=type("Agent", (), {"llm": type("Model", (), {"capability": {"vision": "none"}})()})()), \
            patch.object(api, "analyze_images", return_value=(None, ("画面",), {"mode": "harness"})):
        client = TestClient(api.app)
        first = client.post("/api/vision/frame", files={"file": ("frame.png", PNG, "image/png")})
        second = client.post("/api/vision/frame", files={"file": ("frame.png", PNG, "image/png")})
    assert first.json()["accepted"] is True
    assert second.json()["throttled"] is True


def test_live_frame_uses_native_vision_model_when_available():
    api._LIVE_VISION_LAST.clear()
    api._LIVE_VISION_BUSY.clear()
    class VisionModel:
        capability = {"vision": "native"}

        def chat(self, messages, **kwargs):
            assert messages[0]["images"]
            return "画面显示一个窗口"

    with patch.object(api, "_get_cockpit_llm", return_value=(None, None)), \
            patch.object(api, "_agent_for", return_value=type("Agent", (), {"llm": VisionModel()})()):
        response = TestClient(api.app).post(
            "/api/vision/frame", files={"file": ("frame.png", PNG, "image/png")})
    assert response.status_code == 200
    assert response.json()["observations"] == ["画面显示一个窗口"]
    assert response.json()["audit"]["mode"] == "native"


def test_live_frame_compares_previous_observation_without_trusting_it():
    api._LIVE_VISION_LAST.clear()
    api._LIVE_VISION_BUSY.clear()

    class VisionModel:
        capability = {"vision": "native"}

        def chat(self, messages, **kwargs):
            prompt = messages[0]["content"]
            assert "上一条视觉观察" in prompt
            assert "血条仍有一半" in prompt
            assert "仅供比较" in prompt
            return "当前血条已空；与上一观察相比有变化"

    with patch.object(api, "_get_cockpit_llm", return_value=(None, None)), \
            patch.object(api, "_agent_for", return_value=type("Agent", (), {"llm": VisionModel()})()):
        response = TestClient(api.app).post(
            "/api/vision/frame",
            data={"previous_observation": "血条仍有一半"},
            files={"file": ("frame.png", PNG, "image/png")},
        )
    assert response.status_code == 200
    assert "当前血条已空" in response.json()["observations"][0]


def test_focused_frame_prompt_identifies_partial_view():
    api._LIVE_VISION_LAST.clear()
    api._LIVE_VISION_BUSY.clear()

    class VisionModel:
        capability = {"vision": "native"}

        def chat(self, messages, **kwargs):
            prompt = messages[0]["content"]
            assert "不是整个界面" in prompt
            assert "勿推断区域外状态" in prompt
            return "局部有红色警告"

    with patch.object(api, "_get_cockpit_llm", return_value=(None, None)), \
            patch.object(api, "_agent_for", return_value=type("Agent", (), {"llm": VisionModel()})()):
        response = TestClient(api.app).post(
            "/api/vision/frame",
            data={"focused_region": "1"},
            files={"file": ("focus.png", PNG, "image/png")},
        )
    assert response.status_code == 200
    assert response.json()["observations"] == ["局部有红色警告"]


def test_live_frame_returns_structured_anomaly_candidates():
    api._LIVE_VISION_LAST.clear()
    api._LIVE_VISION_BUSY.clear()

    class VisionModel:
        capability = {"vision": "native"}

        def chat(self, messages, **kwargs):
            assert "anomalies" in messages[0]["content"]
            return ('{"observation":"错误弹窗可见","anomalies":['
                    '{"type":"error_message","target":"Error 404",'
                    '"evidence":"弹窗显示 Error 404","confidence":0.94}]}')

    with patch.object(api, "_get_cockpit_llm", return_value=(None, None)), \
            patch.object(api, "_agent_for", return_value=type("Agent", (), {"llm": VisionModel()})()):
        response = TestClient(api.app).post(
            "/api/vision/frame", files={"file": ("frame.png", PNG, "image/png")})
    assert response.status_code == 200
    assert response.json()["observations"] == ["错误弹窗可见"]
    assert response.json()["anomalies"][0]["target"] == "Error 404"


def test_live_frame_rejects_non_image():
    api._LIVE_VISION_LAST.clear()
    api._LIVE_VISION_BUSY.clear()
    response = TestClient(api.app).post(
        "/api/vision/frame", files={"file": ("frame.png", b"not an image", "image/png")})
    assert response.status_code == 400


def test_desktop_frame_returns_real_capture_only():
    image = "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode("ascii")
    with patch("screen_capture.grab_embedded", return_value=(b"pixels", 1, 1, 123)) as capture, \
            patch("screen_capture.encode_frame", return_value=image):
        response = TestClient(api.app).post("/api/vision/desktop-frame", json={"target": "embedded"})
    assert response.status_code == 200
    assert response.json()["image"] == image
    capture.assert_called_once()


def test_desktop_frame_strict_project_never_uses_default_window():
    with patch.object(api, "_ctx_project_id", return_value="project-test"), \
            patch("screen_capture.grab_embedded", return_value=None) as capture:
        response = TestClient(api.app, headers={"X-DocMind-Project": "project-test"}).post(
            "/api/vision/desktop-frame", json={"target": "embedded", "strict_project": True})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    capture.assert_called_once_with("project-test", strict_project=True)


def test_desktop_frame_falls_back_to_project_engine_window():
    """浏览器模式无嵌入宿主：embedded 抓不到时应回退抓本项目引擎独立窗口，而不是报错/截前台。"""
    image = "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode("ascii")
    with patch.object(api, "_ctx_project_id", return_value="project-test"), \
            patch.object(api, "_request_project_id", return_value="project-test"), \
            patch.object(api, "_project_root_for", return_value="D:/proj"), \
            patch("screen_capture.grab_embedded", return_value=None), \
            patch.object(api, "_engine_window_frame_safe",
                         return_value=((b"px", 2, 2, 99), "StarVoyager (DEBUG)")) as eng, \
            patch("screen_capture.encode_frame", return_value=image):
        response = TestClient(api.app, headers={"X-DocMind-Project": "project-test"}).post(
            "/api/vision/desktop-frame", json={"target": "embedded"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["source"] == "engine"
    assert body["image"] == image
    eng.assert_called_once_with("D:/proj")


def test_desktop_frame_strict_project_does_not_fall_back_to_engine_window():
    """strict_project 明确只要登记过的嵌入宿主：宁可无画面，也不回退抓独立引擎窗口（防串窗）。"""
    with patch.object(api, "_ctx_project_id", return_value="project-test"), \
            patch.object(api, "_request_project_id", return_value="project-test"), \
            patch("screen_capture.grab_embedded", return_value=None), \
            patch.object(api, "_engine_window_frame_safe") as eng:
        response = TestClient(api.app, headers={"X-DocMind-Project": "project-test"}).post(
            "/api/vision/desktop-frame", json={"target": "embedded", "strict_project": True})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    eng.assert_not_called()
