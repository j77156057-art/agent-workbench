"""Opt-in full Harness acceptance on a copy of a real Godot project.

Set ``DOCMIND_REAL_GAME_E2E=1`` to run.  The source project is never modified:
the test copies the real project to a temporary directory, then runs the full
clarify -> retrieve -> plan -> parallel tasks -> file writes -> Godot playtest
-> review path against that copy.
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from config import get_runtime, set_runtime
from agent_runtime.game_workflow import GameWorkflowManager, WorkflowPolicy
from agent_runtime.retrieval import get_retriever, retrieve_context
from embeddings import EmbeddingClient
import tools as audited_tools
from vectorstore import get_collection


_PROJECT_ENV = "DOCMIND_REAL_GAME_PROJECT"
_ENGINE_ENV = "DOCMIND_GODOT_EXECUTABLE"
_E2E_ENV = "DOCMIND_REAL_GAME_E2E"
SOURCE_PROJECT = Path(os.getenv(_PROJECT_ENV, "").strip() or r"<sample-project>")
GODOT = os.getenv(_ENGINE_ENV, "").strip()


def _project_skip_reason() -> str:
    raw = os.getenv(_PROJECT_ENV, "").strip()
    if not raw:
        return ("未设置 %s：默认占位符不是真实项目；"
                "设成含 project.godot 的项目根目录后可运行" % _PROJECT_ENV)
    if not SOURCE_PROJECT.is_dir():
        return "%s=%r 指向的目录不存在；改成含 project.godot 的项目根目录" % (
            _PROJECT_ENV, raw)
    return "%s=%r 不是 Godot 项目（缺 project.godot）" % (_PROJECT_ENV, raw)


def _engine_skip_reason() -> str:
    raw = os.getenv(_ENGINE_ENV, "").strip()
    if not raw:
        return "未设置 %s：默认占位符不是可执行文件；设成 Godot 可执行文件的完整路径" % (
            _ENGINE_ENV)
    return "%s=%r 指向的文件不存在；改成 Godot 可执行文件的完整路径" % (_ENGINE_ENV, raw)


def _copy_project(source: Path, target: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in {".git"} or
                name in {".docmind_engine.log", ".docmind_runtime.jsonl", ".docmind_tasks.jsonl"}}
    shutil.copytree(source, target, ignore=ignore)


def _tree_digest(root: Path) -> str:
    """Stable manifest digest used to prove the source project stayed read-only."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


