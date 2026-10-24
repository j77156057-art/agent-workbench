# -*- coding: utf-8 -*-
"""长任务里「（并行调用）…」内部标记被模型复读成'最终回答'的回归。

## 为什么存在这个文件

**旧版本**的原生 function-calling 并行批次会往 assistant 历史里写一条**内部串**
`（并行调用）read_file、read_file`。模型偶尔会把自己这条历史**原样复读**一遍当回答。
而原生通道把「有正文、无 tool_call」直接当作最终答案（`native_prose_final`），
于是这行内部标记就被当成真结论下发给用户，再被核验环节追加 `（核验：FAIL）`——
用户拿到的根本不是一份综合结论，而是 harness 自己写的一行噪声。

实测（longrun-a1 两次长跑）：final 事件正文 == `（并行调用）read_file、read_file`。

## 三层修法与不变量

1. **不再写**：批次回填历史时删掉那条 assistant 调度摘要，工具名并进每条
   Observation 的前缀（`Observation: [read_file] …`），从源头消灭可被复读的孤立发言。
2. **先纠偏**：闸门命中时不再直接掉进 `format_stall`（旧实现两轮就判死，前端 18 秒
   只显示「执行失败」）。用独立的 `marker_echoes` / `_MAX_MARKER_ECHOES` 额度，
   先明确告诉模型「那行不是回答」，且提示**不回显**标记本身；仍复读才降级收尾，
   收尾文案也不含那行标记。闸门绝不能只是清空 `final_body` 后往下走——末尾的
   `_clean_fallback_answer` 会把标记当"残句"清洗后下发。
3. **清洗存量**：`_fit_budget` 在发送前删掉整条就是旧标记的 assistant 消息；
   只删纯标记，带正文的正常发言不动。

判据必须**保守**：只有整段是「（并行调用）+ 纯 ASCII 工具名（可含 、 空格 . -）」才算；
任何以该标记开头、后面接了自然语言正文的正常回答都**不能**被清空（绝不误伤好答案）。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent import (Agent, TOOLS, _find_prunable_trail,  # noqa: E402
                   _is_internal_marker_only)


class DetectorTests(unittest.TestCase):
    """纯函数判据：命中内部标记，但绝不命中真回答。"""

    def test_pure_marker_is_detected(self):
        for t in (
            "（并行调用）read_file、read_file",
            "（并行调用）read_file、grep",
            "（并行调用） dev_glob 、 search_code ",
            "（并行调用）dev_glob",
            "（并行调用）",
        ):
            self.assertTrue(_is_internal_marker_only(t), "应判为内部标记：%r" % t)

    def test_marker_prefixed_real_answer_is_NOT_detected(self):
        # 关键：以内部标记开头、但后面接了真实句子内容的正常回答不能被误伤。
        for t in (
            "（并行调用）read_file，我已确认 sessions.py 共 825 行",
            "（并行调用）read_file\n\n综合结论：sessions.py 负责会话持久化与压缩。",
            "（并行调用）run_command(wc -l) 的结果显示 api.py 有 6774 行",
        ):
            self.assertFalse(_is_internal_marker_only(t), "不应误伤真回答：%r" % t)

    def test_plain_answer_is_not_detected(self):
        self.assertFalse(_is_internal_marker_only("正常最终结论：sessions.py 共 825 行"))
        self.assertFalse(_is_internal_marker_only(""))


class _ScriptedNativeLLM:
    """原生通道下按脚本逐轮吐正文；不声明 tool_calls（last_tool_calls 恒空）。"""

    provider = "p"
    model = "m"

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self._idx = 0
        self.last_tool_calls = []
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}

    def clone(self):
        return self

    def count_tokens(self, text):
        return 1

    def chat(self, messages, stream=True, **kw):
        text = self._outputs[min(self._idx, len(self._outputs) - 1)]
        self._idx += 1
        return iter([text]) if stream else text


class _RecordingNativeLLM(_ScriptedNativeLLM):
    """在脚本 LLM 基础上记录每轮 messages，并可按轮返回原生 tool_calls。"""

    def __init__(self, outputs, tool_turns=None):
        super().__init__(outputs)
        self.messages_seen = []
        self._tool_turns = list(tool_turns or [])

    def chat(self, messages=None, stream=True, **kw):
        self.messages_seen.append(
            [(m.get("role"), m.get("content")) for m in (messages or [])])
        self.last_tool_calls = self._tool_turns.pop(0) if self._tool_turns else []
        return super().chat(messages, stream=stream, **kw)


class EndToEndGuardTests(unittest.TestCase):
    def test_bare_marker_echo_never_becomes_final(self):
        """第一轮复读内部标记 → 绝不下发成 final；纠偏后第二轮的真结论才交付。"""
        llm = _ScriptedNativeLLM([
            "（并行调用）read_file、read_file",           # 复读内部标记（触发闸门）
            "Final Answer: 这是真正的综合结论：sessions.py 负责会话持久化。",
        ])
        a = Agent(llm=llm, tool_registry={}, tool_allowlist=None, tool_mode="native")
        events = list(a.run("帮我把体检结论汇总一下", stream=True, web_enabled=False))

        finals = [e for e in events if e.get("type") == "final"]
        self.assertTrue(finals, "应当有 final 事件")
        last_final = finals[-1].get("text", "") or ""
        # 内部标记绝不能原样充当答案（哪怕带核验后缀也不行）
        self.assertNotEqual(
            last_final.strip(), "（并行调用）read_file、read_file",
            "纯内部标记被当成了最终答案：%r" % last_final)
        self.assertFalse(
            last_final.strip().startswith("（并行调用）") and "综合结论" not in last_final,
            "最终答案仍是内部标记：%r" % last_final)

    def test_real_prose_answer_is_still_delivered_untouched(self):
        """回归：以（并行调用）开头但接了正文的正常回答，必须一字不改地交付。"""
        real = "（并行调用）read_file、read_file 之后我确认：sessions.py 共 825 行，负责会话持久化。"
        llm = _ScriptedNativeLLM([real])
        a = Agent(llm=llm, tool_registry={}, tool_allowlist=None, tool_mode="native")
        events = list(a.run("汇总结论", stream=True, web_enabled=False))
        finals = [e for e in events if e.get("type") == "final"]
        self.assertTrue(finals)
        self.assertIn("sessions.py 共 825 行", finals[-1].get("text", ""),
                      "带正文的正常回答被误伤了")


class MarkerEchoCorrectionTests(unittest.TestCase):
    """闸门命中后的处置：先纠偏、额度耗尽才降级，且标记绝不回灌历史/答案。"""

    def test_first_echo_gets_a_correction_not_an_immediate_stall(self):
        llm = _RecordingNativeLLM([
            "（并行调用）read_file、read_file",   # 复读内部标记（触发闸门）
            "sessions.py 负责会话持久化与压缩。",  # 纠偏后的真结论
        ])
        a = Agent(llm=llm, tool_registry={}, tool_allowlist=None, tool_mode="native")
        events = list(a.run("汇总一下会话模块", stream=True, web_enabled=False))

        finals = [e for e in events if e.get("type") == "final"]
        self.assertTrue(finals, "应当有 final 事件")
        self.assertFalse(finals[-1].get("degraded"),
                         "只复读了一轮就被判 degraded：%r" % (finals[-1],))
        self.assertIn("会话持久化", finals[-1].get("text", ""))

        # 第二轮的 messages 里：有纠偏 Nudge，但**没有**被复读的那行标记——
        # 把模型自己吐出来的标记再写回 assistant 历史，就是下一轮复读的燃料。
        self.assertGreaterEqual(len(llm.messages_seen), 2)
        second = llm.messages_seen[1]
        self.assertTrue(
            any((c or "").startswith("Nudge:") and "内部调度" in (c or "")
                for _r, c in second),
            "第一次复读后没有给出明确纠偏：%r" % (second,))
        self.assertFalse(
            any((c or "").strip() == "（并行调用）read_file、read_file"
                for _r, c in second),
            "被复读的标记被写回了历史，下一轮还会复读：%r" % (second,))

    def test_repeated_echo_terminates_with_a_marker_free_diagnosis(self):
        llm = _RecordingNativeLLM(["（并行调用）read_file、read_file"] * 5)
        a = Agent(llm=llm, tool_registry={}, tool_allowlist=None, tool_mode="native")
        events = list(a.run("汇总一下会话模块", stream=True, web_enabled=False))

        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1, "应当只收尾一次：%r" % (finals,))
        self.assertTrue(finals[0].get("degraded"),
                        "复读到额度耗尽仍未标 degraded：%r" % (finals[0],))
        self.assertNotIn("（并行调用）read_file", finals[0].get("text", ""),
                         "降级诊断又把内部标记原样放出来了")
        # 第一次纠偏 + 第二次额度耗尽即停，不该继续空转
        self.assertEqual(len(llm.messages_seen), 2,
                         "复读额度没有生效，模型被允许空转 %d 轮" % len(llm.messages_seen))


class BatchTrailHasNoReplayableMarkerTests(unittest.TestCase):
    """批次回填：不再有「（并行调用）…」assistant 摘要，工具名进 Observation 前缀。"""

    def test_batch_observations_carry_the_tool_name_instead(self):
        calls = [
            {"id": "c1", "name": "list_dir", "arguments": '{"path":"."}'},
            {"id": "c2", "name": "list_dir", "arguments": '{"path":"tests"}'},
        ]
        llm = _RecordingNativeLLM(
            ["我先看看目录结构。", "目录里包含项目源码与 tests 子目录。"],
            tool_turns=[calls])
        a = Agent(llm=llm,
                  tool_registry={name: TOOLS[name] for name in ("list_dir",)},
                  tool_allowlist=None, tool_mode="native")
        events = list(a.run("看看目录结构", stream=True, web_enabled=False))

        finals = [e for e in events if e.get("type") == "final"]
        self.assertTrue(finals)
        self.assertIn("tests", finals[-1].get("text", ""))

        self.assertGreaterEqual(len(llm.messages_seen), 2)
        second = llm.messages_seen[1]
        contents = [c or "" for _r, c in second]
        self.assertFalse(
            any(c.strip().startswith("（并行调用）") for c in contents),
            "批次又把内部标记写进了历史：%r" % (second,))
        self.assertTrue(
            any(c.startswith("Observation: [list_dir]") for c in contents),
            "Observation 没有带上工具名：%r" % (second,))


class LegacyHistoryCleanupTests(unittest.TestCase):
    """存量会话兜底：发送前删纯标记；真发言不误伤；孤儿观察可裁剪。"""

    def test_fit_budget_drops_pure_marker_assistant_messages(self):
        a = Agent(llm=_ScriptedNativeLLM(["x"]), tool_registry={})
        head = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "问题"}]
        trail = [
            {"role": "assistant", "content": "（并行调用）read_file、read_file"},
            {"role": "user", "content": "Observation: [read_file] A"},
            {"role": "assistant", "content": "（并行调用）read_file 之后我确认了 A"},
        ]
        out = a._fit_budget(head, trail)
        contents = [m.get("content") for m in out]
        self.assertNotIn("（并行调用）read_file、read_file", contents,
                         "存量内部标记没有被清洗掉")
        self.assertIn("Observation: [read_file] A", contents,
                      "清洗标记时把观察一起删了——证据不能丢")
        self.assertIn("（并行调用）read_file 之后我确认了 A", contents,
                      "带正文的正常发言被误删了")

    def test_orphan_observations_are_pruned_oldest_first(self):
        trail = [
            {"role": "user", "content": "问题"},
            {"role": "user", "content": "Observation: [list_dir] a"},
            {"role": "user", "content": "Observation: [list_dir] b"},
        ]
        self.assertEqual(_find_prunable_trail(trail), (1, 1),
                         "孤儿观察没有进入裁剪队列，预算超限时只会一路删 head 历史")

    def test_the_only_observation_is_kept(self):
        trail = [
            {"role": "user", "content": "问题"},
            {"role": "user", "content": "Observation: [list_dir] only"},
        ]
        self.assertIsNone(_find_prunable_trail(trail),
                          "唯一的观察也被判可删——当前轮刚拿到的证据不能剪")


if __name__ == "__main__":
    unittest.main()
