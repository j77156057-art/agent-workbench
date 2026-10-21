"""Wave8（选项3）回归：语音接线的浏览器行为回路。

这一条要守的是 Wave7 做不到的那半：页面接上了真能量门，所以「静音期不发包、
说完再推 1 秒然后真的停下来」才可断言。最要紧的两件事：
① 判定表必须逐条能红（否则又是"看起来通过"）；② 编译不出来时必须【失败】，
绝不许悄悄退回"抄一份生产逻辑"来把测试跑绿。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from agent_runtime import voice_loop as vl  # noqa: E402


def _node():
    return shutil.which("node") or ""


def _has_esbuild(frontend: Path) -> bool:
    return frontend.joinpath(*vl.ESBUILD_ENTRY).is_file()


def _browser() -> str:
    # 只吞"确实没有浏览器"这一种异常。写错属性名（曾把 vl.visual 写成
    # vl.visual_acceptance）如果也被吞掉，整类现场测试会【静默跳过】——
    # 那比红更糟：看起来一切正常。
    #
    # 判据是「浏览器真能起」，不只是「二进制在」：CI 上 runner 镜像自带 Edge，
    # 但它一起身就退 —— 只判二进制会让守卫放行，然后每条用例各自以不同形状
    # 报红（`KeyError: 'pattern'` / 端口未就绪），看不出是一件事。
    if not vl.visual.browser_available():
        return ""
    try:
        return vl.visual._edge_binary()
    except vl.visual.VisualAcceptanceError:
        return ""


FRONTEND = Path(ROOT) / "frontend"
TOOLCHAIN_OK = bool(_node()) and _has_esbuild(FRONTEND)


def audio_rows(labels, sizes=None, gap=0.1, start=0.0):
    sizes = sizes or [vl.media_fixture.CHUNK_BYTES] * len(labels)
    rows = []
    at = start
    for index, (label, size) in enumerate(zip(labels, sizes)):
        rows.append({"kind": "audio", "label": label, "bytes": size, "sequence": index + 1,
                     "rms": 0.28 if label == "T" else 0.0, "at": round(at, 3),
                     "gap": gap if index else round(at, 3)})
        at += gap
    return rows


def good_state(**overrides):
    state = {"mic": "on", "connected": True, "helloOk": True, "sent": 20, "bytes": 20 * 3200,
             "tailDone": 1, "cancelled": 1, "sampleRate": 48000, "speaking": True, "errors": [],
             # 页面自己收到并记下的服务端事件：探针的 sent 列表不能替代它
             "serverEvents": ["hello.ok", "speeching", "model.delta", "turn.end"]}
    state.update(overrides)
    return state


SENT_OK = [{"type": "hello.ok"}, {"type": "model.delta", "final": True}]
CONTROL_OK = [{"type": "hello"}, {"type": "cancel"}]


def good_packets():
    """与真机实测同形（2026-10-01）：1 片 audio-ready 标记 + 7 片语音 + 15 片静音尾
    + 停 2.69 秒后下一段语音。尾后那片用真实间隔，别写成均匀 0.1——那会把
    「推完就停」这条最要紧的证据抹掉。"""
    rows = audio_rows(["."] + ["T"] * 7 + ["q"] * 3 + ["."] * 12 + ["T"] * 7, gap=0.1)
    rows[23]["gap"] = 2.69                       # 尾后第一次开口的间隔
    return rows


def packets_without_marker():
    """没有前导 audio-ready 标记的形状（允许但不要求）：语音前 0 片静默。"""
    rows = audio_rows(["T"] * 7 + ["q"] * 3 + ["."] * 12 + ["T"] * 7, gap=0.1)
    rows[22]["gap"] = 2.69
    return rows


class PageGlueTests(unittest.TestCase):
    def test_glue_must_call_the_production_symbols(self):
        page = vl.voice_page_script("ws://127.0.0.1:9/live")
        for symbol in vl.GLUE_CALLS:
            self.assertIn(symbol, page, "胶水漏了对生产符号 %s 的调用" % symbol)
        self.assertIn('"ws://127.0.0.1:9/live"', page)
        self.assertNotIn("__WS_URL__", page)

    def test_the_transcribed_tail_rules_are_present(self):
        page = vl.voice_page_script("ws://127.0.0.1:9/live")
        self.assertIn("signal.state === 'tail'", page, "tail 期零值替换是生产行为，漏了就不是同一条回路")
        self.assertIn("pump.pending() > 0", page)
        self.assertIn("bufferedAmount > 500000", page, "背压丢弃是生产 sendLiveAudioChunk 的一部分")
        self.assertIn("new Uint8Array(3200)", page, "sendNativeAudioReady 的 100ms 静音标记")

    def test_missing_symbol_fails_at_build_time_not_at_runtime(self):
        original = vl.GLUE_CALLS
        vl.GLUE_CALLS = original + ("this_is_not_in_the_glue",)
        try:
            with self.assertRaises(vl.VoiceLoopError) as caught:
                vl.voice_page_script("ws://127.0.0.1:9/live")
        finally:
            vl.GLUE_CALLS = original
        self.assertIn("缺少生产调用", str(caught.exception))

    def test_a_name_that_only_sits_in_the_import_block_is_not_a_call(self):
        """`liveAudioControl` 这个词只在 import 行里出现过，函数体一次都没用。
        若校验退回"整页找名字"，这条就会假通过——正是审计指出的废话判定。"""
        original = vl.GLUE_CALLS
        vl.GLUE_CALLS = original + ("liveAudioControl",)
        try:
            with self.assertRaises(vl.VoiceLoopError) as caught:
                vl.voice_page_script("ws://127.0.0.1:9/live")
        finally:
            vl.GLUE_CALLS = original
        self.assertIn("liveAudioControl", str(caught.exception))

    def test_unserialisable_ws_url_cannot_break_the_page(self):
        with self.assertRaises((vl.VoiceLoopError, TypeError)):
            vl.voice_page_script(object())

    def test_the_sequence_resets_only_on_a_new_socket(self):
        """生产在新建 socket 时把 liveAudioSequence 归零（AutonomousCockpit.vue:1433），
        开麦时不归零。胶水若在 startMic 里归零，hello.ok 之后的 audio-ready 标记片会
        和第一片语音撞同一个序号——真机跑过一次，sequence_monotonic 当场变红。"""
        page = vl.voice_page_script("ws://127.0.0.1:9/live")
        connect = page.split("function connect()", 1)[1].split("async function startMic", 1)[0]
        start_mic = page.split("async function startMic", 1)[1].split("async function stopMic", 1)[0]
        self.assertIn("sequence = 0", connect, "序号必须在新建 socket 时归零，与生产一致")
        self.assertNotIn("sequence = 0", start_mic, "开麦时归零序号 = 标记片与首片语音同号")

    def test_click_labels_are_plain_strings_that_exist_on_the_page(self):
        """点击目标写成元组时定位会静默失配，整条回路退化成"只点了连接"，
        现场测试仍能跑出看似合理的 pattern——所以文案要在无浏览器时就把住。"""
        for barge_in in (True, False):
            labels = vl.click_labels(barge_in)
            self.assertTrue(all(isinstance(item, str) for item in labels), labels)
        self.assertEqual(vl.click_labels(False), ("连接", "开麦"))
        page = vl.voice_page_script("ws://127.0.0.1:9/live")
        for label in vl.click_labels(True):
            self.assertIn(">%s</button>" % label, page, "页面上没有这个按钮：%s" % label)


class BundleTests(unittest.TestCase):
    def test_command_shape_uses_node_and_the_local_esbuild(self):
        cmd = vl.bundle_command("node", Path("/fe"), "src/a.ts", Path("/out/a.mjs"))
        self.assertEqual(cmd[0], "node")
        self.assertIn("esbuild", cmd[1])
        self.assertIn("--bundle", cmd)
        self.assertIn("src/a.ts", cmd)
        self.assertTrue(cmd[-1].startswith("--outfile="))

    def test_missing_node_is_a_failure_not_a_silent_fallback(self):
        """缺 node 必须走 node 那条分支报错——本机装了 node，所以要把它藏起来测。"""
        frontend = Path(tempfile.mkdtemp())
        original = vl.shutil.which
        vl.shutil.which = lambda name: None if name == "node" else original(name)
        try:
            result = vl.build_bundle(frontend, Path(tempfile.mkdtemp()) / "out")
        finally:
            vl.shutil.which = original
        self.assertFalse(result["ok"])
        self.assertIn("node", result["error"])

    def test_missing_esbuild_is_a_failure_not_a_silent_fallback(self):
        result = vl.build_bundle(Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "out")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"], "缺工具链必须给出原因，不能返回 ok")
        self.assertIn("esbuild", result["error"])

    def test_a_claimed_success_without_output_is_a_failure(self):
        """esbuild 返回 0 但没落盘（或落了个空文件）不能算编译成功。"""
        class Fake:
            returncode = 0
            stdout = ""
            stderr = ""

        original = vl.shutil.which
        vl.shutil.which = lambda name: "node" if name == "node" else original(name)
        try:
            result = vl.build_bundle(FRONTEND, Path(tempfile.mkdtemp()) / "out",
                                     runner=lambda cmd, **kw: Fake())
        finally:
            vl.shutil.which = original
        self.assertFalse(result["ok"])
        self.assertIn("没有产出", result["error"])

    def test_an_empty_bundle_is_not_a_successful_compile(self):
        """esbuild 返回 0 并落下一个 0 字节文件：`is_file()` 会骗人，长度才不会。"""
        class Fake:
            returncode = 0
            stdout = ""
            stderr = ""

        def runner(cmd, **kwargs):
            Path(str(cmd[-1]).split("--outfile=", 1)[1]).write_bytes(b"")
            return Fake()

        original = vl.shutil.which
        vl.shutil.which = lambda name: "node" if name == "node" else original(name)
        try:
            result = vl.build_bundle(FRONTEND, Path(tempfile.mkdtemp()) / "out", runner=runner)
        finally:
            vl.shutil.which = original
        self.assertFalse(result["ok"])
        self.assertIn("没有产出", result["error"])

    def test_a_stale_bundle_cannot_disguise_a_failed_compile(self):
        """上一次留下的产物必须先删掉：否则这次 esbuild 什么都没编，
        旧的 .mjs 还躺在那里，回路会拿着【上一个版本】的模块跑绿。"""
        class Fake:
            returncode = 0
            stdout = ""
            stderr = ""

        target = Path(tempfile.mkdtemp()) / "out"
        target.mkdir(parents=True)
        for name in vl.PRODUCTION_MODULES:
            target.joinpath(name).write_text("// 上一次的旧产物\nexport const stale = 1\n",
                                             encoding="utf-8", newline="\n")
        original = vl.shutil.which
        vl.shutil.which = lambda name: "node" if name == "node" else original(name)
        try:
            result = vl.build_bundle(FRONTEND, target, runner=lambda cmd, **kw: Fake())
        finally:
            vl.shutil.which = original
        self.assertFalse(result["ok"])
        self.assertIn("没有产出", result["error"])
        self.assertFalse(target.joinpath("liveAudioControl.mjs").exists(),
                         "旧产物没删干净，下次运行会读到它")

    def test_esbuild_failure_is_reported_with_its_output(self):
        class Fake:
            returncode = 1
            stdout = ""
            stderr = "Transform failed"

        called = []

        def runner(cmd, **kwargs):
            called.append(cmd)
            return Fake()
        original = vl.shutil.which
        vl.shutil.which = lambda name: "node" if name == "node" else original(name)
        try:
            result = vl.build_bundle(FRONTEND, Path(tempfile.mkdtemp()) / "out", runner=runner)
        finally:
            vl.shutil.which = original
        self.assertFalse(result["ok"])
        self.assertIn("Transform failed", result["error"])
        self.assertEqual(len(called), 1, "第一个模块编译失败就该立刻停下，不带病继续")

    def test_a_missing_frontend_source_is_loud(self):
        with self.assertRaises(vl.VoiceLoopError):
            vl.frontend_dir(tempfile.mkdtemp())


class TailStatsTests(unittest.TestCase):
    def test_reads_the_gap_after_the_tail_not_inside_it(self):
        rows = audio_rows(["."] + ["T"] * 5 + ["q"] * 3 + ["."] * 12 + ["T"] * 4, gap=0.1)
        # 尾内那一片（index 20）与尾后第一片（index 21）必须给不同的值：
        # 都写成 0.1 时，取错下标也看不出错。
        rows[20]["gap"] = 0.1
        rows[21]["gap"] = 2.4
        stats = vl.tail_stats([row["label"] for row in rows], [row["gap"] for row in rows])
        self.assertEqual(stats["leading_quiet"], 1)
        self.assertEqual(stats["tail_after_speech"], 15)
        self.assertAlmostEqual(stats["gap_after_tail"], 2.4, places=3)
        self.assertEqual(stats["stopped"], "yes")

    def test_gap_after_tail_is_the_real_stop_measure(self):
        labels = ["T"] * 8 + ["."] * 10 + ["T"] * 5
        rows = audio_rows(labels, gap=0.1)
        # 把尾后那一片的间隔改成 2.7 秒：那才是"停下来过"的证据
        rows[18]["gap"] = 2.7
        stats = vl.tail_stats(labels, [row["gap"] for row in rows])
        self.assertAlmostEqual(stats["gap_after_tail"], 2.7, places=3)
        self.assertEqual(stats["stopped"], "yes")

    def test_a_small_gap_after_the_tail_means_the_stream_never_stopped(self):
        labels = ["T"] * 8 + ["."] * 10 + ["T"] * 5
        stats = vl.tail_stats(labels, [row["gap"] for row in audio_rows(labels, gap=0.1)])
        self.assertAlmostEqual(stats["gap_after_tail"], 0.1, places=3)
        self.assertEqual(stats["stopped"], "no")

    def test_truncated_capture_is_not_evidence_that_the_stream_stopped(self):
        """采集在尾巴中间收工：没继续观察够就不能算"停了"——必须是 not_observed。

        这是实测复现过的洞：一个完全没接门限、逐帧无条件推流的页面，只要截断在
        尾巴中间，旧版会把撞列表末尾当成 `stopped_then_nothing=True` 而全项通过。
        """
        rows = audio_rows(["T"] * 6 + ["."] * 10)
        stats = vl.tail_stats([row["label"] for row in rows], [row["gap"] for row in rows])
        self.assertEqual(stats["stopped"], "not_observed")
        self.assertEqual(
            vl.tail_stats([row["label"] for row in rows], [row["gap"] for row in rows],
                          trailing_observation=vl.RESUME_GAP_SECONDS)["stopped"], "yes")

    def test_no_speech_at_all_is_reported_as_such(self):
        rows = audio_rows(["."] * 12)
        stats = vl.tail_stats([row["label"] for row in rows], [row["gap"] for row in rows])
        self.assertFalse(stats["first_is_speech"])
        self.assertEqual(stats["tail_after_speech"], 0)
        self.assertEqual(stats["stopped"], "not_observed")
        self.assertIn("trailing_observation", stats)


class VerdictTableTests(unittest.TestCase):
    def verdict(self, packets=None, *, state=None, control=None, sent=None, violations=(),
                barge_in=True, tail_min=vl.TAIL_MIN, tail_max=vl.TAIL_MAX,
                trailing_observation=0.0, connections=1, **kwargs):
        return vl.verdict(packets if packets is not None else good_packets(),
                          control if control is not None else CONTROL_OK,
                          sent if sent is not None else SENT_OK,
                          state=state or good_state(), violations=list(violations),
                          tail_min=tail_min, tail_max=tail_max, expect_barge_in=barge_in,
                          trailing_observation=trailing_observation, connections=connections,
                          **kwargs)


    def test_a_faithful_run_passes_every_check(self):
        result = self.verdict()
        self.assertTrue(result["passed"], result["checks"])

    def test_unconditional_pumping_fails_the_bounded_tail_and_the_stop(self):
        """逐帧无条件推流（Wave7 那种页面）必须判未通过——否则这条守卫没意义。"""
        rows = audio_rows(["T"] * 8 + ["."] * 40 + ["T"] * 8)
        result = self.verdict(rows)
        self.assertFalse(result["checks"]["tail_is_bounded_not_always_pumping"])
        self.assertFalse(result["checks"]["stream_really_stops_after_tail"])
        self.assertFalse(result["passed"])

    def test_short_tail_fails_the_one_second_contract(self):
        rows = audio_rows(["."] + ["T"] * 8 + ["."] * 4 + ["T"] * 6, gap=0.1)
        result = self.verdict(rows)
        self.assertFalse(result["checks"]["silence_tail_at_least_1s"])
        self.assertTrue(result["checks"]["chunks_arrived"])

    def test_gate_leaking_silence_is_caught(self):
        rows = audio_rows(["."] * 4 + ["T"] * 8 + ["."] * 12 + ["T"] * 4, gap=0.1)
        self.assertFalse(self.verdict(rows)["checks"]["gate_closed_during_silence"])
        # 没有 audio-ready 标记时，一片静默都不许有
        self.assertFalse(self.verdict(rows, barge_in=False)["checks"]["gate_closed_during_silence"])

    def test_the_audio_ready_marker_is_allowed_only_once(self):
        rows = audio_rows(["."] + ["T"] * 8 + ["."] * 12 + ["T"] * 4, gap=0.1)
        self.assertTrue(self.verdict(rows)["checks"]["gate_closed_during_silence"])

    def test_wrong_chunk_shape_fails_even_when_timing_is_right(self):
        rows = good_packets()
        for row in rows[:5]:
            row["bytes"] = 9600
        self.assertFalse(self.verdict(rows)["checks"]["chunk_is_16k_100ms"])

    def test_missing_barge_in_cancel_fails_only_when_we_ask_for_it(self):
        rows = good_packets()
        self.assertFalse(self.verdict(rows, control=[{"type": "hello"}])["checks"]["barge_in_sent_cancel"])
        self.assertTrue(self.verdict(packets_without_marker(), control=[{"type": "hello"}],
                                     barge_in=False)["passed"],
                        "没开 audio-ready 时不该有前导静默片")

    def test_protocol_violation_and_page_error_and_missing_mic_all_fail(self):
        cases = (
            ({"violations": ["包不符合 R0 媒体协议"]}, "protocol_valid"),
            ({"state": good_state(errors=["mic:NotAllowedError"])}, "no_page_errors"),
            ({"state": good_state(mic="off")}, "mic_started"),
            ({"state": good_state(helloOk=False)}, "hello_ok_returned"),
            ({"sent": [{"type": "hello.ok"}]}, "tail_closed_the_turn"),
            ({"packets": []}, "chunks_arrived"),
        )
        for kwargs, name in cases:
            with self.subTest(check=name):
                self.assertFalse(self.verdict(**kwargs)["checks"][name], name)
                self.assertFalse(self.verdict(**kwargs)["passed"])

    def test_the_page_must_have_received_the_closing_event_too(self):
        """探针自己发了 model.delta final 不算证据：页面没记下它就说明收口没回到调用方。"""
        quiet_state = good_state(serverEvents=["hello.ok", "speeching"])
        self.assertTrue(any(row.get("final") is True for row in SENT_OK),
                        "前提是探针侧确实报了 final，否则这条测不到东西")
        self.assertFalse(self.verdict(state=quiet_state)["checks"]["tail_closed_the_turn"])
        self.assertFalse(self.verdict(state=quiet_state)["passed"])

    def test_out_of_order_sequence_fails(self):
        rows = good_packets()
        rows[6]["sequence"] = rows[5]["sequence"]
        self.assertFalse(self.verdict(rows)["checks"]["sequence_monotonic"])

    def test_one_packet_or_non_integer_sequence_is_not_monotonic_evidence(self):
        """单包、或 sequence 根本不是整数时，"单调"不能算成立——那等于没判。"""
        self.assertFalse(self.verdict(audio_rows(["T"]))["checks"]["sequence_monotonic"])
        rows = good_packets()
        for row in rows:
            row["sequence"] = "1"
        self.assertFalse(self.verdict(rows)["checks"]["sequence_monotonic"])

    def test_a_second_connection_fails(self):
        self.assertFalse(self.verdict(connections=2)["checks"]["single_connection"])
        self.assertFalse(self.verdict(connections=0)["checks"]["single_connection"])
        self.assertFalse(self.verdict(connections=2)["passed"])

    def test_truncated_always_pumping_page_cannot_pass(self):
        """实测复现过的洞（审计 HIGH-1）：完全没接门限、逐帧无条件推流的页面，
        只要采集在静音尾中间截断，旧判定会全项通过。现在必须红。"""
        truncated = audio_rows(["T"] * 8 + ["."] * 12)[:20]   # 截在尾巴里
        self.assertEqual(len(truncated), 20)
        result = self.verdict(truncated)                      # 观察不够
        self.assertFalse(result["checks"]["stream_really_stops_after_tail"])
        self.assertFalse(result["passed"])
        # 就算把观察窗口拉长到够，无门限页面也只是"停在截断处"：尾巴超长，照样红
        long_run = self.verdict(audio_rows(["T"] * 8 + ["."] * 12 + ["T"] * 8))
        self.assertFalse(long_run["passed"])

    def test_console_errors_and_failed_requests_reach_the_verdict(self):
        self.assertFalse(self.verdict(
            console_errors=[{"text": "Uncaught TypeError: x is not a function"}])["checks"]["no_page_errors"])
        self.assertFalse(self.verdict(
            console_errors=[{"text": "boom"}])["passed"])
        self.assertFalse(self.verdict(
            failed_requests=[{"url": "http://127.0.0.1:1/missing.mjs"}])["checks"]["no_page_errors"])

    def test_thresholds_are_reported_so_render_cannot_print_stale_ones(self):
        result = self.verdict(tail_min=5, tail_max=8)
        self.assertEqual(result["thresholds"], {"tail_min": 5, "tail_max": 8})
        text = vl.render({**result, "ok": result["passed"], "artifacts": {}})
        self.assertIn("要求 5~8", text)
        self.assertNotIn("要求 10~16", text)


@unittest.skipUnless(TOOLCHAIN_OK and _browser(), "缺 node/esbuild 或浏览器（%s），跳过真回路"
                    % (vl.visual.browser_probe_reason() or "未知原因"))
class LiveVoiceLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docmind_voice_e2e_")
        (Path(self.tmp) / "frontend" / "src" / "workbench").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_real_loop_passes_and_leaves_evidence(self):
        result = vl.run_voice_loop(ROOT, script=vl.DEFAULT_SCRIPT, wait=12)
        self.assertTrue(result["ok"], json.dumps(
            {"checks": result["checks"], "page": result.get("page_state"),
             "error": result.get("error", "")}, ensure_ascii=False))
        self.assertEqual(result["checks"]["chunk_is_16k_100ms"], True)
        self.assertGreaterEqual(result["tail"]["tail_after_speech"], vl.TAIL_MIN)
        self.assertTrue(result["tail"]["gap_after_tail"] >= vl.RESUME_GAP_SECONDS,
                        json.dumps(result["tail"], ensure_ascii=False))
        self.assertEqual(result["tail"]["stopped"], "yes")
        self.assertTrue(Path(result["artifacts"]["dir"], "run.json").is_file())
        self.assertIn("语音行为回路【通过】", vl.render(result))

    def test_pure_silence_cannot_pass_this_loop(self):
        """反证：整段纯静音时门不开。生产在 hello.ok 之后必发【恰好 1 片】
        audio-ready 静音标记，除此之外一片音频都不该出现，更不该收口。"""
        result = vl.run_voice_loop(ROOT, script="sil:2.5 sil:2.5", wait=6, barge_in=False)
        self.assertFalse(result["ok"])
        self.assertLessEqual(result["packets"], 1,
                             "静音期发出多片 = 门限没在工作，或页面在无条件推流")
        self.assertNotIn("T", result["pattern"], "纯静音里出现了响亮片")
        self.assertFalse(result["checks"]["silence_tail_at_least_1s"])
        self.assertFalse(result["checks"]["tail_closed_the_turn"])
        text = vl.render(result)
        self.assertIn("未通过", text)
        self.assertIn("silence_tail_at_least_1s", text)

    def test_below_threshold_audio_never_closes_a_turn(self):
        """`quiet` 段（文件里 RMS 0.0057 < startRms 0.02）实测经浏览器 AEC/NS 链路后
        能量会被抬到能开门——所以"低于门限"只对【生成的文件】成立，不能拿它在回路上
        断言"绝不开门"。这条钉住我们能断言的部分：不出现成段语音、也不收口。"""
        result = vl.run_voice_loop(ROOT, script="sil:2.0 quiet:1.5 sil:2.0", wait=7,
                                   barge_in=False)
        self.assertFalse(result["ok"])
        self.assertNotIn("T", result["pattern"], "quiet 段不该产生成段响亮语音")
        self.assertFalse(result["checks"]["tail_closed_the_turn"])

    def test_a_project_without_the_frontend_fails_explicitly(self):
        result = vl.run_voice_loop(self.tmp)
        self.assertFalse(result["ok"])
        self.assertIn("前端源码", result["error"])


class ToolSurfaceTests(unittest.TestCase):
    def setUp(self):
        self._prev = config.get_runtime("code_root")
        self.tmp = tempfile.mkdtemp(prefix="docmind_voice_tool_")
        config.set_runtime("code_root", self.tmp)
        self.addCleanup(config.set_runtime, "code_root", self._prev)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_voice_is_documented_everywhere_it_must_be(self):
        import agent
        import tools
        entry = tools.TOOLS["dev_media"]["description"]
        self.assertIn("voice", entry)
        self.assertIn("action: build|run|voice", tools.dev_media.__doc__)
        line = next(l for l in agent._SYSTEM_PROMPT_FULL.splitlines() if l.startswith("- dev_media"))
        self.assertIn("voice", line)

    def test_unknown_action_lists_the_three(self):
        import tools
        text = tools.TOOLS["dev_media"]["func"]("action: teleport")
        self.assertIn("build|run|voice", text)

    def test_a_project_without_the_frontend_reports_failure_not_green(self):
        import tools
        text = tools.TOOLS["dev_media"]["func"]("action: voice")
        self.assertNotIn("【通过】", text)
        self.assertTrue(text.strip(), "必须给出原因，不能空串")


if __name__ == "__main__":
    unittest.main()
