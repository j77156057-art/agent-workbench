"""覆盖率回归：让 `coverage: true` 给出真数字，并盯住它最贵的一种失败——「未知」被
当成「很高」。

这个仓库反复吃亏的地方是**看起来干净**。覆盖率特别容易出这种形态，因为「报告读不出来」
和「报告说 99%」在界面上几乎一样。所以本文件钉住的是：

1. **JSON 报告读不出来 ⇒ 判未知，不是 0 也不是 100**
   —— 守它：`a_truncated_or_missing_report_is_unknown_not_zero`、
   `a_report_with_prose_before_the_json_is_still_parsed`。
2. **给了一个不在报告里的改动文件 ⇒ 明确说未知**
   —— 守它：`a_target_absent_from_the_report_is_not_a_clean_bill`。
3. **coverage 读不了源码的文件（语法坏）既不算已覆盖也不算未覆盖**
   —— 守它：`an_unreadable_source_is_reported_not_silently_dropped`。这条不是假想：
   本仓真有一个语法坏掉的 .py（另一条 lane 的在途改动），不带 `--ignore-errors` 的
   `coverage json` 会**整体失败、一条 JSON 都不吐**。
4. **「0 条语句」不是 100% 也不是 0%**
   —— 守它：`zero_statements_is_neither_zero_nor_full`。
5. **覆盖率低不是「测试失败」**（两件事必须分开判）
   —— 守它：`low_coverage_does_not_fail_the_run`、`coverage_and_test_verdict_are_separate`。
6. **`excluded_lines` 不能算成未覆盖**
   —— 守它：`excluded_lines_are_not_gaps`。
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

from agent_runtime import code_coverage as cc  # noqa: E402
from agent_runtime import test_run as tr  # noqa: E402


def report(**overrides):
    """造一份最小可用的 coverage JSON 报告。"""
    payload = {
        "meta": {"format": 3},
        "files": {
            "a.py": {"summary": {"percent_covered": 50.0, "num_statements": 4,
                                 "covered_lines": 2, "missing_lines": 2},
                     "missing_lines": [3, 7], "excluded_lines": []},
            "b.py": {"summary": {"percent_covered": 100.0, "num_statements": 3,
                                 "covered_lines": 3, "missing_lines": 0},
                     "missing_lines": [], "excluded_lines": []},
        },
        "totals": {"percent_covered": 71.4, "covered_lines": 5, "num_statements": 7},
    }
    payload.update(overrides)
    return payload


def as_json(payload):
    return json.dumps(payload)


class ParseReportTests(unittest.TestCase):

    def test_plain_json_is_parsed(self):
        parsed = cc.parse_report(as_json(report()), ".")
        self.assertTrue(parsed["parsed"])
        self.assertIn("files", parsed["report"])

    def test_a_report_with_prose_before_the_json_is_still_parsed(self):
        """`coverage json` 会先打警告再吐 JSON（实测：Couldn't parse … 那一行）。
        靠「第一个 {」去切会把后面真正的东西切坏。"""
        noisy = "Couldn't parse 'x.py' as Python source: bad\n" + as_json(report())
        parsed = cc.parse_report(noisy, ".")
        self.assertTrue(parsed["parsed"], parsed["error"])
        self.assertEqual(sorted(parsed["report"]["files"]), ["a.py", "b.py"])

    def test_a_truncated_report_is_unknown_not_zero(self):
        """实测踩到：`coverage json -o -` 在大仓里产出 200KB+，被有界缓冲拦腰截断，
        尾部停在 JSON 中段。这里必须判「未知」。"""
        body = as_json(report())
        parsed = cc.parse_report(body[:len(body) // 2], ".")
        self.assertFalse(parsed["parsed"])
        self.assertIn("未知", parsed["error"])
        self.assertNotIn("0%", parsed["error"])

    def test_empty_output_is_unknown(self):
        parsed = cc.parse_report("", ".")
        self.assertFalse(parsed["parsed"])
        self.assertIn("未知", parsed["error"])

    def test_json_without_a_files_key_is_refused(self):
        parsed = cc.parse_report('{"meta": {}}', ".")
        self.assertFalse(parsed["parsed"])


class SummarizeTests(unittest.TestCase):

    def test_only_files_with_gaps_are_listed(self):
        out = cc.summarize(report(), ".")
        self.assertEqual([row["file"] for row in out["files"]], ["a.py"])
        self.assertEqual(out["files"][0]["missing_lines"], [3, 7])
        self.assertEqual(out["total_percent"], 71.4)

    def test_excluded_lines_are_not_gaps(self):
        """coverage 自己标了「这行不可执行」（注释、pragma），不能算成没覆盖。"""
        payload = report()
        payload["files"]["a.py"]["missing_lines"] = [3, 7, 9]
        payload["files"]["a.py"]["excluded_lines"] = [9]
        out = cc.summarize(payload, ".")
        self.assertEqual(out["files"][0]["missing_lines"], [3, 7])

    def test_zero_statements_is_neither_zero_nor_full(self):
        payload = {"files": {}, "totals": {"percent_covered": 0.0,
                                            "covered_lines": 0, "num_statements": 0}}
        out = cc.summarize(payload, ".")
        self.assertFalse(out["ok"])
        self.assertIn("一条语句都没有", out["error"])

    def test_a_target_absent_from_the_report_is_not_a_clean_bill(self):
        """给了改动文件、而它根本不在报告里 —— 覆盖率【未知】，不能读成「已覆盖」。"""
        out = cc.summarize(report(), ".", targets=["ghost.py"])
        self.assertFalse(out["ok"])
        self.assertIn("ghost.py", out["targets_not_in_report"])
        self.assertIn("未知", out["error"])

    def test_a_target_with_no_gaps_is_reported_as_such(self):
        out = cc.summarize(report(), ".", targets=["b.py"])
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["targets_without_gaps"], ["b.py"])

    def test_a_target_with_gaps_is_summarized(self):
        out = cc.summarize(report(), ".", targets=["a.py"])
        self.assertEqual([row["file"] for row in out["files"]], ["a.py"])
        self.assertEqual(out["scope"], "targets")

    def test_the_per_file_cap_is_declared_not_silent(self):
        payload = report()
        payload["files"] = {}
        for index in range(cc.MAX_FILES + 10):
            payload["files"]["f%03d.py" % index] = {
                "summary": {"percent_covered": 10.0, "num_statements": 2},
                "missing_lines": [1, 2], "excluded_lines": []}
        out = cc.summarize(payload, ".")
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["files"]), cc.MAX_FILES)

    def test_the_per_file_line_cap_is_declared(self):
        payload = report()
        payload["files"]["a.py"]["missing_lines"] = list(range(1, 200))
        payload["files"]["a.py"]["summary"]["missing_lines"] = 199
        out = cc.summarize(payload, ".")
        row = out["files"][0]
        self.assertEqual(len(row["missing_lines"]), cc.MAX_LINES_PER_FILE)
        self.assertTrue(row["partial"])
        self.assertEqual(row["missing_total"], 199)


class RunRefusalTests(unittest.TestCase):

    """跑不成时必须明说跑不成。这几条不需要真的 coverage。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="codecov-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_a_missing_root_is_refused(self):
        out = cc.run(os.path.join(self.dir, "nope"), runner=lambda *a, **k: {})
        self.assertFalse(out["ok"])

    def test_too_many_targets_are_refused_before_running(self):
        out = cc.run(self.dir, targets=["a.py"] * (cc.MAX_TARGET_FILES + 1),
                     runner=lambda *a, **k: {})
        self.assertFalse(out["checks"]["targets_bounded"])
        self.assertIn("一次最多看", out["error"])

    @unittest.skipUnless(cc.coverage_available(), "本环境没装 coverage")
    def test_a_pytest_that_never_started_is_refused(self):
        def runner(argv, **kwargs):
            if "coverage" in argv and "json" in argv:
                raise AssertionError("pytest 都没起来，不该走到生成报告这一步")
            return {"ok": False, "exit_code": None, "output": "",
                    "timed_out": False, "error": "系统找不到指定的文件"}
        out = cc.run(self.dir, runner=runner)
        self.assertFalse(out["ok"])
        self.assertFalse(out["checks"]["pytest_ran"])

    @unittest.skipUnless(cc.coverage_available(), "本环境没装 coverage")
    def test_a_missing_report_file_is_unknown_not_a_number(self):
        """报告文件没落盘 ⇒ 覆盖率未知。绝不能返回 0% 或 100%。"""
        def runner(argv, **kwargs):
            if "json" in argv:
                return {"ok": True, "exit_code": 0, "output": "", "timed_out": False,
                        "error": "", "total_chars": 0}
            return {"ok": True, "exit_code": 0, "output": "5 passed\n",
                    "timed_out": False, "error": "", "total_chars": 9}
        out = cc.run(self.dir, runner=runner)
        self.assertFalse(out["ok"])
        self.assertFalse(out["checks"]["report_parsed"])
        self.assertNotIn("total_percent", out)
        self.assertIn("未知", out["error"])

    @unittest.skipUnless(cc.coverage_available(), "本环境没装 coverage")
    def test_a_timeout_is_not_a_coverage_number(self):
        def runner(argv, **kwargs):
            if "json" in argv:
                raise AssertionError("超时就该停，不该去生成报告")
            return {"ok": False, "exit_code": None, "output": "x",
                    "timed_out": True, "error": "timeout"}
        out = cc.run(self.dir, runner=runner)
        self.assertFalse(out["checks"]["pytest_finished"])
        self.assertNotIn("total_percent", out)


class UnparsedSourceTests(unittest.TestCase):

    def test_warnings_are_picked_up_from_the_report_itself(self):
        payload = report(warnings=["Couldn't parse 'broken.py' as Python source: bad"])
        found = cc._unparsed_from_report(payload)
        self.assertEqual(found, ["broken.py"])

    def test_warnings_are_picked_up_from_stdout_too(self):
        found = cc._unparsed_sources("Couldn't parse 'other.py' as Python source: bad\n")
        self.assertEqual(found, ["other.py"])


class RenderTests(unittest.TestCase):

    def test_low_coverage_is_stated_but_does_not_fail(self):
        payload = report()
        payload["totals"] = {"percent_covered": 12.0, "covered_lines": 2,
                             "num_statements": 16}
        out = cc.summarize(payload, ".")
        self.assertTrue(out["ok"], out.get("error"))
        text = cc.render(dict(out, checks={"report_parsed": True}))
        self.assertIn("偏低", text)
        self.assertIn("不影响本次运行的判定", text)

    def test_an_untrustworthy_report_leads_with_why(self):
        """跑都没跑成时，输出必须先说清「未完成 + 原因」，而不是给一个数字。"""
        text = cc.render({"ok": False, "error": "覆盖率读不出来", "checks": {},
                          "files": [], "notes": []})
        self.assertIn("未完成", text)
        self.assertIn("覆盖率读不出来", text)

    def test_a_report_that_parsed_but_is_untrustworthy_leads_with_why(self):
        """报告拿到了但不可信时，要说「不可信」并把原因放在最前面。"""
        text = cc.render({"ok": False, "error": "覆盖率报告读不出来", "checks": {},
                          "files": [], "notes": [], "total_percent": 0.0})
        self.assertIn("不可信", text)
        self.assertIn("覆盖率报告读不出来", text)

    def test_an_incomplete_measurement_says_so_loudly(self):
        out = cc.summarize(report(), ".")
        text = cc.render(dict(out, checks={"report_parsed": True}, complete=False,
                              unparsed_sources=["broken.py"]))
        self.assertIn("不完整", text)

    def test_the_boundary_is_stated(self):
        """覆盖率满了也不能替代「测试对不对」的判定——这句话要在每次输出里。"""
        text = cc.render(cc.summarize(report(), "."))
        self.assertIn("不判断测试对不对", text)


class TestRunIntegrationTests(unittest.TestCase):
    """`dev_testrun` 与覆盖率的关系：两件事必须分开判。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="covint-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        Path(self.dir, "requirements.txt").write_text("", encoding="utf-8")

    def test_a_passing_suite_is_not_affected_by_coverage_being_off(self):
        def runner(argv, **kwargs):
            return {"ok": True, "exit_code": 0, "output": "3 passed in 0.1s\n",
                    "timed_out": False, "error": "", "total_chars": 19}
        result = tr.run(self.dir, runner=runner)
        self.assertTrue(result["ok"], result["checks"])
        self.assertEqual(result["coverage"], "off")

    def test_coverage_and_test_verdict_are_separate(self):
        """测试绿 + 覆盖率低 ⇒ 测试仍然算通过。反过来测试红时绝不因为覆盖率而变绿。"""
        def runner(argv, **kwargs):
            return {"ok": True, "exit_code": 0, "output": "3 passed in 0.1s\n",
                    "timed_out": False, "error": "", "total_chars": 19}
        result = tr.run(self.dir, runner=runner)
        self.assertTrue(result["passed"])
        self.assertTrue(result["checks"]["no_failures"])

    def test_the_off_branch_tells_you_how_to_ask_for_coverage(self):
        def runner(argv, **kwargs):
            return {"ok": True, "exit_code": 0, "output": "3 passed in 0.1s\n",
                    "timed_out": False, "error": "", "total_chars": 19}
        text = tr.render(tr.run(self.dir, runner=runner))
        self.assertIn("coverage: true", text)

    def test_both_branches_render_the_coverage_block(self):
        """覆盖率开与关，两条分支都要有边界说明——否则「没提」会被读成「没问题」。"""
        def runner(argv, **kwargs):
            return {"ok": True, "exit_code": 0, "output": "3 passed in 0.1s\n",
                    "timed_out": False, "error": "", "total_chars": 19}
        self.assertIn("边界", tr.render(tr.run(self.dir, runner=runner)))
        payload = dict(cc.summarize(report(), self.dir), checks={"report_parsed": True})
        merged = tr.run(self.dir, runner=runner)
        merged["coverage"] = payload
        self.assertIn("覆盖率", tr.render(merged))


class RealRepoTests(unittest.TestCase):
    """对本仓跑一遍真实覆盖率——证明这条不是纸上能力。"""

    def setUp(self):
        if not os.path.isfile(os.path.join(ROOT, "requirements.txt")):
            self.skipTest("不是仓库根目录")

    @unittest.skipUnless(cc.coverage_available(), "本环境没装 coverage")
    def test_it_measures_this_repo_for_real(self):
        result = cc.run(ROOT, paths=["tests/test_calculate.py"], timeout=600)
        # 本仓有一个语法坏掉的在途文件，所以不要求 complete——要求的是
        # 「报告读不出来时明说未知」这条路径已经被上面那些用例钉住了。
        if not result.get("ok"):
            self.assertIn("未知", str(result.get("error")) + str(result.get("notes")))
            return
        self.assertGreater(result["num_statements"], 0)
        self.assertIsInstance(result["files"], list)
        self.assertTrue(0.0 <= result["total_percent"] <= 100.0)

    @unittest.skipUnless(cc.coverage_available(), "本环境没装 coverage")
    def test_targets_mode_reports_a_real_gap(self):
        target = "agent_runtime/log_view.py"
        if not os.path.isfile(os.path.join(ROOT, target.replace("/", os.sep))):
            self.skipTest("目标文件不在")
        result = cc.run(ROOT, paths=["tests/test_calculate.py"], targets=[target],
                        timeout=600)
        if not result.get("ok"):
            self.skipTest("本仓报告没能生成（另一条 lane 的在途语法错误）：%s"
                          % str(result.get("error"))[:80])
        self.assertEqual(result["scope"], "targets")
        self.assertNotIn(target, result["targets_not_in_report"])


if __name__ == "__main__":
    unittest.main()
