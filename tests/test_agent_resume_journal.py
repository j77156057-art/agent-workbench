# -*- coding: utf-8 -*-
"""精确断点续跑（journal）的回归测试：现场落库、从下一步继续、幂等与 fail-closed。

这些用例只碰内存/临时 SQLite，不触网、不调真实模型。核心判据不是「有续跑提示」，
而是**上次已完成的工具调用不会被重放**、**未完成的只读调用会被重放**、
**未完成的有副作用调用不会被自动重放**。
"""
import json
import unittest
from unittest.mock import patch

import agent as agent_mod
import agent_memory as memory
import config
from agent_runtime.tools import ToolResult
from tests.test_agent_trace import _FakeLLM, _IsoBase

QUESTION = "把剩下的步骤做完"


class _CapturingLLM(_FakeLLM):
    """按脚本回放，同时记下每次收到的 messages，供断言「现场真的喂给了模型」。"""

    def __init__(self, scripts):
        super().__init__(scripts)
        self.calls = []

    def chat(self, messages, stream=True, **kw):
        self.calls.append([dict(m) for m in messages])
        return super().chat(messages, stream=stream, **kw)


def _fake_execute(record, text="工具观察：完成"):
    """替换上层 execute_tool：只记录调用并回一条成功观察，不碰真实工具。"""
    def _exec(function, argument, legacy_failure, *, tool_name="", side_effect=None):
        record.append((tool_name, argument))
        return ToolResult(True, text)
    return _exec


def _journal(**overrides):
    base = {
        "version": 1,
        "phase": "step_done",
        "question": QUESTION,
        "history_len": 0,
        "trail": [
            {"role": "assistant", "content": "Action: read_file\nAction Input: one.txt"},
            {"role": "user", "content": "Observation: 第一份文件的内容"},
        ],
        "counters": {"tool_steps": 1, "iterations": 1},
        "executed": [["read_file", "one.txt"]],
        "pending_batch": [],
        "native_queue": [],
        "started": None,
        "file_changes": [],
        "evidence": ["read_file(one.txt)"],
        "audit_steps": [{"action": "read_file(one.txt)", "ok": True, "truncated": False}],
    }
    base.update(overrides)
    return base


class JournalPersistenceTests(_IsoBase):
    def test_journal_round_trips_through_checkpoint(self):
        scope = memory.scope_key("p")
        cid = memory.checkpoint(scope, "sess-j", QUESTION, status="interrupted",
                                journal=_journal())
        got = memory.checkpoint_get(scope, "sess-j", cid)
        self.assertIsNotNone(got)
        self.assertEqual(got["journal"]["phase"], "step_done")
        self.assertEqual(got["journal"]["executed"], [["read_file", "one.txt"]])
        self.assertEqual(got["journal"]["trail"][1]["content"], "Observation: 第一份文件的内容")
        self.assertIsNotNone(
            memory.latest_checkpoint(scope, "sess-j")["journal"]["trail"][1])

    def test_event_only_update_does_not_wipe_last_journal(self):
        scope = memory.scope_key("p")
        cid = memory.checkpoint(scope, "sess-j2", QUESTION, journal=_journal())
        # 只更新事件、不带 journal（旧调用路径）时，已落盘的现场必须保留。
        memory.checkpoint(scope, "sess-j2", QUESTION, cid=cid, status="running",
                          events=["observation: 新观察"])
        got = memory.checkpoint_get(scope, "sess-j2", cid)
        self.assertEqual(got["journal"]["phase"], "step_done")
        self.assertEqual(got["events"], ["observation: 新观察"])

    def test_oversized_journal_sheds_oldest_trail_but_stays_parsable(self):
        scope = memory.scope_key("p")
        big = "x" * 20000
        trail = [{"role": "user", "content": "%05d-%s" % (i, big)} for i in range(60)]
        cid = memory.checkpoint(scope, "sess-big", QUESTION,
                                journal=_journal(trail=trail, executed=[]))
        got = memory.checkpoint_get(scope, "sess-big", cid)
        journal = got["journal"]
        self.assertIsNotNone(journal, "超限时应裁剪而不是整份丢弃")
        self.assertTrue(journal.get("trail_truncated"))
        self.assertLessEqual(len(json.dumps(journal, ensure_ascii=False)),
                             memory.JOURNAL_LIMIT_CHARS)
        # 保留的是较新的一端，最新一条必须还在。
        self.assertTrue(journal["trail"][-1]["content"].startswith("00059-"))


