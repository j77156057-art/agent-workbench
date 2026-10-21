"""Bounded execution for agent-initiated project commands.

Two entry points share one output policy:

* ``run_bounded`` runs a command to completion under a caller chosen timeout
  and returns a head/tail window of the combined output.
* ``start_job`` / ``job_logs`` keep long commands alive in the background so a
  build, a test suite or a dev server stays observable after the tool returns.

Both paths drain the pipes continuously: a chatty command can never block on a
full pipe and the captured window never grows with the command's real output.
Failures are reported, never silently swallowed, and every process is attached
to a killable group so a cancelled job cannot leave orphans behind.
"""
from __future__ import annotations

import codecs
import locale
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
MAX_BACKGROUND_TIMEOUT = 1800
HEAD_CHARS = 4000
TAIL_CHARS = 4000
MAX_JOBS = 8
FINISHED_TTL = 900.0
READ_CHUNK = 8192

_ENV_SECRET_TOKENS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY")


def _windows_legacy_encoding() -> str:
    """系统 ANSI 代码页（中文系统为 cp936），不被 Python UTF-8 模式影响。

    UTF-8 模式（`PYTHONUTF8=1`）下 `locale.getpreferredencoding()` 恒报 utf-8；
    拿它做「非 UTF-8 流」的回退等于没回退——GBK 字节照样全变替换符。
    老工具（`dir`/`ping`/无编码提示的子进程）的真实输出页只有 Win32
    `GetACP()` 说真话。
    """
    try:
        import ctypes
        code_page = int(ctypes.windll.kernel32.GetACP())
    except Exception:  # noqa: BLE001 - 非完整 Win32 环境时退回旧口径
        code_page = 0
    if code_page:
        try:
            return codecs.lookup(f"cp{code_page}").name
        except LookupError:
            pass
    return locale.getpreferredencoding(False) or "utf-8"


def _preferred_encoding() -> str:
    """非 UTF-8 流的回落编码（**绝不能是 utf-8**）。

    判定器只在「这段流**不是** UTF-8」时才会走到这里 —— 若回落编码还是 utf-8，
    那个分支就是**死的**（判了不是 UTF-8、又按 UTF-8 解），GBK 字节照样全变
    替换符。Windows 分支早就为此绕开过 `locale.getpreferredencoding()`
    （UTF-8 模式下它恒报 utf-8）；Linux 这边原来直接 `return "utf-8"`，
    是同一个坑的另一个形状：CI（ubuntu）上 GBK 子进程输出一律变乱码。

    没有 Win32 ACP 可问时就用 **cp936**：本产品面向中文 Windows 生态，老工具
    （`dir`/`ping`/无编码提示的子进程）在那边吐的就是 cp936 那一页。回落编码
    钉成 cp936 之后，**同一段字节在任何平台上解出同一个结果**。
    `DOCMIND_COMMAND_ENCODING` 仍可显式指定别的编码。
    """
    configured = str(os.getenv("DOCMIND_COMMAND_ENCODING") or "").strip()
    if configured:
        return configured
    if os.name == "nt":
        return _windows_legacy_encoding()
    return "cp936"


