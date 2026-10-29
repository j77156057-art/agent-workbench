# -*- coding: utf-8 -*-
"""刷新恢复（in-flight turn / partial）的注册表契约。

2026-10-29 体检发现三个问题：
- 断线（没走到 final）时终态仍是 `streaming` → /api/turn/active 把死回合当 in-flight，
  前端刷新后挂一条永远「正在执行」的幽灵消息；
- `_mark_in_flight_terminal` 建了**两个** Timer，第二个才是被 start 的，且不是 daemon；
- 旧回合的 60s 定时器按 turn_id 直接 pop，可能误删同 id 的新回合（前端会复用 id）。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402


class _FakeTimer:
    created: list = []

    def __init__(self, interval, fn):
        self.interval = interval
        self.fn = fn
        self.daemon = False
        self.started = False
        _FakeTimer.created.append(self)

    def start(self):
        self.started = True


class InFlightTurnRegistryTests(unittest.TestCase):
    def setUp(self):
        _FakeTimer.created = []
        self._ids = []

    def tearDown(self):
        with api._IN_FLIGHT_TURNS_LOCK:
            for turn_id in self._ids:
                api._IN_FLIGHT_TURNS.pop(turn_id, None)

    def _register(self, turn_id, session_id="s", project_id=None):
        self._ids.append(turn_id)
        return api._register_in_flight_turn(turn_id, session_id, project_id)

    def test_aborted_turn_is_not_left_streaming(self):
        rec = self._register("t-abort")
        api._mark_in_flight_terminal(rec, "streaming")
        self.assertEqual(rec["status"], "stopped",
                         "没有 final 的回合必须标成 stopped，否则前端会挂幽灵执行态")

    def test_terminal_state_keeps_done_and_error(self):
        for status in ("done", "error"):
            with self.subTest(status=status):
                rec = self._register("t-" + status)
                api._mark_in_flight_terminal(rec, status)
                self.assertEqual(rec["status"], status)

    def test_one_daemon_timer_is_created(self):
        rec = self._register("t-timer")
        with patch.object(api.threading, "Timer", _FakeTimer):
            api._mark_in_flight_terminal(rec, "done")
        self.assertEqual(len(_FakeTimer.created), 1, "只能建一个定时器")
        timer = _FakeTimer.created[0]
        self.assertTrue(timer.daemon, "定时器必须 daemon，否则退出时等 60s")
        self.assertTrue(timer.started)

    def test_stale_timer_does_not_evict_a_new_turn_with_the_same_id(self):
        old = self._register("t-same")
        with patch.object(api.threading, "Timer", _FakeTimer):
            api._mark_in_flight_terminal(old, "stopped")
        new = self._register("t-same")
        _FakeTimer.created[-1].fn()  # 旧回合的 60s 定时器到期
        self.assertIs(api._get_in_flight_turn("t-same"), new,
                      "旧定时器只能删自己那条记录")

    def test_consecutive_reasoning_fragments_are_coalesced(self):
        rec = self._register("t-reason")
        api._append_in_flight_event(rec, {"type": "reasoning", "text": "a"})
        api._append_in_flight_event(rec, {"type": "reasoning", "text": "b"})
        api._append_in_flight_event(rec, {"type": "thought", "text": "t"})
        api._append_in_flight_event(rec, {"type": "reasoning", "text": "c"})
        types = [ev["type"] for ev in rec["events"]]
        self.assertEqual(types, ["reasoning", "thought", "reasoning"])
        self.assertEqual(rec["events"][0]["text"], "ab")

    def test_active_list_is_ordered_by_creation_time(self):
        older = self._register("t-older")
        newer = self._register("t-newer")
        older["created_at"] = 100.0
        newer["created_at"] = 200.0
        # 插入顺序故意与时间相反（先旧后新也测；这里反转字典顺序）
        with api._IN_FLIGHT_TURNS_LOCK:
            api._IN_FLIGHT_TURNS["t-newer"] = newer
            api._IN_FLIGHT_TURNS["t-older"] = older
        items = api._list_in_flight_turns("s", None)
        ids = [item["turn_id"] for item in items]
        self.assertLess(ids.index("t-older"), ids.index("t-newer"))


if __name__ == "__main__":
    unittest.main()
