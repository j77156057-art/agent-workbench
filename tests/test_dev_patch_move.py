"""Wave5 回归：dev_patch 的原子性与护栏、dev_move 的历史保留。

这里最要紧的性质是【全成或全不成】：多文件补丁里第二个文件对不上时，第一个文件
必须一个字都没动。其次是上下文不许模糊匹配、路径不许越界、删除不许借补丁发生。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import tools  # noqa: E402
from agent_runtime import patch_apply  # noqa: E402


def _git_available():
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True,
                              timeout=15)
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


GIT_OK = _git_available()

ORIG_A = "line one\nline two\nline three\n"
PATCH_A = """--- a/a.txt
+++ b/a.txt
@@ -1,3 +1,3 @@
 line one
-line two
+line two changed
 line three
"""
PATCH_CREATE = """--- /dev/null
+++ b/new/mod.py
@@ -0,0 +1,2 @@
+def hello():
+    return 1
"""
PY_ORIG = "VALUE = 1\nprint(VALUE)\n"
PY_PATCH = """--- a/pkg/ok.py
+++ b/pkg/ok.py
@@ -1,2 +1,2 @@
 VALUE = 1
-print(VALUE)
+print(VALUE + 1)
"""
PY_BROKEN_PATCH = """--- a/pkg/ok.py
+++ b/pkg/ok.py
@@ -1,2 +1,2 @@
 VALUE = 1
