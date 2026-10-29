# -*- coding: utf-8 -*-
"""状态 / 密钥库的并发写不丢键。

`save_state` 与 `secrets_store.save` 都是「读整个 JSON → 改一个键 → 写回」。
SSE 工作线程、设置端点、项目切换会并发调用；没有锁时两次写会交错读同一份旧
数据，后写的那次把先写的键整块抹掉。这里用多线程屏障把窗口撑到最大。
"""
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import secrets_store  # noqa: E402


class StateWriteConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="docmind_state_concurrency_")
        self._old_root = config.STATE_ROOT
        self._old_file = config.STATE_FILE
        config.STATE_ROOT = self._tmp.name
        config.STATE_FILE = os.path.join(self._tmp.name, ".docmind_state.json")

    def tearDown(self):
        config.STATE_ROOT = self._old_root
        config.STATE_FILE = self._old_file
        self._tmp.cleanup()

    def test_concurrent_state_saves_do_not_lose_keys(self):
        threads_n, keys_n = 12, 8
        barrier = threading.Barrier(threads_n)

        def worker(i):
            barrier.wait()
            for j in range(keys_n):
                config.save_state("k%d_%d" % (i, j), j)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        data = json.loads(open(config.STATE_FILE, encoding="utf-8").read())
        missing = [(i, j) for i in range(threads_n) for j in range(keys_n)
                   if data.get("k%d_%d" % (i, j)) != j]
        self.assertEqual(missing, [], "并发写丢键：%r" % (missing[:5],))

    def test_concurrent_secret_saves_do_not_lose_keys(self):
        threads_n = 8
        root = os.path.join(self._tmp.name, "project")
        os.makedirs(root, exist_ok=True)
        barrier = threading.Barrier(threads_n)

        def worker(i):
            barrier.wait()
            secrets_store.save(root, "provider%d" % i, "sk-%d" % i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        missing = [i for i in range(threads_n)
                   if secrets_store.load(root, "provider%d" % i) != "sk-%d" % i]
        self.assertEqual(missing, [], "并发写密钥库丢键：%r" % missing)


if __name__ == "__main__":
    unittest.main()
