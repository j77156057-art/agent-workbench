"""Wave6 回归：本机 dev server 生命周期 + 预览指向已运行的服务。

这里最怕【谎称就绪】，所以所有断言都以实际连通为准：真起一个 `python -m http.server`
子进程、真探测、真回收；也真验证进程秒退、端口没人听、越界 cwd 这些失败路径不会被
报成成功。回环限制是安全边界，同样有用例钉住（不能把它当网页浏览器去访问别的机器）。
"""

import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import tools  # noqa: E402
from agent_runtime import dev_server, visual_acceptance  # noqa: E402


class _MiniServer(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 别把测试输出刷屏
        pass


class _Case(unittest.TestCase):
    def setUp(self):
        self._prev = config.get_runtime("code_root")
        self.tmp = tempfile.mkdtemp(prefix="docmind_serve_")
        config.set_runtime("code_root", self.tmp)

    def tearDown(self):
        config.set_runtime("code_root", self._prev)
        shutil.rmtree(self.tmp, ignore_errors=True)


class UrlCases(unittest.TestCase):
    def test_accepts_loopback_forms(self):
        for raw, want in (("http://127.0.0.1:5173/", "http://127.0.0.1:5173/"),
                          ("localhost:3000/app", "http://localhost:3000/app"),
                          ("127.0.0.1:8000", "http://127.0.0.1:8000/"),
                          ("http://localhost:5173/?x=1", "http://localhost:5173/?x=1")):
            url, err = dev_server.loopback_url(raw)
            self.assertEqual(err, "", raw)
            self.assertEqual(url, want, raw)

    def test_refuses_anything_that_is_not_local(self):
        for raw in ("http://example.com/", "https://10.0.0.5:3000/",
                    "http://192.168.1.7/", "file:///etc/passwd", "ftp://127.0.0.1/x"):
            url, err = dev_server.loopback_url(raw)
            self.assertEqual(url, "", raw)
            self.assertTrue(err, raw)

    def test_missing_port_needs_a_default(self):
        self.assertTrue(dev_server.loopback_url("http://localhost")[1])
        self.assertEqual(dev_server.loopback_url("http://localhost", default_port=5173)[0],
                         "http://localhost:5173/")

    def test_empty_url_reports(self):
        self.assertTrue(dev_server.loopback_url("  ")[1])

    def test_port_hint_from_service_logs(self):
        for line, want in (("  ➜  Local:   http://localhost:5173/", 5173),
                           ("Uvicorn running on http://127.0.0.1:8000", 8000),
                           ("listening on [::1]:4200", 4200),
                           ("no port here at all", 0)):
            self.assertEqual(dev_server.parse_port_hint(line), want, line)


class PortCases(_Case):
    def test_free_port_is_bindable(self):
        port = dev_server.free_port()
        self.assertTrue(1024 <= port <= 65535, port)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))

    def test_probe_reports_a_closed_port(self):
        port = dev_server.free_port()
        ready, reason = dev_server.probe("http://127.0.0.1:%d/" % port)
        self.assertFalse(ready)
        self.assertIn("还没有在听", reason)

    def test_probe_is_honest_about_non_loopback(self):
        ready, reason = dev_server.probe("http://example.com/")
        self.assertFalse(ready)
        self.assertIn("回环", reason)


class ProbeAgainstRealServer(_Case):
    def setUp(self):
        super().setUp()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MiniServer)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    def test_probe_ready_on_a_running_server(self):
        ready, reason = dev_server.probe("http://127.0.0.1:%d/" % self.port)
        self.assertTrue(ready, reason)
        self.assertIn("HTTP", reason)

    def test_404_still_counts_as_ready(self):
        # 服务已经能应答就是“起来了”，不能因为 404 就判成没就绪
        ready, reason = dev_server.probe("http://127.0.0.1:%d/nope.html" % self.port)
        self.assertTrue(ready, reason)
        self.assertIn("404", reason)


