"""Agent 兜底：模型写了不在 self.tools 里的 action 时应当显式反馈。

之前 `if parsed["action"] and parsed["action"] in self.tools` 漏判
的情况让"未知 action + Final Answer"组合直接走到收尾 → 用户侧
完全不知道模型调了不存在的工具。现在：未知 action 立即给一条
Observation + 显式提示，连续 2 次同未知 action 触发强制收尾。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _ScriptedLLM:
    """只输出指定的 Action 行；不构造真实工具。"""
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self._idx = 0

    provider = "p"
    model = "m"

    def clone(self):
        return self

    def chat(self, messages, stream=True, **kw):
        text = self._outputs[min(self._idx, len(self._outputs) - 1)]
        self._idx += 1
        return iter([text]) if stream else text

    def count_tokens(self, text):
        return 1


def _act(name, arg=""):
    return f"Thought: call {name}\nAction: {name}\nAction Input: {arg}\n"


class _BadFinishLLM:
    """持续写未知 action 直到 forced_final。"""
    def __init__(self, n=4):
        self._n = n
        self._called = 0

    provider = "p"
    model = "m"

    def clone(self):
        return self

    def chat(self, messages, stream=True, **kw):
        self._called += 1
        if self._called <= self._n:
            text = _act("totally_unknown_tool", f"x{self._called}")
        else:
            text = "Final Answer: 已经没办法继续了。"
        return iter([text]) if stream else text

    def count_tokens(self, text):
        return 1


class AgentUnknownActionGuardTests(unittest.TestCase):
    def test_unknown_action_emits_observation_and_keeps_running(self):
        """模型写一个未知 action → 当回合 yield observation、要求模型重试。"""
        from agent import Agent

        llm = _ScriptedLLM([
            _act("totally_unknown_tool", "first"),
            "Final Answer: 改用别的方式回答。",
        ])
        a = Agent(llm=llm, tool_registry={}, tool_allowlist=None)
        events = list(a.run("q", stream=True, web_enabled=False))
        # 至少出现一条 observation（"未知工具"）
        obs_texts = [e.get("text", "") for e in events if e.get("type") == "observation"]
        self.assertTrue(any("未知工具" in t for t in obs_texts),
                        f"未出现「未知工具」observation：{obs_texts}")
        # action 也应标注"已拦截：未知工具"
        action_texts = [e.get("text", "") for e in events if e.get("type") == "action"]
        self.assertTrue(any("已拦截：未知工具" in t for t in action_texts),
                        f"action 事件未拦截：{action_texts}")
        # 最终以 Final Answer 收尾
        final = [e for e in events if e.get("type") == "final"]
        self.assertTrue(final, "应当有 final 事件")

    def test_unknown_action_repeated_triggers_forced_final(self):
        """连续 2 次同未知 action → 强制收尾（不再继续）。"""
        from agent import Agent

        # 模型依次输出：未知 action / 未知 action / 未知 action / 未知 action
        # 期望：第二次触发 forced_finals，第三次进入 5041 检查 → yield final → return，
        # 第四次 Action 不会被执行（卡片不会出现第 4 次的 action 事件）。
        llm = _BadFinishLLM(n=4)
        a = Agent(llm=llm, tool_registry={}, tool_allowlist=None)
        events = list(a.run("q", stream=True, web_enabled=False))
        action_texts = [e.get("text", "") for e in events if e.get("type") == "action"]
        # 应当有 1 条 action（第一次，未知），第二次就走 forced_final 路径（不再 yield action 事件）
        # 第 3 / 4 次更不应出现
        unknown_actions = [t for t in action_texts if "unknown_tool" in t and "未知" in t]
        # 第一次拦截 + 第二次触发 forced_final（仍 yield action 事件作为留痕），
        # 之后立即 yield final → return，第 3 / 4 次 action 不会再执行。
        self.assertLessEqual(len(unknown_actions), 2,
                              f"强制收尾后仍继续执行未知 action：{action_texts}")
        # 最后一次 action 应当带"强制收尾"标注
        self.assertTrue(any("强制收尾" in t for t in action_texts),
                        f"未标注强制收尾：{action_texts}")
        # final 应当出现
        final = [e for e in events if e.get("type") == "final"]
        self.assertTrue(final, f"未出现 final 事件：events={[e.get('type') for e in events]}")


if __name__ == "__main__":
    unittest.main()
