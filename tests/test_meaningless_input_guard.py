"""BUG-8 回归锁：无意义输入不得跑满整条 ReAct 链硬生成自信答案。

实测（钻测轮次6）：只发一个全角问号"？"，trace 却记 outcome=completed、
final_chars=1491、跑了完整一轮（llm_calls=2）——白烧 token，且对无意图输入
给长篇"看起来很有道理"的回答是幻觉温床。

修法：入口在调任何工具/provider 之前先挡纯符号/空输入，回一句澄清走
clarification_required 早退。判据保守：含任一字母/数字/CJK 汉字即放行。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402
from agent import _meaningless_input_clarification  # noqa: E402


class _CountingFakeLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self, scripts=("Final Answer: 不该被调用",)):
        self.scripts = list(scripts)
        self.i = 0
        self.calls = 0
        self.last_usage = {"prompt_tokens": 5, "completion_tokens": 3}

    def chat(self, messages, stream=True, **kw):
        self.calls += 1
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


class MeaninglessDetectorTests(unittest.TestCase):
    def test_pure_symbol_blocked(self):
        for probe in ("？", "?", "。。。", "……", "!!!", "()[]{}", "  ", ""):
            self.assertTrue(_meaningless_input_clarification(probe),
                            "%r 是纯符号/空，应被挡下" % probe)

    def test_real_questions_pass(self):
        for probe in ("玩家受伤扣多少血", "hp", "0.8秒", "这行代码干嘛的", "好", "x"):
            self.assertEqual(_meaningless_input_clarification(probe), "",
                             "%r 含实质字符，不该误伤" % probe)


class MeaninglessEarlyReturnTests(unittest.TestCase):
    """「留地板、拆其余」重构后：无意义输入顶回门已拆，纯符号/空输入直达模型，
    不再在模型之前弹 clarification_required + degraded 早退。真没内容时由模型自己
    自然追问。判据函数 _meaningless_input_clarification 仍在（上面的
    MeaninglessDetectorTests 直接钉它），只是不再劫持聊天主路径。"""

    def _run(self, question):
        llm = _CountingFakeLLM()
        agent = agent_mod.Agent(llm=llm)
        events = list(agent.run(question))
        return llm, events

    def test_question_mark_now_reaches_model(self):
        llm, events = self._run("？")
        self.assertGreaterEqual(llm.calls, 1,
                                "顶回门已拆：纯符号输入应交给模型，而非零调用早退")
        finals = [e for e in events if e.get("type") == "final"]
        self.assertTrue(finals, "模型应答产出一条 final")
        self.assertFalse(finals[0].get("clarification_required"),
                         "不应再被顶回门标记为 clarification_required")
        self.assertFalse(finals[0].get("degraded"),
                         "不应再被顶回门打上 degraded 降级章")

    def test_empty_input_now_reaches_model(self):
        llm, _ = self._run("   ")
        self.assertGreaterEqual(llm.calls, 1,
                                "顶回门已拆：空白输入也应交给模型处理")

    def test_normal_question_is_not_blocked(self):
        llm, _ = self._run("behaviors/player.gd 里玩家移动怎么实现的？")
        self.assertGreaterEqual(llm.calls, 1,
                                "正常问题绝不该被误拦")


if __name__ == "__main__":
    unittest.main()
