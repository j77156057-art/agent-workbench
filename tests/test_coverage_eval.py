"""改动行覆盖率门的守卫：判据、fail-closed、CI 接线，以及「这扇门能因覆盖率下降而红」。

门的来历（都是实测，不是理念）：
- 全仓百分比当门是无效的：同一份覆盖率报告里全仓 **15.4%**（19237/124712 条语句），
  而本次改动行覆盖率 **95.1%**（41 行改动、2 行未执行）。前者涨跌都回答不了
  「我改的那几行被走过吗」，后者能。
- 门槛取 90%（合计）而不是 100%：今天这批里未执行的两行是 `except HTTPError:` 的
  写法改动与一处校验器调用 —— 要求它们必须被执行，会把人逼去写只为覆盖而存在的用例，
  而那正好污染变异电池（它靠「改坏实现有测试变红」判有没有牙）。所以这类只出
  **warning**（照样以 `::warning` 注释露出来），不判红。
- 「改了一整块却一行都没被执行」（≥5 行）单独判红：合计规则会把这种形状藏起来。
"""
import json
import os
import pathlib
import subprocess
import sys
import unittest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime import coverage_eval as cov  # noqa: E402

CI_FILE = ROOT / ".github" / "workflows" / "harness.yml"


def _steps():
    doc = yaml.safe_load(CI_FILE.read_text(encoding="utf-8"))
    return [s for s in doc["jobs"]["test"]["steps"] if isinstance(s, dict)]


def _code(step) -> str:
    """步骤里的**命令**文本，剥掉 YAML run 里的 `#` 注释行。

    不剥就会被注释糊住：我给门写的解释注释里正好有「coverage run」这个词，
    真命令被变异摘掉时字符串断言仍然绿。
    """
    run = str(step.get("run") or "")
    return "\n".join(ln for ln in run.splitlines() if not ln.strip().startswith("#"))


class ClassifyTests(unittest.TestCase):
    def test_production_manual_and_ignored(self):
        self.assertEqual(cov.classify("agent_runtime/dev_server.py"), "production")
        self.assertEqual(cov.classify("tools.py"), "production")
        self.assertEqual(cov.classify("desktop.py"), "production")
        self.assertEqual(cov.classify("tests/test_self_verify.py"), "not_tracked")
        self.assertEqual(cov.classify("frontend/src/App.vue"), "not_tracked")
        self.assertEqual(cov.classify("verify_engine_embed.py"), "manual")

    def test_manual_list_is_pinned_with_a_reason(self):
        """往人工清单里加一项必须同时给理由，否则清单会变成「不想测就往里塞」。

        `run_golden.py` 在清单里是**结构性**原因：它导入期就写进程环境
        （`DOCMIND_TRACE=0` 等，必须早于 `import agent`），进程内导入会把
        `unittest discover` 那条 lane 的红从 30 放大到 146 ——
        见 `tests/test_eval_memory_isolation.py` 里 `test_run_golden_defaults_to_the_isolated_scope`
        的 docstring。所以它的行永远不可能被进程内用例覆盖。
        """
        self.assertEqual(set(cov.MANUAL_SCRIPTS),
                         {"verify_", "tools/mutation/probe_", "tools/mutation/diagnose_",
                          "make_lnk_pure.py", "demo_", "tools/backfill_", "run_golden.py"})

    def test_run_golden_is_not_testable_in_process(self):
        # 上面那条理由的证据本身也要被钉住：那份守卫确实声明了「不许 import run_golden」。
        text = (ROOT / "tests" / "test_eval_memory_isolation.py").read_text(encoding="utf-8")
        self.assertIn("不许在这里 `import run_golden`", text)


