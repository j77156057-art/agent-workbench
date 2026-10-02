"""Wave17b 回归：整页集成守卫——真后端 + 真构建产物 + 真浏览器。

判定表与围栏必须【不起浏览器】就能验（这一半是这条守卫能不能被 CI 跑的关键），
真凭据在现场类里；现场类里除了绿的那一条，还有**页面不存在**与**后端起不来**两条反证——
守卫的红比绿有用。
"""

import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from agent_runtime import app_test as at  # noqa: E402


def _web(root: Path, *, stamp: float = 0.0) -> Path:
    web = root / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "index.html").write_text("<!doctype html><div id=app></div>\n", encoding="utf-8",
                                    newline="\n")
    if stamp:
        os.utime(web / "index.html", (stamp, stamp))
    return web


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docmind_apptest_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class ArgShapeTests(Base):
    def test_page_must_stay_a_bare_in_site_path(self):
        self.assertEqual(at.page_path(""), "/workbench")
        self.assertEqual(at.page_path("/workbench"), "/workbench")
        for bad in ("//evil.example/x", "http://127.0.0.1/x", "../etc/passwd",
                    "/workbench?next=x", "/workbench#y", "/a b", "/a\nb", '"x"'):
            with self.subTest(page=bad):
                with self.assertRaises(at.AppTestError):
                    at.page_path(bad)

    def test_text_needles_are_not_css_and_are_capped(self):
        self.assertEqual(at.text_needles("资源管理器\n0 个分区\n资源管理器"),
                         ["资源管理器", "0 个分区"])
        self.assertEqual(at.text_needles(""), [])
        with self.assertRaises(at.AppTestError):
            at.text_needles("\n".join("t%d" % i for i in range(20)))
        with self.assertRaises(at.AppTestError):
            at.text_needles("x" * 200)

    def test_server_argv_pins_loopback_and_the_given_port(self):
        argv = at.server_argv(51_234, python="/x/python")
        self.assertEqual(argv[0], "/x/python")
        self.assertIn("api:app", argv)
        self.assertIn("--host", argv)
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
        self.assertEqual(argv[argv.index("--port") + 1], "51234")
        for port in (0, 80, 99999):
            with self.subTest(port=port):
                with self.assertRaises(at.AppTestError):
                    at.server_argv(port, python="p")

    def test_child_env_keeps_the_rest_of_the_environment(self):
        """`process_runner.clean_environment` 拿到什么就用什么——只传一个键会让
        uvicorn 连 PATH/SYSTEMROOT 都拿不到（实测这类错表现为"进程起了又退了"）。"""
        env = at.server_env(Path("D:/tmp/state"))
        self.assertEqual(env["DOCMIND_STATE_ROOT"], str(Path("D:/tmp/state")))
        # Windows 上 os.environ 里是 `Path`/`SystemRoot`——按大小写无关地核对，
        # 否则这条测试只是在赌平台
        for key in ("PATH", "SystemRoot" if os.name == "nt" else "HOME"):
            self.assertTrue([name for name in env if name.upper() == key.upper()],
                            "%s 不能丢" % key)

    def test_only_loopback_and_only_the_two_api_paths(self):
        """这条是 SSRF 围栏：工具能碰的地址必须钉死在本机 + 两个已知接口上。"""
        for bad in ("http://example.com/api/projects", "http://10.0.0.5/api/projects",
                    "http://127.0.0.1/api/ingest_code", "http://127.0.0.1/static/x"):
            with self.subTest(url=bad):
                with self.assertRaises(at.AppTestError):
                    at.http_json(bad)


class SandboxTests(Base):
    def test_each_run_gets_its_own_unique_fixture(self):
        first = at.make_sandbox(self.tmp)
        second = at.make_sandbox(self.tmp)
        self.addCleanup(shutil.rmtree, first["sandbox"], ignore_errors=True)
        self.addCleanup(shutil.rmtree, second["sandbox"], ignore_errors=True)
        self.assertNotEqual(first["fixture"], second["fixture"])
        self.assertTrue((first["project"] / first["fixture"]).is_file())
        self.assertTrue(first["state"].is_dir())
        self.assertNotEqual(first["sandbox"], second["sandbox"])


