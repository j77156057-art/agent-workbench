# -*- coding: utf-8 -*-
"""LONGCTX-LLMSlice-Leak 锁定测试：主压缩路径 _llm_summary 的输入切片
必须走 _clip_with_mark（超长显式带"…（截断）"），不得用裸 [:400] 静默腰斩精确标识符。
全部离线，不触网：用一个捕获型假 LLM 把 chat 实收的 prompt 存下来直读。
"""
import unittest

import sessions


KEY = "SECRET-KEY-OMEGA-9527"


class _CapturingLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self, reply="（假摘要）已压缩。"):
        self.reply = reply
        self.seen = ""

    def chat(self, messages, stream=False, **kw):
        self.seen = messages[0]["content"]
        return self.reply

    def count_tokens(self, text):
        return max(0, len(text) // 3)


def _turn_key_beyond_400():
    """KEY 只放在 user 文本第 400 字之后；assistant 不含 KEY，
    于是任何 KEY 出现都只可能来自 user 侧切片——可判 user 侧被切后是否留痕。"""
    pad = "配置说明如下请注意查收。" * 40  # 每段 12 字 → 480 字，KEY 稳落 400 之后
    user = pad + " 部署密钥是 " + KEY + "，请以它为准。"
    assistant = "好的，已记录你给的部署密钥，稍后按它配置。"
    assert user.index(KEY) > 400
    return {"user": user, "assistant": assistant}


class LlmSummarySlice(unittest.TestCase):
    def test_truncated_input_carries_explicit_mark(self):
        """核心锁点：超长 user 被切时，喂给摘要器的文本必须带"…（截断）"标记，
        而非裸 [:400] 静默腰斩（修复前该标记缺失）。"""
        fake = _CapturingLLM()
        sessions._llm_summary([_turn_key_beyond_400()], fake)
        self.assertIn("截断", fake.seen,
                      "主路径切片必须显式标截断，不许静默把精确值切没了还不留痕")

    def test_beyond_window_key_not_silently_present_as_complete(self):
        """落在 400 字之后的 KEY 不该原样出现在输入里（切片所致），
        但缺席必须伴随截断标记——二者要么都在、要么都别谎报完整。"""
        fake = _CapturingLLM()
        sessions._llm_summary([_turn_key_beyond_400()], fake)
        if KEY not in fake.seen:
            self.assertIn("截断", fake.seen,
                          "KEY 被切却没截断标记 = 静默腰斩，正是要禁的 bug")

    def test_short_turns_pass_through_unchanged(self):
        """未超长的轮次不得被 _clip_with_mark 乱加标记（保既有短轮契约、防反向误伤）。"""
        turns = [{"user": "问" + "甲" * 20, "assistant": "答" + "乙" * 10}]
        fake = _CapturingLLM()
        sessions._llm_summary(turns, fake)
        self.assertNotIn("截断", fake.seen)
        self.assertIn(turns[0]["user"], fake.seen)

    def test_matches_naive_marking_convention(self):
        """主路径与兜底路径对"截断"的标示用词一致（都出"…（截断）"），
        坐实这是把同一把安全刀装到正路，非各造各的。"""
        turn = _turn_key_beyond_400()
        fake = _CapturingLLM()
        sessions._llm_summary([turn], fake)
        naive = sessions._naive_summary([turn])
        self.assertIn("截断", fake.seen)
        self.assertIn("截断", naive)

    def test_none_llm_still_falls_to_naive(self):
        """llm=None 的既有短路不动（仍走 _naive_summary），本次改动不碰这条分支。"""
        out = sessions._llm_summary([_turn_key_beyond_400()], None)
        self.assertIn("本地兜底", out)

    def test_empty_reply_falls_back_to_marked_naive(self):
        """LLM 返回空 → 落 _naive_summary（既有行为），产物须仍带截断标记、非静默。"""
        out = sessions._llm_summary([_turn_key_beyond_400()], _CapturingLLM(reply=""))
        self.assertIn("截断", out)
        self.assertIn("本地兜底", out)


if __name__ == "__main__":
    unittest.main()
