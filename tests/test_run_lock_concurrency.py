# -*- coding: utf-8 -*-
"""同会话 run 锁的惰性创建必须原子。

`_run_lock_for` 原先「get -> 没有 -> new -> set」分三步；两个并发请求可能各自
拿到一把不同的锁，同会话串行化（interrupt-and-redirect）失效，两轮 run 会并发
写同一个 Agent 的 history。
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402


class RunLockStabilityTests(unittest.TestCase):
    def tearDown(self):
        for key in list(api._RUN_LOCKS):
            if key.startswith("lock-test-"):
                api._RUN_LOCKS.pop(key, None)

    def test_concurrent_callers_share_one_lock(self):
        for round_no in range(20):
            key = "lock-test-%d" % round_no
            workers = 24
            barrier = threading.Barrier(workers)
            locks = []
            guard = threading.Lock()

            def worker():
                barrier.wait()
                lock = api._run_lock_for(key)
                with guard:
                    locks.append(lock)

            threads = [threading.Thread(target=worker) for _ in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len({id(lock) for lock in locks}), 1,
                             "同一个 key 在并发下拿到了多把锁（%d）" % round_no)

    def test_different_keys_get_different_locks(self):
        self.assertIsNot(api._run_lock_for("lock-test-a"), api._run_lock_for("lock-test-b"))


if __name__ == "__main__":
    unittest.main()