class DiffParseTests(unittest.TestCase):
    def test_added_lines_only_on_the_new_side(self):
        diff = "\n".join([
            "diff --git a/agent_runtime/x.py b/agent_runtime/x.py",
            "--- a/agent_runtime/x.py",
            "+++ b/agent_runtime/x.py",
            "@@ -10,3 +12,2 @@",
            "@@ -40 +42 @@",
        ])
        self.assertEqual(cov.parse_diff_added_lines(diff),
                         {"agent_runtime/x.py": [12, 13, 42]})

    def test_pure_deletion_produces_no_changed_lines(self):
        diff = "\n".join([
            "diff --git a/agent_runtime/x.py b/agent_runtime/x.py",
            "--- a/agent_runtime/x.py",
            "+++ b/agent_runtime/x.py",
            "@@ -5,2 +4,0 @@",
            "-old line",
        ])
        self.assertEqual(cov.parse_diff_added_lines(diff), {})

    def test_hunk_without_a_count_means_one_line(self):
        diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -7 +7 @@\n")
        self.assertEqual(cov.parse_diff_added_lines(diff), {"a.py": [7]})


class EvaluateTests(unittest.TestCase):
    def test_all_changed_lines_executed_is_green(self):
        changed = {"agent_runtime/x.py": [1, 2, 3]}
        out = cov.evaluate(changed, {}, present={"agent_runtime/x.py"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["percent"], 100.0)

    def test_blanket_uncovered_block_is_red(self):
        changed = {"agent_runtime/x.py": [1, 2, 3, 4, 5]}
        missing = {"agent_runtime/x.py": {1, 2, 3, 4, 5}}
        out = cov.evaluate(changed, missing, present={"agent_runtime/x.py"})
        self.assertFalse(out["ok"])
        self.assertTrue(any("一行都没被执行" in item for item in out["failures"]), out["failures"])

    def test_one_uncovered_line_inside_a_bigger_change_only_warns(self):
        """一批改动里漏掉一两行没被走过 → 警告，不判红。

        这就是今天 `dev_server.py:140` 与 `head_check.py:194` 的真实形状（37 行改动里
        2 行未执行 = 94.6%）。100% 门槛会把人逼去写只为覆盖而存在的用例，
        那会污染变异电池（它靠「改坏实现有测试变红」判有没有牙）。
        """
        changed = {"agent_runtime/y.py": [i for i in range(1, 21)],   # 20 行，覆盖 19
                   "agent_runtime/z.py": [3]}
        missing = {"agent_runtime/y.py": {7}}
        out = cov.evaluate(changed, missing, present={"agent_runtime/y.py", "agent_runtime/z.py"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["percent"], round(100.0 * 20 / 21, 1))   # 95.2%
        self.assertTrue(any("没有任何用例走过" in item for item in out["warnings"]), out["warnings"])

    def test_a_lone_uncovered_change_is_red(self):
        """只改一行且没人执行 → 合计 0%，判红。这条是刻意严：小改动更容易「顺手改了没人看」。"""
        out = cov.evaluate({"agent_runtime/y.py": [7]}, {"agent_runtime/y.py": {7}},
                           present={"agent_runtime/y.py"})
        self.assertFalse(out["ok"])
        self.assertTrue(any("低于门槛" in item for item in out["failures"]), out["failures"])

    def test_aggregate_below_floor_is_red(self):
        changed = {"agent_runtime/a.py": [1, 2, 3, 4, 5, 6, 7, 8, 9]}   # 9 行全没执行
        missing = {"agent_runtime/a.py": set(range(1, 10))}
        # 另给一个覆盖良好的文件，把合计拉到门槛附近：合计 9/18 = 50% < 90%
        changed["agent_runtime/b.py"] = [i for i in range(1, 10)]
        out = cov.evaluate(changed, missing, present={"agent_runtime/a.py", "agent_runtime/b.py"})
        self.assertFalse(out["ok"])
        self.assertTrue(any("低于门槛" in item for item in out["failures"]), out["failures"])

    def test_changed_file_missing_from_report_is_unknown_not_covered(self):
        out = cov.evaluate({"agent_runtime/z.py": [1, 2]}, {}, present=set())
        self.assertFalse(out["ok"])
        self.assertTrue(any("未知" in item for item in out["failures"]), out["failures"])

    def test_manual_script_never_gates(self):
        changed = {"verify_engine_embed.py": [1, 2, 3, 4, 5]}
        missing = {"verify_engine_embed.py": {1, 2, 3, 4, 5}}
        out = cov.evaluate(changed, missing, present={"verify_engine_embed.py"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["changed_total"], 0)

    def test_tests_and_frontend_do_not_count(self):
        out = cov.evaluate({"tests/test_a.py": [1, 2, 3], "frontend/src/App.vue": [4]},
                           {}, present=set())
        self.assertTrue(out["ok"], out)

    def test_the_exclusion_is_not_merely_an_absence_of_inclusion(self):
        """允许清单没列 `tests/` 只是巧合，真正的防线是那行显式排除。

        这条是被变异逼出来的：把 `classify` 里「tests/ 一律不判」那行删掉，全部断言
        仍然绿 —— 因为 `tests/...` 本来就不在允许清单里。所以必须反过来钉：
        有人往允许清单里塞 `tests/` 时，判定得仍然把它排除掉。
        """
        import unittest.mock as mock
        with mock.patch.object(cov, "PRODUCTION_PREFIXES", ("agent_runtime/", "tests/")):
            self.assertEqual(cov.classify("tests/test_a.py"), "not_tracked")
            out = cov.evaluate({"tests/test_a.py": [1, 2, 3]}, {"tests/test_a.py": {1, 2, 3}},
                               present={"tests/test_a.py"})
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["changed_total"], 0)

    def test_excluded_lines_are_not_counted_as_missing(self):
        """coverage 自己标为 excluded 的行（注释/不可执行）不许算成「未覆盖」。"""
        payload = {"files": {"agent_runtime/e.py": {"summary": {"percent_covered": 50.0,
                                                                 "num_statements": 2},
                                                    "missing_lines": [7],
                                                    "excluded_lines": [7]}},
                   "totals": {"percent_covered": 50.0}}
        missing = cov.missing_lines_from_report(payload, str(ROOT))
        self.assertEqual(missing["agent_runtime/e.py"], set())


class SelfCheckTests(unittest.TestCase):
    def test_module_self_check_passes_as_a_subprocess(self):
        proc = subprocess.run([sys.executable, "-m", "agent_runtime.coverage_eval", "--self-check"],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(ROOT), timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        body = json.loads(proc.stdout)
        self.assertGreaterEqual(body["self_check"]["cases"], 8)
        self.assertEqual(body["self_check"]["failed"], [])


class CliFailClosedTests(unittest.TestCase):
    def test_missing_base_is_red_and_says_so(self):
        """退出码 1 还不够：打印的内容也不能说「通过」。

        这条是被变异逼出来的 —— 有人把 fail-closed 那句 `"ok": False` 改成 True，
        退出码仍然是 1，只看退出码的断言就看不见那句谎。
        """
        proc = subprocess.run([sys.executable, "-m", "agent_runtime.coverage_eval",
                               "--report", "nope.json"],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(ROOT), timeout=180)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        body = json.loads(proc.stdout)
        self.assertFalse(body["ok"], body)
        self.assertTrue(body["failures"], body)

    def test_unreadable_report_is_red_not_green(self):
        bad = pathlib.Path(os.devnull)
        self.assertEqual(cov.main(["--report", str(bad), "--base", "HEAD~1", "--root", str(ROOT)]), 1)


class EndToEndRepoTests(unittest.TestCase):
    """真仓库 + 真 git diff + 合成覆盖率报告：门必须能因覆盖率下降而红、恢复而绿。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="covgate-")
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        self._write("agent_runtime/mod.py", "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        # 改 5 行（新行号 1..5）
        self._write("agent_runtime/mod.py", "a = 10\nb = 20\nc = 30\nd = 40\ne = 50\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "改动")

    def _write(self, rel, text):
        path = pathlib.Path(self.dir) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.dir, *args], capture_output=True,
                              text=True, encoding="utf-8").stdout

    def _report(self, missing_lines):
        payload = {"files": {"agent_runtime/mod.py": {
                        "summary": {"percent_covered": 0.0, "num_statements": 5},
                        "missing_lines": missing_lines, "excluded_lines": []}},
                   "totals": {"percent_covered": 0.0}}
        out = pathlib.Path(self.dir) / "report.json"
        out.write_text(json.dumps(payload), encoding="utf-8")
        return str(out)

    def test_changed_lines_are_seen_and_blanket_miss_is_red(self):
        changed = cov.changed_python_files(self.base, "HEAD", self.dir)
        self.assertEqual(changed.get("agent_runtime/mod.py"), [1, 2, 3, 4, 5])
        rc = cov.main(["--report", self._report([1, 2, 3, 4, 5]), "--base", self.base,
                       "--root", self.dir, "--json"])
        self.assertEqual(rc, 1)

    def test_same_change_with_coverage_is_green(self):
        rc = cov.main(["--report", self._report([]), "--base", self.base,
                       "--root", self.dir, "--json"])
        self.assertEqual(rc, 0)

    def test_untracked_base_fails_closed(self):
        rc = cov.main(["--report", self._report([]), "--base", "deadbeef" * 5,
                       "--root", self.dir, "--json"])
        self.assertEqual(rc, 1)


class CiWiringTests(unittest.TestCase):
    """接线守卫：门在 CI 里真的会跑，而且红会传出去。"""

    def _gate_step(self):
        hits = [s for s in _steps() if "coverage_eval" in str(s.get("run") or "")]
        self.assertTrue(hits, "CI 里没有跑 coverage_eval 的步骤")
        return hits[0]

    def test_gate_step_exists_and_is_sqlite_only(self):
        self.assertIn("sqlite", str(self._gate_step().get("if") or ""))

    def test_gate_exit_code_is_not_swallowed(self):
        run = _code(self._gate_step())
        self.assertIn("exit $rc", run)
        gate_lines = [ln for ln in run.splitlines() if "coverage_eval" in ln]
        self.assertTrue(gate_lines and not any("|| true" in ln for ln in gate_lines),
                        "门自己的退出码不许被吞")

    def test_both_test_lanes_run_under_coverage(self):
        """两条测试 lane 各自的那一步必须被包成 `coverage run`。

        这条被两轮变异教做人：
        - 只看「整份文件里同时出现过 pytest 与 coverage run」→ 门那一步本来就带着
          `.coverage.pytest` 字样，等价变异被放过去；
        - 改成按步骤找之后，**我自己写的注释里就有「coverage run」这四个字**，
          注释行没剥掉，于是把真命令摘掉仍算通过。所以先剥 `#` 开头的行再看命令。
        """
        lane = [s for s in _steps() if "pytest $files" in _code(s)]
        self.assertTrue(lane, "找不到 pytest lane 那一步")
        self.assertIn("coverage run", _code(lane[0]), "pytest lane 没在覆盖率下跑")
        self.assertIn("-m pytest", _code(lane[0]))
        suite = [s for s in _steps() if "unittest discover" in _code(s)]
        self.assertTrue(suite, "找不到全量测试那一步")
        self.assertIn("coverage run", _code(suite[0]), "unittest lane 没在覆盖率下跑")
        gate = _code(self._gate_step())
        self.assertIn("coverage combine", gate, "两趟覆盖率数据没合并，门只会看到半边")
        self.assertIn("coverage json", gate, "门没有可读的报告输入")

    def test_checkout_fetches_full_history(self):
        checkout = [s for s in _steps() if "checkout" in str(s.get("uses") or "")]
        self.assertTrue(checkout, "找不到 checkout 步骤")
        self.assertEqual((checkout[0].get("with") or {}).get("fetch-depth"), 0,
                         "浅克隆会让门取不到 base 而恒红")

    def test_sibling_gates_still_run_their_own_commands(self):
        joined = "\n".join(str(s.get("run") or "") for s in _steps())
        for fragment, label in (("npm run lint", "前端 lint 门"),
                                ("ruff check", "静态缺陷门"),
                                ("python -m compileall", "编译检查")):
            self.assertIn(fragment, joined, "%s 的命令不见了" % label)


if __name__ == "__main__":
    unittest.main()
