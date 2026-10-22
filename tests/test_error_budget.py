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
        # 原值必须在 set 之前取；还原也必须还原成**原值**而不是 "" ——
        # 还原成 "" 会让后续用例拿到空根而不是它自己设的根，同样是顺序依赖。
        self._prev_code_root = C.get_runtime("code_root")
        self._prev_rules = C.get_runtime("project_rules")
        C.set_runtime("code_root", self.tmp.name)
        C.set_runtime("project_rules", "")
        T.clear_read_files()
        self.addCleanup(lambda: (C.set_runtime("code_root", self._prev_code_root),
                                 C.set_runtime("project_rules", self._prev_rules),
                                 T.clear_read_files()))

    def _bytes(self, arg):
        out = T.read_file(arg)
        return len((out if isinstance(out, str) else str(out)).encode("utf-8"))

    def test_errors_stay_within_a_small_byte_budget(self):
        """长错误不只是费token——它还会把模型引向错误的排查方向。

        `start=1` 原来被列在这里当「区间参数未识别」的样本，那是**分类错了**：
        `_RE_READ_START` 明确收 `[:：=＝]` 四种分隔符（`tools.py:3683` 的注释写着
        「模型把 `:` 敲成 `=` 就又掉回同一个坑」，是有意为之）。所以
        `path: behaviors/a.gd start=1` 是一次**成功的**区间读取，返回第 1-40 行全文，
        不是错误路径 —— 拿它当错误样本，这条测试就永远红，而且会把真正的错误路径
        一起拖下水（我为此白改了一轮生产代码才发现）。
        """
        cases = {
            "区间参数未识别(行)": "path: behaviors/a.gd 行1-20",
            "区间参数未识别(start_line)": "path: behaviors/a.gd start_line 1-20",
            "路径不存在": "path: behaviors/nope.gd",
            "起始行越界": "path: behaviors/a.gd start: 99",
            "越界路径": "path: ../../etc/passwd",
        }
        for label, arg in cases.items():
            n = self._bytes(arg)
            self.assertLessEqual(
                n, ERROR_BUDGET_BYTES,
                "%s 的返回有 %d 字节，超出预算——错误路径应该是廉价的" % (label, n))

    def test_the_equals_form_of_a_range_is_accepted_not_an_error(self):
        """**反向守卫**：`start=` / `end=` 是**有意支持**的写法，不是「未识别」。

        上一版把 `path: behaviors/a.gd start=1` 当成「区间参数未识别」的样本，于是
        这条测试要求工具对合法输入报错 —— 而工具行为是对的（它读了第 1-40 行并返回）。
        那条注释说得很清楚：只补 `:` 等于让模型把 `:` 敲成 `=` 就又掉回同一个坑。

        这里把该语义钉住，免得以后有人为了让那条预算测试变绿而把 `=` 从正则里删掉 ——
        那是**把开关偷偷放开**的反面：从「本该支持」退化成「本该报错」。
        """
        out = str(T.read_file("path: behaviors/a.gd start=1 end=3"))
        self.assertIn("line1", out)
        self.assertIn("line3", out)
        self.assertNotIn("line4", out, "end=3 就该只到第 3 行")
        self.assertNotIn("区间参数未识别", out, "`=` 是有意支持的写法，不该报错")
        self.assertNotIn("文件不存在", out, "更不该伪装成路径问题")

    def test_the_syntax_error_still_names_the_real_cause(self):
        """短**不等于**含糊：必须看得出是参数写法问题，而不是路径写错。

        这条不能为了省字节而牺牲——早先「文件不存在：xxx 行1-20」就是反例，
        模型因此去换路径分隔符、白试四次。
        """
        out = T.read_file("path: behaviors/a.gd 行1-20")
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
        # 原来这里**只设不还原**：code_root 被留在一个随即被 tmp.cleanup 删掉的
        # 临时目录上，后续任何依赖 code_root 的用例都会 `in_root=True` 而
        # `exists=False`。实测害得 tests/test_evidence_audit_source.py 的
        # 「四次成功 read_file 之后仍被判未核验」在**单跑时绿、与本文件同跑就红**。
        # 守卫生成在 tests/test_no_global_state_leak.py。
        #
        # 原值必须**作为实参冻结**，不能写成 `lambda: …(self._prev_code_root)`：
        # `_setup` 会被 `test_the_stub_is_never_longer_than_what_it_replaces` 在
        # **一个循环里调 6 次**，后一次会覆盖 `self._prev_code_root`，而 lambda 是
        # 晚绑定 -> 6 个 cleanup 全都还原成同一个「最后一次之前」的值。
        # 第一版就是这么写的，探针照出来仍是 `code_root='D:\\Temp\\tmp9nk6gcnb'`。
        self.addCleanup(C.set_runtime, "code_root", C.get_runtime("code_root"))
        C.set_runtime("code_root", self.tmp.name)
        self.addCleanup(T.clear_read_files)
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


class ReadFileObservationBudgetTests(unittest.TestCase):
    """read_file 的观察预算不能比工具自己的上限还小。

    工具把大文件裁到 12000 字，并在**尾部**留一句补读提示
    （「…需要后续段落请用 start:/end: 指定行号」）。通用裁剪只留头部 →
    提示连同正文尾一起没了：模型既拿不到内容、**也不知道能按行号补读**，
    只能凭半截文件下结论。

    实测（读 agent.py 7796 行）：工具给 12094 字，观察预算 8000 一刀下去
    只剩 8011 字，末尾那句补读提示**没了** —— 工具自己的契约在模型面前破了。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "behaviors"))
        # 一个远超 12000 字的源码文件
        with open(os.path.join(self.tmp.name, "behaviors", "big.gd"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join("line%d %s" % (i, "x" * 60) for i in range(1, 601)))
        self._prev_code_root = C.get_runtime("code_root")
        self.addCleanup(C.set_runtime, "code_root", self._prev_code_root)
        C.set_runtime("code_root", self.tmp.name)
        self.addCleanup(T.clear_read_files)
        T.clear_read_files()

    def test_the_model_still_sees_the_hint_for_reading_the_next_chunk(self):
        from agent import _clip_tool_observation, _observation_budget
        raw = str(T.read_file("path: behaviors/big.gd"))
        self.assertIn("start:/end:", raw, "工具自己没有给出补读提示")
        seen = _clip_tool_observation(raw, _observation_budget("看看这个文件"), "read_file")
        self.assertIn("start:/end:", seen,
                      "补读提示被观察预算砍掉了——模型既没有正文尾，也不知道能按行号续读")
        self.assertIn("已截断", seen, "还必须说清这是截断，不是全文")

    def test_the_whole_read_of_a_big_file_reaches_the_model(self):
        """工具给的 12000 字不该被观察预算再砍一刀 —— 那会让「前 12000 字」这个承诺落空。"""
        from agent import _clip_tool_observation, _observation_budget
        raw = str(T.read_file("path: behaviors/big.gd"))
        seen = _clip_tool_observation(raw, _observation_budget("看看这个文件"), "read_file")
        self.assertGreaterEqual(len(seen), len(raw) - 200,
                                "观察预算把工具自己的输出砍掉了 %d 字"
                                % (len(raw) - len(seen)))


if __name__ == "__main__":
    unittest.main()