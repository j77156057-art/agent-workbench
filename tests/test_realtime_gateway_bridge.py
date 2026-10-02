"""R4/R5/R10 接线（`agent_runtime/realtime_bridge.py`）接进网关后的行为契约（R11）。

这一组测试**不连任何真实服务**：原生通道用一个注册进 `realtime_provider` 的假 provider
驱动，所以首 token / 首音频这类只有原生通道才会产生的指标也能确定性地覆盖。

它锁住四件事：

1. **降级要响亮**：没配 provider 时 `hello.ok` 必须报 `sampled-frames` + 原因，
   并且帧仍然走抽帧路径产出 `video.observation`。
2. **原生要如实上报**：配了 provider 时 `hello.ok` 报 `native-realtime`，
   `degraded_to` 为 null，能力集合来自 provider。
3. **适配器的握手事件不得冒充网关的握手**（真机复现过的坑）：provider 的
   `EVENT_STATUS` 被 `WIRE_BY_KIND` 映射成 `hello.ok`，其 `EVENT_DONE` 被映射成
   `session.closed`。两者转发都会撒谎——尤其前者会让前端把原生会话显示成"兼容抽帧"。
   但 `EVENT_DONE` 也**不能只是丢掉**：有转写时 `model.delta final: true` 会收口，
   抢话打断/无转写时没有它，前端回合永远收不了口，所以要补一条结束标记
   （见 `realtime_bridge` 模块文档 2.2 与下面 3.1 的三个用例）。
4. **收尾要干净**：会话结束时 provider 被关闭、项目时间线被释放、指标会话作用域被回收
   （不回收就是无界增长）。

已知环境陷阱（本仓库踩过）：
* `AsyncMock` 配"可调用实例"不会 await 返回的协程——所以假 provider 是**真类**。
* Starlette `TestClient` 里若处理器抛异常，`receive_*` 会永久阻塞——所以下面的读取都
  只在**已确定会有事件**时才读，不靠超时兜底。
"""
from __future__ import annotations

import base64
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api
from agent_runtime import realtime_bridge, realtime_metrics as metrics, realtime_provider

ENDPOINT = "/api/vision/live-stream"
PROJECT_ID = "bridge-project"
SESSION_ID = "bridge-session"
FAKE_NAME = "fake-native"
PAYLOAD = b"\xff\xd8bridge-jpeg\xff\xd9"


# ---- 夹具 -------------------------------------------------------------------

class FakeRealtimeProvider(realtime_provider.RealtimeProvider):
    """可编排的假适配器：记录网关送进来的东西，按测试要求吐事件。"""

    name = FAKE_NAME

    def __init__(self, *, start_ok: bool = True, announce_ready: bool = True,
                 fail_on: tuple = ()) -> None:
        super().__init__()
        self.start_ok = start_ok
        self.announce_ready = announce_ready
        self.started = False
        self.closed = False
        self.frames: list[tuple[bytes, int]] = []
        self.audio: list[tuple[bytes, int]] = []
        self.interrupts = 0
        # 让某个转发方法外抛，用来验证网关的兜底（基类契约要求 fail-closed，
        # 但网关不能指望适配器一定守约）。
        self.fail_on = set(fail_on)

    def capabilities(self) -> list[str]:
        return [realtime_provider.CAP_AUDIO_IN, realtime_provider.CAP_VIDEO_IN,
                realtime_provider.CAP_TEXT_OUT, realtime_provider.CAP_AUDIO_OUT,
                realtime_provider.CAP_INTERRUPT]

    def availability(self) -> dict:
        return {"ok": True, "provider": FAKE_NAME}

    def start(self) -> bool:
        if not self.start_ok:
            return False
        self.started = True
        self._running = True
        if self.announce_ready:
            # 真实适配器收到 vendor 的 session.created 后就是这么发的，
            # 而它会被 WIRE_BY_KIND 映射成 hello.ok（见模块文档 2.1）。
            self._emit(realtime_provider.EVENT_STATUS, state="ready", provider=FAKE_NAME)
        return True

    def close(self) -> None:
        self.closed = True
        super().close()

    def send_frame(self, image: bytes, captured_at: int = 0) -> bool:
        if "send_frame" in self.fail_on:
            raise RuntimeError("假适配器 send_frame 故障")
        self.frames.append((image, captured_at))
        return True

    def send_audio(self, pcm: bytes, captured_at: int = 0) -> bool:
        # 注意：网关调的是 bridge.take_audio()，桥再转到这里——注入点必须落在
        # provider 这一层，桥那层没有可注入的方法。
        if "send_audio" in self.fail_on:
            raise RuntimeError("假适配器 send_audio 故障")
        self.audio.append((pcm, captured_at))
        return True

    def interrupt(self) -> bool:
        if "interrupt" in self.fail_on:
            raise RuntimeError("假适配器 interrupt 故障")
        self.interrupts += 1
        return True

    def emit_text(self, text: str, *, final: bool = False) -> None:
        self._emit(realtime_provider.EVENT_TEXT_DELTA, text=text, role="assistant", final=final)

    def emit_audio(self, audio: bytes) -> None:
        self._emit(realtime_provider.EVENT_AUDIO_DELTA, audio=audio, chars=len(audio))

    def emit_done(self) -> None:
        self._emit(realtime_provider.EVENT_DONE, reason="completed")


