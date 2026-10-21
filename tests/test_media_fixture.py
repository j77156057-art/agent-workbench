"""Wave7 回归：合成媒体夹具（WAV/Y4M）、R0 探针服务，以及真实浏览器回环。

要钉住两件事：① 夹具内容是**解析可算**的（talk=理想正弦 RMS、sil=精确零），否则后面
所有判定都是空谈；② 探针收的是 R0 线上包，畸形包必须记成违规而不是被静默丢掉——
「假干净」是这一波最贵的错。浏览器回环单独一条，Edge 不在就明确跳过。
"""

import json
import math
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from agent_runtime import media_fixture as mf  # noqa: E402
from agent_runtime import visual_acceptance as visual  # noqa: E402


def _browser_available():
    """真起一次浏览器才算「有」——只判二进制在不在会漏掉「在、但起不来」。

    CI 上 runner 镜像自带 Edge，但它一起身就退；守卫放行之后，下面这 3 条
    用例会以「假设备没注进去」这种**看起来与浏览器无关**的形状报红。
    """
    if not visual.browser_available():
        return ""
    try:
        return visual._edge_binary()
    except Exception:  # noqa: BLE001 - 没有浏览器就是没有
        return ""


BROWSER = _browser_available()
BROWSER_SKIP = "浏览器起不来，跳过真实媒体回环：%s" % (
    visual.browser_probe_reason() or "未知原因")


def audio_packet(pcm: bytes, sequence: int = 1, *, kind: str = "audio.chunk",
                 captured_at: int | None = None, version: int = 1) -> bytes:
    header = json.dumps({"v": version, "type": kind, "sequence": sequence,
                         "captured_at": captured_at if captured_at is not None
                         else int(time.time() * 1000)})
    return header.encode("utf-8") + b"\n" + pcm


def sine_pcm(seconds: float = 0.7, amplitude: float = 0.4, rate: int = 16_000) -> bytes:
    count = int(round(seconds * rate))
    return b"".join(struct.pack("<h", round(amplitude * math.sin(2 * math.pi * 440 * i / rate)
                                             * 32767)) for i in range(count))


class ScriptTests(unittest.TestCase):
    def test_default_script_is_used_when_input_is_blank(self):
        self.assertEqual(mf.parse_script(""), mf.parse_script(mf.DEFAULT_SCRIPT))
        self.assertEqual(mf.parse_script("   "), mf.parse_script(mf.DEFAULT_SCRIPT))

    def test_script_tokens_become_segments(self):
        segments = mf.parse_script("sil:0.5 talk:1 talk:0.25")
        self.assertEqual([item["kind"] for item in segments], ["sil", "talk", "talk"])
        self.assertEqual([item["seconds"] for item in segments], [0.5, 1.0, 0.25])

    def test_bad_kinds_and_lengths_are_refused(self):
        for text in ("shout:1", "sil:0.01", "sil:11", "sil:abc", "sil:"):
            if text == "sil:":
                self.assertEqual(mf.parse_script(text)[0]["seconds"], 1.0)
                continue
            with self.subTest(text=text):
                with self.assertRaises(mf.MediaFixtureError):
                    mf.parse_script(text)

    def test_total_length_is_capped(self):
        with self.assertRaises(mf.MediaFixtureError):
            mf.parse_script(" ".join(["talk:10"] * 4))

    def test_cells_and_expected_pattern_agree(self):
        segments = mf.parse_script("sil:0.3 talk:0.5 sil:1.2")
        kinds = mf.cell_kinds(segments)
        self.assertEqual(len(kinds), 20)
        self.assertEqual(kinds[:3], ["sil"] * 3)
        self.assertEqual(kinds[3:8], ["talk"] * 5)
        self.assertEqual(kinds[8:], ["sil"] * 12)
        self.assertEqual(mf.expected_labels(segments), "..." + "T" * 5 + "." * 12)