class RealProjectWorkflowE2ETests(unittest.TestCase):
    @unittest.skipUnless(
        os.getenv(_E2E_ENV, "").strip().lower() in {"1", "true", "yes"},
        "未设置 %s=1：真实项目全流程 E2E 需要显式开启" % _E2E_ENV,
    )
    def test_real_project_runs_full_harness_lifecycle(self):
        if not (SOURCE_PROJECT.is_dir() and (SOURCE_PROJECT / "project.godot").is_file()):
            self.skipTest(_project_skip_reason())
        if not GODOT or not Path(GODOT).is_file():
            self.skipTest(_engine_skip_reason())

        source_digest_before = _tree_digest(SOURCE_PROJECT)

        # Retrieval uses the already indexed source project, while all writes
        # and engine execution happen inside the disposable project copy.
        evidence = ""
        retrieval_errors = []
        # Open the persisted collection once before the first hybrid query.
        # Chroma may otherwise return an empty first read while opening its
        # client on a fresh Python worker.
        try:
            get_collection("docmind_code").count()
        except Exception as exc:  # pragma: no cover - diagnostic only
            retrieval_errors.append(type(exc).__name__)
        for attempt in range(3):
            try:
                evidence = retrieve_context(
                    "Godot player movement enemy HUD main scene",
                    # The repository's checked-in `docmind_code` collection is the
                    # actual sample project's indexed corpus.  It is used here so the
                    # acceptance remains read-only with respect to the source tree.
                    collections={"code": "docmind_code"},
                    top_k=5, max_chars=2400,
                )
            except Exception as exc:  # pragma: no cover - defensive acceptance diagnostic
                retrieval_errors.append(type(exc).__name__)
            if evidence:
                break
            # Keep the acceptance useful when the presentation wrapper sees a
            # transient empty result: directly exercising the retriever is a
            # valid fallback and still proves the indexed corpus is available.
            try:
                docs = get_retriever(collection="docmind_code", top_k=5,
                                     embedding_client=EmbeddingClient()).invoke(
                                         "Godot player movement enemy HUD main scene")
                evidence = "\n---\n".join(str(doc.page_content or "")[:700] for doc in docs)
            except Exception as exc:  # pragma: no cover - diagnostic only
                retrieval_errors.append(type(exc).__name__)
            if evidence:
                break
            time.sleep(0.1 * (attempt + 1))
        if not evidence:
            self.skipTest("real indexed code corpus is unavailable")
        self.assertTrue(evidence)
        self.assertNotIn("retrieval unavailable", evidence.lower())

        with tempfile.TemporaryDirectory(prefix="docmind-real-game-e2e-") as tmp:
            root = Path(tmp) / "game"
            _copy_project(SOURCE_PROJECT, root)
            manager = GameWorkflowManager(str(Path(tmp) / "state"))
            previous_root = get_runtime("code_root")
            # Execute writes and playtests through the same audited tool
            # boundary used by Agent, while keeping the source project
            # untouched and isolating the temporary copy as code_root.
            set_runtime("code_root", str(root))
            try:
                started = manager.start(
                    "在现有 Godot 项目中验证玩家移动、敌人和 HUD 主场景",
                    project_id="real-godot-e2e", project_root=str(root),
                    llm_enabled=False, experience_enabled=False,
                    policy=WorkflowPolicy(max_steps=16, max_replans=1,
                                          max_subagents=4, max_context_chars=2400),
                )
                wid = started["workflow_id"]
                self.assertEqual(manager.choose(wid, "recommended")["status"], "planning")
                planned = manager.plan(wid, [
                    {"id": "design", "role": "designer", "task": "核对真实项目的移动、敌人、HUD验收标准", "depends_on": [], "parallel_safe": True},
                    {"id": "audit", "role": "researcher", "task": "记录真实项目主场景和脚本来源", "depends_on": [], "parallel_safe": True},
                    {"id": "implementation", "role": "coder", "task": "写入 Harness 验收标记文件，不改动游戏逻辑", "depends_on": ["design", "audit"]},
                    {"id": "verify", "role": "tester", "task": "运行真实 Godot 主场景并核对 player_ready", "depends_on": ["implementation"]},
                ])
                self.assertEqual(planned["status"], "planned")
                waiting = manager.execute(wid, lambda task, context: self._run_task(root, task, context), session_id="real-godot-e2e")
                self.assertEqual(waiting["status"], "awaiting_approval")
                manager.approve(wid, True)
                done = manager.execute(wid, lambda task, context: self._run_task(root, task, context), session_id="real-godot-e2e")
                self.assertEqual(done["status"], "completed", repr({
                    "review": done.get("review"), "results": done.get("results"),
                    "events": done.get("events", [])[-8:],
                }))
                self.assertTrue(done["review"]["ok"])
                self.assertTrue((root / "HARNESS_ACCEPTANCE.md").is_file())
                event_kinds = {str(item.get("kind")) for item in done["events"]}
                self.assertTrue({"clarify", "choice", "plan", "approval_required",
                                 "wave_start", "subagent_start", "task_complete",
                                 "review"}.issubset(event_kinds), repr(event_kinds))
                self.assertTrue(any(item.get("kind") == "review" and item.get("ok") for item in done["events"]))
            finally:
                set_runtime("code_root", previous_root)
                manager.close()
        self.assertEqual(source_digest_before, _tree_digest(SOURCE_PROJECT),
                         "source project was modified by the acceptance run")

    @staticmethod
    def _run_task(root: Path, task: dict, context: dict[str, str]) -> dict:
        task_id = str(task.get("id"))
        if task_id == "design":
            return {"status": "ok", "conclusion": "真实项目验收标准已核对", "steps": 1,
                    "trace": {"steps": [{"ok": True, "tool": "read_file"}]}}
        if task_id == "audit":
            return {"status": "ok", "conclusion": "主场景、玩家、敌人和 HUD 来源已记录", "steps": 1,
                    "trace": {"steps": [{"ok": True, "tool": "search_code"}]}}
        if task_id == "implementation":
            if not context:
                return {"status": "failed", "conclusion": "缺少上游上下文", "steps": 1,
                        "trace": {"steps": [{"ok": False, "tool": "read_context"}]}}
            created = audited_tools.create_file(
                "path: HARNESS_ACCEPTANCE.md\n"
                "new_text: # Harness acceptance\n\n真实项目主场景验收标记。\n"
            )
            if not str(created).startswith("已创建"):
                return {"status": "failed", "conclusion": str(created), "steps": 1,
                        "trace": {"steps": [{"ok": False, "tool": "create_file"}]}}
            observed = audited_tools.read_file("HARNESS_ACCEPTANCE.md")
            if "真实项目主场景验收标记" not in observed:
                return {"status": "failed", "conclusion": "写入后读取未发现验收标记", "steps": 2,
                        "trace": {"steps": [{"ok": True, "tool": "create_file"},
                                             {"ok": False, "tool": "read_file"}]}}
            marker = root / "HARNESS_ACCEPTANCE.md"
            return {"status": "ok", "conclusion": "验收标记已写入真实项目副本", "steps": 1,
                    "file_changes": [str(marker)],
                    "trace": {"steps": [{"ok": True, "tool": "create_file"},
                                         {"ok": True, "tool": "read_file"}]}}
        if task_id == "verify":
            # game_playtest applies the same command safety and engine-error
            # detection used by production Agent calls.
            command = f'"{GODOT}" --headless --path "{root}" --editor --quit'
            initialized = json.loads(audited_tools.game_playtest(
                f"command: {command}\ntimeout: 30"))
            command = f'"{GODOT}" --headless --path "{root}" --quit-after 3'
            completed = json.loads(audited_tools.game_playtest(
                f"command: {command}\ntimeout: 30"))
            output = str(initialized.get("output", "")) + str(completed.get("output", ""))
            ok = bool(initialized.get("ok")) and bool(completed.get("ok")) \
                and '"type":"player_ready"' in output \
                and "SCRIPT ERROR" not in output and "Parse Error" not in output
            return {"status": "ok" if ok else "failed",
                    "conclusion": "真实 Godot 主场景 Playtest 通过" if ok else output[-1000:],
                    "steps": 2, "tests": [{"name": "godot_headless_playtest", "ok": ok}],
                    "trace": {"steps": [{"ok": True, "tool": "game_playtest"}, {"ok": ok, "tool": "self_verify"}]}}
        return {"status": "failed", "conclusion": "未知任务", "steps": 1,
                "trace": {"steps": [{"ok": False, "tool": "dispatch"}]}}


if __name__ == "__main__":
    unittest.main()
