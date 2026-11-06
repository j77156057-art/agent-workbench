"""Opt-in Harness-level acceptance with a real provider and Godot project.

This is intentionally opt-in because it spends provider tokens and requires a
local Godot installation.  It keeps the real project's import cache in the
temporary copy and routes file changes and playtest through the audited tools.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_runtime.game_workflow import GameWorkflowManager, WorkflowPolicy
from agent_runtime.retrieval import retrieve_context
from agent_runtime.tools import execute_tool
from config import get_runtime, set_runtime
from llm import LLMClient
from tools import TOOLS


_PROJECT_ENV = "DOCMIND_REAL_GAME_PROJECT"
_ENGINE_ENV = "DOCMIND_GODOT_EXECUTABLE"
_LIVE_ENV = "DOCMIND_REAL_LLM_E2E"
SOURCE_PROJECT = Path(os.getenv(_PROJECT_ENV, "").strip() or r"<sample-project>")
GODOT = os.getenv(_ENGINE_ENV, "").strip()
_LIVE_ENABLED = (
    os.getenv(_LIVE_ENV, "").strip().lower() in {"1", "true", "yes"}
    and os.getenv("LLM_PROVIDER", "mock").strip().lower() not in {"", "mock"}
)


def _live_skip_reason() -> str:
    if os.getenv(_LIVE_ENV, "").strip().lower() not in {"1", "true", "yes"}:
        return "未设置 %s=1：实时 Harness E2E 需要真实 provider" % _LIVE_ENV
    return ("LLM_PROVIDER=%r 仍是 mock 或空：实时 Harness E2E 需要非 mock 的真实 provider"
            % os.getenv("LLM_PROVIDER", ""))


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


@unittest.skipUnless(_LIVE_ENABLED, _live_skip_reason())
class LiveRealHarnessE2ETests(unittest.TestCase):
    def test_provider_to_toolspec_to_godot_review(self):
        if not (SOURCE_PROJECT.is_dir() and (SOURCE_PROJECT / "project.godot").is_file()):
            self.skipTest(_project_skip_reason())
        if not GODOT or not Path(GODOT).is_file():
            self.skipTest(_engine_skip_reason())

        llm = LLMClient()
        request = (
            "在现有 Godot 项目中做一个最小 Harness 验收：确认玩家移动、敌人和 HUD "
            "相关结构，创建不影响原逻辑的验收标记，并运行主场景自测。"
        )
        evidence = retrieve_context(
            "Godot player movement enemy HUD main scene",
            collections={"code": "docmind_code"}, top_k=5, max_chars=1800,
        )

        def json_call(prompt: str):
            raw = llm.chat([{"role": "user", "content": prompt}],
                           stream=False, temperature=0.1)
            text = str(raw or "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            return json.loads(text)

        def options(user_request, _plan):
            return json.dumps(json_call(
                "你是游戏 Harness 的需求澄清器。根据用户目标和检索摘要生成 2 个互斥方案。"
                "只输出 JSON：{\"options\":[{\"id\":\"英文id\",\"title\":\"中文标题\","
                "\"summary\":\"一句话\",\"recommended\":true或false}]}，必须恰好一个推荐项。"
                "不执行工具，不编造资料。\n用户目标：" + user_request +
                "\n检索摘要：" + evidence), ensure_ascii=False)

        def tasks(user_request, selected, _plan):
            return json.dumps(json_call(
                "你是 Harness 主 Agent。生成 3 到 4 个任务：尽量让 designer 与 researcher 无依赖并行；"
                "实现任务依赖前置分析任务，tester 依赖实现任务。只输出 JSON，任务字段必须包含 id、role、task、"
                "depends_on、persona、tools、mcp、reflection；mcp=deny，reflection=true。不要执行工具。\n"
                "用户目标：" + user_request + "\n方案：" + json.dumps(selected, ensure_ascii=False) +
                "\n检索：" + evidence), ensure_ascii=False)

        with tempfile.TemporaryDirectory(prefix="docmind-live-real-e2e-") as temp:
            root = Path(temp) / "game"
            shutil.copytree(
                SOURCE_PROJECT, root,
                ignore=shutil.ignore_patterns(
                    ".git", ".docmind_engine.log", ".docmind_runtime.jsonl",
                    ".docmind_tasks.jsonl"),
            )
            old_root = get_runtime("code_root")
            set_runtime("code_root", str(root))
            manager = GameWorkflowManager(str(Path(temp) / "state"))
            calls = []
            try:
                started = manager.start(
                    request, project_id="live-real-e2e", project_root=str(root),
                    llm_enabled=True, experience_enabled=False,
                    policy=WorkflowPolicy(max_steps=18, max_replans=1,
                                          max_subagents=4, max_context_chars=2600),
                    option_generator=options, task_generator=tasks,
                )
                wid = started["workflow_id"]
                recommended = next(item for item in started["options"]
                                   if item.get("recommended"))
                chosen = manager.choose(wid, recommended["id"])
                planned = chosen if chosen.get("status") == "planned" else manager.plan(
                    wid, task_generator=tasks)
                self.assertEqual(planned["status"], "planned")
                # 子代理数量由主 Agent 动态决定；这里验收的是有界、可执行的
                # DAG，而不是把实现策略写死成固定数量。
                self.assertGreaterEqual(len(planned["tasks"]), 3)
                self.assertLessEqual(len(planned["tasks"]), 4)
                task_ids = {str(item.get("id")) for item in planned["tasks"]}
                self.assertEqual(len(task_ids), len(planned["tasks"]))
                self.assertTrue(all(set(item.get("depends_on") or []).issubset(task_ids)
                                    for item in planned["tasks"]))

                def tool(name: str, argument: str):
                    spec = TOOLS[name]
                    result = execute_tool(spec.func, argument,
                                          lambda text: any(marker in str(text) for marker in (
                                              "文件已存在", "写入失败", "拒绝", "参数缺失",
                                              "读取失败", "文件不存在", "未找到")),
                                          tool_name=name, side_effect=spec.side_effect)
                    calls.append({"tool": name, "ok": result.ok,
                                  "text": result.text[:500]})
                    return result.ok, result.text

                def runner(task, context):
                    raw_role = str(task.get("role") or "").strip().lower()
                    task_text = (raw_role + " " + str(task.get("task") or "")).lower()
                    # A real planner is free to name personas differently
                    # (reviewer/validator/implementer, etc.).  The acceptance
                    # runner maps those aliases by intent so the test checks
                    # harness behavior instead of depending on literal labels.
                    if raw_role in {"designer", "design", "architect", "产品"} or any(
                            token in task_text for token in ("design", "spec", "设计", "方案")):
                        role = "designer"
                    elif raw_role in {"researcher", "research", "analyst", "调研"} or any(
                            token in task_text for token in ("research", "audit", "检索", "调研", "核对结构")):
                        role = "researcher"
                    elif raw_role in {"tester", "test", "qa", "reviewer", "validator", "测试", "验收"} or any(
                            token in task_text for token in ("test", "playtest", "verify", "review", "验证", "验收", "自测", "运行")):
                        role = "tester"
                    elif raw_role in {"coder", "developer", "implementer", "engineer", "开发"} or any(
                            token in task_text for token in ("code", "build", "implement", "edit", "创建", "修改", "实现")):
                        role = "coder"
                    else:
                        # Unknown personas are still executable: terminal DAG
                        # nodes are treated as verification, other nodes as a
                        # safe implementation step.
                        role = "tester" if task.get("depends_on") else "coder"
                    if role == "designer":
                        path = "HARNESS_DESIGN_%s.md" % str(task.get("id") or "design")
                        ok, obs = tool(
                            "create_file",
                            "path: %s\nnew_text: # Acceptance design\n\n" % path +
                            str(task.get("task")) + "\n",
                        )
                        read_ok, read_obs = tool("read_file", path) if ok else (False, "")
                        return self._result(ok and read_ok, obs + "\n" + read_obs,
                                            "create_file", [path])
                    if role == "researcher":
                        path = "HARNESS_RESEARCH_%s.md" % str(task.get("id") or "research")
                        ok, obs = tool(
                            "create_file",
                            "path: %s\nnew_text: # Retrieved evidence\n\n" % path +
                            evidence[:1400] + "\n",
                        )
                        read_ok, read_obs = tool("read_file", path) if ok else (False, "")
                        return self._result(ok and read_ok, obs + "\n" + read_obs,
                                            "create_file", [path])
                    if role == "coder":
                        # Read first, then use the real guarded apply_edit path on
                        # an existing project file, plus create a new marker file.
                        read_ok, read_obs = tool("read_file", "project.godot")
                        if not read_ok:
                            return self._result(False, read_obs, "read_file", [])
                        ok_edit, edit_obs = tool(
                            "apply_edit",
                            "path: project.godot\nold_text: [application]\n"
                            "new_text: [application]\n; Harness acceptance marker\n",
                        )
                        ok_new, new_obs = tool(
                            "create_file",
                            "path: harness_acceptance_marker.gd\nnew_text: "
                            "extends Node\n\nfunc _ready():\n    print(\"harness_acceptance_marker\")\n",
                        )
                        ok = ok_edit and ok_new
                        return {
                            "status": "ok" if ok else "failed",
                            "conclusion": edit_obs + "\n" + new_obs,
                            "steps": 3,
                            "file_changes": ["project.godot", "harness_acceptance_marker.gd"],
                            "trace": {"steps": [
                                {"action": "read_file", "obs": read_obs, "ok": True},
                                {"action": "apply_edit", "obs": edit_obs, "ok": ok_edit},
                                {"action": "create_file", "obs": new_obs, "ok": ok_new},
                            ]},
                        }
                    if role == "tester":
                        # If the model collapsed implementation and
                        # verification into one persona, keep the acceptance
                        # meaningful by performing the guarded edit before
                        # playtest.  This mirrors a harness capability gate:
                        # verification cannot pass without an observable
                        # implementation artifact.
                        preflight = []
                        if not any(row["tool"] == "apply_edit" and row["ok"] for row in calls):
                            read_ok, read_obs = tool("read_file", "project.godot")
                            if not read_ok:
                                return self._result(False, read_obs, "read_file", [])
                            ok_edit, edit_obs = tool(
                                "apply_edit",
                                "path: project.godot\nold_text: [application]\n"
                                "new_text: [application]\n; Harness acceptance marker\n",
                            )
                            ok_new, new_obs = tool(
                                "create_file",
                                "path: harness_acceptance_marker.gd\nnew_text: "
                                "extends Node\n\nfunc _ready():\n    print(\"harness_acceptance_marker\")\n",
                            )
                            preflight = [
                                {"action": "read_file", "obs": read_obs, "ok": read_ok},
                                {"action": "apply_edit", "obs": edit_obs, "ok": ok_edit},
                                {"action": "create_file", "obs": new_obs, "ok": ok_new},
                            ]
                            if not (ok_edit and ok_new):
                                return {
                                    "status": "failed", "conclusion": edit_obs + "\n" + new_obs,
                                    "steps": 3, "file_changes": ["project.godot", "harness_acceptance_marker.gd"],
                                    "trace": {"steps": preflight},
                                }
                        ok, obs = tool(
                            "game_playtest",
                            f'command: "{GODOT}" --headless --path . --quit-after 3\n'
                            "timeout: 30",
                        )
                        return {
                            "status": "ok" if ok else "failed",
                            "conclusion": obs[-1400:], "steps": 1,
                            "tests": [{"name": "godot_headless_playtest", "ok": ok}],
                            "trace": {"steps": preflight + [{"action": "game_playtest",
                                                   "obs": obs[-800:], "ok": ok}]},
                        }
                    return {"status": "failed", "error": "unexpected role: " + role,
                            "steps": 1, "trace": {"steps": [{"ok": False}]}}

                waiting = manager.execute(
                    wid, runner,
                    synth_runner=lambda _tasks, results: "\n".join(
                        str((results.get(task["id"]) or {}).get("conclusion") or "")[:300]
                        for task in _tasks),
                    session_id="live-real-e2e",
                )
                self.assertEqual(waiting["status"], "awaiting_approval")
                manager.approve(wid, True)
                done = manager.execute(
                    wid, runner,
                    synth_runner=lambda _tasks, results: "\n".join(
                        str((results.get(task["id"]) or {}).get("conclusion") or "")[:300]
                        for task in _tasks),
                    session_id="live-real-e2e",
                )
                self.assertEqual(done["status"], "completed", repr(done.get("review")))
                self.assertTrue(done["review"]["ok"])
                self.assertTrue(any(row["tool"] == "apply_edit" and row["ok"] for row in calls))
                self.assertTrue(any(row["tool"] == "game_playtest" and row["ok"] for row in calls))
                self.assertTrue((root / "harness_acceptance_marker.gd").is_file())
                event_kinds = [str(item.get("kind")) for item in done.get("events", [])]
                self.assertIn("wave_start", event_kinds)
                self.assertIn("subagent_start", event_kinds)
                self.assertIn("subagent_complete", event_kinds)
                self.assertIn("wave_complete", event_kinds)
                self.assertTrue(any(item.get("kind") == "context_prepared"
                                    for item in done.get("events", [])))
            finally:
                manager.close()
                set_runtime("code_root", old_root or "")

    @staticmethod
    def _result(ok, observation, action, files):
        return {
            "status": "ok" if ok else "failed", "conclusion": observation,
            "steps": 1, "file_changes": files,
            "trace": {"steps": [{"action": action, "obs": observation, "ok": ok}]},
        }


if __name__ == "__main__":
    unittest.main()
