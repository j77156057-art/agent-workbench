"""Browser backed visual acceptance for the autonomous development cockpit.

The production workflow uses this small adapter for ordinary web previews.  It
serves only the current project directory, captures a real browser frame, and
returns the image through the normal tool vision channel.  Domain specific
engines can register richer adapters later; a missing browser or unsupported
entry point is reported as an explicit verification failure.
"""
from __future__ import annotations

import base64
import contextlib
import ctypes
import functools
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .dev_server import loopback_url
import temp_state
import urllib.request


class VisualAcceptanceError(RuntimeError):
    """A visual capture could not produce trustworthy evidence."""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        # The browser always probes /favicon.ico; a 404 there would be reported
        # as a real page failure, so answer it with an empty success instead.
        if self.path.split("?")[0].rstrip("/").lower().endswith("/favicon.ico"):
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass


def _edge_binary() -> str:
    configured = str(os.getenv("DOCMIND_EDGE_BIN") or "").strip()
    candidates = [configured] if configured else []
    candidates.extend([
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("msedge") or "",
        shutil.which("microsoft-edge") or "",
    ])
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    raise VisualAcceptanceError(
        "未找到 Edge/Chromium 浏览器，无法进行真实画面验收；可配置 DOCMIND_EDGE_BIN。"
    )


_BASE_FLAGS = ("--headless=new", "--disable-gpu", "--no-first-run",
               "--no-default-browser-check", "--remote-debugging-port=0",
               "--remote-allow-origins=*")
_MAX_EXTRA_FLAGS = 32
_PROFILE_PREFIX = "docmind-visual-preview-"


class _JobBasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


_KERNEL32 = None


def _kernel32():
    """取一个只归本模块用的 kernel32，并把原型声明齐。

    两个坑：① 不写 restype/argtypes 时 ctypes 按 C int 传参，x64 上句柄会被截成 32 位，
    表现为赋值偶发失败，而失败又被下面那个 best-effort 分支吃掉，比直接崩更难查。
    ② 必须用 `WinDLL` 新建实例，不能拿 `ctypes.windll.kernel32`——后者是进程级共享缓存，
    在它上面设 argtypes 等于改掉同进程里所有其他调用方看到的原型。
    """
    global _KERNEL32
    if _KERNEL32 is None:
        # use_last_error=True：CreateProcessW / UpdateProcThreadAttribute 失败时要把
        # GetLastError 报出来，否则"绑定失败"和"平台不适用"就长得一模一样。
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                                    ctypes.c_void_p, ctypes.c_ulong)
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.TerminateProcess.restype = ctypes.c_int
        kernel32.IsProcessInJob.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
        kernel32.IsProcessInJob.restype = ctypes.c_int
        kernel32.InitializeProcThreadAttributeList.argtypes = (
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_size_t))
        kernel32.InitializeProcThreadAttributeList.restype = ctypes.c_int
        kernel32.UpdateProcThreadAttribute.argtypes = (
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
        kernel32.UpdateProcThreadAttribute.restype = ctypes.c_int
        kernel32.DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
        kernel32.CreateProcessW.argtypes = (
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_wchar_p,
            ctypes.c_void_p, ctypes.c_void_p)
        kernel32.CreateProcessW.restype = ctypes.c_int
        _KERNEL32 = kernel32
    return _KERNEL32


_EXT_FLAG = 0x00080000                    # EXTENDED_STARTUPINFO_PRESENT：漏了它属性列表被静默忽略
_JOB_LIST_ATTRIBUTE = 0x0002000D          # PROC_THREAD_ATTRIBUTE_JOB_LIST
_WAIT_OBJECT_0 = 0x0
_WAIT_TIMEOUT = 0x102


class _StartupInfoW(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("lpReserved", ctypes.c_wchar_p),
                ("lpDesktop", ctypes.c_wchar_p), ("lpTitle", ctypes.c_wchar_p),
                ("dwX", ctypes.c_uint32), ("dwY", ctypes.c_uint32),
                ("dwXSize", ctypes.c_uint32), ("dwYSize", ctypes.c_uint32),
                ("dwXCountChars", ctypes.c_uint32), ("dwYCountChars", ctypes.c_uint32),
                ("dwFillAttribute", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", ctypes.c_void_p), ("hStdOutput", ctypes.c_void_p),
                ("hStdError", ctypes.c_void_p)]


class _StartupInfoExW(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfoW), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
                ("dwProcessId", ctypes.c_uint32), ("dwThreadId", ctypes.c_uint32)]


class BrowserSpawnError(VisualAcceptanceError):
    """浏览器进程没能被创建在作业对象里——此时不存在任何回收保证，必须响亮失败。"""


class _BrowserProcess:
    """`CreateProcessW` 起浏览器后交回的句柄对象，只暴露调用点真正用到的三个动作。

    形状刻意贴着 `subprocess.Popen`（`pid` / `poll()` / `wait(timeout=)` / `kill()`），
    这样既有调用方与打 `wait` 补丁的测试不用改。
    """

    def __init__(self, pid: int, handle: Any):
        self.pid = pid
        self._handle = handle
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        kernel32 = _kernel32()
        if kernel32.WaitForSingleObject(self._handle, 0) != _WAIT_OBJECT_0:
            return None
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code))
        self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        millis = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        kernel32 = _kernel32()
        if kernel32.WaitForSingleObject(self._handle, millis) == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(cmd="browser", timeout=timeout)
        return self.poll() or 0

    def kill(self) -> None:
        _kernel32().TerminateProcess(self._handle, 1)