class ExactResumeTests(_IsoBase):
    def _seed(self, session, **overrides):
        scope = memory.scope_key("p")
        cid = memory.checkpoint(scope, session, QUESTION,
                                status="interrupted", journal=_journal(**overrides))
        return scope, cid

    def test_resume_continues_from_next_step_without_replaying_finished_tool(self):
        scope, cid = self._seed("sess-next")
        llm = _CapturingLLM([
            "Action: read_file\nAction Input: two.txt",
            "Final Answer: 剩下的一步也完成了",
        ])
        a = agent_mod.Agent(llm=llm, session_id="sess-next", project_id="p")
        executed = []
        with patch.object(agent_mod, "execute_tool", _fake_execute(executed, "第二份文件的内容")):
            events = list(a.run("继续", resume=cid))

        # 继续的是下一步：只执行了两个文件中的第二个，已完成的一步没有重放。
        self.assertEqual(executed, [("read_file", "two.txt")])
        final = next(e for e in events if e["type"] == "final")
        self.assertEqual(final["text"], "剩下的一步也完成了")
        # 上一次的观察真的进了模型上下文，不是只发了一句「继续」。
        prompt = json.dumps(llm.calls[0], ensure_ascii=False)
        self.assertIn("第一份文件的内容", prompt)
        # 问题被还原成中断时的原问题，历史里不会多出一条「继续」。
        self.assertEqual(a.history[-1]["user"], QUESTION)
        # 续跑提示存在且不骗人。
        self.assertTrue(any(e["type"] == "notice" and "已从断点继续" in e["text"]
                            for e in events))
        # 同一 checkpoint 被继续写：现场变成新终态，新旧账本都在。
        rec = memory.checkpoint_get(scope, "sess-next", cid)
        self.assertEqual(rec["journal"]["phase"], "final")
        self.assertIn(["read_file", "one.txt"], rec["journal"]["executed"])
        self.assertIn(["read_file", "two.txt"], rec["journal"]["executed"])

    def test_repeating_an_already_executed_action_is_blocked_by_restored_ledger(self):
        self._seed("sess-ledger")
        llm = _CapturingLLM([
            "Action: read_file\nAction Input: one.txt",
            "Final Answer: 已基于已有观察作答",
        ])
        a = agent_mod.Agent(llm=llm, session_id="sess-ledger", project_id="p")
        executed = []
        with patch.object(agent_mod, "execute_tool", _fake_execute(executed)):
            events = list(a.run("继续", resume="latest"))

        self.assertEqual(executed, [], "已记录的执行签名被重放了")
        self.assertTrue(any(e["type"] == "reflection" and "重复调用" in e["text"]
                            for e in events))
        self.assertTrue(any(e["type"] == "final" and e["text"] == "已基于已有观察作答"
                            for e in events))
        # 第二轮问模型时带上了「不要重复调用」的纠正。
        self.assertIn("不要重复调用", json.dumps(llm.calls[1], ensure_ascii=False))

    def test_started_readonly_action_is_replayed_without_asking_model_again(self):
        self._seed("sess-replay",
                   phase="action_started",
                   trail=[{"role": "user", "content": "Observation: 之前的观察"}],
                   executed=[],
                   started={"name": "read_file", "arg": "two.txt", "side_effect": False})
        llm = _CapturingLLM(["Final Answer: 重放后收尾"])
        a = agent_mod.Agent(llm=llm, session_id="sess-replay", project_id="p")
        executed = []
        with patch.object(agent_mod, "execute_tool", _fake_execute(executed, "重放拿到的内容")):
            events = list(a.run("继续", resume="latest"))

        self.assertEqual(executed, [("read_file", "two.txt")])
        self.assertTrue(any(e["type"] == "observation" and e["text"] == "重放拿到的内容"
                            for e in events))
        self.assertTrue(any(e["type"] == "notice" and "正在重放未完成的一步" in e["text"]
                            for e in events))
        # 重放发生在问模型之前：第一次模型调用收到的上下文里已有重放观察。
        self.assertIn("重放拿到的内容", json.dumps(llm.calls[0], ensure_ascii=False))

    def test_started_side_effect_action_is_not_replayed(self):
        self._seed("sess-side",
                   phase="action_started",
                   trail=[{"role": "user", "content": "Observation: 写之前的观察"}],
                   executed=[],
                   started={"name": "create_file", "arg": "notes/app.txt\nhello",
                            "side_effect": True})
        llm = _CapturingLLM(["Final Answer: 已先核对，未重复写入"])
        a = agent_mod.Agent(llm=llm, session_id="sess-side", project_id="p")
        executed = []
        with patch.object(agent_mod, "execute_tool", _fake_execute(executed)):
            events = list(a.run("继续", resume="latest"))

        self.assertEqual(executed, [], "有副作用的未完成调用被自动重放了")
        self.assertTrue(any(e["type"] == "notice" and "可能已执行" in e["text"]
                            for e in events))
        prompt = json.dumps(llm.calls[0], ensure_ascii=False)
        self.assertIn("可能已生效", prompt)
        self.assertIn("不会自动重放", prompt)
        # 账本里登记它，模型若坚持用同参重试也会被防重复闸门拦住。
        rec = memory.latest_checkpoint(memory.scope_key("p"), "sess-side")
        self.assertIn(["create_file", "notes/app.txt\nhello"], rec["journal"]["executed"])

    def test_final_phase_resets_budget_but_keeps_ledger_and_trail(self):
        self._seed("sess-final",
                   phase="final",
                   counters={"tool_steps": 99, "iterations": 99, "failures": 9},
                   executed=[["read_file", "one.txt"]])
        llm = _CapturingLLM([
            "Action: read_file\nAction Input: two.txt",
            "Final Answer: 新一轮努力完成",
        ])
        a = agent_mod.Agent(llm=llm, session_id="sess-final", project_id="p")
        executed = []
        with patch.object(agent_mod, "execute_tool", _fake_execute(executed, "第二个文件")):
            events = list(a.run("继续", resume="latest"))

        self.assertEqual(executed, [("read_file", "two.txt")])
        self.assertTrue(any(e["type"] == "final" and e["text"] == "新一轮努力完成"
                            for e in events))
        rec = memory.latest_checkpoint(memory.scope_key("p"), "sess-final")
        counters = rec["journal"]["counters"]
        # 预算/纠偏计数已重置（否则上一轮耗尽的 99 步会立刻再撞上限）。
        self.assertLess(counters["tool_steps"], 10)
        self.assertEqual(counters["failures"], 0)
        # 现场与账本保留。
        self.assertIn(["read_file", "one.txt"], rec["journal"]["executed"])
        self.assertIn("第一份文件的内容",
                      json.dumps(llm.calls[0], ensure_ascii=False))

    def test_stale_journal_is_ignored_and_old_checkpoint_untouched(self):
        scope, cid = self._seed("sess-stale")
        # 中断之后用户又聊过一轮：旧现场不再代表「下一步」。
        a2 = agent_mod.Agent(llm=_FakeLLM(["Final Answer: 新话题的答复"]),
                             session_id="sess-stale", project_id="p")
        list(a2.run("先聊点别的", stream=True))

        llm = _CapturingLLM(["Final Answer: 这是新问题的新回答"])
        a3 = agent_mod.Agent(llm=llm, session_id="sess-stale", project_id="p")
        events = list(a3.run("一个全新的问题", resume=cid))

        self.assertFalse(any(e["type"] == "notice" and "已从断点继续" in e["text"]
                             for e in events))
        self.assertEqual(a3.history[-1]["user"], "一个全新的问题")
        self.assertIn("一个全新的问题", json.dumps(llm.calls[0], ensure_ascii=False))
        # 旧记录不被新一轮覆盖。
        rec = memory.checkpoint_get(scope, "sess-stale", cid)
        self.assertEqual(rec["journal"]["phase"], "step_done")
        self.assertEqual(rec["journal"]["executed"], [["read_file", "one.txt"]])

    def test_completed_checkpoint_is_not_resumed(self):
        scope = memory.scope_key("p")
        cid = memory.checkpoint(scope, "sess-done", QUESTION, status="completed",
                                journal=_journal())
        llm = _CapturingLLM(["Final Answer: 普通新回合"])
        a = agent_mod.Agent(llm=llm, session_id="sess-done", project_id="p")
        events = list(a.run("另一个问题", resume=cid))
        self.assertFalse(any(e["type"] == "notice" and "已从断点继续" in e["text"]
                             for e in events))
        self.assertEqual(a.history[-1]["user"], "另一个问题")

    def test_final_event_carries_checkpoint_id_and_final_journal(self):
        scope = memory.scope_key("p")
        llm = _FakeLLM(["Final Answer: 一步完成"])
        a = agent_mod.Agent(llm=llm, session_id="sess-cid", project_id="p")
        events = list(a.run("做个小事"))
        final = next(e for e in events if e["type"] == "final")
        self.assertTrue(final.get("checkpoint_id"))
        rec = memory.checkpoint_get(scope, "sess-cid", final["checkpoint_id"])
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(rec["journal"]["phase"], "final")
        self.assertEqual(rec["journal"]["question"], "做个小事")


