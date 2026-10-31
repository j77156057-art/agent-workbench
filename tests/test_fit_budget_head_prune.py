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
        # 裁剪后，可裁区间 [_head_fixed_end, 尾部保护区) 内必须仍以 user 打头成对出现，
        # 不能出现「assistant 紧跟固定块」这种 assistant-first 非法序列；尾部那条
        # 「当前时间」system 与当前问题一样永不可裁（2026-10-31 两边界契约）。
        a = self._agent(8)
        question = "Q_last"
        head = a._build_messages(question)
        hist_start = a._head_fixed_end
        result = a._fit_budget(head, [])
        self.assertEqual(result[-1].get("content"), question)
        self.assertIn("当前时间", str(result[-2].get("content")),
                      "尾部时间块必须存活且紧贴当前问题")
        self.assertEqual(result[-2].get("role"), "system")
        hist_msgs = result[hist_start:-2]
        for m in hist_msgs:
            self.assertIn(m.get("role"), ("user", "assistant"))
        if hist_msgs:
            self.assertEqual(hist_msgs[0].get("role"), "user",
                             "历史段必须 user 打头，避免 assistant-first 非法序列")

    def test_partial_prune_keeps_whole_units_including_resume_triples(self):
        """**部分**裁剪（不是裁到地板）下，存活区间必须恰好等于「最近 K 个完整单元」。

        为什么不能只用极小预算裁到地板：地板会把历史全删光，删除顺序里的错位/孤儿
        在最终结果里被「自愈」掉，变异（把 3 条单元当 2 条删）照样全绿 —— 第一版
        测试就是这么没牙的。这里把预算精确设成「固定块+尾部+最近 3 单元」的 token
        数，逼裁剪器停在历史中间，任何拆单元的行为都会让存活切片对不上。
        """
        a = self._agent(0)
        a.llm.prompt_budget = 100000  # 先让历史滑窗放下全部 6 单元，再收紧逼裁剪
        a.history = [
            {"user": "u%d" % i + "x" * 60, "assistant": "a%d" % i + "y" * 60,
             "resume_context": ("现场%d" % i) if i in (0, 3, 5) else "", "ts": ""}
            for i in range(6)
        ]
        head = a._build_messages("Q_partial")
        hs, ts = a._head_fixed_end, a._head_tail_start
        region = head[hs:ts]
        units = []
        for m in region:
            is_resume = str(m.get("content", "")).startswith(agent_mod._RESUME_CONTEXT_MARK)
            if m.get("role") == "user" and not is_resume:
                units.append([m])
            elif units:
                units[-1].append(m)
        self.assertEqual(len(units), 6, "历史应被切成 6 个单元（3 个带 resume）")
        # 尺寸刻意取 [3,2,2,3,2,3]：待删的前 3 单元共 7 条（奇数），成对删除不可能
        # 恰好删完它们 —— 对称尺寸（如 3,2,3,2,3,2）会让拆单元删法碰巧重新对齐，
        # 变异就杀不掉（第一版正是这么漏的）。
        self.assertEqual([len(u) for u in units], [3, 2, 2, 3, 2, 3])
        keep = [m for unit in units[-3:] for m in unit]
        desired = head[:hs] + keep + head[ts:]
        a.llm.prompt_budget = a._prompt_tokens(desired, [])
        result = a._fit_budget(head, [])
        self.assertEqual([(m.get("role"), m.get("content")) for m in result],
                         [(m.get("role"), m.get("content")) for m in desired],
                         "部分裁剪必须整单元删：存活切片对不上说明单元被拆着删了")

    def test_cross_turn_prefix_is_stable_for_provider_cache(self):
        """跨回合前缀缓存的收益证明：第二轮的 head 必须把第一轮「固定块+全部历史」
        原样当前缀，分叉只允许发生在尾部（时间块 + 新问题）。

        时间块还在固定头区第 3 条时，公共前缀长度只有 2 —— 历史明明只在尾部追加，
        却整段无法命中 provider 缓存（按回合命中率 12.91% 的根因）。
        """
        a = self._agent(0)
        a.history = [{"user": "h-u%d" % i, "assistant": "h-a%d" % i, "ts": ""}
                     for i in range(3)]
        head1 = a._build_messages("第一问")
        a.history = a.history + [{"user": "第一问", "assistant": "第一答", "ts": ""}]
        head2 = a._build_messages("第二问")
        common = 0
        for m1, m2 in zip(head1, head2):
            if m1.get("role") != m2.get("role") or m1.get("content") != m2.get("content"):
                break
            common += 1
        self.assertEqual(common, len(head1) - 2,
                         "跨回合公共前缀应铺到第一轮历史末尾（只留尾部两条分叉）")

    def test_boundary_attribute_is_set_by_build_messages(self):
        a = self._agent(3)
        a._build_messages("hello")
        self.assertTrue(isinstance(getattr(a, "_head_fixed_end", None), int))
        self.assertGreaterEqual(a._head_fixed_end, 1)


if __name__ == "__main__":
    unittest.main()
