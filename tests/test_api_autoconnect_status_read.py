"""`/api/mcp/autoconnect/register/status` 读会话文件的三条路径。

来历：SIM115 那批把这处 `json.load(open(path))` 改成 `with` 之后，CI 的改动行覆盖率门
点名 `api.py:7741-7742`「这一行改完没有任何用例执行过」。也就是说这条端点的读取分支
在 CI 里从没被走过 —— 读出来是坏的（比如把畸形 JSON 抛出去）也没人知道。
"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import api


def _status(**kwargs):
    return asyncio.run(api.mcp_autoconnect_register_status_ep(**kwargs))


class AutoconnectStatusReadTests(unittest.TestCase):
    def test_reads_the_file_and_filters_by_task_id(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "autoconnect_sessions.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"t1": {"state": "waiting", "tier": "L1"},
                           "t2": {"state": "done"}}, handle)
            with mock.patch.object(api.project_state, "path", return_value=path):
                out = _status(task_id="t1")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["state"], "waiting", out)
        self.assertEqual(out["tier"], "L1", "把别的任务的字段也带出来了：%r" % out)

    def test_missing_file_is_ok_and_empty(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(api.project_state, "path",
                                   return_value=os.path.join(root, "nope.json")):
                out = _status(task_id="t1")
        self.assertEqual(out, {"ok": True}, "文件不存在时不该报错，也不该编字段：%r" % out)

    def test_malformed_file_degrades_to_empty_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "autoconnect_sessions.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with mock.patch.object(api.project_state, "path", return_value=path):
                out = _status(task_id="t1")
        self.assertEqual(out, {"ok": True}, "畸形文件把端点抛炸了 / 或编出了字段：%r" % out)


if __name__ == "__main__":
    unittest.main()
