# -*- coding: utf-8 -*-
"""B3 · W-C per-number 背书粒度（§5 高估问题的可观测化）。

承重前提（用户第56批钉）：**只加旁路信号**——episode 的 `backed` bool 语义
（any-hit，`test_provenance.test_episode_is_backed_helper`）与 backed→trust 升序
（`test_backed_episode_outranks_nonbacked_on_equal_score`）逐字节不变。本套只测
纯函数 `_episode_number_audit`：它把那条粗 bool 摊成逐个数明细，且
`backed_hit` 与 `_episode_is_backed` **恒等**（零漂移不变式）。不碰 remember/to_record。
"""
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
import agent_memory  # noqa: E402
import agent_trace  # noqa: E402
import sessions  # noqa: E402

_audit = agent_mod._episode_number_audit
_backed = agent_mod._episode_is_backed
_emit = agent_mod._provenance_audit_emit


class PureAuditTests(unittest.TestCase):
    def test_overestimate_scenario_headline_unbacked_but_incidental_backed(self):
        # §5 的病灶：头条 20100 程序内自算、不在强池；附带 5000 命中强池。
        # 旧 episode bool = any-hit -> True（整条被背书，高估）。
        # 新审计把它摊开：backed=[5000]、unbacked=[20100]。
        ev = {"numbers_strong": ["5000"], "numbers_weak": []}
        a = _audit("总营收 20100，其中 A 类 5000。", ev)
        self.assertEqual(a["backed"], ["5000"])
        self.assertEqual(a["unbacked"], ["20100"])
        self.assertTrue(a["backed_hit"])          # bool 语义不动，仍 True
        self.assertTrue(_backed("总营收 20100，其中 A 类 5000。", ev))

    def test_no_backed_any_number_when_pool_empty(self):
        ev = {"numbers_strong": [], "numbers_weak": ["5000", "20100"]}
        a = _audit("总营收 20100，其中 A 类 5000。", ev)
        self.assertEqual(a["backed"], [])
        self.assertEqual(a["unbacked"], ["20100", "5000"])
        self.assertFalse(a["backed_hit"])
        self.assertFalse(_backed("总营收 20100，其中 A 类 5000。", ev))

    def test_all_numbers_backed(self):
        ev = {"numbers_strong": ["1.25", "3"]}
        a = _audit("单价 1.25 元，共 3 件。", ev)
        self.assertEqual(a["backed"], ["1.25", "3"])
        self.assertEqual(a["unbacked"], [])

    def test_dotted_version_not_extracted(self):
        # _PROV_NUM_RE 排除点分版本串（对齐 test_number_regex_excludes_dotted_version）。
        ev = {"numbers_strong": ["9"]}
        a = _audit("升级 v1.2.3 后修了 9 个 bug。", ev)
        self.assertNotIn("1.2.3", a["fnums"])
        self.assertEqual(a["backed"], ["9"])

    def test_normalization_int_float_zero_collapse(self):
        # _prov_norm 把 '7.0' 收成 '7'，抽取器与 bool 判定共用同一归一化。
        ev = {"numbers_strong": ["7"]}
        a = _audit("实测 7.0 次。", ev)
        self.assertIn("7", a["fnums"])
        self.assertEqual(a["backed"], ["7"])


class ZeroDriftInvariantTests(unittest.TestCase):
    """核心不变式：`_episode_number_audit(...)["backed_hit"]` 恒等于
    `_episode_is_backed(...)`，逐位复现锁定 helper 的全部判据。"""

    _CASES = [
        # (final_text, tool_evidence) —— 覆盖 test_episode_is_backed_helper 的每一条
        ("所以结果是 1.25 元", {"numbers_strong": ["1.25"]}),
        ("打了 1 下", {"numbers_strong": [], "numbers_weak": ["1"]}),
        ("共 7 项", {"numbers_strong": ["9", "8"]}),
        ("无数字", {"numbers_strong": ["9"]}),
        ("1.25", None),
        ("1.25", {}),
        # §5 高估场景也必须相等（bool 仍是 True，明细才揭示头条未背书）
        ("总营收 20100，其中 A 类 5000。", {"numbers_strong": ["5000"]}),
    ]

    def test_backed_hit_equals_episode_is_backed_on_every_case(self):
        for text, ev in self._CASES:
            with self.subTest(text=text):
                self.assertEqual(
                    _audit(text, ev)["backed_hit"],
                    _backed(text, ev),
                    "零漂移不变式破坏：backed_hit 必须逐位等于 episode bool",
                )


class FailClosedAuditTests(unittest.TestCase):
    def test_none_evidence_returns_empty_not_raise(self):
        a = _audit("答案是 42。", None)
        self.assertEqual(a, {"fnums": ["42"], "backed": [],
                             "unbacked": ["42"], "backed_hit": False})

    def test_malformed_evidence_fail_closed(self):
        # numbers_strong 不是可迭代 -> 内部异常被吞、回空审计，绝不抛。
        a = _audit("x 1", {"numbers_strong": object()})
        self.assertEqual(a, {"fnums": [], "backed": [],
                             "unbacked": [], "backed_hit": False})

    def test_empty_final(self):
        a = _audit("", {"numbers_strong": ["1"]})
        self.assertEqual(a["fnums"], [])
        self.assertFalse(a["backed_hit"])


