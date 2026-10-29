# -*- coding: utf-8 -*-
"""OpenCode Go 接入契约：Go 端点、默认模型、网关强制的会话头、能力画像。

2026-10-29 实测：该网关只认 Bearer Key 时 chat/completions 直接 400
{"type":"MissingSessionID"}，必须带 x-opencode-session；且文档要求客户端
自带 User-Agent。这里锁住「provider 注册」「请求头真的交给 SDK」与「能力画像」
三层，避免以后有人只加 provider、忘了头，把 400 伪装成网络故障。

能力画像同日真跑（secrets store 里的 go key）：deepseek-v4.1-flash 默认返回
reasoning_content、thinking {enabled|disabled} 生效、标准 image_url 收图成；
reasoning_effort 虽被接受但三档无可观测差异 → 只标二元开关，不发 effort。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import llm  # noqa: E402


class OpenCodeProviderTests(unittest.TestCase):
    def test_provider_registered_with_go_endpoint(self):
        meta = config.PROVIDERS.get("opencode")
        self.assertIsNotNone(meta, "opencode provider 未注册")
        self.assertEqual(meta["base_url"], "https://opencode.ai/inference/go/openai/v1")
        self.assertEqual(meta["default_model"], "deepseek-v4.1-flash")
        self.assertTrue(meta["api_key_env"])
        self.assertTrue(meta["cloud"])

    def test_headers_carry_session_and_client_user_agent(self):
        headers = llm.provider_default_headers("opencode")
        self.assertTrue(headers.get("x-opencode-session", "").startswith("docmind-"))
        self.assertTrue(headers.get("User-Agent", "").startswith("DocMind-"))

    def test_other_providers_keep_sdk_defaults(self):
        self.assertEqual(llm.provider_default_headers("deepseek"), {})
        self.assertEqual(llm.provider_default_headers(""), {})

    def test_capability_profile_matches_live_probe(self):
        """2026-10-29 实测：默认返 reasoning_content、thinking 开关生效、收图成。

        effort 参数被网关接受，但低/高/最大档对 reasoning 长度没有可观测差异，
        所以画像只到二元开关 —— 绝不发未验证的 reasoning_effort。
        """
        cap = config.model_capability("opencode", "deepseek-v4.1-flash")
        self.assertEqual(cap["thinking"], "toggle")
        self.assertEqual(cap["thinking_effort"], "binary")
        self.assertEqual(cap["thinking_levels"], ["off", "medium"])
        self.assertEqual(cap["vision"], "native")

    def test_llm_client_passes_headers_to_sdk(self):
        """锁住「LLMClient 把厂商头交给 SDK」这一层。

        不读 openai 的私有 `_custom_headers`：那是版本相关的内部结构，而
        requirements 没有钉 openai 版本 —— 换一版 SDK 这条测试就会碎。
        """
        captured = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch.object(llm, "OpenAI", FakeOpenAI):
            llm.LLMClient(provider="opencode", model="deepseek-v4.1-flash",
                          api_key="oc_sk_test", web_fn=lambda _q: "")
        headers = {k.lower(): v for k, v in (captured.get("default_headers") or {}).items()}
        self.assertTrue(headers.get("x-opencode-session", "").startswith("docmind-"))
        self.assertTrue(headers.get("user-agent", "").startswith("DocMind-"))


if __name__ == "__main__":
    unittest.main()
