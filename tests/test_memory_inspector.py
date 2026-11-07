"""记忆检查器：只把真实提示词内容标成已注入，合规证据不足时不假通过。"""
import unittest
from unittest.mock import patch

import agent
import agent_memory as memory
from agent_runtime.memory import inspector
from tests.test_agent_trace import _FakeLLM, _IsoBase


class MemoryInspectorTests(_IsoBase):
    def test_injected_ids_match_prompt_and_sessions_do_not_share_snapshot(self):
        scope = memory.scope_key("p")
        mid = memory.remember(scope, "fact", "中文守则", "回答时使用中文，保持事实可核验",
                              source="user_explicit", importance=0.9)
        first = agent.Agent(llm=_FakeLLM(["Final Answer: 这是中文回答。"]),
                            session_id="inspect-a", project_id="p")
        second = agent.Agent(llm=_FakeLLM([]), session_id="inspect-b", project_id="p")
        list(first.run("请用中文回答"))

        view = inspector.view(first, project="p", session="inspect-a")
        injected = next(row for row in view["injected"] if row["id"] == mid)
        legacy = next(row for row in view["prompt_blocks"] if row["layer_key"] == "legacy")
        self.assertIn(mid, legacy["item_ids"])
        self.assertIn(mid, legacy["text"])
        self.assertIn("回答时使用中文", injected["injected_content"])
        self.assertTrue(injected["used"])
        self.assertEqual(injected["importance"], 0.9)
        self.assertGreaterEqual(injected["use_count"], 1)
        initial_check = next(row for row in view["compliance"] if row["id"].startswith("session-initial"))
        self.assertEqual(initial_check["status"], "pass")
        self.assertIn("最终回答含中文", initial_check["evidence"])

        other = inspector.view(second, project="p", session="inspect-b")
        self.assertFalse(other["has_snapshot"])
        self.assertEqual(other["injected"], [])
        self.assertTrue(any(row["id"] == mid and not row["used"] for row in other["memories"]))

    def test_read_only_compliance_uses_real_file_changes(self):
        rules = [{"id": "ro", "source": "user", "text": "只读分析，不修改文件"}]
        passed = inspector._compliance(rules, {"final_text": "done", "file_changes": []})
        failed = inspector._compliance(rules, {
            "final_text": "done", "file_changes": [{"path": "agent.py"}],
        })
        unknown = inspector._compliance(rules, None)
        self.assertEqual(passed[0]["status"], "pass")
        self.assertEqual(failed[0]["status"], "fail")
        self.assertIn("agent.py", failed[0]["evidence"])
        self.assertEqual(unknown[0]["status"], "unknown")

    def test_arbitrary_requirement_is_not_faked_as_pass(self):
        rows = inspector._compliance(
            [{"id": "quality", "source": "project", "text": "架构要保持优雅"}],
            {"final_text": "完成了", "file_changes": []})
        self.assertEqual(rows[0]["status"], "unknown")

    def test_context_renderer_is_byte_identical(self):
        scope = memory.scope_key("p")
        memory.remember(scope, "fact", "中文守则", "回答时使用中文")
        legacy = memory.context(scope, "中文")
        rendered, rows = memory.context_with_rows(scope, "中文")
        self.assertEqual(rendered, legacy)
        self.assertEqual(len(rows), 1)
        self.assertEqual(memory.audit(scope)[0]["id"], rows[0]["id"])

    def test_read_only_api_returns_live_snapshot(self):
        import api
        from starlette.testclient import TestClient

        scope = memory.scope_key("p")
        mid = memory.remember(scope, "fact", "中文守则", "回答时使用中文")
        selected = agent.Agent(llm=_FakeLLM(["Final Answer: 中文。"]),
                               session_id="inspect-api", project_id="p")
        list(selected.run("请用中文回答"))
        key = api._agent_key("inspect-api", "p")
        api._SESSION_AGENTS[key] = selected
        try:
            with patch.object(api, "_request_project_id", return_value="p"), TestClient(api.app) as client:
                response = client.get("/api/agent/memory/inspect?session_id=inspect-api")
                wrong_method = client.post("/api/agent/memory/inspect?session_id=inspect-api")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(wrong_method.status_code, 405)
            self.assertEqual(wrong_method.headers.get("allow"), "GET")
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["session_id"], "inspect-api")
            self.assertIn(mid, [row["id"] for row in payload["injected"]])
        finally:
            api._SESSION_AGENTS.pop(key, None)


if __name__ == "__main__":
    unittest.main()
