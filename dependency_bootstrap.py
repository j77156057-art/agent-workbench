"""Install and update source-runtime dependencies before DocMind starts.

The frozen distribution already carries Python dependencies, so this helper is
for source checkouts and build machines. It is intentionally stdlib-only so it
can run before importing the application and can create a missing .venv.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


STATE_RELATIVE = Path(".docmind") / "dependency-state.json"
PYTHON_REQUIREMENTS = "requirements.txt"
FRONTEND_LOCK = Path("frontend") / "package-lock.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_if_exists(path: Path) -> str:
    return _sha256(path) if path.is_file() else ""


def _run(command: list[str], root: Path, *, label: str) -> None:
    print(f"[DocMind] {label}: {' '.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=str(root), check=False)
    if completed.returncode:
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}")


def _python_for_venv(venv: Path) -> Path:
    candidate = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return candidate


def _pip_for_venv(venv: Path) -> Path:
    candidate = venv / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
    return candidate


def _load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="dependency-state-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _find_npm() -> str | None:
    configured = os.getenv("DOCMIND_NPM_BIN", "").strip()
    if configured:
        return configured
    return shutil.which("npm")


def ensure_dependencies(root: str | os.PathLike[str], *, force: bool = False,
                        frontend: bool = True) -> dict:
    """Create/update the local source environment and return a JSON report."""
    project = Path(root).resolve()
    requirements = project / PYTHON_REQUIREMENTS
    if not requirements.is_file():
        raise FileNotFoundError(f"missing {requirements}")
    venv = project / ".venv"
    python = _python_for_venv(venv)
    pip = _pip_for_venv(venv)
    state_path = project / STATE_RELATIVE
    previous = _load_state(state_path)
    req_hash = _hash_if_exists(requirements)
    lock_hash = _hash_if_exists(project / FRONTEND_LOCK)
    report = {
        "root": str(project),
        "python": str(python),
        "requirements_hash": req_hash,
        "frontend_lock_hash": lock_hash,
        "python_changed": False,
        "frontend_changed": False,
        "created_venv": False,
    }

    if not python.is_file():
        _run([sys.executable, "-m", "venv", str(venv)], project, label="创建 Python 环境")
        report["created_venv"] = True
    if not python.is_file() or not pip.is_file():
        raise RuntimeError(f"virtual environment is incomplete: {venv}")

    python_changed = force or report["created_venv"] or previous.get("requirements_hash") != req_hash
    if python_changed:
        # Do not pin pip itself: the requirements file is the project-owned contract.
        _run([str(pip), "install", "--upgrade", "-r", str(requirements)], project,
             label="同步 Python 依赖")
        report["python_changed"] = True

    # Persist the Python half before touching npm. A locked frontend binary or
    # a temporary network failure must not make the next launch reinstall all
    # Python packages again.
    _save_state(state_path, {
        "schema": 1,
        "python_version": sys.version.split()[0],
        "requirements_hash": req_hash,
        "frontend_lock_hash": previous.get("frontend_lock_hash", ""),
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    })

    npm = _find_npm() if frontend else None
    lock_file = project / FRONTEND_LOCK
    frontend_changed = bool(frontend and npm and lock_file.is_file() and (
        force or previous.get("frontend_lock_hash") != lock_hash or
        not (project / "frontend" / "node_modules").is_dir() or
        not any((project / "web").glob("assets/*.js"))))
    if frontend_changed:
        _run([npm, "ci"], project / "frontend", label="同步前端依赖")
        _run([npm, "run", "build"], project / "frontend", label="构建前端资源")
        report["frontend_changed"] = True
    elif frontend and not npm and not any((project / "web").glob("assets/*.js")):
        raise RuntimeError("npm is required because built frontend assets are missing")

    state = {
        "schema": 1,
        "python_version": sys.version.split()[0],
        "requirements_hash": req_hash,
        "frontend_lock_hash": lock_hash,
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    _save_state(state_path, state)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare DocMind source dependencies")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--force", action="store_true", help="同步全部依赖，即使哈希未变化")
    parser.add_argument("--no-frontend", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = ensure_dependencies(args.root, force=args.force, frontend=not args.no_frontend)
    except (OSError, RuntimeError, FileNotFoundError) as exc:
        print(f"[DocMind] 依赖准备失败：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        changed = []
        if result["created_venv"]:
            changed.append("Python 环境")
        if result["python_changed"]:
            changed.append("Python 依赖")
        if result["frontend_changed"]:
            changed.append("前端依赖和资源")
        print("[DocMind] 依赖已就绪" + ("：" + "、".join(changed) if changed else "（无需更新）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
