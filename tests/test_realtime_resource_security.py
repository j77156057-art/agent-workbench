"""R9 可靠性/资源/安全审计（AI-A/AI-B 兼任）——对 `realtime_bridge` + `live-stream` 网关的
只读审计测试。**只新增测试，不改任何实现**（网关 `api.py` 归 /root 独占）。

与 `test_realtime_gateway_bridge.py`（AI-F，覆盖降级/上报/收尾/指标/会话劫持等）互补，
本文件专门审计 R9 验收里那些**AI-F 用例没测的资源韧性与隔离面**：

1. **provider.close() 外抛时收尾仍要落地**——时间线释放、指标会话作用域回收不能被异常打断；
2. 原生模式下**超限音频不得转发**给 provider（先于 take_audio 命中大小门）；
3. `send_frame` 返回 False 时帧必须**回落到抽帧单槽**，不能被静默丢弃；
4. **跨项目时间线隔离**：项目 B 的状态视图不得含项目 A 的帧/观察条目；
5. **守卫回归**：cancel 时 provider `interrupt()` 外抛不得击穿会话（/root 于 6832251
   落地三处守卫后，由本文件原「已知缺口」用例按翻转说明改写）。

真实设备/真实模型联验属 R12；全部用注册进 `realtime_provider` 的假 provider 驱动。
「多轮会话不泄漏」类用例因依赖异步收尾时序（AI-F 已标注其 green/red 交替）不在本文件
重复——逐会话收尾由 `test_realtime_gateway_bridge.py` 的 `test_session_close_*` 覆盖。
"""
from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api
from agent_runtime import realtime_bridge, realtime_provider

PROJECT_A = "r9-project-a"
PROJECT_B = "r9-project-b"
FAKE_NAME = "r9-fake"
FRAME = b"\xff\xd8r9-jpeg\xff\xd9"


# ---- 假 provider（可控：可让 close/interrupt/send_frame 外抛） -----------------

class R9FakeProvider(realtime_provider.RealtimeProvider):
    name = FAKE_NAME

    def __init__(self, *, raise_on: str = "", send_frame_ok: bool = True) -> None:
        super().__init__()
        self.raise_on = raise_on          # "close" | "interrupt" | "send_frame" | "take_audio"
        self.send_frame_ok = send_frame_ok
        self.closed = False
        self.frames: list[bytes] = []
        self.audio: list[bytes] = []
        self.interrupts = 0

    def capabilities(self) -> list[str]:
        return [realtime_provider.CAP_AUDIO_IN, realtime_provider.CAP_VIDEO_IN,
                realtime_provider.CAP_TEXT_OUT, realtime_provider.CAP_INTERRUPT]

    def availability(self) -> dict:
        return {"ok": True, "provider": FAKE_NAME}

    def start(self) -> bool:
        self._running = True
        return True

    def close(self) -> None:
        if self.raise_on == "close":
            raise RuntimeError("provider close boom")
        self.closed = True
        super().close()

    def send_frame(self, image: bytes, captured_at: int = 0) -> bool:
        if self.raise_on == "send_frame":
            raise RuntimeError("provider send_frame boom")
        if not self.send_frame_ok:
            return False
        self.frames.append(image)
        return True

    def send_audio(self, pcm: bytes, captured_at: int = 0) -> bool:
        if self.raise_on == "take_audio":
            raise RuntimeError("provider send_audio boom")
        self.audio.append(pcm)
        return True

    def interrupt(self) -> bool:
        if self.raise_on == "interrupt":
            raise RuntimeError("provider interrupt boom")
        self.interrupts += 1
        return True


def _register(instance):
    realtime_provider.register(FAKE_NAME, lambda: instance)


def _get_project(project_id):
    return {"root": f"root-{project_id}"} if (
        project_id in (PROJECT_A, PROJECT_B) or str(project_id).startswith("r9-cycle-")) else None


