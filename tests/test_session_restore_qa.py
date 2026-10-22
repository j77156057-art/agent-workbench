# -*- coding: utf-8 -*-
"""QA 独立验证：会话历史回灌端点 GET /api/sessions/{session_id}（离线，不触网）。

与工程师自测 tests/test_session_restore.py 相互独立（本文件不 import 它），
额外覆盖：
  - 认证 GET 能逐条取回写入的 turns（长度 / user / assistant / ts 完全一致）；
  - 不存在 / 空 id → 200 + ok=True + 空 turns（不得 404）；
  - DELETE 与新 GET 同路径共存、互不覆盖：DELETE 真实落盘删除，之后 GET 变空；
  - OpenAPI schema 里同路径同时存在 GET 与 DELETE 两个 operation（证无静默覆盖）；
  - /api/sessions 列表仍可用，且删除后该会话从列表消失；
  - 会话 id 归一化（含特殊字符）后仍能往返。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
import sessions  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _methods_for_path(path):
    """收集 app 路由表里某路径上已注册的 HTTP 方法集合。"""
    out = set()
    for route in api.app.routes:
        if getattr(route, "path", "") == path:
            for m in (getattr(route, "methods", set()) or set()):
                if m not in ("HEAD", "OPTIONS"):
                    out.add(m)
    return out


class SessionRestoreQATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_sess_qa_")
        self._old_dir = sessions.SESSIONS_DIR
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "sessions")
        self.client = TestClient(api.app)
        self.sid = "web-qa-0001"

    def tearDown(self):
        sessions.SESSIONS_DIR = self._old_dir

    # ---- A1: 逐条比对取回的 turns ----
    def test_get_returns_exact_turns(self):
        turns = [
            {"user": "Q1 数值在哪", "assistant": "A1 在 player_stats.gd:12"},
            {"user": "Q2 受击逻辑", "assistant": "A2 见 damage_calc.gd:34"},
            {"user": "Q3 入口场景", "assistant": "A3 main.tscn"},
        ]
        self.assertTrue(sessions.save(self.sid, turns, "SUM-X"))

        r = self.client.get("/api/sessions/%s" % self.sid)
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertTrue(b["ok"])
        self.assertEqual(b["session_id"], self.sid)
        self.assertEqual(b["summary"], "SUM-X")
        self.assertTrue(b["updated_at"])
        got = b["turns"]
        self.assertEqual(len(got), len(turns))
        for exp, act in zip(turns, got):
            self.assertEqual(act["user"], exp["user"])
            self.assertEqual(act["assistant"], exp["assistant"])
            self.assertTrue(act.get("ts"), "ts 应由 sessions.save 补全并原样返回")

    # ---- A2: 不存在 id 不得 404 ----
    def test_missing_id_is_200_empty_not_404(self):
        r = self.client.get("/api/sessions/web-does-not-exist")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertTrue(b["ok"])
        self.assertEqual(b["turns"], [])
        self.assertEqual(b["summary"], "")

    # ---- A3: DELETE 真实可达且未被 GET 覆盖 ----
    def test_delete_really_reachable_and_deletes_file(self):
        sessions.save(self.sid, [{"user": "u", "assistant": "a"}])
        path = sessions._path(self.sid)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(len(self.client.get("/api/sessions/%s" % self.sid).json()["turns"]), 1)

        d = self.client.delete("/api/sessions/%s" % self.sid)
        self.assertEqual(d.status_code, 200, d.text)
        self.assertTrue(d.json()["ok"])
        self.assertFalse(os.path.isfile(path), "DELETE 应真实删除磁盘文件")
        self.assertEqual(self.client.get("/api/sessions/%s" % self.sid).json()["turns"], [])

    # ---- A4: 路由表 + OpenAPI 双证 GET/DELETE 共存 ----
    def test_route_table_and_openapi_have_both_methods(self):
        path = "/api/sessions/{session_id}"
        self.assertEqual(_methods_for_path(path), {"GET", "DELETE"},
                         "同路径应同时注册 GET 与 DELETE，未互相覆盖")
        schema = self.client.get("/openapi.json").json()
        ops = schema["paths"].get(path, {})
        self.assertIn("get", ops)
        self.assertIn("delete", ops)
        # GET 与 DELETE 必须是不同 handler（防同函数被重复挂到两个方法）
        get_ids = {r.endpoint.__name__ for r in api.app.routes
                   if getattr(r, "path", "") == path and "GET" in (getattr(r, "methods", set()) or set())}
        del_ids = {r.endpoint.__name__ for r in api.app.routes
                   if getattr(r, "path", "") == path and "DELETE" in (getattr(r, "methods", set()) or set())}
        self.assertTrue(get_ids and del_ids and get_ids != del_ids)

    # ---- A5: 列表端点仍可用，删除后消失 ----
    def test_list_endpoint_still_works_and_reflects_delete(self):
        # ⚠ 列表端点按**当前项目**过滤（`api.py`：`project_id=_request_project_id() or None`），
        # 而这条夹具存的会话是**无归属**的（`sessions.save` 不写归属，归属由
        # `bind_code_root()` 绑）。于是「当前项目」一旦非空（CI 上 checkout 本身
        # 就成了一个项目），它就被过滤掉 —— 这条测试的成败取决于**跑它之前别人
        # 在状态文件里留了什么**。
        # 钉住「当前项目」为空 = 这条测的确实是它要测的两件事：
        # 列表端点可用、删完即消失。
        with patch.object(api, "_request_project_id", return_value=""):
            sessions.save(self.sid, [{"user": "u1", "assistant": "a1"},
                                     {"user": "u2", "assistant": "a2"}])
            items = self.client.get("/api/sessions").json()["items"]
            found = [x for x in items if x["session_id"] == self.sid]
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["turns"], 2)
            self.assertEqual(found[0]["title"], "u1")
            self.assertEqual(found[0]["preview"], "u1")

            self.client.delete("/api/sessions/%s" % self.sid)
            items2 = self.client.get("/api/sessions").json()["items"]
            self.assertEqual([x for x in items2 if x["session_id"] == self.sid], [])

    # ---- A6: 真实形态（web-<hex>）的 id 往返一致；含点的 id 也可 ----
    def test_safe_id_roundtrip(self):
        for sid in ("web-qa.2", "web-3f9a1b20c4d5"):
            self.assertTrue(sessions.save(sid, [{"user": "x", "assistant": "y"}]))
            b = self.client.get("/api/sessions/%s" % sid).json()
            self.assertEqual(len(b["turns"]), 1, sid)
            self.assertEqual(b["turns"][0]["user"], "x", sid)


if __name__ == "__main__":
    unittest.main()
