# -*- coding: utf-8 -*-
"""_fit_budget 的 head 裁剪正确性回归测试（全部离线，不触网）。

钉死 2026-10-27 修复的行为：极端小预算、空 trail（逼 head 分支）时，
_fit_budget 只能在「固定上下文块之后、当前问题之前」这段历史问答对区间成对删除，
绝不触碰固定块（渠道 system / 时间守卫 / 各 system 注入 / 早期对话摘要 /
memory_context），也绝不删最后一条当前问题。

旧实现把固定块长度硬当成 2，del head[2:4] 会先吃掉项目规则/路由/摘要等
关键 system 注入，级联下去还可能啃掉当前问题——本测就是防它复发。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
import sessions  # noqa: E402


class _FakeLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self, prompt_budget=0):
        self.capability = {"context_window": 32768, "thinking": "none", "cloud": False}
        self.prompt_budget = prompt_budget

    def chat(self, messages, stream=True, **kw):  # 不会被 _fit_budget 用到
        return "Final Answer: ok"

    def count_tokens(self, text):
        # 确定性：token 数 = 字符数，便于用极小预算逼出裁剪
        return len(text or "")


class FitBudgetHeadPruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_fit_")
        self._old_dir = sessions.SESSIONS_DIR
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "sessions")

    def tearDown(self):
        sessions.SESSIONS_DIR = self._old_dir

    def _agent(self, n_history):
        # 极小预算：固定块 + 问题本身就超，裁剪必然到地板
        a = agent_mod.Agent(llm=_FakeLLM(prompt_budget=50), session_id="fit")
        # 早期对话摘要——它是压缩后唯一记录更早轮次的载体，绝不能被 head 分支吃掉
        a.summary = "EARLY_SUMMARY_MARKER_必须存活 早前我们确认了 ALPHA-7731-OMEGA 约定"
        a.history = [
            {"user": "u%d" % i + "x" * 40, "assistant": "a%d" % i + "y" * 40, "ts": ""}
            for i in range(n_history)
        ]
        return a

    def test_extreme_tiny_budget_keeps_fixed_block_and_question(self):
        a = self._agent(10)
        question = "CURRENT_QUESTION_MARKER_必须存活并排最后"
        head = a._build_messages(question)
        hist_start = a._head_fixed_end
        # 快照固定块（含两条基础 system、各注入、摘要、memory）的原文
        fixed_before = [m.get("content", "") for m in head[:hist_start]]
        self.assertGreaterEqual(len(fixed_before), 3,
                                "固定块应远不止 2 条（旧 bug 正是按 2 假设裁的）")
        # 当前问题确在末尾
        self.assertEqual(head[-1].get("content"), question)

        result = a._fit_budget(head, [])
        result_contents = [m.get("content", "") for m in result]

        # 1) 当前问题必须在，且仍是最后一条
        self.assertTrue(result, "裁剪后不应为空")
        self.assertEqual(result[-1].get("content"), question,
                         "当前问题绝不能被裁掉，且必须稳居末尾")
        # 2) 固定块每一条原文都还在、且相对顺序不变（作为结果的前缀）
        self.assertEqual(result_contents[:len(fixed_before)], fixed_before,
                         "固定上下文块（system/摘要/memory）不得被裁剪或乱序")
        # 3) 早期摘要那条 system 明确存活
        self.assertTrue(any("EARLY_SUMMARY_MARKER_必须存活" in c for c in result_contents),
                        "早期对话摘要必须保留，否则跨轮记忆彻底丢失")

    def test_prunes_only_history_pairs_never_orphan_assistant_first(self):
        # 若干历史对：裁剪后结果里，固定块之后的历史段必须仍以 user 打头成对出现，
        # 不能出现「assistant 紧跟固定块」这种 assistant-first 非法序列。
        a = self._agent(8)
        question = "Q_last"
        head = a._build_messages(question)
        hist_start = a._head_fixed_end
        result = a._fit_budget(head, [])
        tail = result[hist_start:]  # 固定块之后（含可能的历史残留 + 当前问题）
        hist_msgs = tail[:-1]
        for m in hist_msgs:
            self.assertIn(m.get("role"), ("user", "assistant"))
        if hist_msgs:
            self.assertEqual(hist_msgs[0].get("role"), "user",
                             "历史段必须 user 打头，避免 assistant-first 非法序列")

    def test_boundary_attribute_is_set_by_build_messages(self):
        a = self._agent(3)
        a._build_messages("hello")
        self.assertTrue(isinstance(getattr(a, "_head_fixed_end", None), int))
        self.assertGreaterEqual(a._head_fixed_end, 1)


if __name__ == "__main__":
    unittest.main()
