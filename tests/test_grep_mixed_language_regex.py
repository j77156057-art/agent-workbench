"""grep 必须原样执行「中英混合正则」，不能把它降级成首个标识符。

## 实测故障（2026-10-25，godot_sample 问「新手引导流程涉及哪些文件」）

模型发出的搜索是：

    (?i)tutorial|onboard|guide|新手|引导|教学|hint|tip
    path: behaviors/

`_clean_symbol` 检测到中文后，按「符号 + 中文说明」的兜底逻辑提取首个
ASCII 标识符——而 `(?i)` 里的 `i` 正是第一个。整条搜索变成 `grep("i")`，
几乎每一行都命中，返回整份源码。模型看到"有结果"，换关键词再搜，又被
降成 `i`；单轮实测 223 次 grep，直到用户中断。

判据必须落在**实际返回的观察**上：混合正则若无命中，应明确说「未匹配到」，
而不是把整文件内容当命中；若有命中，只返回真正匹配的行。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tools  # noqa: E402


class GrepMixedLanguageRegexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "behaviors"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp.name, "ui"), exist_ok=True)
        self._write("behaviors/enemy.gd", [
            "const CONTACT_DAMAGE: int = 10",
            "func _ready() -> void:",
            "\tpass",
        ])
        self._write("ui/hud.gd", [
            'var 新手引导提示 := "按 WASD 移动"',
            "var item := 1",
            "func seekTo() -> void:",
            "\tpass",
        ])
        patcher = patch.object(tools, "_get_code_root",
                               return_value=self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, rel, lines):
        with open(os.path.join(self.tmp.name, rel.replace("/", os.sep)),
                  "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def test_the_exact_incident_no_longer_returns_the_whole_file(self):
        """事故原参数：无命中就必须说未匹配，不能返回 enemy.gd 的正文。"""
        out = tools.grep(
            "(?i)tutorial|onboard|guide|新手|引导|教学|hint|tip\n"
            "path: behaviors/")
        self.assertIn("未匹配到", out, out[:300])
        self.assertNotIn("CONTACT_DAMAGE", out,
                         "混合正则又被降成了 grep(\"i\")，整文件都成了命中")

    def test_chinese_alternative_matches_only_the_real_line(self):
        """中文备选词必须真的参与匹配，而不是退化成匹配字母 i。"""
        out = tools.grep("(?i)新手|引导\npath: ui/")
        self.assertIn("新手引导提示", out, out[:300])
        self.assertNotIn("var item := 1", out,
                         "非匹配行被带出来了——说明实际执行的不是这条正则")
        self.assertNotIn("func seekTo", out)

    def test_plain_ascii_regex_is_unchanged(self):
        out = tools.grep("(?i)contact_damage\npath: behaviors/")
        self.assertIn("CONTACT_DAMAGE", out, out[:300])

    def test_symbol_plus_chinese_prose_still_extracts_the_symbol(self):
        """原有兜底不能退化：`seekTo 进行进度跳转` 仍应只搜 seekTo。"""
        out = tools.grep("seekTo 进行进度跳转\npath: ui/")
        self.assertIn("func seekTo", out, out[:300])

    def test_clean_symbol_unit_cases(self):
        mixed = "(?i)tutorial|onboard|新手|引导"
        self.assertEqual(tools._clean_symbol(mixed, regex_literal=True), mixed)
        self.assertEqual(tools._clean_symbol("seekTo 进行进度跳转"), "seekTo")
        # find_references 这类非 grep 调用仍取符号，但不能取到 (?i) 里的 i。
        self.assertEqual(tools._clean_symbol("(?i)foo 的引用点"), "foo")


if __name__ == "__main__":
    unittest.main()