@pytest.fixture
def provider():
    """注册假 provider 并让网关选中它；退出时反注册，避免污染别的测试。"""
    instance = FakeRealtimeProvider()
    realtime_provider.register(FAKE_NAME, lambda: instance)
    with patch.dict("os.environ", {realtime_provider.DEFAULT_PROVIDER_ENV: FAKE_NAME}):
        try:
            yield instance
        finally:
            realtime_provider.unregister(FAKE_NAME)


@pytest.fixture(autouse=True)
def _clean_bridge_state():
    """每个用例前后清空接线状态；**收尾前先等上一个会话的异步拆除跑完**。

    网关的拆除（`bridge.close()` → 释放时间线）跑在服务端处理器的 `finally` 里，
    比客户端 socket 上下文退出晚。不等它，下一个用例就会和上一个用例的迟到收尾抢
    同一张 `_timelines` 表：本文件所有用例共用同一个 `project_id`，迟到的
    `release_timeline` 会把引用计数从 1 减到 0，**把正在跑的会话的时间线误释放掉**。
    这正是本文件在"单独跑全绿、混进全量套件却红一项"的原因（`realtime_bench_tool`
    等前置文件的会话收尾尚未落地），所以把同步点放在唯一的地方：用例之间。
    """
    realtime_bridge.reset_state()
    # 原生模式的**旁路观察**（每 docmind_native_observe_interval 秒多产出一条
    # video.observation）会改变原生会话的线上事件序列，本文件多数用例断言的是精确序列，
    # 所以这里默认关掉它，只有专门测它的那条用例自己打开。
    # 同时清掉按项目的计时表：本文件所有用例共用同一个 project_id，不清就会互相压制
    # （前一条用例刚点过，后一条就永远轮不到）。
    api._NATIVE_OBSERVE_LAST.clear()
    with patch.object(api, "_NATIVE_OBSERVE_INTERVAL", 0.0):
        yield
        _wait_for(lambda: realtime_bridge.active_timeline_projects() == [], timeout=10.0)
    realtime_bridge.reset_state()


def _get_project(project_id):
    return {"root": f"root-{project_id}"} if project_id == PROJECT_ID else None


class _StubAnalyzer:
    """抽帧路径的占位视觉模型（原生路径不会走到它）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, file, previous_observation="", focused_region=""):
        await file.read()
        self.calls += 1
        return {"ok": True, "observations": ["抽帧观察"], "anomalies": [], "audit": {}}


@pytest.fixture
def gateway():
    analyzer = _StubAnalyzer()
    with patch.object(api.projects, "get_project", side_effect=_get_project), \
            patch.object(api, "analyze_live_frame_ep", side_effect=analyzer.analyze):
        with TestClient(api.app) as client:
            try:
                yield client, analyzer
            finally:
                # **必须在退出这个 client 之前**等收尾跑完。`with TestClient(...)` 退出会把
                # 它的事件循环拆掉，而会话拆除跑在服务端 handler 的异步 finally 里、比客户端
                # socket 退出晚；循环一拆，那个 finally 就再也不会完成——时间线/指标作用域
                # 永远不回收，于是「等一会儿再断言」的用例只能靠运气（本文件间歇红的根因）。
                # 拿它当同步点：等到没有活跃时间线，说明 finally 已经跑完。
                _wait_for(lambda: realtime_bridge.active_timeline_projects() == [], timeout=20.0)


def _hello(session_id: str = SESSION_ID) -> str:
    return json.dumps({"v": api.PROTOCOL_VERSION, "type": "hello", "project_id": PROJECT_ID,
                       "session_id": session_id, "capabilities": ["video.frame"]})


def _now_ms() -> int:
    """`captured_at` 必须是**当前**时间：网关只接受 now-120s ~ now+60s 的时间戳，
    写死一个历史时间戳会被判 `invalid_media`（本仓库踩过）。"""
    return int(time.time() * 1000)


def _frame(sequence: int = 0, focused: bool = False) -> bytes:
    header = {"v": api.PROTOCOL_VERSION, "type": "video.frame", "sequence": sequence,
              "captured_at": _now_ms()}
    if focused:
        header["focused"] = True
    return json.dumps(header).encode("utf-8") + b"\n" + PAYLOAD


def _audio_chunk(sequence: int = 0) -> bytes:
    header = {"v": api.PROTOCOL_VERSION, "type": "audio.chunk", "sequence": sequence,
              "captured_at": _now_ms()}
    return json.dumps(header).encode("utf-8") + b"\n" + b"\x00\x01\x02\x03"


def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """等一个异步收尾条件成立。

    客户端的 socket 上下文退出**不等**服务端处理器的 `finally`：原生会话的
    `bridge.close()` 要关 WebSocket 并 join 接收线程，实测在客户端退出之后才跑完
    （探针里 `close:exit` 打在读取语句之后）。所以断言收尾结果必须等，不能立即读。
    带硬期限，超时就返回 False 让断言给出明确的失败信息，不会挂死。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _handshake(socket, session_id: str = SESSION_ID) -> dict:
    socket.send_text(_hello(session_id))
    hello = socket.receive_json()
    assert hello.get("type") == "hello.ok", f"握手失败：{hello}"
    return hello


