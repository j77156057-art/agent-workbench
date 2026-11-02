"""Offline end-to-end acceptance for a minimal game project.

The fixture deliberately uses the real workflow manager and real files, while
the runner is a deterministic stand-in for the existing audited tool layer.
This keeps CI offline but still verifies the user-visible lifecycle end to end.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_runtime.game_workflow import GameWorkflowManager, WorkflowPolicy


class GameWorkflowEndToEndTests(unittest.TestCase):
    def test_godot_fixture_runs_from_clarification_to_review(self):
        with tempfile.TemporaryDirectory(prefix="docmind_game_e2e_") as tmp:
            root = Path(tmp) / "game"
            state_root = Path(tmp) / "state"
            root.mkdir()
            (root / "project.godot").write_text(
                "[application]\nconfig/name=Harness E2E\nrun/main_scene=\"res://main.tscn\"\n",
                encoding="utf-8",
            )
            manager = GameWorkflowManager(str(state_root))
            try:
                started = manager.start(
                    "制作一个 Godot 4 的 2D 移动和跳跃原型",
                    project_id="e2e-game",
                    project_root=str(root),
                    llm_enabled=False,
                    experience_enabled=True,
                    policy=WorkflowPolicy(max_steps=12, max_replans=1,
                                          max_subagents=4, max_context_chars=1800),
                )
                workflow_id = started["workflow_id"]
                self.assertEqual(started["status"], "awaiting_choice")
                self.assertIn("code", started["sources"])

                chosen = manager.choose(workflow_id, "recommended")
                self.assertEqual(chosen["selected_option"]["id"], "recommended")

                planned = manager.plan(workflow_id, [
                    {"id": "design", "role": "designer", "task": "明确移动、跳跃和场景验收标准",
                     "depends_on": [], "parallel_safe": True},
                    {"id": "art", "role": "artist", "task": "创建最小占位图标",
                     "depends_on": [], "parallel_safe": True},
                    {"id": "implementation", "role": "coder", "task": "创建 Godot 场景和角色脚本",
                     "depends_on": ["design", "art"]},
                    {"id": "verify", "role": "tester", "task": "运行离线静态自测并核对入口场景",
                     "depends_on": ["implementation"]},
                ])
                self.assertEqual(planned["status"], "planned")
                self.assertEqual(planned["context_layers"]["task"]["count"], 4)

                waiting = manager.execute(
                    workflow_id,
                    lambda task, context: self._run_fixture_task(root, task, context),
                    synth_runner=lambda _tasks, results: "\n".join(
                        str(item.get("conclusion") or "") for item in results.values()),
                    session_id="e2e-session",
                )
                self.assertEqual(waiting["status"], "awaiting_approval")
                self.assertTrue(any(item["kind"] == "approval_required"
                                    for item in waiting["events"]))

                approved = manager.approve(workflow_id, True)
                self.assertEqual(approved["status"], "planned")
                done = manager.execute(
                    workflow_id,
                    lambda task, context: self._run_fixture_task(root, task, context),
                    synth_runner=lambda _tasks, results: "\n".join(
                        str(item.get("conclusion") or "") for item in results.values()),
                    session_id="e2e-session",
                )
                self.assertEqual(done["status"], "completed")
                self.assertTrue(done["review"]["ok"])
                self.assertEqual(done["context_layers"]["subagent"]["count"], 4)
                self.assertTrue(done["context_layers"]["output"]["review_ok"])
                self.assertTrue(any(item["kind"] == "wave" for item in done["events"]))
                self.assertTrue(any(item["kind"] == "review" and item["ok"]
                                    for item in done["events"]))

                self.assertTrue((root / "main.tscn").is_file())
                self.assertTrue((root / "player.gd").is_file())
                self.assertTrue((root / "icon.svg").is_file())
                self.assertTrue((root / "tests" / "test_fixture.py").is_file())
                result_rows = done["results"]["results"]
                self.assertEqual({row["status"] for row in result_rows.values()}, {"ok"})
                self.assertTrue(any(item.get("ok") for item in result_rows["verify"]["tests"]))
                trace = done["langsmith_trace"]
                if trace.get("enabled"):
                    self.assertEqual(trace.get("event_count", 0), len(done["events"]))
            finally:
                manager.close()

    @staticmethod
    def _run_fixture_task(root: Path, task: dict, context: dict[str, str]) -> dict:
        task_id = str(task.get("id"))
        if task_id == "design":
            path = root / "DESIGN.md"
            path.write_text(
                "# Movement Prototype\n\n- Arrow keys move the player.\n- Space triggers jump.\n",
                encoding="utf-8",
            )
            return {"status": "ok", "conclusion": "玩法和验收标准已确定",
                    "steps": 1, "file_changes": [str(path)],
                    "trace": {"steps": [{"ok": True, "tool": "write_file"}]}}
        if task_id == "art":
            path = root / "icon.svg"
            path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">'
                '<rect width="16" height="16" fill="#4c8bf5"/></svg>',
                encoding="utf-8",
            )
            return {"status": "ok", "conclusion": "占位图标已创建",
                    "steps": 1, "file_changes": [str(path)],
                    "trace": {"steps": [{"ok": True, "tool": "write_file"}]}}
        if task_id == "implementation":
            design = (root / "DESIGN.md").read_text(encoding="utf-8")
            if "Space" not in design or not context:
                return {"status": "failed", "conclusion": "缺少设计上下文", "steps": 1,
                        "trace": {"steps": [{"ok": False, "tool": "read_file"}]}}
            scene = root / "main.tscn"
            player = root / "player.gd"
            scene.write_text(
                '[gd_scene load_steps=2 format=3]\n\n'
                '[ext_resource path="res://player.gd" type="Script" id="1"]\n\n'
                '[node name="Main" type="Node2D"]\n\n'
                '[node name="Player" type="CharacterBody2D" parent="."]\nscript = ExtResource("1")\n',
                encoding="utf-8",
            )
            player.write_text(
                "extends CharacterBody2D\n\nvar speed := 220.0\n\n"
                "func _physics_process(_delta):\n"
                "    velocity.x = Input.get_axis(\"ui_left\", \"ui_right\") * speed\n"
                "    if Input.is_action_just_pressed(\"ui_accept\"):\n"
                "        velocity.y = -320.0\n"
                "    move_and_slide()\n",
                encoding="utf-8",
            )
            return {"status": "ok", "conclusion": "Godot 场景和角色脚本已创建",
                    "steps": 2, "file_changes": [str(scene), str(player)],
                    "trace": {"steps": [
                        {"ok": True, "tool": "read_file"},
                        {"ok": True, "tool": "write_file"},
                    ]}}
        if task_id == "verify":
            tests_dir = root / "tests"
            tests_dir.mkdir(exist_ok=True)
            test_file = tests_dir / "test_fixture.py"
            test_file.write_text(
                "from pathlib import Path\n"
                "root = Path(__file__).parents[1]\n"
                "assert 'run/main_scene' in (root / 'project.godot').read_text()\n"
                "assert 'CharacterBody2D' in (root / 'player.gd').read_text()\n"
                "assert (root / 'main.tscn').is_file()\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, "-m", "py_compile", str(test_file)],
                cwd=str(root), capture_output=True, text=True, timeout=10,
            )
            ok = completed.returncode == 0
            return {"status": "ok" if ok else "failed",
                    "conclusion": "离线静态自测通过" if ok else completed.stderr,
                    "steps": 2,
                    "file_changes": [str(test_file)],
                    "tests": [{"name": "py_compile", "ok": ok}],
                    "trace": {"steps": [
                        {"ok": True, "tool": "write_file"},
                        {"ok": ok, "tool": "game_playtest"},
                    ]}}
        return {"status": "failed", "conclusion": "未知任务", "steps": 1,
                "trace": {"steps": [{"ok": False, "tool": "dispatch"}]}}


if __name__ == "__main__":
    unittest.main()
