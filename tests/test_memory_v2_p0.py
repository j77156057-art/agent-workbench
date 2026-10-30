"""记忆系统 v2 · P0 回归：开关门、宪法注入顺序与上限、保存/哈希审计、
索引重建幂等、路径越界、turn trace 快照。
"""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import agent as agent_mod
import agent_memory as legacy_memory
import agent_trace
import config
import tools as agent_tools
from agent_runtime.memory import (
    constitution,
    context_panel,
    episodes,
    errors,
    feedback,
    hooks,
    index,
    injection,
    metrics,
    migrate,
    promote,
    receipts,
    profile,
    store,
    taskbook,
    turns,
)
from agent_trace import Turn


class _LLM:
    provider = "stub"
    model = "stub"

    def count_tokens(self, text):
        return max(1, len(str(text or "")) // 4)

    def chat(self, messages, stream=True, enable_thinking=None, **kw):
        return iter(["Final Answer: 好"])


class _MemBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_memv2_")
        self._env = {key: os.environ.get(key)
                     for key in ("DOCMIND_MEMORY_DIR", "DOCMIND_MEMORY_V2")}
        os.environ["DOCMIND_MEMORY_DIR"] = os.path.join(self.tmp, "memory")
        os.environ["DOCMIND_MEMORY_V2"] = "1"
        self._runtime = dict(config._RUNTIME)
        config._RUNTIME.pop("memory_v2", None)
        self._old_mem_db = legacy_memory.DB_PATH
        legacy_memory.DB_PATH = os.path.join(self.tmp, "legacy_memory.sqlite3")

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._RUNTIME.clear()
        config._RUNTIME.update(self._runtime)
        legacy_memory.DB_PATH = self._old_mem_db

    def build(self, question="你好"):
        return agent_mod.Agent(llm=_LLM(), session_id="memv2")._build_messages(question)

    def ledger_events(self):
        path = os.path.join(store.root(), "ledger.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as handle:
            import json
            return [json.loads(line)["event"] for line in handle if line.strip()]


class FlagTests(_MemBase):
    def test_v2_flag_runtime_over_env(self):
        os.environ["DOCMIND_MEMORY_V2"] = "0"
        self.assertFalse(config.get_memory_v2())
        config.set_runtime("memory_v2", True)
        self.assertTrue(config.get_memory_v2())

    def test_off_adds_nothing_and_writes_no_file(self):
        os.environ.pop("DOCMIND_MEMORY_V2", None)
        self.assertEqual(injection.build_injection(), [])
        messages = self.build()
        self.assertNotIn("系统宪法", str(messages[1]["content"]))
        self.assertFalse(os.path.exists(os.path.join(store.root(), "constitution.md")))


class ConstitutionTests(_MemBase):
    def test_first_injection_after_base_prompt(self):
        messages = self.build()
        self.assertTrue(str(messages[1]["content"]).startswith("【系统宪法 v0"))
        self.assertIn("当前时间", str(messages[2]["content"]))
        self.assertLessEqual(len(messages[1]["content"]), constitution.INJECT_MAX_CHARS)

    def test_bootstrap_once_then_save_bumps_version(self):
        first = constitution.load(ensure=True)
        second = constitution.load(ensure=True)
        self.assertEqual(first["version"], 0)
        self.assertEqual(second["version"], 0)
        self.assertEqual(self.ledger_events().count("constitution.bootstrap"), 1)
        saved = constitution.save(body="新的正文", base_version=0)
        self.assertEqual(saved["version"], 1)
        self.assertTrue(saved["hash_ok"])
        with self.assertRaises(constitution.VersionConflict):
            constitution.save(body="过期写入", base_version=0)

    def test_hand_edit_is_audited_and_still_injected(self):
        constitution.load(ensure=True)
        target = os.path.join(store.root(), "constitution.md")
        text = store.read_text(target).replace("你是 DocMind", "你是 DocMind（手改）")
        store.atomic_write(target, text)
        doc = constitution.load(ensure=True)
        self.assertFalse(doc["hash_ok"])
        self.assertIn("系统宪法", doc["text"])
        self.assertIn("constitution.hash_mismatch", self.ledger_events())

    def test_injection_is_capped(self):
        constitution.load(ensure=True)
        doc = constitution.save(body="长" * 2000)
        self.assertLessEqual(len(doc["text"]), constitution.INJECT_MAX_CHARS)
        self.assertLessEqual(len(injection.build_injection()[0]["text"]),
                             constitution.INJECT_MAX_CHARS)

    def test_broken_yaml_fails_closed(self):
        constitution.load(ensure=True)
        target = os.path.join(store.root(), "constitution.md")
        store.atomic_write(target, "---\nversion: [坏掉的\n---\n正文\n")
        doc = constitution.load(ensure=True)
        self.assertTrue(doc["error"])
        self.assertEqual(doc["text"], "")
        self.assertEqual(injection.build_injection(), [])
        self.assertIn("constitution.invalid", self.ledger_events())


class StoreTests(_MemBase):
    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            store.safe_path("..", "escape.md")
        with self.assertRaises(ValueError):
            store.safe_path("/abs/path.md")

    def test_turn_record_carries_injection_only_when_present(self):
        empty = Turn(session_id="s").to_record()
        self.assertNotIn("injection", empty)
        turn = Turn(session_id="s")
        turn.add_injection([{"layer": "L0", "chars": 12, "hash": "abc",
                             "version": 3, "text": "宪法正文"}])
        record = turn.to_record()
        self.assertEqual(record["injection"][0]["layer"], "L0")
        self.assertEqual(record["injection"][0]["version"], 3)


class _RecordTurn:
    """record_turn 用到的 Turn 结构化字段子集。"""

    def __init__(self):
        self.steps = [
            {"action": "read_file", "ok": True, "arg_chars": 10, "obs_chars": 20, "latency_ms": 5},
            {"action": "run_command", "ok": False, "arg_chars": 8, "obs_chars": 30, "latency_ms": 7},
        ]
        self.failure_count = 1
        self.last_failure = "命令退出码 1：pytest 失败"
        self.prompt_tokens = 120
        self.completion_tokens = 30


class EpisodeTests(_MemBase):
    def test_append_and_latest_tail(self):
        for i in range(5):
            episodes.append("prj", "s1", {"goal": f"目标 {i}", "result": {"status": "completed"}})
        rows = episodes.latest("prj", "s1", 2)
        self.assertEqual([row["goal"] for row in rows], ["目标 4", "目标 3"])
        self.assertEqual(len(episodes.latest("prj", "s2", 2)), 0)

    def test_episode_has_no_verbatim_body(self):
        episodes.append("prj", "s1", {"goal": "q", "result": {"summary": "短结论"}})
        row = episodes.latest("prj", "s1", 1)[0]
        self.assertNotIn("trail", row)
        self.assertEqual(row["result"]["summary"], "短结论")


class HooksTests(_MemBase):
    def test_dedupe_and_pin_render(self):
        first = hooks.add("prj", "s1", "user_agreement", "以后都用中文", verified=True)
        second = hooks.add("prj", "s1", "user_agreement", "以后都用中文", verified=True)
        self.assertEqual(first["id"], second["id"])
        hooks.add("prj", "s1", "fact", "入口文件是 main.py", verified=False)
        text = hooks.render("prj", "s1")
        self.assertIn("约定", text)
        self.assertIn("main.py", text)
        self.assertIn("未验证", text)
        self.assertLessEqual(len(injection.build_session_injection("s1", "prj")[0]["text"]), 600)

    def test_cap_keeps_newest(self):
        for i in range(90):
            hooks.add("prj", "s1", "fact", f"事实 {i}")
        rows = hooks.items("prj", "s1")
        self.assertLessEqual(len(rows), hooks.MAX_ITEMS)
        self.assertTrue(any(row["text"] == "事实 89" for row in rows))

    def test_remove(self):
        row = hooks.add("prj", "s1", "fact", "可删除")
        self.assertTrue(hooks.remove("prj", "s1", row["id"]))
        self.assertFalse([r for r in hooks.items("prj", "s1") if r["id"] == row["id"]])


class ErrorsTests(_MemBase):
    def test_signature_dedupes_and_counts(self):
        sig = errors.signature("run_command", "exit 1\n  pytest failed")
        errors.record("prj", "s1", sig, tool="run_command", cause="exit 1 pytest failed")
        errors.record("prj", "s1", sig, tool="run_command", cause="exit 1 pytest failed")
        rows = errors.top("prj", "s1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurrences"], 2)
        self.assertIn("易错", errors.render("prj", "s1"))

    def test_mark_verified(self):
        sig = errors.signature("t", "boom")
        errors.record("prj", "s1", sig, tool="t", cause="boom")
        self.assertTrue(errors.mark_verified("prj", "s1", sig, fix="换用 --basetemp")["verified"])


class ReceiptTests(_MemBase):
    def test_record_get_list(self):
        rid = receipts.record("prj", session="s1", type="verify", result="pass",
                              cmd="self_verify", evidence='{"a.py": true}')
        row = receipts.get("prj", rid)
        self.assertEqual((row["type"], row["result"]), ("verify", "pass"))
        self.assertEqual(receipts.list_receipts("prj")[0]["id"], rid)

    def test_invalid_type_rejected(self):
        with self.assertRaises(ValueError):
            receipts.record("prj", type="made-up", result="pass")


class TurnRecordTests(_MemBase):
    def test_record_turn_writes_episode_hooks_errors_receipts(self):
        journal = {"file_changes": [{"path": "a.py"}],
                   "verification": {"a.py": True}, "verified": True,
                   "last_action": "run_command", "last_obs": "pytest 失败"}
        result = turns.record_turn(
            session_id="s1", project_id="prj",
            question="记住：以后都先跑测试", final_text="完成", status="completed",
            turn=_RecordTurn(), journal=journal, next_step="补测试")
        self.assertTrue(result["episode"].startswith("ep-"))
        self.assertEqual(len(result["receipts"]), 1)
        self.assertEqual(result["errors"], 1)
        row = episodes.latest("prj", "s1", 1)[0]
        self.assertEqual(row["files"], ["a.py"])
        self.assertEqual(row["next"], "补测试")
        self.assertEqual(row["receipts"], result["receipts"])
        self.assertEqual(receipts.get("prj", result["receipts"][0])["result"], "pass")
        kinds = {item["kind"] for item in hooks.items("prj", "s1")}
        self.assertIn("user_agreement", kinds)
        self.assertIn("error", kinds)
        self.assertEqual(errors.top("prj", "s1")[0]["occurrences"], 1)

    def test_flag_off_writes_nothing(self):
        os.environ.pop("DOCMIND_MEMORY_V2", None)
        result = turns.record_turn(
            session_id="s1", project_id="prj", question="记住这个",
            final_text="完成", status="completed", turn=_RecordTurn(), journal={})
        self.assertEqual(result, {})
        self.assertEqual(episodes.latest("prj", "s1", 1), [])
        self.assertEqual(hooks.items("prj", "s1"), [])

    def test_failed_verification_writes_fail_receipt(self):
        journal = {"verification": {"a.py": False}, "verified": False,
                   "last_action": "write_file", "last_obs": "校验失败"}
        result = turns.record_turn(
            session_id="s1", project_id="prj", question="改一下", final_text="完成",
            status="completed", turn=_RecordTurn(), journal=journal, next_step="")
        self.assertEqual(receipts.get("prj", result["receipts"][0])["result"], "fail")


class SessionInjectionTests(_MemBase):
    def test_l4_block_reaches_prompt_and_trace(self):
        hooks.add("prj", "s1", "user_agreement", "以后都用中文", verified=True)
        episodes.append("prj", "s1", {"goal": "q", "next": "补测试",
                                      "result": {"status": "completed"}})
        agent = agent_mod.Agent(llm=_LLM(), session_id="s1", project_id="prj")
        messages = agent._build_messages("继续")
        text = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertIn("【会话钩子", text)
        self.assertIn("以后都用中文", text)
        self.assertIn("下一步", text)
        self.assertEqual([b["layer"] for b in agent._last_injection], ["L0", "L4"])
        turn = Turn(session_id="s1")
        turn.add_injection(agent._last_injection)
        self.assertEqual(turn.to_record()["injection"][1]["layer"], "L4")

    def test_l4_absent_when_off(self):
        os.environ.pop("DOCMIND_MEMORY_V2", None)
        hooks.add("prj", "s1", "user_agreement", "以后都用中文", verified=True)
        messages = agent_mod.Agent(
            llm=_LLM(), session_id="s1", project_id="prj")._build_messages("继续")
        self.assertNotIn("【会话钩子", "\n".join(str(m.get("content") or "") for m in messages))


class TaskbookTests(_MemBase):
    def _plan(self, *items):
        return {"title": "长任务", "items": list(items)}

    def test_small_plan_stays_a_plan(self):
        from unittest.mock import patch
        with patch("agent_runtime.plan_state.read",
                   return_value=self._plan({"key": "a", "text": "甲", "done": False})):
            self.assertIsNone(taskbook.sync_from_plan("prj", "root"))
        self.assertFalse(os.path.exists(taskbook.book_path("prj")))

    def test_plan_creates_book_and_mirrors_states(self):
        from unittest.mock import patch
        plan = self._plan({"key": "a", "text": "甲", "done": False},
                          {"key": "b", "text": "乙", "done": False},
                          {"key": "c", "text": "丙", "done": True})
        with patch("agent_runtime.plan_state.read", return_value=plan):
            book = taskbook.sync_from_plan("prj", "root")
        states = {item["key"]: item["state"] for item in book["items"]}
        self.assertEqual(states, {"a": "todo", "b": "todo", "c": "done_pending_merge"})
        self.assertIn("参考", taskbook.summary("prj"))

    def test_done_requires_pass_receipt_and_merge(self):
        taskbook.open_book("prj", "任务", [{"text": "条目一"}])
        key = taskbook.items("prj")[0]["key"]
        with self.assertRaises(ValueError):
            taskbook.complete("prj", key, receipt_ids=())
        rid = receipts.record("prj", type="verify", result="pass")
        item = taskbook.complete("prj", key, receipt_ids=[rid])
        self.assertEqual(item["state"], "done_pending_merge")
        item = taskbook.complete("prj", key, receipt_ids=[rid], merge="abc1234")
        self.assertEqual(item["state"], "done")

    def test_user_override_is_recorded(self):
        taskbook.open_book("prj", "任务", [{"text": "条目一"}])
        key = taskbook.items("prj")[0]["key"]
        item = taskbook.complete("prj", key, user_override=True)
        self.assertEqual(item["state"], "done")
        self.assertTrue(item["user_override"])

    def test_claim_conflict_and_expiry_shape(self):
        taskbook.open_book("prj", "任务", [{"text": "条目一"}])
        key = taskbook.items("prj")[0]["key"]
        self.assertTrue(taskbook.claim("prj", key, "w1")["ok"])
        conflict = taskbook.claim("prj", key, "w2")
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["claimed_by"], "w1")

    def test_fallback_draft_only_after_fifteen_steps(self):
        self.assertIsNone(taskbook.maybe_draft("prj", "问题", "下一步", 14))
        book = taskbook.maybe_draft("prj", "问题", "下一步", 15)
        self.assertEqual(len(book["items"]), 1)
        self.assertEqual(book["items"][0]["state"], "doing")

    def test_injection_and_prompt(self):
        taskbook.open_book("prj", "任务", [{"text": "补测试"}, {"text": "跑验收"}])
        block = injection.build_taskbook_injection("prj")[0]
        self.assertEqual(block["layer"], "L3")
        self.assertLessEqual(len(block["text"]), 400)
        messages = agent_mod.Agent(
            llm=_LLM(), session_id="s1", project_id="prj")._build_messages("继续")
        text = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertIn("【任务书", text)


