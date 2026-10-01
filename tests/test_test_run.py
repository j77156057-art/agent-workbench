"""Wave15 回归：把"跑测试 → 读失败 → 只重跑失败"变成结构化的一步。

要防的是这一类假干净：pytest 在**一个用例都没收集到**时退出码是 5，而 `run_command` 把整屏
文本交回来说"没看到 FAILED"就等于通过；`-q` 的失败摘要也最容易在读文本时被漏掉。
另外本工作树的 `.pytest_cache` 是 Permission denied，所以 pytest 自己的 `--lf` 靠不住，
记账必须由本模块做——而且要能在"记账没写成"的时候变红。
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
from agent_runtime import test_run as tr  # noqa: E402

REAL_SUMMARY = """................FFFF                                       [100%]
================================== FAILURES ==================================
D:\\WorkBuddy\\rag-agent\\tests\\test_a.py:12: AssertionError: 1 != 2
D:\\WorkBuddy\\rag-agent\\tests\\test_a.py:18: IndexError: list index out of range
=========================== short test summary info ============================
FAILED tests/test_a.py::T::test_one - AssertionError: 1 != 2
FAILED tests/test_a.py::T::test_two - IndexError: list index out of range
ERROR tests/test_a.py::T::test_three
2 failed, 8 passed, 1 error, 3 subtests passed in 1.23s
"""

GOOD_SUMMARY = """.....                                                                    [100%]
5 passed in 0.42s
"""


class FakeRunner:
    """假 subprocess：记下 argv，按顺序回放多段输出（`related` 会先 collect 再 run）。"""

    def __init__(self, output="", exit_code=0, timed_out=False,
                 sequence=None):
        self.scripted = list(sequence) if sequence is not None else [
            {"output": output, "exit_code": exit_code, "timed_out": timed_out}]
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": kwargs})
        step = self.scripted[min(len(self.calls) - 1, len(self.scripted) - 1)]
        code = step.get("exit_code", 0)
        if step.get("timed_out"):
            return {"ok": False, "exit_code": None, "output": step.get("output", ""),
                    "timed_out": True, "error": "timeout"}
        return {"ok": code == 0, "exit_code": code, "output": step.get("output", ""),
                "timed_out": False, "error": ""}


class StateCase(unittest.TestCase):
    def setUp(self):
        self._prev_state = config.STATE_ROOT
        self.state = tempfile.mkdtemp(prefix="docmind_testrun_state_")
        config.STATE_ROOT = self.state
        self.project = tempfile.mkdtemp(prefix="docmind_testrun_proj_")
        self.addCleanup(setattr, config, "STATE_ROOT", self._prev_state)
        self.addCleanup(shutil.rmtree, self.project, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)


class ParseTests(unittest.TestCase):
    def test_counts_and_failure_rows(self):
        parsed = tr.parse_output(REAL_SUMMARY, 1)
        self.assertEqual(parsed["counts"]["failed"], 2)
        self.assertEqual(parsed["counts"]["passed"], 8)
        self.assertEqual(parsed["counts"]["error"], 1)
        self.assertEqual(parsed["subtests_passed"], 3)
        self.assertEqual(len(parsed["failures"]), 3, "ERROR 行也是必须列出来的失败")
        self.assertEqual(parsed["failures"][0]["nodeid"], "tests/test_a.py::T::test_one")
        self.assertIn("1 != 2", parsed["failures"][0]["reason"])
        self.assertTrue(parsed["summary_parsed"] and parsed["collected"])

    def test_duration_is_seconds_not_minutes(self):
        self.assertAlmostEqual(tr.parse_output(REAL_SUMMARY, 1)["duration"], 1.23, places=2)

    def test_no_summary_is_not_green(self):
        parsed = tr.parse_output("Traceback (most recent call last):\n  boom\n", 0)
        self.assertFalse(parsed["summary_parsed"])
        self.assertFalse(parsed["collected"])

    def test_exit_5_no_tests_collected_is_visible(self):
        parsed = tr.parse_output("no tests ran in 0.01s\n", 5)
        self.assertTrue(parsed["no_tests_ran"])
        self.assertFalse(parsed["collected"])


class CommandShapeTests(unittest.TestCase):
    def setUp(self):
        self.argv = tr.command(paths=["tests/test_a.py"], keyword="slow",
                               nodeids=["tests/test_b.py::T::t"], basetemp="/tmp/bt")

    def test_cacheprovider_is_disabled(self):
        """.pytest_cache 在这台机器上写不进去；不禁用它就每次都刷警告，
        还会让人以为 `--lf` 的记账成功了。"""
        self.assertIn("no:cacheprovider", self.argv)

    def test_selection_travels_as_separate_args(self):
        """两个选择器必须是 argv 里两个独立元素——拼成一项就是走 shell 的味道。"""
        self.assertIn("tests/test_a.py", self.argv)
        self.assertIn("tests/test_b.py::T::t", self.argv)
        self.assertNotIn("tests/test_a.py tests/test_b.py::T::t", self.argv)
        self.assertFalse([a for a in self.argv if " " in a and "::" in a], self.argv)

    def test_keyword_and_basetemp_are_flagged(self):
        index = self.argv.index("-k")
        self.assertEqual(self.argv[index + 1], "slow")
        self.assertIn("--basetemp=/tmp/bt", self.argv)


class NodeidGuardTests(unittest.TestCase):
    def test_hostile_or_useless_nodeids_are_rejected(self):
        bad = ["", "tests/test_a.py", "--co tests", "tests/../x.py::t",
               str(Path(tempfile.mkdtemp()) / "x.py::t"), "tests/a.py::t\nrm -rf",
               "tests/a.py::t\x00"]
        for item in bad:
            with self.subTest(nodeid=item[:24]):
                self.assertFalse(tr.valid_nodeid(item))

    def test_normal_nodeids_pass(self):
        for item in ("tests/test_a.py::T::t", "tests/sub/test_b.py::t"):
            self.assertTrue(tr.valid_nodeid(item))


class BookkeepingTests(StateCase):
    def test_record_keeps_the_real_total_not_the_capped_list(self):
        """上限截的是【清单】，不能把"上次失败 205 条"这件事一起截掉。"""
        rows = [{"nodeid": "tests/test_a.py::T::t%s" % i} for i in range(tr.MAX_NODEIDS + 5)]
        tr.record_failures(self.project, rows, paths=[], keyword="")
        record = tr.load_record(self.project)
        self.assertEqual(record["total"], tr.MAX_NODEIDS + 5)
        self.assertEqual(len(record["nodeids"]), tr.MAX_NODEIDS)
    def test_failures_round_trip_and_hostile_rows_are_dropped(self):
        rows = [{"nodeid": "tests/test_a.py::T::ok1"}, {"nodeid": "--help"},
                {"nodeid": "tests/test_a.py::T::ok2"}]
        kept = tr.record_failures(self.project, rows, paths=[], keyword="")
        self.assertTrue(kept["ok"])
        self.assertEqual(kept["recorded"], 2, "非法 nodeid 不该进记账")
        self.assertEqual(tr.load_failures(self.project),
                         ["tests/test_a.py::T::ok1", "tests/test_a.py::T::ok2"])

    def test_record_reports_truncation_instead_of_silently_dropping(self):
        rows = [{"nodeid": "tests/test_a.py::T::t%s" % i} for i in range(tr.MAX_NODEIDS + 5)]
        kept = tr.record_failures(self.project, rows, paths=[], keyword="")
        self.assertTrue(kept["truncated"])
        self.assertEqual(kept["recorded"], tr.MAX_NODEIDS)

    def test_missing_record_is_not_read_as_zero_failures(self):
        self.assertEqual(tr.load_failures(Path(self.project) / "nope"), [])


class RunTests(StateCase):
    def test_a_red_run_is_structured_and_recorded(self):
        runner = FakeRunner(REAL_SUMMARY, exit_code=1)
        result = tr.run(self.project, paths="tests/test_a.py", runner=runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["counts"]["failed"], 2)
        self.assertFalse(result["checks"]["no_failures"])
        self.assertTrue(result["checks"]["counts_agree_with_exit"])
        self.assertEqual(result["recorded"]["recorded"], 3)
        self.assertEqual(len(tr.load_failures(self.project)), 3)

    def test_a_green_run_reports_green_and_uses_the_project_basetemp(self):
        runner = FakeRunner(GOOD_SUMMARY, exit_code=0)
        result = tr.run(self.project, paths="tests/test_a.py", runner=runner)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["passed"])
        basetemp = [a for a in result["command"] if a.startswith("--basetemp=")][0]
        self.assertTrue(basetemp[len("--basetemp="):].startswith(str(self.project).replace("\\", "/"))
                        or basetemp[len("--basetemp="):].startswith(str(self.project)),
                        "basetemp 必须落在项目内（用户名带引号时默认目录会 PermissionError）")

    def test_zero_collected_is_never_a_pass(self):
        """这是本工具存在的理由：exit=5 / "no tests ran" 不能读成"0 个失败所以通过"。"""
        for output, code in (("no tests ran in 0.01s\n", 5), ("", 5),
                             ("............\n", 0)):
            with self.subTest(code=code):
                result = tr.run(self.project, paths="tests/missing.py",
                                runner=FakeRunner(output, exit_code=code))
                self.assertFalse(result["ok"])
                self.assertFalse(result["checks"]["tests_collected"])
                self.assertIn("没收集到", result["error"])

    def test_unparsable_output_is_not_a_pass(self):
        result = tr.run(self.project, runner=FakeRunner("some random stdout\n", exit_code=0))
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["summary_parsed"])

    def test_counts_and_exit_code_must_agree(self):
        """摘要说 3 passed 却退出码 1：说明输出被截断/污染，报告不可信。"""
        result = tr.run(self.project, runner=FakeRunner(GOOD_SUMMARY, exit_code=1))
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["counts_agree_with_exit"])
        self.assertIn("不自洽", result["error"])

    def test_unknown_exit_code_is_not_a_pass(self):
        result = tr.run(self.project, runner=FakeRunner(GOOD_SUMMARY, exit_code=42))
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["runner_ran"])

    def test_timeout_says_it_did_not_finish(self):
        result = tr.run(self.project, timeout=60, runner=FakeRunner("...", timed_out=True))
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["finished"])
        self.assertIn("没跑完", result["error"])

    def test_each_zero_collected_signal_is_load_bearing_on_its_own(self):
        """三个信号必须各自都能单独判红。

        只测"三个都不对"的输出（exit=5 + no tests ran + 零计数），那么把其中任何一个
        条件删掉，剩下的两个还会兜住——守卫看起来在守，其实没有牙（实测就是这样：
        变异电池里这两条活了下来）。这里每个用例都让另外两个信号说"绿"。
        """
        only_exit_code = tr.parse_output("1 passed in 0.01s\n", 5)
        self.assertTrue(only_exit_code["collected"], "前提：计数说它跑到了")
        self.assertFalse(only_exit_code["no_tests_ran"], "前提：文本没说 no tests ran")
        only_text = tr.parse_output("1 passed in 0.01s\nno tests ran in 0.01s\n", 0)
        self.assertTrue(only_text["collected"])
        self.assertNotEqual(only_text["exit_code"], 5)
        for label, step in (("exit=5", {"output": "1 passed in 0.01s\n", "exit_code": 5}),
                            ("no tests ran", {"output": "1 passed in 0.01s\n"
                                              "no tests ran in 0.01s\n", "exit_code": 0}),
                            ("零计数", {"output": "...........\n", "exit_code": 0})):
            with self.subTest(signal=label):
                result = tr.run(self.project, runner=FakeRunner(sequence=[step]))
                self.assertFalse(result["ok"], "%s 这一路竟然判成通过" % label)
                self.assertFalse(result["checks"]["tests_collected"], "%s 没被识破" % label)

    def test_only_failed_reuses_the_record_and_names_no_scope(self):
        tr.record_failures(self.project, [{"nodeid": "tests/test_a.py::T::t1"}],
                           paths=[], keyword="")
        runner = FakeRunner(GOOD_SUMMARY, exit_code=0)
        result = tr.run(self.project, only_failed=True, runner=runner)
        self.assertIn("tests/test_a.py::T::t1", result["command"])
        self.assertEqual(result["scope"]["mode"], "only_failed")
        self.assertTrue(result["ok"])

    def test_only_failed_without_a_record_is_refused(self):
        runner = FakeRunner(GOOD_SUMMARY, exit_code=0)
        result = tr.run(self.project, only_failed=True, runner=runner)
        self.assertFalse(result["ok"])
        self.assertIn("记账", result["error"])
        self.assertEqual(runner.calls, [], "没记账就不该真的跑一次")

    def test_a_flood_of_failures_is_not_replayed_one_by_one(self):
        rows = [{"nodeid": "tests/test_a.py::T::t%s" % i} for i in range(tr.MAX_NODEIDS + 1)]
        tr.record_failures(self.project, rows, paths=[], keyword="")
        runner = FakeRunner(GOOD_SUMMARY, exit_code=0)
        result = tr.run(self.project, only_failed=True, runner=runner)
        self.assertFalse(result["ok"])
        self.assertIn("整片红", result["error"])
        self.assertEqual(runner.calls, [])

    def test_paths_that_look_like_options_or_escapes_are_refused(self):
        for bad in ("--collect-only", "../outside/tests", str(Path.cwd())):
            with self.subTest(paths=bad):
                runner = FakeRunner(GOOD_SUMMARY, exit_code=0)
                result = tr.run(self.project, paths=bad, runner=runner)
                self.assertFalse(result["ok"])
                self.assertEqual(runner.calls, [], "校验前不许起进程")

    def test_coverage_is_reported_as_missing_not_zero_percent(self):
        """本仓没装 coverage/pytest-cov：要明说【没测】，不能静默跳过也不能报 0%。"""
        if tr.coverage_available():
            self.skipTest("这台机器装了覆盖率工具，换不到「没装」这条路")
        runner = FakeRunner(GOOD_SUMMARY, exit_code=0)
        result = tr.run(self.project, coverage=True, runner=runner)
        self.assertFalse(result["ok"])
        self.assertIn("coverage", result["error"])
        self.assertEqual(runner.calls, [], "缺依赖时连跑都不该跑")


class RelatedTests(StateCase):
    def setUp(self):
        super().setUp()
        tests = Path(self.project) / "tests"
        tests.mkdir(parents=True, exist_ok=True)
        for name in ("test_head_check.py", "test_agent_capacity.py", "helpers.py"):
            tests.joinpath(name).write_text("def test_x():\n    assert True\n",
                                            encoding="utf-8", newline="\n")
        nested = tests / "sub"
        nested.mkdir(exist_ok=True)
        nested.joinpath("test_login_edge_cases.py").write_text("x = 1\n", encoding="utf-8",
                                                               newline="\n")
        junk = tests / "__pycache__"
        junk.mkdir(exist_ok=True)
        junk.joinpath("test_stale.py").write_text("x = 1\n", encoding="utf-8", newline="\n")

    def test_the_pool_skips_caches_and_non_test_files(self):
        pool = tr.test_files(self.project)
        self.assertIn("tests/test_head_check.py", pool)
        self.assertIn("tests/sub/test_login_edge_cases.py", pool)
        self.assertNotIn("tests/helpers.py", pool)
        self.assertFalse([row for row in pool if "__pycache__" in row], pool)

    def test_maps_a_source_file_to_test_files_that_actually_exist(self):
        got = tr.related_files(self.project, ["agent_runtime/head_check.py"])
        self.assertEqual(got["picked"], ["tests/test_head_check.py"])
        self.assertEqual(got["missed"], [])

    def test_names_files_with_no_related_test_instead_of_running_everything(self):
        got = tr.related_files(self.project, ["agent_runtime/nonexistent_thing.py"])
        self.assertEqual(got["picked"], [])
        self.assertIn("nonexistent_thing", got["missed"])

    def test_a_wide_change_is_capped_not_spilled_into_one_command(self):
        names = ["agent_runtime/m_%d.py" % i for i in range(tr.MAX_RELATED_FILES + 5)]
        for name in names:
            Path(self.project, "tests").joinpath("test_%s.py" % Path(name).stem).write_text(
                "def test_x():\n    assert True\n", encoding="utf-8", newline="\n")
        got = tr.related_files(self.project, names)
        self.assertEqual(len(got["picked"]), tr.MAX_RELATED_FILES)
        self.assertTrue(got["truncated"], "截了却不说，就等于偷偷改了范围")

    def test_related_run_passes_the_picked_files_and_counts_one_call(self):
        runner = FakeRunner(GOOD_SUMMARY, exit_code=0)
        result = tr.run(self.project, related="agent_runtime/head_check.py", runner=runner)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len(runner.calls), 1, "按文件名选就不必先跑一次 --collect-only")
        self.assertIn("tests/test_head_check.py", result["command"])
        self.assertEqual(result["scope"]["mode"], "related")

    def test_related_with_nothing_picked_is_red_and_never_runs(self):
        runner = FakeRunner(GOOD_SUMMARY, exit_code=0)
        result = tr.run(self.project, related="nothing_matches_here.py", runner=runner)
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["related_found"])
        self.assertEqual(runner.calls, [], "选不出测试就一次都不许跑")


class LocationTests(unittest.TestCase):
    def test_mismatched_traceback_lines_are_dropped_not_zipped(self):
        """`--tb=line` 的行数与摘要里的 FAILED 行数不等时（subtests、被截断的输出）
        强行按顺序配对，就会把 A 的错误消息挂到 B 的 nodeid 上。"""
        text = ('FAILED tests/test_a.py::T::one\nFAILED tests/test_a.py::T::two\n'
                'D:/x/tests/test_a.py:9: AssertionError: 只有一条现场\n'
                '1 failed, 2 passed in 0.1s\n')
        rows = tr.parse_output(text, 1)["failures"]
        self.assertEqual(len(rows), 2)
        self.assertFalse([row for row in rows if row.get("location")],
                         "数量对不上还硬配对：%s" % rows)

    def test_matching_traceback_lines_do_attach(self):
        text = ('FAILED tests/test_a.py::T::one\nFAILED tests/test_a.py::T::two\n'
                'D:/x/tests/test_a.py:9: AssertionError: 一\n'
                'D:/x/tests/test_a.py:14: IndexError: 二\n'
                '2 failed in 0.1s\n')
        rows = tr.parse_output(text, 1)["failures"]
        self.assertEqual(rows[0]["location"], "D:/x/tests/test_a.py:9")
        # 摘要行没带原因时，用 `--tb=line` 那一行的完整消息补上
        self.assertEqual(rows[1]["reason"], "IndexError: 二")

    def test_out_of_project_frames_are_not_passed_off_as_the_scene(self):
        row = {"nodeid": "tests/test_a.py::T::t", "location": "C:/py/unittest/case.py:900"}
        kept = tr.keep_project_location(row, "D:/WorkBuddy/rag-agent")
        self.assertEqual(kept["location"], "tests/test_a.py")
        self.assertEqual(kept["external_frame"], "C:/py/unittest/case.py:900")

    def test_in_project_locations_are_kept(self):
        row = {"nodeid": "tests/test_a.py::T::t",
               "location": "D:/WorkBuddy/rag-agent/tests/test_a.py:12"}
        kept = tr.keep_project_location(row, "D:/WorkBuddy/rag-agent")
        self.assertEqual(kept["location"], "D:/WorkBuddy/rag-agent/tests/test_a.py:12")
        self.assertNotIn("external_frame", kept)


class RenderTests(StateCase):
    def test_render_lists_the_failures_and_the_rerun_hint(self):
        result = tr.run(self.project, runner=FakeRunner(REAL_SUMMARY, exit_code=1))
        text = tr.render(result)
        self.assertIn("测试【未通过】", text)
        self.assertIn("FAILED tests/test_a.py::T::test_one", text)
        self.assertIn("only_failed", text)
        self.assertIn("边界", text)

    def test_render_of_an_unstarted_run_has_a_reason(self):
        text = tr.render({"checks": {}, "error": "没记账"})
        self.assertIn("未能开始", text)


@unittest.skipUnless(shutil.which("python") or True, "pytest 现场")
class RealPytestTests(StateCase):
    """真跑一次 pytest：假 runner 能骗过解析，真现场骗不过。"""

    def setUp(self):
        super().setUp()
        tests = Path(self.project) / "tests"
        tests.mkdir()
        tests.joinpath("test_math.py").write_text(
            "def test_ok():\n    assert 1 + 1 == 2\n\n\n"
            "def test_bad():\n    assert 1 + 1 == 3, '算错了'\n", encoding="utf-8", newline="\n")
        Path(self.project).joinpath("pytest.ini").write_text("[pytest]\n", encoding="utf-8",
                                                             newline="\n")

    def test_a_real_red_run_then_a_real_rerun_of_only_that_test(self):
        first = tr.run(self.project, timeout=120)
        self.assertFalse(first["ok"], json.dumps(first.get("checks"), ensure_ascii=False))
        self.assertEqual(first["counts"]["failed"], 1)
        self.assertEqual(first["failures"][0]["nodeid"], "tests/test_math.py::test_bad")
        self.assertIn("算错了", first["failures"][0]["reason"])
        self.assertEqual(tr.load_failures(self.project), ["tests/test_math.py::test_bad"])
        # 修掉它，再只重跑记账里那一条
        Path(self.project, "tests", "test_math.py").write_text(
            "def test_ok():\n    assert 1 + 1 == 2\n\n\ndef test_bad():\n"
            "    assert 1 + 1 == 2\n", encoding="utf-8", newline="\n")
        again = tr.run(self.project, only_failed=True, timeout=120)
        self.assertTrue(again["ok"], json.dumps(again, ensure_ascii=False, default=str)[:600])
        self.assertEqual(again["counts"]["passed"], 1)
        self.assertEqual(again["scope"]["mode"], "only_failed")

    def test_a_real_scope_typo_is_caught_as_nothing_collected(self):
        result = tr.run(self.project, paths="tests/test_does_not_exist.py", timeout=120)
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["tests_collected"])

    def test_a_real_green_run_of_a_clean_file(self):
        Path(self.project, "tests", "test_math.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8", newline="\n")
        result = tr.run(self.project, paths="tests/test_math.py", timeout=120)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["counts"]["passed"], 1)


class ToolSurfaceTests(unittest.TestCase):
    def setUp(self):
        self._prev = config.get_runtime("code_root")
        self.project = tempfile.mkdtemp(prefix="docmind_testrun_tool_")
        config.set_runtime("code_root", self.project)
        self.addCleanup(config.set_runtime, "code_root", self._prev)
        self.addCleanup(shutil.rmtree, self.project, ignore_errors=True)

    def _capture(self, arg):
        """让工具跑完整解析，但把真正的 pytest 换成记录器。"""
        import tools
        from agent_runtime import test_run
        seen = {}

        def fake(root, **kwargs):
            seen.update(kwargs)
            seen["_root"] = root
            return {"ok": True, "passed": True, "checks": {"x": True}, "counts": {"passed": 1},
                    "failures": [], "failure_total": 0, "scope": {}, "recorded": {},
                    "related": {}, "error": "", "exit_code": 0, "command": []}

        original = test_run.run
        test_run.run = fake
        try:
            text = tools.TOOLS["dev_testrun"]["func"](arg)
        finally:
            test_run.run = original
        return seen, text

    def test_every_documented_key_reaches_the_module(self):
        """`_parse_keyed` 少列一个 key，参数就被静默丢掉。

        实测代价：`coverage` 一开始没进 keys 列表，于是 `coverage: true` 变成了
        "跑一次全量 pytest"，300 秒后超时——工具不但没说缺依赖，还把整仓跑了一遍。
        """
        seen, text = self._capture("paths: tests/a.py\nkeyword: slow\nonly_failed: true\n"
                                   "related: agent_runtime/x.py\ntimeout: 42")
        self.assertEqual(seen["paths"], "tests/a.py")
        self.assertEqual(seen["keyword"], "slow")
        self.assertIs(seen["only_failed"], True)
        self.assertEqual(seen["related"], "agent_runtime/x.py")
        self.assertEqual(seen["timeout"], 42.0)
        self.assertIn("测试【通过】", text)

    def test_coverage_flag_is_not_silently_dropped(self):
        seen, _ = self._capture("coverage: true")
        self.assertIs(seen["coverage"], True, "coverage 没传到模块：会被当成普通全量跑")

    def test_registered_in_all_three_places(self):
        import agent
        import tools
        self.assertIn("dev_testrun", tools.TOOLS)
        self.assertIn("only_failed", tools.TOOLS["dev_testrun"]["description"])
        self.assertIn("timeout", tools.dev_testrun.__doc__)
        line = next(l for l in agent._SYSTEM_PROMPT_FULL.splitlines()
                    if l.startswith("- dev_testrun"))
        self.assertIn("5", line, "目录里必须写清「没收集到」这一手")
        self.assertEqual(len(tools.tool_schemas()), len(tools.TOOLS))

    def test_mcp_exposure_is_reviewed(self):
        from agent_runtime import mcp_server
        self.assertIn("dev_testrun", mcp_server.WRITE_TOOLS,
                      "会跑项目代码的工具不能进默认只读表")

    def test_timeout_out_of_range_is_refused_before_any_process_starts(self):
        import tools
        for arg in ("timeout: 3", "timeout: 99999", "timeout: abc"):
            with self.subTest(arg=arg):
                text = tools.TOOLS["dev_testrun"]["func"](arg)
                self.assertTrue(text.startswith("timeout"), text[:80])


if __name__ == "__main__":
    unittest.main()