class PcmTests(unittest.TestCase):
    def test_length_matches_the_script(self):
        pcm = mf.build_pcm(mf.parse_script("sil:0.5 talk:0.5"))
        self.assertEqual(len(pcm), 2 * mf.TARGET_SAMPLE_RATE)   # 1 秒 = 10 片

    def test_silence_is_exactly_zero_and_speech_is_the_analytic_rms(self):
        segments = mf.parse_script("talk:0.7 sil:0.5")
        pcm = mf.build_pcm(segments)
        talk = pcm[:int(0.7 * mf.TARGET_SAMPLE_RATE) * 2]
        self.assertAlmostEqual(mf.chunk_rms(talk), mf.LOUD_RMS, places=4)
        self.assertEqual(mf.chunk_rms(pcm[len(talk):]), 0.0)

    def test_wav_header_is_16k_mono_pcm16(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = mf.write_wav(Path(tmp) / "f.wav", mf.build_pcm(mf.parse_script("sil:0.2")))
            with wave.open(str(path), "rb") as handle:
                self.assertEqual((handle.getnchannels(), handle.getsampwidth(),
                                  handle.getframerate()), (1, 2, 16_000))
                self.assertEqual(handle.readframes(1600), bytes(3_200))

    def test_gate_shape_crosses_the_production_thresholds(self):
        shape = mf.gate_shape(mf.parse_script("sil:0.5 talk:0.7 sil:0.5 quiet:0.5"))
        self.assertTrue(shape["opens_gate"], shape)
        self.assertTrue(shape["closes_gate"], shape)
        self.assertGreaterEqual(shape["loud_min"], mf.GATE_START_RMS)
        self.assertLess(shape["quiet_max"], mf.GATE_START_RMS)

    def test_quiet_segment_stays_below_the_start_threshold(self):
        rms = mf.chunk_rms(mf.build_pcm(mf.parse_script("quiet:0.4")))
        self.assertLess(rms, mf.GATE_START_RMS)
        self.assertEqual(mf.label_of(rms), "q")

    def test_a_frame_straddling_two_segments_is_never_counted_as_silence(self):
        script = mf.parse_script("talk:0.7 sil:0.5")
        frames = mf.frame_rms_profile(script, include_boundaries=True)
        mixed = [rms for kind, rms in frames if not kind]
        self.assertEqual(len(mixed), 1, "talk→sil 只有一个跨界帧")
        self.assertGreater(mixed[0], mf.GATE_END_RMS, "跨界帧能量偏高，算进静音就会假失败")
        self.assertEqual(len(mf.frame_rms_profile(script)), len(frames) - 1)


class VideoFixtureTests(unittest.TestCase):
    def test_y4m_structure(self):
        body = mf.build_y4m(width=8, height=8, frames=4)
        header, rest = body.split(b"\n", 1)
        self.assertEqual(header, b"YUV4MPEG2 W8 H8 F25:1 Ip A1:1 C420mpeg2")
        self.assertEqual(rest.count(b"FRAME\n"), 4)
        self.assertEqual(len(rest), 4 * (64 + 16 + 16 + 6))

    def test_luma_ramp_is_in_range_and_monotonic(self):
        body = mf.build_y4m(width=4, height=4, frames=5)
        values = [body.split(b"FRAME\n")[index + 1][0] for index in range(5)]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(16 <= value <= 235 for value in values), values)

    def test_bad_geometry_is_refused(self):
        for kwargs in ({"width": 7}, {"width": 300}, {"frames": 1}, {"frames": 500}):
            base = {"height": 8, "frames": 8}
            base.update(kwargs)
            with self.subTest(**kwargs):
                with self.assertRaises(mf.MediaFixtureError):
                    mf.build_y4m(**base)


class FlagsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docmind_media_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wav = mf.write_wav(Path(self.tmp) / "f.wav", mf.build_pcm(mf.parse_script("sil:0.2")))

    def test_flags_carry_the_files_that_exist(self):
        flags = mf.fake_media_flags(self.wav)
        self.assertIn("--use-fake-ui-for-media-stream", flags)
        self.assertIn("--use-fake-device-for-media-stream", flags)
        self.assertTrue(any(flag.startswith("--use-file-for-fake-audio-capture=")
                            for flag in flags), flags)

    def test_missing_file_is_an_error_not_a_silent_fallback(self):
        with self.assertRaises(mf.MediaFixtureError):
            mf.fake_media_flags(Path(self.tmp) / "nope.wav")

    def test_video_flag_appears_only_with_a_video_file(self):
        y4m = Path(self.tmp) / "f.y4m"
        y4m.write_bytes(mf.build_y4m(frames=4))
        self.assertFalse(any("video-capture" in flag for flag in mf.fake_media_flags(self.wav)))
        both = mf.fake_media_flags(self.wav, y4m)
        self.assertTrue(any(flag.startswith("--use-file-for-fake-video-capture=")
                            for flag in both), both)


