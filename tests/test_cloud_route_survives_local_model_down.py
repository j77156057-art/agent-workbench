# -*- coding: utf-8 -*-
"""本地模型没起**不该**拦掉一个由云端服务的回合。

## 实测事故

`tests/test_cloud_agent_registration.py::test_cloud_uses_cloud_llm_then_restores`
在「llm_provider=ollama 且 Ollama 未运行」的机器上红，报：

    云端分支应构造一个云端 LLMClient

探针（`.tmp/cloudspy.py`）把现场固定下来：

    PROVIDERS['deepseek'].get('cloud')       = True
    PROVIDERS['deepseek'].get('api_key_env') = 'DEEPSEEK_API_KEY'
    key = get_runtime('llm_api_key')         = 'fake-key-for-test'
    => gate 应该通过?  True
    _FakeLLM.made 条数 = 0                    <-- 云端 client 一个都没造
    产出的事件 = ['data: {"type": "final", "text": "⚠️ 无法对话：**对话模型**配置为本机
                    Ollama，但服务未运行或不可达。…"}']

两个错叠在一起：路由误判（说**不能对话**）和建议误导（让用户去装 Ollama）。
而这一轮**根本不需要本地模型** —— `agent_policy.route_for` 的 `auto_cloud_enabled`
只看 `AGENT_AUTO_CLOUD`，与 `llm_provider` 无关，所以这个组合真实可达。

## 根因是顺序

早先的次序是「Ollama 探活守卫 -> 路由 -> 构造云端 client」，守卫**看不到**路由结果。
现在路由提到守卫之前，守卫与构造共用 `_resolve_cloud_client` 这一份判定。

## 反向守卫（防「把开关偷偷放开」）

守卫**没有**被削弱：
  · 本地回合（`_cloud_plan is None`）在 Ollama 不可达时**仍然被拦**——
    `LocalTurnIsStillBlockedTests` 钉住这一条；
  · 云端不可用（无 key / provider 不支持云端 / 路由非 cloud）时同样仍被拦；
  · 判定只有一份实现，构造处不允许再算一遍——两份逻辑漂移的方向恰好是
    「又拦掉一个云端能服务的回合」。
"""
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import api  # noqa: E402
import config  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from fastapi.responses import StreamingResponse  # noqa: E402


class _FakeLLM:
    made = []

    def __init__(self, **kw):
        self.init_kw = kw
        self.calls = 0
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        _FakeLLM.made.append(self)

    def chat(self, messages, stream=True, **kwargs):
        self.calls += 1
        script = "Final Answer: ok"
        if stream:
            def g():
                for ch in script:
                    yield ch
            return g()
        return script

    def count_tokens(self, text):
        return max(0, len(text) // 3)


_DOWN = {
    "reachable": False,
    "base": "http://127.0.0.1:11434",
    "provider": "ollama",
    "embedding_provider": "ollama",
    "needed_models": ["bge-m3"],
    "present_models": [],
    "missing_models": ["bge-m3"],
    "guidance": "未检测到 Ollama 服务（http://127.0.0.1:11434 无响应）。",
    "blocks_conversation": True,
    "search_degraded": False,
}


def _drain(resp):
    """SSE 响应体可能是异步或同步迭代器；统一成同步列表。"""
    it = getattr(resp, "body_iterator", None)
    if it is None:
        return []
    if hasattr(it, "__aiter__"):
        return _DrainAsync(it)
    return list(it)


class _DrainAsync:
    """把异步迭代器攒成一个可 await 的列表。"""

    def __init__(self, it):
        self._it = it

    def __await__(self):
        async def _go():
            out = []
            async for c in self._it:
                out.append(c)
            return out
        return _go().__await__()


class _Base(unittest.IsolatedAsyncioTestCase):
    SID = "cloud-guard"

    def setUp(self):
        self._orig_route_for = api.route_for
        self._orig_llm_client = api.LLMClient
        self._orig_check = api.check_ollama
        self._orig_runtime = dict(config._RUNTIME)
        _FakeLLM.made = []
        api.LLMClient = _FakeLLM
        api.check_ollama = lambda: dict(_DOWN)  # Ollama 不可达
        config.set_runtime("llm_provider", "ollama")
        config.set_runtime("embedding_provider", "ollama")
        config.set_runtime("llm_mode", "fixed")
        config.set_runtime("auto_cloud_preset_id", "")
        config.set_runtime("cloud_llm_provider", "")
        config.set_runtime("llm_api_key", "fake-key-for-test")

    def tearDown(self):
        api.route_for = self._orig_route_for
        api.LLMClient = self._orig_llm_client
        api.check_ollama = self._orig_check
        config._RUNTIME.clear()
        config._RUNTIME.update(self._orig_runtime)

    async def _invoke(self, route):
        api.route_for = lambda q: {
            "route": route, "auto_cloud_enabled": True,
            "complexity": "high", "reason": "unit-test",
        }
        resp = await api.chat(
            question="你好", session_id=self.SID,
            tool_mode="", plan_mode="", web_mode="", thinking_mode="", images=None,
        )
        self.assertIsInstance(resp, StreamingResponse)
        chunks = _drain(resp)
        if hasattr(chunks, "__await__"):
            chunks = await chunks
        assert chunks is not None, "_drain 必须给出列表，None 说明辅助函数自己坏了"
        return chunks

    @staticmethod
    def _text(chunks):
        return "".join(str(c) for c in chunks)


class CloudTurnSurvivesLocalModelDownTests(_Base):
    """本轮由云端服务 -> 本地模型没起与它无关，不该拦。"""

    async def test_a_cloud_turn_is_not_blocked(self):
        chunks = await self._invoke("cloud")
        self.assertTrue(_FakeLLM.made,
                        "云端 client 必须被构造出来")
        self.assertNotIn("无法对话", self._text(chunks),
                         "本地模型不可达不该拦住一个云端回合")

    async def test_the_cloud_client_gets_the_configured_key(self):
        await self._invoke("cloud")
        cloud = _FakeLLM.made[-1]
        self.assertEqual(cloud.init_kw.get("provider"), "deepseek")
        self.assertEqual(cloud.init_kw.get("api_key"), "fake-key-for-test")


class LocalTurnIsStillBlockedTests(_Base):
    """**反向守卫**：守卫没被削弱。本地回合该拦还得拦。"""

    async def test_a_local_turn_is_still_blocked(self):
        chunks = await self._invoke("local")
        self.assertIn("无法对话", self._text(chunks),
                      "本地路由 + 本地模型不可达 -> 必须拦，不能放行")
        self.assertFalse(_FakeLLM.made,
                         "本地回合不该构造任何 client")

    async def test_blocked_when_cloud_key_is_missing(self):
        config.set_runtime("llm_api_key", "")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            chunks = await self._invoke("cloud")
        self.assertIn("无法对话", self._text(chunks),
                      "路由到云端但没有 key -> 无路可走，必须拦")
        self.assertFalse(_FakeLLM.made, "没有 key 就不该构造云端 client")


class SearchDegradationIsStillDisclosedTests(_Base):
    """云端救回对话后，检索降级**必须**如实告知——不能静默。"""

    async def test_the_user_is_told_retrieval_is_unavailable(self):
        chunks = await self._invoke("cloud")
        text = self._text(chunks)
        self.assertIn("检索当前不可用", text,
                      "向量后端挂了却在界面上一个字不提，用户会以为检索过了")
        self.assertIn("云端对话模型仍可正常使用", text,
                      "要说清是哪一部分坏了——早先的文案只说「与对话模型无关」，"
                      "在这一轮里对话模型根本就是云端的")


if __name__ == "__main__":
    unittest.main()