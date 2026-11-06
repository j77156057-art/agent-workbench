"""委派额度口径的回归用例：结束的节点必须释放额度，在执节点仍受上限约束。

对应改动 a5abdb4：`request()` 的 total / siblings 只统计
pending / running / waiting_child，completed / failed / expired / rejected /
unknown 不再占额度。临时脚本的证据在此固化为可重复执行的用例。
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from agent_runtime import delegation


class _Parent:
    """只提供 classify/request 需要的字段，不构造真实 Agent。"""

    def __init__(self, depth=0, session="quota-parent"):
        self.memory_session_id = session
        self.session_id = session
        self.project_id = "p"
        self.depth = depth
        self.agent_id = "main"
        self.tools = set()
        self.capability_lease = None


class QuotaReleaseTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {
            "DOCMIND_DELEGATION_MAX_DEPTH": "4",
            "DOCMIND_DELEGATION_MAX_BRANCHES": "12",
            "DOCMIND_DELEGATION_MAX_CHILDREN": "4",
        })
        self._env.start()
        delegation.clear("quota-sequential")
        delegation.clear("quota-concurrent")

    def tearDown(self):
        delegation.clear("quota-sequential")
        delegation.clear("quota-concurrent")
        self._env.stop()

    def test_sequential_completed_dispatches_never_exhaust_quota(self):
        """顺序派发并立即完成 20 次：全部直接放行，不产生审批。"""
        parent = _Parent(depth=0, session="quota-sequential")
        for index in range(20):
            request = delegation.request(
                parent, role="researcher", task="顺序任务 %d" % index,
                session_id="quota-sequential", project_id="p", effective_tools=[],
                run=lambda: {"status": "ok", "conclusion": "完成", "steps": 1})
            self.assertFalse(
                request["approval_required"],
                "第 %d 次顺序派发被要求审批：%r" % (index + 1, request["approval_reasons"]))
            result = delegation.execute("quota-sequential", request["id"])
            self.assertEqual(result["status"], "ok")

        tree = delegation.tree("quota-sequential")
        holding = [node for node in tree["nodes"]
                   if node["status"] in delegation._QUOTA_HOLDING_STATUSES]
        self.assertEqual(holding, [], "已完成的节点不应继续占用额度")
        self.assertEqual(tree["pending"], [])

    def test_concurrent_pending_dispatches_still_hit_the_caps(self):
        """只登记不执行：第 5 个起命中单父上限 4，第 13 个起同时命中总分支上限 12。"""
        parent = _Parent(depth=1, session="quota-concurrent")  # depth 2 → 必进审批，节点保持 pending
        for index in range(15):
            request = delegation.request(
                parent, role="researcher", task="并发任务 %d" % index,
                session_id="quota-concurrent", project_id="p", effective_tools=[],
                run=lambda: {"status": "ok", "conclusion": "不应执行"})
            self.assertIn("跨层递归派发", request["approval_reasons"])
            self.assertEqual(request["status"], "pending")
            reasons = list(request["limit_reasons"])
            if index < 4:
                self.assertEqual(reasons, [], "前 4 个并发节点不该触发上限")
            elif index < 12:
                self.assertIn("单父分支 %d 超过默认上限 4" % (index + 1), reasons)
                self.assertNotIn("总分支 %d 超过默认上限 12" % (index + 1), reasons)
            else:
                self.assertIn("单父分支 %d 超过默认上限 4" % (index + 1), reasons)
                self.assertIn("总分支 %d 超过默认上限 12" % (index + 1), reasons)

        tree = delegation.tree("quota-concurrent")
        self.assertEqual(len(tree["pending"]), 15, "只登记不执行的节点应全部保持 pending")
        self.assertEqual(len(tree["nodes"]) - 1, 15)  # 减去合成根 main


if __name__ == "__main__":
    unittest.main()
