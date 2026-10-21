# -*- coding: utf-8 -*-
"""长驻校验浏览器（tests/manual/cockpit_live_validation.py）的回收契约。

这个脚本整场真实模型校验都开着同一个浏览器，比一次性预览更经不起漏：父进程一旦被硬杀
就是一次 16 进程的 99°C。它原先自带一份复制出来的启动路径，没有作业对象，且 `close()`
里 `process.wait` 一超时还会把 profile 回收一起跳掉。这里锁住这两条修掉的洞。
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
MANUAL = REPO / "tests" / "manual"

sys.path.insert(0, str(REPO))

from agent_runtime import visual_acceptance as visual  # noqa: E402

CHILD = r'''
import importlib.util, sys, time
from pathlib import Path
REPO = r"%s"
sys.path.insert(0, REPO)
sys.path.insert(0, str(Path(REPO) / "tests" / "manual"))
spec = importlib.util.spec_from_file_location("clv", str(Path(REPO) / "tests" / "manual" / "cockpit_live_validation.py"))
clv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clv)
browser = clv.ProjectBrowser(Path(r"%s"), Path(r"%s"), "000000")
Path(r"%s").write_text(browser.profile.name, encoding="utf-8")
time.sleep(180)
'''


def _load_manual():
    spec = importlib.util.spec_from_file_location(
        "cockpit_live_validation", MANUAL / "cockpit_live_validation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _msedge_pids_using(profile: str) -> list[int]:
    """按 user-data-dir 精确点名，不拿进程总数当代理指标。"""
    script = ("$r=@(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
              "Where-Object { $_.CommandLine -like '*%s*' } | ForEach-Object { $_.ProcessId }); "
              "$r -join ','" % profile.replace("'", ""))
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         capture_output=True, text=True, timeout=90)
    return [int(x) for x in out.stdout.strip().split(",") if x.strip().isdigit()]


class CockpitLiveReapingTests(unittest.TestCase):
    def setUp(self):
        try:
            # 判据是「浏览器真能起」，不只是「二进制在」：CI 上 runner 镜像自带
            # Edge，但它一起身就退，于是这两条会以裸 FileNotFoundError 报红。
            if not visual.browser_available():
                self.skipTest("浏览器起不来：%s" % (
                    visual.browser_probe_reason() or "未知原因"))
            visual._edge_binary()
        except visual.VisualAcceptanceError as exc:
            self.skipTest(str(exc))
        self.module = _load_manual()

    def _browser(self, root):
        project = Path(root) / "project"
        project.mkdir()
        (project / "index.html").write_text("<h1>live</h1>", encoding="utf-8")
        evidence = Path(root) / "evidence"
        evidence.mkdir()
        return self.module.ProjectBrowser(project, evidence, "000000")

    @unittest.skipUnless(os.name == "nt", "作业对象是 Windows 机制")
    def test_hard_killed_owner_leaves_no_browser(self):
        """只杀校验进程本身（不带 /T），浏览器树必须由作业对象收走。

        不带 /T 是关键：带上了就是让 taskkill 替被测机制干活，什么都测不到。
        """
        with tempfile.TemporaryDirectory() as root:
            project, evidence = Path(root) / "project", Path(root) / "evidence"
            project.mkdir()
            evidence.mkdir()
            marker = Path(root) / "profile.txt"
            child = subprocess.Popen(
                [sys.executable, "-c", CHILD % (str(REPO), str(project), str(evidence),
                                                str(marker))],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            deadline = time.time() + 90
            while not marker.exists() and time.time() < deadline:
                time.sleep(0.2)
            if not marker.exists():
                child.kill()
                self.fail("ProjectBrowser never reported a profile: %s"
                          % child.stderr.read().decode("utf-8", "replace")[-600:])
            try:
                profile = marker.read_text(encoding="utf-8").strip()
                time.sleep(1.5)                  # 等 renderer/gpu/utility 全部派生出来
                tree = _msedge_pids_using(profile)
                self.assertGreater(len(tree), 1,
                                   "长驻浏览器应当已派生出子进程，否则这条什么都没断")
                subprocess.run(["taskkill", "/PID", str(child.pid), "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                survivors: list[int] = []
                deadline = time.time() + 25
                while time.time() < deadline:
                    survivors = _msedge_pids_using(profile)
                    if not survivors:
                        break
                    time.sleep(1)
                self.assertEqual(survivors, [],
                                 "校验进程被硬杀后 %d 个浏览器进程存活——就是 99°C 那一类"
                                 % len(survivors))
            finally:
                for pid in _msedge_pids_using(marker.read_text(encoding="utf-8").strip()):
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=20)
                child.kill()
                child.wait(timeout=20)
                # 作业对象只收进程，不收目录；红的时候也不能把 profile 留在盘上——
                # 生产的 sweep 刻意不碰 live-preview 这一族（它可能是别人开着的长驻会话），
                # 所以这里的垃圾由本测试自己负责。
                import shutil
                shutil.rmtree(marker.read_text(encoding="utf-8").strip(), ignore_errors=True)

    @unittest.skipUnless(os.name == "nt", "作业对象是 Windows 机制")
    def test_unbinding_the_job_reaps_immediately_not_eventually(self):
        """解绑作业句柄必须【当场】收干净，因为紧接着就是 `_release_profile` 的 rmtree。

        这条钉的是不变量本身，不是某个实现细节：实测只 `CloseHandle` 就已经是同步的，
        加 `TerminateJobObject` 测不出差别，所以没加。若哪天有人去掉
        `JOB_OBJECT_LIMIT_KILL_ON_CLOSE`、或让作业句柄被继承出去留下第二个句柄，
        这里会立刻红——而不是等到第二天发现盘上多了 16 个进程。

        计数必须按自己的 user-data-dir 收窄，且用 ctypes 查已知 pid：这台机器上别的
        lane 同时在跑浏览器，而 PowerShell 单次约 1.15 秒的开销会把任何异步窗口掩盖掉
        （我第一版就是这么写出过一条永远成立的断言）。
        """
        with tempfile.TemporaryDirectory() as root:
            profile = str(Path(root) / "edge-profile")
            Path(profile).mkdir()
            browser, job, error = visual.spawn_browser_in_job(
                [visual._edge_binary(), *visual._BASE_FLAGS,
                 "--user-data-dir=%s" % profile, "about:blank"])
            self.assertIsNotNone(browser, error)
            try:
                time.sleep(4.0)                       # 让树长齐
                tree = _msedge_pids_using(profile)
                self.assertGreater(len(tree), 1, "没派生出子进程，这条什么都没断")
                self.assertTrue(visual._close_reaper(job))
                job = None
                still_alive = [pid for pid in tree if visual._pid_alive(pid)]
                self.assertEqual(still_alive, [],
                                 "解绑后仍有 %d 个已知进程存活——紧接着的 rmtree 会撞上文件锁"
                                 % len(still_alive))
                browser.kill()
                browser.wait(timeout=10)
            finally:
                if job is not None:
                    visual._close_reaper(job)
                for pid in _msedge_pids_using(profile):
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=20)

    def test_close_finishes_even_when_the_browser_refuses_to_exit(self):
        """`process.wait` 抛超时不能把服务停止与 profile 回收一起跳掉。"""
        with tempfile.TemporaryDirectory() as root:
            browser = self._browser(root)
            try:
                profile = browser.profile.name
                self.assertTrue(os.path.isdir(profile))
                with patch.object(browser.process, "wait",
                                  side_effect=subprocess.TimeoutExpired(cmd="msedge", timeout=10)):
                    browser.close()
                self.assertFalse(os.path.isdir(profile),
                                 "进程没在等到时间内退出，profile 就被整段跳过了——第二个泄漏点")
                self.assertFalse(browser.thread.is_alive(), "静态服务线程应随 close 停掉")
                # 这里原来还有一句 assertIsNone(browser.job)：close() 无条件把 job 置 None，
                # 而 _close_reaper 自己吞掉 OSError、也不看 CloseHandle 返回值，所以那句
                # 恒真，删掉。要断「句柄真的关了」得先让 _close_reaper 报告成败。
            finally:
                browser.close()

    def test_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            browser = self._browser(root)
            browser.close()
            browser.close()


if __name__ == "__main__":
    unittest.main()