class EmitGateTests(unittest.TestCase):
    """env 门控侧信道：默认关=不写、开=一条 JSONL 只记数、坏路径静默降级。"""

    def test_default_off_writes_nothing(self):
        d = tempfile.mkdtemp(prefix="prov_audit_off_")
        target = os.path.join(d, "audit.jsonl")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCMIND_PROVENANCE_AUDIT", None)
            ok = _emit(_audit("营收 20100，其中 5000。", {"numbers_strong": ["5000"]}))
        self.assertFalse(ok)                    # 未配置 -> 空操作
        self.assertFalse(os.path.exists(target))

    def test_gate_on_writes_one_jsonl_with_numbers(self):
        d = tempfile.mkdtemp(prefix="prov_audit_on_")
        target = os.path.join(d, "audit.jsonl")
        audit = _audit("总营收 20100，其中 A 类 5000。", {"numbers_strong": ["5000"]})
        with mock.patch.dict(os.environ, {"DOCMIND_PROVENANCE_AUDIT": target}):
            ok = _emit(audit, key="营收问题")
        self.assertTrue(ok)
        with open(target, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertTrue(rec["backed_hit"])
        self.assertEqual(rec["backed"], ["5000"])
        self.assertEqual(rec["unbacked"], ["20100"])
        self.assertEqual(rec["n_fnums"], 2)
        self.assertEqual(rec["key"], "营收问题")
        # 只记数，绝不写 final 全文（规避泄漏/注入面）
        self.assertNotIn("总营收", lines[0])
        self.assertNotIn("A 类", lines[0])

    def test_unwritable_path_degrades_silently(self):
        # 把一个普通文件当父目录 -> makedirs 抛 -> 捕获 -> 回 False，不炸主流程
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".notadir")
        f.close()
        bad = os.path.join(f.name, "audit.jsonl")
        with mock.patch.dict(os.environ, {"DOCMIND_PROVENANCE_AUDIT": bad}):
            ok = _emit({"backed_hit": False, "fnums": [], "backed": [], "unbacked": []})
        os.unlink(f.name)
        self.assertFalse(ok)

    def test_malformed_audit_dict_defaults(self):
        d = tempfile.mkdtemp(prefix="prov_audit_malformed_")
        target = os.path.join(d, "audit.jsonl")
        with mock.patch.dict(os.environ, {"DOCMIND_PROVENANCE_AUDIT": target}):
            ok = _emit({})                       # 缺键也不抛，写默认
        self.assertTrue(ok)
        rec = json.loads(open(target, encoding="utf-8").read().strip())
        self.assertFalse(rec["backed_hit"])
        self.assertEqual(rec["n_fnums"], 0)


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


class ProductionWiringTests(unittest.TestCase):
    """端到端：真实 agent.run 走完 finalize 块，证明 B3 旁路**触发**且**不改动** backed。"""

    SCRIPTS = [
        "Thought: 算 A 类\nAction: calculate\nAction Input: 5000",
        "Final Answer: 总营收 20100，其中 A 类 5000。",
    ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_b3_wire_")
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
                "SELECT backed FROM memories WHERE scope=? AND kind='episode' "
                "ORDER BY updated DESC LIMIT 1", (scope,)).fetchone()
        return None if row is None else row[0]

    def _run(self):
        a = agent_mod.Agent(llm=_FakeLLM(list(self.SCRIPTS)), session_id="s-b3")
        list(a.run("营收是多少", stream=True))
        return a

    def test_gate_on_audit_fires_and_backed_unchanged(self):
        target = os.path.join(self.tmp, "audit.jsonl")
        with mock.patch.dict(os.environ, {"DOCMIND_PROVENANCE_AUDIT": target}):
            a = self._run()
        # 旁路确实触发：一条审计，backed=[5000]、unbacked=[20100]、backed_hit=True
        with open(target, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertIn("5000", rec["backed"])
        self.assertIn("20100", rec["unbacked"])
        self.assertTrue(rec["backed_hit"])
        # 但 episode 的 backed bool 仍按 any-hit = 1（头条未背书不改变 bool，§5 只观测）
        self.assertEqual(self._episode_backed_col(a.memory_scope), 1)

    def test_default_off_no_file_and_backed_identical(self):
        target = os.path.join(self.tmp, "audit.jsonl")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCMIND_PROVENANCE_AUDIT", None)
            a = self._run()
        self.assertFalse(os.path.exists(target))      # 默认关=零写盘
        self.assertEqual(self._episode_backed_col(a.memory_scope), 1)  # 落库逐字节同今天

    def test_emit_failure_cannot_break_persist(self):
        target = os.path.join(self.tmp, "audit.jsonl")
        with mock.patch.dict(os.environ, {"DOCMIND_PROVENANCE_AUDIT": target}), \
                mock.patch.object(agent_mod, "_provenance_audit_emit",
                                  side_effect=RuntimeError("boom")):
            a = self._run()
        # 审计抛异常也吞掉——上面已完成的 remember 不受影响
        self.assertEqual(self._episode_backed_col(a.memory_scope), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
