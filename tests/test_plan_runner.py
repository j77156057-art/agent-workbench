# -*- coding: utf-8 -*-
"""Plan 长跑：任务书门、用户停止、复盘状态、决策账本。"""
import os
import tempfile
import time
import unittest

from agent_runtime import plan_log, plan_runner
from agent_runtime.memory import taskbook
import tools


class _PlanLLM:
    provider = "stub"
    model = "stub"

    def __init__(self):
        self.messages = []

    def count_tokens(self, text):
        return max(1, len(str(text or "")) // 4)

    def chat(self, messages, stream=True, **kw):
        self.messages.append([dict(m) for m in messages])
        return iter(["Final Answer: 本轮继续执行，暂无新任务。"])


class PlanRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_plan_")
        self._old = os.environ.get("DOCMIND_MEMORY_DIR")
        os.environ["DOCMIND_MEMORY_DIR"] = self.tmp
        self.project = "prj-plan"
        self.session = "s-plan"

    def tearDown(self):
        plan_runner.stop(self.project, self.session, by="test")
        # runner 的每一轮都会提交异步固化任务；不排空会漂到下一个用例的存储里。
        from agent_runtime.memory import consolidate
        consolidate.drain()
        if self._old is None:
            os.environ.pop("DOCMIND_MEMORY_DIR", None)
        else:
            os.environ["DOCMIND_MEMORY_DIR"] = self._old

    def test_taskbook_gate_accepts_only_closed_items(self):
        taskbook.open_book(self.project, "长跑目标", ["第一步", "第二步"])
        self.assertFalse(plan_runner._closed(self.project))
        for item in taskbook.items(self.project):
            taskbook.complete(self.project, item["key"], user_override=True)
        self.assertTrue(plan_runner._closed(self.project))

    def test_start_status_and_user_stop(self):
        started = plan_runner.start(
            self.project, self.session, "把项目里的两个测试修好",
            llm=_PlanLLM(), review_interval_minutes=30, max_iterations=10)
        self.assertTrue(started.get("ok"))
        self.assertTrue(plan_runner.is_active(self.project, self.session))
        status = plan_runner.status(self.project, self.session)
        self.assertTrue(status["active"])
        self.assertEqual(status["state"]["goal"], "把项目里的两个测试修好")
        stopped = plan_runner.stop(self.project, self.session)
        self.assertTrue(stopped.get("ok"))
        self.assertTrue(stopped["state"]["user_stop"])
        self.assertFalse(plan_runner.is_active(self.project, self.session))
        rows = plan_log.rows(self.project, self.session, limit=20)
        self.assertIn("start", {row.get("kind") for row in rows})
        self.assertIn("stop", {row.get("kind") for row in rows})

    def test_plan_decision_tool_writes_log(self):
        tokens = plan_log.bind(self.project, self.session)
        try:
            result = tools.plan_decision(
                "decision: 先修空输入\ntask: 空输入\nrationale: 测试最先失败")
        finally:
            plan_log.reset(tokens)
        self.assertIn('"ok": true', result.lower())
        rows = plan_log.rows(self.project, self.session, limit=10)
        self.assertTrue(any(row.get("kind") == "decision" for row in rows))

    def test_stop_without_record_is_safe(self):
        result = plan_runner.stop("prj-missing", "s-missing")
        self.assertFalse(result.get("ok"))

    def test_drive_loop_runs_a_real_turn_and_books_it(self):
        """长跑不是「起了个线程」就算数：必须真的驱动一轮、把每轮不变量和
        「先建任务书」的要求送到模型面前，并把它记进决策账本。"""
        llm = _PlanLLM()
        started = plan_runner.start(self.project, self.session, "把两个测试修好",
                                    llm=llm, review_interval_minutes=30,
                                    max_iterations=50)
        self.assertTrue(started.get("ok"))
        turn = None
        deadline = time.time() + 120
        while turn is None and time.time() < deadline:
            turn = next((row for row in plan_log.rows(self.project, self.session, limit=50)
                         if row.get("kind") == "turn"), None)
            if turn is None:
                time.sleep(0.5)
        self.assertIsNotNone(turn, "runner 没有真正驱动一轮并记入账本")
        self.assertIn("本轮继续执行", turn["text"])
        self.assertEqual(turn["phase"], "execute")

        sent = "\n".join(str(m.get("content") or "") for m in llm.messages[0])
        self.assertIn("无人值守执行器", sent, "每轮的 system 不变量丢了")
        self.assertIn("dev_plan(action: open", sent,
                      "空任务书时首轮必须先建计划，否则门永远闭着")
        self.assertIn("只能由用户", sent, "要把「模型不能自关」讲明白")
        self.assertNotIn("先用 `Plan:` 开头输出", sent,
                         "不该复用 Agent.plan_mode：那是「先输出编号计划」，与长跑无关")

        status = plan_runner.status(self.project, self.session)
        self.assertGreaterEqual(status["state"]["iteration"], 1)


if __name__ == "__main__":
    unittest.main()