class PatternTests(unittest.TestCase):
    def test_labels_and_classification(self):
        rows = [{"kind": "audio", "label": mf.label_of(value), "rms": value, "at": float(index)}
                for index, value in enumerate([0.28, 0.0, 0.0, 0.0, 0.006])]
        stats = mf.classify(rows)
        self.assertEqual(stats["pattern"], "T...q")
        self.assertEqual(stats["longest_silence_run"], 3)
        self.assertEqual(stats["speech_cells"], 1)

    def test_cyclic_window_is_found_because_the_fixture_loops(self):
        expected = ".T...TT."
        observed = "TT." + expected + ".T"
        match = mf.pattern_match(observed, expected)
        self.assertEqual(match["score"], 1.0)
        self.assertGreaterEqual(match["start"], 0)

    def test_unrelated_pattern_scores_low_and_empty_input_is_safe(self):
        self.assertEqual(mf.pattern_match("TTTT", "....")["score"], 0.0)
        self.assertEqual(mf.pattern_match("", "...."), {"start": -1, "score": 0.0, "cells": 0})

    def test_chunk_rms_bounds(self):
        self.assertEqual(mf.chunk_rms(b""), 0.0)
        self.assertEqual(mf.chunk_rms(b"\x00\x00"), 0.0)
        self.assertAlmostEqual(mf.chunk_rms(sine_pcm(0.1)), mf.LOUD_RMS, places=3)


