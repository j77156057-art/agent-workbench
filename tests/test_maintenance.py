# -*- coding: utf-8 -*-
"""内部记忆定时检查：run_check 落盘、可启动/停止、不因空库报错。"""
import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
