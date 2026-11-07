"""store 真实语义分层与真实边出口（2026-11-04）。

覆盖：
- store→layer_key 映射（按 store 真实语义，逐 store 断言）与中文层名
- 亮度系数按内容稳定度单调（rule > shared > legacy > project > hook）
- 旧编号只作为历史 read 别名，不再对外输出
- 未知 store 回落到配置的 fallback
- 索引 edges 表只读导出 / 文件缺失 / 端点缺失不补合成边
- view 在 edges 为空时仍返回完整载荷（图可渲染“无边状态”）
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_runtime.memory import index, inspector, layers


class LayerMappingTests(unittest.TestCase):
    def test_store_mapping_follows_store_semantics(self):
        expected = {"system": "rule", "shared": "shared", "legacy": "legacy",
                    "project": "project", "hook": "hook"}
        self.assertEqual(layers.STORE_LAYER, expected)
        for store, layer_key in expected.items():
            self.assertEqual(layers.layer_for_store(store), layer_key)

    def test_labels_cover_every_layer(self):
        self.assertEqual(layers.LAYER_ORDER, ("rule", "shared", "legacy", "project", "hook"))
        for key in layers.LAYER_ORDER:
            self.assertTrue(layers.LAYER_LABELS[key])

    def test_brightness_is_strictly_decreasing_by_stability(self):
        values = [layers.LAYER_BRIGHTNESS[key] for key in layers.LAYER_ORDER]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertGreater(values[0], values[-1])
        self.assertTrue(all(0 < value <= 1 for value in values))
        self.assertIn("内容稳定度", layers.BRIGHTNESS_BASIS)

    def test_injection_boost_does_not_flatten_layers(self):
        for key in ("shared", "legacy", "project", "hook"):
            self.assertLess(layers.LAYER_BRIGHTNESS[key] * layers.INJECTED_BOOST, 1.0, key)

    def test_legacy_numbers_are_read_aliases_only(self):
        self.assertEqual(layers.normalize_layer("L0"), "rule")
        self.assertEqual(layers.normalize_layer("L2"), "shared")
        self.assertEqual(layers.normalize_layer("shared"), "shared")
        self.assertNotIn("L0", layers.LAYER_ORDER)

    def test_unknown_store_falls_back_to_configured_layer(self):
        self.assertEqual(layers.layer_for_store("mystery"), layers.FALLBACK_LAYER)
        self.assertEqual(layers.layer_for_store(""), layers.FALLBACK_LAYER)

    def test_config_marks_pending_confirmation_and_basis(self):
        config = layers.config()
        self.assertFalse(config["calibrated"])
        self.assertIn("待确认", config["pending_confirmation"])
        self.assertIn("内容稳定度", config["brightness_basis"])
        self.assertEqual(config["misfits"], [])

    def test_every_store_node_carries_expected_layer(self):
        for store in ("legacy", "shared", "project", "hook"):
            node = inspector._node({}, store)
            self.assertEqual(node["layer_key"], layers.STORE_LAYER[store], store)
            self.assertEqual(node["layer_label"], layers.LAYER_LABELS[store], store)
            self.assertFalse(node["layer_fallback"], store)

    def test_unmapped_store_node_is_flagged_as_fallback(self):
        node = inspector._node({}, "mystery")
        self.assertEqual(node["layer_key"], layers.FALLBACK_LAYER)
        self.assertTrue(node["layer_fallback"])


class EdgeReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_edges_")
        self._old = os.environ.get("DOCMIND_MEMORY_DIR")
        os.environ["DOCMIND_MEMORY_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DOCMIND_MEMORY_DIR", None)
        else:
            os.environ["DOCMIND_MEMORY_DIR"] = self._old

    def _make_index(self):
        target = os.path.join(self.tmp, "shared", "index.sqlite3")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        conn = sqlite3.connect(target)
        conn.executescript(index.SCHEMA)
        conn.execute("INSERT INTO items(id, scope, status) VALUES('mem-a', 'global', 'active')")
        conn.execute("INSERT INTO items(id, scope, status) VALUES('mem-b', 'project', 'active')")
        conn.execute("INSERT INTO items(id, scope, status) VALUES('mem-gone', 'project', 'superseded')")
        conn.execute("INSERT INTO edges VALUES('mem-a', 'mem-b', 'supersedes')")
        conn.execute("INSERT INTO edges VALUES('mem-b', 'mem-gone', 'supersedes')")
        conn.commit()
        conn.close()

    def test_missing_index_returns_empty(self):
        self.assertEqual(index.read_edges(), [])

    def test_read_edges_includes_scopes(self):
        self._make_index()
        rows = index.read_edges()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["src"], "mem-a")
        self.assertEqual(rows[0]["src_scope"], "global")
        self.assertEqual(rows[0]["dst_scope"], "project")

    def test_edges_resolve_to_node_keys_and_skip_missing_endpoints(self):
        self._make_index()
        memories = [{"store": "shared", "id": "mem-a"}, {"store": "project", "id": "mem-b"}]
        edges, stats = inspector._edges(memories)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["source"], "shared:mem-a")
        self.assertEqual(edges[0]["target"], "project:mem-b")
        self.assertIsNone(edges[0]["weight"])
        self.assertEqual(stats["in_table"], 2)
        self.assertEqual(stats["resolved"], 1)
        self.assertEqual(stats["unresolved"], 1)
        self.assertFalse(stats["weight_available"])

    def test_view_still_renders_when_edges_are_empty(self):
        class FakeAgent:
            memory_scope = "legacy-scope"
            history = []
            _last_memory_snapshot = None

        self._make_index()
        node = inspector._node({"id": "legacy-1", "title": "旧记忆"}, "legacy")
        with patch.object(inspector, "_catalog", return_value=[node]):
            payload = inspector.view(FakeAgent(), project="p", session="s")
        self.assertEqual(payload["edges"], [])
        self.assertTrue(payload["edge_stats"]["readable"])
        self.assertEqual(payload["layer_config"]["store_map"]["project"], "project")
        self.assertEqual(payload["memories"][0]["layer_key"], "legacy")
        self.assertEqual(payload["hub"]["layer_key"], "rule")


if __name__ == "__main__":
    unittest.main()