class FeedbackTests(_MemBase):
    def test_record_latest_and_task_filter(self):
        feedback.record("prj", session="s1", stars=5, task="tb-1")
        feedback.record("prj", session="s1", stars=2, task="tb-1", note="慢")
        feedback.record("prj", session="s1", stars=4, task="tb-2")
        self.assertEqual(feedback.latest("prj", "s1")["stars"], 4)
        rows = feedback.list_for_task("prj", "tb-1", "s1")
        self.assertEqual([row["stars"] for row in rows], [5, 2])

    def test_stars_must_be_one_to_five(self):
        for bad in (0, 6, "x", None):
            with self.assertRaises(ValueError):
                feedback.record("prj", stars=bad)


class FinalEventTests(_MemBase):
    def test_deliverable_flag_only_when_v2_on(self):
        agent = agent_mod.Agent(llm=_LLM(), session_id="s1", project_id="prj")
        agent._pending_file_changes = [{"path": "a.py"}]
        event = agent._final_event("完成", None)
        self.assertTrue(event["deliverable"])
        self.assertEqual(event["task"], "")
        os.environ.pop("DOCMIND_MEMORY_V2", None)
        plain = agent._final_event("完成", None)
        self.assertNotIn("deliverable", plain)
        self.assertNotIn("task", plain)

    def test_verification_counts_as_deliverable(self):
        agent = agent_mod.Agent(llm=_LLM(), session_id="s1", project_id="prj")
        agent._pending_file_changes = []

        class _T:
            def __init__(self):
                self.verification_targets = {"a.py": True}

        self.assertTrue(agent._final_event("完成", _T())["deliverable"])


