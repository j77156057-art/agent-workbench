# -*- coding: utf-8 -*-
"""Provenance 供给层（P1 基建）确定性测试。

覆盖：
- `_tool_evidence_from_obs` 对 calculate / python_exec / memory_save 的解析与密度门分级；
- `Turn.add_tool_evidence` 的分桶/去重/单向 OR；
- 数字抽取正则不误吃版本号；归一化；
- 线程安全（并行批次多线程写同一 Turn）；
- 护栏：`to_record()` **不含** tool_evidence（纯内存、不落盘）；
- 端到端接线：真实 agent.run 走 calculate → Turn.tool_evidence 被填。
"""
import json
import os
import tempfile
import threading
import unittest
from contextlib import closing
from unittest.mock import patch

import agent as agent_mod
import agent_memory
import agent_trace
import sessions


class ParserTests(unittest.TestCase):
    """纯函数层：_tool_evidence_from_obs 的解析与分级。"""

    def test_calculate_pure_number_is_strong(self):
        for expr_obs in ("1.25", "-3", "0.5", "42"):
            ev = agent_mod._tool_evidence_from_obs("calculate", expr_obs, True)
            self.assertIsNotNone(ev, expr_obs)
            self.assertTrue(ev["strong"], expr_obs)
            self.assertTrue(ev["derive"], expr_obs)
            self.assertEqual(len(ev["numbers"]), 1, expr_obs)

    def test_calculate_bool_result_keeps_derive_flag(self):
        for b in ("成立（True）", "不成立（False）"):
            ev = agent_mod._tool_evidence_from_obs("calculate", b, True)
            self.assertEqual(ev, {"numbers": [], "strong": True, "derive": True}, b)

    def test_calculate_error_or_failure_returns_none(self):
        self.assertIsNone(agent_mod._tool_evidence_from_obs("calculate", "计算失败: x", True))
        self.assertIsNone(agent_mod._tool_evidence_from_obs("calculate", "1.25", False))  # ok=False

    def test_python_exec_scalar_is_strong(self):
        ev = agent_mod._tool_evidence_from_obs("python_exec", "result = 1.25\n", True)
        self.assertIsNotNone(ev)
        self.assertTrue(ev["strong"])
        self.assertIn("1.25", ev["numbers"])

    def test_python_exec_dense_numbers_are_weak(self):
        # 池 > K（默认 6）的计数表 → 不授予正向背书（fail-closed）
        dense = "counts:\n" + "\n".join("row %d" % i for i in range(1, 12))
        ev = agent_mod._tool_evidence_from_obs("python_exec", dense, True)
        self.assertIsNotNone(ev)
        self.assertFalse(ev["strong"])
        self.assertTrue(len(ev["numbers"]) > agent_mod._PROVENANCE_POOL_K)

    def test_python_exec_raw_source_echo_is_weak(self):
        # 逐行带行号源码 echo（BUG-7 形态）：即便数字个数被 K 卡住也须判 WEAK
        echo = "\n".join("%d: some_code_line_%d" % (i, i) for i in range(1, 41))
        ev = agent_mod._tool_evidence_from_obs("python_exec", echo, True)
        self.assertIsNotNone(ev)
        self.assertFalse(ev["strong"])

    def test_python_exec_pending_or_blocked_returns_none(self):
        self.assertIsNone(agent_mod._tool_evidence_from_obs(
            "python_exec", "__EXEC_PENDING__ pid=E1 tier=2 need=1\n尚未运行。", True))
        self.assertIsNone(agent_mod._tool_evidence_from_obs(
            "python_exec", "__EXEC_BLOCKED__ reason=x\n拒绝执行。", True))

    def test_python_exec_no_numbers_still_marks_derive(self):
        ev = agent_mod._tool_evidence_from_obs("python_exec", "printed nothing numeric", True)
        self.assertEqual(ev, {"numbers": [], "strong": True, "derive": True})

    def test_memory_save_superseded_detected(self):
        ok_super = json.dumps({"ok": True, "id": "m2", "superseded": "m1"}, ensure_ascii=False)
        self.assertEqual(agent_mod._tool_evidence_from_obs("memory_save", ok_super, True),
                         {"numbers": [], "superseded": True})

    def test_memory_save_without_supersede_or_failed_is_none(self):
        note = json.dumps({"ok": True, "id": "m2", "note": "未作废任何旧记忆"}, ensure_ascii=False)
        self.assertIsNone(agent_mod._tool_evidence_from_obs("memory_save", note, True))
        bad = json.dumps({"ok": False, "error": "supersedes 键拼错"}, ensure_ascii=False)
        self.assertIsNone(agent_mod._tool_evidence_from_obs("memory_save", bad, True))

    def test_other_tool_returns_none(self):
        self.assertIsNone(agent_mod._tool_evidence_from_obs("read_file", "12: code", True))

    def test_number_regex_excludes_dotted_version(self):
        # 版本号 1.2.3 不应被拆出任一数字 token（前瞻/后顾挡住点分）
        ev = agent_mod._tool_evidence_from_obs("python_exec", "version 1.2.3 only\n", True)
        self.assertNotIn("1", ev["numbers"])
        self.assertNotIn("2", ev["numbers"])
        self.assertNotIn("3", ev["numbers"])

    def test_normalizer(self):
        self.assertEqual(agent_mod._prov_norm("3.0"), "3")
        self.assertEqual(agent_mod._prov_norm("+7"), "7")
        self.assertEqual(agent_mod._prov_norm("1.25"), "1.25")
        self.assertEqual(agent_mod._prov_norm(""), "")


