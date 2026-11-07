# -*- coding: utf-8 -*-
"""上下文窗口用量与按窗口压缩的回归测试（全部离线，不触网）。

覆盖：
① Agent.context_stats 字段与高占用分级；
② run() 每轮开/收尾各推一次 SSE context 事件；
③ 历史回放窗口按模型预算缩放（大窗口多带轮次，小窗口只带最近几轮）；
④ 端到端：小预算模型多轮后触发压缩，notice 后进度条回落；
⑤ GET /api/context 刷新恢复用量。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
import sessions  # noqa: E402


class _FakeLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self, scripts=(), capability=None, prompt_budget=0, token_fn=None):
        self.scripts = list(scripts)
        self.i = 0
        self.last_usage = {"prompt_tokens": 7, "completion_tokens": 3}
        self.capability = capability or {"context_window": 32768, "thinking": "none", "cloud": False}
        # 0 表示走 Agent 的 PROMPT_TOKEN_BUDGET 兜底（模拟旧假客户端）
        self.prompt_budget = prompt_budget
        self._token_fn = token_fn

    def chat(self, messages, stream=True, **kw):
        script = self.scripts[min(self.i, len(self.scripts) - 1)] if self.scripts else "Final Answer: ok"
        self.i += 1
        self.last_usage = {"prompt_tokens": 7, "completion_tokens": 3}
        if stream:
            def g():
                for ch in script:
                    yield ch
            return g()
        return script

    def count_tokens(self, text):
        if self._token_fn:
            return self._token_fn(text)
        return max(0, len(text) // 3)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_ctx_")
        self._old_dir = sessions.SESSIONS_DIR
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "sessions")

    def tearDown(self):
        sessions.SESSIONS_DIR = self._old_dir


class ContextStatsTests(_Base):
    def test_stats_fields_and_percent_range(self):
        a = agent_mod.Agent(llm=_FakeLLM(), session_id="s1")
        st = a.context_stats("你好")
        self.assertEqual(set(st), {
            "used_tokens", "context_window", "prompt_budget", "percent", "level",
            "history_tokens", "compact_trigger_tokens", "compact_percent",
        })
        self.assertEqual(st["context_window"], 32768)
        self.assertGreaterEqual(st["percent"], 0)
        self.assertLessEqual(st["percent"], 100)
        self.assertGreater(st["used_tokens"], 0)
        self.assertEqual(st["level"], "ok")
        # 空历史时压缩口径应为 0，level 不误报
        self.assertEqual(st["history_tokens"], 0)
        self.assertEqual(st["compact_percent"], 0)

    def test_level_from_compact_percentile(self):
        # level 由「历史 token / 压缩触发线」口径分级：budget=100000 -> 触发线 80000
        budget = 100000
        cap = {"context_window": 131072, "thinking": "none", "cloud": True}
        high = _FakeLLM(capability=cap, prompt_budget=budget, token_fn=lambda _t: 90000)
        a = agent_mod.Agent(llm=high, session_id="lv")
        a.history = [{"user": "x", "assistant": "y", "ts": ""}]
        st = a.context_stats("q")
        self.assertEqual(st["compact_trigger_tokens"], 80000)
        self.assertEqual(st["compact_percent"], 100)   # 90000/80000=112% 封顶 100
        self.assertEqual(st["level"], "high")

        warn = _FakeLLM(capability=cap, prompt_budget=budget, token_fn=lambda _t: 70000)
        a2 = agent_mod.Agent(llm=warn, session_id="lv")
        a2.history = [{"user": "x", "assistant": "y", "ts": ""}]
        self.assertEqual(a2.context_stats("q")["level"], "warn")   # 87%

        ok = _FakeLLM(capability=cap, prompt_budget=budget, token_fn=lambda _t: 10000)
        a3 = agent_mod.Agent(llm=ok, session_id="lv")
        a3.history = [{"user": "x", "assistant": "y", "ts": ""}]
        self.assertEqual(a3.context_stats("q")["level"], "ok")     # 12%

    def test_compact_fields_align_with_trigger(self):
        import config as config_mod
        budget = 12000
        llm = _FakeLLM(
            capability={"context_window": 16384, "thinking": "none", "cloud": False},
            prompt_budget=budget, token_fn=lambda _t: 6000)
        a = agent_mod.Agent(llm=llm, session_id="cmp")
        a.history = [{"user": "u", "assistant": "a", "ts": ""}]
        st = a.context_stats("q")
        trigger = int(budget * config_mod.COMPACT_TRIGGER_RATIO)
        self.assertEqual(st["compact_trigger_tokens"], trigger)
        self.assertEqual(st["history_tokens"], 6000)
        self.assertEqual(st["compact_percent"], round(6000 * 100 / trigger))
        self.assertEqual(st["level"], "ok")
        # compact_percent 恰为 100 时必须判为 high（历史达到压缩触发线）
        llm2 = _FakeLLM(
            capability={"context_window": 16384, "thinking": "none", "cloud": False},
            prompt_budget=budget, token_fn=lambda _t: trigger)
        a2 = agent_mod.Agent(llm=llm2, session_id="cmp2")
        a2.history = [{"user": "u", "assistant": "a", "ts": ""}]
        st2 = a2.context_stats("q")
        self.assertEqual(st2["compact_percent"], 100)
        self.assertEqual(st2["level"], "high")

    def test_percent_denominator_is_budget_not_full_window(self):
        # 用量恰好等于「可用额度」时即 100%，即便它只占完整窗口的一部分；
        # 超过额度也封顶 100%（由每轮 _fit_budget 保证实际不会超发）
        llm = _FakeLLM(
            capability={"context_window": 16384, "thinking": "none", "cloud": False},
            prompt_budget=12000, token_fn=lambda _t: 12000)
        st = agent_mod.Agent(llm=llm, session_id="dn").context_stats("q")
        self.assertEqual(st["percent"], 100)
        llm._token_fn = lambda _t: 6000
        self.assertEqual(agent_mod.Agent(llm=llm, session_id="dn").context_stats("q")["percent"], 50)


class ContextEventTests(_Base):
    def test_run_emits_context_events_start_and_end(self):
        llm = _FakeLLM(["Final Answer: A"])
        evs = list(agent_mod.Agent(llm=llm, session_id="ev").run("q1", stream=True))
        ctx = [e for e in evs if e.get("type") == "context"]
        self.assertEqual(len(ctx), 2, "开工与收尾各应推一次 context 事件")
        for e in ctx:
            self.assertIn("percent", e)
            self.assertIn("used_tokens", e)
            self.assertIn("context_window", e)
            self.assertIn("level", e)
        # 收尾时本轮问答已入历史，用量不应低于开工时
        self.assertGreaterEqual(ctx[-1]["used_tokens"], ctx[0]["used_tokens"])

    def test_ephemeral_agent_no_context_event(self):
        # 无 session_id 的临时 Agent 不刷 UI 用量
        evs = list(agent_mod.Agent(llm=_FakeLLM(["Final Answer: x"])).run("q", stream=True))
        self.assertFalse([e for e in evs if e.get("type") == "context"])


class HistoryWindowTests(_Base):
    def _agent_with_history(self, n, budget, chars=300):
        llm = _FakeLLM(prompt_budget=budget)
        a = agent_mod.Agent(llm=llm, session_id="hw")
        a.history = [{"user": "u" * chars, "assistant": "a" * chars, "ts": ""} for _ in range(n)]
        return a

    def test_small_budget_keeps_only_recent_turns(self):
        # 每轮约 600 字符 ≈ 200 token；预算 1000 -> 保留目标 350 -> 只装得下最近 1 轮
        a = self._agent_with_history(10, budget=1000)
        window = a._history_window()
        self.assertEqual(len(window), 1)
        self.assertEqual(window[0]["user"], "u" * 300)

    def test_large_budget_carries_more_turns(self):
        # 预算 4000 -> 保留目标 1400 -> 约 7 轮（受 AGENT_HISTORY_TURNS 硬上限保护）
        small = self._agent_with_history(10, budget=1000)
        large = self._agent_with_history(10, budget=4000)
        self.assertLess(len(small._history_window()), len(large._history_window()))
        self.assertGreaterEqual(len(large._history_window()), 6)

    def test_history_never_exceeds_hard_ceiling(self):
        # 预算极大也不得回放超过 AGENT_HISTORY_TURNS 轮
        from config import AGENT_HISTORY_TURNS
        a = self._agent_with_history(AGENT_HISTORY_TURNS + 50, budget=10 ** 8, chars=10)
        self.assertEqual(len(a._history_window()), AGENT_HISTORY_TURNS)


class EndToEndCompactionTests(_Base):
    def test_compaction_drops_usage_gauge(self):
        # 真实口径：预算 12000（16k 窗口模型），触发线 80%=9600 token；
        # 每轮问题 8000 字 ≈ 2667 token，累积到第 4 轮必触发压缩
        llm = _FakeLLM(["Final Answer: ok"] * 8, prompt_budget=12000)
        a = agent_mod.Agent(llm=llm, session_id="e2e")
        question = "压测内容" + "甲乙丙丁" * 1999  # 8000 字
        notices = []
        compacted = None
        for i in range(4):
            ctxs, turn_notices = [], []
            for ev in a.run(question, stream=True):
                if ev.get("type") == "context":
                    ctxs.append(ev)
                elif ev.get("type") == "notice":
                    turn_notices.append(ev.get("text", ""))
            self.assertGreaterEqual(len(ctxs), 2)
            if turn_notices:
                notices.extend(turn_notices)
                # 压缩当轮：开工事件在前，notice 后的收尾事件在最后
                compacted = (ctxs[0], ctxs[-1])
        self.assertTrue(notices, "累积历史超过 80% 额度后应触发自动压缩")
        # run() 还会发送“准备上下文/等待模型”等过程通知，压缩通知
        # 不保证是第一条；只要求本轮确实出现过压缩提示。
        self.assertTrue(any("压缩" in notice for notice in notices))
        self.assertGreaterEqual(len(a.history), sessions.KEEP_TURNS_MIN)
        self.assertTrue(a.summary)
        # notice 后的收尾事件必须把进度条刷回去（至少回落一整轮的体积）
        start_ctx, end_ctx = compacted
        self.assertLess(end_ctx["used_tokens"], start_ctx["used_tokens"])
        self.assertLess(end_ctx["percent"], start_ctx["percent"])

    def test_pre_and_post_compaction_share_prompt_budget_thresholds(self):
        import config as config_mod
        budget = 12000
        llm = _FakeLLM(
            ["Final Answer: ok"],
            capability={"context_window": 16384, "thinking": "none", "cloud": False},
            prompt_budget=budget)
        calls = []
        original = sessions.maybe_compact

        def capture(*args, **kwargs):
            calls.append(kwargs)
            return original(*args, **kwargs)

        with patch.object(sessions, "maybe_compact", new=capture):
            list(agent_mod.Agent(llm=llm, session_id="same-budget").run("q", stream=True))
        self.assertEqual(len(calls), 2)  # 回合前、回合后各一次
        for call in calls:
            self.assertEqual(call["trigger_tokens"], int(budget * config_mod.COMPACT_TRIGGER_RATIO))
            self.assertEqual(call["keep_tokens"], int(budget * config_mod.COMPACT_KEEP_RATIO))


class ContextApiTests(_Base):
    def test_unknown_session_inactive(self):
        from api import app
        from starlette.testclient import TestClient
        with TestClient(app) as c:
            r = c.get("/api/context?session_id=__no_such_session__")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["active"])

    def test_returns_cached_snapshot(self):
        import api
        from api import app
        from starlette.testclient import TestClient

        class _StubAgent:
            last_context = {
                "used_tokens": 1234, "context_window": 16384,
                "prompt_budget": 12288, "percent": 8, "level": "ok",
            }

            def context_stats(self, question=""):
                return self.last_context

        key = "__ctx_stub__"
        api._SESSION_AGENTS[key] = _StubAgent()
        try:
            with TestClient(app) as c:
                data = c.get(f"/api/context?session_id={key}").json()
        finally:
            api._SESSION_AGENTS.pop(key, None)
        self.assertTrue(data["active"])
        self.assertEqual(data["used_tokens"], 1234)
        self.assertEqual(data["context_window"], 16384)
        self.assertEqual(data["percent"], 8)


if __name__ == "__main__":
    unittest.main()
