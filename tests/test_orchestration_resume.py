"""DAG 审批后恢复：持久任务图、拒绝传播、重启恢复与执行幂等。"""
from pathlib import Path
import tempfile
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


class OrchestrationResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root_patch = mock.patch.object(orchestration, "_ROOT", Path(self.tmp.name) / "plans")
        self.root_patch.start()
        self.session = "resume-contract"

    def tearDown(self):
        delegation.clear(self.session)
        self.root_patch.stop()
        self.tmp.cleanup()

    def parent(self, llm):
        # depth=1 → 2 让每个业务节点都先审批，便于确定地验证恢复状态机。
        return agent.Agent(llm=llm, session_id=self.session, project_id="p", depth=1)

    def plan(self):
        return [{"id": "t1", "role": "researcher", "task": "第一步", "reflection": False},
                {"id": "t2", "role": "researcher", "task": "第二步", "depends_on": ["t1"],
                 "reflection": False}]

    def pending_node(self, report, task_id):
        return report["results"][task_id]["needs_confirmation"]["id"]

    def test_approval_resumes_downstream_synthesizes_and_never_reruns(self):
        llm = _LLM(["A1", "A2", "B1", "B2", "最终合成"])
        parent = self.parent(llm)
        initial = parent.orchestrate({"tasks": self.plan()}, synth=True)
        self.assertFalse(initial["final"])
        self.assertEqual(initial["results"]["t1"]["status"], "pending")
        self.assertNotIn("t2", initial["results"])
        plan_id = initial["plan_id"]
        self.assertTrue((Path(self.tmp.name) / "plans" / (plan_id + ".json")).is_file())

        first = delegation.decide(self.session, self.pending_node(initial, "t1"), True)
        self.assertEqual(first["report"]["results"]["t1"]["status"], "ok")
        self.assertEqual(first["report"]["results"]["t2"]["status"], "pending")
        self.assertFalse(first["report"]["final"])

        second = delegation.decide(self.session, self.pending_node(first["report"], "t2"), True)
        self.assertTrue(second["final"])
        self.assertTrue(second["report"]["final"])
        self.assertEqual(second["report"]["merged"], "最终合成")
        self.assertEqual([second["report"]["results"][tid]["status"] for tid in ("t1", "t2")], ["ok", "ok"])
        self.assertEqual(llm.calls, 5, "t1/t2 各两轮模型调用、最后一次合成；t1 不得重跑")

        again = delegation.decide(self.session, second["item"]["id"], True)
        self.assertTrue(again["idempotent"])
        self.assertEqual(llm.calls, 5, "重复审批不得再次执行或合成")

    def test_rejection_marks_downstream_skipped(self):
        llm = _LLM([])
        parent = self.parent(llm)
        initial = parent.orchestrate({"tasks": self.plan()}, synth=True)
        rejected = delegation.decide(self.session, self.pending_node(initial, "t1"), False)
        report = rejected["report"]
        self.assertEqual(report["results"]["t1"]["status"], "rejected")
        self.assertEqual(report["results"]["t2"]["status"], "skipped")
        self.assertNotIn("t2", report["blocked"])
        self.assertIn("t2", report["skipped"])
        self.assertIn("用户拒绝", report["results"]["t1"]["error"])
        self.assertEqual(llm.calls, 0)

    def test_disk_plan_resumes_with_new_agent_after_restart(self):
        parent = self.parent(_LLM([]))
        initial = parent.orchestrate({"tasks": self.plan()}, synth=True)
        plan_id = initial["plan_id"]
        persisted = orchestration.get(plan_id)
        self.assertEqual([row["id"] for row in persisted["tasks"]], ["t1", "t2"])
        self.assertEqual(persisted["results"]["t1"]["status"], "pending")

        # 模拟进程重启：丢掉内存 Agent 注册，只保留 JSON 图，并注入新 Agent。
        new_llm = _LLM(["重启后合成"])
        new_agent = self.parent(new_llm)
        with mock.patch.dict(orchestration._AGENTS, {}, clear=True):
            node = delegation.get(self.session, self.pending_node(initial, "t1"))
            node["result"] = {"status": "ok", "conclusion": "A", "steps": 1, "error": ""}
            resumed = orchestration.resume(plan_id, node, agent=new_agent)
            self.assertEqual(resumed["report"]["results"]["t2"]["status"], "pending")
            second = delegation.get(self.session, self.pending_node(resumed["report"], "t2"))
            second["result"] = {"status": "ok", "conclusion": "B", "steps": 1, "error": ""}
            final = orchestration.resume(plan_id, second, agent=new_agent)
        self.assertTrue(final["report"]["final"])
        self.assertEqual(final["report"]["merged"], "重启后合成")
        self.assertEqual(new_llm.calls, 1, "已完成的 t1 没有重跑，仅执行最终合成")

    def test_interrupted_running_becomes_unknown_not_a_retry(self):
        parent = self.parent(_LLM([]))
        initial = parent.orchestrate({"tasks": self.plan()}, synth=True)
        plan_id = initial["plan_id"]

        def inject_crash(record):
            record["results"]["t1"] = {"status": "running", "conclusion": "", "steps": 0}
        orchestration._update(plan_id, inject_crash)
        node = delegation.get(self.session, self.pending_node(initial, "t1"))
        node["result"] = {"status": "ok", "conclusion": "外部已核对完成", "steps": 1}
        # 这里 t1 被人工核对为 ok；另置一个 running 结果模拟崩溃，验证不会自动重试。
        orchestration._update(plan_id, lambda record: record["results"].__setitem__(
            "t2", {"status": "running", "conclusion": "", "steps": 0}))
        resumed = orchestration.resume(plan_id, node, agent=parent)
        self.assertEqual(resumed["report"]["results"]["t2"]["status"], "unknown")
        self.assertFalse(resumed["report"]["final"])


if __name__ == "__main__":
    unittest.main()
