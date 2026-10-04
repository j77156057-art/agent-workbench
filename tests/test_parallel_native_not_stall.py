# -*- coding: utf-8 -*-
"""并行 tool_call 那一轮**不得**被判成格式失守。

## 实测事故（探针落盘，`.tmp/stall_probe/stall-075620-01.json`）

`wf-a79129b8e691` 的 `recover_project_config` 子代理：

    finish_reason='tool_calls'   ← 模型用协议允许的方式表达了动作
    tool_calls_seen=2
    acc_chars=91
    acc='I need to read the actual game source files to understand the gameplay…'
    turn_outcome='format_stall' ← 误判

模型**完全正常**地发出了两个 tool call。但 `len(pairs) > 1` 那一支只设
`_pending_batch`，**不设 `native_override`、不往 `acc` 里拼 `Action:`**，
而批次要到**循环顶**才执行——可代码在到达循环顶之前就先 `parse_response(acc)`。
于是在一句散文上解析出「无 action、无 final」，直接判 stall。

## 后果不是「多纠偏一次」

`_MAX_FORMAT_STALLS` 耗尽后 `return`，**这一轮的工具调用一次都没执行**——
模型想做的事被 harness 判成「它什么都没说」。而纠偏文案还在告诉它
「请直接调用工具」，于是下一轮它说同样的话、同样进不了批次。

**那是一条结构上不可能成功的路**，和「任务要写文件却派给只读角色」同一族。

## 修的是判据的口径

判据从「`acc` 里**有没有** `Action:` 字样」改成「模型**有没有**按协议允许的
方式表达动作」。后者才是意图——`Action:` 字样只是**文本协议的形状**，
原生通道下它本来就不该出现，拿它的有无去判模型的守规与否是问错了问题。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402


#: 只给三个真工具。`Agent.__init__` 的参数是 `tool_registry` 而不是 `tools`
#: （后者是 `__init__` 之后才有的属性），传错会 TypeError——写测试时踩过一次。
_REGISTRY = {name: agent.TOOLS[name] for name in ("read_file", "grep", "list_dir")}


class _FakeLLM:
    """模拟实测那一轮：2 个 tool_call + 一句 91 字符散文。"""

    def __init__(self):
        self.provider = "deepseek"
        self.model = "deepseek-chat"
        self.capability = {"native_tools": True, "thinking": "none"}
        self.last_tool_calls = [
            {"id": "c1", "name": "read_file", "arguments": '{"path":"project.godot"}'},
            {"id": "c2", "name": "read_file", "arguments": '{"path":"regions.json"}'},
        ]
        self.last_usage = {}
        self.last_finish_reason = "tool_calls"
        self.last_stream_ref = None
        self.last_raw_head = ""
        self.output_budget = 4096

    def count_tokens(self, _text):
        return len(str(_text or "")) // 3

    def chat(self, **_kwargs):
        return ("I need to read the actual game source files to understand "
                "the gameplay, scenes, and inputs.")


class ParallelNativeCallsAreNotAFormatStallTests(unittest.TestCase):
    def _agent(self, tool_mode="auto"):
        return agent.Agent(llm=_FakeLLM(), session_id="t", tool_mode=tool_mode,
                           tool_registry=_REGISTRY)

    def test_it_does_not_emit_the_format_stall_diagnosis(self):
        """这一轮不该产出「模型连续 N 轮没有给出可判定的动作」。"""
        events = []
        for item in self._agent()._run("读 project.godot 与 regions.json"):
            events.append(item)
        texts = " ".join(str(item.get("text") or "") for item in events)
        self.assertNotIn("没有给出可判定的动作", texts,
                         "并行 tool_call 被判成格式失守了——那些工具调用一次都没执行")

    def test_the_tools_actually_get_executed(self):
        """根因修在「判据」，但真正要保住的是**工具被执行**这件事。"""
        events = list(self._agent()._run("读 project.godot 与 regions.json"))
        actions = [str(item.get("text") or "") for item in events
                   if item.get("type") == "action"]
        self.assertTrue(any("read_file" in a for a in actions),
                        "两个 read_file 都没执行：%r" % (actions,))

    def test_no_format_stall_outcome_is_recorded(self):
        for item in self._agent()._run("读 project.godot 与 regions.json"):
            self.assertNotEqual(item.get("text", "").split("\n")[0],
                                "模型连续 2 轮没有给出可判定的动作或结论，"
                                "本轮已停止重试。")

    def test_a_single_native_call_still_parses_as_an_action(self):
        """单调用那条路本来就走 `native_override`，必须保持原样。

        回归保护：把两支合并成同一个判据时，很容易顺手改坏单调用。
        """
        llm = _FakeLLM()
        llm.last_tool_calls = [llm.last_tool_calls[0]]
        a = agent.Agent(llm=llm, session_id="t",
                        tool_registry={n: agent.TOOLS[n]
                                       for n in ("read_file", "list_dir")})
        actions = [str(item.get("text") or "") for item in a._run("读 project.godot")
                   if item.get("type") == "action"]
        self.assertTrue(any("read_file" in x for x in actions), actions)


class BareProseIsStillAFinalNotAStallTests(unittest.TestCase):
    """**放宽的口径要说清楚**：原生通道下的无工具散文**本来就该是最终答案**。

    我最初把这条写成「裸散文仍必须判失守」——**那是我把口径搞反了**，测试第一跑
    就红。`agent.py` 的 `native_prose_final` 已明确放行这条路，理由写在注释里：
    API 通过 `tool_calls` 字段告诉了 harness 模型想不想要工具，模型选择写正文，
    那就是它的答案。再拿 `Final Answer:` 字样去判，是把**文本协议的启发式套到
    结构化通道上**——一个范畴错误。

    同一个理由也解释了本次根因：`acc` 里**有没有** `Action:` 字样，
    本身就只是文本协议的形状，原生通道下它永远不该出现。
    **判据问的是「模型有没有表达动作」，不是「字样在不在」。**

    所以这条守住反面：原生通道下没有「把散文判成失守」这条分支——
    真 bug 是 `_pending_batch` 未执行就去解析 acc。文本协议那侧另测。
    """

    class _ProseLLM(_FakeLLM):
        def __init__(self):
            super().__init__()
            self.last_tool_calls = []
            self.last_finish_reason = "stop"

    def test_native_prose_is_not_mislabelled_as_a_format_stall(self):
        a = agent.Agent(llm=self._ProseLLM(), session_id="t", tool_registry=_REGISTRY)
        texts = " ".join(str(item.get("text") or "") for item
                         in a._run("读 project.godot"))
        self.assertNotIn("没有给出可判定的动作", texts,
                         "原生通道下无工具的散文被误判成格式失守——"
                         "那是 `native_prose_final` 已明确放行的形状")

    def test_the_prose_reaches_the_user_verbatim(self):
        a = agent.Agent(llm=self._ProseLLM(), session_id="t", tool_registry=_REGISTRY)
        texts = " ".join(str(item.get("text") or "") for item
                         in a._run("读 project.godot"))
        self.assertIn("I need to read", texts,
                      "模型的真实输出没到用户面前——被换成别的东西了")

    def test_the_text_protocol_still_requires_the_marker(self):
        """ReAct 文本协议下，散文**不能**直接当成答案——那是该协议的形状。"""
        a = agent.Agent(llm=self._ProseLLM(), session_id="t", tool_registry=_REGISTRY,
                        tool_mode="react")
        events = list(a._run("读 project.godot"))
        finals = [str(ev.get("text") or "") for ev in events
                  if ev.get("type") == "final"]
        self.assertTrue(finals, "文本协议下应该有 final 事件")
        # 文本协议确实判它失守——这与原生通道相反，正说明分流是按通道走的。
        self.assertIn("没有给出可判定的动作", finals[-1])
        # 但失守**不等于**把散文当成答案：诊断文案必须标明那只是模型输出、未核验。
        self.assertIn("不要当作结论", finals[-1])


class TheCriterionIsAboutIntentNotSpellingTests(unittest.TestCase):
    """判据的依据是「模型有没有表达动作」，不是「acc 里有没有 Action: 字样」。"""

    def test_the_stall_branch_excludes_native_calls(self):
        import inspect
        src = inspect.getsource(agent.Agent._run)
        self.assertIn("not native_calls_this_turn", src,
                      "格式失守判定没有排除「本轮已发出原生 tool_call」——"
                      "那正是实测误判的那一行")

    def test_the_flag_is_set_before_the_criterion(self):
        import inspect
        src = inspect.getsource(agent.Agent._run)
        flag_at = src.find("native_calls_this_turn = True")
        use_at = src.find("not native_calls_this_turn")
        self.assertNotEqual(flag_at, -1, "标记从未被置真")
        self.assertNotEqual(use_at, -1)
        self.assertLess(flag_at, use_at,
                        "标记在使用点之后才置真——判据读到的永远是 False")

    def test_the_flag_resets_every_turn(self):
        """标记必须**每轮**归零，否则某一轮发过调用就永远免检。"""
        import inspect
        src = inspect.getsource(agent.Agent._run)
        decl_at = src.find("native_calls_this_turn = False")
        self.assertNotEqual(decl_at, -1,
                            "标记没有每轮重置——第一轮发过调用之后，"
                            "后面所有轮次都会跳过格式失守判定")


if __name__ == "__main__":
    unittest.main()