-print(VALUE)
+def broken(:
"""


class _TmpRoot(unittest.TestCase):
    def setUp(self):
        self._prev = config.get_runtime("code_root")
        self.tmp = tempfile.mkdtemp(prefix="docmind_patch_")
        config.set_runtime("code_root", self.tmp)

    def tearDown(self):
        config.set_runtime("code_root", self._prev)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, content):
        full = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        return full

    def read(self, rel):
        with open(os.path.join(self.tmp, rel), encoding="utf-8") as fh:
            return fh.read()

    def exists(self, rel):
        return os.path.exists(os.path.join(self.tmp, rel))


class ParseCases(_TmpRoot):
    def test_parse_modify_patch(self):
        patches, errors = patch_apply.parse_unified(PATCH_A, self.tmp)
        self.assertEqual(errors, [])
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["path"], "a.txt")
        self.assertFalse(patches[0]["created"])
        self.assertEqual(patches[0]["hunks"][0]["start_old"], 1)

    def test_parse_strips_a_and_b_prefixes(self):
        patches, errors = patch_apply.parse_unified(PATCH_A, self.tmp)
        self.assertEqual(errors, [])
        self.assertEqual(patches[0]["path"], "a.txt")

    def test_deletion_is_refused(self):
        text = "--- a/gone.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n"
        patches, errors = patch_apply.parse_unified(text, self.tmp)
        self.assertFalse(patches)
        self.assertTrue(any("删除" in e for e in errors))

    def test_escape_paths_refused(self):
        for bad in ("../evil.py", "a/../../evil.py", "/etc/passwd"):
            text = f"--- a/x\n+++ b/{bad}\n@@ -1 +1 @@\n-a\n+b\n"
            patches, errors = patch_apply.parse_unified(text, self.tmp)
            self.assertFalse(patches, bad)
            self.assertTrue(any("越界" in e or "相对路径" in e for e in errors), bad)

    def test_hunk_without_header(self):
        patches, errors = patch_apply.parse_unified("@@ -1 +1 @@\n-a\n+b\n", self.tmp)
        self.assertFalse(patches)
        self.assertTrue(errors)

    def test_patch_without_hunks(self):
        patches, errors = patch_apply.parse_unified("--- a/a.py\n+++ b/a.py\n", self.tmp)
        self.assertFalse(patches)
        self.assertTrue(any("hunk" in e for e in errors))


class ApplyCases(_TmpRoot):
    def test_apply_modify(self):
        self.write("a.txt", ORIG_A)
        res = patch_apply.apply_patch(self.tmp, PATCH_A)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["changes"][0]["added"], 1)
        self.assertEqual(res["changes"][0]["removed"], 1)
        self.assertEqual(self.read("a.txt"), "line one\nline two changed\nline three\n")

    def test_apply_python_patch_stays_valid(self):
        self.write("pkg/ok.py", PY_ORIG)
        res = patch_apply.apply_patch(self.tmp, PY_PATCH)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(self.read("pkg/ok.py"), "VALUE = 1\nprint(VALUE + 1)\n")

    def test_apply_create(self):
        res = patch_apply.apply_patch(self.tmp, PATCH_CREATE)
        self.assertTrue(res["ok"], res["errors"])
        self.assertTrue(self.exists("new/mod.py"))
        self.assertIn("def hello", self.read("new/mod.py"))
        self.assertTrue(res["changes"][0]["created"])

    def test_created_file_must_not_already_exist(self):
        self.write("new/mod.py", "existing\n")
        res = patch_apply.apply_patch(self.tmp, PATCH_CREATE)
        self.assertFalse(res["ok"])
        self.assertTrue(any("已存在" in e for e in res["errors"]))

    def test_modify_requires_existing_file(self):
        res = patch_apply.apply_patch(self.tmp, PATCH_A)
        self.assertFalse(res["ok"])
        self.assertTrue(any("不存在" in e for e in res["errors"]))

    def test_context_must_match_exactly(self):
        self.write("a.txt", "line one\nline TWO\nline three\n")
        res = patch_apply.apply_patch(self.tmp, PATCH_A)
        self.assertFalse(res["ok"])
        self.assertIn("对不上", res["errors"][0])
        self.assertEqual(self.read("a.txt"), "line one\nline TWO\nline three\n")

    def test_shifted_hunk_still_applies_and_says_so(self):
        self.write("a.txt", "header line\nanother header\n\n" + ORIG_A)
        res = patch_apply.apply_patch(self.tmp, PATCH_A)
        self.assertTrue(res["ok"], res["errors"])
        self.assertIn("line two changed", self.read("a.txt"))
        self.assertTrue(any("实际落在" in n for n in res["notes"]))

    def test_multi_file_patch_is_atomic(self):
        # 关键性质：第二个文件上下文对不上时，第一个文件必须一个字都没改
        self.write("a.txt", ORIG_A)
        self.write("b.txt", "totally\ndifferent\ncontent\nhere\n")
        text = PATCH_A + """--- a/b.txt
+++ b/b.txt
@@ -1,3 +1,3 @@
 totally
-missing line
+whatever
 content
"""
        before = self.read("a.txt")
        res = patch_apply.apply_patch(self.tmp, text)
        self.assertFalse(res["ok"])
        self.assertEqual(self.read("a.txt"), before)
        self.assertEqual(res["written"], [])
        self.assertTrue(any("没有半成品" in n for n in res["notes"]))

    def test_python_syntax_error_rolls_the_whole_patch_back(self):
        self.write("pkg/ok.py", PY_ORIG)
        res = patch_apply.apply_patch(self.tmp, PY_BROKEN_PATCH)
        self.assertFalse(res["ok"])
        self.assertTrue(any("语法不通过" in e for e in res["errors"]))
        self.assertEqual(self.read("pkg/ok.py"), PY_ORIG)

    def test_pure_insertion_goes_after_the_declared_line(self):
        # `@@ -2,0` = 插在第 2 行之后；早先按 -1 处理会插到前面（off-by-one）
        self.write("a.txt", "one\ntwo\nthree\n")
        res = patch_apply.apply_patch(self.tmp, "--- a/a.txt\n+++ b/a.txt\n@@ -2,0 +3,2 @@\n+extra one\n+extra two\n")
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(self.read("a.txt"), "one\ntwo\nextra one\nextra two\nthree\n")

    def test_pure_insertion_at_end_of_file(self):
        self.write("a.txt", "one\ntwo\nthree\n")
        res = patch_apply.apply_patch(self.tmp, "--- a/a.txt\n+++ b/a.txt\n@@ -3,0 +4 @@\n+tail\n")
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(self.read("a.txt"), "one\ntwo\nthree\ntail\n")

    def test_dry_run_writes_nothing(self):
        self.write("a.txt", ORIG_A)
        res = patch_apply.apply_patch(self.tmp, PATCH_A, dry_run=True)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(self.read("a.txt"), ORIG_A)
        self.assertEqual(res["written"], [])
        self.assertTrue(any("dry_run" in n for n in res["notes"]))

    def test_oversized_target_refused(self):
        self.write("big.txt", "x" * 5000)
        text = "--- a/big.txt\n+++ b/big.txt\n@@ -1 +1,2 @@\n-%s\n+z\n+z2\n" % ("x" * 5000)
        with mock.patch.object(patch_apply, "MAX_TARGET_BYTES", 1000):
            res = patch_apply.apply_patch(self.tmp, text)
        self.assertFalse(res["ok"])
        self.assertTrue(any("上限" in e for e in res["errors"]))

    def test_created_file_size_cap(self):
        body = "\n".join("line %d" % i for i in range(2000))
        text = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,%d @@\n" % 2000 + \
            "".join("+%s\n" % line for line in body.splitlines())
        with mock.patch.object(patch_apply, "MAX_NEW_FILE_BYTES", 1000):
            res = patch_apply.apply_patch(self.tmp, text)
        self.assertFalse(res["ok"])
        self.assertTrue(any("新建文件超过" in e for e in res["errors"]))
        self.assertFalse(self.exists("new.txt"))

    def test_patch_payload_cap(self):
        huge = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n" * 200
        with mock.patch.object(patch_apply, "MAX_PATCH_BYTES", 500):
            res = patch_apply.apply_patch(self.tmp, huge)
        self.assertFalse(res["ok"])
        self.assertTrue(any("拆" in e for e in res["errors"]))

    def test_too_many_files_refused(self):
        text = "".join("--- /dev/null\n+++ b/f%d.txt\n@@ -0,0 +1 @@\n+x\n" % i
                       for i in range(patch_apply.MAX_FILES + 1))
        res = patch_apply.apply_patch(self.tmp, text)
        self.assertFalse(res["ok"])
        self.assertTrue(any("拆分" in e for e in res["errors"]))

    def test_missing_root_reported(self):
        res = patch_apply.apply_patch("", PATCH_A)
        self.assertFalse(res["ok"])

    def test_render_shapes(self):
        self.write("a.txt", ORIG_A)
        text = patch_apply.render_patch(patch_apply.apply_patch(self.tmp, PATCH_A))
        self.assertIn("补丁已应用", text)
        self.assertIn("a.txt", text)
        refused = patch_apply.render_patch({"ok": False, "errors": ["a.py：上下文对不上"],
                                            "notes": ["整体未写入（原子性）：树上没有半成品。"]})
        self.assertIn("补丁被拒绝", refused)
        self.assertIn("没有半成品", refused)


@unittest.skipUnless(GIT_OK, "git 不可用")
class MoveCases(_TmpRoot):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("config", "user.name", "T")
        self.git("config", "user.email", "t@example.com")

    def git(self, *args, cwd=None):
        proc = subprocess.run(["git", *args], cwd=cwd or self.tmp, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def status(self):
        return self.git("status", "--porcelain")

    def test_tracked_file_moves_via_git_mv_and_keeps_history(self):
        self.write("pkg/old.py", "VALUE = 1\n")
        self.git("add", "pkg/old.py")
        self.git("commit", "-q", "-m", "add old")
        res = patch_apply.move_path(self.tmp, "pkg/old.py", "pkg/sub/new.py")
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["engine"], "git mv")
        self.assertFalse(self.exists("pkg/old.py"))
        self.assertTrue(self.exists("pkg/sub/new.py"))
        self.assertIn("R", self.status())
        # git mv 只暂存重命名，不提交——提交之后 --follow 才能追到改名前那笔
        self.git("commit", "-q", "-m", "rename it")
        self.assertIn("add old", self.git("log", "--oneline", "--follow",
                                          "--", "pkg/sub/new.py"))

    def test_untracked_file_falls_back_to_filesystem(self):
        self.write("free.txt", "hi\n")
        res = patch_apply.move_path(self.tmp, "free.txt", "moved/free.txt")
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["engine"], "filesystem")
        self.assertTrue(self.exists("moved/free.txt"))
        self.assertTrue(any("未被跟踪" in n for n in res["notes"]))

    def test_existing_target_is_never_overwritten(self):
        self.write("a.txt", "1\n")
        self.write("b.txt", "2\n")
        res = patch_apply.move_path(self.tmp, "a.txt", "b.txt")
        self.assertFalse(res["ok"])
        self.assertIn("已存在", res["error"])
        self.assertEqual(self.read("b.txt"), "2\n")

    def test_missing_source_refused(self):
        res = patch_apply.move_path(self.tmp, "nope.txt", "x.txt")
        self.assertFalse(res["ok"])
        self.assertIn("不存在", res["error"])

    def test_paths_stay_inside_root(self):
        self.write("a.txt", "1\n")
        outside = os.path.join(tempfile.gettempdir(), "docmind_escape.txt")
        try:
            res = patch_apply.move_path(self.tmp, "../docmind_escape.txt", "a.txt")
            self.assertFalse(res["ok"])
            res2 = patch_apply.move_path(self.tmp, "a.txt", outside)
            self.assertFalse(res2["ok"])
        finally:
            if os.path.exists(outside):
                os.remove(outside)

    def test_directory_source_refused(self):
        os.makedirs(os.path.join(self.tmp, "dir"), exist_ok=True)
        res = patch_apply.move_path(self.tmp, "dir", "other")
        self.assertFalse(res["ok"])

    def test_move_render(self):
        self.write("a.txt", "1\n")
        text = patch_apply.render_move(patch_apply.move_path(self.tmp, "a.txt", "b.txt"))
        self.assertIn("已移动", text)
        bad = patch_apply.render_move({"ok": False, "error": "目标已存在"})
        self.assertIn("移动失败", bad)


class ToolLayerCases(_TmpRoot):
    def test_registration_and_prompt(self):
        names = {row["function"]["name"] for row in tools.tool_schemas()}
        self.assertIn("dev_patch", names)
        self.assertIn("dev_move", names)
        prompt = open(os.path.join(ROOT, "agent.py"), encoding="utf-8").read()
        self.assertIn("- dev_patch:", prompt)
        self.assertIn("- dev_move(from, to", prompt)

    def test_missing_code_root(self):
        config.set_runtime("code_root", "")
        # 工具现在说**可行动的那一半**（缺什么 + 怎么补），不再只说「没配置」。
        self.assertIn("尚未选择项目，无法定位代码库", tools.dev_patch(PATCH_A))
        # 工具现在说**可行动的那一半**（缺什么 + 怎么补），不再只说「没配置」。
        self.assertIn("尚未选择项目，无法定位代码库", tools.dev_move("from: a\n to: b"))

    def test_dev_patch_dry_run_first_line(self):
        self.write("a.txt", ORIG_A)
        out = tools.dev_patch("dry_run: true\n" + PATCH_A)
        self.assertIn("dry_run", out)
        self.assertEqual(self.read("a.txt"), ORIG_A)

    def test_dev_patch_applies_and_reports(self):
        self.write("a.txt", ORIG_A)
        out = tools.dev_patch(PATCH_A)
        self.assertIn("已应用", out)
        self.assertIn("line two changed", self.read("a.txt"))

    def test_dev_move_needs_both_lines(self):
        self.write("a.txt", "1\n")
        out = tools.dev_move("from: a.txt")
        self.assertIn("from:", out)
        self.assertIn("to:", out)
        self.assertTrue(self.exists("a.txt"))

    def test_dev_move_end_to_end_text(self):
        self.write("a.txt", "1\n")
        out = tools.dev_move("from: a.txt\nto: sub/b.txt")
        self.assertIn("已移动", out)
        self.assertTrue(self.exists("sub/b.txt"))


if __name__ == "__main__":
    unittest.main()
