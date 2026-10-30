# -*- coding: utf-8 -*-
"""并行工具批次与子代理并行编排（离线）。

覆盖：
  ① 只读工具批次真并发（墙钟时间 ≈ 单条耗时，线程 id 不同、结果保序）；
  ② 批内含写/副作用工具时自动退回顺序执行；
  ③ 单条异常不拖垮整批；
  ④ 子代理各自持独立 LLMClient（并发不互相覆盖 last_usage）；
  ⑤ 一轮多 delegate 并行扇出并汇总。
"""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import agent as agent_mod
import agent_trace
import sessions
from llm import LLMClient
from tools import TOOLS


class _CloningLLM:
    """支持 clone() 的假 LLM：父实例按脚本回放，clone 出来的子实例固定一句 Final Answer。"""
    provider = "deepseek"
    model = "cloning"

    def __init__(self, parent_steps=None, child_reply="Final Answer: 子代理结论 OK"):
        self.parent_steps = parent_steps or []
        self.child_reply = child_reply
        self.i = 0
        self.last_usage = {}
        self.last_tool_calls = []

    def clone(self):
        return _CloningLLM([("text", self.child_reply)], self.child_reply)

    def chat(self, messages, stream=True, **kw):
        spec = self.parent_steps[min(self.i, len(self.parent_steps) - 1)] if self.parent_steps \
            else ("text", self.child_reply)
        self.i += 1
        self.last_usage = {"prompt_tokens": 4, "completion_tokens": 2}
        self.last_tool_calls = []
        content = ""
        if spec[0] == "tools":
            self.last_tool_calls = [
                {"id": f"c{i}", "name": n, "arguments": json.dumps(a)}
                for i, (n, a) in enumerate(spec[1])
            ]
        else:
            content = spec[1]
        if stream:
            def g():
                for ch in content:
                    yield ch
            return g()
        return content

    def count_tokens(self, text):
        return max(1, len(text) // 3)


class _Iso(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_par_")
        self._t = agent_trace.TRACE_FILE
        self._s = sessions.SESSIONS_DIR
        agent_trace.TRACE_FILE = os.path.join(self.tmp, "t.jsonl")
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "s")

    def tearDown(self):
        agent_trace.TRACE_FILE = self._t
        sessions.SESSIONS_DIR = self._s


class ParallelBatchTests(_Iso):
    def setUp(self):
        super().setUp()
        self.slept = []          # (name, thread_id)
        self.lock = threading.Lock()

        def _slow(arg):
            with self.lock:
                self.slept.append((arg, threading.get_ident()))
            time.sleep(0.25)
            return f"slow:{arg}"

        TOOLS["_slow_probe"] = {"description": "test probe", "func": _slow}

    def tearDown(self):
        TOOLS.pop("_slow_probe", None)
        super().tearDown()

    def test_parallel_safe_predicate(self):
        a = agent_mod.Agent(llm=_CloningLLM())
        self.assertTrue(a._parallel_safe([("search_code", "x"), ("read_file", "y")]))
        self.assertTrue(a._parallel_safe([("delegate", "role: researcher")]))
        self.assertFalse(a._parallel_safe([("apply_edit", "x")]))
        self.assertFalse(a._parallel_safe([("search_code", "x"), ("run_command", "y")]))
        self.assertFalse(a._parallel_safe([]))

    def test_batch_runs_concurrently_and_keeps_order(self):
        a = agent_mod.Agent(llm=_CloningLLM())
        batch = [("_slow_probe", "a"), ("_slow_probe", "b"), ("_slow_probe", "c")]
        t0 = time.monotonic()
        results = a._run_batch(batch, None)
        elapsed = time.monotonic() - t0
        self.assertEqual([r[0] for r in results], ["_slow_probe"] * 3)
        self.assertEqual([r[1] for r in results], ["a", "b", "c"])   # 保序
        self.assertTrue(all(r[3] for r in results))
        self.assertLess(elapsed, 0.6, f"未并发：{elapsed:.2f}s（串行应 ~0.75s）")
        self.assertEqual(len({tid for _n, tid in self.slept}), 3, "应跑在 3 个不同线程")

    def test_batch_survives_single_failure(self):
        def _boom(arg):
            raise RuntimeError("kaboom")

        TOOLS["_boom_probe"] = {"description": "x", "func": _boom}
        try:
            a = agent_mod.Agent(llm=_CloningLLM())
            results = a._run_batch([("_slow_probe", "ok"), ("_boom_probe", "x")], None)
            self.assertIn("slow:ok", results[0][2])
            self.assertIn("并行执行失败", results[1][2])
            self.assertFalse(results[1][3])
            self.assertEqual(results[1][5], "exception",
                             "并行失败必须带 error_kind 供熔断分类")
        finally:
            TOOLS.pop("_boom_probe", None)

    def test_parallel_network_timeout_hook_can_block_follow_up(self):
        def timeout_probe(_arg):
            raise TimeoutError("upstream timed out")

        events = []

        def observe(kind, payload):
            events.append((kind, dict(payload)))
            return {"blocked": kind == "network_timeout",
                    "reason": "超时后需要审核", "errors": []}

        registry = {"web_timeout_probe": {
            "description": "test network probe", "func": timeout_probe,
        }}
        a = agent_mod.Agent(llm=_CloningLLM(), tool_registry=registry)
        with patch.object(agent_mod._hooks, "run_workflow", side_effect=observe):
            results = a._run_batch([("web_timeout_probe", "ping")], None)
        self.assertFalse(results[0][3])
        self.assertIn("超时后需要审核", results[0][2])
        self.assertIn("network_timeout", [kind for kind, _payload in events])
        after = [payload for kind, payload in events if kind == "after_tool"]
        self.assertEqual(after[0]["error_kind"], "timeout")

    def test_batch_failure_classification_counts_and_nudges(self):
        a = agent_mod.Agent(llm=_CloningLLM())

        class _Turn:
            failure_count = 0
            max_tool_streak = 0
            max_consec_failures = 0
            last_failure = ""

        turn = _Turn()
        permanent, streak, web = {}, {}, {}
        total, forced, nudge = a._record_batch_failures(
            [("read_file", "x", "bad payload", False, None, "invalid_result"),
             ("web_search", "q", "timeout", False, None, "timeout")],
            turn, streak, 0, web, permanent)
        self.assertEqual(permanent.get("read_file"), 1)
        self.assertEqual(streak["read_file"], 1)
        self.assertEqual(streak["web_search"], 1)
        self.assertEqual(total, 1, "permanent 不消耗失败预算，timeout 计 1")
        self.assertEqual(turn.failure_count, 2)
        self.assertIn("invalid_result", nudge)
        self.assertIn("可重试", nudge)
        total, forced, nudge = a._record_batch_failures(
            [("read_file", "x", "bad payload", False, None, "invalid_result")],
            turn, streak, total, web, permanent)
        self.assertEqual(permanent["read_file"], 2)
        self.assertIn("禁止", nudge)
        streak["read_file"] = agent_mod._TOOL_FAIL_LIMIT - 1
        total, forced, nudge = a._record_batch_failures(
            [("read_file", "x", "bad payload", False, None, "invalid_result")],
            turn, streak, total, web, permanent)
        self.assertTrue(forced, "连续失败到上限必须触发证据兜底")
        self.assertIn("收尾", nudge)

    def test_native_batch_failure_produces_nudge(self):
        def _boom(_arg):
            raise RuntimeError("kaboom")

        TOOLS["_batch_boom"] = {"description": "x", "func": _boom}
        try:
            llm = _CloningLLM([
                ("tools", [("_slow_probe", {"input": "a"}),
                           ("_batch_boom", {"input": "x"})]),
                ("text", "Final Answer: 收尾"),
            ])
            a = agent_mod.Agent(llm=llm, tool_mode="native")
            events = list(a.run("并行失败测试", stream=True))
            reflections = [e.get("text", "") for e in events
                           if e.get("type") == "reflection"]
            self.assertTrue(any("并行调用中有工具失败" in text for text in reflections),
                            reflections)
        finally:
            TOOLS.pop("_batch_boom", None)

    def test_native_multi_readonly_calls_use_batch(self):
        llm = _CloningLLM([
            ("tools", [("search_code", {"input": "query: alpha"}),
                       ("search_code", {"input": "query: beta"})]),
            ("text", "Final Answer: 都查完了"),
        ])
        a = agent_mod.Agent(llm=llm, tool_mode="native")
        events = list(a.run("查两下", stream=True))
        actions = [e for e in events if e["type"] == "action"]
        self.assertEqual(len(actions), 2)
        self.assertEqual(agent_trace.recent(1)[0]["n_steps"], 2)

    def test_batch_with_side_effect_tool_falls_back_to_sequential(self):
        # python_exec 在 _NO_PARALLEL_TOOLS 里：整批退回顺序，但仍两条都执行
        llm = _CloningLLM([
            ("tools", [("search_code", {"input": "query: a"}),
                       ("python_exec", {"input": "print(1)"})]),
            ("text", "Final Answer: 好了"),
        ])
        a = agent_mod.Agent(llm=llm, tool_mode="native")
        events = list(a.run("查并算", stream=True))
        actions = [e["text"] for e in events if e["type"] == "action"]
        self.assertEqual(len(actions), 2, actions)
        self.assertTrue(actions[0].startswith("search_code"))
        self.assertTrue(actions[1].startswith("python_exec"))


class SubagentParallelTests(_Iso):
    def test_clone_gives_independent_client(self):
        c = LLMClient(provider="mock")
        c2 = c.clone()
        self.assertIsNot(c2, c)
        self.assertEqual(c2.provider, "mock")

    def test_child_llm_is_independent_when_clone_available(self):
        llm = _CloningLLM()
        a = agent_mod.Agent(llm=llm)
        self.assertIsNot(a._child_llm(), llm)

    def test_child_llm_falls_back_for_plain_double(self):
        class _Plain:
            provider = "x"
            model = "y"
            last_usage = {}
            last_tool_calls = []

            def chat(self, messages, stream=True, **kw):
                return "Final Answer: ok"

            def count_tokens(self, t):
                return 1

        p = _Plain()
        a = agent_mod.Agent(llm=p)
        self.assertIs(a._child_llm(), p)

    def test_parallel_delegate_fanout(self):
        llm = _CloningLLM([
            ("tools", [("delegate", {"input": "role: researcher\ntask: 查 A"}),
                       ("delegate", {"input": "role: reviewer\ntask: 评审 B"})]),
            ("text", "Final Answer: 两个子代理都完成了"),
        ])
        a = agent_mod.Agent(llm=llm, tool_mode="native")
        events = list(a.run("并行调研", stream=True))
        actions = [e["text"] for e in events if e["type"] == "action"]
        obs = [e["text"] for e in events if e["type"] == "observation"]
        self.assertEqual(len(actions), 2, actions)
        self.assertTrue(all(t.startswith("delegate") for t in actions), actions)
        self.assertEqual(sum("[子代理" in t for t in obs), 2, obs)
        self.assertIn("两个子代理都完成", events[-1]["text"])
        # 两个子代理的 token 都计入父回合
        self.assertGreaterEqual(agent_trace.recent(1)[0]["total_tokens"], 4 * 3)


if __name__ == "__main__":
    unittest.main()