class BundleFreshnessTests(Base):
    def test_a_tree_without_a_build_says_so(self):
        got = at.bundle_freshness(self.tmp)
        self.assertFalse(got["built"])
        self.assertFalse(got["fresh"])
        self.assertIn("index.html", got["detail"])

    def test_source_newer_than_the_build_is_not_fresh(self):
        now = time.time()
        _web(self.tmp, stamp=now - 3600)
        src = self.tmp / "frontend" / "src"
        src.mkdir(parents=True)
        (src / "App.vue").write_text("<template/>\n", encoding="utf-8", newline="\n")
        got = at.bundle_freshness(self.tmp)
        self.assertTrue(got["built"])
        self.assertFalse(got["fresh"])
        self.assertGreater(got["stale_by"], 3000)

    def test_build_newer_than_source_is_fresh(self):
        now = time.time()
        src = self.tmp / "frontend" / "src"
        src.mkdir(parents=True)
        (src / "App.vue").write_text("<template/>\n", encoding="utf-8", newline="\n")
        os.utime(src / "App.vue", (now - 3600, now - 3600))
        _web(self.tmp, stamp=now)
        self.assertTrue(at.bundle_freshness(self.tmp)["fresh"])

    def test_node_modules_never_count_as_source(self):
        now = time.time()
        _web(self.tmp, stamp=now - 3600)
        junk = self.tmp / "frontend" / "src" / "node_modules" / "pkg"
        junk.mkdir(parents=True)
        (junk / "index.js").write_text("1\n", encoding="utf-8", newline="\n")
        os.utime(junk / "index.js", (now, now))
        self.assertTrue(at.bundle_freshness(self.tmp)["fresh"],
                        "把 node_modules 算进源码会让这条永远红")


class ActivationTests(Base):
    def test_a_200_from_the_register_call_alone_is_not_enough(self):
        """必须 GET 回来核对 current 与清单——只信 POST 的 200 会出现"登记表根本没写、
        页面却在空项目下正常渲染"的假绿。project_id 由 `posted` 自己供，调用方拿不到
        第二条路去填一个"看起来对"的 id。"""
        listing = {"ok": True, "current": "prj-1", "projects": [{"project_id": "prj-1"}]}
        good = at.activate(200, listing, {"project_id": "prj-1"})
        self.assertTrue(good["ok"])
        self.assertEqual(good["project_id"], "prj-1")
        for name, row in (
                ("注册接口没给 id", at.activate(200, listing, {})),
                ("HTTP 非 200", at.activate(500, listing, {"project_id": "prj-1"})),
                ("激活的是别的项目", at.activate(200, dict(listing, current="prj-9"),
                                                 {"project_id": "prj-1"})),
                ("清单里不止这一次的项目", at.activate(
                    200, {"ok": True, "current": "prj-1",
                          "projects": [{"project_id": "prj-1"}, {"project_id": "prj-0"}]},
                    {"project_id": "prj-1"})),
                ("回包根本不是 dict", at.activate(200, "not json", "<html>")),
        ):
            with self.subTest(name):
                self.assertFalse(row["ok"])

    def test_isolation_needs_both_the_write_and_the_scoped_registry(self):
        verdict = at.activate(200, {"current": "prj-1", "projects": [{"project_id": "prj-1"}]},
                              {"project_id": "prj-1"})
        empty = self.tmp / "state"
        empty.mkdir()
        self.assertFalse(at.state_isolated(empty, verdict), "什么都没写进去不算隔离成功")
        (empty / ".docmind_state.json").write_text("{}", encoding="utf-8", newline="\n")
        self.assertTrue(at.state_isolated(empty, verdict))
        self.assertFalse(at.state_isolated(empty, {"ok": False, "listed": ["a", "b"]}),
                         "漏传 DOCMIND_STATE_ROOT 时会读到仓库真登记表——必须红")
        # 就算注册那一步自认为成功，登记表里混着别的项目也不是"这次是隔离的"
        self.assertFalse(at.state_isolated(empty, {"ok": True, "listed": ["prj-1", "prj-0"]}))