class _StreamDecoder:
    """按流的第一个非 ASCII 字节定编码，而不是假定本地编码。

    管道另一头写什么编码，本进程无从知晓，而 `locale.getpreferredencoding()`
    在中文 Windows 上是 cp936。两个方向都会翻车：

    * `node`/`git`/`cargo` 这类工具输出 **UTF-8**，按 cp936 解是乱码；
    * 反过来，子进程若继承了 `PYTHONIOENCODING=utf-8`（IDE、CI、开发机常设），
      Python 子进程也会输出 UTF-8，而父进程仍按 cp936 解 —— `中文输出测试`
      变成 `涓枃杈撳嚭娴嬭瘯`。这个坑在本机真实复现过（`test_multibyte_text_`
      `survives_chunked_reads`）。

    规则：纯 ASCII 前缀在任何编码下都一样，先按 ASCII 吐出去、**不下判断**；
    第一个非 ASCII 字节出现时才定编码 —— 这些字节能按 UTF-8 解出来就用 UTF-8，
    否则退回 legacy 编码（cp936，`dir`/`ping` 这类老工具走这条路；见
    `_preferred_encoding` —— **不能回落 utf-8**，那会让「不是 UTF-8」那个分支
    变成死代码，GBK 字节照样全变替换符）。纯 ASCII 输出永远不会被判错；
    `DOCMIND_COMMAND_ENCODING` 仍可强制指定。

    判定必须用**增量**解码器：分块读取会把一个多字节字符切成两半，此时尾部是
    不完整的 UTF-8 序列。整段 `bytes.decode("utf-8")` 会因此抛错，把 UTF-8 流
    误判成 GBK —— 那正是这个类要修的病。

    已知代价：GBK 输出的头几个字节若恰好构成合法 UTF-8 序列（少见），会被误判
    成 UTF-8；只影响该段输出的可读性，不影响命令本身。
    """

    def __init__(self, encoding: str = ""):
        # 显式配置的编码不做嗅探，直接采信。
        self._decided = bool(encoding)
        self._encoding = encoding
        self._decoder = self._make(encoding) if self._decided else None
        self._pending = b""

    @staticmethod
    def _make(encoding: str):
        try:
            return codecs.getincrementaldecoder(encoding)(errors="replace")
        except LookupError:
            return codecs.getincrementaldecoder("utf-8")(errors="replace")

    def decode(self, chunk: bytes, final: bool = False) -> str:
        if self._decided:
            return self._decoder.decode(chunk, final)
        self._pending += chunk
        if not final:
            # 纯 ASCII 直接放行，保持未决状态。
            try:
                text = self._pending.decode("ascii")
            except UnicodeDecodeError:
                return self._decide()
            self._pending = b""
            return text
        return self._decide(final=True)

    def _decide(self, final: bool = False) -> str:
        verdict = _probe_utf8(self._pending, final)
        if verdict is None:
            # 只拿到一段不完整的 UTF-8 序列（分块正好切在这儿），这一块判断不了。
            # 字节留着，等下一块 —— 否则一个孤立的 GBK 首字节（如 \xd6，本身是合法
            # 的 UTF-8 首字节）就会把整个流误判成 UTF-8。
            return ""
        pending, self._pending = self._pending, b""
        self._encoding = "utf-8" if verdict else _preferred_encoding()
        self._decided = True
        self._decoder = self._make(self._encoding)
        return self._decoder.decode(pending, final)


def _probe_utf8(pending: bytes, final: bool) -> bool | None:
    """严格按增量 UTF-8 试解：True=是，False=不是，None=还判断不了。"""
    probe = codecs.getincrementaldecoder("utf-8")()
    try:
        probe.decode(pending, final)
    except UnicodeDecodeError:
        return False
    if final:
        return True
    buffered = getattr(probe, "getstate", lambda: (b"", 0))()[0]
    return None if buffered else True


def _new_decoder():
    configured = str(os.getenv("DOCMIND_COMMAND_ENCODING") or "").strip()
    return _StreamDecoder(configured)


