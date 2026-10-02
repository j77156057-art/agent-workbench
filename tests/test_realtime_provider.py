"""Contract tests for the realtime provider layer (plan R4/R5).

No network and no API key: the WebSocket is injected, so these tests pin the
event vocabulary and the degradation contract. Anything that would need a real
DashScope key belongs to manual validation, not here.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from agent_runtime import realtime_provider as rp
from agent_runtime import realtime_timeline as rt
from agent_runtime.realtime_omni import (
    DEFAULT_MODEL, INPUT_AUDIO_FORMAT, OUTPUT_AUDIO_FORMAT, SILENCE_CHUNK_MS,
    OmniRealtimeProvider,
)

_ENV_KEYS = ("DOCMIND_REALTIME_PROVIDER", "DOCMIND_OMNI_API_KEY", "DASHSCOPE_API_KEY",
             "DOCMIND_OMNI_MODEL", "DOCMIND_OMNI_VOICE", "DOCMIND_OMNI_VAD",
             "DOCMIND_OMNI_URL", "DOCMIND_OMNI_INSTRUCTIONS",
             "DOCMIND_OMNI_WORKSPACE", "DOCMIND_OMNI_VAD_SILENCE_MS")


class _FakeWS:
    """Minimal websocket-client stand-in that records sends and replays events."""

    def __init__(self, events=None):
        self.sent: list[dict] = []
        self.closed = False
        self._events = list(events or [])
        self._lock = threading.Lock()

    def send(self, raw):
        with self._lock:
            self.sent.append(json.loads(raw))

    def recv(self):
        while not self.closed:
            with self._lock:
                if self._events:
                    return json.dumps(self._events.pop(0))
            time.sleep(0.005)
        raise RuntimeError("closed")

    def close(self):
        self.closed = True

    def types(self):
        with self._lock:
            return [item.get("type") for item in self.sent]


def _event(event_type, **fields):
    payload = {"type": event_type}
    payload.update(fields)
    return payload


class ProviderRegistryTests(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_resolve_without_config_degrades(self):
        state = rp.resolve()
        self.assertFalse(state["ok"])
        self.assertEqual(state["degraded_to"], rp.DEGRADED_SAMPLED_FRAMES)
        self.assertIn("dashscope_omni", state["available"])

    def test_resolve_unknown_provider_name(self):
        state = rp.resolve("nope-realtime")
        self.assertFalse(state["ok"])
        self.assertIn("nope-realtime", state["reason"])

    def test_custom_provider_registers_and_resolves(self):
        class _Stub(rp.RealtimeProvider):
            name = "stub"

            def availability(self):
                return {"ok": True, "provider": "stub"}

            def start(self):
                self._running = True
                return True

        rp.register("stub", _Stub)
        try:
            state = rp.resolve("stub")
            self.assertTrue(state["ok"])
            self.assertEqual(state["capabilities"], [])
            self.assertIsInstance(state["provider"], _Stub)
        finally:
            rp.unregister("stub")

    def test_event_to_wire_mapping(self):
        cases = {
            rp.EVENT_STATUS: "hello.ok",
            rp.EVENT_OBSERVATION: "video.observation",
            rp.EVENT_TRANSCRIPT: "audio.transcript",
            rp.EVENT_TEXT_DELTA: "model.delta",
            rp.EVENT_AUDIO_DELTA: "model.audio",
            rp.EVENT_DONE: "session.closed",
            rp.EVENT_ERROR: "error",
        }
        for kind, wire in cases.items():
            event = rp.RealtimeEvent(kind=kind, captured_at=1700000000000,
                                     session_id="s1", payload={"text": "hi"})
            rendered = event.to_wire(sent_at=1700000000001)
            self.assertEqual(rendered["type"], wire)
            self.assertEqual(rendered["v"], 1)
            self.assertEqual(rendered["session_id"], "s1")

    def test_unknown_event_kind_rejected(self):
        with self.assertRaises(ValueError):
            rp.RealtimeEvent("mystery")


class OmniAdapterTests(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["DOCMIND_OMNI_API_KEY"] = "test-key"
        self._providers: list[OmniRealtimeProvider] = []

    def tearDown(self):
        for provider in self._providers:
            provider.close()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _start(self, events=None):
        ws = _FakeWS(events)
        provider = OmniRealtimeProvider(project_id="prj-test",
                                        ws_factory=lambda url, headers: ws)
        self._providers.append(provider)
        self.assertTrue(provider.start())
        return provider, ws

    def test_availability_without_key(self):
        os.environ.pop("DOCMIND_OMNI_API_KEY", None)
        provider = OmniRealtimeProvider()
        state = provider.availability()
        self.assertFalse(state["ok"])
        self.assertEqual(state["degraded_to"], rp.DEGRADED_SAMPLED_FRAMES)
        self.assertFalse(provider.start())
        error = provider.poll(timeout=1)
        self.assertIsNotNone(error)
        self.assertEqual(error.kind, rp.EVENT_ERROR)
        self.assertEqual(error.payload["code"], "unavailable")

    def test_session_update_sent_on_start(self):
        provider, ws = self._start()
        deadline = time.time() + 2
        while not ws.types() and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(ws.types()[0], "session.update")
        session = ws.sent[0]["session"]
        self.assertEqual(session["modalities"], ["text", "audio"])
        self.assertEqual(session["turn_detection"]["type"], "server_vad")
        self.assertEqual(session["turn_detection"]["silence_duration_ms"], 500)
        self.assertEqual(session["input_audio_format"], INPUT_AUDIO_FORMAT)
        self.assertEqual(session["output_audio_format"], OUTPUT_AUDIO_FORMAT)
        self.assertEqual(session["input_audio_transcription"]["model"], "gummy-realtime-v1")
        self.assertTrue(provider.running)

    def test_vad_wait_can_be_tuned_without_exceeding_the_audio_tail(self):
        for value, expected in (("800", 800), ("0", 300), ("5000", 1000),
                                ("invalid", 500), ("", 500)):
            with self.subTest(value=value):
                os.environ["DOCMIND_OMNI_VAD_SILENCE_MS"] = value
                _, ws = self._start()
                self.assertEqual(ws.sent[0]["session"]["turn_detection"]["silence_duration_ms"], expected)

    def test_manual_mode_disables_vad(self):
        os.environ["DOCMIND_OMNI_VAD"] = "manual"
        provider, ws = self._start()
        deadline = time.time() + 2
        while not ws.sent and time.time() < deadline:
            time.sleep(0.01)
        self.assertIsNone(ws.sent[0]["session"]["turn_detection"])

    def test_interrupt_does_not_send_cancel(self):
        provider, ws = self._start()
        provider.interrupt()
        self.assertNotIn("response.cancel", ws.types())

    def test_audio_is_base64_framed(self):
        provider, ws = self._start()
        pcm = b"\x01\x02\x03\x04"
        self.assertTrue(provider.send_audio(pcm, captured_at=1700000000000))
        deadline = time.time() + 2
        while "input_audio_buffer.append" not in ws.types() and time.time() < deadline:
            time.sleep(0.01)
        sent = [item for item in ws.sent if item["type"] == "input_audio_buffer.append"][0]
        self.assertEqual(base64.b64decode(sent["audio"]), pcm)

    def test_send_silence_tail_emits_one_second_of_vad_audio(self):
        provider, ws = self._start()
        with patch("agent_runtime.realtime_omni.time.sleep"):
            self.assertTrue(provider.send_silence_tail(1.0))
        deadline = time.time() + 2
        while ws.types().count("input_audio_buffer.append") < 10 and time.time() < deadline:
            time.sleep(0.01)
        chunks = [item for item in ws.sent if item["type"] == "input_audio_buffer.append"]
        self.assertEqual(len(chunks), 10)
        self.assertTrue(all(len(base64.b64decode(item["audio"])) == 3200 for item in chunks))

    def test_oversized_audio_rejected(self):
        provider, ws = self._start()
        self.assertFalse(provider.send_audio(b"\x00" * ((1 << 20) + 1)))
        error = provider.poll(timeout=1)
        self.assertEqual(error.payload["code"], "audio_too_large")

    def test_video_frame_uses_image_buffer(self):
        provider, ws = self._start()
        self.assertTrue(provider.send_frame(b"jpegbytes", captured_at=1700000000000))
        deadline = time.time() + 2
        while "input_image_buffer.append" not in ws.types() and time.time() < deadline:
            time.sleep(0.01)
        sent = [item for item in ws.sent if item["type"] == "input_image_buffer.append"][0]
        self.assertEqual(base64.b64decode(sent["image"]), b"jpegbytes")

    def _wait_for_types(self, ws, event_type, count=1, timeout=2.0):
        deadline = time.time() + timeout
        while ws.types().count(event_type) < count and time.time() < deadline:
            time.sleep(0.01)
        return ws.types()

    def test_first_frame_primes_the_session_with_audio(self):
        """首帧前必须先有音频：真实 dashscope_omni 对同一会话要求「先音频后图像」。

        先送帧会回 `vendor_error: Error append image before append audio.`（2026-09-29
        设备侧实测），而 UI 的自然顺序是「先开摄像头/屏幕共享，再点开麦对话」——生产
        路径会稳定踩中，所以首帧之前必须补一个音频块。
        """
        provider, ws = self._start()
        self.assertTrue(provider.send_frame(b"jpegbytes", captured_at=1700000000000))
        types = self._wait_for_types(ws, "input_image_buffer.append")
        self.assertIn("input_audio_buffer.append", types, f"首帧前必须先有音频：{types}")
        self.assertLess(types.index("input_audio_buffer.append"),
                        types.index("input_image_buffer.append"),
                        f"音频必须在图像之前：{types}")
        primed = [item for item in ws.sent if item["type"] == "input_audio_buffer.append"][0]
        self.assertEqual(base64.b64decode(primed["audio"]),
                         b"\x00" * (16_000 * 2 * SILENCE_CHUNK_MS // 1000),
                         "引导块必须是 100ms 的 16kHz PCM16 静音")

    def test_frames_after_real_audio_are_not_primed_again(self):
        """用户先说话（音频已在流里）时不该再多塞静音：每帧都要是干净的一帧。"""
        provider, ws = self._start()
        self.assertTrue(provider.send_audio(b"\x01\x02\x03\x04", captured_at=1700000000000))
        self.assertTrue(provider.send_frame(b"one", captured_at=1700000000001))
        types = self._wait_for_types(ws, "input_image_buffer.append")
        self.assertEqual(types.count("input_audio_buffer.append"), 1, f"只该有用户那一片：{types}")

    def test_first_frame_primes_only_once(self):
        """只补一次：第二帧不许再塞静音（否则每帧都多一片，白占带宽与 turn）。"""
        provider, ws = self._start()
        provider.send_frame(b"one", captured_at=1700000000001)
        provider.send_frame(b"two", captured_at=1700000000002)
        types = self._wait_for_types(ws, "input_image_buffer.append", count=2)
        self.assertEqual(types.count("input_audio_buffer.append"), 1, f"引导只该发生一次：{types}")

    def test_reconnect_primes_again_for_the_new_session(self):
        """重连后是全新会话，供应商的顺序约束重新生效——引导标记必须清掉。"""
        provider, ws = self._start()
        self.assertTrue(provider.send_frame(b"one", captured_at=1700000000001))
        self._wait_for_types(ws, "input_image_buffer.append")
        self.assertTrue(provider._audio_primed)
        provider.close()
        self.assertFalse(provider._audio_primed, "close 后应视为新会话，需重新引导")

    def test_session_carries_the_cockpit_persona_by_default(self):
        """不配 DOCMIND_OMNI_INSTRUCTIONS 时必须下发出厂人设。

        没有 instructions 时模型按厂商默认助手答话，实测会说「我没法直接帮你改」——
        它不知道自己是开发舱的语音前端、也不知道用户的话会被转交开发 Agent。
        """
        provider, ws = self._start()
        sent = [item for item in ws.sent if item["type"] == "session.update"][0]
        instructions = sent["session"].get("instructions") or ""
        self.assertIn("语音前端", instructions, f"出厂人设没下发：{instructions[:80]}")
        self.assertIn("开发 Agent", instructions, "人设必须讲清『你的话会被转交开发 Agent』")
        # 人设里会**引用**那句错误回答来禁止它，所以断言的是「禁令在」，不是「字面不在」。
        self.assertIn("不要回答", instructions, "必须明确禁止『我没法修改』这类回答")
        # B：原生模式下网关不再兜抽帧观察，所以要求它自己主动报明显的界面问题。
        self.assertIn("主动提醒用户", instructions, "必须要求主动报告画面中的明显问题")
        self.assertIn("不要凭推测报异常", instructions, "误报比不报更伤信任：必须禁止推测式报异常")
        self.assertIn("普通话", instructions)
        self.assertIn("轻快、均匀", instructions)
        self.assertIn("先说结论", instructions)
        self.assertIn("不在词语中间停顿", instructions)

    def test_explicit_instructions_override_the_default(self):
        os.environ["DOCMIND_OMNI_INSTRUCTIONS"] = "只说你好。"
        provider, ws = self._start()
        sent = [item for item in ws.sent if item["type"] == "session.update"][0]
        self.assertEqual(sent["session"]["instructions"], "只说你好。")

    def test_interrupt_clears_buffer(self):
        provider, ws = self._start()
        self.assertTrue(provider.interrupt())
        self.assertIn("input_audio_buffer.clear", ws.types())

    def test_commit_reaches_the_wire(self):
        provider, ws = self._start()
        self.assertTrue(provider.commit())
        self.assertIn("input_audio_buffer.commit", ws.types())

    def test_interrupt_advertised_again(self):
        """The retiring model forced a local no-op; the current one supports it."""
        provider = OmniRealtimeProvider(project_id="prj-test")
        self.assertIn(rp.CAP_INTERRUPT, provider.capabilities())

    def test_transcription_delta_falls_back_to_stash(self):
        """Early partials carry only ``stash``; text is still empty."""
        provider, _ws = self._start([
            _event("conversation.item.input_audio_transcription.delta",
                   text="", stash="And", language="en"),
            _event("conversation.item.input_audio_transcription.delta",
                   text="And so", stash="", language="en"),
        ])
        first = provider.poll(timeout=2)
        second = provider.poll(timeout=2)
        self.assertEqual(first.kind, rp.EVENT_TRANSCRIPT)
        self.assertEqual(first.payload["text"], "And")
        self.assertFalse(first.payload["final"])
        self.assertEqual(second.payload["text"], "And so")

    def test_speech_boundaries_become_status(self):
        provider, _ws = self._start([
            _event("input_audio_buffer.speech_started", audio_start_ms=40),
            _event("input_audio_buffer.speech_stopped", audio_end_ms=2320),
        ])
        started = provider.poll(timeout=2)
        stopped = provider.poll(timeout=2)
        self.assertEqual(started.payload["state"], "listening")
        self.assertEqual(started.payload["audio_start_ms"], 40)
        self.assertEqual(stopped.payload["state"], "thinking")
        self.assertEqual(stopped.payload["audio_end_ms"], 2320)

    def test_nested_vendor_error_is_unwrapped(self):
        """Vendor errors nest under ``error``, not at the top level."""
        provider, _ws = self._start([
            {"type": "error", "error": {"code": "COMMON_ERROR",
                                        "message": "Voice 'Chelsie' is not supported."}},
        ])
        error = provider.poll(timeout=2)
        self.assertEqual(error.kind, rp.EVENT_ERROR)
        self.assertEqual(error.payload["code"], "COMMON_ERROR")
        self.assertIn("Chelsie", error.payload["message"])

    def test_response_done_reads_nested_id(self):
        """The id is under ``response.id``; the top level is empty."""
        provider, _ws = self._start([
            _event("response.done", response={"id": "resp_abc", "status": "completed"}),
        ])
        done = provider.poll(timeout=2)
        self.assertEqual(done.kind, rp.EVENT_DONE)
        self.assertEqual(done.payload["response_id"], "resp_abc")
        self.assertEqual(done.payload["reason"], "completed")

    def test_workspace_header_and_model_query_forwarded(self):
        os.environ["DOCMIND_OMNI_WORKSPACE"] = "ws-abc123"
        captured: dict[str, object] = {}

        def factory(url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return _FakeWS()

        provider = OmniRealtimeProvider(project_id="prj-test", ws_factory=factory)
        self._providers.append(provider)
        self.assertTrue(provider.start())
        self.assertIn("X-DashScope-WorkSpace: ws-abc123", captured["headers"])
        self.assertIn("Authorization: Bearer test-key", captured["headers"])
        self.assertIn(f"model={DEFAULT_MODEL}", captured["url"])

    def test_no_workspace_header_when_unset(self):
        captured: dict[str, object] = {}

        def factory(url, headers):
            captured["headers"] = headers
            return _FakeWS()

        provider = OmniRealtimeProvider(project_id="prj-test", ws_factory=factory)
        self._providers.append(provider)
        self.assertTrue(provider.start())
        self.assertFalse([h for h in captured["headers"] if "WorkSpace" in h])

    def test_server_events_translated(self):
        events = [
            _event("session.created"),
            _event("conversation.item.input_audio_transcription.completed", transcript="这里不对"),
            _event("response.audio_transcript.delta", delta="我看到"),
            _event("response.audio.delta", delta=base64.b64encode(b"pcm24k").decode()),
            _event("response.done", response_id="resp_1"),
        ]
        provider, _ws = self._start(events)
        kinds = []
        deadline = time.time() + 3
        while len(kinds) < 5 and time.time() < deadline:
            event = provider.poll(timeout=0.5)
            if event is not None:
                kinds.append(event.kind)
        self.assertEqual(kinds, [rp.EVENT_STATUS, rp.EVENT_TRANSCRIPT,
                                 rp.EVENT_TEXT_DELTA, rp.EVENT_AUDIO_DELTA, rp.EVENT_DONE])

    def test_vendor_error_becomes_error_event(self):
        provider, _ws = self._start([_event("error", code="rate_limit", message="slow down")])
        event = provider.poll(timeout=3)
        self.assertEqual(event.kind, rp.EVENT_ERROR)
        self.assertEqual(event.payload["code"], "rate_limit")

    def test_close_stops_session(self):
        provider, ws = self._start()
        provider.close()
        self.assertFalse(provider.running)
        self.assertTrue(ws.closed)
        self.assertFalse(provider.send_audio(b"\x01"))


class TimelineTests(unittest.TestCase):
    def test_cross_project_entries_dropped(self):
        timeline = rt.RealtimeTimeline("prj-a")
        self.assertIsNone(timeline.add(rt.KIND_FRAME, 1000, project_id="prj-b"))
        self.assertEqual(timeline.dropped_other_project, 1)
        self.assertEqual(len(timeline.entries()), 0)

    def test_evidence_never_after_speech(self):
        timeline = rt.RealtimeTimeline("prj-a")
        timeline.add(rt.KIND_FRAME, captured_at=1000, label="before")
        timeline.add(rt.KIND_FRAME, captured_at=9000, label="after")
        evidence = timeline.evidence_for_speech(500, 1500)
        self.assertTrue(evidence["within"])
        labels = [item["data"].get("label") for item in evidence["frames"]]
        self.assertNotIn("after", labels)
        self.assertIn("before", labels)

    def test_evidence_missing_is_reported(self):
        timeline = rt.RealtimeTimeline("prj-a")
        evidence = timeline.evidence_for_speech(5000, 6000)
        self.assertFalse(evidence["within"])
        self.assertTrue(evidence["reason"])

    def test_tolerance_allows_slightly_late_frame(self):
        timeline = rt.RealtimeTimeline("prj-a", tolerance_ms=1500)
        timeline.add(rt.KIND_FRAME, captured_at=7000)
        self.assertTrue(timeline.evidence_for_speech(5000, 6000)["within"])

    def test_project_switch_clears(self):
        timeline = rt.RealtimeTimeline("prj-a")
        timeline.add(rt.KIND_OBSERVATION, captured_at=1000)
        self.assertEqual(timeline.set_project("prj-b"), 1)
        self.assertEqual(len(timeline.entries()), 0)
        self.assertEqual(timeline.project_id, "prj-b")

    def test_bounded_capacity(self):
        timeline = rt.RealtimeTimeline("prj-a", max_entries=3)
        for index in range(10):
            timeline.add(rt.KIND_FRAME, captured_at=1000 + index, index=index)
        self.assertEqual(len(timeline.entries()), 3)
        self.assertEqual(timeline.entries()[-1].data["index"], 9)

    def test_latest_before_respects_clock(self):
        timeline = rt.RealtimeTimeline("prj-a")
        timeline.add(rt.KIND_OBSERVATION, captured_at=1000, text="old")
        timeline.add(rt.KIND_OBSERVATION, captured_at=5000, text="new")
        self.assertEqual(timeline.latest_before(3000, kind=rt.KIND_OBSERVATION).data["text"], "old")


if __name__ == "__main__":
    unittest.main()