class ProbeTests(Base):
    """`read_facts` / `click_and_read` 的注入面：真浏览器的形状在这里被固定下来。"""

    class Fake:
        def __init__(self, tree="PY  probe_abc123.py", counts=None):
            self.tree = tree
            self.counts = {".wb-shell": 1, ".ws-tab": 4, ".ft-row": 1, **(counts or {})}
            self.active = ["概览", "素材"]
            self.clicks = []

        def probe(self, mode, target, scope=""):
            if mode == "count":
                return self.counts.get(target, 0)
            if target == ".ft-body":
                return self.tree
            if target == "main":
                # 活动标签决定正文；空标签＝整块没挂上
                return {"概览": "概览驾驶舱", "素材": "素材中心"}.get(self.active[0], "")
            if target == ".ws-active":
                return self.active[0]
            return ""

        def click(self, value, scope):
            self.clicks.append((value, scope))
            self.active[0] = self.active[1]
            return {"ok": True, "effect": "changed"}

    def test_the_fixture_is_matched_by_tree_text_not_by_text_locator(self):
        """`page_action` 的 `text:` 只在**可交互元素**里找，文件树那行是个 `<span>`——
        实测命中 0 个，所以凭据必须是"文件树容器的文本里有这个文件名"。"""
        fake = self.Fake()
        got = at.read_facts(fake.probe, fixture="probe_abc123.py")
        self.assertTrue(got["fixture_in_tree"])
        self.assertEqual((got["shell"], got["tabs"], got["rows"]), (1, 4, 1))
        other = at.read_facts(fake.probe, fixture="probe_zzzzzz.py")
        self.assertFalse(other["fixture_in_tree"], "别的运行的文件名不能冒充这一行的凭据")

    def test_clicking_outside_the_nav_bar_is_not_how_this_finds_the_tab(self):
        """`text:` 全站找同名元素，「素材」在顶栏「素材区」徽标上也有——必须限定在
        工作区标签条里点，否则点到别的按钮还报生效。"""
        fake = self.Fake()
        at.click_and_read(fake.probe, fake.click, tab="素材")
        self.assertEqual(fake.clicks, [("素材", ".ws-bar")])

    def test_content_change_is_a_digest_not_a_truncated_length(self):
        """不能用 `#app *` 计数：`page_action` 的候选清单在 64 个就截断，
        实测切工作区前后都报 64；html 长度同理被 4000 字上限砍平。"""
        fake = self.Fake()
        before = at.read_facts(fake.probe, fixture="probe_abc123.py")
        after = at.click_and_read(fake.probe, fake.click, tab="素材")
        self.assertTrue(after["click_ok"])
        self.assertNotEqual(before["content_before"], after["content_after"])
        self.assertTrue(before["content_before"].split(":")[0].isdigit())

    def test_an_empty_body_is_not_a_content_change(self):
        blank = self.Fake(tree="")
        blank.active = ["概览", ""]
        before = at.read_facts(blank.probe, fixture="x")
        after = at.click_and_read(blank.probe, blank.click, tab="素材")
        judged = at.evaluate(dict(observation(), **dict(before, **after)),
                             WANT, FRESH, CLEAN)
        self.assertIn("page_content_changed_on_click", judged["failed"],
                      "点完变成空正文是崩了，不是换了视图")


def observation(over=None, **extra):
    base = {"ready": True, "isolated": True, "activated": True, "shell": 1, "tabs": 4,
            "rows": 1, "fixture_in_tree": True, "console_errors": [], "failed_requests": [],
            "click_ok": True, "active_before": "概览", "active_after": "素材",
            "content_before": "467:aaaa", "content_after": "182:bbbb",
            "body_truncated": False, "texts": {}}
    base.update(over or {})
    base.update(extra)
    return base


FRESH = {"built": True, "fresh": True, "stale_by": 0, "detail": "x"}
CLEAN = {"removed": True, "job_stopped": True, "live_pages": 0}
WANT = {"tab": "素材", "texts": [], "tabs": 4}


