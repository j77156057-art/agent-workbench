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


    def test_agent_memory_api_scopes_lists_but_keeps_shared_rows(self):
        import api
        from starlette.testclient import TestClient

        scope = memory.scope_key("p")
        # agent_id="" 模拟引入 agent 维度前的历史行：下一次 _connect() 会把它回填成
        # main + agent_legacy=1，即「共享/项目级」池，所有 Agent 都应看得到。
        shared = memory.remember(scope, "fact", "共享历史", "项目级共享知识", agent_id="")
        main_private = memory.remember(scope, "fact", "main 私有", "只有 main 看得到")
        a_private = memory.remember(scope, "fact", "ag-a 私有", "只有 ag-a 看得到", agent_id="ag-a")
        b_private = memory.remember(scope, "fact", "ag-b 私有", "只有 ag-b 看得到", agent_id="ag-b")

        with patch.object(api, "_request_project_id", return_value="p"), TestClient(api.app) as client:
            def list_payload(agent_id, level="agent"):
                resp = client.get(f"/api/agent/memory?agent_id={agent_id}&level={level}")
                self.assertEqual(resp.status_code, 200)
                return resp.json()

            a = list_payload("ag-a")
            b = list_payload("ag-b")
            project = list_payload("ag-a", "project")

        a_ids = {row["id"] for row in a["items"]}
        b_ids = {row["id"] for row in b["items"]}
        project_ids = {row["id"] for row in project["items"]}
        self.assertEqual(a["level"], "agent")
        self.assertEqual(a["agent_id"], "ag-a")
        self.assertNotEqual(a_ids, b_ids)
        # 两个 Agent 都能看到共享/项目级行，且只多出自己名下的私有行。
        for ids in (a_ids, b_ids):
            self.assertIn(shared, ids)
            self.assertNotIn(main_private, ids)
        self.assertIn(a_private, a_ids)
        self.assertNotIn(a_private, b_ids)
        self.assertIn(b_private, b_ids)
        self.assertNotIn(b_private, a_ids)
        # 项目级浏览是另一种口径：返回该项目下所有 Agent 的 active 记忆。
        self.assertEqual(project["level"], "project")
        self.assertIsNone(project["agent_id"])
        self.assertTrue({shared, main_private, a_private, b_private} <= project_ids)

    def test_memory_delete_api_enforces_agent_ownership(self):
        """DELETE 跨 agent 必须返回错误码（403），不是 200，也不能静默。"""
        import api
        from starlette.testclient import TestClient

        scope = memory.scope_key("p")
        main_mid = memory.remember(scope, "fact", "main 私有", "只有 main 能删")
        sub_mid = memory.remember(scope, "fact", "ag-a 私有", "只有 ag-a 能删", agent_id="ag-a")

        with patch.object(api, "_request_project_id", return_value="p"), TestClient(api.app) as client:
            project = client.get("/api/agent/memory?level=project").json()
            cross = client.delete(f"/api/agent/memory/{main_mid}?agent_id=ag-a")
            missing = client.delete("/api/agent/memory/not-a-real-id?agent_id=main")
            own = client.delete(f"/api/agent/memory/{main_mid}?agent_id=main")
            sub_own = client.delete(f"/api/agent/memory/{sub_mid}?agent_id=ag-a")

        # 项目级列表回传归属，删除时 UI 才能显式声明删谁的记忆。
        owners = {row["id"]: row["agent_id"] for row in project["items"]}
        self.assertEqual(owners[main_mid], "main")
        self.assertEqual(owners[sub_mid], "ag-a")
        self.assertEqual(cross.status_code, 403)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(own.status_code, 200)
        self.assertTrue(own.json()["ok"])
        self.assertEqual(sub_own.status_code, 200)
        self.assertIsNone(memory.owner(scope, main_mid))


if __name__ == "__main__":
    unittest.main()
