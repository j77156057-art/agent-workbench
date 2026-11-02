"""Phase 3/4 单元测试：经验记忆 + 评测门。

所有用例都用**内存假集合 + 假嵌入客户端**替换真实 Chroma/embedding，
因此无需联网、无需真实向量库即可稳定跑（与经验集合「维度锁定」风险隔离）。
"""
import json
import os
import sys
import tempfile
import unittest
from types import MethodType

# 让测试能 import 顶层模块（tests 目录里运行 discover 时顶层包不在 sys.path）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import experience as exp
import agent as agent_mod


# ---------------------------------------------------------------------------
# 内存假集合 / 假嵌入客户端
# ---------------------------------------------------------------------------
class FakeCol:
    def __init__(self):
        self.store = {}  # id -> {'doc','meta','emb'}

    def upsert(self, ids=None, documents=None, embeddings=None, metadatas=None):
        for i, meta in zip(ids, metadatas):
            self.store[i] = {
                "doc": documents[ids.index(i)] if documents else None,
                "meta": meta,
                "emb": (embeddings or [None])[ids.index(i)],
            }

    def query(self, query_embeddings=None, n_results=5, where=None):
        out = []
        for _id, v in self.store.items():
            if where and v["meta"].get("project_id") != where.get("project_id"):
                continue
            out.append((_id, v))
        ids, dists, metas = [], [], []
        for _id, v in out[:n_results]:
            ids.append(_id)
            metas.append(v["meta"])
            dists.append(0.2)
        return {"ids": [ids], "distances": [dists], "metadatas": [metas]}

    def get(self, where=None, include=None, limit=None):
        ids, metas = [], []
        for _id, v in self.store.items():
            if where and v["meta"].get("project_id") != where.get("project_id"):
                continue
            ids.append(_id)
            metas.append(v["meta"])
        return {"ids": ids, "metadatas": metas}

    def count(self):
        return len(self.store)

    def delete(self, ids=None):
        for _id in (ids or []):
            self.store.pop(_id, None)


class FakeEmbed:
    def embed(self, texts):
        # 固定 4 维向量，足以驱动 upsert / query 走通逻辑
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class FakeLLM:
    """返回确定文本（或被设为抛异常）以测试 _extract_lesson。"""

    def __init__(self, text="教训：先回滚再排查。", raise_on_call=False):
        self._text = text
        self._raise = raise_on_call

    def chat(self, messages, stream=False, temperature=0.0):
        if self._raise:
            raise RuntimeError("llm unavailable")
        return self._text


class ExperienceTestBase(unittest.TestCase):
    def setUp(self):
        self._orig_col = exp._experience_collection
        self._orig_emb = exp._embed_client
        self._orig_max = exp.EXPERIENCE_MAX
        self.col = FakeCol()
        exp._experience_collection = lambda: self.col
        exp._embed_client = lambda: FakeEmbed()

    def tearDown(self):
        exp._experience_collection = self._orig_col
        exp._embed_client = self._orig_emb
        exp.EXPERIENCE_MAX = self._orig_max


class TestRecordScrubDedupIsolation(ExperienceTestBase):
    def test_scrub_redacts_secrets(self):
        ok = exp.record_episode("p1", "token=sk-abcdefghijk123 api_key=secretX 配置改动",
                                "决定先回滚", "fail-then-fixed")
        self.assertTrue(ok)
        meta = self.col.store[list(self.col.store)[0]]["meta"]
        self.assertNotIn("sk-abcdefghijk123", meta["action_summary"])
        self.assertIn("[REDACTED]", meta["action_summary"])

    def test_dedup_same_project_outcome_summary(self):
        a = exp.record_episode("p1", "同样的改动", "d", "fail-then-fixed")
        b = exp.record_episode("p1", "同样的改动", "d", "fail-then-fixed")
        self.assertTrue(a and b)
        # 去重 id 相同 → 仅一条记录
        self.assertEqual(self.col.count(), 1)

    def test_different_outcome_same_summary_is_separate(self):
        exp.record_episode("p1", "同样的改动", "d", "fail-then-fixed")
        exp.record_episode("p1", "同样的改动", "d", "repeated-fail")
        self.assertEqual(self.col.count(), 2)

    def test_project_isolation_on_recall(self):
        exp.record_episode("pA", "A 的经验", "d", "success")
        exp.record_episode("pB", "B 的经验", "d", "success")
        hits = exp.recall_similar("pA", "经验")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["metadata"]["project_id"], "pA")

    def test_invalid_outcome_rejected(self):
        self.assertFalse(exp.record_episode("p1", "x", "d", "nonsense"))
        self.assertEqual(self.col.count(), 0)

    def test_empty_summary_rejected(self):
        self.assertFalse(exp.record_episode("p1", "   ", "d", "success"))
        self.assertEqual(self.col.count(), 0)

    def test_valid_until_ttl_written(self):
        exp.record_episode("p1", "x", "d", "success")
        meta = self.col.store[list(self.col.store)[0]]["meta"]
        self.assertIn("valid_until", meta)
        self.assertIn("ts", meta)