class StartStopCases(_Case):
    def _http_server_argv(self, port):
        return [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]

    def test_start_a_real_server_and_stop_it(self):
        port = dev_server.free_port()
        with open(os.path.join(self.tmp, "index.html"), "w", encoding="utf-8") as fh:
            fh.write("<h1>hi</h1>\n")
        res = dev_server.start(self.tmp, "python -m http.server",
                               argv=self._http_server_argv(port), port=port, wait=25)
        try:
            self.assertTrue(res["ready"], res.get("error"))
            self.assertEqual(res["port"], port)
            self.assertEqual(res["url"], "http://127.0.0.1:%d/" % port)
            ready, reason = dev_server.probe(res["url"])
            self.assertTrue(ready, reason)
        finally:
            stopped = dev_server.stop(res["job_id"])
            self.assertTrue(stopped["ok"], stopped)
        # 回收之后端口必须真的没人听了
        ready, _ = dev_server.probe("http://127.0.0.1:%d/" % port, timeout=1.0)
        self.assertFalse(ready)

    def test_command_that_exits_immediately_is_not_ready(self):
        res = dev_server.start(self.tmp, "quick exit",
                               argv=[sys.executable, "-c", "import sys; sys.exit(3)"],
                               port=dev_server.free_port(), wait=6)
        self.assertFalse(res["ready"])
        self.assertTrue(res["error"])
        if res.get("job_id"):
            dev_server.stop(res["job_id"])

    def test_wrong_port_is_reported_as_not_ready_not_silence(self):
        res = dev_server.start(self.tmp, "serving nothing on this port",
                               argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                               port=dev_server.free_port(), wait=3)
        self.assertFalse(res["ready"])
        self.assertIn("没探测到就绪", res["error"])
        self.assertTrue(res["ok"], "进程起来了就该分开报告")
        dev_server.stop(res["job_id"])

    def test_rejects_cwd_outside_the_project(self):
        res = dev_server.start(self.tmp, "x", argv=[sys.executable, "-c", "pass"],
                               cwd="../elsewhere")
        self.assertFalse(res["ok"])
        self.assertIn("拒绝", res["error"])

    def test_requires_argv_and_root(self):
        self.assertIn("缺少要执行的命令",
                      dev_server.start(self.tmp, "x", argv=[])["error"])
        bad = dev_server.start("", "x", argv=[sys.executable])
        self.assertIn("代码根目录", bad["error"])

    def test_render_tells_the_next_step(self):
        port = dev_server.free_port()
        res = dev_server.start(self.tmp, "python -m http.server",
                               argv=self._http_server_argv(port), port=port, wait=25)
        try:
            text = dev_server.render(res)
            self.assertIn("服务已就绪", text)
            self.assertIn("preview_project", text)
        finally:
            dev_server.stop(res["job_id"])

    def test_render_without_a_job_is_an_error(self):
        self.assertIn("未完成", dev_server.render({"job_id": "", "error": "没配置"}))


class PreviewUrlCases(_Case):
    def test_non_loopback_url_is_refused_before_any_browser_runs(self):
        with self.assertRaises(visual_acceptance.VisualAcceptanceError) as ctx:
            visual_acceptance.preview_target(self.tmp, url="https://example.com/")
        self.assertIn("回环", str(ctx.exception))

    def test_url_mode_skips_the_index_html_requirement(self):
        # 项目里没有 index.html：给本机 url 时应直接返回目标地址，不报「没有入口」
        target, entry = visual_acceptance.preview_target(
            self.tmp, url="http://127.0.0.1:5173/")
        self.assertEqual(target, "http://127.0.0.1:5173/")
        self.assertIsNone(entry)

    def test_static_mode_still_needs_an_entry(self):
        with self.assertRaises(visual_acceptance.VisualAcceptanceError):
            visual_acceptance.preview_target(self.tmp)
        with open(os.path.join(self.tmp, "index.html"), "w", encoding="utf-8") as fh:
            fh.write("<p>x</p>\n")
        target, entry = visual_acceptance.preview_target(self.tmp)
        self.assertEqual(target, "")
        self.assertTrue(entry and entry.name == "index.html")


class ToolLayerCases(_Case):
    def test_registration_and_prompt(self):
        names = {row["function"]["name"] for row in tools.tool_schemas()}
        self.assertIn("dev_serve", names)
        prompt = open(os.path.join(ROOT, "agent.py"), encoding="utf-8").read()
        self.assertIn("- dev_serve(action?", prompt)
        self.assertIn("preview_project(entry?, url?", prompt)

    def test_missing_code_root(self):
        config.set_runtime("code_root", "")
        # 工具现在说**可行动的那一半**（缺什么 + 怎么补），不再只说「没配置」。
        self.assertIn("尚未选择项目，无法定位代码库", tools.dev_serve("cmd: npm run dev"))

    def test_blocked_command_is_refused(self):
        out = tools.dev_serve("cmd: rm -rf /")
        self.assertIn("拦下", out)

    def test_missing_cmd_gives_usage(self):
        out = tools.dev_serve("action: start")
        self.assertIn("cmd", out)

    def test_stop_without_job_id(self):
        self.assertIn("job_id", tools.dev_serve("action: stop"))

    def test_preview_project_reports_a_bad_url(self):
        res = tools.preview_project("url: https://example.com/")
        text = getattr(res, "text", "") or str(res)
        self.assertIn("回环", text)


if __name__ == "__main__":
    unittest.main()
