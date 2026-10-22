# -*- coding: utf-8 -*-
"""「按后台任务抓它自己的窗口」这条生命线。

背景：agent 用 `run_command(..., background: true)` 把游戏跑起来后，
`game_screenshot(target: foreground)` 抓的是**当前有焦点的窗口** —— 那几乎总是
聊天应用自己（实测抓到 1706x1018 的 WorkBuddy 窗口，而不是 804x604 的游戏窗口）。
模型于是拿着一张完全无关的画面当"游戏运行画面"。

这里守住三件事：
  1. 任务 id → 进程 pid → 顶层窗口 这条链真的通（且只认本进程自己启动的任务）；
  2. 抓到的是**那个任务自己的**窗口，不是前台窗口；
  3. 抓不到时**明确失败**，绝不静默退回前台窗口（那个 fallback 正是坑本身）。
"""
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
import screen_capture  # noqa: E402
import tools as tools_mod  # noqa: E402
from agent_runtime import job_window  # noqa: E402
from agent_runtime import process_runner  # noqa: E402
from agent_runtime.tools import ToolResult  # noqa: E402
from config import set_runtime  # noqa: E402


def _bgra(width, height, pixel=(0, 0, 255, 255)):
    return bytes(pixel) * (width * height)


class ParseJobIdTests(unittest.TestCase):
    def test_reads_plain_key(self):
        self.assertEqual(job_window.parse_job_id("job_id: abc123"), "abc123")

    def test_reads_alongside_other_keys(self):
        self.assertEqual(
            job_window.parse_job_id("target: foreground\njob_id: abc123"), "abc123")

    def test_reads_alternative_spellings(self):
        self.assertEqual(job_window.parse_job_id("jobid: xyz"), "xyz")
        self.assertEqual(job_window.parse_job_id("job: xyz"), "xyz")

    def test_missing_key_returns_empty(self):
        self.assertEqual(job_window.parse_job_id("target: foreground"), "")
        self.assertEqual(job_window.parse_job_id(""), "")
        self.assertEqual(job_window.parse_job_id(None), "")

    def test_bare_hex_token_treated_as_job_id(self):
        # 后台任务 id 是 uuid4().hex[:10]；模型有时直接把它当整段参数传进来。
        self.assertEqual(job_window.parse_job_id("a1b2c3d4e5"), "a1b2c3d4e5")
        # 不是 id 的整段话不能被当 id 吃掉
        self.assertEqual(job_window.parse_job_id("foreground"), "")

    def test_does_not_swallow_other_fields(self):
        """不像 tools._parse_keyed 那样把未知键的值吞进前一个字段。"""
        self.assertEqual(job_window.parse_job_id("limit: 40\njob_id: abc"), "abc")


class JobPidTests(unittest.TestCase):
    def test_unknown_job_returns_zero(self):
        self.assertEqual(process_runner.job_pid("does-not-exist"), 0)

    def test_started_job_exposes_its_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = process_runner._Job(
                id="job1", command="python main.py", argv=["python", "main.py"],
                cwd=tmp, created=time.monotonic(), timeout=30)
            job.popen = types.SimpleNamespace(pid=4242)
            with patch.object(process_runner, "_JOBS", {"job1": job}):
                self.assertEqual(process_runner.job_pid("job1"), 4242)
                self.assertEqual(process_runner.job_pid(""), 0)


class JobWindowTargetTests(unittest.TestCase):
    def test_unknown_job_has_no_window(self):
        self.assertIsNone(job_window.job_window_target("nope"))
        self.assertIsNone(job_window.capture_job_window("nope"))

    def test_finds_and_restores_window_of_job_process(self):
        order = []

        def _restore(hwnd):
            order.append(("restore", int(hwnd)))
            return {"ok": True, "restored": True, "foreground": True}

        def _capture(hwnd):
            order.append(("capture", int(hwnd)))
            return (_bgra(320, 240), 320, 240)

        with patch.object(process_runner, "job_pid", return_value=4321), \
                patch("desktop_bridge.find_window",
                      return_value=(9876, "StarVoyager")), \
                patch.object(job_window, "restore_window", side_effect=_restore), \
                patch.object(screen_capture, "capture_window", side_effect=_capture):
            hit = job_window.capture_job_window("job1")
        self.assertIsNotNone(hit)
        frame, title = hit
        self.assertEqual(title, "StarVoyager")
        self.assertEqual(frame[1], 320)
        self.assertEqual(order, [("restore", 9876), ("capture", 9876)],
                         "必须先把窗口还原再抓，顺序反了就抓不到画面")

    def test_no_window_returns_none(self):
        with patch.object(process_runner, "job_pid", return_value=4321), \
                patch("desktop_bridge.find_window", return_value=None):
            self.assertIsNone(job_window.capture_job_window("job1"))