class _MemoryLLM:
    provider = "stub"
    model = "stub"

    def __init__(self, output):
        self.output = output
        self.last_usage = {"prompt_tokens": 11, "completion_tokens": 5}

    def clone(self):
        return _MemoryLLM(self.output)

    def chat(self, messages, stream=False, temperature=0.0, **kw):
        return self.output


class PromoteTests(_MemBase):
    TEXT = "合并前先跑完整测试，能显著降低回归率"

    def _candidate(self, text=None, importance=0.8, task="tb-1"):
        rid = receipts.record("prj", type="verify", result="pass")
        candidate = promote.create_candidate(
            "prj", "fact", text or self.TEXT, importance=importance,
            receipt_ids=[rid], task=task)
        return candidate, rid

    def test_requires_adoption_before_promotion(self):
        self._candidate()
        self.assertEqual(promote.sweep("prj")["promoted"], 0)
        self.assertEqual(promote.shared_items(), [])
        feedback.record("prj", session="s1", stars=5, task="tb-1")
        self.assertEqual(promote.sweep("prj")["promoted"], 1)
        shared = promote.shared_items()
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["scope"], "global")
        self.assertTrue(shared[0]["pinned"])
        block = injection.build_shared_injection()[0]
        self.assertEqual(block["layer"], "L2")
        self.assertIn("先跑完整测试", block["text"])

    def test_secret_and_context_rejected(self):
        rid = receipts.record("prj", type="verify", result="pass")
        promote.create_candidate("prj", "fact", "api_key=sk-abcdefghijklmnop 用于鉴权",
                                 importance=0.9, receipt_ids=[rid], task="t1")
        promote.create_candidate("prj", "fact", "本项目的入口在注册表里，先读再改",
                                 importance=0.9, receipt_ids=[rid], task="t1")
        feedback.record("prj", session="s1", stars=5, task="t1")
        self.assertEqual(promote.sweep("prj")["promoted"], 0)
        self.assertEqual(promote.shared_items(), [])

    def test_absolute_path_is_redacted_before_promotion(self):
        rid = receipts.record("prj", type="verify", result="pass")
        promote.create_candidate(
            "prj", "fact", "配置文件在 D:\\work\\proj\\conf.ini，改完要重启验证",
            importance=0.9, receipt_ids=[rid], task="t2")
        feedback.record("prj", session="s1", stars=5, task="t2")
        self.assertEqual(promote.sweep("prj")["promoted"], 1)
        body = promote.shared_items()[0]["body"]
        self.assertIn("<path>", body)
        self.assertNotIn("D:\\work", body)

    def test_low_importance_and_no_receipt_stay_candidates(self):
        promote.create_candidate("prj", "fact", "低价值的一次性观察，先记下来", importance=0.2)
        candidate, _ = self._candidate(importance=0.9)
        self.assertEqual(promote.sweep("prj")["promoted"], 0)
        self.assertEqual(len(promote.candidates("prj")), 2)
        self.assertEqual(candidate["status"], "candidate")

    def test_promotion_is_idempotent(self):
        self._candidate()
        feedback.record("prj", session="s1", stars=5, task="tb-1")
        self.assertEqual(promote.sweep("prj")["promoted"], 1)
        self.assertEqual(promote.sweep("prj")["promoted"], 0)
        self.assertEqual(len(promote.shared_items()), 1)

    def test_project_candidate_indexed(self):
        self._candidate()
        out = index.rebuild(verify=True)
        self.assertEqual((out["items"], out["verified"]), (1, True))


