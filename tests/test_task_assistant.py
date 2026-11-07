"""任务助手只编排：标准规格、风险分档审批、失败恢复与自然语言回报。"""
import json
import unittest
from unittest.mock import patch

import agent
import orchestrator
from agent_runtime import delegation, task_assistant
from tests.test_agent_trace import _IsoBase
from tests.test_delegation import _LLM


def _spec(task_id="t1", depends=None):
    return {
        "id": task_id, "role": "researcher", "goal": "核对现有实现",
        "input": ["用户目标"], "outputs": ["结论与证据"],
        "boundaries": ["只读"], "acceptance": ["结论附来源"],
        "depends_on": depends or [],
    }


class TaskAssistantTests(_IsoBase):
    def setUp(self):
        super().setUp()
        delegation.clear()
        self._audit_patch = patch("agent._audit_live_answer", return_value={
            "required": False, "checks": {}, "gate_checks": {}, "references": [],
        })
        self._audit_patch.start()

    def tearDown(self):
        self._audit_patch.stop()
        delegation.clear()
        super().tearDown()

    def test_standard_spec_survives_dag_parser(self):
        tasks = task_assistant.parse_tasks(json.dumps({"tasks": [_spec()]}), "总目标")
        normalized = orchestrator.parse_plan(tasks)[0]
        for key in ("goal", "input", "outputs", "boundaries", "acceptance"):
            self.assertIn(key, normalized)
        rendered = task_assistant.render_spec(normalized)
        self.assertIn("产出物", rendered)
        self.assertIn("验收条件", rendered)

    def test_pending_dispatch_has_not_run_business_agent(self):
        llm = _LLM([json.dumps({"tasks": [_spec()]}, ensure_ascii=False)])
        # 跨层任务仍先审批；同级、同项目且未扩权的直接任务由其它用例覆盖立即执行。
        parent = agent.Agent(llm=llm, session_id="assistant-pending", project_id="p", depth=1)
        report = parent.task_assist("请拆解并核对实现")
        self.assertEqual(llm.calls, 1, "待审批时只能有规划调用，不能偷偷执行业务")
        self.assertTrue(report["assistant"]["orchestrator_only"])
        result = report["results"]["t1"]
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["spec"]["outputs"], ["结论与证据"])
        self.assertIn("等待风险审批", report["merged"])
        node_id = delegation.tree(parent.session_id)["pending"][0]["id"]
        llm.outputs.append("Final Answer: 已核对，证据充分")
        approved = delegation.decide(parent.session_id, node_id, True)
        self.assertEqual(approved["item"]["status"], "completed")
        self.assertEqual(approved["item"]["result"]["conclusion"], "已核对，证据充分")

    def test_crash_is_reported_and_recovery_is_dispatched(self):
        planner = json.dumps({"tasks": [_spec("t1")]}, ensure_ascii=False)
        recovery = json.dumps([_spec("t2", ["t1"])], ensure_ascii=False)
        llm = _LLM([planner, recovery, "补救任务已完成；原失败原因和续跑点已保留。"])
        parent = agent.Agent(llm=llm)
        calls = []

        def run_child(role, task, context=None, **kwargs):
            calls.append({"role": role, "task": task, "context": context})
            if len(calls) == 1:
                raise RuntimeError("agent process crashed")
            return {"status": "ok", "conclusion": "补救结论", "steps": 1, "error": ""}

        parent._run_child = run_child
        report = parent.task_assist("核对并修复", max_replans=1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(report["replans"], 1)
        self.assertEqual(report["results"]["t2"]["status"], "ok")
        self.assertIn("agent process crashed", report["results"]["t1"]["failure_reason"])
        self.assertIn("重派", report["results"]["t1"]["resume_point"])
        self.assertEqual(report["results"]["t2"]["spec"]["outputs"], ["结论与证据"])
        self.assertEqual(report["merged"], "补救任务已完成；原失败原因和续跑点已保留。")
        self.assertNotIn('"tasks"', report["merged"])

    def test_api_exposes_natural_language_report(self):
        import api
        from starlette.testclient import TestClient

        parent = agent.Agent(llm=_LLM([json.dumps({"tasks": [_spec()]}, ensure_ascii=False)]),
                             session_id="assistant-api", project_id="p")
        with patch.object(api, "_agent_for", return_value=parent), \
                patch.object(api, "_request_project_id", return_value="p"), TestClient(api.app) as client:
            response = client.post("/api/agent/task-assistant", json={
                "session_id": "assistant-api", "goal": "拆解任务", "max_replans": 1,
            })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["assistant"]["orchestrator_only"])
        self.assertIsInstance(payload["merged"], str)
        self.assertNotIn('"results"', payload["merged"])


if __name__ == "__main__":
    unittest.main()
