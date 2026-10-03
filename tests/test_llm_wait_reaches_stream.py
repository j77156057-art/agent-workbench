# -*- coding: utf-8 -*-
"""端到端：真跑一次子代理，断言 `llm_wait_*` 真的落在工作流事件流里。

## 为什么要有这条

前两次我都在**错的那一层**验证：

1. 第一次 spy 的是 `hooks.run_workflow` 本身——看到它被调用就判"通了"。
   而 `run_workflow` 第一行是 `if kind not in _WORKFLOW_KINDS: return`，
   不在那个元组里的 kind **静默丢弃、连事件都不发布**。真实跑一次工作流后，
   `llm_wait_*` 出现 **0 次**，静默度仍 99%。
2. 第二次把名字加进 `_WORKFLOW_KINDS` 后，仍然用 spy 那一层判断——
   于是又"通过"了。

**验「函数被调用」不是验「事件到了」。** 前者在调用边界，后者在用户看得到的
那条路。这条测试只认后者：跑真 Agent、走真 sink，然后读**持久化事件数组**。

## 它还钉住一件事

工具事件（`before_tool`/`after_tool`）是同一条链上已有工作的对照组。
如果将来这条链又断在本测试看不见的地方，这条会红，而工具事件那条不会。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402
from agent_runtime.game_workflow import GameWorkflowManager  # noqa: E402

ANSWER = "Thought: 看一下" + chr(10) + "Final Answer: 看完了。"


class _ScriptLLM:
    """一轮就收尾的假模型。它不需要联网，所以本测试是确定性的。"""

    provider = "fake"
    model = "script"
    last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
    last_tool_calls: list = []

    def clone(self):
        return _ScriptLLM()

    def chat(self, messages, stream=True, **kw):
        return (ch for ch in ANSWER)

    def count_tokens(self, text):
        return 1


class WaitEventsReachTheStreamTests(unittest.TestCase):
    def setUp(self):
        self.manager = GameWorkflowManager(tempfile.mkdtemp())
        state = self.manager.start("做一个 2D 游戏原型")
        self.wid = state["workflow_id"]
        self.manager.choose(self.wid, "recommended")
        self.manager.plan(self.wid, tasks=[{"id": "t1", "task": "查一下",
                                            "depends_on": []}])

    def _kinds(self):
        return [e.get("kind") for e in self.manager.get(self.wid)["events"]]

    def _run_child(self):
        """走真链路：真 Agent + 真 step_sink 接线 + 真 record_agent_step + 真持久事件流。

        `step_sink` **必须**自己接上——真实工作流里是 `game_workflow` 的 `runner`
        把它接到 `WORKFLOWS.record_agent_step(wid, task_id, role, item)` 的。
        第一版这条测试漏了接线，于是量的是一条**根本没人走的路**，红得莫名其妙。
        """
        def sink(item):
            self.manager.record_agent_step(self.wid, "t1", "researcher", item)

        return agent.Agent(llm=_ScriptLLM())._run_child(
            "researcher", "查一下东西", step_sink=sink)

    def test_llm_wait_start_reaches_the_persisted_event_stream(self):
        self._run_child()
        kinds = self._kinds()
        self.assertIn("llm_wait_start", kinds,
                      "llm_wait_start 没有落到持久事件流——时间线上仍会有大段静默。"
                      "已落地的 kind: %s" % sorted(set(kinds)))

    def test_llm_wait_end_reaches_the_persisted_event_stream(self):
        self._run_child()
        self.assertIn("llm_wait_end", self._kinds())

    def test_they_pair_up_in_the_stream(self):
        self._run_child()
        kinds = self._kinds()
        self.assertEqual(kinds.count("llm_wait_start"), kinds.count("llm_wait_end"),
                         "开始与结束不配对——「等待模型」会永远停在那里")

    def test_the_event_text_says_it_is_waiting_and_how_long(self):
        self._run_child()
        waits = [e for e in self.manager.get(self.wid)["events"]
                 if e.get("kind") == "llm_wait_end"]
        self.assertTrue(waits, "没有 llm_wait_end 事件")
        text = str(waits[0].get("text") or "")
        self.assertIn("模型已返回", text)
        self.assertIn("秒", text, "结束事件必须带耗时——否则界面算不出等了很久")

    def test_the_tool_events_still_arrive_as_a_control(self):
        """对照组：工具事件是同一条链上已有工作的成果。

        它通了而 llm_wait 没通，就说明断点在这两个 kind 之间的某处，
        而不是整条链都坏了。
        """
        self._run_child()
        # 本测试的假模型不调工具，所以这里只断言「工具事件这条通道没被弄坏」：
        # 直接用 record_agent_step 落一条，确认 sink 仍接受工具 kind。
        self.manager.record_agent_step(self.wid, "t1", "coder", {
            "type": "before_tool", "text": "调用前 · read_file"})
        self.assertIn("before_tool", self._kinds())

    def test_a_bare_agent_outside_a_workflow_does_not_crash(self):
        """没有 sink 时（普通问答主Agent）这两个事件必须安静地什么都不做。"""
        out = agent.Agent(llm=_ScriptLLM())._run_child("researcher", "查一下东西")
        self.assertIn(out.get("status"), {"ok", "degraded", "failed"})
        self.assertIsNotNone(out.get("conclusion"))


if __name__ == "__main__":
    unittest.main()