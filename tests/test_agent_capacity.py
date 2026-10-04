import unittest
import io
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from agent_runtime.capacity import (gpu_snapshot, local_model_memory_snapshot,
                                    normalize_settings, remaining_agent_capacity)
from agent_runtime.game_workflow import GameWorkflowManager, WorkflowPolicy
from orchestrator import parse_plan


class AgentCapacityTests(unittest.TestCase):
    def test_defaults_and_unlimited_values_are_normalized(self):
        """默认四项**全是 0（不限）**——2026-10-02 起。

        这条断言以前写的是 8/8/24/50000。它是"默认值"的唯一凭据：一旦默认值改了而
        这里没同步，测试会红；反过来若有人只改了这里，行为与断言就脱钩了。
        """
        self.assertEqual(normalize_settings(None), {
            "max_agents": 0, "main_max_steps": 0, "child_max_steps": 0,
            "max_nudges": 2, "max_final_continuations": 2,
            "task_token_budget": 0, "local_auto_limit": True,
        })
        # 只给一部分时，未给的那几项沿用默认（也必须是 0，而不是旧默认）。
        self.assertEqual(normalize_settings({
            "max_agents": 4, "local_auto_limit": False,
        }), {"max_agents": 4, "main_max_steps": 0, "child_max_steps": 0,
              "max_nudges": 2, "max_final_continuations": 2,
              "task_token_budget": 0, "local_auto_limit": False})
        # 显式 0 必须原样保留（这是"填 0 = 不限"能成立的前提）。
        self.assertEqual(normalize_settings({
            "max_agents": 0, "child_max_steps": 0,
        })["child_max_steps"], 0)

    def test_invalid_limits_fall_back_to_safe_defaults(self):
        self.assertEqual(normalize_settings({
            "max_agents": 65, "child_max_steps": -1, "local_auto_limit": "false",
        }), {"max_agents": 0, "main_max_steps": 0, "child_max_steps": 0,
              "max_nudges": 2, "max_final_continuations": 2,
              "task_token_budget": 0, "local_auto_limit": True})

    def test_gpu_snapshot_reports_per_device_and_process_occupancy(self):
        memory = {
            "used_mb": 12000, "total_mb": 24000, "free_mb": 12000,
            "gpus": [{"index": 0, "name": "GPU A", "used_mb": 4000,
                      "total_mb": 8000, "utilization": 30},
                     {"index": 1, "name": "GPU B", "used_mb": 8000,
                      "total_mb": 16000, "utilization": 60}],
        }
        processes = {"available": True, "compute_apps": [
            {"pid": 42, "process_name": "ollama", "used_mb": 7000},
        ]}
        gpu = SimpleNamespace(memory_info=lambda: memory,
                              process_status=lambda: processes)
        with patch.dict(sys.modules, {"gpu_coordinator": gpu}):
            result = gpu_snapshot()
        self.assertEqual(result["free_mb"], 12000)
        self.assertEqual([gpu["free_mb"] for gpu in result["gpus"]], [4000, 8000])
        self.assertEqual(result["compute_apps"][0]["used_mb"], 7000)

    def test_ollama_snapshot_reads_loaded_model_vram(self):
        payload = b'{"models":[{"name":"local-model","size":900,"size_vram":700}]}'
        with patch("agent_runtime.capacity.urllib.request.urlopen",
                   return_value=io.BytesIO(payload)) as open_url:
            result = local_model_memory_snapshot("ollama", "http://127.0.0.1:11434/v1")
        self.assertTrue(result["available"])
        self.assertEqual(result["models"][0]["name"], "local-model")
        self.assertEqual(result["models"][0]["size_vram_bytes"], 700)
        self.assertTrue(open_url.call_args.args[0].endswith("/api/ps"))

    def test_unlimited_policy_removes_default_orchestrator_task_ceiling(self):
        tasks = [{"id": "t%d" % i, "role": "tester", "task": "check %d" % i}
                 for i in range(20)]
        self.assertEqual(len(parse_plan(tasks, max_tasks=0)), 20)

    def test_remaining_capacity_distinguishes_exhausted_and_unlimited(self):
        self.assertEqual(remaining_agent_capacity(2, 2), 0)
        self.assertEqual(remaining_agent_capacity(2, 1), 1)
        self.assertIsNone(remaining_agent_capacity(0, 200))

    def test_unlimited_workflow_and_raised_child_steps_are_accepted(self):
        self.assertEqual(WorkflowPolicy(max_subagents=0).normalized().max_subagents, 0)
        tasks = GameWorkflowManager._parse_generated_tasks([
            {"id": "a", "role": "coder", "task": "long", "max_steps": 120},
            {"id": "b", "role": "tester", "task": "verify"},
        ], max_tasks=0)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["max_steps"], 120)

    def test_generic_orchestrator_preserves_child_step_requests_above_12(self):
        tasks = parse_plan([{"id": "a", "role": "coder", "task": "long",
                             "max_steps": 120}])
        self.assertEqual(tasks[0]["max_steps"], 120)

    def test_workflow_plan_surfaces_configured_child_step_limit(self):
        # `close()` 必须在 `with` **内部**调，不能靠 addCleanup/tearDown：
        # `GameWorkflowManager.__init__` 默认开一个 sqlite 落地点
        # （DOCMIND_LANGGRAPH_SQLITE_CHECKPOINT 默认 1），而 Windows 不允许删除
        # 仍被打开的文件。`TemporaryDirectory.__exit__` 是在测试体里跑的，
        # 排在 addCleanup 之前——于是清理阶段炸：
        #   PermissionError: [WinError 32] ... langgraph_checkpoints.sqlite
        # 失败点在 teardown 而非断言，很容易被误当成「环境问题」。
        # 同一仓里 test_project_stage.py 等 10 处用的就是这个 try/finally 形状。
        with tempfile.TemporaryDirectory() as root:
            manager = GameWorkflowManager(root)
            try:
                state = manager.start("capacity test", policy=WorkflowPolicy(
                    max_subagents=2, child_max_steps=42))
                workflow_id = state["workflow_id"]
                manager.choose(workflow_id, "recommended")
                planned = manager.plan(workflow_id, [
                    {"id": "a", "role": "tester", "task": "check", "max_steps": 120},
                ])
                self.assertEqual(planned["policy"]["child_max_steps"], 42)
                self.assertEqual(planned["subagents"][0]["max_steps"], 42)
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
