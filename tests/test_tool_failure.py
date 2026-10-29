"""工具失败的分级处置——三类"重试必然同样失败"曾经被当成"再试一次说不定就成了"。

## 为什么这个文件存在

`execute_tool` 早就把失败分了类（`ToolResult.error_kind`），但主循环**一视同仁**：
不管哪一类，都只给模型一句"换一种方式重试（改用其他工具或改写查询）"。

而三类失败重试必然同样失败：

| error_kind | 真实性质 | 一律重试的代价 |
|---|---|---|
| `invalid_result` | 工具返回值不符协议 —— **代码 bug** | 浪费步数，原因埋在观察堆里 |
| `idempotency_in_doubt` | 上次执行中途挂了，完成状态未知 | **绕过唯一那道防重复副作用的闸门** |
| `idempotency_unavailable` | 幂等账本不可用 | 无从安全重试 |

`idempotency_in_doubt` 那条最要紧：`execute_tool` 已经**拒绝执行**了，但主循环若
还提示模型重试，模型换个参数再调 —— 幂等账本按新参数算出新 key，于是这道唯一
挡住重复副作用的闸门被绕开。**提示模型重试，在这一类上是错的信息。**

反过来，`timeout` 恰恰值得原样重试（大概率瞬时抖动），而旧措辞只说"换个方式"，
模型往往会去换工具而不是重试 —— 该重试的没重试。

## 处置分级

    transient  可原样重试：timeout / legacy_failure
    permanent  禁止重试：invalid_result / idempotency_* / project_stage_blocked
    unknown    看情况：exception（错误文本带 bug 迹象则归 permanent）
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_runtime import tool_failure as tf  # noqa: E402


class ClassificationTests(unittest.TestCase):

    def test_timeout_is_transient(self):
        """超时要鼓励原样重试——旧措辞只说"换个方式"，模型反而去换工具。"""
        self.assertEqual(tf.classify_failure("timeout"), "transient")

    def test_legacy_failure_is_transient(self):
        self.assertEqual(tf.classify_failure("legacy_failure"), "transient")

    def test_invalid_result_is_permanent(self):
        """工具返回值不符协议是代码 bug，重试 100% 还是同样的错。"""
        self.assertEqual(tf.classify_failure("invalid_result"), "permanent")

    def test_idempotency_in_doubt_is_permanent(self):
        """最要紧的一条：重放会造成重复副作用，必须禁止。"""
        self.assertEqual(tf.classify_failure("idempotency_in_doubt"), "permanent")

    def test_idempotency_unavailable_is_permanent(self):
        self.assertEqual(tf.classify_failure("idempotency_unavailable"), "permanent")

    def test_project_stage_blocked_is_permanent(self):
        self.assertEqual(tf.classify_failure("project_stage_blocked"), "permanent")

    def test_exception_with_bug_hints_is_permanent(self):
        """`AttributeError: 'NoneType' object has no attribute 'peek'` 这类是代码 bug。"""
        for text in ("工具执行失败（AttributeError）。",
                     "TypeError: can only concatenate str to str",
                     "NameError: name 'policy' is not defined",
                     "工具执行失败（KeyError）。"):
            with self.subTest(text=text):
                self.assertEqual(tf.classify_failure("exception", text), "permanent")

    def test_exception_without_bug_hints_is_unknown(self):
        """拿不准就别武断，让模型自己判断——但也不许当成必然可重试。"""
        self.assertEqual(tf.classify_failure("exception", "连接被重置"), "unknown")

    def test_unknown_kind_fails_open(self):
        """不认识的类别归 unknown 而不是 transient：新类别不该被默认当成可重试。"""
        self.assertEqual(tf.classify_failure("brand_new_kind"), "unknown")

    def test_empty_kind_is_transient(self):
        self.assertEqual(tf.classify_failure(""), "transient")


class McpConnectorFailureTests(unittest.TestCase):
    """MCP 连接器起不来 → 必须判 permanent，**不能**鼓励原样重试。

    ## 实测代价（2026-10-16，真机 UI「正在执行 127s」）

    `dev_list_connector_tools` / `dev_mcp_call` 把失败写成**返回值字符串**
    （「MCP 调用失败：…」），不抛异常，于是 `error_kind` 是空的 —— 空串落在
    `TRANSIENT_ERROR_KINDS` 里，主循环据此告诉模型「这是一次**可重试**的失败，
    可以用完全相同的参数直接重试一次」。

    而 Godot 连接器的 `uvx godot-ai attach` 起不来（编辑器没开 / 8000 端口被
    Agent 工作台 后端自己占着）时，一次失败要 **60~70 秒**。agent 按提示再调一次，
    两次叠起来就是 UI 上那个 127s，且第二次必然同样失败。

    **失败本身没错，错的是让它失败两遍。**
    """

    def test_a_dead_connector_process_is_permanent_even_with_no_error_kind(self):
        """没有 error_kind（字符串失败路径）也要认出来。"""
        texts = (
            "MCP 进程意外退出（code=98）。godot-ai attach [PORT_OCCUPIED]",
            "工具清单失败：MCPError: MCP 进程已退出（code=1）。",
            "MCP 调用失败：Address already in use",
            "引擎未运行，请先打开 Godot 编辑器",
            "连接器未启用，请先在工作台启用并审批",
        )
        for text in texts:
            with self.subTest(text=text[:40]):
                self.assertEqual(tf.classify_failure("", text), "permanent")

    def test_it_does_not_swallow_a_genuine_timeout(self):
        """反向守卫：超时仍然该鼓励原样重试，别把这一类也禁掉。"""
        self.assertEqual(tf.classify_failure("timeout", "工具执行超时"), "transient")
        # 文本里带 "timeout" 但 kind 明确是 timeout → 仍 transient
        self.assertEqual(
            tf.classify_failure("timeout", "MCP initialize 超时（180s）"), "transient")

    def test_an_unrelated_string_failure_is_not_permanent(self):
        """不相关的字符串失败不得被这条判据顺手禁掉。"""
        for text in ("检索无果", "参数格式不对，缺少 name", "返回结果为空"):
            with self.subTest(text=text):
                self.assertNotEqual(tf.classify_failure("", text), "permanent")

    def test_the_guidance_tells_the_model_not_to_retry_the_connector(self):
        category, guidance = tf.failure_guidance(
            "", "MCP 调用失败：MCP 进程意外退出（code=98）。PORT_OCCUPIED")
        self.assertEqual(category, "permanent")
        self.assertIn("重试必然同样失败", guidance)

    def test_the_failure_hint_names_the_real_cause(self):
        """提示要点出真因（端口被占），而不是让 Agent 从安装噪声里自己翻。"""
        from tools import _mcp_failure_hint
        hint = _mcp_failure_hint(
            "MCP 进程意外退出（code=98）。Installed 73 packages in 4.94s\n"
            "godot-ai attach [PORT_OCCUPIED]: Port 8000 is occupied")
        self.assertIn("端口", hint)
        self.assertIn("Agent 工作台", hint)

    def test_an_unrelated_failure_gets_no_hint(self):
        from tools import _mcp_failure_hint
        self.assertEqual(_mcp_failure_hint("连接器未启用"), "")


class GuidanceTests(unittest.TestCase):
    """措辞必须明确回答"能不能再试"——这是本模块存在的全部理由。"""

    def test_permanent_says_do_not_retry(self):
        _, text = tf.failure_guidance("invalid_result")
        self.assertIn("重试必然同样失败", text)
        self.assertIn("不要", text)

    def test_transient_says_same_args_retry(self):
        _, text = tf.failure_guidance("timeout")
        self.assertIn("可重试", text)
        self.assertIn("完全相同的参数", text)

    def test_guidance_names_the_kind(self):
        """模型需要知道是哪一类失败，否则无法针对性换做法。"""
        for kind in ("timeout", "invalid_result", "idempotency_in_doubt"):
            with self.subTest(kind=kind):
                _, text = tf.failure_guidance(kind)
                self.assertIn(kind, text)

    def test_unknown_does_not_promise_retry(self):
        _, text = tf.failure_guidance("exception")
        self.assertIn("不要原样重试", text)


class RetryBudgetTests(unittest.TestCase):

    def test_permanent_failures_do_not_consume_the_budget(self):
        """不累加熔断计数：否则模型还没试够别的路，整轮就被提前收尾了。"""
        self.assertEqual(tf.retry_budget_cost("permanent"), 0)

    def test_transient_and_unknown_do_consume_it(self):
        self.assertEqual(tf.retry_budget_cost("transient"), 1)
        self.assertEqual(tf.retry_budget_cost("unknown"), 1)


class AgentWiringTests(unittest.TestCase):
    """接线检查：分类必须真的进了主循环，而不是只写了个没人调用的模块。"""

    def test_agent_imports_the_module(self):
        import agent as agent_mod
        self.assertTrue(hasattr(agent_mod, "_tool_failure"),
                        "agent.py 没导入 tool_failure —— 模块写了却没接线")

    def test_the_source_uses_the_guidance(self):
        import agent as agent_mod
        import inspect
        src = inspect.getsource(agent_mod.Agent._run)
        self.assertIn("failure_guidance", src)
        self.assertIn("retry_budget_cost", src)
        self.assertIn("_permanent_failures", src)

    def test_the_old_blanket_retry_wording_is_gone(self):
        """旧措辞「请换一种方式重试」不区分类别，正是这次要消掉的东西。"""
        import agent as agent_mod
        import inspect
        src = inspect.getsource(agent_mod.Agent._run)
        self.assertNotIn("请换一种方式\n", src.replace("\r", ""))
        self.assertNotIn("例如改用 web_search，或换关键词", src,
                         "这句一视同仁的措辞还在——失败分类就白做了")


class EndToEndBehaviourTests(unittest.TestCase):
    """真跑一轮 Agent：不可重试的失败必须立刻换工具，而不是继续撞。"""

    def _run_with_failing_tool(self, tool_result, question="做点事。"):
        import agent as agent_mod

        class _LLM:
            provider = "mock"
            model = "mock"
            last_usage = {}

            def __init__(self, replies):
                self.replies = list(replies)
                self.seen = []

            def count_tokens(self, messages):
                return 100

            def clone(self):
                return self

            def chat(self, messages, stream=False, **kw):
                self.seen.append(messages)
                return self.replies.pop(0) if self.replies else "Final Answer: 结束"

        llm = _LLM(["Thought: 试一下\nAction: flaky\nAction Input: x", "Final Answer: 已收尾"])
        registry = {"flaky": {
            "func": lambda arg: tool_result,
            "capability": "read_local",
        }}
        agent = agent_mod.Agent(llm=llm, tool_registry=registry)
        events = []
        for ev in agent.run(question, stream=True):
            events.append(ev)
        return events, llm

    def test_invalid_result_tells_the_model_not_to_retry(self):
        from agent_runtime.tools import ToolResult
        bad = ToolResult(False, "返回值不符合协议", error_kind="invalid_result")
        events, llm = self._run_with_failing_tool(bad)
        joined = " ".join(
            str(m.get("content") or "")
            for m in (llm.seen[-1] if llm.seen else [])
            if isinstance(m, dict))
        self.assertIn("重试必然同样失败", joined,
                      "没把「不可重试」告诉模型——它会换个参数再撞一次")

    def test_timeout_tells_the_model_to_retry_the_same_call(self):
        from agent_runtime.tools import ToolResult
        slow = ToolResult(False, "工具执行失败（TimeoutError）。", error_kind="timeout")
        events, llm = self._run_with_failing_tool(slow)
        joined = " ".join(
            str(m.get("content") or "")
            for m in (llm.seen[-1] if llm.seen else [])
            if isinstance(m, dict))
        self.assertIn("完全相同的参数", joined,
                      "超时是瞬时抖动，该鼓励原样重试而不是换工具")


class PermanentFailureToolSwitchTests(unittest.TestCase):
    """连续不可重试失败后：nudge 让换工具，就必须真的能换；重撞被禁工具才停。"""

    @staticmethod
    def _run(scripts, registry):
        import agent as agent_mod

        class _LLM:
            provider = "mock"
            model = "mock"
            last_usage = {}

            def __init__(self, replies):
                self.replies = list(replies)

            def count_tokens(self, messages):
                return 100

            def clone(self):
                return self

            def chat(self, messages, stream=False, **kw):
                return self.replies.pop(0) if self.replies else "Final Answer: 结束"

        agent = agent_mod.Agent(llm=_LLM(scripts), tool_registry=registry)
        events = []
        for ev in agent.run("做点事。", stream=True):
            events.append(ev)
        finals = [e.get("text") for e in events if e["type"] == "final"]
        return events, finals[-1] if finals else ""

    def test_switching_to_another_tool_is_allowed(self):
        """实测 bug：两次不可重试失败后 nudge 说「改用其它工具」，但任何 Action 都被锁死。"""
        from agent_runtime.tools import ToolResult
        calls = {"flaky": 0, "good": 0}

        def flaky(arg):
            calls["flaky"] += 1
            return ToolResult(False, "返回值不符合协议", error_kind="invalid_result")

        def good(arg):
            calls["good"] += 1
            return "GOOD-OBS"

        scripts = [
            "Thought: 试\nAction: flaky\nAction Input: x",
            "Thought: 再试\nAction: flaky\nAction Input: z",
            "Thought: 换工具\nAction: good\nAction Input: y",
            "Final Answer: 已收尾",
        ]
        _, final = self._run(scripts, {
            "flaky": {"func": flaky, "capability": "read_local"},
            "good": {"func": good, "capability": "read_local"},
        })
        self.assertEqual(calls["flaky"], 2)
        self.assertEqual(calls["good"], 1, "nudge 让换工具，别的工具必须能执行")
        self.assertEqual(final, "已收尾")

    def test_retrying_the_blocked_tool_still_stops(self):
        """封禁只针对那个工具：模型再撞它一次就停，不能无限重试。"""
        from agent_runtime.tools import ToolResult
        calls = {"flaky": 0}

        def flaky(arg):
            calls["flaky"] += 1
            return ToolResult(False, "返回值不符合协议", error_kind="invalid_result")

        scripts = [
            "Thought: 试\nAction: flaky\nAction Input: x",
            "Thought: 再试\nAction: flaky\nAction Input: z",
            "Thought: 第三次\nAction: flaky\nAction Input: w",
            "Final Answer: 已收尾",
        ]
        _, final = self._run(scripts, {
            "flaky": {"func": flaky, "capability": "read_local"},
        })
        self.assertEqual(calls["flaky"], 2, "第三次调用必须在执行前被拦下")
        self.assertIn("不可重试", final)

    def test_success_resets_that_tools_permanent_streak(self):
        """非连续失败不该累计：失败 -> 成功 -> 再失败 只是一次。"""
        from agent_runtime.tools import ToolResult
        calls = {"flaky": 0, "good": 0}
        seq = [
            ToolResult(False, "返回值不符合协议", error_kind="invalid_result"),
            "OK",
            ToolResult(False, "返回值不符合协议", error_kind="invalid_result"),
        ]

        def flaky(arg):
            calls["flaky"] += 1
            return seq.pop(0)

        def good(arg):
            calls["good"] += 1
            return "GOOD-OBS"

        scripts = [
            "Thought: 试\nAction: flaky\nAction Input: x",
            "Thought: 再试\nAction: flaky\nAction Input: z",
            "Thought: 又试\nAction: flaky\nAction Input: w",
            "Thought: 换工具\nAction: good\nAction Input: y",
            "Final Answer: 已收尾",
        ]
        events, final = self._run(scripts, {
            "flaky": {"func": flaky, "capability": "read_local"},
            "good": {"func": good, "capability": "read_local"},
        })
        reflections = [e.get("text", "") for e in events if e["type"] == "reflection"]
        self.assertFalse(any("反复返回不可重试" in t for t in reflections),
                         "失败-成功-失败 不是连续两次，不该触发封禁提示")
        self.assertEqual(calls["good"], 1)
        self.assertEqual(final, "已收尾")


if __name__ == "__main__":
    unittest.main()