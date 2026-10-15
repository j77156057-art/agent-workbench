"""`list_workflows` / `active_for_project` 改 AND + Windows 路径归一。

之前两个方法用 OR 匹配：`state.project_root == project_root or
state.project_id == project_id`。混绑 state（A 项目 id + B 项目 root）会
被任意一边命中 → 跨项目可见。

新契约：所有**已提供**的绑定条件必须全部匹配（AND）；路径走
`os.path.normcase(normpath(...))` 归一，避免 Windows 大小写/尾斜杠
/symlink 绕过。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.game_workflow import GameWorkflowManager


class ProjectListAndMatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project_a = self.tmp / "project_a"
        self.project_b = self.tmp / "project_b"
        self.project_a.mkdir()
        self.project_b.mkdir()
        self.pid_a = "pid-a"
        self.pid_b = "pid-b"
        self.mgr = GameWorkflowManager(str(self.tmp / "_state"))

    def tearDown(self):
        self.mgr.close()

    # ----- list_workflows 改 AND --------------------------------------

    def test_mixed_binding_state_is_not_returned_to_either_project(self):
        """混绑 state（A id + B root）：A 项目 (pid_a, root_a) 看不到，B 也看不到。"""
        mixed_wid = self.mgr.start("混绑",
                                     project_id=self.pid_a,
                                     project_root=str(self.project_b))["workflow_id"]
        # A 项目查询
        items_a = self.mgr.list_workflows(self.pid_a, str(self.project_a))
        # B 项目查询
        items_b = self.mgr.list_workflows(self.pid_b, str(self.project_b))
        ids_a = [item["workflow_id"] for item in items_a]
        ids_b = [item["workflow_id"] for item in items_b]
        self.assertNotIn(mixed_wid, ids_a,
                          f"混绑 state 不应被 A 项目看见：{ids_a}")
        self.assertNotIn(mixed_wid, ids_b,
                          f"混绑 state 不应被 B 项目看见：{ids_b}")

    def test_consistent_state_is_returned(self):
        """一致 state：同 pid+root 应当能查到。"""
        wid = self.mgr.start("一致", project_id=self.pid_a,
                              project_root=str(self.project_a))["workflow_id"]
        items = self.mgr.list_workflows(self.pid_a, str(self.project_a))
        self.assertIn(wid, [item["workflow_id"] for item in items])

    def test_only_pid_provided_root_matches(self):
        """只传 pid（root 留空）→ 该 pid 的所有 root state 都返回。

        2026-10-15 修正：本条曾经的断言与它自己的 docstring 相反——旧写法
        `(not want_root and not raw_root)` 把「没问 root」当成「state 必须
        没有 root」，只传 pid 时任何带 root 的 state 都被滤掉。AND 语义约束
        的是【已提供】的条件，没提供的维度是通配符。混绑保护不受影响：
        那条查询两个条件都给了，root 不匹配照样滤掉（见上一条用例）。
        """
        wid = self.mgr.start("A", project_id=self.pid_a,
                              project_root=str(self.project_a))["workflow_id"]
        items = self.mgr.list_workflows(self.pid_a, "")
        self.assertIn(wid, [item["workflow_id"] for item in items],
                      "只传 pid 时该 pid 的 state 应返回（未提供的 root 是通配符）")

    def test_empty_filters_returns_all(self):
        """两者皆空时返回全部工作流（与 active_for_project 同口径）。"""
        wid_a = self.mgr.start("A", project_id=self.pid_a,
                                project_root=str(self.project_a))["workflow_id"]
        wid_b = self.mgr.start("B", project_id=self.pid_b,
                                project_root=str(self.project_b))["workflow_id"]
        items = self.mgr.list_workflows("", "")
        ids = [item["workflow_id"] for item in items]
        self.assertIn(wid_a, ids)
        self.assertIn(wid_b, ids)

    # ----- active_for_project 改 AND --------------------------------

    def test_mixed_binding_not_active_for_either_project(self):
        """混绑 state 不应是 A 或 B 的活跃工作流。"""
        # 把混绑 state 启动后，尝试 set 到非终态（start 默认是 planning），
        # 然后 A / B 任一项目都不应当看到它。
        self.mgr.start("混绑", project_id=self.pid_a,
                        project_root=str(self.project_b))
        active_a = self.mgr.active_for_project(self.pid_a, str(self.project_a))
        active_b = self.mgr.active_for_project(self.pid_b, str(self.project_b))
        self.assertIsNone(active_a, f"A 不应看到混绑 active: {active_a}")
        self.assertIsNone(active_b, f"B 不应看到混绑 active: {active_b}")

    def test_consistent_active_visible(self):
        """一致 state：A 项目可作为活跃工作流查到。"""
        self.mgr.start("一致", project_id=self.pid_a,
                        project_root=str(self.project_a))
        active = self.mgr.active_for_project(self.pid_a, str(self.project_a))
        self.assertIsNotNone(active)
        self.assertEqual(active["project_id"], self.pid_a)


class WindowsPathNormalizationTests(unittest.TestCase):
    """路径大小写/尾斜杠差异不应绕过 list / active 匹配。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project = self.tmp / "MyProject"
        self.project.mkdir()
        self.mgr = GameWorkflowManager(str(self.tmp / "_state"))

    def tearDown(self):
        self.mgr.close()

    def test_list_workflows_matches_case_insensitive(self):
        """用大写盘符/不同大小写查同一项目 → 应当匹配。"""
        self.mgr.start("X", project_id="pid-x",
                        project_root=str(self.project))
        canonical = str(self.project)
        from agent_runtime.game_workflow import _canonical_project_root
        # 同目录不同表示
        variants = [
            canonical,
            canonical + "/",
            canonical + "\\",
            canonical.upper() if os.name == "nt" else canonical,
        ]
        seen = set()
        for v in variants:
            items = self.mgr.list_workflows("pid-x", v)
            seen.update(item["workflow_id"] for item in items)
        # 至少应当稳定看到 X
        self.assertEqual(len(seen), 1,
                          f"路径变体应当都映射到同一份列表：{seen}")

    def test_canonical_project_root_lowercases_on_windows(self):
        from agent_runtime.game_workflow import _canonical_project_root
        canonical = str(self.project)
        norm = _canonical_project_root(canonical)
        if os.name == "nt":
            self.assertEqual(norm, norm.lower())
        self.assertEqual(norm, _canonical_project_root(canonical + "/"))


if __name__ == "__main__":
    unittest.main()
