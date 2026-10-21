# -*- coding: utf-8 -*-
"""模型某一轮什么都没说时，子代理不该崩成 `AttributeError`。

## 实测故障（真实工作流trace，`wf-f3da9d3ffb32`）

`impl_update_hp_growth` 任务，4.9 秒后失败：

    error    : AttributeError: 'NoneType' object has no attribute 'strip'
    steps    : 0
    traceback:
        agent.py  _run_child  ->  for ev in child.run(...)
        agent.py  run         ->  yield from self._run_shell(...)
        agent.py  _run_shell  ->  for ev in inner:
        agent.py  _run        ->  acc = (acc + "\n" if acc.strip() else "") + ...
                                             ^^^^^^^^^

## 根因（链条每一环都有既存代码，而它们各自都是对的）

1. `llm.py` 的 `_complete()` 末尾是 `return getattr(msg, "content", None)` ——
   `content` 是 `None` 就**原样返回 None**。
2. 这件事本身**是对的**：tool-call-only 的消息 content 就是 None，原生 FC 通道
   正是靠它表达"这轮只调工具"。`tests/test_empty_provider_response.py::
   test_a_message_with_none_content_is_still_returned` 就把它钉成了正确行为。
3. `agent.py` 把返回值**直接赋给** `acc`，下一行 `acc.strip()` 当字符串用。

**缺的是第3 步的区分**：「带 tool_calls 的 None」是 tool-call-only 消息，
「不带 tool_calls 的 None」是**空响应**。旧代码不区分，于是后者崩成AttributeError。

## 后果

子代理整个失败、任务判红、工作流据此重规划——而真实情况只是
「模型这次什么都没说」。这正是本仓库反复出现的那类错：
**harness 把「一次可恢复的模型空转」记成了一次代码崩溃**。

## 修法

归一成空串，让它走本来就为它准备好的那条路：`format_stalls`
（`_MAX_FORMAT_STALLS`）会纠偏两次，然后**如实**告诉用户
「模型没按格式应答」。

刻意**不**改`llm.py` 那个 `getattr(..., None)`：改它会破坏原生 FC 通道
（那里 content=None 是有效信号），而且有一个测试正把它钉成正确行为。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402


class _TurnsLLM:
    """按脚本逐轮回放的假模型，可以显式回 None。"""

    provider = "fake"
    model = "turns"

    def __init__(self, script):
        self.script = list(script)
        self.i = 0
        self.last_usage = {}
        self.last_tool_calls = []

    def clone(self):
        return _TurnsLLM(self.script)

    def chat(self, messages, stream=True, **kw):
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        self.last_tool_calls = []
        turn = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        if turn is None:
            # 与 llm.py 的 `getattr(msg, "content", None)` 同形：空响应。
            return None
        if stream:
            return (ch for ch in turn)
        return turn

    def count_tokens(self, text):
        return 1


class EmptyModelTurnTests(unittest.TestCase):
    """复现原始崩法：第一轮正常给工具调用，第二轮返回 None。"""

    def _run(self, script, role="researcher"):
        llm = _TurnsLLM(script)
        a = agent.Agent(llm=llm)
        return a._run_child(role, "读一个文件")

    def test_a_none_turn_does_not_crash_the_subagent(self):
        out = self._run([
            "Thought: 先看目录\nAction: list_dir\nAction Input: path: .",
            None,                      # ← 模型这一轮什么都没说
        ])
        self.assertNotIn("AttributeError", str(out.get("error") or ""),
                         "空响应又被记成了一次代码崩溃：%r" % (out.get("error"),))
        self.assertNotIn("'NoneType'", str(out.get("error") or ""))

    def test_it_is_reported_as_an_empty_model_turn_not_a_success(self):
        """也不能反过来当成成功——空响应既不是崩溃，也不是答对了。"""
        out = self._run([
            "Thought: 先看目录\nAction: list_dir\nAction Input: path: .",
            None,
        ])
        self.assertIn(out.get("status"), {"ok", "failed", "degraded"})
        if out.get("status") == "ok":
            # 报成功的话，结论必须**如实**说明它是降级/空转来的，
            # 而不能把一句空话当答案交给下游任务。
            self.assertTrue(out.get("degraded") or not (out.get("conclusion") or "").strip(),
                            "空响应被判成功且既没标降级、结论也不空——下游会拿到假答案")

    def test_an_empty_turn_is_not_reported_as_if_the_model_had_said_none(self):
        """归一必须落成**空串**，不是把 None 的 repr 当成模型的正文交出去。

        `if acc is None: acc = "None"` 一样不崩——所以「不崩」那条判据抓不住它。
        但报告里会多出一段「模型最后一次输出：None」：模型明明什么都没说，报告却说
        它说了「None」。下游拿到的是一句**假话**，比崩溃更难查（崩溃至少是红的）。
        """
        out = self._run([
            "Thought: 先看目录\nAction: list_dir\nAction Input: path: .",
            None,
        ])
        conclusion = out.get("conclusion") or ""
        self.assertNotRegex(conclusion, r"(?<!\w)None(?!\w)",
                            "空响应被当成模型的正文交出去了：结论里出现了字面量 None")

    def test_a_none_turn_on_the_very_first_turn_also_survives(self):
        """崩法与「第几轮」无关：旧代码第一轮就崩在 acc.strip()。"""
        out = self._run([None])
        self.assertNotIn("AttributeError", str(out.get("error") or ""),
                         "第一次调用就返回 None 时崩了：%r" % (out.get("error"),))

    def test_a_normal_turn_is_unaffected(self):
        out = self._run([
            "Thought: 看看目录\nAction: list_dir\nAction Input: path: .",
            "Final Answer: 目录里有一些文件。",
        ])
        self.assertEqual(out.get("status"), "ok")
        self.assertIn("目录", out.get("conclusion") or "")


class LlmContractTests(unittest.TestCase):
    """`llm.py` 那个 `getattr(msg, "content", None)` 是对的，钉住它不被改坏。"""

    def test_chat_may_still_return_none_for_a_tool_call_only_message(self):
        """原生 FC 通道靠 content=None 表达「这轮只调工具」。

        改掉它就会破坏那条通道——所以本轮刻意**只**在 agent 侧归一。
        """
        import inspect

        src = inspect.getsource(agent)
        self.assertIn("if acc is None:", src,
                      "agent 侧的空响应归一不见了")
        # 归一必须发生在任何 .strip() 之前
        idx_norm = src.index("if acc is None:")
        idx_strip = src.index("acc.strip()", idx_norm)
        self.assertLess(idx_norm, idx_strip,
                        "归一写在 .strip() 之后——等于没修")


class NativeToolCallStillReachesTheTextProtocolTests(unittest.TestCase):
    """原生 FC 通道的 `last_tool_calls` 必须被归一进文本协议。

    `agent.py:3400` 附近把 `llm.last_tool_calls` 取出来，翻译成
    `Action: /Action Input:` 拼进 `acc`。这层归一一旦被摘掉（哪怕只是改成
    `calls = []`），模型给的工具调用就**静默消失**：既不执行也不报错，子代理
    拿着一个空结论继续往下走。

    这层此前没有任何测试覆盖——`EmptyModelTurnTests` 的脚本全走文本协议
    （回复里自带 `Action:`），模型从不设 `last_tool_calls`，所以那条分支
    一次都没被走到过。
    """

    def test_the_native_tool_calls_are_translated_into_the_text_protocol(self):
        import inspect

        src = inspect.getsource(agent)
        self.assertIn('getattr(self.llm, "last_tool_calls"', src,
                      "原生 FC 的 last_tool_calls 归一不见了——工具调用会静默消失")
        self.assertIn('f"Action: {_nm}\\nAction Input: {_nin}"', src,
                      "单调用那条分支不再拼出 Action:/Action Input: 文本")
        self.assertIn("native_override = (_nm, _nin)", src,
                      "单调用分支不再设置 native_override")

    def test_translation_happens_before_the_text_is_used(self):
        """归一必须发生在 `acc` 被当成最终文本用掉之前。"""
        import inspect

        src = inspect.getsource(agent)
        idx_fetch = src.index('getattr(self.llm, "last_tool_calls"')
        idx_use = src.index('f"Action: {_nm}\\nAction Input: {_nin}"', idx_fetch)
        self.assertLess(idx_fetch, idx_use,
                        "先用了 acc 才去取 last_tool_calls——归一来不及生效")


class CancellationBeatsEmptyTurnTests(unittest.TestCase):
    """空响应归一**不能**盖过取消检查。

    `agent.py:3391-3393` 的顺序是：先 `if acc is None: acc = ""`，再
    `_raise_if_cancelled()`。两行调个位置看着无害，实则取消标记是在
    `acc` 归一**之后**才被检查的——一旦调换，被取消的子代理会先拿到一个
    空串继续往下走，取消就失效了。
    """

    def test_the_cancellation_check_still_runs_right_after_the_normalisation(self):
        import inspect

        src = inspect.getsource(agent)
        idx_norm = src.index("if acc is None:")
        tail = src[idx_norm:idx_norm + 200]
        self.assertIn("_raise_if_cancelled()", tail,
                      "归一之后 200 字符内看不到取消检查——它被挪走了，"
                      "被取消的子代理会先把空串当正常一轮走完")
        idx_cancel = src.index("_raise_if_cancelled()", idx_norm)
        self.assertLess(idx_norm, idx_cancel,
                        "取消检查跑在归一之前了——顺序反了")


if __name__ == "__main__":
    unittest.main()