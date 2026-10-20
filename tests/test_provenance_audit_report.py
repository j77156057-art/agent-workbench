# -*- coding: utf-8 -*-
"""B3 审计消费侧 —— 解析器 + 聚合器 确定性测试（只读、fail-closed）。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import provenance_audit_report as par  # noqa: E402
import agent  # noqa: E402
import agent_memory  # noqa: E402
import agent_trace  # noqa: E402
import sessions  # noqa: E402


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


def _write_jsonl(lines):
    d = tempfile.mkdtemp(prefix="prov_rep_")
    p = os.path.join(d, "audit.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")
    return p


def _rec(**kw):
    base = {"ts": 1, "key": "q", "backed_hit": False,
            "n_fnums": 0, "backed": [], "unbacked": []}
    base.update(kw)
    return base


class LoadAuditTests(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(par.load_audit(""), [])
        self.assertEqual(par.load_audit("/no/such/audit.jsonl"), [])

    def test_empty_and_blank_lines(self):
        p = _write_jsonl(["", "   ", ""])
        self.assertEqual(par.load_audit(p), [])

    def test_parses_valid_records(self):
        p = _write_jsonl([json.dumps(_rec(key="A", backed=["5000"], unbacked=["20100"],
                                          backed_hit=True, n_fnums=2))])
        recs = par.load_audit(p)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["key"], "A")
        self.assertEqual(recs[0]["backed"], ["5000"])
        self.assertEqual(recs[0]["unbacked"], ["20100"])
        self.assertEqual(recs[0]["cls"], "overestimated")

    def test_malformed_lines_skipped_not_raise(self):
        p = _write_jsonl([
            "{ not valid json",
            json.dumps(_rec(key="ok", backed_hit=True, unbacked=["7"])),
            "[1,2,3]",            # 非对象行
            "null",
            json.dumps(_rec(key="nokeys")),
        ])
        recs = par.load_audit(p)
        keys = [r["key"] for r in recs]
        self.assertIn("ok", keys)
        self.assertIn("nokeys", keys)
        self.assertNotIn(None, keys)

    def test_missing_keys_fail_closed_defaults(self):
        p = _write_jsonl([json.dumps({})])   # 完全空对象
        recs = par.load_audit(p)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0], {"ts": 0, "key": "", "backed_hit": False,
                                   "n_fnums": 0, "backed": [], "unbacked": [],
                                   "cls": "not_backed"})

    def test_bad_types_normalized(self):
        p = _write_jsonl([json.dumps(
            {"ts": "x", "key": 123, "backed_hit": "truthy", "n_fnums": "2",
             "backed": None, "unbacked": "notalist"})])
        r = par.load_audit(p)[0]
        self.assertEqual(r["ts"], 0)          # 坏 int -> 0
        self.assertEqual(r["key"], "123")     # str 化
        self.assertTrue(r["backed_hit"])      # bool("truthy")=True
        self.assertEqual(r["n_fnums"], 2)     # 可转 int
        self.assertEqual(r["backed"], [])     # None -> []
        self.assertEqual(r["unbacked"], [])   # 非列表 -> []


class ClassifyTests(unittest.TestCase):
    def test_three_categories(self):
        self.assertEqual(par.classify(_rec(backed_hit=False, unbacked=["1"])), "not_backed")
        self.assertEqual(par.classify(_rec(backed_hit=True, unbacked=["1"])), "overestimated")
        self.assertEqual(par.classify(_rec(backed_hit=True, unbacked=[])), "fully_backed")

    def test_backed_false_always_not_backed_even_with_backed_list(self):
        self.assertEqual(par.classify(_rec(backed_hit=False, backed=["9"], unbacked=[])),
                         "not_backed")


class SummarizeTests(unittest.TestCase):
    def _mix(self):
        return par.load_audit(_write_jsonl([
            json.dumps(_rec(key="over", backed_hit=True, backed=["5000"],
                            unbacked=["20100"], n_fnums=2)),
            json.dumps(_rec(key="over2", backed_hit=True, backed=["3"],
                            unbacked=["9", "20100"], n_fnums=3)),
            json.dumps(_rec(key="full", backed_hit=True, backed=["1.25"], n_fnums=1)),
            json.dumps(_rec(key="none", backed_hit=False, n_fnums=0)),
            json.dumps(_rec(key="weakonly", backed_hit=False, n_fnums=1,
                            unbacked=["42"])),
        ]))

    def test_counts_partition_total(self):
        s = par.summarize(self._mix())
        c = s["counts"]
        self.assertEqual(s["total"], 5)
        self.assertEqual(c["overestimated"] + c["fully_backed"] + c["not_backed"], s["total"])

    def test_overestimated_count_matches_invariant(self):
        recs = self._mix()
        s = par.summarize(recs)
        # 高估判定不变式：overestimated == #(backed_hit 且 unbacked 非空)
        manual = sum(1 for r in recs if r["backed_hit"] and r["unbacked"])
        self.assertEqual(s["counts"]["overestimated"], manual)
        self.assertEqual(s["counts"]["overestimated"], 2)

    def test_overestimate_rate_denominator_is_numbered_episodes(self):
        s = par.summarize(self._mix())
        # 有数字 episode = over/over2/full/weakonly = 4（none n_fnums=0 且无列表被排除）
        self.assertEqual(s["episodes_with_numbers"], 4)
        self.assertEqual(s["overestimate_rate"], round(2 / 4, 4))

    def test_top_unbacked_frequency(self):
        s = par.summarize(self._mix())
        top = dict(s["top_unbacked"])
        self.assertEqual(top.get("20100"), 2)   # 出现在 over + over2
        self.assertEqual(top.get("9"), 1)
        self.assertEqual(top.get("42"), 1)      # 未背书数也统计 not_backed 的

    def test_empty_input_no_division_error(self):
        s = par.summarize([])
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["episodes_with_numbers"], 0)
        self.assertEqual(s["overestimate_rate"], 0.0)
        self.assertEqual(s["top_unbacked"], [])

    def test_build_report_end_to_end(self):
        p = _write_jsonl([json.dumps(_rec(key="x", backed_hit=True,
                                          backed=["1"], unbacked=["2"], n_fnums=2))])
        s = par.build_report(p)
        self.assertEqual(s["counts"]["overestimated"], 1)


class RealEpisodeDrivenTests(unittest.TestCase):
    """真跑 episode 开落盘：驱动真实 agent.run 产审计 JSONL，消费侧在其上聚合。

    四个场景覆盖三类分类——overestimated(头条未背书)×2、fully_backed×1、not_backed×1。
    断言全部落在**真实 emit** 出来的数据上，证明消费侧与生产端 schema 严丝合缝。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prov_rep_real_")
        self.audit = os.path.join(self.tmp, "audit.jsonl")
        self._o = (agent_memory.DB_PATH, agent_trace.TRACE_FILE,
                   sessions.SESSIONS_DIR, agent_trace.TRACE_ENABLED)
        agent_memory.DB_PATH = os.path.join(self.tmp, "memory.sqlite3")
        agent_trace.TRACE_FILE = os.path.join(self.tmp, "traces.jsonl")
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "sessions")
        agent_trace.TRACE_ENABLED = True

    def tearDown(self):
        (agent_memory.DB_PATH, agent_trace.TRACE_FILE,
         sessions.SESSIONS_DIR, agent_trace.TRACE_ENABLED) = self._o

    def _drive(self, scripts, question, sid):
        with mock.patch.dict(os.environ, {"DOCMIND_PROVENANCE_AUDIT": self.audit}):
            a = agent.Agent(llm=_FakeLLM(scripts), session_id=sid)
            list(a.run(question, stream=True))

    def test_real_audit_drives_report(self):
        # Ep A：calculate 出 5000（强），但 final 头条 20100 程序内自算 → overestimated
        self._drive(["Thought: a\nAction: calculate\nAction Input: 2500 * 2",
                     "Final Answer: 总营收 20100，其中 A 类 5000。"], "营收多少", "sA")
        # Ep B：calculate 出 1.25，final 只引 1.25 → fully_backed
        self._drive(["Thought: b\nAction: calculate\nAction Input: 5 * 0.25",
                     "Final Answer: 结果是 1.25"], "5 乘 0.25", "sB")
        # Ep C：无 derive 工具、无数字 → not_backed
        self._drive(["Final Answer: 这是一段没有数字的定性回答。"], "随便说说", "sC")
        # Ep D：calculate 出 7，final 另有 99 存疑未背书 → overestimated
        self._drive(["Thought: d\nAction: calculate\nAction Input: 3 + 4",
                     "Final Answer: 共 7 项，另有 99 存疑"], "计数", "sD")

        s = par.build_report(self.audit)
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["counts"]["overestimated"], 2)   # A、D
        self.assertEqual(s["counts"]["fully_backed"], 1)    # B
        self.assertEqual(s["counts"]["not_backed"], 1)      # C
        top = dict(s["top_unbacked"])
        self.assertEqual(top.get("20100"), 1)
        self.assertEqual(top.get("99"), 1)
        # 高估率分母 = 有数字 episode（A/B/D = 3），C 无数字被排除
        self.assertEqual(s["episodes_with_numbers"], 3)
        self.assertEqual(s["overestimate_rate"], round(2 / 3, 4))
        # 逐条 records 也真含 per-number 明细
        by_key = {r["key"]: r for r in s["records"]}
        self.assertIn("5000", by_key["营收多少"]["backed"])
        self.assertIn("20100", by_key["营收多少"]["unbacked"])


