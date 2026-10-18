"""Small Windows Job Object wrapper for generated adapter processes.

The adapter runtime must fail closed when Windows cannot provide the requested
process boundary. Non-Windows callers get a bounded subprocess fallback so the
contract remains testable in CI.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
from typing import Any, Mapping

try:  # 非 Windows 平台没有 ctypes.wintypes（那里 import 它抛的是 ValueError 不是 ImportError）。
    # 本模块在那些平台只走 subprocess fallback，但**必须能被 import**：限位表要能在 CI 上被测。
    import ctypes.wintypes  # noqa: F401
except (ImportError, ValueError):  # pragma: no cover - 只有非 Windows 才走到
    pass


class SandboxUnavailable(RuntimeError):
    pass


JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

#: 子进程输出的解码方式。
#:
#: 原来是 `text=True` 而**没有 `encoding`** —— 于是按**本机区域编码**解码（本机 cp936/GBK）。
#: 子进程只要吐一个非 GBK 字节，`Popen` 的读线程就抛 `UnicodeDecodeError`，而
#: `communicate()` 把那一路返回成 **`None`**：实测中文、非 UTF-8 字节、甚至"只写 stderr"
#: 都得到 `returncode=0` + `stdout=''` + `stderr=''`。**退出码 0 配空输出**，唯一线索是
#: 一个打印到控制台、调用方永远看不见的后台线程异常。
#:
#: 这里固定成 **utf-8 + errors="replace"**：不匹配��字节变成 U+FFFD 而不是丢掉整条流，
#: 调用方至少拿得到"有内容、里面有乱码"这个可判断的事实。代价是本机真按 GBK 输出的
#: 子进程会看到少量替换字符——但**内容长度与结构仍然可见**，远好过整条流消失。
SUBPROCESS_ENCODING = "utf-8"
SUBPROCESS_DECODE_ERRORS = "replace"

#: 作业对象里允许的**并发**进程数上限。
#:
#: 这里是 2026-10-02 那次门禁事故的根因：原值是裸字面量 ``4``，而门禁整集那条子进程树是
#: ``python -m dev_eval`` → ``python -m ruff`` → ``ruff.exe``（工具自己还会再开一层），
#: 整集里别的题同时还留着并发的后端/浏览器/git 进程。预算花光之后 ``CreateProcess``
#: 直接回 ``WinError 1816``（配额不足），三道门禁题被压红，而 ``diagnostics`` 把它
#: 误报成「ruff 退出码 1 但输出解析不出来」——**退出码 1 的真实内容是一段 traceback**。
#: 门禁整集的实测峰值是 **7**（``.tmp`` 探针按 ``--user-data-dir`` 无关的作业对象
#: ``QueryInformationJobObject`` 采样，105 次采样 0 次失败）；32 是它的 4 倍余量。
#:
#: 为什么不是无界：真正的资源上限是 ``JOB_OBJECT_LIMIT_JOB_MEMORY``（1 GiB），
#: ``ACTIVE_PROCESS`` 是**第二道**刹车——没有它，一个坏循环能在触到内存上限之前
#: 先把整台机器的进程表铺满。
#:
#: 为什么抬高它不削弱 99°C 漏浏览器那道修法：真正回收整棵树的是
#: ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``，与进程数无关；浏览器路径至今也**只**设这一个
#: flag（``visual_acceptance._make_job``），也就是说"只靠 kill-on-close"在本仓已经是
#: 交付过一次事故的写法。
ACTIVE_PROCESS_LIMIT = 32


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTime", ctypes.c_longlong),
                ("PerJobUserTime", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_ulong),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_ulong),
                ("SchedulingClass", ctypes.c_ulong)]


class _EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _win32_error(prefix: str) -> SandboxUnavailable:
    code = ctypes.get_last_error() or 0
    return SandboxUnavailable("%s（Win32=%s）" % (prefix, code))


def job_limits(memory_bytes: int) -> _EXTENDED_LIMIT_INFORMATION:
    """这张作业对象的限位表长什么样——抽成纯函数，好让"限位是怎么摆的"能被测到。

    内联在 `_create_job` 里的时候，非 Windows 上根本没有任何函数能读到它，于是
    `KILL_ON_JOB_CLOSE` 掉了、`ActiveProcessLimit` 写回 4 了，谁都不会红。
    """
    limits = _EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PROCESS_MEMORY |
        JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION |
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
    limits.BasicLimitInformation.ActiveProcessLimit = ACTIVE_PROCESS_LIMIT
    limits.ProcessMemoryLimit = int(memory_bytes)
    limits.JobMemoryLimit = int(memory_bytes)
    return limits


def _create_job(memory_bytes: int):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    kernel32.SetInformationJobObject.restype = ctypes.wintypes.BOOL if hasattr(ctypes, "wintypes") else ctypes.c_int
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise _win32_error("无法创建适配器 Job Object")
    limits = job_limits(memory_bytes)
    ok = kernel32.SetInformationJobObject(handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                          ctypes.byref(limits), ctypes.sizeof(limits))
    if not ok:
        kernel32.CloseHandle(handle)
        raise _win32_error("无法设置适配器 Job Object 限制")
    return kernel32, handle


def run_isolated(command: list[str], *, cwd: str, env: Mapping[str, str], timeout: int,
                 memory_bytes: int = 512 * 1024 * 1024):
    """Run one process under a bounded Job Object and return CompletedProcess."""
    # BUG-10：本函数按 utf-8+errors=replace 解码子进程输出，但子进程（stdout 是管道时）
    # 默认用本机区域编码（中文 Windows=GBK）写中文 → GBK 字节被当非法 UTF-8 替换成 ◆。
    # 这里给子进程强制 PYTHONIOENCODING=utf-8，让输出字节与父端解码对齐。
    env = {**env, "PYTHONIOENCODING": "utf-8"}
    if os.name != "nt":
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                              encoding=SUBPROCESS_ENCODING,
                              errors=SUBPROCESS_DECODE_ERRORS,
                              timeout=timeout, env=dict(env))
    kernel32, job = _create_job(memory_bytes)
    process = None
    try:
        # CPython does not expose the primary thread handle needed to resume a
        # CREATE_SUSPENDED process. Start immediately, then assign the process
        # to the Job Object before any adapter output is consumed; failures are
        # terminated and never fall back to an unbounded run.
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding=SUBPROCESS_ENCODING,
                                   errors=SUBPROCESS_DECODE_ERRORS, env=dict(env),
                                   creationflags=CREATE_NO_WINDOW)
        process_handle = ctypes.c_void_p(int(getattr(process, "_handle", 0)))
        if not process_handle.value or not kernel32.AssignProcessToJobObject(job, process_handle):
            raise _win32_error("无法将适配器加入 Job Object")
        try:
            stdout, stderr = process.communicate(timeout=max(1, int(timeout)))
        except subprocess.TimeoutExpired:
            kernel32.TerminateJobObject(job, 124)
            process.kill()
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process is not None and process.poll() is None:
            try:
                kernel32.TerminateJobObject(job, 125)
            except Exception:
                pass
            try:
                process.kill()
            except Exception:
                pass
        kernel32.CloseHandle(job)


def spawn_isolated(command: list[str], *, cwd: str, env: Mapping[str, str],
                   memory_bytes: int = 1024 * 1024 * 1024):
    """Start a command under a bounded Job Object for background supervision.

    Returns ``(popen, kernel32, job_handle)`` so the caller can terminate the
    whole process tree later. Non-Windows callers get a session-led subprocess
    with ``(popen, None, None)`` so the same supervision code stays testable.
    """
    if os.name != "nt":
        popen = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=False, env=dict(env), start_new_session=True)
        return popen, None, None
    kernel32, job = _create_job(memory_bytes)
    popen = None
    try:
        popen = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=False, env=dict(env),
                                 creationflags=CREATE_NO_WINDOW)
        process_handle = ctypes.c_void_p(int(getattr(popen, "_handle", 0)))
        if not process_handle.value or not kernel32.AssignProcessToJobObject(job, process_handle):
            raise _win32_error("无法将进程加入 Job Object")
        return popen, kernel32, job
    except BaseException:
        if popen is not None and popen.poll() is None:
            try:
                popen.kill()
            except Exception:
                pass
        kernel32.CloseHandle(job)
        raise


def terminate_isolated(kernel32, job_handle, *, exit_code: int = 124) -> None:
    """Kill every process still attached to a Job Object."""
    if kernel32 is None or job_handle is None:
        return
    try:
        kernel32.TerminateJobObject(job_handle, int(exit_code))
    finally:
        try:
            kernel32.CloseHandle(job_handle)
        except Exception:
            pass


__all__ = ["SandboxUnavailable", "run_isolated", "spawn_isolated", "terminate_isolated",
           "job_limits", "ACTIVE_PROCESS_LIMIT"]
