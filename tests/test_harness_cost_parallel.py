# -*- coding: utf-8 -*-
"""harness 成本/并行增强（离线）：

1. 并行批次内去重 + 依赖重路由（orchestrator.dedup_tasks / run_plan）
2. 成本/显存感知调度（run_plan 预算熔断停波 + replanner ctx 透传）
3. pricing 每分钟限流 + 跨进程强一致
"""
import os
import tempfile
import threading
import unittest

import orchestrator as orch
import pricing


class _BudgetEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_harness_")
        self._ob = pricing.BUDGET_FILE
        self._op = pricing.PRICING_FILE
        pricing.BUDGET_FILE = os.path.join(self.tmp, "budget.json")
        pricing.PRICING_FILE = os.path.join(self.tmp, "pricing.json")
        if os.path.isfile(pricing.BUDGET_FILE):
            os.remove(pricing.BUDGET_FILE)

    def tearDown(self):
        pricing.BUDGET_FILE = self._ob
        pricing.PRICING_FILE = self._op


class DedupTests(unittest.TestCase):
    def test_dedup_keeps_first_and_remaps_deps(self):
        tasks = orch.parse_plan([
            {"id": "a", "role": "coder", "task": "实现 登录 接口"},
            {"id": "b", "role": "coder", "task": "实现  登录   接口"},   # 同 role+归一化文本 → 重复
            {"id": "c", "role": "coder", "task": "写测试", "depends_on": ["b"]},
        ])
        kept, dropped, remap = orch.dedup_tasks(tasks)
        self.assertEqual(dropped, ["b"])
        self.assertEqual(remap["b"], "a")
        self.assertEqual([t["id"] for t in kept], ["a", "c"])
        # 依赖重路由到保留副本
        self.assertEqual(kept[1]["depends_on"], ["a"])

    def test_dedup_whitespace_and_case_insensitive(self):
        tasks = orch.parse_plan([
            {"id": "a", "task": "Read  the  file"},
            {"id": "b", "task": "read the file"},
        ])
        kept, dropped, _ = orch.dedup_tasks(tasks)
        self.assertEqual(dropped, ["b"])
        self.assertEqual([t["id"] for t in kept], ["a"])

    def test_dedup_keeps_distinct_role(self):
        tasks = orch.parse_plan([
            {"id": "a", "role": "coder", "task": "X"},
            {"id": "b", "role": "reviewer", "task": "X"},   # 不同 role → 不重复
        ])
        kept, dropped, _ = orch.dedup_tasks(tasks)
        self.assertEqual(dropped, [])
        self.assertEqual([t["id"] for t in kept], ["a", "b"])

    def test_dedup_no_op_when_unique(self):
        tasks = orch.parse_plan([{"id": "a", "task": "X"}, {"id": "b", "task": "Y"}])
        kept, dropped, remap = orch.dedup_tasks(tasks)
        self.assertEqual(dropped, [])
        self.assertEqual(remap, {})
        self.assertEqual([t["id"] for t in kept], ["a", "b"])

    def test_run_plan_dedup_marks_and_reroutes(self):
        events = []
        seen = {}

        def runner(task, ctx):
            seen[task["id"]] = ctx
            return {"status": "ok", "conclusion": f"done-{task['id']}"}

        rep = orch.run_plan(orch.parse_plan([
            {"id": "a", "task": "甲"},
            {"id": "b", "task": "甲"},                       # 重复 → 并入 a
            {"id": "c", "task": "乙", "depends_on": ["b"]},  # 依赖 b → 重路由到 a
        ]), runner, on_event=lambda k, p: events.append((k, p)))
        self.assertEqual(rep["deduped"]["dropped"], ["b"])
        self.assertEqual(rep["deduped"]["remap"]["b"], "a")
        self.assertTrue(rep["ok"])
        self.assertIn("dedup", [e[0] for e in events])
        self.assertEqual(rep["results"]["b"]["status"], "deduped")
        # c 实际执行（依赖已重路由到 a）
        self.assertEqual(rep["results"]["c"]["status"], "ok")
        # c 收到的 ctx 来自 a（而非被删的 b）
        self.assertIn("done-a", seen["c"].values())