# ---- 1. 降级：响亮上报，且抽帧路径照旧 ---------------------------------------

def test_degraded_hello_reports_the_sampled_mode_with_a_reason(gateway):
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        hello = _handshake(socket)
    assert hello["mode"] == realtime_bridge.MODE_SAMPLED
    assert hello["degraded_to"] == realtime_bridge.MODE_SAMPLED
    assert hello["reason"], "降级必须给出原因，不能静默"
    assert hello["provider"] == ""
    assert hello["provider_capabilities"] == []
    assert "video.observation" in hello["capabilities"]


def test_degraded_frames_still_produce_observations(gateway):
    client, analyzer = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        socket.send_bytes(_frame(0))
        event = socket.receive_json()
    assert event["type"] == "video.observation" and event["ok"] is True
    assert event["sequence"] == 0
    assert analyzer.calls == 1, "降级路径必须真的调用抽帧视觉模型"


# ---- 2. 原生：如实上报 -------------------------------------------------------

def test_native_hello_reports_mode_provider_and_capabilities(gateway, provider):
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        hello = _handshake(socket)
    assert hello["mode"] == realtime_bridge.MODE_NATIVE
    assert hello["degraded_to"] is None
    assert hello["reason"] == ""
    assert hello["provider"] == FAKE_NAME
    assert set(hello["provider_capabilities"]) == {
        "audio.in", "video.in", "text.out", "audio.out", "interrupt"}
    # 原生会话仍必须保留网关自己的控制能力。
    assert {"heartbeat", "cancel"} <= set(hello["capabilities"])
    assert provider.started is True


def test_start_failure_degrades_instead_of_faking_a_session(gateway):
    """provider 起不来时必须降级，绝不能假装原生会话已经建立。"""
    failing = FakeRealtimeProvider(start_ok=False)
    realtime_provider.register(FAKE_NAME, lambda: failing)
    with patch.dict("os.environ", {realtime_provider.DEFAULT_PROVIDER_ENV: FAKE_NAME}):
        try:
            client, analyzer = gateway
            with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
                hello = _handshake(socket)
                socket.send_bytes(_frame(0))
                event = socket.receive_json()
        finally:
            realtime_provider.unregister(FAKE_NAME)
    assert hello["mode"] == realtime_bridge.MODE_SAMPLED
    assert hello["reason"], "降级原因不能为空"
    # 起不来就必须真的回到抽帧路径，而不是把帧吞掉。
    assert event["type"] == "video.observation"
    assert analyzer.calls == 1


# ---- 3. 适配器事件不得冒充网关事件（真机复现过的坑） --------------------------

def test_provider_ready_never_leaks_a_second_hello_ok(gateway, provider):
    """假 provider 在 start() 里就发了一条 ready，它必须被拦下。

    真机上这条会以 `hello.ok` 上线，且不带 `mode` 字段——前端
    `AutonomousCockpit.vue` 收到后会把模式显示改写成"兼容抽帧"，
    于是原生会话被显示成抽帧。这里用"下一条事件必须是模型 delta"来证明它没上线。
    """
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        hello = _handshake(socket)
        provider.emit_text("原生回答")
        event = socket.receive_json()
    assert hello["mode"] == realtime_bridge.MODE_NATIVE
    assert event["type"] == "model.delta", f"第二条不该是握手事件：{event}"
    assert event["text"] == "原生回答"


def test_spoken_opening_realtime_discovery_is_relayed_as_unverified_and_deduplicated(provider):
    """触发句是**自然口语**（不是括号标记）：它会被朗读出来，标记词念着很生硬。"""
    bridge = _bridge()
    try:
        provider.emit_text("我看到画面上有个问题：右侧保存按钮被错误提示遮挡", final=True)
        events = bridge.next_events()
        provider.emit_text("我看到画面上有个问题：右侧保存按钮被错误提示遮挡。", final=True)
        repeated = bridge.next_events()
    finally:
        bridge.close()
    discoveries = [item for item in events if item["type"] == "model.observation"]
    assert len(discoveries) == 1
    assert discoveries[0]["source"] == "realtime-model"
    assert discoveries[0]["verified"] is False
    assert discoveries[0]["text"] == "右侧保存按钮被错误提示遮挡"
    assert all(item["type"] != "model.observation" for item in repeated)


def test_legacy_bracket_opening_still_works_during_the_wording_change(provider):
    """旧标记作为兼容别名保留：措辞刚换时，仍在跑的会话里模型可能还按老格式说，
    不能出现「模型报了、网关不认」的静默漏报。"""
    bridge = _bridge()
    try:
        provider.emit_text("【疑似异常】底部按钮被遮挡", final=True)
        events = bridge.next_events()
    finally:
        bridge.close()
    discoveries = [item for item in events if item["type"] == "model.observation"]
    assert len(discoveries) == 1
    assert discoveries[0]["text"] == "底部按钮被遮挡"


