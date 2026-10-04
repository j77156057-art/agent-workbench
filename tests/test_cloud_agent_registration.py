# -*- coding: utf-8 -*-
"""R1 回归：云端路由改为「逐请求 llm 覆盖」，不再新建/注册 Agent。

旧病（B2 引入）：云端分支执行 `_SESSION_AGENTS[sid] = cloud_agent`，把注册表里的
会话 Agent 换成一个新对象，导致模块级 `agent`（= `_agent_for("default")`）变成孤儿：
set_config / ingest 等仍打旧对象；且该会话此后被永久黏到云端。

新机制：只为本轮造一个云端 client，随 `run(llm=cloud_llm)` 临时覆盖，请求结束还原；
不新建 Agent、不改 `_SESSION_AGENTS`。

不联网：用假 LLMClient + 假 route_for 构造云端分支，并真消费 SSE 流（触发一次
agent.run，但用的是假 client）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import config
import sessions


class _FakeLLM:
    """可用的假客户端：记录构造 kwarg 与 chat 调用次数。"""

    made = []

    def __init__(self, **kwargs):
        self.init_kw = kwargs
        self.provider = kwargs.get("provider", "fake")
        self.model = kwargs.get("model", "fake-1")
        self.capability = {"context_window": 32768, "thinking": "none", "cloud": False}
        self.prompt_budget = 0
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        self.calls = 0
        _FakeLLM.made.append(self)

    def chat(self, messages, stream=True, **kwargs):
        self.calls += 1
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        script = "Final Answer: ok"
        if stream:
            def g():
                for ch in script:
                    yield ch
            return g()
        return script

    def count_tokens(self, text):
        return max(0, len(text) // 3)


class CloudLlmOverrideTests(unittest.IsolatedAsyncioTestCase):
    SID = "default"

    def setUp(self):
        self._orig_route_for = api.route_for
        self._orig_llm_client = api.LLMClient
        self._orig_run = api.Agent.run
        self._orig_runtime = dict(config._RUNTIME)
        self._orig_turns = sessions.history(self.SID)
        self._orig_summary = sessions.summary_text(self.SID)

        self.agent_ref = api._agent_for(self.SID)
        self._orig_agent_llm = self.agent_ref.llm
        self._orig_agent_hist = list(self.agent_ref.history)
        self._orig_agent_summary = self.agent_ref.summary

        _FakeLLM.made = []
        self.local_fake = _FakeLLM(provider="local", model="local-1")
        self.agent_ref.llm = self.local_fake

        # 钉住前置守卫依赖的两个键，否则本文件的结果取决于**跑测试那台机器的配置**。
        #
        # 实测：`/api/chat` 在构造云端 client 之前有一道 Ollama 探活守卫
        # （`api.chat` 里 `_prov == "ollama"` 且服务不可达 -> 直接回「无法对话」）。
        # `get_runtime` 取不到键时会回落到模块常量 `LLM_PROVIDER`/`EMBEDDING_PROVIDER`，
        # 也就是 `.env`。于是「llm_provider=ollama 且 Ollama 没起」的机器上：
        #   · 云端那一轮被守卫拦掉，`_FakeLLM.made == []`；
        #   · 本地那一轮也被守卫拦掉，`Agent.run` 根本没被调用，
        #     `seen[-1]` 仍是上一次云端请求留下的 client ——
        #     报「本地路由应传 llm=None」，而这条测试压根没测到本地路由。
        # 这两条测试要的是「云端覆盖后能还原」，与 Ollama 无关，不该被它决定生死。
        #
        # 注意这不是给实现「开口子」：守卫拦**本地**回合仍然是正确的（本地模型
        # 确实没起）。云端那一轮不该被拦是另一件事，由
        # `tests/test_cloud_route_survives_local_model_down.py` 单独钉住。
        config.set_runtime("llm_provider", "deepseek")
        config.set_runtime("embedding_provider", "deepseek")

    def tearDown(self):
        api.route_for = self._orig_route_for
        api.LLMClient = self._orig_llm_client
        api.Agent.run = self._orig_run
        config._RUNTIME.clear()
        config._RUNTIME.update(self._orig_runtime)
        self.agent_ref.llm = self._orig_agent_llm
        self.agent_ref.history = self._orig_agent_hist
        self.agent_ref.summary = self._orig_agent_summary
        # 还原本会话的持久化内容，避免污染其他用例
        if self._orig_turns or self._orig_summary:
            sessions.save(self.SID, self._orig_turns, self._orig_summary)
        else:
            sessions.delete(self.SID)

    def _install_run_spy(self):
        """包裹 Agent.run，记录每次传入的 llm 关键字实参。"""
        seen = []
        orig = self._orig_run

        def spy(self, question, *args, **kwargs):
            seen.append(kwargs.get("llm"))
            return orig(self, question, *args, **kwargs)

        api.Agent.run = spy
        return seen

    async def _invoke(self, route):
        api.route_for = lambda q: {
            "route": route, "auto_cloud_enabled": True,
            "complexity": "high", "reason": "unit-test",
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

    async def test_cloud_uses_cloud_llm_then_restores(self):
        api.LLMClient = _FakeLLM
        config.set_runtime("llm_api_key", "fake-key-for-test")
        seen = self._install_run_spy()

        chunks = await self._invoke("cloud")

        cloud = [x for x in _FakeLLM.made if x is not self.local_fake]
        self.assertTrue(cloud, "云端分支应构造一个云端 LLMClient")
        self.assertEqual(cloud[-1].init_kw.get("provider"), "deepseek")
        self.assertEqual(cloud[-1].init_kw.get("api_key"), "fake-key-for-test")

        # 运行期传入并实际调用了云端 client
        self.assertIs(seen[-1], cloud[-1], "run 应收到云端 client")
        self.assertGreaterEqual(cloud[-1].calls, 1, "运行期应实际调用云端 client")
        self.assertEqual(self.local_fake.calls, 0, "云端请求不应调用本地 client")

        # 请求结束：llm 还原为本地原值
        self.assertIs(self.agent_ref.llm, self.local_fake, "请求结束后应还原本地 llm")

        # 不再新建/注册 Agent：注册对象 identity 未变，模块级 agent 不再是孤儿
        self.assertIs(api._SESSION_AGENTS[self.SID], self.agent_ref,
                      "云端请求不得替换 _SESSION_AGENTS 中的 Agent")
        self.assertIs(api.agent, api._SESSION_AGENTS["default"],
                      "模块级 agent 不得被孤立")

        self.assertTrue(any("data:" in c for c in chunks), "SSE 流应被正常消费")

    async def test_subsequent_local_request_uses_local_llm(self):
        # 复现 QA「黏云端」场景：先云端、后本地 → 本地请求必须用回本地 client
        api.LLMClient = _FakeLLM
        config.set_runtime("llm_api_key", "fake-key-for-test")
        seen = self._install_run_spy()

        await self._invoke("cloud")
        cloud = [x for x in _FakeLLM.made if x is not self.local_fake]
        cloud_calls_after_first = cloud[-1].calls
        local_calls_after_first = self.local_fake.calls

        await self._invoke("local")

        self.assertIsNone(seen[-1], "本地路由应传 llm=None（用会话自身本地 client）")
        self.assertEqual(self.local_fake.calls, local_calls_after_first + 1,
                         "本地请求应调用本地 client")
        self.assertEqual(cloud[-1].calls, cloud_calls_after_first,
                         "本地请求不得再调用云端 client（不黏云端）")
        self.assertIs(self.agent_ref.llm, self.local_fake)


if __name__ == "__main__":
    unittest.main()
