import unittest

from agent_runtime.run_budget import RunBudget


class RunBudgetTests(unittest.TestCase):
    def test_provider_usage_is_accumulated(self):
        budget = RunBudget(1000)
        self.assertTrue(budget.preflight(400, 100))
        budget.record({"prompt_tokens": 120, "completion_tokens": 30})
        budget.record({"input_tokens": 80, "output_tokens": 20})
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["total_tokens"], 250)
        self.assertEqual(snapshot["calls"], 2)
        self.assertEqual(snapshot["remaining"], 750)

    def test_fallback_estimate_and_hard_stop(self):
        budget = RunBudget(100)
        budget.record(None, prompt_chars=160, output_chars=40)
        # 字符→token 除数 1.9（tokenizer 实测，见 run_budget.CHARS_PER_TOKEN）：
        # 160/1.9 = 84，40/1.9 = 21 -> 105。旧口径 /4 会得到 50。
        self.assertEqual(budget.total_tokens, 105)
        self.assertFalse(budget.preflight(100, 60))
        self.assertTrue(budget.snapshot()["exhausted"])

    def test_zero_means_unlimited(self):
        budget = RunBudget(0)
        self.assertTrue(budget.preflight(10_000_000, 10_000_000))


if __name__ == "__main__":
    unittest.main()