@pytest.fixture(autouse=True)
def _clean_state():
    realtime_bridge.reset_state()
    yield
    _wait_for(lambda: realtime_bridge.active_timeline_projects() == [], timeout=10.0)
    realtime_bridge.reset_state()
    realtime_provider.unregister(FAKE_NAME)


def _gateway_client():
    """返回一个 TestClient 上下文（网关已就绪，抽帧路径用占位模型）。"""
    async def _analyze(file, previous_observation="", focused_region=""):
        await file.read()
        return {"ok": True, "observations": ["抽帧观察"], "anomalies": [], "audit": {}}

    stack = [
        patch.object(api.projects, "get_project", side_effect=_get_project),
        patch.object(api, "analyze_live_frame_ep", side_effect=_analyze),
    ]
    for p in stack:
        p.start()
    client = TestClient(api.app)
    client.__enter__()
    return client, stack


def _now_ms() -> int:
    return int(time.time() * 1000)


def _frame(sequence: int, project: str = PROJECT_A) -> bytes:
    header = {"v": api.PROTOCOL_VERSION, "type": "video.frame", "sequence": sequence,
              "captured_at": _now_ms()}
    return json.dumps(header).encode("utf-8") + b"\n" + FRAME


def _audio(sequence: int, payload: bytes = b"\x00\x01\x02\x03") -> bytes:
    header = {"v": api.PROTOCOL_VERSION, "type": "audio.chunk", "sequence": sequence,
              "captured_at": _now_ms()}
    return json.dumps(header).encode("utf-8") + b"\n" + payload


def _hello(project: str, session_id: str) -> str:
    return json.dumps({"v": api.PROTOCOL_VERSION, "type": "hello", "project_id": project,
                       "session_id": session_id, "capabilities": ["video.frame", "audio.chunk"]})


def _control(kind, **fields) -> str:
    return json.dumps({"v": api.PROTOCOL_VERSION, "type": kind, **fields})


def api_app_path() -> str:
    return "/api/vision/live-stream?project_id=" + PROJECT_A


def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _handshake(socket, project: str, session_id: str) -> dict:
    socket.send_text(_hello(project, session_id))
    event = socket.receive_json()
    assert event.get("type") == "hello.ok", f"握手失败：{event}"
    return event


def _native(instance, **kwargs):
    """注册假 provider 并让网关选中它（原生通道）。"""
    _register(instance)
    return patch.dict("os.environ", {realtime_provider.DEFAULT_PROVIDER_ENV: FAKE_NAME})


# ---- 1. provider.close() 外抛时收尾仍要落地 -----------------------------------

def test_provider_close_exception_still_releases_timeline_and_metric_scope():
    client, stack = _gateway_client()
    try:
        instance = R9FakeProvider(raise_on="close")
        with _native(instance):
            with client.websocket_connect(api_app_path()) as socket:
                _handshake(socket, PROJECT_A, "boom-close")
                socket.send_bytes(_frame(1))
        # 即使 close 抛错，会话作用域指标仍被回收、时间线仍被释放（收尾异常不能阻断清理）。
        assert _wait_for(lambda: realtime_bridge.active_timeline_projects() == [], timeout=10.0)
        snapshot = realtime_bridge.metrics_snapshot()
        assert "boom-close" not in snapshot.get("sessions", {}), "close 抛错导致指标作用域泄漏"
    finally:
        for p in stack:
            p.stop()


# ---- 3. 原生模式超限音频不得转发给 provider -----------------------------------

def test_oversized_audio_is_rejected_before_reaching_native_provider():
    client, stack = _gateway_client()
    try:
        instance = R9FakeProvider()
        oversized = b"\x00" * (api._LIVE_VISION_MAX_BYTES + 1)
        with _native(instance):
            with client.websocket_connect(api_app_path()) as socket:
                hello = _handshake(socket, PROJECT_A, "big-audio")
                assert hello["mode"] == "native-realtime"
                socket.send_bytes(_audio(1, oversized))
                rejected = socket.receive_json()
        assert rejected["type"] == "error" and rejected["code"] == "frame_too_large"
        assert instance.audio == [], "超限音频仍被转发进了 provider"
    finally:
        for p in stack:
            p.stop()


