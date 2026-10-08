# -*- coding: utf-8 -*-
"""R2：模型上下文里**不得**出现本轮路由标签（泄露）。

## 用户实测报出来的现象

模型在回答里说：「本轮系统给我的路由建议是 local（本地模型，复杂度 0）」。

那是 harness 内部的元数据，用户不该看到，更不该由模型复述出来。

## 为什么「加个过滤器」这条路走不通

`agent._PromptLeakStreamFilter._MARKER = "【系统提示】"` 认的是**逐字回显**
旧版前缀的形态（`【系统提示】…用户问题：…`）。

而这里的泄露是模型**用自己的话复述**（「本轮系统给我的路由建议是…」）——
**没有任何正则能匹配复述**。api.py 早先把路由元数据从「拼在用户问题前面」
挪到「真正的 system message」，那只解决了位置，没解决「模型引用系统内容」。

所以修法是**不给模型看**，不是「事后过滤」。

## 保留了什么，为什么

保留的那句「只能用已配置且可审计的 provider」对模型行为**有约束力**；
删掉的是「local/cloud + 复杂度分」—— 对模型没有任何约束力，
唯一作用就是让它去播报自己的路由。

## ⚠ 这条测试盯的是**模型收到的 system 消息**，不是界面

界面上的 `route` 事件由另一条缺陷负责（标签算错），
见 `tests/test_route_label_follows_actual_client.py`。两条分开修、分开验。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import config
import sessions


class _RecordingLLM:
    """记录收到的每一条 system 消息。"""

    def __init__(self, **kwargs):
        self.provider = kwargs.get("provider", "fake")
        self.model = kwargs.get("model", "fake-1")
        self.capability = {"context_window": 32768, "thinking": "none", "cloud": False}
        self.prompt_budget = 0
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        self.system_texts = []

    def chat(self, messages, stream=True, **kwargs):
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system":
                self.system_texts.append(str(m.get("content") or ""))
        script = "Final Answer: ok"
        if stream:
            def g():
                for ch in script:
                    yield ch
            return g()
        return script

    def count_tokens(self, text):
        return max(0, len(text) // 3)


class RoutingHintDoesNotReachTheModelTests(unittest.IsolatedAsyncioTestCase):
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

        config.set_runtime("llm_provider", "deepseek")
        config.set_runtime("embedding_provider", "deepseek")
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

    async def _system_texts_for(self, route):
        api.route_for = lambda q: {
            "route": route, "auto_cloud_enabled": False,
            "complexity": 4, "reason": "复杂度达到升级阈值",
        }
        fake = _RecordingLLM(provider="deepseek", model="ds-1")
        self.agent_ref.llm = fake
        resp = await api.chat(
            question="你好", session_id=self.SID,
            tool_mode="", plan_mode="", web_mode="", thinking_mode="", images=None,
        )
        it = getattr(resp, "body_iterator", None)
        if it is not None:
            if hasattr(it, "__aiter__"):
                async for _ in it:
                    pass
            else:
                for _ in it:
                    pass
        return fake.system_texts

    async def test_the_route_label_never_reaches_the_model(self):
        """模型收到的 system 消息里**不得**出现路由标签或复杂度分。"""
        for route in ("local", "cloud"):
            texts = await self._system_texts_for(route)
            assert texts, "一条 system 消息都没收到 —— 前提不成立，判据失效"
            joined = "\n".join(texts)
            assert "模型路由建议" not in joined, (
                "route=%s：模型上下文里仍有「模型路由建议」标签" % route)
            assert "复杂度 4" not in joined, (
                "route=%s：复杂度分仍出现在模型上下文里" % route)

    async def test_the_substantive_provider_rule_is_kept(self):
        """删标签不许把**有约束力的**那条规则一起删掉。

        那条规则（只能用已配置且可审计的 provider）真的会约束模型行为；
        连它一起删，等于为了堵泄露把防护也拆了。
        """
        texts = await self._system_texts_for("cloud")
        joined = "\n".join(texts)
        assert "可审计" in joined, "有约束力的 provider 规则被误删了"
        assert "厂商身份" in joined, "厂商身份那一条防播报的约束被误删了"

    async def test_the_route_event_still_carries_the_label(self):
        """反向守卫：界面上的 route 事件**必须**仍然带标签。

        这是最容易改坏的地方 —— 修「模型不该看」时顺手把事件也删了，
        界面上就再也看不到路由状态了。两条是不同的事。
        """
        api.route_for = lambda q: {
            "route": "cloud", "auto_cloud_enabled": False,
            "complexity": 4, "reason": "复杂度达到升级阈值",
        }
        self.agent_ref.llm = _RecordingLLM(provider="deepseek", model="ds-1")
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
        got = None
        for c in chunks:
            body = c[len("data: "):].strip() if c.startswith("data: ") else None
            if not body:
                continue
            try:
                ev = json.loads(body)
            except ValueError:
                continue
            if ev.get("type") == "route":
                got = ev
                break
        assert got is not None, "SSE 里没有 route 事件 —— 前提不成立"
        assert got.get("route") == "cloud", "界面上看不到路由状态了：%r" % got


if __name__ == "__main__":
    unittest.main()
