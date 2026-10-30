# -*- coding: utf-8 -*-
"""搜索缓存命名空间必须带上 API 地址指纹。

只按 provider 分桶不够：SearXNG/自建兼容端点换了地址，旧缓存仍会命中，
用户会以为新实例没生效。URL 可能含凭据，只能进哈希，不能明文进缓存键。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402


class WebSearchCacheKeyTests(unittest.TestCase):
    def test_api_url_changes_namespace(self):
        with patch.object(tools, "get_web_search_api_url",
                          return_value="https://a.example/search"):
            first = tools._web_search_cache_prefix("searxng", True)
        with patch.object(tools, "get_web_search_api_url",
                          return_value="https://b.example/search"):
            second = tools._web_search_cache_prefix("searxng", True)
        self.assertNotEqual(first, second, "换搜索实例后仍复用旧缓存")
        self.assertIn("search:v8:", first)
        self.assertNotIn("a.example", first, "URL 可能含凭据，不能明文进缓存键")

    def test_provider_still_separates_cache(self):
        with patch.object(tools, "get_web_search_api_url", return_value=""):
            self.assertNotEqual(
                tools._web_search_cache_prefix("searxng", True),
                tools._web_search_cache_prefix("bocha", True))


if __name__ == "__main__":
    unittest.main()
