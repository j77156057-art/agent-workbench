import tempfile
import unittest

from agent_runtime.game_workflow import GameWorkflowManager


class WorkflowTimelineTests(unittest.TestCase):
    def test_timeline_keeps_longer_history_than_sse_window_and_survives_restart(self):
        root = tempfile.mkdtemp()
        manager = GameWorkflowManager(root)
        workflow_id = manager.start("记录长任务时间线") ["workflow_id"]
        state = manager._load(workflow_id)
        initial = len(state.timeline)
        for index in range(120):
            manager._event(state, "timeline_test", index=index)
        manager._save(state)

        current = manager.get(workflow_id)
        self.assertEqual(len(current["events"]), min(100, initial + 120))
        self.assertEqual(len(current["timeline"]), initial + 120)
        self.assertEqual(current["timeline"][-1]["index"], 119)

        restarted = GameWorkflowManager(root)
        recovered = restarted.get(workflow_id)
        self.assertEqual(len(recovered["timeline"]), initial + 120)
        self.assertEqual(recovered["timeline"][-1]["index"], 119)

    def test_event_seq_continues_after_restart(self):
        """重启后新事件必须接着持久台账编号，不能从 1 重来。

        SSE 重连带 `after=<最后看到的 seq>`；若新事件 seq 小于游标，
        `events_since` 会把它们全部过滤掉，前端表现为工作流不再更新。
        """
        root = tempfile.mkdtemp()
        manager = GameWorkflowManager(root)
        workflow_id = manager.start("重启后事件序号")["workflow_id"]
        state = manager._load(workflow_id)
        manager._event(state, "before_restart")
        manager._save(state)
        last_seq = max(int(ev["seq"]) for ev in state.events)

        restarted = GameWorkflowManager(root)
        reloaded = restarted._load(workflow_id, register=True)
        restarted._event(reloaded, "after_restart")
        new_seq = int(reloaded.events[-1]["seq"])
        self.assertGreater(new_seq, last_seq,
                           "新事件序号必须大于重启前台账的最后一个 seq")

        events, latest = restarted.events_since(workflow_id, after=last_seq)
        self.assertTrue(any(ev.get("kind") == "after_restart" for ev in events),
                        "after=旧最大 seq 的重连必须能收到重启后的新事件")
        self.assertGreaterEqual(latest, new_seq)


if __name__ == "__main__":
    unittest.main()
