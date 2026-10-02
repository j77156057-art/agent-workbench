import os
import unittest
from unittest.mock import patch

from agent_runtime.local_runtime import (effective_parallelism,
                                          effective_subagent_limit,
                                          resource_profile)


class LocalRuntimeTests(unittest.TestCase):
    def test_profiles_allow_user_agent_capacity_to_override_static_tiers(self):
        with patch("agent_runtime.local_runtime._local_endpoint", return_value=True), \
                patch("agent_runtime.local_runtime._detected_parallel_capacity", return_value=None):
            tiny = resource_profile("ollama", "qwen3:4b")
            medium = resource_profile("ollama", "qwen3:14b")
            large = resource_profile("ollama", "qwen3.6:35b-a3b")
        self.assertEqual(tiny.tier, "tiny")
        # 本地档位保留按体量推出的子代理默认值（tiny=3），**不**跟随"默认不限"：
        # 本地模型共用一个推理服务（max_parallel 已被钳到 1），不限子代理只会让任务
        # 无上限地排进同一个队列——显存不炸，但用户看到的是"卡住"。
        # 这条断言以前写的是 8，那是 capacity 的旧默认值恰好盖住了 tier 默认值 3，
        # 属于"蒙对"而不是在测本地档位的行为。
        self.assertEqual(tiny.max_subagents, 3)
        self.assertEqual((medium.tier, medium.max_parallel), ("medium", 1))
        self.assertEqual((large.tier, large.max_parallel), ("large", 1))

    def test_an_explicit_subagent_setting_still_overrides_the_tier(self):
        """用户在设置里填了正数就听用户的——本地档位只是"填 0 时"的兜底。"""
        with patch("agent_runtime.local_runtime._local_endpoint", return_value=True), \
                patch("agent_runtime.local_runtime._detected_parallel_capacity", return_value=None), \
                patch("agent_runtime.capacity.get_settings",
                      return_value={"max_agents": 6, "child_max_steps": 0,
                                    "local_auto_limit": True}):
            profile = resource_profile("ollama", "qwen3:4b")
        self.assertEqual(profile.max_subagents, 6)

    def test_requested_parallelism_is_clamped_for_local_only(self):
        with patch("agent_runtime.local_runtime._local_endpoint", return_value=True), \
                patch("agent_runtime.local_runtime._detected_parallel_capacity", return_value=None):
            self.assertEqual(effective_parallelism("ollama", "qwen3:8b", 4), 1)
            self.assertEqual(effective_subagent_limit("ollama", "qwen3:8b", 4), 4)
            # 当前没检测到额外显存余量时仍保守地串行生成；任务数由用户设置决定。
            self.assertEqual(effective_parallelism("ollama", "qwen3:4b", 4), 1)
            # 4b 落在 tiny 档，档位默认子代理数是 3，所以请求 4 会被夹到 3。
            # （以前断言 4 能过，是因为 capacity 的旧默认 max_agents=8 先一步
            #   把 tier 默认盖住了——那是蒙对，不是被测行为。）
            self.assertEqual(effective_subagent_limit("ollama", "qwen3:4b", 4), 3)
        self.assertEqual(effective_parallelism("deepseek", "deepseek-chat", 4), 4)

    def test_operator_can_raise_local_inference_slots(self):
        with patch.dict(os.environ, {"DOCMIND_LOCAL_LLM_MAX_CONCURRENCY": "2",
                                     "DOCMIND_LOCAL_SUBAGENT_MAX": "3"}, clear=False):
            profile = resource_profile("ollama", "qwen3:8b")
            self.assertEqual(profile.max_parallel, 2)
            self.assertEqual(profile.max_subagents, 3)

    def test_local_parallelism_uses_detected_free_vram(self):
        with patch.dict(os.environ, {"DOCMIND_LOCAL_LLM_MAX_CONCURRENCY": ""}, clear=False):
            os.environ.pop("DOCMIND_LOCAL_LLM_MAX_CONCURRENCY", None)
            with patch("agent_runtime.local_runtime._local_endpoint", return_value=True), \
                    patch("agent_runtime.local_runtime._detected_parallel_capacity", return_value=5):
                profile = resource_profile("ollama", "qwen3:8b")
        self.assertEqual(profile.max_parallel, 5)

    def test_unlimited_requested_local_agent_count_has_no_model_tier_clamp(self):
        with patch("agent_runtime.capacity.get_settings", return_value={
                "max_agents": 12, "child_max_steps": 24, "local_auto_limit": True}), \
                patch("agent_runtime.local_runtime._local_endpoint", return_value=True), \
                patch("agent_runtime.local_runtime._detected_parallel_capacity", return_value=None):
            self.assertEqual(effective_subagent_limit("ollama", "qwen3:4b", 12), 12)


if __name__ == "__main__":
    unittest.main()
