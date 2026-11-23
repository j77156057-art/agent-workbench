"""Postgres checkpointer 的**构造期**契约（不依赖真实数据库）。

背景：把 checkpointer 整体推迟到「第一次真正需要时」时，踩坏了一个既有契约 ——
调用方（租约栅栏、集成用例）会在 `GameWorkflowManager(...)` 构造完之后**立刻**
读 `manager._checkpoint_pool`。如果池是后来才建的，它用的就是**后来的**环境变量
（例如池大小），配置就错了。

所以约定：
- 配了 DSN  → **构造期**就建好 pool + saver（池大小等 env 在构造那一刻生效）。
- 没配 DSN  → 保持惰性（启动期不付 LangGraph 的导入成本）。

这里用假 pool / 假 saver 把契约钉住：不需要真的 Postgres。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from agent_runtime.game_workflow import GameWorkflowManager


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        low = str(sql).lower()
        self._row = (1,) if "select version from docmind_checkpoint_schema" in low else None

    def fetchone(self):
        return self._row


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def cursor(self):
        return _FakeCursor()


class _FakePool:
    """只记录构造参数 —— 契约要验的正是「构造那一刻读到的配置」。"""

    def __init__(self, conninfo=None, min_size=1, max_size=4, kwargs=None,
                 timeout=30, open=False):
        self.conninfo = conninfo
        self.min_size = min_size
        self.max_size = max_size
        self.timeout = timeout
        self.closed = False

    def connection(self, timeout=None):
        return _FakeConn()

    def close(self):
        self.closed = True


class _FakeSaver:
    def __init__(self, pool):
        self.pool = pool

    def setup(self):
        return None


class PostgresCheckpointerContractTests(unittest.TestCase):
    def test_pool_is_built_at_construction_with_current_env(self):
        base_env = {
            "DOCMIND_CHECKPOINT_POOL_SIZE": "1",
            "DOCMIND_CHECKPOINT_POOL_TIMEOUT_S": "0.2",
        }
        with patch("agent_runtime.game_workflow._load_langgraph"), \
                patch("agent_runtime.game_workflow.ConnectionPool", _FakePool), \
                patch("agent_runtime.game_workflow.PostgresSaver", _FakeSaver), \
                patch.dict(os.environ, base_env, clear=False):
            manager = GameWorkflowManager(tempfile.mkdtemp(),
                                         checkpoint_dsn="postgresql://contract")
        try:
            pool = manager._checkpoint_pool
            self.assertIsNotNone(pool, "构造完必须立刻能拿到 pool（既有契约）")
            self.assertEqual(pool.max_size, 1, "池大小取构造那一刻的 env")
            self.assertEqual(pool.timeout, 0.2, "池超时取构造那一刻的 env")
            self.assertEqual(manager.checkpoint_backend, "langgraph-postgres")
            self.assertTrue(manager.distributed_leases)
        finally:
            manager.close()

    def test_strict_mode_raises_at_construction_when_dependencies_missing(self):
        with patch("agent_runtime.game_workflow._load_langgraph"), \
                patch("agent_runtime.game_workflow.ConnectionPool", None), \
                patch("agent_runtime.game_workflow.PostgresSaver", None), \
                patch.dict(os.environ,
                           {"DOCMIND_CHECKPOINT_POSTGRES_REQUIRED": "1"}, clear=False):
            with self.assertRaises(RuntimeError):
                GameWorkflowManager(tempfile.mkdtemp(),
                                   checkpoint_dsn="postgresql://required")

    def test_without_dsn_pool_stays_lazy(self):
        with patch("agent_runtime.game_workflow._load_langgraph"), \
                patch("agent_runtime.game_workflow.ConnectionPool", _FakePool), \
                patch("agent_runtime.game_workflow.PostgresSaver", _FakeSaver), \
                patch.dict(os.environ,
                           {"DOCMIND_CHECKPOINT_POSTGRES_REQUIRED": "0"}, clear=False):
            manager = GameWorkflowManager(tempfile.mkdtemp())
        try:
            self.assertIsNone(manager._checkpoint_pool,
                              "没配 DSN 时不该在构造期建池")
            self.assertFalse(manager.checkpoint_error)
            self.assertIn(manager.checkpoint_backend,
                          {"langgraph-sqlite", "langgraph-memory", "native"})
        finally:
            manager.close()

    def test_failed_postgres_setup_keeps_diagnostic_and_falls_back(self):
        with patch("agent_runtime.game_workflow._load_langgraph"), \
                patch("agent_runtime.game_workflow.ConnectionPool", None), \
                patch("agent_runtime.game_workflow.PostgresSaver", None), \
                patch.dict(os.environ,
                           {"DOCMIND_CHECKPOINT_POSTGRES_REQUIRED": "0"}, clear=False):
            manager = GameWorkflowManager(tempfile.mkdtemp(),
                                          checkpoint_dsn="postgresql://invalid")
        try:
            self.assertTrue(manager.checkpoint_error,
                            "Postgres 不可用时必须留下诊断")
            self.assertIn(manager.checkpoint_backend,
                          {"langgraph-sqlite", "langgraph-postgres"})
            self.assertTrue(manager.persistent_checkpoint)
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
