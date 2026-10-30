# -*- coding: utf-8 -*-
"""同一会话的跨实例 run 租约：第二个实例不能再跑同一会话。

实测：3 进程对同一 session 各读-改-写 50 轮，150 轮只剩 37。会话文件写入
只能串行化文件替换，挡不住两份过期快照互相覆盖；真正的并发写由 API 层
`_session_run_scope` 的跨进程租约挡在入口。
"""
import os
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402
from file_locks import CrossProcessFileLock  # noqa: E402


class SessionRunLeaseTests(unittest.TestCase):
    def test_second_holder_is_rejected(self):
        key = "session-lease-unit-%d" % os.getpid()
        held = CrossProcessFileLock(api._session_lease_path(key), timeout=0.5)
        held.acquire()
        try:
            with self.assertRaises(api.SessionBusyError):
                with api._session_run_scope(key, threading.Event(),
                                            threading.current_thread()):
                    pass
        finally:
            held.release()

    def test_stuck_previous_run_times_out_with_clear_error(self):
        key = "session-join-timeout-%d" % os.getpid()
        release = threading.Event()

        def stuck():
            release.wait(3)

        previous = threading.Thread(target=stuck, daemon=True)
        previous.start()
        api._ACTIVE_RUN[key] = {"stop": threading.Event(), "thread": previous}
        old_timeout = api._SESSION_JOIN_TIMEOUT_S
        api._SESSION_JOIN_TIMEOUT_S = 0.2
        try:
            with self.assertRaises(api.SessionStillRunningError):
                with api._session_run_scope(key, threading.Event(),
                                            threading.current_thread()):
                    pass
        finally:
            api._SESSION_JOIN_TIMEOUT_S = old_timeout
            api._ACTIVE_RUN.pop(key, None)
            release.set()
            previous.join(2)

    def test_run_lock_is_evicted_after_scope(self):
        key = "session-lock-evict-%d" % os.getpid()
        with api._session_run_scope(key, threading.Event(), threading.current_thread()):
            self.assertIn(key, api._RUN_LOCKS)
            self.assertEqual(api._RUN_LOCK_REFS.get(key), 1)
        self.assertNotIn(key, api._RUN_LOCKS)
        self.assertNotIn(key, api._RUN_LOCK_REFS)

    def test_busy_error_has_a_user_facing_message(self):
        source = Path(api.__file__).read_text(encoding="utf-8")
        self.assertIn("另一个 Agent 工作台实例", source)
        self.assertIn("上一轮仍在执行", source)


if __name__ == "__main__":
    unittest.main()