def _consolidate_episode(*, status="completed", actions=1,
                         summary="完成了一次可复用的改动，记录了合并前先跑测试的结论。",
                         receipts_list=(), task=""):
    return {"id": "ep-test", "session": "s1", "task": task,
            "actions": [{"tool": "x"}] * actions,
            "result": {"status": status, "summary": summary, "error": ""},
            "files": [], "errors": [], "next": "", "receipts": list(receipts_list)}


class ConsolidateTests(_MemBase):
    _episode = staticmethod(_consolidate_episode)

    def _out(self, keep=True, text="合并前先跑完整测试能降低回归率", kind="fact", importance=0.7):
        return json.dumps({"keep": keep, "kind": kind, "title": "t",
                           "text": text, "importance": importance}, ensure_ascii=False)

    def test_keep_true_creates_candidate_and_meters(self):
        result = consolidate_run(self, self._out())
        self.assertTrue(result["kept"])
        self.assertEqual(len(promote.candidates("prj")), 1)
        report = metrics.report()
        self.assertEqual(report["consolidate_calls"], 1)
        self.assertEqual(report["errors"], 0)

    def test_keep_false_and_trivial_are_skipped(self):
        result = consolidate_run(self, self._out(keep=False))
        self.assertFalse(result["kept"])
        self.assertEqual(promote.candidates("prj"), [])
        result = consolidate_run(self, self._out(), episode=self._episode(actions=0, summary="好"))
        self.assertFalse(result["ok"])
        self.assertEqual(metrics.report()["skipped"], 2)

    def test_receipt_and_rating_promote_in_same_run(self):
        rid = receipts.record("prj", type="verify", result="pass")
        feedback.record("prj", session="s1", stars=5, task="tb-1")
        result = consolidate_run(self, self._out(importance=0.8),
                                 episode=self._episode(receipts_list=[rid], task="tb-1"))
        self.assertEqual(result["promoted"], 1)
        self.assertEqual(len(promote.shared_items()), 1)


