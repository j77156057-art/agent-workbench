# -*- coding: utf-8 -*-
"""定时提醒主动推送进语音会话（2026-11-17 推送增量）。

此前到期提醒只在模型下一次准备发言时注入文本trail：语音会话静音听答时
提醒永远不到达。增量：桥加 server outbox + 会话注册表，agent 到期时向活跃桥
推一条 model.delta final 收口文本；无活跃语音会话退回纯文本注入（不假装有推送）。
"""
import unittest
from unittest.mock import patch

from agent_runtime import realtime_bridge, timers
from tests.test_agent_trace import _FakeLLM, _IsoBase


class ReminderPushTests(_IsoBase):
    def test_bridge_outbox_delivers_and_unregisters(self):
        bridge = realtime_bridge.SessionBridge("prj-push")
        bridge.bind_session("s-push")
        try:
            self.assertTrue(realtime_bridge.push_reminder("s-push", "提醒正文"))
            events = bridge.next_events()
            self.assertEqual(len(events), 1)
            ev = events[0]
            self.assertEqual(ev.get("type"), "model.delta")
            self.assertTrue(ev.get("final"))
            self.assertIn("提醒正文", str(ev.get("text")))
            self.assertEqual(bridge.next_events(), [], "出箱应一次泵空")
            self.assertFalse(realtime_bridge.push_reminder("s-other", "x"),
                             "未绑定会话不应假装有推送")
        finally:
            bridge.close()
        self.assertFalse(realtime_bridge.push_reminder("s-push", "x"),
                         "close 后注册表应注销")

    def test_agent_due_reminder_pushes_to_active_bridge(self):
        import agent as agent_mod
        pushed = []
        fake = _FakeLLM(["Final Answer: ok"])
        a = agent_mod.Agent(llm=fake, session_id="s-push-agent")
        with patch.object(timers, "due", return_value=[
                {"message": "喝水", "reminder": "定时提醒：喝水"}]), \
                patch.object(realtime_bridge, "push_reminder",
                             side_effect=lambda sid, text: pushed.append((sid, text)) or True):
            events = list(a.run("hi", stream=True))
        self.assertTrue(any(e.get("type") == "notice" and "喝水" in str(e.get("text"))
                            for e in events), "文本 notice 仍应存在")
        self.assertEqual(pushed, [("s-push-agent", "定时提醒：喝水")],
                         "到期提醒应推给活跃语音会话")


if __name__ == "__main__":
    unittest.main(verbosity=2)