def _make_job() -> Any:
    kernel32 = _kernel32()
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return None
    limits = _JobExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel32.CloseHandle(handle)
        return None
    return handle


def _in_job(handle: Any, job: Any) -> bool:
    flag = ctypes.c_int(0)
    if not _kernel32().IsProcessInJob(handle, job, ctypes.byref(flag)):
        return False
    return bool(flag.value)


def spawn_browser_in_job(argv: list[str], creationflags: int = 0):
    """让浏览器【一出生】就在 kill-on-close 作业对象里。

    返回 `(process, job, error)`。原先的实现是 `Popen` 之后再 `AssignProcessToJobObject`，
    实测超过约 150ms 就永久失败（`ERROR_ACCESS_DENIED`，Chromium 已建好自己的作业对象），
    而失败被当成「平台不适用」静默吞掉——CPU 饱和时（也就是 99°C 那次）必输。用
    `PROC_THREAD_ATTRIBUTE_JOB_LIST` 让进程出生即在作业对象里，窗口归零。

    非 Windows 回退到 `subprocess.Popen`，job 为 None：那边本来就没有作业对象可用。
    """
    if os.name != "nt":
        process = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=creationflags)
        return process, None, ""
    kernel32 = _kernel32()
    job = _make_job()
    if not job:
        return None, None, "CreateJobObjectW/SetInformationJobObject failed gle=%d" % ctypes.get_last_error()
    size = ctypes.c_size_t(0)
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attribute = (ctypes.c_byte * size.value)()
    if not kernel32.InitializeProcThreadAttributeList(attribute, 1, 0, ctypes.byref(size)):
        kernel32.CloseHandle(job)
        return None, None, "InitializeProcThreadAttributeList failed gle=%d" % ctypes.get_last_error()
    jobs = (ctypes.c_void_p * 1)(job)
    if not kernel32.UpdateProcThreadAttribute(attribute, 0, _JOB_LIST_ATTRIBUTE,
                                              jobs, ctypes.sizeof(jobs), None, None):
        kernel32.DeleteProcThreadAttributeList(attribute)
        kernel32.CloseHandle(job)
        return None, None, "UpdateProcThreadAttribute failed gle=%d" % ctypes.get_last_error()
    info = _StartupInfoExW()
    info.StartupInfo.cb = ctypes.sizeof(_StartupInfoExW)
    info.lpAttributeList = ctypes.cast(attribute, ctypes.c_void_p)
    out = _ProcessInformation()
    cmdline = subprocess.list2cmdline(argv)
    ok = kernel32.CreateProcessW(None, cmdline, None, None, False,
                                 creationflags | _EXT_FLAG, None, None,
                                 ctypes.byref(info), ctypes.byref(out))
    error = "" if ok else "CreateProcessW failed gle=%d" % ctypes.get_last_error()
    kernel32.DeleteProcThreadAttributeList(attribute)
    if not ok:
        kernel32.CloseHandle(job)
        return None, None, error
    kernel32.CloseHandle(out.hThread)
    process = _BrowserProcess(int(out.dwProcessId), out.hProcess)
    # 出生即在内是这套机制唯一的不变量；漏掉 EXTENDED_STARTUPINFO_PRESENT 时
    # CreateProcessW 会「成功」但属性被整个忽略，所以这里必须当场断言。
    if not _in_job(out.hProcess, job):
        process.kill()
        kernel32.CloseHandle(out.hProcess)
        kernel32.CloseHandle(job)
        return None, None, "browser was not born inside the job object"
    return process, job, ""


