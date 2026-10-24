# -*- coding: utf-8 -*-
"""回归锁：python_exec 待确认卡必须跟着「回合归属」走。

实据（工作台）：一次因额度耗尽（402）中断的回合，在会话里留下两张
「需确认 · 同意执行」的卡片；回合早已结束、没人再等那段代码，用户点下去
却会真的执行上一轮的旧操作。修法：stage 时记 owner（会话 id），
  · 回合异常结束（raise/断连）→ 立刻清；
  · 同一会话开新回合 → 上一回合没被批准就换话题的旧卡作废；
  · 正常收尾 → 保留（模型常常是「登记待确认」后结束回合等用户点确认）。
"""

import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as agent_mod  # noqa: E402
import tools as tools_mod  # noqa: E402


class _FakeLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self):
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}

    def chat(self, messages, stream=True, **kw):
        text = "Final Answer: 完成"
        if stream:
            def gen():
                for ch in text:
                    yield ch
            return gen()
        return text

    def count_tokens(self, text):
        return max(0, len(text) // 3)


def _stage(code="print(1)", tier=2, reason="测试"):
    return tools_mod.stage_exec(code, tier, reason, source="preexec")


@contextmanager
def _owner(value):
    """set_exec_owner 返回 reset token（不是上下文管理器），测试里自己包一层。"""
    token = tools_mod.set_exec_owner(value)
    try:
        yield
    finally:
        tools_mod._exec_owner.reset(token)


class DropPendingExecsTests(unittest.TestCase):
    """drop_pending_execs 只认精确 owner，空 owner 绝不清表。"""

    def setUp(self):
        tools_mod._PENDING_EXEC.clear()

    def tearDown(self):
        tools_mod._PENDING_EXEC.clear()

    def test_drops_only_matching_owner(self):
        with _owner("sess-A"):
            pa = _stage()
        with _owner("sess-B"):
            pb = _stage()
        self.assertEqual(tools_mod.drop_pending_execs("sess-A"), 1)
        self.assertEqual(sorted(tools_mod._PENDING_EXEC), [pb],
                         "别的会话的待确认不能误删")

    def test_empty_owner_is_noop(self):
        with _owner("sess-A"):
            pid = _stage()
        self.assertEqual(tools_mod.drop_pending_execs(""), 0)
        self.assertIn(pid, tools_mod._PENDING_EXEC,
                      "空 owner 不能把整张表清掉")

    def test_entry_records_owner(self):
        with _owner("sess-A"):
            pid = _stage()
        self.assertEqual(tools_mod._PENDING_EXEC[pid]["owner"], "sess-A")


class TurnCleanupTests(unittest.TestCase):
    """走真实 Agent.run 收尾路径（只把 _run_shell 换成桩）。"""

    def setUp(self):
        tools_mod._PENDING_EXEC.clear()

    def tearDown(self):
        tools_mod._PENDING_EXEC.clear()

    def _agent(self, session_id):
        return agent_mod.Agent(
            llm=_FakeLLM(), session_id=session_id,
            tool_registry=dict(agent_mod.TOOLS))

    @staticmethod
    def _run_with(agent, shell, question="测试"):
        with patch.object(agent_mod.Agent, "_run_shell", shell):
            return list(agent.run(question))

    def test_normal_turn_keeps_pending_for_human_click(self):
        holder = {}

        def shell(self, question, **kw):
            holder["pid"] = _stage()
            yield {"type": "final", "text": "已登记待确认"}

        agent = self._agent("sess-keep")
        self._run_with(agent, shell)
        entry = tools_mod._PENDING_EXEC.get(holder["pid"])
        self.assertIsNotNone(entry, "正常收尾的待确认要留给用户点批准")
        self.assertEqual(entry["owner"], "sess-keep")
        self.assertEqual(tools_mod._exec_owner.get(), "",
                         "回合结束必须把 owner 还原，不能污染后续请求")

    def test_aborted_turn_drops_pending(self):
        holder = {}

        def shell(self, question, **kw):
            holder["pid"] = _stage()
            raise RuntimeError("boom")
            yield  # pragma: no cover - 让函数成为生成器

        agent = self._agent("sess-abort")
        with self.assertRaises(RuntimeError):
            self._run_with(agent, shell)
        self.assertNotIn(holder["pid"], tools_mod._PENDING_EXEC,
                         "回合异常结束，审批卡必须随之作废")

    def test_new_turn_in_same_session_clears_leftover(self):
        holder = {}

        def shell1(self, question, **kw):
            holder["pid"] = _stage()
            yield {"type": "final", "text": "登记待确认"}

        agent = self._agent("sess-same")
        self._run_with(agent, shell1)
        self.assertIn(holder["pid"], tools_mod._PENDING_EXEC)

        def shell2(self, question, **kw):
            yield {"type": "final", "text": "新回合"}

        self._run_with(agent, shell2)
        self.assertNotIn(holder["pid"], tools_mod._PENDING_EXEC,
                         "用户没批就开了新回合，旧卡应作废")

    def test_other_session_pending_survives(self):
        with _owner("sess-other"):
            pid = _stage()

        def shell(self, question, **kw):
            yield {"type": "final", "text": "完成"}

        agent = self._agent("sess-mine")
        self._run_with(agent, shell)
        self.assertIn(pid, tools_mod._PENDING_EXEC,
                      "会话之间不能互相清理待确认")

    def test_next_turn_keeps_pending_staged_by_itself(self):
        """同一回合内先清旧、再登记新的：新回合自己的卡不能被开头那次清理误删。"""
        holder = {}

        def shell(self, question, **kw):
            holder["pid"] = _stage()
            yield {"type": "final", "text": "登记待确认"}

        agent = self._agent("sess-fresh")
        self._run_with(agent, shell)
        self.assertIn(holder["pid"], tools_mod._PENDING_EXEC)
        self.assertEqual(tools_mod._PENDING_EXEC[holder["pid"]]["owner"], "sess-fresh")


if __name__ == "__main__":
    unittest.main()
