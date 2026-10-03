# -*- coding: utf-8 -*-
"""`read_file` 的行号参数名必须都认——`start_line:` 是第四层语法陷阱。

## 实测（`wf-2c5612c6ee53`）

    read_file(path: project.godot, start_line: 6, end_line: 8)
    → 文件不存在：project.godot, start_line: 6, end_line: 8

**那个文件明明就在那儿**，而报错断言它不存在。模型于是去找别的路径写法
（换分隔符、换目录），连试四次全失败——与前三次同一形状。

而重规划器给的任务里明写着「改用 search_code **按行范围分段读取**，避免整文件
截断」——**它建议了一件工具做不到的事**。参数 schema 里写的是 `start`/`end`，
但 `start_line:` 是同样自然的写法。

## 这个模块已经修过三层

1. 只认独占一行的 `start:`/`end:` → 模型普遍写成行内，整串被当文件名
2. `path: a.gd` 的**键名**没人剥 → 路径变成字面量 `"path: a.gd"`
3. 语法问题与路径问题不区分 → 报错一律说「文件不存在」

**第四层就是参数名的 `_line` 变体。** 前三层的教训一致：**收紧语法等于让模型在
一个纯语法问题上反复试错**，而每次失败的报错都把它引向错误的方向。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config as C  # noqa: E402
import tools as T  # noqa: E402


class ReadFileRangeSyntaxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "project.godot"), "w",
                  encoding="utf-8") as f:
            for i in range(1, 43):
                f.write("line_%02d\n" % i)
        C.set_runtime("code_root", self.root)
        T.clear_read_files()
        self.addCleanup(lambda: (T.clear_read_files(),
                                 C.set_runtime("code_root", "")))

    def _read(self, arg):
        return str(T.read_file(arg))

    # ---- 第四层：参数名的 _line 变体 ----------------------------------

    def test_inline_start_line_end_line(self):
        """本轮实测失败的那一种。"""
        out = self._read("path: project.godot, start_line: 6, end_line: 8")
        self.assertIn("第 6-8 行", out,
                      "参数名写成 start_line:/end_line: 就不认——"
                      "而模型就是这么写的")
        self.assertIn("line_06", out)

    def test_multiline_start_line_end_line(self):
        out = self._read("path: project.godot\nstart_line: 6\nend_line: 8")
        self.assertIn("第 6-8 行", out)

    def test_equals_sign_as_the_separator(self):
        """`:` 敲成 `=` 就又掉回同一个坑——而那正是这一层要治的那句报错。"""
        out = self._read("path: project.godot, start_line=6, end_line=8")
        self.assertIn("第 6-8 行", out,
                      "等号分隔不被认——模型把冒号敲成等号就又走死路")

    def test_hyphenated_parameter_names(self):
        out = self._read("path: project.godot, start-line: 6, end-line: 8")
        self.assertIn("第 6-8 行", out)

    def test_bare_start_with_equals_and_spaces(self):
        """原本只有裸 `start =` 会得到一条可自纠的提示；带 `_line` 的变体此前没有。"""
        out = self._read("path: project.godot, start = 6, end = 8")
        self.assertIn("第 6-8 行", out)

    def test_an_unrecognised_line_parameter_is_not_reported_as_a_missing_file(self):
        """认不出的写法必须被归为**语法**问题，且报错要点名是哪个键写错了。"""
        out = self._read("path: project.godot, 行 6-8")
        self.assertNotIn("文件不存在", out,
                         "认不出的行号写法被报成「文件不存在」——"
                         "模型会因此去改路径，而问题在语法")
        self.assertTrue("行" in out or "start" in out)

    def test_the_camel_case_variant_too(self):
        out = self._read("path: project.godot, startLine: 6, endLine: 8")
        self.assertIn("第 6-8 行", out)

    # ---- 前三层不能被带坏 ------------------------------------------------

    def test_bare_start_end_still_works(self):
        self.assertIn("第 6-8 行", self._read("path: project.godot, start: 6, end: 8"))

    def test_bare_start_end_multiline_still_works(self):
        self.assertIn("第 6-8 行", self._read("path: project.godot\nstart: 6\nend: 8"))

    def test_the_path_key_is_still_stripped(self):
        """第二层：`path:` 键名不被剥会得到字面量 `"path: a.gd"`。"""
        out = self._read("path: project.godot start: 6 end: 8")
        self.assertIn("第 6-8 行", out)
        self.assertNotIn("文件不存在", out)

    def test_a_whole_file_read_is_unaffected(self):
        out = self._read("path: project.godot")
        self.assertIn("line_01", out)
        self.assertIn("line_42", out)
        self.assertNotIn("第", out.splitlines()[0].split("===")[-1][:6])

    def test_only_start_without_end(self):
        out = self._read("path: project.godot, start_line: 40")
        self.assertIn("line_40", out)
        self.assertNotIn("line_01", out)

    def test_swapped_range_is_still_corrected(self):
        """end < start 时应当被顺延，而不是读出空区间。"""
        out = self._read("path: project.godot, start_line: 8, end_line: 6")
        self.assertIn("line_08", out)

    # ---- 真正不存在的路径仍然要报「文件不存在」 --------------------------

    def test_a_genuinely_missing_file_still_reports_not_found(self):
        """放宽参数解析不能变成「什么都报成功」。"""
        out = self._read("path: NoSuchFile.gd")
        self.assertIn("文件不存在", out)


class StrayParameterStillDiagnosedTests(unittest.TestCase):
    """放宽之后，**认不出的**残留仍要与路径问题区分开。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "a.gd"), "w", encoding="utf-8") as f:
            f.write("x\n" * 20)
        C.set_runtime("code_root", self.root)
        T.clear_read_files()
        self.addCleanup(lambda: (T.clear_read_files(),
                                 C.set_runtime("code_root", "")))

    def test_an_unrecognised_range_form_is_not_reported_as_a_missing_file(self):
        """`行1-55` 这种认不出的写法必须被归为语法问题。

        否则又回到「文件明明在、报错说它不存在」，模型转去改路径——
        这正是本模块前三层反复修掉的那个形状。
        """
        out = str(T.read_file("path: a.gd 行1-55"))
        self.assertNotIn("文件不存在", out,
                         "认不出的行号写法被报成了「文件不存在」——"
                         "模型会因此去改路径写法，而问题在语法")


if __name__ == "__main__":
    unittest.main()