class TurnAccumulatorTests(unittest.TestCase):
    def test_bucketing_dedup_and_flags(self):
        t = agent_trace.Turn(session_id="s")
        t.add_tool_evidence(numbers=["1.25"], strong=True, derive=True)
        t.add_tool_evidence(numbers=["1.25", "9", "11"], strong=False)  # 1.25 已在 strong，去重
        t.add_tool_evidence(superseded=True)
        e = t.tool_evidence
        self.assertEqual(e["numbers_strong"], ["1.25"])
        self.assertNotIn("1.25", e["numbers_weak"])
        self.assertEqual(sorted(e["numbers_weak"]), ["11", "9"])
        self.assertTrue(e["superseded_this_turn"])
        self.assertTrue(e["had_derive_tool"])

    def test_empty_call_is_noop(self):
        t = agent_trace.Turn(session_id="s")
        t.add_tool_evidence()  # 无 numbers/superseded/derive → 直接返回
        self.assertEqual(t.tool_evidence,
                         {"numbers_strong": [], "numbers_weak": [],
                          "superseded_this_turn": False, "had_derive_tool": False})

    def test_record_does_not_persist_tool_evidence(self):
        # 护栏：tool_evidence 是纯内存，绝不进 to_record（与 verification_targets 原始字典同策略）
        t = agent_trace.Turn(session_id="s")
        t.add_tool_evidence(numbers=["1.25"], strong=True, derive=True)
        t.finish("completed", final_text="ans")
        rec = t.to_record()
        self.assertNotIn("tool_evidence", rec)
        for v in rec.values():
            self.assertNotIn("1.25", str(v))

    def test_thread_safety_parallel_writes(self):
        t = agent_trace.Turn(session_id="s")
        N = 16

        def worker(i):
            t.add_tool_evidence(numbers=[str(i)], strong=(i % 2 == 0))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        got = set(t.tool_evidence["numbers_strong"]) | set(t.tool_evidence["numbers_weak"])
        self.assertEqual(got, {str(i) for i in range(N)})


class _FakeLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.i = 0
        self.last_usage = {}

    def chat(self, messages, stream=True, **kw):
        s = self.scripts[min(self.i, len(self.scripts) - 1)]
        self.i += 1
        self.last_usage = {"prompt_tokens": 7, "completion_tokens": 3}
        if stream:
            def g():
                for ch in s:
                    yield ch
            return g()
        return s

    def count_tokens(self, text):
        return max(0, len(text) // 3)


class IntegrationWiringTests(unittest.TestCase):
    """驱动真实 agent.run（含一次 calculate），断言接线把证据填进了 Turn。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_prov_")
        self._o = (agent_trace.TRACE_FILE, sessions.SESSIONS_DIR, agent_trace.TRACE_ENABLED)
        agent_trace.TRACE_FILE = os.path.join(self.tmp, "traces.jsonl")
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "sessions")
        agent_trace.TRACE_ENABLED = True

    def tearDown(self):
        agent_trace.TRACE_FILE, sessions.SESSIONS_DIR, agent_trace.TRACE_ENABLED = self._o

    def test_calculate_action_populates_turn_evidence(self):
        captured = {}
        orig = agent_trace.Turn.tool_step

        def spy(self, *a, **k):
            captured["turn"] = self
            return orig(self, *a, **k)

        llm = _FakeLLM([
            "Thought: 算一下\nAction: calculate\nAction Input: 5 * 0.25",
            "Final Answer: 结果是 1.25",
        ])
        a = agent_mod.Agent(llm=llm, session_id="s-prov")
        with patch.object(agent_trace.Turn, "tool_step", spy):
            list(a.run("5 乘 0.25 是多少", stream=True))

        turn = captured.get("turn")
        self.assertIsNotNone(turn, "未捕获到本轮 Turn")
        self.assertIn("1.25", turn.tool_evidence["numbers_strong"])
        self.assertTrue(turn.tool_evidence["had_derive_tool"])


class _MemIso(unittest.TestCase):
    """把记忆 DB / 会话 / trace 重定向到临时目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_wc_")
        self._o = (agent_memory.DB_PATH, agent_trace.TRACE_FILE,
                   sessions.SESSIONS_DIR, agent_trace.TRACE_ENABLED)
        agent_memory.DB_PATH = os.path.join(self.tmp, "memory.sqlite3")
        agent_trace.TRACE_FILE = os.path.join(self.tmp, "traces.jsonl")
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "sessions")
        agent_trace.TRACE_ENABLED = True

    def tearDown(self):
        (agent_memory.DB_PATH, agent_trace.TRACE_FILE,
         sessions.SESSIONS_DIR, agent_trace.TRACE_ENABLED) = self._o

    def _episode_backed_col(self, scope):
        with closing(agent_memory._connect()) as conn:
            row = conn.execute(
                "SELECT backed FROM memories WHERE scope=? AND kind='episode' ORDER BY updated DESC LIMIT 1",
                (scope,)).fetchone()
        return None if row is None else row[0]


