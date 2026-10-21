"""Run the complete game Harness acceptance against a real Godot project.

The command is opt-in and always works on a disposable copy by default.  It
is intentionally deterministic at the task runner boundary: the acceptance
checks the runtime contract and audited file tools, while provider-specific
planning remains covered by the live contract test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from config import get_runtime, set_runtime
from .game_workflow import GameWorkflowManager, WorkflowPolicy
from .retrieval import retrieve_context
import tools as audited_tools


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _playtest(executable: str, root: Path, timeout: int, required_event: str = "") -> dict[str, Any]:
    command = [executable, "--headless", "--path", str(root), "--quit-after", "3"]
    try:
        completed = subprocess.run(command, cwd=str(root), capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   timeout=max(1, int(timeout)))
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:300]}
    output = (completed.stdout or "") + (completed.stderr or "")
    ok = completed.returncode == 0 and "SCRIPT ERROR" not in output and "Parse Error" not in output
    if required_event and required_event not in output:
        ok = False
    return {"ok": ok, "returncode": completed.returncode,
            "required_event": required_event, "event_found": bool(required_event and required_event in output),
            "output_tail": output[-1200:]}


def run_real_project_acceptance(project_root: str, godot: str, *, state_root: str = "",
                                collection: str = "docmind_code", timeout: int = 30,
                                require_event: str = "", isolate: bool = True) -> dict[str, Any]:
    """Execute and return a JSON-safe report for the full real-project path."""
    source = Path(project_root).expanduser().resolve()
    executable = str(Path(godot).expanduser())
    report: dict[str, Any] = {"ok": False, "project": str(source), "isolated": bool(isolate),
                              "phases": [], "errors": []}
    if not (source.is_dir() and (source / "project.godot").is_file()):
        report["errors"].append("project.godot is missing")
        return report
    if not Path(executable).is_file():
        report["errors"].append("Godot executable is missing")
        return report
    before = _digest(source)
    temp: tempfile.TemporaryDirectory[str] | None = None
    target = source
    if isolate:
        temp = tempfile.TemporaryDirectory(prefix="docmind-real-acceptance-")
        target = Path(temp.name) / "project"
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", ".docmind*"))
    old_root = get_runtime("code_root")
    manager: GameWorkflowManager | None = None
    try:
        set_runtime("code_root", str(target))
        try:
            evidence = retrieve_context(
                "Godot player movement enemy HUD main scene",
                collections={"code": collection}, top_k=5, max_chars=2400,
            )
        except Exception as exc:
            evidence = ""
            report["errors"].append("retrieval: " + type(exc).__name__)
        report["phases"].append({"name": "retrieval", "ok": bool(evidence),
                                 "chars": len(evidence), "collection": collection})
        if not evidence:
            return report

        manager = GameWorkflowManager(state_root or str(target / ".docmind-acceptance"))
        started = manager.start(
            "验证真实游戏项目并运行主场景自测", project_id="real-acceptance",
            project_root=str(target), llm_enabled=False, experience_enabled=False,
            policy=WorkflowPolicy(max_steps=16, max_replans=1, max_subagents=4,
                                  max_context_chars=2400),
        )
        workflow_id = started["workflow_id"]
        chosen = manager.choose(workflow_id, "recommended")
        report["phases"].append({"name": "clarify", "ok": bool(chosen)})
        manager.plan(workflow_id, [
            {"id": "inspect", "role": "researcher", "task": "检查项目主场景与脚本结构",
             "depends_on": [], "parallel_safe": True},
            {"id": "marker", "role": "coder", "task": "写入 Harness 验收标记文件",
             "depends_on": ["inspect"]},
            {"id": "verify", "role": "tester", "task": "运行 Godot headless 主场景并检查错误",
             "depends_on": ["marker"]},
        ])
        report["phases"].append({"name": "plan", "ok": True, "subagents": 3})
        playtest_report: dict[str, Any] = {}

        def runner(task: dict[str, Any], context: dict[str, str]) -> dict[str, Any]:
            task_id = str(task.get("id"))
            if task_id == "inspect":
                ok = (target / "project.godot").is_file()
                return {"status": "ok" if ok else "failed", "conclusion": "project structure inspected",
                        "steps": 1, "trace": {"steps": [{"tool": "read_file", "ok": ok}]}}
            if task_id == "marker":
                created = audited_tools.create_file(
                    "path: HARNESS_ACCEPTANCE.md\nnew_text: # Harness acceptance\n\nreal project acceptance marker.\n"
                )
                observed = audited_tools.read_file("HARNESS_ACCEPTANCE.md")
                ok = str(created).startswith("已创建") and "acceptance marker" in observed
                return {"status": "ok" if ok else "failed", "conclusion": observed[-500:],
                        "steps": 2, "file_changes": ["HARNESS_ACCEPTANCE.md"],
                        "trace": {"steps": [{"tool": "create_file", "ok": ok},
                                             {"tool": "read_file", "ok": ok}]}}
            result = _playtest(executable, target, timeout, require_event)
            playtest_report.clear()
            playtest_report.update(result)
            # Keep engine paths and raw logs out of the workflow conclusion:
            # output audit treats arbitrary engine source paths as unverified
            # file evidence. The complete tail remains in the top-level
            # acceptance report for operators.
            conclusion = "Godot headless playtest passed" if result["ok"] else \
                "Godot headless playtest failed: " + str(result.get("error") or "runtime error")[:240]
            return {"status": "ok" if result["ok"] else "failed",
                    "conclusion": conclusion, "steps": 1,
                    "tests": [{"name": "godot_headless", "ok": result["ok"]}],
                    "trace": {"steps": [{"tool": "game_playtest", "ok": result["ok"]}]}}

        waiting = manager.execute(workflow_id, runner, session_id="real-acceptance")
        approved = waiting.get("status") == "awaiting_approval"
        if approved:
            manager.approve(workflow_id, True)
        done = manager.execute(workflow_id, runner, session_id="real-acceptance")
        report["workflow"] = done
        report["playtest"] = dict(playtest_report)
        report["phases"].append({"name": "approval", "ok": approved})
        report["phases"].append({"name": "execute", "ok": done.get("status") == "completed"})
        report["phases"].append({"name": "review", "ok": bool((done.get("review") or {}).get("ok"))})
        report["ok"] = done.get("status") == "completed" and bool((done.get("review") or {}).get("ok"))
        return report
    except Exception as exc:
        report["errors"].append(type(exc).__name__ + ": " + str(exc)[:300])
        return report
    finally:
        report["source_unchanged"] = before == _digest(source)
        report["ok"] = bool(report.get("ok")) and bool(report["source_unchanged"])
        if manager is not None:
            manager.close()
        set_runtime("code_root", old_root or "")
        if temp is not None:
            temp.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Workbench real game Harness acceptance")
    parser.add_argument("--project", required=True)
    parser.add_argument("--godot", required=True)
    parser.add_argument("--state-root", default="")
    parser.add_argument("--collection", default="docmind_code")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--require-event", default="")
    parser.add_argument("--in-place", action="store_true", help="disable disposable project copy")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_real_project_acceptance(
        args.project, args.godot, state_root=args.state_root, collection=args.collection,
        timeout=args.timeout, require_event=args.require_event, isolate=not args.in_place)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else report)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
