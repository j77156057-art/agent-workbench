# -*- coding: utf-8 -*-
"""原生并行 tool_call 轮不得被「模型输出尚未结束」nudge 误伤。

## 实测事故（活体重跑 + 离线桩各复现一次）

长任务里 `agent.run` 的原生 function-calling 通道，每一轮发出 **>1 个并行 tool_call** 时，
`native_calls_this_turn=True`、`self._pending_batch` 被填、正文只有一句很短的思考散文。
这样的轮次：

    finish_reason != "tool_calls"（并行批次分支不置它）
    parsed["action"] is None（批次不改写 acc）
    has_real_final = False（没有 Final Answer 标记）

于是撞上收尾前的 nudge 判据 `truncated or not acc.strip() or not has_real_final`，
每轮白 nudge 一次：既 yield 一条误导性的 `模型输出尚未结束，正在继续执行。`，
又往 trail 注入一句「你既没调用工具也没给结论」的**假提示**——模型明明发了 tool_call。
实测 52 事件里刷出 10 条这种 notice。

## 修法

在 nudge 块之前为「原生并行批次轮」补一条干净的静默 `continue`：批次派发（含内部标记与
observation）本就由循环顶部 `if self._pending_batch:` 负责，这里只需推进，不再发 notice、
不再注 hint。单调用（`native_override → parsed["action"]`）`_pending_batch` 为空，不走这里。

## 反过来也必须成立

真正「只吐散文、没调工具、没给结论」的轮次仍要 nudge 一次，不能把门焊死。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402

NOTICE_TEXT = "模型输出尚未结束"


def _tc(name, arguments):
    import json
    return {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}


class _NativeBatchLLM:
    """按轮脚本地吐「并行 tool_call + 短散文」，最后一轮给 Final Answer。"""
    provider = "deepseek"
    model = "deepseek-chat"

    def __init__(self, rounds):
        # rounds: [( [tool_call,...], prose ), ...]
        self._rounds = list(rounds)
        self._idx = 0
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        self.last_tool_calls = []
        self.last_finish_reason = None

    def clone(self):
        return self

    def count_tokens(self, text):
        return 1

    def chat(self, messages, stream=True, **kw):
        calls, prose = self._rounds[min(self._idx, len(self._rounds) - 1)]
        self._idx += 1
        self.last_tool_calls = list(calls)
        # 并行批次分支不会把 finish_reason 置成 tool_calls——忠实还原
        self.last_finish_reason = None
        return iter([prose]) if stream else prose


class _NativeEmptyLLM:
    """只吐一句非结论散文，不调工具、不写 Final Answer——nudge 的正牌目标。"""
    provider = "deepseek"
    model = "deepseek-chat"

    def __init__(self, prose):
        self._prose = prose
        self._idx = 0
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        self.last_tool_calls = []
        self.last_finish_reason = None

    def clone(self):
        return self

    def count_tokens(self, text):
        return 1

    def chat(self, messages, stream=True, **kw):
        self._idx += 1
        self.last_tool_calls = []
        self.last_finish_reason = None
        return iter([self._prose]) if stream else self._prose


class NativeBatchNotNudgedTests(unittest.TestCase):
    def setUp(self):
        self._orig = getattr(agent_mod, "TOOLS", None)
        agent_mod.TOOLS = {"echo": {"func": lambda arg: "obs:" + str(arg)}}

    def tearDown(self):
        if self._orig is not None:
            agent_mod.TOOLS = self._orig

    def test_parallel_batch_round_emits_no_spurious_notice(self):
        llm = _NativeBatchLLM([
            ([_tc("echo", "a"), _tc("echo", "b")], "我先并行读两处取证。"),
            ([_tc("echo", "c"), _tc("echo", "d")], "再并行补两处。"),
            ([], "Final Answer: 综合结论：已核对完毕。"),
        ])
        a = agent_mod.Agent(llm=llm, tool_mode="native")
        events = list(a.run("用工具核对后汇总", stream=True, web_enabled=False))

        notices = [e for e in events
                   if e.get("type") == "notice" and NOTICE_TEXT in str(e.get("text", ""))]
        actions = [e for e in events if e.get("type") == "action"]
        finals = [e for e in events if e.get("type") == "final"]

        self.assertGreaterEqual(len(actions), 2,
                                "并行 tool_call 应被正常派发（修复不能退化成漏派发）：%s"
                                % [e.get("type") for e in events])
        self.assertEqual(len(notices), 0,
                         "并行调用轮被误判成「输出尚未结束」：%r"
                         % [n.get("text") for n in notices])
        self.assertTrue(finals, "应有 final")
        self.assertIn("综合结论", finals[-1].get("text", ""),
                      "最终答案应收敛到模型的真实结论：%r" % finals[-1].get("text", ""))


class PermanentBlockedToolBatchTests(unittest.TestCase):
    """批次路径不能绕过「不可重试工具」封禁。"""

    def setUp(self):
        from agent_runtime.tools import ToolResult
        self._orig = getattr(agent_mod, "TOOLS", None)
        self.calls = {"flaky": 0, "good": 0}

        def flaky(arg):
            self.calls["flaky"] += 1
            return ToolResult(False, "返回值不符合协议", error_kind="invalid_result")

        def good(arg):
            self.calls["good"] += 1
            return "GOOD-OBS"

        agent_mod.TOOLS = {
            "flaky": {"func": flaky, "capability": "read_local"},
            "good": {"func": good, "capability": "read_local"},
        }

    def tearDown(self):
        if self._orig is not None:
            agent_mod.TOOLS = self._orig

    def test_blocked_tool_in_a_native_batch_cannot_bypass_the_ban(self):
        llm = _NativeBatchLLM([
            ([_tc("flaky", "x")], ""),                      # 单调用：失败 1
            ([_tc("flaky", "z")], ""),                      # 换参再失败：2 次 → 封禁
            ([_tc("good", "y"), _tc("flaky", "w")], ""),    # 批次夹带被封禁工具
            ([], "Final Answer: 已收尾"),
        ])
        a = agent_mod.Agent(llm=llm, tool_mode="native")
        events = list(a.run("做点事。", stream=True, web_enabled=False))
        finals = [e.get("text", "") for e in events if e["type"] == "final"]
        self.assertEqual(self.calls["flaky"], 2, "被封禁的工具不能经批次再执行")
        self.assertEqual(self.calls["good"], 1, "批次里的其它工具仍应执行")
        self.assertTrue(finals)
        self.assertIn("不可重试", finals[-1])


class RealIncompleteRoundStillNudgesTests(unittest.TestCase):
    """反向守卫：只吐非结论散文、没调工具的一轮，nudge 必须照常触发（不把门焊死）。"""

    def setUp(self):
        self._orig = getattr(agent_mod, "TOOLS", None)
        agent_mod.TOOLS = {"echo": {"func": lambda arg: "obs:" + str(arg)}}

    def tearDown(self):
        if self._orig is not None:
            agent_mod.TOOLS = self._orig

    def test_empty_output_round_still_nudges(self):
        # 空正文 → `not acc.strip()` 命中 nudge；没有 tool_call，不触发批次静默 continue
        llm = _NativeEmptyLLM("")
        a = agent_mod.Agent(llm=llm, tool_mode="native")
        events = list(a.run("随便答一下", stream=True, web_enabled=False))
        notices = [e for e in events
                   if e.get("type") == "notice" and NOTICE_TEXT in str(e.get("text", ""))]
        self.assertGreaterEqual(len(notices), 1,
                                "真正的「输出尚未结束」轮次被误关了 nudge：%s"
                                % [e.get("type") for e in events])


if __name__ == "__main__":
    unittest.main()
