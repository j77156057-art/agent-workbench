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


if __name__ == "__main__":
    unittest.main()