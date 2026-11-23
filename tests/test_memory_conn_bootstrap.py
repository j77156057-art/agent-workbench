"""记忆库连接与 frontmatter 解析的回归测试。

背景（2026-11-23 的后端体检）：/api/memory/context 每次开面板都要开 4 次数据库连接，
而 _connect() 每次都重跑整套建表语句 + 无条件全表 UPDATE —— 单次 5.4ms，且每次连接都
抢一次库文件的写锁。改成「先廉价校验、缺了才引导」后单次降到 0.97ms。

这里锁住三件事：
1. 引导路径仍然正确 —— 新库建表、老库补列、老库补索引都不许退化；
2. 没有遗留归属行时**不许再发 UPDATE**（不许拿写锁换零收益）；
3. frontmatter 解析换 libyaml 之后语义不变，且没有 libyaml 时能回退。
"""
import sqlite3
import unittest
from unittest import mock

import agent_memory as memory
from agent_runtime.memory import store
from tests.test_agent_trace import _IsoBase


class MemoryConnBootstrapTests(_IsoBase):
    """_connect() 的引导与守卫。"""

    def _indexes(self):
        conn = sqlite3.connect(memory.DB_PATH)
        try:
            return {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        finally:
            conn.close()

    def _columns(self, table):
        conn = sqlite3.connect(memory.DB_PATH)
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        finally:
            conn.close()

    def test_fresh_db_creates_schema_and_agent_index(self):
        """新库一次连接就建齐表和索引。"""
        memory._connect()
        self.assertIn("idx_memories_agent_id", self._indexes())
        for table in ("memories", "checkpoints", "user_profiles"):
            self.assertTrue(self._columns(table), f"{table} 没建出来")

    def test_existing_db_without_index_gets_it_back(self):
        """老库升级路径：表在、索引缺 —— 连接时补建索引。"""
        memory._connect()
        conn = sqlite3.connect(memory.DB_PATH)
        conn.execute("DROP INDEX IF EXISTS idx_memories_agent_id")
        conn.commit()
        conn.close()
        self.assertNotIn("idx_memories_agent_id", self._indexes())
        memory._connect()
        self.assertIn("idx_memories_agent_id", self._indexes())

    def test_legacy_db_without_columns_still_gets_them(self):
        """老库缺列 —— 补列路径不许因为「表在就跳过建表」而失效。"""
        conn = sqlite3.connect(memory.DB_PATH)
        conn.execute("CREATE TABLE memories (scope TEXT, id TEXT, kind TEXT, title TEXT,"
                     " content TEXT, evidence TEXT, source TEXT, repeats INTEGER,"
                     " updated TEXT, PRIMARY KEY(scope, id))")
        conn.commit()
        conn.close()
        memory._connect()
        cols = self._columns("memories")
        for column in ("status", "supersedes", "agent_id", "agent_legacy", "backed",
                       "importance", "emb", "emb_dim", "access_count", "last_accessed"):
            self.assertIn(column, cols, f"老库没补上列 {column}")

    def test_connect_without_legacy_rows_issues_no_write(self):
        """没有遗留归属行时，连接不许再发 UPDATE（那是一次纯写锁开销）。"""
        memory.remember("p", "fact", "标题", "正文")
        seen = []
        real_connect = sqlite3.connect

        def spy(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(seen.append)
            return conn

        with mock.patch("sqlite3.connect", side_effect=spy):
            memory._connect()
        writes = [s for s in seen if s.lstrip().upper().startswith("UPDATE")]
        self.assertEqual(writes, [], f"没有遗留行却还是写了库：{writes}")

    def test_legacy_row_is_backfilled_to_main(self):
        """有遗留归属行时，回填照旧发生（这是老实现写死的不变量）。"""
        memory.remember("p", "fact", "历史行", "正文", agent_id="")
        memory._connect()  # 回填发生在**下一次**连接（老实现就是这个语义）
        conn = sqlite3.connect(memory.DB_PATH)
        try:
            rows = conn.execute("SELECT agent_id, agent_legacy FROM memories").fetchall()
        finally:
            conn.close()
        self.assertEqual([tuple(r) for r in rows], [("main", 1)])


class FrontmatterParserTests(unittest.TestCase):
    """frontmatter 解析：换了 libyaml 之后语义必须一样。"""

    def test_parses_header_and_body(self):
        header, body = store.split_frontmatter("---\ntitle: 标题\nnum: 3\n---\n正文\n")
        self.assertEqual(header, {"title": "标题", "num": 3})
        self.assertEqual(body.strip(), "正文")

    def test_broken_yaml_raises_value_error(self):
        with self.assertRaises(ValueError):
            store.split_frontmatter("---\nbroken: [1, 2\n---\n正文\n")

    def test_falls_back_to_pure_python_without_libyaml(self):
        """装不上 libyaml 的机器上必须照常能解析（行为不变）。"""
        old = store._YAML_LOADER
        store._YAML_LOADER = None
        try:
            header, body = store.split_frontmatter("---\ntitle: 标题\n---\n正文\n")
        finally:
            store._YAML_LOADER = old
        self.assertEqual(header, {"title": "标题"})
        self.assertEqual(body.strip(), "正文")


if __name__ == "__main__":
    unittest.main()
