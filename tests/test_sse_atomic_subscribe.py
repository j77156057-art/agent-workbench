"""SSE 原子订阅与并发删除保护。

之前 `subscribe` 只注册 queue，不校验 workflow 是否还存在。竞态：
1. SSE handler `WORKFLOWS.get(wid)` 拿到 state；
2. 另一个请求 `delete_workflow(wid)`，从 `_states` 弹掉 state；
3. SSE handler `WORKFLOWS.subscribe(wid)` 照常注册 queue；
4. SSE 循环里 `is_terminal(wid)` 永远 False（state 已弹），但
   `events_since` 返回 `(None, latest_seq)`；
5. 循环持续 15s 一次 ping，queue 永远不被释放 → 内存泄漏 + 假活跃。

现在 `subscribe` 原子校验 state 存在 + 磁盘 JSON 存在；不存在抛
`WorkflowError`，SSE handler 转 404。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.game_workflow import GameWorkflowManager, WorkflowError


class _Ctx:
    def __init__(self, root, pid):
        self._root = root
        self._pid = pid

    def _project_root_or_error(self):
        return self._root

    def _request_project_id(self):
        return self._pid


def _build_client(case, manager, root, pid):
    """建一个只挂 agent 路由的 TestClient。

    ⚠⚠ 换 `api_agent.WORKFLOWS` 之后**必须还原**。它是模块级全局，路由在调用时
    直接读它（build_router 不透传 manager）。漏还原的后果实测过：本文件按字母序
    排在 test_workflow_chat_stream / test_workflow_ownership / test_workflow_profiles
    / test_workflow_start_consistency 等一批之前，那些测试用**原始 singleton**
    建 workflow，路由读的却是被换掉的这个 → 全线 404「工作流不存在」。
    用 addCleanup 而不是 finally：异常路径也保证还原。
    """
    import api_routes.agent as api_agent
    original = api_agent.WORKFLOWS
    api_agent.WORKFLOWS = manager
    case.addCleanup(setattr, api_agent, "WORKFLOWS", original)
    app = FastAPI()
    app.include_router(api_agent.build_router(_Ctx(root, pid)))
    return TestClient(app)


class SseAtomicSubscribeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project = self.tmp / "p"
        self.project.mkdir()
        self.pid = "pid-x"
        self.mgr = GameWorkflowManager(str(self.tmp / "_state"))

    def tearDown(self):
        self.mgr.close()

    def test_subscribe_after_delete_raises(self):
        """workflow 已被 `delete_workflow` 完整清理 → subscribe 抛 WorkflowError。

        这是真正竞态：state 内存和磁盘 JSON 都不存在。如果只清理内存但
        磁盘仍在，sse 重连可以接受（state 可恢复）。
        """
        wid = self.mgr.start("x", project_id=self.pid,
                              project_root=str(self.project))["workflow_id"]
        # 把 state 推进到终态后删除
        state = self.mgr._load(wid)
        state.status = "completed"
        self.mgr._save(state)
        self.mgr.delete_workflow(wid)
        # 验证内存和磁盘都没了
        self.assertIsNone(self.mgr._states.get(wid))
        self.assertFalse(self.mgr._path(wid).is_file())
        with self.assertRaises(WorkflowError):
            self.mgr.subscribe(wid)

    def test_subscribe_after_memory_evicted_but_disk_persists_keeps_queue(self):
        """state 在内存不在但磁盘 JSON 仍存在 → subscribe 仍允许（重连可恢复）。

        注意：`unsubscribe` 会在最后一个 queue 移除时把 `_subscribers[wid]`
        字典项一起 pop 掉（现有契约）；因此断言改成"成功注册过 + delete
        后不再泄漏"二者结合。
        """
        wid = self.mgr.start("x", project_id=self.pid,
                              project_root=str(self.project))["workflow_id"]
        with self.mgr._lock:
            self.mgr._states.pop(wid, None)
        # 磁盘 JSON 仍在：subscribe 不应抛
        q1 = self.mgr.subscribe(wid)
        q2 = self.mgr.subscribe(wid)
        self.assertEqual(len(self.mgr._subscribers.get(wid, set())), 2)
        # unsubscribe 仍走正常清理
        self.mgr.unsubscribe(wid, q1)
        self.assertEqual(len(self.mgr._subscribers.get(wid, set())), 1)
        self.mgr.unsubscribe(wid, q2)
        self.assertNotIn(wid, self.mgr._subscribers)
        # delete_workflow 终态后才允许；先转终态
        state = self.mgr._load(wid)
        state.status = "completed"
        self.mgr._save(state)
        self.mgr.delete_workflow(wid)
        self.assertNotIn(wid, self.mgr._subscribers)

    def test_sse_endpoint_returns_404_when_workflow_removed_between_get_and_subscribe(self):
        """SSE handler 在 state 被删时返回 404，不进入 stream 循环。"""
        wid = self.mgr.start("x", project_id=self.pid,
                              project_root=str(self.project))["workflow_id"]
        # 注入 subscribe 抛错，模拟竞态
        import api_routes.agent as api_agent
        original_subscribe = api_agent.WORKFLOWS.subscribe

        def boom(_wid):
            raise WorkflowError("工作流不存在：%s" % _wid)

        client = _build_client(self, self.mgr, str(self.project), self.pid)
        api_agent.WORKFLOWS.subscribe = boom
        try:
            resp = client.get(f"/api/agent/workflow/{wid}/events")
            self.assertEqual(resp.status_code, 404, resp.text)
        finally:
            api_agent.WORKFLOWS.subscribe = original_subscribe

    def test_delete_workflow_drops_subscribers_atomically(self):
        """delete_workflow 一次性把 subscribers set 一起 pop 掉。"""
        wid = self.mgr.start("x", project_id=self.pid,
                              project_root=str(self.project))["workflow_id"]
        q1 = self.mgr.subscribe(wid)
        q2 = self.mgr.subscribe(wid)
        self.assertEqual(len(self.mgr._subscribers.get(wid, set())), 2)
        state = self.mgr._load(wid)
        state.status = "completed"
        self.mgr._save(state)
        result = self.mgr.delete_workflow(wid)
        self.assertTrue(result["ok"])
        self.assertNotIn(wid, self.mgr._subscribers)
        # 同时清理 q1/q2 是调用方责任（unsub），但 delete 后不再泄漏
        self.assertEqual(self.mgr._subscribers, {})


if __name__ == "__main__":
    unittest.main()
