# -*- coding: utf-8 -*-
"""两处 HIGH 的 fail-closed 锁定测试（2026-10-19，开修队列顶部两个 HIGH）。

1) BUG-GIT-GATE-RESTORE-RM：run_command 的 git 写黑名单补齐 restore/rm/
   filter-branch/replace——直测纯函数 _cmd_is_blocked，不执行任何 git。
   同时锁定：既有写子命令仍拦、只读子命令仍放、`git mv` 出口不破。

2) BUG-PATCH-DRYRUN-FAILOPEN：dev_patch 的 dry_run 全角冒号 / 近义值不再静默真写盘。
   用真实临时目录 + 真实 apply_patch，读回文件字节判定，非 mock。
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import tools
from tools import _cmd_is_blocked, dev_patch


class GitWriteGateTypos(unittest.TestCase):
    """_GIT_WRITE_SUBCMDS 补齐后：破坏性写拦、只读放、git mv 出口保。"""

    def test_newly_blocked_destructive_ops(self):
        for cmd in ("git restore src/foo.py",
                    "git restore --worktree src/foo.py",
                    "git restore .",
                    "git rm src/foo.py",
                    "git rm --cached src/foo.py",
                    "git filter-branch --all",
                    "git replace foo bar"):
            with self.subTest(cmd=cmd):
                blocked, why = _cmd_is_blocked(cmd)
                self.assertTrue(blocked, "应拦却放行: %s" % cmd)
                self.assertIn("git 写操作", why)

    def test_existing_write_ops_still_blocked(self):
        # 回归保护：补枚举不能把原有已拦的写操作放出去。
        for cmd in ("git commit -m x", "git push origin main",
                    "git reset --hard HEAD", "git clean -fd",
                    "git checkout -- src/foo.py", "git stash",
                    "git rebase main", "git merge feature"):
            with self.subTest(cmd=cmd):
                self.assertTrue(_cmd_is_blocked(cmd)[0])

    def test_readonly_still_allowed(self):
        for cmd in ("git status", "git log --oneline", "git diff",
                    "git show HEAD", "git ls-files", "git blame foo.py"):
            with self.subTest(cmd=cmd):
                self.assertFalse(_cmd_is_blocked(cmd)[0], "只读被误拦: %s" % cmd)

    def test_git_mv_passthrough_preserved(self):
        # dev_move docstring 明写 run_command 放行 `git mv`（非破坏性重命名），别误加。
        self.assertFalse(_cmd_is_blocked("git mv old.py new.py")[0])


class DevPatchDryRunFailClosed(unittest.TestCase):
    """dev_patch dry_run 三态：真词预览 / 假词真应用 / 不可判定拒绝执行。"""

    DIFF = (
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " line one\n"
        "-line two\n"
        "+line two changed\n"
        " line three\n"
    )
    ORIG = "line one\nline two\nline three\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="docmind_dryrun_")
        self.root = self.tmp.name
        self._write()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8", newline="") as f:
            f.write(self.ORIG)

    def _read(self):
        with open(os.path.join(self.root, "a.txt"), "r", encoding="utf-8") as f:
            return f.read()

    def _run(self, arg):
        self._write()  # 每场景重置
        with patch.object(tools, "_get_code_root", return_value=self.root):
            out = dev_patch(arg)
        changed = "line two changed" in self._read()
        return out, changed

    # —— 真词：预览，不落盘 ——
    def test_ascii_true_previews(self):
        out, changed = self._run("dry_run: true\n" + self.DIFF)
        self.assertIn("dry_run", out)
        self.assertFalse(changed, "半角 true 不该落盘")

    def test_fullwidth_true_previews(self):
        # 修①：全角冒号、值就是 true，原会静默真写盘——现在正确预览。
        out, changed = self._run("dry_run：true\n" + self.DIFF)
        self.assertIn("dry_run", out)
        self.assertFalse(changed, "全角 dry_run：true 不该落盘")

    # —— 假词：显式真应用 ——
    def test_explicit_false_applies(self):
        out, changed = self._run("dry_run: false\n" + self.DIFF)
        self.assertIn("已应用", out)
        self.assertTrue(changed, "显式 false 应真应用")

    # —— 省略：真应用（既有契约） ——
    def test_omitted_marker_applies(self):
        out, changed = self._run(self.DIFF)
        self.assertIn("已应用", out)
        self.assertTrue(changed)

    # —— 不可判定：拒绝执行，绝不落盘 ——
    def test_nearmiss_value_refused(self):
        for bad in ("t", "真", "ture", "check"):
            with self.subTest(bad=bad):
                out, changed = self._run("dry_run: %s\n" % bad + self.DIFF)
                self.assertIn("无法确定 dry_run", out)
                self.assertIn("本次未执行任何改动", out)
                self.assertFalse(changed, "近义值不该落盘")

    def test_fullwidth_nearmiss_refused(self):
        out, changed = self._run("dry_run：真\n" + self.DIFF)
        self.assertIn("无法确定 dry_run", out)
        self.assertFalse(changed)

    def test_empty_value_refused(self):
        out, changed = self._run("dry_run:\n" + self.DIFF)
        self.assertIn("无法确定 dry_run", out)
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
