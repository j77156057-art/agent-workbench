# -*- coding: utf-8 -*-
"""trace 账本 + 会话隔离/持久化/压缩 的回归测试（全部离线，不触网）。"""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import agent as agent_mod
import agent_trace
import sessions
import agent_memory


class _FakeLLM:
    """按脚本回放的假 LLM：逐字符流式，并回报固定 token 用量。"""
    provider = "fake"
    model = "fake-1"

    def __init__(self, scripts):
        self.scripts = scripts
        self.i = 0
        self.last_usage = {}

    def chat(self, messages, stream=True, **kw):
        script = self.scripts[min(self.i, len(self.scripts) - 1)]
        self.i += 1
        self.last_usage = {"prompt_tokens": 7, "completion_tokens": 3}
        if stream:
            def g():
                for ch in script:
                    yield ch
            return g()
        return script

    def count_tokens(self, text):
        return max(0, len(text) // 3)


class _IsoBase(unittest.TestCase):
    """把 trace 文件与会话目录重定向到临时目录，避免污染仓库/相互干扰。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_iso_")
        self._old_trace = agent_trace.TRACE_FILE
        self._old_dir = sessions.SESSIONS_DIR
        self._old_memory_db = agent_memory.DB_PATH
        agent_memory.DB_PATH = os.path.join(self.tmp, "memory.sqlite3")
        self._old_enabled = agent_trace.TRACE_ENABLED
        agent_trace.TRACE_FILE = os.path.join(self.tmp, "traces.jsonl")
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "sessions")
        agent_trace.TRACE_ENABLED = True

    def tearDown(self):
        agent_trace.TRACE_FILE = self._old_trace
        sessions.SESSIONS_DIR = self._old_dir
        agent_memory.DB_PATH = self._old_memory_db
        agent_trace.TRACE_ENABLED = self._old_enabled


class TraceRecordTests(_IsoBase):
    def test_turn_record_fields(self):
        t = agent_trace.Turn(session_id="s", provider="p", model="m", question="hello")
        t.snapshot_prompt([{"role": "user", "content": "hello"}])
        t.add_usage({"prompt_tokens": 10, "completion_tokens": 4})
        t.tool_step("search_code", "query: x", 12.0, "obs", True)
        t.llm_step(30.0, "stop")
        t.finish("completed", final_text="ans")
        r = t.to_record()
        self.assertEqual(r["total_tokens"], 14)
        self.assertEqual(r["n_steps"], 1)
        self.assertEqual(r["messages_count"], 1)
        self.assertEqual(r["outcome"], "completed")
        self.assertTrue(r["messages_hash"])
        self.assertGreaterEqual(r["elapsed_ms"], 0)

    def test_add_usage_captures_cache_tokens(self):
        t = agent_trace.Turn(session_id="s", provider="qwen", model="qwen-plus", question="q")
        t.add_usage({"prompt_tokens": 1000, "completion_tokens": 100,
                     "prompt_tokens_details": {"cached_tokens": 900},
                     "cache_creation_input_tokens": 50})
        self.assertEqual(t.prompt_tokens, 1000)
        self.assertEqual(t.completion_tokens, 100)
        self.assertEqual(t.cache_read_tokens, 900)
        self.assertEqual(t.cache_creation_tokens, 50)
        r = t.to_record()
        self.assertEqual(r["cache_read_tokens"], 900)
        self.assertEqual(r["cache_creation_tokens"], 50)

    def test_summary_aggregates_cache_tokens(self):
        t = agent_trace.Turn(session_id="s", provider="qwen", model="qwen-plus")
        t.add_usage({"prompt_tokens": 1000, "completion_tokens": 0,
                     "prompt_tokens_details": {"cached_tokens": 800}})
        t.cost_cny = 0.02
        t.finish("completed")
        agent_trace.record(t.to_record())
        s = agent_trace.summary()
        self.assertEqual(s["cache_read_tokens"], 800)
        self.assertEqual(s["by_provider"]["qwen"]["cache_read_tokens"], 800)

    def test_messages_hash_is_stable_and_content_addressed(self):
        a = agent_trace.messages_hash([{"role": "user", "content": "x"}])
        b = agent_trace.messages_hash([{"role": "user", "content": "x"}])
        c = agent_trace.messages_hash([{"role": "user", "content": "y"}])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_recent_and_summary(self):
        for i in range(3):
            t = agent_trace.Turn(session_id="s", provider="mock", model="m", question="q")
            t.add_usage({"prompt_tokens": 2, "completion_tokens": 1})
            t.cost_cny = 0.125
            t.finish("completed")
            agent_trace.record(t.to_record())
        rows = agent_trace.recent(10)
        self.assertEqual(len(rows), 3)
        s = agent_trace.summary()
        self.assertEqual(s["turns"], 3)
        self.assertEqual(s["total_tokens"], 9)
        self.assertEqual(s["by_provider"]["mock"]["turns"], 3)
        self.assertEqual(s["by_provider"]["mock"]["prompt_tokens"], 6)
        self.assertEqual(s["by_provider"]["mock"]["completion_tokens"], 3)
        self.assertEqual(s["total_cost_cny"], 0.375)
        self.assertEqual(s["by_provider"]["mock"]["cost_cny"], 0.375)

    def test_disabled_trace_writes_nothing(self):
        agent_trace.TRACE_ENABLED = False
        t = agent_trace.Turn(session_id="s", provider="mock", model="m")
        t.finish("completed")
        self.assertFalse(agent_trace.record(t.to_record()))
        self.assertEqual(agent_trace.recent(10), [])


class AgentTraceIntegrationTests(_IsoBase):
    def test_failed_repeated_question_is_saved_as_a_new_turn(self):
        from unittest.mock import patch
        a = agent_mod.Agent(llm=_FakeLLM(["Final Answer: ok"]), session_id="s-failure")
        a.history = [{"user": "广西北海", "assistant": "previous answer"}]
        with patch.object(a, "_run", side_effect=RuntimeError("sensitive backend details")):
            with self.assertRaises(RuntimeError):
                list(a.run("广西北海", stream=False))
        self.assertEqual(len(a.history), 2)
        self.assertEqual(a.history[-1]["user"], "广西北海")
        self.assertIn("未完成", a.history[-1]["assistant"])
        self.assertNotIn("sensitive", a.history[-1]["assistant"])
        restored = agent_mod.Agent(llm=_FakeLLM(["Final Answer: ok"]), session_id="s-failure")
        self.assertEqual(restored.history, a.history)

    def test_agent_run_records_one_trace(self):
        a = agent_mod.Agent(llm=_FakeLLM(["Final Answer: 你好"]), session_id="s-trace")
        events = list(a.run("hi", stream=True))
        # 收尾会补发 context 用量事件，final 不再是流里最后一个事件
        final_ev = next(e for e in events if e["type"] == "final")
        self.assertEqual(final_ev["type"], "final")
        rows = agent_trace.recent(10)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["session_id"], "s-trace")
        self.assertEqual(r["provider"], "fake")
        self.assertEqual(r["outcome"], "completed")
        self.assertGreater(r["prompt_tokens"], 0)
        self.assertGreater(r["completion_tokens"], 0)
        self.assertIsNotNone(r["messages_hash"])

    def test_tool_step_is_recorded(self):
        a = agent_mod.Agent(llm=_FakeLLM([
            "Thought: 查一下\nAction: read_file\nAction Input: path: agent.py",
            "Final Answer: 读完了",
        ]), session_id="s-tool")
        list(a.run("读文件", stream=True))
        r = agent_trace.recent(1)[0]
        self.assertEqual(r["n_steps"], 1)
        self.assertEqual(r["actions"], ["read_file"])
        self.assertIn("read_file", [s["action"] for s in r["steps"]])

    def test_deadline_aborts_turn(self):
        a = agent_mod.Agent(llm=_FakeLLM(["Final Answer: 不该到达"]), session_id="s-dl")
        events = list(a.run("q", stream=True, deadline=time.monotonic() - 1))
        final_ev = next(e for e in events if e["type"] == "final")
        self.assertIn("时间上限", final_ev["text"])
        self.assertEqual(agent_trace.recent(1)[0]["outcome"], "deadline_exceeded")

    def test_client_disconnect_marks_aborted(self):
        a = agent_mod.Agent(llm=_FakeLLM(["Thought: 慢慢想" * 5]), session_id="s-abort")
        gen = a.run("q", stream=True)
        next(gen)          # 取第一个 token 后模拟前端断开
        gen.close()
        rows = agent_trace.recent(10)
        self.assertTrue(rows and rows[-1]["aborted"])


class SessionIsolationTests(_IsoBase):
    def test_history_persisted_per_session(self):
        list(agent_mod.Agent(llm=_FakeLLM(["Final Answer: A"]), session_id="s1").run("q1", stream=True))
        list(agent_mod.Agent(llm=_FakeLLM(["Final Answer: B"]), session_id="s2").run("q2", stream=True))
        self.assertEqual([t["user"] for t in sessions.history("s1")], ["q1"])
        self.assertEqual([t["user"] for t in sessions.history("s2")], ["q2"])

    def test_history_survives_new_agent(self):
        list(agent_mod.Agent(llm=_FakeLLM(["Final Answer: A"]), session_id="keep").run("q1", stream=True))
        # 新建 Agent（模拟进程重启）：应从会话文件恢复历史
        a2 = agent_mod.Agent(llm=_FakeLLM(["Final Answer: B"]), session_id="keep")
        self.assertEqual([t["user"] for t in a2.history], ["q1"])

    def test_no_session_id_is_ephemeral(self):
        # 无 session_id：保留进程内记忆（与旧行为一致），但绝不落盘。
        a = agent_mod.Agent(llm=_FakeLLM(["Final Answer: x"]))
        list(a.run("q", stream=True))
        self.assertEqual([t["user"] for t in a.history], ["q"])
        self.assertFalse(os.path.isdir(sessions.SESSIONS_DIR) and os.listdir(sessions.SESSIONS_DIR),
                         "无 session_id 时不应写入任何会话文件")

    def test_compaction_folds_old_turns_into_summary(self):
        # 每轮 60 字符 -> _FakeLLM 口径约 20 token/轮；阈值按 token 传入
        turns = [{"user": f"问{i}" + "甲" * 28, "assistant": "乙" * 30} for i in range(20)]
        kept, summary = sessions.maybe_compact(
            "long", turns, _FakeLLM([]), trigger_tokens=100, keep_tokens=70)
        # 每轮约 20 token：保留目标 70 token -> 恰好 3 轮（高于 KEEP_TURNS_MIN 下限）
        self.assertEqual(len(kept), 3)
        self.assertGreaterEqual(len(kept), sessions.KEEP_TURNS_MIN)
        self.assertIn("摘要", summary)
        self.assertTrue(kept[-1]["user"].startswith("问19"))

    def test_large_window_threshold_does_not_trigger_on_moderate_history(self):
        # 1M 窗口模型：8000 字历史远未到其压缩阈值，绝不能按旧的固定 8000 字符压缩
        turns = [{"user": "字" * 200, "assistant": "答" * 200} for _ in range(20)]
        kept, summary = sessions.maybe_compact(
            "bigwin", turns, None, trigger_tokens=700_000, keep_tokens=350_000)
        self.assertEqual(len(kept), 20)
        self.assertEqual(summary, "")

    def test_turn_hard_cap_still_compacts(self):
        # token 没超但轮数超过硬上限：也必须收敛（防失控保险）
        turns = [{"user": f"q{i}", "assistant": f"a{i}"} for i in range(sessions.MAX_TURNS + 5)]
        kept, _ = sessions.maybe_compact(
            "cap", turns, None, trigger_tokens=10 ** 9, keep_tokens=10 ** 9)
        self.assertLessEqual(len(kept), sessions.MAX_TURNS)
        self.assertEqual(kept[-1]["user"], f"q{sessions.MAX_TURNS + 4}")

    def test_slug_blocks_path_traversal(self):
        self.assertNotIn("/", sessions._slug("../../evil"))
        self.assertNotIn("..", sessions._slug("../../evil"))

    def test_list_sessions(self):
        list(agent_mod.Agent(llm=_FakeLLM(["Final Answer: A"]), session_id="ls1").run("q", stream=True))
        ids = [s["session_id"] for s in sessions.list_sessions()]
        self.assertIn("ls1", ids)

    def test_load_cleans_legacy_system_prompt_prefix(self):
        """旧版本写入的路由提示只在会话展示/回放边界清理。"""
        sessions.save("legacy-prompt", [{
            "user": "【系统提示】知识库中已上传以下文档：LICENSE。\n\n用户问题：你可以帮我修改文件吗",
            "assistant": "【系统提示】代码库已索引。\n\n用户问题：可以，先确认目标。",
        }])
        row = sessions.load("legacy-prompt")["turns"][0]
        self.assertEqual(row["user"], "你可以帮我修改文件吗")
        self.assertEqual(row["assistant"], "可以，先确认目标。")
        self.assertEqual(
            sessions._clean_legacy_prompt("用户问：系统提示是正文"),
            "用户问：系统提示是正文",
        )


class _TruncStream:
    """可迭代的假流式响应，带 finish_reason / reasoning_chars（模拟 llm.StreamChat）。"""
    def __init__(self, chunks, finish_reason="stop", reasoning_chars=0):
        self._chunks = list(chunks)
        self.finish_reason = finish_reason
        self.reasoning_chars = reasoning_chars

    def __iter__(self):
        return iter(self._chunks)


class _TruncFakeLLM:
    """第一次返回被长度截断、无 Final 的回答；续写重试（关思考）后给简短 Final。"""
    provider = "fake"
    model = "fake-1"

    def __init__(self):
        self.calls = []  # 记录每次 chat 收到的 enable_thinking
        self.last_usage = {"prompt_tokens": 5, "completion_tokens": 5}
        self.last_tool_calls = []

    def chat(self, messages, stream=True, enable_thinking=None, **kw):
        self.calls.append(enable_thinking)
        if len(self.calls) == 1:
            # 思考烧满预算、被截断、没有 Final Answer
            return _TruncStream(["Thought: 思考很长很长很长……"],
                                finish_reason="length", reasoning_chars=2500)
        return _TruncStream(["Final Answer: addons 目录含 X、Y 两个插件。"])

    def count_tokens(self, text):
        return max(0, len(text) // 3)


class TruncationRetryTests(_IsoBase):
    def test_final_answer_continues_without_rewriting_or_losing_first_part(self):
        class FinalLLM(_TruncFakeLLM):
            def chat(self, messages, stream=True, enable_thinking=None, **kw):
                self.calls.append((enable_thinking, messages[-1]["content"]))
                segments = [
                    ("Final Answer: 已核对 agent.py 的入口、", "length"),
                    ("工具调用和错误处理。另有完整测试记录、", "length"),
                    ("以及最终审查结论。", "stop"),
                ]
                text, reason = segments[min(len(self.calls) - 1, len(segments) - 1)]
                return _TruncStream([text], finish_reason=reason)

        llm = FinalLLM()
        a = agent_mod.Agent(llm=llm, session_id="s-final-continuation")
        a.thinking_enabled = True
        events = list(a.run("帮我审查一下代码", stream=True))
        finals = [ev for ev in events if ev["type"] == "final"]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["text"],
                         "已核对 agent.py 的入口、工具调用和错误处理。另有完整测试记录、以及最终审查结论。")
        self.assertEqual(llm.calls[1][0], False)
        self.assertNotIn("3-6 句", llm.calls[1][1])
        self.assertFalse(any(ev["type"] == "reflection" for ev in events))

    def test_continuation_merges_repeated_tail_and_marker(self):
        self.assertEqual(agent_mod._join_final_continuation(
            "第一段结论，第二段", "Final Answer: 第二段继续。"),
            "第一段结论，第二段继续。")

    def test_continuation_preserves_word_boundary(self):
        class SplitLLM(_TruncFakeLLM):
            def chat(self, messages, stream=True, enable_thinking=None, **kw):
                self.calls.append(enable_thinking)
                if len(self.calls) == 1:
                    return _TruncStream(["Final Answer: Hello "], finish_reason="length")
                return _TruncStream(["world"], finish_reason="stop")

        a = agent_mod.Agent(llm=SplitLLM(), session_id="s-final-space")
        final = next(ev for ev in a.run("say hello", stream=True) if ev["type"] == "final")
        self.assertEqual(final["text"], "Hello world")

    def test_repeated_length_returns_partial_as_incomplete_not_success(self):
        class NeverEndingLLM(_TruncFakeLLM):
            def chat(self, messages, stream=True, enable_thinking=None, **kw):
                self.calls.append(enable_thinking)
                return _TruncStream(["Final Answer: 第一段。" if len(self.calls) == 1 else "第二段。"],
                                    finish_reason="length")

        with patch.object(agent_mod, "_MAX_FINAL_CONTINUATIONS", 2):
            a = agent_mod.Agent(llm=NeverEndingLLM(), session_id="s-final-incomplete")
            events = list(a.run("帮我审查代码", stream=True))
        final = next(ev for ev in events if ev["type"] == "final")
        self.assertEqual(final.get("status"), "error")
        self.assertIn("第一段。第二段。", final["text"])
        self.assertIn("回答尚未结束", final["text"])

    def test_truncation_retry_suppresses_thinking(self):
        llm = _TruncFakeLLM()
        a = agent_mod.Agent(llm=llm, session_id="s-trunc")
        a.thinking_enabled = True
        events = list(a.run("继续勘察 addons 目录", stream=True))
        # 第 1 次思考开着；被截断后续写重试应关思考，把预算留给答案
        self.assertEqual(llm.calls[0], True)
        self.assertEqual(llm.calls[1], False)
        self.assertTrue(any(e["type"] == "final" for e in events),
                        "截断重试后仍未产出 final 回答")

    def test_truncation_without_thinking_stays_off(self):
        # 思考本就关闭时，截断续写不应反复失败，且保持关思考
        llm = _TruncFakeLLM()
        a = agent_mod.Agent(llm=llm, session_id="s-trunc2")
        a.thinking_enabled = False
        events = list(a.run("继续勘察 addons 目录", stream=True))
        self.assertEqual(llm.calls[0], False)
        self.assertEqual(llm.calls[1], False)
        self.assertTrue(any(e["type"] == "final" for e in events))


class TestCommandEvidenceTests(unittest.TestCase):
    def test_recognizes_test_commands_without_grep_false_positive(self):
        for command in (
            "python -m pytest tests -q",
            "py -m pytest",
            "pytest",
            "npm test",
            "npm run test",
            "pnpm test",
            "go test ./...",
            "cargo test",
            "cd sub && python -m pytest -q",
        ):
            self.assertTrue(agent_mod._is_test_command(command), command)
        for command in ("grep pytest", "echo npm test", "python script.py --test"):
            self.assertFalse(agent_mod._is_test_command(command), command)

    def test_test_evidence_requires_unchanged_targets(self):
        turn = agent_trace.Turn()
        turn.verification_targets = {"a.py": "hash-1"}
        turn.test_command_ok = True
        turn.test_command_targets = {"a.py": "hash-1"}
        self.assertTrue(agent_mod._test_command_verified(turn))
        turn.verification_targets = {"a.py": "hash-2"}
        self.assertFalse(agent_mod._test_command_verified(turn),
                         "测试之后又改了文件，旧测试结果不能继续作数")


if __name__ == "__main__":
    unittest.main()
