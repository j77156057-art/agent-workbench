"""Audited, project-local installation of optional development tools.

The installer is deliberately narrow: packages are installed below the
project's ``.docmind/tool_envs`` directory, shell strings are never accepted,
and a caller must provide an explicit approval before any subprocess runs.
The module is usable in offline mode for planning/audit tests without touching
the network.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Mapping


class ToolInstallError(ValueError):
    pass


_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]{0,79}$")
_MANAGERS = {"python", "node"}


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-.")
    return slug[:80] or "tool"


class ToolInstallManager:
    def __init__(self, project_root: str | os.PathLike[str]):
        self.root = Path(project_root).resolve()
        self.env_root = (self.root / ".docmind" / "tool_envs").resolve()
        self.audit_path = (self.root / ".docmind" / "tool_installs.jsonl").resolve()
        self.lock_path = (self.root / ".docmind" / "tool-lock.json").resolve()
        if self.root not in self.env_root.parents:
            raise ToolInstallError("工具沙箱路径必须位于项目目录内")

    def _validate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        manager = str(request.get("manager") or "").strip().lower()
        package = str(request.get("package") or request.get("name") or "").strip()
        version = str(request.get("version") or "").strip()
        if manager not in _MANAGERS:
            raise ToolInstallError("manager 仅支持 python 或 node")
        if not _PACKAGE.fullmatch(package):
            raise ToolInstallError("package 名称包含不允许的字符")
        if version and not _VERSION.fullmatch(version):
            raise ToolInstallError("version 包含不允许的字符")
        fallback = request.get("fallback_tools") or request.get("fallback") or []
        if isinstance(fallback, str):
            fallback = [item.strip() for item in fallback.split(",") if item.strip()]
        if not isinstance(fallback, (list, tuple)):
            fallback = []
        fallback = [re.sub(r"[^A-Za-z0-9_.:-]", "", str(item))[:80]
                    for item in fallback[:8] if str(item).strip()]
        spec = "%s%s" % (package, ("==" + version) if manager == "python" and version else
                          ("@" + version) if manager == "node" and version else "")
        # Keep versions isolated so stale dist-info/package files cannot make
        # a later verifier report the wrong installed version.
        target = self.env_root / ("%s-%s-%s" % (
            _safe_slug(manager), _safe_slug(package), _safe_slug(version or "latest")))
        target = target.resolve()
        if self.root not in target.parents:
            raise ToolInstallError("工具沙箱路径越界")
        return {"manager": manager, "package": package, "version": version,
                "spec": spec, "target": str(target), "fallback_tools": fallback}

    def plan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        spec = self._validate(request)
        if spec["manager"] == "python":
            command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                       "--no-input", "--no-cache-dir", "--target", spec["target"], spec["spec"]]
            expected = spec["version"]
            verifier = [sys.executable, "-c",
                        ("import importlib.metadata as m, sys; "
                         "sys.path.insert(0, %r); "
                         "v=m.version(%r); "
                         "assert not %r or v == %r, (v, %r); "
                         "print(v)" % (spec["target"], spec["package"],
                                       expected, expected, expected))]
            source = "https://pypi.org/project/%s/" % spec["package"]
            launch = {"mode": "python_path", "target": spec["target"],
                      "command": [sys.executable, "-m", spec["package"]]}
        else:
            command = ["npm", "install", "--prefix", spec["target"], "--ignore-scripts",
                       "--no-audit", "--no-fund", spec["spec"]]
            verifier = ["npm", "--prefix", spec["target"], "list", "--depth=0", spec["spec"]]
            source = "https://www.npmjs.com/package/%s" % spec["package"]
            launch = {"mode": "node_prefix", "target": spec["target"],
                      "command": ["npx", "--prefix", spec["target"], spec["package"]]}
        return {**spec, "source": source, "launch": launch,
                "command": command, "verifier": verifier,
                "approval_action": "install_tool",
                "approval_target": "%s:%s" % (spec["manager"], spec["spec"]),
                "sandbox": True,
                "network_required": True,
                "alternatives": spec["fallback_tools"]}

    @staticmethod
    def _fingerprint(target: Path) -> dict[str, Any]:
        """Hash an installed environment without following links or unbounded files."""
        digest = hashlib.sha256()
        count = 0
        total = 0
        partial = False
        if not target.is_dir():
            return {"sha256": "", "file_count": 0, "bytes": 0, "partial": True}
        for path in sorted(target.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                rel = path.relative_to(target).as_posix()
                size = path.stat().st_size
                if count >= 10000 or total + size > 50 * 1024 * 1024:
                    partial = True
                    break
                digest.update(rel.encode("utf-8", "replace"))
                digest.update(b"\0")
                with path.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                count += 1
                total += size
            except OSError:
                partial = True
        return {"sha256": digest.hexdigest(), "file_count": count,
                "bytes": total, "partial": partial}

    def _write_lock(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        existing: dict[str, Any] = {"version": 1, "tools": {}}
        try:
            if self.lock_path.is_file():
                loaded = json.loads(self.lock_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing.update(loaded)
                    existing["tools"] = dict(loaded.get("tools") or {})
        except (OSError, ValueError):
            existing = {"version": 1, "tools": {}}
        key = "%s:%s" % (entry.get("manager"), entry.get("spec"))
        existing["tools"][key] = dict(entry)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.lock_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.lock_path)
        return dict(existing["tools"][key])

    def _audit(self, row: Mapping[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")

    def audit(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.audit_path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            with self.audit_path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        value = json.loads(line)
                        if isinstance(value, dict):
                            rows.append(value)
        except (OSError, ValueError):
            return []
        return rows[-max(1, min(500, int(limit))):]

    def install(self, request: Mapping[str, Any], *, approved: bool = False,
                runner: Callable[..., Any] | None = None, timeout: int = 300) -> dict[str, Any]:
        plan = self.plan(request)
        record = {"ts": int(time.time()), "package": plan["package"],
                  "manager": plan["manager"], "version": plan["version"],
                  "target": plan["target"], "approved": bool(approved),
                  "source": plan["source"], "launch": plan["launch"],
                  "sandbox": True, "status": "planned"}
        if not approved:
            record["status"] = "approval_required"
            self._audit(record)
            return {"ok": False, "approval_required": True, "plan": plan,
                    "audit": record}
        if plan["manager"] == "node" and not shutil.which("npm"):
            record.update(status="failed", error="npm_not_found")
            self._audit(record)
            return {"ok": False, "error": "找不到 npm", "fallback_tools": plan["alternatives"],
                    "audit": record}
        Path(plan["target"]).mkdir(parents=True, exist_ok=True)
        try:
            run_timeout = max(1, min(1800, int(timeout)))

            def run(command):
                if runner is None:
                    from .enterprise_sandbox import execution_mode
                    if execution_mode() == "enterprise":
                        raise ToolInstallError(
                            "企业沙箱模式禁止在宿主机安装工具；需使用隔离安装后审查并导入产物")
                    return subprocess.run(command, cwd=str(self.root), capture_output=True,
                                          text=True, timeout=run_timeout)
                return runner(command, str(self.root), run_timeout)

            completed = run(plan["command"])
            returncode = int(getattr(completed, "returncode", 0) if not isinstance(completed, Mapping)
                             else completed.get("returncode", 0))
            stdout = str(getattr(completed, "stdout", "") if not isinstance(completed, Mapping)
                         else completed.get("stdout", ""))[-500:]
            if returncode != 0:
                raise ToolInstallError("安装命令退出码 %s" % returncode)
            target = Path(plan["target"]).resolve()
            sandbox_ok = self.root in target.parents and target.is_dir()
            if not sandbox_ok:
                raise ToolInstallError("工具安装目录未通过沙箱校验")
            verified = run(plan["verifier"])
            verify_code = int(getattr(verified, "returncode", 0) if not isinstance(verified, Mapping)
                              else verified.get("returncode", 0))
            verify_stdout = str(getattr(verified, "stdout", "") if not isinstance(verified, Mapping)
                                else verified.get("stdout", ""))[-500:]
            verify_stderr = str(getattr(verified, "stderr", "") if not isinstance(verified, Mapping)
                                else verified.get("stderr", ""))[-500:]
            if verify_code != 0:
                raise ToolInstallError("工具版本验证失败（退出码 %s）" % verify_code)
            verify = {"target_exists": True, "sandbox_ok": True,
                      "version_ok": True, "command": plan["verifier"],
                      "stdout": stdout, "version_stdout": verify_stdout,
                      "version_stderr": verify_stderr}
            fingerprint = self._fingerprint(target)
            lock = self._write_lock({
                "manager": plan["manager"], "package": plan["package"],
                "spec": plan["spec"], "version": plan["version"],
                "source": plan["source"], "target": plan["target"],
                "launch": plan["launch"], "fingerprint": fingerprint,
                "installed_at": int(time.time()),
            })
            verify["fingerprint"] = fingerprint
            verify["lockfile"] = str(self.lock_path)
            record.update(status="installed", verify=verify, lock=lock)
            self._audit(record)
            return {"ok": True, "plan": plan, "verify": verify, "audit": record}
        except Exception as exc:
            record.update(status="failed", error=type(exc).__name__)
            self._audit(record)
            return {"ok": False, "error": str(exc)[:200],
                    "fallback_tools": plan["alternatives"], "audit": record}


__all__ = ["ToolInstallError", "ToolInstallManager"]
