"""`agent_runtime/realtime_scripted.py`：零网络的离线实时适配器 + 真 ws 回合。

这一组的价值不在于"能收事件"，而在于**回包里出现的数字只能来自真实到达的音频**：
片数、字节数、峰值 RMS 全部由收到的 PCM 算出。所以它同时钉住两件事——

1. 适配器自己的判定：静音不开轮、**尾静音满 10 片才收轮**（生产契约"停止说话 ≠ 停止推流"）、
   上限/异常不炸、打断真的丢弃这一轮；
2. 真 `TestClient` websocket 走一遍 `/api/vision/live-stream`：hello 报原生、上行 PCM
   真的被 `send_audio` 接管、线上按 `audio.transcript → model.delta×N → model.audio` 回来、
   `final: true` 收口，**且默认路径一点没变**（没设 env 时仍旧响亮地降级）。

环境陷阱（本仓库踩过，见 test_realtime_gateway_bridge.py 头部）：Starlette TestClient 里
`receive_json()` 没事件会永久阻塞，所以下面用**守护线程收集**而不是"猜有几个然后读"。
"""
from __future__ import annotations

import base64
import json
import math
import os
import struct
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api
from agent_runtime import realtime_bridge, realtime_scripted as sc
from agent_runtime import realtime_provider as registry

ENDPOINT = "/api/vision/live-stream"
PROJECT_ID = "scripted-project"
SESSION_ID = "scripted-session"
CHUNK_BYTES = sc.INPUT_SAMPLE_RATE * sc.CHUNK_MS // 1000 * sc.BYTES_PER_SAMPLE   # 3200


def tone(ms: int = sc.CHUNK_MS, hz: int = 440, amplitude: float = 0.4) -> bytes:
    """16 kHz pcm16 正弦：RMS 解析值 = amplitude/√2，与实现无关，可以硬写进断言。"""
    total = sc.INPUT_SAMPLE_RATE * ms // 1000
    return b"".join(struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * hz * i
                                                                       / sc.INPUT_SAMPLE_RATE)))
                    for i in range(total))