class EvaluateTableTests(Base):
    def evaluate(self, obs=None, wanted=None, fresh=None, cleanup=None):
        return at.evaluate(observation(**(obs or {})), dict(WANT, **(wanted or {})),
                           dict(fresh or FRESH), dict(cleanup or CLEAN))

    def test_a_faithful_run_passes_every_line(self):
        got = self.evaluate()
        self.assertTrue(got["passed"], got["failed"])

    def test_the_backend_must_have_actually_answered(self):
        self.assertIn("backend_started", self.evaluate({"ready": False})["failed"])

    def test_an_empty_page_is_not_a_rendered_page(self):
        """外壳/标签是"页面确实挂起来了"的最低证据：少一个标签也算没挂对。"""
        self.assertIn("shell_mounted", self.evaluate({"shell": 0})["failed"])
        self.assertIn("nav_renders_every_tab", self.evaluate({"tabs": 3})["failed"])
        self.assertIn("nav_renders_every_tab", self.evaluate({"tabs": 5})["failed"])

    def test_a_stale_bundle_is_red_even_when_the_page_looks_fine(self):
        """这一条是整页守卫最容易骗自己的地方：页面绿、跑的却是旧构建。"""
        got = self.evaluate(fresh={"built": True, "fresh": False, "stale_by": 900, "detail": "x"})
        self.assertIn("bundle_built_after_latest_source", got["failed"])

    def test_an_unbuilt_tree_is_red_too(self):
        self.assertIn("bundle_built_after_latest_source",
                      self.evaluate(fresh={"built": False, "fresh": False})["failed"])

    def test_state_must_be_isolated(self):
        self.assertIn("state_isolated", self.evaluate({"isolated": False})["failed"])

    def test_the_project_actually_has_to_be_current(self):
        self.assertIn("project_activated", self.evaluate({"activated": False})["failed"])

    def test_a_rendered_page_without_backend_data_is_red(self):
        """外壳、标签全在，但文件树里没有这次那个文件＝后端那段没通。"""
        self.assertIn("backend_data_reaches_page",
                      self.evaluate({"fixture_in_tree": False})["failed"])
        self.assertIn("backend_data_reaches_page", self.evaluate({"rows": 0})["failed"])

    def test_console_noise_and_failed_requests_are_red(self):
        self.assertIn("console_clean",
                      self.evaluate({"console_errors": [{"text": "boom"}]})["failed"])
        self.assertIn("no_failed_requests",
                      self.evaluate({"failed_requests": [{"url": "/api/x"}]})["failed"])

    def test_a_click_that_changed_nothing_is_red(self):
        self.assertIn("nav_click_applied", self.evaluate({"click_ok": False})["failed"])
        self.assertIn("view_changed_on_click", self.evaluate({"active_after": "概览"})["failed"])
        self.assertIn("view_changed_on_click", self.evaluate({"active_after": "开发舱"})["failed"])
        # 活动标签本来就是「素材」？那"点了素材"什么都没发生——光比"事后是不是它"看不出来
        self.assertIn("view_changed_on_click",
                      self.evaluate({"active_before": "素材", "active_after": "素材"})["failed"])
        self.assertIn("page_content_changed_on_click",
                      self.evaluate({"content_after": "467:aaaa"})["failed"])
        self.assertIn("page_content_changed_on_click",
                      self.evaluate({"content_after": "0:da39a3ee"})["failed"])

    def test_a_truncated_read_cannot_claim_a_text_is_absent(self):
        wanted = {"texts": ["资源管理器"]}
        found = self.evaluate({"texts": {"资源管理器": True}}, wanted=wanted)
        self.assertNotIn("extra_texts_rendered", found["failed"])
        for name, row in (("读到 False", {"texts": {"资源管理器": False}}),
                          ("根本没读这个键", {"texts": {}}),
                          ("读成 None", {"texts": {"资源管理器": None}})):
            with self.subTest(name):
                self.assertIn("extra_texts_rendered", self.evaluate(row, wanted=wanted)["failed"])
        blind = self.evaluate({"texts": {"资源管理器": False}, "body_truncated": True},
                              wanted=wanted)
        self.assertIn("page_text_read_wholly", blind["failed"],
                      "正文没读全时不能说「没有」——但要红出来")

    def test_leftovers_are_red(self):
        # 每条单独翻红，其余照旧是干净的——否则"缺键"本身就让断言成立，测的是运气
        for name, row in (("沙箱没删掉", {"removed": False}), ("服务没停", {"job_stopped": False}),
                          ("页面还开着", {"live_pages": 1})):
            with self.subTest(name):
                self.assertIn("artifacts_cleaned",
                              self.evaluate(cleanup=dict(CLEAN, **row))["failed"])


class RenderTests(Base):
    def test_a_green_run_says_green_and_names_the_boundary(self):
        judged = at.evaluate(observation(), WANT, FRESH, CLEAN)
        text = at.render({"ok": True, "checks": judged["assertions"], "url": "http://x/y",
                          "elapsed": 8.4, "failed": [], "observed": {"shell": 1},
                          "bundle": dict(FRESH, detail="a/b"),
                          "cleanup": dict(CLEAN, sandbox="D:/tmp/x")})
        self.assertIn("整页守卫【通过】", text)
        self.assertIn("边界", text)
        self.assertNotIn("未通过项", text)

    def test_a_red_run_names_every_failed_line(self):
        judged = at.evaluate(observation({"ready": False}), WANT, FRESH, CLEAN)
        text = at.render({"ok": False, "checks": judged["assertions"],
                          "failed": judged["failed"], "error": "后端没起来",
                          "log_tail": "ImportError: x"})
        self.assertIn("整页守卫【未通过】", text)
        self.assertIn("未通过项：backend_started", text)
        self.assertIn("ImportError", text)
        self.assertIn("为什么不可信", text)

    def test_no_checks_means_it_never_started(self):
        self.assertIn("未能开始", at.render({"checks": {}, "error": "没有 web/"}))


