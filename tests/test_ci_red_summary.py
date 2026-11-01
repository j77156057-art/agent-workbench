# -*- coding: utf-8 -*-
"""`tools/ci_red_summary.py`：把测试日志压成红名单 + GitHub 注释。

这层存在的唯一理由：CI 的原始日志下载地址（objects.githubusercontent.com）
在本机不可达，而 check-run 注释走 api.github.com。所以解析必须**确定**——
漏一条等于把一条真回归藏起来，多一条等于把别人的红记到自己账上。
"""
import importlib.util
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `tools/` 目录与仓库根的 `tools.py` 重名，`import tools.xxx` 会解析到那个模块，
# 所以按路径加载这一个脚本。
_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "ci_red_summary.py")
_spec = importlib.util.spec_from_file_location("ci_red_summary_under_test", _PATH)
summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summary)

UNITTEST_LOG = """\
....F...
======================================================================
FAIL: test_engine_single_instance (test_engine_single_instance.EngineTests.test_engine_single_instance)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/repo/tests/test_engine_single_instance.py", line 42, in test_engine_single_instance
    self.assertEqual(seen, 1)
AssertionError: 2 != 1

======================================================================
ERROR: setUpClass (test_voice.VoiceConfigApiTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/repo/tests/test_voice.py", line 88, in setUpClass
    raise RuntimeError("缺少密钥")
RuntimeError: 缺少密钥

======================================================================
FAIL: test_stream (tests.test_workflow_chat_stream.WorkflowEventsSseTests.test_stream)
----------------------------------------------------------------------
AssertionError: b'' != b'data:'

----------------------------------------------------------------------
Ran 5236 tests in 1310.421s

FAILED (failures=2, errors=1)
"""

PYTEST_LOG = """\
.....................................
FAILED tests/test_live_vision.py::LiveVisionTests::test_origin - AssertionError: closed
========================= 1 failed, 1084 passed in 1200.00s (0:20:00) =========================
"""


class ParseTests(unittest.TestCase):
    def test_unittest_block_is_parsed_into_three_rows(self):
        report = summary.parse(UNITTEST_LOG)
        self.assertEqual([(row["path"], row["title"]) for row in report["reds"]], [
            ("tests/test_engine_single_instance.py", "EngineTests.test_engine_single_instance"),
            ("tests/test_voice.py", "VoiceConfigApiTests.setUpClass"),
            ("tests/test_workflow_chat_stream.py", "WorkflowEventsSseTests.test_stream"),
        ])
        self.assertEqual((report["ran"], report["failures"], report["errors"]), (5236, 2, 1))

    def test_hook_name_is_kept_in_the_title(self):
        """setUpClass 必须带进标题：不带的话同类报错会塌成同一个名字。"""
        report = summary.parse(UNITTEST_LOG)
        self.assertIn("VoiceConfigApiTests.setUpClass",
                      [row["title"] for row in report["reds"]])

    def test_reason_line_is_captured_for_each_red(self):
        """只有用例名没法归因 —— 带上那行异常文本。"""
        rows = {row["title"]: row for row in summary.parse(UNITTEST_LOG)["reds"]}
        self.assertIn("2 != 1", rows["EngineTests.test_engine_single_instance"]["detail"])
        self.assertIn("缺少密钥", rows["VoiceConfigApiTests.setUpClass"]["detail"])

    def test_pytest_short_summary_reason_is_kept(self):
        row = summary.parse(PYTEST_LOG)["reds"][0]
        self.assertIn("closed", row["detail"])

    def test_pytest_summary_line_is_parsed(self):
        report = summary.parse(PYTEST_LOG)
        self.assertEqual(len(report["reds"]), 1)
        self.assertEqual(report["reds"][0]["path"], "tests/test_live_vision.py")
        self.assertEqual(report["reds"][0]["title"], "LiveVisionTests.test_origin")

    def test_duplicates_collapse_and_order_is_preserved(self):
        text = UNITTEST_LOG + "FAIL: test_stream (tests.test_workflow_chat_stream.WorkflowEventsSseTests.test_stream)\n"
        report = summary.parse(text)
        self.assertEqual(len(report["reds"]), 3)
        self.assertEqual(report["reds"][-1]["title"], "WorkflowEventsSseTests.test_stream")

    def test_clean_log_reports_nothing(self):
        report = summary.parse("Ran 12 tests in 0.1s\n\nOK\n")
        self.assertEqual(report["reds"], [])
        self.assertEqual(report["ran"], 12)

    def test_garbage_lines_do_not_crash(self):
        report = summary.parse("FAIL: \n::error::whatever\nFAILED ()\n")
        self.assertEqual(report["reds"], [])


