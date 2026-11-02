"""dev_eval 自身的回归：题库合法、门禁会咬人、跑完不污染运行时。

一个「永远绿」的评测门比没有门更糟——它会让人以为量过了。所以这里重点不是
「题目能过」，而是【题目坏掉时门禁必须变红】：伪造一次回退，必须被
compare_to_baseline 抓到；伪造一条坏题，validate_cases 必须拒绝。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import tools as tools_module  # noqa: E402
from agent_runtime import dev_eval  # noqa: E402


def _bad_case(**overrides):
    case = {"id": "synthetic", "fixture": "plain",
            "steps": [dev_eval._react("grep", "notes"), dev_eval._final("看过了")],
            "expect": {"called": ["grep"], "observation_contains": ["# notes"]}}
    case.update(overrides)
    return case


class DatasetShapeCases(unittest.TestCase):
    def test_dataset_is_well_formed(self):
        ids = [case["id"] for case in dev_eval.DEV_DATASET]
        self.assertEqual(len(ids), len(set(ids)), "题号不能重复")
        self.assertGreaterEqual(len(ids), 12, "dev lane 题量不该退化")
        for case in dev_eval.DEV_DATASET:
            self.assertTrue((case.get("expect") or case.get("direct")),
                            "%s 既没有 expect 也没有 direct" % case.get("id"))
            used = [str(row.get("tool") or "") for row in
                    list(case.get("steps") or []) if "tool" in row]
            used += [str(row.get("tool") or "") for row in case.get("verify") or []]
            for tool in used:
                self.assertIn(tool, tools_module.TOOLS, "%s 用了不存在的工具 %s" % (
                    case.get("id"), tool))

    def test_every_capability_wave_has_a_case(self):
        """这些工具一旦从题面里消失，就是评测在悄悄失去覆盖面。"""
        covered = set()
        for case in dev_eval.DEV_DATASET:
            for row in list(case.get("steps") or []) + list(case.get("verify") or []):
                covered.add(str(row.get("tool") or ""))
            # direct 题在函数里调工具，看不见就白名单声明（uses），否则覆盖率会假性缺项
            covered.update(str(name) for name in case.get("uses") or [])
        for tool in ("dev_glob", "dev_git_log", "dev_find_references", "dev_diagnostics",
                     "dev_patch", "dev_lanes", "dev_propose", "dev_ci_status"):
            self.assertIn(tool, covered, "%s 已经不在 dev lane 题库里了" % tool)

    def test_validate_cases_rejects_broken_datasets(self):
        problems = dev_eval.validate_cases([
            _bad_case(id=""),
            _bad_case(id="dup"), _bad_case(id="dup"),
            _bad_case(id="noexpect", expect={}),
            _bad_case(id="badtool", steps=[dev_eval._react("no_such_tool", "x"),
                                          dev_eval._final("done")]),
            _bad_case(id="escape", fixture="plain"),
        ], tools_module)
        text = "\n".join(problems)
        self.assertIn("缺少 id", text)
        self.assertIn("id 重复", text)
        self.assertIn("expect", text)
        self.assertIn("不存在的工具", text)

    def test_validate_cases_accepts_the_shipped_dataset(self):
        self.assertEqual(dev_eval.validate_cases(list(dev_eval.DEV_DATASET), tools_module), [])


class GateBitesCases(unittest.TestCase):
    def test_a_real_failure_is_reported(self):
        result = dev_eval.evaluate_case(_bad_case(
            expect={"called": ["grep"], "observation_contains": ["这句绝不会出现"]}))
        self.assertFalse(result["passed"])
        self.assertTrue([item for item in result["checks"] if not item["ok"]])

    def test_wrong_tool_is_caught_by_the_script_layer(self):
        """脚本轨迹里用【不该用的工具】：注册表/护栏问题必须在这层暴露。"""
        result = dev_eval.evaluate_case(_bad_case(
            steps=[dev_eval._react("dev_glob", "*.md"), dev_eval._final("看过了")],
            expect={"called": ["grep"]}))
        self.assertFalse(result["passed"])
        self.assertIn("called:grep", [item["name"] for item in result["checks"]])

    def test_compare_flags_a_regression(self):
        report = dev_eval.evaluate([_bad_case(id="stable")])
        self.assertTrue(report["ok"], report["results"])
        self.assertTrue(report["results"][0]["passed"], report["results"][0]["checks"])

        def baseline_of(passed: bool) -> str:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "total": 1, "scored": 1,
                           "passed": 1 if passed else 0,
                           "results": [{"id": "stable", "passed": passed, "skipped": ""}]},
                          handle)
            return handle.name

        # 基线说这题没过、现在过了 = improvement，不是回退
        down = baseline_of(False)
        # 基线说这题过了、现在没过 = 回退，门禁必须变红
        up = baseline_of(True)
        try:
            gate = dev_eval.compare_to_baseline(report, down)
            self.assertEqual(gate["improvements"], ["stable"])
            self.assertFalse(gate["regressed"])
            failing = {"results": [{"id": "stable", "passed": False, "skipped": ""}],
                       "scored": 1, "passed": 0, "failed": ["stable"]}
            flipped = dev_eval.compare_to_baseline(failing, up)
            self.assertEqual(flipped["regressions"], ["stable"])
            self.assertTrue(flipped["regressed"])
            self.assertFalse(flipped["ok"])
        finally:
            os.remove(down)
            os.remove(up)

    def test_new_case_is_not_a_regression(self):
        baseline = {"schema_version": 1, "total": 0, "scored": 0, "passed": 0, "results": []}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                        encoding="utf-8") as handle:
            json.dump(baseline, handle)
            path = handle.name
        try:
            gate = dev_eval.compare_to_baseline(
                {"results": [{"id": "brand-new", "passed": True, "skipped": ""}],
                 "scored": 1, "passed": 1}, path)
            self.assertEqual(gate["new_cases"], ["brand-new"])
            self.assertFalse(gate["regressed"])
        finally:
            os.remove(path)

    def test_cli_exit_codes(self):
        good = dev_eval.main(["--json"])
        self.assertEqual(good, 0)
        with mock.patch.object(dev_eval, "DEV_DATASET",
                               [_bad_case(id="broken",
                                         expect={"observation_contains": ["不可能出现的话"]})]):
            self.assertEqual(dev_eval.main(["--json"]), 1)

    def test_write_baseline_roundtrip(self):
        report = dev_eval.evaluate([_bad_case(id="roundtrip")])
        path = os.path.join(tempfile.mkdtemp(prefix="devbase_"), "baseline.json")
        try:
            dev_eval.write_baseline(report, path)
            with open(path, encoding="utf-8") as handle:
                saved = json.loads(handle.read())
            self.assertEqual(saved["results"][0]["id"], "roundtrip")
            self.assertTrue(saved["results"][0]["passed"])
            self.assertFalse(os.path.exists(path + ".tmp"))
        finally:
            os.remove(path)


class IsolationCases(unittest.TestCase):
    def test_runtime_is_restored_after_a_case(self):
        prev_root = tools_module.get_runtime("code_root")
        prev_state = config.STATE_ROOT
        dev_eval.evaluate_case(_bad_case(id="isolate"))
        self.assertEqual(tools_module.get_runtime("code_root"), prev_root)
        self.assertEqual(config.STATE_ROOT, prev_state)

    def test_fixture_writes_never_touch_the_real_repo(self):
        marker = os.path.join(ROOT, "a.txt")
        self.assertFalse(os.path.exists(marker))
        dev_eval.evaluate_case({"id": "leak-check", "fixture": "patch",
                                "steps": [dev_eval._final("跳过")], "expect": {},
                                "verify": [{"tool": "dev_patch", "input": "",
                                            "note": "no-op"}]})
        self.assertFalse(os.path.exists(marker))

    def test_git_cases_skip_instead_of_failing_when_git_is_missing(self):
        case = next(row for row in dev_eval.DEV_DATASET if row.get("fixture") == "git-history")
        with mock.patch.object(dev_eval, "_git_ready", return_value=False):
            result = dev_eval.evaluate_case(case)
        self.assertEqual(result["skipped"], "git 不可用")
        self.assertTrue(result["passed"])


class FullDatasetCases(unittest.TestCase):
    def test_whole_dataset_is_green(self):
        report = dev_eval.evaluate()
        failed = {row["id"]: [item for item in row["checks"] if not item["ok"]]
                  for row in report["results"] if not row["passed"]}
        self.assertTrue(report["ok"], "%s / %s" % (report["failed"], json.dumps(
            {key: [item["name"] + "→" + item["detail"][:120] for item in value]
             for key, value in failed.items()}, ensure_ascii=False)))
        self.assertEqual(report["failed"], [])
        self.assertGreaterEqual(report["scored"], 1)

    def test_shipped_baseline_matches_the_dataset(self):
        """基线文件要跟题集一起走：题加了/改了却没重刷基线，这里就该响。"""
        path = os.path.join(ROOT, ".github", "dev-eval-baseline.json")
        self.assertTrue(os.path.isfile(path), "缺少 .github/dev-eval-baseline.json")
        report = dev_eval.evaluate()
        gate = dev_eval.compare_to_baseline(report, path)
        self.assertEqual(gate["regressions"], [], gate)
        self.assertEqual(gate["new_cases"], [], "题集比基线多出题，请重刷基线")


if __name__ == "__main__":
    unittest.main()