def stream_observation(**over):
    base = {"native": True, "mic_on": True,
            "heard": [{"chunks": 29, "bytes": 92800, "peak": 0.685}],
            "replied": [{"bytes": 92800, "round": 1}, {"bytes": 92800, "round": 2}],
            "rounds": [1, 2]}
    base.update(over)
    return base


CAPTION_TEXT = ("你说 | 29 片 · 92800 字节 · 峰值 0.685 | AI | "
                "收到 92800 字节，峰值 0.685，第 1 轮剧本回复。 | 你说 | 31 片 · 99200 字节 · "
                "峰值 0.685 | AI | 收到 99200 字节，峰值 0.685，第 2 轮剧本回复。")
MODE_TEXT = "当前模式：原生实时 · 模型能力：麦克风音频输入 · 可打断"


class StreamParsingTests(unittest.TestCase):
    """流式那一档的判定：纯字符串就能验，所以 CI 上不必起浏览器也有牙。"""

    def test_captions_are_parsed_into_measurable_numbers(self):
        got = at.read_turns(CAPTION_TEXT, MODE_TEXT)
        self.assertTrue(got["native"])
        self.assertEqual([row["bytes"] for row in got["heard"]], [92800, 99200])
        self.assertEqual([row["chunks"] for row in got["heard"]], [29, 31])
        self.assertEqual(got["rounds"], [1, 2])
        # `mic_on` 不是从字幕文本里读出来的（它是另一次 DOM 计数），所以这里
        # 只要求"字幕能撑起来的那四条"全绿
        checks = at.stream_checks(dict(got, mic_on=True))
        self.assertEqual([name for name, ok in checks.items() if not ok], [],
                         "形状对的字幕应当全条绿：%s" % checks)
        self.assertEqual([name for name, ok in at.stream_checks(got).items() if not ok],
                         ["stream_mic_started"], "少了麦克风状态就该只红那一条")

    def test_a_sample_that_is_all_silence_is_not_a_measurement(self):
        """喂静音也能凑出"0 片 · 0 字节"这种自洽数字，所以峰值必须单独卡一道。"""
        got = at.read_turns("你说 | 12 片 · 38400 字节 · 峰值 0.000 | AI | "
                           "收到 38400 字节，第 1 轮剧本回复。| x 收到 38400 字节，第 2 轮",
                           MODE_TEXT)
        checks = at.stream_checks(got)
        self.assertIs(checks["stream_audio_measured"], False, got["heard"])

    def test_numbers_that_do_not_add_up_are_a_fabricated_page(self):
        """字节数必须等于片数 × 3200（16k mono 100ms）：对不上就是页面自己写了串数字。"""
        checks = at.stream_checks(stream_observation(
            heard=[{"chunks": 9, "bytes": 92800, "peak": 0.685}]))
        self.assertIs(checks["stream_audio_measured"], False)

    def test_the_reply_has_to_cite_the_same_measurement(self):
        """转写与回包是两个独立事件：引用同一个字节数才互相印证。"""
        checks = at.stream_checks(stream_observation(
            replied=[{"bytes": 11111, "round": 1}, {"bytes": 11111, "round": 2}]))
        self.assertIs(checks["stream_reply_matches_measured_audio"], False)

    def test_one_lucky_turn_is_not_a_loop(self):
        checks = at.stream_checks(stream_observation(rounds=[1], replied=[{"bytes": 92800,
                                                                          "round": 1}]))
        self.assertIs(checks["stream_turns_repeat"], False)

    def test_a_degraded_session_is_red_before_anything_else(self):
        checks = at.stream_checks(stream_observation(native=False))
        self.assertIs(checks["stream_negotiated_native"], False)

    def test_a_mic_that_never_opened_is_its_own_red_line(self):
        """"没转写"要能分清是麦没开还是上行不通：合成一条红会浪费一整次现场排查。"""
        checks = at.stream_checks(stream_observation(mic_on=False))
        self.assertIs(checks["stream_mic_started"], False)

    def test_evaluate_merges_the_stream_table_only_when_asked(self):
        observed = dict(observation(), **stream_observation())
        plain = at.evaluate(observed, WANT, FRESH, CLEAN)
        asked = at.evaluate(observed, dict(WANT, stream=True, rounds=2), FRESH, CLEAN)
        self.assertNotIn("stream_audio_measured", plain["assertions"])
        self.assertTrue(asked["passed"], asked["failed"])
        broken = at.evaluate(dict(observed, native=False),
                             dict(WANT, stream=True, rounds=2), FRESH, CLEAN)
        self.assertIn("stream_negotiated_native", broken["failed"])