def consolidate_run(case, output, episode=None):
    from agent_runtime.memory import consolidate
    return consolidate.run_once("prj", "s1", _MemoryLLM(output),
                                episode or _consolidate_episode())


class MigrateTests(_MemBase):
    def _legacy_db(self):
        path = os.path.join(self.tmp, "legacy.sqlite3")
        connection = sqlite3.connect(path)
        connection.executescript("""
        CREATE TABLE memories (
          scope TEXT, id TEXT, kind TEXT, title TEXT, content TEXT, evidence TEXT,
          source TEXT, repeats INTEGER, updated TEXT, status TEXT, backed INTEGER,
          importance REAL);
        CREATE TABLE checkpoints (
          scope TEXT, id TEXT, session TEXT, question TEXT, status TEXT, reason TEXT,
          events TEXT, answer TEXT, updated TEXT, journal TEXT);
        """)
        rows = [
            ("prj", "f1", "fact", "入口", "入口文件是 main.py，启动前先跑测试", "", "agent", 1,
             "2026-10-01", "active", 0, 0.7),
            ("prj", "d1", "episode", "蒸馏", "模型报告（已蒸馏）：合并前必须跑完整测试，否则回归率高",
             "", "model_report", 1, "2026-10-02", "active", 1, 0.8),
            ("prj", "e1", "episode", "转写", "模型报告（尚需核对验收）：逐字转写内容", "", "model_report",
             1, "2026-10-03", "active", 0, 0.5),
            ("prj", "p1", "preference", "语言", "使用中文沟通", "使用中文沟通", "user_explicit",
             1, "2026-10-04", "active", 0, 0.5),
            ("prj", "r1", "episode", "退休", "模型报告（尚需核对验收）：旧结论", "", "model_report",
             1, "2026-10-05", "retired", 0, 0.5),
        ]
        connection.executemany(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        connection.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("prj", "c1", "s1", "问题", "completed", "", "[]", "答案", "2026-10-06", "{}"))
        connection.commit()
        connection.close()
        return path

    def test_migration_exports_and_is_idempotent(self):
        db = self._legacy_db()
        with patch.object(migrate, "_migrate_experience", lambda result: None):
            first = migrate.run(db_path=db)
            self.assertEqual(first["archived"], 5)
            self.assertEqual(first["seeded_facts"], 2)
            self.assertEqual(first["preferences"], 1)
            self.assertEqual(first["checkpoints"], 1)
            self.assertEqual(first["retired"], 1)
            self.assertEqual(len(promote.shared_items()), 0, "迁移只进候选，不 pin")
            archive = os.path.join(store.root(), "archive", "legacy", "episodes.jsonl")
            self.assertTrue(os.path.exists(archive))
            second = migrate.run(db_path=db)
            self.assertTrue(second.get("skipped"))
            forced = migrate.run(db_path=db, force=True)
            self.assertEqual(forced["seeded_facts"], 2)
            shared_files = []
            for kind in ("facts", "lessons"):
                directory = os.path.join(store.root(), "shared", kind)
                if os.path.isdir(directory):
                    shared_files.extend(os.listdir(directory))
            self.assertEqual(len(shared_files), 2, "重跑不得重复")


