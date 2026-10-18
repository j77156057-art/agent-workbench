"""BUG-14 回归锁：`_is_raw_source_echo` 必须认**竖线**行号格式。

背景：判据此前只把 `.` `)` `:` 当行号分隔符（`^\\s*\\d+[\\.\\):]\\s+\\S`）。但仓库里
`search_code` 产出的是 gutter 格式 `   45| code`，模型 echo 带行号源码时也偏爱
编辑器竖线风格 `45 | code`——这两种竖线形态**数学上永不匹配**旧正则。结果：
:8000 现场让模型「逐行原样列出 enemy.gd」，它吐通篇 `45 | velocity.x = 0.0`，
覆盖率≈100% →「覆盖存疑」不弹（预期），但共享同一判据的 P1-2 终态门与 P1-3
「未综合结论」徽章**也都不弹**（不该）——真 dump 整段漏网。

修法：分隔符字符类加 `|`、并容忍数字与分隔符之间的空白（`^\\s*\\d+\\s*[.):|]\\s+\\S`）。
本测试把三种命中形态（冒号/点/gutter竖线/带空格竖线）与两类负例（散文、纯数字）
钉死，防止回归。此前那批 fixture 全用冒号格式，正是"测试形态≠生产形态"的教训。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent import _is_raw_source_echo  # noqa: E402


def _lines(fmt, n=24):
    return "\n".join(fmt % i for i in range(1, n + 1))


class RawSourceEchoFormatTests(unittest.TestCase):
    def test_colon_format_still_detected(self):
        # read_file 的 `{i}: {ln}` 冒号格式——既有形态，不能因放宽而回退。
        self.assertTrue(_is_raw_source_echo(_lines("%d: velocity.x = 0.0")))

    def test_dot_and_paren_detected(self):
        self.assertTrue(_is_raw_source_echo(_lines("%d. velocity.x = 0.0")))
        self.assertTrue(_is_raw_source_echo(_lines("%d) velocity.x = 0.0")))

    def test_pipe_with_space_detected(self):
        # 模型 echo 的编辑器风格 `45 | code`（BUG-14 现场形态）。
        self.assertTrue(_is_raw_source_echo(_lines("%d | velocity.x = 0.0")))

    def test_gutter_pipe_no_leading_gap_detected(self):
        # search_code 的 `{n:>5}| code`：右对齐 + 竖线紧跟数字、无空格。
        self.assertTrue(_is_raw_source_echo(_lines("%5d| velocity.x = 0.0")))

    def test_prose_conclusion_not_flagged(self):
        prose = ("这套逻辑整体是敌人受击后扣血并播放动画。9.9 更大。"
                 "CONTACT_DAMAGE 是伤害常量，只在 take_damage 里用到。")
        self.assertFalse(_is_raw_source_echo(prose), "正常综合结论绝不能被误判成 dump")

    def test_pure_number_list_not_flagged(self):
        # 纯数字、无「分隔符+空格+代码」结构 → 不算源码清单。
        self.assertFalse(_is_raw_source_echo(_lines("%d")))

    def test_short_dump_below_min_lines_not_flagged(self):
        # 行数 < _DUMP_MIN_LINES 时放行，避免误伤"贴两三行举例"的正常回答。
        self.assertFalse(_is_raw_source_echo("1 | a = 1\n2 | b = 2\n3 | c = 3"))


if __name__ == "__main__":
    unittest.main()
