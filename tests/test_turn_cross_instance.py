# -*- coding: utf-8 -*-
"""跨实例 turn 索引：owner 实例登记，其他实例据此找到转发目标。

事件缓冲仍在 owner 内存，B 的 /active、/partial、/stop 通过这个小索引找到
owner 并转发；索引只在注册/终态/30s 心跳时写。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402


class _Request:
    def __init__(self, base_url="http://127.0.0.1:8101/", headers=None):
        self.base_url = base_url
        self.headers = headers or {}


class TurnIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patch = patch.object(api, "STATE_ROOT", self.tmp)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        with api._IN_FLIGHT_TURNS_LOCK:
            api._IN_FLIGHT_TURNS.pop("t-index", None)
        api._remove_turn_index("t-index")

    def test_index_roundtrip_and_owner_lookup(self):
        rec = api._register_in_flight_turn(
            "t-index", "s", None, owner_base="http://127.0.0.1:8100")
        entry = api._read_turn_index("t-index")
        self.assertEqual(entry["session_id"], "s")
        self.assertEqual(entry["status"], "streaming")
        self.assertEqual(entry["owner_base"], "http://127.0.0.1:8100")
        self.assertEqual(api._list_turn_index("s", None)[0]["turn_id"], "t-index")
        self.assertEqual(
            api._remote_turn_owner("t-index", "s", _Request()), "http://127.0.0.1:8100")
        # 请求本身就在 owner 上，不能再转发；带转发标记也不能再跳。
        self.assertEqual(
            api._remote_turn_owner(
                "t-index", "s", _Request("http://127.0.0.1:8100/")), "")
        self.assertEqual(
            api._remote_turn_owner(
                "t-index", "s", _Request(headers={"x-docmind-forwarded": "1"})), "")

    def test_dead_owner_is_dropped_immediately(self):
        """owner 崩溃（存活锁可被抢到）→ active 不显示、转发也不去，直接删索引。"""
        entry = {
            "turn_id": "t-dead", "session_id": "s", "project_id": None,
            "owner_base": "http://127.0.0.1:8100", "owner_instance": "deadbeef",
            "status": "streaming", "created_at": time.time(), "updated_at": time.time(),
        }
        path = api._turn_index_path("t-dead")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry), encoding="utf-8")
        self.assertEqual(api._list_turn_index("s", None), [])
        self.assertFalse(path.exists())
        path.write_text(json.dumps(entry), encoding="utf-8")
        self.assertEqual(api._remote_turn_owner("t-dead", "s", _Request()), "")
        self.assertFalse(path.exists())

    def test_terminal_status_is_written_and_removed(self):
        rec = api._register_in_flight_turn(
            "t-index", "s", None, owner_base="http://127.0.0.1:8100")
        api._mark_in_flight_terminal(rec, "stopped")
        self.assertEqual(api._read_turn_index("t-index")["status"], "stopped")
        api._remove_turn_index("t-index", rec["created_at"])
        self.assertIsNone(api._read_turn_index("t-index"))


if __name__ == "__main__":
    unittest.main()
