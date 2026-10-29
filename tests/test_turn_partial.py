# -*- coding: utf-8 -*-
"""刷新恢复（in-flight turn / partial）的注册表契约。

2026-10-29 体检发现三个问题：
- 断线（没走到 final）时终态仍是 `streaming` → /api/turn/active 把死回合当 in-flight，
  前端刷新后挂一条永远「正在执行」的幽灵消息；
- `_mark_in_flight_terminal` 建了**两个** Timer，第二个才是被 start 的，且不是 daemon；
- 旧回合的 60s 定时器按 turn_id 直接 pop，可能误删同 id 的新回合（前端会复用 id）。
"""
import asyncio
import os
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


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

    def _call_partial(self, turn_id, session_id="s", project_id=None):
        with patch.object(api, "_http_origin_allowed", return_value=True), \
                patch.object(api, "_ctx_project_id", return_value=project_id):
            return asyncio.run(api.get_turn_partial(turn_id, session_id=session_id))

    def test_partial_requires_matching_session_and_project(self):
        self._register("t-own", session_id="s", project_id="p1")
        ok = self._call_partial("t-own", "s", "p1")
        self.assertIsInstance(ok, dict)
        self.assertTrue(ok["ok"])
        for sid, pid in (("other", "p1"), ("s", "p2"), ("s", None), ("", "p1")):
            with self.subTest(session=sid, project=pid):
                resp = self._call_partial("t-own", sid, pid)
                self.assertEqual(resp.status_code, 404)
                self.assertIn("不存在或已过期", resp.body.decode("utf-8"),
                              "归属不匹配不能泄露「存在但属于别人」")

    def test_missing_turn_still_404(self):
        resp = self._call_partial("t-missing", "s", None)
        self.assertEqual(resp.status_code, 404)

    def test_stop_endpoint_sets_the_event_and_checks_ownership(self):
        import threading
        rec = self._register("t-stop", session_id="s", project_id="p1")
        rec["stop"] = threading.Event()
        with patch.object(api, "_http_origin_allowed", return_value=True), \
                patch.object(api, "_ctx_project_id", return_value="p1"):
            ok = asyncio.run(api.stop_turn("t-stop", session_id="s"))
            self.assertTrue(ok["ok"])
            self.assertTrue(rec["stop"].is_set())
            bad = asyncio.run(api.stop_turn("t-stop", session_id="other"))
            self.assertEqual(bad.status_code, 404)

    def test_frontend_stop_calls_the_server_endpoint(self):
        chat_ts = (ROOT / "frontend/src/workbench/api/chat.ts").read_text(encoding="utf-8")
        block = chat_ts[chat_ts.index("stopTurn"):chat_ts.index("stopTurn") + 400]
        self.assertIn("/stop", block)
        self.assertIn("session_id", block)
        dock = (ROOT / "frontend/src/workbench/components/ChatDock.vue").read_text(
            encoding="utf-8")
        self.assertIn("aiApi.stopTurn", dock)
        ask = (ROOT / "frontend/src/ask/AskApp.vue").read_text(encoding="utf-8")
        self.assertIn("aiApi.stopTurn", ask)

    def test_workflow_stop_notice_does_not_promise_auto_interrupt(self):
        """真机核对：停止只停本轮对话，不会中断已创建的工作流（源码里也没有该调用）。

        旧提示写「若服务端已经创建，会自动中断」——实测工作流仍是 awaiting_choice，
        卡片也还在。提示必须说清它不会被中断、可从卡片/历史继续或中断；同时补探
        孤儿工作流，覆盖「创建了但卡片没挂上」的窗口。
        """
        dock = (ROOT / "frontend/src/workbench/components/ChatDock.vue").read_text(
            encoding="utf-8")
        start = dock.index("function stop()")
        block = dock[start:dock.index("\n}\n", start) + 3]
        self.assertNotIn("workflowInterrupt", block, "stop() 不应擅自中断工作流")
        self.assertNotIn("会自动中断。", block, "停止不会自动中断工作流，提示不能这样写")
        self.assertIn("不会被自动中断", block)
        self.assertIn("工作流历史", block)
        self.assertIn("detectOrphanWorkflow", block,
                      "卡片没挂上时要探测孤儿工作流，否则它会静默拦截下一次工作流请求")

    def test_frontend_passes_session_id_to_partial(self):
        chat_ts = (ROOT / "frontend/src/workbench/api/chat.ts").read_text(encoding="utf-8")
        block = chat_ts[chat_ts.index("getTurnPartial"):chat_ts.index("getTurnPartial") + 500]
        self.assertIn("session_id", block)
        dock = (ROOT / "frontend/src/workbench/components/ChatDock.vue").read_text(
            encoding="utf-8")
        self.assertIn("getTurnPartial(target.turn_id, sid)", dock)

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
