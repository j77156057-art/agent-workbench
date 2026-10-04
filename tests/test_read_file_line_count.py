# -*- coding: utf-8 -*-
"""`read_file` 的**每一种**形态都必须能回答「这个文件共几行」。

## 实测事故（`wf-fc3cf86c5eaf`）

同一个文件被模型报成**两个不同的行数**，而真实是 49：

    read-player    「文件共 44 行」
    read-enemy     「文件共 51 行」   ← enemy.gd 真实 53

而反思判据独立印证了原因：

    「read_file观察为裸正文无行号，行号准确性无法从观察直接证实，仅能按顺序推断」

**判据是对的。**观察里确实没有行数可查，模型只能自己数裸正文，数错了。
本会话此前它报过 47 / 50 / 51 —— 同一个原因、同一个缺陷。

## 这不是「给模型加信息」，是补上一个同族工具早就有的字段

实测五种形态的抬头：

    区间读      === f（第 5-8 行，共 49 行）===      ✓ 早就有
    只有 start  === f（第 25-49 行，共 49 行）===    ✓ 早就有
    start > end === f（第 40-49 行，共 49 行）===    ✓ 早就有
    超出末尾    === f（共 49 行）===                ✓ 早就有
    整文件      === f ===                          ← **只有它缺**

四种带区间信息的形态全都给了总行数，唯独「读整个文件」这个**最常用**的形态没有。
模型选整文件读（工具说明里它是推荐用法）就必然落进唯一那个没有行数的洞。

## 两条克制

**不加逐行行号。**那会让整文件读显著变大，而且与区间读的输出形态混淆——
判据正是靠「有没有行号」区分两种形态的（`_obs_has_line_numbers` 只认区间读抬头
与 grep 标签）。这里只补一个**标量**。

**截断分支的 total 按完整文件算。**否则模型会把「仅显示前 12000 字」读成
「文件就这么长」——而它同时又在结论里报一个自己数出来的行数，两个数互相矛盾。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import tools  # noqa: E402

BODY = "".join("line_%02d\n" % i for i in range(1, 50))  # 49 行


class EveryShapeCarriesTheLineCountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "sub"))
        with open(os.path.join(self.tmp.name, "sub", "a.gd"), "w",
                  encoding="utf-8") as handle:
            handle.write(BODY)
        self._prev = config._RUNTIME.get("code_root")
        config.set_runtime("code_root", self.tmp.name)
        self.addCleanup(self._restore)

    def _restore(self):
        config.set_runtime("code_root", self._prev or "")
        tools.clear_read_files()

    def _read(self, arg):
        tools.clear_read_files()
        return str(tools.read_file(arg))

    def _header(self, arg):
        return self._read(arg).splitlines()[0]

    # ---- 五种形态，逐个断言 ---------------------------------------------

    def test_whole_file_read(self):
        """**这个形态此前完全没有行数** —— 而它是工具说明里的推荐用法。"""
        self.assertIn("共 49 行", self._header("path: sub/a.gd"))

    def test_range_read(self):
        self.assertIn("共 49 行", self._header("path: sub/a.gd, start: 5, end: 8"))

    def test_start_only(self):
        self.assertIn("共 49 行", self._header("path: sub/a.gd, start: 25"))

    def test_inverted_range(self):
        self.assertIn("共 49 行", self._header("path: sub/a.gd, start: 40, end: 10"))

    def test_out_of_bounds_start(self):
        self.assertIn("共 49 行", self._header("path: sub/a.gd, start: 999"))

    def test_every_shape_agrees_on_the_same_number(self):
        """五种形态必须给出**同一个**行数——不一致就是又一处自相矛盾。"""
        counts = set()
        for arg in ("path: sub/a.gd", "path: sub/a.gd, start: 5, end: 8",
                    "path: sub/a.gd, start: 25", "path: sub/a.gd, start: 40, end: 10",
                    "path: sub/a.gd, start: 999"):
            head = self._header(arg)
            for token in head.replace("（", " ").replace("）", " ").split():
                if token.isdigit():
                    counts.add(int(token))
                    break
        self.assertEqual(counts, {49}, "各形态给出的行数不一致：%r" % counts)


class TheCountMatchesTheFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._prev = config._RUNTIME.get("code_root")
        config.set_runtime("code_root", self.tmp.name)
        self.addCleanup(lambda: (config.set_runtime("code_root", self._prev or ""),
                                 tools.clear_read_files()))

    def _write(self, name, text, binary=False):
        path = os.path.join(self.tmp.name, name)
        if binary:
            # **必须按字节写。** 文本模式（`newline=None`）会把写入的每个 `\n`
            # 再翻译成 `\r\n`，于是 `"a\r\n"` 落盘成 `b'a\r\r\n'`——双 CR。
            # 读回时 universal newlines 把 `\r\n` 变 `\n`，`\r` 留下，于是
            # 一行变两行、3 行变 6 行。
            #
            # 第一版就踩了这个坑：断言写「共 3 行」而实测「共 6 行」，
            # 我据此怀疑实现写错了行数公式——**错的是 fixture，不是实现**。
            # 真编辑器的 CRLF 文件是**单** `\r\n`，必须按字节造才造得出来。
            with open(path, "wb") as fh:
                fh.write(text)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        tools.clear_read_files()
        return str(tools.read_file("path: " + name)).splitlines()[0]

    def test_a_file_without_a_trailing_newline(self):
        """"abc" 是一行，不是零行——`splitlines()` 的行为必须对齐。"""
        self.assertIn("共 1 行", self._write("x.gd", "abc"))

    def test_an_empty_file(self):
        self.assertIn("共 0 行", self._write("empty.gd", ""))

    def test_a_single_line_with_newline(self):
        self.assertIn("共 1 行", self._write("one.gd", "a\n"))

    def test_windows_line_endings(self):
        """真 CRLF（单 `\\r\\n`，按字节写入）算 3 行。"""
        self.assertIn("共 3 行",
                      self._write("crlf.gd", b"a\r\nb\r\nc\r\n", binary=True))

    def test_a_crlf_file_without_a_trailing_newline(self):
        self.assertIn("共 3 行",
                      self._write("crlf2.gd", b"a\r\nb\r\nc", binary=True))

    def test_a_file_that_really_holds_double_cr(self):
        """双 CR 是真实存在的文件（某些生成器会这么写），那就真是 6 行。

        与上一条对照：断言的是**实现对输入的解读**，不是「CRLF 应该算几行」。
        """
        self.assertIn("共 6 行",
                      self._write("dblcr.gd", b"a\r\r\nb\r\r\nc\r\r\n", binary=True))


class TruncationKeepsTheRealCountTests(unittest.TestCase):
    """截断时的 total 必须按**完整文件**算。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        big = "".join("x%06d\n" % i for i in range(4000))  # 4000 行，远超 12000 字
        with open(os.path.join(self.tmp.name, "big.txt"), "w", encoding="utf-8") as fh:
            fh.write(big)
        self._prev = config._RUNTIME.get("code_root")
        config.set_runtime("code_root", self.tmp.name)
        self.addCleanup(lambda: (config.set_runtime("code_root", self._prev or ""),
                                 tools.clear_read_files()))

    def test_the_count_is_the_files_not_the_shown_slice(self):
        tools.clear_read_files()
        out = str(tools.read_file("path: big.txt"))
        head = out.splitlines()[0]
        self.assertIn("共 4000 行", head,
                      "抬头按截断后的片段算行数——模型会以为文件只有这么长")
        self.assertIn("已截断", out, "截断必须有标记")

    def test_the_note_also_states_the_total(self):
        """截断说明里也要带总行数，否则模型读到的是两个矛盾的数字。"""
        tools.clear_read_files()
        out = str(tools.read_file("path: big.txt"))
        self.assertIn("共 4000 行", out,
                      "截断说明没给总行数——只剩「仅显示前 12000 字」")


