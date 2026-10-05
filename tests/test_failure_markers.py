# -*- coding: utf-8 -*-
"""工具失败文案白名单（agent._FAILURE_MARKERS）回归。

守住的这一类问题：工具的失败观察若不命中 _FAILURE_MARKERS，会被 _is_failure 误判为
成功 —— 不触发自我反思、trace 的 ok 也误标为 True。这里用表驱动把所有已知失败文案
一次性钉死，新增工具失败文案时同步补此表。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent


# (说明, 观察文案, 期望 is_failure)
FAILURE_CASES = [
    ("web_search 双后端硬异常", "搜索失败: bing: OSError: boom（DuckDuckGo/Bing 均不可达…）", True),
    ("web_search 无结果", "搜索未返回结果，可能是网络受限或该关键词无结果。", True),
    ("web_fetch 网络失败", "网页读取失败：TimeoutError: late", True),
    ("web_fetch 协议非法", "网页读取失败：只允许 http/https。", True),
    ("calculate 运行异常", "计算失败: division by zero", True),
    ("calculate 非法字符", "表达式包含非法字符，已拒绝执行。", True),
    ("read_file 读取异常", "读取失败: PermissionError: [Errno 13] access denied", True),
    ("read_file 文件缺失", "文件不存在：src/missing.py", True),
    ("read_file 路径越界", "拒绝访问：../../etc/passwd 不在代码根目录内。", True),
    ("空参缺输入", "未提供搜索关键词。", True),
    ("工具入参缺失", "参数缺失：请提供分区清单。可传 JSON 数组…", True),
    ("工具入参格式错", "参数缺失：请提供 regions: <JSON 数组或 {\"regions\":[...]} 分区清单>。", True),
    ("安全策略拦截", "安全限制：该操作被拒绝。", True),
    ("写操作护栏", "拒绝写入：未授权写操作。", True),
]

# 「检索没有匹配」是**合法答案**，不是失败（用户定的口径）。
#
# 这条以前被钉成失败（`("search_code 无命中", "未找到相关内容。", True)`），
# 后果实测得到：`_is_failure` 命中 -> `_tool_ok=False` ->
# `_TOOL_FAIL_LIMIT=3` 熔断 -> 子代理拿 3 次空结果就强制收尾，还给出
# 「工具 search_code 已连续失败 3 次，模型未能换出可用的调用方式」这种
# **误导性**结论——真实原因只是「确实没有匹配」。
NO_MATCH_CASES = [
    ("search_code 无命中", "代码库未找到相关内容，建议改用 grep 搜索关键词或 read_file 查看具体文件。"),
    ("search_code 无命中（短文案）", "未找到相关内容。"),
    ("search_knowledge 无命中", "知识库中未找到相关内容。"),
]

#: 「无匹配」文案里**同时**含有真失败标记时，仍要判为失败。
#:
#: 这是本族最容易出的错：为了修「无匹配不算失败」而把判定放宽成
#: 「只要沾上『未找到』就不算失败」，于是
#: 「未找到相关内容：索引读取失败」这类**真的**失败被放过，模型无限重试。
MIXED_CASES = [
    ("无匹配 + 真实失败标记", "未找到相关内容：索引读取失败，PermissionError。", True),
    ("无匹配 + 文件不存在", "未找到相关内容：文件不存在：src/missing.py", True),
]

# 正常文案必须判为非失败，否则会误触发反思、打断正常收尾
NORMAL_CASES = [
    ("检索命中", "已找到 2 处匹配：a.py:12、b.py:34", False),
    ("网页正文", "网页内容如下：Hello world", False),
    ("计算结果回填", "计算结果为：2", False),
]


class FailureMarkerTableTests(unittest.TestCase):
    def test_all_failure_texts_detected(self):
        for name, text, expected in FAILURE_CASES:
            with self.subTest(case=name):
                self.assertIs(
                    agent._is_failure(text), expected,
                    f"{name} 应判为失败：{text!r}",
                )

    def test_normal_texts_not_failure(self):
        for name, text, expected in NORMAL_CASES:
            with self.subTest(case=name):
                self.assertIs(
                    agent._is_failure(text), expected,
                    f"{name} 不应判为失败：{text!r}",
                )

    def test_no_match_is_not_a_failure(self):
        """「检索没有匹配」是合法答案。

        判错的代价很具体：`_is_failure` 命中 -> `_tool_ok=False` ->
        `_TOOL_FAIL_LIMIT=3` 熔断 -> 子代理提前收尾，且结论误导
        （「模型未能换出可用的调用方式」，而真因是「确实没有匹配」）。
        """
        for name, text in NO_MATCH_CASES:
            with self.subTest(case=name):
                self.assertFalse(
                    agent._is_failure(text),
                    f"{name} 是合法答案，不该判为失败：{text!r}",
                )

    def test_reverse_guard_a_no_match_text_carrying_a_real_failure_still_fails(self):
        """**反向守卫**：不许为了修「无匹配」把判定放宽成「沾上『未找到』就算成功」。

        「未找到相关内容：索引读取失败」里那个真失败必须仍然算数，否则模型会
        对着一个必然失败的调用无限重试。
        """
        for name, text, expected in MIXED_CASES:
            with self.subTest(case=name):
                self.assertIs(
                    agent._is_failure(text), expected,
                    f"{name} 判错：{text!r}",
                )

    def test_every_no_match_marker_really_comes_from_a_tool(self):
        """**反向守卫**：`_NO_MATCH_MARKERS` 里的每一条都必须**真的出现在
        `tools.py` 源码里**。

        没有这条，改一次文案就会让「无匹配」悄悄退回失败形态，而所有单测照绿
        ——判据与实现脱节，正是 `_FAILURE_MARKERS` 当年靠注释约定、
        没有守卫而漂移的同一个坑。
        """
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools.py"), encoding="utf-8").read()
        for marker in agent._NO_MATCH_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(
                    marker, src,
                    "「无匹配」标记 %r 在 tools.py 里已不存在——工具改文案了，"
                    "这条标记会让判定悄悄失效" % marker,
                )

    def test_empty_observation_is_failure(self):
        # 既有语义：空/空白观察一律视为失败
        self.assertTrue(agent._is_failure(""))
        self.assertTrue(agent._is_failure("   "))
        self.assertTrue(agent._is_failure(None))

    def test_markers_cover_every_table_entry(self):
        # 反向校验：表中每条失败文案都必须真的命中 _FAILURE_MARKERS 中的某个子串，
        # 防止 _is_failure 逻辑变动后表与实现脱节。
        for name, text, expected in FAILURE_CASES:
            if not expected:
                continue
            with self.subTest(case=name):
                self.assertTrue(
                    any(m in text for m in agent._FAILURE_MARKERS),
                    f"{name} 未命中任何 _FAILURE_MARKERS：{text!r}",
                )


if __name__ == "__main__":
    unittest.main()
