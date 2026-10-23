import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.game_workflow import GameWorkflowManager, WorkflowError
from agent_runtime.project_stage import ProjectStageError, apply_stage, cleanup_stage, create_stage, stage_changes


class ProjectStageTests(unittest.TestCase):
    def test_changes_are_applied_only_after_exact_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary, "project")
            state = Path(temporary, "state")
            project.mkdir()
            (project / "app.py").write_text("print(1)\n", encoding="utf-8")
            stage = create_stage(str(project), str(state), "wf-stage")
            workspace = Path(stage["workspace_root"])
            (workspace / "app.py").write_text("print(2)\n", encoding="utf-8")
            changes = stage_changes(stage)
            self.assertEqual(changes[0]["status"], "modified")
            with self.assertRaises(ProjectStageError):
                apply_stage(stage, [])
            result = apply_stage(stage, changes)
            self.assertTrue(result["ok"])
            self.assertEqual((project / "app.py").read_text(encoding="utf-8"), "print(2)\n")

    def test_original_change_after_review_blocks_merge(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary, "project")
            project.mkdir()
            (project / "app.py").write_text("print(1)\n", encoding="utf-8")
            stage = create_stage(str(project), str(Path(temporary, "state")), "wf-stage")
            workspace = Path(stage["workspace_root"])
            (workspace / "app.py").write_text("print(2)\n", encoding="utf-8")
            changes = stage_changes(stage)
            (project / "app.py").write_text("print(3)\n", encoding="utf-8")
            with self.assertRaises(ProjectStageError):
                apply_stage(stage, changes)

    def test_cleanup_only_removes_valid_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary, "project")
            state = Path(temporary, "state")
            project.mkdir()
            (project / "app.py").write_text("safe", encoding="utf-8")
            stage = create_stage(str(project), str(state), "wf-stage")
            forged = dict(stage, workspace_root=str(project))
            with self.assertRaises(ProjectStageError):
                cleanup_stage(forged, str(state))
            self.assertTrue(Path(stage["workspace_root"]).is_dir())
            cleanup_stage(stage, str(state))
            self.assertFalse(Path(stage["workspace_root"]).exists())
            self.assertEqual((project / "app.py").read_text(encoding="utf-8"), "safe")

    def test_rereview_keeps_agent_failure_out_of_apply_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary, "project")
            project.mkdir()
            (project / "app.py").write_text("before", encoding="utf-8")
            manager = GameWorkflowManager(str(Path(temporary, "state")))
            try:
                workflow_id = manager.start("修改文件", project_root=str(project),
                                            stage_enabled=True)["workflow_id"]
                state = manager._load(workflow_id)
                state.project_stage = create_stage(str(project), str(manager.state_root.parent), workflow_id)
                Path(state.project_stage["workspace_root"], "app.py").write_text("after", encoding="utf-8")
                state.results = {"ok": True}
                state.review = {"ok": False, "agent_ok": False}
                state.status = "failed"
                manager._save(state)
                project_review = {"ok": True, "status": "passed", "independent": True,
                                  "changes": stage_changes(state.project_stage), "tests": []}
                with patch.object(manager, "_review_project", return_value=project_review):
                    manager.rerun_project_review(workflow_id)
                with self.assertRaises(WorkflowError):
                    manager.apply_project_stage(workflow_id, approved=True)
                self.assertEqual((project / "app.py").read_text(encoding="utf-8"), "before")
                manager.cleanup_project_stage(workflow_id)
                self.assertFalse(Path(state.project_stage["workspace_root"]).exists())
            finally:
                manager.close()

    def test_review_reads_evidence_from_the_stage_copy(self):
        """stage 里产出的截图/日志，复核时必须按副本根去找。

        旧行为写死 `code_root=state.project_root`：子代理在副本里写下的
        `.docmind/screenshots/click.png` 在真实项目里不存在，证据审计判
        「引用打不开」——stage 跑得再对也过不了复核；运行期取证同样读不到
        副本里的 engine.log，被判「没跑过」。
        """
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary, "project")
            project.mkdir()
            (project / "app.py").write_text("print(1)\n", encoding="utf-8")
            manager = GameWorkflowManager(str(Path(temporary, "state")))
            try:
                wid = manager.start("运行游戏并截图核验", project_root=str(project),
                                    stage_enabled=True)["workflow_id"]
                manager.choose(wid, "recommended")
                manager.plan(wid, [{"id": "a", "role": "tester", "task": "点击并截图"}])
                manager.execute(wid, lambda *_: {
                    "status": "ok", "conclusion": "未执行", "steps": 0})
                state = manager._load(wid)
                workspace = Path(state.project_stage["workspace_root"])
                shot = workspace / ".docmind" / "screenshots" / "click.png"
                shot.parent.mkdir(parents=True, exist_ok=True)
                shot.write_bytes(b"\x89PNG\r\n\x1a\n")
                manager.approve(wid, True)
                project_review = {"ok": True, "status": "passed", "independent": True,
                                  "changes": stage_changes(state.project_stage), "tests": []}
                with patch.object(manager, "_review_project", return_value=project_review):
                    done = manager.execute(wid, lambda *_: {
                        "status": "ok",
                        "conclusion": "点击截图：.docmind/screenshots/click.png",
                        "steps": 1})
                checks = done["review"]["evidence_audit"]["checks"]
                self.assertTrue(checks["references_exist"],
                                "复核拿真实项目根去找副本截图，正是 stage 下的假失败")
                self.assertFalse((project / ".docmind" / "screenshots" / "click.png").exists())
            finally:
                manager.close()

    def test_review_root_falls_back_when_stage_workspace_is_gone(self):
        from agent_runtime.game_workflow import _review_code_root

        with tempfile.TemporaryDirectory() as temporary:
            project = os.path.join(temporary, "project")
            os.makedirs(project)
            state = type("S", (), {"project_root": project,
                                   "project_stage": {"workspace_root":
                                                     os.path.join(temporary, "gone")}})()
            self.assertEqual(_review_code_root(state), project)
            state.project_stage = {"workspace_root": project}
            self.assertEqual(_review_code_root(state), project)


if __name__ == "__main__":
    unittest.main()
