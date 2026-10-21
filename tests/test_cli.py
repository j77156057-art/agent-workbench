# -*- coding: utf-8 -*-
"""CLI 最小集回归：doctor / chat / bridge。

全程用假响应，不发真请求、不碰真实服务；活体冒烟另做。
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cli  # noqa: E402


def _sse(event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")


class _FakeResponse:
    def __init__(self, lines=None, body=b"{}"):
        self._lines = lines or []
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines)


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _doctor_fake(health=None, config=None, bridge=None, code_root=None, failing=()):
    def fake(base, path, **kwargs):
        if path in failing:
            raise cli.CliError("boom")
        if path == "/api/health":
            return health
        if path == "/api/config":
            return config
        if path == "/api/bridge_writes":
            return bridge
        if path == "/api/code_root_status":
            return code_root
        raise AssertionError("unexpected path: %s" % path)

    return fake


def _healthy_payloads():
    return dict(
        health={"ok": True, "service": "docmind"},
        config={
            "llm_provider": "qwen",
            "llm_model": "qwen-plus",
            "has_key": True,
            "capability": {"context_window": 131072},
            "edit_confirm": True,
            "llm_mode": "fixed",
            "warnings": [],
        },
        bridge={"write_bridge": True, "pending": []},
        code_root={"effective": "D:/proj", "source": "global", "mismatch_top_vs_project": False},
    )


class BaseResolutionTests(unittest.TestCase):
    def _args(self, argv):
        return cli.build_parser().parse_args(argv)

    def test_port_flag(self):
        self.assertEqual(cli._resolve_base(self._args(["--port", "8013", "doctor"])),
                         "http://127.0.0.1:8013")

    def test_env_port(self):
        with patch.dict(os.environ, {"DOCMIND_PORT": "8012"}, clear=False):
            os.environ.pop("DOCMIND_API_BASE", None)
            self.assertEqual(cli._resolve_base(self._args(["doctor"])),
                             "http://127.0.0.1:8012")

    def test_env_base_wins_over_env_port(self):
        with patch.dict(os.environ, {"DOCMIND_API_BASE": "example.test:9000/",
                                     "DOCMIND_PORT": "8012"}, clear=False):
            self.assertEqual(cli._resolve_base(self._args(["doctor"])),
                             "http://example.test:9000")

    def test_base_and_port_conflict(self):
        code, _out, err = _run(["--base", "http://x", "--port", "8013", "doctor"])
        self.assertEqual(code, 2)
        self.assertIn("只能给一个", err)

    def test_bridge_confirm_parses_id_and_session(self):
        args = cli.build_parser().parse_args(["bridge", "confirm", "W2", "--session", "dev"])
        self.assertEqual(args.bridge_command, "confirm")
        self.assertEqual(args.id, "W2")
        self.assertEqual(args.session, "dev")


class DoctorTests(unittest.TestCase):
    def test_healthy_exit_zero(self):
        with patch.object(cli, "_request_json", side_effect=_doctor_fake(**_healthy_payloads())):
            code, out, _err = _run(["--base", "http://x", "doctor"])
        self.assertEqual(code, 0)
        self.assertIn("[OK]", out)
        self.assertIn("待确认写 0 条", out)
        self.assertIn("结论：基本可用", out)

    def test_missing_key_is_hard_failure(self):
        payloads = _healthy_payloads()
        payloads["config"]["has_key"] = False
        with patch.object(cli, "_request_json", side_effect=_doctor_fake(**payloads)):
            code, out, _err = _run(["--base", "http://x", "doctor"])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)
        self.assertIn("key 缺失", out)

    def test_unreachable_service_is_hard_failure(self):
        with patch.object(cli, "_request_json", side_effect=cli.CliError("connection refused")):
            code, out, _err = _run(["--base", "http://x", "doctor"])
        self.assertEqual(code, 1)
        self.assertIn("无法访问 /api/health", out)
        self.assertIn("结论：有硬性失败", out)

    def test_config_unavailable_is_warning_only(self):
        payloads = _healthy_payloads()
        with patch.object(cli, "_request_json",
                          side_effect=_doctor_fake(**payloads, failing=("/api/config",))):
            code, out, _err = _run(["--base", "http://x", "doctor"])
        self.assertEqual(code, 0)
        self.assertIn("/api/config 不可用", out)

    def test_empty_code_root_warns(self):
        payloads = _healthy_payloads()
        payloads["code_root"] = {"effective": "", "source": "global", "mismatch_top_vs_project": False}
        with patch.object(cli, "_request_json", side_effect=_doctor_fake(**payloads)):
            code, out, _err = _run(["--base", "http://x", "doctor"])
        self.assertEqual(code, 0)
        self.assertIn("code_root 为空", out)

    def test_qwen_output_budget_warning(self):
        payloads = _healthy_payloads()
        with patch.object(cli, "_request_json", side_effect=_doctor_fake(**payloads)):
            _code, out, _err = _run(["--base", "http://x", "doctor"])
        self.assertIn("DOCMIND_OUTPUT_MAX_TOKENS=30000", out)

    def test_json_output(self):
        with patch.object(cli, "_request_json", side_effect=_doctor_fake(**_healthy_payloads())):
            code, out, _err = _run(["--base", "http://x", "doctor", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["config"]["llm_model"], "qwen-plus")
        self.assertEqual(data["bridge"]["pending"], [])


class ChatTests(unittest.TestCase):
    def _run_chat(self, lines, argv=None, capture=None):
        def fake_urlopen(req, timeout=None):
            if capture is not None:
                capture["url"] = req.full_url
                capture["data"] = req.data
                capture["headers"] = dict(req.header_items())
                capture["timeout"] = timeout
            return _FakeResponse(lines=lines)

        with patch.object(cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            return _run(argv or ["--base", "http://x", "chat", "你好"])

    def test_streams_token_once(self):
        lines = [
            _sse({"type": "turn", "session_id": "s", "turn_id": "t"}),
            _sse({"type": "token", "text": "你好"}),
            _sse({"type": "final", "text": "你好\n", "status": "ok"}),
            _sse({"type": "done"}),
        ]
        capture = {}
        code, out, _err = self._run_chat(lines, capture=capture)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("你好"), 1)
        self.assertEqual(capture["url"], "http://x/api/chat")
        self.assertIn("你好".encode("utf-8"), capture["data"])
        self.assertIn(b'name="session_id"', capture["data"])

    def test_project_header(self):
        lines = [_sse({"type": "final", "text": "ok\n", "status": "ok"}),
                 _sse({"type": "done"})]
        capture = {}
        code, _out, _err = self._run_chat(
            lines,
            argv=["--base", "http://x", "chat", "hi", "--project", "prj-1"],
            capture=capture,
        )
        self.assertEqual(code, 0)
        headers = {k.lower(): v for k, v in capture["headers"].items()}
        self.assertEqual(headers.get("x-docmind-project"), "prj-1")

    def test_file_question_is_utf8(self):
        lines = [_sse({"type": "final", "text": "ok\n", "status": "ok"}),
                 _sse({"type": "done"})]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "q.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("来自文件的问题")
            capture = {}
            code, _out, _err = self._run_chat(
                lines,
                argv=["--base", "http://x", "chat", "--file", path],
                capture=capture,
            )
        self.assertEqual(code, 0)
        self.assertIn("来自文件的问题".encode("utf-8"), capture["data"])

    def test_error_final_exit_one(self):
        lines = [_sse({"type": "final", "text": "本轮请求失败，未完成。\n", "status": "error"}),
                 _sse({"type": "done"})]
        code, out, _err = self._run_chat(lines)
        self.assertEqual(code, 1)
        self.assertIn("本轮请求失败", out)

    def test_error_final_after_partial_tokens_is_not_swallowed(self):
        lines = [_sse({"type": "token", "text": "半截回答"}),
                 _sse({"type": "final", "text": "本轮请求失败，未完成。\n", "status": "error"})]
        code, out, _err = self._run_chat(lines)
        self.assertEqual(code, 1)
        self.assertIn("半截回答", out)
        self.assertIn("本轮请求失败", out)

    def test_missing_final_exit_one(self):
        lines = [_sse({"type": "token", "text": "半截"})]
        code, _out, err = self._run_chat(lines)
        self.assertEqual(code, 1)
        self.assertIn("final 之前结束", err)

    def test_json_mode_prints_one_object_per_line(self):
        lines = [_sse({"type": "token", "text": "x"}),
                 _sse({"type": "final", "text": "x", "status": "ok"}),
                 _sse({"type": "done"})]
        code, out, _err = self._run_chat(
            lines, argv=["--base", "http://x", "chat", "hi", "--json"])
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual([event["type"] for event in events], ["token", "final", "done"])

    def test_empty_question_errors(self):
        class _Tty:
            def isatty(self):
                return True

        with patch.object(cli.sys, "stdin", _Tty()):
            code, _out, err = _run(["--base", "http://x", "chat"])
        self.assertEqual(code, 1)
        self.assertIn("问题为空", err)


class BridgeTests(unittest.TestCase):
    def test_list_renders_pending(self):
        data = {
            "write_bridge": True,
            "pending": [{
                "id": "W2", "tier": 1, "confirms": 1, "need": 1,
                "tool": "apply_edit", "path": "C:/x", "reason": "根内", "session": "s",
            }],
        }
        with patch.object(cli, "_request_json", return_value=data):
            code, out, _err = _run(["--base", "http://x", "bridge", "list"])
        self.assertEqual(code, 0)
        self.assertIn("#W2", out)
        self.assertIn("tier=1", out)
        self.assertIn("need=1/1", out)
        self.assertIn("C:/x", out)

    def test_list_passes_session(self):
        data = {"write_bridge": False, "pending": []}
        with patch.object(cli, "_request_json", return_value=data) as mocked:
            code, _out, _err = _run(
                ["--base", "http://x", "bridge", "list", "--session", "dev"])
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_args.kwargs["params"], {"session": "dev"})

    def test_confirm_success(self):
        with patch.object(cli, "_request_json",
                          return_value={"ok": True, "message": "已执行"}) as mocked:
            code, out, _err = _run(
                ["--base", "http://x", "bridge", "confirm", "W2", "--session", "s"])
        self.assertEqual(code, 0)
        self.assertIn("确认 #W2", out)
        self.assertEqual(mocked.call_args.kwargs["payload"], {"id": "W2", "session": "s"})

    def test_reject_not_found_exit_one(self):
        with patch.object(cli, "_request_json",
                          return_value={"ok": False, "message": "未找到待确认写 #W9"}):
            code, out, _err = _run(["--base", "http://x", "bridge", "reject", "W9"])
        self.assertEqual(code, 1)
        self.assertIn("未找到", out)
        self.assertIn("拒绝 #W9", out)


if __name__ == "__main__":
    unittest.main()