class ConsumerWCTests(_MemIso):
    def test_episode_is_backed_helper(self):
        ev_strong = {"numbers_strong": ["1.25"], "numbers_weak": []}
        self.assertTrue(agent_mod._episode_is_backed("所以结果是 1.25 元", ev_strong))
        # WEAK-only 池永不升（第35批边界）
        ev_weak = {"numbers_strong": [], "numbers_weak": ["1", "2", "3"]}
        self.assertFalse(agent_mod._episode_is_backed("打了 1 下", ev_weak))
        # 无交集
        self.assertFalse(agent_mod._episode_is_backed("共 7 项", ev_strong))
        # 空 / None
        self.assertFalse(agent_mod._episode_is_backed("无数字", ev_strong))
        self.assertFalse(agent_mod._episode_is_backed("1.25", None))
        self.assertFalse(agent_mod._episode_is_backed("1.25", {}))

    def test_recall_does_not_leak_backed_key(self):
        agent_memory.remember("p", "episode", "q", "模型报告：结果 1.25", "ev",
                              source="model_report", key="q", backed=True)
        rows = agent_memory.recall("p", "结果")
        self.assertTrue(rows)
        for r in rows:
            self.assertNotIn("backed", r)          # 内部元数据绝不进注入串
            self.assertNotIn("status", r)
            self.assertNotIn("supersedes", r)
        # 且序列化后不含该列名（context()/API JSON 逐字节不变的护栏）
        self.assertNotIn("backed", json.dumps(rows, ensure_ascii=False))

    def test_backed_episode_outranks_nonbacked_on_equal_score(self):
        # 同词面分（都含"价格"），非背书的先写（更旧无所谓）：背书的 trust 0 > 非背书 -1
        agent_memory.remember("p", "episode", "a", "价格 模型报告", "e1",
                              source="model_report", key="a", backed=False)
        agent_memory.remember("p", "episode", "b", "价格 模型报告", "e2",
                              source="model_report", key="b", backed=True)
        titles = [r["title"] for r in agent_memory.recall("p", "价格", limit=5)]
        self.assertEqual(titles[0], "b", "被工具背书的 episode 应压过未背书（证据 > recency，G2）")

    def test_nonbacked_episode_still_demoted_vs_fact(self):
        # fail-closed：未背书 model_report episode 仍低于已核实 fact（与今天一致）
        agent_memory.remember("p", "fact", "F", "价格 是已核实事实", "e", source="agent", key="F")
        agent_memory.remember("p", "episode", "E", "价格 模型报告", "e", source="model_report", key="E", backed=False)
        titles = [r["title"] for r in agent_memory.recall("p", "价格", limit=5)]
        self.assertEqual(titles[0], "F")

    def test_end_to_end_calculate_backs_persisted_episode(self):
        llm = _FakeLLM([
            "Thought: 算一下\nAction: calculate\nAction Input: 5 * 0.25",
            "Final Answer: 结果是 1.25",
        ])
        a = agent_mod.Agent(llm=llm, session_id="s-wc")
        list(a.run("5 乘 0.25 是多少", stream=True))
        self.assertEqual(self._episode_backed_col(a.memory_scope), 1)

    def test_end_to_end_no_derive_episode_not_backed(self):
        # 纯散文（无 derive 工具）→ episode 落库 backed=0（fail-closed，行为同今天）
        llm = _FakeLLM(["Final Answer: 这是一段没有数字的定性回答"])
        a = agent_mod.Agent(llm=llm, session_id="s-wc2")
        list(a.run("随便说说", stream=True))
        self.assertEqual(self._episode_backed_col(a.memory_scope), 0)


if __name__ == "__main__":
    unittest.main()
