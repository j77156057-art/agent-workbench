"""心跳打点、锁竞争、三档阈值与 progress 冻结的单元测试。"""
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agent_runtime import delegation
from file_locks import cross_process_lock


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = delegation._DIR
        delegation._DIR = Path(self._tmp.name) / "delegations"
        delegation._ROOTS.clear()
        self._env = patch.dict(os.environ, {
            "DOCMIND_HEARTBEAT_INTERVAL_S": "10",
            "DOCMIND_HEARTBEAT_STALE_FACTOR": "3",
            "DOCMIND_HEARTBEAT_DEAD_FACTOR": "6",
            "DOCMIND_HEARTBEAT_PROGRESS_FREEZE_FACTOR": "3",
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        delegation._DIR = self._old_dir
        delegation._ROOTS.clear()
        self._tmp.cleanup()

    def _payload(self, session):
        path = delegation._session_path(session)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_beat_updates_last_seen_beats_and_progress(self):
        self.assertTrue(delegation.beat("hb-session", "ag-1", create=True,
                                        phase="started", event="run_start", progress={"steps": 0}))
        first = self._payload("hb-session")["nodes"][0]
        first_seen = first["last_seen_epoch"]
        self.assertEqual(first["beats"], 1)
        self.assertEqual(first["progress"]["steps"], 0)

        time.sleep(0.05)
        self.assertTrue(delegation.beat("hb-session", "ag-1", phase="tool",
                                        event="read_file:ok", progress={"steps": 2}))
        second = self._payload("hb-session")["nodes"][0]
        self.assertEqual(second["beats"], 2)
        self.assertGreater(second["last_seen_epoch"], first_seen)
        self.assertEqual(second["progress"]["steps"], 2)
        self.assertEqual(second["progress"]["phase"], "tool")
        self.assertEqual(len(second["recent_beats"]), 2)

    def test_concurrent_beats_do_not_lose_updates(self):
        delegation.beat("hb-race", "ag-race", create=True, phase="start", event="run_start")

        def worker(_):
            return delegation.beat("hb-race", "ag-race", phase="tool",
                                   event="concurrent", progress={"steps": 1})

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(24)))
        self.assertTrue(all(results), "并发 beat 不应因撞锁失败")
        node = self._payload("hb-race")["nodes"][0]
        self.assertEqual(node["beats"], 25, "每次 beat 都必须落盘，撞锁时不得丢更新")
        self.assertEqual(len(node["recent_beats"]), delegation._RECENT_BEATS)

    def test_beat_waits_out_a_held_lock_instead_of_losing_update(self):
        delegation.beat("hb-lock", "ag-lock", create=True, phase="start", event="run_start")
        path = delegation._session_path("hb-lock")
        acquired = threading.Event()
        results = []

        def beat_worker():
            acquired.set()
            results.append(delegation.beat("hb-lock", "ag-lock", phase="tool",
                                           event="blocked_then_retried",
                                           progress={"steps": 3}))

        with cross_process_lock(path, timeout=10):
            thread = threading.Thread(target=beat_worker)
            thread.start()
            acquired.wait(timeout=2)
            time.sleep(0.4)
        thread.join(timeout=15)
        self.assertTrue(results and results[0], "锁释放后 beat 必须成功")
        node = self._payload("hb-lock")["nodes"][0]
        self.assertEqual(node["beats"], 2)
        self.assertEqual(node["progress"]["steps"], 3)

    def test_no_heartbeat_is_explicitly_none_not_zero_seconds(self):
        view = delegation.heartbeat_view({"status": "running"})
        self.assertEqual(view["state"], "none")
        self.assertIsNone(view["silence_seconds"])
        self.assertEqual(view["reason"], "无心跳记录")
        tree = delegation.tree("hb-empty")
        self.assertEqual(tree["nodes"], [tree["root"]])
        self.assertNotIn("heartbeat", tree["root"], "无记录时不得伪造心跳对象")

    def test_progress_frozen_while_clock_keeps_running(self):
        now = time.time()
        row = {
            "status": "running",
            "last_seen": delegation._iso(now - 2),
            "last_seen_epoch": now - 2,
            "heartbeat_interval": 10,
            "beats": 6,
            "progress": {"steps": 3, "phase": "tool",
                         "changed_at": delegation._iso(now - 45),
                         "changed_at_epoch": now - 45},
        }
        view = delegation.heartbeat_view(row, now=now)
        self.assertEqual(view["state"], "alive")
        self.assertTrue(view["progress_frozen"])
        self.assertGreaterEqual(view["progress_age_seconds"], 30)

    def test_three_tier_boundaries_use_interval_multiples(self):
        now = time.time()
        base = {"status": "running", "heartbeat_interval": 10}

        def state_at(silence):
            row = {**base, "last_seen": delegation._iso(now - silence),
                   "last_seen_epoch": now - silence,
                   "progress": {"steps": 1, "phase": "tool",
                                "changed_at": delegation._iso(now - silence),
                                "changed_at_epoch": now - silence}}
            return delegation.heartbeat_view(row, now=now)["state"]

        self.assertEqual(state_at(30), "alive", "30s = 10s×3，边界仍算 alive")
        self.assertEqual(state_at(30.5), "stale")
        self.assertEqual(state_at(60), "stale", "60s = 10s×6，边界仍算 stale")
        self.assertEqual(state_at(60.5), "dead")
        self.assertEqual(state_at(0.2), "alive")

    def test_terminal_node_is_closed_not_dead(self):
        now = time.time()
        row = {"status": "completed", "last_seen_epoch": now - 120,
               "last_seen": delegation._iso(now - 120), "heartbeat_interval": 10,
               "progress": {"steps": 4, "phase": "completed",
                            "changed_at_epoch": now - 120}}
        self.assertEqual(delegation.heartbeat_view(row, now=now)["state"], "closed")

    def test_tree_sees_brand_new_disk_writes_from_another_process(self):
        """读路径缓存早于节点创建时，另一个进程写入的心跳仍必须可见。"""
        empty = delegation.tree("hb-xproc")
        self.assertEqual(empty["nodes"], [empty["root"]])
        now = time.time()
        payload = {"schema": 1, "session_id": "hb-xproc", "nodes": [{
            "id": "ag-xproc", "ticket_id": "ag-xproc", "session_id": "hb-xproc",
            "parent_id": "main", "role": "researcher", "status": "running",
            "created_at": delegation._iso(now), "created_at_epoch": now,
            "last_seen": delegation._iso(now), "last_seen_epoch": now,
            "heartbeat_interval": 10, "beats": 4,
            "progress": {"steps": 1, "phase": "thinking",
                         "changed_at": delegation._iso(now - 5),
                         "changed_at_epoch": now - 5},
            "recent_beats": [],
        }], "alerts": []}
        with cross_process_lock(delegation._session_path("hb-xproc"), timeout=10):
            delegation._atomic_write(delegation._session_path("hb-xproc"), payload)
        tree = delegation.tree("hb-xproc")
        self.assertEqual(len(tree["nodes"]), 2, "跨进程写入的子节点必须进入树")
        child = tree["nodes"][1]
        self.assertEqual(child["id"], "ag-xproc")
        self.assertEqual(child["heartbeat"]["state"], "alive")
        self.assertEqual(child["heartbeat"]["beats"], 4)

    def test_dead_node_produces_alert_with_required_fields(self):
        now = time.time()
        delegation.beat("hb-dead", "ag-dead", create=True, phase="tool",
                        event="read_file:ok", progress={"steps": 2})
        payload = self._payload("hb-dead")
        node = payload["nodes"][0]
        node["status"] = "active"
        node["last_seen_epoch"] = now - 90
        node["last_seen"] = delegation._iso(now - 90)
        node["progress"] = {**node["progress"], "changed_at_epoch": now - 90,
                            "changed_at": delegation._iso(now - 90)}
        with cross_process_lock(delegation._session_path("hb-dead"), timeout=10):
            delegation._atomic_write(delegation._session_path("hb-dead"), payload)
        delegation._ROOTS.clear()

        tree = delegation.tree("hb-dead")
        self.assertEqual(tree["nodes"][1]["heartbeat"]["state"], "dead")
        self.assertEqual(len(tree["alerts"]), 1)
        alert = tree["alerts"][0]
        for key in ("agent_id", "last_seen", "silence_seconds", "progress_frozen"):
            self.assertIn(key, alert)
        self.assertEqual(alert["agent_id"], "ag-dead")
        self.assertTrue(alert["progress_frozen"])
        self.assertEqual(alert["received_by"], "platform_assistant")
        persisted = self._payload("hb-dead")["alerts"]
        self.assertEqual(len(persisted), 1, "失联报告必须真实落盘，而不是前端模拟")
        # 同一 last_seen 不重复报告。
        self.assertEqual(len(delegation.tree("hb-dead")["alerts"]), 1)


if __name__ == "__main__":
    unittest.main()