class _FakeAgent:
    """API 接线用的最小 Agent：只关心 run 收到的 resume 参数。"""

    def __init__(self, fail=False):
        self.llm = _FakeLLM(["Final Answer: ok"])
        self.last_checkpoint_id = "cp-after-run"
        self.resume_seen = []
        self.fail = fail

    def run(self, question, **kwargs):
        self.resume_seen.append(kwargs.get("resume"))
        if self.fail:
            def boom():
                yield {"type": "token", "text": "半"}
                raise RuntimeError("upstream down")
            return boom()

        def ok():
            yield {"type": "final", "text": "ok", "checkpoint_id": "cp-after-run"}
        return ok()


class ChatResumeWiringTests(_IsoBase):
    def _client(self, fake):
        import api
        from starlette.testclient import TestClient
        for key in ("llm_provider", "embedding_provider", "code_root", "llm_mode"):
            self.addCleanup(config.set_runtime, key, config.get_runtime(key))
        config.set_runtime("llm_provider", "mock")
        config.set_runtime("embedding_provider", "local")
        config.set_runtime("code_root", self.tmp)
        config.set_runtime("llm_mode", "fixed")
        return api, TestClient(api.app)

    def test_resume_checkpoint_form_field_reaches_agent_run(self):
        fake = _FakeAgent()
        api, client = self._client(fake)
        with patch.object(api, "_agent_for", return_value=fake), \
                patch.object(api, "check_ollama", return_value={"reachable": True}):
            resp = client.post("/api/chat", data={
                "question": "继续完成任务",
                "session_id": "api-resume",
                "resume_checkpoint": "cp-123",
            })
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertEqual(fake.resume_seen, ["cp-123"])

    def test_error_final_exposes_checkpoint_id_for_continue(self):
        fake = _FakeAgent(fail=True)
        api, client = self._client(fake)
        with patch.object(api, "_agent_for", return_value=fake), \
                patch.object(api, "check_ollama", return_value={"reachable": True}):
            resp = client.post("/api/chat", data={
                "question": "继续完成任务",
                "session_id": "api-resume-fail",
            })
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        finals = [line for line in resp.text.splitlines()
                  if line.startswith("data: ") and '"type": "final"' in line]
        self.assertTrue(finals, resp.text[:500])
        event = json.loads(finals[-1][len("data: "):])
        self.assertEqual(event.get("status"), "error")
        self.assertEqual(event.get("checkpoint_id"), "cp-after-run")


if __name__ == "__main__":
    unittest.main()
