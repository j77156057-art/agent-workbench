"""P2 只读工具回归：dev_glob（按文件名找文件）与 dev_git_log（只读历史）。

覆盖三件事：匹配语义（文件名 vs 相对路径、大小写、跳过噪声目录）、
只读与参数注入护栏、以及真 git 仓库下的历史解析。
需要真 git 的用例在 git 不可用时 skip，其余全部本地可跑。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import tools  # noqa: E402
from agent_runtime import code_intel  # noqa: E402


def _git_available():
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True,
                              timeout=15)
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


GIT_OK = _git_available()


class _TmpRoot(unittest.TestCase):
    """code_root 指向临时目录，用完恢复。"""

    def setUp(self):
        self._prev = config.get_runtime("code_root")
        self.tmp = tempfile.mkdtemp(prefix="docmind_globlog_")

    def tearDown(self):
        config.set_runtime("code_root", self._prev)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use_root(self):
        config.set_runtime("code_root", self.tmp)

    def write(self, rel, content="x\n"):
        full = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        return full


# --------------------------------------------------------------------------- #
# glob_files: 匹配语义
# --------------------------------------------------------------------------- #

class GlobMatchCases(_TmpRoot):
    def setUp(self):
        super().setUp()
        self.write("frontend/src/views/Foo.vue")
        self.write("frontend/src/workbench/liveAudioControl.ts")
        self.write("frontend/src/workbench/liveVision.ts")
        self.write("tests/test_realtime_provider.py")
        self.write("tests/test_voice.py")
        self.write("Dockerfile")
        self.write(".github/workflows/harness.yml")
        self.write("node_modules/pkg/evil.vue")
        self.write("frontend/node_modules/deep/bad.vue")

    def rel(self, result):
        return list(result["files"])

    def test_basename_pattern_matches_any_depth(self):
        res = code_intel.glob_files(self.tmp, "*.vue")
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(self.rel(res), ["frontend/src/views/Foo.vue"])

    def test_noise_dirs_are_skipped(self):
        res = code_intel.glob_files(self.tmp, "*.vue")
        joined = " ".join(self.rel(res))
        self.assertNotIn("node_modules", joined)

    def test_non_source_extension_is_findable(self):
        # _SOURCE_EXTS 在这里故意不生效：找 Dockerfile / *.yml 正是它的用途
        self.assertIn("Dockerfile", self.rel(code_intel.glob_files(self.tmp, "Dockerfile")))
        self.assertIn(".github/workflows/harness.yml",
                      self.rel(code_intel.glob_files(self.tmp, "*.yml")))

    def test_path_pattern_with_double_star(self):
        res = code_intel.glob_files(self.tmp, "frontend/src/**/*.ts")
        self.assertEqual(sorted(self.rel(res)),
                         ["frontend/src/workbench/liveAudioControl.ts",
                          "frontend/src/workbench/liveVision.ts"])

    def test_leading_double_star_prefix_equals_basename_search(self):
        self.assertEqual(self.rel(code_intel.glob_files(self.tmp, "**/live*.ts")),
                         self.rel(code_intel.glob_files(self.tmp, "live*.ts")))

    def test_scope_limits_search(self):
        res = code_intel.glob_files(self.tmp, "*.ts", scope="frontend/src/workbench")
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(sorted(self.rel(res)),
                         ["frontend/src/workbench/liveAudioControl.ts",
                          "frontend/src/workbench/liveVision.ts"])
        self.assertEqual(res["scope"], "frontend/src/workbench")

    def test_lowercase_pattern_is_case_insensitive(self):
        self.assertIn("frontend/src/views/Foo.vue",
                      self.rel(code_intel.glob_files(self.tmp, "foo.vue")))

    def test_pattern_with_uppercase_is_exact(self):
        self.assertEqual(code_intel.glob_files(self.tmp, "Foo.vue")["count"], 1)
        self.assertEqual(code_intel.glob_files(self.tmp, "foo.Vue")["count"], 0)

    def test_results_are_shallow_first(self):
        self.write("a/b/c/d/deep.py")
        res = code_intel.glob_files(self.tmp, "*.py")
        depths = [row.count("/") for row in res["files"]]
        self.assertEqual(depths, sorted(depths))

    def test_limit_truncates_and_reports(self):
        res = code_intel.glob_files(self.tmp, "*.ts", limit=1)
        self.assertEqual(res["count"], 2)
        self.assertEqual(len(res["files"]), 1)
        self.assertTrue(res["truncated"])

    def test_empty_pattern_rejected(self):
        res = code_intel.glob_files(self.tmp, "   ")
        self.assertFalse(res["ok"])
        self.assertIn("pattern", res["error"])

    def test_control_char_pattern_rejected(self):
        self.assertFalse(code_intel.glob_files(self.tmp, "*.py\nlog --all")["ok"])

    def test_scope_escape_rejected(self):
        res = code_intel.glob_files(self.tmp, "*", scope="../")
        self.assertFalse(res["ok"])
        self.assertIn("越界", res["error"])

    def test_flag_shaped_scope_rejected(self):
        self.assertFalse(code_intel.glob_files(self.tmp, "*", scope="-rf")["ok"])

    def test_missing_scope_dir_reports(self):
        self.assertFalse(code_intel.glob_files(self.tmp, "*", scope="nope")["ok"])

    def test_missing_root_reports(self):
        self.assertFalse(code_intel.glob_files("/no/such/root", "*.py")["ok"])

    def test_render_shapes(self):
        hit = code_intel.render_glob(code_intel.glob_files(self.tmp, "*.vue"))
        self.assertIn("命中 1 个", hit)
        self.assertIn("frontend/src/views/Foo.vue", hit)
        miss = code_intel.render_glob(code_intel.glob_files(self.tmp, "*.nope"))
        self.assertIn("没有命中", miss)
        self.assertIn("dev_glob 失败", code_intel.render_glob(
            code_intel.glob_files(self.tmp, "")))


# --------------------------------------------------------------------------- #
# git_log_preview: 只读历史（真仓库）
# --------------------------------------------------------------------------- #

@unittest.skipUnless(GIT_OK, "git 不可用")
class GitLogCases(_TmpRoot):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("config", "user.name", "Committer")
        self.git("config", "user.email", "c@example.com")

    def git(self, *args):
        proc = subprocess.run(["git", *args], cwd=self.tmp, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def commit(self, rel, subject, author="Alice <alice@example.com>", date="2026-01-02T00:00:00"):
        self.write(rel, subject + "\n")
        self.git("add", rel)
        self.git("commit", "-q", f"--author={author}", f"--date={date}", "-m", subject)

    def test_log_subcommand_is_readonly_allowed(self):
        self.assertIn("log", code_intel._GIT_ALLOWED)
        rep = code_intel._run_git(["log", "--oneline"], cwd=self.tmp)
        self.assertNotIn("白名单", rep.get("error") or "")

    def test_writes_are_still_refused(self):
        rep = code_intel._run_git(["commit", "-m", "x"], cwd=self.tmp)
        self.assertFalse(rep.get("ok", True))
        self.assertIn("白名单", rep["error"])

    def test_entries_newest_first_with_author_and_subject(self):
        self.commit("a.py", "first")
        self.commit("b.py", "second", date="2026-01-03T00:00:00")
        res = code_intel.git_log_preview(self.tmp)
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual([row["subject"] for row in res["entries"]], ["second", "first"])
        self.assertEqual(res["entries"][0]["author"], "Alice")
        self.assertEqual(res["entries"][0]["date"], "2026-01-03")
        self.assertTrue(res["head"])

    def test_paths_filter_limits_history(self):
        self.commit("a.py", "touch a")
        self.commit("b.py", "touch b")
        res = code_intel.git_log_preview(self.tmp, paths=["a.py"])
        self.assertEqual([row["subject"] for row in res["entries"]], ["touch a"])

    def test_author_filter(self):
        self.commit("a.py", "by alice")
        self.commit("b.py", "by bob", author="Bob <bob@example.com>")
        res = code_intel.git_log_preview(self.tmp, author="Bob")
        self.assertEqual([row["subject"] for row in res["entries"]], ["by bob"])

    def test_at_single_commit(self):
        self.commit("a.py", "one")
        self.commit("b.py", "two")
        head = self.git("rev-parse", "--short", "HEAD").strip()
        res = code_intel.git_log_preview(self.tmp, at=head)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["entries"][0]["subject"], "two")

    def test_files_listing(self):
        self.commit("nested/deep.py", "add deep file")
        res = code_intel.git_log_preview(self.tmp, files=True)
        self.assertEqual(res["entries"][0]["files"], ["nested/deep.py"])

    def test_files_listing_keeps_records_separate(self):
        # --name-only 的文件名跟在提交行之后；记录分隔符一旦被当成换行，
        # 第二条提交就会被吞成第一条的「文件名」。
        self.commit("a.py", "alpha")
        self.commit("b.py", "beta")
        res = code_intel.git_log_preview(self.tmp, files=True)
        self.assertEqual([row["subject"] for row in res["entries"]], ["beta", "alpha"])
        self.assertEqual([row["files"] for row in res["entries"]], [["b.py"], ["a.py"]])

    def test_limit_marks_truncated(self):
        for i in range(3):
            self.commit(f"f{i}.py", f"c{i}", date=f"2026-01-0{i + 1}T00:00:00")
        res = code_intel.git_log_preview(self.tmp, limit=2)
        self.assertEqual(res["count"], 2)
        self.assertTrue(res["truncated"])

    def test_empty_repo_is_reported_as_no_history_not_failure(self):
        res = code_intel.git_log_preview(self.tmp)
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["count"], 0)
        self.assertIn("还没有任何提交", " ".join(res["notes"]))
        self.assertIn("没有记录", code_intel.render_git_log(res))

    def test_nonexistent_ref_is_a_real_error(self):
        self.commit("a.py", "one")
        res = code_intel.git_log_preview(self.tmp, at="deadbeefdeadbeef")
        self.assertFalse(res["ok"])
        self.assertIn("git log 失败", res["error"])
        # 诊断行会被噪声过滤剥掉，但报错必须保留 git 的原话而不是「无输出」
        self.assertNotIn("无输出", res["error"])

    def test_at_range_still_expands(self):
        for i in range(3):
            self.commit(f"f{i}.py", f"c{i}", date=f"2026-01-0{i + 1}T00:00:00")
        res = code_intel.git_log_preview(self.tmp, at="HEAD~2..HEAD")
        self.assertEqual([row["subject"] for row in res["entries"]], ["c2", "c1"])

    def test_flag_injection_in_scalar_rejected(self):
        for field in ("since", "until", "author", "at"):
            res = code_intel.git_log_preview(self.tmp, **{field: "--output=/dev/stdout"})
            self.assertFalse(res["ok"], field)
            self.assertIn("参数注入", res["error"])

    def test_path_escape_rejected(self):
        self.commit("a.py", "one")
        res = code_intel.git_log_preview(self.tmp, paths=["../outside"])
        self.assertFalse(res["ok"])
        self.assertIn("越界", res["error"])

    def test_non_repo_reports_readably(self):
        plain = tempfile.mkdtemp(prefix="docmind_nogit_")
        try:
            res = code_intel.git_log_preview(plain)
            self.assertFalse(res["ok"])
            self.assertTrue(res["error"])
        finally:
            shutil.rmtree(plain, ignore_errors=True)

    def test_render_lists_commits(self):
        self.commit("nested/deep.py", "alpha subject")
        text = code_intel.render_git_log(code_intel.git_log_preview(self.tmp, files=True))
        self.assertIn("alpha subject", text)
        self.assertIn("nested/deep.py", text)
        self.assertIn("当前 HEAD=", text)


# --------------------------------------------------------------------------- #
# 工具层：输入解析与未配置根目录
# --------------------------------------------------------------------------- #

class ToolSurfaceCases(_TmpRoot):
    def test_registration_and_prompt(self):
        names = {row["function"]["name"] for row in tools.tool_schemas()}
        for name in ("dev_glob", "dev_git_log"):
            self.assertIn(name, tools.TOOLS)
            self.assertTrue(tools.TOOLS[name]["description"])
            self.assertIn(name, names)
        prompt = open(os.path.join(ROOT, "agent.py"), encoding="utf-8").read()
        for name in ("dev_glob", "dev_git_log"):
            self.assertIn("- " + name + "(", prompt)

    def test_missing_code_root_is_reported(self):
        config.set_runtime("code_root", "")
        # 工具现在说**可行动的那一半**（缺什么 + 怎么补），不再只说「没配置」。
        self.assertIn("尚未选择项目，无法定位代码库", tools.dev_glob(""))
        # 工具现在说**可行动的那一半**（缺什么 + 怎么补），不再只说「没配置」。
        self.assertIn("尚未选择项目，无法定位代码库", tools.dev_git_log(""))

    def test_dev_glob_accepts_bare_pattern(self):
        self.use_root()
        self.write("src/a/one.ts")
        out = tools.dev_glob("*.ts")
        self.assertIn("src/a/one.ts", out)

    def test_dev_glob_parses_keyed_input(self):
        self.use_root()
        self.write("src/a/one.ts")
        self.write("src/b/two.ts")
        out = tools.dev_glob("pattern: *.ts\nscope: src/a\nlimit: 10")
        self.assertIn("src/a/one.ts", out)
        self.assertNotIn("two.ts", out)

    def test_dev_glob_bad_limit_falls_back(self):
        self.use_root()
        self.write("one.ts")
        self.assertIn("one.ts", tools.dev_glob("pattern: *.ts\nlimit: not-a-number"))

    @unittest.skipUnless(GIT_OK, "git 不可用")
    def test_dev_git_log_bare_path_means_paths_filter(self):
        self.use_root()
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=30)
        for args in (["config", "user.name", "C"], ["config", "user.email", "c@e.com"]):
            subprocess.run(["git", *args], cwd=self.tmp, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
        self.write("a.py")
        self.write("b.py")
        subprocess.run(["git", "add", "a.py"], cwd=self.tmp, capture_output=True, timeout=30)
        subprocess.run(["git", "commit", "-q", "-m", "only a"], cwd=self.tmp,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=30)
        out = tools.dev_git_log("paths: a.py")
        self.assertIn("only a", out)
        self.assertNotIn("b.py", out)


if __name__ == "__main__":
    unittest.main()
