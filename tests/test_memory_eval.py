"""记忆专用评测的回归锁：基准本身必须能跑通且达到最低线。"""
import unittest

from agent_runtime.memory import eval as memory_eval


class MemoryEvalTests(unittest.TestCase):
    def test_keyword_baseline_passes(self):
        result = memory_eval.run(semantic=False)
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(result["retrieval"]["recall_at_3"], 0.75)
        self.assertFalse(result["semantic_effective"], "关闭语义时不应写/用向量")
        self.assertTrue(result["checks"]["receipt_gate"])
        self.assertTrue(result["checks"]["adopt_promotes"])
        self.assertTrue(result["checks"]["dedupe_merge"])
        self.assertTrue(result["checks"]["scope_isolation"])
        self.assertTrue(result["checks"]["star_freeze_blocks"])


if __name__ == "__main__":
    unittest.main()
