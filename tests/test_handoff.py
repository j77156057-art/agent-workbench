"""dev_handoff 回归：交付这段要能往前走一步，又不能越过人的决定。

`dev_propose` 出「补丁 + 草稿」就停了，人还得自己决定分支名、自己写 commit message、
自己确认「这些改动里有没有混进别人的在途 WIP」。本模块补的是**决策材料**，所以要钉住
的是两件事同时成立：**材料有用**，且**它绝不自己动手**。

第二件事是本文件的主要防线，因为违反它的后果最重（本仓 `b828d10` 就是这么连累 HEAD
的：有人用 `git commit -- <路径>`，把别人 ~457 行在途 WIP 扫进自己的提交，而 HEAD
单独检出会 `import agent` 失败）。所以：

1. **只读 git** —— 守它：`it_only_ever_runs_read_only_git_subcommands`。源码级断言：
   模块里出现的 git 子命令必须全在 `code_intel._GIT_ALLOWED`（diff/log/rev-parse/status）。
2. **不 add / 不 commit / 不 push / 不切分支** —— 守它：`the_suggested_steps_are_not_executed`、
   `no_write_command_appears_in_the_rendered_output`。
3. **共享文件必须被点名** —— 那是 `git commit -- <路径>` 出事的高发区。
4. **未跟踪文件不得被说成「已交付」** —— 它们不在 diff 里。
5. **不是 git 工作树时不得输出「一切就绪」** —— 守它：`a_non_repo_says_so_instead_of_ready`。
6. **标题前缀不重复拼**（`chore(harness): feat: …`、`fix/harness-fix-x`）——
   守它：`an_existing_conventional_prefix_is_not_doubled`。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_runtime import code_intel, handoff  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                           "-c", "commit.gpgsign=false"] + list(args),
                          cwd=repo, capture_output=True, text=True)


class SlugTests(unittest.TestCase):

    def test_ascii_titles_become_readable_slugs(self):
        self.assertEqual(handoff.slugify("Fix the broken thing"), "fix-the-broken-thing")

    def test_a_cjk_title_never_becomes_empty(self):
        """本仓提交标题大量是中文。空 slug 会产出 `feat/` 这种没法看的分支名。"""
        for title in ("修好打断后不闭嘴的问题", "门禁题", "交付段补齐"):
            self.assertTrue(handoff.slugify(title), title)

    def test_punctuation_only_falls_back(self):
        self.assertTrue(handoff.slugify("---"))

    def test_slugs_are_bounded(self):
        self.assertLessEqual(len(handoff.slugify("word " * 200)), 60)


class ScopeAndTypeTests(unittest.TestCase):

    def test_the_deepest_matching_directory_wins(self):
        self.assertEqual(handoff.suggest_scope(["agent_runtime/x.py"]), "harness")
        self.assertEqual(handoff.suggest_scope(["tests/test_x.py"]), "tests")
        self.assertEqual(handoff.suggest_scope(["docs/a.md", "tests/b.py"]), "tests")

    def test_a_single_nested_file_gives_a_readable_scope(self):
        self.assertEqual(handoff.suggest_scope(["pkg/deep/thing.py"]), "thing")

    def test_type_follows_the_change_not_the_filename_alone(self):
        self.assertEqual(handoff.suggest_type(["tests/test_x.py"]), "test")
        self.assertEqual(handoff.suggest_type(["docs/a.md"]), "docs")
        self.assertEqual(handoff.suggest_type(["a.py"], "fix: broken"), "fix")
        self.assertEqual(handoff.suggest_type(["a.py"], "add support for x"), "feat")
        self.assertEqual(handoff.suggest_type(["a.py"], "重构这一段"), "refactor")
        self.assertEqual(handoff.suggest_type([".github/workflows/ci.yml"]), "ci")

    def test_an_undecidable_change_falls_back_to_chore(self):
        self.assertEqual(handoff.suggest_type(["random.txt"]), "chore")

    def test_the_type_is_always_inside_the_agreed_set(self):
        for files in (["a.py"], ["tests/b.py"], ["docs/c.md"], ["frontend/src/d.ts"]):
            self.assertIn(handoff.suggest_type(files), handoff.TYPES, files)


class ConventionTests(unittest.TestCase):

    def test_conventional_history_is_detected(self):
        self.assertEqual(handoff._convention(["feat: a", "fix: b", "chore: c"]), "conventional")

    def test_freeform_history_is_detected(self):
        titles = ["修好了门禁", "加了个工具", "Wave20 的修复", "再改一次"]
        self.assertEqual(handoff._convention(titles), "freeform")

    def test_no_history_is_unknown_not_conventional(self):
        self.assertEqual(handoff._convention([]), "unknown")


class RepoCase(unittest.TestCase):

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="handoff-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        git(self.repo, "init", "-q")
        os.makedirs(os.path.join(self.repo, "agent_runtime"), exist_ok=True)
        os.makedirs(os.path.join(self.repo, "tests"), exist_ok=True)
        self.write("agent_runtime/thing.py", "X = 1\n")
        self.write("tests/test_thing.py", "def test_x():\n    assert True\n")
        self.write("README.md", "# hi\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "feat: init")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "fix: second")
        # 造出真实的改动：一处已跟踪文件的修改 + 一个未跟踪的新文件
        self.write("agent_runtime/thing.py", "X = 2\n")
        self.write("agent_runtime/brand_new.py", "Y = 1\n")

    def write(self, rel, text):
        path = os.path.join(self.repo, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path


class AnalyzeTests(RepoCase):

    def test_it_reports_the_files_this_commit_would_carry(self):
        result = handoff.analyze(self.repo)
        self.assertTrue(result["ok"], result["error"])
        self.assertIn("agent_runtime/thing.py", result["changed"])
        self.assertTrue(result["head"])

    def test_untracked_files_are_listed_and_flagged_as_not_included(self):
        """未跟踪文件不在 diff 里——把它们算进「这次提交会包含」就是骗人。"""
        result = handoff.analyze(self.repo)
        self.assertIn("agent_runtime/brand_new.py", result["untracked"])
        self.assertNotIn("agent_runtime/brand_new.py", result["changed"])
        text = handoff.render(result)
        self.assertIn("不会被这次提交带上", text)

    def test_shared_files_are_called_out_by_name(self):
        """非 tests/ 非 .md 的主干文件要单独点名：那是 `git commit -- <路径>` 出事的
        高发区（把别人的在途 WIP 一起扫进去）。"""
        result = handoff.analyze(self.repo)
        self.assertIn("agent_runtime/thing.py", result["shared_files"])
        self.assertNotIn("tests/test_thing.py", result["shared_files"])
        self.assertIn("b828d10", handoff.render(result))

    def test_it_suggests_a_branch_and_a_message(self):
        result = handoff.analyze(self.repo, title="修好门禁")
        self.assertTrue(result["branch_suggestion"])
        self.assertTrue(result["message_suggestion"])
        self.assertEqual(result["convention"], "conventional")
        self.assertTrue(result["message_suggestion"].startswith(("fix(", "feat(", "chore(")))

    def test_an_existing_conventional_prefix_is_not_doubled(self):
        """实测踩到：`feat: 交付段` 不该变成 `chore(harness): feat: 交付段`，
        分支名也不该变成 `fix/harness-fix-x`（type 已经在第一段了）。"""
        result = handoff.analyze(self.repo, title="feat: 交付段补齐")
        message = result["message_suggestion"]
        # 前缀被剥掉后重新按仓库约定拼一次，所以整条消息里只应有一个前缀。
        self.assertEqual(message.count(":"), 1, message)
        self.assertNotIn("feat: 交付段", message)
        self.assertTrue(message.endswith("交付段补齐"), message)
        self.assertNotIn("harness-feat-", result["branch_suggestion"])

    def test_a_freeform_title_keeps_its_own_prefix_out_of_the_branch_name(self):
        result = handoff.analyze(self.repo, title="fix: 门禁题")
        self.assertNotIn("harness-fix-", result["branch_suggestion"])

    def test_a_narrow_scope_filters_the_file_list(self):
        result = handoff.analyze(self.repo, files=["agent_runtime/thing.py"])
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["changed"], ["agent_runtime/thing.py"])

    def test_a_path_outside_the_root_is_refused(self):
        result = handoff.analyze(self.repo, files=["../outside.py"])
        self.assertFalse(result["ok"])
        self.assertIn("拒绝", result["error"])

    def test_an_option_shaped_path_is_refused_before_anything_runs(self):
        result = handoff.analyze(self.repo, files=["--upload-pack=touch pwned"])
        self.assertFalse(result["ok"])

    def test_too_broad_a_change_says_to_split_it(self):
        """已跟踪的文件被改动得很宽时，要说「该拆成几个提交」——
        没人能 review 一个 40 文件的提交。"""
        for index in range(45):
            self.write("agent_runtime/mod%02d.py" % index, "Z = %d  # touched\n" % index)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "feat: wide change")
        for index in range(45):
            self.write("agent_runtime/mod%02d.py" % index, "Z = %d  # edited\n" % index)
        result = handoff.analyze(self.repo)
        self.assertTrue(result["too_broad"], len(result["changed"]))
        self.assertIn("拆成几个提交", handoff.render(result))

    def test_a_non_repo_says_so_instead_of_ready(self):
        plain = tempfile.mkdtemp(prefix="notarepo-")
        self.addCleanup(shutil.rmtree, plain, ignore_errors=True)
        result = handoff.analyze(plain)
        self.assertFalse(result["ok"])
        self.assertIn("不是 git 工作树", result["error"])
        self.assertNotIn("建议分支名", handoff.render(result))

    def test_a_clean_tree_says_there_is_nothing_to_commit(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "feat: all in")
        result = handoff.analyze(self.repo)
        self.assertFalse(result["ok"])
        self.assertIn("没有已跟踪的改动", result["error"])


class RefusalTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="handoff-empty-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_a_missing_root_is_refused(self):
        result = handoff.analyze(os.path.join(self.dir, "nope"))
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])

    def test_an_empty_root_argument_is_refused(self):
        self.assertFalse(handoff.analyze("")["ok"])


class ReadOnlyGuaranteeTests(RepoCase):
    """本文件最重要的一节：它只许看，不许动。

    用自带的临时仓库而不是本仓：本仓的工作树是共享的（随时有别的 lane 在改），
    拿它的真实状态当断言依据，测试就会因为**别人**的改动而变红。
    """

    def test_it_only_ever_runs_read_only_git_subcommands(self):
        """源码级断言：**传给 `_run_git` 的**子命令必须全在只读白名单里。

        只看 `_run_git([...])` 的第一个元素，不做全文正则——那会把结果字典的键
        （"scope"、"notes"…）也当成子命令，然后报一堆假阳性，真正写错了反而被淹没。
        `diff` 走的是 `code_intel.git_diff_preview`（它内部自己过同一道白名单）。
        写错一个 `add` 或 `commit`，这里立刻红。
        """
        source = open(handoff.__file__, encoding="utf-8").read()
        direct = set(re.findall(r"_run_git\(\[\s*\"([a-z][a-z0-9-]*)\"", source))
        self.assertTrue(direct, "没找到任何 _run_git 调用——这个断言本身失效了")
        allowed = set(code_intel._GIT_ALLOWED)
        self.assertTrue(direct <= allowed, "越过了只读白名单：%s" % (direct - allowed))
        # 更强的一道：整个模块里不得出现任何写类子命令的字面量调用。
        writers = {"add", "commit", "push", "checkout", "switch", "reset", "merge",
                   "rebase", "stash", "clean", "branch", "tag", "worktree", "restore"}
        for name in writers:
            self.assertIsNone(re.search(r"_run_git\(\[\s*\"%s\"" % name, source),
                              "handoff 不得调用 git %s" % name)

    def test_the_rendered_steps_are_for_a_human_to_run_not_the_tool(self):
        result = handoff.analyze(self.repo, title="feat: something")
        text = handoff.render(result)
        self.assertIn("下一步（都由你执行）", text)
        self.assertIn("git add <路径>", text)

    def test_no_write_command_is_reported_as_having_run(self):
        """输出里提到写命令是**给人看的建议**，但不能出现「已提交」这种完成态。"""
        result = handoff.analyze(self.repo, title="feat: something")
        text = handoff.render(result)
        for claim in ("已提交", "已推送", "已切换到分支", "已创建分支"):
            self.assertNotIn(claim, text)

    def test_analyzing_does_not_change_the_repository(self):
        """跑一遍之后 HEAD、工作树、暂存区都必须原样——这是「只读」的字面意思。"""
        before_head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        before_status = git(self.repo, "status", "--porcelain").stdout
        handoff.analyze(self.repo, title="feat: x")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), before_head)
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, before_status)


class ToolWiringTests(unittest.TestCase):

    def setUp(self):
        import tools
        self.tools = tools

    def test_the_tool_is_registered(self):
        self.assertIn("dev_handoff", self.tools.TOOLS)
        self.assertTrue(callable(self.tools.TOOLS["dev_handoff"]["func"]))

    def test_the_tool_has_one_clean_catalog_line(self):
        import agent
        rows = [line for line in agent._SYSTEM_PROMPT_FULL.splitlines()
                if line.startswith("- dev_handoff")]
        self.assertEqual(len(rows), 1, "dev_handoff 在提示词目录里应当恰好一行")

    def test_a_missing_root_is_reported_by_the_tool(self):
        original = self.tools.CODE_ROOT
        self.tools.CODE_ROOT = ""
        try:
            out = self.tools.TOOLS["dev_handoff"]["func"]("")
        finally:
            self.tools.CODE_ROOT = original
        self.assertIn("尚未配置代码库根目录", out)


if __name__ == "__main__":
    unittest.main()
