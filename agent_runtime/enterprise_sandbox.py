"""Fail-closed container boundary for agent-initiated project commands.

This boundary is for process tools and independent review checks. File editing,
MCP calls and desktop automation need their own boundaries before the whole
agent can be described as isolated.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


class SandboxUnavailable(RuntimeError):
    pass


_EXCLUDED = {".git", ".docmind", ".venv", "node_modules", "__pycache__", "dist", "build"}
_SECRET_NAMES = {".env", ".env.local", ".env.production", ".env.development",
                 ".docmind_secrets.json", ".docmind_secret.key", "credentials.json"}
_MAX_COPY_BYTES = 2 * 1024 * 1024 * 1024
_MAX_COPY_FILES = 20000
_STAGE_ACTIVE: ContextVar[bool] = ContextVar("docmind_stage_active", default=False)
_IMAGE_DIGEST = re.compile(r"^(?:sha256:|[^\s@]+@sha256:)[0-9a-fA-F]{64}$")

# BUG-10：子进程 stdout 是管道（非控制台）时，CPython 按本机区域编码写中文
# （中文 Windows=cp936/GBK）；而父端多处按 utf-8 + errors=replace 解码，GBK 字节
# 不是合法 UTF-8 序列 → 被静默替换成 ◆（U+FFFD），即"到 0 时刻"变"◆◆0◆◆"那种乱码。
# 统一约定：给子进程强制 PYTHONIOENCODING=utf-8（吐 UTF-8 字节），父端统一 utf-8 解码，
# 两侧编码对齐，任何执行路径都不会再出现方向相反的错配。
SUBPROCESS_ENCODING = "utf-8"
SUBPROCESS_DECODE_ERRORS = "replace"


def utf8_child_env(base_env=None):
    """Return a copy of ``base_env`` (defaults to os.environ) forcing UTF-8 child stdio.

    Always overwrites PYTHONIOENCODING rather than setdefault: a stray locale/gbk
    value inherited from the server process would defeat the parent-side utf-8 decode.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@contextmanager
def stage_execution_scope():
    token = _STAGE_ACTIVE.set(True)
    try:
        yield
    finally:
        _STAGE_ACTIVE.reset(token)


def stage_execution_active() -> bool:
    return _STAGE_ACTIVE.get()


def execution_mode() -> str:
    mode = os.getenv("DOCMIND_EXECUTION_MODE", "enterprise").strip().lower()
    return mode if mode in {"enterprise", "local"} else "enterprise"


def stage_backend() -> str:
    """Describe the process boundary actually available to staged commands."""
    image = os.getenv("DOCMIND_SANDBOX_IMAGE", "").strip()
    if (shutil.which("docker") or shutil.which("podman")) and _IMAGE_DIGEST.fullmatch(image):
        return "container"
    if os.name == "nt" or os.getenv("DOCMIND_STAGE_HOST_COMMANDS", "0").strip().lower() in {"1", "true", "yes", "on"}:
        return "host_compat"
    return "unavailable"


def _copy_project(source: Path, destination: Path) -> None:
    """Copy project inputs without following links or carrying common secrets."""
    count = 0
    total = 0
    for base, dirs, files in os.walk(source, followlinks=False):
        base_path = Path(base)
        dirs[:] = [name for name in dirs if name not in _EXCLUDED
                   and not (base_path / name).is_symlink()]
        relative = base_path.relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            item = base_path / name
            if item.is_symlink() or name.lower() in _SECRET_NAMES or name.lower().startswith(".env."):
                continue
            if item.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
                continue
            count += 1
            total += item.stat().st_size
            if count > _MAX_COPY_FILES or total > _MAX_COPY_BYTES:
                raise SandboxUnavailable("项目副本超出企业沙箱容量限制")
            shutil.copy2(item, target_dir / name)


