# -*- coding: utf-8 -*-
"""LLM 弹性层：重试/退避/deadline/可重试判定（离线，不触网）。"""
import time
import threading
import unittest
from types import SimpleNamespace

from llm import retry_call, is_retryable, _OllamaStream, StreamChat, LLMClient
import config


class _Err(Exception):
    def __init__(self, code=None, msg=""):
        super().__init__(msg or f"http {code}")
        if code is not None:
            self.status_code = code


class RetryableClassificationTests(unittest.TestCase):
    def test_rate_limit_and_5xx_are_retryable(self):
        self.assertTrue(is_retryable(_Err(429, "too many requests")))
        self.assertTrue(is_retryable(_Err(500, "internal error")))
        self.assertTrue(is_retryable(_Err(503, "server error")))
        self.assertTrue(is_retryable(TimeoutError("timed out")))
        self.assertTrue(is_retryable(_Err(None, "connection reset by peer")))

    def test_client_errors_are_not_retryable(self):
        self.assertFalse(is_retryable(_Err(401, "unauthorized")))
        self.assertFalse(is_retryable(_Err(403, "forbidden")))
        self.assertFalse(is_retryable(_Err(400, "bad request")))


class RetryCallTests(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _Err(503, "server error")
            return "ok"

        self.assertEqual(retry_call(fn, attempts=5, base=0.0), "ok")
        self.assertEqual(calls["n"], 3)

    def test_non_retryable_raises_immediately(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise _Err(401, "unauthorized")

        with self.assertRaises(_Err):
            retry_call(fn, attempts=5, base=0.0)
        self.assertEqual(calls["n"], 1, "客户端错误不应触发重试")

    def test_exhausted_retries_raise_last_error(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise _Err(500, "internal error")

        with self.assertRaises(_Err):
            retry_call(fn, attempts=3, base=0.0)
        self.assertEqual(calls["n"], 3)

    def test_deadline_stops_retrying(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise _Err(503, "server error")

        with self.assertRaises(Exception):
            retry_call(fn, deadline=time.monotonic() + 0.05, attempts=1000, base=0.05)
        self.assertLess(calls["n"], 1000, "到达 deadline 后必须停止重试")

    def test_past_deadline_fails_fast(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "never"

        with self.assertRaises(TimeoutError):
            retry_call(fn, deadline=time.monotonic() - 1, attempts=3, base=0.0)
        self.assertEqual(calls["n"], 0, "已过期的 deadline 不应发起任何调用")


class OllamaStreamCancellationTests(unittest.TestCase):
    def test_thinking_is_available_before_first_content(self):
        sink = []
        response = self._Response([
            b'{"message":{"thinking":"planning"}}',
            b'{"message":{"content":"answer"},"done":true}',
        ])
        stream = iter(_OllamaStream(response, reasoning_sink=sink))
        self.assertEqual(next(stream), "")
        self.assertEqual(sink, ["planning"])
        self.assertEqual(list(stream), ["answer"])

    def test_cloud_reasoning_is_available_before_first_content(self):
        sink = []
        chunks = [SimpleNamespace(usage=None, choices=[SimpleNamespace(
            finish_reason=None, delta=SimpleNamespace(
                reasoning_content="planning", content=None, tool_calls=None))]),
            SimpleNamespace(usage=None, choices=[SimpleNamespace(
                finish_reason="stop", delta=SimpleNamespace(
                    reasoning_content=None, content="answer", tool_calls=None))])]
        stream = iter(StreamChat(chunks, reasoning_sink=sink))
        self.assertEqual(next(stream), "")
        self.assertEqual(sink, ["planning"])
        self.assertEqual(list(stream), ["answer"])

    class _Response:
        def __init__(self, rows=()):
            self.rows = list(rows)
            self.closed = threading.Event()

        def __iter__(self):
            return iter(self.rows)

        def close(self):
            self.closed.set()

    def test_cancel_event_closes_blocked_response(self):
        cancel = threading.Event()
        response = self._Response()
        stream = _OllamaStream(response, cancel_event=cancel)
        cancel.set()
        self.assertTrue(response.closed.wait(1.0), "取消后应关闭底层 Ollama 响应")

    def test_normal_stream_closes_response_after_consumption(self):
        response = self._Response([
            b'{"message":{"content":"done"},"done":true}',
        ])
        stream = _OllamaStream(response)
        self.assertEqual(list(stream), ["done"])
        self.assertTrue(response.closed.is_set())

    def test_openai_compatible_stream_closes_on_cancel(self):
        cancel = threading.Event()

        class _Stream:
            def __init__(self):
                self.closed = threading.Event()

            def __iter__(self):
                return iter(())

            def close(self):
                self.closed.set()

        response = _Stream()
        wrapped = StreamChat(response, cancel_event=cancel)
        cancel.set()
        self.assertTrue(response.closed.wait(1.0), "云端流取消后应关闭底层响应")
        list(wrapped)


class NonStreamCancellationTests(unittest.TestCase):
    """非流式调用在有取消事件时也必须能打断底层生成。"""

    @staticmethod
    def _client_with_stream(stream):
        client = object.__new__(LLMClient)
        client.provider = "custom"
        client.model = "test-model"
        client.capability = {"thinking": "none", "cloud": True, "context_window": 32768}
        client.output_budget = 256
        client.max_retries = 1
        client.retry_base = 0.0
        client.timeout = 2.0
        client.last_usage = {}
        client.last_tool_calls = []
        # 诊断字段（`__init__` 里新增的那一组）。这个 fixture 用 `object.__new__`
        # 绕过 `__init__`，所以它们**不会自动存在**——而 `chat()` 会读
        # `last_call_probe` / `last_stream_ref`，缺了就 AttributeError。
        # 这不是实现的问题，是 fixture 的义务跟着变了。
        client.last_finish_reason = None
        client.last_stream_ref = None
        client.last_raw_head = ""
        client.last_call_probe = False
        client.last_messages = []
        # chat() 的缺 Key 守卫同样读 __init__ 字段；本 fixture 是 custom（免 Key）。
        client.requires_api_key = False
        client.provider_label = "custom"
        client.api_key = ""

        class _Completions:
            def create(self, **kwargs):
                self.kwargs = kwargs
                return stream

        client.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        client._to_openai_messages = lambda messages: messages
        return client

    def test_cloud_non_stream_cancel_closes_response_and_raises(self):
        cancel = threading.Event()

        class _BlockingStream:
            def __init__(self):
                self.closed = threading.Event()

            def __iter__(self):
                yield SimpleNamespace(
                    choices=[SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(content="partial", reasoning_content=None,
                                               tool_calls=None),
                    )],
                    usage=None,
                )
                self.closed.wait(2.0)

            def close(self):
                self.closed.set()

        raw = _BlockingStream()
        client = self._client_with_stream(raw)
        outcome = []

        def run():
            try:
                client.chat([{"role": "user", "content": "hi"}],
                            stream=False, cancel_event=cancel)
            except BaseException as exc:  # GeneratorExit is intentional here
                outcome.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.05)
        cancel.set()
        worker.join(1.0)
        self.assertFalse(worker.is_alive(), "取消后非流式请求不应继续阻塞")
        self.assertTrue(raw.closed.is_set())
        self.assertTrue(any(isinstance(exc, GeneratorExit) for exc in outcome))

    def test_cloud_non_stream_cancelable_path_still_returns_text(self):
        cancel = threading.Event()

        class _FiniteStream:
            def __iter__(self):
                yield SimpleNamespace(
                    choices=[SimpleNamespace(
                        finish_reason="stop",
                        delta=SimpleNamespace(content="hello", reasoning_content=None,
                                               tool_calls=None),
                    )],
                    usage=None,
                )

            def close(self):
                pass

        client = self._client_with_stream(_FiniteStream())
        self.assertEqual(
            client.chat([{"role": "user", "content": "hi"}],
                        stream=False, cancel_event=cancel),
            "hello",
        )


class ThinkingResolutionTests(unittest.TestCase):
    """_resolve_thinking：native 恒开 / toggle 跟随开关 / none 恒关。"""

    def _client(self, provider, model):
        from llm import LLMClient
        return LLMClient(provider=provider, model=model)

    def test_native_model_forced_on_even_when_switched_off(self):
        c = self._client("deepseek", "deepseek-reasoner")
        self.assertTrue(c._resolve_thinking(False))
        self.assertTrue(c._resolve_thinking(None))

    def test_toggle_family_follows_flag(self):
        c = self._client("ollama", "qwen3:8b")
        self.assertTrue(c._resolve_thinking(True))
        self.assertFalse(c._resolve_thinking(False))

    def test_plain_model_never_thinks(self):
        c = self._client("ollama", "qwen2.5:7b")
        self.assertFalse(c._resolve_thinking(True))
        self.assertFalse(c._resolve_thinking(None))

    def test_runtime_toggle_used_when_flag_omitted(self):
        from config import set_runtime
        c = self._client("qwen", "qwen3-235b-a22b")
        try:
            set_runtime("llm_enable_thinking", "1")
            self.assertTrue(c._resolve_thinking(None))
            set_runtime("llm_enable_thinking", "0")
            self.assertFalse(c._resolve_thinking(None))
            # 显式参数优先于运行时开关
            self.assertTrue(c._resolve_thinking(True))
        finally:
            set_runtime("llm_enable_thinking", "")


class OllamaPayloadTests(unittest.TestCase):
    """ollama 原生 /api/chat：think 字段恒携带、num_ctx 按画像封顶、输出预算给满。"""

    def _capture(self, model, thinking_on, messages=None, output_limit=None, reject_image=False):
        import json as _json
        from unittest.mock import patch
        from llm import LLMClient

        # 离线：窗口实时探测打桩为 None，回退内置画像，保证 num_ctx 断言确定
        with patch("llm.probe_ollama_context", return_value=None):
            client = LLMClient(provider="ollama", model=model)
        captured = {}

        class _Resp:
            def read(self):
                return b'{"message":{"content":"ok"},"prompt_eval_count":7,"eval_count":3}'

        class _Opener:
            def open(self, req, timeout=None):
                if reject_image and "payload" not in captured:
                    import io
                    from urllib.error import HTTPError
                    captured["payload"] = _json.loads(req.data.decode("utf-8"))
                    raise HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"image input rejected"}'))
                captured["payload"] = _json.loads(req.data.decode("utf-8"))
                return _Resp()

        with patch("llm._gpu_acquire", return_value=True), \
                patch("llm._gpu_release"), patch("llm._gpu_note_activity"), \
                patch("llm.urllib.request.build_opener", return_value=_Opener()):
            out = client._ollama_chat(
                messages if messages is not None else [{"role": "user", "content": "hi"}],
                stream=False, thinking_on=thinking_on, max_output_tokens=output_limit)
        return captured["payload"], out

    def test_image_rejection_retries_text_once_without_losing_evidence(self):
        messages = [{"role": "user", "content": "source evidence", "images": ["data:image/png;base64,aGVsbG8="]}]
        payload, out = self._capture("qwen3:8b", False, messages, reject_image=True)
        self.assertEqual(out, "ok")
        self.assertNotIn("images", payload["messages"][0])
        self.assertIn("source evidence", payload["messages"][0]["content"])
        self.assertIn("图片被模型服务拒绝", payload["messages"][0]["content"])
        self.assertIn("images", messages[0])

    def test_request_output_limit_preserves_context_window(self):
        baseline, _ = self._capture("qwen3:8b", True)
        bounded, _ = self._capture("qwen3:8b", True, output_limit=1024)
        self.assertLessEqual(bounded["options"]["num_predict"], 1024)
        self.assertEqual(baseline["options"]["num_ctx"], bounded["options"]["num_ctx"])

    def test_quick_lookup_limit_does_not_apply_to_development(self):
        from agent import _quick_lookup_output_limit
        self.assertEqual(_quick_lookup_output_limit("帮我搜索一下今天的天气"), 1024)
        self.assertIsNone(_quick_lookup_output_limit("帮我开发一个天气查询系统"))
        self.assertIsNone(_quick_lookup_output_limit("分析今天的天气异常原因"))

    def test_image_data_urls_normalized_without_mutating_messages(self):
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        messages = [{"role": "user", "content": "look",
                     "images": ["data:image/png;base64," + png, png]}]
        payload, out = self._capture("qwen3:8b", False, messages)
        self.assertEqual(out, "ok")
        self.assertEqual(payload["messages"][0]["images"], [png, png])
        self.assertTrue(messages[0]["images"][0].startswith("data:"))
        client = object.__new__(LLMClient)
        cloud = client._to_openai_messages(messages)
        self.assertNotIn("images", cloud[0])
        for part in cloud[0]["content"][1:]:
            self.assertEqual(part["image_url"]["url"], "data:image/png;base64," + png)

    def test_payload_always_carries_think_flag(self):
        from config import (prompt_token_budget, output_token_budget,
                             model_context_window)
        payload_on, out = self._capture("qwen3:8b", True)
        self.assertEqual(out, "ok")
        self.assertIs(payload_on["think"], True)
        # 16k 窗口：num_ctx 按「prompt预算 + 输出预算(窗口派生,不再写死3072) + 512」
        # 封顶到真实窗口；num_predict 给足派生输出预算（放不下时按窗口余量裁剪,保底512）。
        win = model_context_window("ollama", "qwen3:8b")   # 16384（probe 已打桩 None）
        budget = prompt_token_budget("ollama", "qwen3:8b")
        out_b = output_token_budget("ollama", "qwen3:8b")
        want = budget + out_b + 512
        self.assertEqual(payload_on["options"]["num_ctx"], min(want, win))
        self.assertEqual(payload_on["options"]["num_predict"],
                         min(out_b, max(512, win - budget - 512)))

        payload_off, _ = self._capture("qwen3:8b", False)
        self.assertIs(payload_off["think"], False)

    def test_plain_model_payload_think_false_ctx_by_window(self):
        from config import (prompt_token_budget, output_token_budget,
                             model_context_window)
        # qwen2.5:7b 画像窗口 32768：大窗口不再被 14.5k 限死，输出预算给足
        payload, _ = self._capture("qwen2.5:7b", False)
        self.assertIs(payload["think"], False)
        win = model_context_window("ollama", "qwen2.5:7b")   # 32768（override 画像）
        budget = prompt_token_budget("ollama", "qwen2.5:7b")
        out_b = output_token_budget("ollama", "qwen2.5:7b")
        want = budget + out_b + 512
        self.assertEqual(payload["options"]["num_ctx"], min(want, win))
        self.assertEqual(payload["options"]["num_predict"],
                         min(out_b, max(512, win - budget - 512)))

    def test_small_window_model_caps_ctx_and_keeps_min_output(self):
        # 未知 ollama 模型 → 窗口 16384（非小窗口）；构造一个 8k 画像的客户端
        from llm import LLMClient
        from unittest.mock import patch
        with patch("llm.probe_ollama_context", return_value=None):
            client = LLMClient(provider="ollama", model="tiny:7b")
        client.capability = {"context_window": 8192, "thinking": "none", "cloud": False}
        import json as _json
        captured = {}

        class _Resp:
            def read(self):
                return b'{"message":{"content":"x"}}'

        class _Opener:
            def open(self, req, timeout=None):
                captured["payload"] = _json.loads(req.data.decode("utf-8"))
                return _Resp()

        with patch("llm._gpu_acquire", return_value=True), \
                patch("llm._gpu_release"), patch("llm._gpu_note_activity"), \
                patch("llm.urllib.request.build_opener", return_value=_Opener()):
            client._ollama_chat([{"role": "user", "content": "hi"}],
                                stream=False, thinking_on=False)
        self.assertEqual(captured["payload"]["options"]["num_ctx"], 8192)
        self.assertGreaterEqual(captured["payload"]["options"]["num_predict"], 512)


class OllamaContextProbeTests(unittest.TestCase):
    """/api/show 窗口探测：字段解析、进程内缓存、失败静默回退、LLMClient 接入。"""

    def tearDown(self):
        import llm
        llm._OLLAMA_CTX_CACHE.clear()
        from config import clear_context_window_override
        clear_context_window_override("ollama", "zz-unit-custom:1b")

    def test_parse_show_payload_variants(self):
        from llm import parse_ollama_show_context as p
        # 新版字段 model_info.<arch>.context_length
        self.assertEqual(p({"model_info": {"llama.context_length": 32768}}), 32768)
        # 多个架构字段取最大值，无关数值（total_params）不干扰
        self.assertEqual(p({"model_info": {
            "llama.context_length": 8192,
            "qwen2.context_length": 32768,
            "total_params": 8_000_000_000,
        }}), 32768)
        # 顶层字段兜底
        self.assertEqual(p({"context_length": 16384}), 16384)
        # 无法识别的载荷一律 None
        self.assertIsNone(p(None))
        self.assertIsNone(p({}))
        self.assertIsNone(p({"model_info": {"llama.context_length": 256}}))
        self.assertIsNone(p({"context_length": "4096"}))

    def test_native_base_strips_v1_suffix(self):
        from llm import _ollama_native_base
        self.assertEqual(_ollama_native_base("http://127.0.0.1:11434/v1"),
                         "http://127.0.0.1:11434")
        self.assertEqual(_ollama_native_base("http://127.0.0.1:11434"),
                         "http://127.0.0.1:11434")

    def test_probe_success_uses_native_show_endpoint_and_caches(self):
        import json as _json
        from unittest.mock import patch
        import llm
        seen = {"urls": [], "n": 0}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return _json.dumps({"model_info": {"llama.context_length": 65536}}).encode()

        class _Opener:
            def open(self, req, timeout=None):
                seen["n"] += 1
                seen["urls"].append(req.full_url)
                return _Resp()

        with patch("llm.urllib.request.build_opener", return_value=_Opener()):
            win = llm.probe_ollama_context("unitmodel:1b",
                                           "http://127.0.0.1:11434/v1", force=True)
        self.assertEqual(win, 65536)
        self.assertTrue(seen["urls"][0].endswith("/api/show"))
        self.assertNotIn("/v1/api", seen["urls"][0])
        # 第二次不强制：命中缓存，不再发请求
        with patch("llm.urllib.request.build_opener",
                   side_effect=AssertionError("缓存命中不应再建 opener")):
            win2 = llm.probe_ollama_context("unitmodel:1b",
                                            "http://127.0.0.1:11434/v1")
        self.assertEqual(win2, 65536)
        self.assertEqual(seen["n"], 1)

    def test_probe_failure_returns_none_silently(self):
        from unittest.mock import patch
        import llm

        class _BadOpener:
            def open(self, req, timeout=None):
                raise OSError("connection refused")

        with patch("llm.urllib.request.build_opener", return_value=_BadOpener()):
            self.assertIsNone(llm.probe_ollama_context("zz-missing:1b", force=True))
        # 失败结果同样缓存：切模型等路径重复构造客户端时不会反复等待超时
        with patch("llm.urllib.request.build_opener",
                   side_effect=AssertionError("失败结果应缓存")):
            self.assertIsNone(llm.probe_ollama_context("zz-missing:1b"))

    def test_client_adopts_probed_window_and_custom_override_wins(self):
        import os as _os
        from unittest.mock import patch
        from llm import LLMClient
        from config import set_context_window_override

        env = {"DOCMIND_OLLAMA_PROBE": "1"}
        with patch.dict(_os.environ, env, clear=False), \
                patch("llm.probe_ollama_context", return_value=40960) as mk:
            c = LLMClient(provider="ollama", model="zz-unit-probe:1b")
        self.assertEqual(mk.call_count, 1)
        self.assertEqual(c.context_source, "probe")
        self.assertEqual(c.capability["context_window"], 40960)
        # ⚠⚠ **这条断言原为 `int(40960 * 0.75)`（=30720）。**
        # 那个 75% 是旧公式：把窗口的四分之三全给 prompt，**输出侧不留余量**。
        # `config.prompt_token_budget` 已改为
        #     reserve = max(LLM_MAX_TOKENS, output_token_budget(...)) + OUTPUT_TOKEN_RESERVE
        #     prompt_budget = window - reserve
        # 所以现在 prompt_budget 必然**小于** 75%。
        #
        # 为什么不直接写 26932：写死数字会让公式下次调整时这条测试又红一遍，
        # 而那时人得重新推导一遍才敢改。**断言契约，不断言结果数字**：
        #   ① prompt + 输出预留 必须不越过窗口（这是这次改动要修的事）
        #   ② prompt_budget 必须小于 75%（证明余量真的被留出来了）
        budget = c.prompt_budget
        reserve = max(config.LLM_MAX_TOKENS,
                      config.output_token_budget("ollama", "zz-unit-probe:1b", 40960)) \
            + config.OUTPUT_TOKEN_RESERVE
        self.assertEqual(budget, 40960 - reserve,
                         "prompt_budget 不符合「窗口减预留」的新契约")
        self.assertLess(budget, int(40960 * 0.75),
                        "prompt 仍占 75% 以上 —— 输出侧没有余量，正是这次要修的")
        self.assertGreater(budget, 0)

        # 手填覆盖存在时跳过探测，窗口以覆盖为准
        set_context_window_override("ollama", "zz-unit-custom:1b", 20000)
        with patch.dict(_os.environ, env, clear=False), \
                patch("llm.probe_ollama_context",
                      side_effect=AssertionError("有手填覆盖时不应再探测")):
            c2 = LLMClient(provider="ollama", model="zz-unit-custom:1b")
        self.assertEqual(c2.context_source, "custom")
        self.assertEqual(c2.capability["context_window"], 20000)

    def test_probe_disabled_by_env_falls_back_to_profile(self):
        import os as _os
        from unittest.mock import patch
        from llm import LLMClient
        with patch.dict(_os.environ, {"DOCMIND_OLLAMA_PROBE": "0"}, clear=False), \
                patch("llm.probe_ollama_context",
                      side_effect=AssertionError("env 关闭后不应探测")):
            c = LLMClient(provider="ollama", model="zz-unit-envoff:1b")
        self.assertEqual(c.context_source, "profile")
        self.assertEqual(c.capability["context_window"], 16384)


class ReasoningStreamTests(unittest.TestCase):
    def test_reasoning_content_goes_to_sink_not_body(self):
        from llm import StreamChat

        class _U:
            def model_dump(self):
                return {"prompt_tokens": 3, "completion_tokens": 2}

        class _Chunk:
            def __init__(self, choices, usage=None):
                self.choices = choices
                self.usage = usage

        class _ThinkChoice:
            finish_reason = None

            class delta:
                content = None
                reasoning_content = "先推理一下"

        class _SayChoice:
            finish_reason = "stop"

            class delta:
                content = "答案"
                reasoning_content = None

        sink = []
        sc = StreamChat(
            [_Chunk([_ThinkChoice()]), _Chunk([_SayChoice()]), _Chunk([], usage=_U())],
            reasoning_sink=sink)
        body = "".join(sc)
        self.assertEqual(body, "答案")
        self.assertEqual("".join(sink), "先推理一下")
        self.assertEqual(sc.reasoning_chars, len("先推理一下"))


class UsageCaptureTests(unittest.TestCase):
    def test_stream_chat_captures_openai_usage(self):
        from llm import StreamChat

        class _U:
            def model_dump(self):
                return {"prompt_tokens": 11, "completion_tokens": 5}

        class _Chunk:
            def __init__(self, choices, usage=None):
                self.choices = choices
                self.usage = usage

        class _Choice:
            finish_reason = "stop"

            class delta:
                content = "hi"
                reasoning_content = None

        sink = {}
        sc = StreamChat([_Chunk([_Choice()]), _Chunk([], usage=_U())], usage_sink=sink)
        out = "".join(sc)
        self.assertEqual(out, "hi")
        self.assertEqual(sink.get("prompt_tokens"), 11)
        self.assertEqual(sink.get("completion_tokens"), 5)


if __name__ == "__main__":
    unittest.main()