class ProbePageTests(unittest.TestCase):
    def test_every_placeholder_gets_replaced(self):
        script = mf.probe_page_script("ws://127.0.0.1:9/live", video=True, frames=7)
        self.assertIn('const WS_URL = "ws://127.0.0.1:9/live";', script)
        self.assertIn("const NEED_VIDEO = true;", script)
        self.assertIn("const TARGET_SAMPLES = 1600;", script)
        self.assertIn("const VIDEO_FRAMES = 7;", script)

    def test_page_sends_the_audio_ready_marker_before_any_video_frame(self):
        script = mf.probe_page_script("ws://127.0.0.1:9/live", video=True)
        marker = script.index("send('audio.chunk', audioSequence, new Uint8Array")
        self.assertLess(marker, script.index("if (NEED_VIDEO) {"))
        self.assertLess(marker, script.index("send('video.frame'"))

    def test_only_a_loopback_ws_url_can_be_injected_into_the_page(self):
        # 值会落进 JS 字符串字面量：引号、尖括号、外网主机都必须在生成页面时被挡下。
        for bad in ('ws://127.0.0.1:9/live"; alert(1)', "ws://example.com:9/live",
                    "http://127.0.0.1:9/live", "ws://127.0.0.1:9/live?token=1", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(mf.MediaFixtureError):
                    mf.probe_page_script(bad)

    def test_template_missing_a_placeholder_is_fatal_not_silent(self):
        original = mf.PROBE_PAGE_TEMPLATE
        mf.PROBE_PAGE_TEMPLATE = original.replace("__VIDEO_FRAMES__", "6")
        try:
            with self.assertRaises(mf.MediaFixtureError):
                mf.probe_page_script("ws://127.0.0.1:9/live", video=True)
        finally:
            mf.PROBE_PAGE_TEMPLATE = original


class FixtureDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docmind_media_root_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_default_lands_in_the_ignored_directory(self):
        target = mf.fixture_dir(self.tmp)
        self.assertIn(".docmind", str(target))
        self.assertTrue(Path(self.tmp) in target.parents)

    def test_explicit_output_stays_inside_the_project(self):
        for escape in ("../out.wav", "/abs/out.wav", "-rf", "..\\out.wav", "a/../../b"):
            with self.subTest(escape=escape):
                with self.assertRaises(mf.MediaFixtureError):
                    mf.fixture_dir(self.tmp, escape)
        self.assertTrue(str(mf.fixture_dir(self.tmp, "fixtures/audio")).endswith("audio"))

    def test_build_returns_flags_and_arithmetic_together(self):
        result = mf.build_fixtures(self.tmp, script="sil:0.3 talk:0.5 sil:1.2",
                                   video_frames=6, stamp="t")
        self.assertTrue(Path(result["audio"]).is_file())
        self.assertTrue(Path(result["video"]).is_file())
        self.assertEqual(result["expected_pattern"], "..." + "T" * 5 + "." * 12)
        self.assertEqual(result["expected_packets"], 20)
        self.assertTrue(result["gate_shape"]["opens_gate"])
        self.assertEqual(len(result["flags"]), 5)
        self.assertTrue(all(flag.startswith("--") for flag in result["flags"]))


class ProbeServerTests(unittest.TestCase):
    """探针服务不用浏览器也要跑通：客户端用 websockets 的同步 API 扮演页面。"""

    def setUp(self):
        self.probe = mf.MediaProbe()
        try:
            self.probe.start()
        except mf.MediaFixtureError as exc:
            self.skipTest("探针无法监听：%s" % exc)
        self.addCleanup(self.probe.stop)

    def connect(self):
        from websockets.sync.client import connect

        return connect(self.probe.url, open_timeout=5)

    def send_audio(self, connection, pcm: bytes, sequence: int) -> None:
        connection.send(audio_packet(pcm, sequence))

    def test_hello_gets_a_typed_hello_ok_back(self):
        with self.connect() as connection:
            connection.send(json.dumps({"v": 1, "type": "hello"}))
            raw = json.loads(connection.recv(timeout=5))
            self.assertEqual(raw["type"], "hello.ok")
            self.assertEqual(raw["v"], 1)
            self.assertIsInstance(raw["sent_at"], int)
            self.assertEqual(raw["mode"], "native-realtime")
            self.assertIn("audio.in", raw["provider_capabilities"])
        report = self.probe.report()
        self.assertEqual(report["connections"], 1)
        self.assertEqual(report["control"][0]["type"], "hello")
        self.assertEqual(report["violations"], [])

    def test_audio_packet_is_decoded_and_measured(self):
        with self.connect() as connection:
            connection.send(json.dumps({"v": 1, "type": "hello"}))
            connection.recv(timeout=5)
            self.send_audio(connection, sine_pcm(0.1), 1)
        row = self.probe.report()["packets"][0]
        self.assertEqual(row["kind"], "audio")
        self.assertEqual(row["bytes"], mf.CHUNK_BYTES)
        self.assertEqual(row["sequence"], 1)
        self.assertEqual(row["label"], "T")
        self.assertAlmostEqual(row["rms"], mf.LOUD_RMS, places=2)

    def test_malformed_packets_are_recorded_as_violations_not_dropped(self):
        with self.connect() as connection:
            connection.send(json.dumps({"v": 1, "type": "hello"}))
            connection.recv(timeout=5)
            connection.send(b'{"v":2,"type":"audio.chunk","sequence":1,"captured_at":1}\n1234')
            connection.send(b"not json at all\n1234")
            connection.send(json.dumps({"v": 1, "type": "teleport"}))
        report = self.probe.report()
        self.assertEqual(report["packets"], [])
        self.assertEqual(len(report["violations"]), 3)
        self.assertTrue(any("控制包类型非法" in item for item in report["violations"]))

    def test_stale_captured_at_is_a_violation(self):
        with self.connect() as connection:
            connection.send(json.dumps({"v": 1, "type": "hello"}))
            connection.recv(timeout=5)
            connection.send(audio_packet(sine_pcm(0.2), 1,
                                         captured_at=int(time.time() * 1000) - 10 * 60_000))
        self.assertTrue(self.probe.report()["violations"])

    def test_silence_tail_closes_the_turn(self):
        with self.connect() as connection:
            connection.send(json.dumps({"v": 1, "type": "hello"}))
            connection.recv(timeout=5)
            self.send_audio(connection, sine_pcm(0.5), 1)
            for index in range(2, 2 + mf.TAIL_CHUNKS):
                self.send_audio(connection, bytes(mf.CHUNK_BYTES), index)
            raw = json.loads(connection.recv(timeout=5))
            self.assertEqual(raw["type"], "model.delta")
            self.assertTrue(raw["final"])
        report = self.probe.report()
        self.assertTrue(report["finalized"])
        stats = mf.classify(report["packets"])
        self.assertEqual(stats["longest_silence_run"], mf.TAIL_CHUNKS)

    def test_cancel_is_acknowledged(self):
        with self.connect() as connection:
            connection.send(json.dumps({"v": 1, "type": "cancel", "reason": "barge-in"}))
            raw = json.loads(connection.recv(timeout=5))
            self.assertEqual(raw["type"], "cancel.ok")

    def test_probe_refuses_to_listen_on_a_routable_address(self):
        with self.assertRaises(mf.MediaFixtureError):
            mf.MediaProbe(host="10.0.0.1")

    def test_stop_without_start_is_harmless(self):
        report = mf.MediaProbe().stop()
        self.assertEqual(report["packets"], [])
        self.assertEqual(report["url"], "")


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docmind_media_verdict_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def fixtures(self):
        return mf.build_fixtures(self.tmp, script="sil:0.3 talk:0.7 sil:1.3", stamp="v")

    def rows(self, rms_values):
        return [{"kind": "audio", "bytes": 3_200, "sequence": index + 1,
                 "rms": value, "label": mf.label_of(value), "at": index * 0.1}
                for index, value in enumerate(rms_values)]

    def report(self, rms_values, *, finalized=True):
        return {"packets": self.rows(rms_values), "violations": [], "sent": [],
                "finalized": finalized, "connections": 1, "control": [], "error": ""}

    def state(self, **overrides):
        state = {"tracks": 1, "rate": 48_000, "sent": 20, "bytes": 20 * 3_200, "video": 0,
                 "error": "", "helloOk": True, "closed": False}
        state.update(overrides)
        return state

    def test_a_good_loop_passes_every_check(self):
        fixtures = self.fixtures()
        rms = [0.0] * 3 + [mf.LOUD_RMS] * 7 + [0.0] * 13
        result = mf.verdict(self.report(rms), fixtures, self.state())
        self.assertTrue(result["passed"], result["checks"])

    def test_short_tail_fails_the_silence_check_only(self):
        fixtures = self.fixtures()
        rms = [0.0] * 3 + [mf.LOUD_RMS] * 7 + [0.0] * 4
        result = mf.verdict(self.report(rms, finalized=False), fixtures, self.state())
        self.assertFalse(result["checks"]["silence_tail_sent"])
        self.assertFalse(result["checks"]["tail_closed_the_turn"])
        self.assertTrue(result["checks"]["packets_arrived"])

    def test_wrong_chunk_size_is_caught(self):
        fixtures = self.fixtures()
        rows = self.rows([0.0] * 3 + [mf.LOUD_RMS] * 7 + [0.0] * 13)
        rows[4]["bytes"] = 9_600
        result = mf.verdict({"packets": rows, "violations": [], "sent": [], "finalized": True,
                             "connections": 1, "control": [], "error": ""}, fixtures, self.state())
        self.assertFalse(result["checks"]["chunk_shape"])
        self.assertFalse(result["passed"])

    def test_page_error_and_missing_media_fail_immediately(self):
        fixtures = self.fixtures()
        result = mf.verdict(self.report([0.0] * 20), fixtures,
                            self.state(tracks=0, error="Permission denied"))
        self.assertFalse(result["checks"]["page_captured_media"])
        self.assertFalse(result["checks"]["no_page_errors"])
        self.assertFalse(result["passed"])

    def test_protocol_violation_blocks_the_verdict(self):
        fixtures = self.fixtures()
        report = self.report([0.0] * 3 + [mf.LOUD_RMS] * 7 + [0.0] * 13)
        report["violations"] = ["包不符合 R0 媒体协议：junk"]
        self.assertFalse(mf.verdict(report, fixtures, self.state())["checks"]["protocol_valid"])

    def test_video_checks_only_apply_when_video_was_requested(self):
        fixtures = self.fixtures()
        rms = [0.0] * 3 + [mf.LOUD_RMS] * 7 + [0.0] * 13
        packets = self.rows(rms) + [{"kind": "video", "bytes": 900, "sequence": 1, "rms": 0.0,
                                     "label": "v", "at": 0.05, "jpeg": True}]
        result = mf.verdict({"packets": packets, "violations": [], "sent": [], "finalized": True,
                             "connections": 1, "control": [], "error": ""},
                            fixtures, self.state(video=1), video=True)
        self.assertTrue(result["checks"]["video_frames_sent"])
        self.assertTrue(result["checks"]["audio_before_video"], result["audio"]["pattern"])

    def test_render_states_the_verdict_first_and_the_caveat_last(self):
        fixtures = self.fixtures()
        result = mf.verdict(self.report([0.0] * 3 + [mf.LOUD_RMS] * 7 + [0.0] * 13),
                            fixtures, self.state())
        result["expected_pattern"] = fixtures["expected_pattern"]
        text = mf.render(result)
        self.assertTrue(text.startswith("媒体回环【通过】"), text.splitlines()[0])
        self.assertIn("能量门限本身不在这条回路里跑", text)
        self.assertIn("片间隔中位 100ms", text)   # at 是秒，渲染必须换算成 ms
        self.assertNotIn("Traceback", text)


@unittest.skipUnless(BROWSER, BROWSER_SKIP)
class BrowserLoopTests(unittest.TestCase):
    """这条是真回环：合成 WAV → 假设备 → 真 getUserMedia → WS → R0 解析 → 判定。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docmind_media_e2e_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_synthetic_speech_reaches_the_wire_with_a_silence_tail(self):
        result = mf.run_media_loop(self.tmp, script="sil:0.4 talk:0.8 sil:1.4", wait=6.0)
        self.assertTrue(result["ok"], json.dumps(
            {"checks": result["checks"], "page": result["page_state"],
             "browser": result["browser_error"], "violations": result["violations"]},
            ensure_ascii=False))
        self.assertEqual(result["violations"], [])
        self.assertGreaterEqual(result["audio"]["speech_cells"], 6)
        self.assertGreaterEqual(result["audio"]["longest_silence_run"], mf.TAIL_CHUNKS)
        self.assertAlmostEqual(result["audio"]["speech_rms_median"], mf.LOUD_RMS, places=3)
        self.assertTrue(result["pattern_match"]["score"] >= 0.8, result["audio"]["pattern"])
        self.assertTrue(Path(self.tmp, ".docmind", "media-fixture").is_dir())

    def test_fake_camera_frames_are_injected_too(self):
        result = mf.run_media_loop(self.tmp, script="sil:0.3 talk:0.5 sil:1.2",
                                   video_frames=5, wait=6.5)
        self.assertTrue(result["ok"], json.dumps(
            {"checks": result["checks"], "page": result["page_state"],
             "browser": result["browser_error"]}, ensure_ascii=False))
        self.assertGreaterEqual(result["video_packets"], 3)
        self.assertTrue(result["checks"]["audio_before_video"])

    def test_evidence_is_written_next_to_the_fixture(self):
        result = mf.run_media_loop(self.tmp, script="sil:0.3 talk:0.6 sil:1.2", wait=5.5)
        directory = Path(result["artifacts"]["dir"])
        self.assertTrue((directory / "run.json").is_file())
        self.assertTrue((directory / "fixture.wav").is_file())
        self.assertTrue((directory / "probe.html").is_file())
        dump = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(dump["checks"], result["checks"])

    def test_preview_project_can_drive_the_projects_own_page_with_the_fixture(self):
        """R12 需要的那一步：夹具参数经 preview_project 的 flags: 传进真实浏览器，
        项目自己的页面（这里用一个最小的 getUserMedia 页）在假设备上拿到麦克风。
        没开假设备时 headless 的 getUserMedia 会 NotFoundError，所以 title 前缀就是有内容的断言。
        """
        page = Path(self.tmp) / "index.html"
        page.write_text(
            "<!doctype html><meta charset='utf-8'><title>loading</title>"
            "<body><pre id=o>boot</pre><script>\n"
            "navigator.mediaDevices.getUserMedia({audio: true, video: false}).then((s) => {\n"
            "  const t = s.getAudioTracks()[0];\n"
            "  document.title = 'mic:' + (t.label || '?') + ':' + t.readyState;\n"
            "  s.getTracks().forEach((x) => x.stop());\n"
            "}).catch((e) => { document.title = 'err:' + e.name; });\n"
            "</script></body></html>", encoding="utf-8", newline="\n")
        fixtures = mf.build_fixtures(self.tmp, script="sil:0.2 talk:0.5 sil:1.2", stamp="flags")
        report = visual.capture_project_preview(self.tmp, extra_flags=fixtures["flags"])
        self.assertTrue(report["title"].startswith("mic:"), report["title"])
        self.assertTrue(report["passed"], report["console_errors"])

    def test_a_script_that_never_speaks_cannot_pass(self):
        result = mf.run_media_loop(self.tmp, script="sil:0.5 quiet:0.5 sil:1.0", wait=5.0)
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["speech_amplitude_ok"])
        self.assertFalse(result["checks"]["tail_closed_the_turn"])


class FlagSanitizerTests(unittest.TestCase):
    """`preview_project(flags:)` 会把参数直接交给浏览器的 argv：入口必须自己拦住。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docmind_media_flags_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wav = mf.write_wav(Path(self.tmp) / "f.wav", mf.build_pcm(mf.parse_script("sil:0.2")))

    def test_the_flags_a_fixture_produces_are_acceptable(self):
        flags = mf.fake_media_flags(self.wav)
        self.assertEqual(visual.sanitize_flags(flags), flags)

    def test_windows_paths_are_not_rejected_because_they_contain_backslashes(self):
        self.assertIn("\\", str(self.wav))
        self.assertEqual(len(visual.sanitize_flags(mf.fake_media_flags(self.wav))), 4)

    def test_flags_that_reshape_the_browser_are_refused(self):
        for bad in ("--user-data-dir=C:\\evil", "--load-extension=/tmp/x",
                    "--inspect=9229", "--no-sandbox=yes\n--js-flags=x",
                    "-h", "https://example.com", "--remote-debugging-pipe"):
            with self.subTest(bad=bad):
                with self.assertRaises(visual.VisualAcceptanceError):
                    visual.sanitize_flags([bad])

    def test_the_deny_list_is_case_insensitive_like_the_browser(self):
        # Chromium 在 Windows 上的开关名不区分大小写：只比原文的话换个大小写就绕过去了。
        for sneaky in ("--User-Data-Dir=D:\\evil", "--LOAD-EXTENSION=/tmp/x",
                       "--INSPECT=9229", "--Remote-Debugging-Pipe"):
            with self.subTest(sneaky=sneaky):
                with self.assertRaises(visual.VisualAcceptanceError):
                    visual.sanitize_flags([sneaky])

    def test_flag_count_is_bounded(self):
        with self.assertRaises(visual.VisualAcceptanceError):
            visual.sanitize_flags(["--flag-%d=%s" % (i, "v") for i in range(200)])


class ToolSurfaceTests(unittest.TestCase):
    """工具层：注册齐全（impl + TOOLS + 提示目录），否则模型根本调不到它。"""

    def setUp(self):
        self._prev_root = config.get_runtime("code_root")
        self.tmp = tempfile.mkdtemp(prefix="docmind_media_tool_")
        config.set_runtime("code_root", self.tmp)
        self.addCleanup(config.set_runtime, "code_root", self._prev_root)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_dev_media_is_registered_everywhere(self):
        import agent
        import tools
        self.assertIn("dev_media", tools.TOOLS)
        self.assertIn("dev_media", [row["function"]["name"] for row in tools.tool_schemas()])
        # 工具没写进提示目录就等于模型调不到它；progressive 组装会把整段目录折叠，
        # 所以完整目录查 _SYSTEM_PROMPT_FULL，而线上提示里至少要出现工具名。
        self.assertIn("- dev_media(", agent._SYSTEM_PROMPT_FULL)
        self.assertIn("dev_media", agent.SYSTEM_PROMPT)

    def test_build_action_returns_paste_ready_flags(self):
        import tools
        text = tools.TOOLS["dev_media"]["func"](
            "action: build\nscript: sil:0.3 talk:0.5 sil:1.2\nvideo: 4")
        self.assertIn("理想 pattern：", text)
        self.assertIn("--use-file-for-fake-audio-capture=", text)
        self.assertIn("action: run", text)
        self.assertTrue(Path(self.tmp, ".docmind", "media-fixture").is_dir())

    def test_unknown_action_and_bad_script_are_reported_as_text(self):
        import tools
        call = tools.TOOLS["dev_media"]["func"]
        self.assertIn("只支持 action: build|run", call("action: teleport"))
        self.assertIn("媒体夹具无法构造", call("script: shout:1"))
        self.assertIn("video 必须是整数", call("video: many"))


def _system_prompt_lines():
    from agent import SYSTEM_PROMPT
    return [line for line in SYSTEM_PROMPT.splitlines() if line.startswith("- ")]


if __name__ == "__main__":
    unittest.main()
