"""记忆系统 v2 · P0 回归：开关门、宪法注入顺序与上限、保存/哈希审计、
索引重建幂等、路径越界、turn trace 快照。
"""
import os
import tempfile
import unittest

import agent as agent_mod
import config
from agent_runtime.memory import (
    constitution,
    episodes,
    errors,
    feedback,
    hooks,
    index,
    injection,
    receipts,
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

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._RUNTIME.clear()
        config._RUNTIME.update(self._runtime)

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
            verification_targets = {"a.py": True}

        self.assertTrue(agent._final_event("完成", _T())["deliverable"])


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