class WholeFileReadsAreNumberedTooTests(unittest.TestCase):
    """整文件读**逐行编号** —— 与区间读统一形态。

    ## 为什么翻转了上一轮的决定

    上一轮我刻意克制「不加逐行行号」，理由是「判据靠有没有行号区分两种形态」。

    **那个理由是错的。** 区分两种形态的目的是「知道能不能核验行号」，而不是
    「拒绝整文件读带行号」。拒绝的代价是判据永远无法核验整文件读的行号——
    而整文件读是工具说明里的**推荐用法**，模型最常走的那条路。

    实测（`wf-fd543f79c197`）：总行数补上后模型不再数错（报 53 / 49，与原文
    一致），但紧接着 `verify-constants` 的反思写下：

        「结论中给出的行号在工具观察中并不存在 —— read_file 整文件读取返回的
          是裸正文，无行号，因此这些行号属于无依据的推测性标注」

    **那条指控是对的**，而模型当时正在做的事是「自己纠正上游的行号偏差」——
    它发现两份上游结论对同一段代码给了 `:31` 和 `:33`，因为两份都是各自数
    裸正文数出来的。

    前十层里九层是「已有的信息被丢掉」，这一层是「**从来没有过**」。
    补它比继续在判据上开豁免更根本：豁免只是让判据不去查，给了行号它就能查。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(os.path.join(self.tmp.name, "a.gd"), "w", encoding="utf-8") as fh:
            fh.write(BODY)
        self._prev = config._RUNTIME.get("code_root")
        config.set_runtime("code_root", self.tmp.name)
        self.addCleanup(lambda: (config.set_runtime("code_root", self._prev or ""),
                                 tools.clear_read_files()))

    def _read(self):
        tools.clear_read_files()
        return str(tools.read_file("path: a.gd"))

    def test_every_line_is_numbered(self):
        out = self._read()
        self.assertIn("1: line_01", out)
        self.assertIn("49: line_49", out)

    def test_the_judge_can_now_recognise_it_as_numbered(self):
        """这是整条改动的**目的**——判据得能核验它的行号。"""
        import agent
        self.assertTrue(agent._obs_has_line_numbers(self._read()),
                        "整文件读明明逐行编号了，判据却看成裸正文——"
                        "它会转而去要求代码片段当依据")

    def test_the_numbering_is_continuous_and_matches_the_header(self):
        """编号必须连续且与抬头一致，否则行号本身就是新的一层不可信来源。"""
        out = self._read()
        head = out.splitlines()[0]
        self.assertIn("共 49 行", head)
        body = [ln for ln in out.splitlines()[1:] if ln.strip()]
        self.assertEqual(len(body), 49)
        for i, line in enumerate(body, start=1):
            self.assertTrue(line.startswith("%d: " % i),
                            "第 %d 行没有编号：%r" % (i, line[:40]))

    def test_it_fits_the_trace_budget(self):
        """加了编号就变大——**变大会被落库那道刀砍**，那正是前十层的病因。

        所以这一条要钉住：典型文件在编号之后仍能整份进 `traj`。
        """
        import agent
        self.assertLessEqual(len(self._read()), agent.ORCH_TRACE_OBS_CHARS,
                             "编号之后超出回传预算——末尾会被砍掉，"
                             "而砍掉的恰好是判据要核验的行号")

    def test_both_shapes_look_the_same_to_the_judge(self):
        """整文件读与区间读现在同形——判据不需要为它开特例。"""
        import agent
        whole = self._read()
        tools.clear_read_files()
        part = str(tools.read_file("path: a.gd, start: 5, end: 8"))
        self.assertEqual(agent._obs_has_line_numbers(whole),
                         agent._obs_has_line_numbers(part))


if __name__ == "__main__":
    unittest.main()