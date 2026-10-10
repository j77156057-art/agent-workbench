# -*- coding: utf-8 -*-
"""P3 端点项目化 —— QA 独立对抗性验证（严过关，software-qa-engineer-3）。

与实现者自测（tests/test_project_routing.py）**独立**：
- 中间件/路由部分**直接打真实 `api.app`**（走完整中间件栈 CORS→项目→鉴权→路由），
  而非仅用「探针 sub-app」，以暴露真实注册顺序与鉴权交互；
- 重点攻击两个实现者自述里**没验证**的怀疑点：
  D1 全局/持久化 `code_root` 是「当前项目指针」，只应由当前项目（或无项目）请求改写——
  非当前项目头的 ingest_code/reset_code 绝不得污染它（修复前实测为真 Bug，本轮断言修复后语义）；
  D2 `httpx.AsyncClient(ASGITransport(api.app))` 真并发下的上下文串扰。
- 真实性能：用 monkeypatch 短路 ingest/reset 的 embedding 与 chroma，**不跑真实索引**。

隔离
----
- setUp 把 `config.STATE_FILE` / `sessions.SESSIONS_DIR` / `config.CHROMA_DIR` 指到临时目录，
  快照并还原 `config._RUNTIME` 与 `api._SESSION_AGENTS`，`DOCMIND_PROJECTS=1`；
- tearDown 断言仓库真实 `.docmind_state.json` / `.docmind_sessions/` / `.chroma/` **字节级不变**。

运行：`.venv/Scripts/python.exe -B -m unittest tests.test_project_routing_qa -v`
"""
import asyncio
import json
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

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_STATE = os.path.join(_REPO_ROOT, ".docmind_state.json")
_REPO_SESSIONS = os.path.join(_REPO_ROOT, ".docmind_sessions")
_REPO_CHROMA = os.path.join(_REPO_ROOT, ".chroma")


def _dir_snapshot(path):
    if not os.path.isdir(path):
        return None
    out = {}
    for name in sorted(os.listdir(path)):
        p = os.path.join(path, name)
        try:
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    out[name] = f.read()
            else:
                out[name] = "<dir>"
        except OSError:
            out[name] = "<err>"
    return out


def _snapshot_repo():
    state = None
    if os.path.isfile(_REPO_STATE):
        with open(_REPO_STATE, "rb") as f:
            state = f.read()
    return state, _dir_snapshot(_REPO_SESSIONS), _dir_snapshot(_REPO_CHROMA)


def _build_probe_app():
    """复用**生产用** `api.project_context_middleware` 的最小探针 app（含慢端点，利于并发交错）。"""
    from fastapi import FastAPI
    from starlette.concurrency import run_in_threadpool
    from starlette.middleware.base import BaseHTTPMiddleware

    probe = FastAPI()
    probe.add_middleware(BaseHTTPMiddleware, dispatch=api.project_context_middleware)

    @probe.get("/api/__qa_probe__")
    async def _p():
        return {"code_root": config.get_runtime("code_root"), "pid": api._ctx_project_id()}

    @probe.get("/api/__qa_probe_slow__")
    async def _slow():
        await asyncio.sleep(0.004)  # 强制让出事件循环，放大并发交错
        return {"code_root": config.get_runtime("code_root"), "pid": api._ctx_project_id()}

    @probe.get("/api/__qa_probe_threadpool__")
    async def _tp():
        v = await run_in_threadpool(lambda: config.get_runtime("code_root"))
        return {"code_root": v}

    @probe.get("/api/__qa_probe_daemon__")
    async def _dm():
        box = {}

        def _w():
            box["code_root"] = config.get_runtime("code_root")

        t = threading.Thread(target=_w, daemon=True)
        t.start()
        t.join(timeout=5)
        return {"code_root": box.get("code_root")}

    @probe.get("/__qa_non_api__")
    async def _np():
        return {"code_root": config.get_runtime("code_root"), "pid": api._ctx_project_id()}

    return probe


