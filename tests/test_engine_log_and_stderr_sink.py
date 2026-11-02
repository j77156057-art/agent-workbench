"""两处句柄关闭点的回归守卫：引擎日志、浏览器 stderr 汇点。

这两处不是「改成 with 就完事」的形状 —— 句柄必须活到子进程启动/停止之后，
所以关闭点写在调用方。它们各自都是一个**真泄漏**的修复点：

- `game_workbench.engine_stop` 的早退分支（进程自己已经退出）以前从不摘 `_ENGINE_LOGS`，
  句柄要等 GC；现在两条分支共用 `_close_engine_log`。
- `agent_runtime.visual_acceptance.spawn_browser_in_job` 以前把
  `stderr=_browser_stderr_target(argv)` 直接塞进 `Popen`，父进程那份句柄再没人关 ——
  每起一次浏览器漏一个 fd。现在 `Popen` 之后在 `finally` 立刻关（子进程已 dup 自己那份）。

用例都验证「关闭点真的发生了」，不是验证源码写法 —— 所以把它们改回去会红。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import game_workbench as gw
from agent_runtime import visual_acceptance as va


class EngineLogCloseTests(unittest.TestCase):
    def setUp(self):
        gw._ENGINE_LOGS.clear()

    def tearDown(self):
        gw._ENGINE_LOGS.clear()

    def _open_log(self, root):
        path = Path(root) / "engine.log"
        return open(path, "a", encoding="utf-8")  # noqa: SIM115 - 演示用的长生命周期句柄

    def test_closes_and_forgets_the_registered_handle(self):
        with tempfile.TemporaryDirectory() as root:
            handle = self._open_log(root)
            gw._ENGINE_LOGS[root] = handle
            gw._close_engine_log(root)
            self.assertTrue(handle.closed, "句柄没关：进程早退时日志 fd 会一直挂到 GC")
            self.assertNotIn(root, gw._ENGINE_LOGS, "登记表没摘：下一次会拿到一个已关的句柄")

    def test_is_safe_when_there_is_nothing_registered(self):
        gw._close_engine_log("no-such-root")  # 不抛就够

    def test_an_already_closed_handle_does_not_raise(self):
        with tempfile.TemporaryDirectory() as root:
            handle = self._open_log(root)
            handle.close()
            gw._ENGINE_LOGS[root] = handle
            gw._close_engine_log(root)
            self.assertNotIn(root, gw._ENGINE_LOGS)

    def test_stop_closes_the_log_when_the_process_already_exited(self):
        """早退分支（进程自己退了）也必须关句柄 —— 这正是修掉的那个泄漏。"""
        with tempfile.TemporaryDirectory() as root:
            key = gw._root(root)
            proc = mock.Mock()
            proc.poll.return_value = 0          # 已经退出：走早退分支
            handle = self._open_log(root)
            gw._ENGINE_PROCS[key] = proc
            gw._ENGINE_LOGS[key] = handle
            with mock.patch.object(gw, "engine_detach", return_value={}), \
                 mock.patch.object(gw._gpu, "unregister_process", return_value=None), \
                 mock.patch.object(gw._gpu, "release", return_value=None):
                out = gw.engine_stop(root)
            self.assertTrue(out.get("ok"), out)
            self.assertTrue(handle.closed, "早退分支没关日志句柄：又回到「等 GC 才关」")
            self.assertNotIn(key, gw._ENGINE_LOGS)


    def test_restarting_closes_the_previous_log(self):
        """重启/重载同一项目时，被覆盖的旧句柄必须先关。

        不关就等于每次重载漏一个 fd：本机全量里那条
        `unclosed file <_io.FileIO name='…\\engine.log'>` 就是它——只有 engine_stop
        会关，而重启是「新的起来、旧的没人停」。
        """
        with tempfile.TemporaryDirectory() as root:
            first = self._open_log(root)
            gw._ENGINE_LOGS[root] = first
            second = self._open_log(root)
            try:
                gw._register_engine_log(root, second)
                self.assertTrue(first.closed, "被覆盖的旧句柄没关：每重载一次漏一个 fd")
                self.assertIs(gw._ENGINE_LOGS[root], second, "登记的不是新句柄")
            finally:
                # 新句柄也要收：临时目录在 Windows 上删不掉还被占用的文件。
                gw._close_engine_log(root)

    def test_a_failing_close_does_not_escape(self):
        """close() 自己抛异常时也要吞掉并把登记表清干净 —— 否则停止流程会被日志句柄带崩。"""
        handle = mock.Mock()
        handle.close.side_effect = OSError("close failed")
        gw._ENGINE_LOGS["root-x"] = handle
        gw._close_engine_log("root-x")      # 不抛就够
        self.assertNotIn("root-x", gw._ENGINE_LOGS, "抛异常后没摘登记表")


class ProfileReleaseTests(unittest.TestCase):
    def test_release_marks_the_temporary_directory_as_cleaned(self):
        """目录被 rmtree 删掉之后，对象也要收尾 —— 否则 gc 时还会报「Implicitly cleaning up」。

        本机全量里那 49 条 `Implicitly cleaning up <TemporaryDirectory 'D:\\Temp\\docmind-visual-preview-…'>`
        就是这么来的：`_release_profile` 用 rmtree 把目录删了就直接 return，没调 `cleanup()`，
        TemporaryDirectory 的回收器还是一副「没人管我」的样子。
        """
        import gc
        import shutil
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            profile = tempfile.TemporaryDirectory(prefix="docmind-visual-preview-")
            path = profile.name
            shutil.rmtree(path, ignore_errors=True)   # 目录先没了：走「rmtree 成功」那条路径
            self.assertTrue(va._release_profile(profile), "目录已经删了却报没删掉")
            del profile
            gc.collect()
        implicit = [w for w in caught if "Implicitly cleaning up" in str(w.message)]
        self.assertEqual(implicit, [], "对象没被标记为已收尾：gc 时还会报 Implicitly cleaning up")

    def test_release_still_reports_failure_when_the_directory_survives(self):
        """反方向：目录删不掉时必须如实返回 False（调用方靠它判偏差）。"""
        with tempfile.TemporaryDirectory() as root:
            blocked = Path(root) / "docmind-visual-preview-blocked"
            blocked.mkdir()
            (blocked / "keep.txt").write_text("x", encoding="utf-8")

            with mock.patch.object(va.shutil, "rmtree", return_value=None), \
                 mock.patch.object(va.time, "sleep", return_value=None):
                profile = mock.Mock()
                profile.name = str(blocked)
                ok = va._release_profile(profile)
            self.assertFalse(ok, "目录还在却报成功：偏差就被抹掉了")
            self.assertTrue(blocked.is_dir())


class BrowserStderrSinkTests(unittest.TestCase):
    def test_target_is_a_writable_handle_inside_the_profile_dir(self):
        with tempfile.TemporaryDirectory() as root:
            handle = va._browser_stderr_target(["--user-data-dir=%s" % root])
            try:
                self.assertTrue(hasattr(handle, "write"), "应返回可写句柄：%r" % (handle,))
                self.assertTrue(str(handle.name).endswith(va._STDERR_NAME))
            finally:
                handle.close()

    def test_target_falls_back_to_devnull(self):
        self.assertIs(va._browser_stderr_target([]), va.subprocess.DEVNULL)
        self.assertIs(va._browser_stderr_target(["--user-data-dir=/definitely/missing/dir"]),
                      va.subprocess.DEVNULL)

    def test_spawn_closes_the_parent_copy_after_popen(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(va.os, "name", "posix"), \
                 mock.patch.object(va.subprocess, "Popen") as popen:
                popen.return_value = mock.Mock()
                process, job, err = va.spawn_browser_in_job(
                    ["--user-data-dir=%s" % root], creationflags=0)
            self.assertEqual(err, "")
            sink = popen.call_args.kwargs["stderr"]
            self.assertTrue(hasattr(sink, "closed"),
                            "stderr 参数不是文件句柄：这条用例的前提变了（%r）" % (sink,))
            self.assertTrue(sink.closed,
                            "Popen 之后父进程那份句柄没关 —— 每次起浏览器漏一个 fd")


if __name__ == "__main__":
    unittest.main()