class CostAwareTests(_BudgetEnv):
    def _runner(self):
        def runner(task, ctx):
            return {"status": "ok", "conclusion": "ok"}
        return runner

    def test_budget_exhausted_stops_wave(self):
        pricing.set_limit(0.0001)          # 全局上限极低
        pricing.charge(1.0)                # 已超额 → check 返回 ok=False
        events = []
        rep = orch.run_plan(orch.parse_plan([
            {"id": "a", "task": "甲"},
            {"id": "b", "task": "乙", "depends_on": ["a"]},
        ]), self._runner(), on_event=lambda k, p: events.append((k, p)),
            budget_session="", cost_aware=True)
        kinds = [e[0] for e in events]
        self.assertIn("budget", kinds)
        self.assertEqual(rep["results"]["a"]["status"], "blocked")
        self.assertFalse(rep["ok"])
        pricing.reset()

    def test_cost_aware_disabled_runs_normally(self):
        pricing.set_limit(0.0001)
        pricing.charge(1.0)
        rep = orch.run_plan(orch.parse_plan([{"id": "a", "task": "甲"}]),
                            self._runner(), budget_session="", cost_aware=False)
        self.assertEqual(rep["results"]["a"]["status"], "ok")
        pricing.reset()

    def test_replanner_receives_ctx(self):
        captured = {}

        def replanner(failed, results, attempt, ctx=None):
            captured["ctx"] = ctx
            captured["attempt"] = attempt
            return None
        orch.run_plan(orch.parse_plan([
            {"id": "a", "task": "甲"},
        ]), self._runner(), replanner=replanner, max_replans=1,
            budget_session="s", vram_provider=lambda: 123.4)
        # a 成功，不触发 replanner；用一个会失败的任务触发
        captured.clear()

        def runner_fail(task, ctx):
            return {"status": "failed", "error": "boom"}

        orch.run_plan(orch.parse_plan([{"id": "a", "task": "甲"}]),
                            runner_fail, replanner=replanner, max_replans=1,
                            budget_session="s", vram_provider=lambda: 123.4)
        self.assertIn("ctx", captured)
        self.assertEqual(captured["ctx"]["vram_free"], 123.4)
        self.assertIn("budget", captured["ctx"])
        self.assertEqual(captured["ctx"]["attempt"], 1)
        pricing.reset()


class PricingRateLimitTests(_BudgetEnv):
    def test_per_minute_calls_limit(self):
        pricing.set_rate_limit(calls=3, cost=0.0)
        for _ in range(3):
            self.assertTrue(pricing.rate_check("")["ok"])
        self.assertFalse(pricing.rate_check("")["ok"])   # 第 4 次被拒
        self.assertEqual(pricing.rate_check("")["reason"], "每分钟调用次数已达上限（3/3）")

    def test_per_minute_cost_limit_on_charge(self):
        pricing.set_rate_limit(calls=0.0, cost=1.0)
        r1 = pricing.charge(0.6, "s")
        self.assertTrue(r1.get("ok", True))
        r2 = pricing.charge(0.6, "s")   # 累计 1.2 > 1.0 → 拒绝计费
        self.assertFalse(r2["ok"])
        self.assertTrue(r2.get("rate_limited"))
        # 被拒的计费不落地：global 仅 0.6
        self.assertAlmostEqual(pricing.status()["global_spent"], 0.6, places=4)

    def test_rate_limit_cross_minute_reset(self):
        pricing.set_rate_limit(calls=1, cost=0.0)
        self.assertTrue(pricing.rate_check("")["ok"])
        self.assertFalse(pricing.rate_check("")["ok"])
        # 伪造分钟键切换 → 重置
        st = pricing._read()
        st["minute"]["key"] = "2000-01-01T00:00"
        pricing._write(st)
        self.assertTrue(pricing.rate_check("")["ok"])

    def test_strong_consistency_concurrent_charges(self):
        """多线程并发 charge 不丢更新（跨进程文件锁保证 RMW 原子）。"""
        pricing.reset()
        n = 16
        per = 0.01

        def worker():
            for _ in range(4):
                pricing.charge(per, "s")

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertAlmostEqual(pricing.status()["global_spent"], n * 4 * per, places=3)


if __name__ == "__main__":
    unittest.main()
