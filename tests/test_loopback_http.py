"""`loopback_http` 的行为契约：**回环请求绝不受环境代理影响**。

这两条不是配套装饰，是 2026-10-12 那次全量测试里暴露出来的真缺陷的守门员：
`urllib.request.urlopen()` 默认读 `HTTP_PROXY`，沙箱里代理会替死掉的目标应答 502，
于是「连不上」被读成「服务端应答了」—— fail-closed 语义反转。
"""
import http.server
import threading
import unittest
from unittest.mock import patch

import urllib.error

from agent_runtime import loopback_http


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


class LoopbackHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.url = "http://127.0.0.1:%d/ps" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_env_proxy_does_not_capture_a_loopback_request(self):
        """环境里塞一个**死代理**，回环请求仍然必须直达。

        只做单边断言（不要求裸 `urllib` 一定失败）：机器上有没有别的代理配置
        不是这条测试该管的，硬要断言"裸 urllib 会失败"会随环境变红。
        """
        dead = {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1"}
        with patch.dict("os.environ", dead):
            with loopback_http.urlopen(self.url, timeout=5) as response:
                self.assertEqual(response.read(), b"OK")

    def test_a_dead_port_raises_urlerror_instead_of_a_proxy_502(self):
        """死端口必须是「连不上」（URLError），不能是「服务端应答了」。

        走代理时死端口会返回 `HTTPError 502`，是 `URLError` 的**子类**，
        所以这里必须显式排除 HTTPError —— 只断言 URLError 会让 502 蒙混过关。
        """
        with patch.dict("os.environ", {"http_proxy": "http://127.0.0.1:1",
                                       "HTTP_PROXY": "http://127.0.0.1:1"}):
            with self.assertRaises(urllib.error.URLError) as ctx:
                loopback_http.urlopen("http://127.0.0.1:1/nope", timeout=5)
        self.assertNotIsInstance(ctx.exception, urllib.error.HTTPError,
                                 "拿回了一个 HTTP 应答——请求多半是走了代理：%s" % ctx.exception)


if __name__ == "__main__":
    unittest.main()
