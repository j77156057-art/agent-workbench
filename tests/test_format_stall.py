"""format_stall 早退：模型连续不写标记时，第 2 轮就停并明说，不再转到 nudges 上限。

这个测试要防的回归，按重要性排：

1. **不能误判合规形状** —— `Thought:/Action:/Action Input:` 与真的 `Final Answer:`
   都是**合规**的。我第一版实验脚本只认 `Final Answer:`，把前者判成「不合规」，
   差点照着错数据去改解析器。`ParseShapeTests` 把这条钉死。
2. **不能无限 nudge** —— 用户实测看到同一条提示重复 8 遍。裸正文必须早退。
3. **不能静默** —— 早退时要给可读的原因与下一步。
4. **不能误伤** —— 模型一旦给出合规形状，就绝不该走这条早退。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402
from agent import _MAX_FORMAT_STALLS, parse_response  # noqa: E402


class ParseShapeTests(unittest.TestCase):
    """先把「合规」的判据钉清楚。"""

    def test_action_shape_is_compliant(self):
        parsed = parse_response("Thought: x\nAction: search_code\nAction Input: damage")
        self.assertEqual(parsed["action"], "search_code")
        self.assertFalse(parsed["final"])

    def test_final_shape_is_compliant(self):
        parsed = parse_response("Final Answer: 玩家扣血在 TakeDamage。")
        self.assertIn("玩家扣血", parsed["final"] or "")
        self.assertIsNone(parsed["action"])

    def test_bare_prose_has_neither(self):
        parsed = parse_response("玩家扣血在 TakeDamage 里算的。")
        self.assertIsNone(parsed["action"])
        self.assertFalse(parsed["final"])


class _FakeLLM:
    """按脚本回放模型输出的假 LLM（沿用 tests/test_agent_trace.py 的形状）。"""

    provider = "fake"
    model = "fake-1"

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.i = 0
        self.last_usage = {"prompt_tokens": 7, "completion_tokens": 3}

    def chat(self, messages, stream=True, **kw):
        script = self.scripts[min(self.i, len(self.scripts) - 1)]
        self.i += 1
        if stream:
            def gen():
                for ch in script:
                    yield ch
            return gen()
        return script

    def count_tokens(self, text):
        return max(0, len(text) // 3)


BARE = "玩家扣血在 TakeDamage 里算的。"
COMPLIANT_ACTION = "Thought: 找一下\nAction: list_dir\nAction Input: "
COMPLIANT_FINAL = "Final Answer: 玩家扣血在 TakeDamage 里算的。"


class FormatStallTests(unittest.TestCase):

    def _run(self, scripts, question="玩家受伤扣多少血在哪算的？"):
        llm = _FakeLLM(scripts)
        agent = agent_mod.Agent(llm=llm)
        notices, finals = [], []
        for event in agent.run(question):
            if event.get("type") == "notice":
                notices.append(event.get("text") or "")
            elif event.get("type") == "final":
                finals.append(event.get("text") or "")
        return {"llm_calls": llm.i, "notices": notices, "finals": finals}

    def test_the_budget_is_two(self):
        """两次就够：足以区分「偶发没写」与「稳定不守格式」，再多是消耗用户耐心。"""
        self.assertEqual(_MAX_FORMAT_STALLS, 2)

    def test_bare_prose_does_not_loop_forever(self):
        """核心回归：裸正文曾经会转满 nudges（用户实测看到 8 遍同一条提示）。"""
        result = self._run([BARE] * 20)
        self.assertLessEqual(len(result["notices"]), 2,
                             "裸正文不该反复 nudge，实际 %d 次" % len(result["notices"]))
        self.assertLessEqual(result["llm_calls"], 4,
                             "裸正文的模型调用次数应被限制，实际 %d" % result["llm_calls"])

    def test_it_stops_with_a_readable_reason(self):
        """早退必须说清「为什么停」和「下一步怎么办」，不能只抛半句正文。"""
        result = self._run([BARE] * 20)
        self.assertTrue(result["finals"], "早退时必须给出 final")
        text = result["finals"][-1]
        self.assertIn("格式", text)
        self.assertIn("Action", text)
        self.assertIn("Final Answer", text)
        self.assertNotIn("----------", text, "不应把内部提示原样回显给用户")

    def test_a_compliant_action_is_never_stalled(self):
        """模型给出合规 Action 时绝不能走早退——它要去调工具，不是没答完。"""
        result = self._run([COMPLIANT_ACTION, "Observation: x\n\nFinal Answer: 好了。"])
        self.assertEqual(result["notices"], [], "合规 Action 不该触发任何 nudge")

    def test_a_compliant_final_is_never_stalled(self):
        result = self._run([COMPLIANT_FINAL])
        self.assertEqual(result["notices"], [])
        self.assertTrue(result["finals"])
        self.assertNotIn("格式", result["finals"][-1])

    def test_recovering_after_one_bare_reply_still_works(self):
        """裸一次、然后照做，应该正常结束——早退不能误伤「改一次就好」的情况。

        断言的是**结果**而不是标记：`parse_response` 会把 `Final Answer:` 前缀
        剥掉，所以最终正文里没有这几个字（实测输出只有答案本身）。
        """
        result = self._run([BARE, COMPLIANT_FINAL])
        self.assertTrue(result["finals"], "第二次合规就该正常收尾")
        self.assertIn("TakeDamage", result["finals"][-1])
        self.assertNotIn("格式", result["finals"][-1],
                         "不该误判成格式失守而早退")
        self.assertEqual(len(result["notices"]), 1, "只应被 nudge 一次")


class NudgeContentTests(unittest.TestCase):
    """nudge 的内容：必须带上模型自己的输出，否则它无从判断缺哪一段。"""

    def test_the_nudge_echoes_the_previous_output(self):
        """实测依据：原来只说「你没给 Final Answer」，模型并不知道自己说了什么。"""
        import inspect
        source = inspect.getsource(agent_mod)
        self.assertIn("你上一轮的**实际输出**", source)
        self.assertIn("前面**补一行 `Final Answer: `", source)

    def test_it_does_not_tell_the_model_to_rewrite_from_scratch(self):
        """让它重写会导致内容漂移；正确做法是把已有正文挪到标记后面。"""
        import inspect
        source = inspect.getsource(agent_mod)
        self.assertIn("不要重写内容", source)


class PromptContractTests(unittest.TestCase):
    """契约必须在提示词的**首尾**，不能只埋在三万字工具说明的中间。"""

    def setUp(self):
        self.prompt = agent_mod._SYSTEM_PROMPT_FULL

    def test_contract_is_at_the_head(self):
        self.assertTrue(self.prompt.lstrip().startswith("【输出格式契约"),
                        "契约必须在系统提示词最前面")

    def test_contract_is_restated_at_the_tail(self):
        self.assertIn("【再强调一次 · 输出格式】", self.prompt[-1600:],
                      "长提示词模型遵循首尾优于中段，末尾必须重申")

    def test_contract_names_both_shapes(self):
        head = self.prompt[:1200]
        self.assertIn("Action:", head)
        self.assertIn("Final Answer:", head)


if __name__ == "__main__":
    unittest.main()
