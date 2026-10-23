# -*- coding: utf-8 -*-
"""T1：工具图片观察通道（全部离线，不触网）。

覆盖：
- attach_tool_observation 三态（native 原图 / harness 视觉模型文字 / unavailable 降级）；
- 顺序 ReAct 路径：ToolResult.data["images"] 经能力门后挂到 Observation 消息；
- 无视觉能力时任何发给主模型的消息都不含 image content（多模态 schema 红线）；
- _fit_budget 只保留最后一条带图观察、单条 ≤4 张，head 用户图片不动；
- 并行只读批次携带图片，回填后仍受「仅最近一条带图」约束。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
from agent_runtime.tools import (  # noqa: E402
    SideEffect, ToolResult, execute_tool, tool_idempotency_scope)
from agent_runtime import vision as vision_mod  # noqa: E402


# 1x1 PNG 的 data URL；视觉层不校验像素，只搬运/转述。
_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
_IMG = f"data:image/png;base64,{_PNG_B64}"
_FINAL = "Thought: t\nFinal Answer: 已确认。"


def _act(tool, inp):
    return f"Thought: t\nAction: {tool}\nAction Input: {inp}"


class _ScriptedLLM:
    """按序回放 ReAct 文本，并记录每次真正发给模型的 messages。"""
    provider = "fake"
    model = "fake-1"

    def __init__(self, scripts, capability=None):
        self._scripts = list(scripts)
        self.calls = 0
        self.batches = []
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        self.capability = capability or {
            "context_window": 32768, "thinking": "none", "cloud": False}

    def chat(self, messages, stream=True, **kwargs):
        self.batches.append(messages)
        i = min(self.calls, len(self._scripts) - 1)
        self.calls += 1
        script = self._scripts[i]
        if stream:
            def g():
                for ch in script:
                    yield ch
            return g()
        return script

    def count_tokens(self, text):
        return 0


class _NativeBatchLLM:
    """原生工具协议：一轮发多个 tool_calls 触发并行批次。"""
    provider = "fake"
    model = "fake-native"

    def __init__(self, steps, capability=None):
        self._steps = steps
        self.i = 0
        self.batches = []
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        self.last_tool_calls = []
        self.capability = capability or {
            "context_window": 32768, "thinking": "none", "cloud": False,
            "vision": "native"}

    def chat(self, messages, stream=True, **kw):
        self.batches.append(messages)
        spec = self._steps[min(self.i, len(self._steps) - 1)]
        self.i += 1
        self.last_tool_calls = []
        content = ""
        if spec[0] == "tools":
            self.last_tool_calls = [
                {"id": f"c{i}", "name": n, "arguments": json.dumps(a)}
                for i, (n, a) in enumerate(spec[1])
            ]
        else:
            content = spec[1]
        if stream:
            def g():
                for ch in content:
                    yield ch
            return g()
        return content

    def count_tokens(self, text):
        return 0


class _FakeVisionClient:
    capability = {"vision": "native"}
    last_kw = {}

    def __init__(self, *args, **kwargs):
        pass

    def chat(self, messages, stream=False, **kw):
        type(self).last_kw = dict(kw)
        return "截图里是一个红色报错弹窗，标题为 Fatal Error。"


class AttachToolObservationTests(unittest.TestCase):
    """过门函数自身的三态与截断语义。"""

    def test_native_passes_images_through_with_note(self):
        text, images, audit = vision_mod.attach_tool_observation(
            "抓取完成", [_IMG],
            current_capability={"vision": "native"})
        self.assertEqual(audit["mode"], "native")
        self.assertEqual(images, [_IMG])
        self.assertIn("抓取完成", text)
        self.assertIn("含 1 张图片", text)

    def test_single_observation_capped_at_four(self):
        _text, images, audit = vision_mod.attach_tool_observation(
            "ok", [_IMG] * 6, current_capability={"vision": "native"})
        self.assertEqual(audit["mode"], "native")
        self.assertEqual(len(images), 4)

    def test_harness_mode_folds_vision_text_into_observation(self):
        env = {"DOCMIND_VISION_MODEL": "fake-vision",
               "DOCMIND_VISION_PROVIDER": "mock"}
        with patch.dict(os.environ, env, clear=False), \
                patch.object(vision_mod, "LLMClient", _FakeVisionClient):
            text, images, audit = vision_mod.attach_tool_observation(
                "抓取完成", [_IMG],
                current_capability={"vision": "unknown"})
        self.assertEqual(audit["mode"], "harness")
        self.assertIsNone(images, "harness 模式绝不能给主模型留 image content")
        self.assertIn("Fatal Error", text)
        self.assertIn("Harness 图片观察", text)
        self.assertEqual(_FakeVisionClient.last_kw.get("max_output_tokens"), 2000)

    def test_unavailable_mode_drops_images_with_clear_note(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCMIND_VISION_MODEL", None)
            text, images, audit = vision_mod.attach_tool_observation(
                "抓取完成", [_IMG], current_capability={"vision": "none"})
        self.assertEqual(audit["mode"], "unavailable")
        self.assertIsNone(images)
        self.assertIn("未确认支持图片识别", text)

    def test_empty_images_is_pass_through(self):
        text, images, audit = vision_mod.attach_tool_observation(
            "纯文本观察", [], current_capability={"vision": "native"})
        self.assertEqual((text, images, audit["mode"]),
                         ("纯文本观察", None, "none"))

    def test_vision_model_error_drops_images_with_failure_note(self):
        class _BoomVision:
            capability = {"vision": "native"}

            def __init__(self, *args, **kwargs):
                raise RuntimeError("vision backend down")

        env = {"DOCMIND_VISION_MODEL": "fake-vision",
               "DOCMIND_VISION_PROVIDER": "mock"}
        with patch.dict(os.environ, env, clear=False), \
                patch.object(vision_mod, "LLMClient", _BoomVision):
            text, images, audit = vision_mod.attach_tool_observation(
                "抓取完成", [_IMG],
                current_capability={"vision": "unknown"})
        self.assertEqual(audit["mode"], "error")
        self.assertIsNone(images)
        self.assertIn("视觉辅助失败", text)


class AgentSequentialVisionTests(unittest.TestCase):
    """顺序 ReAct 路径消费 ToolResult.data 图片。"""

    def _registry(self):
        def shot(_arg):
            return ToolResult(ok=True, text="截图完成：.docmind/screenshots/shot-1.jpg",
                              data={"images": [_IMG], "image_sources": ["local"]},
                              artifacts=[{"id": "shot-1", "kind": "image",
                                          "path": ".docmind/screenshots/shot-1.jpg"}])
        return {"shot_tool": {"description": "test screenshot", "func": shot}}

    def test_native_observation_message_carries_data_url(self):
        llm = _ScriptedLLM(
            [_act("shot_tool", "target: foreground"), _FINAL],
            capability={"context_window": 32768, "vision": "native"})
        a = agent_mod.Agent(llm=llm, tool_registry=self._registry())
        events = list(a.run("截一张图看看", stream=True))
        self.assertTrue(any(e.get("type") == "final" for e in events))
        # 第 2 次调用：Observation 已进消息，且带 data URL 图片。
        second = llm.batches[1]
        obs_msgs = [m for m in second
                    if m.get("role") == "user"
                    and "Observation:" in (m.get("content") or "")]
        self.assertEqual(len(obs_msgs), 1)
        self.assertEqual(obs_msgs[0].get("images"), [_IMG])
        self.assertIn("含 1 张图片", obs_msgs[0]["content"])

    def test_visual_artifact_is_exposed_on_observation_event(self):
        llm = _ScriptedLLM(
            [_act("shot_tool", "target: foreground"), _FINAL],
            capability={"context_window": 32768, "vision": "native"})
        events = list(agent_mod.Agent(llm=llm, tool_registry=self._registry()).run(
            "记录预览证据", stream=True))
        observation = next(event for event in events if event.get("type") == "observation")
        self.assertEqual(observation["artifacts"][0]["id"], "shot-1")

    def test_non_vision_model_never_receives_image_content(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCMIND_VISION_MODEL", None)
            llm = _ScriptedLLM(
                [_act("shot_tool", "target: foreground"), _FINAL],
                capability={"context_window": 32768, "vision": "none"})
            a = agent_mod.Agent(llm=llm, tool_registry=self._registry())
            list(a.run("截一张图看看", stream=True))
        for batch in llm.batches:
            for msg in batch:
                self.assertNotIn(
                    "images", msg,
                    f"非视觉模型消息出现 image content：{msg.get('role')}")
        obs_msgs = [m for batch in llm.batches for m in batch
                    if m.get("role") == "user"
                    and "Observation:" in (m.get("content") or "")]
        self.assertTrue(obs_msgs)
        self.assertIn("未确认支持图片识别", obs_msgs[0]["content"])

    def test_harness_vision_error_does_not_break_turn(self):
        class _BoomVision:
            capability = {"vision": "native"}

            def __init__(self, *args, **kwargs):
                raise RuntimeError("vision backend down")

        env = {"DOCMIND_VISION_MODEL": "fake-vision",
               "DOCMIND_VISION_PROVIDER": "mock"}
        with patch.dict(os.environ, env, clear=False), \
                patch.object(vision_mod, "LLMClient", _BoomVision):
            llm = _ScriptedLLM(
                [_act("shot_tool", "target: foreground"), _FINAL],
                capability={"context_window": 32768, "vision": "unknown"})
            a = agent_mod.Agent(llm=llm, tool_registry=self._registry())
            events = list(a.run("截一张图看看", stream=True))
        self.assertTrue(any(e.get("type") == "final" for e in events),
                        "视觉模型异常不应阻断回合收尾")
        for batch in llm.batches:
            for msg in batch:
                self.assertNotIn("images", msg)
        obs_msgs = [m for batch in llm.batches for m in batch
                    if m.get("role") == "user"
                    and "Observation:" in (m.get("content") or "")]
        self.assertIn("视觉辅助失败", obs_msgs[0]["content"])


class FitBudgetImageStripTests(unittest.TestCase):
    """_fit_budget：只保留最后一条带图 trail 观察，单条 ≤4 张。"""

    def _agent(self):
        llm = _ScriptedLLM([_FINAL])
        return agent_mod.Agent(llm=llm)

    def test_older_observation_images_stripped_latest_kept(self):
        a = self._agent()
        head = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "q", "images": ["user-upload"]}]
        trail = [
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "Observation: old",
             "images": ["old1", "old2"]},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "Observation: new",
             "images": ["new1", "new2"]},
        ]
        out = a._fit_budget(head, trail)
        old = next(m for m in out if m.get("content") == "Observation: old")
        new = next(m for m in out if m.get("content") == "Observation: new")
        self.assertNotIn("images", old)
        self.assertEqual(new["images"], ["new1", "new2"])
        # head 里用户当轮上传的图片不受影响
        self.assertEqual(out[1]["images"], ["user-upload"])

    def test_single_message_capped_at_four(self):
        a = self._agent()
        head = [{"role": "system", "content": "sys"}]
        trail = [
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "Observation: x",
             "images": ["i0", "i1", "i2", "i3", "i4", "i5"]},
        ]
        out = a._fit_budget(head, trail)
        obs = [m for m in out if m.get("content") == "Observation: x"][0]
        self.assertEqual(obs["images"], ["i0", "i1", "i2", "i3"])


class ParallelBatchVisionTests(unittest.TestCase):
    """并行批次的图片经能力门并受最近一条约束。"""

    def test_batch_carries_gated_images(self):
        registry = {
            "shot_a": {
                "description": "shot a",
                "func": lambda _arg: ToolResult(
                    ok=True, text="截图 A", data={"images": ["img-a"]}),
            },
            "shot_b": {
                "description": "shot b",
                "func": lambda _arg: ToolResult(
                    ok=True, text="截图 B", data={"images": ["img-b"]}),
            },
        }
        llm = _NativeBatchLLM([
            ("tools", [("shot_a", {"input": "x"}), ("shot_b", {"input": "y"})]),
            ("text", "Final Answer: 都看完了"),
        ], capability={"context_window": 32768, "vision": "native"})
        a = agent_mod.Agent(llm=llm, tool_mode="native", tool_registry=registry)
        events = list(a.run("并行截两张", stream=True))
        self.assertTrue(any(e.get("type") == "final" for e in events))
        second = llm.batches[1]
        obs_msgs = [m for m in second
                    if m.get("role") == "user"
                    and "Observation:" in (m.get("content") or "")]
        self.assertEqual(len(obs_msgs), 2)
        # 两条观察文本都带图片标注；但为控制上下文，只有最后一条保留 image content。
        self.assertTrue(all("含 1 张图片" in m["content"] for m in obs_msgs))
        with_images = [m for m in obs_msgs if m.get("images")]
        self.assertEqual(len(with_images), 1, with_images)
        self.assertEqual(with_images[0]["images"], ["img-b"])


class IdempotencyDataPreservationTests(unittest.TestCase):
    """副作用工具经幂等包装重建时 ToolResult.data 不丢。"""

    def test_data_kept_through_idempotency_wrap(self):
        def side_effect_tool(_arg):
            return ToolResult(ok=True, text="已执行",
                              data={"images": [_IMG], "image_sources": ["local"]})

        with tempfile.TemporaryDirectory() as tmp:
            with tool_idempotency_scope("test-vision-channel", tmp):
                result = execute_tool(
                    side_effect_tool, "target: foreground",
                    lambda _text: False, tool_name="wf_shot",
                    side_effect=SideEffect.MUTATING)
        self.assertTrue(result.ok)
        self.assertTrue(result.idempotency_key)
        self.assertFalse(result.replayed)
        self.assertEqual(result.data["images"], [_IMG])


if __name__ == "__main__":
    unittest.main()