class RestoreWindowFallbackTests(unittest.TestCase):
    """desktop_bridge.restore_window 还没入库时，这条路也不能塌。

    背景：只依赖 desktop_bridge.restore_window 的代码在**干净检出**（= CI）上会
    AttributeError —— 那份实现此刻只在别人未提交的工作树里。仓库里自带一份最小
    实现兜底，这条守卫盯着它。
    """

    def test_falls_back_to_own_implementation(self):
        with patch.object(job_window, "_bridge_restore", return_value=None), \
                patch.object(job_window, "_restore_window_own",
                             return_value={"ok": True, "restored": True,
                                           "foreground": True}) as own:
            out = job_window.restore_window(123)
        self.assertTrue(out["ok"])
        own.assert_called_once_with(123)

    def test_prefers_desktop_bridge_helper_when_available(self):
        with patch.object(job_window, "_bridge_restore",
                          return_value=lambda h: {"ok": True, "restored": False,
                                                  "foreground": True}), \
                patch.object(job_window, "_restore_window_own",
                             side_effect=AssertionError("有现成实现时不该走自带兜底")):
            out = job_window.restore_window(123)
        self.assertTrue(out["ok"])

    def test_bridge_helper_failure_falls_back(self):
        def _boom(_hwnd):
            raise RuntimeError("bridge exploded")

        with patch.object(job_window, "_bridge_restore", return_value=_boom), \
                patch.object(job_window, "_restore_window_own",
                             return_value={"ok": True, "restored": True,
                                           "foreground": False}) as own:
            out = job_window.restore_window(123)
        self.assertTrue(out["ok"])
        own.assert_called_once_with(123)


class MissingWindowMessageTests(unittest.TestCase):
    def test_unknown_job_points_to_dev_job_logs(self):
        msg = job_window.describe_missing_window("nope")
        self.assertIn("dev_job_logs", msg)

    def test_finished_job_says_window_is_gone(self):
        with patch.object(process_runner, "job_logs",
                          return_value={"ok": True, "state": "done"}):
            msg = job_window.describe_missing_window("job1")
        self.assertIn("已经结束", msg)

    def test_running_job_without_window_says_so(self):
        with patch.object(process_runner, "job_logs",
                          return_value={"ok": True, "state": "running"}):
            msg = job_window.describe_missing_window("job1")
        self.assertIn("没有找到它的顶层窗口", msg)


class GameScreenshotJobTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_jobshot_")
        set_runtime("code_root", self.tmp)

    def tearDown(self):
        set_runtime("code_root", "")

    def test_captures_the_jobs_own_window(self):
        frame = (_bgra(640, 360), 640, 360)
        with patch.object(process_runner, "job_pid", return_value=4321), \
                patch("desktop_bridge.find_window",
                      return_value=(9876, "StarVoyager")), \
                patch.object(job_window, "restore_window",
                             return_value={"ok": True, "restored": True,
                                           "foreground": True}), \
                patch.object(screen_capture, "capture_window",
                             lambda hwnd: frame if int(hwnd) == 9876 else None), \
                patch.object(screen_capture, "grab_foreground",
                             lambda *a, **k: self.fail(
                                 "抓任务自己的窗口时不应再抓前台窗口")):
            result = tools_mod.game_screenshot("job_id: job1")
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.ok)
        self.assertIn("StarVoyager", result.text)
        self.assertIn("640x360", result.text)
        self.assertEqual(
            result.artifacts[0]["metadata"]["capture_source"], "job:job1")
        self.assertEqual(len(result.data["images"]), 1)

    def test_missing_window_fails_loudly_and_never_falls_back_to_foreground(self):
        with patch.object(process_runner, "job_pid", return_value=0), \
                patch.object(screen_capture, "grab_foreground",
                             lambda *a, **k: self.fail(
                                 "抓不到任务窗口时必须明确失败，不许退回前台窗口")), \
                patch.object(tools_mod, "_try_mcp_screenshot",
                             lambda root: self.fail("按任务抓窗口不应探测 MCP")):
            result = tools_mod.game_screenshot("job_id: job1")
        self.assertIsInstance(result, str)
        self.assertIn("截图失败", result)
        self.assertIn("job1", result)

    def test_job_target_does_not_probe_mcp_or_embedded(self):
        with patch.object(process_runner, "job_pid", return_value=4321), \
                patch("desktop_bridge.find_window",
                      return_value=(9876, "StarVoyager")), \
                patch.object(job_window, "restore_window",
                             return_value={"ok": True, "restored": True,
                                           "foreground": True}), \
                patch.object(screen_capture, "capture_window",
                             lambda hwnd: (_bgra(8, 8), 8, 8)), \
                patch.object(tools_mod, "_try_mcp_screenshot",
                             lambda root: self.fail("按任务抓窗口不应探测 MCP")), \
                patch.object(screen_capture, "grab_embedded",
                             lambda *a, **k: self.fail("按任务抓窗口不应抓嵌入窗")):
            tools_mod.game_screenshot("job_id: job1")


class PromptWiringTests(unittest.TestCase):
    def test_prompt_tells_model_to_capture_by_job_id(self):
        self.assertIn("game_screenshot(job_id:", agent_mod._SYSTEM_PROMPT_FULL)

    def test_prompt_still_mentions_the_three_step_flow(self):
        self.assertIn("background: true", agent_mod._SYSTEM_PROMPT_FULL)
        self.assertIn("dev_job_logs(job_id:", agent_mod._SYSTEM_PROMPT_FULL)

    def test_tool_description_documents_job_id(self):
        spec = tools_mod.TOOLS["game_screenshot"]
        self.assertIn("job_id", spec["description"])


if __name__ == "__main__":
    unittest.main()