class SharedInjectionTests(_MemBase):
    def test_pinned_shared_reaches_prompt(self):
        rid = receipts.record("prj", type="verify", result="pass")
        promote.create_candidate("prj", "fact", "合并前先跑完整测试，能显著降低回归率",
                                 importance=0.9, receipt_ids=[rid], task="tb-1")
        feedback.record("prj", session="s1", stars=5, task="tb-1")
        promote.sweep("prj")
        agent = agent_mod.Agent(llm=_LLM(), session_id="s1", project_id="prj")
        messages = agent._build_messages("继续")
        text = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertIn("【共享记忆", text)
        self.assertIn("先跑完整测试", text)
        self.assertEqual([block["layer"] for block in agent._last_injection], ["L0", "L2"])


def _pinned_shared(text="合并前先跑完整测试，能显著降低回归率", importance=0.9):
    rid = receipts.record("prj", type="verify", result="pass")
    promote.create_candidate("prj", "fact", text, importance=importance,
                             receipt_ids=[rid], task="tb-1")
    feedback.record("prj", session="s1", stars=5, task="tb-1")
    promote.sweep("prj")


class ContextPanelTests(_MemBase):
    def test_next_view_has_all_layers(self):
        view = context_panel.next_view("prj", "s1")
        keys = [layer["key"] for layer in view["layers"]]
        self.assertEqual(keys, ["L0", "L1", "rules", "L2", "L3", "L4", "summary"])
        self.assertTrue(view["layers"][0]["editable"])
        self.assertIn("【系统宪法", view["layers"][0]["text"])
        self.assertEqual(view["layers"][0]["budget"], 600)

    def test_shared_and_taskbook_items_are_editable_data(self):
        _pinned_shared()
        taskbook.open_book("prj", "任务", [{"text": "补测试"}])
        view = context_panel.next_view("prj", "s1")
        l2 = next(layer for layer in view["layers"] if layer["key"] == "L2")
        self.assertEqual(len(l2["items"]), 1)
        self.assertIn("先跑完整测试", l2["text"])
        l3 = next(layer for layer in view["layers"] if layer["key"] == "L3")
        self.assertEqual(l3["items"][0]["text"], "补测试")

    def test_last_view_reads_turn_snapshot(self):
        target = os.path.join(self.tmp, "traces.jsonl")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "session_id": "s1", "turn_id": "t1", "ts": "2026-10-31",
                "injection": [{"layer": "L0", "chars": 3, "hash": "abc",
                               "version": 1, "text": "宪法快照"}],
            }, ensure_ascii=False) + "\n")
        with patch.object(agent_trace, "TRACE_FILE", target):
            view = context_panel.last_view("s1")
        self.assertEqual(view["mode"], "last")
        self.assertEqual(view["layers"][0]["text"], "宪法快照")
        self.assertFalse(view["layers"][0]["editable"])

    def test_constitution_save_conflict_and_edit_evidence_reset(self):
        first = context_panel.next_view("prj", "s1")["layers"][0]
        saved = context_panel.save_constitution(first["raw"], base_version=0)
        self.assertEqual(saved["version"], 1)
        with self.assertRaises(constitution.VersionConflict):
            context_panel.save_constitution(first["raw"], base_version=0)

        _pinned_shared()
        item = promote.pinned_shared(1)[0]
        edited = context_panel.edit_shared(item["id"], text="用户改过的共享结论，仍可读")
        self.assertEqual(edited["source"], "user_edit")
        self.assertEqual(edited["receipts"], [])
        self.assertEqual(edited["confidence"], 0.5)
        with self.assertRaises(ValueError):
            context_panel.edit_shared(item["id"], text="api_key=sk-abcdefghijklmnop")

    def test_hook_and_taskbook_edits(self):
        row = hooks.add("prj", "s1", "fact", "入口在 main.py")
        edited = context_panel.edit_hook("prj", "s1", row["id"], text="入口在 app.py")
        self.assertEqual(edited["text"], "入口在 app.py")
        self.assertTrue(context_panel.delete_hook("prj", "s1", row["id"]))

        taskbook.open_book("prj", "任务", [{"text": "补测试"}])
        key = taskbook.items("prj")[0]["key"]
        self.assertEqual(context_panel.edit_task_item("prj", key, "补单元测试")["text"],
                         "补单元测试")
        with self.assertRaises(ValueError):
            context_panel.complete_task_item("prj", key)
        self.assertEqual(context_panel.complete_task_item("prj", key, override=True)["state"],
                         "done")

    def test_delete_shared(self):
        _pinned_shared()
        item = promote.pinned_shared(1)[0]
        self.assertTrue(context_panel.delete_shared(item["id"]))
        self.assertEqual(promote.shared_items(), [])