class TestCapacity(ExperienceTestBase):
    def test_capacity_enforced(self):
        exp.EXPERIENCE_MAX = 3
        for i in range(10):
            exp.record_episode("p1", "item-%d" % i, "d", "success")
        self.assertLessEqual(exp.count_experiences("p1"), 3)
        self.assertGreaterEqual(exp.count_experiences("p1"), 1)

    def test_count_without_project_filter(self):
        exp.record_episode("p1", "a", "d", "success")
        exp.record_episode("p2", "b", "d", "success")
        self.assertEqual(exp.count_experiences(), 2)


class TestStaleness(ExperienceTestBase):
    def test_is_stale_past(self):
        self.assertTrue(exp.is_stale({"valid_until": "2000-01-01T00:00:00"}))

    def test_is_stale_future(self):
        self.assertFalse(exp.is_stale({"valid_until": "2999-01-01T00:00:00"}))

    def test_is_stale_no_field(self):
        self.assertFalse(exp.is_stale({}))

    def test_recall_annotates_staleness_and_confidence(self):
        # 一条新鲜、一条陈旧
        exp.record_episode("p1", "新鲜经验", "d", "success")
        vid = list(self.col.store)[0]
        self.col.store[vid]["meta"]["valid_until"] = "2000-01-01T00:00:00"
        hits = exp.recall_similar("p1", "经验")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]["stale"])
        self.assertLess(hits[0]["confidence"], 1.0)


class TestExtractLesson(ExperienceTestBase):
    def test_extract_lesson_success_first_line(self):
        lesson = exp._extract_lesson(FakeLLM(text="教训：先回滚。\n多余说明"),
                                     "改动", "决策", "fail-then-fixed")
        self.assertEqual(lesson, "教训：先回滚。")

    def test_extract_lesson_failure_returns_empty(self):
        lesson = exp._extract_lesson(FakeLLM(raise_on_call=True),
                                     "改动", "决策", "fail-then-fixed")
        self.assertEqual(lesson, "")

    def test_record_with_extract_lesson(self):
        ok = exp.record_episode("p1", "改动", "决策", "fail-then-fixed",
                                llm=FakeLLM(text="教训：先回滚。"), extract_lesson=True)
        self.assertTrue(ok)
        meta = self.col.store[list(self.col.store)[0]]["meta"]
        self.assertEqual(meta["lesson"], "教训：先回滚。")


class TestDegrade(ExperienceTestBase):
    def test_recall_no_embed_client_returns_empty(self):
        exp._embed_client = lambda: None
        # 先写入一条（用可用 embed 写入），再把 embed 置空再 recall
        exp._embed_client = lambda: FakeEmbed()
        exp.record_episode("p1", "x", "d", "success")
        exp._embed_client = lambda: None
        self.assertEqual(exp.recall_similar("p1", "x"), [])

    def test_record_collection_raises_degrades(self):
        class BoomCol(FakeCol):
            def upsert(self, **kw):
                raise RuntimeError("chroma down")
        exp._experience_collection = lambda: BoomCol()
        self.assertFalse(exp.record_episode("p1", "x", "d", "success"))


# ---------------------------------------------------------------------------
# Agent 侧：结局推导 + 回合收尾钩子（不实例化完整 Agent）
# ---------------------------------------------------------------------------
class FakeTurn:
    def __init__(self, **kw):
        self.failure_count = 0
        self.max_tool_streak = 0
        self.max_consec_failures = 0
        self.verified = False
        self.outcome = None
        self.actions = []
        self.last_failure = None
        self.error = None
        self.experience = None
        for k, v in kw.items():
            setattr(self, k, v)


class TestDeriveOutcome(unittest.TestCase):
    def test_no_failure_none(self):
        self.assertIsNone(agent_mod._derive_experience_outcome(FakeTurn()))

    def test_fail_then_fixed(self):
        t = FakeTurn(failure_count=2, max_tool_streak=2, verified=True,
                     outcome="completed")
        self.assertEqual(agent_mod._derive_experience_outcome(t), "fail-then-fixed")

    def test_repeated_fail_by_tool_streak(self):
        t = FakeTurn(failure_count=3, max_tool_streak=3, outcome="max_steps")
        self.assertEqual(agent_mod._derive_experience_outcome(t), "repeated-fail")

    def test_repeated_fail_by_consec(self):
        t = FakeTurn(failure_count=6, max_consec_failures=6, outcome="max_steps")
        self.assertEqual(agent_mod._derive_experience_outcome(t), "repeated-fail")

    def test_ambiguous_not_recorded(self):
        # 有失败但未成功收尾、也未触达上限 → None（不确定项不记）
        t = FakeTurn(failure_count=1, max_tool_streak=1, outcome="max_steps")
        self.assertIsNone(agent_mod._derive_experience_outcome(t))

    def test_error_exit_none(self):
        t = FakeTurn(failure_count=2, max_tool_streak=2, error="boom")
        self.assertIsNone(agent_mod._derive_experience_outcome(t))


