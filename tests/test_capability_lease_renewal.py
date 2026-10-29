# -*- coding: utf-8 -*-
"""长工作流的能力租约必须滑动续期，不能 30 分钟到点就把工具全拦死。

工作流动辄跑几小时；租约原本只在 execute/retry 入口创建，`expires_at_epoch`
固定。这里锁住：临期续期保留租约 id/能力集、已释放/终态不复活、自动重建不升权，
以及 Agent 的租约检查会调用执行器注入的续期器。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import Agent  # noqa: E402
from agent_runtime.game_workflow import GameWorkflowManager  # noqa: E402


class _FakeSpec:
    capability = "read_file"


def _bare_agent(lease, renew=None):
    obj = object.__new__(Agent)
    obj.capability_lease = dict(lease)
    obj.capability_lease_renew = renew
    obj._tool_spec = lambda _name: _FakeSpec()
    return obj


class LeaseRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project = self.tmp / "p"
        self.project.mkdir()
        self.mgr = GameWorkflowManager(str(self.tmp / "_state"))
        self.wid = self.mgr.start("长任务", project_root=str(self.project),
                                  llm_enabled=False)["workflow_id"]

    def tearDown(self):
        self.mgr.close()

    def test_refresh_extends_same_lease_and_keeps_capabilities(self):
        lease = self.mgr.create_capability_lease(
            self.wid, ttl_seconds=600, capabilities=["read_file"])
        old_expiry = float(lease["expires_at_epoch"])
        time.sleep(0.01)
        fresh = self.mgr.refresh_capability_lease(self.wid, ttl_seconds=1200)
        self.assertEqual(fresh["id"], lease["id"])
        self.assertEqual(fresh["capabilities"], ["read_file"])
        self.assertGreater(float(fresh["expires_at_epoch"]), old_expiry)

    def test_released_lease_is_not_revived(self):
        self.mgr.create_capability_lease(self.wid, capabilities=["read_file"])
        state = self.mgr._load(self.wid)
        state.capability_lease["status"] = "released"
        self.mgr._save(state)
        fresh = self.mgr.refresh_capability_lease(self.wid)
        self.assertEqual(fresh.get("status"), "released")

    def test_terminal_workflow_does_not_renew(self):
        self.mgr.create_capability_lease(self.wid, capabilities=["read_file"])
        state = self.mgr._load(self.wid)
        state.status = "completed"
        self.mgr._save(state)
        fresh = self.mgr.refresh_capability_lease(self.wid)
        self.assertEqual(fresh.get("status"), "released")

    def test_auto_rearm_does_not_escalate_capabilities(self):
        """过期后自动重建（重试/续跑）沿用旧能力集，不能升成全能力。"""
        self.mgr.create_capability_lease(self.wid, capabilities=["read_file"])
        state = self.mgr._load(self.wid)
        state.capability_lease["expires_at_epoch"] = 0
        self.mgr._save(state)
        lease = self.mgr._ensure_capability_lease(state)
        self.assertEqual(lease["capabilities"], ["read_file"])


class AgentLeaseCheckTests(unittest.TestCase):
    def test_near_expiry_renews_and_allows(self):
        renew_calls = []

        def renew():
            renew_calls.append(1)
            return {"id": "lease-x", "status": "active",
                    "expires_at_epoch": time.time() + 600,
                    "capabilities": ["read_file"]}

        obj = _bare_agent({"id": "lease-x", "status": "active",
                           "expires_at_epoch": time.time() + 10,
                           "capabilities": ["read_file"]}, renew)
        blocked, reason = Agent._lease_blocked(obj, "read_file")
        self.assertFalse(blocked, reason)
        self.assertEqual(len(renew_calls), 1)
        self.assertGreater(obj.capability_lease["expires_at_epoch"], time.time() + 500)

    def test_released_lease_is_blocked_without_renewing(self):
        calls = []
        obj = _bare_agent({"status": "released", "expires_at_epoch": 0,
                           "capabilities": ["read_file"]}, lambda: calls.append(1) or {})
        blocked, _ = Agent._lease_blocked(obj, "read_file")
        self.assertTrue(blocked)
        self.assertEqual(calls, [])

    def test_failed_renewal_keeps_blocking(self):
        obj = _bare_agent({"status": "active", "expires_at_epoch": time.time() - 1,
                           "capabilities": ["read_file"]}, lambda: {})
        blocked, _ = Agent._lease_blocked(obj, "read_file")
        self.assertTrue(blocked)

    def test_wiring_is_present(self):
        runner = (ROOT / "api_routes/agent.py").read_text(encoding="utf-8")
        self.assertIn("agent.capability_lease_renew", runner)
        self.assertIn("refresh_capability_lease", runner)
        manager = (ROOT / "agent_runtime/game_workflow.py").read_text(encoding="utf-8")
        self.assertIn("self.refresh_capability_lease(workflow_id)", manager)
        agent_src = (ROOT / "agent.py").read_text(encoding="utf-8")
        self.assertIn("child.capability_lease_renew", agent_src)


if __name__ == "__main__":
    unittest.main()