def test_partial_opening_across_deltas_is_still_detected(provider):
    """分块把一个触发句切成两半（语音转写是增量来的）时不能误判成普通对话。"""
    bridge = _bridge()
    try:
        provider.emit_text("我看到画面", final=False)
        assert all(item["type"] != "model.observation" for item in bridge.next_events())
        provider.emit_text("上有个问题：左上角弹出了报错框", final=True)
        events = bridge.next_events()
    finally:
        bridge.close()
    discoveries = [item for item in events if item["type"] == "model.observation"]
    assert len(discoveries) == 1, f"半句 + 半句必须拼出一条提醒：{events}"
    assert discoveries[0]["text"] == "左上角弹出了报错框"


def test_mid_sentence_opening_is_not_a_discovery(provider):
    """触发句必须在**轮次开头**：中途冒出来的不算（否则普通对话里提一句就会误报）。"""
    bridge = _bridge()
    try:
        provider.emit_text("好的，我先把当前画面看一遍。我看到画面上有个问题：右下角有红字", final=True)
        events = bridge.next_events()
    finally:
        bridge.close()
    assert all(item["type"] != "model.observation" for item in events)


def test_unmarked_realtime_text_does_not_become_a_discovery(provider):
    bridge = _bridge()
    try:
        provider.emit_text("我先看一下当前画面。", final=True)
        events = bridge.next_events()
    finally:
        bridge.close()
    assert all(item["type"] != "model.observation" for item in events)


def test_provider_done_is_not_relayed_as_session_closed(gateway, provider):
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        provider.emit_text("先来一条回答", final=True)
        first = socket.receive_json()
        provider.emit_done()
        provider.emit_text("轮次结束后的下一条")
        second = socket.receive_json()
    assert first["type"] == "model.delta" and first["final"] is True
    # 模型一轮说完不等于会话结束：中间不能冒出 session.closed。
    assert second["type"] == "model.delta", f"session.closed 不该被转发：{second}"


# ---- 3.1 `EVENT_DONE` 变结束标记（模块文档 2.2） ------------------------------
#
# `EVENT_DONE` 不能只丢：一轮回答结束的信号通常由 `model.delta final: true` 承载，
# 但那条只在**有转写**时才发。抢话打断 / 本轮只有音频时若把它一起丢掉，前端那条
# `done: false` 的助手回合就永远不收口。
#
# 这三个用例直接驱动 `SessionBridge`（不经 WebSocket）：结束标记与被拦下的
# `EVENT_DONE` 可能落在两次 poll 里，必须能精确控制"哪几条事件在同一次 next_events"。

def _bridge():
    bridge = realtime_bridge.SessionBridge(PROJECT_ID, provider_name=FAKE_NAME, auto_start=True)
    bridge.bind_session(SESSION_ID)
    return bridge


def _wire_types(events):
    return [str(event.get("type") or "") for event in events]


def test_turn_without_transcript_still_gets_an_end_marker(provider):
    """只有音频、没有转写的一轮：`EVENT_DONE` 必须补一条结束标记。"""
    bridge = _bridge()
    try:
        provider.emit_audio(b"\x00\x01" * 8)
        audio = bridge.next_events()
        provider.emit_done()
        ended = bridge.next_events()
    finally:
        bridge.close()
    assert _wire_types(audio) == ["model.audio"], f"前置音频没转发：{audio}"
    assert _wire_types(ended) == ["model.delta"], f"一轮结束必须有结束标记：{ended}"
    marker = ended[0]
    assert marker["final"] is True and marker["text"] == ""
    assert marker["session_id"] == SESSION_ID, "结束标记的会话号必须是网关的"
    assert bridge.suppressed.get("session.closed") == 1


def test_final_transcript_is_not_followed_by_a_duplicate_end_marker(provider):
    """本轮已由 `final: true` 收过口：结束标记不得二次补发。

    这里两条事件**分处两次 `next_events`**——`_turn_closed` 若是局部变量，这一例必红。
    """
    bridge = _bridge()
    try:
        provider.emit_text("说完了", final=True)
        first = bridge.next_events()
        provider.emit_done()
        second = bridge.next_events()
    finally:
        bridge.close()
    assert _wire_types(first) == ["model.delta"] and first[0]["final"] is True
    assert second == [], f"同一轮不该出现两个结束标记：{second}"
    assert bridge.suppressed.get("session.closed") == 1


def test_each_turn_gets_its_own_end_marker(provider):
    """结束标记按轮次计：上一轮补过之后，下一轮仍然要补。"""
    bridge = _bridge()
    try:
        provider.emit_done()
        first = bridge.next_events()
        provider.emit_done()
        second = bridge.next_events()
    finally:
        bridge.close()
    assert _wire_types(first) == ["model.delta"], f"第一轮缺结束标记：{first}"
    assert _wire_types(second) == ["model.delta"], f"第二轮缺结束标记：{second}"
    assert bridge.suppressed.get("session.closed") == 2