class TestMaybeRecordHook(ExperienceTestBase):
    def _call_hook(self, turn, question="测试问题"):
        fake = type("FakeAgent", (), {"project_id": "prjX", "llm": None})()
        bound = MethodType(agent_mod.Agent._maybe_record_experience, fake)
        bound(turn, question)
        return turn

    def test_records_on_fail_then_fixed(self):
        turn = FakeTurn(failure_count=2, max_tool_streak=2, verified=True,
                        outcome="completed", actions=["apply_edit", "self_verify"])
        self._call_hook(turn)
        self.assertTrue(turn.experience["recorded"])
        self.assertEqual(turn.experience["outcome"], "fail-then-fixed")
        self.assertEqual(self.col.count(), 1)
        meta = self.col.store[list(self.col.store)[0]]["meta"]
        self.assertEqual(meta["project_id"], "prjX")
        self.assertIn("测试问题", meta["action_summary"])

    def test_no_record_when_no_failure(self):
        turn = FakeTurn(failure_count=0, outcome="completed")
        self._call_hook(turn)
        self.assertIsNone(turn.experience)
        self.assertEqual(self.col.count(), 0)


# ---------------------------------------------------------------------------
# 工具函数：recall_experience 注册与 JSON 形态
# ---------------------------------------------------------------------------
class TestRecallExperienceTool(unittest.TestCase):
    def setUp(self):
        self._orig_col = exp._experience_collection
        self._orig_emb = exp._embed_client
        self.col = FakeCol()
        exp._experience_collection = lambda: self.col
        exp._embed_client = lambda: FakeEmbed()
        # recall_experience 通过 projects.current_project_id() 取项目，打桩固定为 default
        import projects as projects_mod
        self._projects = projects_mod
        self._orig_pid = projects_mod.current_project_id
        projects_mod.current_project_id = lambda: "default"
        import tools as tools_mod
        self._tools = tools_mod

    def tearDown(self):
        exp._experience_collection = self._orig_col
        exp._embed_client = self._orig_emb
        self._projects.current_project_id = self._orig_pid

    def test_tool_returns_valid_json_and_registered(self):
        self.assertIn("recall_experience", self._tools.TOOLS)
        out = self._tools.recall_experience("改 Vue 组件后 typecheck 报错")
        data = json.loads(out)
        self.assertEqual(data["count"], 0)
        self.assertIsInstance(data["items"], list)

    def test_tool_returns_experiences(self):
        exp.record_episode("default", "改 Vue 组件后 typecheck 报错", "d", "success")
        out = self._tools.recall_experience("改 Vue 组件后 typecheck 报错")
        data = json.loads(out)
        self.assertEqual(data["count"], 1)
        self.assertIn("confidence", data["items"][0])
        self.assertIn("stale", data["items"][0])


# ---------------------------------------------------------------------------
# Phase 4：golden_gate_ok 冻结发布门
# ---------------------------------------------------------------------------
class TestGoldenGate(unittest.TestCase):
    def _write(self, rows):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return f.name

    def _passing(self, ids):
        return [
            {"id": i, "final": "答案包含 passed 与 ran", "actions": ["search_code"],
             "expect": {"any_of": ["passed"]}, "error": None} for i in ids
        ]

    def test_all_pass_no_baseline(self):
        p = self._write(self._passing(["G1", "G2"]))
        self.assertTrue(agent_mod_eval_golden_gate_ok(p))
        os.unlink(p)

    def test_failure_blocks(self):
        rows = self._passing(["G1"])
        rows.append({"id": "G2", "final": "无法回答", "actions": [],
                     "expect": {"any_of": ["passed"]}, "error": None})
        p = self._write(rows)
        self.assertFalse(agent_mod_eval_golden_gate_ok(p))
        os.unlink(p)

    def test_no_scored_blocks(self):
        p = self._write([{"id": "G1", "final": "x", "actions": [], "expect": {}}])
        self.assertFalse(agent_mod_eval_golden_gate_ok(p))
        os.unlink(p)

    def test_baseline_regression_blocks(self):
        # baseline：G1/G2 通过；current：G2 回退为失败 → 应判 regression（不放行）
        base = self._write(self._passing(["G1", "G2"]))
        cur_rows = self._passing(["G1"])
        cur_rows.append({"id": "G2", "final": "无法回答", "actions": [],
                         "expect": {"any_of": ["passed"]}, "error": None})
        cur = self._write(cur_rows)
        self.assertFalse(agent_mod_eval_golden_gate_ok(cur, base))
        os.unlink(base); os.unlink(cur)

    def test_baseline_no_regression_ok(self):
        base = self._write(self._passing(["G1", "G2"]))
        cur = self._write(self._passing(["G1", "G2"]))
        self.assertTrue(agent_mod_eval_golden_gate_ok(cur, base))
        os.unlink(base); os.unlink(cur)


def agent_mod_eval_golden_gate_ok(*a, **k):
    import agent_eval
    return agent_eval.golden_gate_ok(*a, **k)


if __name__ == "__main__":
    unittest.main(verbosity=2)
