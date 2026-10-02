"""上游空响应不再伪装成 `TypeError`。

**实测的故障形态**（2026-10-02，`custom/stealth/space-bunny-alpha` 经 OpenRouter）：
连打三个问题，第二个返回 **HTTP 200**，但 body 是错误信封：

    {"choices": null, "model": null, "usage": null,
     "error": {"message": "Provider returned an empty response", "code": 502}}

原来的写法是 `resp.choices[0].message`，于是抛
`TypeError: 'NoneType' object is not subscriptable`；`api.py` 再按 `type(e).__name__`
把它显示成 `⚠ TypeError`。用户看到的是"服务内部类型错误"，而真实情况是
"上游这次没返回内容，重试通常就好了"——**两件完全不同的事**。

这个测试钉住四件事：
1. 不再抛 TypeError，改为 `EmptyProviderResponse`；
2. 错误消息里带**真实的**上游原因（不是一句无关的内部类型名）；
3. **不返回空字符串冒充答案**——空串会被 Agent 当成"模型什么都没说"，
   从而触发续写纠偏转 8 次，比直接报错难查得多；
4. 流式路径原本就有 `if not chunk.choices: continue` 的守卫，
   这里一并钉住它不被改坏。
"""

import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from llm import EmptyProviderResponse, _first_message, _response_error  # noqa: E402


def _resp(**fields):
    """造一个最小的 ChatCompletion 替身（只需要 choices / model_dump / usage）。"""
    obj = types.SimpleNamespace(**fields)
    if "model_dump" not in fields:
        def model_dump():
            return dict(fields)
        obj.model_dump = model_dump
    return obj


def _msg(content, tool_calls=None, finish_reason="stop"):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=content, tool_calls=tool_calls),
            finish_reason=finish_reason)])


EMPTY_ENVELOPE = _resp(
    id="gen-1", created=None, model=None, usage=None,
    choices=None,
    error={"message": "Provider returned an empty response", "code": 502,
           "metadata": {"error_type": "provider"}})


class ResponseErrorTests(unittest.TestCase):

    def test_it_reads_the_error_envelope(self):
        error = _response_error(EMPTY_ENVELOPE)
        self.assertEqual(error.get("message"), "Provider returned an empty response")
        self.assertEqual(error.get("code"), 502)

    def test_a_nested_envelope_is_unwrapped(self):
        """有的中转会再包一层 {"error": {"error": {...}}}。"""
        nested = _resp(choices=None,
                       error={"error": {"message": "upstream boom", "code": 500}})
        self.assertEqual(_response_error(nested).get("message"), "upstream boom")

    def test_no_envelope_yields_an_empty_dict(self):
        self.assertEqual(_response_error(_resp(choices=None)), {})


class FirstMessageTests(unittest.TestCase):

    def test_a_normal_response_passes_through(self):
        msg = _first_message(_msg("hello"))
        self.assertEqual(msg.content, "hello")

    def test_choices_none_raises_the_named_error(self):
        with self.assertRaises(EmptyProviderResponse) as caught:
            _first_message(EMPTY_ENVELOPE)
        text = str(caught.exception)
        self.assertIn("Provider returned an empty response", text)
        self.assertIn("502", text)

    def test_it_never_raises_a_bare_type_error(self):
        """这条是本测试存在的理由：那正是用户看到的 `⚠ TypeError`。"""
        try:
            _first_message(EMPTY_ENVELOPE)
        except EmptyProviderResponse:
            pass
        except TypeError as exc:  # pragma: no cover - 回归时才会走到
            self.fail("又退回 TypeError 了：%s" % exc)
        else:  # pragma: no cover
            self.fail("空响应必须抛异常，不能当成正常答案")

    def test_the_message_says_it_is_retryable(self):
        """"直接重试通常即可"是用户下一步唯一需要的信息。"""
        try:
            _first_message(EMPTY_ENVELOPE)
        except EmptyProviderResponse as exc:
            self.assertIn("重试", str(exc))
            self.assertIn("与代码无关", str(exc))

    def test_an_empty_choices_list_is_also_refused(self):
        with self.assertRaises(EmptyProviderResponse):
            _first_message(_resp(choices=[]))

    def test_a_null_message_is_refused_with_the_finish_reason(self):
        broken = _resp(choices=[types.SimpleNamespace(message=None,
                                                       finish_reason="content_filter")])
        with self.assertRaises(EmptyProviderResponse) as caught:
            _first_message(broken)
        self.assertIn("content_filter", str(caught.exception))

    def test_a_message_with_none_content_is_still_returned(self):
        """`content=None` 但 message 存在（例如纯 tool_calls）——那是合法的，
        不能在这里误判成空响应，否则会打断原生 function-calling 通道。"""
        msg = _first_message(_msg(None, tool_calls=[{"id": "1"}]))
        self.assertIsNone(msg.content)