class RenderTests(unittest.TestCase):
    def test_annotations_are_emitted_for_every_red(self):
        lines = summary.render(summary.parse(UNITTEST_LOG))
        annotations = [line for line in lines if line.startswith("::error ")]
        self.assertEqual(len(annotations), 3, annotations)
        self.assertTrue(any("file=tests/test_voice.py" in line for line in annotations))
        self.assertTrue(any("title=FAIL" in line for line in annotations))

    def test_annotation_payload_escapes_workflow_metacharacters(self):
        report = {"reds": [{"kind": "FAIL", "path": "tests/test_x.py",
                            "title": "C.test_50%_x", "source": "unittest"}],
                  "ran": 1, "failures": 1, "errors": 0, "summary_line": "",
                  "annotations_truncated": 0}
        line = [row for row in summary.render(report) if row.startswith("::error ")][0]
        self.assertIn("50%25", line)
        self.assertNotIn("50%_", line)

    def test_annotation_count_is_capped(self):
        reds = [{"kind": "FAIL", "path": "tests/test_x.py", "title": "t%d" % i,
                 "source": "unittest"} for i in range(summary.MAX_ANNOTATIONS + 7)]
        report = {"reds": reds, "ran": 0, "failures": 0, "errors": 0, "summary_line": "",
                  "annotations_truncated": 7}
        lines = summary.render(report)
        self.assertEqual(len([row for row in lines if row.startswith("::error ")]),
                         summary.MAX_ANNOTATIONS)
        self.assertTrue(any("其余见上面" in row for row in lines))

    def test_red_lines_are_present_for_humans(self):
        lines = summary.render(summary.parse(UNITTEST_LOG))
        self.assertEqual(len([row for row in lines if row.startswith("RED ")]), 3)

    def test_annotation_carries_the_reason(self):
        lines = summary.render(summary.parse(UNITTEST_LOG))
        self.assertTrue(any("::error " in row and "缺少密钥" in row for row in lines), lines)


class MainTests(unittest.TestCase):
    def _run(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        try:
            return summary.main([handle.name])
        finally:
            os.unlink(handle.name)

    def test_main_never_fails_the_step(self):
        self.assertEqual(self._run(UNITTEST_LOG), 0)
        self.assertEqual(self._run(""), 0)

    def test_missing_log_file_is_not_a_crash(self):
        self.assertEqual(summary.main(["no-such-file.log"]), 0)


class WorkflowWiringTests(unittest.TestCase):
    """CI 真的会打印红名单吗 —— 解析器全绿但接线掉了，等于没有。"""

    def _steps(self):
        import yaml
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".github", "workflows", "harness.yml"),
                  encoding="utf-8") as handle:
            doc = yaml.safe_load(handle)
        return {step.get("name"): step.get("run") or ""
                for step in doc["jobs"]["test"]["steps"]}

    def test_both_lanes_feed_the_summary_tool_and_keep_the_exit_code(self):
        steps = self._steps()
        for name in ("Run full test suite", "Run pytest-style lane"):
            run = steps.get(name, "")
            self.assertIn("tools/ci_red_summary.py", run, "%s 没接红名单" % name)
            self.assertIn("exit $rc", run,
                          "%s 报了红名单却把失败吞成绿色" % name)

    def test_tests_run_before_the_summary_is_rendered(self):
        run = self._steps().get("Run full test suite", "")
        self.assertLess(run.index("unittest discover"), run.index("ci_red_summary.py"),
                        "先解析后跑测试，红名单永远是空的")


if __name__ == "__main__":
    unittest.main()
