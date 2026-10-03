"""工具的**参数语法**必须容得下模型实际写出的形态——一类"死在纯语法上"的兜底。

## 实测故障（godot_sample 定位"扣血在哪算"）

    定位代码  pattern: take_damage|CONTACT_DAMAGE  -> behaviors\\enemy.gd:7: CONTACT_DAMAGE = 10
    读取文件  path: behaviors/player.gd start: 1 end: 55   -> 文件不存在：behaviors/player.gd start: 1 end: 55
    读取文件  path: behaviors\\enemy.gd  start: 1 end: 50   -> 文件不存在：...
    读取文件  path: behaviors/player.gd start: 1 end: 52   -> 文件不存在：...
    读取文件  path: behaviors\\enemy.gd  start: 1 ...      -> 文件不存在：...

模型**文件定位是对的**（grep 命中了正确的文件与行号），却因为 `read_file` 不认
行内 `start:` / `end:` 而四次全部打不开。

而报错把整串原样回显成路径 —— 看起来像路径写错，于是模型去换分隔符、
再试四次，全失败。最后归因成"上游/中转返回空内容，直接重试通常就能拿到"。

**重试多少次都没用**：文件一直是存在的。

## 根因：两个工具的解析器不一致

`grep` 的参数解析器本来就有行内处理（`_RE_GREP_SCOPE_INLINE` / `_RE_GREP_PATTERN_KEY`），
`read_file` 却只认「独占一行」。工具说明写的是独占行，模型写的是行内，
而模型才是实际调用方。

## 与本项目其它 bug 同形

失败信号指向的对象不是真正的原因：分隔符？项目根？provider？都不是——是参数语法。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402
import tools  # noqa: E402


class ReadFileArgumentSyntaxTests(unittest.TestCase):
    """模型实际会写的几种写法，都必须能打开同一个存在的文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "behaviors"), exist_ok=True)
        self.target = os.path.join(self.tmp.name, "behaviors", "player.gd")
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write("\n".join("line%d" % i for i in range(1, 121)))
        patcher = patch.object(tools, "_get_code_root", return_value=self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _opens(self, arg):
        out = tools.read_file(arg)
        text = out if isinstance(out, str) else str(out)
        return "不存在" not in text and "拒绝访问" not in text and bool(text.strip())

    def test_inline_start_and_end(self):
        """本次故障的**确切写法**：模型把区间参数写在同一行。"""
        self.assertTrue(
            self._opens("behaviors/player.gd start: 1 end: 55"),
            tools.read_file("behaviors/player.gd start: 1 end: 55")[:90])

    def test_inline_with_backslash_separator(self):
        self.assertTrue(self._opens("behaviors\\player.gd start: 1 end: 55"))

    def test_inline_start_only(self):
        self.assertTrue(self._opens("behaviors/player.gd start: 1"))

    def test_comma_separated(self):
        """grep 早就能认这种写法——两个工具不该不一致。"""
        self.assertTrue(self._opens("behaviors/player.gd, start: 1, end: 55"))

    def test_newline_separated_still_works(self):
        """工具说明里的原写法不能被这次改动破坏。"""
        self.assertTrue(self._opens("behaviors/player.gd\nstart: 1\nend: 55"))

    def test_no_range_still_works(self):
        self.assertTrue(self._opens("behaviors/player.gd"))

    def test_the_range_is_actually_applied(self):
        """不只是"没报错"——区间必须真的生效，否则就成了放行一切。"""
        out = tools.read_file("behaviors/player.gd start: 10 end: 12")
        self.assertIn("line10", out)
        self.assertIn("line12", out)
        self.assertNotIn("line13", out,
                         "end: 没生效——宽容解析不能变成忽略区间参数")

    def test_a_genuinely_missing_file_still_says_so(self):
        """容错不等于吞掉真错误。"""
        out = tools.read_file("behaviors/nope.gd start: 1 end: 5")
        self.assertIn("文件不存在", out)
        self.assertNotIn("区间参数", out,
                         "路径真的不存在时不该提区间参数——那会把人引向错误的排查方向")


class UnrecognisedRangeSyntaxIsNamedTests(unittest.TestCase):
    """认不出来的写法必须**点名**，否则报错会把语法问题伪装成路径问题。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "behaviors"), exist_ok=True)
        with open(os.path.join(self.tmp.name, "behaviors", "player.gd"),
                  "w", encoding="utf-8") as handle:
            handle.write("x\n")
        patcher = patch.object(tools, "_get_code_root", return_value=self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_equals_syntax_is_flagged_as_syntax_not_path(self):
        out = tools.read_file("behaviors/player.gd start=1")
        self.assertIn("区间参数", out,
                      "start=1 没被识别时，报错必须说明是区间参数写法问题——"
                      "否则「文件不存在：behaviors/player.gd start=1」会让人以为是路径错了")

    def test_line_range_shorthand_is_flagged(self):
        out = tools.read_file("behaviors/player.gd 行1-55")
        self.assertIn("区间参数", out)


class EmptyResponseBlamesTheToolFailuresTests(unittest.TestCase):
    """空响应的归因必须把「工具反复失败」摆出来，而不是只让人去查 provider。"""

    def test_the_message_names_the_failing_tool(self):
        import inspect
        src = inspect.getsource(agent_mod.Agent._run)
        self.assertIn("次工具调用失败", src,
                      "工具连续失败时，空响应文案必须点出它——"
                      "只让用户查 provider 会把人引向错误方向")
        self.assertIn("最常失败", src,
                      "必须点名最常失败的工具，否则用户还得自己翻 trace")

    def test_it_does_not_assert_the_provider_is_the_cause(self):
        """原文案说「这多半不是你的问题写得不清楚」——在没有证据时过于笃定。"""
        import inspect
        src = inspect.getsource(agent_mod.Agent._run)
        self.assertIn("只是**结果**，不是原因", src,
                      "必须把「上游返回空内容」降级为结果而非原因，"
                      "否则工具失败时用户会反复重试，而重试多少次都没用")


if __name__ == "__main__":
    unittest.main()