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


class PathArgumentIsConsistentAcrossToolsTests(unittest.TestCase):
    """三个吃路径的工具，对"路径该怎么写"必须**一致**。

    ## 为什么这条比逐个测试更重要

    同一个问题在本次会话里连撞了三层，每层都是"上一层修好、下一层撞墙"：

    | 层 | 模型写法 | 谁认 | 谁不认 |
    |---|---|---|---|
    | 1 | `a.gd start: 1 end: 55`（行内） | grep | read_file |
    | 2 | `path: a.gd start: 1`（多一个键名） | grep / list_dir | read_file |
    | 3 | 待观察 | ? | ? |

    每层都是靠**用户拿真实 trace 来撞**才发现的，而不是被任何测试挡住。

    所以这里不逐个列举写法，而是直接断言：`read_file` 必须接受 `list_dir` 与
    `grep` 已经接受的那些形态。下一层若是这三者之一先坏掉，这条会红；
    若是 read_file 又漏了新形态，下面那份「形态清单」会红。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "behaviors"), exist_ok=True)
        for name in ("player.gd", "enemy.gd"):
            with open(os.path.join(self.tmp.name, "behaviors", name),
                      "w", encoding="utf-8") as handle:
                handle.write("\n".join("line%d" % i for i in range(1, 61)))
        for fn in ("_get_code_root",):
            patcher = patch.object(tools, fn, return_value=self.tmp.name)
            patcher.start()
            self.addCleanup(patcher.stop)
        tools.clear_read_files()

    def _ok(self, text):
        return "不存在" not in text and "拒绝访问" not in text and bool(text.strip())

    def test_read_file_accepts_the_key_prefix_that_list_dir_accepts(self):
        """`path: a.gd` —— list_dir 认、grep 认，只有 read_file 不认。

        实测（godot_sample 定位扣血逻辑）：模型写
        `path: behaviors/player.gd start: 1 end: 55`，报「文件不存在：path:
        behaviors/player.gd」——而那个文件明明就在那儿。
        """
        bare = tools.read_file("behaviors/player.gd")
        keyed = tools.read_file("path: behaviors/player.gd")
        self.assertTrue(self._ok(bare))
        self.assertTrue(
            self._ok(keyed),
            "read_file 不接受 `path:` 键前缀，而 list_dir 与 grep 都接受——"
            "模型按后两者的写法调用 read_file 就必然失败：%s" % keyed[:80])

    def test_the_error_message_must_not_echo_the_key_prefix(self):
        """真·文件不存在时，报错里不该留着 `path:` 这种键名。

        「文件不存在：path: xxx」看起来像路径写错，实际上路径是对的、只是多了个键名。
        这种报错会把模型引向换路径/换分隔符，而真实原因是它自己的参数写法。
        """
        out = tools.read_file("path: behaviors/nope.gd")
        self.assertIn("文件不存在", out)
        self.assertNotIn(
            "path:", out.replace("path: behaviors/nope.gd", ""),
            "报错把键名原样回显成路径的一部分，看起来像路径写错了：%r" % out[:90])

    def test_key_prefix_with_range_is_still_accepted(self):
        """本次 trace 里的**确切写法**。"""
        out = tools.read_file("path: behaviors/player.gd start: 1 end: 55")
        self.assertTrue(self._ok(out), out[:90])
        self.assertIn("line55", out, "键名剥掉后区间必须照常生效")

    def test_a_windows_drive_letter_is_not_mistaken_for_a_key(self):
        """`C:\\...` 是盘符，不是 `file:` 键名——绝对路径不能被误剥。"""
        out = tools.read_file("C:\\definitely\\not\\here.gd")
        # 项目外的绝对路径走越界门禁（本项目的正确行为）。关键是整条路径**原样**
        # 出现在报错里——若被误认成 `file:` 键剥成 "C"，这里会变成 "不在代码根目录内：C"。
        self.assertIn("C:\\definitely\\not\\here.gd", out,
                      "盘符路径被键名规则误伤了：%r" % out[:90])

    def test_other_key_spellings_and_case_are_accepted(self):
        """`file:` / `filename:` / 大写 —— 键名规则别只认一种拼法。"""
        for arg in ("file: behaviors/player.gd",
                    "filename: behaviors/player.gd",
                    "FILE: behaviors/player.gd",
                    "Path: behaviors/player.gd"):
            self.assertTrue(self._ok(tools.read_file(arg)),
                            "%r 未被接受：%s" % (arg, tools.read_file(arg)[:70]))


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