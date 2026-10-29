# -*- coding: utf-8 -*-
"""搜索相关性判据 `_query_relevant` —— 此前**一条守卫都没有**。

## 它有多要紧

`tools.py` 的三处候选过滤（1812 / 2022 / 2084）都用它：

    if not _query_relevant(query, row["title"] + " " + row["snippet"],
                           row["title"], row["url"]):
        continue        # 候选被**静默丢弃**，模型根本看不到

而 agent 侧的 `_web_result_relevant` 也走同一个函数。于是它同时决定两件事：

1. 哪些候选能被模型看到；
2. 模型会不会被告知「搜索结果相关性不足」（那条会 `_tool_ok=False`、
   计入熔断额度）。

## 修复前的实测（`.tmp/probe_candidate_drop.py`）

query = 「深度求索 训练成本」，6 个**真实形态**的候选（标题短而通用、摘要是正文节选）：

    带标题否决 -> 存活 0 / 6（5 个完全切题的也被丢）
    只按正文   -> 存活 5 / 6（只丢掉真跑题那个）

英文查询同样中招（`deepseek training cost` + `Wikipedia` 也被丢）。原因：判据要求
**标题或 URL 也命中** `anchor_required`（3 词时需 2 个），而真实标题常常一个都不含。

## 判据口径（用户定的）

**标题/URL 从「否决项」改成「另一条通过路径」**：摘要命中**或**标题/URL 命中，
任一即可。它不再是「摘要命中之后还要再过一关」。

## 反向守卫的必要性

放宽最容易出的错是「干脆全放行」。所以下面守着两条：
- 摘要与标题**都不**命中 -> 仍必须拒绝
- 完全跑题（天气/散步之类）-> 仍必须拒绝
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import _query_relevant  # noqa: E402


def _row(title, snippet, url="https://example.com/x"):
    return title + " " + snippet, title, url


class RelevanceGateTests(unittest.TestCase):
    # ---- 修复前被误伤的那一类：摘要切题、标题通用 ----

    def test_reverse_guard_an_on_topic_snippet_survives_a_generic_title(self):
        """**反向守卫**：摘要在切题时，不得因为标题通用就把候选丢掉。

        这一条在修复前是 False —— 而被丢的候选模型**根本看不到**，
        于是它看到的是「搜索未返回结果」，那条又在 `_FAILURE_MARKERS` 里。
        """
        text, title, url = _row("维基百科", "深度求索公司披露了训练成本与算力规模",
                                "https://zh.wikipedia.org/wiki/DeepSeek")
        self.assertTrue(_query_relevant("深度求索 训练成本", text, title, url))

    def test_the_same_holds_for_english_queries(self):
        """**反向守卫**：英文查询同样中招过（`Wikipedia` + `wiki/DeepSeek` 只命中 1 个词）。"""
        text, title, url = _row("Wikipedia", "DeepSeek disclosed its training cost",
                                "https://en.wikipedia.org/wiki/DeepSeek")
        self.assertTrue(_query_relevant("deepseek training cost", text, title, url))

    def test_a_realistic_result_set_keeps_its_relevant_rows(self):
        """整集视角：修复前是 0/6，全灭。"""
        rows = [
            ("维基百科", "深度求索公司披露了训练成本与算力规模，训练成本约 500 万美元。",
             "https://zh.wikipedia.org/wiki/DeepSeek"),
            ("官方公告", "深度求索公布训练成本：本次训练耗费约 500 万美元。",
             "https://example.com/ds/blog/cost"),
            ("GitHub - deepseek-ai/DeepSeek-V3", "深度求索 V3 训练成本与推理成本对照表。",
             "https://github.com/deepseek-ai"),
        ]
        kept = sum(1 for t, s, u in rows
                   if _query_relevant("深度求索 训练成本", t + " " + s, t, u))
        self.assertEqual(kept, len(rows), "真实形态的结果集不该被全灭")

    # ---- 新的通过路径：标题命中也算 ----

    def test_a_matching_title_alone_is_enough(self):
        """标题/URL 现在是**另一条通过路径**：摘要没命中、标题命中 -> 通过。"""
        text, title, url = _row("深度求索 训练成本 - 官方文档", "本页为导航页。",
                                "https://example.com/ds")
        self.assertTrue(_query_relevant("深度求索 训练成本", text, title, url))

    # ---- 收紧方向的反向守卫：不能因为放宽就全放行 ----

    def test_reverse_guard_off_topic_is_still_rejected(self):
        """**反向守卫**：完全跑题必须仍然拒绝——否则所有结果都会被当成证据。"""
        text, title, url = _row("天气预报", "今天多云转晴，气温 18 到 26 度。",
                                "https://weather.example.com/Beijing")
        self.assertFalse(_query_relevant("深度求索 训练成本", text, title, url))

    def test_reverse_guard_neither_content_nor_anchor_matching_is_rejected(self):
        """**反向守卫**：摘要不命中、标题也不命中 -> 拒绝。"""
        text, title, url = _row("体育新闻", "昨天的比赛很精彩。",
                                "https://sports.example.com")
        self.assertFalse(_query_relevant("深度求索 训练成本", text, title, url))

    def test_reverse_guard_a_long_query_still_needs_a_real_signal(self):
        """词多时 `required` 会变大，但**不能**因此要求标题也命中。"""
        q = "深度求索 训练成本 算力规模 显卡 集群 训练数据"
        text, title, url = _row("维基百科", "深度求索训练成本与算力规模、显卡集群、训练数据说明",
                                "https://zh.wikipedia.org/wiki/DeepSeek")
        self.assertTrue(_query_relevant(q, text, title, url))
        # 「跑题」那个样本第一版写成「深度求索与字面形式的关联条目」——它其实**含**
        # 「深度求索」，而长中文查询有短语回退（正文命中任一 3 字短语即可），
        # 所以判 True 是对的，是我的输入错了。换成真不沾边的正文。
        off = _row("维基百科", "本页为导航页，没有相关内容。",
                   "https://zh.wikipedia.org/wiki/X")
        self.assertFalse(_query_relevant(q, off[0], off[1], off[2]))

    # ---- 中英混合 / 版本号查询：通用修饰词不得抬高门槛 ----

    def test_version_query_with_chinese_title_survives(self):
        """实测误杀：`Godot 4.7 release notes` 的 21 条中文结果曾被全丢。

        博查返回的标题是「Godot 4.7.2来了!57个修复…」——主体词齐全，只是没有
        release/notes 两个英文修饰词。旧口径 matched 1/3、标题命中 1 < 2 → 全灭。
        """
        text, title, url = _row(
            "Godot 4.7.2来了!57个修复让这款开源游戏引擎更稳了",
            "Godot 4.7.2 正式发布，包含 57 项修复与性能改进。",
            "https://www.toutiao.com/article/7530")
        self.assertTrue(_query_relevant("Godot 4.7 release notes", text, title, url))

    def test_version_query_still_rejects_off_topic(self):
        """**反向守卫**：去掉修饰词不等于全放行 —— 跑题结果仍须拒绝。"""
        text, title, url = _row("今日天气", "今天多云转晴，气温 18 到 26 度。",
                                "https://weather.example.com/Beijing")
        self.assertFalse(_query_relevant("Godot 4.7 release notes", text, title, url))

    def test_all_generic_query_falls_back_to_the_full_term_set(self):
        """全是通用词时退回原词表，门不能因此变成恒真。"""
        text, title, url = _row("天气", "今天多云", "https://weather.example.com")
        self.assertFalse(_query_relevant("release notes guide", text, title, url))

    # ---- 既有语义不许动 ----

    def test_a_single_term_query_is_always_relevant(self):
        """既有语义：单词查询直接放行（词数 <= 1 时不做判定）。"""
        self.assertTrue(_query_relevant("transformer", "anything at all"))

    def test_no_title_and_url_means_content_only(self):
        """不传 title/url 时只看内容——agent 侧那条路径就是这个形状。"""
        self.assertTrue(_query_relevant("深度求索 训练成本", "深度求索训练成本说明"))
        self.assertFalse(_query_relevant("深度求索 训练成本", "今天天气不错"))

    def test_junk_inputs_do_not_raise(self):
        for bad in (None, "", 0, [], {}):
            with self.subTest(bad=bad):
                self.assertIsInstance(_query_relevant(bad, bad, bad, bad), bool)


if __name__ == "__main__":
    unittest.main()