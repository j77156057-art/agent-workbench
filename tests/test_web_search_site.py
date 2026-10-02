# -*- coding: utf-8 -*-
"""web_search 站点限定 + 百度后端回归：

- _parse_web_search_arg：纯关键词 / site: / platform:(别名映射) / 行内 site / 空；
- web_search：site/platform 必须拼成 `site:` 透传给后端；
- 百度后端：auto 下作为 ddg 失败后的兜底层、可强制指定、HTML 解析正确。
"""
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools


class ParseWebSearchArgTests(unittest.TestCase):
    def test_pure_query(self):
        q, site = tools._parse_web_search_arg("某关键词")
        self.assertEqual(q, "某关键词")
        self.assertIsNone(site)

    def test_multiline_site(self):
        q, site = tools._parse_web_search_arg("query: 某关键词\nsite: github.com")
        self.assertEqual(q, "某关键词")
        self.assertEqual(site, "github.com")

    def test_platform_alias_maps_to_domain(self):
        q, site = tools._parse_web_search_arg("platform: github\n某关键词")
        self.assertEqual(site, "github.com")
        self.assertEqual(q, "某关键词")

    def test_platform_chinese_alias(self):
        q, site = tools._parse_web_search_arg("platform: b站\n如何做特效")
        self.assertEqual(site, "bilibili.com")

    def test_inline_site_within_query(self):
        q, site = tools._parse_web_search_arg("foo site: github.com")
        self.assertEqual(q, "foo")
        self.assertEqual(site, "github.com")

    def test_empty(self):
        q, site = tools._parse_web_search_arg("")
        self.assertEqual(q, "")
        self.assertIsNone(site)


class WebSearchSiteAppendTests(unittest.TestCase):
    """site/platform 必须拼成 site: 透传给后端。"""

    def _auto(self):
        return {**os.environ, "WEB_SEARCH_BACKEND": "auto", "DOCMIND_WEB_CACHE": "0"}

    def test_site_passed_to_backend(self):
        with patch.dict(tools.os.environ, self._auto(), clear=True), \
                patch("tools._github_search", return_value="GitHub API 暂不可用（test）"), \
                patch("tools._ddg_search", return_value="· 结果") as m:
            tools.web_search("query: 某关键词\nsite: github.com")
        sent = m.call_args[0][0]
        self.assertIn("site:github.com", sent)
        self.assertIn("某关键词", sent)

    def test_platform_passed_as_site(self):
        with patch.dict(tools.os.environ, self._auto(), clear=True), \
                patch("tools._github_search", return_value="GitHub API 暂不可用（test）"), \
                patch("tools._ddg_search", return_value="· 结果") as m:
            tools.web_search("platform: github\n某关键词")
        sent = m.call_args[0][0]
        self.assertIn("site:github.com", sent)


class BaiduBackendTests(unittest.TestCase):
    """auto 下百度作为 ddg 失败后的兜底层之一。"""

    def _auto(self):
        return {**os.environ, "WEB_SEARCH_BACKEND": "auto", "DOCMIND_WEB_CACHE": "0"}

    def test_baidu_used_when_ddg_fails(self):
        with patch.dict(tools.os.environ, self._auto(), clear=True), \
                patch("tools._ddg_search", side_effect=OSError("blocked")), \
                patch("tools._baidu_search", return_value="· 某关键词百度结果") as m:
            out = tools.web_search("某关键词")
        self.assertIn("· 某关键词百度结果", out)
        m.assert_called_once()

    def test_forced_baidu_backend(self):
        # 指定单后端：经 config.get_web_search_provider() 读取，不走 auto 故障转移
        with patch("tools.get_web_search_provider", return_value="baidu"), \
                patch("tools._baidu_search", return_value="· 百度关键词\n  百度关键词匹配结果") as m, \
                patch("tools._ddg_search", return_value="x") as ddg:
            out = tools.web_search("百度关键词")
        self.assertIn("· 百度关键词", out)
        ddg.assert_not_called()

    def test_baidu_parse_html(self):
        html = (
            '<div class="result c-container">'
            '<h3 class="t"><a href="https://example.com/a">示例标题</a></h3>'
            '<div class="c-abstract">示例摘要内容</div>'
            '</div>'
        )
        fake = io.BytesIO(html.encode("utf-8"))
        with patch("urllib.request.urlopen", return_value=fake), \
                patch("urllib.request.Request", side_effect=lambda url, **kw: url):
            out = tools._baidu_search("某关键词")
        self.assertIn("示例标题", out)
        self.assertIn("示例摘要内容", out)
        self.assertIn("https://example.com/a", out)


def _fake_json_response(payload):
    """构造一个带 .read() 的假 HTTP 响应对象（urlopen 返回值）。"""
    class _Resp:
        def __init__(self, data):
            self._b = json.dumps(data).encode("utf-8")
        def read(self, *a, **k):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    return _Resp(payload)


class ApiProviderDispatchTests(unittest.TestCase):
    """API 类搜索/抓取服务商：缺失 Key/地址时给出清晰提示；mock urlopen 验证解析。"""

    def test_exa_requires_key(self):
        with patch("tools.get_web_search_provider", return_value="exa"), \
                patch("tools.get_web_search_api_key", return_value=""):
            self.assertIn("API Key", tools.web_search("某关键词"))

    def test_tavily_parse(self):
        resp = _fake_json_response({"results": [
            {"title": "T 标题", "url": "https://x.com/1", "content": "T 摘要内容"},
        ]})
        with patch("tools.get_web_search_provider", return_value="tavily"), \
                patch("tools.get_web_search_api_key", return_value="k"), \
                patch("urllib.request.urlopen", return_value=resp), \
                patch("urllib.request.Request", side_effect=lambda url, **kw: url):
            out = tools.web_search("T 标题")
        self.assertIn("T 标题", out)
        self.assertIn("https://x.com/1", out)
        self.assertIn("T 摘要内容", out)

    def test_searxng_requires_url(self):
        with patch("tools.get_web_search_provider", return_value="searxng"), \
                patch("tools.get_web_search_api_url", return_value=""):
            self.assertIn("API 地址", tools.web_search("某关键词"))

    def test_generic_requires_url(self):
        # zhipu/querit/parallel/mcp_exa 无内置端点，必须填自定义 API 地址
        with patch("tools.get_web_search_provider", return_value="zhipu"), \
                patch("tools.get_web_search_api_url", return_value=""):
            self.assertIn("API 地址", tools.web_search("某关键词"))

    def test_jina_fetch_requires_no_key_builtin_host(self):
        resp = _fake_json_response({})  # 用非 json 正文模拟 jina markdown 返回
        resp._b = "# 标题\n\n正文内容".encode("utf-8")
        with patch("tools.get_web_fetch_provider", return_value="jina"), \
                patch("tools.get_web_fetch_api_key", return_value=""), \
                patch("tools.get_web_fetch_api_url", return_value=""), \
                patch("urllib.request.urlopen", return_value=resp), \
                patch("urllib.request.Request", side_effect=lambda url, **kw: url):
            out = tools.web_fetch("https://example.com/p")
        self.assertIn("正文内容", out)


if __name__ == "__main__":
    unittest.main()