def clean_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Drop credential-shaped variables before any command inherits the env.

    Also asks Python children for unbuffered output: a block-buffered child
    loses everything still in its buffer when a long job is cancelled, which
    would leave the agent staring at an empty log.
    """
    env = dict(base if base is not None else os.environ)
    for key in list(env):
        upper = key.upper()
        if any(token in upper for token in _ENV_SECRET_TOKENS):
            env.pop(key, None)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


class _CappedBuffer:
    """Keep the first and last slice of a stream; drop only the middle."""

    def __init__(self, head_chars: int = HEAD_CHARS, tail_chars: int = TAIL_CHARS):
        self.head_chars = max(200, int(head_chars))
        self.tail_chars = max(200, int(tail_chars))
        self._head: list[str] = []
        self._head_len = 0
        self._tail = ""
        self.total = 0
        self.lock = threading.Lock()

    def feed(self, text: str) -> None:
        if not text:
            return
        with self.lock:
            self.total += len(text)
            if self._head_len < self.head_chars:
                take = self.head_chars - self._head_len
                part = text[:take]
                self._head.append(part)
                self._head_len += len(part)
                text = text[take:]
            if text:
                self._tail = (self._tail + text)[-self.tail_chars:]

    def text(self) -> str:
        with self.lock:
            head = "".join(self._head)
            if not self._tail:
                return head
            omitted = self.total - self._head_len - len(self._tail)
            if omitted <= 0:
                return head + self._tail
            return f"{head}\n...[省略 {omitted} 字符]...\n{self._tail}"

    @property
    def truncated(self) -> bool:
        with self.lock:
            return self.total > self._head_len + len(self._tail)


def _pump(popen: subprocess.Popen, buffer: _CappedBuffer) -> None:
    """Read until EOF using an incremental decoder so multibyte text survives."""
    decoder = _new_decoder()
    stream = popen.stdout
    try:
        while True:
            chunk = stream.read1(READ_CHUNK) if hasattr(stream, "read1") else stream.read(READ_CHUNK)
            if not chunk:
                break
            buffer.feed(decoder.decode(chunk))
        buffer.feed(decoder.decode(b"", True))
    except (ValueError, OSError):
        pass
    finally:
        try:
            stream.close()
        except (ValueError, OSError):
            pass


def _spawn(command: list[str], *, cwd: str, env: dict[str, str]) -> tuple[subprocess.Popen, Any, Any]:
    """Start a command inside a killable group; returns (popen, kernel, job)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt":
        try:
            from .windows_sandbox import spawn_isolated
            return spawn_isolated(command, cwd=cwd, env=env)
        except Exception:
            popen = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     env=env, creationflags=flags)
            return popen, None, None
    popen = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=env, start_new_session=True)
    return popen, None, None