class QABase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_p3qa_")
        self._old_state_file = config.STATE_FILE
        self._old_sessions_dir = sessions.SESSIONS_DIR
        self._old_runtime = dict(config._RUNTIME)
        self._old_agents = dict(api._SESSION_AGENTS)
        config.STATE_FILE = os.path.join(self.tmp, ".docmind_state.json")
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "sessions")
        self._env = mock.patch.dict(os.environ, {"DOCMIND_PROJECTS": "1",
                                                 "DOCMIND_OLLAMA_PROBE": "0"})
        self._env.start()
        # 强制离线：.env 里 LLM_PROVIDER=ollama，构造 LLMClient 会发起 ≤3s 同步探活。
        # 本项目化请求会为每个项目新建 Agent→LLMClient，若不强制 mock 会拖慢/触发网络。
        config.set_runtime("llm_provider", "mock")

        # 让「全局 code_root」既非 A 也非 B，便于区分请求上下文与全局
        self.global_root = self._mkdir("GlobalRoot")
        self.root_a = self._mkdir("Alpha")
        self.root_b = self._mkdir("Beta")
        self.pid_a = projects.ensure_project(self.root_a, "Alpha")
        self.pid_b = projects.ensure_project(self.root_b, "Beta")
        self.assertTrue(projects.set_current(self.pid_a))
        config.set_runtime("code_root", self.global_root)

        self._repo_before = _snapshot_repo()

    def tearDown(self):
        self._env.stop()
        config.STATE_FILE = self._old_state_file
        sessions.SESSIONS_DIR = self._old_sessions_dir
        config._RUNTIME.clear()
        config._RUNTIME.update(self._old_runtime)
        api._SESSION_AGENTS.clear()
        api._SESSION_AGENTS.update(self._old_agents)
        # 污染哨兵：仓库真实状态必须字节级不变
        self.assertEqual(_snapshot_repo(), self._repo_before,
                         "用例污染了仓库真实 .docmind_state.json/.docmind_sessions/.chroma")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mkdir(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return d

    def _root_of(self, pid):
        return projects.get_project(pid)["root"]

    def _client(self):
        from starlette.testclient import TestClient
        return TestClient(api.app)


class LifeLineTests(QABase):
    """A. 生命线：不带项目头 ≡ 改动前行为（直接打真实 api.app）。"""

    def setUp(self):
        super().setUp()
        self._count = mock.patch.object(api, "count", return_value=0)
        self._count.start()
        self._eng = mock.patch.object(api, "engine_running_roots", return_value=[])
        self._eng.start()
        self.client = self._client()

    def tearDown(self):
        self._eng.stop()
        self._count.stop()
        super().tearDown()

    def test_config_no_header_returns_current_project_root(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["code_root"], self._root_of(self.pid_a),
                         "无头请求应绑定当前项目 A 的 root")

    def test_config_no_header_falls_back_to_global_without_any_project(self):
        config.save_state("projects", {})
        config.save_state("current_project_id", "")
        config.set_runtime("code_root", self.global_root)
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["code_root"], self.global_root,
                         "无任何项目时无头请求必须回落全局 code_root（生命线）")

    def test_sessions_and_projects_no_header_ok(self):
        self.assertEqual(self.client.get("/api/sessions").status_code, 200)
        d = self.client.get("/api/projects")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.json()["current"], self.pid_a)

    def test_non_api_path_untouched_by_project_middleware(self):
        base = self.client.get("/workbench").status_code
        r = self.client.get("/workbench", headers={"X-DocMind-Project": "prj-nope"})
        self.assertEqual(r.status_code, base, "非 /api/ 路径不得因项目头而改变行为")
        self.assertNotEqual(r.status_code, 401)

    def test_other_runtime_keys_unchanged(self):
        config.set_runtime("llm_model", "qa-model")
        self.client.get("/api/config", headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(config.get_runtime("llm_model"), "qa-model",
                         "项目上下文不得影响 code_root 之外的 key")

    def test_reset_context_code_root_swallows_bad_token(self):
        import contextvars
        # 真实失败模式①：token 在**别的 Context** 里创建 → 本上下文 reset 抛 ValueError，须被吞掉
        other = contextvars.Context()
        tok_other = other.run(config._CTX_CODE_ROOT.set, self.root_b)
        self.assertIsNone(config.reset_context_code_root(tok_other),
                          "跨 Context 的非法 reset 必须被吞掉，不得抛出")
        # 真实失败模式②：同一 token 二次 reset → 抛 RuntimeError，须被吞掉
        tok = config.set_context_code_root(self.root_b)
        self.assertIsNone(config.reset_context_code_root(tok))
        self.assertIsNone(config.reset_context_code_root(tok),
                          "重复 reset 必须被吞掉，不得抛出")

    def test_set_reset_context_roundtrip(self):
        self.assertIsNone(config._CTX_CODE_ROOT.get())
        tok = config.set_context_code_root(self.root_b)
        self.assertEqual(config.get_runtime("code_root"), self.root_b)
        config.reset_context_code_root(tok)
        self.assertIsNone(config._CTX_CODE_ROOT.get())
        self.assertEqual(config.get_runtime("code_root"), self.global_root)


class RoutingIsolationTests(QABase):
    """B. 路由与隔离（真实 api.app + 探针 app）。"""

    def setUp(self):
        super().setUp()
        self._count = mock.patch.object(api, "count", return_value=0)
        self._count.start()
        self._eng = mock.patch.object(api, "engine_running_roots", return_value=[])
        self._eng.start()
        self.client = self._client()
        from starlette.testclient import TestClient
        self.probe = TestClient(_build_probe_app())

    def tearDown(self):
        self._eng.stop()
        self._count.stop()
        super().tearDown()

    def test_header_a_and_b_route_to_respective_root(self):
        ra = self.client.get("/api/config", headers={"X-DocMind-Project": self.pid_a}).json()
        rb = self.client.get("/api/config", headers={"X-DocMind-Project": self.pid_b}).json()
        self.assertEqual(ra["code_root"], self._root_of(self.pid_a))
        self.assertEqual(rb["code_root"], self._root_of(self.pid_b))

    def test_illegal_headers_fall_back_to_current_without_400_or_500(self):
        for bad in ("nope", "", "   ", "prj-deleted-xyz"):
            r = self.client.get("/api/config", headers={"X-DocMind-Project": bad})
            self.assertEqual(r.status_code, 200, f"非法头 {bad!r} 不得 400/500：{r.text}")
            self.assertEqual(r.json()["code_root"], self._root_of(self.pid_a),
                             f"非法头 {bad!r} 应回落当前项目 A")

    def test_deleted_project_pid_header_falls_back(self):
        pid = projects.ensure_project(self._mkdir("ToDelete"))
        self.assertTrue(projects.remove_project(pid))
        r = self.client.get("/api/config", headers={"X-DocMind-Project": pid})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code_root"], self._root_of(self.pid_a))

    def test_mixed_sequence_no_context_leak(self):
        seq = [
            ({"X-DocMind-Project": self.pid_a}, self._root_of(self.pid_a)),
            ({"X-DocMind-Project": self.pid_b}, self._root_of(self.pid_b)),
            ({}, self._root_of(self.pid_a)),                       # 无头 → 当前项目
            ({"X-DocMind-Project": "garbage"}, self._root_of(self.pid_a)),
        ]
        for headers, expect in seq:
            r = self.client.get("/api/config", headers=headers)
            self.assertEqual(r.json()["code_root"], expect, f"headers={headers}")
        # 调用方（主）线程不得残留上下文
        self.assertEqual(api._ctx_project_id(), "")
        self.assertIsNone(config._CTX_CODE_ROOT.get())
        self.assertEqual(config.get_runtime("code_root"), self.global_root,
                         "连续混发后全局 code_root 不得被改动")

    def test_query_param_and_header_priority(self):
        self.assertEqual(self.probe.get("/api/__qa_probe__", params={"project_id": self.pid_b}).json()["pid"],
                         self.pid_b)
        r = self.probe.get("/api/__qa_probe__", params={"project_id": self.pid_b},
                           headers={"X-DocMind-Project": self.pid_a})
        self.assertEqual(r.json()["pid"], self.pid_a, "header 必须优先于 query")
        self.assertEqual(r.json()["code_root"], self._root_of(self.pid_a))

    def test_run_in_threadpool_inherits_request_context(self):
        r = self.probe.get("/api/__qa_probe_threadpool__",
                           headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(r.json()["code_root"], self._root_of(self.pid_b),
                         "run_in_threadpool 内必须仍读到本请求 root")

    def test_daemon_thread_does_not_inherit_request_context(self):
        r = self.probe.get("/api/__qa_probe_daemon__",
                           headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(r.json()["code_root"], self.global_root,
                         "独立线程不应继承请求上下文（应读全局）")

    def test_non_api_probe_not_bound(self):
        r = self.probe.get("/__qa_non_api__", headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(r.json()["pid"], "")
        self.assertEqual(r.json()["code_root"], self.global_root)


class ConcurrencyTests(QABase):
    """D2. 真并发（httpx ASGITransport）下的上下文串扰。"""

    def setUp(self):
        super().setUp()
        self._count = mock.patch.object(api, "count", return_value=0)
        self._count.start()

    def tearDown(self):
        self._count.stop()
        super().tearDown()

    def _run(self, coro_factory):
        return asyncio.run(coro_factory())

    def test_real_app_concurrent_headers_do_not_crosstalk(self):
        import httpx

        async def _go():
            transport = httpx.ASGITransport(app=api.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                reqs = []
                for i in range(25):
                    hdr_a = {"X-DocMind-Project": self.pid_a}
                    hdr_b = {"X-DocMind-Project": self.pid_b}
                    if i % 2:
                        hdr_a, hdr_b = hdr_b, hdr_a
                    reqs.append(c.get("/api/config", headers=hdr_a))
                    reqs.append(c.get("/api/config", headers=hdr_b))
                resps = await asyncio.gather(*reqs)
                return [r.json()["code_root"] for r in resps]

        roots = self._run(_go)
        ra, rb = self._root_of(self.pid_a), self._root_of(self.pid_b)
        self.assertEqual(len(roots), 50)
        self.assertTrue(all(x in (ra, rb) for x in roots),
                        "并发响应出现了非 A 非 B 的 root → 上下文串扰")
        self.assertEqual(roots.count(ra), 25, "A 头请求必须拿到 A 的 root")
        self.assertEqual(roots.count(rb), 25, "B 头请求必须拿到 B 的 root")

    def test_probe_slow_concurrent_no_crosstalk(self):
        import httpx
        probe_app = _build_probe_app()

        async def _go():
            transport = httpx.ASGITransport(app=probe_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                reqs = []
                for i in range(30):
                    pid = self.pid_a if i % 2 == 0 else self.pid_b
                    reqs.append(c.get("/api/__qa_probe_slow__",
                                      headers={"X-DocMind-Project": pid}))
                resps = await asyncio.gather(*reqs)
                return [(r.json()["pid"], r.json()["code_root"]) for r in resps]

        out = self._run(_go)
        ra, rb = self._root_of(self.pid_a), self._root_of(self.pid_b)
        for i, (pid, root) in enumerate(out):
            want_pid = self.pid_a if i % 2 == 0 else self.pid_b
            want_root = ra if i % 2 == 0 else rb
            self.assertEqual((pid, root), (want_pid, want_root),
                             f"第 {i} 个并发请求串扰：{pid}/{root}")
        self.assertEqual(api._ctx_project_id(), "")


class ProjectCrudTests(QABase):
    """C. 项目 CRUD 与副作用。"""

    def setUp(self):
        super().setUp()
        self._count = mock.patch.object(api, "count", return_value=0)
        self._count.start()
        self._eng = mock.patch.object(api, "engine_running_roots", return_value=[])
        self._eng.start()
        self.client = self._client()

    def tearDown(self):
        self._eng.stop()
        self._count.stop()
        super().tearDown()

    def test_create_missing_or_non_dir_is_400(self):
        r1 = self.client.post("/api/projects", json={"root": os.path.join(self.tmp, "nope")})
        self.assertEqual(r1.status_code, 400)
        f = os.path.join(self.tmp, "afile.txt")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("x")
        r2 = self.client.post("/api/projects", json={"root": f})
        self.assertEqual(r2.status_code, 400, "非目录路径必须 400")

    def test_create_activates_and_duplicate_root_reuses_pid(self):
        new_root = self._mkdir("Gamma")
        r = self.client.post("/api/projects", json={"root": new_root, "name": "GammaP"})
        self.assertEqual(r.status_code, 200, r.text)
        pid = r.json()["project_id"]
        self.assertEqual(pid, projects.project_id(new_root))
        self.assertEqual(projects.current_project_id(), pid, "创建即激活")
        self.assertEqual(config.get_runtime("code_root"), os.path.abspath(new_root))
        n_before = len(projects.list_projects())
        r2 = self.client.post("/api/projects", json={"root": os.path.abspath(new_root)})
        self.assertEqual(r2.json()["project_id"], pid, "同一 root 必须复用同一 pid")
        self.assertEqual(len(projects.list_projects()), n_before, "重复添加不得重复登记")

    def test_current_consistent_and_activate_switches(self):
        data = self.client.get("/api/projects").json()
        self.assertEqual(data["current"], self.pid_a)
        self.assertEqual({p["project_id"] for p in data["projects"]},
                         {self.pid_a, self.pid_b})
        r = self.client.post(f"/api/projects/{self.pid_b}/activate")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(projects.current_project_id(), self.pid_b)
        self.assertEqual(config.get_runtime("code_root"), os.path.abspath(self.root_b))

    def test_rename_takes_effect(self):
        r = self.client.patch(f"/api/projects/{self.pid_a}", json={"name": "AlphaRenamed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(projects.get_project(self.pid_a)["name"], "AlphaRenamed")

    def test_delete_only_deregisters_never_touches_disk(self):
        root = self._mkdir("KeepMe")
        sentinel = os.path.join(root, "user_code.py")
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write("print('keep')\n")
        pid = projects.ensure_project(root, "KeepMe")
        # 伪造「该项目会话桶」与「向量集合目录」
        bucket = os.path.join(sessions.SESSIONS_DIR, sessions._slug(pid))
        os.makedirs(bucket, exist_ok=True)
        with open(os.path.join(bucket, "sess.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        vec = os.path.join(config.CHROMA_DIR, "docmind_code__" + pid)
        os.makedirs(vec, exist_ok=True)
        with open(os.path.join(vec, "index.bin"), "w", encoding="utf-8") as f:
            f.write("vec")

        r = self.client.delete(f"/api/projects/{pid}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(projects.get_project(pid), "注销后不得再出现在注册表")
        self.assertTrue(os.path.isdir(root), "删除项目绝不得删用户代码目录")
        self.assertTrue(os.path.isfile(sentinel), "用户文件必须原样保留")
        self.assertTrue(os.path.isfile(os.path.join(bucket, "sess.json")),
                        "删除项目绝不得删会话桶")
        self.assertTrue(os.path.isfile(os.path.join(vec, "index.bin")),
                        "删除项目绝不得删向量集合目录")

    def test_sessions_scoped_per_project(self):
        """项目隔离仍在，但机制从"分桶"换成了"归属字段"（丙，2026-10-11）。

        ⚠ 构造必须走 `bind_code_root`（生产路径，`Agent.__init__` 走的就是它）——
        `save()` 不写 `owner_project_id`，直接 save 出来的会话没有归属，
        会被归属过滤当成"老会话"处理，产品语义就测不到了。
        """
        self.assertTrue(sessions.bind_code_root("sA", self.root_a, self.pid_a))
        self.assertTrue(sessions.bind_code_root("sB", self.root_b, self.pid_b))
        self.assertTrue(sessions.save("sA", [{"user": "qa", "assistant": "ra"}],
                                      project_id=self.pid_a))
        self.assertTrue(sessions.save("sB", [{"user": "qb", "assistant": "rb"}],
                                      project_id=self.pid_b))
        # 自检：归属真的写进去了，否则下面全是空判
        self.assertEqual(sessions.owned_by("sA", self.pid_a), self.pid_a)
        self.assertEqual(sessions.owned_by("sB", self.pid_b), self.pid_b)
        a = self.client.get("/api/sessions", headers={"X-DocMind-Project": self.pid_a}).json()
        b = self.client.get("/api/sessions", headers={"X-DocMind-Project": self.pid_b}).json()
        a_ids = {x["session_id"] for x in a["items"]}
        b_ids = {x["session_id"] for x in b["items"]}
        self.assertIn("sA", a_ids)
        self.assertNotIn("sB", a_ids)
        self.assertIn("sB", b_ids)
        self.assertNotIn("sA", b_ids)
        # detail
        da = self.client.get("/api/sessions/sA", headers={"X-DocMind-Project": self.pid_a}).json()
        self.assertEqual(da["turns"][0]["user"], "qa")
        db = self.client.get("/api/sessions/sA", headers={"X-DocMind-Project": self.pid_b}).json()
        self.assertEqual(db["turns"], [], "B 项目不得读到 A 的会话")
        # delete 只作用本项目
        self.assertEqual(self.client.delete("/api/sessions/sA",
                         headers={"X-DocMind-Project": self.pid_a}).json()["ok"], True)
        still = self.client.get("/api/sessions", headers={"X-DocMind-Project": self.pid_b}).json()
        self.assertIn("sB", {x["session_id"] for x in still["items"]})


class IngestCodeCurrentGuardTests(QABase):
    """D1（第 2 轮，修复后语义）：全局/持久化 code_root 是「当前项目指针」，只由
    「当前项目（或无项目）」的请求改写。

    修复前（上一轮实测为真 Bug）：带「非当前项目」头的 ingest_code/reset_code 会污染
    全局 `_RUNTIME['code_root']` 与 STATE_FILE，与 current_project_id 脱节。
    本轮改为断言**修复后的正确行为**：非当前项目请求只在项目作用域内建/清索引，绝不动
    全局指针、持久化文件与全局 project_rules；当前项目（含无头）请求照旧写全局。
    """

    def setUp(self):
        super().setUp()
        self._count = mock.patch.object(api, "count", return_value=0)
        self._count.start()
        self._eng = mock.patch.object(api, "engine_running_roots", return_value=[])
        self._eng.start()
        self._ingest = mock.patch.object(api, "ingest_code_directory", return_value=5)
        self.ingest_mock = self._ingest.start()
        self._reset = mock.patch.object(api, "reset_collection", return_value=None)
        self.reset_mock = self._reset.start()
        self._rules = mock.patch.object(api, "load_project_rules", return_value="")
        self._rules.start()

        async def _fake_stop(_root):
            return [], []

        self._stop = mock.patch.object(api, "_stop_other_engines", new=_fake_stop)
        self._stop.start()
        self.client = self._client()

        # 稳态：当前项目 A，全局 code_root 已指 A 的 root 且已持久化（模拟 A 曾是当前项目）
        config.set_runtime("code_root", os.path.abspath(self.root_a))
        config.set_runtime("project_rules", "RULES-A")
        config.save_state("code_root", os.path.abspath(self.root_a))

    def tearDown(self):
        self._stop.stop()
        self._rules.stop()
        self._reset.stop()
        self._ingest.stop()
        self._eng.stop()
        self._count.stop()
        super().tearDown()

    def test_ingest_code_with_noncurrent_project_does_not_pollute_global_code_root(self):
        # 当前项目 = A；带 B 头调用 ingest_code（B 的目录）
        r = self.client.post("/api/ingest_code", data={"root": os.path.abspath(self.root_b)},
                             headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["code_root"], os.path.abspath(self.root_b))

        # 当前项目未切换，仍应是 A
        self.assertEqual(projects.current_project_id(), self.pid_a,
                         "非当前项目 ingest 不得切换当前项目")

        # 全局（非请求上下文）与持久化 code_root 必须保持当前项目 A，绝不被 B 污染
        self.assertEqual(config.get_runtime("code_root"), os.path.abspath(self.root_a),
                         "全局 _RUNTIME['code_root'] 必须仍指当前项目 A")
        self.assertEqual(config.load_state("code_root"), os.path.abspath(self.root_a),
                         "STATE_FILE 的 code_root 必须仍指当前项目 A")
        self.assertNotEqual(config.get_runtime("code_root"), os.path.abspath(self.root_b))

        # 无头 /api/ 请求仍显示 A
        self.assertEqual(self.client.get("/api/config").json()["code_root"],
                         self._root_of(self.pid_a))

    def test_noncurrent_ingest_keeps_current_after_restart(self):
        self.client.post("/api/ingest_code", data={"root": os.path.abspath(self.root_b)},
                         headers={"X-DocMind-Project": self.pid_b})
        # 模拟重启：清空内存 runtime，再按持久化状态恢复（lifespan 行为）
        config._RUNTIME.clear()
        config._apply_persisted_state()
        self.assertEqual(projects.current_project_id(), self.pid_a)
        self.assertEqual(config.get_runtime("code_root"), os.path.abspath(self.root_a),
                         "重启后全局 code_root 仍是 A，不再变成 B")

    def test_reset_code_with_noncurrent_project_keeps_global(self):
        r = self.client.post("/api/reset_code", headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(config.get_runtime("code_root"), os.path.abspath(self.root_a),
                         "非当前项目 reset_code 不得清全局 code_root")
        self.assertEqual(config.load_state("code_root"), os.path.abspath(self.root_a),
                         "非当前项目 reset_code 不得清持久化 code_root")
        self.assertEqual(config.get_runtime("project_rules"), "RULES-A",
                         "非当前项目 reset_code 不得清全局 project_rules")
        self.assertEqual(projects.current_project_id(), self.pid_a)
        self.assertEqual(self.reset_mock.call_args.args[0],
                         projects.code_collection(self.pid_b),
                         "reset_code 必须只清 B 的代码集合")

    def test_ingest_code_without_header_tracks_current_project(self):
        # 生命线：不带项目头 = 当前项目 A → 照旧写全局（先置哨兵，证明确被改写）
        config.set_runtime("code_root", self.global_root)
        r = self.client.post("/api/ingest_code", data={"root": os.path.abspath(self.root_a)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(projects.current_project_id(), self.pid_a)
        self.assertEqual(config.get_runtime("code_root"), os.path.abspath(self.root_a))
        self.assertEqual(config.load_state("code_root"), os.path.abspath(self.root_a))

    def test_reset_code_without_header_clears_global(self):
        # 生命线对照：无头 reset_code = 当前项目 A → 照旧清全局
        r = self.client.post("/api/reset_code")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(config.get_runtime("code_root"), "")
        self.assertEqual(config.load_state("code_root"), "")
        self.assertEqual(self.reset_mock.call_args.args[0],
                         projects.code_collection(self.pid_a))

    def test_ingest_noncurrent_indexes_project_collection_and_notice(self):
        r = self.client.post("/api/ingest_code", data={"root": os.path.abspath(self.root_b)},
                             headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.reset_mock.call_args.args[0],
                         projects.code_collection(self.pid_b), "必须清 B 的集合")
        self.assertEqual(self.ingest_mock.call_args.kwargs["collection"],
                         projects.code_collection(self.pid_b),
                         "必须把 B 的目录索引进 B 的集合")
        self.assertIn("当前项目未切换", r.json()["notice"],
                      "非当前项目建索引须在 notice 说明未切换当前项目")
        self.assertEqual(config.get_runtime("code_root"), os.path.abspath(self.root_a))

    def test_ingest_with_current_project_header_writes_global(self):
        config.set_runtime("code_root", self.global_root)  # 哨兵（≠A），证明确被改写
        r = self.client.post("/api/ingest_code", data={"root": os.path.abspath(self.root_a)},
                             headers={"X-DocMind-Project": self.pid_a})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(config.get_runtime("code_root"), os.path.abspath(self.root_a),
                         "头=当前项目时必须照常写全局（条件判断不是「带头就跳过」）")
        self.assertEqual(config.load_state("code_root"), os.path.abspath(self.root_a))
        self.assertNotIn("当前项目未切换", r.json()["notice"])


class AuthMiddlewareTests(QABase):
    """E. 鉴权仍生效，且项目头不能绕过鉴权。"""

    def setUp(self):
        super().setUp()
        self._count = mock.patch.object(api, "count", return_value=0)
        self._count.start()
        self._eng = mock.patch.object(api, "engine_running_roots", return_value=[])
        self._eng.start()
        self._token = mock.patch.object(api, "API_TOKEN", "s3cr3t-token")
        self._token.start()
        self.client = self._client()

    def tearDown(self):
        self._token.stop()
        self._eng.stop()
        self._count.stop()
        super().tearDown()

    def test_unauthenticated_api_rejected(self):
        self.assertEqual(self.client.get("/api/config").status_code, 401)
        self.assertEqual(self.client.get("/api/projects").status_code, 401)

    def test_project_header_does_not_bypass_auth(self):
        r = self.client.get("/api/config", headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(r.status_code, 401, "项目头不得绕过鉴权")

    def test_authenticated_requests_pass_header_and_no_header(self):
        r1 = self.client.get("/api/config", headers={"X-DocMind-Token": "s3cr3t-token"})
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.get("/api/config", headers={"X-DocMind-Token": "s3cr3t-token",
                                                     "X-DocMind-Project": self.pid_b})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["code_root"], self._root_of(self.pid_b))
        r3 = self.client.get("/api/config", headers={"Authorization": "Bearer s3cr3t-token"})
        self.assertEqual(r3.status_code, 200)

    def test_401_response_does_not_leak_context(self):
        self.client.get("/api/config", headers={"X-DocMind-Project": self.pid_b})
        self.assertEqual(api._ctx_project_id(), "")
        self.assertIsNone(config._CTX_CODE_ROOT.get())
        self.assertEqual(config.get_runtime("code_root"), self.global_root,
                         "鉴权 401 后上下文必须已被 finally 复位")

    def test_non_api_path_not_gated_by_auth(self):
        r = self.client.get("/workbench", headers={"X-DocMind-Project": "prj-nope"})
        self.assertNotEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
