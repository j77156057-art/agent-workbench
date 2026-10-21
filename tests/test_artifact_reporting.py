# -*- coding: utf-8 -*-
"""产物自报门禁：写盘/截图的工具必须把产物报出来，前端才有东西可显示。

## 为什么要有这道门禁

后端 observation 事件早就带 ``artifacts``（工具在**真正落盘那一刻**自报），
前端据此渲染产物卡（图片给缩略图、文件给可点的名字）。但这条链路曾经断在两处：

1. ``apply_edit`` 改完文件直接返回裸字符串，**不报产物** ——
   于是"模型改了哪些文件"在界面上完全看不见。
2. ``game_screenshot`` 报的截图路径是**绝对路径** ——
   浏览器里拼不出预览 URL，用户点开还是得自己找路径。

这两条都补了，这里锁住它们不再退化。

## 为什么判据是"工具自报"而不是"从 action 文本反推"

action 文本里同样可能带 ``path:``，但**被安全拦截的调用也会带** ——
照文本反推会把"没做的"记成"做了"。所以只有工具自报这一条路算数。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools as tools_mod  # noqa: E402


class ArtifactReportingTests(unittest.TestCase):
    """工具写盘/截图后必须自报产物（路径是项目相对路径）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self._old_root = tools_mod._get_code_root
        tools_mod._get_code_root = lambda: self.root

    def tearDown(self):
        tools_mod._get_code_root = self._old_root
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # create_file
    # ------------------------------------------------------------------
    def test_create_file_reports_artifact(self):
        res = tools_mod.create_file(
            "path: notes/demo.py\nnew_text: # demo\nprint('hi')\n"
        )
        artifacts = list(getattr(res, "artifacts", []) or [])
        self.assertTrue(artifacts, "create_file 必须自报产物")
        art = artifacts[0]
        self.assertEqual(art["path"], "notes/demo.py")
        self.assertTrue(art.get("label"), "产物要有展示名")
        self.assertTrue(art.get("summary"), "产物要有一句话说明")
        self.assertGreater(int(art.get("bytes") or 0), 0, "产物要带字节数")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "notes/demo.py")))

    # ------------------------------------------------------------------
    # apply_edit
    # ------------------------------------------------------------------
    def test_apply_edit_reports_artifact(self):
        tools_mod.create_file("path: edit_me.py\nnew_text: OLD\n")
        res = tools_mod.apply_edit(
            "path: edit_me.py\nold_text: OLD\nnew_text: NEW\n"
        )
        artifacts = list(getattr(res, "artifacts", []) or [])
        self.assertTrue(artifacts, "apply_edit 必须自报产物（改过的文件也要能一眼看到）")
        art = artifacts[0]
        self.assertEqual(art["path"], "edit_me.py")
        self.assertNotIn("\\", art["path"], "产物路径必须是项目相对路径（正斜杠）")
        with open(os.path.join(self.root, "edit_me.py"), encoding="utf-8") as fh:
            self.assertIn("NEW", fh.read())

    def test_apply_edit_failed_write_reports_no_artifact(self):
        """写盘失败不算产物 —— 不能把"没做的"记成"做了"。"""
        res = tools_mod.apply_edit(
            "path: missing_dir/x.py\nold_text: A\nnew_text: B\n"
        )
        artifacts = list(getattr(res, "artifacts", []) or [])
        self.assertFalse(artifacts, "写失败不该报产物")


if __name__ == "__main__":
    unittest.main()