def _terminate(popen: subprocess.Popen, kernel: Any, job: Any, *, exit_code: int = 124) -> None:
    if popen is None:
        return
    if popen.poll() is None:
        try:
            if os.name == "nt" and kernel is not None and job is not None:
                from .windows_sandbox import terminate_isolated
                terminate_isolated(kernel, job, exit_code=exit_code)
            elif os.name != "nt":
                import signal
                try:
                    os.killpg(os.getpgid(popen.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    popen.terminate()
            else:
                popen.terminate()
        except Exception:
            try:
                popen.kill()
            except Exception:
                return
    try:
        popen.wait(timeout=5)
    except Exception:
        try:
            popen.kill()
        except Exception:
            pass


def window_text(text: str, *, head_chars: int = HEAD_CHARS, tail_chars: int = TAIL_CHARS) -> str:
    """Apply the same head/tail policy to an already collected string."""
    buffer = _CappedBuffer(head_chars, tail_chars)
    buffer.feed(text or "")
    return buffer.text()


def clamp_timeout(value: Any, ceiling: int = MAX_TIMEOUT, default: int = DEFAULT_TIMEOUT) -> int:
    try:
        seconds = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(1, min(int(ceiling), seconds))


def run_bounded(command: list[str], *, cwd: str, timeout: int = DEFAULT_TIMEOUT,
                env: dict[str, str] | None = None, head_chars: int = HEAD_CHARS,
                tail_chars: int = TAIL_CHARS, command_text: str = "") -> dict[str, Any]:
    """Run one command to completion and return a bounded report."""
    started = time.monotonic()
    seconds = clamp_timeout(timeout, MAX_TIMEOUT, DEFAULT_TIMEOUT)
    buffer = _CappedBuffer(head_chars, tail_chars)
    try:
        popen, kernel, job = _spawn(command, cwd=cwd, env=clean_environment(env))
    except FileNotFoundError as exc:
        return {"ok": False, "exit_code": None, "timed_out": False, "output": "",
                "truncated": False, "total_chars": 0, "elapsed": 0.0,
                "error": f"命令不存在：{exc}", "command": command_text or " ".join(command)}
    except OSError as exc:
        return {"ok": False, "exit_code": None, "timed_out": False, "output": "",
                "truncated": False, "total_chars": 0, "elapsed": 0.0,
                "error": f"命令无法启动：{exc}", "command": command_text or " ".join(command)}
    timed_out = False
    reader = threading.Thread(target=_pump, args=(popen, buffer), name="docmind-cmd-reader",
                              daemon=True)
    reader.start()
    try:
        popen.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        _terminate(popen, kernel, job)
        reader.join(timeout=5)
    exit_code = popen.returncode
    output = buffer.text()
    return {
        "ok": (not timed_out) and exit_code == 0,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output": output,
        "truncated": buffer.truncated,
        "total_chars": buffer.total,
        "elapsed": round(time.monotonic() - started, 2),
        "error": "" if not timed_out else f"命令执行超过 {seconds}s 被终止",
        "command": command_text or " ".join(command),
    }


def run_bounded_report(proc: subprocess.CompletedProcess, timeout: int = DEFAULT_TIMEOUT, *,
                       command_text: str = "") -> dict[str, Any]:
    """Shape an already finished CompletedProcess into the bounded report."""
    raw_out = proc.stdout or ""
    raw_err = proc.stderr or ""
    stdout = window_text(raw_out)
    stderr = window_text(raw_err)
    cut_out = len(stdout) < len(raw_out)
    cut_err = len(stderr) < len(raw_err)
    combined = stdout if not stderr else (f"{stdout}\n{stderr}" if stdout else stderr)
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "timed_out": False,
        "output": combined.strip(),
        "truncated": bool(cut_out or cut_err),
        "total_chars": len(combined),
        "elapsed": None,
        "error": "",
        "command": command_text,
    }


@dataclass
class _Job:
    id: str
    command: str
    argv: list[str]
    cwd: str
    created: float
    timeout: int
    buffer: _CappedBuffer = field(default_factory=_CappedBuffer)
    state: str = "starting"
    exit_code: int | None = None
    error: str = ""
    popen: Any = None
    kernel: Any = None
    job_handle: Any = None
    thread: Any = None
    finished_at: float | None = None


_JOBS: dict[str, _Job] = {}
_LOCK = threading.RLock()


def _reap_locked() -> None:
    now = time.monotonic()
    for ident in [k for k, v in _JOBS.items()
                  if v.finished_at is not None and now - v.finished_at > FINISHED_TTL]:
        _JOBS.pop(ident, None)
    if len(_JOBS) > MAX_JOBS * 2:
        for ident in sorted(_JOBS, key=lambda k: _JOBS[k].created)[:len(_JOBS) - MAX_JOBS]:
            _JOBS.pop(ident, None)


def _monitor(job: _Job) -> None:
    try:
        job.popen.wait(timeout=job.timeout)
        job.state = "done"
        job.exit_code = job.popen.returncode
    except subprocess.TimeoutExpired:
        job.state = "timeout"
        job.error = f"后台任务超过 {job.timeout}s 被终止"
    except Exception as exc:  # noqa: BLE001
        job.state = "failed"
        job.error = f"{type(exc).__name__}"
    finally:
        _terminate(job.popen, job.kernel, job.job_handle)
        if job.thread is not None:
            job.thread.join(timeout=2)
        job.finished_at = time.monotonic()
        with _LOCK:
            if job.state == "starting":
                job.state = "failed"


def start_job(argv: list[str], *, cwd: str, timeout: int = DEFAULT_TIMEOUT,
              env: dict[str, str] | None = None, command_text: str = "") -> dict[str, Any]:
    """Start a long command in the background and return its job id."""
    if not os.path.isdir(cwd):
        return {"ok": False, "error": f"工作目录不存在：{cwd}"}
    seconds = clamp_timeout(timeout, MAX_BACKGROUND_TIMEOUT, DEFAULT_TIMEOUT)
    with _LOCK:
        _reap_locked()
        running = sum(1 for job in _JOBS.values() if job.finished_at is None)
        if running >= MAX_JOBS:
            return {"ok": False, "error": f"后台任务已达上限（{MAX_JOBS}），请先查看或取消已有任务"}
        ident = uuid.uuid4().hex[:10]
        job = _Job(id=ident, command=command_text or " ".join(argv), argv=list(argv),
                   cwd=cwd, created=time.monotonic(), timeout=seconds)
    try:
        job.popen, job.kernel, job.job_handle = _spawn(argv, cwd=cwd, env=clean_environment(env))
    except (FileNotFoundError, OSError) as exc:
        job.state = "failed"
        job.error = f"命令无法启动：{exc}"
        job.finished_at = time.monotonic()
        with _LOCK:
            _JOBS[ident] = job
        return {"ok": False, "job_id": ident, "error": job.error}
    job.state = "running"
    reader = threading.Thread(target=_pump, args=(job.popen, job.buffer),
                              name=f"docmind-job-{ident}", daemon=True)
    reader.start()
    job.thread = reader
    watcher = threading.Thread(target=_monitor, args=(job,), name=f"docmind-job-watch-{ident}",
                               daemon=True)
    watcher.start()
    with _LOCK:
        _JOBS[ident] = job
    return {"ok": True, "job_id": ident, "state": job.state, "timeout": seconds,
            "command": job.command}


def _job_view(job: _Job, *, offset: int = 0, include_logs: bool = True) -> dict[str, Any]:
    text = job.buffer.text()
    safe_offset = max(0, min(int(offset), len(text)))
    elapsed = (job.finished_at or time.monotonic()) - job.created
    view = {
        "job_id": job.id,
        "state": job.state,
        "exit_code": job.exit_code,
        "elapsed": round(elapsed, 2),
        "timeout": job.timeout,
        "total_chars": job.buffer.total,
        "truncated": job.buffer.truncated,
        "command": job.command,
        "error": job.error,
    }
    if include_logs:
        view["offset"] = safe_offset
        view["logs"] = text[safe_offset:]
        view["next_offset"] = len(text)
    return view


def job_logs(job_id: str = "", *, offset: int = 0) -> dict[str, Any]:
    """Return one job's state plus its incremental output, or list recent jobs."""
    with _LOCK:
        _reap_locked()
        ident = str(job_id or "").strip()
        if not ident:
            jobs = sorted(_JOBS.values(), key=lambda item: item.created, reverse=True)
            return {"ok": True, "jobs": [_job_view(item, include_logs=False) for item in jobs[:10]]}
        job = _JOBS.get(ident)
        if job is None:
            return {"ok": False, "error": f"后台任务不存在或已清理：{ident}"}
        return {"ok": True, **_job_view(job, offset=offset)}


def job_cancel(job_id: str) -> dict[str, Any]:
    """Stop a background job and keep its output available for inspection."""
    with _LOCK:
        job = _JOBS.get(str(job_id or "").strip())
        if job is None:
            return {"ok": False, "error": f"后台任务不存在或已清理：{job_id}"}
    if job.finished_at is None:
        _terminate(job.popen, job.kernel, job.job_handle)
        if job.thread is not None:
            job.thread.join(timeout=2)
        job.state = "cancelled"
        job.finished_at = time.monotonic()
    return {"ok": True, **_job_view(job)}


def job_count() -> int:
    with _LOCK:
        return len(_JOBS)


__all__ = [
    "DEFAULT_TIMEOUT", "MAX_TIMEOUT", "MAX_BACKGROUND_TIMEOUT", "HEAD_CHARS", "TAIL_CHARS",
    "MAX_JOBS", "clamp_timeout", "clean_environment", "window_text", "run_bounded",
    "run_bounded_report",
    "start_job", "job_logs", "job_cancel", "job_count",
]
