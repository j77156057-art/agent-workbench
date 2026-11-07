"""审批 ticket 持久化：重启可查询/批准、TTL 过期拒绝与 strict 一次性消费。"""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import agent
from agent_runtime import delegation, orchestration


class _LLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0
        self.provider = "fake"
        self.model = "fake"
        self.base_url = ""
        self.last_usage = None

    def chat(self, messages, **kwargs):
        self.calls += 1
        return self.outputs.pop(0) if self.outputs else "Final Answer: 完成"

    def count_tokens(self, text):
        return max(1, len(str(text)) // 4)


class DelegationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = "approval-restart"
        self.dir_patch = mock.patch.object(delegation, "_DIR", Path(self.tmp.name) / "delegations")
        self.plan_patch = mock.patch.object(orchestration, "_ROOT", Path(self.tmp.name) / "plans")
        self.dir_patch.start()
        self.plan_patch.start()

    def tearDown(self):
        delegation.clear(self.session)
        self.plan_patch.stop()
        self.dir_patch.stop()
        self.tmp.cleanup()

    def parent(self, llm):
        return agent.Agent(llm=llm, session_id=self.session, project_id="p", depth=1)

    def test_pending_ticket_survives_restart_and_strict_ticket_is_consumed(self):
        original = self.parent(_LLM([]))
        pending = original._run_child("researcher", "重启前待审批", reflect=False)
        node_id = pending["delegation"]["id"]
        path = delegation._session_path(self.session)
        payload = json.loads(path.read_text(encoding="utf-8"))
        row = payload["nodes"][0]
        self.assertEqual(row["ticket_id"], node_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["ttl_seconds"], 0)
        self.assertTrue(row["strict_one_shot"])

        # 模拟服务重启：只保留磁盘，所有内存回调和 Agent 实例都丢弃。
        delegation._ROOTS.clear()
        loaded = delegation.tree(self.session)
        self.assertEqual(len(loaded["pending"]), 1)
        new_llm = _LLM(["恢复后结论"])
        new_agent = self.parent(new_llm)
        approved = delegation.decide(self.session, node_id, True,
                                     resume_agent=new_agent, actor="reviewer-a")
        self.assertEqual(approved["item"]["status"], "completed", approved)
        self.assertEqual(approved["item"]["approved_by"], "reviewer-a")
        self.assertEqual(approved["item"]["decision"], "approved")
        self.assertTrue(approved["item"]["decided_at"])
        self.assertGreater(new_llm.calls, 0)
        executed_calls = new_llm.calls

        again = delegation.decide(self.session, node_id, True,
                                  resume_agent=new_agent, actor="reviewer-a")
        self.assertTrue(again["idempotent"])
        self.assertEqual(new_llm.calls, executed_calls, "strict ticket 已消费，不得再次执行")

    def test_expired_ticket_is_rejected_without_executing(self):
        parent = self.parent(_LLM([]))
        pending = parent._run_child("researcher", "会过期的审批", reflect=False)
        node_id = pending["delegation"]["id"]
        root = delegation._root(self.session)
        root["nodes"][node_id]["ttl_seconds"] = 1
        root["nodes"][node_id]["strict_one_shot"] = False
        root["nodes"][node_id]["expires_at_epoch"] = time.time() - 1
        delegation._persist(self.session)

        delegation._ROOTS.clear()
        llm = _LLM(["不应执行"])
        expired = delegation.decide(self.session, node_id, True,
                                    resume_agent=self.parent(llm), actor="reviewer-b")
        self.assertFalse(expired["ok"])
        self.assertTrue(expired["expired"])
        self.assertEqual(expired["item"]["status"], "expired")
        self.assertIn("已过期", expired["error"])
        self.assertEqual(llm.calls, 0)

    def test_restart_pending_dag_rebuilds_callback_and_finishes(self):
        parent = self.parent(_LLM([]))
        initial = parent.orchestrate({"tasks": [
            {"id": "t1", "role": "researcher", "task": "A", "reflection": False},
            {"id": "t2", "role": "researcher", "task": "B", "depends_on": ["t1"],
             "reflection": False}]}, synth=True)
        plan_id = initial["plan_id"]
        self.assertTrue((Path(self.tmp.name) / "plans" / (plan_id + ".json")).is_file())

        delegation._ROOTS.clear()
        new_llm = _LLM(["A1", "A2", "B1", "B2", "重启合成"])
        new_agent = self.parent(new_llm)
        with mock.patch.dict(orchestration._AGENTS, {}, clear=True):
            pending = delegation.tree(self.session)["pending"]
            self.assertEqual(len(pending), 1)
            first = delegation.decide(self.session, pending[0]["id"], True,
                                      resume_agent=new_agent, actor="restarter")
            self.assertEqual(first["report"]["results"]["t2"]["status"], "pending")
            second = delegation.decide(
                self.session, first["report"]["results"]["t2"]["needs_confirmation"]["id"], True,
                resume_agent=new_agent, actor="restarter")
        self.assertTrue(second["report"]["final"])
        self.assertEqual(second["report"]["merged"], "重启合成")
        self.assertEqual(new_llm.calls, 5)


if __name__ == "__main__":
    unittest.main()
