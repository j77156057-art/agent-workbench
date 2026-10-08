# -*- coding: utf-8 -*-
"""非流式路径也必须把模型的思考送进 `reasoning_sink`。

## 这个洞是怎么发现的

2026-10-09 真跑 `deepseek-flash` 验 `b710f2d`（思考画像修复）时，
流式与非流式出现**不一致**：

| 路径 | 思考开 |
|---|---|
| 流式 | reasoning **937 字** |
| 非流式 | reasoning **0 字** |

而 content 长度明显不同（157 vs 147 起步），说明**参数生效了、内容被丢**。
两侧的差别只在实现：`StreamChat` 一直在映射 `delta.reasoning_content`，
非流式只收了 `tool_calls` 和 `content`。

## 为什么这是「静默丢内容」而不是无害省略

`reasoning_sink` 唯一消费者是 `agent.py` 的 `reasoning_q` —— 也就是前端
「深度思考」面板的唯一来源。**丢了它，用户看到的就是「没有思考过程」**，
和「模型没思考」长得一模一样，无从分辨。

## ⚠ 本测试怎么断言（不许复刻实现）

第一版如果自己 copy 一遍抽取逻辑，回退生产代码后测试照样绿 —— 那次变异
实测就是无效的。所以这里**直接构造假 `client.chat.completions.create`**，
让 `LLMClient.chat()` 真跑，断言 `reasoning_sink` 真被写。
"""

import json

import pytest

import llm as llm_mod


class _Msg:
    def __init__(self, content, reasoning=None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, msg):
        self.message = msg
        self.finish_reason = "stop"


class _Resp:
    def __init__(self, msg):
        self.choices = [_Choice(msg)]
        self.usage = None


class _Completions:
    """捕获 kwargs；按 stream 返回非流式响应或流。"""

    def __init__(self, msg):
        self._msg = msg
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if kwargs.get("stream"):
            resp = _Resp(self._msg)
            # 只有一个 chunk；StreamChat 会逐 chunk 读 delta
            class _Chunk:
                class choices:
                    pass
            del _Chunk
            return _FakeStream([self._msg])
        return _Resp(self._msg)


class _FakeStream:
    """最小流：每项暴露 `.choices[0].delta`。"""

    def __init__(self, msgs):
        self._msgs = msgs
        self.finish_reason = "stop"

    def __iter__(self):
        for m in self._msgs:
            ch = type("C", (), {})()
            d = type("D", (), {})()
            d.content = m.content
            d.reasoning_content = m.reasoning_content
            d.tool_calls = m.tool_calls
            choice = type("CC", (), {"delta": d, "finish_reason": None})()
            ch.choices = [choice]
            yield ch


def _cloud_client(monkeypatch, msg):
    c = llm_mod.LLMClient(provider="deepseek", model="deepseek-flash")
    caps = _Completions(msg)
    c.client = type("C", (), {
        "chat": type("Chat", (), {"completions": caps})()})()
    return c, caps


class TestNonStreamingReasoningReachesSink:
    """非流式必须把 reasoning_content 送进 sink（云侧）。"""

    def test_cloud_non_streaming_appends_reasoning(self):
        msg = _Msg(content="答案是 18。", reasoning="先算净效率：1/6 - 1/9 = 1/18")
        c, _caps = _cloud_client(None, msg)
        sink: list = []
        out = c.chat([{"role": "user", "content": "几小时注满？"}],
                     stream=False, enable_thinking=True, reasoning_sink=sink)
        assert out == "答案是 18。", "content 被改动了"
        assert sink, "**非流式没有把 reasoning_content 送进 sink**"
        assert "1/18" in "".join(sink)

    def test_cloud_non_streaming_omits_when_no_reasoning(self):
        """模型没思考时不该塞空串 —— 空串会让前端多一个空段落。"""
        msg = _Msg(content="2", reasoning=None)
        c, _caps = _cloud_client(None, msg)
        sink: list = []
        c.chat([{"role": "user", "content": "1+1?"}],
               stream=False, enable_thinking=True, reasoning_sink=sink)
        assert sink == [], "无思考时写入了空串：%r" % sink

    def test_none_sink_is_not_required(self):
        """没传 sink 也不能炸 —— 大部分调用方不关心思考。"""
        msg = _Msg(content="ok", reasoning="想了一下")
        c, _caps = _cloud_client(None, msg)
        out = c.chat([{"role": "user", "content": "hi"}], stream=False)
        assert out == "ok"


class TestStreamingStillWorks:
    """回归：流式那条原本就是好的，不能被非流式的改动带坏。"""

    def test_streaming_still_appends_reasoning(self):
        msg = _Msg(content="18", reasoning="净效率 1/18")
        c, _caps = _cloud_client(None, msg)
        sink: list = []
        pieces = list(c.chat([{"role": "user", "content": "几小时？"}],
                             stream=True, enable_thinking=True,
                             reasoning_sink=sink))
        assert "".join(pieces) == "18"
        assert "".join(sink) == "净效率 1/18"


class _FakeHTTPResp:
    """最小 urllib 响应：只提供 read()。"""

    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw


class _FakeOpener:
    def __init__(self, payload):
        self._payload = payload
        self.request = None

    def open(self, req, timeout=None):
        self.request = req
        return _FakeHTTPResp(self._payload)


def _ollama_client(monkeypatch, payload):
    import urllib.request
    opener = _FakeOpener(payload)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: opener)
    c = llm_mod.LLMClient(provider="ollama", model="qwen3.6:35b-a3b")
    return c, opener


class TestOllamaNonStreamingReasoning:
    """ollama 非流式也必须把 `message.thinking` 送进 sink。

    ⚠ 第一版漏了这条：变异实测时把 ollama 的 append 注掉，**测试照样全绿**
    —— 说明那 4 个测试一个都没走到 ollama 路径。补上这条才是真的两路都钉住。
    """

    def test_ollama_non_streaming_appends_thinking(self, monkeypatch):
        payload = {"message": {"content": "18",
                               "thinking": "净效率：1/6 减 1/9 等于 1/18"},
                   "done_reason": "stop"}
        c, opener = _ollama_client(monkeypatch, payload)
        sink: list = []
        out = c.chat([{"role": "user", "content": "几小时注满？"}],
                     stream=False, enable_thinking=True, reasoning_sink=sink)
        assert out == "18", "content 被改动了"
        assert sink, "**ollama 非流式没有把 message.thinking 送进 sink**"
        assert "1/18" in "".join(sink)

    def test_ollama_non_streaming_omits_when_no_thinking(self, monkeypatch):
        payload = {"message": {"content": "2"}, "done_reason": "stop"}
        c, _ = _ollama_client(monkeypatch, payload)
        sink: list = []
        c.chat([{"role": "user", "content": "1+1?"}],
               stream=False, enable_thinking=True, reasoning_sink=sink)
        assert sink == [], "无思考时写入了空串：%r" % sink


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
