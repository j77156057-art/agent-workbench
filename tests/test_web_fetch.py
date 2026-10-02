import base64
import io
import os
import unittest
from unittest.mock import patch
import tools

class WebFetchTests(unittest.TestCase):
    def test_rejects_non_http(self):
        self.assertIn('只允许', tools.web_fetch('file:///secret.txt'))

    def test_extracts_html_metadata_and_text(self):
        resp = io.BytesIO(b'<html><head><title>Guide</title></head><body><script>x</script><p>Hello world</p></body></html>')
        resp.headers = {'Content-Type': 'text/html; charset=utf-8'}
        resp.geturl = lambda: 'https://example.test/guide'
        with patch('tools.urllib.request.urlopen', return_value=resp):
            out = tools.web_fetch('https://example.test/guide')
        self.assertIn('标题：Guide', out); self.assertIn('Hello world', out); self.assertNotIn('script', out)

    def test_prefers_article_text_and_decodes_entities(self):
        html = (b'<html><head><title>Guide &amp; reference</title></head><body>'
                b'<nav>Home Search Account</nav><div class="advert">Buy now</div>'
                b'<main><article><h1>Guide</h1><p>One &amp; two are useful.</p>'
                b'<p>' + (b'Article content with details. ' * 8) + b'</p></article></main>'
                b'<footer>Privacy Terms</footer></body></html>')
        resp = io.BytesIO(html)
        resp.headers = {'Content-Type': 'text/html; charset=utf-8'}
        resp.geturl = lambda: 'https://example.test/guide'
        with patch('tools.urllib.request.urlopen', return_value=resp):
            out = tools.web_fetch('https://example.test/guide')
        self.assertIn('标题：Guide & reference', out)
        self.assertIn('One & two are useful.', out)
        self.assertNotIn('Home Search Account', out)
        self.assertNotIn('Buy now', out)
        self.assertNotIn('Privacy Terms', out)

    def test_non_html_is_reported(self):
        resp = io.BytesIO(b'PNGDATA'); resp.headers = {'Content-Type': 'image/png'}; resp.geturl=lambda: 'https://x.test/a.png'
        with patch('tools.urllib.request.urlopen', return_value=resp):
            self.assertIn('仅支持 HTML', tools.web_fetch('https://x.test/a.png'))

    def test_network_failure_is_explicit(self):
        with patch('tools.urllib.request.urlopen', side_effect=TimeoutError('late')):
            self.assertIn('网页读取失败', tools.web_fetch('https://example.test'))


class WebSearchFailoverTests(unittest.TestCase):
    """auto 模式：DuckDuckGo 被风控/无结果时必须自动转 Bing（实测 DDG 常返 202 挑战页）。
    第 2 部分起 web_search 改用 get_web_search_provider() 配置驱动（不再读 WEB_SEARCH_BACKEND）。"""

    @staticmethod
    def _env_no_recency():
        # 关闭近因排序，避免查询被追加 after:<year> 干扰精确入参断言
        return {**os.environ, "WEB_SEARCH_PREFER_RECENT": "0"}

    def test_ddg_empty_falls_back_to_bing(self):
        bing_result = "· 今天新闻真实标题\n  新闻摘要\n  https://example.test/real"
        with patch.dict(tools.os.environ, self._env_no_recency(), clear=True), \
                patch("tools.get_web_search_provider", return_value="builtin_auto"), \
                patch("tools._ddg_search", return_value="搜索未返回结果，可能是网络受限或该关键词无结果。"), \
                patch("tools._baidu_search", return_value="搜索未返回结果，可能是网络受限或该关键词无结果。"), \
                patch("tools._bing_search", return_value=bing_result) as mb:
            out = tools.web_search("今天新闻")
        self.assertEqual(out, bing_result)
        mb.assert_called_once_with(tools._search_recency_query("今天新闻").split(" after:")[0])

    def test_ddg_success_can_merge_parallel_backends(self):
        ddg_result = "· DDG 标题\n  摘要\n  https://ddg.test/x"
        with patch.dict(tools.os.environ, self._env_no_recency(), clear=True), \
                patch("tools.get_web_search_provider", return_value="builtin_auto"), \
                patch("tools._ddg_search", return_value=ddg_result), \
                patch("tools._bing_search") as mb:
            self.assertEqual(tools.web_search("q"), ddg_result)
        mb.assert_called_once_with("q")

    def test_ddg_exception_falls_back_to_bing(self):
        with patch.dict(tools.os.environ, self._env_no_recency(), clear=True), \
                patch("tools.get_web_search_provider", return_value="builtin_auto"), \
                patch("tools._ddg_search", side_effect=TimeoutError("202 challenge")), \
                patch("tools._baidu_search", return_value="搜索未返回结果，可能是网络受限或该关键词无结果。"), \
                patch("tools._bing_search", return_value="· B\n  s\n  https://b.test"):
            out = tools.web_search("q")
        self.assertIn("https://b.test", out)

    def test_forced_ddg_does_not_fail_over(self):
        with patch.dict(tools.os.environ, self._env_no_recency(), clear=True), \
                patch("tools.get_web_search_provider", return_value="ddg"), \
                patch("tools._ddg_search", return_value="搜索未返回结果，可能是网络受限或该关键词无结果。"), \
                patch("tools._bing_search") as mb:
            out = tools.web_search("q")
        self.assertIn("搜索未返回结果", out)
        mb.assert_not_called()

    def test_empty_query_short_circuits(self):
        self.assertIn("未提供搜索关键词", tools.web_search("  "))


class BingRealUrlTests(unittest.TestCase):
    def test_decodes_ck_a_redirect(self):
        target = "https://example.com/page?x=1&y=2"
        enc = base64.urlsafe_b64encode(target.encode("utf-8")).decode("ascii").rstrip("=")
        link = f"https://www.bing.com/ck/a?!&&p=abc&u=a1{enc}&ntb=1"
        self.assertEqual(tools._bing_real_url(link), target)

    def test_plain_link_passes_through(self):
        self.assertEqual(tools._bing_real_url("https://example.com/a"), "https://example.com/a")
        self.assertEqual(tools._bing_real_url(""), "")

    def test_malformed_ck_a_returns_original(self):
        link = "https://www.bing.com/ck/a?!&&p=abc&u=notbase64!!"
        # 正则不匹配时原样返回，不能抛异常
        self.assertEqual(tools._bing_real_url(link), link)


if __name__ == '__main__': unittest.main()