def silence(ms: int = sc.CHUNK_MS) -> bytes:
    return b"\x00" * (sc.INPUT_SAMPLE_RATE * ms // 1000 * sc.BYTES_PER_SAMPLE)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _audio(payload: bytes, sequence: int) -> bytes:
    header = {"v": api.PROTOCOL_VERSION, "type": "audio.chunk", "sequence": sequence,
              "captured_at": _now_ms()}
    return json.dumps(header).encode("utf-8") + b"\n" + payload


def _control(**fields) -> str:
    return json.dumps({"v": api.PROTOCOL_VERSION, **fields})


def _wait_for(predicate, *, timeout: float = 6.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _collector(socket, sink: list) -> None:
    """后台收线上事件：`receive_json()` 没事件会永久阻塞，不能在主线程里"等下一个"。"""
    def run():
        while True:
            try:
                sink.append(socket.receive_json())
            except Exception:  # noqa: BLE001 - socket 关闭就是这个线程的终点
                return
    threading.Thread(target=run, daemon=True).start()


@pytest.fixture
def provider_holder():
    """把注册表里的 `scripted` 换成"记账工厂"：测试要能拿到**连接内部**那个实例。

    这是有意走注册表而不是直接 import 类：`DOCMIND_REALTIME_PROVIDER` + `register()`
    就是生产选适配器的那条路，测试必须沿同一条路把实例拿到手。
    """
    holder: dict = {}

    def factory(**kwargs):
        row = sc.ScriptedRealtimeProvider(**kwargs)
        holder["provider"] = row
        return row

    registry.register(sc.PROVIDER_NAME, factory)
    try:
        yield holder
    finally:
        registry.register(sc.PROVIDER_NAME, sc.ScriptedRealtimeProvider)


class _StubAnalyzer:
    async def analyze(self, file, previous_observation="", focused_region=""):
        await file.read()
        return {"ok": True, "observations": ["抽帧观察"], "anomalies": [], "audit": {}}


@pytest.fixture
def gateway():
    realtime_bridge.reset_state()
    api._NATIVE_OBSERVE_LAST.clear()
    with patch.object(api.projects, "get_project",
                      side_effect=lambda pid: {"root": "r"} if pid == PROJECT_ID else None), \
            patch.object(api, "analyze_live_frame_ep", side_effect=_StubAnalyzer().analyze), \
            patch.object(api, "_NATIVE_OBSERVE_INTERVAL", 0.0):
        with TestClient(api.app) as client:
            try:
                yield client
            finally:
                _wait_for(lambda: realtime_bridge.active_timeline_projects() == [], timeout=20.0)
                realtime_bridge.reset_state()


def _handshake(socket, sink: list) -> dict:
    socket.send_text(json.dumps({"v": api.PROTOCOL_VERSION, "type": "hello",
                                 "project_id": PROJECT_ID, "session_id": SESSION_ID,
                                 "capabilities": ["video.frame", "audio.chunk", "interrupt"]}))
    _collector(socket, sink)
    assert _wait_for(lambda: bool(sink), timeout=8.0), f"握手没回话：{sink}"
    hello = sink.pop(0)
    assert hello.get("type") == "hello.ok", hello
    return hello


def _feed_a_turn(socket, *, voiced: int = 2, tail: int = sc.SILENCE_TAIL_CHUNKS) -> int:
    """喂一轮"有人说话 + 尾静音"，回这一轮真的送进去的字节数。"""
    total = 0
    for index in range(voiced + tail):
        payload = tone() if index < voiced else silence()
        total += len(payload)
        socket.send_bytes(_audio(payload, index))
    return total


# ---- 1. 适配器自己的判定（不起服务、不开 socket） -----------------------------

def _fresh(**kwargs):
    provider = sc.ScriptedRealtimeProvider(**kwargs)
    assert provider.start() is True
    provider.drain(64)                       # 丢掉 EVENT_STATUS
    return provider


def test_silence_alone_never_opens_a_turn():
    provider = _fresh()
    for _ in range(40):
        assert provider.send_audio(silence()) is True
    assert provider.drain(64) == [], "只有静音就不该有回合"
    assert provider.stats()["turns"] == 0
    assert provider.stats()["audio_chunks"] == 40, "收下了多少片要如实记账"


def test_a_turn_closes_only_after_the_full_silence_tail():
    """"停止说话 ≠ 停止推流"：尾静音差一片都不许收口（Wave8 为这条付过真金白银）。"""
    provider = _fresh()
    provider.send_audio(tone())
    for _ in range(sc.SILENCE_TAIL_CHUNKS - 1):
        provider.send_audio(silence())
    assert provider.drain(64) == [], "尾静音还没满就该继续等"
    provider.send_audio(silence())
    kinds = [event.kind for event in provider.drain(64)]
    assert kinds.count("transcript") == 1 and kinds.count("text_delta") == 4
    assert kinds.count("audio_delta") == 1 and kinds[-1] == "done"


def test_the_transcript_reports_the_bytes_that_actually_arrived():
    """回包的数字必须由**送进去的字节**决定：这就是"页面显示了它"能当凭据的原因。

    峰值用解析值 0.4/√2≈0.283 硬写，不调用被测的 `pcm_rms`——否则实现算错测试也跟着错。
    """
    provider = _fresh()
    chunks = tone() + tone() + silence() * sc.SILENCE_TAIL_CHUNKS
    for index in range(0, len(chunks), CHUNK_BYTES):
        provider.send_audio(chunks[index:index + CHUNK_BYTES])
    events = provider.drain(64)
    transcript = [row for row in events if row.kind == sc.EVENT_TRANSCRIPT][0]
    assert transcript.payload["role"] == "user"
    assert transcript.payload["final"] is True
    text = transcript.payload["text"]
    assert "12 片" in text and "%d 字节" % len(chunks) in text and "0.283" in text, text
    final = [row for row in events if row.kind == sc.EVENT_TEXT_DELTA
             and row.payload.get("final") is True][0]
    assert "12 片" not in final.payload["text"]
    assert "%d 字节" % len(chunks) in final.payload["text"] and "第 1 轮" in final.payload["text"]


def test_the_streamed_pieces_add_up_to_the_final_text():
    provider = _fresh()
    provider.send_audio(tone())
    for _ in range(sc.SILENCE_TAIL_CHUNKS):
        provider.send_audio(silence())
    deltas = [row for row in provider.drain(64) if row.kind == sc.EVENT_TEXT_DELTA]
    streamed = "".join(row.payload["text"] for row in deltas if "final" not in row.payload)
    final = [row for row in deltas if row.payload.get("final") is True][0]
    assert streamed == final.payload["text"], "分片拼起来必须等于权威文本，不然界面会掉字"


def test_audio_out_is_reproducible_pcm16_at_24k_not_24_bit():
    """`"pcm24"` 是厂商的**格式名**，含义是"24 kHz 的 pcm16"（liveAudioControl.ts:19-21）。
    写成 24-bit 会让前端按错采样率播——那正是"听起来很快/很慢但看起来成功"那一类。"""
    provider = _fresh()
    provider.send_audio(tone())
    for _ in range(sc.SILENCE_TAIL_CHUNKS):
        provider.send_audio(silence())
    audio = [row for row in provider.drain(64) if row.kind == sc.EVENT_AUDIO_DELTA][0]
    raw = audio.payload["audio"]
    assert isinstance(raw, (bytes, bytearray)) and audio.payload["encoding"] == "pcm24"
    assert audio.payload["sample_rate"] == sc.OUTPUT_SAMPLE_RATE
    assert raw == sc.tone_bytes(), "回包音频必须可逐字节复算"
    assert audio.payload["chars"] == len(raw)


def test_interrupt_drops_the_open_turn_and_still_closes_it_on_the_wire():
    provider = _fresh()
    provider.send_audio(tone())
    assert provider.interrupt() is True
    done = [row for row in provider.drain(64) if row.kind == sc.EVENT_DONE]
    assert len(done) == 1 and done[0].payload["reason"] == "interrupted"
    assert provider.stats()["turns"] == 0, "被打断的那轮不该答"
    # 尾静音继续喂进来也不该把丢掉的那轮补上
    for _ in range(sc.SILENCE_TAIL_CHUNKS):
        provider.send_audio(silence())
    assert provider.drain(64) == []


def test_a_new_turn_still_works_after_an_interrupt():
    provider = _fresh()
    provider.send_audio(tone())
    provider.interrupt()
    provider.drain(64)
    provider.send_audio(tone())
    for _ in range(sc.SILENCE_TAIL_CHUNKS):
        provider.send_audio(silence())
    assert provider.stats()["turns"] == 1


def test_interrupting_nothing_emits_no_phantom_turn_close():
    """没在回答时补一条"收口"标记，页面上就会凭空关掉一条不存在的回合。"""
    provider = _fresh()
    assert provider.interrupt() is True
    assert provider.drain(64) == [], "空轮次的打断不该产出任何事件"
    assert provider.stats()["interrupts"] == 1, "但这次打断本身要如实记账"


def test_oversized_chunk_reports_an_error_event_instead_of_raising():
    provider = _fresh()
    assert provider.send_audio(tone()) is True          # 先开一轮
    assert provider.send_audio(b"\x01" * (sc.MAX_CHUNK_BYTES + 8)) is False
    errors = [row for row in provider.drain(64) if row.kind == sc.EVENT_ERROR]
    assert [row.payload["code"] for row in errors] == ["audio_too_large"]


def test_a_never_ending_turn_is_bounded_and_says_so():
    """一个人不停地说：回合必须有上限，超限要**报出来**并重新开始，而不是默默攒内存。

    上限之后的行为契约（不是"死掉"）：只报一次 `turn_too_long`、被丢掉的那轮不产转写，
    随后接着静音可以正常收一轮——而且那一轮的字节数一定不超过上限。
    """
    provider = _fresh()
    for _ in range(sc.MAX_TURN_BYTES // CHUNK_BYTES + 4):
        provider.send_audio(tone())
    events = provider.drain(64)
    codes = [row.payload.get("code") for row in events if row.kind == sc.EVENT_ERROR]
    assert codes == ["turn_too_long"], "内存上限必须自己守住，别等上游背压来救"
    assert [row for row in events if row.kind == sc.EVENT_TRANSCRIPT] == []
    for _ in range(sc.SILENCE_TAIL_CHUNKS):
        provider.send_audio(silence())
    answered = [row for row in provider.drain(64) if row.kind == sc.EVENT_TRANSCRIPT]
    assert len(answered) == 1 and provider.stats()["turns"] == 1
    bytes_in_reply = int(answered[0].payload["text"].split("·")[1].strip().split(" ")[0])
    assert bytes_in_reply <= sc.MAX_TURN_BYTES, "重新开始的那轮也必须在上限内"


def test_audio_before_start_is_refused_not_faked():
    provider = sc.ScriptedRealtimeProvider()
    assert provider.send_audio(tone()) is False, "没建会话却说接管了音频＝假绿"
    provider.close()
    assert provider.start() is False, "关掉之后不许复活"


def test_odd_sized_payload_does_not_crash_the_statistics():
    provider = _fresh()
    assert provider.send_audio(tone() + b"\x7f") is True
    assert provider.send_audio(b"") is True


def test_capabilities_and_availability_are_honest():
    provider = sc.ScriptedRealtimeProvider()
    assert set(provider.capabilities()) == {sc.CAP_AUDIO_IN, sc.CAP_VIDEO_IN, sc.CAP_TEXT_OUT,
                                            sc.CAP_AUDIO_OUT, sc.CAP_INTERRUPT}
    assert provider.availability()["ok"] is True
    assert "联网" in provider.availability()["reason"]
    assert provider.submit_agent_result("req-1", "报告") is False, "没实现就别装"


def test_the_registry_finds_it_without_the_caller_importing_the_module():
    registry._autoload()
    assert sc.PROVIDER_NAME in registry.provider_names()
    with patch.dict(os.environ, {registry.DEFAULT_PROVIDER_ENV: sc.PROVIDER_NAME}):
        resolved = registry.resolve("")
    assert resolved["ok"] is True and resolved["name"] == sc.PROVIDER_NAME


def test_an_unconfigured_key_still_selects_nothing():
    with patch.dict(os.environ, {registry.DEFAULT_PROVIDER_ENV: ""}, clear=False):
        resolved = registry.resolve("")
    assert resolved["ok"] is False and resolved["degraded_to"] == registry.DEGRADED_SAMPLED_FRAMES


# ---- 2. 真 ws 回合（TestClient，零网络） ------------------------------------

def test_a_real_ws_turn_comes_back_with_measured_numbers(gateway, provider_holder):
    got: list = []
    with patch.dict(os.environ, {registry.DEFAULT_PROVIDER_ENV: sc.PROVIDER_NAME}):
        with gateway.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
            hello = _handshake(socket, got)
            assert hello["mode"] == realtime_bridge.MODE_NATIVE, hello
            assert hello["provider"] == sc.PROVIDER_NAME
            assert hello["degraded_to"] is None
            assert sc.CAP_AUDIO_IN in hello["provider_capabilities"]
            total = _feed_a_turn(socket)
            assert _wait_for(lambda: any(row.get("type") == "audio.transcript"
                                         for row in got), timeout=8.0), got
            assert _wait_for(lambda: any(row.get("type") == "model.audio" for row in got),
                             timeout=8.0), got
    transcript = next(row for row in got if row["type"] == "audio.transcript")
    assert "%d 字节" % total in transcript["text"], transcript
    assert "0.283" in transcript["text"]
    audio = next(row for row in got if row["type"] == "model.audio")
    assert base64.b64decode(audio["audio"]) == sc.tone_bytes()
    assert audio["encoding"] == "pcm24"
    finals = [row for row in got if row["type"] == "model.delta" and row.get("final") is True]
    assert len(finals) == 1, "回合收口必须恰好一次：%s" % [row for row in got]
    # 网关自己拥有的事件不得被适配器冒充：`_handshake` 已经把真那条取走了，
    # 之后再出现任何一条 hello.ok / session.closed 都是适配器在撒谎
    assert [row for row in got if row["type"] == "hello.ok"] == [], got
    assert not [row for row in got if row["type"] == "session.closed"], got
    assert provider_holder["provider"].stats()["turns"] == 1


def test_silence_over_the_real_socket_produces_no_turn(gateway, provider_holder):
    """负对照：真喂 20 片静音，页面上就该什么都没有——这条不成立，上面那条绿灯没意义。"""
    got: list = []
    with patch.dict(os.environ, {registry.DEFAULT_PROVIDER_ENV: sc.PROVIDER_NAME}):
        with gateway.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
            _handshake(socket, got)
            for index in range(20):
                socket.send_bytes(_audio(silence(), index))
            time.sleep(0.4)
    assert [row for row in got if row["type"] in
            ("audio.transcript", "model.delta", "model.audio")] == [], got
    assert provider_holder["provider"].stats()["audio_chunks"] == 20


def test_cancel_on_the_wire_really_interrupts_the_provider_turn(gateway, provider_holder):
    """页面上的"打断"必须真的让适配器丢掉这一轮——只把 UI 关掉而模型还在说，
    是这个回路里最坏的一种假干净。"""
    got: list = []
    with patch.dict(os.environ, {registry.DEFAULT_PROVIDER_ENV: sc.PROVIDER_NAME}):
        with gateway.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
            _handshake(socket, got)
            socket.send_bytes(_audio(tone(), 0))
            assert _wait_for(lambda: provider_holder["provider"].stats()["turn_open"],
                             timeout=8.0), "音频没被接管"
            socket.send_text(_control(type="cancel", session_id=SESSION_ID))
            assert _wait_for(lambda: any(row.get("type") == "cancel.ok" for row in got),
                             timeout=8.0), got
            assert _wait_for(lambda: provider_holder["provider"].stats()["interrupts"] == 1,
                             timeout=8.0)
            socket.send_bytes(_audio(tone(), 1))
            time.sleep(0.3)
    assert [row for row in got if row["type"] == "audio.transcript"] == [], \
        "取消之后又答了一轮＝打断只是把 UI 关了，模型还在说"


def test_the_default_path_is_untouched_when_no_provider_is_configured(gateway):
    """加了 `_BUILTIN_MODULES` 之后默认行为必须一个字节都没变：没配就是响亮降级。"""
    got: list = []
    with patch.dict(os.environ, {registry.DEFAULT_PROVIDER_ENV: ""}, clear=False):
        with gateway.websocket_connect(f"{ENDPOINT}?project_id={PROJECT_ID}") as socket:
            hello = _handshake(socket, got)
    assert hello["mode"] == realtime_bridge.MODE_SAMPLED, hello
    assert hello["degraded_to"] == registry.DEGRADED_SAMPLED_FRAMES
    assert hello["reason"], "降级必须带原因"


def test_status_endpoint_agrees_with_the_socket_about_mode(gateway):
    with patch.dict(os.environ, {registry.DEFAULT_PROVIDER_ENV: sc.PROVIDER_NAME}):
        native = gateway.get("/api/vision/realtime-status").json()
    with patch.dict(os.environ, {registry.DEFAULT_PROVIDER_ENV: ""}):
        plain = gateway.get("/api/vision/realtime-status").json()
    assert native["mode"] == "native-realtime" and native["ok"] is True
    assert plain["mode"] == "sampled-frames", "设了 env 才原生，没设就得回抽帧"


def test_an_unknown_project_is_closed_before_any_provider_is_built(gateway, provider_holder):
    """连接资格的检查在适配器之前：项目不存在就 1008 关掉，一个 provider 都不该被建出来。"""
    from starlette.websockets import WebSocketDisconnect
    with patch.dict(os.environ, {registry.DEFAULT_PROVIDER_ENV: sc.PROVIDER_NAME}):
        with pytest.raises(WebSocketDisconnect) as caught:
            with gateway.websocket_connect(f"{ENDPOINT}?project_id=nope") as socket:
                socket.receive_json()
    assert caught.value.code == 1008, caught.value.code
    assert "unknown project" in (caught.value.reason or "").lower()
    assert "provider" not in provider_holder, "项目都不存在就不该建实时会话"