def _close_reaper(job: Any) -> bool:
    """关掉作业句柄 = 让操作系统收走整棵树。返回是否真的关上了。

    实测这一步是同步的：关句柄后的下一条语句就用 ctypes 逐个查已知 pid，16 个全部已没。
    （曾经加过 `TerminateJobObject`，理由是「只关句柄要等 0.2~2.5 秒」——那是用
    PowerShell 计数测出来的开销，不是异步延迟；去掉后微秒级探针仍然测不出差别，所以撤掉，
    不给不会发生的场景留兜底。）作业句柄不可继承，没有第二个句柄会把树留住。
    """
    if job is None:
        return False
    try:
        return bool(_kernel32().CloseHandle(job))
    except OSError:
        return False


def _release_profile(profile) -> bool:
    """删掉 profile，扛住 Edge 退出前那段文件锁；返回是否真的删干净了。

    正常退出路径实测也会留下目录：`process.wait(3)` 一超时我们就开始删，而 Edge 还锁着
    自己的文件。`ignore_cleanup_errors=True` 只是不抛异常，不等于删掉了。必须自己带
    退避重试 —— `TemporaryDirectory.cleanup()` 本身不会等，试一次失败就交差（实测 3.13
    上它可以再调，但没有任何等待，所以拿它当重试等于原地空转）。删不净的由
    `sweep_stale_profiles` 按归属 PID 兜底。
    """
    path = profile if isinstance(profile, str) else profile.name
    for _ in range(20):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.isdir(path):
            return True
        time.sleep(0.15)
    cleanup = getattr(profile, "cleanup", None)
    if cleanup is not None:
        cleanup()
    return not os.path.isdir(path)


# 存活判断只在 `temp_state` 里实现一份：本模块的 profile 回收和它的测试状态回收面对的是
# 同一个问题（归属 PID 还在不在），两份实现迟早会漂移——这次评审就抓到过同包内两套作业
# 对象实现漂移的先例。
_pid_alive = temp_state.pid_alive


def _claim_profile(path: str) -> None:
    """把归属 PID 写进 profile，作为 sweep 唯一认账的「这个目录还有人用」证据。"""
    temp_state.claim(path)


#: 端口文件里第一行是 CDP 端口。Windows 上 Edge 建好这个文件之后还会**短暂独占**它，
#: 于是「文件已存在」与「文件可读」之间有一个窗口 —— 只等 `exists()` 就去读会拿到
#: `[Errno 13] Permission denied`。实测整页守卫 4 次里撞到 1 次。
#: 所以就绪判据必须是「**读得出第一行**」，读操作本身也要重试。
_ACTIVE_PORT_NAME = "DevToolsActivePort"

_ACTIVE_PORT_TIMEOUT_HINT = (
    "浏览器调试端口未就绪：%s 在 %.1f 秒内没能读出 CDP 端口。"
    "浏览器可能仍在启动，或被别的进程占着这个 profile 目录。"
)


#: 页面「加载完」的判据是 `document.readyState === 'complete'`。它此前在三个模块里
#: 各抄了一份**写死**的 `for(let n=0;n<120;n++) ... setTimeout(50)` —— 固定 6 秒，
#: 且与调用方传进来的 `timeout` **完全脱钩**（`timeout=180` 也不会多等一毫秒）。
#:
#: 实测代价：整页守卫要加载 bundle + vendor chunk，再拉 `GET /api/config`，而那条冷路径
#: 要跑一次 Ollama 探活（`api.py` 的 `check_ollama` 注释自述**首次 6108 毫秒、之后
#: 15 毫秒**，2s TTL 缓存）。6 秒装不下，于是间歇性 `页面加载超时`。
#: `app_test._open_with_retry` 已在用「重试 3 次 + 退避 2s/4s」绕它，而那段注释自己
#: 写着这招「把一次失败放大成三次连续抢资源」。
#:
#: 现在预算跟着 `timeout` 走，但**有上限**：单页加载不该独占整档预算（真挂死时要能
#: 退出来）。上限覆盖实测最坏冷路径 5 倍余量。
PAGE_READY_BUDGET_CAP = 30.0

