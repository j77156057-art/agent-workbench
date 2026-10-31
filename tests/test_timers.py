# -*- coding: utf-8 -*-
"""会话定时器：持久化、重复/一次性、取消，以及到期注入模型上下文。"""
import os
import tempfile
import time
import unittest

import agent as agent_mod
from agent_runtime import timers
import tools


class _TimerLLM:
    provider = "stub"
    model = "stub"

    def __init__(self):
        self.messages = []

    def count_tokens(self, text):
        return max(1, len(str(text or "")) // 4)

    def chat(self, messages, stream=True, **kw):
        self.messages.append(messages)
        if stream:
            return iter(["Final Answer: 好"])
        return "Final Answer: 好"


class _ScriptedLLM:
    """按脚本逐次回包，并留下每次真正送给模型的 messages。"""
    provider = "stub"
    model = "stub"

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self._idx = 0
        self.messages = []

    def clone(self):
        return self

    def count_tokens(self, text):
        return max(1, len(str(text or "")) // 4)

    def chat(self, messages, stream=True, **kw):
        self.messages.append([dict(m) for m in messages])
        text = self._outputs[min(self._idx, len(self._outputs) - 1)]
        self._idx += 1
        return iter([text]) if stream else text


class TimerStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_timer_")
        self._old = os.environ.get("DOCMIND_MEMORY_DIR")
        os.environ["DOCMIND_MEMORY_DIR"] = self.tmp
        self.project = "prj-timer"
        self.session = "s-timer"

    def tearDown(self):
        # 真回合会提交异步固化任务；不排空就会在环境变量还原之后落到下一个用例的存储里。
        from agent_runtime.memory import consolidate
        consolidate.drain()
        if self._old is None:
            os.environ.pop("DOCMIND_MEMORY_DIR", None)
        else:
            os.environ["DOCMIND_MEMORY_DIR"] = self._old

    def test_repeat_due_advances_and_cancel_stops(self):
        row = timers.create(self.project, self.session, message="检查后台任务",
                            delay_seconds=0, interval_seconds=1, task="tb-1")
        self.assertTrue(row["repeat"])
        reminders = timers.due(self.project, self.session)
        self.assertEqual(len(reminders), 1)
        self.assertIn("检查后台任务", reminders[0]["reminder"])
        active = timers.list_timers(self.project, self.session)
        self.assertEqual(active[0]["fired"], 1)
        self.assertTrue(active[0]["next_due"])
        cancelled = timers.cancel(self.project, self.session, row["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(timers.list_timers(self.project, self.session), [])
        self.assertEqual(timers.due(self.project, self.session), [])

    def test_one_shot_becomes_fired(self):
        row = timers.create(self.project, self.session, message="一次性提醒",
                            delay_seconds=0, interval_seconds=0)
        reminders = timers.due(self.project, self.session)
        self.assertEqual(len(reminders), 1)
        self.assertIn("一次性", reminders[0]["reminder"])
        self.assertEqual(timers.list_timers(self.project, self.session), [])
        row = timers.list_timers(self.project, self.session, include_inactive=True)[0]
        self.assertEqual(row["status"], "fired")

    def test_burned_timers_do_not_lock_out_the_session(self):
        """一次性提醒烧满上限后，还必须能再挂新的。

        上限若数「文件里所有行」，一个长会话挂满 24 个一次性提醒后就**永久**设不动
        定时器了 —— 而这类会话恰恰最需要定时器。
        """
        for index in range(timers.MAX_ACTIVE):
            timers.create(self.project, self.session, message="一次性 %d" % index,
                          delay_seconds=0, interval_seconds=0)
        self.assertEqual(len(timers.due(self.project, self.session)), timers.MAX_ACTIVE)
        again = timers.create(self.project, self.session, message="烧完还能再挂",
                              delay_seconds=0, interval_seconds=0)
        self.assertEqual(again["status"], "active")
        self.assertEqual(len(timers.list_timers(self.project, self.session)), 1)
        # 真的活动满额时才拒绝，并且文件不会无限长
        for index in range(timers.MAX_ACTIVE - 1):
            timers.create(self.project, self.session, message="占位 %d" % index,
                          delay_seconds=3600, interval_seconds=0)
        with self.assertRaises(ValueError):
            timers.create(self.project, self.session, message="超了", delay_seconds=3600)
        stored = timers.list_timers(self.project, self.session, include_inactive=True)
        self.assertLessEqual(len(stored), timers.MAX_FILE_ROWS)

    def test_tool_requires_active_session(self):
        tokens = timers.bind("", "")
        try:
            result = tools.timer_set("message: 无会话")
            self.assertIn("没有活动会话", result)
        finally:
            timers.reset(tokens)

    def test_due_reminder_reaches_agent_context(self):
        timers.create(self.project, self.session, message="后台任务到期检查",
                      delay_seconds=1, interval_seconds=60)
        time.sleep(1.1)
        llm = _TimerLLM()
        agent = agent_mod.Agent(llm=llm, session_id=self.session, project_id=self.project)
        list(agent.run("继续", stream=True))
        self.assertTrue(llm.messages)
        joined = "\n".join(str(m.get("content") or "") for m in llm.messages[-1])
        self.assertIn("定时提醒", joined)
        self.assertIn("后台任务到期检查", joined)

    def test_timer_set_mid_turn_fires_inside_the_same_turn(self):
        """**回合内**放的定时器要在同一回合的下一步回灌。

        「等后台任务跑完」这个用法全押在注入点上：如果只在回合开头注入，模型在
        长回合里挂的定时器要等到用户下一次发言才响 —— 等于永远不响。
        """
        llm = _ScriptedLLM([
            "Thought: 先挂个等待\nAction: timer_set\n"
            "Action Input: message: 后台导出还没回来\ntask: 导出\n"
            "delay_seconds: 0\ninterval_seconds: 60\n",
            "Final Answer: 收到提醒，继续检查。",
        ])
        agent = agent_mod.Agent(llm=llm, session_id=self.session, project_id=self.project)
        events = list(agent.run("先起个后台导出", stream=True))
        notices = [event for event in events
                   if event.get("type") == "notice" and "定时提醒" in str(event.get("text") or "")]
        self.assertTrue(notices, "回合内设置的定时器没有在同一回合里回灌")
        self.assertIn("后台导出还没回来", notices[0]["text"])
        self.assertGreaterEqual(len(llm.messages), 2, "模型没走到第二步")
        second = "\n".join(str(m.get("content") or "") for m in llm.messages[1])
        self.assertIn("后台导出还没回来", second)
        self.assertIn("timer_cancel", second,
                      "提醒必须同时给出收尾办法，否则重复定时器永远挂着")


if __name__ == "__main__":
    unittest.main()