class ProfileFileTests(_MemBase):
    def test_bootstrap_save_and_sqlite_merge(self):
        legacy_memory.update_profile("local-user", {"display_name": "小李", "language": "中文"})
        data = profile.load()
        self.assertEqual(data["display_name"], "小李")
        self.assertTrue(os.path.exists(profile.path()))
        self.assertTrue(os.path.exists(profile.learned_path()))
        self.assertIn("小李", profile.context())

        profile.save({"notes": "喜欢简短回答"})
        self.assertEqual(legacy_memory.get_profile().get("notes"), "喜欢简短回答")
        legacy_memory.update_profile("local-user", {"location": "广州"})
        self.assertEqual(profile.load()["location"], "广州")

        view = context_panel.next_view("prj", "s1")
        l1 = next(layer for layer in view["layers"] if layer["key"] == "L1")
        self.assertEqual(l1["profile"]["display_name"], "小李")
        self.assertIn("小李", l1["text"])


class SearchEdgeTests(_MemBase):
    def _shared(self, text, *, importance=0.6, task="t"):
        rid = receipts.record("prj", type="verify", result="pass")
        promote.create_candidate("prj", "fact", text, importance=importance,
                                 receipt_ids=[rid], task=task)
        feedback.record("prj", session="s1", stars=5, task=task)
        promote.sweep("prj")
        return next(item for item in promote.shared_items() if item["body"] == text)

    def test_keyword_search_expands_one_hop(self):
        a = self._shared("回归失败时先看日志，再决定是否重跑", task="ta")
        b = self._shared("退出码非零先核对命令参数", task="tb")
        promote.link(a["id"], b["id"], "refines")
        index.rebuild(verify=True)
        rows = index.search("回归", limit=5)
        ids = [row["id"] for row in rows]
        self.assertIn(a["id"], ids)
        self.assertIn(b["id"], ids)
        related = next(row for row in rows if row["id"] == b["id"])
        self.assertEqual(related.get("related_to"), a["id"])

    def test_retrievable_list_and_promotion_reasons(self):
        a = self._shared("共享但未 pin 的结论：先复现再修复", task="ta")
        pending = promote.create_candidate("prj", "fact", "项目候选：还没过晋升门",
                                           importance=0.6)
        rows = promote.retrievable("prj", 30)
        self.assertTrue(any(row["id"] == a["id"] and row["scope"] == "global" for row in rows))
        self.assertTrue(any(row["id"] == pending["id"] and row["scope"] == "project" for row in rows))
        view = context_panel.next_view("prj", "s1")
        l2 = next(layer for layer in view["layers"] if layer["key"] == "L2")
        self.assertTrue(any(item["id"] == a["id"] for item in l2["retrievable_items"]))
        result = promote.promote("prj", pending["id"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["reasons"])

    def test_search_is_project_scoped(self):
        mine = self._shared("共享条目：先复现再修复，能减少盲目改动", task="ta")
        other = promote.create_candidate("prj-other", "fact",
                                         "别的项目候选：先复现再修复的私有结论",
                                         importance=0.8)
        index.rebuild(verify=True)
        rows = index.search("复现", project="prj")
        self.assertTrue(any(row["id"] == mine["id"] for row in rows))
        self.assertFalse(any(row["id"] == other["id"] for row in rows))
        rows_other = index.search("复现", project="prj-other")
        self.assertTrue(any(row["id"] == other["id"] for row in rows_other))

    def test_memory_search_merges_v2(self):
        a = self._shared("共享检索命中：先复现再修复", task="ta")
        index.rebuild()
        out = json.loads(agent_tools.memory_search("复现"))
        self.assertTrue(any(item.get("source") == "memory_v2" and item.get("id") == a["id"]
                            for item in out["items"]))

    def test_promotion_preserves_edges(self):
        a = self._shared("关联目标条目：先写测试再合并，能降低回归", task="ta")
        rid = receipts.record("prj", type="verify", result="pass")
        promote.create_candidate("prj", "fact", "被关联的新结论：测试先行", importance=0.8,
                                 receipt_ids=[rid], task="tb",
                                 edges=[{"type": "refines", "to": a["id"]}])
        feedback.record("prj", session="s1", stars=5, task="tb")
        promote.sweep("prj")
        fresh = next(item for item in promote.shared_items()
                     if item["body"].startswith("被关联"))
        self.assertTrue(any(edge.get("to") == a["id"] and edge.get("type") == "refines"
                            for edge in fresh["edges"]))


class IndexTests(_MemBase):
    def _write_fact(self, name="m1.md", body="正文一"):
        directory = os.path.join(store.root(), "shared", "facts")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        store.atomic_write(path, (
            "---\nid: mem-1\nkind: fact\nscope: global\ntitle: 标题一\n"
            "confidence: 0.7\nedges:\n- type: refines\n  to: mem-0\n---\n" + body))
        return path

    def test_rebuild_is_idempotent_and_survives_index_loss(self):
        self._write_fact()
        first = index.rebuild(verify=True)
        self.assertEqual((first["items"], first["inserted"], first["verified"]), (1, 1, True))
        second = index.rebuild(verify=True)
        self.assertEqual((second["inserted"], second["updated"], second["unchanged"]),
                         (0, 0, 1))
        os.unlink(index.path())
        third = index.rebuild(verify=True)
        self.assertEqual((third["items"], third["verified"]), (1, True))
        self.assertEqual(index.stats()["edges"], 1)

    def test_edit_and_remove_are_picked_up(self):
        path = self._write_fact()
        index.rebuild()
        store.atomic_write(path, store.read_text(path).replace("正文一", "正文二"))
        edited = index.rebuild()
        self.assertEqual(edited["updated"], 1)
        os.unlink(path)
        removed = index.rebuild()
        self.assertEqual((removed["removed"], removed["items"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