class RenderTests(unittest.TestCase):
    def _data(self):
        return par.summarize(par.load_audit(_write_jsonl([
            json.dumps(_rec(key="总营收问题", backed_hit=True, backed=["5000"],
                            unbacked=["20100"], n_fnums=2, ts=1700000000)),
            json.dumps(_rec(key="干净答案", backed_hit=True, backed=["1.25"],
                            n_fnums=1, ts=1700000001)),
            json.dumps(_rec(key="无数字", backed_hit=False, n_fnums=0, ts=1700000002)),
        ])))

    def test_render_text_reflects_aggregates(self):
        t = par.render_text(self._data(), source="x.jsonl")
        self.assertIn("总营收问题", t)
        self.assertIn("5000", t)
        self.assertIn("20100", t)
        self.assertIn("高估", t)
        self.assertIn("全背书", t)
        self.assertNotIn("Traceback", t)

    def test_render_text_empty_dash_for_no_numbers(self):
        d = par.summarize(par.load_audit(_write_jsonl([json.dumps(_rec(key="e"))])))
        t = par.render_text(d)
        self.assertIn("—", t)   # backed/unbacked 空 -> 破折号

    def test_html_is_self_contained_no_external_refs(self):
        h = par.render_html(self._data(), source="x.jsonl")
        self.assertTrue(h.startswith("<!DOCTYPE html>"))
        self.assertIn("<style>", h)
        self.assertIn("</html>", h)
        # 无外链：没有任何 http(s) 的 src=/href=
        self.assertNotIn("http://", h)
        self.assertNotIn("https://", h)
        self.assertNotIn("<script src", h)
        self.assertNotIn("<link", h)

    def test_html_escapes_malicious_key(self):
        evil = _rec(key="<script>alert(1)</script>", backed_hit=True,
                    backed=["1"], unbacked=["<b>9</b>"])
        d = par.summarize(par.load_audit(_write_jsonl([json.dumps(evil)])))
        h = par.render_html(d)
        self.assertNotIn("<script>alert", h)          # 原样脚本绝不落 HTML
        self.assertIn("&lt;script&gt;", h)            # 被转义
        self.assertNotIn("<b>9</b>", h)               # unbacked 里的标签也转义

    def test_html_highlights_overestimated_rows(self):
        h = par.render_html(self._data())
        self.assertIn("class='hi'", h)               # 高估行被高亮
        self.assertIn("overestimated", h)
        self.assertIn("<table>", h)

    def test_html_empty_state(self):
        h = par.render_html(par.summarize([]))
        self.assertIn("暂无审计记录", h)
        self.assertNotIn("<table>", h)


class MainTests(unittest.TestCase):
    def test_main_writes_html_and_returns_zero(self):
        d = tempfile.mkdtemp(prefix="prov_rep_main_")
        audit = _write_jsonl([json.dumps(_rec(key="q", backed_hit=True,
                                              backed=["5"], unbacked=["6"], n_fnums=2))])
        out = os.path.join(d, "sub", "report.html")
        rc = par.main([audit, "--out", out])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(out))
        with open(out, encoding="utf-8") as f:
            self.assertIn("<!DOCTYPE html>", f.read())

    def test_main_missing_file_returns_two(self):
        self.assertEqual(par.main(["/no/such/audit.jsonl"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
