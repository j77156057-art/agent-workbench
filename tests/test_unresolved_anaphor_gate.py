"""P0（BUG-11）回归锁：首问用悬空写指代、又没给具体路径时，必须**停下来问用户**，
而不是借跨会话记忆把"刚才的改动"脑补成某个文件、直接 dev_region_edit 落盘。

钻测实据（锚/A/E/G/J）：本会话没有任何前文时，模型拿"刚才/之前/这个改动"当已确立
事实，凭空锁定目标去写——翻车面被前几批收紧到唯一一条：**先行词只存在于跨会话记忆、
本会话内无从核对**。R/S/M/V/W 又证明：只要本会话内立过事实，模型就不会犯。所以这道门
只在 `history` 为空（首问）时触发，有前文时绝不打扰（与 R/S/M 的正向结论一致）。

修法落点（agent.py）：新增 `_unresolved_anaphor_clarification`，在 `Agent.run` 的
歧义写门之后加一条 terminal 早退，产出带**结构化 `ask` 选项**的 clarification final。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402
from agent import _unresolved_anaphor_clarification  # noqa: E402


class _NeverCalledFakeLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self):
        self.calls = 0
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}

    def chat(self, messages, stream=True, **kw):
        self.calls += 1
        script = "Final Answer: 不该被调用"
        if stream:
            def gen():
                for ch in script:
                    yield ch
            return gen()
        return script

    def count_tokens(self, text):
        return max(0, len(text) // 3)


class AnaphorGateDetectorTests(unittest.TestCase):
    def test_first_turn_dangling_write_asks(self):
        # 首问（history 空）：写动词 + 悬空回指 + 无具体路径 → 必须返回带选项的澄清。
        for probe in (
            "把之前那个改动改一下",
            "刚才那个文件帮我改一下",
            "把上次改的东西再修改修改",
        ):
            got = _unresolved_anaphor_clarification(probe, [])
            self.assertTrue(got, "%r 首问悬空写指代，应触发提问" % probe)
            self.assertTrue(got.get("ask", {}).get("options"),
                            "%r 的提问必须带可点选项（不能只甩一句话）" % probe)
            self.assertEqual(got["ask"]["kind"], "clarify")

    def test_history_present_does_not_bother(self):
        # 本会话已有前文：先行词多半可落地（R/S/M 证明会话内能核对时模型不犯），
        # 强行弹问反而是过度打扰。
        got = _unresolved_anaphor_clarification("把之前那个改动改一下",
                                                [{"user": "x", "assistant": "y"}])
        self.assertEqual(got, "", "有前文时不该触发悬空指代门")

    def test_concrete_path_is_not_anaphora(self):
        # 用户虽用了回指，但已给出确切路径/细节 → 目标可落地，不该多问。
        got = _unresolved_anaphor_clarification("把刚才说的 player.gd 改一下", [])
        self.assertEqual(got, "", "给了具体文件路径就不算悬空指代")

    def test_pure_read_question_not_gated(self):
        # 没有写动词：纯读问句绝不能被这道门拦下（那是"该答却问"的反向事故）。
        for probe in ("刚才那个函数是干嘛的", "之前那段逻辑怎么实现的？"):
            self.assertEqual(_unresolved_anaphor_clarification(probe, []), "",
                             "%r 无写意图，不该被拦" % probe)

    def test_capability_question_left_to_ambiguous_gate(self):
        # "能不能改一下这个文件" 归 _ambiguous_write_request 管，本门别抢活造成双弹框。
        self.assertEqual(
            _unresolved_anaphor_clarification("能不能改一下这个文件？", []), "",
            "能力问句由歧义写门处理，悬空指代门应让位")


class AnaphorGateEarlyReturnTests(unittest.TestCase):
    def _run(self, question):
        llm = _NeverCalledFakeLLM()
        agent = agent_mod.Agent(llm=llm)
        events = list(agent.run(question))
        return llm, events

    def test_dangling_anaphor_never_reaches_model(self):
        llm, events = self._run("把之前那个改动改一下")
        self.assertEqual(llm.calls, 0,
                         "BUG-11 复发：首问悬空写指代仍去调了模型（应零调用早退提问）")
        finals = [e for e in events if e.get("type") == "final"]
        self.assertTrue(finals, "提问也必须产出一条 final，界面才不会空转")
        self.assertTrue(finals[0].get("clarification_required"))
        ask = finals[0].get("ask")
        self.assertTrue(ask and ask.get("options"),
                        "final 应携带结构化 ask 选项，供前端渲染可点提问框")

    def test_empty_session_history_so_gate_fires(self):
        # 守门前提：全新会话 self.history 为空，门才可能触发（否则上面用例形同虚设）。
        agent = agent_mod.Agent(llm=_NeverCalledFakeLLM())
        self.assertFalse(getattr(agent, "history", []),
                         "新 Agent 的 history 必须为空，悬空指代门才有意义")


if __name__ == "__main__":
    unittest.main()