#: 就绪轮询的步长（毫秒）。预算按它换算成次数，保持原来的 50ms 粒度。
_PAGE_READY_STEP_MS = 50

#: 就绪脚本模板。`budget_ms` 必须由 `PAGE_READY_BUDGET_CAP` 与调用方的 `timeout` 派生，
#: 不许再出现字面量次数 —— `tests/test_app_test_diagnosability.py` 钉住这一点。
PAGE_READY_JS = (
    "(async()=>{const d=%(budget_ms)d,s=%(step_ms)d;"
    "for(let t=0;t<d;t+=s){if(document.readyState==='complete')return true;"
    "await new Promise(r=>setTimeout(r,s))}throw Error('页面加载超时')})()"
)


def page_ready_budget(timeout: float) -> float:
    """单页「加载完」要等多久：跟着 `timeout` 走，但不超过 `PAGE_READY_BUDGET_CAP`。"""
    return max(0.0, min(float(timeout), PAGE_READY_BUDGET_CAP))


def page_ready_js(timeout: float) -> str:
    """按 `timeout` 派生就绪脚本 —— 三个模块共用这一份，别再各抄一遍。"""
    return PAGE_READY_JS % {"budget_ms": int(page_ready_budget(timeout) * 1000),
                            "step_ms": _PAGE_READY_STEP_MS}


