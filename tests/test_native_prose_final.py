# -*- coding: utf-8 -*-
"""原生 function-calling 下「有正文、无 tool_call」就是最终答案。

## 这次的实际损失

模型给出了**逐行核对全部正确**的答案：

    behaviors/enemy.gd:7   const CONTACT_DAMAGE: int = 10
    behaviors/player.gd:39  hp = maxi(0, hp - amount)

然后被 harness 判成「没有给出结论」，两轮后硬失败，用户只看到「请求失败」。
正确答案被扔掉了。

## 为什么这不是"模型不守规矩"

原生 function-calling 里，**API 已经通过 `tool_calls` 字段告诉了 harness 模型想不想要
工具**。模型选择写正文，那就是它的答案——这是结构化通道自带的确定性。

而 harness 还在用 `Final Answer:` 文本标记去判。那是 ReAct 文本协议的东西：
在文本协议里「一段没有标记的散文」确实无法区分是结论还是计划，因为模型只能写字符串。
把那个启发式套到结构化通道上，是范畴错误。

## 此前担心的"散文可能是计划"，那个歧义是自造的

上一轮我明确拒绝过"裸散文当答案"，理由是实测里那段散文是**计划**
（「需要用 read_file 读取原文核验…」）。那个判断在当时是对的，但要说清因果：

**那个歧义是提示词自相矛盾造成的**——提示要求写 `Final Answer:` 标记，
而传输层不允许模型"手写"任何东西，于是模型不知道散文代表什么。

提示词已按通道分流（`_prompt_for_channel`），两套契约不再并存，歧义随之消失。
所以现在放行是安全的，而不是把上一轮的谨慎丢掉。

## 边界（都必须是「不放行」）

- 本轮**有** tool_call -> 走工具派发，不当成答案（否则会吞掉工具结果）
- 输出被**截断**（finish_reason == "length"）-> 走续写，不能把半句当结论
- 正文为空 -> 走原有的空响应分支
- ReAct 文本协议路径**逐字不变**：那边的散文仍然必须带标记
"""

import json
import os
import unittest

import agent as agent_mod


