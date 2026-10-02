"""P1 工具回归：dev_git_diff / dev_find_references / dev_apply_edits。

覆盖三件事：只读性护栏、AST 精确性（注释与字符串不误报）、批量编辑的原子性。
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


class _CodeRootCase(unittest.TestCase):
    """把 code_root 临时指向一个目录，用完恢复。"""

    def setUp(self):
        self._prev = config.get_runtime("code_root")
        self.tmp = tempfile.mkdtemp(prefix="docmind_codetools_")

    def tearDown(self):
        config.set_runtime("code_root", self._prev)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use_root(self):
        config.set_runtime("code_root", self.tmp)

    def write(self, rel, content):
        full = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        return full


# --------------------------------------------------------------------------- #
# code_intel: git 输出解析与护栏（无 git 依赖）
# --------------------------------------------------------------------------- #

class TestGitNoiseAndPorcelain(unittest.TestCase):
    def test_warning_lines_are_stripped(self):
        text = ("warning: could not open directory '.pytest_cache/': Permission denied\n"
                " M tools.py\n")
        self.assertEqual(code_intel._strip_git_noise(text), " M tools.py\n")

    def test_diff_body_keeps_legit_lines(self):
        text = "@@ -1 +1 @@\n-def foo():\n+def foo(x):\n context\n"
        self.assertEqual(code_intel._strip_git_noise(text), text)

    def test_porcelain_drops_diagnostic_shaped_lines(self):
        # 曾经的真实 bug：git warning 被当成 status 条目，解析出 "ning: ..." 这种文件
        text = ("warning: could not open directory '.pytest_cache/': Permission denied\n"
                " M tools.py\n"
                "?? new.py\n")
        entries = code_intel._parse_porcelain(text)
        self.assertEqual([p for _, p in entries], ["tools.py", "new.py"])

    def test_porcelain_classifies_staged_unstaged_untracked(self):
        text = "A  added.py\n M work.py\n?? new.py\n"
        entries = code_intel._parse_porcelain(text)
        self.assertEqual(entries[0][0], "A ")
        self.assertEqual(entries[1][0], " M")
        self.assertEqual(entries[2][0], "??")

    def test_porcelain_handles_rename_arrow(self):
        entries = code_intel._parse_porcelain("R  old.py -> new.py\n")
        self.assertEqual(entries[0][1], "new.py")


class TestPathGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docmind_guard_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_flag_injection_rejected(self):
        kept, err = code_intel._validate_paths(["--stat"], self.tmp, self.tmp)
        self.assertIsNone(kept)
        self.assertIn("命令行选项", err)

    def test_escape_rejected(self):
        outside = os.path.join(os.path.dirname(self.tmp), "outside.py")
        kept, err = code_intel._validate_paths([outside], self.tmp, self.tmp)
        self.assertIsNone(kept)
        self.assertIn("不在代码根目录", err)

    def test_in_root_path_becomes_repo_relative(self):
        with open(os.path.join(self.tmp, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        kept, err = code_intel._validate_paths(["a.py"], self.tmp, self.tmp)
        self.assertEqual(err, "")
        self.assertEqual(kept, ["a.py"])

    def test_git_subcommand_whitelist(self):
        rep = code_intel._run_git(["commit", "-m", "x"], cwd=self.tmp)
        self.assertFalse(rep.get("ok"))
        self.assertIn("只读白名单", rep.get("error") or "")


# --------------------------------------------------------------------------- #
# code_intel: 符号查找的 AST 精确性
# --------------------------------------------------------------------------- #

PY_SRC = '''\
def foo(x):
    # foo 出现在注释里，不应算引用
    label = "foo 出现在字符串里"
    return foo(x)
'''


class TestFindReferencesBody(_CodeRootCase):
    def test_ast_ignores_comments_and_literals(self):
        self.write("m.py", PY_SRC)
        res = code_intel.find_references(self.tmp, "foo")
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["definitions"]), 1)
        self.assertEqual(res["definitions"][0]["line"], 1)
        # 只有 `return foo(x)` 这一处真引用（第 4 行）
        self.assertEqual(len(res["references"]), 1)
        self.assertEqual(res["references"][0]["line"], 4)
        # 字符串里的 foo（第 3 行）单列为低置信命中，不与真引用混在一起
        self.assertEqual(len(res["string_hits"]), 1)
        self.assertEqual(res["string_hits"][0]["line"], 3)

    def test_scope_limits_scan(self):
        self.write("a/m.py", PY_SRC)
        self.write("b/m.py", PY_SRC)
        res = code_intel.find_references(self.tmp, "foo", scope=["a"])
        self.assertTrue(res["ok"])
        self.assertEqual(sorted(res["by_file"]), ["a/m.py"])

    def test_invalid_scope_rejected(self):
        self.write("m.py", "x = 1\n")
        res = code_intel.find_references(self.tmp, "foo", scope=["nope_dir"])
        self.assertFalse(res["ok"])
        self.assertIn("scope 不存在", res["error"])

    def test_scope_outside_root_rejected(self):
        res = code_intel.find_references(self.tmp, "foo", scope=["../outside"])
        self.assertFalse(res["ok"])
        self.assertIn("不在代码根目录", res["error"])

    def test_missing_symbol_reports_note(self):
        self.write("m.py", "x = 1\n")
        res = code_intel.find_references(self.tmp, "nope")
        self.assertTrue(res["ok"])
        self.assertEqual(res["total"], 0)
        self.assertTrue(any("未找到" in n for n in res["notes"]))

    def test_empty_symbol_rejected(self):
        res = code_intel.find_references(self.tmp, "   ")
        self.assertFalse(res["ok"])
        self.assertIn("symbol", res["error"])

    def test_kind_def_only(self):
        self.write("m.py", PY_SRC)
        res = code_intel.find_references(self.tmp, "foo", kind="def")
        self.assertEqual(len(res["definitions"]), 1)
        self.assertEqual(res["references"], [])

    def test_non_python_uses_word_boundary(self):
        # 原断言写于「非 Python 只能走正则」的年代；.js 现在也走 tree-sitter，
        # `const foo = 1` 被判成【定义】而不是引用。作者意图不变：注释与 foobar 都不算。
        self.write("a.js", "const foo = 1;\n// foo in comment\nfoobar = 2;\n")
        res = code_intel.find_references(self.tmp, "foo")
        self.assertEqual([d["line"] for d in res["definitions"]], [1])
        self.assertEqual([r["line"] for r in res["references"]], [])
        self.assertEqual(res["ts_indexed_files"], 1)

    def test_render_includes_headings(self):
        self.write("m.py", PY_SRC)
        text = code_intel.render_references(code_intel.find_references(self.tmp, "foo"))
        self.assertIn("--- 定义 ---", text)
        self.assertIn("--- 引用 ---", text)


# --------------------------------------------------------------------------- #
# dev_git_diff（真 git 仓库）
# --------------------------------------------------------------------------- #

@unittest.skipUnless(GIT_OK, "git 不可用")
class TestDevGitDiff(_CodeRootCase):
    def _init_repo(self):
        self.use_root()
        self.write("a.py", "def foo():\n    return 1\n")
        for argv in (["git", "init", "-q"],
                     ["git", "config", "user.email", "t@example.com"],
                     ["git", "config", "user.name", "test"],
                     ["git", "add", "a.py"],
                     ["git", "commit", "-q", "-m", "init"]):
            subprocess.run(argv, cwd=self.tmp, capture_output=True, text=True,
                           timeout=60)

    def test_clean_tree_reports_no_change(self):
        self._init_repo()
        out = tools.dev_git_diff("")
        self.assertIn("无改动", out)

    def test_modified_file_shows_in_unstaged(self):
        self._init_repo()
        self.write("a.py", "def foo():\n    return 2\n")
        out = tools.dev_git_diff("")
        self.assertIn("未暂存 1", out)
        self.assertIn("return 2", out)

    def test_untracked_file_is_surfaced_but_absent_from_diff(self):
        self._init_repo()
        self.write("brand_new.py", "x = 1\n")
        out = tools.dev_git_diff("")
        self.assertIn("brand_new.py", out)
        self.assertIn("未跟踪", out)
        self.assertIn("不会出现在 git diff", out)

    def test_staged_switch_reads_index(self):
        self._init_repo()
        self.write("a.py", "def foo():\n    return 3\n")
        subprocess.run(["git", "add", "a.py"], cwd=self.tmp, capture_output=True,
                       text=True, timeout=60)
        out = tools.dev_git_diff("staged: true")
        self.assertIn("已暂存 1", out)
        self.assertIn("return 3", out)

    def test_stat_only_omits_body(self):
        self._init_repo()
        self.write("a.py", "def foo():\n    return 4\n")
        out = tools.dev_git_diff("stat: true")
        self.assertIn("--- 统计 ---", out)
        self.assertNotIn("--- 差异正文 ---", out)

    def test_path_scope_narrows_output(self):
        self._init_repo()
        self.write("a.py", "def foo():\n    return 5\n")
        self.write("b.py", "def bar():\n    return 6\n")
        out = tools.dev_git_diff("paths: a.py")
        self.assertIn("范围: a.py", out)
        self.assertIn("return 5", out)
        self.assertNotIn("return 6", out)

    def test_out_of_scope_path_rejected(self):
        self._init_repo()
        out = tools.dev_git_diff("paths: ../elsewhere.py")
        self.assertIn("拒绝路径", out)


# --------------------------------------------------------------------------- #
# dev_apply_edits：原子性
# --------------------------------------------------------------------------- #

class TestDevApplyEdits(_CodeRootCase):
    def test_all_blocks_applied(self):
        self.use_root()
        self.write("a.py", "X = 1\n")
        self.write("b.py", "Y = 2\n")
        out = tools.dev_apply_edits(
            "path: a.py\nold_text: X = 1\nnew_text: X = 11\n"
            "\n---\n\npath: b.py\nold_text: Y = 2\nnew_text: Y = 22\n")
        self.assertIn("已原子写入 2 个文件", out)
        self.assertIn("X = 11", open(os.path.join(self.tmp, "a.py"), encoding="utf-8").read())
        self.assertIn("Y = 22", open(os.path.join(self.tmp, "b.py"), encoding="utf-8").read())

    def test_atomic_failure_writes_nothing(self):
        self.use_root()
        self.write("a.py", "X = 1\n")
        self.write("b.py", "Y = 2\n")
        out = tools.dev_apply_edits(
            "path: a.py\nold_text: X = 1\nnew_text: X = 99\n"
            "\n---\n\npath: b.py\nold_text: 不存在的片段\nnew_text: Y = 22\n")
        self.assertIn("未写入任何文件", out)
        self.assertIn("X = 1", open(os.path.join(self.tmp, "a.py"), encoding="utf-8").read())
        self.assertIn("Y = 2", open(os.path.join(self.tmp, "b.py"), encoding="utf-8").read())

    def test_syntax_error_blocks_every_file(self):
        self.use_root()
        self.write("a.py", "X = 1\n")
        self.write("bad.py", "def f(:\n    pass\n")
        out = tools.dev_apply_edits(
            "path: bad.py\nold_text: def f(:\nnew_text: def f(: :\n"
            "\n---\n\npath: a.py\nold_text: X = 1\nnew_text: X = 2\n")
        self.assertIn("未写入任何文件", out)
        self.assertIn("X = 1", open(os.path.join(self.tmp, "a.py"), encoding="utf-8").read())

    def test_ambiguous_old_text_rejected(self):
        self.use_root()
        self.write("a.py", "dup\ndup\n")
        out = tools.dev_apply_edits("path: a.py\nold_text: dup\nnew_text: only\n")
        self.assertIn("未写入任何文件", out)
        self.assertIn("存在歧义", out)

    def test_duplicate_path_rejected(self):
        self.use_root()
        self.write("a.py", "X = 1\nY = 2\n")
        out = tools.dev_apply_edits(
            "path: a.py\nold_text: X = 1\nnew_text: X = 3\n"
            "\n---\n\npath: a.py\nold_text: Y = 2\nnew_text: Y = 4\n")
        self.assertIn("同一文件出现多次", out)

    def test_missing_old_text_rejected(self):
        self.use_root()
        self.write("a.py", "X = 1\n")
        out = tools.dev_apply_edits("path: a.py\nnew_text: X = 2\n")
        self.assertIn("必须提供精确 old_text", out)

    def test_markdown_separator_not_split(self):
        """new_text 里的 `---` 是文件内容（Markdown 分隔线），不能被切成新块。"""
        self.use_root()
        self.write("doc.md", "head\n---\ntail\n")
        out = tools.dev_apply_edits(
            "path: doc.md\nold_text: head\nnew_text: head2\n---\nbody\n")
        self.assertIn("已原子写入 1 个文件", out)
        body = open(os.path.join(self.tmp, "doc.md"), encoding="utf-8").read()
        self.assertIn("head2\n---\nbody", body)

    def test_out_of_root_rejected(self):
        self.use_root()
        outside = os.path.join(os.path.dirname(self.tmp), "outside_edit.py")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("Z = 0\n")
        try:
            out = tools.dev_apply_edits(
                f"path: {outside}\nold_text: Z = 0\nnew_text: Z = 9\n")
            self.assertIn("不在代码根目录", out)
        finally:
            os.remove(outside)

    def test_no_blocks_parsed(self):
        self.use_root()
        out = tools.dev_apply_edits("我只是随便说点什么")
        self.assertIn("未解析到任何编辑块", out)

    def test_batch_write_triggers_self_verify_path(self):
        """批量写必须能被写后自验证收尾门解析出目标文件（否则会绕过验证闭环）。"""
        import agent as agent_mod
        obs = ("已原子写入 2 个文件：\n- backend/a.py（12 字节）\n"
               "- frontend/b.ts（9 字节）")
        self.assertEqual(agent_mod._parse_written_rel(obs), "backend/a.py,frontend/b.ts")

    def test_batch_write_is_classified_as_a_write_tool(self):
        """dev_apply_edits 必须被**运行期**判成写工具且不可并发。

        这条测试以前断言的是 `agent._WRITE_TOOLS` / `agent._NO_PARALLEL_TOOLS`——
        两张都是运行期不读的死副本，于是它一直绿着，而真实判定把一个批量改文件的
        工具当成 read_local / SideEffect.PURE / parallel_safe=True：既能和别的工具
        并发写，又拿不到幂等账本的保护。现在直接断言 coerce_tool_spec 的输出，
        也就是运行期真正拿到的那个对象。
        """
        import tools as tools_mod
        from agent_runtime.tools import Capability, SideEffect, coerce_tool_spec
        spec = coerce_tool_spec("dev_apply_edits", tools_mod.TOOLS["dev_apply_edits"])
        self.assertEqual(spec.capability, Capability.WRITE_LOCAL)
        self.assertEqual(spec.side_effect, SideEffect.MUTATING)
        self.assertFalse(spec.parallel_safe,
                         "写工具若被判成可并发，会与只读工具一起进并行批次造成写竞争")

    def test_it_matches_apply_edit_its_batch_sibling(self):
        """批量版与单条版的分类必须一致，否则护栏对两者宽严不同。"""
        import tools as tools_mod
        from agent_runtime.tools import coerce_tool_spec
        batch = coerce_tool_spec("dev_apply_edits", tools_mod.TOOLS["dev_apply_edits"])
        single = coerce_tool_spec("apply_edit", tools_mod.TOOLS["apply_edit"])
        for field in ("capability", "side_effect", "parallel_safe"):
            self.assertEqual(getattr(batch, field), getattr(single, field), field)


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #

class TestRegistration(unittest.TestCase):
    def test_new_tools_registered(self):
        for name in ("dev_git_diff", "dev_find_references", "dev_apply_edits"):
            self.assertIn(name, tools.TOOLS)
            self.assertTrue(callable(tools.TOOLS[name]["func"]))

    def test_descriptions_mention_guardrails(self):
        self.assertIn("只读", tools.TOOLS["dev_git_diff"]["description"])
        self.assertIn("原子", tools.TOOLS["dev_apply_edits"]["description"])


if __name__ == "__main__":
    unittest.main()