def _read_active_port(path: Path, *, timeout: float = 15.0,
                      interval: float = 0.05) -> tuple[str, str]:
    """读 `DevToolsActivePort`，返回 ``(端口, 浏览器 ws 路径)``。

    重试到预算用完为止：把「文件出现了但还读不了」也当成「还没就绪」。此前这里只等
    `exists()` 然后裸读一次，于是 Windows 上 Edge 独占该文件的窗口直接变成
    `PermissionError`，而那句报错把人引去查文件系统（该查的是浏览器为什么没起来）。

    顺带守住一个更隐蔽的形状：**空文件**也满足 `exists()`，而 `splitlines()[0]`
    对它是 `IndexError` —— 所以就绪判据里必须包含「有首行」。
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    last: Exception | None = None
    while True:
        try:
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            if lines and lines[0].strip():
                return lines[0].strip(), (lines[1].strip() if len(lines) > 1 else "")
            last = ValueError("端口文件还没有内容")
        except (OSError, ValueError, IndexError) as exc:
            # OSError 覆盖 FileNotFoundError（还没建）与 PermissionError（被独占）
            last = exc
        if time.monotonic() >= deadline:
            raise VisualAcceptanceError(
                _ACTIVE_PORT_TIMEOUT_HINT % (path, max(0.0, float(timeout)))) from last
        time.sleep(max(0.0, float(interval)))


def _browser_still_listening(profile_dir: str | Path) -> bool:
    """这个 profile 对应的浏览器是否还活着 —— 直接证据，不靠归属标记推断。

    headless Edge 会把 CDP 端口写进 `DevToolsActivePort` 第一行。端口能连上就是在用；
    连不上说明浏览器已经没了（端口被别的进程复用只会让我们保守地跳过删除，方向是安全的）。
    读不到文件也当作活着：改动前启动的会话没有 `.docmind-owner`，光靠 PID 判据永远回收不了。
    """
    try:
        line = (Path(profile_dir) / "DevToolsActivePort").read_text(encoding="utf-8")
        port = int(line.splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return True
    if not 0 < port < 65536:
        return True
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def sweep_stale_profiles(directory: str | Path) -> list[str]:
    """回收【浏览器已经没了】的 profile 目录，返回真正删掉的路径。

    这里曾经用「目录 mtime 超过 6 小时」当判据，是错的：Windows 上往已存在的子目录反复
    写文件并不会推进父目录的 mtime，而 Edge 平时正是往 Default\\Cache\\ 里写。实测一个
    **正在使用**的 profile 被这样判定为「7 小时没动过」，222 个文件被删到 95 个，而函数
    返回空列表——报「没什么可清理」的同时损坏了别人正在跑的会话，是最难归因的一类故障。

    现在两条例据都要成立才动手：归属 PID 已经没了（或压根没有标记），且 CDP 端口已经连不上。
    任一条判断不了就跳过 —— 宁可漏下几十 MB，不可删掉别人正在用的浏览器。
    """
    parent = Path(str(directory or ""))
    removed: list[str] = []
    try:
        entries = list(os.scandir(parent))
    except OSError:
        return removed
    for entry in entries:
        if not entry.name.startswith(_PROFILE_PREFIX):
            continue
        try:
            if not entry.is_dir():
                continue
            pid = temp_state.owner_of(Path(entry.path))   # None = 没标记：改动前的会话或别人建的
            if pid is not None and _pid_alive(pid):
                continue
            if _browser_still_listening(entry.path):
                continue
            shutil.rmtree(entry.path, ignore_errors=True)
            # 用 path 而不是 entry 复查：DirEntry 会缓存 stat，删完再问它仍然说「在」。
            if not os.path.isdir(entry.path):
                removed.append(entry.path)
        except OSError:
            continue
    return removed


def sanitize_flags(flags: Any) -> list[str]:
    """浏览器启动参数只接受 `--开关` / `--开关=值` 形态。

    参数会以 argv 直接交给浏览器（不过 shell），但一个能自由写 argv 的入口仍然是
    注入面：`--user-data-dir`、`--load-extension` 这类能改变浏览器行为与落盘位置的
    开关一律拒绝，长度与非空白字符也限死。
    """
    denied = ("--user-data-dir", "--load-extension", "--disable-extensions-except",
              "--extension-process", "--profiler-output", "--inspect", "--remote-debugging-pipe")
    accepted: list[str] = []
    for item in flags or ():
        text = str(item).strip()
        if not text:
            continue
        if not text.startswith("--") or len(text) > 1_000:
            raise VisualAcceptanceError("浏览器启动参数必须是 --开关 形态且不能过长：%r" % text[:60])
        if any(ch in text for ch in ("\n", "\r", "\x00")):
            raise VisualAcceptanceError("浏览器启动参数含有非法字符：%r" % text[:60])
        name = text.partition("=")[0]
        # Chromium 在 Windows 上的开关名不区分大小写，所以黑名单必须按小写比：
        # 只比原文的话 `--User-Data-Dir=...` 就能绕出去改写浏览器的数据目录。
        lowered = name.lower()
        if any(lowered.startswith(blocked) for blocked in denied):
            raise VisualAcceptanceError("浏览器启动参数不允许：%s" % name)
        accepted.append(text)
    if len(accepted) > _MAX_EXTRA_FLAGS:
        raise VisualAcceptanceError("浏览器启动参数最多 %s 条。" % _MAX_EXTRA_FLAGS)
    return accepted


def serve_static(root: str | Path) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """把一个目录临时当静态站服务（只绑回环、随机端口）；调用方负责 `stop_static`。"""
    path = Path(root).resolve()
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 functools.partial(_QuietHandler, directory=str(path)))
    thread = threading.Thread(target=server.serve_forever, name="visual-preview-http",
                              daemon=True)
    thread.start()
    return server, thread


def stop_static(server: Any, thread: Any) -> None:
    if server is None:
        return
    server.shutdown()
    server.server_close()
    if thread is not None:
        thread.join(timeout=2)


@contextlib.contextmanager
def browser_session(timeout: float = 25.0, extra_flags: Any = (),
                    cleanup: dict | None = None):
    """起一个带 CDP 的真实浏览器，交回 `_DevTools`；退出时进程与 profile 一定收干净。

    拆出来是因为媒体夹具需要在同一批启动参数上追加假设备开关——两条启动路径各自演化
    迟早会出现「预览能过、媒体过不了」这种无法解释的差集。

    **新建任何"要看一眼界面"的脚本都必须走这里，不要引 playwright，也不要自己 Popen
    浏览器。** 浏览器由 `spawn_browser_in_job` 创建在 kill-on-close 作业对象里（出生即在，
    没有事后补绑的窗口）；父进程被硬杀时由操作系统收走整棵树。传 `cleanup` 字典可以拿到
    收尾各步的成败，调用方据此决定要不要把偏离报给操作者。
    """
    edge = _edge_binary()
    try:
        import websocket
    except Exception as exc:  # pragma: no cover - depends on desktop bundle
        raise VisualAcceptanceError("浏览器控制依赖 websocket-client 未安装。") from exc
    flags = sanitize_flags(extra_flags)
    sweep_stale_profiles(tempfile.gettempdir())
    profile = tempfile.TemporaryDirectory(prefix=_PROFILE_PREFIX,
                                          ignore_cleanup_errors=True)
    _claim_profile(profile.name)
    process = None
    connection = None
    job = None
    try:
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process, job, spawn_error = spawn_browser_in_job(
            [edge, *_BASE_FLAGS, *flags, "--user-data-dir=%s" % profile.name, "about:blank"],
            creation)
        if process is None:
            raise BrowserSpawnError("浏览器未能创建在回收作业对象里：%s" % spawn_error)
        active = Path(profile.name) / _ACTIVE_PORT_NAME
        port, _ws_path = _read_active_port(
            active, timeout=min(15.0, max(3.0, float(timeout) / 2)))
        with urllib.request.urlopen("http://127.0.0.1:%s/json" % port, timeout=5) as response:
            tabs = json.load(response)
        page = next((row for row in tabs if row.get("type") == "page"), None)
        if not page or not page.get("webSocketDebuggerUrl"):
            raise VisualAcceptanceError("浏览器没有可用页面标签。")
        connection = websocket.create_connection(page["webSocketDebuggerUrl"],
                                                 timeout=max(5.0, float(timeout)),
                                                 suppress_origin=True)
        devtools = _DevTools(connection, recv_timeout=max(5.0, float(timeout)))
        devtools.call("Page.enable")
        devtools.call("Runtime.enable")
        devtools.call("Log.enable")
        devtools.call("Network.enable")
        yield devtools
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        # 实测：Popen/CreateProcessW 拿到的那个 msedge.exe 是 launcher，200ms 内就以退出码 0
        # 自己走了，真正在跑的是继承作业对象的浏览器进程。所以 taskkill 打在一个不存在的
        # pid 上（rc=128「没有找到进程」），从来杀不掉任何东西；作业对象是唯一在干活的
        # 机制，因此先解绑它。taskkill 只留作非 nt 回退路径上的尽力一试。
        job_closed = _close_reaper(job)
        if process is not None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5)
                else:
                    process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        released = _release_profile(profile)
        if cleanup is not None:
            cleanup.update({"spawned": process is not None,
                            "job_closed": bool(job_closed),
                            "profile_released": bool(released)})


_MAX_CONSOLE = 30
_MAX_REQUESTS = 30
_IGNORED_REQUEST_SUFFIX = ("favicon.ico",)


class _DevTools:
    """Minimal CDP client that also records the signals a screenshot cannot show."""

    def __init__(self, connection, runtime_errors: list[str] | None = None,
                 recv_timeout: float = 5.0):
        self.connection = connection
        self.sequence = 0
        self.recv_timeout = max(1.0, float(recv_timeout))
        self.runtime_errors = runtime_errors if runtime_errors is not None else []
        self.console: list[dict[str, Any]] = []
        self.failed_requests: list[dict[str, Any]] = []
        # 去重【前】的事件计数。列表会去重也会截断，所以「这次动作有没有新报错」不能看列表
        # 长度——同一个错误每次动作都复现时，长度差恒为 0，就会把失败的动作报成成功。
        self.error_events = 0
        self.failure_events = 0
        self.runtime_events = 0
        self._requests: dict[str, str] = {}

    def _record_console(self, level: str, text: str) -> None:
        if "favicon.ico" in str(text).lower():
            return
        if level == "error":
            self.error_events += 1
        entry = {"level": level, "text": str(text)[:300]}
        if entry not in self.console and len(self.console) < _MAX_CONSOLE:
            self.console.append(entry)

    def _record_failure(self, kind: str, url: str, detail: str) -> None:
        if str(url).split("?")[0].rstrip("/").lower().endswith(_IGNORED_REQUEST_SUFFIX):
            return
        self.failure_events += 1
        # Chrome reports an HTTP failure twice: once as the status and once as an
        # aborted load. Keep the status, drop the redundant abort noise.
        if kind == "network" and "ERR_ABORTED" in str(detail):
            base = str(url).split("?")[0]
            if any(item["url"].split("?")[0] == base and item["kind"].startswith("http_")
                   for item in self.failed_requests):
                return
        entry = {"kind": kind, "url": str(url)[:300], "detail": str(detail)[:200]}
        if entry not in self.failed_requests and len(self.failed_requests) < _MAX_REQUESTS:
            self.failed_requests.append(entry)

    def _handle_event(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        if method == "Runtime.exceptionThrown":
            self.runtime_events += 1
            details = params.get("exceptionDetails") or {}
            text = str(details.get("text") or details.get("exception", {}).get("description") or "runtime error")
            if text not in self.runtime_errors:
                self.runtime_errors.append(text[:500])
        elif method == "Runtime.consoleAPICalled":
            level = str(params.get("type") or "").lower()
            if level in {"error", "warning"}:
                parts = []
                for argument in params.get("args") or []:
                    value = argument.get("value")
                    parts.append(str(value if value is not None
                                     else (argument.get("description") or argument.get("type") or "")))
                self._record_console(level, " ".join(part for part in parts if part) or str(params.get("text") or ""))
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            level = str(entry.get("level") or "").lower()
            if level in {"error", "warning"}:
                self._record_console(level, f"{entry.get('source') or ''}: {entry.get('text') or ''}".strip(": ").strip())
        elif method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            self._requests[str(params.get("requestId"))] = str(request.get("url") or "")
        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            try:
                status = int(response.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            if status >= 400:
                url = str(response.get("url") or self._requests.get(str(params.get("requestId")), ""))
                self._record_failure(f"http_{status}", url, response.get("statusText") or "")
        elif method == "Network.loadingFailed":
            url = self._requests.get(str(params.get("requestId")), "")
            self._record_failure("network", url, params.get("errorText") or "")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sequence += 1
        ident = self.sequence
        self.connection.send(json.dumps({"id": ident, "method": method,
                                         "params": params or {}}))
        while True:
            message = json.loads(self.connection.recv())
            self._handle_event(message)
            if message.get("id") == ident:
                if message.get("error"):
                    raise VisualAcceptanceError(str(message["error"]))
                return message.get("result") or {}

    def set_timeout(self, seconds: float) -> None:
        """改回阻塞收包的等待时长。

        `drain` 为不卡住会把 recv 超时压到 0.3s，之后任何一次 `call` 都可能因此假性
        超时——所以要么显式恢复，要么用 drain 的默认恢复值。
        """
        self.recv_timeout = max(1.0, float(seconds))
        try:
            self.connection.settimeout(self.recv_timeout)
        except Exception:
            pass

    def drain(self, seconds: float = 1.5, *, restore: bool = True) -> None:
        """Collect late console/network events without waiting on a response."""
        deadline = time.monotonic() + max(0.1, float(seconds))
        try:
            self.connection.settimeout(0.3)
        except Exception:
            pass
        while time.monotonic() < deadline:
            try:
                message = json.loads(self.connection.recv())
            except Exception:
                continue
            self._handle_event(message)
        if restore:
            self.set_timeout(self.recv_timeout)

    def evaluate(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        })
        if result.get("exceptionDetails"):
            raise VisualAcceptanceError(str(result["exceptionDetails"]))
        return (result.get("result") or {}).get("value")


def _entry_file(root: Path, entry: str = "") -> Path:
    requested = str(entry or "").strip().replace("\\", "/")
    candidates = [requested] if requested else ["index.html", "web/index.html"]
    for rel in candidates:
        candidate = (root / rel).resolve()
        if candidate.is_file() and (candidate == root or root in candidate.parents):
            if candidate.suffix.lower() in {".html", ".htm"}:
                return candidate
    raise VisualAcceptanceError("当前项目没有可用于真实网页验收的 index.html 入口。")


def preview_target(root: str | Path, entry: str = "",
                   url: str = "") -> tuple[str, "Path | None"]:
    """这次要打开的本机地址：给了 `url` 就用已经在跑的服务，否则用项目里的 HTML 入口。

    拆出来是为了让「url 模式不需要 index.html」这件事可以不启动浏览器就验证到。
    """
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise VisualAcceptanceError("当前项目目录不存在。")
    if str(url or "").strip():
        target, err = loopback_url(url)
        if err:
            raise VisualAcceptanceError(err)
        return target, None
    return "", _entry_file(root_path, entry)


def capture_project_preview(root: str | Path, *, entry: str = "", evidence_dir: str | Path = "",
                            width: int = 960, height: int = 540, timeout: float = 25.0,
                            url: str = "", extra_flags: Any = ()) -> dict[str, Any]:
    """Capture a same-project browser preview and return bounded evidence.

    默认把项目目录临时当静态站服务并打开其中的 HTML 入口。给 `url`（只允许本机回环，
    如 dev_serve 起来的 http://127.0.0.1:5173/）时【不再起静态服务】，直接截那个已经
    在跑的服务——Vite/Vue 这类必须跑 dev server 的前端才预览得到。

    The returned ``image`` is transient model input.  The saved relative path
    is the durable workflow artifact and is served only through the workflow
    preview endpoint after the project-root check.
    """
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise VisualAcceptanceError("当前项目目录不存在。")
    target, entry_path = preview_target(root_path, entry, url)
    width = max(320, min(1920, int(width)))
    height = max(220, min(1200, int(height)))
    target_dir = Path(evidence_dir).resolve() if evidence_dir else (
        root_path / ".docmind" / "visual-evidence" /
        (time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()))
    if target_dir != root_path and root_path not in target_dir.parents:
        raise VisualAcceptanceError("视觉证据目录必须位于当前项目内。")
    target_dir.mkdir(parents=True, exist_ok=True)
    server = None
    server_thread = None
    if not target:
        # 已经指定了本机 URL 就直接打那个服务，不再多起一个静态站
        server, server_thread = serve_static(root_path)
    cleanup: dict[str, Any] = {}
    result: dict[str, Any] = {}
    try:
        with browser_session(timeout, extra_flags, cleanup=cleanup) as devtools:
            devtools.call("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": height, "deviceScaleFactor": 1,
                "mobile": False,
            })
            if target:
                separator = "&" if "?" in target else "?"
                preview_url = "%s%sdocmind=%d" % (target, separator, time.time_ns())
                shown_entry = preview_url
            else:
                rel_entry = entry_path.relative_to(root_path).as_posix()
                preview_url = ("http://127.0.0.1:%d/%s?docmind=%d"
                               % (server.server_port, rel_entry, time.time_ns()))
                shown_entry = rel_entry
            devtools.call("Page.navigate", {"url": preview_url})
            devtools.evaluate(page_ready_js(timeout))
            devtools.drain(min(2.0, max(0.5, timeout * 0.1)))
            title = str(devtools.evaluate("document.title || ''") or "")[:240]
            body_text = str(devtools.evaluate("document.body?.innerText || ''") or "")[:1200]
            shot = devtools.call("Page.captureScreenshot", {"format": "png"})
            encoded = str(shot.get("data") or "")
            if not encoded:
                raise VisualAcceptanceError("浏览器没有返回截图。")
            raw = base64.b64decode(encoded)
            path = target_dir / "preview.png"
            path.write_bytes(raw)
            rel = path.relative_to(root_path).as_posix()
            console_errors = [item for item in devtools.console if item["level"] == "error"]
            console_warnings = [item for item in devtools.console if item["level"] == "warning"]
            checks = {
                "page_loaded": True,
                "no_runtime_errors": not devtools.runtime_errors,
                "no_console_errors": not console_errors,
                "no_failed_requests": not devtools.failed_requests,
            }
            result = {
                "ok": all(checks.values()), "passed": all(checks.values()),
                "checks": checks, "title": title, "body_excerpt": body_text,
                "runtime_errors": devtools.runtime_errors,
                "console_errors": console_errors,
                "console_warnings": console_warnings,
                "failed_requests": devtools.failed_requests,
                "screenshot": rel,
                "image": encoded, "width": width, "height": height,
                "artifacts": [{
                    "id": "visual-preview",
                    "kind": "image",
                    "adapter": "visual",
                    "path": rel,
                    "label": "真实浏览器预览截图",
                    "summary": "正式开发舱 visual adapter 捕获的当前项目画面",
                    "evidence": ["browser:Page.captureScreenshot",
                                 "browser:Runtime.consoleAPICalled",
                                 "browser:Network.responseReceived",
                                 "entry:" + shown_entry],
                    "metadata": {"width": str(width), "height": str(height), "title": title,
                                 "console_errors": str(len(console_errors)),
                                 "failed_requests": str(len(devtools.failed_requests))},
                }],
            }
        # 收尾偏离才出声：正常路径不往每次预览的输出里加噪音；但「没起成/没关掉/没删净」
        # 必须能被操作者看见——静默泄漏正是 462 个进程攒到 99°C 的成因。
        if cleanup and not all(cleanup.values()):
            result["cleanup"] = dict(cleanup)
        return result
    except VisualAcceptanceError:
        raise
    except Exception as exc:
        raise VisualAcceptanceError(f"真实画面捕获失败：{type(exc).__name__}") from exc
    finally:
        stop_static(server, server_thread)


__all__ = ["BrowserSpawnError", "VisualAcceptanceError", "browser_session",
           "capture_project_preview", "preview_target", "sanitize_flags",
           "serve_static", "spawn_browser_in_job", "stop_static",
           "sweep_stale_profiles"]