class StreamPathTests(unittest.TestCase):
    """流式路径原本就有守卫；这里钉住它不被改坏。"""

    def test_the_stream_guard_is_still_there(self):
        import inspect
        from llm import StreamChat
        source = inspect.getsource(StreamChat.__iter__)
        self.assertIn("if not chunk.choices:", source)


class RetryTests(unittest.TestCase):
    """空响应**必须真的重试**，否则第 4 项修复只是换了个错误名字。

    这是最容易做假的一步：`is_retryable` 返回 True 还不够——
    `_first_message` 必须在 `retry_call` **内部**被调用，否则异常在重试块之外抛出，
    一次都不会重试（实测：本仓原始结构里 usage 提取与 content 取值都在 retry_call
    外面，于是上游空响应永远直达用户）。

    用 `custom` provider 而不是 `mock`：mock 在 `chat()` 开头就 return 了，
    根本走不到 OpenAI 兼容路径——那样测的就不是这段代码了。
    """

    def _client(self, create, *, retries=4):
        from llm import LLMClient
        client = LLMClient(provider="custom", model="test-model",
                           api_key="k", base_url="http://127.0.0.1:1/v1")
        client.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        client.max_retries = retries
        client.retry_base = 0  # 不让测试睡 8 秒
        return client

    def test_it_is_classified_as_retryable(self):
        from llm import is_retryable
        self.assertTrue(is_retryable(EmptyProviderResponse("没给内容")))

    def test_an_unrelated_error_is_still_not_retryable(self):
        from llm import is_retryable
        self.assertFalse(is_retryable(ValueError("参数错了")))

    def test_an_empty_response_is_actually_retried(self):
        """打 3 次：前两次空响应，第三次正常 —— 必须返回第三次的内容。"""
        attempts = {"n": 0}

        def fake_create(**kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return EMPTY_ENVELOPE
            return _msg("第三次才拿到内容")

        client = self._client(fake_create)
        result = client.chat([{"role": "user", "content": "hi"}], stream=False)
        self.assertEqual(result, "第三次才拿到内容")
        self.assertEqual(attempts["n"], 3, "应当正好重试到第三次成功")

    def test_it_stops_after_the_retry_budget_and_raises(self):
        """一直空响应时，用完预算后抛 EmptyProviderResponse（不是 TypeError）。"""
        attempts = {"n": 0}

        def fake_create(**kwargs):
            attempts["n"] += 1
            return EMPTY_ENVELOPE

        client = self._client(fake_create, retries=3)
        with self.assertRaises(EmptyProviderResponse) as caught:
            client.chat([{"role": "user", "content": "hi"}], stream=False)
        self.assertIn("Provider returned an empty response", str(caught.exception))
        self.assertEqual(attempts["n"], 3, "不应超过重试预算")

    def test_usage_is_still_recorded_after_a_retry(self):
        """重试成功后那一次的 usage 也要记账——否则 trace 里这次调用显示 0 token。"""
        attempts = {"n": 0}

        def fake_create(**kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return EMPTY_ENVELOPE
            good = _msg("有内容了")
            good.usage = types.SimpleNamespace(
                model_dump=lambda: {"prompt_tokens": 11, "completion_tokens": 5})
            return good

        client = self._client(fake_create, retries=3)
        self.assertEqual(client.chat([{"role": "user", "content": "hi"}], stream=False),
                         "有内容了")
        self.assertEqual(client.last_usage.get("prompt_tokens"), 11)
        self.assertEqual(client.last_usage.get("completion_tokens"), 5)

    def test_the_check_lives_inside_the_retry_wrapper(self):
        """源码级断言：空响应检查与 usage 提取必须在 `_complete` 闭包里、由
        `retry_call(_complete, ...)` 驱动——也就是被重试包住。

        这里刻意**不**去比字符位置：`def _complete()` 本来就该写在 `retry_call`
        调用之前（要先定义再传），比位置会误判。真正要钉的是「两者在同一个闭包里」。
        """
        import inspect
        from llm import LLMClient
        source = inspect.getsource(LLMClient.chat)
        self.assertIn("def _complete():", source)
        body = source[source.index("def _complete():"):source.index("retry_call(_complete")]
        self.assertIn("_first_message(resp)", body,
                      "_first_message 必须在 _complete 闭包内，否则重试包不住它")
        self.assertIn("_record_usage(resp)", body,
                      "usage 提取必须在闭包内，否则重试成功后这次调用会记成 0 token")
        self.assertIn("retry_call(_complete", source)


if __name__ == "__main__":
    unittest.main()