class StreamPlumbingTests(unittest.TestCase):
    def test_the_realtime_tier_overrides_whatever_the_environment_already_says(self):
        """`realtime: 1` 必须**覆盖**ambient 的适配器选择（本机 .env 就设着
        `dashscope_omni`——写"不含这个键"的断言只在一台干净的机器上才成立）。"""
        with patch.dict(os.environ, {"DOCMIND_REALTIME_PROVIDER": "dashscope_omni"}):
            plain = at.server_env(Path("D:/tmp/state"))
            forced = at.server_env(Path("D:/tmp/state"), realtime=True)
        self.assertEqual(plain["DOCMIND_REALTIME_PROVIDER"], "dashscope_omni",
                         "不开这一档，这个函数不该动实时配置")
        self.assertEqual(forced["DOCMIND_REALTIME_PROVIDER"], "scripted",
                         "开了这一档就必须压过 ambient，否则整页跑在真厂商上")
        for key in ("PATH", "SystemRoot" if os.name == "nt" else "HOME"):
            self.assertTrue([k for k in forced if k.upper() == key.upper()], "%s 不能丢" % key)

    def test_stream_flags_point_at_files_that_actually_exist(self):
        """`--use-file-for-fake-audio-capture` 指向不存在的文件时 Edge 会静默回落成
        内置假设备——"跑通了但内容是假的"比红更糟。"""
        tmp = Path(tempfile.mkdtemp(prefix="docmind_stream_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sandbox = {"sandbox": tmp}
        flags = at.stream_flags(sandbox)
        joined = " ".join(flags)
        self.assertIn("--use-file-for-fake-audio-capture=", joined)
        self.assertIn("--use-file-for-fake-video-capture=", joined)
        for row in flags:
            if "=" in row and "capture" in row:
                self.assertTrue(Path(row.split("=", 1)[1]).is_file(), row)

    def test_the_stream_steps_click_the_nav_scoped_and_the_mic_last(self):
        labels = [row[0] for row in at.STREAM_STEPS]
        self.assertEqual(labels[:2], ["开发舱", "打开摄像头"])
        self.assertEqual(at.STREAM_STEPS[0][1], ".ws-bar",
                         "「开发舱」在别处也有同名文字，必须限定在工作区标签条里点")
        self.assertIn("麦", labels[-1], "不开麦就没有上行音频，顺序错了会把没声音误判成不通")


def _fake_root(tmp: Path, *, body: str, built=True) -> Path:
    root = tmp / ("root-%d" % (time.time_ns() % 100000))
    root.mkdir(parents=True)
    (root / "api.py").write_text(body, encoding="utf-8", newline="\n")
    if built:
        _web(root, stamp=time.time())
    return root


class PreflightTests(Base):
    def test_a_root_without_a_backend_entry_never_starts_anything(self):
        """前置检查必须在前头：没有 `api.py` 就别去起进程、别开浏览器。"""
        empty = self.tmp / "empty"
        empty.mkdir()
        got = at.run_app_test(empty, timeout=30.0)
        self.assertFalse(got["ok"])
        self.assertIn("api.py", got["error"])
        self.assertEqual(got["checks"], {}, "没开始就不该有一张判定表")

    def test_a_tree_without_a_build_never_starts_anything(self):
        root = self.tmp / "no-build"
        root.mkdir()
        (root / "api.py").write_text("app = None\n", encoding="utf-8", newline="\n")
        got = at.run_app_test(root, timeout=30.0)
        self.assertFalse(got["ok"])
        self.assertIn("index.html", got["error"])
        self.assertEqual(got["checks"], {})

    def test_a_bad_page_is_refused_before_the_backend_boots(self):
        got = at.run_app_test(ROOT, page="//evil.example/x", timeout=30.0)
        self.assertFalse(got["ok"])
        self.assertEqual(got["checks"], {})
        self.assertIn("page", got["error"])


@unittest.skipUnless((Path(ROOT) / "web" / "index.html").is_file()
                     and (Path(ROOT) / "api.py").is_file(),
                     "跳过整页现场：没有可用的 web/ 构建产物")
class LivePageTests(Base):
    def test_the_real_page_passes_every_line(self):
        got = at.run_app_test(ROOT, texts="资源管理器", timeout=120.0)
        detail = json.dumps({"failed": got.get("failed"), "observed": got.get("observed"),
                             "error": got.get("error"), "cleanup": got.get("cleanup")},
                            ensure_ascii=False)[:900]
        if not at.bundle_freshness(ROOT)["fresh"]:
            # 别人正在改前端时会恰好卡在"旧构建"这一条：它必须是**唯一**的红，
            # 其余每条照样得绿——否则这条测试就成了"允许整页是坏的"。
            self.assertEqual(got["failed"], ["bundle_built_after_latest_source"], detail)
        else:
            self.assertTrue(got["ok"], detail)
        self.assertEqual(got["observed"]["tabs"], 4)
        self.assertEqual([got["observed"]["active_before"], got["observed"]["active_after"]],
                         ["概览", "素材"], "活动标签必须真的换过了")
        self.assertTrue(got["observed"]["fixture_in_tree"])
        self.assertLess(got["elapsed"], 90.0, "非流式那一档不该被流式拖慢")

    def test_a_page_that_does_not_exist_turns_the_guard_red(self):
        """反证一：换到一个没有的页面——后端照样活着，守卫必须红而不是"没看到错＝通过"。"""
        got = at.run_app_test(ROOT, page="/no-such-page-for-apptest", timeout=90.0)
        self.assertFalse(got["ok"], json.dumps(got.get("checks"), ensure_ascii=False))
        self.assertTrue(got["checks"].get("backend_started"), "后端是活的，红的应该是页面")
        self.assertTrue({"shell_mounted", "backend_data_reaches_page"} & set(got["failed"]),
                        got["failed"])

    def test_a_backend_that_cannot_start_is_red_not_silent(self):
        """反证二：`api.py` 导入就抛错。这条盯的是"进程起来了又退了"那段必须报出来，
        而不是所有断言一起"莫名没成立"。"""
        root = _fake_root(self.tmp, body="raise ImportError('这个后端起不来')\n")
        got = at.run_app_test(root, timeout=60.0, ready_wait=30.0)
        self.assertFalse(got["ok"])
        self.assertIs(got["checks"].get("backend_started"), False)
        self.assertIn("起不来", str(got.get("error")) + str(got.get("log_tail")))

    def test_a_tree_without_a_build_never_starts_a_browser(self):
        root = self.tmp / "no-build"
        root.mkdir()
        (root / "api.py").write_text("app = None\n", encoding="utf-8", newline="\n")
        got = at.run_app_test(root, timeout=30.0)
        self.assertFalse(got["ok"])
        self.assertIn("index.html", got["error"])
        self.assertEqual(page_count(), 0, "前置检查就该挡住，不该起浏览器")

    def test_the_stream_tier_actually_hears_the_fake_microphone(self):
        """整页 + 假麦克风 + scripted：这是本仓第一次自动跑到"真 ws 回合"。

        旧构建那条红允许单独存在（别人正在改前端时会恰好卡在那儿），其余每条——
        包括四条流式判据——都必须绿。
        """
        got = at.run_app_test(ROOT, stream=True, timeout=180.0)
        detail = json.dumps({"failed": got.get("failed"),
                             "observed": {k: (got.get("observed") or {}).get(k)
                                          for k in ("native", "heard", "replied", "rounds",
                                                    "stream_steps", "stream_captions")},
                             "error": got.get("error")}, ensure_ascii=False)[:1200]
        if not at.bundle_freshness(ROOT)["fresh"]:
            self.assertEqual([row for row in (got.get("failed") or [])
                              if row != "bundle_built_after_latest_source"], [], detail)
        else:
            self.assertTrue(got["ok"], detail)
        checks = got["checks"]
        for name in ("stream_negotiated_native", "stream_mic_started",
                     "stream_audio_measured", "stream_reply_matches_measured_audio",
                     "stream_turns_repeat"):
            self.assertIs(checks.get(name), True, detail)
        heard = got["observed"]["heard"]
        self.assertTrue(all(row["bytes"] == row["chunks"] * 3200 for row in heard), heard)
        self.assertGreaterEqual(len(got["observed"]["rounds"]), 2, detail)

    def test_nothing_is_left_behind(self):
        got = at.run_app_test(ROOT, timeout=120.0)
        self.assertTrue(got["cleanup"]["removed"], got["cleanup"])
        self.assertFalse(Path(got["cleanup"]["sandbox"]).exists())
        self.assertTrue(got["cleanup"]["job_stopped"])
        self.assertEqual(got["cleanup"]["live_pages"], 0)


def page_count() -> int:
    from agent_runtime import page_action
    return len(page_action.live_pages())


class ToolSurfaceTests(unittest.TestCase):
    def test_registered_in_all_three_places(self):
        import agent
        import tools
        self.assertIn("dev_apptest", tools.TOOLS)
        self.assertIn("bundle_built_after_latest_source",
                      tools.TOOLS["dev_apptest"]["description"])
        self.assertIn("component", tools.dev_vue.__doc__)
        self.assertIn("整页", tools.dev_apptest.__doc__)
        line = next(l for l in agent._SYSTEM_PROMPT_FULL.splitlines()
                    if l.startswith("- dev_apptest"))
        self.assertIn("真后端", line)
        self.assertEqual(len(tools.tool_schemas()), len(tools.TOOLS))

    def test_it_is_not_exposed_as_a_read_only_mcp_tool(self):
        from agent_runtime import mcp_server
        self.assertIn("dev_apptest", mcp_server.WRITE_TOOLS,
                      "会起后端进程与真浏览器的工具不能进默认只读表")

    def test_bad_arguments_are_refused_before_anything_starts(self):
        import tools
        prev = config.get_runtime("code_root")
        config.set_runtime("code_root", ROOT)
        self.addCleanup(config.set_runtime, "code_root", prev)
        for arg in ("page: //evil.example/x", "page: ../etc/passwd", "timeout: abc",
                    "timeout: 3", "text: " + "x" * 300):
            with self.subTest(arg=arg[:24]):
                text = tools.TOOLS["dev_apptest"]["func"](arg)
                self.assertTrue(text.strip(), arg)
                self.assertNotIn("【通过】", text)

    def test_the_prompt_catalog_has_one_clean_line_per_tool(self):
        """目录里每个工具必须**自己一行、行内不夹带别人**。

        整行替换时用下一行的前缀当锚点，会把那条工具的正文吞进这一行
        （Wave17 真发生过：`dev_testrun` 的正文被并进了 `dev_apptest` 那行），
        而它只表现为"提示词长了一点"，没有任何测试会红。
        只查"有独立行的那些工具"：`dev_mcp_*`、外部文件与记忆那批是分组登记的，
        不是一行一工具，硬要求会变成假红。
        """
        import agent
        import tools
        catalog = agent._SYSTEM_PROMPT_FULL
        rows = {}
        for row in catalog.splitlines():
            for name in tools.TOOLS:
                if row.startswith("- %s(" % name) or row.startswith("- %s:" % name):
                    rows.setdefault(name, []).append(row)
        for name in ("dev_page_action", "dev_media", "dev_headcheck", "dev_testrun",
                     "dev_vue", "dev_apptest"):
            self.assertEqual(len(rows.get(name, [])), 1,
                             "%s 在提示词目录里应当恰好一行" % name)
        for name, hits in rows.items():
            with self.subTest(tool=name):
                self.assertEqual(len(hits), 1, "目录里 %s 出现多行" % name)
                tail = hits[0][-160:]
                swallowed = [other for other, other_rows in rows.items()
                             if other != name and tail in other_rows[0]]
                self.assertFalse(swallowed,
                                 "%s 这一行的尾部同时出现在 %s 里：有一行被吞并了"
                                 % (name, swallowed))

    def test_every_documented_key_reaches_the_module(self):
        """文档里写过的 keyed 参数必须真的进了 `_parse_keyed` 的 keys——
        `dev_testrun` 就因为 `coverage` 漏在 keys 里而被静默丢掉，变成"跑一次全量"。"""
        import ast as _ast
        import inspect
        import tools
        # 用 inspect.getdoc：docstring 的缩进按实现细节会变，判据不能建在它上面
        doc = inspect.getdoc(tools.dev_apptest) or ""
        documented = set(re.findall(r"^\s*(\w+):\s", doc, re.M))
        tree = _ast.parse(inspect.getsource(tools.dev_apptest))
        listed = set()
        for node in _ast.walk(tree):
            if (isinstance(node, _ast.Call) and getattr(node.func, "id", "") == "_parse_keyed"
                    and len(node.args) > 1 and isinstance(node.args[1], _ast.List)):
                listed |= {str(row.value) for row in node.args[1].elts}
        self.assertTrue(documented, "文档里一个 keyed 参数都没写？")
        self.assertEqual(documented - listed, set(),
                         "这些参数写了但没交给模块：%s" % sorted(documented - listed))

    def test_a_tree_without_a_build_reports_failure_not_green(self):
        import tools
        stray = tempfile.mkdtemp(prefix="docmind_apptest_tool_")
        prev = config.get_runtime("code_root")
        config.set_runtime("code_root", stray)
        self.addCleanup(config.set_runtime, "code_root", prev)
        self.addCleanup(shutil.rmtree, stray, ignore_errors=True)
        text = tools.TOOLS["dev_apptest"]["func"]("")
        self.assertTrue(text.strip())
        self.assertNotIn("【通过】", text)
        self.assertIn("未能开始", text)


if __name__ == "__main__":
    unittest.main()