# ---- 3.2 provider 外抛不得打死会话（fail-closed 契约的兜底） ------------------
#
# provider 基类契约写明「每个方法都应 fail-closed：返回 False 而非外抛」，`start()` 与
# pump 循环也都加了守卫；但网关在 cancel / 音频 / 视频三处**直接调用** bridge 的转发
# 方法时曾经一个 try 都没有——适配器违约一次，外抛就会顺着外层 try（只接
# WebSocketDisconnect）打穿整个会话。下面三条钉死这三处兜底。

def _control(event_type: str) -> str:
    return json.dumps({"v": api.PROTOCOL_VERSION, "type": event_type, "sent_at": _now_ms()})


def _session_survived(socket) -> bool:
    """发一次 heartbeat 并拿到回包 = 控制环没被打穿，会话还活着。"""
    socket.send_text(_control("heartbeat"))
    return socket.receive_json().get("type") == "heartbeat"


def test_provider_frame_failure_degrades_that_frame_and_keeps_the_session(gateway, provider):
    """`send_frame` 外抛：只降级这一帧到抽帧路径，会话不死、模式不中途翻脸。"""
    provider.fail_on = {"send_frame"}
    client, analyzer = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        hello = _handshake(socket)
        socket.send_bytes(_frame(0))
        event = socket.receive_json()
        assert event["type"] == "video.observation", f"这一帧该降级到抽帧：{event}"
        assert event["ok"] is True and event["observations"] == ["抽帧观察"]
        assert analyzer.calls == 1
        assert _session_survived(socket), "provider 单帧故障不该打死会话"
    assert hello["mode"] == realtime_bridge.MODE_NATIVE


def test_provider_audio_failure_falls_back_to_the_degraded_reply(gateway, provider):
    """`send_audio` 外抛：按「没接管」处理，回 audio_not_ready，会话不死。"""
    provider.fail_on = {"send_audio"}
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        socket.send_bytes(_audio_chunk(0))
        event = socket.receive_json()
        assert event["type"] == "error" and event["code"] == "audio_not_ready", event
        assert _session_survived(socket), "provider 音频故障不该打死会话"


def test_provider_interrupt_failure_still_completes_the_local_cancel(gateway, provider):
    """`interrupt` 外抛：本地取消照旧完成（cancel.ok），但如实报出模型没停住。

    顺序是契约的一部分：`cancel.ok` 必须先到——它是对这条 cancel 指令的答复，
    客户端按它推进本地状态；「模型没停住」是随后的独立告知，不能挤在答复前面。
    """
    provider.fail_on = {"interrupt"}
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        socket.send_text(_control("cancel"))
        first = socket.receive_json()
        second = socket.receive_json()
        assert first["type"] == "cancel.ok", f"cancel 的答复必须先是 cancel.ok：{first}"
        assert second["type"] == "error" and second["code"] == "interrupt_failed", second
        assert _session_survived(socket), "provider 打断故障不该打死会话"
    assert provider.interrupts == 0


# ---- 8. 原生模式的旁路观察（A 档：把「主动发现画面问题」接回来） ----------------
#
# 原生模式下画面理解在实时模型那侧：它看得见，但没有工具、不产出结构化告警，说的话也不进
# 主 Agent 上下文——所以「AI 主动发现画面问题」这条链原本是断的（live_vision_alerts 只挂在
# 抽帧路径上）。现在按 docmind_native_observe_interval（默认 5s）放一帧给视觉模型做旁路观察。
# 两条契约：①帧照旧直送 provider，旁路只是附加；②旁路失败**绝不上线**——客户端收到
# ok:false 的 video.observation 会直接 close socket，而原生会话上还挂着语音。

def test_native_mode_also_runs_a_low_rate_observation_probe(gateway, provider):
    """原生模式：帧直送 provider（不降级），同时按低频产出一次观察（供告警与时间线）。"""
    client, analyzer = gateway
    with patch.object(api, "_NATIVE_OBSERVE_INTERVAL", 1.0):
        with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
            _handshake(socket)
            socket.send_bytes(_frame(0))
            event = socket.receive_json()
    assert event["type"] == "video.observation", f"旁路观察没有产出：{event}"
    assert event["observations"] == ["抽帧观察"]
    assert analyzer.calls == 1, "旁路观察必须真的调用视觉模型"
    assert len(provider.frames) == 1, "帧仍必须直送 provider（旁路不是降级）"


def test_native_observation_probe_failure_never_reaches_the_client(gateway, provider):
    """旁路探针失败时一个字都不许上线：客户端收到 ok:false 会关掉整个 socket（含语音）。"""
    client, analyzer = gateway
    failing = {"ok": False, "error": "视觉模型暂不可用", "audit": {"mode": "unavailable"}}

    async def broken(_file, previous_observation="", focused_region=""):
        await _file.read()
        analyzer.calls += 1
        return failing

    with patch.object(api, "analyze_live_frame_ep", side_effect=broken), \
            patch.object(api, "_NATIVE_OBSERVE_INTERVAL", 1.0):
        with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
            _handshake(socket)
            socket.send_bytes(_frame(0))
            time.sleep(1.0)          # 让旁路任务跑完；若它上线了，下面的读会先拿到它
            socket.send_text(_control("heartbeat"))
            event = socket.receive_json()
    assert analyzer.calls == 1, "前置：旁路探针确实跑过（否则这条用例是空转）"
    assert event["type"] == "heartbeat", f"失败的旁路观察不得上线：{event}"


