# -*- coding: utf-8 -*-
"""错误文本要**短而具体**；工具描述要劝整文件读。

## 依据（Claude Code 的 limits.ts / prompt.ts）

**一、错误路径要短。** 同一份源码里记着他们的实测：

    Tested truncating instead of throwing for explicit-limit reads that exceed
    the byte cap (#21841, Mar 2026). Reverted: tool error rate dropped but mean
    tokens rose — the throw path yields a ~100-byte error tool-result while
    truncation yields ~25K tokens of content at the cap.

    报错路径约 100 字节，重复/截断返回内容要花上万 token。

**二、默认劝整文件读**，区间只作例外（`OFFSET_INSTRUCTION_DEFAULT`）：

    "...it's recommended to read the whole file by not providing these parameters"

## 但"短"不是唯一目标

早先那句「文件不存在：xxx start=1」就太短了——**短到看不出是参数写法问题**，
模型于是去换路径分隔符、白试四次。所以目标是「一句话说清错在哪 + 给出正确写法」，
不含解释性铺垫。本文件对两条要求分别设了断言。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import config as C  # noqa: E402
import tools as T  # noqa: E402

#: 字节预算。Claude Code 的存根/报错约 100 字节；中文每字 3 字节，
#: 所以按"中文字数"给预算而不是死卡字节数。
ERROR_BUDGET_BYTES = 200


class ErrorTextTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "behaviors"))
        with open(os.path.join(self.tmp.name, "behaviors", "a.gd"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join("line%d" % i for i in range(1, 41)))
        C.set_runtime("code_root", self.tmp.name)
        C.set_runtime("project_rules", "")
        T.clear_read_files()
        self.addCleanup(lambda: (C.set_runtime("code_root", ""), T.clear_read_files()))

    def _bytes(self, arg):
        out = T.read_file(arg)
        return len((out if isinstance(out, str) else str(out)).encode("utf-8"))

    def test_errors_stay_within_a_small_byte_budget(self):
        """长错误不只是费token——它还会把模型引向错误的排查方向。"""
        cases = {
            "区间参数未识别": "path: behaviors/a.gd start=1",
            "区间参数未识别(行)": "path: behaviors/a.gd 行1-20",
            "路径不存在": "path: behaviors/nope.gd",
            "起始行越界": "path: behaviors/a.gd start: 99",
            "越界路径": "path: ../../etc/passwd",
        }
        for label, arg in cases.items():
            n = self._bytes(arg)
            self.assertLessEqual(
                n, ERROR_BUDGET_BYTES,
                "%s 的返回有 %d 字节，超出预算——错误路径应该是廉价的" % (label, n))

    def test_the_syntax_error_still_names_the_real_cause(self):
        """短**不等于**含糊：必须看得出是参数写法问题，而不是路径写错。

        这条不能为了省字节而牺牲——早先「文件不存在：xxx start=1」就是反例，
        模型因此去换路径分隔符、白试四次。
        """
        out = T.read_file("path: behaviors/a.gd start=1")
        self.assertIn("区间参数", out, "没有点名是区间参数的问题")
        self.assertIn("start:20 end:45", out, "必须给出可直接照抄的正确写法")
        self.assertNotIn("文件不存在", out,
                         "仍在说文件不存在——那会把模型引向换路径而不是改写法")

    def test_the_out_of_range_message_gives_the_real_range(self):
        out = T.read_file("path: behaviors/a.gd start: 99")
        self.assertIn("1-40", out, "越界时必须给出真实行数范围，否则模型无从修正")
        self.assertNotIn("99-40", out, "仍在输出倒置区间行号")


class DescriptionNudgesWholeFileTests(unittest.TestCase):
    """D：默认劝整文件读。"""

    def test_the_description_recommends_reading_the_whole_file(self):
        desc = T.TOOLS["read_file"]["description"]
        self.assertIn("默认直接读整个文件", desc,
                      "描述没有劝整文件读——区间会同时放大参数面与重复读取两件事")

    def test_ranges_are_still_documented_as_an_exception(self):
        desc = T.TOOLS["read_file"]["description"]
        self.assertIn("start/end", desc,
                      "劝整文件读不等于禁用区间；必须写清什么时候才该用")
        self.assertIn("1 基起始行", desc,
                      "必须说清 start 是 1 基——否则模型会给出 0 基行号")

    def test_it_mentions_the_unchanged_stub(self):
        """模型该知道重复读会得到短提示，否则它可能以为读不到内容而换工具。"""
        self.assertIn("未变化", T.TOOLS["read_file"]["description"])


class StubMustBeCheaperThanContentTests(unittest.TestCase):
    """存根的前提是「比它替代的内容更短」。对很小的文件它不是——那就别发。"""

    def _setup(self, n_lines):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "behaviors"))
        with open(os.path.join(self.tmp.name, "behaviors", "a.gd"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join("line%d" % i for i in range(1, n_lines + 1)))
        C.set_runtime("code_root", self.tmp.name)
        T.clear_read_files()

    def test_tiny_files_are_reread_not_stubbed(self):
        """实测：10 行文件内容 83 字节，而存根 152 字节——发存根反而更费。"""
        self._setup(10)
        T.read_file("path: behaviors/a.gd")
        out = T.read_file("path: behaviors/a.gd")
        self.assertNotIn("未变化", out,
                         "小文件也发存根——存根比内容还长，那是浪费不是节省")
        self.assertIn("line1", out)

    def test_large_files_do_get_a_stub(self):
        self._setup(200)
        T.read_file("path: behaviors/a.gd")
        out = T.read_file("path: behaviors/a.gd")
        self.assertIn("未变化", out)

    def test_the_stub_is_never_longer_than_what_it_replaces(self):
        """逐档验证：任何规模下，发出去的字节数都不多于重读。"""
        for n in (5, 10, 30, 60, 120, 300):
            self._setup(n)
            real = T.read_file("path: behaviors/a.gd")
            stub = T.read_file("path: behaviors/a.gd")
            self.assertLessEqual(
                len(stub.encode("utf-8")), len(real.encode("utf-8")),
                "%d 行文件：存根 %d 字节 > 内容 %d 字节"
                % (n, len(stub.encode()), len(real.encode())))
            self.addCleanup(T.clear_read_files)


if __name__ == "__main__":
    unittest.main()