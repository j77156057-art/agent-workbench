"""Independent, evidence-based final review for autonomous project changes."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .enterprise_sandbox import SandboxUnavailable, execution_mode, run_project_command, stage_backend, stage_execution_active
from .project_checkpoint import _iter_files
from .output_audit import redact


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def review_project(project_root: str, checkpoint: Mapping[str, Any],
                   storage_root: str, test_commands: list[str] | None = None) -> dict[str, Any]:
    """Compare actual files with the checkpoint and run configured tests afresh.

    A missing baseline, omitted checkpoint files, or absent test command is
    visible as unverified. Model-generated test claims never satisfy this gate.
    """
    if execution_mode() != "enterprise":
        # 决策 9（fail-closed）：非企业环境无法提供容器级隔离的独立复核证据，
        # 不得放行，显式失败而非静默通过。
        return {"ok": False, "status": "unverified", "independent": True,
                "message": "非企业环境未执行独立复核，按 fail-closed 视为未通过"}
    root = Path(project_root).resolve() if project_root else None
    if not root or not root.is_dir() or not checkpoint.get("id"):
        return {"ok": False, "status": "unverified", "independent": True,
                "message": "缺少项目目录或执行前快照", "changes": [], "tests": []}
    if checkpoint.get("manifest_path"):
        try:
            hashes = json.loads(Path(str(checkpoint["manifest_path"])).read_text(encoding="utf-8"))
            before = {str(path): {"sha256": str(digest)} for path, digest in hashes.items()}
        except (OSError, ValueError, AttributeError):
            return {"ok": False, "status": "unverified", "independent": True,
                    "message": "试做区基线清单不可读取", "changes": [], "tests": []}
    else:
        before = {str(row["path"]): row for row in checkpoint.get("files") or []
                  if isinstance(row, Mapping) and row.get("path")}
    after = {rel: path for rel, path in _iter_files(root)}
    changed = sorted(set(before) | set(after))
    changes: list[dict[str, str]] = []
    diff_parts: list[str] = []
    saved = (Path(str(checkpoint["baseline_dir"])).resolve()
             if checkpoint.get("baseline_dir") else
             Path(storage_root).resolve() / "project_checkpoints" /
             str(checkpoint.get("workflow_id")) / str(checkpoint["id"]) / "files")
    for rel in changed:
        old_hash = str(before.get(rel, {}).get("sha256") or "")
        new_hash = _hash(after[rel]) if rel in after else ""
        if old_hash == new_hash:
            continue
        status = "added" if not old_hash else "deleted" if not new_hash else "modified"
        changes.append({"path": rel, "status": status, "before_sha256": old_hash, "after_sha256": new_hash})
        if len(diff_parts) < 30:
            try:
                old_path = saved / rel
                old_text = old_path.read_text(encoding="utf-8") if old_hash else ""
                new_text = after[rel].read_text(encoding="utf-8") if new_hash else ""
                diff_parts.append("".join(difflib.unified_diff(
                    old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                    fromfile="before/" + rel, tofile="after/" + rel))[:3000])
            except (OSError, UnicodeError):
                diff_parts.append(f"{rel}: 二进制或无法读取文本差异\n")
    skipped = list(checkpoint.get("skipped") or [])
    commands = [str(item).strip() for item in (test_commands or []) if str(item).strip()][:5]
    tests: list[dict[str, Any]] = []
    if changes and not skipped:
        for command in commands:
            try:
                argv = (["cmd", "/c", command] if os.name == "nt" and
                        (not stage_execution_active() or stage_backend() == "host_compat")
                        else ["/bin/sh", "-lc", command])
                result = run_project_command(argv,
                                             project_root=str(root), timeout=120)
                tests.append({"command": command, "ok": result.returncode == 0,
                              "exit_code": result.returncode,
                              "output": ((result.stdout or "") + (result.stderr or ""))[-2000:]})
            except SandboxUnavailable as exc:
                tests.append({"command": command, "ok": False, "error": str(exc)})
                break
    if stage_execution_active() and stage_backend() != "container":
        status, message = "unverified", "试做区没有容器级命令隔离，不能作为独立测试通过的证据"
    elif len(changes) > 100:
        status, message = "unverified", "变更文件超过单轮审核上限，请拆分任务"
    elif skipped:
        status, message = "unverified", "执行前快照有遗漏，无法完整核对项目变更"
    elif changes and not commands:
        status, message = "unverified", "项目有变更，但未配置独立测试命令"
    elif any(not item["ok"] for item in tests):
        status, message = "failed", "独立测试失败或沙箱不可用"
    else:
        status, message = "passed", "已核对项目差异和独立测试" if changes else "项目文件没有变化"
    return {"ok": status == "passed", "status": status, "independent": True,
            "message": message, "changes": changes[:100], "change_count": len(changes),
            "diff": redact("\n".join(diff_parts)) if diff_parts else "",
            "diff_truncated": len(changes) > 30 or len("\n".join(diff_parts)) > 8000,
            "checkpoint_skipped": skipped[:20], "tests": tests}
