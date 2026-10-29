# -*- coding: utf-8 -*-
"""SSE 重试层：fallback 也必须享有退避；不可重试错误立即收尾。

2026-10-29 实测发现：`fallback_done` 被塞进重试判断（`if fallback_done or not
retryable`），于是切到备用模型后任何异常都直接终结 —— 代码里「重置 attempts_left
让 fallback 也走两轮退避」那行永远用不到。本文件用假 Agent 钉住真实调用次数。
"""
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402

_FALLBACK = object()
_LOCAL_ROUTE = {"route": "local", "complexity": 0, "reason": "test"}


class _FakeAgent:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.llm = types.SimpleNamespace(provider="mock", model="mock", capability={})
        self.last_checkpoint_id = None
        self.calls = []

    def run(self, question, **kwargs):
        llm = kwargs.get("llm")
        self.calls.append(llm)
        return self.behaviour(llm)


def _chat(agent):
    from starlette.testclient import TestClient

    with patch.object(api, "_agent_for", return_value=agent), \
            patch.object(api, "route_for", return_value=dict(_LOCAL_ROUTE)), \
            patch.object(api, "_resolve_cloud_client", return_value=None), \
            patch.object(api, "_select_fallback_llm", return_value=_FALLBACK), \
            patch.object(api, "_random"), patch.object(api._random, "random", return_value=0.0):
        client = TestClient(api.app)
        return client.post("/api/chat",
                           data={"question": "你好", "session_id": "retry-layer-test"})


class ChatRetryLayerTests(unittest.TestCase):
    def test_fallback_gets_its_own_retries(self):
        calls = {"primary": 0, "fallback": 0}

        def behaviour(llm):
            if llm is _FALLBACK:
                calls["fallback"] += 1
                if calls["fallback"] <= 2:
                    raise api._FakeConnError("fallback blip")
                return iter([{"type": "final", "text": "FALLBACK-OK"}])
            calls["primary"] += 1
            raise api._FakeConnError("primary down")

        resp = _chat(_FakeAgent(behaviour))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls["primary"], 3, "主模型：首次 + 两轮退避")
        self.assertEqual(calls["fallback"], 3,
                         "备用模型：首次 + 两轮退避（修复前第 1 次就终结）")
        self.assertIn("FALLBACK-OK", resp.text)

    def test_non_retryable_error_is_not_retried(self):
        calls = {"n": 0}

        def behaviour(llm):
            calls["n"] += 1
            raise api.MissingAPIKey("没有 Key")

        resp = _chat(_FakeAgent(behaviour))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls["n"], 1, "不可重试错误必须一次就收尾")
        self.assertIn("没有 Key", resp.text)


if __name__ == "__main__":
    unittest.main()
