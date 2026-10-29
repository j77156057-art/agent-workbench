# -*- coding: utf-8 -*-
"""跨进程文件锁：给状态/密钥的读-改-写和工作流执行租约用。

进程内 RLock 只能挡住同进程线程；两个服务实例共享同一 STATE_ROOT 时，
读-改-写仍会互相覆盖（实测 3 进程各写 150 键只剩 134 个）。这里用 OS 文件锁
把关键区串行化：Windows 用 msvcrt，POSIX 用 fcntl，锁文件独立于目标文件
（目标文件是 os.replace 原子替换，锁在它本身上会随 inode 一起失效）。
"""
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_LOCAL = threading.local()


def _held() -> dict:
    paths = getattr(_LOCAL, "paths", None)
    if paths is None:
        paths = {}
        _LOCAL.paths = paths
    return paths


def _try_lock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class CrossProcessFileLock:
    """可显式 acquire/release 的文件锁；也支持 with。同一线程可重入由调用方负责。"""

    def __init__(self, target, timeout=None):
        self.path = Path(str(target) + ".lock")
        self.timeout = timeout
        self._handle = None
        self._locked = False

    def acquire(self) -> "CrossProcessFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = None if self.timeout is None else time.monotonic() + self.timeout
            while True:
                try:
                    _try_lock(handle)
                    self._handle = handle
                    self._locked = True
                    return self
                except OSError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("cross-process lock busy: %s" % self.path)
                    time.sleep(0.02)
        except Exception:
            handle.close()
            raise

    def release(self) -> None:
        if not self._locked:
            return
        try:
            _unlock(self._handle)
        finally:
            try:
                self._handle.close()
            finally:
                self._handle = None
                self._locked = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


@contextmanager
def cross_process_lock(target, timeout=None):
    """短临界区用：同一线程对同一目标可重入（外层持锁时内层直接放行）。"""
    lock = CrossProcessFileLock(target, timeout=timeout)
    key = str(lock.path)
    held = _held()
    if key in held:
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    lock.acquire()
    held[key] = 1
    try:
        yield
    finally:
        held.pop(key, None)
        lock.release()