def run_project_command(command: list[str], *, project_root: str, timeout: int = 30,
                        network: bool = False) -> subprocess.CompletedProcess[str]:
    """Run on a disposable project copy with no host credentials or host writes."""
    if network:
        raise SandboxUnavailable("容器联网尚无域名白名单代理，拒绝开放全网；公开资料检索请使用受审计的联网工具")
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise SandboxUnavailable("项目目录不存在")
    if stage_execution_active() and stage_backend() == "host_compat":
        return run_staged_command(command, cwd=str(root), timeout=timeout)
    if stage_execution_active() and stage_backend() == "unavailable":
        raise SandboxUnavailable("当前平台没有可用的项目命令隔离后端")
    engine = shutil.which("docker") or shutil.which("podman")
    image = os.getenv("DOCMIND_SANDBOX_IMAGE", "").strip()
    if not engine or not image:
        raise SandboxUnavailable("企业沙箱不可用：需要 Docker/Podman 和 DOCMIND_SANDBOX_IMAGE")
    if not _IMAGE_DIGEST.fullmatch(image):
        raise SandboxUnavailable("DOCMIND_SANDBOX_IMAGE 必须固定为镜像摘要")
    with tempfile.TemporaryDirectory(prefix="docmind-sandbox-") as temporary:
        copied = Path(temporary) / "project"
        copied.mkdir()
        _copy_project(root, copied)
        container_name = "docmind-" + uuid.uuid4().hex
        launch = [engine, "run", "--rm", "--name", container_name, "--pull=never",
                  "--network", "none",
                  "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                  "--user=65534:65534", "--pids-limit=64", "--memory=1g", "--cpus=2",
                  "--tmpfs", "/tmp:rw,noexec,nosuid,mode=1777,size=128m",
                  "--mount", f"type=bind,source={copied},target=/workspace", "--workdir", "/workspace",
                  "--env", "HOME=/tmp", "--env", "HTTP_PROXY=", "--env", "HTTPS_PROXY=",
                  "--env", "ALL_PROXY=", "--env", "PYTHONIOENCODING=utf-8", image, *command]
        try:
            result = subprocess.run(launch, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=max(1, int(timeout)))
            if result.returncode in {125, 126, 127}:
                raise SandboxUnavailable("企业沙箱容器无法启动或镜像不可用")
            return result
        except subprocess.TimeoutExpired as exc:
            try:
                subprocess.run([engine, "rm", "-f", container_name], capture_output=True,
                               timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            raise SandboxUnavailable("企业沙箱执行超时") from exc
        except OSError as exc:
            raise SandboxUnavailable("企业沙箱启动失败") from exc


def container_available() -> bool:
    image = os.getenv("DOCMIND_SANDBOX_IMAGE", "").strip()
    return bool((shutil.which("docker") or shutil.which("podman")) and _IMAGE_DIGEST.fullmatch(image))


def fallback_policy() -> str:
    """What an ordinary agent command does when no container backend exists.

    ``host`` (default) falls back to the host process boundary and reports the
    weaker guarantee to the caller. ``off`` keeps the strict fail-closed
    behaviour required by enterprise deployments.
    """
    policy = os.getenv("DOCMIND_SANDBOX_FALLBACK", "host").strip().lower()
    return policy if policy in {"host", "off"} else "host"


def host_fallback_active() -> bool:
    """True when a non-staged agent command will run on the host boundary."""
    if stage_execution_active() or execution_mode() != "enterprise":
        return False
    return fallback_policy() == "host" and not container_available()


def run_bounded_host_command(command: list[str], *, cwd: str, timeout: int = 30,
                             ) -> subprocess.CompletedProcess[str]:
    """Host-boundary execution with credentials stripped and output capped."""
    from .process_runner import clean_environment, window_text
    seconds = max(1, int(timeout))
    if stage_backend() == "host_compat":
        result = run_staged_command(command, cwd=cwd, timeout=seconds)
    else:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                encoding=SUBPROCESS_ENCODING, errors=SUBPROCESS_DECODE_ERRORS,
                                timeout=seconds, env=utf8_child_env(clean_environment()))
    return subprocess.CompletedProcess(command, result.returncode,
                                       window_text(result.stdout or ""),
                                       window_text(result.stderr or ""))


def run_agent_command(command: list[str], *, project_root: str, timeout: int = 30,
                      network: bool = False) -> subprocess.CompletedProcess[str]:
    if stage_execution_active() or execution_mode() == "enterprise":
        try:
            return run_project_command(command, project_root=project_root,
                                       timeout=timeout, network=network)
        except SandboxUnavailable:
            # Staged commands keep the strict container contract; only ordinary
            # agent commands may fall back, and only when explicitly allowed.
            if stage_execution_active() or fallback_policy() != "host" or container_available():
                raise
            return run_bounded_host_command(command, cwd=project_root, timeout=timeout)
    return subprocess.run(command, cwd=project_root, capture_output=True, text=True,
                          encoding=SUBPROCESS_ENCODING, errors=SUBPROCESS_DECODE_ERRORS,
                          timeout=timeout, env=utf8_child_env())


def run_staged_command(command: list[str], *, cwd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Explicitly opted-in host compatibility, not filesystem/network isolation."""
    if stage_backend() != "host_compat":
        raise SandboxUnavailable("宿主兼容执行未启用")
    env = dict(os.environ)
    python_dir = str(Path(os.sys.executable).parent)
    env["PATH"] = python_dir + os.pathsep + env.get("PATH", "")
    for key in list(env):
        if any(token in key.upper() for token in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY")):
            env.pop(key, None)
    if os.name == "nt":
        from .windows_sandbox import run_isolated
        return run_isolated(command, cwd=cwd, env=env, timeout=max(1, int(timeout)),
                            memory_bytes=1024 * 1024 * 1024)
    # 非 Windows 分支要和 run_isolated 用**同一套**解码：原来两边都是 `text=True` 且
    # 不给 encoding，于是子进程吐非本机区域编码的字节时整条流变 `None`（见
    # `windows_sandbox.SUBPROCESS_ENCODING` 上那段注释）。两边不一致的话，跨平台复现
    # 出来的现象就会不一样。
    from .windows_sandbox import SUBPROCESS_DECODE_ERRORS, SUBPROCESS_ENCODING
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                          encoding=SUBPROCESS_ENCODING, errors=SUBPROCESS_DECODE_ERRORS,
                          timeout=max(1, int(timeout)), env=env)