def test_native_probe_can_be_switched_off(gateway, provider):
    """`DOCMIND_NATIVE_OBSERVE_INTERVAL=0` 时原生模式不再跑旁路观察（省算力）。"""
    client, analyzer = gateway
    with patch.object(api, "_NATIVE_OBSERVE_INTERVAL", 0.0):
        with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
            _handshake(socket)
            socket.send_bytes(_frame(0))
            time.sleep(1.0)
            socket.send_text(_control("heartbeat"))
            event = socket.receive_json()
    assert analyzer.calls == 0, "关掉开关后不得再调用视觉模型"
    assert event["type"] == "heartbeat", f"关掉后不该有观察上线：{event}"
    assert len(provider.frames) == 1, "关闭旁路不影响原生主链路"


# ---- 4. 原生通道的帧/音频/打断路由 ------------------------------------------

def test_native_frames_go_to_the_provider_not_the_sampler(gateway, provider):
    client, analyzer = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        socket.send_bytes(_frame(7))
        provider.emit_text("看过了")
        event = socket.receive_json()
    assert event["type"] == "model.delta", "原生通道不该产出 video.observation"
    assert len(provider.frames) == 1
    frame, captured_at = provider.frames[0]
    assert frame == PAYLOAD
    assert abs(captured_at - _now_ms()) < 5_000, "captured_at 必须原样传给 provider"
    assert analyzer.calls == 0, "原生通道不得再调用抽帧模型"


def test_slow_native_upload_does_not_block_other_requests(gateway, provider):
    """供应商同步发送卡住时，本地服务仍可处理其他请求。"""
    client, _ = gateway
    entered, release = threading.Event(), threading.Event()
    original_send = provider.send_frame

    def slow_send(*args):
        entered.set()
        release.wait(3)
        return original_send(*args)

    with patch.object(provider, "send_frame", side_effect=slow_send):
        with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
            _handshake(socket)
            socket.send_bytes(_frame(1))
            assert entered.wait(2)
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    response = pool.submit(client.get, "/api/health").result(timeout=1)
                    assert response.status_code == 200
                    assert not release.is_set()
                finally:
                    release.set()
            socket.send_text(json.dumps({"v": 1, "type": "heartbeat", "sent_at": 42}))
            assert socket.receive_json()["type"] == "heartbeat"


def test_native_audio_is_accepted_instead_of_audio_not_ready(gateway, provider):
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        socket.send_bytes(_audio_chunk())
        provider.emit_text("听到了")
        event = socket.receive_json()
    assert event["type"] == "model.delta", f"原生通道不该回 audio_not_ready：{event}"
    assert len(provider.audio) == 1
    pcm, _ = provider.audio[0]
    assert pcm == b"\x00\x01\x02\x03"


def test_same_session_cancel_and_heartbeat_bypass_a_blocked_video_upload(gateway, provider):
    client, _ = gateway
    entered, release = threading.Event(), threading.Event()
    sent = []

    def slow_video(image, captured_at):
        sent.append(image)
        entered.set()
        release.wait(3)
        return True

    with patch.object(provider, 'send_frame', side_effect=slow_video):
        with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
            _handshake(socket)
            socket.send_bytes(_frame(1))
            assert entered.wait(2)
            # These frames/audio belong to the old turn and must never be replayed.
            socket.send_bytes(_frame(2))
            socket.send_bytes(_audio_chunk(2))
            socket.send_text(_control('cancel'))
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    assert pool.submit(socket.receive_json).result(timeout=1)['type'] == 'cancel.ok'
                    socket.send_text(_control('heartbeat'))
                    assert pool.submit(socket.receive_json).result(timeout=1)['type'] == 'heartbeat'
                    assert not release.is_set()
                finally:
                    release.set()
            assert _wait_for(lambda: provider.interrupts == 1, timeout=2)
            assert len(sent) == 1
            assert not provider.audio


def test_audio_backpressure_is_reported_and_queued_old_turn_is_discarded(gateway, provider):
    client, _ = gateway
    entered, release = threading.Event(), threading.Event()

    def slow_video(*args):
        entered.set()
        release.wait(3)
        return True

    with patch.object(provider, 'send_frame', side_effect=slow_video):
        with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
            _handshake(socket)
            socket.send_bytes(_frame(1))
            assert entered.wait(2)
            try:
                for i in range(21):
                    socket.send_bytes(_audio_chunk(i))
                with ThreadPoolExecutor(max_workers=1) as pool:
                    error = pool.submit(socket.receive_json).result(timeout=1)
                    assert error['code'] == 'audio_backpressure'
                    assert error['retryable'] is True
                    socket.send_text(_control('heartbeat'))
                    assert pool.submit(socket.receive_json).result(timeout=1)['type'] == 'heartbeat'
            finally:
                release.set()
            assert _wait_for(lambda: provider.interrupts == 1, timeout=2)
            assert provider.audio == []


