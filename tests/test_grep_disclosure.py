# -*- coding: utf-8 -*-
"""④ grep 的两处诚实性修复：触顶截断披露 + `.docmind` 与 glob 对齐。

背景（本轮钻测的另两个现场）：
  · grep 命中到 40 条就 `break` 并返回 `hits[:40]`，**无任何截断标注**——实测模型
    把这 40 条当全部，把真值 196 个函数报成 194 还自称"准确总数"。
  · grep 的目录剪枝只用 `ingest._SKIP_DIRS`（不含 `.docmind` 状态目录），而 glob 认
    `code_intel._DOCMIND_STATE_DIRS`。于是 grep 会一头扎进 `.docmind/projects/<id>/headcheck/…`
    这类整仓影子副本、和 glob 口径不一致、还命中过期副本。

修法（用户选定，本轮只做 grep，search_code/ingest 那层只标注不动）：
  · 复用 `code_intel._prune` —— 与 glob 同一政策：`_SKIP_DIRS` 全局不扫，且**只在
    `.docmind/` 直接下一层**跳过状态目录，产物目录（`web/`）照旧可见。
  · 命中触顶、跳过的 >2MB 文件、被剪的 `.docmind` 状态目录，都如实带回一句提示。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools  # noqa: E402
from config import set_runtime  # noqa: E402
import tempfile  # noqa: E402
import shutil  # noqa: E402


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="docmind_grep_")
        set_runtime("code_root", self.d)

    def tearDown(self):
        set_runtime("code_root", "")
        shutil.rmtree(self.d, ignore_errors=True)

    def w(self, rel, text):
        p = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p


class TruncationNoteTests(_Tmp):
    def test_over_cap_discloses_truncation(self):
        self.w("many.gd", "".join("func f%d()\n" % i for i in range(60)))
        out = tools.grep("func f")
        lines = out.split("\n")
        # 前 40 条是命中，末尾附一句截断提示
        self.assertEqual(len([l for l in lines if l.startswith("many.gd:")]), 40)
        self.assertIn("已达 40 条上限", out)
        self.assertIn("**截断**", out)
        self.assertIn("不要据此统计总数", out)

    def test_under_cap_has_no_note(self):
        self.w("few.gd", "func a()\nfunc b()\n")
        out = tools.grep("func ")
        self.assertIn("func a", out)
        self.assertNotIn("上限", out)
        self.assertNotIn("截断", out)


class DocmindScopeParityTests(_Tmp):
    def test_state_dir_pruned_but_products_kept(self):
        # 影子副本（应被剪，不命中）
        self.w(".docmind/projects/shadow/x.gd", "func NEEDLE_DM() -> int:\n")
        # 产物目录（应照旧可搜，命中）
        self.w(".docmind/web/index.gd", "func NEEDLE_DM_PROD() -> int:\n")
        out = tools.grep("NEEDLE_DM").replace("\\", "/")
        self.assertIn(".docmind/web/index.gd", out,
                     "产物目录被误剪了——grep 与 glob 口径不一致")
        self.assertNotIn(".docmind/projects", out.split("（提示")[0],
                         "命中了 `.docmind/projects` 影子副本——grep 仍按旧口径扫状态目录")
        # 如实披露被剪的状态目录
        self.assertIn("已跳过 Agent 工作台 状态目录", out)
        self.assertIn(".docmind/projects", out)

    def test_normal_project_still_searched(self):
        # 项目真实源码不受影响
        self.w("src/main.gd", "func HELLO_PROJ() -> int:\n")
        out = tools.grep("HELLO_PROJ").replace("\\", "/")
        self.assertIn("src/main.gd", out)
        self.assertNotIn("已跳过 Agent 工作台 状态目录", out)


class SkippedBigFileTests(_Tmp):
    def test_oversized_file_skipped_and_disclosed(self):
        # 一个 >2MB 的文件，记号只在里面
        big = "func HUGE_MARKER()\n" + ("// pad\n" * 400000)  # ~2.8MB
        self.w("huge.gd", big)
        out = tools.grep("HUGE_MARKER")
        self.assertIn("未匹配到", out)          # 大文件没被搜，自然没命中
        self.assertIn("另有 1 个 >2MB 的文件未搜索", out)  # 但如实说了"没看"


if __name__ == "__main__":
    unittest.main()
