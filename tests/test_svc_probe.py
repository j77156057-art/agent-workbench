"""dev_probe 回归：让后端与命令行也有真凭据，并盯住它的每一种假干净。

浏览器那套观察能力（preview_project / dev_page_action / dev_vue / dev_apptest）
对纯后端、CLI、worker 都够不着，于是那段闭环退回「一屏文本 + 模型自己读」。
本文件钉住的是文本阅读最容易滑进去的五个坑：

1. **5xx 被读成「服务正常」**（它其实同时是「服务在应答」和「这次不该通过」两件事）
   —— 守它：`a_500_is_answered_and_unhealthy`、`a_500_is_still_proof_of_liveness`。
2. **连不上被读成「没错误」** —— 守它：`a_dead_port_is_not_a_clean_probe`。
3. **退出码 0 被读成「干了活」** —— 守它：`a_silent_zero_exit_is_not_success`。
4. **输出被截断后「后面没看到错」被读成「后面没错」** —— 守它：
   `truncated_output_never_reads_as_clean`。
5. **正文里出现 error 字样的正常输出被误判成报错**（一个天天误报的检查用两次就
   被关掉）—— 守它：`prose_mentioning_error_is_not_an_error_line`。

另外三条边界：access log 噪声折叠、真 traceback 与假 traceback 的区别、命令安全门
必须复用 `run_command` 那一套（自己 split 会拆坏引号，更会绕开黑名单）。
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tools  # noqa: E402
from agent_runtime import svc_probe as sp  # noqa: E402

OK_BODY = json.dumps({"items": [{"id": 1}, {"id": 2}], "total": 2}).encode()
ERR_BODY = json.dumps({"error": "db unreachable"}).encode()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音：测试输出里不要有访问日志
        pass

    def _send(self, status, body=b"", ctype="application/json"):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/ok":
            self._send(200, OK_BODY)
        elif self.path == "/boom":
            self._send(500, ERR_BODY)
        elif self.path == "/empty":
            self._send(204)
        elif self.path == "/html-error":
            self._send(503, b"<html><body>Service Unavailable</body></html>",
                       "text/html")
        elif self.path == "/big":
            self._send(200, b"x" * (sp.BODY_LIMIT * 2))
        elif self.path == "/huge-error-tail":
            # 前 8KB 全是填充，错误行正好落在截断窗口之外——「没看到错」的最坏形态。
            self._send(200, b"y" * (sp.BODY_LIMIT + 500) + b"\nERROR: the real problem\n")
        else:
            self._send(404)

    do_POST = do_GET


class ServerCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)


class HttpProbeTests(ServerCase):

    def test_a_healthy_endpoint_is_green_with_real_evidence(self):
        result = sp.http_probe(self.url("/ok"))
        self.assertTrue(result["ok"], result["checks"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["total"], 2)
        self.assertEqual(result["json_error_fields"], [])

    def test_a_500_is_answered_and_unhealthy(self):
        """同一个 500 有两个意思：服务确实在应答（凭据），但这次不该通过（判定）。
        只报其中一半都是错的。"""
        result = sp.http_probe(self.url("/boom"))
        self.assertEqual(result["status"], 500)
        self.assertTrue(result["checks"]["server_answered"], "500 也是应答")
        self.assertFalse(result["checks"]["not_server_error"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["json_error_fields"], ["error"])

    def test_a_500_is_still_proof_of_liveness(self):
        """把 5xx 直接当「没应答」是另一种谎：那会让 Agent 去重启一个好好在跑的服务。"""
        result = sp.http_probe(self.url("/boom"))
        self.assertIsNotNone(result["status"])
        self.assertTrue(result["checks"]["server_answered"])

    def test_a_404_is_not_a_server_error(self):
        """404 不是服务端故障——「这个路由不存在」是完全正常的观察结果。"""
        result = sp.http_probe(self.url("/nope"))
        self.assertEqual(result["status"], 404)
        self.assertTrue(result["checks"]["not_server_error"])
        self.assertTrue(result["ok"], result["checks"])

    def test_an_html_error_page_is_not_scraped_for_json_error_keys(self):
        """只对 JSON 生效的错误字段识别：HTML 错误页里的 message 不算。"""
        result = sp.http_probe(self.url("/html-error"))
        self.assertEqual(result["status"], 503)
        self.assertEqual(result["json_error_fields"], [])
        self.assertIsNone(result["json"])

    def test_a_dead_port_is_not_a_clean_probe(self):
        """连不上必须是一条红，且说清「没有应答」——不能读成「没发现错误」。"""
        result = sp.http_probe("http://127.0.0.1:1/")
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["server_answered"])
        self.assertIsNone(result["status"])
        self.assertIn("连不上", result["error"])

    def test_a_non_loopback_url_is_refused_before_any_request(self):
        result = sp.http_probe("http://example.com/")
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"].get("url_allowed"))
        self.assertIn("回环", result["error"])

    def test_an_unknown_method_is_refused(self):
        result = sp.http_probe(self.url("/ok"), method="TELEPORT")
        self.assertFalse(result["checks"].get("method_known"))
        self.assertIn("不认这个方法", result["error"])

    def test_an_empty_body_is_reported_as_empty_not_as_missing(self):
        result = sp.http_probe(self.url("/empty"))
        self.assertEqual(result["status"], 204)
        self.assertEqual(result["body_excerpt"], "")

    def test_truncated_output_never_reads_as_clean(self):
        """截断必须显式标出来，而且明确说「后面没看到」不等于「后面没错」。"""
        result = sp.http_probe(self.url("/big"))
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["body_excerpt"]), sp.BODY_LIMIT)

    def test_an_error_past_the_truncation_window_is_not_found(self):
        """这条是截断的代价，必须看得见：错误行正好在窗口之外，于是扫不到——
        但报告得说清「只看了前面这些」，不能让人以为全文都没错。"""
        result = sp.http_probe(self.url("/huge-error-tail"))
        self.assertTrue(result["truncated"])
        text = "未通过" if not result["ok"] else "通过"
        rendered = sp.render_http(result)
        self.assertIn(text, rendered)
        self.assertIn("截断", rendered)

    def test_expect_status_mismatch_is_named_not_hidden(self):
        result = sp.http_probe(self.url("/ok"))
        rendered = sp.render_http(result, expect_status=201)
        self.assertIn("期望 HTTP 201", rendered)
        self.assertIn("实际 200", rendered)

    def test_expect_text_presence_is_reported(self):
        self.assertIn("在响应里", sp.render_http(sp.http_probe(self.url("/ok")),
                                                 expect_text="total"))
        self.assertIn("**不在**", sp.render_http(sp.http_probe(self.url("/ok")),
                                                expect_text="not-there"))


class FakeRunner:
    """假 subprocess：回放一段输出与退出码。"""

    def __init__(self, output="", exit_code=0, truncated=False, timed_out=False,
                 total_chars=None, error=""):
        self.output = output
        self.exit_code = exit_code
        self.truncated = truncated
        self.timed_out = timed_out
        self.total_chars = total_chars
        self.error = error
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return {"ok": self.exit_code == 0, "exit_code": self.exit_code,
                "output": self.output, "truncated": self.truncated,
                "total_chars": self.total_chars or len(self.output),
                "timed_out": self.timed_out, "error": self.error,
                "elapsed": 0.01}


class CliProbeTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="svcprobe-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def probe(self, runner, **kwargs):
        kwargs.setdefault("timeout", 10)
        return sp.cli_probe(self.dir, "some-cmd --flag", runner=runner, **kwargs)

    def test_a_clean_run_with_output_is_green(self):
        runner = FakeRunner("processed 12 records\n", 0)
        result = self.probe(runner)
        self.assertTrue(result["ok"], result["checks"])
        self.assertTrue(all(result["checks"].values()))

    def test_a_silent_zero_exit_is_not_success(self):
        """实测过的那一形态：`python -c pass` 退出码 0，但什么都没干。
        退出码证明不了「它干了活」。"""
        runner = FakeRunner("", 0)
        result = self.probe(runner)
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["checks"]["produced_output"])
        self.assertFalse(result["ok"])
        self.assertIn("没有任何输出", sp.render_cli(result))

    def test_allow_silent_opts_out_of_the_output_requirement(self):
        runner = FakeRunner("", 0)
        result = self.probe(runner, allow_silent=True)
        self.assertTrue(result["ok"], result["checks"])

    def test_a_traceback_is_caught_even_with_a_zero_exit(self):
        runner = FakeRunner("Traceback (most recent call last):\n  File \"x\"\n"
                            "KeyError: 'missing'\n", 0)
        result = self.probe(runner)
        self.assertFalse(result["checks"]["no_traceback"])
        self.assertEqual(result["traceback_tail"], "KeyError: 'missing'")
        self.assertFalse(result["ok"])

    def test_a_bare_error_type_line_counts_as_a_traceback(self):
        runner = FakeRunner("ValueError: bad input\n", 0)
        result = self.probe(runner)
        self.assertTrue(result["checks"]["no_traceback"] is False)

    def test_error_level_lines_are_caught(self):
        runner = FakeRunner("starting\nERROR: could not open db\ndone\n", 0)
        result = self.probe(runner)
        self.assertFalse(result["checks"]["no_error_lines"])
        self.assertEqual(result["error_lines"], ["ERROR: could not open db"])

    def test_prose_mentioning_error_is_not_an_error_line(self):
        """`fix the error handling path` 是正常输出。拿正文里的 error 字样充数，
        这个检查用两次就会被关掉。"""
        runner = FakeRunner("docs: how to fix the error handling path\n", 0)
        result = self.probe(runner)
        self.assertTrue(result["checks"]["no_error_lines"], result["error_lines"])
        self.assertTrue(result["ok"], result["checks"])

    def test_access_log_noise_is_not_mistaken_for_errors(self):
        """真 uvicorn 的 access 行每行都带随机端口，是这个仓库最吵的噪声源。"""
        runner = FakeRunner(
            'INFO:     127.0.0.1:52744 - "GET /api/x HTTP/1.1" 200 OK\n'
            'INFO:     127.0.0.1:52999 - "GET /api/x HTTP/1.1" 200 OK\n', 0)
        result = self.probe(runner)
        self.assertEqual(result["error_lines"], [])
        self.assertTrue(result["ok"], result["checks"])

    def test_an_undefined_name_is_named(self):
        runner = FakeRunner("NameError: name 'total_count' is not defined\n", 1)
        result = self.probe(runner)
        self.assertEqual(result["undefined_names"], ["total_count"])
        self.assertFalse(result["checks"]["no_undefined_names"])
        self.assertIn("total_count", sp.render_cli(result))

    def test_truncated_output_never_reads_as_clean(self):
        runner = FakeRunner("start\n" + "z" * 9000, 0, truncated=True, total_chars=9006)
        result = self.probe(runner)
        self.assertFalse(result["checks"]["no_truncated_output"])
        self.assertFalse(result["ok"])
        self.assertIn("不能当「后面没有错」", "\n".join(result["notes"]))

    def test_a_process_that_never_started_is_not_a_pass(self):
        runner = FakeRunner("", None, error="系统找不到指定的文件")
        result = self.probe(runner)
        self.assertFalse(result["checks"]["process_started"])
        self.assertIn("没能跑起来", result["error"])

    def test_a_timeout_is_not_a_pass(self):
        runner = FakeRunner("partial work\n", None, timed_out=True)
        result = self.probe(runner)
        self.assertFalse(result["checks"]["finished"])
        self.assertIn("超时", sp.render_cli(result))

    def test_expect_text_must_actually_be_there(self):
        runner = FakeRunner("all good\n", 0)
        result = self.probe(runner, expect_text="WROTE 12 FILES")
        self.assertFalse(result["checks"]["expected_text_present"])
        self.assertFalse(result["ok"])
        self.assertIn("没有产生你期待的证据", "\n".join(result["notes"]))

    def test_forbid_text_absence_is_checked(self):
        runner = FakeRunner("ok\nTraceback (most recent call last)\n", 0)
        result = self.probe(runner, forbid_text="Traceback")
        self.assertFalse(result["checks"]["forbidden_text_absent"])

    def test_a_missing_root_is_refused_before_running(self):
        result = sp.cli_probe(os.path.join(self.dir, "nope"), "cmd", runner=FakeRunner())
        self.assertFalse(result["checks"]["root_exists"])
        self.assertFalse(result["ok"])

    def test_an_empty_command_is_refused(self):
        result = sp.cli_probe(self.dir, "   ", runner=FakeRunner())
        self.assertFalse(result["checks"]["command_given"])


class SafetyTests(unittest.TestCase):
    """命令解析与安全门必须复用 run_command 那一套，不能自己发明。"""

    def test_quoted_arguments_survive_splitting(self):
        """实测踩过的坑：`text.split()` 会把 `-c "print(1)"` 拆成三段，
        引号原样留在参数里——命令「跑了」但跑的不是你写的东西。"""
        argv = sp._split_argv('python -c "print(1)"')
        self.assertEqual(argv, ["python", "-c", "print(1)"])

    def test_the_safety_gate_can_refuse_before_anything_runs(self):
        ran = []

        def runner(argv, **kwargs):  # pragma: no cover - 不该被调用
            ran.append(argv)
            raise AssertionError("被拦截的命令不该真的跑起来")

        def gate(command):
            return (True, "rm -rf") if "rm -rf" in command else (False, "")

        with tempfile.TemporaryDirectory() as root:
            result = sp.cli_probe(root, "rm -rf /", runner=runner, gate=gate)
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["command_allowed"])
        self.assertIn("安全策略", result["error"])
        self.assertEqual(ran, [])

    def test_the_tool_passes_the_repo_argv_and_gate(self):
        """工具函数必须把 tools 层的解析与黑名单注入进去。源码级断言：接线一旦
        断开，这里立刻红。"""
        import inspect
        source = inspect.getsource(tools.dev_probe)
        self.assertIn("argv_of=_shell_argv", source)
        self.assertIn("gate=_cmd_is_blocked", source)


class ToolWiringTests(unittest.TestCase):

    def test_the_tool_is_registered(self):
        self.assertIn("dev_probe", tools.TOOLS)
        self.assertTrue(callable(tools.TOOLS["dev_probe"]["func"]))

    def test_the_tool_has_one_clean_catalog_line(self):
        import agent
        rows = [line for line in agent._SYSTEM_PROMPT_FULL.splitlines()
                if line.startswith("- dev_probe")]
        self.assertEqual(len(rows), 1, "dev_probe 在提示词目录里应当恰好一行")

    def test_http_mode_is_reachable_through_the_tool(self):
        original = tools.CODE_ROOT
        tools.CODE_ROOT = ROOT
        try:
            out = tools.TOOLS["dev_probe"]["func"]("mode: http\nurl: http://example.com/")
        finally:
            tools.CODE_ROOT = original
        self.assertIn("回环", out)

    def test_an_unknown_mode_is_refused(self):
        original = tools.CODE_ROOT
        tools.CODE_ROOT = ROOT
        try:
            out = tools.TOOLS["dev_probe"]["func"]("mode: teleport")
        finally:
            tools.CODE_ROOT = original
        self.assertIn("只认", out)


if __name__ == "__main__":
    unittest.main()