def test_degraded_audio_still_reports_audio_not_ready(gateway):
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        socket.send_bytes(_audio_chunk())
        event = socket.receive_json()
    assert event["type"] == "error" and event["code"] == "audio_not_ready"


def test_cancel_interrupts_the_native_provider(gateway, provider):
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        socket.send_text(json.dumps({"v": api.PROTOCOL_VERSION, "type": "cancel",
                                     "reason": "user_interrupt"}))
        event = socket.receive_json()
    assert event["type"] == "cancel.ok"
    assert provider.interrupts == 1, "用户抢话必须真的打断原生模型"


# ---- 5. 音频 delta 必须能过 JSON（前端只解析文本事件） -----------------------

def test_native_audio_delta_reaches_the_wire_as_base64(gateway, provider):
    client, _ = gateway
    raw = b"\x00\x01\xfe\xff"
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        provider.emit_audio(raw)
        event = socket.receive_json()
    assert event["type"] == "model.audio"
    # 裸 bytes 会抛 TypeError，而前端只认字符串，所以只能是 base64。
    assert isinstance(event["audio"], str)
    assert base64.b64decode(event["audio"]) == raw
    assert event["encoding"]


# ---- 6. 时间线（R5）与指标（R10） -------------------------------------------

def test_timeline_records_frames_and_observations_then_releases(gateway):
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        socket.send_bytes(_frame(0))
        assert socket.receive_json()["type"] == "video.observation"
        snapshot = client.get(f"/api/vision/realtime/status?project_id={PROJECT_ID}").json()
        kinds = [entry["kind"] for entry in snapshot["timeline"]["entries"]]
        assert "frame" in kinds and "observation" in kinds
        assert PROJECT_ID in snapshot["timeline_projects"]
    # 会话结束后时间线必须被释放，否则长期运行会无界增长。
    assert _wait_for(lambda: PROJECT_ID not in realtime_bridge.active_timeline_projects())
    after = client.get(f"/api/vision/realtime/status?project_id={PROJECT_ID}").json()
    assert after["timeline"]["count"] == 0
    assert after["timeline_projects"] == []


def test_status_endpoint_serves_timeline_and_metrics_but_not_mode(gateway):
    """模式只由 /api/vision/realtime-status(R13) 与 hello.ok 提供，避免两份互相矛盾。"""
    client, _ = gateway
    body = client.get(f"/api/vision/realtime/status?project_id={PROJECT_ID}").json()
    assert set(body) == {"timeline", "timeline_projects", "metrics"}
    assert "mode" not in body and "degraded_to" not in body


def test_metrics_record_frames_latency_and_connection_scope(gateway):
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        for sequence in range(3):
            socket.send_bytes(_frame(sequence))
            assert socket.receive_json()["type"] == "video.observation"
        body = client.get(f"/api/vision/realtime/status?project_id={PROJECT_ID}").json()
    session = body["metrics"]["sessions"][SESSION_ID]
    assert session["counters"][metrics.FRAMES_SENT] == 3
    assert session["counters"][metrics.CONNECTIONS] == 1
    assert session["histograms"][metrics.OBSERVATION_LATENCY_MS]["count"] == 3


def test_native_session_records_first_token_and_first_audio(gateway, provider):
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        provider.emit_text("第一段")
        provider.emit_audio(b"\x01\x02")
        assert socket.receive_json()["type"] == "model.delta"
        assert socket.receive_json()["type"] == "model.audio"
        body = client.get(f"/api/vision/realtime/status?project_id={PROJECT_ID}").json()
    histograms = body["metrics"]["sessions"][SESSION_ID]["histograms"]
    assert histograms[metrics.FIRST_TOKEN_MS]["count"] == 1
    assert histograms[metrics.FIRST_AUDIO_MS]["count"] == 1
    assert histograms[metrics.FIRST_TOKEN_MS]["p50"] >= 0


def test_first_token_is_recorded_once_even_for_many_deltas(gateway, provider):
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        _handshake(socket)
        for index in range(3):
            provider.emit_text(f"第{index}段")
            assert socket.receive_json()["type"] == "model.delta"
        body = client.get(f"/api/vision/realtime/status?project_id={PROJECT_ID}").json()
    assert body["metrics"]["sessions"][SESSION_ID]["histograms"][metrics.FIRST_TOKEN_MS]["count"] == 1


def test_model_failure_is_counted_as_a_rejection(gateway):
    """抽帧模型抛错时既要回错误观察，也要记一次模型拒绝（R10）。"""
    async def boom(file, previous_observation="", focused_region=""):
        await file.read()
        raise RuntimeError("视觉服务不可用")

    with patch.object(api.projects, "get_project", side_effect=_get_project), \
            patch.object(api, "analyze_live_frame_ep", side_effect=boom):
        with TestClient(api.app) as client:
            with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
                _handshake(socket)
                socket.send_bytes(_frame(0))
                event = socket.receive_json()
                # 在会话还活着时读，避免和异步收尾抢时序（收尾会回收整个作用域）。
                counters = realtime_bridge.METRICS.snapshot()["sessions"][SESSION_ID]["counters"]
    assert event["type"] == "video.observation" and event["ok"] is False
    assert counters[metrics.MODEL_REJECTIONS] == 1