class _ScriptedLLM:
    """按脚本返回原生 tool_calls 或纯文本。"""

    provider = "deepseek"   # 在 _NATIVE_CAPABLE 内 -> auto 走原生 FC
    model = "fake-native"

    def __init__(self, steps):
        self.steps = steps
        self.i = 0
        self.last_usage = {}
        self.last_tool_calls = []

    def chat(self, messages, stream=True, **kw):
        spec = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        self.last_usage = {"prompt_tokens": 5, "completion_tokens": 2}
        self.last_tool_calls = []
        content = ""
        if spec[0] == "tools":
            self.last_tool_calls = [
                {"id": "c%d" % i, "name": n, "arguments": json.dumps(a)}
                for i, (n, a) in enumerate(spec[1])
            ]
        else:
            content = spec[1]
        if stream:
            def g():
                for ch in content:
                    yield ch
            return g()
        return content

    def count_tokens(self, text):
        return max(1, len(text) // 3)

    def clone(self):
        return _ScriptedLLM(self.steps)


#: 实测里模型给出的那段正文（缩略，但保留结构与行号引用的形状）
REAL_ANSWER = (
    "扣血逻辑共三层：\n"
    "1. 伤害数值：behaviors/enemy.gd:7 const CONTACT_DAMAGE: int = 10\n"
    "2. 真正改血量：behaviors/player.gd:39 hp = maxi(0, hp - amount)\n"
    "一句话：扣多少看 enemy.gd:7 的 10，扣在哪看 player.gd:39。"
)


def _run(agent_mod_, steps, tool_mode="auto", question="扣血在哪算？"):
    agent = agent_mod_.Agent(llm=_ScriptedLLM(steps))
    agent.tool_mode = tool_mode
    events = []
    for ev in agent.run(question, stream=True):
        events.append(ev)
    return events


def _final_text(events):
    for ev in reversed(events):
        if ev.get("type") == "final":
            return ev.get("text") or "", ev.get("status")
    return None, None


class NativeProseIsTheAnswerTests(unittest.TestCase):
    """核心断言：原生通道下无调用的正文被当成最终答案交付。"""

    def test_prose_without_markers_is_delivered_as_the_answer(self):
        events = _run(agent_mod, [("text", REAL_ANSWER)])
        text, status = _final_text(events)
        self.assertIsNotNone(text, "原生通道下的纯正文没有被当成答案交付")
        self.assertNotEqual(status, "error",
                            "被判成失败——正确答案被扔掉了（实测就是这样）")
        self.assertIn("CONTACT_DAMAGE", text)
        self.assertIn("player.gd:39", text)

    def test_the_answer_is_delivered_verbatim(self):
        """不能被裁剪、加壳或改写——模型给的行号引用必须原样到达用户。"""
        events = _run(agent_mod, [("text", REAL_ANSWER)])
        text, _ = _final_text(events)
        self.assertIn(REAL_ANSWER, text,
                      "答案正文被改动了：行号引用必须原样送达，"
                      "被加工过的定位信息是不可核验的")

    def test_it_does_not_ask_for_a_marker_afterwards(self):
        """不该再进入「补 Final Answer 标记」的纠偏循环。"""
        events = _run(agent_mod, [("text", REAL_ANSWER)])
        nudges = [e.get("text") or "" for e in events
                  if e.get("type") in ("notice", "reflection")]
        joined = " ".join(nudges)
        self.assertNotIn("Final Answer", joined,
                         "还在要求补文本协议标记——原生通道里那个动作不存在")


class ToolCallsStillWinTests(unittest.TestCase):
    """有工具调用时，绝不能把正文当答案——否则会吞掉工具结果。"""

    def test_a_turn_with_tool_calls_is_not_treated_as_final(self):
        events = _run(agent_mod, [
            ("tools", [("list_dir", {"path": "."})]),
            ("text", REAL_ANSWER),
        ])
        tool_events = [e for e in events
                       if e.get("type") in ("action", "observation", "tool_call")]
        self.assertTrue(tool_events, "工具调用没有被执行——测试前提不成立")
        text, _ = _final_text(events)
        self.assertIn("CONTACT_DAMAGE", text,
                      "第二轮的正文才应是答案；若这里报缺失，说明工具轮被提前当成答案了")


class ReactPathIsUnchangedTests(unittest.TestCase):
    """ReAct 文本协议的行为必须**逐字不变**——那是另一批用户的既有路径。"""

    def test_prose_without_markers_is_still_a_stall_in_react_mode(self):
        events = _run(agent_mod, [("text", REAL_ANSWER)], tool_mode="react")
        text, status = _final_text(events)
        self.assertIsNotNone(text)
        self.assertEqual(status, "error",
                         "文本协议下无标记的散文仍应判为格式失守——"
                         "那里没有 tool_calls 字段提供确定性，散文确实可能是计划")

    def test_a_marked_answer_still_works_in_react_mode(self):
        events = _run(agent_mod, [("text", "Final Answer: " + REAL_ANSWER)],
                      tool_mode="react")
        text, status = _final_text(events)
        self.assertIsNotNone(text)
        self.assertNotEqual(status, "error")
        self.assertIn("CONTACT_DAMAGE", text)


class EmptyResponseStillHandledTests(unittest.TestCase):
    """空正文不能被当成答案——否则会把「上游返空」伪装成正常回答。"""

    def test_empty_content_does_not_become_an_answer(self):
        events = _run(agent_mod, [("text", "   ")])
        text, status = _final_text(events)
        self.assertIsNotNone(text)
        # 空响应走的是既有分支，status 不带 error 标记——它的文案里明确说了
        # 「没有返回可用正文」。所以判据是**文案**而不是 status 字段：
        # 一旦哪天有人把这段也标成 error，这条测试不该跟着失效。
        self.assertIn("没有返回可用正文", text,
                      "空白正文被当成了正常答案交付——那是把故障伪装成回答")
        self.assertNotIn("CONTACT_DAMAGE", text)


if __name__ == "__main__":
    unittest.main()