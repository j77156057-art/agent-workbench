"""HEAD 自洽检查（`dev_headcheck`）的行为守卫。

要守的是 2026-10-01 那次真实事故：pathspec 提交绕过索引，把别人未提交的新文件【的引用】
扫进了 HEAD，于是工作树能跑、单独检出必红。所以这里最要紧的三条是
① 真挂干净检出（不是在工作树里 import）、② 缺模块要归因到"在磁盘但不在提交"、
③ 临时检出必须收回，且没有归属标记就绝不动手。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from agent_runtime import head_check as hchk  # noqa: E402
from agent_runtime import process_runner  # noqa: E402

GIT = ["git", "--no-pager"]


def _run(args, cwd):
    return subprocess.run(GIT + args, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


class RepoCase(unittest.TestCase):
    """在系统 temp 里建真 git 仓：STATE_ROOT 也指到 temp，避免污染项目状态目录。"""

    def setUp(self):
        self._prev_state = config.STATE_ROOT
        self.state = tempfile.mkdtemp(prefix="docmind_hchk_state_")
        config.STATE_ROOT = self.state
        self.repo = tempfile.mkdtemp(prefix="docmind_hchk_repo_")
        self.addCleanup(setattr, config, "STATE_ROOT", self._prev_state)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        _run(["init", "-q"], self.repo)

    def commit(self, *files: str) -> str:
        _run(["add", "--", *files], self.repo)
        _run(["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
              "commit", "-q", "-m", "fixture"], self.repo)
        return _run(["rev-parse", "HEAD"], self.repo).stdout.strip()

    def write(self, rel: str, text: str) -> Path:
        path = Path(self.repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def simple(self, extra: str = "") -> str:
        self.write("entry.py", "VALUE = 1\n" + extra)
        return self.commit("entry.py")


class RefTests(RepoCase):
    def test_head_resolves_to_a_pinned_sha(self):
        sha = self.simple()
        resolved = hchk.resolve_ref(self.repo, "HEAD")
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["sha"], sha)

    def test_unknown_ref_is_an_error_not_a_pass(self):
        self.simple()
        resolved = hchk.resolve_ref(self.repo, "no-such-ref")
        self.assertFalse(resolved["ok"])
        self.assertIn("找不到提交", resolved["error"])

    def test_shape_rejects_option_injection_and_globs(self):
        """断言【拒绝理由】而不是只断言"没收"。

        把这些串原样交给 git 也大多会失败，所以只看 ok=False 的话，删掉形状校验这条守卫
        照样是绿的（实测过：变异后 8 个用例全部仍然通过）。理由必须是"形状不被接受"，
        不是碰巧"找不到提交"。
        """
        self.simple()
        for bad in ("--help", "-b reffile:/etc/passwd", "HEAD;rm -rf", "a..b", "HEAD~2^0 x",
                    "*", "H" * 200, "refs/heads/x y"):
            with self.subTest(ref=bad):
                resolved = hchk.resolve_ref(self.repo, bad)
                self.assertFalse(resolved["ok"], "%r 竟然被接受" % bad)
                self.assertIn("形状不被接受", resolved["error"])

    def test_empty_ref_means_head_rather_than_an_error(self):
        """`ref:` 留空是工具层的常态（`_one` 回空串），当默认值处理而不是报错。"""
        sha = self.simple()
        self.assertEqual(hchk.resolve_ref(self.repo, "")["sha"], sha)
        self.assertEqual(hchk.resolve_ref(self.repo)["sha"], sha)

    def test_ancestor_refs_are_allowed(self):
        self.simple()
        self.assertTrue(hchk.resolve_ref(self.repo, "HEAD~0")["ok"])


class GitAllowlistTests(RepoCase):
    def test_write_history_commands_are_refused(self):
        self.simple()
        for args in (["commit", "-m", "x"], ["checkout", "HEAD~0"], ["reset", "--hard"],
                     ["push"], ["add", "."], []):
            with self.subTest(args=args):
                rep = hchk._git(self.repo, args)
                self.assertFalse(rep.get("ok", True))
                self.assertIn("白名单", rep.get("error", ""))

    def test_read_and_worktree_commands_are_allowed(self):
        self.simple()
        self.assertEqual(hchk._git(self.repo, ["rev-parse", "HEAD"]).get("exit_code"), 0)



class ImporterAttributionTests(RepoCase):
    def test_frame_scanning_finds_the_repo_line_not_the_bootstrap_frame(self):
        trace = (
            'Traceback (most recent call last):\n'
            '  File "<string>", line 3, in <module>\n'
            '    importlib.import_module(sys.argv[1])\n'
            '  File "C:\\py\\importlib\\__init__.py", line 90, in import_module\n'
            '    return _bootstrap._gcd_import(name[level:], package, level)\n'
            '  File "D:\\repo\\agent.py", line 55, in <module>\n'
            '    from agent_runtime.run_budget import RunBudget\n'
            "ModuleNotFoundError: No module named 'agent_runtime.run_budget'\n")
        self.assertEqual(hchk.find_importer(trace, "agent_runtime.run_budget"),
                         ("D:/repo/agent.py", 55, "agent_runtime.run_budget"))

    def test_a_first_party_import_beats_an_unrelated_one(self):
        trace = ('  File "D:\\repo\\a.py", line 2, in <module>\n'
                 '    import json\n'
                 '  File "D:\\repo\\b.py", line 9, in <module>\n'
                 '    import pkg.missing\n')
        self.assertEqual(hchk.find_importer(trace, "pkg.missing"),
                         ("D:/repo/b.py", 9, "pkg.missing"))


class CheckOutcomeTests(RepoCase):
    def test_parse_never_defaults_to_success(self):
        for rep in ({}, {"output": ""}, {"output": "not json"}, {"output": "[1,2]"},
                    {"output": json.dumps({"ok": "truthy-string"})}):
            with self.subTest(rep=str(rep)[:40]):
                self.assertFalse(hchk.parse_outcome(rep)["ok"])

    def test_parse_reads_a_real_success(self):
        self.assertTrue(hchk.parse_outcome(
            {"output": json.dumps({"ok": True, "error": ""})})["ok"])


class CleanCheckoutTests(RepoCase):
    def _tree(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/leaf.py", "VALUE = 2\n")
        self.write("pkg/mid.py", "from pkg.leaf import VALUE\nOTHER = VALUE + 1\n")
        self.write("entry.py", "from pkg.mid import OTHER\nTOP = OTHER\n")
        return self.commit("pkg/__init__.py", "pkg/leaf.py", "pkg/mid.py", "entry.py")

    def test_the_main_checkout_is_never_touched(self):
        """自检只读 + detach：不动 HEAD、不动工作树，也不许留下新分支。

        `--detach` 守的就是这一条：换成 `git worktree add -b <name>` 同样能建检出，
        但会在共享仓里凭空占一个分支名，别人的 `git worktree add` 可能因此撞车。
        """
        self._tree()
        branches_before = _run(["branch", "--list"], self.repo).stdout
        head_before = _run(["rev-parse", "HEAD"], self.repo).stdout
        result = hchk.check(self.repo, modules=("entry",), timeout=60)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(_run(["branch", "--list"], self.repo).stdout, branches_before,
                         "自检创建了分支")
        self.assertEqual(_run(["rev-parse", "HEAD"], self.repo).stdout, head_before)
        self.assertEqual(_run(["status", "--porcelain"], self.repo).stdout.strip(), "",
                         "自检在别人的工作树里留下了痕迹")
        listed = _run(["worktree", "list"], self.repo).stdout
        self.assertNotIn("headcheck-", listed, "临时检出还挂在 worktree 名单上")

    def test_a_self_consistent_head_passes_and_leaves_nothing_behind(self):
        sha = self._tree()
        result = hchk.check(self.repo, modules=("entry",), timeout=60)
        self.assertTrue(result["ok"], json.dumps(result, ensure_ascii=False, default=str)[:900])
        self.assertEqual(result["sha"], sha)
        self.assertTrue(result["checks"]["no_leftover_checkout"])
        self.assertFalse(Path(result["worktree"]).exists(), "临时检出没收回")
        self.assertEqual(list(hchk.worktree_base(self.repo).glob("*")), [],
                         "headcheck 基目录里留下了东西")

    def test_a_committed_reference_to_an_untracked_file_is_called_out(self):
        """就是 2026-10-01 那次：`run_budget.py` 没入库，但引用它的 import 入库了。"""
        self.write("pkg/__init__.py", "")
        self.write("helper.py", "SEEN = []\n")          # 只写在磁盘上，不提交
        self.write("entry.py", "import helper\nTOP = helper.SEEN\n")
        self.commit("pkg/__init__.py", "entry.py")
        result = hchk.check(self.repo, modules=("entry",), timeout=60)
        self.assertFalse(result["ok"])
        failure = result["failures"][0]
        self.assertEqual(failure["missing"], "helper")
        self.assertFalse(failure["in_commit"], "它明明没入库")
        self.assertTrue(failure["on_disk"], "它就在工作树里躺着，所以本地跑得起来")
        self.assertIn("未提交", failure["likely"])
        self.assertEqual(failure["importer"], "entry.py")
        self.assertEqual(failure["importer_line"], 1)
        self.assertTrue(result["checks"]["no_leftover_checkout"])
        self.assertIn("别人未提交的新文件被提交引用", hchk.render(result))

    def test_a_module_that_is_nowhere_is_a_different_answer(self):
        self.write("entry.py", "import totally_absent_xyz\n")
        self.commit("entry.py")
        result = hchk.check(self.repo, modules=("entry",), timeout=60)
        self.assertFalse(result["ok"])
        failure = result["failures"][0]
        self.assertFalse(failure["on_disk"])
        self.assertFalse(failure["in_commit"])
        self.assertIn("根本没有", failure["likely"])

    def test_a_runtime_error_is_not_reported_as_a_missing_module(self):
        self.write("entry.py", "raise RuntimeError('boom at import time')\n")
        self.commit("entry.py")
        result = hchk.check(self.repo, modules=("entry",), timeout=60)
        self.assertFalse(result["ok"])
        self.assertIn("boom at import time", result["failures"][0]["error"])
        self.assertNotIn("missing", result["failures"][0])


class VerdictShapeTests(RepoCase):
    def test_nothing_attempted_is_not_a_pass(self):
        """`all([])` 是 True：入口列表为空时必须判未通过，不能报绿。"""
        self.write("entry.py", "TOP = 1\n")
        self.commit("entry.py")
        result = hchk.check(self.repo, modules=(), timeout=60)
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["every_entry_imported"])

    def test_a_directory_that_is_not_a_repo_fails_explicitly(self):
        stray = tempfile.mkdtemp(prefix="docmind_hchk_plain_")
        self.addCleanup(shutil.rmtree, stray, ignore_errors=True)
        result = hchk.check(stray)
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"], {})
        self.assertIn("git", result["error"])

    def test_missing_root_is_reported_not_raised(self):
        result = hchk.check(Path(tempfile.mkdtemp()) / "nope")
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])

    def test_render_states_the_boundary_even_when_green(self):
        self.write("entry.py", "TOP = 1\n")
        self.commit("entry.py")
        text = hchk.render(hchk.check(self.repo, modules=("entry",), timeout=60))
        self.assertIn("HEAD 自检【通过】", text)
        self.assertIn("边界", text)

    def test_render_surfaces_a_checkout_that_could_not_be_removed(self):
        self.write("entry.py", "TOP = 1\n")
        self.commit("entry.py")
        result = hchk.check(self.repo, modules=("entry",), timeout=60)
        result["cleanup"] = {"ok": False, "removed": False, "error": "占用"}
        self.assertIn("没有收回干净", hchk.render(result))


class OwnershipGuardTests(RepoCase):
    def test_remove_refuses_a_directory_it_did_not_create(self):
        """没有归属标记就绝不 `worktree remove --force`——那是拆别人的检出。"""
        stranger = Path(self.repo) / "someone-elses-worktree"
        stranger.mkdir()
        rep = hchk.remove_worktree(self.repo, stranger)
        self.assertFalse(rep["ok"])
        self.assertIn("归属标记", rep["error"])
        self.assertTrue(stranger.exists(), "不该动的目录被动了")

    def test_add_refuses_to_overwrite_an_existing_target(self):
        sha = self.simple()
        target = hchk.make_target(self.repo, sha)
        target.mkdir(parents=True)
        rep = hchk.add_worktree(self.repo, target, sha)
        self.assertFalse(rep["ok"])
        self.assertIn("已存在", rep["error"])


class ToolSurfaceTests(unittest.TestCase):
    def test_the_tool_exists_in_all_three_places(self):
        import agent
        import tools
        self.assertIn("dev_headcheck", tools.TOOLS)
        self.assertIn("ref", tools.TOOLS["dev_headcheck"]["description"])
        line = next(l for l in agent._SYSTEM_PROMPT_FULL.splitlines()
                    if l.startswith("- dev_headcheck"))
        self.assertIn("HEAD", line)
        self.assertEqual(len(tools.tool_schemas()), len(tools.TOOLS))

    def test_mcp_exposure_is_reviewed(self):
        from agent_runtime import mcp_server
        listed = set(mcp_server.READ_ONLY_TOOLS) | set(getattr(mcp_server, "WRITE_TOOLS", ()))
        self.assertIn("dev_headcheck", listed, "新工具没进 MCP 暴露表")

    def test_a_plain_directory_reports_failure_not_green(self):
        import tools
        stray = tempfile.mkdtemp(prefix="docmind_hchk_tool_")
        prev = config.get_runtime("code_root")
        config.set_runtime("code_root", stray)
        self.addCleanup(config.set_runtime, "code_root", prev)
        self.addCleanup(shutil.rmtree, stray, ignore_errors=True)
        text = tools.TOOLS["dev_headcheck"]["func"]("")
        self.assertTrue(text.strip())
        self.assertNotIn("【通过】", text)


class SuiteArgvTests(unittest.TestCase):
    """命令构造与解析：这一层错了，后面所有判据都建立在"跑错了东西"上。"""

    BS = chr(92)

    def test_relative_path_sanitizer(self):
        table = {
            "tests/test_a.py": "tests/test_a.py",
            "tests//test_a.py": "tests/test_a.py",
            "a/./b.py": "a/b.py",
            ".." + self.BS + "x": "",
            "tests/../../etc/passwd": "",
            "/abs/path": "",
            "C:" + self.BS + "x" + self.BS + "y": "",
            "-p no:cacheprovider": "",        # 以 - 开头会被 pytest 当选项
            "": "",
        }
        for raw, want in table.items():
            with self.subTest(raw=raw):
                self.assertEqual(hchk.safe_relative(raw), want)

    def test_gate_command_runs_the_committed_dataset_and_reads_the_committed_baseline(self):
        argv = hchk.gate_argv()
        self.assertEqual(argv[1:3], ["-B", "-m"])
        self.assertIn("agent_runtime.dev_eval", argv)
        self.assertIn("--json", argv)
        self.assertEqual(argv[argv.index("--baseline") + 1], hchk.GATE_BASELINE)
        self.assertFalse(os.path.isabs(hchk.GATE_BASELINE), "基线必须是仓内相对路径")

    def test_pytest_command_isolates_cache_and_basetemp(self):
        argv = hchk.pytest_argv(["tests/a.py", "tests/a.py"], "T" + self.BS + "run")
        self.assertEqual(argv[1:4], ["-B", "-m", "pytest"])
        self.assertEqual([row for row in argv if row.endswith(".py")], ["tests/a.py"])
        self.assertIn("no:cacheprovider", argv)
        self.assertIn("-rf", argv, "没有 -rf 就拿不到失败题名单，差集无从算起")
        self.assertTrue(any(row.startswith("--basetemp=") for row in argv))

    def test_bad_targets_are_refused_before_anything_runs(self):
        for bad in (".." + self.BS + "evil.py", "/etc/passwd", "-Add"):
            with self.subTest(bad=bad):
                with self.assertRaises(hchk.HeadCheckError):
                    hchk.pytest_argv([bad], "T")
        with self.assertRaises(hchk.HeadCheckError):
            hchk.pytest_argv([], "T")
        with self.assertRaises(hchk.HeadCheckError):
            hchk.pytest_argv(["t/%d.py" % i for i in range(20)], "T")
        with self.assertRaises(hchk.HeadCheckError):
            hchk.gate_argv("/etc/passwd")


class GateParsingTests(unittest.TestCase):
    def test_unparseable_output_is_never_green(self):
        for blob in ("", "Traceback ...", "not json", '{"unexpected": 1}'):
            with self.subTest(blob=blob[:20]):
                self.assertFalse(hchk.parse_gate(blob)["parsed"])

    def test_gate_green_needs_scored_cases_and_no_regressions(self):
        green = '{"report": {"ok": true, "passed": 24, "scored": 24, "failed": []}, ' \
                '"gate": {"regressed": false, "regressions": []}}'
        self.assertTrue(hchk.parse_gate(green)["parsed"])
        red = '{"report": {"ok": false, "passed": 22, "scored": 24, ' \
              '"failed": ["a-case"]}, "gate": {"regressed": true, "regressions": ["b-case"]}}'
        row = hchk.parse_gate(red)
        self.assertEqual(row["failed"], ["a-case"])
        self.assertEqual(row["regressions"], ["b-case"])
        self.assertTrue(row["regressed"])

    def test_zero_collected_is_not_a_pass(self):
        for blob, code in (("no tests ran in 0.01s", 5), ("collected 0 items", 0),
                           ("4 skipped, 1 warning in 2.0s", 0)):
            with self.subTest(blob=blob[:16]):
                row = hchk.parse_pytest(blob, code)
                self.assertEqual(row["ran"], 0)
                self.assertFalse(row["ok"], blob)

    def test_counts_come_from_the_summary_line(self):
        row = hchk.parse_pytest("2 failed, 39 passed, 48 subtests passed in 0.5s", 1)
        self.assertEqual((row["passed"], row["failed"], row["ok"]), (39, 2, False))
        self.assertTrue(hchk.parse_pytest("39 passed in 0.5s", 0)["ok"])


class AttributionTests(unittest.TestCase):
    def test_the_three_verdicts_are_different_sentences(self):
        """断言要比对**常量**，不能只 `assertIn` 一句话：
        "只有检出红"那句里原本也带着"两边都红"字样，`assertIn` 就被同一句话满足了——
        是电池（那条变异）当场指出这个测试没牙，不是我看出来。"""
        green = {"ok": True, "ran": True, "failed_names": []}
        red = {"ok": False, "ran": True, "failed_names": ["only-in-checkout"]}
        same = {"ok": False, "ran": True, "failed_names": ["both"]}
        self.assertEqual(hchk.attribute(green, green), "")
        self.assertEqual(hchk.attribute(green, None), "")
        self.assertIn("only-in-checkout",
                      hchk.attribute({**red, "ran": True}, green))
        self.assertTrue(hchk.attribute(red, green).startswith("只有检出里红的题"))
        self.assertEqual(hchk.attribute({**same, "only_in_checkout": []}, same),
                         hchk.ATTR_BOTH_RED)
        self.assertEqual(hchk.attribute(red, {"ok": False, "ran": False}), hchk.ATTR_NO_CONTROL)
        self.assertEqual(hchk.attribute(red, None), "")

    def test_the_two_sentenced_cannot_be_confused_by_substring(self):
        self.assertNotIn("两棵树里都红", hchk.ATTR_ONLY_IN_COMMIT)
        self.assertNotIn("只有检出里红", hchk.ATTR_BOTH_RED)
        self.assertNotEqual(hchk.ATTR_ONLY_IN_COMMIT, hchk.ATTR_BOTH_RED)
        self.assertIn("%s", hchk.ATTR_ONLY_IN_COMMIT, "归因句必须能塞进失败题的名字")


class FakeGateRunner:
    """真的把 import 步骤交给真子进程，只把【套件】换成可控的假结论。

    真 import 不能造假：这一档的意义就是"在那棵树上跑真东西"。
    """

    def __init__(self, repo, checkout_ok=True, repo_ok=True, explode=False, green_from=0,
                 checkout_names=("x-case",), repo_names=("x-case",)):
        self.repo = os.path.normpath(str(repo))
        self.checkout_ok, self.repo_ok, self.explode = checkout_ok, repo_ok, explode
        self.green_from = green_from      # 第 N 次（从 0 数）起检出里改口说绿：模拟计时抖动
        self.checkout_names, self.repo_names = checkout_names, repo_names
        self.seen = []

    def __call__(self, argv, **kw):
        text = " ".join(str(row) for row in argv)
        if "agent_runtime.dev_eval" not in text and "-m pytest" not in text:
            return process_runner.run_bounded(argv, **kw)
        cwd = os.path.normpath(str(kw.get("cwd") or ""))
        in_checkout = cwd != self.repo
        attempt = len([row for row in self.seen if row["in_checkout"]])
        self.seen.append({"in_checkout": in_checkout, "cwd": cwd,
                          "state": (kw.get("env") or {}).get("DOCMIND_STATE_ROOT") or ""})
        if self.explode:
            raise RuntimeError("假：子进程起不来")
        if in_checkout and attempt >= self.green_from > 0:
            ok = True
        else:
            ok = self.checkout_ok if in_checkout else self.repo_ok
        names = list(self.checkout_names if in_checkout else self.repo_names)
        if ok:
            names = []
        payload = json.dumps({"report": {"ok": ok, "passed": 24 if ok else 22,
                                         "scored": 24, "failed": names},
                              "gate": {"regressed": False, "regressions": []}})
        return {"ok": ok, "exit_code": 0 if ok else 1, "timed_out": False, "truncated": False,
                "output": payload, "error": ""}


class CheckoutSuiteTests(RepoCase):
    """跑批层（第二档）的判据。

    大部分用例直接调 `run_suites`——`target` 在那条回路里只是一个 cwd，不需要真检出目录。
    真挂检出每条要 1~2 秒，而电池会把这套判据乘以变异条数（实测 8 条真检出把这块电池从
    182 秒推到 523 秒）。只留三条走 `check()` 全链路：这一档真的被接上、行为红真的让整检
    红、跑完真的不留东西——那三件事只有真检出才证得了。
    """

    def setUp(self):
        super().setUp()
        self.checkout = Path(tempfile.mkdtemp(prefix="docmind_hchk_fakeco_"))
        self.runstate = Path(tempfile.mkdtemp(prefix="docmind_hchk_rstate_"))
        self.addCleanup(shutil.rmtree, str(self.checkout), ignore_errors=True)
        self.addCleanup(shutil.rmtree, str(self.runstate), ignore_errors=True)

    def _head(self) -> str:
        self.write("entry.py", "TOP = 1\n")
        return self.commit("entry.py")

    def gate_row(self, runner):
        return hchk.run_suites(self.checkout, str(self.repo), want_gate=True, tests=(),
                               state_root=self.runstate, timeout=30, runner=runner)[0]

    def pytest_row(self, runner, tests):
        return hchk.run_suites(self.checkout, str(self.repo), want_gate=False, tests=tests,
                               state_root=self.runstate, timeout=30, runner=runner)[0]

    @staticmethod
    def text_of(row, ok=False):
        return hchk.render({"checks": {"worktree_added": True, "every_entry_imported": True,
                                       "every_suite_ran": True, "every_suite_green": ok},
                            "suites": [row], "imported": ["entry"], "modules": ["entry"]})

    def test_green_gate_costs_no_control_group(self):
        """对照组只在红了时跑：绿的时候多跑一遍整集是白付一倍时间，而且没多一条判据。"""
        fake = FakeGateRunner(self.repo, checkout_ok=True, repo_ok=False)
        row = self.gate_row(fake)
        self.assertEqual([entry["in_checkout"] for entry in fake.seen], [True], fake.seen)
        self.assertTrue(row["ok"], row)
        self.assertIn("跑绿", self.text_of(row, ok=True))

    def test_a_shared_flake_does_not_bury_the_checkout_only_case(self):
        """就是拿 `f7ec14e` 真跑出来的那一形状：两棵树都红（diagnostics 在负载下抖），
        但**只有检出里**多红一题——归因必须指着那一题，而不是"两棵树都红"。"""
        fake = FakeGateRunner(self.repo, checkout_ok=False, repo_ok=False,
                              checkout_names=("page-action-refuses", "diagnostics-flaky"),
                              repo_names=("diagnostics-flaky",))
        row = self.gate_row(fake)
        self.assertFalse(row["ok"])
        self.assertEqual(row["only_in_checkout"], ["page-action-refuses"], row)
        self.assertEqual(row["only_in_worktree"], [])
        self.assertIn("只有检出里红的题", row["likely"])
        self.assertIn("page-action-refuses", row["likely"])
        self.assertNotEqual(row["likely"], hchk.ATTR_BOTH_RED)
        self.assertIn("diagnostics-flaky", self.text_of(row))

    def test_red_only_in_the_checkout_is_attributed_to_missing_content(self):
        fake = FakeGateRunner(self.repo, checkout_ok=False, repo_ok=True)
        row = self.gate_row(fake)
        self.assertEqual([entry["in_checkout"] for entry in fake.seen], [True, True, False],
                         "红了要先在检出里重跑，再在工作树里跑一次当对照组")
        self.assertFalse(row["ok"])
        self.assertEqual(row["attempts"], 2)
        self.assertIn("只有检出里红的题", row["likely"])
        self.assertEqual(row["only_in_checkout"], ["x-case"])
        self.assertEqual(row["failed_names"], ["x-case"])
        self.assertTrue(row["control"]["ok"])
        self.assertIn("x-case", row["detail"],
                      "摘要里不写失败题名字，红了还得再跑一遍才知道红在哪题")
        text = self.text_of(row)
        for needle in ("对照组", "仍然红", "门禁整集", "x-case", "没提交的那部分"):
            self.assertIn(needle, text)

    def test_a_red_that_clears_on_retry_is_not_counted_as_red(self):
        """门禁题里有计时敏感的（ruff 起子进程）：抖一下不能算提交坏了，也不能瞒着不说。"""
        fake = FakeGateRunner(self.repo, checkout_ok=False, repo_ok=True, green_from=1)
        row = self.gate_row(fake)
        self.assertTrue(row["ok"], json.dumps(row, ensure_ascii=False))
        self.assertTrue(row["flipped"])
        self.assertEqual([entry["in_checkout"] for entry in fake.seen], [True, True],
                         "重跑绿了就不该再花一遍时间跑对照组")
        text = self.text_of(row, ok=True)
        self.assertIn("重跑才绿", text)
        self.assertIn("没发生过", text)
        self.assertIn("跑了 2 次", text)

    def test_both_red_is_not_a_self_consistency_failure(self):
        fake = FakeGateRunner(self.repo, checkout_ok=False, repo_ok=False)
        row = self.gate_row(fake)
        self.assertEqual(row["likely"], hchk.ATTR_BOTH_RED)
        self.assertFalse(row["ok"])
        self.assertEqual([entry["in_checkout"] for entry in fake.seen], [True, True, False])
        self.assertIn("两棵树里都红", self.text_of(row))

    def test_a_suite_that_never_ran_is_red_without_a_fake_attribution(self):
        """子进程起不来时不能顺手说"提交缺内容"——那次红根本没法判。"""
        fake = FakeGateRunner(self.repo, explode=True)
        row = self.gate_row(fake)
        self.assertFalse(row["ran"])
        self.assertFalse(row["ok"])
        self.assertEqual(row["likely"], "")
        self.assertEqual(row["attempts"], 1, "没跑成就不该再花一遍时间重跑")
        self.assertEqual(len(fake.seen), 1)
        self.assertIn("假：子进程起不来", self.text_of(row))

    def test_a_target_that_exists_only_on_disk_is_named_without_running_it(self):
        """`tests: x.py` 里那个 x.py 只在工作树上 = 漏提交，直接说出来。"""
        self.write("late_test.py", "def test_x():\n    assert True\n")
        fake = FakeGateRunner(self.repo)
        row = self.pytest_row(fake, ("late_test.py",))
        self.assertEqual(row["targets_missing"], ["late_test.py"])
        self.assertFalse(row["ok"])
        self.assertIn("提交里不存在", self.text_of(row))
        self.assertEqual(fake.seen, [], "文件都不在提交里，跑它没有意义")

    def test_the_suite_runs_where_it_says_and_keeps_state_outside(self):
        fake = FakeGateRunner(self.repo)
        self.gate_row(fake)
        seen = fake.seen[0]
        self.assertEqual(os.path.normpath(seen["cwd"]), os.path.normpath(str(self.checkout)))
        self.assertTrue(seen["state"], "没给子进程隔离状态根")
        self.assertFalse(os.path.normpath(seen["state"]).startswith(
            os.path.normpath(seen["cwd"])),
            "状态目录不能落在跑批的目录里，否则回收判据不可信：%s / %s" % (
                seen["state"], seen["cwd"]))

    def test_asking_for_nothing_runs_nothing(self):
        fake = FakeGateRunner(self.repo)
        self.assertEqual(hchk.run_suites(self.checkout, str(self.repo), want_gate=False,
                                         tests=(), state_root=self.runstate, timeout=30,
                                         runner=fake), [])
        self.assertEqual(fake.seen, [])

    def test_render_names_the_worktree_only_failures_separately(self):
        """反过来也要说清：只在**工作树**里红的是别人在途的东西，不该混进我的结论。"""
        fake = FakeGateRunner(self.repo, checkout_ok=False, repo_ok=False,
                              checkout_names=("mine", "shared"),
                              repo_names=("theirs", "shared"))
        row = self.gate_row(fake)
        self.assertEqual(row["only_in_checkout"], ["mine"])
        self.assertEqual(row["only_in_worktree"], ["theirs"])
        self.assertIn("只在工作树里红", self.text_of(row))

    def test_the_pytest_path_also_names_its_failed_tests(self):
        """`-rf` 那条路也要报题号：`tests:` 档的差集靠的就是它，不然只有门禁那一半能用。"""
        out = ("F..\n"
               "FAILED tests/test_a.py::test_one - AssertionError: 差一个\n"
               "1 failed, 2 passed in 0.4s\n")

        def runner(argv, **kw):
            return {"ok": False, "exit_code": 1, "timed_out": False, "truncated": False,
                    "output": out, "error": ""}

        row = hchk.run_suite(self.checkout, ["py", "-B", "-m", "pytest", "tests/test_a.py"],
                             kind="pytest", state_root=self.runstate, timeout=30, runner=runner)
        self.assertFalse(row["ok"])
        self.assertEqual(row["failed_names"], ["tests/test_a.py::test_one"], row)
        self.assertEqual(row["passed"], 2)

    def test_the_gate_tier_is_actually_wired_into_the_check(self):
        """下面三条走真检出：`check(gate=True)` 真的接到了这一档，而不是各自自洽。"""
        self._head()
        fake = FakeGateRunner(self.repo)
        result = hchk.check(self.repo, modules=("entry",), timeout=60, gate=True, runner=fake)
        self.assertEqual(len(result["suites"]), 1, result.get("error"))
        self.assertTrue(result["checks"]["every_suite_green"], result["suites"])
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(Path(fake.seen[0]["cwd"]).name.startswith("headcheck-"),
                        "跑批没在那棵检出里跑")

    def test_a_red_suite_makes_the_whole_check_red(self):
        self._head()
        result = hchk.check(self.repo, modules=("entry",), timeout=60, gate=True,
                            runner=FakeGateRunner(self.repo, checkout_ok=False, repo_ok=True))
        self.assertFalse(result["ok"], json.dumps(result["suites"], ensure_ascii=False)[:400])
        self.assertFalse(result["checks"]["every_suite_green"])
        self.assertIn("gate 未通过", result["error"])

    def test_nothing_is_left_behind_after_a_suite_run(self):
        self._head()
        fake = FakeGateRunner(self.repo)
        result = hchk.check(self.repo, modules=("entry",), timeout=60, gate=True, runner=fake)
        self.assertTrue(result["suite_state_removed"], result.get("suites"))
        self.assertEqual(list(hchk.worktree_base(self.repo).glob("*")), [],
                         "检出或跑批目录留下了东西")
        seen = fake.seen[0]
        self.assertFalse(os.path.normpath(seen["state"]).startswith(
            os.path.normpath(seen["cwd"])),
            "跑批的状态目录落在了检出**里面**（%s ⊂ %s）——那 `no_leftover_checkout` 就成了自证"
            % (seen["state"], seen["cwd"]))

    def test_a_check_says_when_the_suite_never_ran(self):
        """`every_suite_ran` 只能由 `check()` 那一层回吐：run_suites 自己不产 checks。"""
        self._head()
        result = hchk.check(self.repo, modules=("entry",), timeout=60, gate=True,
                            runner=FakeGateRunner(self.repo, explode=True))
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["every_suite_ran"])
        self.assertFalse(result["checks"]["every_suite_green"])

    def test_a_regression_against_the_baseline_is_not_green(self):
        """`report.ok` 说通过、基线说回退——回退赢。两个数不能互相赦免。"""
        def runner(argv, **kw):
            payload = json.dumps({"report": {"ok": True, "passed": 24, "scored": 24,
                                             "failed": []},
                                  "gate": {"regressed": True, "regressions": ["a-case"]}})
            return {"ok": True, "exit_code": 0, "timed_out": False, "truncated": False,
                    "output": payload, "error": ""}
        row = hchk.run_suite(self.checkout, ["py", "-B", "-m", "agent_runtime.dev_eval"],
                             kind="gate", state_root=self.runstate, timeout=30, runner=runner)
        self.assertFalse(row["ok"], row)
        self.assertEqual(row["gate"]["regressions"], ["a-case"])

    def test_a_timeout_is_red_and_says_so(self):
        """超时的红是"没验成"，措辞上不能和"跑出来是红的"混成一句。"""
        def runner(argv, **kw):
            return {"ok": False, "exit_code": None, "timed_out": True, "truncated": False,
                    "output": "", "error": ""}
        row = hchk.run_suite(self.checkout, ["py", "-B", "-m", "agent_runtime.dev_eval"],
                             kind="gate", state_root=self.runstate, timeout=30, runner=runner)
        self.assertFalse(row["ok"])
        self.assertFalse(row["ran"])
        self.assertIn("超时", row["detail"])

    def test_the_ceiling_promised_is_the_ceiling_enforced(self):
        """`run_bounded` 会把 timeout 削到 `process_runner.MAX_TIMEOUT`：
        这里配一个更大的数字就是**谎报能力**，所以两者必须钉在一起。"""
        self.assertEqual(hchk.SUITE_TIMEOUT_MAX, process_runner.MAX_TIMEOUT)
        self.assertLessEqual(hchk.SUITE_TIMEOUT_DEFAULT, hchk.SUITE_TIMEOUT_MAX)
        self.assertGreaterEqual(hchk.SUITE_TIMEOUT_MIN, 30)


class RealSuiteWiringTests(RepoCase):
    """真子进程一层：cwd 与状态根必须真的落到那棵检出去（假 runner 证不了这件事）。"""

    def test_run_suite_uses_the_given_directory_and_state_root(self):
        where = Path(tempfile.mkdtemp(prefix="docmind_hchk_run_"))
        self.addCleanup(shutil.rmtree, str(where), ignore_errors=True)
        argv = [sys.executable, "-B", "-c",
                "import os,json;print(json.dumps({'cwd':os.getcwd()}));print('3 passed')"]
        row = hchk.run_suite(where, argv, kind="pytest", state_root=where / "state",
                             timeout=60)
        self.assertTrue(row["ok"], json.dumps(row, ensure_ascii=False, default=str))
        self.assertEqual(row["passed"], 3)
        self.assertEqual(os.path.normpath(json.loads(row["detail"].strip().splitlines()[0])
                                          ["cwd"]), os.path.normpath(str(where)))

    def test_a_real_zero_collection_is_red(self):
        where = Path(tempfile.mkdtemp(prefix="docmind_hchk_run2_"))
        self.addCleanup(shutil.rmtree, str(where), ignore_errors=True)
        argv = [sys.executable, "-B", "-c", "print('no tests ran in 0.01s')"]
        row = hchk.run_suite(where, argv, kind="pytest", state_root=where, timeout=60)
        self.assertFalse(row["ok"])
        self.assertEqual(row["passed"], 0)

    def test_a_conclusion_bigger_than_the_default_window_still_parses(self):
        """`dev_eval --json` 一行就 ~26 KB，而输出窗默认只有 4000 字符。

        截断过的 JSON 解析不出结论——那会被读成"门禁没跑成"，而它其实跑成了。
        这两件事必须分得开，所以窗口在这里显式放大。
        """
        where = Path(tempfile.mkdtemp(prefix="docmind_hchk_run3_"))
        self.addCleanup(shutil.rmtree, str(where), ignore_errors=True)
        pad = "x" * 30_000
        code = ("import json\nprint(json.dumps({'report': {'ok': True, 'passed': 24,"
                " 'scored': 24, 'failed': []}, 'pad': %r}))" % pad)
        argv = [sys.executable, "-B", "-c", code]
        row = hchk.run_suite(where, argv, kind="gate", state_root=where, timeout=60)
        self.assertTrue(row.get("parsed"), row.get("detail")[-200:])
        self.assertTrue(row["ok"], row.get("detail")[-200:])
        # 结论能解析成结构化字段，本身就证明整行被读全了（截断过的 JSON 解析不出任何东西）
        self.assertEqual(row["gate"]["passed"], 24)


class WholeRepoGateTests(unittest.TestCase):
    """端到端一次：真的挂本仓的干净检出、真的在里面跑门禁整集。

    慢（约 40~90 秒），所以**不进变异电池**（见 `LAYER_EXEMPT`）——它验的是接线，
    判据表由上面那些类负责。它不断言"门禁一定绿"（那是 test_dev_eval 的活），
    断言的是：这一档确实跑成了、确实读了那棵树的结论，而且红的时候必须给出归因。
    """

    def test_the_checkout_gate_tier_really_runs_against_this_repo(self):
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            self.skipTest("没有 git")
        result = hchk.check(ROOT, modules=("config",), timeout=120, gate=True,
                            suite_timeout=900)
        rows = result.get("suites") or []
        self.assertEqual(len(rows), 1, json.dumps(result, ensure_ascii=False, default=str)[:600])
        row = rows[0]
        self.assertTrue(row.get("ran"), row.get("detail"))
        self.assertTrue(row.get("parsed"), row.get("detail"))
        self.assertGreater(row.get("passed") or 0, 0, row.get("detail"))
        if not row["ok"]:
            self.assertTrue(row.get("likely"), "红了却没有归因")
            self.assertIn("对照组", hchk.render(result))
        self.assertTrue(result["checks"]["no_leftover_checkout"], result.get("worktree"))


if __name__ == "__main__":
    unittest.main()
