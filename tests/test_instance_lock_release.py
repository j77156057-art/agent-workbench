"""`api._release_instance_locks`：进程退出时释放存活锁。

锁的语义是「这个 API 进程还活着」；正常退出就该放手。不释放不会坏（OS 会回收），但句柄
要等 gc 才关 —— 本机全量里那 3 条 `unclosed file <_io.BufferedRandom name='…instances…'>`
就是它。这两条用例直接调用该函数（含「释放抛异常」的兜底分支），所以它不是死代码。
"""

import os
import tempfile
import unittest
from unittest import mock

import api
from file_locks import CrossProcessFileLock


class ReleaseInstanceLocksTests(unittest.TestCase):
    def setUp(self):
        api._INSTANCE_LOCKS.clear()

    def tearDown(self):
        api._INSTANCE_LOCKS.clear()

    def test_releases_every_lock_and_clears_the_registry(self):
        with tempfile.TemporaryDirectory() as root:
            lock = CrossProcessFileLock(os.path.join(root, "instance.lock"), timeout=1.0)
            lock.acquire()
            api._INSTANCE_LOCKS["root-x"] = lock
            api._release_instance_locks()
            self.assertEqual(api._INSTANCE_LOCKS, {}, "登记表没清")
            self.assertIsNone(lock._handle, "句柄没关：退出时又要等 gc")
            self.assertFalse(lock._locked)

    def test_a_failing_release_is_swallowed(self):
        bad = mock.Mock()
        bad.release.side_effect = OSError("release failed")
        api._INSTANCE_LOCKS["root-bad"] = bad
        api._release_instance_locks()                   # 不抛就够
        self.assertEqual(api._INSTANCE_LOCKS, {}, "抛异常后没清登记表")


if __name__ == "__main__":
    unittest.main()
