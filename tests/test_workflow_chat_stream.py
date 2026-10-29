# -*- coding: utf-8 -*-
"""工作流融入对话：子代理实时轨迹 sink、manager 事件总线、SSE 端点、
chat 流 workflow 事件、start_workflow pending 登记。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
import tools as tools_mod  # noqa: E402
from agent_runtime.game_workflow import (  # noqa: E402
    WORKFLOWS, GameWorkflowManager, WorkflowPolicy)


class _ScriptLLM:
    """按序回放脚本（逐字符流式），造出子代理 thought/action 轨迹。"""
    provider = "fake"
    model = "script"

    def __init__(self, scripts):
        self.scripts = scripts
        self.i = 0
        self.last_usage = {"prompt_tokens": 5, "completion_tokens": 2}
        self.last_tool_calls = []

    def clone(self):
        return _ScriptLLM(self.scripts)

    def chat(self, messages, stream=True, **kw):
        s = self.scripts[min(self.i, len(self.scripts) - 1)]
        self.i += 1
        if stream:
            def g():
                for ch in s:
                    yield ch
            return g()
        return s

    def count_tokens(self, text):
        return 1


class StepSinkTests(unittest.TestCase):
    def test_sink_receives_bounded_steps_in_order(self):
        old_cap = agent_mod.SINK_THOUGHT_CHARS
        agent_mod.SINK_THOUGHT_CHARS = 40
        try:
            long_thought = "思" * 500
            llm = _ScriptLLM([
                f"Thought: {long_thought}\nAction: search_code\nAction Input: query: foo",
                "Final Answer: 完成",
            ])
            items = []
            out = agent_mod.Agent(llm=llm)._run_child(
                "researcher", "查 foo", reflect=False,
                step_sink=lambda item: items.append(item))
            self.assertEqual(out["status"], "ok")
            types = [item["type"] for item in items]
            self.assertIn("thought", types)
            self.assertIn("action", types)
            self.assertTrue(types.index("thought") < types.index("action"))
            thought = next(item for item in items if item["type"] == "thought")
            # _clip 裁剪后会追加「…（内容过长，已截断）」标记，故上限 = 裁剪值 + 标记
            self.assertTrue(thought["text"].startswith("思" * 40))
            self.assertLess(len(thought["text"]), 60)
            self.assertIn("截断", thought["text"])
            action = next(item for item in items if item["type"] == "action")
            self.assertIn("search_code", action["text"])
        finally:
            agent_mod.SINK_THOUGHT_CHARS = old_cap

    def test_sink_exception_never_breaks_child(self):
        def boom(_item):
            raise RuntimeError("sink down")

        llm = _ScriptLLM([
            "Thought: 先看看\nAction: search_code\nAction Input: query: foo",
            "Final Answer: 完成",
        ])
        out = agent_mod.Agent(llm=llm)._run_child(
            "researcher", "查", reflect=False, step_sink=boom)
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["conclusion"])


class ManagerEventBusTests(unittest.TestCase):
    def setUp(self):
        self.manager = GameWorkflowManager(tempfile.mkdtemp())
        state = self.manager.start("事件总线测试", project_root=tempfile.mkdtemp(),
                                   policy=WorkflowPolicy(approval_mode="high"))
        self.wid = state["workflow_id"]
        self.state = self.manager._load(self.wid)

    def test_subscribe_replays_and_fans_out_with_monotonic_seq(self):
        events, latest = self.manager.events_since(self.wid, 0)
        self.assertTrue(events)
        self.assertGreaterEqual(latest, 1)
        self.assertTrue(all(event.get("seq") for event in events))
        q = self.manager.subscribe(self.wid)
        try:
            self.manager._event(self.state, "unit_ping", note="x")
            got = q.get(timeout=2)
            self.assertEqual(got["kind"], "unit_ping")
            self.assertGreater(got["seq"], latest)
        finally:
            self.manager.unsubscribe(self.wid, q)
        self.assertNotIn(self.wid, self.manager._subscribers)

    def test_record_agent_step_is_memory_only_and_bounded(self):
        self.manager._step_ring_max = 5
        q = self.manager.subscribe(self.wid)
        try:
            for i in range(8):
                self.manager.record_agent_step(
                    self.wid, "t1", "coder",
                    {"type": "action", "text": "step-%d" % i})
            kinds = []
            while True:
                try:
                    kinds.append(q.get_nowait()["kind"])
                except Exception:
                    break
            self.assertTrue(all(kind == "subagent_step" for kind in kinds))
            self.assertGreaterEqual(len(kinds), 5)
            ring = self.manager.agent_steps(self.wid)["t1"]
            self.assertEqual(len(ring), 5, "每任务轨迹环必须有界")
            self.assertEqual(ring[-1]["text"], "step-7")
            # 实时步不进持久台账
            persisted = self.manager.get(self.wid)["events"]
            self.assertFalse(any(e.get("kind") == "subagent_step" for e in persisted))
        finally:
            self.manager.unsubscribe(self.wid, q)

    def test_terminal_completion_clears_step_ring(self):
        self.manager.choose(self.wid, "recommended")
        self.manager.plan(self.wid, [{"id": "a", "role": "tester", "task": "验证"}])
        self.manager.record_agent_step(
            self.wid, "a", "tester", {"type": "thought", "text": "开始"})
        self.assertIn("a", self.manager.agent_steps(self.wid))

        def runner(_task, _context):
            return {"status": "ok", "conclusion": "通过", "steps": 1,
                    "trace": {"steps": [{"action": "search_code(x)", "obs": "命中"}],
                              "n_steps": 1, "thoughts": ["看一眼"]}}

        done = self.manager.execute(self.wid, runner)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.manager.agent_steps(self.wid), {})
        complete = next(e for e in done["events"] if e.get("kind") == "subagent_complete")
        self.assertTrue(complete.get("seq"))
        self.assertEqual(complete["trace"]["steps"][0]["action"], "search_code(x)")
        self.assertEqual(complete["conclusion"], "通过")

    def test_completed_workflow_replay_after_cursor_and_terminal_flag(self):
        self.manager.choose(self.wid, "recommended")
        self.manager.plan(self.wid, [{"id": "a", "role": "tester", "task": "验证"}])
        done = self.manager.execute(
            self.wid, lambda *_: {"status": "ok", "conclusion": "ok", "steps": 1})
        last_seq = done["events"][-1]["seq"]
        replay, latest = self.manager.events_since(self.wid, last_seq)
        self.assertEqual(replay, [])
        self.assertEqual(latest, last_seq)
        self.assertTrue(self.manager.is_terminal(self.wid))


class WorkflowEventsSseTests(unittest.TestCase):
    """通过真实 FastAPI 路由验证 SSE 端点（重放 + 终态自关 + 404）。

    2026-10-15：本类此前固定 404——路由层的跨项目归属校验
    （`require_workflow_project`，AND 语义：project_id 与 project_root 都要命中，
    任一缺失即拒）是在这些用例写完之后加的，而用例只给了 `project_root` 却没给
    请求侧项目上下文，于是每次都被判「workflow 不属于当前项目」。

    修法不是在守卫上开口子（那会重新打开跨项目读取），而是让用例按真实契约
    携带项目上下文：服务端认 `X-DocMind-Project` 头（api.py 的请求上下文读取），
    前端 `withProject()` 发的也是它。

    **刻意不改全局 `projects.current_project_id()`**：那是进程级持久状态，
    改了会跨测试文件泄漏（见 tests/test_project_context_isolation.py 与
    docs/known-defect-project-context-leaks-across-tests.md 立档的既知限制）。
    带请求头就完全不必动它——用例因此是隔离的，也不会把临时项目留给后续进程。
    """

    def setUp(self):
        import projects as projects_mod
        self.root = tempfile.mkdtemp()
        self.pid = projects_mod.ensure_project(self.root, name="SSE 端点验证")
        state = WORKFLOWS.start("SSE 端点验证", project_id=self.pid,
                                project_root=self.root,
                                policy=WorkflowPolicy(approval_mode="high"))
        self.wid = state["workflow_id"]
        WORKFLOWS.choose(self.wid, "recommended")
        WORKFLOWS.plan(self.wid, [{"id": "a", "role": "tester", "task": "验证"}])

    def tearDown(self):
        import projects as projects_mod
        # 注销临时项目：注册表不该积压指向已删目录的项目（current 一旦悬在
        # 这种项目上，后续进程解析到的项目根就不存在了，密钥会跟着失效）。
        projects_mod.remove_project(self.pid)
        try:
            WORKFLOWS.delete_workflow(self.wid)
        except Exception:  # noqa: BLE001 - 不存在/非终态都无需处理
            pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _headers(self, pid=None):
        return {"X-DocMind-Project": pid or self.pid}

    def _complete(self):
        def runner(_task, _context):
            return {"status": "ok", "conclusion": "通过", "steps": 1,
                    "trace": {"steps": [{"action": "read_file(f)", "obs": "ok"}],
                              "n_steps": 1}}
        return WORKFLOWS.execute(self.wid, runner)

    def test_sse_replays_events_and_closes_at_terminal(self):
        from starlette.testclient import TestClient
        import api
        self._complete()
        with TestClient(api.app) as client:
            resp = client.get("/api/agent/workflow/%s/events" % self.wid,
                              headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.headers.get("content-type", ""))
        payloads = [json.loads(line[5:].strip())
                    for line in resp.text.splitlines()
                    if line.startswith("data: ")]
        kinds = [p.get("kind") for p in payloads]
        self.assertIn("subagent_complete", kinds)
        self.assertEqual(kinds[-1], "__stream_done__")
        complete = next(p for p in payloads if p.get("kind") == "subagent_complete")
        self.assertEqual(complete["trace"]["n_steps"], 1)
        self.assertFalse(WORKFLOWS._subscribers.get(self.wid), "终态后不得残留订阅者")

    def test_sse_respects_after_cursor(self):
        from starlette.testclient import TestClient
        import api
        done = self._complete()
        last_seq = done["events"][-1]["seq"]
        with TestClient(api.app) as client:
            resp = client.get(
                "/api/agent/workflow/%s/events?after=%d" % (self.wid, last_seq),
                headers=self._headers())
        payloads = [json.loads(line[5:].strip())
                    for line in resp.text.splitlines()
                    if line.startswith("data: ")]
        self.assertEqual([p.get("kind") for p in payloads], ["__stream_done__"])

    def test_unknown_workflow_returns_404(self):
        from starlette.testclient import TestClient
        import api
        with TestClient(api.app) as client:
            resp = client.get("/api/agent/workflow/no-such-wf/events",
                              headers=self._headers())
        self.assertEqual(resp.status_code, 404)

    def test_cross_project_workflow_returns_404(self):
        """别项目的工作流不能被当前项目订阅事件（就是本类当初固定 404 的那条守卫）。

        知道 workflow_id 不等于有权读它——这条钉住「守卫不能被顺手放松」。
        用请求头换成另一个项目，全程不改全局 current_project_id。
        """
        from starlette.testclient import TestClient
        import api
        import projects as projects_mod
        other_root = tempfile.mkdtemp()
        other_pid = projects_mod.ensure_project(other_root, name="别项目")
        try:
            with TestClient(api.app) as client:
                resp = client.get("/api/agent/workflow/%s/events" % self.wid,
                                  headers=self._headers(other_pid))
            self.assertEqual(resp.status_code, 404)
            self.assertIn("不属于当前项目", resp.json().get("error", ""))
        finally:
            projects_mod.remove_project(other_pid)
            shutil.rmtree(other_root, ignore_errors=True)

    def test_backend_status_includes_langsmith_diagnostics(self):
        from starlette.testclient import TestClient
        import api
        with TestClient(api.app) as client:
            resp = client.get("/api/agent/workflow/backend")
        self.assertEqual(resp.status_code, 200)
        ls = resp.json().get("langsmith") or {}
        for key in ("enabled", "installed", "key_configured", "project"):
            self.assertIn(key, ls)


class PendingWorkflowTests(unittest.TestCase):
    def test_notify_take_is_one_shot(self):
        holder = tools_mod.push_pending_stream()
        try:
            tools_mod._notify_workflow_started({
                "workflow_id": "wf-1", "status": "awaiting_choice",
                "phase": "clarify", "kind": "eda", "options": [1, 2]})
            first = tools_mod.take_pending_workflow(holder)
            self.assertEqual(first["workflow_id"], "wf-1")
            self.assertEqual(first["kind"], "eda")
            self.assertEqual(first["options_count"], 2)
            self.assertIsNone(tools_mod.take_pending_workflow(holder))
        finally:
            tools_mod.pop_pending_stream(holder)

    def test_start_workflow_registers_pending(self):
        root = tempfile.mkdtemp()
        manager = GameWorkflowManager(tempfile.mkdtemp())
        old_launcher = tools_mod._WORKFLOW_LAUNCHER
        old_root = tools_mod.CODE_ROOT
        holder = tools_mod.push_pending_stream()
        try:
            tools_mod.set_workflow_launcher(
                lambda goal, *, kind="generic", web_enabled=False:
                manager.start(goal, kind=kind, project_root=root, llm_enabled=False))
            tools_mod.set_runtime("code_root", root)
            tools_mod.CODE_ROOT = root
            text = tools_mod.start_workflow("goal: 跑通一条小流水线")
            self.assertNotIn("start_workflow 失败", text)
            pending = tools_mod.take_pending_workflow(holder)
            self.assertIsNotNone(pending)
            self.assertIn(pending["status"], ("awaiting_choice", "generating_options"))
            self.assertIsNone(tools_mod.take_pending_workflow(holder))
        finally:
            tools_mod.pop_pending_stream(holder)
            tools_mod.set_workflow_launcher(old_launcher)
            tools_mod.set_runtime("code_root", "")
            tools_mod.CODE_ROOT = old_root


class ChatSseWorkflowEventTests(unittest.TestCase):
    def test_chat_stream_emits_workflow_event_after_tool_launch(self):
        import config
        from starlette.testclient import TestClient
        import api

        old_provider = config.get_runtime("llm_provider")
        config.set_runtime("llm_provider", "ollama")
        try:
            def fake_run(self, *args, **kwargs):
                tools_mod._notify_workflow_started({
                    "workflow_id": "wf-chat-1", "status": "awaiting_choice",
                    "phase": "clarify", "kind": "generic", "options": []})
                yield {"type": "token", "text": "已为你创建工作流"}
                yield {"type": "final", "text": "已为你创建工作流"}

            with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                    api, "check_ollama",
                    return_value={"reachable": True, "guidance": ""}), \
                 __import__("unittest.mock", fromlist=["patch"]).patch.object(
                    api.Agent, "run", fake_run):
                with TestClient(api.app) as client:
                    resp = client.post("/api/chat", data={"question": "帮我完整做个功能"})
            self.assertEqual(resp.status_code, 200)
            types = []
            for line in resp.text.splitlines():
                if line.startswith("data: "):
                    types.append(json.loads(line[6:]).get("type"))
            self.assertIn("workflow", types)
            # 工作流卡片走结构化 workflow 事件，不再另发通用 notice（token 已告知用户）。
            self.assertNotIn("notice", types)
            # 取走即清：整个 SSE 流只补发一次
            self.assertEqual(types.count("workflow"), 1)
        finally:
            config.set_runtime("llm_provider", old_provider or "")

    def test_chat_stream_emits_heartbeat_while_agent_waits(self):
        import config
        import time
        from starlette.testclient import TestClient
        import api

        old_provider = config.get_runtime("llm_provider")
        old_heartbeat = api.CHAT_STREAM_HEARTBEAT_S
        config.set_runtime("llm_provider", "ollama")
        api.CHAT_STREAM_HEARTBEAT_S = 0.01
        try:
            def fake_run(self, *args, **kwargs):
                time.sleep(0.05)
                yield {"type": "final", "text": "等待后完成"}

            with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                    api, "check_ollama",
                    return_value={"reachable": True, "guidance": ""}), \
                 __import__("unittest.mock", fromlist=["patch"]).patch.object(
                    api.Agent, "run", fake_run):
                with TestClient(api.app) as client:
                    resp = client.post("/api/chat", data={"question": "等待模型"})
            self.assertEqual(resp.status_code, 200)
            # 心跳改为 SSE 注释行：保持连接且不往对话里塞重复提示。
            self.assertIn(": keep-alive", resp.text)
            self.assertNotIn("模型仍在处理", resp.text)
            self.assertIn("等待后完成", resp.text)
        finally:
            api.CHAT_STREAM_HEARTBEAT_S = old_heartbeat
            config.set_runtime("llm_provider", old_provider or "")

    def test_chat_event_stream_is_async_and_polls_disconnect(self):
        """真机踩过：同步 event_stream 断开后 finally 不跑，后端继续生成。

        这里钉住两个承重条件：生成器必须是 async，且必须主动 poll
        request.is_disconnected() —— 缺一个，「停止」就只停了前端 UI。
        """
        import inspect
        import api
        src = inspect.getsource(api.chat)
        self.assertIn("async def event_stream", src)
        self.assertIn("is_disconnected", src)
        self.assertIn("worker_stop.set()", src)
        # 真机回归：点停止后 worker 立刻退出、队列也空，看门狗抢在 stop 判断前
        # 抛「工作线程异常退出」，于是用户停止被记成 error。停止判断必须先于看门狗。
        empty_branch = src[src.index("except queue.Empty:"):src.index("SSE 工作线程异常退出")]
        self.assertIn("worker_stop.is_set()", empty_branch,
                      "停止后 worker 正常退出不能被看门狗当成事故")
        # 停止/断连时写 SSE 会抛 ClientDisconnected，不能落 error 终态。
        err_branch = src[src.index("except BaseException as e:"):]
        self.assertIn("worker_stop.is_set()", err_branch[:400])
        self.assertIn("ClientDisconnected", err_branch[:400])

    def test_chat_stream_passes_cancellation_event_to_agent(self):
        import config
        from starlette.testclient import TestClient
        import api

        old_provider = config.get_runtime("llm_provider")
        seen = {}
        config.set_runtime("llm_provider", "ollama")
        try:
            def fake_run(self, *args, **kwargs):
                seen["cancel_event"] = kwargs.get("cancel_event")
                yield {"type": "final", "text": "已完成"}

            with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                    api, "check_ollama",
                    return_value={"reachable": True, "guidance": ""}), \
                 __import__("unittest.mock", fromlist=["patch"]).patch.object(
                    api.Agent, "run", fake_run):
                with TestClient(api.app) as client:
                    resp = client.post("/api/chat", data={"question": "验证取消链路"})
            self.assertEqual(resp.status_code, 200)
            self.assertIn("已完成", resp.text)
            self.assertIsNotNone(seen.get("cancel_event"))
            self.assertTrue(hasattr(seen["cancel_event"], "is_set"))
        finally:
            config.set_runtime("llm_provider", old_provider or "")


if __name__ == "__main__":
    unittest.main()
