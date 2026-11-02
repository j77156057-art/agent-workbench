# -*- coding: utf-8 -*-
"""T2：web_fetch / web_research 网页图片观察通道（本地 HTTP 夹具，不触外网）。"""
import io
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools as tools_mod  # noqa: E402
from agent_runtime.tools import ToolResult  # noqa: E402


def _png(color, size=(120, 80)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


_PAGE = """<html><head>
<meta property="og:image" content="/og.png">
<title>Godot 报错排查</title></head><body>
<img src="/assets/icon-sprite.png" alt="menu icon" width="16" height="16">
<img src="/img/godot-error-screenshot.png" alt="Godot fatal error 报错弹窗截图">
<img src="/ads/side-advert.png" alt="advertisement">
</body></html>"""

_RESEARCH_1 = """<html><head><title>R1</title></head><body>
<img src="/r/a1.png" alt="x"><img src="/r/a2.png" alt="x">
<img src="/shared.png" alt="shared"></body></html>"""

_RESEARCH_2 = """<html><head><title>R2</title></head><body>
<img src="/r/b1.png" alt="x"><img src="/shared.png" alt="shared">
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    pages = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/redir-ok.png":
            self.send_response(302)
            self.send_header("Location", "/og.png")
            self.end_headers()
            return
        if path == "/redir-internal.png":
            # 公网可解析入口 302 到一个解析必然失败的「内网」标记主机。
            self.send_response(302)
            self.send_header("Location",
                             "http://ssrf-target.invalid/og.png")
            self.end_headers()
            return
        if path == "/redir-file.png":
            self.send_response(302)
            self.send_header("Location", "file:///C:/Windows/win.ini")
            self.end_headers()
            return
        if path == "/redir-ftp.png":
            self.send_response(302)
            self.send_header("Location", "ftp://example.com/pic.png")
            self.end_headers()
            return
        if path == "/slow.png":
            import time
            time.sleep(1.0)
            body, ctype = _png((0, 0, 255)), "image/png"
        elif path == "/big.png":
            body, ctype = os.urandom(900 * 1024), "image/png"
        elif path in self.pages:
            ctype, body = self.pages[path]
        else:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, OSError):
            # 超时/超限用例由客户端主动断连，夹具无需报错。
            pass


class WebImageFixture(unittest.TestCase):
    server = None
    thread = None
    base = ""

    @classmethod
    def setUpClass(cls):
        _Handler.pages = {
            "/page.html": ("text/html; charset=utf-8", _PAGE.encode("utf-8")),
            "/og.png": ("image/png", _png((255, 0, 0))),
            "/img/godot-error-screenshot.png": ("image/png", _png((0, 200, 0))),
            "/assets/icon-sprite.png": ("image/png", _png((0, 0, 0), (16, 16))),
            "/ads/side-advert.png": ("image/png", _png((200, 200, 0))),
            "/notimage.png": ("text/plain; charset=utf-8", b"this is not an image"),
            "/r1.html": ("text/html; charset=utf-8", _RESEARCH_1.encode("utf-8")),
            "/r2.html": ("text/html; charset=utf-8", _RESEARCH_2.encode("utf-8")),
            "/r/a1.png": ("image/png", _png((10, 10, 10))),
            "/r/a2.png": ("image/png", _png((20, 20, 20))),
            "/r/b1.png": ("image/png", _png((30, 30, 30))),
            "/shared.png": ("image/png", _png((40, 40, 40), (200, 200))),
        }
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _allow_loopback(self):
        return patch.object(tools_mod, "_image_host_blocked", lambda host: False)


class ExtractCandidatesTests(WebImageFixture):
    def test_filters_icon_ad_and_prefers_og_then_relevant(self):
        urls = tools_mod._extract_image_candidates(
            _PAGE, self.base + "/page.html",
            query_hint="godot 报错 截图", limit=2)
        self.assertEqual(len(urls), 2)
        # og 头图与高重合的正文截图入选；图标/广告被过滤。查询词高度命中时
        # 允许正文图按相关性排到 og 之前（相关性优先，og 同分时优先）。
        tails = sorted(u.rsplit("/", 1)[-1] for u in urls)
        self.assertEqual(tails, ["godot-error-screenshot.png", "og.png"])
        joined = "\n".join(urls).lower()
        self.assertNotIn("icon", joined)
        self.assertNotIn("advert", joined)

    def test_og_wins_when_query_terms_neutral(self):
        urls = tools_mod._extract_image_candidates(
            _PAGE, self.base + "/page.html",
            query_hint="官网首页", limit=2)
        self.assertTrue(urls[0].endswith("/og.png"), urls)

    def test_dedupes_and_ignores_data_uri(self):
        html = ('<img src="data:image/png;base64,AAAA">'
                '<img src="/r/a1.png"><img src="/r/a1.png">')
        urls = tools_mod._extract_image_candidates(
            html, self.base + "/page.html", limit=5)
        self.assertEqual(urls, [self.base + "/r/a1.png"])


class FetchImageTests(WebImageFixture):
    def test_fetch_returns_jpeg_data_urls_with_sources(self):
        from PIL import Image
        with self._allow_loopback(), \
                patch.object(tools_mod, "_web_images_enabled", lambda: True):
            result = tools_mod._fetch_page(
                self.base + "/page.html", image_budget=2,
                query_hint="godot 报错 截图")
        self.assertIsInstance(result, ToolResult)
        self.assertEqual(len(result.data["images"]), 2)
        self.assertEqual(len(result.data["image_sources"]), 2)
        for img, src in zip(result.data["images"], result.data["image_sources"]):
            self.assertTrue(img.startswith("data:image/jpeg;base64,"))
            self.assertTrue(src.startswith("http://127.0.0.1:"))
            raw = img.split(",", 1)[1]
            import base64
            decoded = Image.open(io.BytesIO(base64.b64decode(raw)))
            self.assertLessEqual(max(decoded.size), 1600)
        self.assertIn("Godot 报错排查", result.text)

    def test_disabled_or_no_vision_returns_plain_text(self):
        with patch.dict(os.environ, {"DOCMIND_WEB_IMAGES": "0"}):
            result = tools_mod._fetch_page(
                self.base + "/page.html", image_budget=2)
        self.assertIsInstance(result, str)

        env = {"DOCMIND_WEB_IMAGES": "1"}
        env.pop("DOCMIND_VISION_MODEL", None)
        with patch.dict(os.environ, env, clear=False), \
                patch("config.model_capability",
                      return_value={"vision": "none"}):
            os.environ.pop("DOCMIND_VISION_MODEL", None)
            result = tools_mod._fetch_page(
                self.base + "/page.html", image_budget=2)
        self.assertIsInstance(result, str)


class DownloadGuardTests(WebImageFixture):
    def test_internal_address_rejected(self):
        # 不打 allow_loopback 补丁：回环地址必须被 SSRF 守卫直接拒绝。
        self.assertIsNone(
            tools_mod._download_observation_image(self.base + "/og.png"))
        self.assertIsNone(
            tools_mod._download_observation_image(
                "http://169.254.169.254/latest/meta-data/x.png"))

    def test_non_image_content_type_rejected(self):
        with self._allow_loopback():
            self.assertIsNone(
                tools_mod._download_observation_image(
                    self.base + "/notimage.png"))

    def test_oversized_rejected(self):
        with self._allow_loopback():
            self.assertIsNone(
                tools_mod._download_observation_image(self.base + "/big.png"))

    def test_timeout_rejected(self):
        with self._allow_loopback(), \
                patch.object(tools_mod, "_WEB_IMAGE_TIMEOUT", 0.3):
            self.assertIsNone(
                tools_mod._download_observation_image(
                    self.base + "/slow.png"))

    def test_same_host_redirect_still_downloads(self):
        with self._allow_loopback():
            data_url = tools_mod._download_observation_image(
                self.base + "/redir-ok.png")
        self.assertIsNotNone(data_url, "同主机良性 302 应正常跟随并下载")
        self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))

    def test_redirect_to_unresolvable_internal_host_rejected(self):
        # 放行回环入口，但重定向目标必须被守卫明确判定为 blocked：
        # 记录守卫调用，断言对重定向主机给出过拦截决策（而非仅靠 DNS 失败
        # 误绿），且 redirect_request 返回 None 不发起二次连接。
        real_guard = tools_mod._image_host_blocked
        decisions = []

        def _guard(host):
            blocked = False if host == "127.0.0.1" else real_guard(host)
            decisions.append((host, blocked))
            return blocked

        with patch.object(tools_mod, "_image_host_blocked", _guard):
            self.assertIsNone(
                tools_mod._download_observation_image(
                    self.base + "/redir-internal.png"))
        self.assertIn(("ssrf-target.invalid", True), decisions,
                      "重定向目标必须被 SSRF 守卫显式拦截")

    def test_redirect_to_file_scheme_rejected(self):
        with self._allow_loopback():
            self.assertIsNone(
                tools_mod._download_observation_image(
                    self.base + "/redir-file.png"))

    def test_redirect_to_ftp_scheme_rejected(self):
        # ftp 重定向：opener 里没有 FTP handler，且逐跳 scheme 白名单先拒。
        with self._allow_loopback():
            self.assertIsNone(
                tools_mod._download_observation_image(
                    self.base + "/redir-ftp.png"))

    def test_image_opener_has_no_local_or_ftp_handlers(self):
        # 纵深防御：图片 opener 链里不得出现 File/FTP/Data handler。
        handler_names = [type(h).__name__
                         for h in tools_mod._IMAGE_OPENER.handlers]
        for forbidden in ("FileHandler", "FTPHandler", "DataHandler"):
            self.assertNotIn(forbidden, handler_names)

    def test_broken_image_does_not_break_page_text(self):
        html = ('<html><head></head><body><img src="/notimage.png">'
                '正文仍然可读</body></html>')
        with self._allow_loopback(), \
                patch.object(tools_mod, "_web_images_enabled", lambda: True):
            self.server  # noqa: B018 — 夹具确保服务在跑
            result = tools_mod._collect_observation_images(
                html, self.base + "/page.html", "x", 2)
        self.assertEqual(result, ([], []))


class SessionVisionGateTests(unittest.TestCase):
    """m-5：云端按请求覆盖模型时，web 抓图门按本轮会话能力画像决策。"""

    def test_context_vision_mode_overrides_global_profile(self):
        env = {"DOCMIND_WEB_IMAGES": "1"}
        env.pop("DOCMIND_VISION_MODEL", None)
        with patch.dict(os.environ, env, clear=False):
            native_token = tools_mod.set_session_vision_mode("native")
            try:
                self.assertTrue(tools_mod._web_images_enabled())
            finally:
                tools_mod._session_vision_mode.reset(native_token)
            none_token = tools_mod.set_session_vision_mode("none")
            try:
                self.assertFalse(tools_mod._web_images_enabled())
            finally:
                tools_mod._session_vision_mode.reset(none_token)

    def test_kill_switch_still_wins(self):
        with patch.dict(os.environ, {"DOCMIND_WEB_IMAGES": "0"}):
            token = tools_mod.set_session_vision_mode("native")
            try:
                self.assertFalse(tools_mod._web_images_enabled())
            finally:
                tools_mod._session_vision_mode.reset(token)


class ResearchAggregationTests(WebImageFixture):
    def test_research_total_four_and_deduped(self):
        search_text = f"one\n{self.base}/r1.html\ntwo\n{self.base}/r2.html"
        with self._allow_loopback(), \
                patch.object(tools_mod, "_web_images_enabled", lambda: True), \
                patch.object(tools_mod, "get_web_fetch_provider",
                             lambda: "builtin"), \
                patch.object(tools_mod, "web_search", lambda q: search_text):
            result = tools_mod.web_research("电路 DRC 检查")
        self.assertIsInstance(result, ToolResult)
        self.assertLessEqual(len(result.data["images"]), 4)
        self.assertEqual(len(result.data["images"]), 4)
        sources = result.data["image_sources"]
        self.assertEqual(len(sources), len(set(sources)), "跨源重复 URL 必须去重")
        self.assertTrue(any(s.endswith("/shared.png") for s in sources), sources)
        self.assertTrue(any(s.endswith("/r/b1.png") for s in sources), sources)

    def test_non_builtin_provider_has_no_images(self):
        search_text = f"one\n{self.base}/r1.html"
        with patch.object(tools_mod, "_web_images_enabled", lambda: True), \
                patch.object(tools_mod, "get_web_fetch_provider",
                             lambda: "jina"), \
                patch.object(tools_mod, "web_search", lambda q: search_text), \
                patch.object(tools_mod, "web_fetch",
                             lambda url: f"来源：{url}\n正文：纯文本"):
            result = tools_mod.web_research("任意查询")
        self.assertIsInstance(result, str)
        self.assertNotIn("data:image", result)


if __name__ == "__main__":
    unittest.main()