# ---- 7. 收尾：provider、时间线、指标作用域都要回收 ---------------------------
#
# 这条属性**直接驱动 SessionBridge** 来钉，不走 WebSocket。
# 经网关的版本断言的是服务端 handler 的异步 finally，而 TestClient 的客户端 socket
# 退出并不等它（本文件开头的坑）；更要命的是 `gateway` 夹具退出时会把事件循环拆掉，
# 那个 finally 就再也不会完成——断言只剩「循环拆掉之前它恰好跑完了」这一种运气
# （实测：同一条用例 5 次跑里红 1~3 次，等 20s 也没用，因为等的对象已经死了）。
# 所以把属性放在能确定性观察的层面：close() 是同步的，三件事的次序在源码里是
# provider.close() → release_timeline() → drop_session()。
# 经网关的收尾另由夹具统一同步（见 `gateway`），R9 的
# test_provider_close_exception_still_releases_timeline_and_metric_scope 覆盖 close 抛错的情形。

def test_close_releases_provider_timeline_and_metric_scope(provider):
    """一次收尾要把三件事都做掉：关 provider、释放项目时间线、回收指标会话作用域。

    第三件曾经真的漏过：`start()` 用适配器自造的 `rt-...` 覆盖了网关会话号，
    `drop_session(网关会话号)` 就永远清不掉那个作用域（进程长期运行无界增长）。
    """
    bridge = _bridge()
    bridge.note_frame({"captured_at": _now_ms(), "sequence": 1})
    bridge.note_model_failure()
    assert SESSION_ID in realtime_bridge.METRICS.snapshot()["sessions"], "前置：作用域应已建立"
    assert PROJECT_ID in realtime_bridge.active_timeline_projects(), "前置：时间线应已建立"

    bridge.close()

    assert provider.closed is True, "会话结束必须关闭 provider"
    assert PROJECT_ID not in realtime_bridge.active_timeline_projects(), "本项目的时间线必须被释放"
    assert SESSION_ID not in realtime_bridge.METRICS.snapshot()["sessions"], \
        f"会话作用域没有被回收：{sorted(realtime_bridge.METRICS.snapshot()['sessions'])}"


def test_observation_latency_is_measured_before_resetting_the_frame_clock():
    clock = [10.0]
    bridge = realtime_bridge.SessionBridge(PROJECT_ID, clock=lambda: clock[0])
    bridge.bind_session(SESSION_ID)
    try:
        bridge.note_frame({"captured_at": _now_ms(), "sequence": 1})
        clock[0] = 11.8
        bridge.note_observation({"captured_at": _now_ms(), "sequence": 1}, ["画面"])
        histogram = bridge.metrics.snapshot()["sessions"][SESSION_ID]["histograms"][metrics.OBSERVATION_LATENCY_MS]
        assert histogram["p50"] == pytest.approx(1800.0)
    finally:
        bridge.close()


def test_provider_session_id_never_hijacks_the_gateway_session(gateway, provider):
    """适配器自造的 `rt-...` 不得冒用网关的会话号，也不得成为指标作用域的键。

    这条是真机复现过的漏点：`start()` 一旦用适配器会话号覆盖网关会话号，
    `drop_session(网关会话号)` 就永远清不掉指标作用域 → 无界增长。
    """
    provider.session_id = "rt-fake-123456"
    client, _ = gateway
    with client.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
        hello = _handshake(socket)
        socket.send_bytes(_frame(0))
        provider.emit_text("回答")
        event = socket.receive_json()
        # 会话还活着的时候检查，避免和异步收尾抢时序。
        scopes = realtime_bridge.METRICS.snapshot()["sessions"]
    assert hello["session_id"] == SESSION_ID
    assert event["session_id"] == SESSION_ID, "转发事件必须带网关的会话号"
    assert event["provider_session_id"] == "rt-fake-123456", "适配器会话号只作诊断保留"
    assert SESSION_ID in scopes, "指标必须记在网关会话号下"
    assert "rt-fake-123456" not in scopes, "适配器会话号不得成为作用域键"


def test_two_sessions_share_one_timeline_and_release_on_the_last_close():
    first = realtime_bridge.SessionBridge(PROJECT_ID)
    first.bind_session("s1")
    second = realtime_bridge.SessionBridge(PROJECT_ID)
    second.bind_session("s2")
    assert realtime_bridge.active_timeline_projects() == [PROJECT_ID]
    first.close()
    assert realtime_bridge.active_timeline_projects() == [PROJECT_ID], "还有活跃会话就不能释放"
    second.close()
    assert realtime_bridge.active_timeline_projects() == []


def test_close_is_idempotent(gateway, provider):
    bridge = realtime_bridge.SessionBridge(PROJECT_ID, provider_name=FAKE_NAME)
    bridge.bind_session("s-idem")
    bridge.close()
    bridge.close()
    assert provider.closed is True
