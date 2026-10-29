# -*- coding: utf-8 -*-
"""搜索主题重排 + 泛词稀释时的主题词补搜。

实测（2026-10-29）：query=「GDScript typed array 类型数组」，博查 22 条结果
全是 JS/TS/文言教程，**没有一条**包含 gdscript —— 词面过滤会保留它们，因为
typed/array/类型/数组 都命中了。这不是过滤问题，是查询被中文泛词稀释：
- 直接用 `GDScript typed array` 搜能拿到 godotengine 的 GitHub issue；
- 用 `gdscript` 单搜能拿到 28 条 GDScript 结果。
所以修法分两层：IDF 加权重排（专有名词优先）+ 仅在主题词整体缺席时补搜一次。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402


def _row(title, snippet, url):
    return "· %s\n  %s\n  %s" % (title, snippet, url)


def _rows(*items):
    return "\n".join(_row(*item) for item in items)


class RankingTests(unittest.TestCase):
    def test_specific_topic_row_beats_generic_lookalike(self):
        js = {"title": "JavaScript 类型数组 TypedArray 教程",
              "snippet": "js array 类型数组", "url": "https://www.php.cn/faq/1.html"}
        gd = {"title": "GDScript 类型数组 typed array",
              "snippet": "gdscript typed array 类型数组 用法",
              "url": "https://docs.godotengine.org/zh-cn/4.x/gdscript.html"}
        ranked = tools._rank_search_rows("GDScript typed array 类型数组", [js, gd])
        self.assertIs(ranked[0], gd)

    def test_focus_terms_drop_generic_modifiers(self):
        self.assertEqual(tools._focus_terms("GDScript typed array 类型数组"),
                         ["gdscript", "typed", "array"])
        self.assertEqual(tools._focus_terms("Godot 4.7 release notes"), ["godot"])
        self.assertEqual(tools._focus_terms("深度求索 训练成本"), [])

    def test_retry_recovers_the_diluted_topic(self):
        original = _rows(
            ("JavaScript 类型数组 TypedArray 教程", "js array 类型数组", "https://www.php.cn/faq/1.html"),
            ("TypeScript 数组类型", "typescript array 类型", "https://juejin.cn/post/1"),
        )
        extra = _row("[GDScript 2.0] Adding typed arrays bad logic · Issue #5248",
                     "gdscript typed array",
                     "https://github.com/godotengine/godot/issues/5248")
        calls = []

        def fake_api(provider, query):
            calls.append((provider, query))
            return extra

        with mock.patch.object(tools, "_api_web_search", fake_api):
            out = tools._rerank_and_focus("bocha", "GDScript typed array 类型数组", original)
        self.assertEqual(calls, [("bocha", "gdscript typed array")],
                         "补搜只能用去掉泛词后的主题词")
        first = out.split("· ")[1].splitlines()[0]
        self.assertIn("GDScript", first)

    def test_no_retry_when_the_topic_term_is_already_present(self):
        original = _rows(
            ("Godot 4.7 release notes", "godot release notes", "https://godotengine.org/news"),
            ("Godot 4.7 发布说明", "godot 发布", "https://example.com/godot-47"),
        )
        with mock.patch.object(tools, "_api_web_search") as api:
            out = tools._rerank_and_focus("bocha", "Godot 4.7 release notes", original)
        api.assert_not_called()
        self.assertEqual(out, original, "顺序没变时应原样返回，不重排格式")

    def test_retry_keeps_the_diagnostic_header(self):
        original = "站内搜索不可用，已回退通用搜索：\n" + _rows(
            ("JavaScript 类型数组", "js array", "https://www.php.cn/faq/1.html"),
            ("TypeScript 数组", "ts array", "https://juejin.cn/post/1"),
        )
        extra = _row("GDScript typed array", "gdscript typed array",
                     "https://github.com/godotengine/godot/issues/5248")
        with mock.patch.object(tools, "_api_web_search", return_value=extra):
            out = tools._rerank_and_focus("bocha", "GDScript typed array 类型数组", original)
        self.assertTrue(out.startswith("站内搜索不可用，已回退通用搜索："))

    def test_single_row_never_triggers_retry(self):
        with mock.patch.object(tools, "_api_web_search") as api:
            tools._rerank_and_focus("bocha", "GDScript typed array 类型数组",
                                    _row("JavaScript 类型数组", "js array", "https://x.cn/1"))
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
