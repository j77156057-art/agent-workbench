# -*- coding: utf-8 -*-
"""内部记忆定时检查：run_check 落盘、可启动/停止、不因空库报错、排期按上次运行算。"""
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import config
from agent_runtime import maintenance


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_maint_")
        self._old = os.environ.get("DOCMIND_MEMORY_DIR")
        os.environ["DOCMIND_MEMORY_DIR"] = self.tmp

    def tearDown(self):
        maintenance.stop()
        if self._old is None:
            os.environ.pop("DOCMIND_MEMORY_DIR", None)
        else:
            os.environ["DOCMIND_MEMORY_DIR"] = self._old

    def test_run_check_records_last_status(self):
        with patch("config.get_memory_v2", return_value=True):
            result = maintenance.run_check("prj-empty")
        self.assertTrue(result["ok"])
        self.assertEqual(result["project"], "prj-empty")
        last = maintenance.last_status()
        self.assertEqual(last.get("project"), "prj-empty")
        self.assertIn("counts", last)
        self.assertIn("maintenance", last)

    def test_start_and_stop_are_idempotent(self):
        with patch("config.get_memory_check_interval", return_value=0):
            self.assertTrue(maintenance.start())
            self.assertFalse(maintenance.start())
            maintenance.stop()
            self.assertTrue(maintenance.start())
            maintenance.stop()

    def test_interval_setting_round_trip_in_runtime(self):
        old = config.get_runtime("memory_check_interval_minutes")
        try:
            config.set_runtime("memory_check_interval_minutes", 60)
            self.assertEqual(config.get_memory_check_interval(), 60)
            config.set_runtime("memory_check_interval_minutes", -5)
            self.assertEqual(config.get_memory_check_interval(), 0)
        finally:
            if old is None:
                config._RUNTIME.pop("memory_check_interval_minutes", None)
            else:
                config.set_runtime("memory_check_interval_minutes", old)

    def test_unconfigured_install_runs_it_by_default(self):
        """「功能交付了但默认 0」= 真实安装里永不运行。没存过值必须是**开**的。"""
        old = config.get_runtime("memory_check_interval_minutes")
        try:
            config._RUNTIME.pop("memory_check_interval_minutes", None)
            with patch("config.load_state", side_effect=lambda key, default=None: default):
                self.assertEqual(config.get_memory_check_interval(),
                                 config.DEFAULT_MEMORY_CHECK_INTERVAL)
                self.assertGreater(config.get_memory_check_interval(), 0)
        finally:
            if old is not None:
                config.set_runtime("memory_check_interval_minutes", old)

    def test_explicit_zero_still_means_off(self):
        """反向守卫：用户显式存 0 必须仍然关闭，默认值不许覆盖明确的选择。"""
        old = config.get_runtime("memory_check_interval_minutes")
        try:
            config._RUNTIME.pop("memory_check_interval_minutes", None)
            with patch("config.load_state", return_value=0):
                self.assertEqual(config.get_memory_check_interval(), 0)
        finally:
            if old is not None:
                config.set_runtime("memory_check_interval_minutes", old)


class ScheduleTests(unittest.TestCase):
    """排期按「上次真正跑过的时刻」算，不按「进程起来了多久」算。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_maint_sched_")
        self._old = os.environ.get("DOCMIND_MEMORY_DIR")
        os.environ["DOCMIND_MEMORY_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DOCMIND_MEMORY_DIR", None)
        else:
            os.environ["DOCMIND_MEMORY_DIR"] = self._old

    def _write_last(self, seconds_ago):
        ts = datetime.fromtimestamp(time.time() - seconds_ago, timezone.utc).isoformat(
            timespec="seconds")
        maintenance._write_last({"ok": True, "ts": ts, "project": "prj-x"})

    def test_never_ran_is_due_now(self):
        self.assertEqual(maintenance._seconds_to_next(1440), 0)
        self.assertEqual(maintenance._seconds_to_next(0), 0)

    def test_full_interval_waits(self):
        self._write_last(60)
        remaining = maintenance._seconds_to_next(1440)
        self.assertAlmostEqual(remaining, 1440 * 60 - 60, delta=5)

    def test_restart_does_not_reset_the_clock(self):
        """这条就是修掉的洞：两天前跑过一次，重启后不该又从头等 24 小时。"""
        self._write_last(2 * 86400)
        self.assertLessEqual(maintenance._seconds_to_next(1440), 0)

    def test_broken_timestamp_fails_open_to_a_check(self):
        """时间戳读不出来时宁可查一次，也不要静默再也不查。"""
        with open(maintenance._last_path(), "w", encoding="utf-8") as handle:
            handle.write('{"ok": true, "ts": "昨天"}')
        self.assertEqual(maintenance._seconds_to_next(1440), 0)

    def test_loop_checks_soon_after_start_when_overdue(self):
        """集成守卫：`_loop` 必须真的在超期时很快跑一次，而不是睡满一个间隔。

        只测 `_seconds_to_next` 不够 —— 接线写错（比如仍然 monotonic 起算）时纯函数
        测试照样全绿，而真实安装的 `maintenance-last.json` 依旧是空的。
        """
        with patch("config.get_memory_check_interval", return_value=1440), \
                patch("config.get_memory_v2", return_value=True), \
                patch.object(maintenance, "STARTUP_GRACE_SECONDS", 0), \
                patch.object(maintenance, "run_check",
                             wraps=maintenance.run_check) as spy:
            maintenance.start()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not spy.call_count:
                time.sleep(0.2)
            maintenance.stop()
        self.assertGreaterEqual(spy.call_count, 1, "服务起来后超期的检查没有真的发生")
        self.assertTrue(maintenance.last_status().get("ts"), "跑完要落下可查的时间戳")


if __name__ == "__main__":
    unittest.main()
