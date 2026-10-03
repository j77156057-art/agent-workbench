# -*- coding: utf-8 -*-
"""交通票价澄清**不得劫持**与出行无关的中文任务。

## 实测（`wf-2a548e060abf` 的 `design_retry2`，切到 deepseek-chat 后第一轮真跑）

那个子代理的结论是：

    交通票价需要先确认具体出发日期（YYYY-MM-DD）。请补充后我再查询；没有具体日期时不能判断当前车次、航班或价格。

而它的任务是「读取并分析 `res://behaviors/*.gd` 等文件并输出设计说明」。

**那句话是 harness 自己写的**（`agent._transport_request_clarification` 的
返回值逐字一致），模型只是把它当观察原样交了回来。trace 里表现为
**步骤数 0、一步工具没跑**——因为命中后 `yield final` 就 `return` 了。

## 触发它的是这一句

    「玩家血量从 100 降到 0 时触发死亡」
         ^^^^^^^        ^^^^^
         「从」          「降到」的「到」

原触发条件里有 `(?:从|由|出发).{0,16}(?:到|至|去|前往)`，本意是匹配出行问句
「从 A 到 B」。但中文里「从…到…」同样表示**任何区间变化**，而这个项目是中文
优先的。这句话就出自**上游子任务结论**里描述扣血机制的那段。

## 后果是最坏的一种

harness 把模型的工作**静默替换**成一句写死的客服话。反思只能看出
「结论与任务无关」，看不出是谁塞进去的——所以现场排查绕了很久。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402


class NonTravelTextIsNotHijackedTests(unittest.TestCase):
    """这些文本里出现「从…到…」，但**没有一样在问出行**。"""

    CASES = [
        ("区间变化（实测命中的那一句）",
         "玩家血量从 100 降到 0 时触发死亡；敌人进入侦测半径后水平距离单调减小。"),
        ("进度条",
         "血条从 0 到 100 平滑增长，缓动时长 0.2 秒。"),
        ("数组下标",
         "循环从 i=0 到 n-1 累加 scores。"),
        ("坐标移动",
         "玩家从 x=3 走到 x=8 触发过场动画。"),
        ("移动向量",
         "速度从 (0,0,0) 到 (0,0,-6) 线性插值。"),
        ("时间范围",
         "从 0:00 到 1:30 之间每 10 秒刷新一次 HUD。"),
    ]

    def test_none_of_them_trigger_the_travel_clarification(self):
        for label, text in self.CASES:
            got = agent._transport_request_clarification(text, [])
            self.assertEqual(got, "",
                             "「%s」被当成出行问句了，返回了：%r" % (label, got))

    def test_the_real_question_from_that_run_is_not_triggered(self):
        """整条真实 question（hint + 工具策略 + 子任务 + 上游结论）。"""
        question = (
            "你是【检索专员】：只负责查证，产出带 文件:行号 或来源 URL 的要点清单；"
            "不要改任何文件。\n工具策略：MCP=deny；允许工具=search_knowledge\n\n"
            "子任务：改用 search_code 按行范围分段读取，避免整文件截断。\n\n"
            "【上游子任务结论（供参考，勿重复劳动）】\n"
            "**循环：移动/跳跃 → 敌人侦测追击 → 贴身接触扣血 → 血量归零判负**\n"
            "血量从 100 降到 0 时触发死亡\n"
            "敌人从 x=1 走到 x=8 后停止")
        self.assertEqual(agent._transport_request_clarification(question, []), "",
                         "真实那条 question 又被劫持了")


class GenuineTravelStillClarifiesTests(unittest.TestCase):
    """收紧不能把功能关掉——真的在问出行时仍要澄清。"""

    CASES = [
        "北京到上海的机票多少钱",
        "帮我查一下高铁票",
        "明天从广州去深圳的车次还有吗",
        "查航班",
        "国庆想去三亚，行程怎么安排",
    ]

    def test_they_still_trigger(self):
        for text in self.CASES:
            got = agent._transport_request_clarification(text, [])
            self.assertTrue(got, "「%s」没有触发澄清——功能被收紧过头了" % text)

    def test_a_hotel_question_is_not_transport_and_must_not_trigger(self):
        """酒店预订**不该**走这条澄清。

        澄清文案写死的是「车次、航班或价格」——拿它去回答酒店预订是**换个领域
        的错答案**，比不澄清更坏。所以这里期望的是**不触发**，让模型自己去答。
        """
        got = agent._transport_request_clarification("剩余的酒店预订房间能退吗", [])
        self.assertEqual(got, "",
                         "酒店问题触发了交通澄清——回复里却写着「车次、航班」")

    def test_cost_question_after_a_travel_turn_still_triggers(self):
        """「从北京到上海多少钱」在本轮没出行词，但上文说过旅游——仍应澄清。"""
        history = [{"user": "国庆想去三亚旅游", "assistant": "好的"}]
        got = agent._transport_request_clarification("从北京到上海多少钱", history)
        self.assertTrue(got, "上文有出行词时不再澄清了——第二个触发条件被改坏了")

    def test_a_cost_question_without_any_travel_context_does_not(self):
        got = agent._transport_request_clarification("这个功能从 0 到 1 要多少钱",
                                                     [{"user": "你好",
                                                       "assistant": "在"}])
        self.assertEqual(got, "",
                         "没有任何出行上下文却澄清了——用户问的是开发成本")


class HarnessTextIsMarkedDegradedTests(unittest.TestCase):
    """harness 自己写的 final 必须标 `degraded`——否则它被当成模型的答案。"""

    def _finals(self, question, history=None):
        class _L:
            provider = "fake"
            model = "x"
            last_usage = {}
            last_tool_calls = []

            def clone(self):
                return self

            def chat(self, messages, stream=True, **kw):
                return iter([{"role": "assistant",
                              "content": "Action: read_file\nAction Input: {}"}])

            def count_tokens(self, text):
                return 1

        a = agent.Agent(llm=_L())
        if history is not None:
            a.history = list(history)
        return [ev for ev in a.run(question, stream=False)
                if ev.get("type") == "final"]

    def test_the_travel_clarification_is_degraded(self):
        """它不是模型的答案，是 harness 的纠偏文案。"""
        out = self._finals("北京到上海的机票多少钱")
        self.assertTrue(out, "这一轮应当以澄清收尾")
        self.assertTrue(out[0].get("clarification_required"))
        self.assertTrue(out[0].get("degraded"),
                        "没标 degraded——下游会把这句话当成模型的正式结论。"
                        "这正是「把没能答记成答了」")

    def test_the_ambiguous_write_clarification_is_degraded(self):
        out = self._finals("帮我改一下")
        self.assertTrue(out, "这一轮应当以澄清收尾")
        if out[0].get("clarification_required"):
            self.assertTrue(out[0].get("degraded"),
                            "「你要改哪个文件」是 harness 写的，不是模型的答案")

    def test_the_weather_clarification_is_degraded(self):
        out = self._finals("今天天气怎么样")
        self.assertTrue(out)
        if out[0].get("clarification_required"):
            self.assertTrue(out[0].get("degraded"),
                            "天气追问是 harness 写的，不是模型的答案")

    def test_run_child_marks_a_hijacked_conclusion_as_degraded(self):
        """端到端：被劫持的子代理，结论必须带降级标记。"""
        captured = []
        original = agent.Agent.run

        def fake(self, question, stream=False):
            for ev in original(self, question, stream=stream):
                captured.append(ev)
                yield ev

        agent.Agent.run = fake
        try:
            class _L:
                provider = "fake"
                model = "x"
                last_usage = {}
                last_tool_calls = []

                def clone(self):
                    return self

                def chat(self, messages, stream=True, **kw):
                    return iter([{"role": "assistant", "content": "x"}])

                def count_tokens(self, text):
                    return 1

            # 直接验子代理读 degraded 那一行：`_run_child` 把 final 的
            # `degraded` 透传成 `final_degraded`。
            out = agent.Agent(llm=_L())._run_child(
                "researcher", "北京到上海的机票多少钱", reflect=False)
        finally:
            agent.Agent.run = original

        # `reflect=False` 时 `status` 按设计恒为 ok（反思被关掉了），
        # 所以这里只能判 `degraded` —— 它才是「这句话不是模型的答案」这个信号。
        self.assertTrue(out.get("degraded"),
                        "子代理结论没标降级——下游会以为模型真的答了，"
                        "而这句话其实是 harness 的纠偏文案")
        self.assertIn("交通票价需要先确认", str(out.get("conclusion") or ""),
                      "这一轮应当以那句写死的澄清文案收尾")


if __name__ == "__main__":
    unittest.main()