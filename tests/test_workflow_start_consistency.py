"""`launch_workflow` 启动时 `project_id` / `project_root` 一致性校验。

之前 `launch_workflow` 允许显式 `project_id` 跟 `ctx` 当前根反查的项目不一致：
- 调用方传 `project_id=B`，但 ctx 给出 `root=A`，工作流 state 被写入
  `(project_id=B, project_root=A)` 混绑；
- 后续 `list_workflows` / `active_for_project` 用 OR 匹配 → A、B 任一
  项目都看得到这条 workflow。

校验后：调用方传的 `project_id`、请求头反查、`root` 反查三者必须一致。
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

import projects
import api_routes.agent as api_agent


def _client(root, request_pid):
    """构造一个 client：ctx 给出当前 `root` 与 `request_pid`。"""
    class _Ctx:
        def _project_root_or_error(self_inner):
            return root

        def _request_project_id(self_inner):
            return request_pid

    app = FastAPI()
    app.include_router(api_agent.build_router(_Ctx()))
    return TestClient(app)


class LaunchWorkflowConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project_a = self.tmp / "project_a"
        self.project_a.mkdir()
        self.pid_a = projects.project_id(str(self.project_a))
        # 替 WORKFLOWS 用 stub，避免启动后做实际工作
        self._orig = api_agent.WORKFLOWS
        from agent_runtime.game_workflow import GameWorkflowManager
        api_agent.WORKFLOWS = GameWorkflowManager(str(self.tmp / "_state"))

    def tearDown(self):
        api_agent.WORKFLOWS.close()
        api_agent.WORKFLOWS = self._orig

    def test_inconsistent_project_id_against_request_header_rejected(self):
        """调用方 `project_id` 跟请求头（ctx 解析的）不一致 → 拒绝。"""
        # ctx 说自己属于 pid_a，但请求体 project_id 写别的
        client = _client(str(self.project_a), self.pid_a)
        resp = client.post("/api/agent/workflow/start",
                            json={"prompt": "x", "kind": "generic",
                                  "project_id": "prj-deadbeefdead"})
        body = resp.json()
        self.assertFalse(body.get("ok"), resp.text)
        self.assertIn("project_id 不一致", str(body.get("error", "")))

    def test_project_id_mismatching_root_rejected(self):
        """调用方省略 `project_id`，但根反查得到的项目 id 跟 request_pid 不一致 → 拒绝。"""
        # 这里让 ctx 自己给的 request_pid 与 ctx 的根反查 project_id 不一致
        # 不可能（两者应同源），但可以构造：ctx request_pid 传 A 项目的 id，
        # ctx 的根是 B 项目；调用方省略 project_id，让 effective_pid=request_pid=A
        # 走根反查得到 B，不匹配。
        other = self.tmp / "other"
        other.mkdir()
        other_pid = projects.project_id(str(other))
        # 用 self.pid_a 作 request_pid，但根是 other
        client = _client(str(other), self.pid_a)
        resp = client.post("/api/agent/workflow/start",
                            json={"prompt": "x", "kind": "generic"})
        body = resp.json()
        self.assertFalse(body.get("ok"), resp.text)
        self.assertIn("不匹配", str(body.get("error", "")))

    def test_consistent_setup_passes_validation(self):
        """调用方 `project_id` 与根反查、request_pid 三者一致 → 通过校验。

        这里「通过校验」指：不会因一致性失败返回 4xx。启动后可能因 LLM 等
        其他原因失败。
        """
        client = _client(str(self.project_a), self.pid_a)
        resp = client.post("/api/agent/workflow/start",
                            json={"prompt": "x", "kind": "generic",
                                  "project_id": self.pid_a})
        body = resp.json()
        if not body.get("ok"):
            # 不应该是因为一致性失败
            msg = str(body.get("error", ""))
            self.assertNotIn("project_id 不一致", msg)
            self.assertNotIn("project_id 与 project_root 不匹配", msg)


if __name__ == "__main__":
    unittest.main()
