# -*- coding: utf-8 -*-
"""P3 端点项目化回归：contextvar 路由 + 项目 CRUD + 会话按项目（全部离线）。

核心机制：`api.project_context_middleware` 把 /api/* 请求按 `X-DocMind-Project`
（或 `?project_id=`）绑定到某个项目，`config.set_context_code_root(root)` 把该 root
写进请求级 ContextVar；于是 tools / regions / workbench_fs / agent 等几十处既有
`get_runtime('code_root')` 调用零改动即自动项目化。**不带项目头 + 无当前项目时行为与
改动前完全一致**（沿用全局 code_root）——这是本期的生命线。

覆盖：
① 中间件路由：header / query 命中对应项目 root；header 优先于 query；非 /api/ 路径不受影响；
② 非法头回落：未登记 pid → 回落「当前项目」；无当前项目 → 沿用全局（不返回 400）；
③ 上下文隔离：连续请求互不串台；请求结束不留残留上下文；
④ threadpool 传播：`run_in_threadpool` 内仍读到本请求 root（真实端点大量使用）；
⑤ daemon 线程不继承：独立线程读不到请求 root（拿到全局）——这是**正确**行为；
⑥ 项目 CRUD：GET/POST/PATCH/DELETE + 400/404 边界；
⑦ 会话按项目：/api/sessions 与 /api/sessions/{id} 按请求项目隔离；
⑧ 显式优先不被破坏：set_runtime('code_root') 不被持久化/请求上下文覆盖。

隔离手段：把 `config.STATE_FILE` / `sessions.SESSIONS_DIR` 指到临时目录，快照/还原
`config._RUNTIME`，特性开关 `DOCMIND_PROJECTS=1`（动态读取）。
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import projects  # noqa: E402
import sessions  # noqa: E402
import api  # noqa: E402


def _build_probe_app():
    """构造最小探针应用：复用**生产用**的 api.project_context_middleware。

    这样能在不牵动 api.py 全局 router（避免污染其它测试）的前提下，验证真实中间件行为。
    """
    from fastapi import FastAPI
    from starlette.concurrency import run_in_threadpool
    from starlette.middleware.base import BaseHTTPMiddleware

    probe = FastAPI()
    probe.add_middleware(BaseHTTPMiddleware, dispatch=api.project_context_middleware)

    @probe.get("/api/__probe__")
    async def _p():
        return {"code_root": config.get_runtime("code_root"), "pid": api._ctx_project_id()}

    @probe.get("/api/__probe_threadpool__")
    async def _pt():
        # 真实端点里大量 await run_in_threadpool(…) 包裹同步逻辑，必须仍读到请求 root
        v = await run_in_threadpool(lambda: config.get_runtime("code_root"))
        return {"code_root": v}

    @probe.get("/api/__probe_daemon__")
    async def _pd():
        box = {}

        def _worker():
            box["code_root"] = config.get_runtime("code_root")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=5)
        return {"code_root": box.get("code_root")}

    @probe.get("/__probe_non_api__")
    async def _np():
        # 非 /api/ 路径：中间件应完全跳过 → 永远读全局
        return {"code_root": config.get_runtime("code_root"), "pid": api._ctx_project_id()}

    return probe


class RoutingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_routing_")
        self._old_state_file = config.STATE_FILE
        self._old_sessions_dir = sessions.SESSIONS_DIR
        self._old_runtime = dict(config._RUNTIME)
        # ⚠ 会话级 code_root 上下文（2026-10-10 新增，`get_runtime` 里优先级最高）。
        # 必须与其他状态一样存-还：否则**上一个用例**残留的会话级值会盖过
        # setUp 刚设的 `_RUNTIME["code_root"]`，断言就会读到别的用例的临时目录。
        # 这个测试文件此前没有这一层可清，所以它当时是对的；加了层就得一起清。
        self._old_session_ctx = config._CTX_SESSION_CODE_ROOT.get()
        config._CTX_SESSION_CODE_ROOT.set(None)
        config.STATE_FILE = os.path.join(self.tmp, ".docmind_state.json")
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "sessions")
        self._env = mock.patch.dict(os.environ, {"DOCMIND_PROJECTS": "1"})
        self._env.start()

        self.root_a = self._mkdir("Alpha")
        self.root_b = self._mkdir("Beta")
        self.global_root = self._mkdir("Global")
        self.pid_a = projects.ensure_project(self.root_a, "Alpha")
        self.pid_b = projects.ensure_project(self.root_b, "Beta")
        self.assertTrue(projects.set_current(self.pid_a))
        # 全局 code_root 设为独立目录：既非 A 也非 B，便于区分「请求上下文」与「全局」
        config.set_runtime("code_root", self.global_root)

    def tearDown(self):
        self._env.stop()
        config.STATE_FILE = self._old_state_file
        sessions.SESSIONS_DIR = self._old_sessions_dir
        config._RUNTIME.clear()
        config._RUNTIME.update(self._old_runtime)
        # 还原会话级上下文（与 setUp 里置空对称）
        config._CTX_SESSION_CODE_ROOT.set(self._old_session_ctx)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mkdir(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return d

    def _root_of(self, pid):
        return projects.get_project(pid)["root"]


class MiddlewareRoutingTests(RoutingBase):
    """① 中间件路由。"""

    def setUp(self):
        super().setUp()
        from starlette.testclient import TestClient
        self.client = TestClient(_build_probe_app())

    def test_chat_agent_worker_keeps_selected_project(self):
        """StreamingResponse + plain Thread must both retain the selected root."""
        from starlette.testclient import TestClient

        old_provider = config.get_runtime("llm_provider")
        old_embedding = config.get_runtime("embedding_provider")
        config.set_runtime("llm_provider", "mock")
        config.set_runtime("embedding_provider", "mock")

        def fake_run(_agent, *_args, **_kwargs):
            root = config.get_runtime("code_root")
            yield {"type": "final", "text": root}

        try:
            with mock.patch.object(api.Agent, "run", fake_run):
                response = TestClient(api.app).post(
                    "/api/chat", data={"question": "介绍这个项目"},
                    headers={"X-DocMind-Project": self.pid_b},
                )
            self.assertEqual(response.status_code, 200, response.text)
            import json
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ")]
            final = next(event for event in events if event.get("type") == "final")
            self.assertEqual(final["text"], self.root_b)
            self.assertNotIn("模型仍在处理", response.text)
        finally:
            config.set_runtime("llm_provider", old_provider)
            config.set_runtime("embedding_provider", old_embedding)

    def test_header_routes_to_project_root(self):
        r = self.client.get("/api/__probe__", headers={"X-DocMind-Project": self.pid_a})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["pid"], self.pid_a)
        self.assertEqual(r.json()["code_root"], self._root_of(self.pid_a))

    def test_header_b_routes_to_b(self):
        r = self.client.get("/api/__probe__", headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(r.json()["pid"], self.pid_b)
        self.assertEqual(r.json()["code_root"], self._root_of(self.pid_b))

    def test_query_param_routes(self):
        r = self.client.get("/api/__probe__", params={"project_id": self.pid_b})
        self.assertEqual(r.json()["pid"], self.pid_b)
        self.assertEqual(r.json()["code_root"], self._root_of(self.pid_b))

    def test_header_priority_over_query(self):
        r = self.client.get("/api/__probe__", params={"project_id": self.pid_b},
                            headers={"X-DocMind-Project": self.pid_a})
        self.assertEqual(r.json()["pid"], self.pid_a, "header 必须优先于 query")
        self.assertEqual(r.json()["code_root"], self._root_of(self.pid_a))

    def test_non_api_path_is_untouched(self):
        r = self.client.get("/__probe_non_api__", headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(r.json()["pid"], "", "非 /api/ 路径不应被绑定到项目")
        self.assertEqual(r.json()["code_root"], self.global_root)


class IllegalHeaderFallbackTests(RoutingBase):
    """② 非法头回落（不 400）。"""

    def setUp(self):
        super().setUp()
        from starlette.testclient import TestClient
        self.client = TestClient(_build_probe_app())

    def test_unknown_pid_falls_back_to_current(self):
        r = self.client.get("/api/__probe__", headers={"X-DocMind-Project": "prj-doesnotexist"})
        self.assertEqual(r.status_code, 200, "非法头不得返回 400，应回落当前项目")
        self.assertEqual(r.json()["pid"], self.pid_a)
        self.assertEqual(r.json()["code_root"], self._root_of(self.pid_a))

    def test_no_project_and_no_current_uses_global(self):
        # 清空注册表 → 无当前项目
        config.save_state("projects", {})
        config.save_state("current_project_id", "")
        r = self.client.get("/api/__probe__", headers={"X-DocMind-Project": "prj-nope"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["pid"], "")
        self.assertEqual(r.json()["code_root"], self.global_root,
                         "无当前项目时必须沿用全局 code_root（生命线）")


class ContextIsolationTests(RoutingBase):
    """③ 上下文隔离与无泄漏。"""

    def setUp(self):
        super().setUp()
        from starlette.testclient import TestClient
        self.client = TestClient(_build_probe_app())

    def test_sequential_requests_do_not_bleed(self):
        b = self.client.get("/api/__probe__", headers={"X-DocMind-Project": self.pid_b}).json()
        self.assertEqual(b["pid"], self.pid_b)
        # 紧随其后、不带头的请求应回到「当前项目」A，而不是上一条的 B
        none = self.client.get("/api/__probe__").json()
        self.assertEqual(none["pid"], self.pid_a, "上一条请求的上下文不得串到本请求")
        self.assertEqual(none["code_root"], self._root_of(self.pid_a))

    def test_no_context_leak_on_caller_thread(self):
        self.client.get("/api/__probe__", headers={"X-DocMind-Project": self.pid_b})
        # 请求在独立事件循环线程中执行；调用方（主）线程上下文不得被污染
        self.assertEqual(api._ctx_project_id(), "")
        self.assertIsNone(config._CTX_CODE_ROOT.get())
        self.assertEqual(config.get_runtime("code_root"), self.global_root)


class ThreadPropagationTests(RoutingBase):
    """④ threadpool 传播 / ⑤ daemon 线程不继承。"""

    def setUp(self):
        super().setUp()
        from starlette.testclient import TestClient
        self.client = TestClient(_build_probe_app())

    def test_threadpool_inherits_request_context(self):
        r = self.client.get("/api/__probe_threadpool__", headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(r.json()["code_root"], self._root_of(self.pid_b),
                         "run_in_threadpool 内必须仍读到本请求 root")

    def test_daemon_thread_does_not_inherit_request_context(self):
        r = self.client.get("/api/__probe_daemon__", headers={"X-DocMind-Project": self.pid_b})
        got = r.json()["code_root"]
        self.assertEqual(got, self.global_root,
                         "独立 daemon 线程不应继承请求上下文（应读全局）")
        self.assertNotEqual(got, self._root_of(self.pid_b))


class ProjectCrudTests(RoutingBase):
    """⑥ 项目 CRUD（走真实 api.app）。"""

    def setUp(self):
        super().setUp()
        from starlette.testclient import TestClient
        # 不吃真引擎状态
        self._eng = mock.patch.object(api, "engine_running_roots", return_value=[])
        self._eng.start()
        self.client = TestClient(api.app)

    def tearDown(self):
        self._eng.stop()
        super().tearDown()

    def test_list_includes_registered_and_current(self):
        data = self.client.get("/api/projects").json()
        self.assertTrue(data["ok"])
        ids = {p["project_id"] for p in data["projects"]}
        self.assertIn(self.pid_a, ids)
        self.assertIn(self.pid_b, ids)
        self.assertEqual(data["current"], self.pid_a)

    def test_create_activate_rename_delete_roundtrip(self):
        new_root = self._mkdir("Gamma")
        r = self.client.post("/api/projects", json={"root": new_root, "name": "GammaP"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        pid = body["project_id"]
        self.assertEqual(pid, projects.project_id(new_root))
        self.assertEqual(body["code_root"], os.path.abspath(new_root))
        self.assertEqual(config.get_runtime("code_root"), os.path.abspath(new_root),
                         "创建即激活：全局 code_root 应同步到新项目")

        a = self.client.post(f"/api/projects/{pid}/activate")
        self.assertEqual(a.status_code, 200, a.text)
        self.assertEqual(projects.current_project_id(), pid)

        p = self.client.patch(f"/api/projects/{pid}", json={"name": "Renamed"})
        self.assertEqual(p.status_code, 200, p.text)
        self.assertEqual(p.json()["project"]["name"], "Renamed")

        d = self.client.delete(f"/api/projects/{pid}")
        self.assertEqual(d.status_code, 200, d.text)
        self.assertIsNone(projects.get_project(pid), "注销后不得再出现在注册表")

    def test_create_missing_dir_is_400(self):
        r = self.client.post("/api/projects", json={"root": os.path.join(self.tmp, "nope")})
        self.assertEqual(r.status_code, 400)

    def test_create_empty_root_is_400(self):
        r = self.client.post("/api/projects", json={"root": ""})
        self.assertEqual(r.status_code, 400)

    def test_activate_unknown_is_404(self):
        r = self.client.post("/api/projects/prj-nope/activate")
        self.assertEqual(r.status_code, 404)

    def test_rename_empty_is_400(self):
        r = self.client.patch(f"/api/projects/{self.pid_a}", json={"name": "   "})
        self.assertEqual(r.status_code, 400)

    def test_rename_unknown_is_404(self):
        r = self.client.patch("/api/projects/prj-nope", json={"name": "x"})
        self.assertEqual(r.status_code, 404)

    def test_delete_unknown_is_404(self):
        r = self.client.delete("/api/projects/prj-nope")
        self.assertEqual(r.status_code, 404)


class ProjectSessionRoutingTests(RoutingBase):
    """⑦ 会话按项目隔离（走真实 api.app）。

    ⚠⚠ 2026-10-11 起，构造方式与判据都变了（乙 + 丙方案）：

    乙：会话存储**全局唯一**（一个 session_id 就是一个会话，跨项目仍是同一个）。
        以前 setUp 一句 `sessions.save(sid, turns, project_id=pid_a)` 就够 ——
        那时 project_id 决定**存在哪**。现在它只决定**归属**，而且归属要走
        生产路径 `bind_code_root` 才写得进去（`save` 不写归属字段）。
        所以 setUp 改成 `bind_code_root`，与 `Agent.__init__` 真实走的那条一致。

    丙：产品语义"项目 B 不该看到 A 的会话"**保留**，靠 `owner_project_id`
        过滤，不再靠分桶。
    """

    def setUp(self):
        super().setUp()
        from starlette.testclient import TestClient
        self._eng = mock.patch.object(api, "engine_running_roots", return_value=[])
        self._eng.start()
        self.client = TestClient(api.app)
        self.sid = "sessA-1"
        # 走生产路径：先有归属绑定，再有内容。
        # `bind_code_root` 内部对已绑定的会话直接返回 False，所以重复 setUp 也安全。
        self.assertTrue(sessions.bind_code_root(self.sid, self.root_a, self.pid_a))
        self.assertTrue(sessions.save(self.sid, [{"user": "qa", "assistant": "ra"}],
                                      project_id=self.pid_a))
        # 自检：归属确实落盘了，否则下面几条全是空判。
        self.assertEqual(sessions.owned_by(self.sid, self.pid_a), self.pid_a,
                         "归属没写进去 —— 后面的项目隔离判据测不到东西")

    def tearDown(self):
        self._eng.stop()
        super().tearDown()

    def test_list_sessions_scoped_to_request_project(self):
        a = self.client.get("/api/sessions", headers={"X-DocMind-Project": self.pid_a}).json()
        self.assertIn(self.sid, {x["session_id"] for x in a["items"]})
        b = self.client.get("/api/sessions", headers={"X-DocMind-Project": self.pid_b}).json()
        self.assertNotIn(self.sid, {x["session_id"] for x in b["items"]},
                         "B 项目不得看到 A 项目的会话")

    def test_session_detail_scoped_to_request_project(self):
        a = self.client.get(f"/api/sessions/{self.sid}",
                            headers={"X-DocMind-Project": self.pid_a}).json()
        self.assertEqual(len(a["turns"]), 1)
        self.assertEqual(a["turns"][0]["user"], "qa")
        b = self.client.get(f"/api/sessions/{self.sid}",
                            headers={"X-DocMind-Project": self.pid_b}).json()
        self.assertEqual(b["turns"], [], "B 项目不得读到 A 项目的会话内容")

    def test_same_session_is_one_session_across_projects(self):
        """**乙方案的核心**：存储全局一份，跨项目仍是同一个会话。

        上面两条守住"B 看不到 A"；这一条守住反面 —— 隔离不是靠
        "拆成两个会话"实现的。若哪天又改回按项目分桶，这条会红。
        """
        # 同一个 sid 在 B 项目下读取：内容是通的（同一个会话）
        got = sessions.load(self.sid, project_id=self.pid_b)
        self.assertEqual([t["user"] for t in got["turns"]], ["qa"],
                         "跨项目读不到同一会话的内容 —— 又成分桶了")
        # 但归属仍属于 A
        self.assertEqual(sessions.owned_by(self.sid, self.pid_b), self.pid_a,
                         "归属被请求项目改了 —— 归属必须跟着会话，不跟着请求")
        # 代码库也仍是 A 的（会话终身沿用，切项目不改）
        self.assertEqual(sessions.current_code_root(self.sid, self.pid_b),
                         str(self.root_a),
                         "切项目把会话的代码库顶掉了 —— 原缺陷重演")

    def test_agent_registry_scoped_by_project_context(self):
        """内存 Agent 亦须按项目隔离：不同项目同名会话不得共用同一 Agent。

        同时验证「无项目上下文 → 纯 session_id 键」（等价改动前，DOCMIND_PROJECTS=0 回退）。
        """
        keys = []
        try:
            tok = api._CTX_PROJECT_ID.set(self.pid_a)
            try:
                a1 = api._agent_for("sess-x")
                keys.append(api._agent_key("sess-x"))
                self.assertEqual(a1.project_id, self.pid_a)
                self.assertIs(a1, api._SESSION_AGENTS[api._agent_key("sess-x")])
            finally:
                api._CTX_PROJECT_ID.reset(tok)
            tok = api._CTX_PROJECT_ID.set(self.pid_b)
            try:
                b1 = api._agent_for("sess-x")
                keys.append(api._agent_key("sess-x"))
                self.assertEqual(b1.project_id, self.pid_b)
                self.assertIsNot(a1, b1, "不同项目的同名会话必须是不同 Agent（内存隔离）")
            finally:
                api._CTX_PROJECT_ID.reset(tok)
            bare = api._agent_for("sess-x")  # 无上下文 → 纯 session_id
            keys.append("sess-x")
            self.assertEqual(bare.project_id, None)
            self.assertIn("sess-x", api._SESSION_AGENTS, "无项目上下文必须退化为纯 session_id 键")
        finally:
            for k in keys:
                api._SESSION_AGENTS.pop(k, None)


class ExplicitPriorityTests(RoutingBase):
    """⑧ 显式优先不被破坏（生命线回归）。"""

    def test_explicit_code_root_not_clobbered_by_persisted(self):
        d_state = self._mkdir("FromState")
        config.save_state("code_root", d_state)
        config.set_runtime("code_root", self.global_root)
        config._apply_persisted_state()
        self.assertEqual(config.get_runtime("code_root"), self.global_root,
                         "显式 set_runtime('code_root') 优先于持久化恢复")

    def test_request_context_does_not_persist_to_global(self):
        from starlette.testclient import TestClient
        client = TestClient(_build_probe_app())
        before = config.get_runtime("code_root")
        client.get("/api/__probe__", headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(config.get_runtime("code_root"), before,
                         "请求级上下文不得写回全局 code_root")

    def test_no_header_binds_current_project(self):
        """不带项目头 → 绑定「当前项目」（A），code_root 即 A 的 root（今日行为等价）。"""
        from starlette.testclient import TestClient
        client = TestClient(_build_probe_app())
        r = client.get("/api/__probe__").json()
        self.assertEqual(r["pid"], self.pid_a)
        self.assertEqual(r["code_root"], self._root_of(self.pid_a))

    def test_registered_project_binds_even_if_root_missing(self):
        """已登记项目的 root 即使已被删除，中间件仍按登记 root 绑定（保持 pid↔root 配对）。

        这是**刻意**设计：中间件保持 (pid, root) 成对，不在这一层做 isdir 校验；
        避免「code_root 回落全局、而 _request_project_id() 仍返回项目 pid」的不一致。
        """
        from starlette.testclient import TestClient
        ghost = self._mkdir("Ghost")
        pid = projects.ensure_project(ghost)
        projects.set_current(pid)
        shutil.rmtree(ghost, ignore_errors=True)  # 目录被删，但注册记录仍在

        client = TestClient(_build_probe_app())
        r = client.get("/api/__probe__").json()
        self.assertEqual(r["pid"], pid)
        self.assertEqual(r["code_root"], os.path.abspath(ghost))


if __name__ == '__main__':
    unittest.main()
