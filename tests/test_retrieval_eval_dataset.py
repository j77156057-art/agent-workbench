import json
import unittest
from pathlib import Path

from agent_runtime.retrieval_eval import (
    evaluate_bm25_corpus, load_retrieval_dataset, retrieval_regression_gate,
)


class RetrievalDatasetTests(unittest.TestCase):
    def test_checked_in_game_dataset_has_recall_gate(self):
        path = Path(__file__).parents[1] / ".github" / "retrieval-eval.json"
        dataset = load_retrieval_dataset(path)
        report = evaluate_bm25_corpus(dataset["documents"], dataset["cases"], dataset["ks"])
        self.assertEqual(report["metrics"]["queries"], len(dataset["cases"]))
        self.assertEqual(report["metrics"]["errors"], 0)
        self.assertGreaterEqual(report["metrics"]["mrr"], 0.75)

        baseline_path = path.with_name("retrieval-eval-baseline.json")
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        gate = retrieval_regression_gate(report, baseline, metric="mrr", minimum=0.75)
        self.assertFalse(gate["regressed"])

    def test_dataset_rejects_unlabelled_case(self):
        with self.assertRaises(ValueError):
            from agent_runtime.retrieval_eval import load_retrieval_dataset
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False) as handle:
                json.dump({"documents": [{"id": "a", "content": "x"}],
                           "cases": [{"query": "x"}]}, handle)
                temporary = handle.name
            try:
                load_retrieval_dataset(temporary)
            finally:
                import os
                os.unlink(temporary)

    def test_live_case_schema_and_per_mode_gate(self):
        from agent_runtime.retrieval_eval import load_retrieval_cases, live_regression_gate
        path = Path(__file__).parents[1] / ".github" / "retrieval-eval-live.json"
        data = load_retrieval_cases(path)
        # The live gate is backed by the real Godot sample index and must keep
        # enough coverage to catch regressions beyond the original four cases.
        self.assertGreaterEqual(len(data["cases"]), 8)
        report = {"dense": {"metrics": {"mrr": 0.8}},
                  "hybrid": {"metrics": {"mrr": 0.9}}}
        gate = live_regression_gate(report, {"modes": {
            "dense": {"metrics": {"mrr": 0.8}},
            "hybrid": {"metrics": {"mrr": 0.9}},
        }}, metric="mrr", minimum=0.75)
        self.assertFalse(gate["regressed"])


if __name__ == "__main__":
    unittest.main()
