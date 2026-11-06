"""交互式子代理派发：直接执行登记、跨层/扩权/超限审批与代理树。"""
import os
import time
import unittest
from unittest.mock import patch

import agent
from agent_runtime import delegation
from agent_runtime.memory import inspector
from tests.test_agent_trace import _IsoBase


class _LLM:
    provider = "mock"
    model = "delegation-test"
    prompt_budget = 8000

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0
        self.last_usage = None

    def clone(self):
        return self

    def count_tokens(self, _text):
        return 0

    def chat(self, _messages=None, stream=False, **_kwargs):
        self.calls += 1
        if not self.outputs:
            return "Final Answer: 完成"
        return self.outputs.pop(0)


class DelegationTests(_IsoBase):
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

    def test_nested_dispatch_requires_approval_beyond_direct_child(self):
        llm = _LLM([
            "Action: delegate\nAction Input: role: researcher\ntask: 查下一层",
            "Final Answer: 下一层正在等待审批",
        ])
        parent = agent.Agent(llm=llm, session_id="delegation-nested", project_id="p")
        first = parent._run_child("researcher", "先查父任务", reflect=False)
        self.assertEqual(first["status"], "ok")
        self.assertNotIn("delegation", first, "安全的直接子代理不产生待审批 id")
        self.assertEqual(llm.calls, 2, "直接子代理已执行，嵌套子代理尚未批准")

        tree = delegation.tree(parent.session_id)
        first_node = tree["root"]["children"][0]
        first_id = first_node["id"]
        self.assertEqual(first_node["status"], "waiting_child")
        self.assertFalse(first_node["approval_required"])
        self.assertEqual(len(tree["pending"]), 1)
        nested = tree["pending"][0]
        self.assertEqual(nested["parent_id"], first_id)
        self.assertEqual(nested["depth"], 2)
        self.assertTrue(nested["approval_required"])
        self.assertNotEqual(nested["id"], first_id)

        child_agent = delegation.agent_for(parent.session_id, first_id)
        self.assertEqual(parent.history, [])
        self.assertEqual(len(child_agent.history), 1, "子代理只应拥有自己的本轮历史")
        self.assertIsNot(child_agent.history, parent.history, "子代理上下文必须独立")
        view = inspector.view(child_agent, project="p", session=parent.session_id,
                              agent_id=first_id)
        self.assertEqual(view["agent_id"], first_id)
        self.assertTrue(view["has_snapshot"])

        rejected = delegation.decide(parent.session_id, nested["id"], False)
        self.assertEqual(rejected["item"]["status"], "rejected")
        self.assertIn("未执行", rejected["item"]["error"])
        self.assertIsNone(delegation.agent_for(parent.session_id, nested["id"]))

    def test_approval_runs_the_registered_branch_once(self):
        llm = _LLM(["Final Answer: 独立结论"])
        # depth=1 → 2 是跨层递归，即使任务只读也必须先审批。
        parent = agent.Agent(llm=llm, session_id="delegation-approve", project_id="p", depth=1)
        pending = parent._run_child("researcher", "只读分析", reflect=False)
        node_id = pending["delegation"]["id"]
        result = delegation.decide(parent.session_id, node_id, True)
        self.assertEqual(result["item"]["status"], "completed", result)
        self.assertEqual(result["item"]["result"]["conclusion"], "独立结论")
        self.assertEqual(llm.calls, 1)
        again = delegation.decide(parent.session_id, node_id, True)
        self.assertTrue(again["ok"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(llm.calls, 1, "重复审批不能重复执行")

    def test_direct_child_is_registered_counted_and_has_no_pending_id(self):
        llm = _LLM(["Final Answer: 独立结论"])
        parent = agent.Agent(llm=llm, session_id="delegation-direct", project_id="p")
        with patch.dict(os.environ, {"DOCMIND_DELEGATION_MAX_CHILDREN": "1"}):
            result = parent._run_child("researcher", "只读分析", reflect=False)
            self.assertEqual(result["status"], "ok")
            self.assertNotIn("delegation", result)
            self.assertFalse(result["needs_confirmation"], "免批分支没有实际确认请求或待审批 id")
            tree = delegation.tree(parent.session_id)
            self.assertEqual(tree["pending"], [])
            self.assertEqual(len(tree["nodes"]), 2)
            node = tree["root"]["children"][0]
            self.assertEqual(node["status"], "completed")
            self.assertFalse(node["approval_required"])
            self.assertEqual(node["result"]["conclusion"], "独立结论")
            self.assertNotEqual(node["id"], "main")
            self.assertEqual(delegation.agent_for(parent.session_id, node["id"]).agent_id, node["id"])

            # 已完成的直接节点不再占额度：第二次直接派发正常执行。
            second = parent._run_child("researcher", "第二个直接子任务", reflect=False)
            self.assertEqual(second["status"], "ok")
            self.assertEqual(llm.calls, 2)
            # 真正并发的节点（pending/running）仍受单父分支上限约束。
            blocker = agent.Agent(llm=_LLM([]), session_id="delegation-direct",
                                  project_id="p", depth=1)
            held = blocker._run_child("researcher", "跨层任务先挂起", reflect=False)
            self.assertEqual(held["status"], "pending")
            blocked = parent._run_child("researcher", "超额直接子任务", reflect=False)
            self.assertEqual(blocked["status"], "pending")
            self.assertTrue(blocked["delegation"]["requires_limit_confirmation"])
            self.assertEqual(llm.calls, 2)

    def test_direct_child_with_insufficient_lease_requires_approval(self):
        llm = _LLM(["Final Answer: 不应提前执行"])
        parent = agent.Agent(llm=llm, session_id="delegation-lease", project_id="p")
        parent.capability_lease = {
            "status": "active", "expires_at_epoch": time.time() + 60,
            "capabilities": ["read_local"],
        }
        pending = parent._run_child("researcher", "需要联网检索", reflect=False)
        self.assertEqual(pending["status"], "pending")
        self.assertIn("network", pending["delegation"]["approval_reasons"][-1])
        self.assertEqual(llm.calls, 0)

    def test_soft_limit_is_explicit_in_approval_not_silent_recursion(self):
        parent = agent.Agent(llm=_LLM([]), session_id="delegation-limit", depth=4)
        with patch.dict(os.environ, {"DOCMIND_DELEGATION_MAX_DEPTH": "4"}):
            pending = parent._run_child("researcher", "继续拆小", reflect=False)
            self.assertTrue(pending["delegation"]["requires_limit_confirmation"])
            self.assertIn("超过默认上限", pending["delegation"]["limit_reasons"][0])
            result = delegation.decide(parent.session_id, pending["delegation"]["id"], False)
            self.assertEqual(result["item"]["status"], "rejected")

    def test_api_lists_and_rejects_only_within_session(self):
        import api
        from starlette.testclient import TestClient

        # 跨层夹具：确保列表里一定有 pending 节点，而非依赖直接子代理审批。
        parent = agent.Agent(llm=_LLM([]), session_id="delegation-api", depth=1)
        pending = parent._run_child("researcher", "等待审批", reflect=False)
        node_id = pending["delegation"]["id"]
        with TestClient(api.app) as client:
            listed = client.get("/api/agent/delegations?session_id=delegation-api").json()
            self.assertEqual(listed["pending"][0]["id"], node_id)
            rejected = client.post(f"/api/agent/delegations/{node_id}/decision", json={
                "session_id": "delegation-api", "approved": False,
            }).json()
            self.assertEqual(rejected["item"]["status"], "rejected")
            wrong = client.post(f"/api/agent/delegations/{node_id}/decision", json={
                "session_id": "other", "approved": True,
            }).json()
            self.assertFalse(wrong["ok"])
            client.delete("/api/sessions/delegation-api")
            self.assertEqual(delegation.tree("delegation-api")["nodes"], [
                {"id": "main", "session_id": "delegation-api", "parent_id": "", "role": "main",
                 "task": "主 Agent", "depth": 0, "status": "active", "children": []},
            ])


if __name__ == "__main__":
    unittest.main()
