"""`windows_sandbox._reap_quietly`：拒绝执行路径上的子进程收尾。

来历：`run_isolated` / `spawn_isolated` 在 `AssignProcessToJobObject` 失败后只 `kill()`
—— 不等、不关管道。本机全量里 `subprocess … is still running` 与成对出现的
`unclosed file <_io.TextIOWrapper name=…>` 就是这条路径留下的。收尾逻辑抽成一个函数，
每条分支都用 mock 直接击穿（包括「kill 抛」「wait 抛」「close 抛」三种兜底）。
"""

import unittest
from unittest import mock

from agent_runtime import windows_sandbox as ws


def _proc(*, poll=None, kill_raises=False, wait_raises=False):
    proc = mock.Mock()
    proc.poll.return_value = poll
    if kill_raises:
        proc.kill.side_effect = OSError("kill failed")
    if wait_raises:
        proc.wait.side_effect = OSError("wait failed")
    return proc


class ReapQuietlyTests(unittest.TestCase):
    def test_none_is_a_no_op(self):
        ws._reap_quietly(None)                      # 不抛就够

    def test_a_live_process_is_killed_waited_and_its_pipes_closed(self):
        proc = _proc(poll=None)
        ws._reap_quietly(proc)
        proc.kill.assert_called_once_with()
        proc.wait.assert_called_once()
        for name in ("stdin", "stdout", "stderr"):
            getattr(proc, name).close.assert_called_once_with()

    def test_an_already_exited_process_is_only_reaped(self):
        proc = _proc(poll=0)
        ws._reap_quietly(proc)
        proc.kill.assert_not_called()
        proc.wait.assert_called_once()

    def test_failures_are_swallowed(self):
        proc = _proc(poll=None, kill_raises=True, wait_raises=True)
        proc.stdout.close.side_effect = OSError("close failed")
        ws._reap_quietly(proc)                      # 不抛就够：收尾失败不该把调用方带崩

    def test_a_process_without_pipes_is_fine(self):
        proc = mock.Mock()
        proc.poll.return_value = 0
        proc.stdin = proc.stdout = proc.stderr = None
        ws._reap_quietly(proc)
        proc.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
