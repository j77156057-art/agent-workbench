# -*- coding: utf-8 -*-
"""R1：路由标签必须跟着**实际应答的 client** 走，而不是跟着复杂度启发式走。

## 用户实测报出来的现象

配好云端模型，问一句短话，界面显示「local」，模型也自述「本轮路由建议是 local」——
而实际应答的是云端模型。

## 根因：两套独立计算，没有交集

* `agent_policy.route_for()` 只按**复杂度**判：
  `route = 'cloud' if score >= 3 else 'local'`
  短消息（< 600 字、不含「架构/重构」）**必然 local**。
* `api.chat()` 里的 `is_cloud` 只看 `_cloud_plan`（本轮有没有为「云端升级」
  单独造一个 client）。

于是「配了云端 + 没触发复杂度升级」这个组合下，标签必然是错的。

而 `cloud_llm is None` 时 `run(llm=None)` 用的是**会话自己的 client**
（不是硬塞本地），所以实际厂商要看会话 client 的 provider，不能只看 `is_cloud`。

## ⚠ 这个断言不能只看「route 事件」

`route` 事件和模型上下文里的 hint 用的是同一个 `routing` 字典，所以
「事件说 cloud」和「模型上下文说 cloud」是一起被修的。
但**模型上下文里的 hint 本身另有问题**（见
`docs/known-defect-routing-hint-leaks-to-model.md`），两条要分开修、分开验。

## 不联网

用假 LLMClient + 假 `route_for`，真消费 SSE 流。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import config
import sessions


class _FakeLLM:
    """可用的假客户端。"""

    def __init__(self, **kwargs):
        self.provider = kwargs.get("provider", "fake")
        self.model = kwargs.get("model", "fake-1")
        self.capability = {"context_window": 32768, "thinking": "none", "cloud": False}
        self.prompt_budget = 0
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        self.seen_system = []

    def chat(self, messages, stream=True, **kwargs):
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system":
                self.seen_system.append(str(m.get("content") or ""))
        script = "Final Answer: ok"
        if stream:
            def g():
                for ch in script:
                    yield ch
            return g()
        return script

    def count_tokens(self, text):
        return max(0, len(text) // 3)


class RouteLabelFollowsActualClientTests(unittest.IsolatedAsyncioTestCase):
    """核心断言：短消息 + 会话跑在云端 -> 标签必须是 cloud。"""

    SID = "default"

    def setUp(self):
        self._orig_route_for = api.route_for
        self._orig_runtime = dict(config._RUNTIME)
        self._orig_turns = sessions.history(self.SID)
        self._orig_summary = sessions.summary_text(self.SID)

        self.agent_ref = api._agent_for(self.SID)
        self._orig_agent_llm = self.agent_ref.llm
        self._orig_agent_hist = list(self.agent_ref.history)
        self._orig_agent_summary = self.agent_ref.summary

        # 钉住前置守卫依赖的键，否则结果取决于跑测试那台机器的 .env。
        config.set_runtime("llm_provider", "deepseek")
        config.set_runtime("embedding_provider", "deepseek")
        # 「fixed」= 不允许自动升级云端，于是 `_cloud_plan` 必为 None，
        # `is_cloud` 必为 False —— **这正是要验的场景**：
        # 标签只能靠「会话 client 的 provider」判出来。
        config.set_runtime("llm_mode", "fixed")

    def tearDown(self):
        api.route_for = self._orig_route_for
        config._RUNTIME.clear()
        config._RUNTIME.update(self._orig_runtime)
        self.agent_ref.llm = self._orig_agent_llm
        self.agent_ref.history = self._orig_agent_hist
        self.agent_ref.summary = self._orig_agent_summary
        if self._orig_turns or self._orig_summary:
            sessions.save(self.SID, self._orig_turns, self._orig_summary)
        else:
            sessions.delete(self.SID)

    async def _invoke(self, route):
        api.route_for = lambda q: {
            "route": route, "auto_cloud_enabled": False,
            "complexity": 0, "reason": "本地模型可处理",
        }
        resp = await api.chat(
            question="你好", session_id=self.SID,
            tool_mode="", plan_mode="", web_mode="", thinking_mode="", images=None,
        )
        chunks = []
        it = getattr(resp, "body_iterator", None)
        if it is not None:
            if hasattr(it, "__aiter__"):
                async for c in it:
                    chunks.append(c)
            else:
                for c in it:
                    chunks.append(c)
        return chunks

    @staticmethod
    def _route_event(chunks):
        for c in chunks:
            body = c[len("data: "):].strip() if c.startswith("data: ") else None
            if not body:
                continue
            try:
                ev = json.loads(body)
            except ValueError:
                continue
            if ev.get("type") == "route":
                return ev
        return None

    async def test_cloud_session_on_a_short_message_is_labelled_cloud(self):
        """会话跑在云端 + 短消息（启发式给 local）-> 标签必须是 cloud。"""
        fake = _FakeLLM(provider="deepseek", model="ds-1")
        self.agent_ref.llm = fake
        chunks = await self._invoke("local")
        ev = self._route_event(chunks)
        assert ev is not None, "SSE 里没有 route 事件 —— 前提不成立，判据失效"
        assert ev.get("route") == "cloud", (
            "配了云端模型却仍被标成 %r（reason=%r）"
            % (ev.get("route"), ev.get("reason")))

    async def test_local_session_stays_local_even_when_heuristic_says_cloud(self):
        """反向：会话跑在本地时，即使启发式说 cloud 也不能标 cloud。"""
        fake = _FakeLLM(provider="ollama", model="qwen3.6:35b-a3b")
        self.agent_ref.llm = fake
        chunks = await self._invoke("cloud")
        ev = self._route_event(chunks)
        assert ev is not None, "SSE 里没有 route 事件 —— 前提不成立，判据失效"
        assert ev.get("route") == "local", (
            "本地会话被标成 %r" % ev.get("route"))

    async def test_complexity_reading_is_not_faked_to_match(self):
        """改标签**不许**顺手把 complexity 也改掉。

        complexity 是启发式的真实读数；把它一起改成「看起来自洽」的值，
        就掩盖了「本轮没触发升级阈值」这个事实 —— 那是排查时要看的线索。
        """
        fake = _FakeLLM(provider="deepseek", model="ds-1")
        self.agent_ref.llm = fake
        chunks = await self._invoke("local")
        ev = self._route_event(chunks)
        assert ev is not None
        assert ev.get("complexity") == 0, (
            "complexity 被改成了 %r —— 启发式的真实读数不该被动" % ev.get("complexity"))


if __name__ == "__main__":
    unittest.main()