# ---- 4. send_frame 返回 False 时帧回落到抽帧，不静默丢弃 ----------------------

def test_send_frame_falsy_falls_back_to_sampled_slot():
    client, stack = _gateway_client()
    try:
        instance = R9FakeProvider(send_frame_ok=False)
        with _native(instance):
            with client.websocket_connect(api_app_path()) as socket:
                _handshake(socket, PROJECT_A, "fallback")
                socket.send_bytes(_frame(1))
                # provider 没收下这帧 → 网关必须走抽帧产出 video.observation，而不是丢掉。
                observation = socket.receive_json()
        assert observation["type"] == "video.observation"
        assert observation["ok"] is True
        assert instance.frames == [], "send_frame 返回 False 却仍记为已转发"
    finally:
        for p in stack:
            p.stop()


# ---- 5. 跨项目时间线隔离 -----------------------------------------------------

def test_status_view_never_exposes_another_projects_timeline():
    client, stack = _gateway_client()
    try:
        instance_a = R9FakeProvider()
        with _native(instance_a):
            with client.websocket_connect("/api/vision/live-stream?project_id=" + PROJECT_A) as socket:
                _handshake(socket, PROJECT_A, "iso-a")
                socket.send_bytes(_frame(1))
                socket.send_text(_control("session.close"))
                socket.receive_json()
        # 项目 B 的状态视图：能列出存在过的项目 id，但 B 的时间线条目必须为空（不含 A 的帧）。
        snapshot = realtime_bridge.status_snapshot(PROJECT_B)
        b_timeline = snapshot["timeline"] or {}
        entries = b_timeline.get("entries", [])
        assert all(item.get("project_id", PROJECT_B) in (PROJECT_B, None) for item in entries)
        b_sequences = [item.get("sequence") for item in entries]
        assert 1 not in b_sequences, f"项目 B 时间线混入了 A 的帧条目：{entries}"
    finally:
        for p in stack:
            p.stop()


# ---- 6. 已关闭的缺口：网关已守卫 provider 的外抛方法 --------------------------
#
# 本条原为 test_known_gap_interrupt_is_not_guarded_and_kills_the_session（作者预写：
# 「加守卫后请把断言翻转为『cancel.ok 正常返回、会话存活』」）。2026-09-29 AI-F 给
# api.py 的 cancel / 音频 / 视频三处转发调用加了守卫，按该说明翻转。

def test_guarded_cancel_survives_a_provider_that_raises():
    """`interrupt()` 外抛不再击穿会话：cancel.ok 照常返回，随后的 error 如实报「模型没停住」。

    「会话存活」用一次心跳往返证明——异常炸穿时客户端拿不到任何回包，这里拿得到。
    顺序也是契约的一部分：cancel.ok 必须先到（它是对 cancel 指令的答复），
    紧接着才是 interrupt_failed；把错误挤在答复前面会让客户端以为取消失败了。
    """
    client, stack = _gateway_client()
    try:
        instance = R9FakeProvider(raise_on="interrupt")
        with _native(instance):
            with client.websocket_connect(api_app_path()) as socket:
                _handshake(socket, PROJECT_A, "break")
                socket.send_text(_control("cancel", reason="用户抢话"))
                reply = socket.receive_json()
                assert reply.get("type") == "cancel.ok", f"cancel 的答复必须先是 cancel.ok：{reply}"
                followed = socket.receive_json()
                assert followed.get("type") == "error", f"还得如实报出模型没停住：{followed}"
                assert followed.get("code") == "interrupt_failed", followed
                socket.send_text(_control("heartbeat", sent_at=1))
                assert socket.receive_json().get("type") == "heartbeat", "会话必须存活"
        # 资源收尾仍不许泄漏（时间线/指标作用域）。
        assert _wait_for(lambda: realtime_bridge.active_timeline_projects() == [], timeout=10.0)
    finally:
        for p in stack:
            p.stop()
