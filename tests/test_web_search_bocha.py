# -*- coding: utf-8 -*-
"""博查搜索端点的回归守卫。

2026-10-29 实测：`api.bochaai.com/openapi/v1/web-search` 已返回 404
（{"status":404,"error":"Not Found"}），而 `api.bocha.cn/v1/web-search`
鉴权与 quota 判断正常（403 带 JSON 错误体）。端点写错时用户只会看到
「搜索未返回结果」，无从判断是 key 问题还是路径问题。
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps({
            "data": {"webPages": {"value": [
                {"name": "Godot 文档", "snippet": "CharacterBody3D", "url": "https://docs.godotengine.org/"},
            ]}}
        }).encode("utf-8")


class BochaSearchEndpointTests(unittest.TestCase):
    def test_uses_the_current_documented_endpoint(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            return _FakeResponse()

        with mock.patch.object(tools.urllib.request, "urlopen", fake_urlopen):
            out = tools._bocha_search("Godot 4", "sk-test")
        self.assertEqual(captured["url"], "https://api.bocha.cn/v1/web-search")
        self.assertEqual(captured["auth"], "Bearer sk-test")
        self.assertIn("· Godot 文档", out)
        self.assertIn("https://docs.godotengine.org/", out)

    def test_missing_key_stays_a_clear_message(self):
        out = tools._bocha_search("q", "")
        self.assertIn("API Key", out)

    def test_http_error_carries_the_server_message(self):
        """403 没额度时必须说清原因，而不是只剩 HTTP Error 403: Forbidden。"""
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {},
                __import__("io").BytesIO(json.dumps({
                    "code": "403",
                    "message": "You do not have enough money or package quota",
                }).encode("utf-8")))

        with mock.patch.object(tools.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as caught:
                tools._bocha_search("q", "sk-test")
        self.assertIn("quota", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
