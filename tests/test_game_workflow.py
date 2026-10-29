import tempfile
import time
import unittest
import os
from unittest.mock import patch

from agent_runtime.game_workflow import GameWorkflowManager, WorkflowError, WorkflowPolicy, review_output
from agent_runtime.tools import SideEffect, execute_tool, tool_idempotency_scope
from agent_runtime.context_router import (allocate_layer_budgets, compress_context,
                                           compress_context_async, compress_layers,
                                           compress_text, layer_snapshot_digest,
                                           summarize_subagent_result,
                                           compress_layers_durable, persistent_compression_worker)
from agent_runtime.workflow_eval import compare_evaluation, evaluate_workflow


def _wait_terminal(manager, wid, timeout=10.0):
    """Wait for a workflow whose execution runs on a manager daemon thread."""
    deadline = time.time() + timeout
    while True:
        status = manager.get(wid)
        if status["status"] in {"completed", "failed", "interrupted"}:
            return status
        if time.time() >= deadline:
            raise AssertionError("workflow %s stuck in %s" % (wid, status["status"]))
        time.sleep(0.01)


class GameWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.manager = GameWorkflowManager(tempfile.mkdtemp())

    def test_choice_research_and_custom_paths(self):
        first = self.manager.start("做一个 2D 横版游戏原型", project_root="D:/project")
        self.assertEqual(first["status"], "awaiting_choice")
        wid = first["workflow_id"]
        waiting = self.manager.choose(wid, "web_research")
        self.assertEqual(waiting["status"], "awaiting_research")
        chosen = self.manager.apply_research(wid, "Godot 4 官方文档摘要")
        self.assertEqual(chosen["status"], "awaiting_choice")
        custom = self.manager.choose(wid, "custom", custom_request="只做一个可运行的移动和跳跃样例")
        self.assertEqual(custom["status"], "awaiting_choice")

    def test_plan_approval_execute_and_review(self):
        state = self.manager.start("创建一个最小游戏原型", policy=WorkflowPolicy(max_subagents=2))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        planned = self.manager.plan(wid, [{"id": "a", "role": "tester", "task": "验证输入"}])
        self.assertEqual(planned["status"], "planned")
        blocked = self.manager.execute(wid, lambda task, context: {
            "status": "ok", "conclusion": "验证通过", "steps": 1})
        self.assertEqual(blocked["status"], "awaiting_approval")
        self.manager.approve(wid, True)
        done = self.manager.execute(wid, lambda task, context: {
            "status": "ok", "conclusion": "验证通过", "steps": 1})
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["review"]["ok"])

    def test_acceptance_contract_survives_restart_and_requires_user_decision(self):
        state_root = tempfile.mkdtemp()
        first = GameWorkflowManager(state_root)
        wid = first.start("完成可检查的项目功能")["workflow_id"]
        first.choose(wid, "recommended")
        planned = first.plan(wid, [{"id": "a", "role": "tester", "task": "运行项目并检查功能"}])
        self.assertEqual(planned["acceptance_contract"]["approved_revision"], 0)
        item = dict(planned["acceptance_contract"]["items"][0],
                    statement="项目运行成功且功能可用", method="运行自动测试",
                    evidence=["tests/report.json"])
        edited = first.set_acceptance_contract(wid, [item])
        restarted = GameWorkflowManager(state_root)
        self.assertEqual(restarted.get(wid)["acceptance_contract"]["items"][0]["statement"],
                         "项目运行成功且功能可用")
        self.assertEqual(edited["acceptance_contract"]["revision"], 2)
        with self.assertRaises(WorkflowError):
            restarted.decide_acceptance(wid, True)
        restarted.execute(wid, lambda *_: {"status": "ok", "conclusion": "通过", "steps": 1})
        approved = restarted.approve(wid, True)
        self.assertEqual(approved["acceptance_contract"]["approved_revision"], 2)
        self.assertTrue(approved["acceptance_contract"]["items"][0]["user_approved"])
        with self.assertRaises(WorkflowError):
            restarted.decide_acceptance(wid, True)
        done = restarted.execute(wid, lambda *_: {"status": "ok", "conclusion": "通过", "steps": 1})
        self.assertEqual(done["acceptance_contract"]["final_decision"], "pending")
        decided = restarted.decide_acceptance(wid, False, "还需要检查界面效果")
        self.assertEqual(decided["acceptance_contract"]["final_decision"], "rejected")
        self.assertEqual(GameWorkflowManager(state_root).get(wid)["acceptance_contract"]["final_note"],
                         "还需要检查界面效果")

    def test_preview_artifacts_survive_manager_restart(self):
        state_root = tempfile.mkdtemp()
        project_root = tempfile.mkdtemp()
        capture = os.path.join(project_root, "evidence", "screen.png")
        os.makedirs(os.path.dirname(capture), exist_ok=True)
        with open(capture, "wb") as fh:
            fh.write(b"png-evidence")

        first = GameWorkflowManager(state_root)
        wid = first.start("验证项目画面", project_root=project_root)["workflow_id"]
        state = first._load(wid)
        state.results = {"results": {"visual": {
            "status": "ok",
            "artifacts": [{"id": "screen", "path": capture,
                           "kind": "image", "mime": "image/png"}],
        }}}
        state.review = {"ok": True, "summary": "画面验收通过"}
        first._save(state)

        restarted = GameWorkflowManager(state_root)
        preview = restarted.get(wid)["preview"]
        self.assertEqual(preview["counts"]["artifacts"], 1)
        self.assertEqual(preview["artifacts"][0]["id"], "screen")
        self.assertTrue(os.path.isfile(preview["artifacts"][0]["path"]))

    def test_project_checkpoint_restores_baseline_without_deleting_new_files(self):
        state_root = tempfile.mkdtemp()
        project_root = tempfile.mkdtemp()
        source = os.path.join(project_root, "src", "main.py")
        os.makedirs(os.path.dirname(source), exist_ok=True)
        with open(source, "w", encoding="utf-8") as fh:
            fh.write("before\n")
        with open(os.path.join(project_root, ".env"), "w", encoding="utf-8") as fh:
            fh.write("API_KEY=should-not-be-snapshotted\n")
        with open(os.path.join(project_root, "private.pem"), "w", encoding="utf-8") as fh:
            fh.write("PRIVATE KEY\n")
        manager = GameWorkflowManager(state_root)
        wid = manager.start("修改项目", project_root=project_root)["workflow_id"]
        checkpoint = manager.create_project_checkpoint(wid)
        self.assertEqual(checkpoint["file_count"], 1)
        with open(source, "w", encoding="utf-8") as fh:
            fh.write("after\n")
        created = os.path.join(project_root, "generated.txt")
        with open(created, "w", encoding="utf-8") as fh:
            fh.write("keep\n")
        restored = manager.rollback_project_checkpoint(wid, approved=True)
        self.assertTrue(restored["ok"])
        with open(source, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "before\n")
        self.assertTrue(os.path.isfile(created), "回滚不应删除快照后新建的文件")

    def test_acceptance_contract_rejects_empty_and_duplicate_criteria(self):
        wid = self.manager.start("检查验收条件")["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "a", "task": "验证"}])
        with self.assertRaises(WorkflowError):
            self.manager.set_acceptance_contract(wid, [])
        with self.assertRaises(WorkflowError):
            self.manager.set_acceptance_contract(wid, [
                {"id": "same", "statement": "条件一", "required": True},
                {"id": "same", "statement": "条件二", "required": True},
            ])

    def test_plan_change_revokes_acceptance_approval_and_preserves_user_criteria(self):
        wid = self.manager.start("改进项目功能")["workflow_id"]
        self.manager.choose(wid, "recommended")
        planned = self.manager.plan(wid, [{"id": "a", "task": "实现功能"}])
        item = dict(planned["acceptance_contract"]["items"][0], statement="用户确认界面效果")
        self.manager.set_acceptance_contract(wid, [item])
        self.manager.execute(wid, lambda *_: {"status": "ok", "conclusion": "ok", "steps": 1})
        self.manager.approve(wid, True)
        self.manager.request_dag_revision(wid, [{"id": "b", "role": "coder", "task": "加入新功能"}])
        revised = self.manager.approve_dag_revision(wid, True)
        contract = revised["acceptance_contract"]
        self.assertEqual(contract["items"][0]["statement"], "用户确认界面效果")
        self.assertEqual(contract["approved_revision"], 0)
        self.assertFalse(contract["items"][0]["user_approved"])
        self.assertEqual(revised["policy"]["approval_mode"], "safe")

    def test_workflow_persists_generic_preview_after_execution(self):
        wid = self.manager.start("生成一个通用证据")["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "a", "role": "tester", "task": "验证资源"}])
        runner = lambda *_: {
            "status": "ok", "conclusion": "验证完成", "steps": 1,
            "artifacts": [{"path": "reports/result.json", "kind": "structured",
                           "after": '{"ok": true}', "evidence": ["report"]}],
        }
        self.manager.execute(wid, runner)
        self.manager.approve(wid, True)
        done = self.manager.execute(wid, runner)
        self.assertEqual(done["preview"]["schema"], "docmind.preview.v1")
        self.assertEqual(done["preview"]["counts"]["artifacts"], 1)
        loaded = self.manager.get(wid)
        self.assertEqual(loaded["preview"]["artifacts"][0]["kind"], "structured")

    def test_visual_feedback_persists_with_workflow_and_bounds_region(self):
        state_root = tempfile.mkdtemp()
        manager = GameWorkflowManager(state_root)
        wid = manager.start("调整画面")['workflow_id']
        saved = manager.record_visual_feedback(wid, {
            "id": "feedback-1", "label": "预览", "note": "按钮太靠右",
            "region": {"x": 110, "y": 10, "width": 20, "height": 15},
            "screenshot": True,
        })
        self.assertEqual(saved["feedback"]["region"]["x"], 100.0)
        restored = GameWorkflowManager(state_root).get(wid)
        self.assertEqual(restored["visual_feedback"][0]["note"], "按钮太靠右")
        self.assertFalse(restored["visual_feedback"][0]["screenshot"])
        with self.assertRaises(WorkflowError):
            manager.record_visual_feedback(wid, {"note": "bad", "region": {"x": "oops"}})

    def test_approval_can_resume_and_execute_in_one_call_when_callback_survives(self):
        state = self.manager.start("自动审批后执行", policy=WorkflowPolicy(max_subagents=1))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "a", "role": "tester", "task": "验证"}])
        self.manager.execute(wid, lambda _task, _context: {
            "status": "ok", "conclusion": "自动完成", "steps": 1})
        approved = self.manager.approve(wid, True, auto_execute=True)
        # The DAG now runs on a daemon thread: approval returns planned at once
        # and completion arrives through state/events instead of blocking HTTP.
        self.assertEqual(approved["status"], "planned")
        self.assertTrue(any(event["kind"] == "auto_execute_start" for event in approved["events"]))
        done = _wait_terminal(self.manager, wid)
        self.assertEqual(done["status"], "completed")

    def test_execute_without_request_session_persists_recoverable_id(self):
        """批准后直接执行的路径没有请求 session，也必须落可恢复 id。

        真 bug：execute() 只在 session_id 非空时写 execution_session_id，
        而 UI 的「批准」走 approve(auto_execute=True)，从不传 session。进程
        重启后 recover_pending 因空 id 跳过它，工作流永远卡在 executing。
        """
        state = self.manager.start("无请求 session", policy=WorkflowPolicy(approval_mode="high"))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "a", "role": "tester", "task": "验证"}])
        done = self.manager.execute(wid, lambda *_: {
            "status": "ok", "conclusion": "ok", "steps": 1})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.manager.get(wid)["execution_session_id"], "workflow")

    def test_approve_auto_execute_persists_recoverable_id(self):
        manager = GameWorkflowManager(tempfile.mkdtemp())
        state = manager.start("批准即跑", policy=WorkflowPolicy(max_subagents=1))
        wid = state["workflow_id"]
        manager.choose(wid, "recommended")
        manager.plan(wid, [{"id": "a", "role": "tester", "task": "验证"}])
        manager.set_execution_resolver(lambda _state: {
            "runner": lambda *_: {"status": "ok", "conclusion": "批准后完成", "steps": 1}})
        approved = manager.approve(wid, True, auto_execute=True)
        self.assertEqual(approved["status"], "planned")
        done = _wait_terminal(manager, wid)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(manager.get(wid)["execution_session_id"], "workflow")

    def test_choose_auto_execute_returns_planning_and_runs_to_completion(self):
        from agent_runtime import game_workflow as gw
        if gw.StateGraph is None:
            self.skipTest("LangGraph not installed")
        manager = self.manager
        state = manager.start(
            "选完就自动开干", policy=WorkflowPolicy(max_subagents=1),
            task_generator=lambda prompt, selected, context_plan:
                '{"tasks":[{"id":"a","role":"tester","task":"跑一遍校验",'
                '"depends_on":[],"optional":false,"parallel_safe":true}]}')
        wid = state["workflow_id"]
        manager.set_execution_resolver(lambda _state: {
            "runner": lambda _task, _context: {
                "status": "ok", "conclusion": "自动跑完", "steps": 1}})
        choosing = manager.choose(wid, "recommended", auto_execute=True)
        # The choice HTTP call returns immediately with the planning state;
        # planner + approval gate + execution all continue in the background.
        self.assertEqual(choosing["status"], "planning")
        done = _wait_terminal(manager, wid)
        self.assertEqual(done["status"], "completed")
        self.assertTrue(any(event["kind"] == "plan" for event in done["events"]))
        self.assertTrue(any(event["kind"] == "execute_start" for event in done["events"]))

    def test_choose_rules_while_options_still_generating(self):
        from agent_runtime import game_workflow as gw
        if gw.StateGraph is None:
            self.skipTest("LangGraph not installed")
        state = self.manager.start(
            "方案精炼中也能直接选", policy=WorkflowPolicy(max_subagents=1),
            option_generator=lambda prompt, plan: '{"options":[]}',
            defer_option_generation=True, llm_enabled=True)
        if state["status"] != "generating_options":
            self.skipTest("deferred option generation unavailable")
        wid = state["workflow_id"]
        # Strict callers (tests/other integrations) still require ready options
        with self.assertRaises(WorkflowError):
            self.manager.choose(wid, "recommended")
        # During refinement only an existing direct option is accepted
        with self.assertRaises(WorkflowError):
            self.manager.choose(wid, "web_research", auto_execute=True)
        with self.assertRaises(WorkflowError):
            self.manager.choose(wid, "custom", auto_execute=True, custom_request="自定义")
        with self.assertRaises(WorkflowError):
            self.manager.choose(wid, "missing-id", auto_execute=True)

    def test_successful_workflow_creates_skill_candidate_without_auto_saving(self):
        state = self.manager.start("沉淀可复用原型流程", experience_enabled=True,
                                   policy=WorkflowPolicy(approval_mode="high"))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "a", "task": "验证"}])
        done = self.manager.execute(wid, lambda *_: {
            "status": "ok", "conclusion": "通过", "steps": 1})
        self.assertEqual(done["status"], "completed")
        candidate = done["skill_candidate"]
        self.assertEqual(candidate["status"], "pending_approval")
        self.assertFalse(any(item.get("source") == "user" and
                             item.get("name") == candidate["name"]
                             for item in __import__("skills").list_skills().get("items", [])))

    def test_workflow_evaluator_and_regression_compare(self):
        state = self.manager.start("评估流程", policy=WorkflowPolicy(approval_mode="high"))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "a", "task": "验证"}])
        done = self.manager.execute(wid, lambda *_: {
            "status": "ok", "conclusion": "通过", "steps": 1})
        evaluation = self.manager.evaluate(wid)
        self.assertTrue(evaluation["passed"])
        degraded = dict(evaluation, passed=False,
                        checks=[dict(item, ok=False) if item["name"] == "review_ok" else item
                                for item in evaluation["checks"]])
        regression = compare_evaluation(degraded, evaluation)
        self.assertTrue(regression["regressed"])

    def test_auto_execute_after_restart_degrades_to_explicit_execute(self):
        state_root = tempfile.mkdtemp()
        first = GameWorkflowManager(state_root)
        state = first.start("重启后审批", policy=WorkflowPolicy(max_subagents=1))
        wid = state["workflow_id"]
        first.choose(wid, "recommended")
        first.plan(wid, [{"id": "a", "task": "验证"}])
        first.execute(wid, lambda *_: {"status": "ok", "conclusion": "ok", "steps": 1})
        restarted = GameWorkflowManager(state_root)
        approved = restarted.approve(wid, True, auto_execute=True)
        self.assertEqual(approved["status"], "planned")
        self.assertTrue(any(event["kind"] == "auto_execute_unavailable" for event in approved["events"]))

    def test_auto_execute_after_restart_rebuilds_callbacks_when_resolver_is_configured(self):
        state_root = tempfile.mkdtemp()
        first = GameWorkflowManager(state_root)
        state = first.start("重启后恢复执行", policy=WorkflowPolicy(max_subagents=1))
        wid = state["workflow_id"]
        first.choose(wid, "recommended")
        first.plan(wid, [{"id": "a", "task": "验证"}])
        first.execute(wid, lambda *_: {"status": "ok", "conclusion": "ok", "steps": 1},
                      session_id="session-rebuilt")
        restarted = GameWorkflowManager(state_root)
        restarted.set_execution_resolver(lambda _state: {
            "runner": lambda *_: {"status": "ok", "conclusion": "恢复后完成", "steps": 1}})
        approved = restarted.approve(wid, True, auto_execute=True)
        self.assertEqual(approved["status"], "planned")
        done = _wait_terminal(restarted, wid)
        self.assertEqual(done["status"], "completed")
        self.assertTrue(any(event["kind"] == "auto_execute_reconstructed"
                            for event in done["events"]))

    def test_auto_execute_resolves_callbacks_when_gate_left_empty_placeholder(self):
        state_root = tempfile.mkdtemp()
        manager = GameWorkflowManager(state_root)
        state = manager.start("修订后再审批", policy=WorkflowPolicy(max_subagents=1))
        wid = state["workflow_id"]
        manager.choose(wid, "recommended")
        manager.plan(wid, [{"id": "seed", "role": "tester", "task": "初始验证"}])
        manager.request_dag_revision(wid, [
            {"id": "a", "role": "tester", "task": "验证", "tools": ["engine_logs"]}])
        manager.approve_dag_revision(wid, True)
        manager.set_execution_resolver(lambda _state: {
            "runner": lambda *_: {"status": "ok", "conclusion": "占位后仍能执行", "steps": 1}})
        approved = manager.approve(wid, True, auto_execute=True)
        self.assertEqual(approved["status"], "planned")
        done = _wait_terminal(manager, wid)
        self.assertEqual(done["status"], "completed")
        self.assertTrue(any(event["kind"] == "auto_execute_reconstructed"
                            for event in done["events"]))

    def test_startup_recovery_scheduler_resumes_approved_workflow(self):
        state_root = tempfile.mkdtemp()
        first = GameWorkflowManager(state_root)
        state = first.start("启动恢复调度", policy=WorkflowPolicy(max_subagents=1))
        wid = state["workflow_id"]
        first.choose(wid, "recommended")
        first.plan(wid, [{"id": "a", "task": "验证"}])
        first.execute(wid, lambda *_: {"status": "ok", "conclusion": "ok", "steps": 1},
                      session_id="session-scheduler")
        first.approve(wid, True)
        restarted = GameWorkflowManager(state_root)
        restarted.set_execution_resolver(lambda _state: {
            "runner": lambda *_: {"status": "ok", "conclusion": "调度恢复完成", "steps": 1}})
        recovered = restarted.recover_pending()
        self.assertEqual(recovered[0]["workflow_id"], wid)
        self.assertEqual(restarted.get(wid)["status"], "completed")

    def test_concurrent_active_workflow_same_project_is_rejected(self):
        root = tempfile.mkdtemp()
        first = self.manager.start("第一个工作流", project_id="p1", project_root=root)
        self.assertEqual(first["status"], "awaiting_choice")
        # 同项目（root 或 project_id 任一匹配）的第二个启动必须被拒
        with self.assertRaises(WorkflowError):
            self.manager.start("第二个工作流", project_root=root)
        with self.assertRaises(WorkflowError):
            self.manager.start("按 project_id 也算同项目", project_id="p1")
        # 不同项目互不影响
        other = self.manager.start("另一个项目", project_root=tempfile.mkdtemp())
        self.assertEqual(other["status"], "awaiting_choice")
        # 原工作流终结后，同项目可以再次启动
        self.manager.interrupt(first["workflow_id"], "测试清理")
        restarted = self.manager.start("中断后重启", project_root=root)
        self.assertEqual(restarted["status"], "awaiting_choice")

    def test_workflow_guard_skips_callers_without_project_binding(self):
        # 两条绑定信息都空（离线/测试直调）时保持旧行为，不做全局互斥
        self.manager.start("无项目绑定 A")
        second = self.manager.start("无项目绑定 B")
        self.assertEqual(second["status"], "awaiting_choice")

    def test_output_review_detects_replacement_chars_and_failures(self):
        result = review_output({"status": "ok", "text": "坏\ufffd结果", "steps": [{"ok": False}]})
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["no_replacement_chars"])

    def test_llm_options_are_validated_and_keep_intervention_choices(self):
        state = self.manager.start(
            "做一个 2D 游戏",
            option_generator=lambda _request, _plan: (
                "```json\n{\"options\":[{\"id\":\"small\",\"title\":\"小原型\","
                "\"summary\":\"先做移动与碰撞\",\"recommended\":true}]}\n```"))
        ids = [item["id"] for item in state["options"]]
        self.assertIn("small", ids)
        self.assertIn("custom", ids)
        self.assertIn("web_research", ids)
        self.assertEqual(state["events"][-1]["option_source"], "llm")

    def test_option_and_plan_generators_execute_in_graph_nodes(self):
        state = self.manager.start(
            "做一个 2D 游戏",
            option_generator=lambda _request, _plan: {
                "options": [{"id": "small", "title": "小原型",
                             "summary": "先做移动与碰撞", "recommended": True}]},
            task_generator=lambda _request, _selected, _plan: {
                "tasks": [{"id": "impl", "role": "coder", "task": "实现原型", "depends_on": []},
                           {"id": "verify", "role": "tester", "task": "验证原型", "depends_on": ["impl"]}]})
        self.assertIn("small", [item["id"] for item in state["options"]])
        chosen = self.manager.choose(state["workflow_id"], "small")
        self.assertEqual(chosen["status"], "planned")
        self.assertEqual([task["id"] for task in chosen["tasks"]], ["impl", "verify"])
        self.assertEqual(chosen["graph_state"]["pending_interrupts"][0]["value"]["kind"],
                         "approval")

    def test_graph_provider_retries_are_checkpointed(self):
        research_calls = []
        option_calls = []

        def research(_query):
            research_calls.append(1)
            if len(research_calls) == 1:
                raise RuntimeError("temporary web failure")
            return "第二次检索成功"

        def options(_request, _plan):
            option_calls.append(1)
            if len(option_calls) == 1:
                raise RuntimeError("temporary llm failure")
            return {"options": [{"id": "retry", "title": "重试方案",
                                 "summary": "重试后得到的方案", "recommended": True}]}

        state = self.manager.start("带重试的联网方案", research_runner=research,
                                   option_generator=options,
                                   policy=WorkflowPolicy(provider_retries=2))
        self.assertEqual(len(option_calls), 2)
        researched = self.manager.choose(state["workflow_id"], "web_research")
        self.assertEqual(researched["status"], "awaiting_choice")
        self.assertEqual(len(research_calls), 2)
        self.assertEqual(len(option_calls), 3)
        self.assertEqual(len(researched["graph_state"]["research_attempts"]), 2)

    def test_research_refines_options_with_llm_and_falls_back_on_bad_output(self):
        state = self.manager.start("做一个联网资料驱动的 2D 游戏")
        wid = state["workflow_id"]
        self.manager.choose(wid, "web_research")
        refined = self.manager.apply_research(
            wid, "Godot 4 CharacterBody2D 文档摘要",
            option_generator=lambda _request, _plan: {
                "options": [{"id": "godot", "title": "Godot 原型",
                             "summary": "按官方节点模型实现移动和碰撞", "recommended": True,
                             "source": "web"}]})
        self.assertEqual(refined["events"][-1]["option_source"], "llm")
        self.assertIn("godot", [item["id"] for item in refined["options"]])

    def test_registered_research_runs_inside_graph_and_checkpoints_normalized_output(self):
        state = self.manager.start(
            "先查资料再做 2D 游戏",
            research_runner=lambda _query: "Godot 4 CharacterBody2D 官方文档摘要",
            option_generator=lambda _request, _plan: {
                "options": [{"id": "godot", "title": "Godot 方案",
                             "summary": "基于官方角色节点实现", "recommended": True,
                             "source": "web"}]})
        researched = self.manager.choose(state["workflow_id"], "web_research")
        self.assertEqual(researched["status"], "awaiting_choice")
        self.assertIn("godot", [item["id"] for item in researched["options"]])
        self.assertEqual(researched["graph_state"]["research_findings"],
                         "Godot 4 CharacterBody2D 官方文档摘要")
        self.assertEqual(researched["graph_state"]["option_source"], "llm")
        self.assertEqual(researched["graph_state"]["pending_interrupts"][0]["value"]["kind"],
                         "choice")

    def test_graph_research_failure_keeps_external_research_interrupt(self):
        def fail(_query):
            raise RuntimeError("offline")
        state = self.manager.start("联网失败后等待人工结果", research_runner=fail)
        waiting = self.manager.choose(state["workflow_id"], "web_research")
        self.assertEqual(waiting["status"], "awaiting_research")
        self.assertEqual(waiting["graph_state"]["pending_interrupts"][0]["value"]["kind"],
                         "research")

    def test_dynamic_task_plan_validates_dag_and_uses_safe_fallback(self):
        state = self.manager.start("制作带战斗验证的游戏", kind="game")
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        planned = self.manager.plan(wid, task_generator=lambda _request, _selected, _plan: {
            "tasks": [
                {"id": "combat", "role": "coder", "task": "实现战斗循环", "depends_on": []},
                {"id": "playtest", "role": "tester", "task": "验证战斗循环", "depends_on": ["combat"]},
            ]})
        self.assertEqual(planned["events"][-1]["task_source"], "llm")
        self.assertEqual([task["id"] for task in planned["tasks"]], ["combat", "playtest"])

        state2 = self.manager.start("制作带循环依赖的游戏", kind="game")
        wid2 = state2["workflow_id"]
        self.manager.choose(wid2, "recommended")
        fallback = self.manager.plan(wid2, task_generator=lambda *_args: {
            "tasks": [{"id": "a", "role": "coder", "task": "坏图", "depends_on": ["b"]},
                       {"id": "b", "role": "coder", "task": "坏图", "depends_on": ["a"]}]})
        self.assertEqual(fallback["events"][-1]["task_source"], "deterministic")
        self.assertEqual([task["id"] for task in fallback["tasks"]], ["design", "prototype", "verify"])

    def test_dynamic_subagent_policy_is_persisted_and_exposed(self):
        state = self.manager.start("制作需要联网查证的游戏原型")
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        planned = self.manager.plan(wid, [{
            "id": "research", "role": "researcher", "task": "查证引擎输入 API",
            "persona": "严谨的引擎文档考据员", "tools": ["search_code", "web_search"],
            "mcp": "deny", "reflection": True,
        }])
        task = planned["tasks"][0]
        self.assertEqual(task["persona"], "严谨的引擎文档考据员")
        self.assertEqual(task["mcp"], "deny")
        self.assertTrue(task["reflection"])
        self.assertEqual(planned["subagents"][0]["tools"], ["search_code", "web_search"])
        self.assertEqual(planned["subagents"][0]["status"], "pending")

    def test_subagent_roster_is_updated_by_live_lifecycle_events(self):
        state = self.manager.start(
            "实时显示团队执行状态",
            policy=WorkflowPolicy(approval_mode="high", max_subagents=1),
        )
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{
            "id": "verify", "role": "tester", "task": "运行最小验证",
            "persona": "谨慎的 QA", "mcp": "deny", "reflection": True,
        }])
        done = self.manager.execute(wid, lambda *_: {
            "status": "ok", "conclusion": "验证通过", "steps": 2,
            "reflection": {"ok": True},
        })
        kinds = [event["kind"] for event in done["events"]]
        self.assertIn("subagent_start", kinds)
        self.assertIn("subagent_complete", kinds)
        roster = done["subagents"][0]
        self.assertEqual(roster["status"], "ok")
        self.assertEqual(roster["steps"], 2)
        self.assertTrue(roster["reflection_result"]["ok"])

    def test_langgraph_checkpoint_is_inspectable(self):
        state = self.manager.start("做一个小游戏")
        checkpoint = self.manager.checkpoint(state["workflow_id"])
        self.assertIn(checkpoint["backend"], {"langgraph", "native"})
        if checkpoint["backend"] == "langgraph":
            self.assertEqual(checkpoint["state"]["status"], "awaiting_choice")

    def test_postgres_checkpoint_configuration_degrades_with_diagnostic(self):
        manager = GameWorkflowManager(tempfile.mkdtemp(), checkpoint_dsn="postgresql://invalid")
        try:
            self.assertTrue(manager.persistent_checkpoint)
            self.assertIn(manager.checkpoint_backend, {"langgraph-sqlite", "langgraph-postgres"})
            if manager.checkpoint_backend != "langgraph-postgres":
                self.assertTrue(manager.checkpoint_error)
        finally:
            manager.close()

    def test_checkpoint_health_reports_backend_and_strict_mode(self):
        health = self.manager.checkpoint_health(probe=False)
        self.assertIn(health["backend"], {"langgraph-sqlite", "langgraph-memory", "native"})
        self.assertFalse(health["required"])
        with patch.dict(os.environ, {"DOCMIND_CHECKPOINT_POSTGRES_REQUIRED": "1"}, clear=False), \
                patch("agent_runtime.game_workflow.PostgresSaver", None), \
                patch("agent_runtime.game_workflow.ConnectionPool", None):
            with self.assertRaises(RuntimeError):
                GameWorkflowManager(tempfile.mkdtemp(), checkpoint_dsn="postgresql://required")

    def test_workflow_hook_can_block_approval_by_policy(self):
        state = self.manager.start(
            "钩子审核", policy=WorkflowPolicy(hook_failure="block"))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "a", "task": "验证"}])
        self.manager.execute(wid, lambda *_: {"status": "ok", "conclusion": "ok", "steps": 1})
        with patch("hooks.run_workflow", return_value={
                "blocked": True, "reason": "需要人工确认", "errors": []}):
            with self.assertRaises(WorkflowError):
                self.manager.approve(wid, True)
        self.assertEqual(self.manager.get(wid)["status"], "interrupted")

    def test_langgraph_checkpoint_preserves_research_gate(self):
        state = self.manager.start("联网后再决定")
        wid = state["workflow_id"]
        self.manager.choose(wid, "web_research")
        checkpoint = self.manager.checkpoint(wid)
        if checkpoint["backend"] == "langgraph":
            self.assertEqual(checkpoint["state"]["phase"], "research")
            self.assertEqual(checkpoint["state"]["status"], "awaiting_research")
            self.assertEqual(checkpoint["state"]["event"], "research_gate")

    def test_human_gates_resume_from_sqlite_after_manager_restart(self):
        state_root = tempfile.mkdtemp()
        first_manager = GameWorkflowManager(state_root)
        started = first_manager.start("联网后再决定")
        wid = started["workflow_id"]
        self.assertEqual(started["graph_state"]["pending_interrupts"][0]["value"]["kind"], "choice")
        first_manager.choose(wid, "web_research")
        restarted = GameWorkflowManager(state_root)
        checkpoint = restarted.checkpoint(wid)
        self.assertEqual(checkpoint["state"]["pending_interrupts"][0]["value"]["kind"], "research")
        researched = restarted.apply_research(wid, "外部资料摘要")
        self.assertEqual(researched["graph_state"]["pending_interrupts"][0]["value"]["kind"], "choice")
        selected = restarted.choose(wid, "recommended")
        self.assertEqual(selected["graph_state"]["pending_interrupts"][0]["value"]["kind"], "plan")
        plan_restarted = GameWorkflowManager(state_root)
        planned = plan_restarted.plan(wid, [{"id": "write", "task": "写入原型"}])
        self.assertEqual(planned["graph_state"]["pending_interrupts"][0]["value"]["kind"], "approval")
        waiting = plan_restarted.execute(wid, lambda *_: {"status": "ok", "conclusion": "ok", "steps": 1})
        self.assertEqual(waiting["graph_state"]["pending_interrupts"][0]["value"]["kind"], "approval")
        restarted_again = GameWorkflowManager(state_root)
        approved = restarted_again.approve(wid, True)
        self.assertEqual(approved["status"], "planned")
        self.assertFalse(approved["graph_state"].get("pending_interrupts"))

    def test_mutating_tool_result_is_replayed_by_durable_idempotency_key(self):
        calls = []
        def mutating(argument):
            calls.append(argument)
            return "written"
        with tempfile.TemporaryDirectory() as storage:
            with tool_idempotency_scope("wf-1:task-1", storage):
                first = execute_tool(mutating, "file=a", lambda _: False,
                                     tool_name="create_file", side_effect=SideEffect.MUTATING)
                second = execute_tool(mutating, "file=a", lambda _: False,
                                      tool_name="create_file", side_effect=SideEffect.MUTATING)
        self.assertEqual(calls, ["file=a"])
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.idempotency_key, second.idempotency_key)

    def test_task_context_compression_preserves_bounded_evidence(self):
        one = compress_text("头部证据" + ("x" * 500) + "尾部证据", 180)
        self.assertTrue(one.truncated)
        self.assertLessEqual(one.compressed_chars, 180)
        self.assertIn("头部证据", one.text)
        self.assertIn("尾部证据", one.text)
        many, meta = compress_context({"a": "a" * 1000, "b": "b" * 1000}, 300)
        self.assertLessEqual(sum(len(value) for value in many.values()), 300)
        self.assertTrue(meta["truncated"])

    def test_context_layer_budget_and_async_compression(self):
        budgets = allocate_layer_budgets(6000)
        self.assertEqual(set(budgets), {"route", "project", "task", "subagent", "output"})
        self.assertEqual(sum(budgets.values()), 6000)
        future = compress_context_async({"dependency": "x" * 2000}, 240)
        bounded, meta = future.result(timeout=2)
        self.assertLessEqual(sum(len(value) for value in bounded.values()), 240)
        self.assertTrue(meta["truncated"])

    def test_durable_compression_queue_persists_job_and_verifies_result(self):
        with tempfile.TemporaryDirectory() as storage:
            db = os.path.join(storage, "compression.sqlite3")
            snapshots, meta = compress_layers_durable(
                {"route": "route " * 1000, "project": {"root": "D:/game"}},
                500, db_path=db, timeout=2)
            self.assertEqual(set(snapshots), {"route", "project", "task", "subagent", "output"})
            self.assertEqual(meta["result_digest"], layer_snapshot_digest(snapshots))
            self.assertIn(meta["job_state"], {"completed", "running", "queued"})
            row = persistent_compression_worker(db).get(meta["job_id"])
            self.assertIsNotNone(row)
            self.assertIn(row["state"], {"completed", "running", "queued"})
            old_worker = persistent_compression_worker(db)
            old_worker.close()
            self.assertIsNot(persistent_compression_worker(db), old_worker)
            persistent_compression_worker(db).close()

    def test_recovery_rebuilds_missing_context_layers(self):
        state = self.manager.start("恢复上下文", project_id="p1", project_root="D:/game")
        wid = state["workflow_id"]
        path = self.manager._path(wid)
        raw = __import__("json").loads(path.read_text(encoding="utf-8"))
        raw["context_layers"] = {}
        path.write_text(__import__("json").dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.manager._states.pop(wid, None)
        recovered = self.manager.get(wid)
        self.assertEqual(recovered["context_layers"]["schema"], "five-layer-v1")
        self.assertEqual(sum(recovered["context_layers"]["budgets"].values()), 6000)
        self.assertEqual(recovered["context_layers"]["project"]["project_id"], "p1")

    def test_cross_manager_recovery_rebuilds_and_verifies_snapshot_body(self):
        """模拟窗口/进程重启：五层快照正文、摘要和 digest 必须一致。"""
        state_root = tempfile.mkdtemp()
        first = GameWorkflowManager(state_root)
        started = first.start("跨窗口恢复快照", project_id="p-recover", project_root="D:/game")
        wid = started["workflow_id"]
        path = first._path(wid)
        raw = __import__("json").loads(path.read_text(encoding="utf-8"))
        # 模拟旧版本/中断写入：逻辑层还在，但快照正文被截断且元数据缺失。
        raw["context_layers"].pop("snapshots", None)
        raw["context_layers"].pop("snapshot_meta", None)
        raw["context_layers"].pop("snapshot_digest", None)
        path.write_text(__import__("json").dumps(raw, ensure_ascii=False), encoding="utf-8")
        first.close()

        second = GameWorkflowManager(state_root)
        recovered = second.get(wid)
        layers = recovered["context_layers"]
        self.assertEqual(set(layers["snapshots"]), {"route", "project", "task", "subagent", "output"})
        self.assertTrue(layers["snapshot_meta"]["valid"])
        self.assertEqual(layers["snapshot_meta"]["digest"],
                         layer_snapshot_digest(layers["snapshots"]))
        self.assertEqual(layers["snapshot_digest"], layers["snapshot_meta"]["digest"])
        self.assertEqual(layers["snapshot_meta"]["job_state"], "recovery_sync")
        second.close()

    def test_five_layer_snapshot_is_bounded_and_recovery_verified(self):
        snapshots, meta = compress_layers({
            "route": "route " * 1000,
            "project": {"root": "D:/game", "files": ["player.gd"]},
            "task": "task " * 1000,
            "subagent": "result " * 1000,
            "output": "output " * 1000,
        }, 1200)
        self.assertEqual(set(snapshots), {"route", "project", "task", "subagent", "output"})
        self.assertLessEqual(sum(len(value) for value in snapshots.values()), 1200)
        self.assertEqual(meta["digest"], layer_snapshot_digest(snapshots))
        self.assertTrue(meta["truncated"])

        state = self.manager.start("快照恢复", project_id="snapshot", project_root="D:/game")
        saved = self.manager.get(state["workflow_id"])
        layer_meta = saved["context_layers"]["snapshot_meta"]
        self.assertTrue(layer_meta["valid"])
        self.assertEqual(layer_meta["digest"],
                         layer_snapshot_digest(saved["context_layers"]["snapshots"]))

    def test_dag_revision_is_staged_until_approval(self):
        state = self.manager.start("安全修改任务", policy=WorkflowPolicy(approval_mode="high"))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "old", "role": "coder", "task": "旧任务"}])
        staged = self.manager.request_dag_revision(wid, [
            {"id": "new", "role": "tester", "task": "新任务"},
        ])
        self.assertEqual(staged["status"], "interrupted")
        self.assertEqual(staged["tasks"][0]["id"], "old")
        self.assertEqual(staged["pending_tasks"][0]["id"], "new")
        approved = self.manager.approve_dag_revision(wid, True)
        self.assertEqual(approved["status"], "planned")
        self.assertEqual(approved["tasks"][0]["id"], "new")
        self.assertFalse(approved["pending_tasks"])

    def test_langgraph_execution_loop_replans_with_bounded_counters(self):
        graph = self.manager.build_langgraph()
        if graph is None:
            self.skipTest("LangGraph 未安装")
        state = graph.invoke(
            {"workflow_id": "loop-test", "selected_option": {"id": "recommended"},
             "approved": True, "failed_tasks": ["prototype"],
             "review": {"ok": False}, "max_replans": 1, "max_steps": 3},
            config={"configurable": {"thread_id": "loop-test"}},
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["replan_count"], 1)
        self.assertEqual(state["step_count"], 2)
        self.assertEqual(state["event"], "fail")

    def test_execute_uses_langgraph_for_single_wave_replan_and_context(self):
        if self.manager.build_langgraph() is None:
            self.skipTest("LangGraph 未安装")
        state = self.manager.start(
            "验证失败后补救",
            policy=WorkflowPolicy(approval_mode="high", max_replans=1, max_steps=4),
        )
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "broken", "role": "coder", "task": "制造一个可观察失败"}])
        calls = []

        def runner(task, context):
            calls.append((task["id"], dict(context)))
            if task["id"] == "broken":
                return {"status": "failed", "conclusion": "失败诊断", "error": "可恢复错误", "steps": 1}
            return {"status": "ok", "conclusion": "补救完成", "steps": 1}

        done = self.manager.execute(
            wid, runner,
            replanner=lambda failed, results, attempt: [
                {"id": "repair", "role": "coder", "task": "修复失败任务",
                 "depends_on": ["broken"]}
            ],
        )
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["replans"], 1)
        self.assertEqual([item[0] for item in calls], ["broken", "repair"])
        self.assertNotIn("broken", calls[0][1])
        self.assertIn("失败诊断", calls[1][1]["broken"])
        self.assertIn("状态：failed", calls[1][1]["broken"])
        self.assertEqual(done["results"]["revisions"][0]["status"], "applied")
        self.assertTrue(any(event["kind"] == "graph_replan_start" for event in done["events"]))
        self.assertEqual(len(done["results"]["task_threads"]), 2)
        self.assertNotEqual(done["results"]["task_threads"][0], done["results"]["task_threads"][1])
        repaired = next(item for item in done["subagents"] if item["id"] == "repair")
        self.assertEqual(repaired["status"], "ok")
        self.assertTrue(any(item.get("id") == "repair" for item in done["tasks"]))

    def test_subagent_summary_keeps_status_and_bounds_text(self):
        summary, meta = summarize_subagent_result(
            {"status": "failed", "conclusion": "诊断" + ("x" * 2000),
             "steps": 4, "trace": {"steps": [{"ok": False}]}}, 180)
        self.assertIn("状态：failed", summary)
        self.assertLessEqual(len(summary), 180)
        self.assertEqual(meta["failed_steps"], 1)

    def test_execute_langgraph_stops_when_replan_is_invalid(self):
        if self.manager.build_langgraph() is None:
            self.skipTest("LangGraph 未安装")
        state = self.manager.start(
            "无效补救不能无限循环",
            policy=WorkflowPolicy(approval_mode="high", max_replans=3, max_steps=10),
        )
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "broken", "role": "coder", "task": "失败任务"}])
        done = self.manager.execute(
            wid, lambda _task, _context: {"status": "failed", "error": "失败", "steps": 1},
            replanner=lambda *_args: [],
        )
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["replans"], 1)
        self.assertEqual(done["graph_state"]["event"], "fail")
        # 这条断言原来写死 `empty_or_invalid_proposal`。那个标签把至少三种原因混成
        # 一句话——提案本身空 / 全被「已执行不可删改」的安全边界拒绝 / **容量用尽**。
        # 其中「容量用尽」报成「提案无效」最误导：用户会去检查提案怎么写，而真因是
        # 「没额度了」，改提示词一点用都没有。所以拆成了精确标签。
        #
        # 本测试的 replanner 返回 `[]`——提案**本来就是空的**，精确答案就是
        # `empty_proposal`。它的意图是「不能无限循环」，不是「标签必须是哪句话」。
        self.assertEqual(done["graph_state"]["replan_error"], "empty_proposal")

    def test_langgraph_send_fanout_runs_independent_tasks_in_one_wave(self):
        if self.manager.build_langgraph() is None:
            self.skipTest("LangGraph 未安装")
        calls = []
        report = self.manager._run_task_dag_langgraph(
            [
                {"id": "art", "role": "artist", "task": "准备素材"},
                {"id": "audio", "role": "audio", "task": "准备音频"},
            ],
            lambda task, _context: (calls.append(task["id"]) or {
                "status": "ok", "conclusion": task["id"], "steps": 1,
            }),
            max_steps=4,
            max_parallel=2,
        )
        self.assertEqual(report["backend"], "langgraph_send")
        self.assertEqual(len(report["waves"]), 1)
        self.assertCountEqual(calls, ["art", "audio"])
        self.assertEqual(report["n_ok"], 2)
        self.assertEqual(len(report["task_threads"]), 1)

    def test_langgraph_fanout_emits_live_subagent_lifecycle_events(self):
        if self.manager.build_langgraph() is None:
            self.skipTest("LangGraph 未安装")
        events = []
        report = self.manager._run_task_dag_langgraph(
            [{"id": "design", "role": "designer", "persona": "严谨设计师",
              "tools": ["read_file"], "mcp": "deny", "reflection": True,
              "task": "输出验收标准"}],
            lambda _task, _context: {"status": "ok", "conclusion": "标准已确认",
                                     "steps": 1, "reflection": {"ok": True}},
            max_steps=3, max_parallel=1, on_event=lambda kind, payload: events.append((kind, payload)),
        )
        kinds = [kind for kind, _payload in events]
        self.assertEqual(report["n_ok"], 1)
        self.assertIn("context_prepared", kinds)
        self.assertIn("wave_start", kinds)
        self.assertIn("subagent_start", kinds)
        self.assertIn("subagent_complete", kinds)
        self.assertIn("wave_complete", kinds)
        started = next(payload for kind, payload in events if kind == "subagent_start")
        self.assertEqual(started["task_id"], "design")
        self.assertEqual(started["mcp"], "deny")
        self.assertIn("context_chars", started)
        self.assertTrue(started["task_thread"].endswith(":design"))

    def test_dispatcher_expands_langgraph_wave_dynamically(self):
        if self.manager.build_langgraph() is None:
            self.skipTest("LangGraph 未安装")
        calls = []
        events = []

        def runner(task, context):
            calls.append((task["id"], dict(context)))
            if task["id"] == "dispatch":
                return {"status": "ok", "steps": 1, "conclusion":
                        '{"tasks":[{"id":"code","role":"coder","task":"修改文件"},'
                        '{"id":"verify","role":"tester","task":"验证文件",'
                        '"depends_on":["code"]}]}'}
            return {"status": "ok", "steps": 1, "conclusion": "完成 " + task["id"]}

        report = self.manager._run_task_dag_langgraph(
            [{"id": "dispatch", "role": "dispatcher", "task": "拆解文件并分派任务"}],
            runner, max_steps=6, max_parallel=2,
            on_event=lambda kind, payload: events.append((kind, payload)),
        )
        self.assertEqual(report["n_ok"], 3)
        self.assertEqual([item[0] for item in calls], ["dispatch", "code", "verify"])
        self.assertIn("dispatch", calls[1][1])
        self.assertEqual(report["dispatches"][0]["added"], ["code", "verify"])
        self.assertTrue(any(kind == "dispatch" for kind, _ in events))

    # ---------------- list_workflows / delete_workflow（历史弹层） ----------------
    def test_list_workflows_filters_by_project_and_exposes_summary(self):
        a = self.manager.start("项目 A 的目标", project_id="pA", project_root="D:/a")
        b = self.manager.start("项目 B 的目标", project_id="pB", project_root="D:/b")
        self.manager.choose(a["workflow_id"], "recommended")
        self.manager.plan(a["workflow_id"], [{"id": "a1", "task": "任务一"},
                                             {"id": "a2", "task": "任务二"}])
        self.manager.interrupt(a["workflow_id"], "测试中断")

        a_items = self.manager.list_workflows(project_id="pA")
        self.assertEqual([i["workflow_id"] for i in a_items], [a["workflow_id"]])
        summary = a_items[0]
        self.assertEqual(summary["request"], "项目 A 的目标")
        self.assertEqual(summary["status"], "interrupted")
        self.assertEqual(summary["interrupt_reason"], "测试中断")
        self.assertEqual(summary["task_count"], 2)

        b_items = self.manager.list_workflows(project_root="D:/b")
        self.assertEqual([i["workflow_id"] for i in b_items], [b["workflow_id"]])

        all_items = self.manager.list_workflows()
        self.assertEqual({i["workflow_id"] for i in all_items},
                         {a["workflow_id"], b["workflow_id"]})
        # updated_at 非增序（时间戳为秒级，相等时允许任意相对次序）
        stamps = [i["updated_at"] or i["created_at"] or "" for i in all_items]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_list_workflows_counts_done_tasks_from_execution_results(self):
        """历史弹层的完成数来自 results，不是 tasks[].status（后者不存在）。

        实测 wf-57af9cdb2f18：卡片 3/3，历史却写「已完成 · 0/3 个任务」。
        """
        state = self.manager.start("历史计数", project_root="D:/count",
                                   policy=WorkflowPolicy(approval_mode="high"))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "a", "task": "一"},
                                {"id": "b", "task": "二"},
                                {"id": "c", "task": "三"}])
        outcomes = {"a": "ok", "b": "ok", "c": "failed"}
        done = self.manager.execute(
            wid, lambda task, _ctx: {"status": outcomes[task["id"]],
                                     "conclusion": "x", "steps": 1})
        self.assertEqual(done["status"], "failed")

        summary = self.manager.list_workflows(project_root="D:/count")[0]
        self.assertEqual(summary["task_count"], 3)
        self.assertEqual(summary["task_done"], 2)

        # 重启后读磁盘 JSON，计数口径必须一致（不是只在内存态碰巧对）。
        base = self.manager.state_root.parent.parent
        fresh = GameWorkflowManager(str(base))
        reloaded = fresh.list_workflows(project_root="D:/count")[0]
        self.assertEqual(reloaded["task_done"], 2)

    def test_list_workflows_is_readonly_and_does_not_arm_mutex(self):
        # 重启后扫描磁盘：list 绝不能把门控态注册进 _states（否则会武装项目互斥）
        state_root = tempfile.mkdtemp()
        first = GameWorkflowManager(state_root)
        gate = first.start("停在方案门", project_id="pG", project_root="D:/g")
        fresh = GameWorkflowManager(state_root)
        items = fresh.list_workflows(project_id="pG")
        self.assertEqual([i["workflow_id"] for i in items], [gate["workflow_id"]])
        self.assertNotIn(gate["workflow_id"], fresh._states)

    def test_delete_workflow_rejects_non_terminal_then_removes_file(self):
        active = self.manager.start("还在等待选择", project_root="D:/p")
        with self.assertRaises(WorkflowError):
            self.manager.delete_workflow(active["workflow_id"])

        self.manager.interrupt(active["workflow_id"], "先中断")
        result = self.manager.delete_workflow(active["workflow_id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["workflow_id"], active["workflow_id"])
        # 磁盘文件与列表都清空；再次删除报「不存在」
        self.assertEqual(self.manager.list_workflows(), [])
        with self.assertRaises(WorkflowError):
            self.manager.delete_workflow(active["workflow_id"])

    def test_delete_completed_workflow(self):
        state = self.manager.start("完整跑完的工作流", policy=WorkflowPolicy(approval_mode="high"))
        wid = state["workflow_id"]
        self.manager.choose(wid, "recommended")
        self.manager.plan(wid, [{"id": "a", "task": "验证"}])
        done = self.manager.execute(wid, lambda *_: {
            "status": "ok", "conclusion": "通过", "steps": 1})
        self.assertEqual(done["status"], "completed")
        self.manager.delete_workflow(wid)
        self.assertEqual(self.manager.list_workflows(), [])


if __name__ == "__main__":
    unittest.main()
